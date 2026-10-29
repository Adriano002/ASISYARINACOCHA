# app.py - Sistema de Asistencia I.E. Yarinacocha
import hashlib
import logging
import re
import secrets
import time
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path

import pandas as pd
import streamlit as st
from db import (escribir, leer_df, cursor, to_time, to_date, to_ts,
                fmt_time, fmt_date, fmt_ts)
from schema import aplicar_schema
from qr_scanner_component import qr_scanner

try:
    from streamlit_autorefresh import st_autorefresh
except ImportError:
    def st_autorefresh(**kwargs):
        pass
LOG_DIR = Path("logs")
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("asistencia")


MESES_ES = ["", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
            "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"]

C_PRIMARIO = "#B85C38"
PBK_ITER = 260_000
PBK_ALG = "sha256"
MAX_INTENTOS = 3
MIN_BLOQUEO = 10

PUNTUAL = "Puntual"
TARDANZA = "Tardanza"
FALTA = "Falta"
PERMISO = "Permiso"
REF_ASISTIO = "Asistio"
ACC_PERDONADO = "PERDONADO"
ACC_DERIVADO = "DERIVADO_DIRECCION"

VENT_CLASES = "clases"
VENT_REF = "reforzamiento"
TIPO_ASIST_EVENTO = "evento"

MAX_DIAS_PERMISO = 7
HORAS_CIERRE_EVENTO = 2

LIMITE_NUEVO = 3
LIMITE_REINCIDENTE = 1


def ahora():
    return datetime.now(timezone.utc) - timedelta(hours=5)


def hoy_str():
    return ahora().strftime("%Y-%m-%d")


def hoy_date():
    return ahora().date()


def hora_corta():
    return ahora().strftime("%H:%M")


def timestamp_str():
    return ahora().strftime("%Y-%m-%d %H:%M:%S")


def es_fin_de_semana(fecha=None):
    return (fecha or ahora().date()).weekday() >= 5


def _safe_df(fn, default_cols=None):
    try:
        df = fn()
        if df is None:
            return pd.DataFrame(columns=default_cols or [])
        return df
    except Exception as e:
        log.warning("_safe_df: query fallo -> %s", e)
        return pd.DataFrame(columns=default_cols or [])



def confirmar_accion(clave, texto_boton="Confirmar",
                     texto_check="¿Estás seguro de hacer esta acción?",
                     tipo_boton="primary"):
    check = st.checkbox(texto_check, key=f"conf_{clave}")
    if st.button(texto_boton, type=tipo_boton, key=f"btn_conf_{clave}",
                 disabled=not check):
        return True
    return False

def grado_a_ui(g):
    if g is None:
        return ""
    mapa = {"1": "1ro", "2": "2do", "3": "3ro", "4": "4to", "5": "5to"}
    return mapa.get(str(g).strip(), g)
  
def generar_qr(dni, size=220):
    import qrcode  # 🆕 Carga diferida
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H,
                        box_size=10, border=2)
    qr.add_data(str(dni).strip())
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img


def generar_qr_bytes(dni, size=220):
    img = generar_qr(dni, size)
    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def calcular_limite(es_reincidente):
    return LIMITE_REINCIDENTE if es_reincidente else LIMITE_NUEVO


def texto_ultima_oportunidad():
    return "⚠️ ÚLTIMA OPORTUNIDAD — si llega tarde otra vez, mañana no podrá escanear."


def _columnas_responsive(n_items, max_cols=3):
    """
    SIEMPRE 1 columna para garantizar orden alfabético en cualquier
    dispositivo u orientación. Streamlit reordena columnas cuando la
    pantalla cambia de ancho, por eso no se puede confiar en N columnas.
    """
    return st.columns(1)


def _botones_lista(items, etiqueta_fn, key_fn, max_cols=3):
    """
    Renderiza botones en orden alfabético garantizado (1 columna siempre).
    Devuelve el item clickeado o None.
    """
    clickeado = None
    for item in items:
        if st.button(etiqueta_fn(item), width='stretch', key=key_fn(item)):
            clickeado = item
    return clickeado

def hashear_password(password):
    salt = secrets.token_bytes(16)
    d = hashlib.pbkdf2_hmac(PBK_ALG, password.encode("utf-8"), salt, PBK_ITER)
    return f"pbkdf2_{PBK_ALG}${PBK_ITER}${salt.hex()}${d.hex()}"


def verificar_password(password, hash_guardado):
    if hash_guardado.startswith("pbkdf2_"):
        try:
            _, it, salt_hex, hash_hex = hash_guardado.split("$")
            d = hashlib.pbkdf2_hmac(PBK_ALG, password.encode("utf-8"),
                                     bytes.fromhex(salt_hex), int(it))
            return secrets.compare_digest(d.hex(), hash_hex)
        except (ValueError, TypeError):
            return False
    return secrets.compare_digest(
        hashlib.sha256(password.encode()).hexdigest(), hash_guardado)


def _ip():
    try:
        return st.context.headers.get("X-Forwarded-For", "local")
    except Exception:
        return "local"


def auditar(usuario, accion, va=None, vn=None, tb=None, rid=None):
    try:
        escribir("INSERT INTO auditoria(usuario,accion,fecha,valor_anterior,"
                 "valor_nuevo,tabla_afectada,registro_id,ip) "
                 "VALUES(?,?,?,?,?,?,?,?)",
                 (usuario, accion, timestamp_str(), va, vn, tb, rid, _ip()))
    except Exception as e:
        log.warning("audit: %s", e)


def cerrar_sesion():
    usuario = st.session_state.get("user")
    if usuario:
        auditar(usuario["usuario"], "Logout")
    for k in list(st.session_state.keys()):
        del st.session_state[k]


def _bloqueado(u):
    if not u.get("bloqueado_hasta"):
        return False
    try:
        bh = u["bloqueado_hasta"]
        if isinstance(bh, str):
            bh = datetime.strptime(bh, "%Y-%m-%d %H:%M:%S")
        return ahora() < bh
    except (ValueError, TypeError):
        return False


def autenticar(nombre_usuario, password):
    nombre_usuario = (nombre_usuario or "").strip().lower()
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM usuarios WHERE LOWER(usuario)=? AND activo=1",
                    (nombre_usuario,))
        f = cur.fetchone()
        if not f:
            return None, "Usuario no encontrado o inactivo"
        u = dict(f)
        u["bloqueado_hasta"] = fmt_ts(u.get("bloqueado_hasta"))

        if _bloqueado(u):
            lim = datetime.strptime(u["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S")
            min_rest = int((lim - ahora().replace(tzinfo=None)).total_seconds() / 60) + 1
            return None, f"Cuenta bloqueada. Intenta en {min_rest} min"

        if not verificar_password(password, u["password"]):
            if u["rol"] == "Admin":
                auditar(nombre_usuario, "Intento fallido de login (Admin)")
                return None, "Credenciales incorrectas."
            it = (u.get("intentos_fallidos") or 0) + 1
            if it >= MAX_INTENTOS:
                bh = to_ts(ahora() + timedelta(minutes=MIN_BLOQUEO))
                cur.execute("UPDATE usuarios SET intentos_fallidos=0,"
                            "bloqueado_hasta=? WHERE id=?",
                            (bh, u["id"]))
                return None, f"Cuenta bloqueada por {MIN_BLOQUEO} min"
            cur.execute("UPDATE usuarios SET intentos_fallidos=? WHERE id=?",
                        (it, u["id"]))
            return None, f"Credenciales incorrectas. Quedan {MAX_INTENTOS-it} intento(s)"

        cur.execute("UPDATE usuarios SET intentos_fallidos=0,bloqueado_hasta=NULL,"
                    "ultimo_login=?,ultimo_ip=? WHERE id=?",
                    (timestamp_str(), _ip(), u["id"]))
        u["ultimo_login"] = timestamp_str()
        return u, ""


def _verificar_admin_activo():
    with cursor() as (con, cur):
        cur.execute("SELECT id FROM usuarios WHERE rol='Admin' AND activo=1")
        return cur.fetchone() is not None


@st.cache_resource
def inicializar_bd():
    aplicar_schema()
    _seed()
    log.info("base de datos respondiendo correctamente")
    return True


def _seed():
    with cursor() as (con, cur):
        cur.execute("SELECT COUNT(*) AS n FROM turnos")
        if cur.fetchone()["n"] == 0:
            cur.executemany(
                "INSERT INTO turnos(nombre,hora_entrada,hora_salida,tolerancia_min) "
                "VALUES(?,?,?,?)",
                [("Mañana", "06:00", "12:25", 10),
                 ("Tarde", "12:00", "18:10", 10)])

        cur.execute("SELECT COUNT(*) AS n FROM ventanas")
        if cur.fetchone()["n"] == 0:
            cur.execute("SELECT id FROM turnos WHERE nombre=?", ("Mañana",))
            tm = cur.fetchone()
            cur.execute("SELECT id FROM turnos WHERE nombre=?", ("Tarde",))
            tt = cur.fetchone()
            if tm:
                cur.execute(
                    "INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,"
                    "hora_limite_puntual,hora_cierre,tolerancia_min,orden) "
                    "VALUES(?,'clases','Clases mañana','06:00','06:55','07:10',10,1)",
                    (tm["id"],))
                cur.execute(
                    "INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,"
                    "hora_limite_puntual,hora_cierre,tolerancia_min,orden) "
                    "VALUES(?,'reforzamiento','Reforzamiento mañana','08:00','08:00','14:00',0,2)",
                    (tm["id"],))
            if tt:
                cur.execute(
                    "INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,"
                    "hora_limite_puntual,hora_cierre,tolerancia_min,orden) "
                    "VALUES(?,'reforzamiento','Reforzamiento tarde','10:00','10:00','10:20',0,1)",
                    (tt["id"],))
                cur.execute(
                    "INSERT INTO ventanas(turno_id,tipo,nombre,hora_apertura,"
                    "hora_limite_puntual,hora_cierre,tolerancia_min,orden) "
                    "VALUES(?,'clases','Clases tarde','12:00','12:49','18:10',10,2)",
                    (tt["id"],))

        cur.execute("SELECT COUNT(*) AS n FROM grados")
        if cur.fetchone()["n"] == 0:
            for g in ["1ro", "2do", "3ro", "4to", "5to"]:
                cur.execute("INSERT INTO grados(nombre) VALUES(?)", (g,))

        cur.execute("SELECT COUNT(*) AS n FROM usuarios")
        if cur.fetchone()["n"] == 0:
            pwd = secrets.token_urlsafe(9)
            cur.execute(
                "INSERT INTO usuarios(usuario,password,rol,nombres,"
                "debe_cambiar_password,es_principal) "
                "VALUES(? ,?,'Admin','Administrador',1,1)",
                ("admin", hashear_password(pwd)))
            log.warning("admin creado, pass temporal: %s", pwd)


def modo_mantenimiento():
    with cursor() as (con, cur):
        cur.execute("SELECT valor FROM config WHERE clave=?", ("mantenimiento",))
        f = cur.fetchone()
        return bool(f and f["valor"] == "1")


def activar_mantenimiento(usuario, mensaje=""):
    escribir("INSERT INTO config(clave,valor) VALUES(?,?) "
             "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
             ("mantenimiento", "1"))
    escribir("INSERT INTO config(clave,valor) VALUES(?,?) "
             "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
             ("mantenimiento_msg", mensaje or ""))
    auditar(usuario["usuario"], "Activo modo mantenimiento")


def desactivar_mantenimiento(usuario):
    escribir("INSERT INTO config(clave,valor) VALUES(?,?) "
             "ON CONFLICT(clave) DO UPDATE SET valor=excluded.valor",
             ("mantenimiento", "0"))
    auditar(usuario["usuario"], "Desactivo modo mantenimiento")


def mensaje_mantenimiento():
    with cursor() as (con, cur):
        cur.execute("SELECT valor FROM config WHERE clave=?",
                    ("mantenimiento_msg",))
        f = cur.fetchone()
        return f["valor"] if f else ""


def vista_mantenimiento():
    st.title("Sistema en mantenimiento")
    msg = mensaje_mantenimiento()
    if msg:
        st.warning(msg)
    st.info("Vuelve a intentarlo mas tarde.")

def obtener_periodo_activo():
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM periodos WHERE activo=1 LIMIT 1")
        f = cur.fetchone()
        if not f:
            return None
        d = dict(f)
        d["fecha_inicio"] = fmt_date(d.get("fecha_inicio"))
        d["fecha_fin"] = fmt_date(d.get("fecha_fin"))
        d["fecha_cierre"] = fmt_ts(d.get("fecha_cierre"))
        return d


def periodo_tiene_alumnos(pid):
    with cursor() as (con, cur):
        cur.execute("SELECT COUNT(*) AS n FROM alumnos "
                    "WHERE periodo_id=? AND activo=1", (pid,))
        return (cur.fetchone()["n"] or 0) > 0


def sistema_bloqueado():
    p = obtener_periodo_activo()
    return not p or not periodo_tiene_alumnos(p["id"])


def listar_periodos():
    return leer_df("SELECT id,nombre,fecha_inicio,fecha_fin,activo,cerrado "
                   "FROM periodos ORDER BY id DESC")


def listar_periodos_cerrados():
    return leer_df("SELECT id,nombre,fecha_inicio,fecha_fin,fecha_cierre "
                   "FROM periodos WHERE cerrado=1 ORDER BY id DESC")


def activar_periodo(idp, usuario):
    with cursor() as (con, cur):
        cur.execute("SELECT cerrado FROM periodos WHERE id=?", (idp,))
        f = cur.fetchone()
        if not f:
            return False, "Periodo no encontrado."
        if f["cerrado"]:
            return False, "Ese periodo esta cerrado."
        cur.execute("UPDATE periodos SET activo=0")
        cur.execute("UPDATE periodos SET activo=1 WHERE id=?", (idp,))
    auditar(usuario["usuario"], f"Activo periodo id={idp}",
            tb="periodos", rid=idp)
    return True, "Periodo activado."


def info_periodo_con_alumnos():
    return leer_df("""
        SELECT p.id, p.nombre, p.fecha_inicio, p.fecha_fin, p.activo, p.cerrado,
               (SELECT COUNT(*) FROM alumnos a
                WHERE a.periodo_id=p.id AND a.activo=1) AS alumnos_activos,
               (SELECT COUNT(*) FROM alumnos a WHERE a.periodo_id=p.id) AS alumnos_total
        FROM periodos p ORDER BY p.id DESC
    """)


def listar_turnos():
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM turnos ORDER BY id")
        filas = cur.fetchall()
    out = []
    for f in filas:
        d = dict(f)
        d["hora_entrada"] = fmt_time(d.get("hora_entrada"))
        d["hora_salida"] = fmt_time(d.get("hora_salida"))
        out.append(d)
    return out


def listar_ventanas(id_turno=None):
    with cursor() as (con, cur):
        if id_turno:
            cur.execute(
                "SELECT * FROM ventanas WHERE turno_id=? AND activo=1 "
                "ORDER BY orden,id", (id_turno,))
        else:
            cur.execute(
                "SELECT * FROM ventanas WHERE activo=1 "
                "ORDER BY turno_id,orden")
        filas = cur.fetchall()
    out = []
    for f in filas:
        d = dict(f)
        d["hora_apertura"] = fmt_time(d.get("hora_apertura"))
        d["hora_limite_puntual"] = fmt_time(d.get("hora_limite_puntual"))
        d["hora_cierre"] = fmt_time(d.get("hora_cierre"))
        out.append(d)
    return out



def _dia_dict(f):
    d = dict(f)
    d["fecha"] = fmt_date(d.get("fecha"))
    d["hora_entrada"] = fmt_time(d.get("hora_entrada"))
    return d


def dia_especial_hoy(id_turno, fecha, id_seccion=None):
    f_str = fmt_date(fecha)
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM dias_especiales WHERE fecha=? AND activo=1 "
                    "AND tipo='feriado' LIMIT 1", (f_str,))
        feriado = cur.fetchone()
        if feriado:
            return _dia_dict(feriado)

        if id_seccion:
            cur.execute("""
                SELECT d.* FROM dias_especiales d
                JOIN dias_especiales_secciones ds
                  ON ds.dia_especial_id = d.id
                WHERE d.fecha=? AND d.activo=1 AND d.tipo='evento'
                  AND ds.seccion_id=? AND (d.turno_id=? OR d.turno_id IS NULL)
                ORDER BY d.turno_id IS NULL, d.turno_id DESC, d.id DESC LIMIT 1
            """, (f_str, id_seccion, id_turno))
            r = cur.fetchone()
            if r:
                return _dia_dict(r)

        cur.execute("""
            SELECT * FROM dias_especiales
            WHERE fecha=? AND activo=1 AND tipo='evento' AND turno_id=?
              AND id NOT IN (SELECT dia_especial_id FROM dias_especiales_secciones)
            ORDER BY id DESC LIMIT 1
        """, (f_str, id_turno))
        r = cur.fetchone()
        if r:
            return _dia_dict(r)

        cur.execute("""
            SELECT * FROM dias_especiales
            WHERE fecha=? AND activo=1 AND tipo='evento' AND turno_id IS NULL
              AND id NOT IN (SELECT dia_especial_id FROM dias_especiales_secciones)
            ORDER BY id DESC LIMIT 1
        """, (f_str,))
        r = cur.fetchone()
        return _dia_dict(r) if r else None


def ventana_activa_para_alumno(id_turno, fecha, id_seccion=None):
    dia = dia_especial_hoy(id_turno, fecha, id_seccion)
    if dia and dia["tipo"] == "feriado":
        return None
    h = hora_corta()
    for v in listar_ventanas(id_turno):
        ap = v["hora_apertura"]
        ci = v["hora_cierre"]
        lim = v["hora_limite_puntual"] or ap
        if v["tipo"] == VENT_CLASES and dia and dia["tipo"] == "evento":
            ap = dia["hora_entrada"]
            hh, mm = map(int, ap.split(":"))
            base = datetime(2000, 1, 1, hh, mm)
            ci = (base + timedelta(hours=HORAS_CIERRE_EVENTO)).strftime("%H:%M")
            lim = (base + timedelta(minutes=v["tolerancia_min"] or 0)).strftime("%H:%M")
        if ap <= h <= ci:
            es_ev = bool(dia and dia["tipo"] == "evento" and v["tipo"] == VENT_CLASES)
            return {
                **v,
                "hora_apertura_efectiva": ap,
                "hora_limite_efectiva": lim,
                "hora_cierre_efectiva": ci,
                "es_evento": es_ev,
                "evento_nombre": (dia.get("descripcion") if es_ev else None),
                "dia_especial_id": (dia.get("id") if es_ev else None),
                "contar_como_clases": (bool(dia.get("contar_como_clases")) if es_ev else False),
            }
    return None


def listar_grados():
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM grados ORDER BY nombre")
        return [dict(f) for f in cur.fetchall()]


def secciones_por_grado(idg):
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM secciones WHERE grado_id=? ORDER BY nombre",
                    (idg,))
        return [dict(f) for f in cur.fetchall()]


def secciones_por_turno(idt):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT s.*, g.nombre AS grado FROM secciones s
            JOIN grados g ON s.grado_id=g.id
            WHERE s.turno_id=? ORDER BY g.nombre, s.nombre
        """, (idt,))
        return [dict(f) for f in cur.fetchall()]


def alumnos_de_seccion(idsec):
    return leer_df(
        "SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,"
        "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres "
        "AS nombre_completo FROM alumnos a "
        "WHERE a.seccion_id=? AND a.activo=1 "
        "ORDER BY a.apellido_paterno,a.apellido_materno,a.nombres", (idsec,))


def buscar_alumnos(texto, idg=None, idsec=None, limite=200):
    q = ("SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,"
         "g.id AS grado_id,g.nombre AS grado,s.id AS seccion_id,s.nombre AS seccion,"
         "t.nombre AS turno, "
         "a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres "
         "AS nombre_completo "
         "FROM alumnos a JOIN secciones s ON a.seccion_id=s.id "
         "JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id "
         "WHERE a.activo=1")
    p = []
    if texto:
        for w in [x.strip() for x in texto.split() if x.strip()]:
            q += (" AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? "
                  "OR a.apellido_materno LIKE ?)")
            pat = f"%{w}%"
            p += [pat, pat, pat]
    if idg:
        q += " AND g.id=?"
        p.append(idg)
    if idsec:
        q += " AND s.id=?"
        p.append(idsec)
    q += " ORDER BY a.apellido_paterno LIMIT ?"
    p.append(limite)
    return leer_df(q, p)


def _buscar_alumno_por_dni(dni):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.id,a.dni,a.nombres,a.apellido_paterno,a.apellido_materno,
                   s.id AS seccion_id,s.nombre AS seccion,
                   g.nombre AS grado,t.id AS turno_id,t.nombre AS turno
            FROM alumnos a
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE a.dni=? AND a.activo=1
        """, (dni,))
        f = cur.fetchone()
        return dict(f) if f else None


def _nombre_completo(a):
    return (a['apellido_paterno'] + " " + (a['apellido_materno'] or "") +
            ", " + a['nombres']).strip(", ")


def crear_alumno(dni, nombres, ap, am, idsec, apo_n, apo_t, usuario):
    per = obtener_periodo_activo()
    if not per:
        return False, "No hay periodo activo."
    try:
        with cursor() as (con, cur):
            ida = None
            if apo_n:
                cur.execute("SELECT id FROM apoderados WHERE nombre=? "
                            "AND COALESCE(telefono,'')=?",
                            (apo_n, apo_t or ""))
                f = cur.fetchone()
                if f:
                    ida = f["id"]
                else:
                    cur.execute("INSERT INTO apoderados(nombre,telefono) "
                                "VALUES(?,?)", (apo_n, apo_t or None))
                    ida = cur.lastrowid
            cur.execute("""
                INSERT INTO alumnos(dni,nombres,apellido_paterno,apellido_materno,
                                    seccion_id,apoderado_id,nombre_apoderado,
                                    telefono_apoderado,periodo_id)
                VALUES(?,?,?,?,?,?,?,?,?)
            """, (dni, nombres, ap, am or None, idsec, ida,
                  apo_n or None, apo_t or None, per["id"]))
        auditar(usuario["usuario"], "Creo alumno DNI " + dni, tb="alumnos")
        return True, "Alumno " + nombres + " creado."
    except Exception as e:
        if "unique" in str(e).lower() or "duplicate" in str(e).lower():
            return False, "Ya existe un alumno con DNI " + dni
        log.error("crear_alumno: %s", e)
        return False, "Error al crear el alumno."


def editar_alumno(idal, apo_n, apo_t, idsec, dni, usuario):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT g.nombre AS grado, s.nombre AS seccion
            FROM alumnos a
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            WHERE a.id=?
        """, (idal,))
        viejo = cur.fetchone()
        old_txt = f"{viejo['grado']} {viejo['seccion']}" if viejo else "?"

        ida = None
        if apo_n:
            cur.execute("SELECT id FROM apoderados WHERE nombre=? "
                        "AND COALESCE(telefono,'')=?", (apo_n, apo_t or ""))
            f = cur.fetchone()
            if f:
                ida = f["id"]
            else:
                cur.execute("INSERT INTO apoderados(nombre,telefono) "
                            "VALUES(?,?)", (apo_n, apo_t or None))
                ida = cur.lastrowid
        cur.execute("UPDATE alumnos SET apoderado_id=?,nombre_apoderado=?,"
                    "telefono_apoderado=?,seccion_id=? WHERE id=?",
                    (ida, apo_n or None, apo_t or None, idsec, idal))

        cur.execute("""
            SELECT g.nombre AS grado, s.nombre AS seccion
            FROM secciones s JOIN grados g ON s.grado_id=g.id
            WHERE s.id=?
        """, (idsec,))
        nuevo = cur.fetchone()
        new_txt = f"{nuevo['grado']} {nuevo['seccion']}" if nuevo else "?"

    if old_txt != new_txt:
        accion = f"Movio alumno DNI {dni} de {old_txt} a {new_txt}"
    else:
        accion = f"Edito alumno DNI {dni}"
    auditar(usuario["usuario"], accion, tb="alumnos", rid=idal)
    return True, "Alumno editado."


def retirar_alumno(idal, dni, usuario):
    escribir("UPDATE alumnos SET activo=0,retirado_en=? WHERE id=?",
             (timestamp_str(), idal))
    auditar(usuario["usuario"], "Desactivo alumno DNI " + dni,
            tb="alumnos", rid=idal)
    return True, "Alumno desactivado."


def reactivar_alumno(idal, dni, usuario):
    escribir("UPDATE alumnos SET activo=1,retirado_en=NULL WHERE id=?", (idal,))
    auditar(usuario["usuario"], "Reactivo alumno DNI " + dni,
            tb="alumnos", rid=idal)
    return True, "Alumno reactivado."


# IMPORTACIÓN
def _normalizar_grado(n):
    n = (n or "").strip().title()
    r = {"1°": "1ro", "2°": "2do", "3°": "3ro", "4°": "4to", "5°": "5to",
         "1o": "1ro", "2o": "2do", "3o": "3ro", "4o": "4to", "5o": "5to",
         "1ero": "1ro", "3ero": "3ro"}
    return r.get(n, n)


def validar_importacion(df, mapeo):
    errs = []
    val = []
    vistos = {}

    def _limpiar(v):
        if v is None:
            return ""
        if isinstance(v, float):
            if pd.isna(v):
                return ""
            if v == int(v):
                return str(int(v))
            return str(v)
        if isinstance(v, int):
            return str(v)
        s = str(v).strip()
        if s.endswith(".0") and s[:-2].isdigit():
            s = s[:-2]
        if s.lower() == "nan":
            return ""
        return s

    with cursor() as (con, cur):
        cur.execute("SELECT nombre FROM grados")
        grados_set = {r["nombre"] for r in cur.fetchall()}

    for idx, fila in df.iterrows():
        nf = idx + 2
        try:
            dni = _limpiar(fila[mapeo["dni"]])
            nom = _limpiar(fila[mapeo["nombres"]])
            ap = _limpiar(fila[mapeo["apellido_paterno"]])
            am = _limpiar(fila[mapeo["apellido_materno"]]) if mapeo.get("apellido_materno") else ""
            gr = _normalizar_grado(_limpiar(fila[mapeo["grado"]]))
            sec = _limpiar(fila[mapeo["seccion"]]).upper()
            tur = _limpiar(fila[mapeo["turno"]]).lower()
            an = _limpiar(fila[mapeo["apoderado_nombre"]]) if mapeo.get("apoderado_nombre") else ""
            at = _limpiar(fila[mapeo["apoderado_telefono"]]) if mapeo.get("apoderado_telefono") else ""

            if not dni:
                errs.append({"fila": nf, "motivo": "DNI vacio"})
                continue
            if not re.fullmatch(r"\d{8}", dni):
                errs.append({"fila": nf, "motivo": f"DNI invalido '{dni}'"})
                continue
            if dni in vistos:
                errs.append({"fila": nf, "motivo": f"DNI {dni} duplicado"})
                continue
            if not nom or not ap or not gr or not sec:
                errs.append({"fila": nf, "motivo": "Faltan campos"})
                continue
            if gr not in grados_set:
                errs.append({"fila": nf, "motivo": f"Grado '{gr}' no existe"})
                continue
            if tur in ("mañana", "manana", "m", "am", "mñ"):
                tn = "Mañana"
            elif tur in ("tarde", "t", "tm", "pm"):
                tn = "Tarde"
            else:
                errs.append({"fila": nf, "motivo": f"Turno '{tur}'"})
                continue
            vistos[dni] = nf
            val.append({
                "dni": dni, "nombres": nom, "apellido_paterno": ap,
                "apellido_materno": am, "grado": gr, "seccion": sec,
                "turno": tn, "apoderado_nombre": an, "apoderado_telefono": at,
            })
        except (KeyError, ValueError, TypeError) as e:
            errs.append({"fila": nf, "motivo": f"Error: {e}"})
    return val, errs, {"total": len(df), "validas": len(val), "errores": len(errs)}


def insertar_alumnos_validos(val):
    per = obtener_periodo_activo()
    if not per:
        return 0, 0, ["No hay periodo activo."]
    pid = per["id"]
    ins = 0
    reac = 0
    errs = []

    if not val:
        return 0, 0, ["Lista vacia."]

    with cursor() as (con, cur):
        cur.execute("SELECT id, nombre FROM turnos")
        mapa_turnos = {r["nombre"]: r["id"] for r in cur.fetchall()}

        cur.execute("SELECT id, nombre FROM grados")
        mapa_grados = {r["nombre"]: r["id"] for r in cur.fetchall()}

        cur.execute("SELECT id, nombre, grado_id, turno_id FROM secciones")
        mapa_secciones = {}
        for r in cur.fetchall():
            mapa_secciones[(r["nombre"], r["grado_id"], r["turno_id"])] = r["id"]

        dni_list = [d["dni"] for d in val]
        mapa_alumnos = {}
        for i in range(0, len(dni_list), 500):
            chunk = dni_list[i:i + 500]
            ph = ",".join("?" * len(chunk))
            cur.execute(f"SELECT id, dni FROM alumnos WHERE dni IN ({ph})", chunk)
            for r in cur.fetchall():
                mapa_alumnos[r["dni"]] = r["id"]

        cur.execute("SELECT id, nombre, COALESCE(telefono,'') AS tel FROM apoderados")
        mapa_apoderados = {}
        for r in cur.fetchall():
            mapa_apoderados[(r["nombre"], r["tel"])] = r["id"]

        for d in val:
            grado_id = mapa_grados.get(d["grado"])
            turno_id = mapa_turnos.get(d["turno"])
            if not grado_id or not turno_id:
                continue
            key = (d["seccion"], grado_id, turno_id)
            if key not in mapa_secciones:
                cur.execute(
                    "INSERT INTO secciones(nombre, grado_id, turno_id) VALUES(?,?,?)",
                    (d["seccion"], grado_id, turno_id))
                mapa_secciones[key] = cur.lastrowid

        for d in val:
            if d["apoderado_nombre"]:
                key = (d["apoderado_nombre"], d["apoderado_telefono"] or "")
                if key not in mapa_apoderados:
                    cur.execute(
                        "INSERT INTO apoderados(nombre, telefono) VALUES(?,?)",
                        (d["apoderado_nombre"], d["apoderado_telefono"] or None))
                    mapa_apoderados[key] = cur.lastrowid

        for d in val:
            try:
                grado_id = mapa_grados.get(d["grado"])
                turno_id = mapa_turnos.get(d["turno"])
                if not grado_id or not turno_id:
                    errs.append(f"{d['dni']}: grado/turno no encontrado")
                    continue
                idsec = mapa_secciones.get((d["seccion"], grado_id, turno_id))
                if not idsec:
                    errs.append(f"{d['dni']}: seccion no encontrada")
                    continue

                ida = None
                if d["apoderado_nombre"]:
                    ida = mapa_apoderados.get(
                        (d["apoderado_nombre"], d["apoderado_telefono"] or ""))

                aid = mapa_alumnos.get(d["dni"])
                if aid:
                    cur.execute("""
                        UPDATE alumnos SET nombres=?, apellido_paterno=?,
                            apellido_materno=?, seccion_id=?, apoderado_id=?,
                            nombre_apoderado=?, telefono_apoderado=?,
                            periodo_id=?, activo=1, retirado_en=NULL
                        WHERE id=?
                    """, (d["nombres"], d["apellido_paterno"],
                          d["apellido_materno"] or None, idsec, ida,
                          d["apoderado_nombre"] or None,
                          d["apoderado_telefono"] or None, pid, aid))
                    reac += 1
                else:
                    cur.execute("""
                        INSERT INTO alumnos(dni, nombres, apellido_paterno,
                            apellido_materno, seccion_id, apoderado_id,
                            nombre_apoderado, telefono_apoderado,
                            periodo_id, activo)
                        VALUES(?,?,?,?,?,?,?,?,?,1)
                    """, (d["dni"], d["nombres"], d["apellido_paterno"],
                          d["apellido_materno"] or None, idsec, ida,
                          d["apoderado_nombre"] or None,
                          d["apoderado_telefono"] or None, pid))
                    ins += 1
            except Exception as e:
                errs.append(f"{d['dni']}: {e}")

    log.info("Import: %d nuevos, %d actualizados, %d errs", ins, reac, len(errs))
    return ins, reac, errs


#  BLOQUEOS
def _fecha_ultimo_desbloqueo(idal):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT fecha_fin FROM bloqueos
            WHERE alumno_id=? AND activo=0 AND fecha_fin IS NOT NULL
            ORDER BY fecha_fin DESC LIMIT 1
        """, (idal,))
        f = cur.fetchone()
        return f["fecha_fin"] if f else None


def _es_reincidente(idal):
    return _fecha_ultimo_desbloqueo(idal) is not None


def contar_tardanzas_desde_ultimo_desbloqueo(idal):
    desde = _fecha_ultimo_desbloqueo(idal)
    with cursor() as (con, cur):
        if desde is None:
            cur.execute("""
                SELECT COUNT(*) AS n FROM tardanzas
                WHERE alumno_id=? AND justificada=0
                  AND periodo_id=(SELECT id FROM periodos WHERE activo=1 LIMIT 1)
            """, (idal,))
        else:
            desde_d = desde.date() if hasattr(desde, "date") else desde
            cur.execute("""
                SELECT COUNT(*) AS n FROM tardanzas
                WHERE alumno_id=? AND justificada=0 AND fecha > ?
            """, (idal, desde_d))
        return cur.fetchone()["n"] or 0


def contar_tardanzas_injustificadas(idal, pid=None):
    with cursor() as (con, cur):
        if pid is not None:
            cur.execute("SELECT COUNT(*) AS n FROM tardanzas "
                        "WHERE alumno_id=? AND justificada=0 AND periodo_id=?",
                        (idal, pid))
        else:
            cur.execute("SELECT COUNT(*) AS n FROM tardanzas "
                        "WHERE alumno_id=? AND justificada=0", (idal,))
        return cur.fetchone()["n"] or 0


def alumno_bloqueado(idal):
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM bloqueos WHERE alumno_id=? AND activo=1 "
                    "ORDER BY id DESC LIMIT 1", (idal,))
        f = cur.fetchone()
        if not f:
            return None
        d = dict(f)
        d["fecha_inicio"] = fmt_date(d.get("fecha_inicio"))
        d["fecha_fin"] = fmt_ts(d.get("fecha_fin"))
        return d


def crear_bloqueo(idal, motivo, usuario, origen="automatico"):
    escribir("INSERT INTO bloqueos(alumno_id,motivo,activo,fecha_inicio,"
             "creado_por,origen) VALUES(?,?,1,?,?,?)",
             (idal, motivo, hoy_str(), usuario["usuario"], origen))
    auditar(usuario["usuario"], f"Bloqueo {origen} alumno_id={idal}",
            tb="bloqueos", rid=idal)


def liberar_bloqueo(idal, usuario, obs=""):
    escribir("UPDATE bloqueos SET activo=0,fecha_fin=?,liberado_por=? "
             "WHERE alumno_id=? AND activo=1",
             (timestamp_str(), usuario["usuario"], idal))
    auditar(usuario["usuario"],
            f"Libero bloqueo alumno_id={idal}. Obs: {obs}",
            tb="bloqueos", rid=idal)


def historial_bloqueos_alumno(alumno_id):
    return leer_df("SELECT id,motivo,activo,fecha_inicio,fecha_fin,"
                   "creado_por,origen,liberado_por FROM bloqueos "
                   "WHERE alumno_id=? ORDER BY id DESC", (alumno_id,))


def listar_bloqueados():
    return leer_df("""
        SELECT b.id,a.id AS alumno_id,a.dni,
        a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS alumno,
        g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,
        b.motivo,b.fecha_inicio,COALESCE(b.origen,'automatico') AS origen
        FROM bloqueos b
        JOIN alumnos a ON b.alumno_id=a.id
        JOIN secciones s ON a.seccion_id=s.id
        JOIN grados g ON s.grado_id=g.id
        JOIN turnos t ON s.turno_id=t.id
        WHERE b.activo=1 ORDER BY b.fecha_inicio DESC
    """)


def obtener_auditoria(limite=500):
    return leer_df("SELECT id,usuario,accion,fecha,ip FROM auditoria "
                   "ORDER BY id DESC LIMIT ?", (int(limite),))


# JUSTIFICACIONES Y PERMISOS 
def _aplicar_just_prev(cur, idal, fecha, tipo):
    cur.execute("SELECT id FROM justificaciones_previas "
                "WHERE alumno_id=? AND fecha_objetivo=? AND tipo=? AND aplicada=0",
                (idal, to_date(fecha), tipo))
    f = cur.fetchone()
    if f:
        cur.execute("UPDATE justificaciones_previas SET aplicada=1 WHERE id=?",
                    (f["id"],))
        return True
    return False


def hay_permiso_activo(idal, fecha):
    with cursor() as (con, cur):
        cur.execute("SELECT id FROM permisos WHERE alumno_id=? AND activo=1 "
                    "AND fecha_inicio<=? AND fecha_fin>=? LIMIT 1",
                    (idal, to_date(fecha), to_date(fecha)))
        return cur.fetchone() is not None


def _puede_justificar(fecha_objetivo_str, tipo_asistencia=None, ventana_id=None):
    try:
        f_obj = datetime.strptime(fecha_objetivo_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False, "Fecha invalida."
    if tipo_asistencia and tipo_asistencia != FALTA:
        return False, "Solo se pueden justificar FALTAS."
    hoy = ahora().date()
    diff = (hoy - f_obj).days
    if diff > 0:
        return False, "Ya paso la ventana."
    if diff < -2:
        return False, "Solo se puede justificar hasta pasado mañana."
    if diff == 0 and ventana_id is not None:
        with cursor() as (con, cur):
            cur.execute("SELECT hora_apertura,hora_cierre FROM ventanas WHERE id=?",
                        (ventana_id,))
            v = cur.fetchone()
            if v:
                h = hora_corta()
                ap = fmt_time(v["hora_apertura"])
                ci = fmt_time(v["hora_cierre"])
                if not (ap <= h <= ci):
                    return False, "La ventana ya esta cerrada."
    return True, ""


def crear_justificacion_previa(idal, fecha_obj, tipo, motivo, usuario):
    if tipo != FALTA:
        return False, "Solo se pueden registrar justificaciones para FALTAS."
    ok, msg = _puede_justificar(fecha_obj, tipo_asistencia=FALTA)
    if not ok:
        return False, msg
    if not motivo or not motivo.strip():
        return False, "El motivo es obligatorio."
    with cursor() as (con, cur):
        cur.execute("SELECT id FROM justificaciones_previas "
                    "WHERE alumno_id=? AND fecha_objetivo=?",
                    (idal, to_date(fecha_obj)))
        if cur.fetchone():
            return False, "Ya existe una justificacion para ese alumno en esa fecha."
        cur.execute("""
            INSERT INTO justificaciones_previas(alumno_id,fecha_objetivo,tipo,
                motivo,creado_por,"timestamp",aplicada)
            VALUES(?,?,?,?,?,?,0)
        """, (idal, to_date(fecha_obj), FALTA, motivo.strip(),
              usuario["usuario"], timestamp_str()))
    auditar(usuario["usuario"], f"Creo justificacion previa {fecha_obj} id={idal}",
            tb="justificaciones_previas", rid=idal)
    return True, "Justificacion registrada."


def crear_permiso(idal, fi, ff, motivo, usuario):
    try:
        fi_d = datetime.strptime(fi, "%Y-%m-%d").date()
        ff_d = datetime.strptime(ff, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return False, "Fechas invalidas."
    hoy = ahora().date()
    manana = hoy + timedelta(days=1)
    if fi_d < manana:
        return False, "El permiso solo puede empezar desde mañana en adelante."
    if ff_d < fi_d:
        return False, "La fecha fin no puede ser anterior a la fecha inicio."
    dias = (ff_d - fi_d).days + 1
    if dias > MAX_DIAS_PERMISO:
        return False, f"El permiso no puede exceder {MAX_DIAS_PERMISO} dias."
    if not motivo or not motivo.strip():
        return False, "El motivo es obligatorio."
    with cursor() as (con, cur):
        cur.execute("SELECT id FROM permisos WHERE alumno_id=? AND activo=1 "
                    "AND NOT (fecha_fin < ? OR fecha_inicio > ?)",
                    (idal, to_date(fi_d), to_date(ff_d)))
        if cur.fetchone():
            return False, "El alumno ya tiene un permiso que se solapa con esas fechas."
        per = obtener_periodo_activo()
        pid = per["id"] if per else None
        cur.execute("""
            INSERT INTO permisos(alumno_id,fecha_inicio,fecha_fin,motivo,
                creado_por,"timestamp",activo,periodo_id)
            VALUES(?,?,?,?,?,?,1,?)
        """, (idal, to_date(fi_d), to_date(ff_d), motivo.strip(),
              usuario["usuario"], timestamp_str(), pid))
    auditar(usuario["usuario"], f"Creo permiso {fi} a {ff} id={idal}",
            tb="permisos", rid=idal)
    return True, f"Permiso registrado del {fi} al {ff}."


def eliminar_permiso(id_permiso, usuario, motivo_eliminacion=""):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT p.id, p.alumno_id, p.fecha_inicio, p.fecha_fin,
                   COALESCE(p.motivo,'') AS motivo, p.periodo_id,
                   a.dni,
                   a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS alumno
            FROM permisos p
            JOIN alumnos a ON p.alumno_id = a.id
            WHERE p.id=? AND p.activo=1
        """, (id_permiso,))
        perm = cur.fetchone()

        if not perm:
            return False, "Permiso no encontrado o ya estaba anulado."

        cur.execute("""
            SELECT COUNT(*) AS n FROM asistencias
            WHERE alumno_id=?
              AND fecha BETWEEN ? AND ?
              AND estado='Permiso'
        """, (perm["alumno_id"], perm["fecha_inicio"], perm["fecha_fin"]))
        ya_aplicado = (cur.fetchone()["n"] or 0) > 0

        if ya_aplicado:
            return False, ("Este permiso ya fue aplicado y no se puede anular. "
                           "Solo se pueden anular permisos pendientes (futuros).")

        cur.execute("UPDATE permisos SET activo=0 WHERE id=?", (id_permiso,))

    auditar(usuario["usuario"],
            f"Anulo permiso id={id_permiso} de {perm['alumno']} "
            f"(DNI {perm['dni']}, del {fmt_date(perm['fecha_inicio'])} "
            f"al {fmt_date(perm['fecha_fin'])})"
            + (f" | Motivo: {motivo_eliminacion.strip()}"
               if motivo_eliminacion.strip() else ""),
            tb="permisos", rid=id_permiso)
    return True, f"Permiso de {perm['alumno']} anulado."


def anular_justificacion_previa(id_just, usuario, motivo_anulacion=""):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT jp.id, jp.alumno_id, jp.fecha_objetivo, jp.tipo,
                   COALESCE(jp.motivo,'') AS motivo, jp.aplicada,
                   a.dni,
                   a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS alumno
            FROM justificaciones_previas jp
            JOIN alumnos a ON jp.alumno_id = a.id
            WHERE jp.id=?
        """, (id_just,))
        just = cur.fetchone()

        if not just:
            return False, "Justificacion previa no encontrada."
        if just["aplicada"] == -1:
            return False, "Esta justificacion previa ya estaba anulada."
        if just["aplicada"] == 1:
            return False, ("Esta justificacion ya fue aplicada y no se puede anular. "
                           "Solo se pueden anular justificaciones pendientes.")

        cur.execute("UPDATE justificaciones_previas SET aplicada = -1 WHERE id = ?",
                    (id_just,))

    auditar(usuario["usuario"],
            f"Anulo justificacion previa id={id_just} de {just['alumno']} "
            f"(DNI {just['dni']}, fecha objetivo {fmt_date(just['fecha_objetivo'])})"
            + (f" | Motivo: {motivo_anulacion.strip()}"
               if motivo_anulacion.strip() else ""),
            tb="justificaciones_previas", rid=id_just)
    return True, f"Justificacion previa de {just['alumno']} anulada."


def reactivar_justificacion_previa(id_just, usuario, motivo_nuevo=""):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT jp.id, jp.alumno_id, jp.fecha_objetivo, jp.tipo,
                   COALESCE(jp.motivo,'') AS motivo, jp.aplicada,
                   a.dni,
                   a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS alumno
            FROM justificaciones_previas jp
            JOIN alumnos a ON jp.alumno_id = a.id
            WHERE jp.id=?
        """, (id_just,))
        just = cur.fetchone()

        if not just:
            return False, "Justificacion previa no encontrada."
        if just["aplicada"] != -1:
            return False, "Esta justificacion no esta anulada."

        motivo_final = motivo_nuevo.strip() if motivo_nuevo and motivo_nuevo.strip() \
                       else just["motivo"]

        cur.execute("UPDATE justificaciones_previas "
                    "SET aplicada = 0, motivo = ? WHERE id = ?",
                    (motivo_final, id_just))

    auditar(usuario["usuario"],
            f"Reactivo justificacion previa id={id_just} de {just['alumno']}",
            tb="justificaciones_previas", rid=id_just)
    return True, f"Justificacion de {just['alumno']} reactivada (pendiente)."

#REGISTRAR ENTRADA
def registrar_entrada(dni, usuario, origen="qr"):
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni):
        return False, "ERROR", "DNI invalido", {}
    al = _buscar_alumno_por_dni(dni)
    if not al:
        return False, "ERROR", "DNI no encontrado", {}

    bloq = alumno_bloqueado(al["id"])
    if bloq:
        auditar(usuario["usuario"], "Intento escaneo bloqueado DNI " + dni,
                tb="bloqueos", rid=al["id"])
        return False, "BLOQUEADO", (
            _nombre_completo(al) + " | BLOQUEADO - retener y llevar a Direccion"
        ), {"alumno": al, "motivo": bloq["motivo"]}

    reincidente = _es_reincidente(al["id"])
    limite = calcular_limite(reincidente)
    n_previas = contar_tardanzas_desde_ultimo_desbloqueo(al["id"])

    if n_previas >= limite:
        if not bloq:
            crear_bloqueo(al["id"],
                          f"{n_previas} tardanza(s) acumulada(s) - reincidente={reincidente}",
                          usuario, "automatico")
        auditar(usuario["usuario"], "Intento escaneo bloqueado DNI " + dni,
                tb="bloqueos", rid=al["id"])
        return False, "BLOQUEADO", (
            _nombre_completo(al) +
            " | BLOQUEADO (alcanzó el limite de tardanzas) - retener"
        ), {"alumno": al, "motivo": "limite alcanzado"}

    fecha_str = hoy_str()
    fecha_d = hoy_date()
    ha = hora_corta()
    per = obtener_periodo_activo()
    pid = per["id"] if per else None

    with cursor() as (con, cur):
        cur.execute("SELECT id FROM dias_especiales WHERE fecha=? AND activo=1 "
                    "AND tipo='feriado' LIMIT 1", (fecha_d,))
        if cur.fetchone():
            return False, "ERROR", "Hoy es feriado, no se registra", {}

    v = ventana_activa_para_alumno(al["turno_id"], fecha_str, al["seccion_id"])
    if not v:
        return False, "ERROR", (
            _nombre_completo(al) + " | Sin ventana activa (" + ha + ")"
        ), {"alumno": al}

    permiso = hay_permiso_activo(al["id"], fecha_str)
    tipo_real = v["tipo"]
    if v.get("es_evento") and v["tipo"] == VENT_CLASES:
        tipo_real = VENT_CLASES if v.get("contar_como_clases") else TIPO_ASIST_EVENTO
    dia_id = v.get("dia_especial_id")
    ev_nombre = v.get("evento_nombre")

    with cursor() as (con, cur):
        cur.execute("SELECT id,estado FROM asistencias "
                    "WHERE alumno_id=? AND fecha=? AND tipo=?",
                    (al["id"], fecha_d, tipo_real))
        ex = cur.fetchone()
        if ex:
            return False, "ERROR", (
                _nombre_completo(al) + f" ya registro {tipo_real} hoy ({ex['estado']})"
            ), {}

        if v["tipo"] == VENT_REF:
            try:
                cur.execute("""
                    INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,
                        estado,justificada,origen,periodo_id,dia_especial_id)
                    VALUES(?,?,?,'reforzamiento',?,?,?,?,?,?)
                """, (al["id"], fecha_d, v["id"], ha, REF_ASISTIO,
                      1 if permiso else 0, origen, pid, dia_id))
                if al["turno"] == "Tarde":
                    cur.execute("SELECT id FROM asistencias WHERE alumno_id=? "
                                "AND fecha=? AND tipo='clases'",
                                (al["id"], fecha_d))
                    if not cur.fetchone():
                        cur.execute("""
                            INSERT INTO asistencias(alumno_id,fecha,ventana_id,
                                tipo,hora,estado,justificada,origen,periodo_id,
                                dia_especial_id)
                            VALUES(?,?,NULL,'clases',?,'Puntual',?,?,?,?)
                        """, (al["id"], fecha_d, ha, 1 if permiso else 0,
                              origen, pid, dia_id))
            except Exception as e:
                if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                    return False, "ERROR", (
                        _nombre_completo(al) + " ya registro reforzamiento hoy"
                    ), {}
                raise
            
            msg = (_nombre_completo(al) + " | " + al["grado"] + " " +
                   al["seccion"] + " | Reforzamiento + Clases Puntual " + ha
                   if al["turno"] == "Tarde" else
                   _nombre_completo(al) + " | " + al["grado"] + " " +
                   al["seccion"] + " | Asistio a reforzamiento " + ha)
            return True, "REFORZAMIENTO", msg, {"alumno": al, "evento_nombre": None}

        if al["turno"] == "Tarde":
            cur.execute("SELECT id,estado,hora FROM asistencias "
                        "WHERE alumno_id=? AND fecha=? AND tipo IN ('clases','evento')",
                        (al["id"], fecha_d))
            if cur.fetchone():
                return False, "ERROR", (
                    _nombre_completo(al) + " ya tiene registro hoy."
                ), {}

        lim = v["hora_limite_efectiva"]
        est = PUNTUAL if ha <= lim else TARDANZA
        just_final = 1 if permiso else 0

        try:
            cur.execute("""
                INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,hora,
                    estado,justificada,origen,periodo_id,dia_especial_id)
                VALUES(?,?,?,?,?,?,?,?,?,?)
            """, (al["id"], fecha_d, v["id"], tipo_real, ha, est,
                  just_final, origen, pid, dia_id))
        except Exception as e:
            if "unique" in str(e).lower() or "duplicate" in str(e).lower():
                return False, "ERROR", (
                    _nombre_completo(al) + " ya registro hoy (carrera)"
                ), {}
            raise

        n = 0
        acc = None
        if est == TARDANZA:
            n = n_previas + 1
            acc = ACC_PERDONADO if n < limite else ACC_DERIVADO
            try:
                cur.execute("""
                    INSERT INTO tardanzas(alumno_id,fecha,hora,numero,accion,
                        justificada,origen,registrado_por,"timestamp",periodo_id)
                    VALUES(?,?,?,?,?,?,?,?,?,?)
                """, (al["id"], fecha_d, ha, n, acc, just_final, origen,
                      usuario["usuario"], timestamp_str(), pid))
            except Exception as e:
                if "unique" not in str(e).lower() and "duplicate" not in str(e).lower():
                    log.warning("tardanza: %s", e)

    if est == TARDANZA:
        alcanzo_limite = (n >= limite)
        ultima_oport = (n == limite - 1) and not alcanzo_limite

        sufijo = " [JUSTIFICADA]" if just_final else ""
        msg = (_nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] +
               f" | Tardanza {n}a ({acc}){sufijo} {ha}")
        if alcanzo_limite:
            msg += " | ⚠️ MANANA NO PODRA ESCANEAR"
        elif ultima_oport:
            msg += " | ⚠️ Ultima oportunidad"

        return True, "TARDANZA", msg, {
            "alumno": al, "numero": n, "accion": acc,
            "evento_nombre": ev_nombre,
            "alcanzo_limite": alcanzo_limite,
            "ultima_oportunidad": ultima_oport,
        }

    return True, "PUNTUAL", (
        _nombre_completo(al) + " | " + al["grado"] + " " + al["seccion"] +
        " | " + tipo_real.upper() + " Puntual " + ha
    ), {"alumno": al, "evento_nombre": ev_nombre}


# MARCAR FALTAS AL CIERRE 
def marcar_faltas_al_cierre():
    fecha_d = hoy_date()
    fecha_str = hoy_str()
    ha = hora_corta()
    h_str = hora_corta()
    per = obtener_periodo_activo()
    pid = per["id"] if per else None

    with cursor() as (con, cur):
        cur.execute("SELECT id FROM dias_especiales WHERE fecha=? AND activo=1 "
                    "AND tipo='feriado' LIMIT 1", (fecha_d,))
        if cur.fetchone():
            return

        cur.execute("SELECT id,turno_id,hora_entrada FROM dias_especiales "
                    "WHERE fecha=? AND activo=1 AND tipo='evento' LIMIT 1",
                    (fecha_d,))
        evento = cur.fetchone()
        if evento:
            for t in listar_turnos():
                if evento["turno_id"] and t["id"] != evento["turno_id"]:
                    continue
                for v in listar_ventanas(t["id"]):
                    if v["tipo"] != VENT_CLASES:
                        continue
                    if ha < v["hora_cierre"]:
                        continue
                    cur.execute("""
                        SELECT a.id FROM alumnos a
                        JOIN secciones s ON a.seccion_id=s.id
                        WHERE s.turno_id=? AND a.activo=1
                    """, (t["id"],))
                    for al in cur.fetchall():
                        cur.execute("SELECT id FROM asistencias "
                                    "WHERE alumno_id=? AND fecha=? AND tipo='evento'",
                                    (al["id"], fecha_d))
                        if cur.fetchone():
                            continue
                        if hay_permiso_activo(al["id"], fecha_str):
                            cur.execute("""
                                INSERT INTO asistencias(alumno_id,fecha,ventana_id,
                                    tipo,hora,estado,justificada,observacion,origen,
                                    periodo_id,dia_especial_id)
                                VALUES(?,?,?,'evento',?,?,1,?,?,?,?)
                            """, (al["id"], fecha_d, v["id"], h_str,
                                  PERMISO, "Permiso", "manual", pid, evento["id"]))
                            continue
                        cur.execute("""
                            INSERT INTO asistencias(alumno_id,fecha,ventana_id,
                                tipo,hora,estado,origen,periodo_id,dia_especial_id)
                            VALUES(?,?,?,'evento',?,?,?,?,?)
                        """, (al["id"], fecha_d, v["id"], h_str, "Falta",
                              "auto", pid, evento["id"]))
            return

        if es_fin_de_semana():
            return
        for t in listar_turnos():
            for v in listar_ventanas(t["id"]):
                if ha < v["hora_cierre"]:
                    continue
                tipo = "clases" if v["tipo"] == VENT_CLASES else "reforzamiento"
                est = "Falta" if v["tipo"] == VENT_CLASES else "No asistio"
                cur.execute("""
                    SELECT a.id FROM alumnos a
                    JOIN secciones s ON a.seccion_id=s.id
                    WHERE s.turno_id=? AND a.activo=1
                """, (t["id"],))
                for al in cur.fetchall():
                    cur.execute("SELECT id FROM asistencias "
                                "WHERE alumno_id=? AND fecha=? AND tipo=?",
                                (al["id"], fecha_d, tipo))
                    if cur.fetchone():
                        continue
                    if hay_permiso_activo(al["id"], fecha_str):
                        cur.execute("""
                            INSERT INTO asistencias(alumno_id,fecha,ventana_id,
                                tipo,hora,estado,justificada,observacion,origen,
                                periodo_id)
                            VALUES(?,?,?,?,?,?,1,?,?,?)
                        """, (al["id"], fecha_d, v["id"], tipo, h_str,
                              PERMISO, "Permiso otorgado", "manual", pid))
                        continue
                    jp = (_aplicar_just_prev(cur, al["id"], fecha_str, "Falta")
                          if v["tipo"] == VENT_CLASES else False)
                    cur.execute("""
                        INSERT INTO asistencias(alumno_id,fecha,ventana_id,tipo,
                            hora,estado,justificada,origen,periodo_id)
                        VALUES(?,?,?,?,?,?,?,?,?)
                    """, (al["id"], fecha_d, v["id"], tipo, h_str, est,
                          1 if jp else 0, "auto", pid))


# ─── JUSTIFICAR ASISTENCIA ─────────────────────────────────────────────────
def justificar_asistencia(ida, obs, usuario):
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM asistencias WHERE id=?", (ida,))
        reg = cur.fetchone()
        if not reg:
            return False, "Registro no encontrado."
        if reg["estado"] != FALTA:
            return False, "Solo se pueden justificar FALTAS."
        if reg["justificada"]:
            return False, "Esta asistencia ya esta justificada."
        fecha_str = fmt_date(reg["fecha"])
        ok, msg = _puede_justificar(fecha_str, tipo_asistencia=FALTA,
                                     ventana_id=reg["ventana_id"])
        if not ok:
            return False, msg
        if not obs or not obs.strip():
            return False, "La observacion es obligatoria."
        cur.execute("UPDATE asistencias SET justificada=1,observacion=?,"
                    "justificado_por=?,justificado_en=? WHERE id=?",
                    (obs.strip(), usuario["usuario"], timestamp_str(), ida))
    auditar(usuario["usuario"], f"Justifico falta id={ida} motivo: {obs}",
            tb="asistencias", rid=ida)
    return True, "Falta justificada."


def quitar_justificacion(ida, usuario):
    with cursor() as (con, cur):
        cur.execute("SELECT id FROM asistencias WHERE id=?", (ida,))
        if not cur.fetchone():
            return False, "Registro no encontrado."
        cur.execute("UPDATE asistencias SET justificada=0,observacion=NULL,"
                    "justificado_por=NULL,justificado_en=NULL WHERE id=?",
                    (ida,))
    auditar(usuario["usuario"], f"Quito justificacion id={ida}",
            tb="asistencias", rid=ida)
    return True, "Justificacion eliminada."


# ─── ESCÁNER QR ────────────────────────────────────────────────────────────
def _procesar_escaneo(dni):
    u = st.session_state.get("user")
    if not u:
        return
    ok, tipo, msg, extra = registrar_entrada(dni, u, origen="qr")

    es_duplicado = (not ok and tipo == "ERROR" and
                    ("ya registro" in msg or "ya tiene" in msg))

    if es_duplicado:
        sonido = "duplicado"
    elif not ok and tipo == "ERROR":
        sonido = "error"
    elif tipo == "BLOQUEADO":
        sonido = "bloqueado"
    elif ok and tipo == "TARDANZA":
        sonido = "tardanza"
    elif ok:
        sonido = "puntual"
    else:
        sonido = "error"

    st.session_state.setdefault("_qr_mensajes", [])
    st.session_state["_qr_mensajes"].insert(0, {
        "dni": dni, "tipo": tipo, "mensaje": msg, "extra": extra, "ts": time.time()})
    st.session_state["_qr_mensajes"] = st.session_state["_qr_mensajes"][:3] # 🆕 SOLO 3
    contador = st.session_state.get("_qr_sonido_contador", 0) + 1
    st.session_state["_qr_sonido_contador"] = contador
    st.session_state["_qr_sonido_pendiente"] = {
        "kind": sonido, "nonce": contador, "ts": time.time()}


def escaner_qr_continuo(key="qr_scanner"):
    # 🆕 ELIMINADO EL HEADER (Más espacio y menos RAM)
    if st.session_state.get("_qr_desbloqueo_exitoso"):
        result = None
    else:
        mount_id = st.session_state.get("_qr_mount_id", 0)
        result = qr_scanner(key=f"qr_{key}_{mount_id}", on_scan=lambda: None)

    if result is not None and getattr(result, "qr_dni", None):
        dni = str(result.qr_dni).strip()
        ult = st.session_state.get("_ultimo_qr_scan", {})
        
        if not (ult.get("dni") == dni and (time.time() - ult.get("ts", 0)) < 5.0):
            st.session_state["_ultimo_qr_scan"] = {"dni": dni, "ts": time.time()}
            _procesar_escaneo(dni)
            
            m = st.session_state.get("_qr_mensajes", [{}])[0]
            if m.get("tipo") == "BLOQUEADO":
                st.rerun()
                
            sp = st.session_state.get("_qr_sonido_pendiente") or {}
            kind = sp.get("kind", "")
            if kind:
                st.components.v1.html(f"""
                    <script>
                    (function() {{
                        try {{
                            if (window.parent && typeof window.parent.__qrFeedback === 'function') {{
                                window.parent.__qrFeedback('{kind}');
                            }}
                        }} catch(e) {{}}
                    }})();
                    </script>
                """, height=0)
                st.session_state.pop("_qr_sonido_pendiente", None)

    desbloq = st.session_state.get("_qr_desbloqueo_exitoso")
    if desbloq:
        st.success(f"✅ **{desbloq['nombre']}** desbloqueado con éxito. **Vuelve a escanear su QR**.")
        if st.button("OK, entendido", key="ok_desbloq_msg", type="secondary", width='stretch'):
            st.session_state.pop("_qr_desbloqueo_exitoso", None)
            st.session_state["_qr_mount_id"] = st.session_state.get("_qr_mount_id", 0) + 1
            st.rerun()

    mensajes = st.session_state.get("_qr_mensajes", [])
    if len(mensajes) > 3:
        st.session_state["_qr_mensajes"] = mensajes[:3]
        mensajes = mensajes[:3]

    if mensajes:
        m = mensajes[0]
        if (time.time() - m.get("ts", 0)) < 8:
            _render_mensaje_qr(m)
        if len(mensajes) > 1:
            st.markdown('<div class="scan-ultimos">Últimos escaneos</div>',
                        unsafe_allow_html=True)
            for msg in mensajes[1:3]:
                _render_mensaje_qr(msg)


def _render_mensaje_qr(msg):
    tipo = msg["tipo"]
    mensaje = msg["mensaje"]
    extra = msg.get("extra") or {}
    alumno = extra.get("alumno") or {}
    ev = extra.get("evento_nombre")

    clase = {"PUNTUAL": "qr-puntual", "TARDANZA": "qr-tardanza",
             "REFORZAMIENTO": "qr-refuerzo", "BLOQUEADO": "qr-bloqueado",
             "INFO": "qr-info", "ERROR": "qr-error"}.get(tipo, "qr-error")

    if tipo == "TARDANZA":
        acc = extra.get("accion")
        if acc == ACC_DERIVADO:
            mensaje += " -> Derivar a Direccion"
            clase = "qr-derivado"
        if extra.get("alcanzo_limite"):
            clase = "qr-retenido"
        elif extra.get("ultima_oportunidad"):
            clase = "qr-ultima"

    html = f'<div class="qr-msg {clase}">'
    if ev:
        html += f'<div class="qr-evento">Evento: {ev}</div>'
    if alumno:
        nombre = (alumno.get("apellido_paterno", "") + " " +
                  (alumno.get("apellido_materno") or "") + ", " +
                  alumno.get("nombres", "")).strip(", ")
        html += f'<div class="qr-nombre">{nombre}</div>'
    html += f'<div class="qr-texto">{mensaje}</div></div>'
    st.markdown(html, unsafe_allow_html=True)

    if tipo == "BLOQUEADO" and alumno and alumno.get("id"):
        alumno_id = alumno.get("id")
        dni = alumno.get("dni")
        st.warning("¿Deseas desbloquear a este estudiante ahora?")
        c1, c2 = st.columns(2)
        with c1:
            if st.button("Si, desbloquear", key=f"desbloq_si_{alumno_id}",
                         type="primary", width='stretch'):
                liberar_bloqueo(alumno_id,
                                st.session_state["user"],
                                "Desbloqueo inmediato desde escaneo QR")
                st.session_state["_qr_desbloqueo_exitoso"] = {
                    "dni": dni,
                    "nombre": (alumno.get("apellido_paterno", "") + " " +
                               (alumno.get("apellido_materno") or "") + ", " +
                               alumno.get("nombres", "")).strip(", "),
                }
                st.session_state.pop("_qr_mensajes", None)
                st.rerun()
        with c2:
            if st.button("No, dejarlo bloqueado",
                         key=f"desbloq_no_{alumno_id}",
                         width='stretch'):
                if st.session_state.get("_qr_mensajes"):
                    st.session_state["_qr_mensajes"] = \
                        st.session_state["_qr_mensajes"][1:]
                st.rerun()
# ─── PERFIL ALUMNO ─────────────────────────────────────────────────────────

def perfil_alumno_datos(idal):
    try:
        with cursor() as (con, cur):
            cur.execute("""
                SELECT a.*,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,
                       s.id AS seccion_id,t.id AS turno_id
                FROM alumnos a JOIN secciones s ON a.seccion_id=s.id
                JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
                WHERE a.id=?
            """, (idal,))
            a = cur.fetchone()
            if not a:
                return {}
            a = dict(a)
            a["retirado_en"] = fmt_ts(a.get("retirado_en"))
    except Exception as e:
        log.exception("perfil_alumno_datos: alumno_id=%s", idal)
        return {}

    da = _safe_df(lambda: leer_df("""
        SELECT a.id,a.fecha,a.hora,a.tipo,a.estado,
               COALESCE(a.justificada,0) AS justificada,
               COALESCE(a.observacion,'') AS observacion,
               COALESCE(a.origen,'qr') AS origen,
               COALESCE(a.justificado_por,'') AS justificado_por,
               COALESCE(a.justificado_en,'') AS justificado_en,
               COALESCE(d.descripcion,'') AS evento_nombre,
               a.ventana_id
        FROM asistencias a
        LEFT JOIN dias_especiales d ON a.dia_especial_id=d.id
        WHERE a.alumno_id=? ORDER BY a.fecha DESC,a.hora DESC
    """, (idal,)), default_cols=["id","fecha","hora","tipo","estado",
                                  "justificada","observacion","origen",
                                  "justificado_por","justificado_en",
                                  "evento_nombre","ventana_id"])

    dt = _safe_df(lambda: leer_df("""
        SELECT fecha,hora,numero AS "N",accion,
               COALESCE(justificada,0) AS justificada,
               COALESCE(origen,'qr') AS origen
        FROM tardanzas WHERE alumno_id=? ORDER BY fecha DESC,hora DESC
    """, (idal,)), default_cols=["fecha","hora","N","accion","justificada","origen"])

    db = _safe_df(lambda: leer_df("""
        SELECT fecha_inicio,COALESCE(fecha_fin,'-') AS fecha_fin,
               COALESCE(motivo,'') AS motivo,
               COALESCE(activo,0) AS activo
        FROM bloqueos WHERE alumno_id=? ORDER BY fecha_inicio DESC
    """, (idal,)), default_cols=["fecha_inicio","fecha_fin","motivo","activo"])

    dp = _safe_df(lambda: leer_df("""
        SELECT id,fecha_inicio,fecha_fin,COALESCE(motivo,'') AS motivo,
               COALESCE(activo,0) AS activo,
               COALESCE(creado_por,'') AS creado_por
        FROM permisos WHERE alumno_id=? ORDER BY fecha_inicio DESC
    """, (idal,)), default_cols=["id","fecha_inicio","fecha_fin","motivo",
                                  "activo","creado_por"])

    dj = _safe_df(lambda: leer_df("""
        SELECT id,fecha_objetivo,tipo,COALESCE(motivo,'') AS motivo,
               COALESCE(aplicada,0) AS aplicada,
               COALESCE(creado_por,'') AS creado_por
        FROM justificaciones_previas WHERE alumno_id=?
        ORDER BY fecha_objetivo DESC
    """, (idal,)), default_cols=["id","fecha_objetivo","tipo","motivo",
                                  "aplicada","creado_por"])

    tp = int((da["estado"] == PUNTUAL).sum()) if (not da.empty and "estado" in da.columns) else 0
    tf = int((da["estado"] == FALTA).sum()) if (not da.empty and "estado" in da.columns) else 0
    tt = int((da["estado"] == TARDANZA).sum()) if (not da.empty and "estado" in da.columns) else 0
    tr = 0
    if not da.empty and "tipo" in da.columns and "estado" in da.columns:
        tr = int(((da["tipo"] == "reforzamiento") &
                  (da["estado"] == REF_ASISTIO)).sum())

    return {"alumno": a, "asistencias": da, "tardanzas": dt, "bloqueos": db,
            "permisos": dp, "justificaciones_previas": dj,
            "total_puntuales": tp, "total_faltas": tf,
            "total_tardanzas": tt, "total_ref_asistio": tr,
            "tard_injust": contar_tardanzas_injustificadas(idal),
            "n_ciclo": contar_tardanzas_desde_ultimo_desbloqueo(idal),
            "reincidente": _es_reincidente(idal),
            "limite": calcular_limite(_es_reincidente(idal)),
            "bloqueado": alumno_bloqueado(idal) is not None}


# ─── MÉTRICAS ──────────────────────────────────────────────────────────────
def metricas_dia_turno(fecha, id_turno):
    fecha_d = to_date(fecha)
    with cursor() as (con, cur):
        cur.execute("""
            SELECT COUNT(*) AS n FROM alumnos a
            JOIN secciones s ON a.seccion_id=s.id
            WHERE a.activo=1 AND s.turno_id=?
        """, (id_turno,))
        total = cur.fetchone()["n"]
        cur.execute("""
            SELECT COUNT(*) AS n FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            WHERE ast.fecha=? AND ast.tipo='clases' AND ast.estado='Puntual'
              AND s.turno_id=?
        """, (fecha_d, id_turno))
        puntuales = cur.fetchone()["n"]
        cur.execute("""
            SELECT COUNT(*) AS n FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            WHERE ast.fecha=? AND ast.tipo='clases' AND ast.estado='Tardanza'
              AND s.turno_id=?
        """, (fecha_d, id_turno))
        tardanzas = cur.fetchone()["n"]
        cur.execute("""
            SELECT COUNT(*) AS n FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            WHERE ast.fecha=? AND ast.tipo='clases' AND ast.estado='Falta'
              AND s.turno_id=?
        """, (fecha_d, id_turno))
        faltas = cur.fetchone()["n"]
        cur.execute("""
            SELECT COUNT(*) AS n FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            WHERE ast.fecha=? AND ast.estado='Permiso' AND s.turno_id=?
        """, (fecha_d, id_turno))
        permisos = cur.fetchone()["n"]
        cur.execute("""
            SELECT COUNT(*) AS n FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            WHERE ast.fecha=? AND ast.tipo='reforzamiento'
              AND ast.estado='Asistio' AND s.turno_id=?
        """, (fecha_d, id_turno))
        ref_asistio = cur.fetchone()["n"]
        cur.execute("""
            SELECT COUNT(*) AS n FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            WHERE ast.fecha=? AND ast.justificada=1 AND s.turno_id=?
        """, (fecha_d, id_turno))
        just_hoy = cur.fetchone()["n"]
        cur.execute("""
            SELECT COUNT(DISTINCT b.alumno_id) AS n FROM bloqueos b
            JOIN alumnos a ON b.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            WHERE b.activo=1 AND s.turno_id=?
        """, (id_turno,))
        bloqueados = cur.fetchone()["n"]
    return {"total": total, "puntuales": puntuales, "tardanzas": tardanzas,
            "faltas": faltas, "ref_asistio": ref_asistio,
            "bloqueados": bloqueados, "justificadas": just_hoy,
            "permisos": permisos}


def ultimos_registros_turno(fecha, id_turno, limite=30):
    return leer_df("""
        SELECT a.dni,
        a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,
        a.nombres, g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
        ast.tipo, ast.hora, ast.estado
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id=a.id
        JOIN secciones s ON a.seccion_id=s.id
        JOIN grados g ON s.grado_id=g.id
        JOIN turnos t ON s.turno_id=t.id
        WHERE ast.fecha=? AND ast.hora IS NOT NULL AND s.turno_id=?
        ORDER BY ast.hora DESC LIMIT ?
    """, (to_date(fecha), id_turno, limite))


def _turnos_para_panel():
    ha = hora_corta()
    resultado = []
    turnos = listar_turnos()
    turnos.sort(key=lambda t: (0 if t["nombre"] == "Mañana" else 1, t["id"]))
    for t in turnos:
        for v in listar_ventanas(t["id"]):
            if v["tipo"] != VENT_CLASES:
                continue
            if v["hora_apertura"] <= ha <= v["hora_cierre"]:
                resultado.append({"turno": t, "ventana": v, "estado": "activo"})
            else:
                resultado.append({"turno": t, "ventana": v, "estado": "cerrado"})
    return resultado


# ─── CIERRE ANUAL ──────────────────────────────────────────────────────────
def reporte_cierre_anual(pid):
    return leer_df("""
        SELECT g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,
        COUNT(DISTINCT a.id) AS total_alumnos,
        (SELECT COUNT(*) FROM asistencias ast
         WHERE ast.periodo_id=? AND ast.alumno_id IN
            (SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?)
           AND ast.estado='Puntual') AS puntuales,
        (SELECT COUNT(*) FROM asistencias ast
         WHERE ast.periodo_id=? AND ast.alumno_id IN
            (SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?)
           AND ast.estado='Tardanza') AS tardanzas,
        (SELECT COUNT(*) FROM asistencias ast
         WHERE ast.periodo_id=? AND ast.alumno_id IN
            (SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?)
           AND ast.estado='Falta' AND ast.justificada=0) AS faltas_injust,
        (SELECT COUNT(*) FROM asistencias ast
         WHERE ast.periodo_id=? AND ast.alumno_id IN
            (SELECT id FROM alumnos WHERE seccion_id=s.id AND periodo_id=?)
           AND ast.estado='Falta' AND ast.justificada=1) AS faltas_just
        FROM secciones s JOIN grados g ON s.grado_id=g.id
        JOIN turnos t ON s.turno_id=t.id
        LEFT JOIN alumnos a ON a.seccion_id=s.id AND a.periodo_id=?
        GROUP BY s.id, g.nombre, s.nombre, t.nombre
        ORDER BY t.nombre,g.nombre,s.nombre
    """, [pid] * 9)


def reporte_detallado_por_mes(pid):
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM periodos WHERE id=?", (pid,))
        p = cur.fetchone()
        if not p:
            return {}
        fi = p["fecha_inicio"]
        ff = p["fecha_fin"]
        if isinstance(fi, str):
            fi = datetime.strptime(fi, "%Y-%m-%d").date()
        if isinstance(ff, str):
            ff = datetime.strptime(ff, "%Y-%m-%d").date()

    res = {}
    cur_d = date(fi.year, fi.month, 1)
    while cur_d <= ff:
        um = monthrange(cur_d.year, cur_d.month)[1]
        im = max(cur_d.replace(day=1), fi)
        fm = min(cur_d.replace(day=um), ff)
        df = leer_df("""
            SELECT a.dni,
            a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,
            a.nombres,g.nombre AS grado,s.nombre AS seccion,t.nombre AS turno,
            ast.fecha,ast.hora,ast.tipo,ast.estado,ast.justificada,
            COALESCE(ast.observacion,'') AS observacion
            FROM asistencias ast JOIN alumnos a ON ast.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE ast.periodo_id=? AND ast.fecha BETWEEN ? AND ?
            ORDER BY ast.fecha,t.nombre,g.nombre,s.nombre,a.apellido_paterno
        """, (pid, im, fm))
        res[f"{cur_d.year}-{str(cur_d.month).zfill(2)}"] = df
        cur_d = (date(cur_d.year + 1, 1, 1) if cur_d.month == 12
                 else date(cur_d.year, cur_d.month + 1, 1))
    return res


def cerrar_ano_escolar(usuario, pid, nuevo_nombre, fi, ff):
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM periodos WHERE id=?", (pid,))
        p = cur.fetchone()
        if not p:
            return False, "Periodo no encontrado."
        if p["cerrado"]:
            return False, "Ese periodo ya esta cerrado."
        rep_json = None
        try:
            rep = reporte_cierre_anual(pid)
            if not rep.empty:
                rep_json = rep.to_json(orient="records", force_ascii=False)
        except Exception:
            rep_json = None
        fecha_str = timestamp_str()
        cur.execute("""
            INSERT INTO cierres_anuales(periodo_id,fecha_cierre,generado_por,
                reporte_json) VALUES(?,?,?,?)
        """, (pid, fecha_str, usuario["usuario"], rep_json))
        cur.execute("UPDATE periodos SET activo=0,fecha_cierre=?,cerrado=1 "
                    "WHERE id=?", (fecha_str, pid))
        cur.execute("UPDATE alumnos SET activo=0,retirado_en=? WHERE periodo_id=?",
                    (fecha_str, pid))
        cur.execute("""
            INSERT INTO periodos(nombre,fecha_inicio,fecha_fin,activo,cerrado)
            VALUES(?,?,?,1,0)
        """, (nuevo_nombre, to_date(fi), to_date(ff)))
    auditar(usuario["usuario"], f"Cerro periodo {p['nombre']}",
            tb="periodos", rid=pid)
    return True, f"Periodo '{p['nombre']}' cerrado. Nuevo: '{nuevo_nombre}'."


def listar_cierres_anuales():
    return leer_df("""
        SELECT c.id,p.nombre AS periodo,c.fecha_cierre,c.generado_por
        FROM cierres_anuales c JOIN periodos p ON c.periodo_id=p.id
        ORDER BY c.id DESC
    """)

# ─── GENERACIÓN DE PDF ─────────────────────────────────────────────────────
def generar_pdf_tabla_ancha(df, titulo, subtitulo=None, fuente_chica=False):
    from reportlab.lib import colors  # 🆕
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from io import BytesIO

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(A4[1], A4[0]),
                            rightMargin=15, leftMargin=15,
                            topMargin=20, bottomMargin=20)
    est = getSampleStyleSheet()
    el = [Paragraph(f"<b>{titulo}</b>", est["Heading1"])]
    if subtitulo:
        el.append(Paragraph(subtitulo, est["Normal"]))
    el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"),
                        est["Normal"]))
    el.append(Spacer(1, 12))
    if not df.empty:
        datos = [df.columns.tolist()] + df.astype(str).values.tolist()
        anchos = []
        for col in df.columns:
            cl = str(col).lower()
            if any(k in cl for k in ["auxiliar", "apellido", "nombres",
                                      "alumno", "observacion"]):
                anchos.append(3.5)
            elif any(k in cl for k in ["grado", "seccion", "turno",
                                        "tipo", "estado"]):
                anchos.append(1.5)
            elif any(k in cl for k in ["fecha", "hora"]):
                anchos.append(1.8)
            else:
                anchos.append(1.0)
        at = A4[1] - 30
        sm = sum(anchos)
        anchos = [a * at / sm for a in anchos]
        GRIS = colors.HexColor("#FAFAFA")
        if fuente_chica:
            fc = ff = 6
            pad = 3
        else:
            fc = 11
            ff = 10
            pad = 5
        t = Table(datos, colWidths=anchos, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), fc),
            ("FONTSIZE", (0, 1), (-1, -1), ff),
            ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.black),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), pad),
            ("RIGHTPADDING", (0, 0), (-1, -1), pad),
            ("TOPPADDING", (0, 0), (-1, -1), pad),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GRIS]),
        ]))
        el.append(t)
    doc.build(el)
    buf.seek(0)
    return buf.getvalue()


def generar_pdf_multilhoja(hojas, titulo_base="Reporte"):
    from reportlab.lib import colors  # 🆕
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, PageBreak, Paragraph, Spacer, Table, TableStyle
    from io import BytesIO

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(A4[1], A4[0]),
                            rightMargin=15, leftMargin=15,
                            topMargin=20, bottomMargin=20)
    est = getSampleStyleSheet()
    el = []
    primera = True
    for nombre_hoja, bloques in hojas.items():
        if not primera:
            el.append(PageBreak())
        primera = False
        el.append(Paragraph(f"<b>{titulo_base}</b>", est["Heading2"]))
        el.append(Paragraph(f"<b>{nombre_hoja}</b>", est["Heading3"]))
        el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"),
                            est["Normal"]))
        el.append(Spacer(1, 10))

        es_hoja_faltas = (nombre_hoja == "ALUMNOS QUE FALTARON")

        for titulo_bloque, df in bloques:
            el.append(Paragraph(f"<b>{titulo_bloque}</b>", est["Heading4"]))
            el.append(Spacer(1, 4))
            if df is None or df.empty:
                el.append(Paragraph("Sin faltas registradas.", est["Normal"]))
                el.append(Spacer(1, 10))
                continue

            datos = [df.columns.tolist()] + df.astype(str).values.tolist()
            at = A4[1] - 30

            if es_hoja_faltas:
                ancho_grado = 1.6 * 28.35
                ancho_alumno = (at - 2 * ancho_grado) / 2
                col_widths = [ancho_grado, ancho_alumno,
                              ancho_grado, ancho_alumno]
                t = Table(datos, colWidths=col_widths, repeatRows=1)
                estilos = [
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LINEAFTER", (1, 0), (1, -1), 1.5, colors.black),
                    ("BACKGROUND", (0, 0), (1, 0), colors.HexColor("#E8F5E9")),
                    ("BACKGROUND", (2, 0), (3, 0), colors.HexColor("#FFEBEE")),
                ]
                for i in range(1, len(datos)):
                    if i % 2 == 0:
                        estilos.append(("BACKGROUND", (0, i), (1, i),
                                        colors.HexColor("#F1F8E9")))
                        estilos.append(("BACKGROUND", (2, i), (3, i),
                                        colors.HexColor("#FFF3E0")))
                t.setStyle(TableStyle(estilos))
            else:
                n_cols = len(df.columns)
                ac = at / n_cols
                t = Table(datos, colWidths=[ac] * n_cols, repeatRows=1)
                estilos = [
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
                col_estado = None
                for j, c in enumerate(df.columns):
                    if str(c).lower() == "estado":
                        col_estado = j
                        break
                if col_estado is not None:
                    mapa = {
                        "P": colors.HexColor("#22C55E"),
                        "T": colors.HexColor("#F59E0B"),
                        "J": colors.HexColor("#EAB308"),
                        "F": colors.HexColor("#EF4444"),
                        "PERMISO": colors.HexColor("#3B82F6"),
                    }
                    for i in range(1, len(datos)):
                        val = str(datos[i][col_estado]).strip().upper()
                        if val in mapa:
                            estilos.append(
                                ("TEXTCOLOR", (col_estado, i),
                                 (col_estado, i), mapa[val]))
                            estilos.append(
                                ("FONTNAME", (col_estado, i),
                                 (col_estado, i), "Helvetica-Bold"))
                t.setStyle(TableStyle(estilos))

            el.append(t)
            el.append(Spacer(1, 12))
    doc.build(el)
    buf.seek(0)
    return buf.getvalue()



# ─── CARNETS ───────────────────────────────────────────────────────────────
def _generar_fotocheck_pil(alumno, escudo_path=None):
    from PIL import Image, ImageDraw, ImageFont
    from pathlib import Path as _P
    import qrcode
    import datetime

    BASE_DIR = _P(__file__).resolve().parent

    ANCHO_PX = 817
    ALTO_PX = 550
    NARANJA_OSCURO = (184, 92, 56)
    NARANJA_CLARO = (212, 132, 96)
    NARANJA_FRANJA = (184, 92, 56)
    FONDO = (255, 240, 225)
    BLANCO = (255, 255, 255)
    NEGRO = (17, 17, 17)
    GRIS_LABEL = (90, 60, 30)
    GRIS_TXT = (150, 150, 150)

    img = Image.new("RGB", (ANCHO_PX, ALTO_PX), FONDO)
    draw = ImageDraw.Draw(img)
    color_punto = (235, 210, 180)
    paso = 22
    radio = 1
    for py in range(0, ALTO_PX, paso):
        for px in range(0, ANCHO_PX, paso):
            draw.ellipse([px - radio, py - radio, px + radio, py + radio],
                         fill=color_punto)

    if escudo_path and _P(escudo_path).exists():
        try:
            esc = Image.open(str(escudo_path)).convert("RGBA").resize(
                (280, 280), Image.LANCZOS)
            a = esc.split()[3]
            a = a.point(lambda p: int(p * 0.15))
            esc.putalpha(a)
            img.paste(esc, ((ANCHO_PX - 280) // 2 + 100, (ALTO_PX - 280) // 2),
                      esc)
        except Exception:
            pass

    FOTO_W = 216
    FOTO_H = 280
    FRANJA_W = FOTO_W
    for y in range(ALTO_PX):
        t = y / ALTO_PX
        r = int(NARANJA_OSCURO[0] + (NARANJA_CLARO[0] - NARANJA_OSCURO[0]) * t)
        g = int(NARANJA_OSCURO[1] + (NARANJA_CLARO[1] - NARANJA_OSCURO[1]) * t)
        b = int(NARANJA_OSCURO[2] + (NARANJA_CLARO[2] - NARANJA_OSCURO[2]) * t)
        draw.line([(0, y), (FRANJA_W, y)], fill=(r, g, b))

    def _font(size, bold=False, italic=False):
        if bold and italic:
            nombres = ["arialbi.ttf"]
        elif bold:
            nombres = ["arialbd.ttf"]
        elif italic:
            nombres = ["ariali.ttf"]
        else:
            nombres = ["arial.ttf"]

        for n in nombres:
            try:
                return ImageFont.truetype(str(BASE_DIR / n), size)
            except Exception:
                pass

        fallback = ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
                    if bold else
                    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")
        try:
            return ImageFont.truetype(fallback, size)
        except Exception:
            pass

        return ImageFont.load_default()

    def _dibujar_texto(x, y, texto, font, color):
        draw.text((x, y), texto, fill=color, font=font)

    f_colegio = _font(18, bold=True)
    f_foto = _font(24, bold=True)
    
    # TÍTULO DE ARRIBA (TAMAÑO 32)
    f_titulo = _font(32, bold=True)
    
    # 👇 FRASE TAMAÑO 32 EXACTO (Igual que el título) SIN CURSIVA 👇
    f_frase = _font(32, bold=True, italic=False) 
    
    f_label = _font(26, bold=True)

    esc_size = int(FRANJA_W * 0.75)
    esc_x = (FRANJA_W - esc_size) // 2
    espacio = ALTO_PX - FOTO_H
    alto_bloque_texto = 3 * 18
    alto_bloque_total = esc_size + 8 + alto_bloque_texto
    esc_y = max(8, (espacio - alto_bloque_total) // 2 + 6)

    if escudo_path and _P(escudo_path).exists():
        try:
            esc = Image.open(str(escudo_path)).convert("RGBA").resize(
                (esc_size, esc_size), Image.LANCZOS)
            img.paste(esc, (esc_x, esc_y), esc)
        except Exception:
            pass

    def _txt_centrado(texto, y, font, color):
        try:
            bbox = draw.textbbox((0, 0), texto, font=font)
            tw = bbox[2] - bbox[0]
        except Exception:
            tw = len(texto) * 7
        _dibujar_texto((FRANJA_W - tw) // 2, y, texto, font, color)

    y_txt = esc_y + esc_size + 6
    for lbl in ["INSTITUCION", "EDUCATIVA", "YARINACOCHA"]:
        _txt_centrado(lbl, y_txt, f_colegio, BLANCO)
        y_txt += 18

    foto_x = 0
    foto_y = ALTO_PX - FOTO_H
    draw.rectangle([foto_x, foto_y, foto_x + FOTO_W, foto_y + FOTO_H],
                   fill=BLANCO)
    draw.rectangle([foto_x, foto_y, foto_x + FOTO_W - 1, foto_y + FOTO_H - 1],
                   outline=NARANJA_FRANJA, width=1)
    try:
        bbox = draw.textbbox((0, 0), "FOTO", font=f_foto)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
    except Exception:
        tw = 40
        th = 12
    draw.text((foto_x + (FOTO_W - tw) // 2, foto_y + (FOTO_H - th) // 2),
              "FOTO", fill=GRIS_TXT, font=f_foto)

    DER_X = FRANJA_W + 14
    titulo_txt = "FOTOCHECK DEL ESTUDIANTE"
    try:
        bbox = draw.textbbox((0, 0), titulo_txt, font=f_titulo)
        tw = bbox[2] - bbox[0]
    except Exception:
        tw = 280
    espacio_d = ANCHO_PX - DER_X
    _dibujar_texto(DER_X + (espacio_d - tw) // 2, 10, titulo_txt,
                   f_titulo, NEGRO)

    ap_p = (alumno.get('apellido_paterno') or "").upper()
    ap_m = (alumno.get('apellido_materno') or "").upper()
    nombres = (alumno.get('nombres') or "").upper()
    dni = alumno["dni"]
    
    grado_val = alumno.get('grado')
    if 'grado_a_ui' in globals():
        grado_val = grado_a_ui(grado_val)
    grado = (grado_val or "").upper()
    
    seccion = (alumno.get('seccion') or "").upper()
    turno = (alumno.get('turno') or "").upper()

    try:
        if 'obtener_periodo_activo' in globals():
            per = obtener_periodo_activo()
            periodo_txt = per["nombre"] if per else str(datetime.datetime.now().year)
        else:
            periodo_txt = str(datetime.datetime.now().year)
    except Exception:
        periodo_txt = str(datetime.datetime.now().year)

    info_x = DER_X
    QR_SIZE = 215
    qr_x = ANCHO_PX - QR_SIZE - 12
    ancho_info = qr_x - info_x - 12

    def _ajustar(texto, size_ini, max_ancho, bold):
        size = size_ini
        while size > 10:
            f = _font(size, bold=bold)
            try:
                ancho = draw.textlength(texto, font=f)
            except Exception:
                ancho = len(texto) * size * 0.55
            if ancho <= max_ancho:
                return f
            size -= 1
        return _font(10, bold=bold)

    INFO_Y = 95
    alto_linea = 60 
    
    ap_full = ap_p + " " + ap_m
    _dibujar_texto(info_x, INFO_Y, ap_full,
                   _ajustar(ap_full, 46, ancho_info, True), NEGRO)
    y2 = INFO_Y + alto_linea
    _dibujar_texto(info_x, y2, nombres,
                   _ajustar(nombres, 46, ancho_info, True), NEGRO)

    def _linea(y, label, valor, size=42):
        _dibujar_texto(info_x, y, label, f_label, GRIS_LABEL)
        try:
            an = draw.textlength(label, font=f_label)
        except Exception:
            an = len(label) * 15
        vx = info_x + int(an) + 10
        _dibujar_texto(vx, y, valor,
                       _ajustar(valor, size, ancho_info - int(an) - 10, True),
                       NEGRO)

    _linea(y2 + alto_linea, "DNI:", dni)
    _linea(y2 + 2 * alto_linea, "GRADO:", f'{grado} "{seccion}"')
    _linea(y2 + 3 * alto_linea, "TURNO:", turno)
    _linea(y2 + 4 * alto_linea, "PERIODO:", periodo_txt)

    qr = qrcode.QRCode(version=1, box_size=10, border=1)
    qr.add_data(str(alumno["dni"]).strip())
    qr.make(fit=True)
    qr_img_pil = qr.make_image(fill_color="black",
                               back_color="white").convert("RGB").resize(
        (QR_SIZE, QR_SIZE), Image.LANCZOS)
    
    img.paste(qr_img_pil, (qr_x, 65))

    # =====================================================================
    # 👇 FRASE TAMAÑO 32 (Igual que el título) SIN COMILLAS Y RECTA 👇
    # =====================================================================
    frase = 'Ser del CNY, es ser mejor'
    
    frase_centro_x = DER_X + espacio_d // 2
    frase_y = ALTO_PX - 55 

    try:
        bbox = draw.textbbox((0, 0), frase, font=f_frase)
        fw = bbox[2] - bbox[0]
    except Exception:
        fw = 500

    _dibujar_texto(frase_centro_x - (fw // 2), frase_y, frase, f_frase, NARANJA_FRANJA)

    return img
    
def _render_carnets(filas, titulo=None):
    # 🆕 IMPORTS LOCALES (Lazy Loading)
    import tempfile
    import os
    import gc
    from io import BytesIO
    from pathlib import Path
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, 
                                     PageBreak, Table, TableStyle, 
                                     Image as RLImage)

    # Guardamos el PDF en un archivo temporal, NO en la RAM
    temp_file = tempfile.NamedTemporaryFile(delete=False, suffix=".pdf")
    temp_path = temp_file.name
    temp_file.close()

    m = 5
    doc = SimpleDocTemplate(temp_path, pagesize=A4, rightMargin=m, leftMargin=m,
                            topMargin=m, bottomMargin=m)
    est = getSampleStyleSheet()
    MM = 2.8346
    ANCHO = 85.0 * MM
    ALTO = 53.0 * MM
    escudo_path = Path("escudo.png")

    def _fotocheck(alumno):
        img = _generar_fotocheck_pil(
            alumno, escudo_path if escudo_path.exists() else None)
        ib = BytesIO()
        img.save(ib, format="PNG")
        ib.seek(0)
        return RLImage(ib, width=ANCHO, height=ALTO)

    el = []
    if titulo:
        el.append(Paragraph(f"<b>{titulo}</b>", est["Heading2"]))
        el.append(Spacer(1, 6))
    
    sep_x = 6
    sep_y = 10
    caben_x = 2
    alto_hoja = A4[1] - 2 * m
    caben_y = max(1, int((alto_hoja + sep_y) // (ALTO + sep_y)))
    
    # 👇 AQUÍ ESTÁ EL TRUCO: Procesamos de 2 en 2, no todos de golpe
    for i in range(0, len(filas), caben_x * caben_y):
        lote = filas[i:i + caben_x * caben_y]
        
        # Generamos SOLO las imágenes de este lote
        fotochecks_lote = [_fotocheck(a) for a in lote]
        
        tabla_filas = []
        for j in range(0, len(fotochecks_lote), caben_x):
            fila = fotochecks_lote[j:j + caben_x]
            while len(fila) < caben_x:
                fila.append("")
            tabla_filas.append(fila)
            
        while len(tabla_filas) < caben_y:
            tabla_filas.append([""] * caben_x)
            
        t = Table(tabla_filas, colWidths=[ANCHO, ANCHO],
                  rowHeights=[ALTO] * len(tabla_filas), hAlign="CENTER")
        t.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
            ("LEFTPADDING", (0, 0), (-1, -1), sep_x / 2),
            ("RIGHTPADDING", (0, 0), (-1, -1), sep_x / 2),
            ("TOPPADDING", (0, 0), (-1, -1), sep_y / 2),
            ("BOTTOMPADDING", (0, 0), (-1, -1), sep_y / 2),
        ]))
        el.append(t)
        
        if i + caben_x * caben_y < len(filas):
            el.append(PageBreak())
            
        # 👇 LIBERAMOS LA MEMORIA DE ESTE LOTE
        del fotochecks_lote
        gc.collect()

    doc.build(el)
    
    # Leemos el archivo y lo devolvemos
    with open(temp_path, "rb") as f:
        pdf_bytes = f.read()
    
    try:
        os.remove(temp_path)
    except Exception:
        pass
        
    return pdf_bytes

def _filas_alumnos_por_seccion(idsec):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno
            FROM alumnos a JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE a.seccion_id=? AND a.activo=1
            ORDER BY a.apellido_paterno,a.apellido_materno
        """, (idsec,))
        return [dict(r) for r in cur.fetchall()]


def pdf_carnets_por_seccion(idsec):
    f = _filas_alumnos_por_seccion(idsec)
    return _render_carnets(f) if f else None


def pdf_carnets_seleccionados(ids, titulo=None):
    if not ids:
        return None
    ph = ",".join(["?"] * len(ids))
    with cursor() as (con, cur):
        cur.execute(f"""
            SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
            FROM alumnos a
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE a.id IN ({ph}) AND a.activo=1
            ORDER BY a.apellido_paterno, a.apellido_materno
        """, tuple(ids))
        filas = [dict(r) for r in cur.fetchall()]
    return _render_carnets(filas, titulo) if filas else None

def pdf_carnets_todos():
    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.*,s.nombre AS seccion,g.nombre AS grado,t.nombre AS turno
            FROM alumnos a JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE a.activo=1
            ORDER BY t.nombre, g.nombre, s.nombre,
                     a.apellido_paterno, a.apellido_materno
        """)
        filas = [dict(r) for r in cur.fetchall()]
    return _render_carnets(filas, "Carnets - Todos los salones") if filas else None


def pdf_carnet_alumno(dni):
    from reportlab.platypus import SimpleDocTemplate # 🆕 FIX
    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
            FROM alumnos a
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE a.dni=? AND a.activo=1
        """, (dni,))
        filas = [dict(r) for r in cur.fetchall()]
    return _render_carnets(filas) if filas else None


def generar_qr(dni, size=220):
    import qrcode  # 🆕 Carga diferida
    qr = qrcode.QRCode(version=1, error_correction=qrcode.constants.ERROR_CORRECT_H,
                        box_size=10, border=2)
    qr.add_data(str(dni).strip())
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return img


def generar_pdf_tabla_ancha(df, titulo, subtitulo=None, fuente_chica=False):
    from reportlab.lib import colors  # 🆕
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from io import BytesIO

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(A4[1], A4[0]),
                            rightMargin=15, leftMargin=15,
                            topMargin=20, bottomMargin=20)
    est = getSampleStyleSheet()
    el = [Paragraph(f"<b>{titulo}</b>", est["Heading1"])]
    if subtitulo:
        el.append(Paragraph(subtitulo, est["Normal"]))
    el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"),
                        est["Normal"]))
    el.append(Spacer(1, 12))
    if not df.empty:
        datos = [df.columns.tolist()] + df.astype(str).values.tolist()
        anchos = []
        for col in df.columns:
            cl = str(col).lower()
            if any(k in cl for k in ["auxiliar", "apellido", "nombres",
                                      "alumno", "observacion"]):
                anchos.append(3.5)
            elif any(k in cl for k in ["grado", "seccion", "turno",
                                        "tipo", "estado"]):
                anchos.append(1.5)
            elif any(k in cl for k in ["fecha", "hora"]):
                anchos.append(1.8)
            else:
                anchos.append(1.0)
        at = A4[1] - 30
        sm = sum(anchos)
        anchos = [a * at / sm for a in anchos]
        GRIS = colors.HexColor("#FAFAFA")
        if fuente_chica:
            fc = ff = 6
            pad = 3
        else:
            fc = 11
            ff = 10
            pad = 5
        t = Table(datos, colWidths=anchos, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, 0), fc),
            ("FONTSIZE", (0, 1), (-1, -1), ff),
            ("LINEBELOW", (0, 0), (-1, 0), 1.5, colors.black),
            ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#CCCCCC")),
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("LEFTPADDING", (0, 0), (-1, -1), pad),
            ("RIGHTPADDING", (0, 0), (-1, -1), pad),
            ("TOPPADDING", (0, 0), (-1, -1), pad),
            ("BOTTOMPADDING", (0, 0), (-1, -1), pad),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, GRIS]),
        ]))
        el.append(t)
    doc.build(el)
    buf.seek(0)
    return buf.getvalue()


def generar_pdf_multilhoja(hojas, titulo_base="Reporte"):
    from reportlab.lib import colors  # 🆕
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, PageBreak, Paragraph, Spacer, Table, TableStyle
    from io import BytesIO

    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=(A4[1], A4[0]),
                            rightMargin=15, leftMargin=15,
                            topMargin=20, bottomMargin=20)
    est = getSampleStyleSheet()
    el = []
    primera = True
    for nombre_hoja, bloques in hojas.items():
        if not primera:
            el.append(PageBreak())
        primera = False
        el.append(Paragraph(f"<b>{titulo_base}</b>", est["Heading2"]))
        el.append(Paragraph(f"<b>{nombre_hoja}</b>", est["Heading3"]))
        el.append(Paragraph("Generado: " + ahora().strftime("%Y-%m-%d %H:%M"),
                            est["Normal"]))
        el.append(Spacer(1, 10))

        es_hoja_faltas = (nombre_hoja == "ALUMNOS QUE FALTARON")

        for titulo_bloque, df in bloques:
            el.append(Paragraph(f"<b>{titulo_bloque}</b>", est["Heading4"]))
            el.append(Spacer(1, 4))
            if df is None or df.empty:
                el.append(Paragraph("Sin faltas registradas.", est["Normal"]))
                el.append(Spacer(1, 10))
                continue

            datos = [df.columns.tolist()] + df.astype(str).values.tolist()
            at = A4[1] - 30

            if es_hoja_faltas:
                ancho_grado = 1.6 * 28.35
                ancho_alumno = (at - 2 * ancho_grado) / 2
                col_widths = [ancho_grado, ancho_alumno,
                              ancho_grado, ancho_alumno]
                t = Table(datos, colWidths=col_widths, repeatRows=1)
                estilos = [
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
                    ("TOPPADDING", (0, 0), (-1, -1), 3),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
                    ("LINEAFTER", (1, 0), (1, -1), 1.5, colors.black),
                    ("BACKGROUND", (0, 0), (1, 0), colors.HexColor("#E8F5E9")),
                    ("BACKGROUND", (2, 0), (3, 0), colors.HexColor("#FFEBEE")),
                ]
                for i in range(1, len(datos)):
                    if i % 2 == 0:
                        estilos.append(("BACKGROUND", (0, i), (1, i),
                                        colors.HexColor("#F1F8E9")))
                        estilos.append(("BACKGROUND", (2, i), (3, i),
                                        colors.HexColor("#FFF3E0")))
                t.setStyle(TableStyle(estilos))
            else:
                n_cols = len(df.columns)
                ac = at / n_cols
                t = Table(datos, colWidths=[ac] * n_cols, repeatRows=1)
                estilos = [
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 10),
                    ("LINEBELOW", (0, 0), (-1, 0), 1.2, colors.black),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#CCCCCC")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 3),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 3),
                    ("TOPPADDING", (0, 0), (-1, -1), 2),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 2),
                ]
                col_estado = None
                for j, c in enumerate(df.columns):
                    if str(c).lower() == "estado":
                        col_estado = j
                        break
                if col_estado is not None:
                    mapa = {
                        "P": colors.HexColor("#22C55E"),
                        "T": colors.HexColor("#F59E0B"),
                        "J": colors.HexColor("#EAB308"),
                        "F": colors.HexColor("#EF4444"),
                        "PERMISO": colors.HexColor("#3B82F6"),
                    }
                    for i in range(1, len(datos)):
                        val = str(datos[i][col_estado]).strip().upper()
                        if val in mapa:
                            estilos.append(
                                ("TEXTCOLOR", (col_estado, i),
                                 (col_estado, i), mapa[val]))
                            estilos.append(
                                ("FONTNAME", (col_estado, i),
                                 (col_estado, i), "Helvetica-Bold"))
                t.setStyle(TableStyle(estilos))

            el.append(t)
            el.append(Spacer(1, 12))
    doc.build(el)
    buf.seek(0)
    return buf.getvalue()


def pdf_resumen_alumno(idal):
    from reportlab.lib import colors  # 🆕
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
    from io import BytesIO

    d = perfil_alumno_datos(idal)
    if not d:
        return None
    al = d["alumno"]
    buf = BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, rightMargin=30, leftMargin=30,
                            topMargin=30, bottomMargin=30)
    est = getSampleStyleSheet()
    el = []
    el.append(Paragraph("<b>Reporte de Asistencia</b>", est["Heading1"]))
    el.append(Paragraph("I.E. Yarinacocha", est["Normal"]))
    el.append(Spacer(1, 20))
    nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") +
              ", " + al['nombres'])
    el.append(Paragraph(f"<b>Alumno:</b> {nombre}", est["Normal"]))
    el.append(Paragraph(f"<b>DNI:</b> {al['dni']}", est["Normal"]))
    el.append(Paragraph(
        f"<b>Grado:</b> {al['grado']} {al['seccion']} - {al['turno']}",
        est["Normal"]))
    el.append(Spacer(1, 15))
    el.append(Paragraph("<b>Resumen</b>", est["Heading2"]))
    resumen = [["Puntuales", "Tardanzas", "Faltas", "Reforzamiento", "Tard. injust."],
               [d["total_puntuales"], d["total_tardanzas"], d["total_faltas"],
                d["total_ref_asistio"], d["tard_injust"]]]
    t = Table(resumen)
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(C_PRIMARIO)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    el.append(t)
    doc.build(el)
    buf.seek(0)
    return buf.getvalue()


# ─── EXCEL ─────────────────────────────────────────────────────────────────
def df_a_xlsx(df, hoja="Datos"):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        df.to_excel(w, index=False, sheet_name=hoja)
    buf.seek(0)
    return buf.getvalue()


def df_a_xlsx_multilhoja(hojas):
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as w:
        for n, df in hojas.items():
            df.to_excel(w, index=False, sheet_name=n[:31])
    buf.seek(0)
    return buf.getvalue()


# ─── HOJAS DE REPORTE ──────────────────────────────────────────────────────
def _hoja_faltaron_dia(fecha, ids_secs):
    todos_just = []
    todos_injust = []

    for idsec in ids_secs:
        with cursor() as (con, cur):
            cur.execute("""SELECT s.id, s.nombre AS seccion, g.nombre AS grado,
                           t.nombre AS turno FROM secciones s
                           JOIN grados g ON s.grado_id=g.id
                           JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""",
                        (idsec,))
            sec = cur.fetchone()
        if not sec:
            continue

        grado_secc = f"{sec['grado']} {sec['seccion']}"

        df_just = leer_df("""
            SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno
            FROM alumnos a JOIN asistencias ast ON ast.alumno_id=a.id
            WHERE a.seccion_id=? AND a.activo=1 AND ast.fecha=?
              AND ast.tipo='clases' AND ast.estado='Falta' AND ast.justificada=1
            ORDER BY a.apellido_paterno, a.apellido_materno
        """, (idsec, to_date(fecha)))

        df_injust = leer_df("""
            SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno
            FROM alumnos a JOIN asistencias ast ON ast.alumno_id=a.id
            WHERE a.seccion_id=? AND a.activo=1 AND ast.fecha=?
              AND ast.tipo='clases' AND ast.estado='Falta' AND ast.justificada=0
            ORDER BY a.apellido_paterno, a.apellido_materno
        """, (idsec, to_date(fecha)))

        for _, r in df_just.iterrows():
            todos_just.append({"Grado": grado_secc, "Alumno": r["Alumno"]})
        for _, r in df_injust.iterrows():
            todos_injust.append({"Grado": grado_secc, "Alumno": r["Alumno"]})

    filas = []
    n_max = max(len(todos_just), len(todos_injust))

    for i in range(n_max):
        fila = {}
        if i < len(todos_just):
            fila["J_Grado"] = todos_just[i]["Grado"]
            fila["J_Alumno"] = todos_just[i]["Alumno"]
        else:
            fila["J_Grado"] = ""
            fila["J_Alumno"] = ""
        if i < len(todos_injust):
            fila["F_Grado"] = todos_injust[i]["Grado"]
            fila["F_Alumno"] = todos_injust[i]["Alumno"]
        else:
            fila["F_Grado"] = ""
            fila["F_Alumno"] = ""
        filas.append(fila)

    if filas:
        df_lado = pd.DataFrame(filas).rename(columns={
            "J_Grado": "Grado/Secc.",
            "J_Alumno": "Apellidos y Nombres (JUST)",
            "F_Grado": "Grado/Secc. ",
            "F_Alumno": "Apellidos y Nombres (INJUST)",
        })
    else:
        df_lado = pd.DataFrame(columns=[
            "Grado/Secc.", "Apellidos y Nombres (JUST)",
            "Grado/Secc. ", "Apellidos y Nombres (INJUST)",
        ])

    titulo = (f"FALTAS DEL DIA {fecha}   "
              f"| JUSTIFICADAS: {len(todos_just)}   "
              f"| INJUSTIFICADAS: {len(todos_injust)}")
    return [(titulo, df_lado)]


def _generar_hojas_diario(fecha, ids_secs):
    hojas = {}
    for idsec in ids_secs:
        with cursor() as (con, cur):
            cur.execute("""SELECT s.id,s.nombre AS seccion,g.nombre AS grado,
                           t.nombre AS turno FROM secciones s
                           JOIN grados g ON s.grado_id=g.id
                           JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""",
                        (idsec,))
            sec = cur.fetchone()
        if not sec:
            continue
        df_clases = leer_df("""
            SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS Apellidos,
            a.nombres AS Nombres,
            CASE
              WHEN ast.estado IS NULL THEN '-'
              WHEN ast.estado='Puntual' THEN 'P'
              WHEN ast.estado='Tardanza' THEN 'T'
              WHEN ast.estado='Falta' AND ast.justificada=1 THEN 'J'
              WHEN ast.estado='Falta' THEN 'F'
              WHEN ast.estado='Permiso' THEN 'PERMISO'
              ELSE ast.estado END AS Estado
            FROM alumnos a
            LEFT JOIN asistencias ast ON ast.alumno_id=a.id AND ast.fecha=?
              AND ast.tipo IN ('clases','evento')
            WHERE a.seccion_id=? AND a.activo=1
            ORDER BY a.apellido_paterno,a.apellido_materno
        """, (to_date(fecha), idsec))
        if not df_clases.empty:
            df_clases.insert(0, "N", range(1, len(df_clases) + 1))

        df_ref = leer_df("""
            SELECT a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS Apellidos,
            a.nombres AS Nombres, 'Vino' AS Estado, COALESCE(ast.hora,'') AS Hora
            FROM alumnos a JOIN asistencias ast ON ast.alumno_id=a.id
            WHERE a.seccion_id=? AND a.activo=1 AND ast.fecha=?
              AND ast.tipo='reforzamiento' AND ast.estado='Asistio'
            ORDER BY a.apellido_paterno
        """, (idsec, to_date(fecha)))
        if not df_ref.empty:
            df_ref.insert(0, "N", range(1, len(df_ref) + 1))

        nombre_hoja = f"{sec['grado']} {sec['seccion']} - {sec['turno']}"
        hojas[nombre_hoja] = [
            ("ASISTENCIA DE CLASES   (P=Puntual | T=Tardanza | J=Falta just. | F=Falta | PERMISO=azul)", df_clases),
            ("ALUMNOS QUE VINIERON A REFORZAMIENTO", df_ref),
        ]

    bloques_faltaron = _hoja_faltaron_dia(fecha, ids_secs)
    hojas = {"ALUMNOS QUE FALTARON": bloques_faltaron, **hojas}
    return hojas


def _generar_hojas_mensual(mes, ano, ids_secs):
    hojas = {}
    ult = monthrange(ano, mes)[1]
    ini = date(ano, mes, 1)
    fin = date(ano, mes, ult)

    dias_especiales_mes = set()
    with cursor() as (con, cur):
        cur.execute("""
            SELECT fecha FROM dias_especiales
            WHERE fecha BETWEEN ? AND ? AND activo=1 AND tipo='evento'
        """, (ini, fin))
        for r in cur.fetchall():
            f = r["fecha"]
            if isinstance(f, str):
                f = datetime.strptime(f, "%Y-%m-%d").date()
            dias_especiales_mes.add(f)

    for idsec in ids_secs:
        with cursor() as (con, cur):
            cur.execute("""SELECT s.id,s.nombre AS seccion,g.nombre AS grado,
                           t.nombre AS turno FROM secciones s
                           JOIN grados g ON s.grado_id=g.id
                           JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""",
                        (idsec,))
            sec = cur.fetchone()
        if not sec:
            continue

        dias = []
        for d in range(1, ult + 1):
            f = date(ano, mes, d)
            if f.weekday() >= 5 and f not in dias_especiales_mes:
                continue
            dias.append(f)

        df_al = leer_df("""
            SELECT a.id AS alumno_id,
            a.apellido_paterno||' '||COALESCE(a.apellido_materno,'') AS apellidos,
            a.nombres FROM alumnos a WHERE a.seccion_id=? AND a.activo=1
            ORDER BY a.apellido_paterno
        """, (idsec,))
        if df_al.empty:
            continue

        asis = {}
        with cursor() as (con, cur):
            cur.execute("""
                SELECT alumno_id,fecha,estado,justificada FROM asistencias
                WHERE fecha BETWEEN ? AND ? AND tipo='clases'
                  AND alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=?)
            """, (ini, fin, idsec))
            for r in cur.fetchall():
                asis[(r["alumno_id"], fmt_date(r["fecha"]))] = (
                    "F" if r["estado"] == "Falta" else "P")

        ref_map = {}
        with cursor() as (con, cur):
            cur.execute("""
                SELECT alumno_id,fecha FROM asistencias
                WHERE fecha BETWEEN ? AND ? AND tipo='reforzamiento'
                  AND estado='Asistio'
                  AND alumno_id IN (SELECT id FROM alumnos WHERE seccion_id=?)
            """, (ini, fin, idsec))
            for r in cur.fetchall():
                ref_map.setdefault(r["alumno_id"], []).append(fmt_date(r["fecha"]))

        filas = []
        for _, al in df_al.iterrows():
            fila = {"Apellidos": al["apellidos"], "Nombres": al["nombres"]}
            tp = tf = 0
            for d in dias:
                f_str = d.strftime("%Y-%m-%d")
                etq = ["Lun", "Mar", "Mie", "Jue", "Vie", "Sab", "Dom"][d.weekday()] + " " + str(d.day).zfill(2)
                est = asis.get((al["alumno_id"], f_str), "-")
                fila[etq] = est
                if est == "P":
                    tp += 1
                elif est == "F":
                    tf += 1
            fila["Total P"] = tp
            fila["Total F"] = tf
            filas.append(fila)
        df_cal = pd.DataFrame(filas)
        cols_base = ["Apellidos", "Nombres"]
        cols_dias = [c for c in df_cal.columns
                     if c not in cols_base and not c.startswith("Total")]
        df_cal = df_cal[cols_base + cols_dias + ["Total P", "Total F"]]

        filas_f = []
        for _, al in df_al.iterrows():
            faltas = sum(1 for d in dias
                         if asis.get((al["alumno_id"], d.strftime("%Y-%m-%d"))) == "F")
            if faltas > 0:
                filas_f.append({"Apellidos": al["apellidos"],
                                "Nombres": al["nombres"],
                                "Faltas en el mes": faltas})
        df_faltas = (pd.DataFrame(filas_f) if filas_f
                     else pd.DataFrame(columns=["Apellidos", "Nombres",
                                                 "Faltas en el mes"]))

        filas_r = []
        for _, al in df_al.iterrows():
            refs = ref_map.get(al["alumno_id"], [])
            if refs:
                filas_r.append({"Apellidos": al["apellidos"],
                                "Nombres": al["nombres"],
                                "Veces que vino": len(refs)})
        df_ref = (pd.DataFrame(filas_r) if filas_r
                  else pd.DataFrame(columns=["Apellidos", "Nombres",
                                              "Veces que vino"]))

        nombre_hoja = f"{sec['grado']} {sec['seccion']} - {sec['turno']}"
        hojas[nombre_hoja] = [
            ("CALENDARIO DEL MES", df_cal),
            ("CONTEO DE FALTAS (solo los que faltaron)", df_faltas),
            ("REFORZAMIENTO (solo los que vinieron)", df_ref),
        ]
    return hojas

# ─── SELECTORES REUTILIZABLES ──────────────────────────────────────────────
def _selector_grado_seccion(clave, titulo="Elige el grado"):
    idsec = st.session_state.get(clave + "_idsec")
    if idsec:
        return idsec

    idg = st.session_state.get(clave + "_grado")
    if idg:
        g = next((x for x in listar_grados() if x["id"] == idg), None)
        if not g:
            st.session_state.pop(clave + "_grado", None)
            st.rerun()
            return None
        if st.button("← Regresar a grados", key=clave + "_volver_g"):
            st.session_state.pop(clave + "_grado", None)
            st.rerun()
        st.subheader(f"Secciones de {g['nombre']}")
        secs = secciones_por_grado(g["id"])
        if not secs:
            st.info("Sin secciones.")
            return None
        for s in secs:
            n = len(alumnos_de_seccion(s["id"]))
            if st.button(f"{s['nombre']}  ({n})", width='stretch',
                         key=f"{clave}_s_{s['id']}"):
                st.session_state[clave + "_idsec"] = s["id"]
                st.rerun()
        return None

    st.markdown(f"### {titulo}")
    grados = listar_grados()
    if not grados:
        st.info("No hay grados.")
        return None
    for g in grados:
        secs = secciones_por_grado(g["id"])
        if st.button(f"{g['nombre']}  ({len(secs)})", width='stretch',
                     key=f"{clave}_g_{g['id']}"):
            st.session_state[clave + "_grado"] = g["id"]
            st.rerun()
    return None


def _selector_alumno_lista(clave, idsec, on_click_key):
    with cursor() as (con, cur):
        cur.execute("""SELECT s.id, s.nombre AS seccion, g.nombre AS grado,
                       t.nombre AS turno FROM secciones s
                       JOIN grados g ON s.grado_id=g.id
                       JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""",
                    (idsec,))
        sec = cur.fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop(clave + "_idsec", None)
        st.session_state.pop(clave + "_grado", None)
        st.rerun()
        return None

    if st.button("← Regresar a secciones", key=clave + "_volver_s"):
        st.session_state.pop(clave + "_idsec", None)
        st.session_state.pop(on_click_key, None)
        st.rerun()

    st.markdown(f"### {sec['grado']} {sec['seccion']} - Turno {sec['turno']}")

    df = alumnos_de_seccion(idsec)
    if df.empty:
        st.info("Sin alumnos activos.")
        return None

    st.caption(f"{len(df)} alumnos. Aprieta un nombre para seleccionarlo.")
    for _, al in df.iterrows():
        if st.button(al["nombre_completo"], width='stretch',
                     key=f"{clave}_al_{al['id']}"):
            st.session_state[on_click_key] = al["id"]
            st.rerun()

    idal = st.session_state.get(on_click_key)
    if not idal:
        return None

    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
                   a.seccion_id, a.nombre_apoderado, a.telefono_apoderado,
                   g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
            FROM alumnos a JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
            WHERE a.id=?
        """, (idal,))
        al = cur.fetchone()
    if not al:
        return None
    return dict(al)


def _selector_alumno_accion(clave, idsec, texto_boton, key_boton, on_click_key):
    with cursor() as (con, cur):
        cur.execute("""SELECT s.id, s.nombre AS seccion, g.nombre AS grado,
                       t.nombre AS turno FROM secciones s
                       JOIN grados g ON s.grado_id=g.id
                       JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""",
                    (idsec,))
        sec = cur.fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop(clave + "_idsec", None)
        st.session_state.pop(clave + "_grado", None)
        st.rerun()
        return None

    if st.button("← Regresar a secciones", key=clave + "_volver_s"):
        st.session_state.pop(clave + "_idsec", None)
        st.session_state.pop(on_click_key, None)
        st.rerun()

    st.markdown(f"### {sec['grado']} {sec['seccion']} - Turno {sec['turno']}")

    df = alumnos_de_seccion(idsec)
    if df.empty:
        st.info("Sin alumnos activos.")
        return None

    st.caption(f"{len(df)} alumnos. Aprieta '{texto_boton}' en el alumno que necesites.")

    for _, al in df.iterrows():
        c1, c2 = st.columns([5, 1])
        with c1:
            st.markdown(
                f"<div style='padding: 10px 0; font-size: 15px;'>"
                f"{al['nombre_completo']}</div>",
                unsafe_allow_html=True)
        with c2:
            if st.button(texto_boton, key=f"{key_boton}_{al['id']}",
                         width='stretch'):
                st.session_state[on_click_key] = al["id"]
                st.rerun()

    idal = st.session_state.get(on_click_key)
    if not idal:
        return None

    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
                   a.seccion_id, a.nombre_apoderado, a.telefono_apoderado,
                   g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
            FROM alumnos a JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
            WHERE a.id=?
        """, (idal,))
        al = cur.fetchone()
    if not al:
        return None
    return dict(al)


def _reset_selector(clave, on_click_key=None):
    for k in (clave + "_idsec", clave + "_grado"):
        st.session_state.pop(k, None)
    if on_click_key:
        st.session_state.pop(on_click_key, None)


# ─── PUERTA ────────────────────────────────────────────────────────────────
def _puerta_puede_operar(fecha):
    f_d = to_date(fecha)
    with cursor() as (con, cur):
        cur.execute("""
            SELECT descripcion FROM dias_especiales
            WHERE fecha=? AND activo=1 AND tipo='feriado' LIMIT 1
        """, (f_d,))
        fer = cur.fetchone()
        if fer:
            return False, f"Feriado / sin clases: {fer['descripcion'] or ''}.", None
        if es_fin_de_semana():
            cur.execute("""
                SELECT COUNT(*) AS n FROM dias_especiales
                WHERE fecha=? AND activo=1 AND tipo='evento'
            """, (f_d,))
            if cur.fetchone()["n"] == 0:
                return False, "Hoy no es dia laboral.", None
        return True, "", None


def _puerta_escanear(usuario, fecha):
    ok, msg, _ = _puerta_puede_operar(fecha)
    if not ok:
        (st.info if "Feriado" in msg else st.warning)(msg)
        return
        
    with cursor() as (con, cur):
        cur.execute("""
            SELECT d.descripcion, d.hora_entrada,
                   COALESCE(t.nombre,'Ambos') AS turno,
                   CASE WHEN (SELECT COUNT(*) FROM dias_especiales_secciones ds
                              WHERE ds.dia_especial_id = d.id) > 0
                        THEN 'algunas secciones' ELSE 'todo el colegio' END AS alcance
            FROM dias_especiales d
            LEFT JOIN turnos t ON d.turno_id = t.id
            WHERE d.fecha=? AND d.activo=1 AND d.tipo='evento'
            ORDER BY d.hora_entrada
        """, (to_date(fecha),))
        for ev in cur.fetchall():
            st.info(f"Evento: **{ev['descripcion']}** — "
                    f"entrada {fmt_time(ev['hora_entrada'])} — "
                    f"{ev['turno']} — {ev['alcance']}")
            
    # 🆕 FIX CÁMARA: Forzamos un ID de montaje único
    if "puerta_qr_mount" not in st.session_state:
        st.session_state["puerta_qr_mount"] = 0
        
    escaner_qr_continuo(key=f"puerta_qr_{st.session_state['puerta_qr_mount']}")


def _puerta_manual_alumnos(idsec, usuario):
    with cursor() as (con, cur):
        cur.execute("""SELECT s.id, s.nombre AS seccion, g.nombre AS grado,
                       t.nombre AS turno FROM secciones s
                       JOIN grados g ON s.grado_id=g.id
                       JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""",
                    (idsec,))
        sec = cur.fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        return
    if st.button("← Regresar a secciones", key="pm_volver_s"):
        st.session_state.pop("pm_sel_idsec", None)
        st.rerun()

    st.markdown(f"### {sec['grado']} {sec['seccion']} - Turno {sec['turno']}")
    df = alumnos_de_seccion(idsec)
    if df.empty:
        st.info("Sin alumnos activos.")
        return

    st.caption(f"{len(df)} alumnos. Aprieta MARCAR en cada uno que llegue.")
    for _, al in df.iterrows():
        c1, c2 = st.columns([5, 1])
        with c1:
            st.markdown(
                f"<div style='padding: 8px 0; font-size: 15px;'>"
                f"{al['nombre_completo']}</div>",
                unsafe_allow_html=True)
        with c2:
            if st.button("Marcar", key=f"pm_marcar_{al['id']}",
                         width='stretch'):
                _, _, msg, _ = registrar_entrada(
                    al["dni"], usuario, origen="manual")
                st.toast(msg)
                st.rerun()


def _puerta_manual(usuario, fecha):
    ok, msg, _ = _puerta_puede_operar(fecha)
    if not ok:
        (st.info if "Feriado" in msg else st.warning)(msg)
        return
    ha = hora_corta()
    turnos_activos = []
    for t in listar_turnos():
        for v in listar_ventanas(t["id"]):
            if v["hora_apertura"] <= ha <= v["hora_cierre"]:
                turnos_activos.append({"turno_id": t["id"], "turno": t["nombre"],
                                        "ventana": v["nombre"]})
                break
    if not turnos_activos:
        st.info("No hay ventanas activas en este momento.")
        return
    st.caption("Ventanas activas: " +
               " | ".join([f"{t['turno']} ({t['ventana']})"
                            for t in turnos_activos]))
    st.warning("El modo manual es solo para emergencias.")

    clave = "pm_sel"
    idsec = _selector_grado_seccion(clave, "Elige el grado")
    if not idsec:
        return
    _puerta_manual_alumnos(idsec, usuario)


def vista_puerta():
    usuario = st.session_state["user"]
    fecha = hoy_str()
    st.markdown(f"""
    <div class="puerta-header">
        <div class="titulo">Control de Puerta</div>
        <div class="fecha">{fecha}</div>
    </div>
    """, unsafe_allow_html=True)
    
    if "puerta_modo" not in st.session_state:
        st.session_state["puerta_modo"] = "Escanear QR"
        
    modo = st.radio("Modo", ["Escanear QR", "Manual"], key="puerta_modo",
                    horizontal=True, label_visibility="collapsed")
                    
    if modo == "Escanear QR":
        # 🆕 FIX CÁMARA: Si acaba de entrar al escáner, forzamos un reinicio limpio
        if st.session_state.get("_ultimo_modo") != "Escanear QR":
            st.session_state["puerta_qr_mount"] = st.session_state.get("puerta_qr_mount", 0) + 1
            st.session_state["_ultimo_modo"] = "Escanear QR"
            
        _puerta_escanear(usuario, fecha)
    else:
        st.session_state["_ultimo_modo"] = "Manual"
        _puerta_manual(usuario, fecha)

# ─── BLOQUEADOS ────────────────────────────────────────────────────────────
def vista_bloqueados():
    st.title("Alumnos bloqueados")
    st.caption(
        "Reglas automaticas: alumno nuevo → 3 tardanzas → bloqueo al dia siguiente. "
        "Alumno reincidente → 1 tardanza → bloqueo al dia siguiente. "
        "Al desbloquear, el alumno sigue con la misma regla."
    )
    usuario = st.session_state["user"]

    tabs = st.tabs(["Lista de Bloqueados", "Bloquear Nuevo Alumno"])

    # PESTAÑA 1: VER Y DESBLOQUEAR
    with tabs[0]:
        df = listar_bloqueados()
        st.metric("Total bloqueados", len(df))

        if df.empty:
            st.success("No hay alumnos bloqueados actualmente.")
        else:
            st.dataframe(df, width='stretch', hide_index=True)

            st.markdown("---")
            st.subheader("Desbloquear alumno")

            clave = "bloq_sel"
            idsec = _selector_grado_seccion(clave, "Elige el grado del alumno a desbloquear")
            if not idsec:
                return

            al = _selector_alumno_accion(
                clave, idsec,
                texto_boton="Seleccionar",
                key_boton="bloq_sel_btn",
                on_click_key="bloq_al_sel_id")
            if not al:
                return

            bloq = alumno_bloqueado(al["id"])
            if not bloq:
                st.warning("Este alumno ya no esta bloqueado.")
                if st.button("Volver", key="bloq_volver_sin_bloqueo"):
                    _reset_selector(clave, "bloq_al_sel_id")
                    st.rerun()
                return

            st.markdown("---")
            st.markdown(f"### Desbloquear a {al['apellido_paterno']} "
                        f"{(al['apellido_materno'] or '')}, {al['nombres']}")
            st.markdown(f"**DNI:** {al['dni']}  \n"
                        f"**{al['grado']} {al['seccion']}** — Turno {al['turno']}")

            st.info(f"Motivo del bloqueo: **{bloq['motivo']}**  \n"
                    f"Fecha de bloqueo: **{bloq['fecha_inicio']}**")

            with st.expander("Historial completo de bloqueos", expanded=False):
                st.dataframe(historial_bloqueos_alumno(al["id"]), width='stretch',
                             hide_index=True)

            st.markdown("---")
            motivo = st.text_input("Motivo de desbloqueo (obligatorio)",
                                    key=f"bloq_motivo_{al['id']}")

            if confirmar_accion(f"bloq_{al['id']}",
                                texto_boton="Desbloquear alumno",
                                texto_check="¿Estas seguro de desbloquear a este alumno?"):
                if not motivo.strip():
                    st.error("El motivo es obligatorio.")
                else:
                    liberar_bloqueo(al["id"], usuario, motivo.strip())
                    st.toast(f"Alumno {al['nombres']} desbloqueado.")
                    _reset_selector(clave, "bloq_al_sel_id")
                    st.rerun()

            if st.button("Cancelar", key=f"bloq_cancel_{al['id']}"):
                _reset_selector(clave, "bloq_al_sel_id")
                st.rerun()

    # PESTAÑA 2: BLOQUEAR MANUALMENTE
    with tabs[1]:
        st.subheader("Bloquear a un alumno manualmente")
        st.caption("Usa esta opcion si necesitas bloquear a alguien por faltas graves, acumulacion, etc.")
        
        # 🆕 VARIABLE DE CONTROL DE FLUJO
        if "bloq_man_id_activo" not in st.session_state:
            st.session_state["bloq_man_id_activo"] = None
            
        # Si la bandera de éxito está encendida, limpiamos TODO y recargamos
        if st.session_state.get("_exito_bloqueo_manual"):
            st.session_state["_exito_bloqueo_manual"] = False
            st.session_state["bloq_man_id_activo"] = None
            st.session_state.pop("bloq_man_sel_id", None)
            st.session_state.pop("bloq_man_grado", None)
            st.session_state.pop("bloq_man_idsec", None)
            st.rerun()

        # Si no hay nadie seleccionado, mostramos los selectores
        if not st.session_state["bloq_man_id_activo"]:
            clave_man = "bloq_man"
            idsec_man = _selector_grado_seccion(clave_man, "Elige el grado del alumno a bloquear")
            if not idsec_man:
                return
            al_man = _selector_alumno_accion(
                clave_man, idsec_man,
                texto_boton="Seleccionar",
                key_boton="bloq_man_btn",
                on_click_key="bloq_man_sel_id")
            
            # Si el usuario selecciona a alguien, lo guardamos y recargamos
            if al_man:
                st.session_state["bloq_man_id_activo"] = al_man["id"]
                st.rerun()
            return

        # Si ya hay alguien seleccionado, mostramos sus datos
        al_man_id = st.session_state["bloq_man_id_activo"]
        
        with cursor() as (con, cur):
            cur.execute("""
                SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
                       g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
                FROM alumnos a JOIN secciones s ON a.seccion_id=s.id
                JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
                WHERE a.id=?
            """, (al_man_id,))
            al_man_row = cur.fetchone()
            
        if not al_man_row:
            st.session_state["bloq_man_id_activo"] = None
            st.rerun()
            return
            
        al_man = dict(al_man_row)
        bloq_man = alumno_bloqueado(al_man["id"])
        
        if bloq_man:
            st.warning(f"Este alumno ya esta bloqueado (Motivo: {bloq_man['motivo']}).")
        else:
            st.markdown("---")
            st.markdown(f"### Bloquear a {al_man['apellido_paterno']} {(al_man['apellido_materno'] or '')}, {al_man['nombres']}")
            st.markdown(f"**DNI:** {al_man['dni']}  \n**{al_man['grado']} {al_man['seccion']}** — Turno {al_man['turno']}")
            
            motivo_manual = st.text_input("Motivo del bloqueo manual (obligatorio)", key=f"bloq_man_motivo_{al_man['id']}")
            
            if confirmar_accion(f"bloq_man_{al_man['id']}", texto_boton="Bloquear Alumno Ahora", texto_check="¿Estas seguro de BLOQUEAR a este alumno?"):
                if not motivo_manual.strip():
                    st.error("El motivo es obligatorio.")
                else:
                    crear_bloqueo(al_man["id"], f"BLOQUEO MANUAL: {motivo_manual.strip()}", usuario, origen="manual")
                    st.toast(f"Alumno {al_man['nombres']} bloqueado manualmente.")
                    st.session_state["_exito_bloqueo_manual"] = True
                    st.rerun()

        if st.button("← Buscar otro alumno", key="bloq_man_buscar_otro"):
            st.session_state["bloq_man_id_activo"] = None
            st.rerun()
# ─── PANEL DIRECCION ───────────────────────────────────────────────────────
def vista_panel_direccion():
    st.title("Panel Direccion")
    fecha = hoy_str()
    turnos_info = _turnos_para_panel()
    if not turnos_info:
        st.info("No hay turnos configurados.")
        return

    etiquetas = []
    for info in turnos_info:
        t = info["turno"]
        est = "EN CURSO" if info["estado"] == "activo" else "CERRADO"
        etiquetas.append(f"{t['nombre']} ({est})")

    sel = st.radio("Turno", etiquetas, horizontal=True,
                   key="panel_dir_turno", label_visibility="collapsed")
    idx = etiquetas.index(sel)
    info = turnos_info[idx]
    turno = info["turno"]
    ventana = info["ventana"]
    estado = info["estado"]

    if estado == "activo":
        st.success(f"Turno **{turno['nombre']}** en curso — "
                   f"ventana: {ventana['nombre']} "
                   f"({ventana['hora_apertura']} a {ventana['hora_cierre']})")
    else:
        st.info(f"Turno **{turno['nombre']}** ya cerro — "
                f"ventana: {ventana['nombre']} "
                f"(cerro a las {ventana['hora_cierre']})")

    if st.button("Actualizar Datos", key="refresh_panel"):
        marcar_faltas_al_cierre()
        st.rerun()

    m = metricas_dia_turno(fecha, turno["id"])
    st.subheader(f"Resumen del turno {turno['nombre']}")
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total alumnos", m["total"])
    c2.metric("Puntuales", m["puntuales"])
    c3.metric("Tardanzas", m["tardanzas"])
    c4.metric("Faltas", m["faltas"])
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Reforzamiento asistio", m["ref_asistio"])
    c2.metric("Bloqueados", m["bloqueados"])
    c3.metric("Justificadas hoy", m["justificadas"])
    c4.metric("Permisos hoy", m["permisos"])


#REPORTES 
def _rep_seleccionar_seccion(usuario):
    rol = usuario["rol"]
    permitidas = None
    if rol == "Auxiliar":
        with cursor() as (con, cur):
            cur.execute("SELECT seccion_id FROM auxiliar_secciones WHERE usuario_id=?",
                        (usuario["id"],))
            permitidas = [f["seccion_id"] for f in cur.fetchall()]
        if not permitidas:
            st.info("No tienes secciones asignadas. Contacta al Admin.")
            return

    if st.session_state.get("rep_idsec"):
        _rep_pantalla_tipos(st.session_state["rep_idsec"])
        return

    idg = st.session_state.get("rep_grado_sel")
    if idg:
        g = next((x for x in listar_grados() if x["id"] == idg), None)
        if not g:
            st.session_state.pop("rep_grado_sel", None)
            st.rerun()
            return
        if st.button("← Regresar a grados", key="rep_volver_g"):
            st.session_state.pop("rep_grado_sel", None)
            st.rerun()
        st.subheader(f"Secciones de {g['nombre']}")
        secs = secciones_por_grado(g["id"])
        if permitidas is not None:
            secs = [s for s in secs if s["id"] in permitidas]
        if not secs:
            st.info("No hay secciones visibles para este grado.")
            return
        for s in secs:
            n = len(alumnos_de_seccion(s["id"]))
            if st.button(f"{s['nombre']}  ({n})", width='stretch',
                         key="rep_s_" + str(s["id"])):
                st.session_state["rep_idsec"] = s["id"]
                st.rerun()
        return

    st.markdown("### Elige el grado")

    if rol == "Auxiliar" and permitidas:
        st.markdown("---")
        st.markdown("**Descargar Reporte Diario de TODAS mis secciones**")
        if st.button("Descargar Reporte Diario (todas mis secciones)",
                     type="primary", width='stretch', key="rep_dl_todas_diario"):
            hojas = _generar_hojas_diario(hoy_str(), permitidas)
            if not hojas:
                st.warning("Sin datos para hoy.")
            else:
                pdf = generar_pdf_multilhoja(hojas,
                                              "Reporte Diario - " + hoy_str())
                st.download_button("Guardar PDF", pdf,
                                   f"Reporte_diario_todas_{hoy_str()}.pdf",
                                   "application/pdf", width='stretch',
                                   key="rep_dl_todas_diario_pdf")
        st.markdown("---")

    grados_mostrar = []
    for g in listar_grados():
        secs = secciones_por_grado(g["id"])
        if permitidas is not None:
            secs = [s for s in secs if s["id"] in permitidas]
        if secs:
            grados_mostrar.append({"grado": g, "n": len(secs)})
    if not grados_mostrar:
        st.info("No hay secciones disponibles.")
        return
    for item in grados_mostrar:
        if st.button(f"{item['grado']['nombre']}  ({item['n']})",
                     width='stretch',
                     key="rep_g_" + str(item["grado"]["id"])):
            st.session_state["rep_grado_sel"] = item["grado"]["id"]
            st.rerun()


def _rep_pantalla_tipos(idsec):
    with cursor() as (con, cur):
        cur.execute("""SELECT s.id, s.nombre AS seccion, g.nombre AS grado,
                       t.nombre AS turno FROM secciones s
                       JOIN grados g ON s.grado_id=g.id
                       JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""", (idsec,))
        sec = cur.fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop("rep_idsec", None)
        st.rerun()
        return
    if st.button("← Regresar a secciones", key="rep_volver_secciones"):
        st.session_state.pop("rep_idsec", None)
        st.session_state.pop("rep_tipo", None)
        st.rerun()
    st.subheader(f"{sec['grado']} {sec['seccion']} - Turno {sec['turno']}")

    hoy = ahora().date()
    ult_dia_mes = date(hoy.year, hoy.month, monthrange(hoy.year, hoy.month)[1])
    ult_lab = ult_dia_mes
    while ult_lab.weekday() >= 5:
        ult_lab -= timedelta(days=1)
    cierre_activo = hoy >= ult_lab

    st.markdown("### Elige el tipo de reporte")
    c1, c2, c3 = st.columns(3)
    with c1:
        if st.button("Reporte Diario", width='stretch',
                     key="rep_tipo_btn_diario"):
            st.session_state["rep_tipo"] = "diario"
            st.session_state["rep_idsec_val"] = idsec
            st.rerun()
    with c2:
        if cierre_activo:
            if st.button("Cierre Mensual", width='stretch',
                         key="rep_tipo_btn_mensual"):
                st.session_state["rep_tipo"] = "mensual"
                st.session_state["rep_idsec_val"] = idsec
                st.rerun()
        else:
            st.button("Cierre Mensual", width='stretch', disabled=True,
                      key="rep_tipo_btn_mensual_disabled",
                      help=f"Se activa a partir del {ult_lab}.")
    with c3:
        if st.button("Reporte General", width='stretch',
                     key="rep_tipo_btn_general"):
            st.session_state["rep_tipo"] = "general"
            st.session_state["rep_idsec_val"] = idsec
            st.rerun()

    if not cierre_activo:
        st.caption(f"El Cierre Mensual se activara el **{ult_lab}**.")

    if st.session_state.get("rep_tipo") and \
            st.session_state.get("rep_idsec_val") == idsec:
        if st.button("← Regresar a tipos", key="rep_volver_tipos"):
            st.session_state.pop("rep_tipo", None)
            st.rerun()
        st.markdown("---")
        tipo = st.session_state["rep_tipo"]
        if tipo == "diario":
            _rep_mostrar_reporte(idsec, "diario",
                                  ahora().date() - timedelta(days=30),
                                  ahora().date())
        elif tipo == "mensual":
            _rep_mostrar_reporte(idsec, "mensual",
                                  ahora().date() - timedelta(days=30),
                                  ahora().date())
        elif tipo == "general":
            _rep_general_por_turno_admin()


def _rep_mostrar_reporte(idsec, tipo, desde, hasta):
    with cursor() as (con, cur):
        cur.execute("""SELECT s.id, s.nombre AS seccion, g.nombre AS grado,
                       t.nombre AS turno FROM secciones s
                       JOIN grados g ON s.grado_id=g.id
                       JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""", (idsec,))
        sec = cur.fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop("rep_idsec_val", None)
        st.session_state.pop("rep_tipo", None)
        st.rerun()
        return
    if st.button("← Regresar a tipos de reporte",
                 key="rep_volver_tipos_desde_mostrar"):
        st.session_state.pop("rep_tipo", None)
        st.session_state.pop("rep_idsec_val", None)
        st.rerun()

    if tipo == "diario":
        st.subheader(f"Reporte Diario - {sec['grado']} {sec['seccion']}")
        st.caption(f"Fecha: {ahora().date()}")
        hojas = _generar_hojas_diario(hoy_str(), [idsec])
        if not hojas:
            st.info("Sin datos para hoy.")
            return
        pdf = generar_pdf_multilhoja(hojas,
                                      "Reporte Diario - " + str(ahora().date()))
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "Descargar PDF", pdf,
                f"Reporte_diario_{sec['grado']}_{sec['seccion']}_{hoy_str()}.pdf",
                "application/pdf", key="rep_dl_diario_pdf")
        with c2:
            hojas_xlsx = {}
            for nombre, bloques in hojas.items():
                for titulo_bloque, df in bloques:
                    hojas_xlsx[(nombre + "_" + titulo_bloque)[:31]] = df
            if hojas_xlsx:
                st.download_button(
                    "Descargar Excel",
                    df_a_xlsx_multilhoja(hojas_xlsx),
                    f"Reporte_diario_{sec['grado']}_{sec['seccion']}_{hoy_str()}.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="rep_dl_diario_xlsx")
    elif tipo == "mensual":
        hoy = ahora().date()
        st.subheader(f"Cierre Mensual {MESES_ES[hoy.month]} {hoy.year} - "
                     f"{sec['grado']} {sec['seccion']}")
        hojas = _generar_hojas_mensual(hoy.month, hoy.year, [idsec])
        if not hojas:
            st.info("Sin datos para este mes.")
            return
        pdf = generar_pdf_multilhoja(
            hojas, f"Cierre Mensual {MESES_ES[hoy.month]} {hoy.year}")
        c1, c2 = st.columns(2)
        with c1:
            st.download_button(
                "Descargar PDF", pdf,
                f"Cierre_mensual_{sec['grado']}_{sec['seccion']}_{MESES_ES[hoy.month]}.pdf",
                "application/pdf", key="rep_dl_mensual_pdf")
        with c2:
            hojas_xlsx = {}
            for nombre, bloques in hojas.items():
                for titulo_bloque, df in bloques:
                    hojas_xlsx[(nombre + "_" + titulo_bloque)[:31]] = df
            if hojas_xlsx:
                st.download_button(
                    "Descargar Excel",
                    df_a_xlsx_multilhoja(hojas_xlsx),
                    f"Cierre_mensual_{sec['grado']}_{sec['seccion']}.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    key="rep_dl_mensual_xlsx")
    elif tipo == "general":
        _rep_general_por_turno_admin()


def _rep_general_por_turno_admin():
    st.subheader("Reporte general por auxiliar")
    st.caption("Muestra TODAS las secciones del turno. Si no hay asistencias, "
               "aparecen con 0.")
    turnos = listar_turnos()
    if not turnos:
        st.info("Sin turnos configurados.")
        return
    ops = {t["nombre"]: t["id"] for t in turnos}
    sel_turno = st.selectbox("Turno", list(ops.keys()), key="repgen_turno")
    id_turno = ops[sel_turno]
    ha = hora_corta()
    ventana_clases = None
    for v in listar_ventanas(id_turno):
        if v["tipo"] == "clases":
            ventana_clases = v
            break
    if not ventana_clases:
        st.warning("Este turno no tiene ventana de clases configurada.")
        return
    if ha < ventana_clases["hora_cierre"]:
        st.warning(f"**No se puede generar el reporte todavia.** La ventana de "
                   f"clases de {sel_turno} aun esta abierta (cierra a las "
                   f"{ventana_clases['hora_cierre']}).")
        return

    hoy_d = ahora().date()
    c1, c2 = st.columns(2)
    with c1:
        desde = st.date_input("Desde", hoy_d, key="repgen_desde")
    with c2:
        hasta = st.date_input("Hasta", hoy_d, key="repgen_hasta")
    if desde > hasta:
        st.error("La fecha Desde no puede ser mayor que Hasta.")
        return

    pid = None
    dfp = listar_periodos()
    if not dfp.empty:
        ops_p = {}
        for _, r in dfp.iterrows():
            et = f"{r['nombre']} ({r['fecha_inicio']} - {r['fecha_fin']})"
            if r["cerrado"]:
                et += " [CERRADO]"
            elif r["activo"]:
                et += " [ACTIVO]"
            ops_p[et] = r["id"]
        sel_lbl = st.selectbox("Periodo", list(ops_p.keys()), key="repgen_pid")
        pid = ops_p[sel_lbl]

    cond_periodo = " AND ast.periodo_id = ?" if pid is not None else ""

    q = f"""
    SELECT
        s.id AS seccion_id,
        g.nombre AS grado,
        s.nombre AS seccion,
        COALESCE(u.nombres, '(Sin auxiliar)') AS auxiliar,
        COALESCE((
            SELECT COUNT(*) FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id = a.id
            WHERE a.seccion_id = s.id AND a.activo = 1
              AND ast.tipo = 'clases' AND ast.estado = 'Puntual'
              AND ast.fecha BETWEEN ? AND ? {cond_periodo}
        ), 0) AS puntuales,
        COALESCE((
            SELECT COUNT(*) FROM asistencias ast
            JOIN alumnos a ON ast.alumno_id = a.id
            WHERE a.seccion_id = s.id AND a.activo = 1
              AND ast.tipo = 'clases' AND ast.estado = 'Falta'
              AND ast.fecha BETWEEN ? AND ? {cond_periodo}
        ), 0) AS faltas
    FROM secciones s
    JOIN grados g ON s.grado_id = g.id
    LEFT JOIN auxiliar_secciones ause ON ause.seccion_id = s.id
    LEFT JOIN usuarios u ON u.id = ause.usuario_id
        AND u.rol = 'Auxiliar' AND u.activo = 1
    WHERE s.turno_id = ?
    ORDER BY g.nombre, s.nombre, u.nombres
    """

    p = [to_date(desde), to_date(hasta)]
    if pid is not None:
        p.append(pid)
    p += [to_date(desde), to_date(hasta)]
    if pid is not None:
        p.append(pid)
    p.append(id_turno)

    df = leer_df(q, p)
    if df.empty:
        st.warning("No hay secciones creadas en este turno.")
        return

    df["total"] = df["puntuales"] + df["faltas"]

    st.markdown("---")
    st.markdown(f"**Turno {sel_turno}** — del {desde} al {hasta}")
    st.caption(f"{len(df)} secciones en este turno.")

    filas = []
    for aux in df["auxiliar"].unique().tolist():
        df_aux = df[df["auxiliar"] == aux]
        for _, row in df_aux.iterrows():
            filas.append({
                "Auxiliar": aux,
                "Grado": row["grado"],
                "Seccion": row["seccion"],
                "Puntuales": int(row["puntuales"]),
                "Faltas": int(row["faltas"]),
                "Total": int(row["total"]),
            })
        filas.append({
            "Auxiliar": f"  >> Subtotal {aux}",
            "Grado": "", "Seccion": "",
            "Puntuales": int(df_aux["puntuales"].sum()),
            "Faltas": int(df_aux["faltas"].sum()),
            "Total": int(df_aux["total"].sum()),
        })
    filas.append({
        "Auxiliar": "TOTAL GENERAL",
        "Grado": "", "Seccion": "",
        "Puntuales": int(df["puntuales"].sum()),
        "Faltas": int(df["faltas"].sum()),
        "Total": int(df["total"].sum()),
    })
    df_export = pd.DataFrame(filas)
    st.dataframe(df_export, width='stretch', hide_index=True)

    st.markdown("---")
    st.markdown("### Descargar")
    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "Excel", df_a_xlsx(df_export, "General por auxiliar"),
            f"Reporte_general_{sel_turno}_{desde}_{hasta}.xlsx",
            width='stretch', key="repgen_dl_x")
    with c2:
        st.download_button(
            "PDF",
            generar_pdf_tabla_ancha(
                df_export, f"Reporte general {sel_turno}",
                f"{desde} a {hasta}"),
            f"Reporte_general_{sel_turno}_{desde}_{hasta}.pdf",
            "application/pdf", width='stretch', key="repgen_dl_p")


def vista_reportes():
    st.title("Reportes y Consultas")
    usuario = st.session_state["user"]
    rol = usuario["rol"]
    if not st.session_state.get("_rep_iniciado"):
        for k in ["rep_grado_sel", "rep_idsec", "rep_tipo", "rep_idsec_val",
                  "rep_desde_val", "rep_hasta_val", "rep_modo_admin"]:
            st.session_state.pop(k, None)
        st.session_state["_rep_iniciado"] = True

    if rol == "Admin":
        modo = st.radio("Modo",
                        ["Por seccion", "Reporte general por auxiliar"],
                        key="rep_modo_admin", horizontal=True,
                        label_visibility="collapsed")
        if modo == "Reporte general por auxiliar":
            if st.button("← Volver a reportes por seccion",
                         key="rep_volver_modo"):
                st.session_state.pop("rep_modo_admin", None)
                st.session_state["_rep_iniciado"] = False
                st.rerun()
            _rep_general_por_turno_admin()
            return

    if st.session_state.get("rep_tipo") and st.session_state.get("rep_idsec_val"):
        _rep_mostrar_reporte(st.session_state.get("rep_idsec_val"),
                             st.session_state.get("rep_tipo"),
                             ahora().date() - timedelta(days=30),
                             ahora().date())
        return
    if st.session_state.get("rep_idsec"):
        _rep_pantalla_tipos(st.session_state["rep_idsec"])
        return
    _rep_seleccionar_seccion(usuario)


# ─── ALUMNOS UI ────────────────────────────────────────────────────────────
def _frag_crear_alumno():
    st.subheader("Crear alumno manualmente")
    grados = listar_grados()
    if not grados:
        st.warning("No hay grados.")
        return

    c1, c2 = st.columns(2)
    with c1:
        dni = st.text_input("DNI * (8 digitos)", max_chars=8, key="nue_al_dni")
        nom = st.text_input("Nombres *", key="nue_al_nom")
        pat = st.text_input("Apellido Paterno *", key="nue_al_pat")
    with c2:
        mat = st.text_input("Apellido Materno", key="nue_al_mat")
        g = st.selectbox("Grado *", grados, format_func=lambda x: x["nombre"],
                         key="nue_al_grado")
        secs = secciones_por_grado(g["id"]) if g else []
        s = (st.selectbox("Seccion *", secs,
                          format_func=lambda x: x["nombre"],
                          key=f"nue_al_sec_{g['id'] if g else 0}")
             if secs else None)

    c3, c4 = st.columns(2)
    with c3:
        apo = st.text_input("Apoderado (opcional)", key="nue_al_apo")
    with c4:
        tel = st.text_input("Telefono (opcional)", key="nue_al_tel")

    if confirmar_accion("nue_al", "Crear alumno",
                        "¿Estas seguro de crear este alumno?"):
        if not dni or not nom or not pat or not s:
            st.error("Completa obligatorios.")
        elif not re.fullmatch(r"\d{8}", dni.strip()):
            st.error("DNI invalido (8 digitos).")
        else:
            ok, msg = crear_alumno(
                dni.strip(), nom.strip(), pat.strip(), mat.strip(),
                s["id"], apo.strip(), tel.strip(),
                st.session_state["user"])
            if ok:
                for k in ["nue_al_dni", "nue_al_nom", "nue_al_pat",
                           "nue_al_mat", "nue_al_apo", "nue_al_tel"]:
                    st.session_state.pop(k, None)
                st.toast(msg)
                st.rerun()
            else:
                st.error(msg)


def _frag_editar_alumno():
    st.subheader("Editar alumno")
    idg, ids, texto = filtros_grado_seccion_nombre("ed_al")
    if not (texto or idg):
        st.info("Selecciona un grado o busca por nombre/DNI para empezar.")
        return

    df = buscar_alumnos(texto, idg, ids, limite=50)
    if df.empty:
        st.info("Sin coincidencias.")
        return

    ops = {f"{r['nombre_completo']} - {r['grado']} {r['seccion']}": r["id"]
           for _, r in df.iterrows()}
    sel = st.selectbox("Alumno", list(ops.keys()), key="ed_sel")
    idal = ops[sel]

    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.*, g.id AS grado_id, g.nombre AS grado,
                   s.id AS seccion_id, s.nombre AS seccion
            FROM alumnos a
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            WHERE a.id=?
        """, (idal,))
        datos = cur.fetchone()
    if not datos:
        st.error("Alumno no encontrado.")
        return

    st.info(f"**{datos['nombres']} {datos['apellido_paterno']}** — "
            f"DNI: {datos['dni']} (no editable) — "
            f"Actualmente en **{datos['grado']} {datos['seccion']}**")

    c1, c2 = st.columns(2)
    with c1:
        apo = st.text_input("Apoderado",
                            value=datos["nombre_apoderado"] or "",
                            key=f"ed_apo_{idal}")
        tel = st.text_input("Telefono",
                            value=datos["telefono_apoderado"] or "",
                            key=f"ed_tel_{idal}")
    with c2:
        grados = listar_grados()
        idxg = next((i for i, g in enumerate(grados)
                     if g["id"] == datos["grado_id"]), 0)
        g = st.selectbox("Grado *", grados, index=idxg,
                         format_func=lambda x: x["nombre"],
                         key=f"ed_grado_{idal}")
        secs = secciones_por_grado(g["id"]) if g else []

        with cursor() as (con, cur):
            conteo = {}
            for s in secs:
                cur.execute("SELECT COUNT(*) AS n FROM alumnos "
                            "WHERE seccion_id=? AND activo=1", (s["id"],))
                conteo[s["id"]] = cur.fetchone()["n"]

        def _label_sec(s):
            n = conteo.get(s["id"], 0)
            return f"{s['nombre']}  ({n} alumnos)"

        idxs = next((i for i, s in enumerate(secs)
                     if s["id"] == datos["seccion_id"]), 0)
        s = st.selectbox("Seccion *", secs, index=idxs,
                         format_func=_label_sec,
                         key=f"ed_seccion_{idal}_{g['id'] if g else 0}")

    cambio_grado = g and g["id"] != datos["grado_id"]
    cambio_seccion = s and s["id"] != datos["seccion_id"]
    if cambio_grado or cambio_seccion:
        st.warning(
            f"Mover a **{datos['grado']} {datos['seccion']}** -> "
            f"**{g['nombre']} {s['nombre'] if s else '?'}**")

    if confirmar_accion(f"ed_al_{idal}", "Guardar cambios",
                        "¿Estas seguro de guardar estos cambios?"):
        if not g or not s:
            st.error("Selecciona grado y sección.")
        else:
            ok, msg = editar_alumno(idal, apo, tel, s["id"],
                                     datos["dni"], st.session_state["user"])
            if ok:
                st.toast(msg)
                st.rerun()
            else:
                st.error(msg)


def _frag_listar_alumnos():
    # 1. Mostramos los filtros
    idg, ids, texto = filtros_grado_seccion_nombre("list_al")
    
    # 2. Botón para ejecutar la búsqueda
    st.markdown("---")
    if st.button("🔍 Buscar Alumnos", type="primary", width='stretch'):
        st.session_state["_realizar_busqueda_alumnos"] = True

    # 3. Solo mostramos la tabla si se presionó el botón
    if st.session_state.get("_realizar_busqueda_alumnos"):
        # Si no hay filtros aplicados, pedimos que seleccione algo
        if not idg and not ids and not texto:
            st.info("Selecciona un grado, una sección o escribe un nombre para buscar.")
            return

        df = buscar_alumnos(texto, idg, ids, limite=500)
        
        st.write(f"**{len(df)} alumnos encontrados**")
        if df.empty:
            st.info("Sin resultados para esta búsqueda.")
            return

        # Mostramos la tabla ligera
        df_mostrar = df[['nombre_completo', 'grado', 'seccion', 'turno', 'dni']].copy()
        df_mostrar.columns = ["Apellidos y Nombres", "Grado", "Sección", "Turno", "DNI"]
        st.dataframe(df_mostrar, width='stretch', hide_index=True)

        st.markdown("---")
        st.subheader("Ver perfil de un alumno")
        
        # Selector para elegir al alumno de la lista filtrada
        ops = {f"{r['nombre_completo']} - {r['grado']} {r['seccion']}": r["id"] for _, r in df.iterrows()}
        sel = st.selectbox("Selecciona el alumno", list(ops.keys()), key="perfil_sel_lista")
        
        if st.button("Ver perfil completo", width='stretch'):
            st.session_state["perfil_alumno_id"] = ops[sel]
            st.session_state["_realizar_busqueda_alumnos"] = False # Limpiamos la memoria
            st.rerun()

def _perfil_alumno(idal):
    d = perfil_alumno_datos(idal)
    if not d:
        st.warning("Alumno no encontrado.")
        st.session_state.pop("perfil_alumno_id", None)
        return

    al = d["alumno"]
    usuario = st.session_state["user"]

    ultimo_id = st.session_state.get("_perfil_ultimo_id")
    if ultimo_id != idal:
        for k in ("_anular_perm_id", "_anular_jp_id", "justif_id"):
            st.session_state.pop(k, None)
    st.session_state["_perfil_ultimo_id"] = idal

    if st.button("Volver a la lista", key="volver_perfil"):
        st.session_state.pop("perfil_alumno_id", None)
        st.session_state.pop("_perfil_ultimo_id", None)
        st.rerun()

    nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") +
              ", " + al['nombres']).strip(", ")
    inicial = (al['apellido_paterno'] or al['nombres'] or "?")[0].upper()
    badge_txt = "BLOQUEADO" if d["bloqueado"] else "ACTIVO"

    st.markdown(f"""
    <div class="perfil-hero">
        <div class="hero-avatar">{inicial}</div>
        <div class="hero-info">
            <div class="hero-nombre">{nombre}</div>
            <div class="hero-meta">{al['grado']} {al['seccion']} | Turno {al['turno']} | DNI {al['dni']}</div>
            <span class="hero-badge">{badge_txt}</span>
        </div>
    </div>
    """, unsafe_allow_html=True)

    apo = al['nombre_apoderado'] or '-'
    tel = al['telefono_apoderado'] or '-'
    st.markdown(f"""
    <div class="perfil-bloque">
        <div class="titulo-seccion">Datos del Apoderado</div>
        <div class="info-linea"><span class="info-label">Apoderado:</span><span class="info-valor">{apo}</span></div>
        <div class="info-linea"><span class="info-label">Telefono:</span><span class="info-valor">{tel}</span></div>
    </div>
    """, unsafe_allow_html=True)

    tard_injust = d['tard_injust']
    tard_injust_class = "kpi-lbl-alerta" if tard_injust > 0 else "kpi-lbl"
    bloqueado_txt = "SI" if d['bloqueado'] else "NO"
    bloqueado_class = "kpi-lbl-alerta" if d['bloqueado'] else "kpi-lbl"
    limite_txt = d['limite']
    reinc_txt = "SI" if d['reincidente'] else "NO"
    reinc_class = "kpi-lbl-alerta" if d['reincidente'] else "kpi-lbl"

    st.markdown(f"""
    <div class="perfil-bloque">
        <div class="titulo-seccion">Estadisticas del Periodo</div>
        <div class="kpi-grid">
            <div class="kpi-card"><div class="kpi-num">{d['total_puntuales']}</div><div class="kpi-lbl">Puntuales</div></div>
            <div class="kpi-card"><div class="kpi-num">{d['total_tardanzas']}</div><div class="kpi-lbl">Tardanzas</div></div>
            <div class="kpi-card"><div class="kpi-num">{d['total_faltas']}</div><div class="kpi-lbl">Faltas</div></div>
            <div class="kpi-card"><div class="kpi-num">{d['total_ref_asistio']}</div><div class="kpi-lbl">Reforzamiento</div></div>
            <div class="kpi-card"><div class="kpi-num">{tard_injust}</div><div class="{tard_injust_class}">Tard. Injust.</div></div>
            <div class="kpi-card"><div class="kpi-num">{d['n_ciclo']}</div><div class="kpi-lbl">Tard. desde desbloqueo</div></div>
            <div class="kpi-card"><div class="kpi-num">{limite_txt}</div><div class="kpi-lbl">Limite permitido</div></div>
            <div class="kpi-card"><div class="kpi-num">{reinc_txt}</div><div class="{reinc_class}">Reincidente</div></div>
            <div class="kpi-card"><div class="kpi-num">{bloqueado_txt}</div><div class="{bloqueado_class}">Bloqueado</div></div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    st.markdown('<div class="perfil-bloque"><div class="titulo-seccion">Acciones</div>',
                unsafe_allow_html=True)
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        pdf = pdf_carnet_alumno(al["dni"])
        if pdf:
            st.download_button("Carnet QR", pdf, f"carnet_{al['dni']}.pdf",
                               "application/pdf", width='stretch',
                               key="acc_carnet")
    with c2:
        st.download_button("Historial (Excel)",
                           df_a_xlsx(d["asistencias"], "Historial"),
                           f"historial_{al['dni']}.xlsx", width='stretch',
                           key="acc_hist")
    with c3:
        pdf_res = pdf_resumen_alumno(al["id"])
        if pdf_res:
            st.download_button("Resumen (PDF)", pdf_res,
                               f"resumen_{al['dni']}.pdf",
                               "application/pdf", width='stretch',
                               key="acc_res")
    with c4:
        if usuario["rol"] == "Admin":
            if al.get("activo", 1) == 1:
                if st.button("Desactivar", width='stretch', key="acc_desac"):
                    st.session_state["_desac_al"] = True
            else:
                if st.button("Reactivar", width='stretch', key="acc_reac"):
                    st.session_state["_reac_al"] = True
    st.markdown('</div>', unsafe_allow_html=True)

    if st.session_state.get("_desac_al"):
        with st.expander("Confirmar desactivacion", expanded=True):
            if confirmar_accion("desac_al", "Confirmar",
                                 "¿Estas seguro de desactivar a este alumno?"):
                ok, msg = retirar_alumno(al["id"], al["dni"], usuario)
                st.session_state.pop("_desac_al", None)
                st.toast(msg)
                st.rerun()
    if st.session_state.get("_reac_al"):
        with st.expander("Confirmar reactivacion", expanded=True):
            if confirmar_accion("reac_al", "Confirmar",
                                 "¿Estas seguro de reactivar a este alumno?"):
                ok, msg = reactivar_alumno(al["id"], al["dni"], usuario)
                st.session_state.pop("_reac_al", None)
                st.toast(msg)
                st.rerun()

    st.markdown('<div class="perfil-bloque"><div class="titulo-seccion">Codigo QR</div>',
                unsafe_allow_html=True)
    col1, col2, col3 = st.columns([1, 1, 1])
    with col2:
        st.image(generar_qr(al["dni"]), width=220)
    st.markdown('</div>', unsafe_allow_html=True)

    tabs = st.tabs(["Asistencias", "Tardanzas", "Bloqueos", "Permisos",
                    "Justificaciones previas"])

    with tabs[0]:
        if d["asistencias"].empty:
            st.info("Sin asistencias.")
        else:
            for _, ast in d["asistencias"].head(50).iterrows():
                c1, c2, c3, c4 = st.columns([2, 3, 2, 1])
                c1.write(f"**{ast['fecha']}**")
                desc = str(ast['tipo']) + " - " + str(ast['estado'])
                if ast['tipo'] == "evento" and ast.get("evento_nombre"):
                    desc += " - " + str(ast['evento_nombre'])
                c2.write(desc)
                just_flag = bool(ast["justificada"]) if not pd.isna(ast["justificada"]) else False
                just_por = ast.get("justificado_por") or ""
                just_txt = "Justificada"
                if just_flag and just_por:
                    just_txt += " por " + str(just_por)
                c3.write(just_txt if just_flag else "Sin justificar")
                if just_flag:
                    if c4.button("Quitar", key="quitar_" + str(ast['id'])):
                        ok, msg = quitar_justificacion(ast["id"], usuario)
                        if ok:
                            st.toast(msg)
                            st.rerun()
                        else:
                            st.error(msg)
                else:
                    if ast["estado"] == "Falta":
                        puede, _m = _puede_justificar(
                            fmt_date(ast["fecha"]),
                            tipo_asistencia=FALTA,
                            ventana_id=ast.get("ventana_id"))
                        if puede:
                            if c4.button("Justificar", key="just_" + str(ast['id'])):
                                st.session_state["justif_id"] = ast["id"]
                                st.rerun()
                        else:
                            c4.caption("Cerrada")

            if st.session_state.get("justif_id"):
                jid = st.session_state["justif_id"]
                st.markdown("---")
                st.markdown("Justificar falta")
                obs = st.text_input("Observacion (obligatoria)", key="justif_obs")
                cc1, cc2 = st.columns(2)
                with cc1:
                    if st.button("Confirmar", type="primary"):
                        if not obs.strip():
                            st.error("La observacion es obligatoria.")
                        else:
                            ok, msg = justificar_asistencia(jid, obs, usuario)
                            if ok:
                                st.session_state.pop("justif_id", None)
                                st.toast(msg)
                                st.rerun()
                            else:
                                st.error(msg)
                with cc2:
                    if st.button("Cancelar"):
                        st.session_state.pop("justif_id", None)
                        st.rerun()

    with tabs[1]:
        if d["tardanzas"].empty:
            st.info("Sin tardanzas.")
        else:
            st.dataframe(d["tardanzas"], width='stretch', hide_index=True)

    with tabs[2]:
        if d["bloqueos"].empty:
            st.info("Sin bloqueos.")
        else:
            st.dataframe(d["bloqueos"], width='stretch', hide_index=True)

    with tabs[3]:
        if d["permisos"].empty:
            st.info("Sin permisos registrados para este alumno.")
        else:
            st.caption(f"{len(d['permisos'])} permiso(s) registrado(s). "
                       "Solo se pueden anular los PENDIENTES (futuros).")
            for _, perm in d["permisos"].iterrows():
                c1, c2, c3 = st.columns([4, 2, 1])
                activo = bool(perm["activo"]) if not pd.isna(perm["activo"]) else False
                with cursor() as (con, cur):
                    cur.execute("""
                        SELECT COUNT(*) AS n FROM asistencias
                        WHERE alumno_id=? AND fecha BETWEEN ? AND ?
                          AND estado='Permiso'
                    """, (al["id"], to_date(perm['fecha_inicio']),
                          to_date(perm['fecha_fin'])))
                    aplicado = (cur.fetchone()["n"] or 0) > 0
                if not activo:
                    estado_txt = "🔴 Anulado"
                elif aplicado:
                    estado_txt = "🔒 Aplicado"
                else:
                    estado_txt = "🟢 Pendiente"
                c1.write(f"**{perm['fecha_inicio']} → {perm['fecha_fin']}** — {perm['motivo']}")
                c2.write(estado_txt)
                if activo and not aplicado:
                    if c3.button("Anular", key=f"anul_perm_{perm['id']}"):
                        st.session_state["_anular_perm_id"] = int(perm["id"])
                        st.rerun()

            if st.session_state.get("_anular_perm_id"):
                pid = st.session_state["_anular_perm_id"]
                st.markdown("---")
                st.warning("Vas a anular este permiso (pendiente).")
                motivo_elim = st.text_input("Motivo de anulacion (opcional)",
                                             key=f"anul_perm_motivo_{pid}")
                if confirmar_accion(f"anul_perm_{pid}", "Confirmar anulacion",
                                     "¿Estas seguro de anular este permiso?"):
                    ok, msg = eliminar_permiso(pid, usuario, motivo_elim)
                    st.session_state.pop("_anular_perm_id", None)
                    if ok:
                        st.toast(msg)
                        st.rerun()
                    else:
                        st.error(msg)
                if st.button("Cancelar", key=f"anul_perm_cancel_{pid}"):
                    st.session_state.pop("_anular_perm_id", None)
                    st.rerun()

    with tabs[4]:
        if d["justificaciones_previas"].empty:
            st.info("Sin justificaciones previas registradas para este alumno.")
        else:
            dj = d["justificaciones_previas"].copy()

            def _estado_jp(v):
                try:
                    v = int(v)
                except (ValueError, TypeError):
                    v = 0
                if v == 1:
                    return "🔒 Aplicada"
                if v == -1:
                    return "🔴 Anulada"
                return "🟡 Pendiente"

            dj["estado_txt"] = dj["aplicada"].apply(_estado_jp)
            st.caption(f"{len(dj)} justificacion(es) previa(s). "
                       "Solo se anulan las PENDIENTES.")

            for _, jp in dj.iterrows():
                c1, c2, c3, c4 = st.columns([3, 3, 2, 1])
                c1.write(f"**{jp['fecha_objetivo']}**")
                c2.write(jp["motivo"])
                c3.write(jp["estado_txt"])
                try:
                    aplicada = int(jp["aplicada"])
                except (ValueError, TypeError):
                    aplicada = 0
                if aplicada == 0:
                    if c4.button("Anular", key=f"anul_jp_{jp['id']}"):
                        st.session_state["_anular_jp_id"] = int(jp["id"])
                        st.rerun()
                elif aplicada == -1:
                    if c4.button("Reactivar", key=f"reac_jp_{jp['id']}"):
                        st.session_state["_reactivar_jp_id"] = int(jp["id"])
                        st.rerun()

            if st.session_state.get("_anular_jp_id"):
                jid = st.session_state["_anular_jp_id"]
                st.markdown("---")
                st.warning("Vas a anular esta justificacion previa (pendiente).")
                motivo_anul = st.text_input("Motivo de anulacion (opcional)",
                                             key=f"anul_jp_motivo_{jid}")
                if confirmar_accion(f"anul_jp_{jid}", "Confirmar anulacion",
                                     "¿Estas seguro de anular esta justificacion?"):
                    ok, msg = anular_justificacion_previa(jid, usuario, motivo_anul)
                    st.session_state.pop("_anular_jp_id", None)
                    if ok:
                        st.toast(msg)
                        st.rerun()
                    else:
                        st.error(msg)
                if st.button("Cancelar", key=f"anul_jp_cancel_{jid}"):
                    st.session_state.pop("_anular_jp_id", None)
                    st.rerun()

            if st.session_state.get("_reactivar_jp_id"):
                jid = st.session_state["_reactivar_jp_id"]
                st.markdown("---")
                st.info("Vas a reactivar esta justificacion previa (quedara pendiente).")
                motivo_nuevo = st.text_input("Motivo nuevo (opcional)",
                                             key=f"reac_jp_motivo_{jid}")
                if confirmar_accion(f"reac_jp_{jid}", "Confirmar reactivacion",
                                     "¿Estas seguro de reactivar esta justificacion?"):
                    ok, msg = reactivar_justificacion_previa(jid, usuario, motivo_nuevo)
                    st.session_state.pop("_reactivar_jp_id", None)
                    if ok:
                        st.toast(msg)
                        st.rerun()
                    else:
                        st.error(msg)
                if st.button("Cancelar", key=f"reac_jp_cancel_{jid}"):
                    st.session_state.pop("_reactivar_jp_id", None)
                    st.rerun()


def vista_alumnos():
    st.title("Alumnos")
    pid = st.session_state.get("perfil_alumno_id")
    if pid:
        _perfil_alumno(pid)
        return
    tabs = st.tabs(["Listar", "Crear", "Editar"])
    with tabs[0]:
        _frag_listar_alumnos()
    with tabs[1]:
        _frag_crear_alumno()
    with tabs[2]:
        _frag_editar_alumno()


# ─── GRADOS Y SECCIONES ────────────────────────────────────────────────────
def vista_grados_secciones():
    st.title("Grados y Secciones")
    st.caption("Vista de referencia. Las secciones se crean automaticamente "
               "al importar el Excel de alumnos.")

    df = leer_df("""
        SELECT g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno,
        (SELECT COUNT(*) FROM alumnos a
         WHERE a.seccion_id=s.id AND a.activo=1) AS Alumnos
        FROM secciones s JOIN grados g ON s.grado_id=g.id
        JOIN turnos t ON s.turno_id=t.id
        ORDER BY t.nombre, g.nombre, s.nombre
    """)

    if df.empty:
        st.info("No hay secciones creadas todavia.")
        return

    st.subheader("Resumen por grado")
    resumen = df.groupby("Grado", as_index=False)["Alumnos"].sum()
    resumen.columns = ["Grado", "Total alumnos"]
    st.dataframe(resumen, width='stretch', hide_index=True)

    st.subheader("Resumen por turno")
    resumen_t = df.groupby("Turno", as_index=False)["Alumnos"].sum()
    resumen_t.columns = ["Turno", "Total alumnos"]
    st.dataframe(resumen_t, width='stretch', hide_index=True)

    st.subheader("Detalle por seccion")
    st.dataframe(df, width='stretch', hide_index=True)

    st.metric("Total de secciones", len(df))
    st.metric("Total de alumnos activos", int(df["Alumnos"].sum()))


# ─── CARNETS ───────────────────────────────────────────────────────────────
def vista_carnets():
    st.title("Carnets QR")

    st.markdown("### Descargar por sección")
    st.caption("Selecciona un grado para ver sus secciones y generar los carnets.")

    if st.session_state.get("carn_ver_seccion"):
        _carnets_ver_seccion(st.session_state["carn_ver_seccion"])
        return
        
    grados = listar_grados()
    if not grados:
        st.warning("No hay grados registrados.")
        return

    # Control para regresar al menú de grados
    gid = st.session_state.get("carn_grado_sel")
    if gid:
        g = next((x for x in grados if x["id"] == gid), None)
        if not g:
            st.session_state.pop("carn_grado_sel", None)
            st.rerun()
            return
            
        if st.button("← Regresar a grados", key="carn_volver_grados"):
            st.session_state.pop("carn_grado_sel", None)
            st.rerun()
            
        st.subheader(f"Secciones de {g['nombre']}")
        secs = secciones_por_grado(g["id"])
        if not secs:
            st.info("Sin secciones en este grado.")
            return
            
        for s in secs:
            n = len(alumnos_de_seccion(s["id"]))
            if st.button(f"{s['nombre']}  ({n} alumnos)", width='stretch',
                         key="carn_s_" + str(s['id'])):
                st.session_state["carn_ver_seccion"] = s["id"]
                st.rerun()
        return

    # Si no hay grado seleccionado, mostramos la lista de grados
    for g in grados:
        secs = secciones_por_grado(g["id"])
        if st.button(f"{g['nombre']}  ({len(secs)} secciones)", width='stretch',
                     key="carn_g_" + str(g['id'])):
            st.session_state["carn_grado_sel"] = g["id"]
            st.rerun()


def _carnets_ver_seccion(idsec):
    with cursor() as (con, cur):
        cur.execute("""SELECT s.id,s.nombre AS seccion,g.nombre AS grado,
                       t.nombre AS turno FROM secciones s
                       JOIN grados g ON s.grado_id=g.id
                       JOIN turnos t ON s.turno_id=t.id WHERE s.id=?""", (idsec,))
        sec = cur.fetchone()
    if not sec:
        st.warning("Seccion no encontrada.")
        st.session_state.pop("carn_ver_seccion", None)
        return
    if st.button("Regresar a grados", key="carn_volver"):
        st.session_state.pop("carn_ver_seccion", None)
        st.session_state.pop("carn_sel_alumnos", None)
        st.rerun()
    st.subheader(f"{sec['grado']} {sec['seccion']} - Turno {sec['turno']}")
    df = alumnos_de_seccion(idsec)
    if df.empty:
        st.info("Sin alumnos activos.")
        return
    st.caption(f"{len(df)} alumnos. Aprieta un nombre para seleccionarlo.")
    if "carn_sel_alumnos" not in st.session_state:
        st.session_state["carn_sel_alumnos"] = set()
    sel = st.session_state["carn_sel_alumnos"]
    for _, al in df.iterrows():
        if al["id"] in sel:
            st.markdown('<div class="btn-sel">', unsafe_allow_html=True)
            if st.button(al['nombre_completo'],
                         key="carn_al_" + str(al['id']),
                         width='stretch'):
                sel.discard(al["id"])
                st.rerun()
            st.markdown('</div>', unsafe_allow_html=True)
        else:
            if st.button(al['nombre_completo'],
                         key="carn_al_" + str(al['id']),
                         width='stretch'):
                sel.add(al["id"])
                st.rerun()
    st.markdown("---")
    st.write(f"Seleccionados: {len(sel)}")
    c1, c2, c3 = st.columns(3)
    with c1:
        if sel:
            if st.button("Descargar seleccionados", type="primary",
                         width='stretch', key="carn_dl_sel"):
                pdf = pdf_carnets_seleccionados(
                    list(sel),
                    titulo=f"Carnets - {sec['grado']} {sec['seccion']}")
                if pdf:
                    st.download_button(
                        "Guardar PDF", pdf,
                        f"carnets_sel_{sec['grado']}{sec['seccion']}.pdf",
                        "application/pdf", width='stretch')
        else:
            st.info("Marca al menos un alumno.")
    with c2:
        if st.button("Descargar todo el salon", width='stretch',
                     key="carn_dl_todo"):
            pdf = pdf_carnets_por_seccion(idsec)
            if pdf:
                st.download_button(
                    "Guardar PDF", pdf,
                    f"carnets_{sec['grado']}{sec['seccion']}.pdf",
                    "application/pdf", width='stretch')
    with c3:
        if st.button("Limpiar seleccion", width='stretch', key="carn_limpiar"):
            st.session_state["carn_sel_alumnos"] = set()
            st.rerun()


# ─── VENTANAS ──────────────────────────────────────────────────────────────
def vista_ventanas():
    st.title("Ventanas de asistencia")
    st.caption("Configura apertura, limite puntual y cierre por turno y tipo.")
    for turno in listar_turnos():
        st.subheader("Turno " + turno['nombre'])
        for v in listar_ventanas(turno["id"]):
            with st.expander(f"{v['nombre']} ({v['tipo']})"):
                ap_t = st.time_input(
                    "Apertura",
                    value=datetime.strptime(v["hora_apertura"], "%H:%M").time(),
                    key="ap_" + str(v['id']))
                lim_val = v["hora_limite_puntual"] or v["hora_apertura"]
                lim_t = st.time_input(
                    "Limite puntual",
                    value=datetime.strptime(lim_val, "%H:%M").time(),
                    key="lim_" + str(v['id']))
                ci_t = st.time_input(
                    "Cierre",
                    value=datetime.strptime(v["hora_cierre"], "%H:%M").time(),
                    key="ci_" + str(v['id']))
                if confirmar_accion(
                        f"vent_{v['id']}", "Guardar",
                        "¿Estas seguro de guardar los cambios de esta ventana?"):
                    ap = ap_t.strftime("%H:%M")
                    lim = lim_t.strftime("%H:%M")
                    ci = ci_t.strftime("%H:%M")
                    escribir("UPDATE ventanas SET hora_apertura=?,"
                             "hora_limite_puntual=?,hora_cierre=? WHERE id=?",
                             (ap, lim, ci, v["id"]))
                    auditar(st.session_state["user"]["usuario"],
                            f"Edito ventana id={v['id']}")
                    st.toast("Ventana actualizada")
                    st.rerun()

# ─── USUARIOS ──────────────────────────────────────────────────────────────
def puede_gestionar_usuario(usuario_actual, id_objetivo):
    with cursor() as (con, cur):
        cur.execute("SELECT id,rol,es_principal,usuario FROM usuarios WHERE id=?",
                    (id_objetivo,))
        obj = cur.fetchone()
    if not obj:
        return False, "Usuario no encontrado."
    if id_objetivo == usuario_actual["id"]:
        return False, "Para cambiar tus datos usa Mi cuenta."
    soy_principal = (usuario_actual.get("es_principal") or 0) == 1
    if soy_principal:
        return True, ""
    if obj["rol"] == "Admin":
        return False, "No tienes permisos para gestionar a un Admin."
    return True, ""


def _usuario_form_editar(idu):
    usuario = st.session_state["user"]
    soy_principal = (usuario.get("es_principal") or 0) == 1
    with cursor() as (con, cur):
        cur.execute("SELECT * FROM usuarios WHERE id=?", (idu,))
        datos = cur.fetchone()
    if not datos:
        st.error("Usuario no encontrado.")
        return

    c1, c2 = st.columns(2)
    with c1:
        u = st.text_input("Usuario", value=datos["usuario"], key=f"eu_u_{idu}")
        n = st.text_input("Nombres", value=datos["nombres"], key=f"eu_n_{idu}")
    with c2:
        p = st.text_input("Nueva contraseña (opcional)", type="password",
                          key=f"eu_p_{idu}")
        roles_edit = (["Admin", "Direccion", "Auxiliar"] if soy_principal
                      else ["Direccion", "Auxiliar"])
        idx_rol = (roles_edit.index(datos["rol"])
                   if datos["rol"] in roles_edit else 0)
        r = st.selectbox("Rol", roles_edit, index=idx_rol, key=f"eu_r_{idu}")
        act = st.checkbox("Activo", value=bool(datos["activo"]),
                          key=f"eu_a_{idu}")

    if confirmar_accion(
            f"eu_{idu}", "Guardar cambios",
            "¿Estas seguro de guardar los cambios de este usuario?"):
        u_norm = u.strip().lower()
        with cursor() as (con, cur):
            cur.execute("SELECT id FROM usuarios "
                        "WHERE LOWER(usuario)=? AND id!=?", (u_norm, idu))
            if cur.fetchone():
                st.error("Ya existe otro usuario con ese nombre.")
            else:
                if p:
                    cur.execute("UPDATE usuarios SET usuario=?,nombres=?,"
                                "rol=?,password=?,activo=? WHERE id=?",
                                (u_norm, n, r, hashear_password(p),
                                 1 if act else 0, idu))
                else:
                    cur.execute("UPDATE usuarios SET usuario=?,nombres=?,"
                                "rol=?,activo=? WHERE id=?",
                                (u_norm, n, r, 1 if act else 0, idu))
                auditar(usuario["usuario"], "Edito usuario " + u_norm)
                st.session_state["_aviso_usuarios"] = {
                    "tipo": "ok", "msg": "Usuario editado."}
                st.session_state.pop(f"_editando_u_{idu}", None)
                st.rerun()


def _frag_usuarios_botones():
    usuario = st.session_state["user"]
    soy_principal = (usuario.get("es_principal") or 0) == 1

    st.markdown("### Buscar Usuarios")
    st.caption("Selecciona un filtro para cargar la lista (ahorra memoria al no mostrar todo de golpe).")

    # 🆕 FILTROS
    c1, c2 = st.columns(2)
    with c1:
        roles_disponibles = ["Todos", "Admin", "Direccion", "Auxiliar"]
        filtro_rol = st.selectbox("Filtrar por Rol", roles_disponibles, key="usr_filtro_rol")
    with c2:
        filtro_nombre = st.text_input("Buscar por nombre o usuario", key="usr_filtro_nombre", placeholder="Ej: admin")

    if st.button("🔍 Buscar Usuarios", type="primary", width='stretch'):
        st.session_state["_buscar_usuarios_activado"] = True

    if st.session_state.get("_buscar_usuarios_activado"):
        if filtro_rol == "Todos" and not filtro_nombre.strip():
            st.info("Selecciona un rol o escribe un nombre para buscar.")
            return

        # Construimos la consulta dinámicamente
        query = "SELECT id, usuario, rol, nombres, activo, ultimo_login, es_principal FROM usuarios WHERE 1=1"
        params = []
        
        if not soy_principal:
            # Si no es principal, no puede ver a los otros Admins (excepto a sí mismo)
            query += " AND (id=? OR rol!='Admin')"
            params.append(usuario["id"])

        if filtro_rol != "Todos":
            query += " AND rol=?"
            params.append(filtro_rol)
            
        if filtro_nombre.strip():
            query += " AND (LOWER(nombres) LIKE ? OR LOWER(usuario) LIKE ?)"
            pat = f"%{filtro_nombre.strip().lower()}%"
            params += [pat, pat]

        query += " ORDER BY nombres, usuario"
        df = leer_df(query, params)

        if df.empty:
            st.info("No se encontraron usuarios con esos filtros.")
            return

        st.write(f"**{len(df)} usuarios encontrados**")
        
        # 🆕 MOSTRAMOS SOLO UN DATAFRAME (Cero CSS, cero botones gastando RAM)
        df_mostrar = df.copy()
        df_mostrar["estado"] = df_mostrar["activo"].apply(lambda x: "🟢 Activo" if x else "🔴 Inactivo")
        df_mostrar["rol_ui"] = df_mostrar.apply(
            lambda r: "Admin (principal)" if (r["rol"] == "Admin" and r["es_principal"]) 
            else ("Sub Admin" if r["rol"] == "Admin" else r["rol"]), axis=1
        )
        
        # Formateamos las columnas para que se vea limpio
        df_mostrar = df_mostrar[["nombres", "usuario", "rol_ui", "estado", "ultimo_login"]]
        df_mostrar.columns = ["Nombres", "Usuario", "Rol", "Estado", "Último Login"]
        
        st.dataframe(df_mostrar, width='stretch', hide_index=True)

        st.markdown("---")
        st.subheader("Editar un usuario")
        
        # El único botón que se genera es para el selectbox
        ops = {f"{r['nombres']} ({r['usuario']})": r["id"] for _, r in df.iterrows()}
        sel = st.selectbox("Selecciona el usuario a editar", list(ops.keys()))
        
        if st.button("Ver / Editar usuario seleccionado", width='stretch'):
            st.session_state["_editando_u_" + str(ops[sel])] = True
            st.rerun()

    # 🆕 AQUÍ SE MUESTRA EL FORMULARIO DE EDICIÓN (Si se seleccionó a alguien)
    for key in list(st.session_state.keys()):
        if key.startswith("_editando_u_"):
            idu = int(key.replace("_editando_u_", ""))
            if st.session_state[key]:
                st.markdown("---")
                ok, msg = puede_gestionar_usuario(usuario, idu)
                if not ok:
                    st.warning(msg)
                    st.session_state[key] = False
                else:
                    _usuario_form_editar(idu)
                
                if st.button("Cancelar edición", key=f"cancel_edit_{idu}"):
                    st.session_state[key] = False
                    st.rerun()


def _frag_usuarios_crear():
    usuario = st.session_state["user"]
    soy_principal = (usuario.get("es_principal") or 0) == 1

    st.subheader("Crear usuario")
    aviso = st.session_state.pop("_aviso_usuarios", None)
    if aviso:
        if aviso.get("tipo") == "ok":
            st.success(aviso.get("msg", ""))
        elif aviso.get("tipo") == "error":
            st.error(aviso.get("msg", ""))

    c1, c2 = st.columns(2)
    with c1:
        u = st.text_input("Usuario", key="crear_u_usuario")
        p = st.text_input("Contraseña", type="password", key="crear_u_pass")
    with c2:
        n = st.text_input("Nombres", key="crear_u_nombres")
        roles_disp = (["Admin", "Direccion", "Auxiliar"] if soy_principal
                      else ["Direccion", "Auxiliar"])
        r = st.selectbox("Rol", roles_disp, key="crear_u_rol")

    idt = None
    if r == "Auxiliar":
        t_lbl = st.selectbox("Turno", [x["nombre"] for x in listar_turnos()],
                              key="crear_u_turno")
        idt = next((x["id"] for x in listar_turnos()
                    if x["nombre"] == t_lbl), None)

    if confirmar_accion(
            "crear_u", "Crear usuario",
            "¿Estas seguro de crear este usuario?"):
        if not u or not p or not n:
            st.error("Completa usuario, contraseña y nombres.")
        elif len(p) < 6:
            st.error("Minimo 6 caracteres.")
        elif r == "Auxiliar" and not idt:
            st.error("Selecciona un turno.")
        else:
            u_norm = u.strip().lower()
            with cursor() as (con, cur):
                cur.execute("SELECT 1 FROM usuarios WHERE LOWER(usuario)=?",
                            (u_norm,))
                if cur.fetchone():
                    st.error("El usuario ya existe.")
                else:
                    try:
                        cur.execute("INSERT INTO usuarios(usuario,password,rol,"
                                    "nombres,turno_asignado,es_principal) "
                                    "VALUES(?,?,?,?,?,0)",
                                    (u_norm, hashear_password(p), r, n, idt))
                        auditar(usuario["usuario"], "Creo usuario " + u_norm)
                        st.session_state["_aviso_usuarios"] = {
                            "tipo": "ok", "msg": "Usuario creado."}
                        for k in ["crear_u_usuario", "crear_u_pass",
                                   "crear_u_nombres", "crear_u_rol",
                                   "crear_u_turno"]:
                            st.session_state.pop(k, None)
                        st.rerun()
                    except Exception as e:
                        if "unique" in str(e).lower():
                            st.error("Ese usuario ya existe.")
                        else:
                            st.error(f"Error: {e}")


def _frag_usuarios_asignar():
    usuario = st.session_state["user"]
    st.subheader("Asignar secciones a Auxiliares")
    st.caption("Cada seccion solo puede estar asignada a un auxiliar.")

    dfa = leer_df("SELECT id,usuario,nombres,turno_asignado FROM usuarios "
                  "WHERE rol='Auxiliar' AND activo=1 ORDER BY nombres")
    if dfa.empty:
        st.info("Sin auxiliares.")
        return

    ops = {f"{r['nombres']} ({r['usuario']})": r["id"] for _, r in dfa.iterrows()}
    sel = st.selectbox("Auxiliar", list(ops.keys()), key="asig_aux_sel")
    ida = ops[sel]

    with cursor() as (con, cur):
        cur.execute("SELECT turno_asignado FROM usuarios WHERE id=?", (ida,))
        ta = cur.fetchone()
    if not (ta and ta["turno_asignado"]):
        st.warning("Este auxiliar no tiene turno asignado.")
        return

    secs = secciones_por_turno(ta["turno_asignado"])
    secs = sorted(secs, key=lambda s: (s["grado"], s["nombre"]))

    with cursor() as (con, cur):
        cur.execute("SELECT seccion_id FROM auxiliar_secciones "
                    "WHERE usuario_id!=?", (ida,))
        asignadas_a_otros = {r["seccion_id"] for r in cur.fetchall()}
        cur.execute("SELECT seccion_id FROM auxiliar_secciones "
                    "WHERE usuario_id=?", (ida,))
        asig = {r["seccion_id"] for r in cur.fetchall()}

    st.write("Secciones disponibles:")
    sel_s = []
    for s in secs:
        if s["id"] in asignadas_a_otros:
            continue
        etiqueta = f"{s['grado']} {s['nombre']}"
        if st.checkbox(etiqueta, value=s["id"] in asig,
                       key=f"asig_{ida}_{s['id']}"):
            sel_s.append(s["id"])

    if confirmar_accion("asig_" + str(ida), "Guardar asignaciones",
                         "¿Estas seguro de guardar estas asignaciones?"):
        with cursor() as (con, cur):
            cur.execute("DELETE FROM auxiliar_secciones WHERE usuario_id=?",
                        (ida,))
            for sid in sel_s:
                try:
                    cur.execute("INSERT INTO auxiliar_secciones"
                                "(usuario_id,seccion_id) VALUES(?,?)",
                                (ida, sid))
                except Exception as e:
                    if "unique" in str(e).lower():
                        st.warning("La seccion ya estaba asignada.")
        auditar(usuario["usuario"],
                f"Asigno {len(sel_s)} secciones a usuario_id={ida}")
        st.session_state["_aviso_usuarios"] = {
            "tipo": "ok", "msg": "Asignaciones guardadas."}
        st.rerun()


def _frag_usuarios_mantenimiento():
    usuario = st.session_state["user"]
    st.subheader("Modo mantenimiento")
    if modo_mantenimiento():
        st.error("El sistema esta en MANTENIMIENTO.")
        if confirmar_accion("mant_off", "Desactivar mantenimiento",
                             "¿Estas seguro de desactivar el mantenimiento?"):
            desactivar_mantenimiento(usuario)
            st.session_state["_aviso_usuarios"] = {
                "tipo": "ok", "msg": "Mantenimiento desactivado."}
            st.rerun()
    else:
        st.success("El sistema esta operativo.")
        msg = st.text_input("Mensaje para mostrar (opcional)", key="mant_msg")
        if confirmar_accion("mant_on", "Activar mantenimiento",
                             "¿Estas seguro de activar el mantenimiento?"):
            activar_mantenimiento(usuario, msg)
            st.session_state["_aviso_usuarios"] = {
                "tipo": "ok", "msg": "Mantenimiento activado."}
            st.rerun()


def vista_usuarios():
    st.title("Usuarios")
    tabs = st.tabs(["Usuarios", "Crear", "Asignar secciones", "Mantenimiento"])
    with tabs[0]:
        _frag_usuarios_botones()
    with tabs[1]:
        _frag_usuarios_crear()
    with tabs[2]:
        _frag_usuarios_asignar()
    with tabs[3]:
        _frag_usuarios_mantenimiento()


# ─── AUDITORÍA ─────────────────────────────────────────────────────────────
def _frag_importar_excel():
    st.subheader("Cargar alumnos al periodo")
    st.info("Columnas: DNI, Nombres, Apellido Paterno, Apellido Materno, Grado, "
            "Seccion, Turno, Apoderado, Telefono.")
    st.caption("Si un DNI ya existe, se reactiva y actualiza.")
    arch = st.file_uploader("Sube el Excel", type=["xlsx", "xls"],
                            key="import_excel_periodo")
    if not arch:
        return
    df = pd.read_excel(arch)
    st.write(f"{len(df)} filas detectadas.")
    cols = list(df.columns)
    with st.form("mapeo_periodo"):
        c1, c2 = st.columns(2)
        with c1:
            m_dni = st.selectbox("DNI *", cols)
            m_nom = st.selectbox("Nombres *", cols)
            m_pat = st.selectbox("Apellido Paterno *", cols)
            m_mat = st.selectbox("Apellido Materno", [""] + cols)
        with c2:
            m_gra = st.selectbox("Grado *", cols)
            m_sec = st.selectbox("Seccion *", cols)
            m_tur = st.selectbox("Turno *", cols)
            m_apo_n = st.selectbox("Nombre Apoderado", [""] + cols)
            m_apo_t = st.selectbox("Telefono Apoderado", [""] + cols)
        validar = st.form_submit_button("Validar", type="primary")
    if validar:
        mapeo = {"dni": m_dni, "nombres": m_nom, "apellido_paterno": m_pat,
                 "apellido_materno": m_mat, "grado": m_gra, "seccion": m_sec,
                 "turno": m_tur, "apoderado_nombre": m_apo_n,
                 "apoderado_telefono": m_apo_t}
        val, errs, res = validar_importacion(df, mapeo)
        st.session_state["_iv"] = val
        st.session_state["_ie"] = errs
        st.session_state["_ir"] = res
    if "_ir" in st.session_state:
        r = st.session_state["_ir"]
        c1, c2, c3 = st.columns(3)
        c1.metric("Total", r["total"])
        c2.metric("Validas", r["validas"])
        c3.metric("Errores", r["errores"])
        if st.session_state.get("_ie"):
            with st.expander("Errores"):
                st.dataframe(pd.DataFrame(st.session_state["_ie"]),
                             width='stretch')
        if st.session_state.get("_iv"):
            if confirmar_accion("importar_excel", "Importar validas",
                                 "¿Estas seguro de importar los alumnos validos?"):
                with st.spinner("Insertando alumnos y respaldando en la nube..."):
                    ins, reac, errs = insertar_alumnos_validos(
                        st.session_state["_iv"])
                    
                    # 🆕 RESPALDO AUTOMÁTICO A R2
                    from sync import subir_bd
                    from db import DB_PATH
                    try:
                        subir_bd(DB_PATH)
                    except Exception as e:
                        log.error(f"Fallo backup automático: {e}")
                
                st.toast(f"{ins} importados, {reac} reactivados.")
                if errs:
                    st.warning(f"{len(errs)} errores al insertar")
                for k in ["_iv", "_ie", "_ir"]:
                    st.session_state.pop(k, None)
                time.sleep(1)
                st.rerun()


def vista_auditoria():
    st.title("Auditoria y Periodos")
    usuario = st.session_state["user"]
    
    tabs = st.tabs(["Registros", "Periodos", "Cierre de año"])
    
    with tabs[0]:
        df = obtener_auditoria(500)
        st.write(f"{len(df)} registros")
        if not df.empty:
            st.dataframe(df, width='stretch')
            
    with tabs[1]:
        st.subheader("Crear nuevo periodo")
        with st.form("crear_periodo_form"):
            c1, c2, c3 = st.columns(3)
            with c1:
                nom = st.text_input("Nombre del periodo (ej: 2026)")
            with c2:
                fi = st.date_input("Fecha de inicio", ahora().date())
            with c3:
                ff = st.date_input("Fecha de fin", ahora().date() + timedelta(days=270))
            
            if st.form_submit_button("Crear y Activar Periodo", type="primary"):
                if not nom.strip():
                    st.error("Ingresa un nombre para el periodo.")
                elif (ff - fi).days < 30:
                    st.error("El periodo debe durar mínimo 1 mes.")
                else:
                    with cursor() as (con, cur):
                        cur.execute("INSERT INTO periodos(nombre,fecha_inicio,fecha_fin,activo,cerrado) VALUES(?,?,?,1,0)", 
                                    (nom.strip(), to_date(fi), to_date(ff)))
                        nuevo_id = cur.lastrowid
                        cur.execute("UPDATE periodos SET activo=0 WHERE id!=?", (nuevo_id,))
                    auditar(usuario["usuario"], f"Creo periodo {nom}", tb="periodos", rid=nuevo_id)
                    st.success(f"Periodo '{nom}' creado y activado con éxito.")
                    time.sleep(1)
                    st.rerun()

        st.markdown("---")
        st.subheader("Periodos registrados")
        df_info = info_periodo_con_alumnos()
        if df_info.empty:
            st.info("Aún no hay periodos creados.")
        else:
            st.dataframe(df_info, width='stretch', hide_index=True)

        st.markdown("---")
        st.subheader("Cargar alumnos al periodo activo")
        p = obtener_periodo_activo()
        if p:
            st.info(f"Periodo activo: **{p['nombre']}** ({p['fecha_inicio']} - {p['fecha_fin']})")
            if periodo_tiene_alumnos(p["id"]):
                st.success("El periodo ya tiene alumnos cargados.")
            else:
                st.warning("El periodo NO tiene alumnos. Sube el Excel aquí abajo.")
            
            _frag_importar_excel()
        else:
            st.warning("Primero debes crear un periodo arriba para poder cargar alumnos.")

        st.markdown("---")
        st.subheader("Activar otro periodo (solo no cerrados)")
        df2 = listar_periodos()
        df2 = df2[df2["cerrado"] == 0]
        if not df2.empty:
            ops = {}
            for _, r in df2.iterrows():
                et = f"{r['nombre']} ({r['fecha_inicio']} - {r['fecha_fin']})"
                if r["activo"]:
                    et += " [ACTIVO]"
                ops[et] = r["id"]
            sel = st.selectbox("Selecciona el periodo a activar", list(ops.keys()),
                               key="act_periodo_sel")
            if confirmar_accion("activar_periodo", "Activar periodo",
                                 "¿Estas seguro de activar este periodo?"):
                ok, msg = activar_periodo(ops[sel], usuario)
                if ok:
                    st.toast(msg)
                    st.rerun()
                else:
                    st.error(msg)
        else:
            st.info("No hay otros periodos abiertos para activar.")

    with tabs[2]:
        st.subheader("Cierre de año escolar")
        st.warning("Al cerrar el periodo se desactivan TODOS los alumnos y se crea uno nuevo.")
        p = obtener_periodo_activo()
        if not p:
            st.info("No hay periodo activo.")
            return
        st.info(f"Periodo activo: {p['nombre']} ({p['fecha_inicio']} - {p['fecha_fin']})")
        
        st.markdown("Reporte resumen del periodo:")
        df_rep = reporte_cierre_anual(p["id"])
        if not df_rep.empty:
            st.dataframe(df_rep, width='stretch')

        st.markdown("---")
        st.markdown("**Paso 1: Descargar reporte anual detallado (OBLIGATORIO)**")
        if st.button("Generar y descargar reporte anual", key="btn_desc_anual"):
            hojas = reporte_detallado_por_mes(p["id"])
            if not hojas:
                st.warning("Sin datos.")
            else:
                st.download_button(
                    "Descargar Excel anual",
                    df_a_xlsx_multilhoja(hojas),
                    f"reporte_anual_{p['nombre']}.xlsx",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
                st.session_state["_reporte_descargado"] = True
                st.toast("Reporte generado.")

        st.markdown("---")
        st.markdown("**Paso 2: Cerrar periodo (creara uno nuevo)**")
        desc = st.session_state.get("_reporte_descargado", False)
        if not desc:
            st.info("Debes descargar el reporte anual antes.")

        c1, c2, c3 = st.columns(3)
        with c1:
            nn = st.text_input("Nombre nuevo periodo", value=str(ahora().year + 1))
        with c2:
            fi = st.date_input("Inicio nuevo", date(ahora().year + 1, 3, 1))
        with c3:
            ff = st.date_input("Fin nuevo", date(ahora().year + 1, 12, 31))
        conf = st.text_input("Escribe CERRAR para confirmar")

        if confirmar_accion("cerrar_ano", "Cerrar año escolar",
                             "¿Estas seguro de cerrar el año escolar?"):
            if not desc:
                st.error("Primero debes descargar el reporte anual.")
            elif conf.strip() != "CERRAR":
                st.error("Debes escribir exactamente CERRAR.")
            else:
                ok, msg = cerrar_ano_escolar(
                    usuario, p["id"], nn,
                    fi.strftime("%Y-%m-%d"),
                    ff.strftime("%Y-%m-%d"))
                if ok:
                    st.session_state.pop("_reporte_descargado", None)
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)

        st.markdown("---")
        st.markdown("Cierres anteriores:")
        dfc = listar_cierres_anuales()
        if not dfc.empty:
            st.dataframe(dfc, width='stretch')
        st.markdown("Periodos cerrados (solo consulta):")
        dfp = listar_periodos_cerrados()
        if not dfp.empty:
            st.dataframe(dfp, width='stretch')


# ─── DÍAS ESPECIALES ───────────────────────────────────────────────────────
def vista_dias_especiales():
    st.title("Dias especiales")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Crear", "Listar / eliminar"])
    with tabs[0]:
        st.caption("Puedes crear varios eventos para el mismo dia.")
        for k in list(st.session_state.keys()):
            if (k.startswith("dia_sec_") or k.startswith("dia_grado_")):
                if k not in st.session_state.get("_dia_keys_actuales", set()):
                    del st.session_state[k]
        if "dia_tipo" not in st.session_state:
            st.session_state["dia_tipo"] = "Evento"
        tipo = st.radio("Tipo", ["Evento", "Feriado"], key="dia_tipo",
                        horizontal=True)

        c1, c2 = st.columns(2)
        with c1:
            fecha = st.date_input("Fecha", min_value=ahora().date(),
                                   key="dia_fecha")
            desc = st.text_input("Descripcion / Nombre del evento",
                                  key="dia_desc")
        with c2:
            if tipo == "Evento":
                turnos_opts = {"Ambos": None}
                turnos_opts.update({t["nombre"]: t["id"] for t in listar_turnos()})
                t_lbl = st.selectbox("Turno", list(turnos_opts.keys()),
                                      key="dia_turno")
                hora_t = st.time_input(
                    "Hora entrada",
                    value=datetime.strptime("08:00", "%H:%M").time(),
                    key="dia_hora")
                hora = hora_t.strftime("%H:%M")
            else:
                turnos_opts = {"Ambos": None}
                t_lbl = "Ambos"
                hora = "00:00"
                st.info("Los feriados no tienen horario ni turno.")

        contar_como_clases = False
        if tipo == "Evento":
            contar_como_clases = st.checkbox(
                "Contar como CLASES NORMALES (no como evento)",
                value=False, key="dia_contar_clases")

        alcance = "Todo el colegio"
        if tipo == "Evento":
            alcance = st.radio(
                "Aplica a:",
                ["Todo el colegio", "Solo algunas secciones"],
                key="dia_alcance", horizontal=True)

        keys_actuales = set()
        selecciones_secciones = []
        if tipo == "Evento" and alcance == "Solo algunas secciones":
            st.markdown("**Alcance del dia especial:**")
            with st.expander("Seleccionar grados y secciones", expanded=True):
                grados = listar_grados()
                for g in grados:
                    k_grado = "dia_grado_" + str(g["id"])
                    keys_actuales.add(k_grado)
                    st.checkbox("Todo " + g["nombre"], key=k_grado)
                    secs = secciones_por_grado(g["id"])
                    for s in secs:
                        k_sec = f"dia_sec_{g['id']}_{s['id']}"
                        keys_actuales.add(k_sec)
                        if st.checkbox(f"   {g['nombre']} {s['nombre']}",
                                       key=k_sec):
                            selecciones_secciones.append(s["id"])

        st.session_state["_dia_keys_actuales"] = keys_actuales

        if confirmar_accion("dia_crear", "Crear evento",
                             "¿Estas seguro de crear este dia especial?"):
            if not desc.strip():
                st.error("Descripcion requerida.")
            elif tipo == "Evento" and alcance == "Solo algunas secciones" \
                    and not selecciones_secciones:
                st.error("Selecciona al menos una seccion.")
            else:
                selecciones_secciones = list(set(selecciones_secciones))
                idt = turnos_opts.get(t_lbl) if tipo == "Evento" else None
                per = obtener_periodo_activo()
                pid = per["id"] if per else None
                with cursor() as (con, cur):
                    cur.execute("""
                        INSERT INTO dias_especiales(fecha,descripcion,turno_id,
                            hora_entrada,tipo,periodo_id,contar_como_clases)
                        VALUES(?,?,?,?,?,?,?)
                    """, (to_date(fecha), desc.strip(), idt,
                          to_time(hora) if tipo == "Evento" else to_time("00:00"),
                          "evento" if tipo == "Evento" else "feriado",
                          pid, 1 if contar_como_clases else 0))
                    idd = cur.lastrowid
                    for sid in selecciones_secciones:
                        try:
                            cur.execute("INSERT INTO dias_especiales_secciones"
                                        "(dia_especial_id,seccion_id) VALUES(?,?)",
                                        (idd, sid))
                        except Exception as e:
                            if "unique" not in str(e).lower():
                                log.warning("dia_sec: %s", e)
                auditar(usuario["usuario"], "Creo dia especial " + desc)
                for k in list(st.session_state.keys()):
                    if k.startswith("dia_sec_") or k.startswith("dia_grado_"):
                        del st.session_state[k]
                st.session_state.pop("_dia_keys_actuales", None)
                st.session_state["_aviso_usuarios"] = {
                    "tipo": "ok", "msg": "Evento creado."}
                st.rerun()

    with tabs[1]:
        df = leer_df("""
            SELECT d.id, d.fecha, d.descripcion,
                   COALESCE(t.nombre, 'Ambos') AS turno,
                   d.hora_entrada, d.tipo, d.contar_como_clases,
                   (SELECT GROUP_CONCAT(g.nombre || ' ' || s.nombre, ', ')
                    FROM dias_especiales_secciones ds
                    JOIN secciones s ON ds.seccion_id = s.id
                    JOIN grados g ON s.grado_id = g.id
                    WHERE ds.dia_especial_id = d.id) AS secciones_aplicadas
            FROM dias_especiales d
            LEFT JOIN turnos t ON d.turno_id = t.id
            WHERE d.fecha >= ? AND d.activo=1
            ORDER BY d.fecha, d.hora_entrada
        """, (to_date(hoy_str()),))
        if df.empty:
            st.info("Sin dias especiales.")
        else:
            df_display = df.copy()
            df_display["contar_como_clases"] = df_display[
                "contar_como_clases"].apply(
                    lambda x: "Clases normales" if x else "Evento")
            df_display["secciones_aplicadas"] = df_display[
                "secciones_aplicadas"].fillna("Todo el colegio")
            st.dataframe(df_display, width='stretch')
            ops = {f"{r['fecha']} {r['hora_entrada']} - {r['descripcion']}": r["id"]
                   for _, r in df.iterrows()}
            sel = st.selectbox("Eliminar", list(ops.keys()))
            st.warning("Esta accion es irreversible.")
            if confirmar_accion("del_dia", "Eliminar dia especial",
                                 "¿Estas seguro de eliminar este dia especial?"):
                escribir("DELETE FROM dias_especiales WHERE id=?", (ops[sel],))
                auditar(usuario["usuario"],
                        f"Elimino dia especial id={ops[sel]}")
                st.session_state["_aviso_usuarios"] = {
                    "tipo": "ok", "msg": "Dia especial eliminado."}
                st.rerun()


# ─── JUSTIFICACIONES PREVIAS ───────────────────────────────────────────────
def _frag_justificacion_previa(usuario):
    st.subheader("Justificacion previa")
    st.caption("Solo aplica a FALTAS. Solo hoy, mañana o pasado mañana.")
    tabs = st.tabs(["Historial global", "Registrar"])
    with tabs[0]:
        _render_historial_global_jp(usuario)
    with tabs[1]:
        _render_registrar_jp(usuario)


def _render_registrar_jp(usuario):
    clave = "jp_sel"
    
    # 🆕 Lógica mejorada: Si ya hay alumno seleccionado, saltamos directo al formulario
    al_sel_id = st.session_state.get("jp_al_sel_id")
    
    if not al_sel_id:
        idsec = _selector_grado_seccion(clave, "Elige el grado")
        if not idsec:
            return
        al = _selector_alumno_accion(
            clave, idsec,
            texto_boton="Seleccionar",
            key_boton="jp_sel_btn",
            on_click_key="jp_al_sel_id")
        if not al:
            return
    else:
        # Traemos los datos del alumno seleccionado
        with cursor() as (con, cur):
            cur.execute("""
                SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
                       a.seccion_id, a.nombre_apoderado, a.telefono_apoderado,
                       g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
                FROM alumnos a JOIN secciones s ON a.seccion_id=s.id
                JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
                WHERE a.id=?
            """, (al_sel_id,))
            al_row = cur.fetchone()
        if not al_row:
            st.session_state.pop("jp_al_sel_id", None)
            _reset_selector(clave)
            st.rerun()
            return
        al = dict(al_row)

    bloq = alumno_bloqueado(al["id"])
    if bloq:
        st.error("Este alumno esta BLOQUEADO: " + (bloq.get("motivo") or ""))
        
    nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") + ", " + al['nombres']).strip(", ")
    st.markdown(f"**{nombre}** | DNI {al['dni']} | {al['grado']} {al['seccion']} | Turno {al['turno']}", unsafe_allow_html=True)
    
    if al.get("nombre_apoderado"):
        st.caption("Apoderado: " + al["nombre_apoderado"] + (" - Tel: " + al["telefono_apoderado"] if al.get("telefono_apoderado") else ""))

    # Botón para regresar y buscar a otro alumno
    if st.button("← Buscar otro alumno", key="jp_volver_buscar"):
        st.session_state.pop("jp_al_sel_id", None)
        _reset_selector(clave)
        st.rerun()

    try:
        df_prev = leer_df("""
            SELECT id, fecha_objetivo AS "Fecha objetivo", motivo AS Motivo,
            CASE WHEN aplicada=1 THEN 'Aplicada'
                 WHEN aplicada=-1 THEN 'Anulada'
                 ELSE 'Pendiente' END AS Estado,
            COALESCE(creado_por,'') AS "Creado por"
            FROM justificaciones_previas WHERE alumno_id=?
            ORDER BY fecha_objetivo DESC
        """, (al["id"],))
    except Exception as e:
        log.warning("hist just previas fallo: %s", e)
        df_prev = pd.DataFrame()

    if not df_prev.empty:
        st.markdown("**Justificaciones previas de este alumno**")
        st.dataframe(df_prev, width='stretch', hide_index=True)
    else:
        st.info("Sin justificaciones previas para este alumno.")

    st.markdown("---")
    st.markdown("**Registrar nueva justificacion previa (FALTA)**")
    hoy = ahora().date()
    with st.form("form_just_prev"):
        c1, c2 = st.columns(2)
        with c1:
            fecha_obj = st.date_input("Fecha objetivo", value=hoy,
                                       min_value=hoy,
                                       max_value=hoy + timedelta(days=2),
                                       key="jp_fecha")
        with c2:
            motivo = st.text_area("Motivo (obligatorio)", key="jp_motivo",
                                   placeholder="Ej: Cita medica")
        sub = st.form_submit_button("Registrar justificacion", type="primary")
    if sub:
        if not motivo.strip():
            st.error("El motivo es obligatorio.")
        else:
            ok, msg = crear_justificacion_previa(
                al["id"], fecha_obj.strftime("%Y-%m-%d"), FALTA,
                motivo.strip(), usuario)
            if ok:
                st.toast(msg)
                st.rerun()
            else:
                st.error(msg)


def _render_historial_global_jp(usuario):
    st.markdown("**Justificaciones previas de HOY / mañana / PASADO mañana**")
    hoy_s = hoy_str()
    pasado_s = (ahora().date() + timedelta(days=2)).strftime("%Y-%m-%d")
    
    try:
        df_vig = leer_df("""
            SELECT jp.fecha_objetivo AS Fecha, a.dni AS DNI,
            a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno,
            g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno,
            jp.motivo AS Motivo,
            CASE WHEN jp.aplicada=1 THEN 'Aplicada'
                 WHEN jp.aplicada=-1 THEN 'Anulada'
                 ELSE 'Pendiente' END AS Estado
            FROM justificaciones_previas jp JOIN alumnos a ON jp.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE jp.fecha_objetivo BETWEEN ? AND ?
            ORDER BY jp.fecha_objetivo, a.apellido_paterno
        """, (to_date(hoy_s), to_date(pasado_s)))
    except Exception as e:
        log.warning("jp vigentes fallo: %s", e)
        df_vig = pd.DataFrame()

    if df_vig.empty:
        st.info("Sin justificaciones previas para hoy, mañana o pasado mañana.")
    else:
        st.dataframe(df_vig, width='stretch', hide_index=True)

    st.markdown("---")
    st.markdown("**Buscar en todos los tiempos**")
    
    # 🆕 FILTROS Y BÚSQUEDA
    c1, c2 = st.columns([1, 2])
    with c1:
        filtro_estado = st.selectbox("Filtrar por estado",
                                     ["Todas", "Pendientes", "Aplicadas", "Anuladas"],
                                     key="jp_filtro_estado")
    with c2:
        filtro_texto = st.text_input("Buscar por DNI o apellido",
                                      key="jp_filtro_texto",
                                      placeholder="Ej: 12345678 o Quispe")
    
    if st.button("🔍 Buscar Justificaciones", type="primary", width='stretch'):
        st.session_state["_buscar_jp_hist"] = True

    if st.session_state.get("_buscar_jp_hist"):
        if filtro_estado == "Todas" and not filtro_texto.strip():
            st.info("Selecciona un estado o escribe un nombre/DNI para buscar.")
            return

        q = ("""SELECT jp.fecha_objetivo AS Fecha, a.dni AS DNI,
             a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno,
             g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno,
             jp.motivo AS Motivo,
             CASE WHEN jp.aplicada=1 THEN 'Aplicada'
                  WHEN jp.aplicada=-1 THEN 'Anulada'
                  ELSE 'Pendiente' END AS Estado,
             COALESCE(jp.creado_por,'') AS "Creado por"
             FROM justificaciones_previas jp JOIN alumnos a ON jp.alumno_id=a.id
             JOIN secciones s ON a.seccion_id=s.id
             JOIN grados g ON s.grado_id=g.id
             JOIN turnos t ON s.turno_id=t.id WHERE 1=1""")
        params = []
        if filtro_estado == "Pendientes":
            q += " AND jp.aplicada=0"
        elif filtro_estado == "Aplicadas":
            q += " AND jp.aplicada=1"
        elif filtro_estado == "Anuladas":
            q += " AND jp.aplicada=-1"
        if filtro_texto.strip():
            q += (" AND (a.dni LIKE ? OR a.apellido_paterno LIKE ? "
                  "OR a.apellido_materno LIKE ?)")
            pat = "%" + filtro_texto.strip() + "%"
            params += [pat, pat, pat]
        q += " ORDER BY jp.fecha_objetivo DESC, a.apellido_paterno LIMIT 500"

        try:
            df_all = leer_df(q, params)
        except Exception as e:
            log.warning("jp todas fallo: %s", e)
            st.error(f"Error consultando justificaciones: {e}")
            df_all = pd.DataFrame()

        if df_all.empty:
            st.info("Sin justificaciones previas con esos filtros.")
        else:
            st.write(f"{len(df_all)} justificaciones encontradas")
            # 🆕 SOLO DATAFRAME (Cero RAM en CSS)
            st.dataframe(df_all, width='stretch', hide_index=True)


# ─── PERMISOS ──────────────────────────────────────────────────────────────
def _frag_permisos(usuario):
    st.subheader("Permisos de inasistencia")
    st.caption(f"Permisos para dias FUTUROS (desde mañana, maximo "
               f"{MAX_DIAS_PERMISO} dias).")
    tabs = st.tabs(["Historial global", "Registrar"])
    with tabs[0]:
        _render_historial_global_perm(usuario)
    with tabs[1]:
        _render_registrar_perm(usuario)


def _render_registrar_perm(usuario):
    clave = "perm_sel"
    
    # 🆕 Lógica mejorada: Si ya hay alumno seleccionado, mostramos el formulario directo
    al_sel_id = st.session_state.get("perm_al_sel_id")
    
    if not al_sel_id:
        idsec = _selector_grado_seccion(clave, "Elige el grado")
        if not idsec:
            return
        al = _selector_alumno_accion(
            clave, idsec,
            texto_boton="Seleccionar",
            key_boton="perm_sel_btn",
            on_click_key="perm_al_sel_id")
        if not al:
            return
    else:
        with cursor() as (con, cur):
            cur.execute("""
                SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
                       a.seccion_id, a.nombre_apoderado, a.telefono_apoderado,
                       g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
                FROM alumnos a JOIN secciones s ON a.seccion_id=s.id
                JOIN grados g ON s.grado_id=g.id JOIN turnos t ON s.turno_id=t.id
                WHERE a.id=?
            """, (al_sel_id,))
            al_row = cur.fetchone()
        if not al_row:
            st.session_state.pop("perm_al_sel_id", None)
            _reset_selector(clave)
            st.rerun()
            return
        al = dict(al_row)

    bloq = alumno_bloqueado(al["id"])
    if bloq:
        st.error("Este alumno esta BLOQUEADO: " + (bloq.get("motivo") or ""))
        
    nombre = (al['apellido_paterno'] + " " + (al['apellido_materno'] or "") + ", " + al['nombres']).strip(", ")
    st.markdown(f"**{nombre}** | DNI {al['dni']} | {al['grado']} {al['seccion']}", unsafe_allow_html=True)

    if st.button("← Buscar otro alumno", key="perm_volver_buscar"):
        st.session_state.pop("perm_al_sel_id", None)
        _reset_selector(clave)
        st.rerun()

    try:
        df_perm = leer_df("""
            SELECT id, fecha_inicio AS Inicio, fecha_fin AS Fin,
            COALESCE(motivo,'') AS Motivo,
            CASE WHEN activo=1 THEN 'Activo' ELSE 'Inactivo' END AS Estado,
            COALESCE(creado_por,'') AS "Creado por"
            FROM permisos WHERE alumno_id=? ORDER BY fecha_inicio DESC
        """, (al["id"],))
    except Exception as e:
        log.warning("hist permisos fallo: %s", e)
        df_perm = pd.DataFrame()

    if not df_perm.empty:
        st.markdown("**Permisos de este alumno**")
        st.dataframe(df_perm, width='stretch', hide_index=True)
    else:
        st.info("Sin permisos para este alumno.")

    st.markdown("---")
    st.markdown("**Registrar nuevo permiso**")
    manana = ahora().date() + timedelta(days=1)
    with st.form("form_permiso"):
        c1, c2 = st.columns(2)
        with c1:
            fi = st.date_input("Fecha inicio", value=manana,
                                min_value=manana, key="perm_fi")
        with c2:
            ff = st.date_input("Fecha fin", value=manana,
                                min_value=manana, key="perm_ff")
        motivo = st.text_area("Motivo (obligatorio)", key="perm_motivo",
                               placeholder="Ej: Viaje familiar")
        sub = st.form_submit_button("Registrar permiso", type="primary")
    if sub:
        if not motivo.strip():
            st.error("El motivo es obligatorio.")
        elif ff < fi:
            st.error("La fecha fin no puede ser anterior a la fecha inicio.")
        elif (ff - fi).days + 1 > MAX_DIAS_PERMISO:
            st.error(f"Maximo {MAX_DIAS_PERMISO} dias.")
        else:
            ok, msg = crear_permiso(al["id"], fi.strftime("%Y-%m-%d"),
                                     ff.strftime("%Y-%m-%d"),
                                     motivo.strip(), usuario)
            if ok:
                st.toast(msg)
                st.rerun()
            else:
                st.error(msg)


def _render_historial_global_perm(usuario):
    st.markdown("**Permisos vigentes HOY**")
    hoy_s = hoy_str()
    
    try:
        df_hoy = leer_df("""
            SELECT p.fecha_inicio AS Inicio, p.fecha_fin AS Fin, a.dni AS DNI,
            a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno,
            g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno,
            COALESCE(p.motivo,'') AS Motivo
            FROM permisos p JOIN alumnos a ON p.alumno_id=a.id
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE p.activo=1 AND p.fecha_inicio<=? AND p.fecha_fin>=?
            ORDER BY a.apellido_paterno
        """, (to_date(hoy_s), to_date(hoy_s)))
    except Exception as e:
        log.warning("perm hoy fallo: %s", e)
        df_hoy = pd.DataFrame()

    if df_hoy.empty:
        st.info("Sin permisos vigentes hoy.")
    else:
        st.dataframe(df_hoy, width='stretch', hide_index=True)

    st.markdown("---")
    st.markdown("**Buscar en todos los tiempos**")
    
    # 🆕 FILTROS Y BÚSQUEDA
    c1, c2 = st.columns([1, 2])
    with c1:
        filtro_estado = st.selectbox("Filtrar por estado",
                                     ["Todos", "Activos", "Inactivos"],
                                     key="perm_filtro_estado")
    with c2:
        filtro_texto = st.text_input("Buscar por DNI o apellido",
                                      key="perm_filtro_texto",
                                      placeholder="Ej: 12345678")
                                      
    if st.button("🔍 Buscar Permisos", type="primary", width='stretch'):
        st.session_state["_buscar_perm_hist"] = True

    if st.session_state.get("_buscar_perm_hist"):
        if filtro_estado == "Todos" and not filtro_texto.strip():
            st.info("Selecciona un estado o escribe un nombre/DNI para buscar.")
            return

        q = ("""SELECT p.fecha_inicio AS Inicio, p.fecha_fin AS Fin,
             a.dni AS DNI,
             a.apellido_paterno||' '||COALESCE(a.apellido_materno,'')||', '||a.nombres AS Alumno,
             g.nombre AS Grado, s.nombre AS Seccion, t.nombre AS Turno,
             COALESCE(p.motivo,'') AS Motivo,
             CASE WHEN p.activo=1 THEN 'Activo' ELSE 'Inactivo' END AS Estado
             FROM permisos p JOIN alumnos a ON p.alumno_id=a.id
             JOIN secciones s ON a.seccion_id=s.id
             JOIN grados g ON s.grado_id=g.id
             JOIN turnos t ON s.turno_id=t.id WHERE 1=1""")
        params = []
        if filtro_estado == "Activos":
            q += " AND p.activo=1"
        elif filtro_estado == "Inactivos":
            q += " AND p.activo=0"
        if filtro_texto.strip():
            q += (" AND (a.dni LIKE ? OR a.apellido_paterno LIKE ? "
                  "OR a.apellido_materno LIKE ?)")
            pat = "%" + filtro_texto.strip() + "%"
            params += [pat, pat, pat]
        q += " ORDER BY p.fecha_inicio DESC, a.apellido_paterno LIMIT 500"

        try:
            df_all = leer_df(q, params)
        except Exception as e:
            log.warning("perm todas fallo: %s", e)
            st.error(f"Error consultando permisos: {e}")
            df_all = pd.DataFrame()

        if df_all.empty:
            st.info("Sin permisos con esos filtros.")
        else:
            st.write(f"{len(df_all)} permisos encontrados")
            st.dataframe(df_all, width='stretch', hide_index=True)


def vista_justificaciones_permisos():
    st.title("Justificaciones y Permisos")
    usuario = st.session_state["user"]
    tabs = st.tabs(["Justificacion previa", "Permisos"])
    with tabs[0]:
        _frag_justificacion_previa(usuario)
    with tabs[1]:
        _frag_permisos(usuario)


# ─── BACKUP ────────────────────────────────────────────────────────────────
def vista_backup():
    st.title("Backup de base de datos")
    from sync import boton_backup
    boton_backup()


# ─── ESTILOS CSS ───────────────────────────────────────────────────────────
def aplicar_estilos():
    st.markdown("""
    <style>
    /* ══════════════════════════════════════════════════════════════════
       IMPORTAR FUENTE NUNITO DESDE GOOGLE FONTS
       ══════════════════════════════════════════════════════════════════ */
    @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@300;400;500;600;700;800;900&display=swap');

    /* ══════════════════════════════════════════════════════════════════
       VARIABLES GLOBALES
       ══════════════════════════════════════════════════════════════════ */
    :root{
        --primario:       #E87B4A;
        --primario-dark:  #D66535;
        --primario-soft:  rgba(232,123,74,.15);
        --primario-borde: rgba(232,123,74,.35);
        --acento:         #F5B896;
        --acento-soft:    rgba(245,184,150,.25);
        --texto-btn:      #FFFFFF;
        --texto-titulo:   #C4562A;
        --texto-suave:    #4A4A4A;
        --radius:         12px;
        --radius-sm:      8px;
        --sombra:         0 2px 8px rgba(0,0,0,.06);
        --fs-base:        16px;
        --fs-small:       14px;
        --fs-label:       12px;
        --fs-titulo:      30px;
        --fs-h2:          22px;
        --fuente:         'Nunito', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }

    @media (prefers-color-scheme: dark){
        :root{
            --primario:       #F08A5C;
            --primario-dark:  #E87B4A;
            --primario-soft:  rgba(240,138,92,.20);
            --primario-borde: rgba(240,138,92,.45);
            --acento:         #F5B896;
            --acento-soft:    rgba(245,184,150,.20);
            --texto-btn:      #FFFFFF;
            --texto-titulo:   #F5A582;
            --texto-suave:    #B8B8B8;
            --sombra:         0 2px 8px rgba(0,0,0,.30);
        }
    }

    /* ══════════════════════════════════════════════════════════════════
       TIPOGRAFÍA (Nunito) — SIN !important para no romper iconos
       ══════════════════════════════════════════════════════════════════ */
    html, body, [class*="css"], .stApp,
    .stMarkdown, .stText, .stButton, .stTextInput, .stTextArea,
    .stSelectbox, .stMultiSelect, .stDateInput, .stTimeInput,
    .stNumberInput, .stRadio, .stCheckbox, .stSlider,
    .stTabs, .stExpander, div[data-testid="stSidebar"],
    div[data-testid="stMetric"], button, input, textarea, select,
    h1, h2, h3, h4, h5, h6, p, span, a, label, div{
        font-family: var(--fuente);
    }

    /* ══════════════════════════════════════════════════════════════════
       FIX: ICONOS MATERIAL SYMBOLS (NO tocar)
       ══════════════════════════════════════════════════════════════════ */
    span[data-testid="stIconMaterial"],
    span[data-testid="stIconMaterial"] *,
    .material-symbols-rounded,
    .material-symbols-rounded *,
    [class*="material-symbols"],
    [class*="material-icons"],
    [class*="MaterialIcon"]{
        font-family: 'Material Symbols Rounded', 'Material Icons' !important;
        font-weight: normal !important;
        font-style: normal !important;
        font-size: 24px !important;
        line-height: 1 !important;
        letter-spacing: normal !important;
        text-transform: none !important;
        display: inline-block !important;
        white-space: nowrap !important;
        word-wrap: normal !important;
        direction: ltr !important;
        -webkit-font-feature-settings: 'liga' !important;
        -webkit-font-smoothing: antialiased !important;
    }

    html, body, [class*="css"], .stApp{
        font-size: var(--fs-base);
    }

    h1{
        font-size: var(--fs-titulo) !important;
        font-weight: 800 !important;
        color: var(--texto-titulo) !important;
        -webkit-text-fill-color: var(--texto-titulo) !important;
        letter-spacing: -0.5px;
    }
    h2{
        font-size: var(--fs-h2) !important;
        font-weight: 700 !important;
        color: var(--texto-titulo) !important;
    }
    h3{
        font-size: 19px !important;
        font-weight: 700 !important;
        color: var(--texto-titulo) !important;
    }

    p, span, label, .stMarkdown, .stText, div[data-testid="stMarkdownContainer"]{
        font-size: var(--fs-base);
    }

    .stCaption, small, div[data-testid="stCaptionContainer"]{
        font-size: var(--fs-small) !important;
        opacity: 0.75;
    }

    /* ══════════════════════════════════════════════════════════════════
       SIDEBAR
       ══════════════════════════════════════════════════════════════════ */
    section[data-testid="stSidebar"]{
        border-right: 1px solid var(--primario-borde);
    }
    section[data-testid="stSidebar"] .stRadio label{
        border-radius: var(--radius-sm) !important;
        padding: 12px 15px !important;
        font-weight: 600 !important;
        font-size: var(--fs-small) !important;
        margin: 2px 0 !important;
        cursor: pointer;
        border-left: 3px solid transparent;
        transition: all .15s ease;
    }
    section[data-testid="stSidebar"] .stRadio label:hover{
        background: var(--primario-soft) !important;
        border-left-color: var(--primario);
    }
    section[data-testid="stSidebar"] .stRadio input{display:none;}

    .encabezado-sidebar{
        background: linear-gradient(135deg, var(--primario) 0%, var(--primario-dark) 100%);
        padding: 20px 16px;
        margin: 8px 10px 18px 10px;
        border-radius: var(--radius);
        text-align: center;
        color: #FFF;
        box-shadow: var(--sombra);
    }
    .encabezado-sidebar .avatar{
        width: 58px; height: 58px; border-radius: 50%;
        background: rgba(255,255,255,.25);
        display: flex; align-items: center; justify-content: center;
        font-size: 24px; font-weight: 800; color: #FFF;
        margin: 0 auto 10px auto;
        border: 2px solid rgba(255,255,255,.5);
    }
    .encabezado-sidebar .nombre{
        font-size: 16px; font-weight: 700; color: #FFF !important;
    }
    .encabezado-sidebar .rol{
        display: inline-block; margin-top: 8px; padding: 4px 12px;
        background: rgba(255,255,255,.25); border-radius: 20px;
        font-size: 11px; font-weight: 800; text-transform: uppercase;
        letter-spacing: .10em; color: #FFF !important;
    }

    /* ══════════════════════════════════════════════════════════════════
       INPUTS
       ══════════════════════════════════════════════════════════════════ */
    .stTextInput input, .stNumberInput input, .stDateInput input,
    .stTimeInput input, .stTextArea textarea, .stSelectbox > div > div{
        border-radius: var(--radius-sm) !important;
        min-height: 46px;
        font-size: var(--fs-base) !important;
        border: 2px solid var(--primario-borde) !important;
    }
    .stTextInput input:focus, .stTextArea textarea:focus{
        border-color: var(--primario) !important;
        box-shadow: 0 0 0 3px var(--primario-soft) !important;
    }

    /* ══════════════════════════════════════════════════════════════════
       BOTONES
       ══════════════════════════════════════════════════════════════════ */
    .stButton > button, .stFormSubmitButton > button, .stDownloadButton > button{
        background: var(--primario) !important;
        color: #FFFFFF !important;
        border-radius: var(--radius-sm) !important;
        font-weight: 700 !important;
        border: none !important;
        padding: 12px 22px !important;
        font-size: var(--fs-small) !important;
        min-height: 46px;
        box-shadow: 0 2px 6px rgba(232,123,74,.25);
        transition: all .15s ease;
    }
    .stButton > button p,
    .stFormSubmitButton > button p,
    .stDownloadButton > button p{
        color: #FFFFFF !important;
    }
    .stButton > button:hover, .stFormSubmitButton > button:hover,
    .stDownloadButton > button:hover{
        background: var(--primario-dark) !important;
        transform: translateY(-1px);
    }

    /* ══════════════════════════════════════════════════════════════════
       MÉTRICAS
       ══════════════════════════════════════════════════════════════════ */
    div[data-testid="stMetric"]{
        background: var(--primario-soft);
        border: 1px solid var(--primario-borde);
        border-radius: var(--radius);
        padding: 20px 22px !important;
    }
    div[data-testid="stMetric"] label{
        font-size: 12px !important; font-weight: 800 !important;
        text-transform: uppercase; letter-spacing: .08em !important;
    }
    div[data-testid="stMetric"] div[data-testid="stMetricValue"]{
        font-size: 32px !important; font-weight: 800 !important;
        color: var(--texto-titulo) !important;
    }

    /* ══════════════════════════════════════════════════════════════════
       TABS
       ══════════════════════════════════════════════════════════════════ */
    .stTabs [data-baseweb="tab-list"]{
        gap: 4px;
        border-bottom: 2px solid var(--primario-borde);
    }
    .stTabs [data-baseweb="tab"]{
        font-weight: 700 !important;
        padding: 12px 18px !important;
        font-size: var(--fs-small) !important;
        opacity: 0.7;
    }
    .stTabs [aria-selected="true"]{
        font-weight: 800 !important;
        color: var(--texto-titulo) !important;
        border-bottom: 3px solid var(--primario) !important;
        opacity: 1;
    }

    /* ══════════════════════════════════════════════════════════════════
       EXPANDERS
       ══════════════════════════════════════════════════════════════════ */
    .streamlit-expanderHeader, details summary{
        border: 1px solid var(--primario-borde) !important;
        border-radius: var(--radius-sm) !important;
        font-weight: 700 !important;
        padding: 14px 16px !important;
        font-size: var(--fs-base) !important;
    }
    .streamlit-expanderHeader:hover{
        border-color: var(--primario) !important;
    }

    /* ══════════════════════════════════════════════════════════════════
       HEADERS DE PÁGINA
       ══════════════════════════════════════════════════════════════════ */
    .scan-header, .puerta-header{
        background: linear-gradient(135deg, var(--primario) 0%, var(--primario-dark) 100%);
        padding: 24px 28px;
        border-radius: var(--radius);
        margin-bottom: 20px;
        color: #FFF;
        box-shadow: var(--sombra);
    }
    .scan-header .scan-titulo{ font-size: 26px; font-weight: 800; color:#FFF; }
    .scan-header .scan-sub{ font-size: 15px; opacity:.9; margin-top:4px; color:#FFF; }
    .puerta-header .titulo{ font-size: 28px; font-weight: 800; color:#FFF; }
    .puerta-header .fecha{ font-size: 15px; opacity:.9; margin-top:4px; color:#FFF; }

    /* ══════════════════════════════════════════════════════════════════
       PERFIL HERO
       ══════════════════════════════════════════════════════════════════ */
    .perfil-hero{
        background: linear-gradient(135deg, var(--primario) 0%, var(--primario-dark) 100%);
        border-radius: 14px;
        padding: 26px 30px;
        margin-bottom: 22px;
        display: flex;
        align-items: center;
        gap: 22px;
        flex-wrap: wrap;
        color: #FFF;
        box-shadow: 0 4px 16px rgba(232,123,74,.20);
    }
    .perfil-hero .hero-avatar{
        width: 84px; height: 84px; border-radius: 50%;
        background: rgba(255,255,255,.20);
        border: 2px solid rgba(255,255,255,.45);
        display: flex; align-items: center; justify-content: center;
        font-size: 34px; font-weight: 800; color: #FFF;
        flex-shrink: 0;
    }
    .perfil-hero .hero-info{ flex: 1; min-width: 240px; }
    .perfil-hero .hero-nombre{
        font-size: 24px; font-weight: 800; color: #FFF !important;
        margin-bottom: 4px;
    }
    .perfil-hero .hero-meta{
        font-size: 14px; opacity: .95; color: #FFF !important;
        line-height: 1.5;
    }
    .perfil-hero .hero-badge{
        display: inline-block; margin-top: 10px;
        padding: 4px 14px; border-radius: 20px;
        font-size: 10px; font-weight: 800;
        text-transform: uppercase; letter-spacing: .12em;
        background: rgba(255,255,255,.25);
        border: 1px solid rgba(255,255,255,.45);
        color: #FFF !important;
    }

    /* ══════════════════════════════════════════════════════════════════
       BLOQUES / CARDS
       ══════════════════════════════════════════════════════════════════ */
    .perfil-bloque{
        background: var(--secondary-background-color);
        border: 1px solid var(--primario-borde);
        border-radius: var(--radius);
        padding: 20px 22px;
        margin-bottom: 20px;
        box-shadow: var(--sombra);
    }
    .perfil-bloque .titulo-seccion{
        font-size: 12px; font-weight: 800; text-transform: uppercase;
        letter-spacing: .12em; color: var(--texto-titulo);
        margin-bottom: 14px; padding-bottom: 8px;
        border-bottom: 1px solid var(--primario-borde);
    }
    .info-linea{
        display: flex; padding: 6px 0;
        font-size: 14px;
        border-bottom: 1px solid var(--primario-soft);
    }
    .info-linea:last-child{ border-bottom: none; }
    .info-label{
        font-size: 12px; font-weight: 800;
        text-transform: uppercase; letter-spacing: .05em;
        opacity: 0.65;
        min-width: 130px;
    }
    .info-valor{ font-weight: 600; }

    /* ══════════════════════════════════════════════════════════════════
       KPI CARDS
       ══════════════════════════════════════════════════════════════════ */
    .kpi-grid{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
        gap: 14px;
    }
    .kpi-card{
        background: var(--primario-soft);
        border: 1px solid var(--primario-borde);
        border-radius: 10px;
        padding: 16px 12px;
        text-align: center;
        transition: all .15s ease;
    }
    .kpi-card:hover{
        border-color: var(--primario);
        box-shadow: var(--sombra);
    }
    .kpi-card .kpi-num{
        font-size: 30px; font-weight: 800; color: var(--texto-titulo);
        line-height: 1; margin-bottom: 6px;
    }
    .kpi-card .kpi-lbl{
        font-size: 11px; font-weight: 800; text-transform: uppercase;
        letter-spacing: .08em; line-height: 1.3;
        opacity: 0.75;
    }
    .kpi-card .kpi-lbl-alerta{ color: #C62828; opacity: 1; }
    @media (prefers-color-scheme: dark){
        .kpi-card .kpi-lbl-alerta{ color: #FC8181; }
    }

    /* ══════════════════════════════════════════════════════════════════
       MENSAJES QR
       ══════════════════════════════════════════════════════════════════ */
    .scan-ultimos{
        font-size: 11px; font-weight: 800; text-transform: uppercase;
        letter-spacing: .12em; margin: 24px 0 12px 0; padding-bottom: 8px;
        border-bottom: 2px solid var(--primario-borde); color: var(--texto-titulo);
    }
    .qr-msg{
        background: var(--secondary-background-color);
        padding: 16px 20px;
        border-radius: var(--radius-sm);
        margin: 10px 0;
        border: 1px solid rgba(128,128,128,.15);
        border-left: 5px solid #808080;
        font-size: 15px;
    }
    .qr-nombre{ font-size: 17px; font-weight: 800; margin-bottom: 4px; }
    .qr-texto{ font-size: 15px; font-weight: 600; line-height: 1.4; }
    .qr-evento{
        font-size: 12px; font-weight: 800; text-transform: uppercase;
        letter-spacing: .10em; margin-bottom: 5px;
        color: var(--texto-titulo); display: inline-block;
        padding: 3px 10px; background: var(--primario-soft); border-radius: 6px;
    }
    .qr-puntual   { border-left-color:#22C55E; background: rgba(34,197,94,.06); }
    .qr-tardanza  { border-left-color:#F59E0B; background: rgba(245,158,11,.06); }
    .qr-derivado  { border-left-color:#FF9800; background: rgba(255,152,0,.08); }
    .qr-retenido  { border-left-color:#FF7043; font-weight:700; background: rgba(255,112,67,.08); }
    .qr-refuerzo  { border-left-color:#42A5F5; background: rgba(66,165,245,.06); }
    .qr-bloqueado { border-left-color:#EF4444; border:2px solid #EF4444; font-weight:800; background: rgba(239,68,68,.10); }
    .qr-ultima    { border-left-color:#E87B4A; font-weight:800; background: var(--acento-soft); }
    .qr-error     { border-left-color:#888888; }

    @media (prefers-color-scheme: dark){
        .qr-puntual   { background: rgba(34,197,94,.10); }
        .qr-tardanza  { background: rgba(245,158,11,.10); }
        .qr-derivado  { background: rgba(255,152,0,.12); }
        .qr-retenido  { background: rgba(255,112,67,.12); }
        .qr-refuerzo  { background: rgba(66,165,245,.10); }
        .qr-bloqueado { background: rgba(239,68,68,.15); }
    }

    /* ══════════════════════════════════════════════════════════════════
       BOTÓN SELECCIONADO
       ══════════════════════════════════════════════════════════════════ */
    .btn-sel .stButton > button{
        background: #43A047 !important;
        color: #FFF !important;
    }
    .btn-sel .stButton > button p{
        color: #FFF !important;
    }

    /* ══════════════════════════════════════════════════════════════════
       SEPARADORES
       ══════════════════════════════════════════════════════════════════ */
    hr{
        border: none;
        height: 1px;
        background: var(--primario-borde);
        margin: 20px 0;
    }

    /* ══════════════════════════════════════════════════════════════════
       RESPONSIVE
       ══════════════════════════════════════════════════════════════════ */
    @media (max-width: 768px){
        h1{ font-size: 1.5rem !important; }
        h2{ font-size: 1.2rem !important; }
        .stButton > button{ width: 100% !important; }
        .perfil-hero{ padding: 20px; gap: 16px; }
        .perfil-hero .hero-avatar{ width: 64px; height: 64px; font-size: 26px; }
        .perfil-hero .hero-nombre{ font-size: 19px; }
        .kpi-grid{ grid-template-columns: repeat(2, 1fr); gap: 10px; }
        .kpi-card .kpi-num{ font-size: 24px; }
        .info-linea{ flex-direction: column; }
        .info-label{ min-width: auto; margin-bottom: 2px; }
    }
    </style>
    """, unsafe_allow_html=True)
def filtros_grado_seccion_nombre(clave, placeholder="Buscar"):
    grados = listar_grados()
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        ops = [{"id": None, "nombre": "Todos"}] + grados
        g = st.selectbox("Grado", ops, format_func=lambda x: x["nombre"],
                         key=clave + "_g")
    with c2:
        secs = (([{"id": None, "nombre": "Todas"}] + secciones_por_grado(g["id"]))
                if (g and g["id"]) else [{"id": None, "nombre": "Todas"}])
        s = st.selectbox("Seccion", secs, format_func=lambda x: x["nombre"],
                         key=clave + "_s")
    with c3:
        t = st.text_input("Buscar", placeholder=placeholder, key=clave + "_t")
    return (g["id"] if g else None,
            s["id"] if (g and g["id"] and s) else None,
            t.strip())


# ─── LOGIN / CAMBIO PASSWORD / MI CUENTA ───────────────────────────────────
def vista_login():
    st.markdown("""
    <div style="text-align:center; margin-top:100px; margin-bottom:40px;">
        <h1 style="font-size:44px; margin-bottom:0; border:none; letter-spacing:-1px;">asisyarina</h1>
        <p style="opacity:0.6; margin-top:6px; font-size:15px;">Sistema de Asistencia</p>
    </div>
    """, unsafe_allow_html=True)
    _, centro, _ = st.columns([1, 1, 1])
    with centro:
        with st.form("login"):
            usuario = st.text_input("Usuario", placeholder="tu usuario")
            password = st.text_input("Contraseña", type="password",
                                     placeholder="tu contraseña")
            enviado = st.form_submit_button("Ingresar", type="primary",
                                            width='stretch')
            if enviado:
                datos, err = autenticar(usuario, password)
                if datos:
                    st.session_state["user"] = datos
                    auditar(datos["usuario"], "Login")
                    st.rerun()
                else:
                    st.error(err or "Credenciales incorrectas")


def vista_cambio_password_obligatorio():
    usuario = st.session_state["user"]
    st.title("Cambio obligatorio de contraseña")
    st.warning("Tu cuenta tiene una contraseña temporal.")
    _, centro, _ = st.columns([1, 1.2, 1])
    with centro:
        with st.form("cambio_pwd"):
            nueva = st.text_input("Nueva contraseña", type="password")
            confirmar = st.text_input("Confirmar", type="password")
            ok = st.form_submit_button("Cambiar", type="primary", width='stretch')
        if ok:
            if len(nueva) < 6:
                st.error("Minimo 6 caracteres.")
            elif nueva != confirmar:
                st.error("No coinciden.")
            else:
                nuevo_hash = hashear_password(nueva)
                escribir("UPDATE usuarios SET password=?,debe_cambiar_password=0 "
                         "WHERE id=?", (nuevo_hash, usuario["id"]))
                st.session_state["user"]["debe_cambiar_password"] = 0
                st.session_state["user"]["password"] = nuevo_hash
                auditar(usuario["usuario"], "Cambio pwd obligatorio")
                st.rerun()


def vista_mi_cuenta():
    st.title("Mi cuenta")
    usuario = st.session_state["user"]
    inicial = (usuario["nombres"] or "?")[0].upper()
    st.markdown(f"""
    <div class="perfil-bloque">
        <div class="perfil-hero" style="margin-bottom:0;">
            <div class="hero-avatar">{inicial}</div>
            <div class="hero-info">
                <div class="hero-nombre">{usuario['nombres']}</div>
                <div class="hero-meta">Usuario: {usuario['usuario']} &nbsp;|&nbsp; Rol: {usuario['rol']}</div>
                <div class="hero-meta">Ultimo login: {usuario.get('ultimo_login') or 'Nunca'}</div>
            </div>
        </div>
    </div>
    """, unsafe_allow_html=True)

    if usuario["rol"] == "Auxiliar":
        df_secs = leer_df("""
            SELECT g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
            FROM auxiliar_secciones a
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE a.usuario_id=? ORDER BY g.nombre, s.nombre
        """, (usuario["id"],))
        st.markdown("---")
        st.subheader("Mis secciones asignadas")
        if df_secs.empty:
            st.warning("No tienes secciones asignadas.")
        else:
            st.dataframe(df_secs, width='stretch')

    st.markdown("---")
    st.subheader("Mi actividad reciente")
    df_act = leer_df("SELECT accion,fecha FROM auditoria WHERE usuario=? "
                     "ORDER BY id DESC LIMIT 10", (usuario["usuario"],))
    if df_act.empty:
        st.info("Sin actividad.")
    else:
        st.dataframe(df_act, width='stretch')

    st.markdown("---")
    st.subheader("Cambiar contraseña")
    with st.form("cambiar_mi_pwd"):
        actual = st.text_input("Contraseña actual", type="password")
        nueva = st.text_input("Nueva contraseña", type="password")
        confirmar = st.text_input("Confirmar nueva contraseña", type="password")
        if st.form_submit_button("Cambiar contraseña", type="primary"):
            if not verificar_password(actual, usuario["password"]):
                st.error("Contraseña actual incorrecta.")
            elif len(nueva) < 6:
                st.error("Minimo 6 caracteres.")
            elif nueva != confirmar:
                st.error("No coinciden.")
            else:
                nueva_hash = hashear_password(nueva)
                escribir("UPDATE usuarios SET password=? WHERE id=?",
                         (nueva_hash, usuario["id"]))
                st.session_state["user"]["password"] = nueva_hash
                auditar(usuario["usuario"], "Cambio su contraseña")
                st.success("Contraseña actualizada.")

    st.markdown("---")
    if st.button("Cerrar sesion"):
        cerrar_sesion()
        st.rerun()


# ─── MENÚ Y RUTAS ──────────────────────────────────────────────────────────
def obtener_opciones_por_rol(usuario):
    rol = usuario["rol"]
    if rol == "Admin":
        return ["Puerta", "Bloqueados", "Panel Direccion", "Reportes", "Alumnos",
                "Grados y Secciones", "Carnets", "Dias especiales", "Ventanas",
                "Justificaciones y Permisos", "Usuarios", "Backup", "Auditoria",
                "Mi cuenta"]
    if rol == "Direccion":
        return ["Panel Direccion", "Bloqueados", "Reportes", "Alumnos", "Carnets",
                "Dias especiales", "Justificaciones y Permisos", "Mi cuenta"]
    if rol == "Auxiliar":
        return ["Puerta", "Bloqueados", "Justificaciones y Permisos", "Reportes",
                "Mi cuenta"]
    return []


RUTAS = {
    "Puerta": vista_puerta,
    "Bloqueados": vista_bloqueados,
    "Panel Direccion": vista_panel_direccion,
    "Reportes": vista_reportes,
    "Alumnos": vista_alumnos,
    "Grados y Secciones": vista_grados_secciones,
    "Carnets": vista_carnets,
    "Dias especiales": vista_dias_especiales,
    "Ventanas": vista_ventanas,
    "Usuarios": vista_usuarios,
    "Backup": vista_backup,
    "Auditoria": vista_auditoria,
    "Mi cuenta": vista_mi_cuenta,
    "Justificaciones y Permisos": vista_justificaciones_permisos,
}


def menu_lateral():
    usuario = st.session_state["user"]
    rol = usuario["rol"]
    opciones = obtener_opciones_por_rol(usuario)
    with st.sidebar:
        inicial = (usuario["nombres"] or "?")[0].upper()
        st.markdown(
            f'<div class="encabezado-sidebar">'
            f'<div class="avatar">{inicial}</div>'
            f'<div class="nombre">{usuario["nombres"]}</div>'
            f'<div class="rol">{rol}</div>'
            f'</div>', unsafe_allow_html=True)
        if "menu" not in st.session_state or \
                st.session_state["menu"] not in opciones:
            st.session_state["menu"] = opciones[0]
        op = st.radio("Menu", opciones, key="menu",
                      label_visibility="collapsed")
        st.markdown("---")
        if st.button("Cerrar sesion", width='stretch'):
            cerrar_sesion()
            st.rerun()
    return op


def _enrutar(op, usuario):
    v = RUTAS.get(op)
    if not v:
        st.warning("Vista no disponible.")
        return
    if op not in obtener_opciones_por_rol(usuario):
        st.error("Sin permisos.")
        auditar(usuario["usuario"], "Intento acceso no autorizado a " + op)
        return
        
    if op != "Reportes":
        st.session_state.pop("_rep_iniciado", None)
        
    # 🆕 DESTRUIR CÁMARA AL SALIR DE PUERTA
    if op != "Puerta":
        st.session_state["_qr_mount_id"] = st.session_state.get("_qr_mount_id", 0) + 1

    # 🆕 LIMPIAR MEMORIA AL CAMBIAR DE PESTAÑA (Soluciona bug de Carnets y Perfil)
    op_anterior = st.session_state.get("_menu_anterior")
    if op_anterior and op_anterior != op:
        st.session_state.pop("perfil_alumno_id", None)
        st.session_state.pop("_perfil_ultimo_id", None)
        st.session_state.pop("carn_ver_seccion", None)
        st.session_state.pop("carn_grado_sel", None)
        st.session_state.pop("carn_sel_alumnos", None)
        st.session_state.pop("incidencia_ver_id", None)

    st.session_state["_menu_anterior"] = op
    v()

def _control_faltas():
    ult = st.session_state.get("_ultimo_control_faltas")
    t = time.time()
    if ult and (t - ult) < 300:
        return
    st.session_state["_ultimo_control_faltas"] = t
    try:
        marcar_faltas_al_cierre()
    except Exception as e:
        log.warning(f"No se pudieron procesar faltas al cierre: {e}")
# ─── MAIN ──────────────────────────────────────────────────────────────────
def main():
    st.set_page_config(page_title="Asistencia I.E. Yarinacocha",
                       page_icon="escudo.png", layout="wide",
                       initial_sidebar_state="expanded")
    try:
        from db import descargar_bd_si_no_existe
        descargar_bd_si_no_existe()

        inicializar_bd()
        aplicar_estilos()

        if not st.session_state.get("user"):
            vista_login()
            return
        if not _verificar_admin_activo():
            st.error("No hay Admin activo en el sistema.")
            st.info("Contacta al desarrollador para restaurar el acceso.")
            st.stop()
        if st.session_state["user"].get("debe_cambiar_password"):
            vista_cambio_password_obligatorio()
            return
        if modo_mantenimiento() and st.session_state["user"]["rol"] != "Admin":
            vista_mantenimiento()
            return
        if sistema_bloqueado():
            st.warning("El sistema no esta configurado. No hay periodo activo "
                       "con alumnos cargados.")
            if st.session_state["user"]["rol"] == "Admin":
                st.info("Ve a Auditoria -> Periodos para crear un periodo y subir "
                        "el Excel de alumnos.")
                st.session_state["menu"] = "Auditoria"
                _enrutar("Auditoria", st.session_state["user"])
            else:
                st.info("Contacta al Administrador.")
            return
        
        # 🆕 Blinde para que no tumbe la app si la BD se traba
        try:
            _control_faltas()
        except Exception as e:
            log.warning(f"No se pudieron procesar faltas: {e}")
            
        op = menu_lateral()
        if op:
            _enrutar(op, st.session_state["user"])
    except Exception as e:
        st.error(f"Error inesperado: {e}")
        log.exception("Error en main")


if __name__ == "__main__":
    main()
