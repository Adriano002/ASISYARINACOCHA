# app.py
# Sistema de Asistencia I.E. Yarinacocha
# Versión simplificada, con comentarios en español.

# ─────────────────────────────────────────────────────────────
# IMPORTACIONES
# ─────────────────────────────────────────────────────────────

import hashlib
import logging
import re
import secrets
import time
from calendar import monthrange
from datetime import date, datetime, timedelta
from io import BytesIO
from pathlib import Path

import pandas as pd
import qrcode
import streamlit as st
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import (
    Image as RLImage,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from db import (
    escribir_seguro,
    escribir_y_esperar,
    formatear_fecha,
    formatear_hora,
    formatear_timestamp,
    leer_dataframe,
    leer_uno,
    leer_varios,
    obtener_conexion,
    obtener_fecha_actual,
    obtener_fecha_hora_actual,
    obtener_hora_actual,
    obtener_timestamp_actual,
)
from qr_scanner_component import qr_scanner


# ─────────────────────────────────────────────────────────────
# CONFIGURACIÓN DE LOGS
# ─────────────────────────────────────────────────────────────

CARPETA_LOGS = Path("logs")
CARPETA_LOGS.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(CARPETA_LOGS / "app.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("asistencia")


# ─────────────────────────────────────────────────────────────
# CONSTANTES
# ─────────────────────────────────────────────────────────────

MESES_ES = [
    "", "Enero", "Febrero", "Marzo", "Abril", "Mayo", "Junio",
    "Julio", "Agosto", "Septiembre", "Octubre", "Noviembre", "Diciembre"
]

COLOR_PRIMARIO = "#B85C38"
ITERACIONES_HASH = 260_000
ALGORITMO_HASH = "sha256"
MAX_INTENTOS_LOGIN = 3
MINUTOS_BLOQUEO = 10

ESTADO_PUNTUAL = "Puntual"
ESTADO_TARDANZA = "Tardanza"
ESTADO_FALTA = "Falta"
ESTADO_PERMISO = "Permiso"
ESTADO_ASISTIO_REFUERZO = "Asistio"

ACCION_PERDONADO = "PERDONADO"
ACCION_DERIVADO = "DERIVADO_DIRECCION"

TIPO_VENTANA_CLASES = "clases"
TIPO_VENTANA_REFUERZO = "reforzamiento"

MAX_DIAS_PERMISO = 7
HORAS_CIERRE_EVENTO = 2

LIMITE_ALUMNO_NUEVO = 3
LIMITE_ALUMNO_REINCIDENTE = 1


# ─────────────────────────────────────────────────────────────
# UTILIDADES
# ─────────────────────────────────────────────────────────────

def ahora():
    """Devuelve la fecha y hora actual en hora de Perú."""
    return obtener_fecha_hora_actual()


def hoy_texto():
    """Devuelve la fecha de hoy como texto YYYY-MM-DD."""
    return obtener_fecha_actual()


def hoy_fecha():
    """Devuelve la fecha de hoy como objeto date."""
    return obtener_fecha_hora_actual().date()


def hora_corta():
    """Devuelve la hora actual como texto HH:MM."""
    return obtener_hora_actual()


def timestamp_texto():
    """Devuelve fecha y hora completas como texto."""
    return obtener_timestamp_actual()


def es_fin_de_semana(fecha=None):
    """Devuelve True si la fecha es sábado o domingo."""
    fecha = fecha or hoy_fecha()
    return fecha.weekday() >= 5


def hash_password(password):
    """
    Convierte una contraseña en un hash seguro.
    Usamos PBKDF2 con SHA-256 (el estándar recomendado).
    """
    salt = secrets.token_bytes(16)
    hash_bytes = hashlib.pbkdf2_hmac(
        ALGORITMO_HASH,
        password.encode("utf-8"),
        salt,
        ITERACIONES_HASH,
    )
    return f"pbkdf2_{ALGORITMO_HASH}${ITERACIONES_HASH}${salt.hex()}${hash_bytes.hex()}"


def verificar_password(password, hash_guardado):
    """
    Verifica si una contraseña coincide con su hash.
    Devuelve True o False.
    """
    if not hash_guardado or not hash_guardado.startswith("pbkdf2_"):
        return False
    try:
        _, iteraciones, salt_hex, hash_hex = hash_guardado.split("$")
        hash_calculado = hashlib.pbkdf2_hmac(
            ALGORITMO_HASH,
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            int(iteraciones),
        )
        return secrets.compare_digest(hash_calculado.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def obtener_ip():
    """Devuelve la IP del usuario (o 'local' si no se puede)."""
    try:
        return st.context.headers.get("X-Forwarded-For", "local").split(",")[0].strip()
    except Exception:
        return "local"


def auditar(usuario, accion, tabla=None, registro_id=None):
    """
    Guarda un registro en la tabla de auditoría.
    Sirve para saber quién hizo qué y cuándo.
    """
    try:
        escribir_seguro(
            "INSERT INTO auditoria(usuario, accion, fecha, tabla_afectada, registro_id, ip) "
            "VALUES(?, ?, ?, ?, ?, ?)",
            (usuario, accion, timestamp_texto(), tabla, registro_id, obtener_ip()),
        )
    except Exception as error:
        log.warning("No se pudo auditar: %s", error)


def generar_qr(dni, tamano=220):
    """
    Genera un código QR con el DNI del alumno.
    Devuelve una imagen PIL.
    """
    qr = qrcode.QRCode(
        version=1,
        error_correction=qrcode.constants.ERROR_CORRECT_H,
        box_size=10,
        border=2,
    )
    qr.add_data(str(dni).strip())
    qr.make(fit=True)
    imagen = qr.make_image(fill_color="black", back_color="white").convert("RGB")
    return imagen


def generar_qr_bytes(dni, tamano=220):
    """Genera un QR y devuelve sus bytes (para descargar)."""
    imagen = generar_qr(dni, tamano)
    buffer = BytesIO()
    imagen.save(buffer, format="PNG")
    buffer.seek(0)
    return buffer.getvalue()


def calcular_limite_tardanzas(es_reincidente):
    """Devuelve cuántas tardanzas se permiten antes de bloquear."""
    if es_reincidente:
        return LIMITE_ALUMNO_REINCIDENTE
    return LIMITE_ALUMNO_NUEVO


def nombre_completo_alumno(alumno):
    """Devuelve el nombre completo en formato 'Apellidos, Nombres'."""
    apellidos = alumno.get("apellido_paterno", "")
    if alumno.get("apellido_materno"):
        apellidos += " " + alumno["apellido_materno"]
    return f"{apellidos}, {alumno.get('nombres', '')}".strip(", ")


# ─────────────────────────────────────────────────────────────
# ESTILOS CSS (minimalista, sin HTML de faz)
# ─────────────────────────────────────────────────────────────

def aplicar_estilos():
    """
    Aplica los estilos CSS globales.
    Todo apunta a clases .st-key-... que Streamlit genera
    cuando usamos st.container(key="...").
    """
    st.markdown("""
    <style>
    @import url('https://fonts.googleapis.com/css2?family=Nunito:wght@400;600;700;800&display=swap');

    :root{
        --primario:       #E87B4A;
        --primario-dark:  #D66535;
        --primario-soft:  rgba(232,123,74,.15);
        --primario-borde: rgba(232,123,74,.35);
        --texto-titulo:   #C4562A;
        --radius:         12px;
        --radius-sm:      8px;
        --fuente:         'Nunito', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    @media (prefers-color-scheme: dark){
        :root{
            --primario:       #F08A5C;
            --primario-dark:  #E87B4A;
            --primario-soft:  rgba(240,138,92,.20);
            --primario-borde: rgba(240,138,92,.45);
            --texto-titulo:   #F5A582;
        }
    }

    html, body, [class*="css"], .stApp,
    .stMarkdown, .stText, .stButton, .stTextInput, .stTextArea,
    .stSelectbox, .stMultiSelect, .stDateInput, .stTimeInput,
    .stNumberInput, .stRadio, .stCheckbox, .stTabs, .stExpander,
    div[data-testid="stSidebar"], div[data-testid="stMetric"],
    button, input, textarea, select,
    h1, h2, h3, h4, h5, h6, p, span, a, label, div{
        font-family: var(--fuente);
    }

    h1{ font-size: 30px !important; font-weight: 800 !important;
        color: var(--texto-titulo) !important; letter-spacing: -0.5px; }
    h2{ font-size: 22px !important; font-weight: 700 !important;
        color: var(--texto-titulo) !important; }
    h3{ font-size: 19px !important; font-weight: 700 !important;
        color: var(--texto-titulo) !important; }

    /* ── SIDEBAR ── */
    section[data-testid="stSidebar"]{
        border-right: 1px solid var(--primario-borde);
    }
    section[data-testid="stSidebar"] .stRadio label{
        border-radius: var(--radius-sm) !important;
        padding: 12px 15px !important;
        font-weight: 600 !important;
        font-size: 14px !important;
        margin: 2px 0 !important;
        cursor: pointer;
        border-left: 3px solid transparent;
        transition: all .15s ease;
    }
    section[data-testid="stSidebar"] .stRadio label:hover{
        background: var(--primario-soft) !important;
        border-left-color: var(--primario);
    }
    section[data-testid="stSidebar"] .stRadio input{ display: none; }

    /* ── INPUTS ── */
    .stTextInput input, .stNumberInput input, .stDateInput input,
    .stTimeInput input, .stTextArea textarea, .stSelectbox > div > div{
        border-radius: var(--radius-sm) !important;
        min-height: 46px;
        font-size: 16px !important;
        border: 2px solid var(--primario-borde) !important;
    }
    .stTextInput input:focus, .stTextArea textarea:focus{
        border-color: var(--primario) !important;
        box-shadow: 0 0 0 3px var(--primario-soft) !important;
    }

    /* ── BOTONES ── */
    .stButton > button, .stFormSubmitButton > button,
    .stDownloadButton > button{
        background: var(--primario) !important;
        color: #FFFFFF !important;
        border-radius: var(--radius-sm) !important;
        font-weight: 700 !important;
        border: none !important;
        padding: 12px 22px !important;
        font-size: 14px !important;
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

    /* ── MÉTRICAS ── */
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

    /* ── TABS ── */
    .stTabs [data-baseweb="tab-list"]{
        gap: 4px;
        border-bottom: 2px solid var(--primario-borde);
    }
    .stTabs [data-baseweb="tab"]{
        font-weight: 700 !important;
        padding: 12px 18px !important;
        font-size: 14px !important;
        opacity: 0.7;
    }
    .stTabs [aria-selected="true"]{
        font-weight: 800 !important;
        color: var(--texto-titulo) !important;
        border-bottom: 3px solid var(--primario) !important;
        opacity: 1;
    }

    /* ── EXPANDERS ── */
    .streamlit-expanderHeader, details summary{
        border: 1px solid var(--primario-borde) !important;
        border-radius: var(--radius-sm) !important;
        font-weight: 700 !important;
        padding: 14px 16px !important;
        font-size: 16px !important;
    }

    /* ── SIDEBAR HEADER ── */
    .st-key-sidebar_header{
        background: linear-gradient(135deg, var(--primario) 0%, var(--primario-dark) 100%);
        padding: 20px 16px;
        margin: 8px 10px 18px 10px;
        border-radius: var(--radius);
        text-align: center;
    }
    .st-key-sidebar_header *{ color: #FFF !important; }
    .st-key-sidebar_header p{
        font-size: 14px !important;
        margin: 0 !important;
    }
    .st-key-sidebar_header .avatar p{
        font-size: 32px !important;
        font-weight: 800 !important;
    }

    /* ── MENSAJES QR (verde/amarillo/rojo) ── */
    .st-key-qr_msg_puntual,
    .st-key-qr_msg_tardanza,
    .st-key-qr_msg_bloqueado,
    .st-key-qr_msg_refuerzo,
    .st-key-qr_msg_error{
        padding: 16px 20px;
        border-radius: 8px;
        margin: 10px 0;
        border-left: 5px solid #808080;
    }
    .st-key-qr_msg_puntual{
        background: rgba(34,197,94,.10);
        border-left: 5px solid #22C55E;
    }
    .st-key-qr_msg_tardanza{
        background: rgba(245,158,11,.10);
        border-left: 5px solid #F59E0B;
    }
    .st-key-qr_msg_bloqueado{
        background: rgba(239,68,68,.12);
        border: 2px solid #EF4444;
    }
    .st-key-qr_msg_bloqueado p{
        color: #B91C1C !important;
        font-weight: 800 !important;
    }
    .st-key-qr_msg_refuerzo{
        background: rgba(66,165,245,.10);
        border-left: 5px solid #42A5F5;
    }
    .st-key-qr_msg_error{
        background: rgba(128,128,128,.06);
        border-left: 5px solid #888888;
    }
    .st-key-qr_nombre p{
        font-size: 17px !important;
        font-weight: 800 !important;
        margin: 0 0 4px 0 !important;
    }
    .st-key-qr_texto p{
        font-size: 15px !important;
        font-weight: 600 !important;
        margin: 0 !important;
    }

    /* ── PERFIL HERO ── */
    .st-key-perfil_hero{
        background: linear-gradient(135deg, var(--primario) 0%, var(--primario-dark) 100%);
        border-radius: 14px;
        padding: 26px 30px;
        margin-bottom: 22px;
    }
    .st-key-perfil_hero *{ color: #FFF !important; }
    .st-key-hero_avatar p{
        font-size: 34px !important;
        font-weight: 800 !important;
        margin: 0 !important;
    }
    .st-key-hero_nombre p{
        font-size: 24px !important;
        font-weight: 800 !important;
        margin: 0 !important;
    }
    .st-key-hero_meta p{
        font-size: 14px !important;
        opacity: .95;
        margin: 4px 0 !important;
    }

    /* ── BLOQUES ── */
    .st-key-bloque{
        background: var(--secondary-background-color);
        border: 1px solid var(--primario-borde);
        border-radius: var(--radius);
        padding: 20px 22px;
        margin-bottom: 20px;
    }

    /* ── CARNET EN PERFIL ── */
    .st-key-carnet_alumno img{
        border-radius: var(--radius);
        box-shadow: 0 4px 16px rgba(232,123,74,.20);
    }

    /* ── RESPONSIVE ── */
    @media (max-width: 768px){
        h1{ font-size: 1.5rem !important; }
        h2{ font-size: 1.2rem !important; }
        .stButton > button{ width: 100% !important; }
    }
    </style>
    """, unsafe_allow_html=True)


# ─────────────────────────────────────────────────────────────
# INICIALIZACIÓN DE LA BASE DE DATOS
# ─────────────────────────────────────────────────────────────

@st.cache_resource
def inicializar_base_datos():
    """
    Crea las tablas si no existen y siembra datos iniciales.
    Solo se ejecuta una vez por proceso.
    """
    from schema import aplicar_schema
    aplicar_schema()
    _sembrar_datos_iniciales()
    log.info("Base de datos lista.")
    return True


def _sembrar_datos_iniciales():
    """Crea turnos, ventanas, grados y admin si no existen."""
    from db import obtener_conexion
    conexion = obtener_conexion()
    cursor = conexion.cursor()

    # Turnos
    cursor.execute("SELECT COUNT(*) AS n FROM turnos")
    if cursor.fetchone()["n"] == 0:
        cursor.executemany(
            "INSERT INTO turnos(nombre, hora_entrada, hora_salida, tolerancia_min) "
            "VALUES(?, ?, ?, ?)",
            [
                ("Mañana", "06:00", "12:25", 10),
                ("Tarde", "12:00", "18:10", 10),
            ],
        )

    # Ventanas
    cursor.execute("SELECT COUNT(*) AS n FROM ventanas")
    if cursor.fetchone()["n"] == 0:
        cursor.execute("SELECT id FROM turnos WHERE nombre = ?", ("Mañana",))
        turno_manana = cursor.fetchone()
        cursor.execute("SELECT id FROM turnos WHERE nombre = ?", ("Tarde",))
        turno_tarde = cursor.fetchone()

        if turno_manana:
            cursor.execute(
                "INSERT INTO ventanas(turno_id, tipo, nombre, hora_apertura, "
                "hora_limite_puntual, hora_cierre, tolerancia_min, orden) "
                "VALUES(?, 'clases', 'Clases mañana', '06:00', '06:55', '07:10', 10, 1)",
                (turno_manana["id"],),
            )
            cursor.execute(
                "INSERT INTO ventanas(turno_id, tipo, nombre, hora_apertura, "
                "hora_limite_puntual, hora_cierre, tolerancia_min, orden) "
                "VALUES(?, 'reforzamiento', 'Reforzamiento mañana', '08:00', '08:00', '14:00', 0, 2)",
                (turno_manana["id"],),
            )

        if turno_tarde:
            cursor.execute(
                "INSERT INTO ventanas(turno_id, tipo, nombre, hora_apertura, "
                "hora_limite_puntual, hora_cierre, tolerancia_min, orden) "
                "VALUES(?, 'reforzamiento', 'Reforzamiento tarde', '10:00', '10:00', '10:20', 0, 1)",
                (turno_tarde["id"],),
            )
            cursor.execute(
                "INSERT INTO ventanas(turno_id, tipo, nombre, hora_apertura, "
                "hora_limite_puntual, hora_cierre, tolerancia_min, orden) "
                "VALUES(?, 'clases', 'Clases tarde', '12:00', '12:49', '18:10', 10, 2)",
                (turno_tarde["id"],),
            )

    # Grados
    cursor.execute("SELECT COUNT(*) AS n FROM grados")
    if cursor.fetchone()["n"] == 0:
        for grado in ["1ro", "2do", "3ro", "4to", "5to"]:
            cursor.execute("INSERT INTO grados(nombre) VALUES(?)", (grado,))

    # Usuario admin
    cursor.execute("SELECT COUNT(*) AS n FROM usuarios")
    if cursor.fetchone()["n"] == 0:
        password_temporal = secrets.token_urlsafe(9)
        cursor.execute(
            "INSERT INTO usuarios(usuario, password, rol, nombres, "
            "debe_cambiar_password, es_principal) "
            "VALUES(?, ?, 'Admin', 'Administrador', 1, 1)",
            ("admin", hash_password(password_temporal)),
        )
        log.warning("Admin creado. Contraseña temporal: %s", password_temporal)

    conexion.commit()


# ─────────────────────────────────────────────────────────────
# AUTENTICACIÓN
# ─────────────────────────────────────────────────────────────

def usuario_esta_bloqueado(usuario):
    """Devuelve True si la cuenta está bloqueada por intentos fallidos."""
    if not usuario.get("bloqueado_hasta"):
        return False
    try:
        bloqueado_hasta = datetime.strptime(
            usuario["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S"
        )
        return ahora().replace(tzinfo=None) < bloqueado_hasta
    except (ValueError, TypeError):
        return False


def autenticar(nombre_usuario, password):
    """
    Verifica las credenciales del usuario.
    Devuelve (usuario, mensaje_error).
    Si el login es exitoso, mensaje_error es "".
    """
    nombre_usuario = (nombre_usuario or "").strip().lower()

    usuario = leer_uno(
        "SELECT * FROM usuarios WHERE LOWER(usuario) = ? AND activo = 1",
        (nombre_usuario,),
    )
    if not usuario:
        return None, "Usuario no encontrado o inactivo."

    if usuario_esta_bloqueado(usuario):
        bloqueado_hasta = datetime.strptime(
            usuario["bloqueado_hasta"], "%Y-%m-%d %H:%M:%S"
        )
        minutos_restantes = int(
            (bloqueado_hasta - ahora().replace(tzinfo=None)).total_seconds() / 60
        ) + 1
        return None, f"Cuenta bloqueada. Intenta en {minutos_restantes} min."

    if not verificar_password(password, usuario["password"]):
        intentos = (usuario.get("intentos_fallidos") or 0) + 1
        if intentos >= MAX_INTENTOS_LOGIN:
            bloqueado_hasta = (ahora() + timedelta(minutes=MINUTOS_BLOQUEO)).strftime(
                "%Y-%m-%d %H:%M:%S"
            )
            escribir_seguro(
                "UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = ? WHERE id = ?",
                (bloqueado_hasta, usuario["id"]),
            )
            return None, f"Cuenta bloqueada por {MINUTOS_BLOQUEO} minutos."
        escribir_seguro(
            "UPDATE usuarios SET intentos_fallidos = ? WHERE id = ?",
            (intentos, usuario["id"]),
        )
        return None, f"Credenciales incorrectas. Quedan {MAX_INTENTOS_LOGIN - intentos} intento(s)."

    # Login exitoso
    escribir_seguro(
        "UPDATE usuarios SET intentos_fallidos = 0, bloqueado_hasta = NULL, "
        "ultimo_login = ?, ultimo_ip = ? WHERE id = ?",
        (timestamp_texto(), obtener_ip(), usuario["id"]),
    )
    usuario["ultimo_login"] = timestamp_texto()
    return usuario, ""


def cerrar_sesion():
    """Cierra la sesión del usuario."""
    usuario = st.session_state.get("user")
    if usuario:
        auditar(usuario["usuario"], "Logout")
    for clave in list(st.session_state.keys()):
        del st.session_state[clave]

# ─────────────────────────────────────────────────────────────
# VISTA: LOGIN
# ─────────────────────────────────────────────────────────────

def vista_login():
    """Pantalla de inicio de sesión. Solo título y formulario."""

    st.title("asisyarina")

    _, centro, _ = st.columns([1, 1, 1])
    with centro:
        with st.form("formulario_login"):
            usuario = st.text_input("Usuario", placeholder="tu usuario")
            password = st.text_input(
                "Contraseña", type="password", placeholder="tu contraseña"
            )
            enviado = st.form_submit_button("Ingresar", type="primary",
                                            use_container_width=True)

            if enviado:
                datos, error = autenticar(usuario, password)
                if datos:
                    st.session_state["user"] = datos
                    auditar(datos["usuario"], "Login")
                    st.rerun()
                else:
                    st.error(error or "Credenciales incorrectas.")


def vista_cambio_password_obligatorio():
    """Cambio obligatorio de contraseña en el primer login."""
    usuario = st.session_state["user"]
    st.title("Cambio obligatorio de contraseña")
    st.warning("Tu cuenta tiene una contraseña temporal.")

    _, centro, _ = st.columns([1, 1.2, 1])
    with centro:
        with st.form("formulario_cambio_password"):
            nueva = st.text_input("Nueva contraseña", type="password")
            confirmar = st.text_input("Confirmar", type="password")
            ok = st.form_submit_button("Cambiar", type="primary",
                                       use_container_width=True)

        if ok:
            if len(nueva) < 6:
                st.error("Mínimo 6 caracteres.")
            elif nueva != confirmar:
                st.error("No coinciden.")
            else:
                nuevo_hash = hash_password(nueva)
                escribir_seguro(
                    "UPDATE usuarios SET password = ?, debe_cambiar_password = 0 "
                    "WHERE id = ?",
                    (nuevo_hash, usuario["id"]),
                )
                st.session_state["user"]["debe_cambiar_password"] = 0
                st.session_state["user"]["password"] = nuevo_hash
                auditar(usuario["usuario"], "Cambio pwd obligatorio")
                st.rerun()


# ─────────────────────────────────────────────────────────────
# MENÚ LATERAL
# ─────────────────────────────────────────────────────────────

def opciones_por_rol(usuario):
    """Devuelve las opciones del menú según el rol del usuario."""
    rol = usuario["rol"]
    if rol == "Admin":
        return [
            "Puerta", "Bloqueados", "Panel Direccion", "Reportes",
            "Alumnos", "Carnets", "Dias especiales", "Ventanas",
            "Justificaciones y Permisos", "Usuarios", "Backup",
            "Auditoria", "Mi cuenta",
        ]
    if rol == "Direccion":
        return [
            "Panel Direccion", "Bloqueados", "Reportes", "Alumnos",
            "Carnets", "Dias especiales", "Justificaciones y Permisos",
            "Mi cuenta",
        ]
    if rol == "Auxiliar":
        return [
            "Puerta", "Bloqueados", "Justificaciones y Permisos",
            "Reportes", "Mi cuenta",
        ]
    return []


def menu_lateral():
    """Dibuja el menú lateral y devuelve la opción elegida."""
    usuario = st.session_state["user"]
    rol = usuario["rol"]
    opciones = opciones_por_rol(usuario)
    inicial = (usuario["nombres"] or "?")[0].upper()

    with st.sidebar:
        with st.container(key="sidebar_header"):
            with st.container(key="avatar"):
                st.markdown(f"**{inicial}**")
            st.markdown(f"**{usuario['nombres']}**")
            st.caption(rol)

        if "menu" not in st.session_state or st.session_state["menu"] not in opciones:
            st.session_state["menu"] = opciones[0]

        opcion = st.radio("Menu", opciones, key="menu",
                          label_visibility="collapsed")

        st.markdown("---")
        if st.button("Cerrar sesión", use_container_width=True):
            cerrar_sesion()
            st.rerun()

    return opcion


# ─────────────────────────────────────────────────────────────
# VISTA: MI CUENTA
# ─────────────────────────────────────────────────────────────

def vista_mi_cuenta():
    st.title("Mi cuenta")
    usuario = st.session_state["user"]
    inicial = (usuario["nombres"] or "?")[0].upper()

    with st.container(key="perfil_hero"):
        c1, c2 = st.columns([1, 6], vertical_alignment="center")
        with c1:
            with st.container(key="hero_avatar"):
                st.markdown(f"**{inicial}**")
        with c2:
            with st.container(key="hero_nombre"):
                st.markdown(usuario["nombres"])
            with st.container(key="hero_meta"):
                st.markdown(
                    f"Usuario: {usuario['usuario']} · Rol: {usuario['rol']}"
                )
                st.markdown(
                    f"Último login: {usuario.get('ultimo_login') or 'Nunca'}"
                )

    if usuario["rol"] == "Auxiliar":
        secciones = leer_dataframe("""
            SELECT g.nombre AS Grado, s.nombre AS Sección, t.nombre AS Turno
            FROM auxiliar_secciones a
            JOIN secciones s ON a.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            JOIN turnos t ON s.turno_id = t.id
            WHERE a.usuario_id = ?
            ORDER BY g.nombre, s.nombre
        """, (usuario["id"],))

        st.subheader("Mis secciones asignadas")
        if secciones.empty:
            st.warning("No tienes secciones asignadas.")
        else:
            st.dataframe(secciones, use_container_width=True, hide_index=True)

    st.subheader("Mi actividad reciente")
    actividad = leer_dataframe(
        "SELECT accion, fecha FROM auditoria WHERE usuario = ? "
        "ORDER BY id DESC LIMIT 10",
        (usuario["usuario"],),
    )
    if actividad.empty:
        st.info("Sin actividad.")
    else:
        st.dataframe(actividad, use_container_width=True, hide_index=True)

    st.subheader("Cambiar contraseña")
    with st.form("formulario_mi_password"):
        actual = st.text_input("Contraseña actual", type="password")
        nueva = st.text_input("Nueva contraseña", type="password")
        confirmar = st.text_input("Confirmar nueva contraseña", type="password")

        if st.form_submit_button("Cambiar contraseña", type="primary"):
            if not verificar_password(actual, usuario["password"]):
                st.error("Contraseña actual incorrecta.")
            elif len(nueva) < 6:
                st.error("Mínimo 6 caracteres.")
            elif nueva != confirmar:
                st.error("No coinciden.")
            else:
                nuevo_hash = hash_password(nueva)
                escribir_seguro(
                    "UPDATE usuarios SET password = ? WHERE id = ?",
                    (nuevo_hash, usuario["id"]),
                )
                st.session_state["user"]["password"] = nuevo_hash
                auditar(usuario["usuario"], "Cambio su contraseña")
                st.success("Contraseña actualizada.")


# ─────────────────────────────────────────────────────────────
# ESCANEO QR Y MENSAJES
# ─────────────────────────────────────────────────────────────

def _buscar_alumno_por_dni(dni):
    """Busca un alumno por DNI con todos sus datos en 1 sola query."""
    return leer_uno("""
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               a.seccion_id, s.nombre AS seccion,
               g.nombre AS grado, t.id AS turno_id, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni = ? AND a.activo = 1
    """, (dni,))


def _obtener_datos_alumno_completos(dni, id_periodo):
    """
    Una sola query que trae todo lo necesario para registrar
    la entrada: alumno, bloqueo, tardanzas, permiso.
    Hacemos esto para que el escaneo sea rápido.
    """
    fecha = hoy_texto()
    return leer_uno("""
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               a.seccion_id, s.nombre AS seccion,
               g.nombre AS grado, t.id AS turno_id, t.nombre AS turno,
               (SELECT id FROM bloqueos WHERE alumno_id = a.id AND activo = 1
                ORDER BY id DESC LIMIT 1) AS bloqueo_id,
               (SELECT motivo FROM bloqueos WHERE alumno_id = a.id AND activo = 1
                ORDER BY id DESC LIMIT 1) AS bloqueo_motivo,
               (SELECT COUNT(*) FROM tardanzas WHERE alumno_id = a.id
                AND justificada = 0 AND fecha > COALESCE(
                    (SELECT MAX(fecha_fin) FROM bloqueos
                     WHERE alumno_id = a.id AND activo = 0), '1900-01-01')) AS tardanzas_ciclo,
               (SELECT COUNT(*) FROM permisos WHERE alumno_id = a.id
                AND activo = 1 AND fecha_inicio <= ? AND fecha_fin >= ?) AS tiene_permiso
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni = ? AND a.activo = 1
    """, (fecha, fecha, dni))


def _obtener_ventana_activa(id_turno, fecha, id_seccion=None):
    """Devuelve la ventana activa para el turno y hora actual."""
    hora = hora_corta()

    # Primero revisar si hay feriado hoy
    feriado = leer_uno(
        "SELECT id FROM dias_especiales WHERE fecha = ? AND activo = 1 "
        "AND tipo = 'feriado' LIMIT 1",
        (fecha,),
    )
    if feriado:
        return None

    # Revisar si hay evento hoy
    evento = leer_uno(
        "SELECT * FROM dias_especiales WHERE fecha = ? AND activo = 1 "
        "AND tipo = 'evento' AND turno_id = ? ORDER BY id DESC LIMIT 1",
        (fecha, id_turno),
    )

    ventanas = leer_varios(
        "SELECT * FROM ventanas WHERE turno_id = ? AND activo = 1 "
        "ORDER BY orden, id",
        (id_turno,),
    )

    for ventana in ventanas:
        apertura = formatear_hora(ventana["hora_apertura"])
        cierre = formatear_hora(ventana["hora_cierre"])
        limite = formatear_hora(ventana["hora_limite_puntual"] or ventana["hora_apertura"])

        es_evento = False
        if ventana["tipo"] == TIPO_VENTANA_CLASES and evento:
            apertura = formatear_hora(evento["hora_entrada"])
            hora_evento, minuto_evento = map(int, apertura.split(":"))
            base = datetime(2000, 1, 1, hora_evento, minuto_evento)
            cierre = (base + timedelta(hours=HORAS_CIERRE_EVENTO)).strftime("%H:%M")
            limite = (base + timedelta(minutes=ventana["tolerancia_min"] or 0)).strftime("%H:%M")
            es_evento = True

        if apertura <= hora <= cierre:
            return {
                **ventana,
                "hora_apertura_efectiva": apertura,
                "hora_limite_efectiva": limite,
                "hora_cierre_efectiva": cierre,
                "es_evento": es_evento,
                "evento_nombre": evento["descripcion"] if es_evento else None,
                "dia_especial_id": evento["id"] if es_evento else None,
            }

    return None


def registrar_entrada(dni, usuario, origen="qr"):
    """
    Registra la entrada de un alumno.
    Devuelve (ok, tipo, mensaje, extra).
    """
    dni = (dni or "").strip()
    if not re.fullmatch(r"\d{8}", dni):
        return False, "ERROR", "DNI inválido", {}

    periodo = leer_uno("SELECT * FROM periodos WHERE activo = 1 LIMIT 1")
    id_periodo = periodo["id"] if periodo else None
    fecha = hoy_texto()
    fecha_d = hoy_fecha()
    hora = hora_corta()

    alumno = _obtener_datos_alumno_completos(dni, id_periodo)
    if not alumno:
        return False, "ERROR", "DNI no encontrado", {}

    nombre = nombre_completo_alumno(alumno)

    # ¿Está bloqueado?
    if alumno.get("bloqueo_id"):
        auditar(usuario["usuario"], f"Intento escaneo bloqueado DNI {dni}",
                tabla="bloqueos", registro_id=alumno["id"])
        return False, "BLOQUEADO", (
            f"{nombre} | BLOQUEADO - retener y llevar a Dirección"
        ), {"alumno": alumno, "motivo": alumno.get("bloqueo_motivo")}

    # ¿Ventana activa?
    ventana = _obtener_ventana_activa(alumno["turno_id"], fecha, alumno["seccion_id"])
    if not ventana:
        return False, "ERROR", f"{nombre} | Sin ventana activa ({hora})", {"alumno": alumno}

    tiene_permiso = (alumno.get("tiene_permiso") or 0) > 0

    # ¿Ya registró hoy?
    ya_registro = leer_uno(
        "SELECT id FROM asistencias WHERE alumno_id = ? AND fecha = ? "
        "AND tipo = ? LIMIT 1",
        (alumno["id"], fecha_d, ventana["tipo"]),
    )
    if ya_registro:
        return False, "ERROR", f"{nombre} ya registró {ventana['tipo']} hoy", {}

    # Reforzamiento
    if ventana["tipo"] == TIPO_VENTANA_REFUERZO:
        escribir_seguro(
            "INSERT INTO asistencias(alumno_id, fecha, ventana_id, tipo, hora, "
            "estado, justificada, origen, periodo_id) "
            "VALUES(?, ?, ?, 'reforzamiento', ?, ?, ?, ?, ?)",
            (alumno["id"], fecha_d, ventana["id"], hora,
             ESTADO_ASISTIO_REFUERZO, 1 if tiene_permiso else 0,
             origen, id_periodo),
        )
        auditar(usuario["usuario"], f"Reforzamiento {origen} DNI {dni}",
                tabla="asistencias")
        return True, "REFORZAMIENTO", (
            f"{nombre} | {alumno['grado']} {alumno['seccion']} | "
            f"Reforzamiento {hora}"
        ), {"alumno": alumno}

    # Clases (puntual o tardanza)
    estado = ESTADO_PUNTUAL if hora <= ventana["hora_limite_efectiva"] else ESTADO_TARDANZA

    escribir_seguro(
        "INSERT INTO asistencias(alumno_id, fecha, ventana_id, tipo, hora, "
        "estado, justificada, origen, periodo_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alumno["id"], fecha_d, ventana["id"], "clases", hora, estado,
         1 if tiene_permiso else 0, origen, id_periodo),
    )

    if estado == ESTADO_PUNTUAL:
        auditar(usuario["usuario"], f"Entrada Puntual {origen} DNI {dni}",
                tabla="asistencias")
        return True, "PUNTUAL", (
            f"{nombre} | {alumno['grado']} {alumno['seccion']} | Puntual {hora}"
        ), {"alumno": alumno}

    # Es tardanza: calcular número y acción
    es_reincidente = False
    ultimo_desbloqueo = leer_uno(
        "SELECT fecha_fin FROM bloqueos WHERE alumno_id = ? AND activo = 0 "
        "AND fecha_fin IS NOT NULL ORDER BY fecha_fin DESC LIMIT 1",
        (alumno["id"],),
    )
    if ultimo_desbloqueo:
        es_reincidente = True

    limite = calcular_limite_tardanzas(es_reincidente)
    numero = (alumno.get("tardanzas_ciclo") or 0) + 1
    accion = ACCION_PERDONADO if numero < limite else ACCION_DERIVADO

    escribir_seguro(
        "INSERT INTO tardanzas(alumno_id, fecha, hora, numero, accion, "
        "justificada, origen, registrado_por, timestamp, periodo_id) "
        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (alumno["id"], fecha_d, hora, numero, accion,
         1 if tiene_permiso else 0, origen, usuario["usuario"],
         timestamp_texto(), id_periodo),
    )

    # ¿Alcanzó el límite? Crear bloqueo automático
    alcanzo_limite = numero >= limite
    if alcanzo_limite:
        escribir_seguro(
            "INSERT INTO bloqueos(alumno_id, motivo, activo, fecha_inicio, "
            "creado_por, origen) VALUES(?, ?, 1, ?, ?, 'automatico')",
            (alumno["id"], f"{numero} tardanza(s) - reincidente={es_reincidente}",
             fecha, usuario["usuario"]),
        )

    auditar(usuario["usuario"], f"Tardanza {numero}a DNI {dni} -> {accion}",
            tabla="tardanzas")

    sufijo = " [JUSTIFICADA]" if tiene_permiso else ""
    mensaje = f"{nombre} | {alumno['grado']} {alumno['seccion']} | Tardanza {numero}a ({accion}){sufijo} {hora}"
    if alcanzo_limite:
        mensaje += " | ⚠️ MAÑANA NO PODRÁ ESCANEAR"
    elif numero == limite - 1:
        mensaje += " | ⚠️ Última oportunidad"

    return True, "TARDANZA", mensaje, {
        "alumno": alumno,
        "numero": numero,
        "accion": accion,
        "alcanzo_limite": alcanzo_limite,
    }


def _procesar_escaneo(dni):
    """Procesa un escaneo y guarda el mensaje en session_state."""
    usuario = st.session_state.get("user")
    if not usuario:
        return

    ok, tipo, mensaje, extra = registrar_entrada(dni, usuario, origen="qr")

    es_duplicado = (not ok and tipo == "ERROR"
                    and ("ya registro" in mensaje or "ya tiene" in mensaje))

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

    # Guardar solo el último mensaje (para ahorrar RAM)
    st.session_state["_qr_mensaje_actual"] = {
        "dni": dni,
        "tipo": tipo,
        "mensaje": mensaje,
        "extra": extra,
        "ts": time.time(),
    }
    st.session_state["_qr_sonido_pendiente"] = {
        "kind": sonido,
        "ts": time.time(),
    }


def _disparar_sonido(kind):
    """
    Dispara el sonido en el iframe padre.
    Es JS puro (no HTML de faz).
    """
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


def _render_mensaje_qr(mensaje):
    """Renderiza un mensaje QR con el color correcto."""
    tipo = mensaje["tipo"]
    texto = mensaje["mensaje"]
    extra = mensaje.get("extra") or {}
    alumno = extra.get("alumno") or {}

    # Determinar el color
    if tipo == "TARDANZA":
        if extra.get("alcanzo_limite") or extra.get("accion") == ACCION_DERIVADO:
            clave = "qr_msg_bloqueado"
        else:
            clave = "qr_msg_tardanza"
    elif tipo == "BLOQUEADO":
        clave = "qr_msg_bloqueado"
    elif tipo == "PUNTUAL":
        clave = "qr_msg_puntual"
    elif tipo == "REFORZAMIENTO":
        clave = "qr_msg_refuerzo"
    else:
        clave = "qr_msg_error"

    with st.container(key=clave):
        if alumno:
            nombre = nombre_completo_alumno(alumno)
            with st.container(key="qr_nombre"):
                st.markdown(f"**{nombre}**")
        with st.container(key="qr_texto"):
            st.markdown(texto)

    # Botones de desbloqueo si está bloqueado
    if tipo == "BLOQUEADO" and alumno.get("id"):
        _render_botones_desbloqueo(alumno)


def _render_botones_desbloqueo(alumno):
    """Muestra botones para desbloquear un alumno desde el escaneo."""
    id_alumno = alumno.get("id")
    dni = alumno.get("dni")
    nombre = nombre_completo_alumno(alumno)

    c1, c2 = st.columns(2)
    with c1:
        if st.button("Sí, desbloquear ahora",
                     key=f"desbloquear_si_{id_alumno}",
                     type="primary", use_container_width=True):
            escribir_seguro(
                "UPDATE bloqueos SET activo = 0, fecha_fin = ?, liberado_por = ? "
                "WHERE alumno_id = ? AND activo = 1",
                (timestamp_texto(), st.session_state["user"]["usuario"],
                 id_alumno),
            )
            auditar(st.session_state["user"]["usuario"],
                    f"Desbloqueo inmediato alumno_id={id_alumno}")
            st.session_state["_qr_mensaje_actual"] = None
            st.rerun()
    with c2:
        if st.button("No, dejarlo bloqueado",
                     key=f"desbloquear_no_{id_alumno}",
                     use_container_width=True):
            st.session_state["_qr_mensaje_actual"] = None
            st.rerun()


def escaner_qr_continuo(key="qr_scanner"):
    """Renderiza el escáner QR y procesa los escaneos."""
    # Componente
    resultado = qr_scanner(key=f"qr_{key}", on_scan=lambda: None)

    if resultado is not None and getattr(resultado, "qr_dni", None):
        dni = str(resultado.qr_dni).strip()
        ultimo = st.session_state.get("_ultimo_qr_escaneado", {})
        if not (ultimo.get("dni") == dni
                and (time.time() - ultimo.get("ts", 0)) < 5.0):
            st.session_state["_ultimo_qr_escaneado"] = {
                "dni": dni, "ts": time.time()
            }
            _procesar_escaneo(dni)

            # Disparar sonido
            sonido = st.session_state.get("_qr_sonido_pendiente") or {}
            if sonido.get("kind"):
                _disparar_sonido(sonido["kind"])
                st.session_state.pop("_qr_sonido_pendiente", None)

            st.rerun()

    # Mostrar mensaje actual
    mensaje = st.session_state.get("_qr_mensaje_actual")
    if mensaje and (time.time() - mensaje.get("ts", 0)) < 10:
        _render_mensaje_qr(mensaje)

    # Botón para reiniciar cámara
    if st.button("🔄 Reiniciar cámara", key="reiniciar_camara"):
        st.session_state.pop("_ultimo_qr_escaneado", None)
        st.rerun()


# ─────────────────────────────────────────────────────────────
# VISTA: PUERTA
# ─────────────────────────────────────────────────────────────

def vista_puerta():
    """Vista minimalista: solo título y cámara."""
    st.title("Escaneo QR")
    escaner_qr_continuo(key="puerta")


# ─────────────────────────────────────────────────────────────
# VISTA: PERFIL DEL ALUMNO
# ─────────────────────────────────────────────────────────────

def perfil_alumno_datos(id_alumno):
    """Carga todos los datos del perfil del alumno."""
    alumno = leer_uno("""
        SELECT a.*, g.nombre AS grado, s.nombre AS seccion,
               s.id AS seccion_id, t.id AS turno_id, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.id = ?
    """, (id_alumno,))
    if not alumno:
        return {}

    asistencias = leer_dataframe(
        "SELECT id, fecha, hora, tipo, estado, justificada, observacion, "
        "origen, justificado_por, justificado_en "
        "FROM asistencias WHERE alumno_id = ? "
        "ORDER BY fecha DESC, hora DESC",
        (id_alumno,),
    )

    tardanzas = leer_dataframe(
        "SELECT fecha, hora, numero AS N, accion, justificada, origen "
        "FROM tardanzas WHERE alumno_id = ? "
        "ORDER BY fecha DESC, hora DESC",
        (id_alumno,),
    )

    bloqueos = leer_dataframe(
        "SELECT fecha_inicio, COALESCE(fecha_fin, '-') AS fecha_fin, "
        "COALESCE(motivo, '') AS motivo, COALESCE(activo, 0) AS activo "
        "FROM bloqueos WHERE alumno_id = ? "
        "ORDER BY fecha_inicio DESC",
        (id_alumno,),
    )

    permisos = leer_dataframe(
        "SELECT id, fecha_inicio, fecha_fin, COALESCE(motivo, '') AS motivo, "
        "COALESCE(activo, 0) AS activo "
        "FROM permisos WHERE alumno_id = ? "
        "ORDER BY fecha_inicio DESC",
        (id_alumno,),
    )

    justificaciones = leer_dataframe(
        "SELECT id, fecha_objetivo, COALESCE(motivo, '') AS motivo, "
        "COALESCE(aplicada, 0) AS aplicada "
        "FROM justificaciones_previas WHERE alumno_id = ? "
        "ORDER BY fecha_objetivo DESC",
        (id_alumno,),
    )

    total_puntuales = int((asistencias["estado"] == ESTADO_PUNTUAL).sum()) if not asistencias.empty else 0
    total_tardanzas = int((asistencias["estado"] == ESTADO_TARDANZA).sum()) if not asistencias.empty else 0
    total_faltas = int((asistencias["estado"] == ESTADO_FALTA).sum()) if not asistencias.empty else 0
    total_refuerzos = 0
    if not asistencias.empty:
        total_refuerzos = int(
            ((asistencias["tipo"] == "reforzamiento")
             & (asistencias["estado"] == ESTADO_ASISTIO_REFUERZO)).sum()
        )

    # Contar tardanzas injustificadas
    tardanzas_injust = int((tardanzas["justificada"] == 0).sum()) if not tardanzas.empty else 0

    # ¿Está bloqueado?
    bloqueado = leer_uno(
        "SELECT id FROM bloqueos WHERE alumno_id = ? AND activo = 1 LIMIT 1",
        (id_alumno,),
    ) is not None

    # ¿Es reincidente?
    ultimo_desbloqueo = leer_uno(
        "SELECT fecha_fin FROM bloqueos WHERE alumno_id = ? AND activo = 0 "
        "AND fecha_fin IS NOT NULL ORDER BY fecha_fin DESC LIMIT 1",
        (id_alumno,),
    )
    es_reincidente = ultimo_desbloqueo is not None
    limite = calcular_limite_tardanzas(es_reincidente)

    # Tardanzas desde el último desbloqueo
    if ultimo_desbloqueo:
        desde_fecha = formatear_fecha(ultimo_desbloqueo["fecha_fin"])
        tardanzas_ciclo = len(
            tardanzas[(tardanzas["justificada"] == 0)
                      & (tardanzas["fecha"] > desde_fecha)]
        ) if not tardanzas.empty else 0
    else:
        tardanzas_ciclo = tardanzas_injust

    return {
        "alumno": alumno,
        "asistencias": asistencias,
        "tardanzas": tardanzas,
        "bloqueos": bloqueos,
        "permisos": permisos,
        "justificaciones_previas": justificaciones,
        "total_puntuales": total_puntuales,
        "total_tardanzas": total_tardanzas,
        "total_faltas": total_faltas,
        "total_refuerzos": total_refuerzos,
        "tardanzas_injust": tardanzas_injust,
        "tardanzas_ciclo": tardanzas_ciclo,
        "reincidente": es_reincidente,
        "limite": limite,
        "bloqueado": bloqueado,
    }


def vista_perfil_alumno(id_alumno):
    """Vista del perfil del alumno con carnet completo."""
    datos = perfil_alumno_datos(id_alumno)
    if not datos:
        st.warning("Alumno no encontrado.")
        st.session_state.pop("perfil_alumno_id", None)
        return

    alumno = datos["alumno"]
    usuario = st.session_state["user"]

    if st.button("← Volver a la lista"):
        st.session_state.pop("perfil_alumno_id", None)
        st.rerun()

    nombre = nombre_completo_alumno(alumno)
    inicial = (alumno["apellido_paterno"] or alumno["nombres"] or "?")[0].upper()
    estado = "BLOQUEADO" if datos["bloqueado"] else "ACTIVO"

    # Header
    with st.container(key="perfil_hero"):
        c1, c2 = st.columns([1, 6], vertical_alignment="center")
        with c1:
            with st.container(key="hero_avatar"):
                st.markdown(f"**{inicial}**")
        with c2:
            with st.container(key="hero_nombre"):
                st.markdown(nombre)
            with st.container(key="hero_meta"):
                st.markdown(
                    f"{alumno['grado']} {alumno['seccion']} · "
                    f"Turno {alumno['turno']} · DNI {alumno['dni']} · {estado}"
                )

    # Carnet completo (reemplaza el QR triste)
    st.subheader("Carnet del alumno")
    try:
        imagen_carnet = _generar_fotocheck_pil(alumno, Path("escudo.png"))
        with st.container(key="carnet_alumno"):
            st.image(imagen_carnet, use_container_width=True)
    except Exception as error:
        st.warning(f"No se pudo generar el carnet: {error}")

    # Botones de acción
    st.subheader("Acciones")
    c1, c2, c3, c4 = st.columns(4)
    with c1:
        pdf_carnet = pdf_carnet_alumno(alumno["dni"])
        if pdf_carnet:
            st.download_button(
                "Descargar carnet", pdf_carnet,
                f"carnet_{alumno['dni']}.pdf",
                "application/pdf",
                use_container_width=True,
            )
    with c2:
        st.download_button(
            "Historial Excel",
            _df_a_excel(datos["asistencias"], "Historial"),
            f"historial_{alumno['dni']}.xlsx",
            use_container_width=True,
        )
    with c3:
        pdf_resumen = pdf_resumen_alumno(id_alumno)
        if pdf_resumen:
            st.download_button(
                "Resumen PDF", pdf_resumen,
                f"resumen_{alumno['dni']}.pdf",
                "application/pdf",
                use_container_width=True,
            )
    with c4:
        if usuario["rol"] == "Admin":
            if alumno.get("activo", 1) == 1:
                if st.button("Desactivar", use_container_width=True,
                             key="desactivar_alumno"):
                    escribir_seguro(
                        "UPDATE alumnos SET activo = 0, retirado_en = ? WHERE id = ?",
                        (timestamp_texto(), id_alumno),
                    )
                    auditar(usuario["usuario"], f"Desactivó alumno DNI {alumno['dni']}")
                    st.success("Alumno desactivado.")
                    st.rerun()
            else:
                if st.button("Reactivar", use_container_width=True,
                             key="reactivar_alumno"):
                    escribir_seguro(
                        "UPDATE alumnos SET activo = 1, retirado_en = NULL WHERE id = ?",
                        (id_alumno,),
                    )
                    auditar(usuario["usuario"], f"Reactivó alumno DNI {alumno['dni']}")
                    st.success("Alumno reactivado.")
                    st.rerun()

    # Tabs
    tabs = st.tabs(["Asistencias", "Tardanzas", "Bloqueos", "Permisos",
                    "Justificaciones previas"])

    with tabs[0]:
        if datos["asistencias"].empty:
            st.info("Sin asistencias.")
        else:
            st.dataframe(datos["asistencias"], use_container_width=True,
                         hide_index=True, height=400)

    with tabs[1]:
        if datos["tardanzas"].empty:
            st.info("Sin tardanzas.")
        else:
            st.dataframe(datos["tardanzas"], use_container_width=True,
                         hide_index=True, height=400)

    with tabs[2]:
        if datos["bloqueos"].empty:
            st.info("Sin bloqueos.")
        else:
            st.dataframe(datos["bloqueos"], use_container_width=True,
                         hide_index=True, height=400)

    with tabs[3]:
        if datos["permisos"].empty:
            st.info("Sin permisos registrados.")
        else:
            st.dataframe(datos["permisos"], use_container_width=True,
                         hide_index=True, height=400)

    with tabs[4]:
        if datos["justificaciones_previas"].empty:
            st.info("Sin justificaciones previas.")
        else:
            st.dataframe(datos["justificaciones_previas"],
                         use_container_width=True, hide_index=True, height=400)


# ─────────────────────────────────────────────────────────────
# VISTA: ALUMNOS
# ─────────────────────────────────────────────────────────────

def vista_alumnos():
    st.title("Alumnos")

    id_perfil = st.session_state.get("perfil_alumno_id")
    if id_perfil:
        vista_perfil_alumno(id_perfil)
        return

    tabs = st.tabs(["Listar", "Crear", "Editar"])
    with tabs[0]:
        _alumnos_listar()
    with tabs[1]:
        _alumnos_crear()
    with tabs[2]:
        _alumnos_editar()


def _alumnos_listar():
    c1, c2, c3 = st.columns([2, 2, 3])
    with c1:
        grados = leer_dataframe(
            "SELECT DISTINCT nombre FROM grados ORDER BY nombre"
        )
        opciones_grado = ["Todos"] + grados["nombre"].tolist()
        grado_sel = st.selectbox("Grado", opciones_grado, key="fil_grado")
    with c2:
        if grado_sel != "Todos":
            secciones = leer_dataframe("""
                SELECT DISTINCT s.nombre FROM secciones s
                JOIN grados g ON s.grado_id = g.id
                WHERE g.nombre = ? ORDER BY s.nombre
            """, (grado_sel,))
            opciones_seccion = ["Todas"] + secciones["nombre"].tolist()
        else:
            opciones_seccion = ["Todas"]
        seccion_sel = st.selectbox("Sección", opciones_seccion, key="fil_seccion")
    with c3:
        texto = st.text_input("Buscar", placeholder="Nombre o DNI",
                              key="fil_texto")

    consulta = """
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.activo = 1
    """
    parametros = []

    if texto.strip():
        consulta += " AND (a.nombres LIKE ? OR a.apellido_paterno LIKE ? " \
                    "OR a.apellido_materno LIKE ? OR a.dni LIKE ?)"
        patron = f"%{texto.strip()}%"
        parametros.extend([patron, patron, patron, patron])
    if grado_sel != "Todos":
        consulta += " AND g.nombre = ?"
        parametros.append(grado_sel)
    if seccion_sel != "Todas":
        consulta += " AND s.nombre = ?"
        parametros.append(seccion_sel)

    consulta += " ORDER BY a.apellido_paterno LIMIT 200"

    alumnos = leer_dataframe(consulta, parametros)
    st.write(f"**{len(alumnos)} alumnos**")

    if alumnos.empty:
        st.info("Sin resultados.")
        return

    for _, alumno in alumnos.iterrows():
        nombre = nombre_completo_alumno(alumno)
        c1, c2 = st.columns([5, 1])
        with c1:
            st.markdown(
                f"**{nombre}** — {alumno['grado']} {alumno['seccion']} "
                f"({alumno['turno']}) — DNI {alumno['dni']}"
            )
        with c2:
            if st.button("Ver", key=f"ver_alumno_{alumno['id']}"):
                st.session_state["perfil_alumno_id"] = alumno["id"]
                st.rerun()


def _alumnos_crear():
    st.subheader("Crear alumno")
    grados = leer_dataframe("SELECT * FROM grados ORDER BY nombre")

    with st.form("formulario_crear_alumno"):
        c1, c2 = st.columns(2)
        with c1:
            dni = st.text_input("DNI *", max_chars=8)
            nombres = st.text_input("Nombres *")
            apellido_paterno = st.text_input("Apellido Paterno *")
        with c2:
            apellido_materno = st.text_input("Apellido Materno")
            grado_sel = st.selectbox(
                "Grado *", grados.to_dict("records"),
                format_func=lambda g: g["nombre"],
            )
            secciones = leer_dataframe(
                "SELECT * FROM secciones WHERE grado_id = ? ORDER BY nombre",
                (grado_sel["id"],),
            ) if grado_sel else leer_dataframe("SELECT * FROM secciones")
            seccion_sel = st.selectbox(
                "Sección *", secciones.to_dict("records"),
                format_func=lambda s: s["nombre"],
            ) if not secciones.empty else None

        apoderado = st.text_input("Apoderado")
        telefono = st.text_input("Teléfono apoderado")

        if st.form_submit_button("Crear alumno", type="primary"):
            if not dni or not nombres or not apellido_paterno or not seccion_sel:
                st.error("Completa DNI, nombres, apellido y sección.")
            elif not re.fullmatch(r"\d{8}", dni.strip()):
                st.error("DNI inválido (8 dígitos).")
            else:
                periodo = leer_uno("SELECT id FROM periodos WHERE activo = 1 LIMIT 1")
                id_periodo = periodo["id"] if periodo else None
                try:
                    escribir_y_esperar(
                        "INSERT INTO alumnos(dni, nombres, apellido_paterno, "
                        "apellido_materno, seccion_id, nombre_apoderado, "
                        "telefono_apoderado, periodo_id, activo) "
                        "VALUES(?, ?, ?, ?, ?, ?, ?, ?, 1)",
                        (dni.strip(), nombres.strip(),
                         apellido_paterno.strip(),
                         apellido_materno.strip() or None,
                         seccion_sel["id"],
                         apoderado.strip() or None,
                         telefono.strip() or None,
                         id_periodo),
                    )
                    auditar(st.session_state["user"]["usuario"],
                            f"Creó alumno DNI {dni}")
                    st.success(f"Alumno {nombres} creado.")
                    st.rerun()
                except Exception as error:
                    if "unique" in str(error).lower():
                        st.error(f"Ya existe un alumno con DNI {dni}.")
                    else:
                        st.error(f"Error: {error}")


def _alumnos_editar():
    st.subheader("Editar alumno")
    alumnos = leer_dataframe("""
        SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
               a.nombre_apoderado, a.telefono_apoderado,
               g.nombre AS grado, s.nombre AS seccion
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        WHERE a.activo = 1
        ORDER BY a.apellido_paterno LIMIT 200
    """)

    if alumnos.empty:
        st.info("No hay alumnos para editar.")
        return

    opciones = {
        f"{nombre_completo_alumno(row)} — {row['grado']} {row['seccion']}": row["id"]
        for _, row in alumnos.iterrows()
    }
    seleccion = st.selectbox("Alumno", list(opciones.keys()))
    id_alumno = opciones[seleccion]

    alumno = leer_uno("""
        SELECT a.*, g.id AS grado_id, g.nombre AS grado,
               s.id AS seccion_id, s.nombre AS seccion
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        WHERE a.id = ?
    """, (id_alumno,))

    if not alumno:
        st.error("Alumno no encontrado.")
        return

    st.info(f"DNI: {alumno['dni']} — Actualmente en {alumno['grado']} {alumno['seccion']}")

    with st.form("formulario_editar_alumno"):
        c1, c2 = st.columns(2)
        with c1:
            apoderado = st.text_input("Apoderado",
                                       value=alumno["nombre_apoderado"] or "")
            telefono = st.text_input("Teléfono",
                                      value=alumno["telefono_apoderado"] or "")
        with c2:
            grados = leer_dataframe("SELECT * FROM grados ORDER BY nombre")
            grado_sel = st.selectbox(
                "Grado", grados.to_dict("records"),
                format_func=lambda g: g["nombre"],
                index=next((i for i, g in enumerate(grados.to_dict("records"))
                           if g["id"] == alumno["grado_id"]), 0),
            )
            secciones = leer_dataframe(
                "SELECT * FROM secciones WHERE grado_id = ? ORDER BY nombre",
                (grado_sel["id"],),
            )
            seccion_sel = st.selectbox(
                "Sección", secciones.to_dict("records"),
                format_func=lambda s: s["nombre"],
                index=next((i for i, s in enumerate(secciones.to_dict("records"))
                           if s["id"] == alumno["seccion_id"]), 0),
            )

        if st.form_submit_button("Guardar cambios", type="primary"):
            escribir_seguro(
                "UPDATE alumnos SET seccion_id = ?, nombre_apoderado = ?, "
                "telefono_apoderado = ? WHERE id = ?",
                (seccion_sel["id"], apoderado.strip() or None,
                 telefono.strip() or None, id_alumno),
            )
            auditar(st.session_state["user"]["usuario"],
                    f"Editó alumno id={id_alumno}")
            st.success("Cambios guardados.")
            st.rerun()


# ─────────────────────────────────────────────────────────────
# VISTA: BLOQUEADOS
# ─────────────────────────────────────────────────────────────

def vista_bloqueados():
    st.title("Alumnos bloqueados")
    st.caption(
        "Alumno nuevo → 3 tardanzas → bloqueo. "
        "Alumno reincidente → 1 tardanza → bloqueo."
    )

    bloqueados = leer_dataframe("""
        SELECT b.id, a.id AS alumno_id, a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno, '') || ', ' || a.nombres AS alumno,
               g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               b.motivo, b.fecha_inicio
        FROM bloqueos b
        JOIN alumnos a ON b.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE b.activo = 1
        ORDER BY b.fecha_inicio DESC
    """)

    st.metric("Total bloqueados", len(bloqueados))

    if bloqueados.empty:
        st.success("No hay alumnos bloqueados.")
        return

    st.dataframe(bloqueados, use_container_width=True, hide_index=True)

    st.subheader("Desbloquear alumno")
    opciones = {
        f"{row['alumno']} — {row['grado']} {row['seccion']}": row["alumno_id"]
        for _, row in bloqueados.iterrows()
    }
    seleccion = st.selectbox("Alumno", list(opciones.keys()),
                              key="desbloquear_alumno_sel")
    id_alumno = opciones[seleccion]

    motivo = st.text_input("Motivo de desbloqueo *",
                            key="motivo_desbloqueo")

    confirmar = st.checkbox(
        "Confirmo que deseo desbloquear a este alumno",
        key="confirmar_desbloqueo",
    )

    if st.button("Desbloquear", type="primary",
                 disabled=not confirmar):
        if not motivo.strip():
            st.error("El motivo es obligatorio.")
        else:
            escribir_seguro(
                "UPDATE bloqueos SET activo = 0, fecha_fin = ?, "
                "liberado_por = ? WHERE alumno_id = ? AND activo = 1",
                (timestamp_texto(), st.session_state["user"]["usuario"],
                 id_alumno),
            )
            auditar(st.session_state["user"]["usuario"],
                    f"Desbloqueó alumno_id={id_alumno}. Motivo: {motivo.strip()}")
            st.success("Alumno desbloqueado.")
            st.rerun()


# ─────────────────────────────────────────────────────────────
# VISTA: PANEL DIRECCION
# ─────────────────────────────────────────────────────────────

def vista_panel_direccion():
    st.title("Panel Dirección")

    # Botón actualizar (reemplaza st_autorefresh)
    if st.button("🔄 Actualizar"):
        st.rerun()

    turnos = leer_varios("SELECT * FROM turnos ORDER BY id")
    if not turnos:
        st.info("No hay turnos configurados.")
        return

    opciones = {t["nombre"]: t["id"] for t in turnos}
    turno_sel = st.radio("Turno", list(opciones.keys()),
                          horizontal=True, key="panel_turno")
    id_turno = opciones[turno_sel]

    fecha = hoy_texto()
    fecha_d = hoy_fecha()

    # Una sola query con todas las métricas
    metricas = leer_uno("""
        SELECT
            COUNT(DISTINCT a.id) AS total,
            SUM(CASE WHEN ast.estado = 'Puntual' THEN 1 ELSE 0 END) AS puntuales,
            SUM(CASE WHEN ast.estado = 'Tardanza' THEN 1 ELSE 0 END) AS tardanzas,
            SUM(CASE WHEN ast.estado = 'Falta' THEN 1 ELSE 0 END) AS faltas,
            SUM(CASE WHEN ast.estado = 'Permiso' THEN 1 ELSE 0 END) AS permisos,
            SUM(CASE WHEN ast.tipo = 'reforzamiento' AND ast.estado = 'Asistio' THEN 1 ELSE 0 END) AS refuerzos,
            SUM(CASE WHEN ast.justificada = 1 THEN 1 ELSE 0 END) AS justificadas
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        LEFT JOIN asistencias ast ON ast.alumno_id = a.id AND ast.fecha = ?
        WHERE s.turno_id = ? AND a.activo = 1
    """, (fecha_d, id_turno))

    bloqueados = leer_uno("""
        SELECT COUNT(DISTINCT b.alumno_id) AS n
        FROM bloqueos b
        JOIN alumnos a ON b.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        WHERE b.activo = 1 AND s.turno_id = ?
    """, (id_turno,))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total alumnos", metricas["total"] or 0)
    c2.metric("Puntuales", metricas["puntuales"] or 0)
    c3.metric("Tardanzas", metricas["tardanzas"] or 0)
    c4.metric("Faltas", metricas["faltas"] or 0)

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Refuerzos", metricas["refuerzos"] or 0)
    c2.metric("Bloqueados", bloqueados["n"] or 0)
    c3.metric("Justificadas", metricas["justificadas"] or 0)
    c4.metric("Permisos", metricas["permisos"] or 0)

    st.subheader("Últimos escaneos")
    ultimos = leer_dataframe("""
        SELECT a.dni, a.apellido_paterno, a.nombres,
               g.nombre AS grado, s.nombre AS seccion,
               ast.tipo, ast.hora, ast.estado
        FROM asistencias ast
        JOIN alumnos a ON ast.alumno_id = a.id
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        WHERE ast.fecha = ? AND s.turno_id = ?
        ORDER BY ast.hora DESC LIMIT 30
    """, (fecha_d, id_turno))

    if ultimos.empty:
        st.info("Sin escaneos hoy.")
    else:
        st.dataframe(ultimos, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────
# VISTA: CARNETS
# ─────────────────────────────────────────────────────────────

def vista_carnets():
    st.title("Carnets QR")
    st.caption("Descarga por sección o por selección.")

    if st.session_state.get("carnet_ver_seccion"):
        _carnets_ver_seccion(st.session_state["carnet_ver_seccion"])
        return

    grados = leer_dataframe("SELECT * FROM grados ORDER BY nombre")
    for _, grado in grados.iterrows():
        secciones = leer_dataframe(
            "SELECT * FROM secciones WHERE grado_id = ? ORDER BY nombre",
            (grado["id"],),
        )
        if st.button(f"{grado['nombre']} ({len(secciones)} secciones)",
                     use_container_width=True,
                     key=f"carnet_grado_{grado['id']}"):
            st.session_state["carnet_grado_sel"] = grado["id"]
            st.rerun()

    grado_sel = st.session_state.get("carnet_grado_sel")
    if grado_sel:
        st.subheader("Secciones")
        secciones = leer_dataframe(
            "SELECT * FROM secciones WHERE grado_id = ? ORDER BY nombre",
            (grado_sel,),
        )
        for _, seccion in secciones.iterrows():
            alumnos = leer_dataframe(
                "SELECT COUNT(*) AS n FROM alumnos WHERE seccion_id = ? AND activo = 1",
                (seccion["id"],),
            )
            n = alumnos.iloc[0]["n"] if not alumnos.empty else 0
            if st.button(f"{seccion['nombre']} ({n} alumnos)",
                         use_container_width=True,
                         key=f"carnet_seccion_{seccion['id']}"):
                st.session_state["carnet_ver_seccion"] = seccion["id"]
                st.rerun()


def _carnets_ver_seccion(id_seccion):
    seccion = leer_uno("""
        SELECT s.id, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM secciones s
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE s.id = ?
    """, (id_seccion,))

    if not seccion:
        st.warning("Sección no encontrada.")
        st.session_state.pop("carnet_ver_seccion", None)
        return

    if st.button("← Regresar"):
        st.session_state.pop("carnet_ver_seccion", None)
        st.rerun()

    st.subheader(f"{seccion['grado']} {seccion['seccion']} — Turno {seccion['turno']}")

    alumnos = leer_dataframe("""
        SELECT * FROM alumnos WHERE seccion_id = ? AND activo = 1
        ORDER BY apellido_paterno
    """, (id_seccion,))

    if alumnos.empty:
        st.info("Sin alumnos.")
        return

    if st.button("Descargar todo el salón", type="primary",
                 use_container_width=True):
        pdf = _pdf_carnets_por_seccion(id_seccion)
        if pdf:
            st.download_button(
                "Guardar PDF", pdf,
                f"carnets_{seccion['grado']}{seccion['seccion']}.pdf",
                "application/pdf",
            )


# ─────────────────────────────────────────────────────────────
# VISTA: REPORTES
# ─────────────────────────────────────────────────────────────

def vista_reportes():
    st.title("Reportes y Consultas")
    st.info("Vista de reportes en construcción. Pronto estará disponible.")


# ─────────────────────────────────────────────────────────────
# VISTA: USUARIOS
# ─────────────────────────────────────────────────────────────

def vista_usuarios():
    st.title("Usuarios")
    tab_listar, tab_crear = st.tabs(["Listar", "Crear"])

    with tab_listar:
        usuarios = leer_dataframe(
            "SELECT id, usuario, rol, nombres, activo, ultimo_login "
            "FROM usuarios ORDER BY nombres"
        )
        if usuarios.empty:
            st.info("Sin usuarios.")
        else:
            st.dataframe(usuarios, use_container_width=True, hide_index=True)

    with tab_crear:
        st.subheader("Crear usuario")
        with st.form("formulario_crear_usuario"):
            c1, c2 = st.columns(2)
            with c1:
                nuevo_usuario = st.text_input("Usuario *")
                nueva_password = st.text_input("Contraseña *", type="password")
            with c2:
                nombres = st.text_input("Nombres *")
                rol = st.selectbox("Rol", ["Admin", "Direccion", "Auxiliar"])

            if st.form_submit_button("Crear", type="primary"):
                if not nuevo_usuario or not nueva_password or not nombres:
                    st.error("Completa todos los campos.")
                elif len(nueva_password) < 6:
                    st.error("Mínimo 6 caracteres.")
                else:
                    try:
                        escribir_y_esperar(
                            "INSERT INTO usuarios(usuario, password, rol, nombres) "
                            "VALUES(?, ?, ?, ?)",
                            (nuevo_usuario.strip().lower(),
                             hash_password(nueva_password),
                             rol, nombres.strip()),
                        )
                        auditar(st.session_state["user"]["usuario"],
                                f"Creó usuario {nuevo_usuario}")
                        st.success("Usuario creado.")
                        st.rerun()
                    except Exception as error:
                        if "unique" in str(error).lower():
                            st.error("Ese usuario ya existe.")
                        else:
                            st.error(f"Error: {error}")


# ─────────────────────────────────────────────────────────────
# VISTA: AUDITORIA
# ─────────────────────────────────────────────────────────────

def vista_auditoria():
    st.title("Auditoría")
    registros = leer_dataframe(
        "SELECT id, usuario, accion, fecha, ip FROM auditoria "
        "ORDER BY id DESC LIMIT 500"
    )
    st.write(f"**{len(registros)} registros**")
    if not registros.empty:
        st.dataframe(registros, use_container_width=True, hide_index=True)


# ─────────────────────────────────────────────────────────────
# VISTA: BACKUP
# ─────────────────────────────────────────────────────────────

def vista_backup():
    st.title("Backup de base de datos")
    try:
        from sync import boton_backup
        boton_backup()
    except ImportError:
        st.warning("Módulo de sincronización no disponible.")


# ─────────────────────────────────────────────────────────────
# VISTA: DIAS ESPECIALES (básica)
# ─────────────────────────────────────────────────────────────

def vista_dias_especiales():
    st.title("Días especiales")
    st.info("Vista en construcción. Pronto estará disponible.")


# ─────────────────────────────────────────────────────────────
# VISTA: VENTANAS (básica)
# ─────────────────────────────────────────────────────────────

def vista_ventanas():
    st.title("Ventanas de asistencia")
    st.info("Vista en construcción. Pronto estará disponible.")


# ─────────────────────────────────────────────────────────────
# VISTA: JUSTIFICACIONES Y PERMISOS (básica)
# ─────────────────────────────────────────────────────────────

def vista_justificaciones_permisos():
    st.title("Justificaciones y Permisos")
    st.info("Vista en construcción. Pronto estará disponible.")


# ─────────────────────────────────────────────────────────────
# FUNCIONES AUXILIARES DE PDF Y CARNETS
# ─────────────────────────────────────────────────────────────

def _df_a_excel(df, nombre_hoja="Datos"):
    """Convierte un DataFrame a bytes de Excel."""
    buffer = BytesIO()
    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name=nombre_hoja)
    buffer.seek(0)
    return buffer.getvalue()


def _generar_fotocheck_pil(alumno, ruta_escudo=None):
    """Genera el carnet del alumno como imagen PIL."""
    from PIL import Image, ImageDraw, ImageFont

    ANCHO = 980
    ALTO = 626
    NARANJA_OSCURO = (184, 92, 56)
    NARANJA_CLARO = (212, 132, 96)
    FONDO = (255, 240, 225)
    BLANCO = (255, 255, 255)
    NEGRO = (17, 17, 17)
    GRIS = (150, 150, 150)

    imagen = Image.new("RGB", (ANCHO, ALTO), FONDO)
    dibujo = ImageDraw.Draw(imagen)

    # Patrón de puntos
    for y in range(0, ALTO, 22):
        for x in range(0, ANCHO, 22):
            dibujo.ellipse([x-1, y-1, x+1, y+1], fill=(235, 210, 180))

    # Franja degradada izquierda (foto)
    FOTO_ANCHO = 240
    FOTO_ALTO = 310
    for y in range(ALTO):
        t = y / ALTO
        r = int(NARANJA_OSCURO[0] + (NARANJA_CLARO[0] - NARANJA_OSCURO[0]) * t)
        g = int(NARANJA_OSCURO[1] + (NARANJA_CLARO[1] - NARANJA_OSCURO[1]) * t)
        b = int(NARANJA_OSCURO[2] + (NARANJA_CLARO[2] - NARANJA_OSCURO[2]) * t)
        dibujo.line([(0, y), (FOTO_ANCHO, y)], fill=(r, g, b))

    # Fuente
    try:
        fuente_titulo = ImageFont.truetype("arial.ttf", 34)
        fuente_label = ImageFont.truetype("arial.ttf", 30)
        fuente_valor = ImageFont.truetype("arial.ttf", 42)
        fuente_frase = ImageFont.truetype("arial.ttf", 28)
    except Exception:
        fuente_titulo = ImageFont.load_default()
        fuente_label = ImageFont.load_default()
        fuente_valor = ImageFont.load_default()
        fuente_frase = ImageFont.load_default()

    # Título
    dibujo.text((300, 15), "FOTOCHECK DEL ESTUDIANTE", fill=NEGRO,
                font=fuente_titulo)

    # Datos
    x_info = FOTO_ANCHO + 20
    apellidos = (alumno.get("apellido_paterno", "") + " " +
                 (alumno.get("apellido_materno") or "")).upper()
    nombres = (alumno.get("nombres", "")).upper()

    dibujo.text((x_info, 110), apellidos, fill=NEGRO, font=fuente_valor)
    dibujo.text((x_info, 180), nombres, fill=NEGRO, font=fuente_valor)

    dibujo.text((x_info, 260), "DNI:", fill=(90, 60, 30), font=fuente_label)
    dibujo.text((x_info + 100, 260), alumno.get("dni", ""), fill=NEGRO,
                font=fuente_valor)

    dibujo.text((x_info, 330), "GRADO:", fill=(90, 60, 30), font=fuente_label)
    dibujo.text((x_info + 150, 330),
                f"{alumno.get('grado', '')} \"{alumno.get('seccion', '')}\"",
                fill=NEGRO, font=fuente_valor)

    dibujo.text((x_info, 400), "TURNO:", fill=(90, 60, 30), font=fuente_label)
    dibujo.text((x_info + 150, 400), alumno.get("turno", ""), fill=NEGRO,
                font=fuente_valor)

    # QR
    qr_imagen = generar_qr(alumno.get("dni", ""))
    qr_imagen = qr_imagen.resize((225, 225), Image.LANCZOS)
    imagen.paste(qr_imagen, (ANCHO - 240, 70))

    # Frase
    dibujo.text((x_info, ALTO - 60), '"Ser del CNY, es ser mejor"',
                fill=NARANJA_OSCURO, font=fuente_frase)

    return imagen


def _render_carnets(filas, titulo=None):
    """Renderiza un PDF de carnets en formato 2x5 por hoja A4."""
    buffer = BytesIO()
    margen = 5
    documento = SimpleDocTemplate(
        buffer, pagesize=A4,
        rightMargin=margen, leftMargin=margen,
        topMargin=margen, bottomMargin=margen,
    )
    estilos = getSampleStyleSheet()
    MM = 2.8346
    ANCHO_CARNET = 85.0 * MM
    ALTO_CARNET = 53.0 * MM
    ruta_escudo = Path("escudo.png")

    elementos = []
    if titulo:
        elementos.append(Paragraph(f"<b>{titulo}</b>", estilos["Heading2"]))
        elementos.append(Spacer(1, 6))

    # Generar por lotes de 10 para no saturar memoria
    for i in range(0, len(filas), 10):
        lote = filas[i:i+10]
        fotochecks = []
        for alumno in lote:
            imagen = _generar_fotocheck_pil(
                alumno,
                ruta_escudo if ruta_escudo.exists() else None,
            )
            buffer_img = BytesIO()
            imagen.save(buffer_img, format="PNG")
            buffer_img.seek(0)
            fotochecks.append(RLImage(buffer_img,
                                      width=ANCHO_CARNET,
                                      height=ALTO_CARNET))
            del imagen

        # Armar tabla 2 columnas
        filas_tabla = []
        for j in range(0, len(fotochecks), 2):
            fila = fotochecks[j:j+2]
            while len(fila) < 2:
                fila.append("")
            filas_tabla.append(fila)

        tabla = Table(filas_tabla,
                      colWidths=[ANCHO_CARNET, ANCHO_CARNET],
                      hAlign="CENTER")
        tabla.setStyle(TableStyle([
            ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
            ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ]))
        elementos.append(tabla)
        if i + 10 < len(filas):
            elementos.append(PageBreak())

    documento.build(elementos)
    buffer.seek(0)
    return buffer.getvalue()


def _pdf_carnets_por_seccion(id_seccion):
    filas = leer_varios("""
        SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.seccion_id = ? AND a.activo = 1
        ORDER BY a.apellido_paterno
    """, (id_seccion,))
    if not filas:
        return None
    return _render_carnets(filas)


def pdf_carnet_alumno(dni):
    """Genera el PDF del carnet de un alumno."""
    filas = leer_varios("""
        SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        WHERE a.dni = ? AND a.activo = 1
    """, (dni,))
    if not filas:
        return None
    return _render_carnets(filas)


def pdf_resumen_alumno(id_alumno):
    """Genera un PDF con el resumen del alumno."""
    datos = perfil_alumno_datos(id_alumno)
    if not datos:
        return None
    alumno = datos["alumno"]

    buffer = BytesIO()
    documento = SimpleDocTemplate(buffer, pagesize=A4,
                                    rightMargin=30, leftMargin=30,
                                    topMargin=30, bottomMargin=30)
    estilos = getSampleStyleSheet()
    elementos = [
        Paragraph("<b>Reporte de Asistencia</b>", estilos["Heading1"]),
        Paragraph("I.E. Yarinacocha", estilos["Normal"]),
        Spacer(1, 20),
        Paragraph(f"<b>Alumno:</b> {nombre_completo_alumno(alumno)}",
                  estilos["Normal"]),
        Paragraph(f"<b>DNI:</b> {alumno['dni']}", estilos["Normal"]),
        Paragraph(
            f"<b>Grado:</b> {alumno['grado']} {alumno['seccion']} — "
            f"{alumno['turno']}",
            estilos["Normal"],
        ),
        Spacer(1, 15),
        Paragraph("<b>Resumen</b>", estilos["Heading2"]),
    ]

    resumen = [
        ["Puntuales", "Tardanzas", "Faltas", "Refuerzos"],
        [datos["total_puntuales"], datos["total_tardanzas"],
         datos["total_faltas"], datos["total_refuerzos"]],
    ]
    tabla = Table(resumen)
    tabla.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor(COLOR_PRIMARIO)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
    ]))
    elementos.append(tabla)

    documento.build(elementos)
    buffer.seek(0)
    return buffer.getvalue()

# MAIN

def main():
    st.set_page_config(
        page_title="Asistencia I.E. Yarinacocha",
        page_icon="escudo.png",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    try:
        from db import descargar_bd_si_no_existe
        descargar_bd_si_no_existe()
    except Exception as error:
        log.warning("No se pudo verificar BD en R2: %s", error)

    inicializar_base_datos()
    aplicar_estilos()

    if not st.session_state.get("user"):
        vista_login()
        return

    if st.session_state["user"].get("debe_cambiar_password"):
        vista_cambio_password_obligatorio()
        return

    opcion = menu_lateral()

    rutas = {
        "Puerta": vista_puerta,
        "Bloqueados": vista_bloqueados,
        "Panel Direccion": vista_panel_direccion,
        "Reportes": vista_reportes,
        "Alumnos": vista_alumnos,
        "Carnets": vista_carnets,
        "Dias especiales": vista_dias_especiales,
        "Ventanas": vista_ventanas,
        "Usuarios": vista_usuarios,
        "Backup": vista_backup,
        "Auditoria": vista_auditoria,
        "Mi cuenta": vista_mi_cuenta,
        "Justificaciones y Permisos": vista_justificaciones_permisos,
    }

    funcion_vista = rutas.get(opcion)
    if funcion_vista:
        funcion_vista()

if __name__ == "__main__":
    main()
