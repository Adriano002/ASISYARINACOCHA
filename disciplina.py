# disciplina.py
"""
Módulo de Disciplina Escolar - D.S. 021-2026-MINEDU
Sistema SUGIERE medidas (no castiga solo). El director decide.
Sanciones activas bloquean el escaneo QR de asistencia.
"""
import logging
from datetime import datetime, timedelta, timezone

import pandas as pd
import streamlit as st

from db import cursor, escribir, leer_df, fmt_date, to_date

log = logging.getLogger("asistencia.disciplina")


# ═══════════════════════════════════════════════════════════════════
# CONSTANTES
# ═══════════════════════════════════════════════════════════════════
FALTA_LEVE      = "leve"
FALTA_GRAVE     = "grave"
FALTA_MUY_GRAVE = "muy_grave"

LEVES_PARA_GRAVE      = 3
GRAVES_PARA_MUY_GRAVE = 3
OPORTUNIDADES_POR_CICLO = 3

MEDIDA_SUSPENSION = "suspension_temporal"
MEDIDA_MATRICULA  = "matricula_condicional"
MEDIDA_SEPARACION = "separacion_definitiva"


# ═══════════════════════════════════════════════════════════════════
# CATÁLOGO BASE (32 faltas del D.S.)
# ═══════════════════════════════════════════════════════════════════
CATALOGO_LEVES = [
    {"inciso": "6.7 / 6.10", "nombre": "Mala presentación personal",
     "descripcion": (
         "Asistir sin adecuada higiene y aseo personal, o con presentación "
         "no acorde con las condiciones de seguridad y disciplina escolar: "
         "uniforme o vestimenta inadecuada, cabello desordenado, uso de "
         "maquillaje, aretes, piercings, anillos, cadenas, pulseras, "
         "tatuajes, faldas por encima de la rodilla, cabello pintado."),
     "base_legal": "Numerales 6.7 y 6.10"},
    {"inciso": "7.2.a", "nombre": "Incumplimiento de normas del RI",
     "descripcion": (
         "Incumplir las normas de convivencia o disciplina del Reglamento "
         "Interno, sin comprometer la integridad física, psicológica, "
         "emocional o moral de los miembros de la comunidad educativa."),
     "base_legal": "Numeral 7.2.a"},
    {"inciso": "7.3.1.d (leve)", "nombre": "Tardanza aislada",
     "descripcion": (
         "Tardanza no justificada que NO supere 3 en el bimestre."),
     "base_legal": "Numeral 7.3.1.d"},
]

CATALOGO_GRAVES = [
    {"inciso": "7.3.1.a", "nombre": "Daño a la propiedad escolar",
     "descripcion": "Deteriorar, ensuciar o usar indebidamente mobiliario, "
                    "materiales o instalaciones sin vandalismo.",
     "base_legal": "Numeral 7.3.1.a"},
    {"inciso": "7.3.1.b", "nombre": "Reincidencia en faltas leves",
     "descripcion": "3 o más faltas leves en el año escolar, notificadas.",
     "base_legal": "Numeral 7.3.1.b"},
    {"inciso": "7.3.1.c", "nombre": "Uso indebido de celulares",
     "descripcion": "Uso de dispositivos sin autorización o sin acreditación.",
     "base_legal": "Numeral 7.3.1.c"},
    {"inciso": "7.3.1.d", "nombre": "Tardanza reiterada",
     "descripcion": "Más de 3 tardanzas no justificadas en un bimestre.",
     "base_legal": "Numeral 7.3.1.d"},
    {"inciso": "7.3.1.e", "nombre": "Agresión física leve o psicológica",
     "descripcion": "Empujones, forcejeos, agresión verbal/psicológica sin "
                    "lesión ni atención médica.",
     "base_legal": "Numeral 7.3.1.e"},
    {"inciso": "7.3.1.f", "nombre": "Posesión de sustancias prohibidas",
     "descripcion": "Drogas, alcohol, tabaco, vapeadores, medicamentos sin "
                    "receta (sin fines de comercialización).",
     "base_legal": "Numeral 7.3.1.f"},
    {"inciso": "7.3.1.g", "nombre": "Justificativos adulterados",
     "descripcion": "Notas o justificativos con firma/contenido falso.",
     "base_legal": "Numeral 7.3.1.g"},
    {"inciso": "7.3.1.h", "nombre": "Vulneración de intimidad / dignidad",
     "descripcion": "Acciones verbales/físicas agresivas o divulgación de "
                    "información personal/reservada.",
     "base_legal": "Numeral 7.3.1.h"},
    {"inciso": "7.3.1.i", "nombre": "Incitación a conductas peligrosas",
     "descripcion": "Retos, apuestas, conductas viralizadas que ponen en "
                    "riesgo la integridad.",
     "base_legal": "Numeral 7.3.1.i"},
    {"inciso": "7.3.1.j", "nombre": "Atentar contra la imagen institucional",
     "descripcion": "Actos que afectan la imagen de la IIEE o sus miembros.",
     "base_legal": "Numeral 7.3.1.j"},
]

CATALOGO_MUY_GRAVES = [
    {"inciso": "7.3.2.a", "nombre": "Actos de odio o apología a la violencia",
     "descripcion": "Símbolos, discursos racistas, xenófobos, homófobos o "
                    "apología a la violencia.",
     "base_legal": "Numeral 7.3.2.a"},
    {"inciso": "7.3.2.b", "nombre": "Actos de discriminación",
     "descripcion": "Trato desigual por discapacidad, origen, lengua, "
                    "cultura, religión, nivel socioeconómico.",
     "base_legal": "Numeral 7.3.2.b"},
    {"inciso": "7.3.2.c", "nombre": "Actos de connotación sexual / acoso",
     "descripcion": "Conductas sexuales, hostigamiento, tocamientos, "
                    "vulneración de la libertad sexual.",
     "base_legal": "Numeral 7.3.2.c"},
    {"inciso": "7.3.2.d", "nombre": "Acoso escolar con afectación",
     "descripcion": "Bullying / ciberbullying reiterado que menoscaba la "
                    "integridad o induce a autolesión/suicidio.",
     "base_legal": "Numeral 7.3.2.d"},
    {"inciso": "7.3.2.e", "nombre": "Alteración violenta del orden",
     "descripcion": "Boicot, toma de instalaciones, interrupción masiva.",
     "base_legal": "Numeral 7.3.2.e"},
    {"inciso": "7.3.2.f", "nombre": "Daño intencional / sabotaje",
     "descripcion": "Destruir, inutilizar o vandalizar infraestructura, "
                    "bienes, ciberseguridad o movilidad escolar.",
     "base_legal": "Numeral 7.3.2.f"},
    {"inciso": "7.3.2.g", "nombre": "Extorsión / coacción / cobro de cupos",
     "descripcion": "Exigir, condicionar o sustraer bajo amenaza; cobro de "
                    "protección, ritos de iniciación, bautizos.",
     "base_legal": "Numeral 7.3.2.g"},
    {"inciso": "7.3.2.h", "nombre": "Falsa denuncia / difamación",
     "descripcion": "Imputar falsamente delitos/faltas o difundir información "
                    "que afecta la dignidad.",
     "base_legal": "Numeral 7.3.2.h"},
    {"inciso": "7.3.2.i", "nombre": "Falsificación de documentos",
     "descripcion": "Adulterar documentos oficiales, certificados, constancias.",
     "base_legal": "Numeral 7.3.2.i"},
    {"inciso": "7.3.2.j", "nombre": "Uso no autorizado de datos privados",
     "descripcion": "Difusión de datos personales sensibles sin autorización.",
     "base_legal": "Numeral 7.3.2.j"},
    {"inciso": "7.3.2.k", "nombre": "Fraude físico o tecnológico",
     "descripcion": "Sustraer, hackear o suplantar para alterar notas o "
                    "evaluaciones.",
     "base_legal": "Numeral 7.3.2.k"},
    {"inciso": "7.3.2.l", "nombre": "Organización para delitos o apuestas",
     "descripcion": "Grupos/redes para apuestas, pandillaje o captación.",
     "base_legal": "Numeral 7.3.2.l"},
    {"inciso": "7.3.2.m", "nombre": "Comercialización de sustancias prohibidas",
     "descripcion": "Introducir, vender o facilitar drogas, alcohol, tabaco, "
                    "vapeadores o medicamentos.",
     "base_legal": "Numeral 7.3.2.m"},
    {"inciso": "7.3.2.n", "nombre": "Portar armas u objetos punzocortantes",
     "descripcion": "Armas, réplicas, navajas, objetos contundentes.",
     "base_legal": "Numeral 7.3.2.n"},
    {"inciso": "7.3.2.o", "nombre": "Suplantación de identidad digital",
     "descripcion": "Perfiles falsos usurpando identidad en redes o aulas "
                    "virtuales.",
     "base_legal": "Numeral 7.3.2.o"},
    {"inciso": "7.3.2.p", "nombre": "Artefactos incendiarios / explosivos",
     "descripcion": "Bombas molotov, fuegos artificiales, petardos.",
     "base_legal": "Numeral 7.3.2.p"},
    {"inciso": "7.3.2.q", "nombre": "Vulneración de intimidad mediante TIC",
     "descripcion": "Grabar, fotografiar o difundir imágenes/audios sin "
                    "consentimiento.",
     "base_legal": "Numeral 7.3.2.q"},
    {"inciso": "7.3.2.r", "nombre": "Hurto o robo de bienes patrimoniales",
     "descripcion": "Sustraer bienes de la IIEE o de miembros de la comunidad.",
     "base_legal": "Numeral 7.3.2.r"},
]

PROHIBICIONES = [
    {"inciso": "10.1", "titulo": "Castigo físico o humillante",
     "descripcion": "Trato ofensivo, jalones de oreja/pelo, correazos, "
                    "palmadas, bofetadas, burlas."},
    {"inciso": "10.2", "titulo": "Medidas discriminatorias",
     "descripcion": "Medidas más gravosas por discapacidad, origen, lengua, "
                    "religión, condición socioeconómica."},
    {"inciso": "10.3", "titulo": "Privación de necesidades básicas",
     "descripcion": "Privar de alimentos, agua, servicios higiénicos, "
                    "descanso, atención de salud."},
    {"inciso": "10.4", "titulo": "Privación del ingreso a la IIEE",
     "descripcion": "Impedir el ingreso por tardanzas, inasistencias o "
                    "presentación personal."},
]


# ═══════════════════════════════════════════════════════════════════
# SCHEMA
# ═══════════════════════════════════════════════════════════════════
TABLAS_DISCIPLINA = [
    """
    CREATE TABLE IF NOT EXISTS faltas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        fecha TEXT NOT NULL,
        tipo TEXT NOT NULL CHECK(tipo IN ('leve','grave','muy_grave')),
        inciso TEXT,
        nombre_falta TEXT,
        descripcion TEXT,
        registrada_por TEXT,
        timestamp TEXT NOT NULL,
        periodo_id INTEGER,
        notificada_familia INTEGER DEFAULT 0,
        fecha_notificacion TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS medidas_sancionadoras (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        falta_id INTEGER REFERENCES faltas(id),
        tipo TEXT NOT NULL CHECK(tipo IN (
            'suspension_temporal','matricula_condicional','separacion_definitiva')),
        fecha_inicio TEXT NOT NULL,
        fecha_fin TEXT,
        dias INTEGER DEFAULT 0,
        motivo TEXT,
        aplicada_por TEXT,
        timestamp TEXT NOT NULL,
        activa INTEGER DEFAULT 1,
        periodo_id INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS acciones_reparadoras (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        falta_id INTEGER REFERENCES faltas(id),
        descripcion TEXT,
        fecha TEXT NOT NULL,
        registrada_por TEXT,
        timestamp TEXT NOT NULL,
        completada INTEGER DEFAULT 0,
        periodo_id INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS ciclos_disciplina (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        numero_ciclo INTEGER NOT NULL,
        leves_acumuladas INTEGER DEFAULT 0,
        graves_acumuladas INTEGER DEFAULT 0,
        iniciado_en TEXT NOT NULL,
        cerrado_en TEXT,
        estado TEXT DEFAULT 'abierto',
        periodo_id INTEGER,
        UNIQUE(alumno_id, numero_ciclo, periodo_id)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS desbloqueos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        ciclo_id INTEGER REFERENCES ciclos_disciplina(id),
        motivo TEXT,
        liberado_por TEXT,
        timestamp TEXT NOT NULL,
        periodo_id INTEGER
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS catalogo_faltas_custom (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        tipo TEXT NOT NULL CHECK(tipo IN ('leve','grave','muy_grave')),
        inciso TEXT,
        nombre TEXT NOT NULL,
        descripcion TEXT,
        base_legal TEXT,
        creado_por TEXT,
        timestamp TEXT NOT NULL,
        activo INTEGER DEFAULT 1
    )
    """,
]

INDICES_DISCIPLINA = [
    "CREATE INDEX IF NOT EXISTS idx_faltas_alumno ON faltas(alumno_id, fecha)",
    "CREATE INDEX IF NOT EXISTS idx_faltas_tipo ON faltas(tipo, periodo_id)",
    "CREATE INDEX IF NOT EXISTS idx_medidas_alumno ON medidas_sancionadoras(alumno_id, activa)",
    "CREATE INDEX IF NOT EXISTS idx_ciclos_alumno ON ciclos_disciplina(alumno_id, estado)",
    "CREATE INDEX IF NOT EXISTS idx_desbloqueos_alumno ON desbloqueos(alumno_id)",
    "CREATE INDEX IF NOT EXISTS idx_catalogo_custom_tipo ON catalogo_faltas_custom(tipo, activo)",
]


def aplicar_schema_disciplina():
    with cursor() as (con, cur):
        for sql in TABLAS_DISCIPLINA:
            cur.execute(sql)
        for sql in INDICES_DISCIPLINA:
            cur.execute(sql)
    log.info("Schema disciplina aplicado")


# ═══════════════════════════════════════════════════════════════════
# HELPERS
# ═══════════════════════════════════════════════════════════════════
def _ahora():
    return datetime.now(timezone.utc) - timedelta(hours=5)


def _hoy_str():
    return _ahora().strftime("%Y-%m-%d")


def _timestamp_str():
    return _ahora().strftime("%Y-%m-%d %H:%M:%S")


# ═══════════════════════════════════════════════════════════════════
# CATÁLOGO UNIFICADO
# ═══════════════════════════════════════════════════════════════════
def obtener_catalogo_custom():
    try:
        df = leer_df("""
            SELECT id, tipo, inciso, nombre, descripcion, base_legal
            FROM catalogo_faltas_custom WHERE activo=1
        """)
        if df.empty:
            return []
        return df.to_dict("records")
    except Exception:
        return []


def obtener_catalogo(tipo):
    base = {
        FALTA_LEVE:      CATALOGO_LEVES,
        FALTA_GRAVE:     CATALOGO_GRAVES,
        FALTA_MUY_GRAVE: CATALOGO_MUY_GRAVES,
    }.get(tipo, [])
    custom = [f for f in obtener_catalogo_custom() if f.get("tipo") == tipo]
    return list(base) + list(custom)


def crear_falta_custom(tipo, inciso, nombre, descripcion, base_legal, usuario):
    if tipo not in (FALTA_LEVE, FALTA_GRAVE, FALTA_MUY_GRAVE):
        return False, "Tipo inválido."
    if not nombre or not nombre.strip():
        return False, "El nombre es obligatorio."
    with cursor() as (con, cur):
        cur.execute("""
            INSERT INTO catalogo_faltas_custom
                (tipo, inciso, nombre, descripcion, base_legal,
                 creado_por, timestamp, activo)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1)
        """, (
            tipo, (inciso or "").strip(), nombre.strip(),
            (descripcion or "").strip(), (base_legal or "").strip(),
            usuario["usuario"], _timestamp_str(),
        ))
    return True, f"Falta '{nombre}' agregada al catálogo {tipo}."


def eliminar_falta_custom(custom_id):
    with cursor() as (con, cur):
        cur.execute("UPDATE catalogo_faltas_custom SET activo=0 WHERE id=?",
                    (custom_id,))
    return True, "Falta eliminada del catálogo."


# ═══════════════════════════════════════════════════════════════════
# CICLOS
# ═══════════════════════════════════════════════════════════════════
def obtener_ciclo_abierto(alumno_id, periodo_id):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT * FROM ciclos_disciplina
            WHERE alumno_id=? AND estado='abierto' AND periodo_id=?
            ORDER BY numero_ciclo DESC LIMIT 1
        """, (alumno_id, periodo_id))
        f = cur.fetchone()
        return dict(f) if f else None


def crear_ciclo(alumno_id, periodo_id):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT COALESCE(MAX(numero_ciclo),0)+1 AS n
            FROM ciclos_disciplina WHERE alumno_id=? AND periodo_id=?
        """, (alumno_id, periodo_id))
        n = cur.fetchone()["n"]
        cur.execute("""
            INSERT INTO ciclos_disciplina
                (alumno_id, numero_ciclo, iniciado_en, estado, periodo_id)
            VALUES (?, ?, ?, 'abierto', ?)
        """, (alumno_id, n, _timestamp_str(), periodo_id))
    return n


def cerrar_ciclo(ciclo_id, estado):
    with cursor() as (con, cur):
        cur.execute("""
            UPDATE ciclos_disciplina
            SET estado=?, cerrado_en=? WHERE id=?
        """, (estado, _timestamp_str(), ciclo_id))


# ═══════════════════════════════════════════════════════════════════
# CONTADORES
# ═══════════════════════════════════════════════════════════════════
def contar_desbloqueos(alumno_id, periodo_id=None):
    with cursor() as (con, cur):
        if periodo_id:
            cur.execute("""
                SELECT COUNT(*) AS n FROM desbloqueos
                WHERE alumno_id=? AND periodo_id=?
            """, (alumno_id, periodo_id))
        else:
            cur.execute("SELECT COUNT(*) AS n FROM desbloqueos WHERE alumno_id=?",
                        (alumno_id,))
        return cur.fetchone()["n"] or 0


def contar_faltas_por_tipo(alumno_id, periodo_id=None):
    with cursor() as (con, cur):
        if periodo_id:
            cur.execute("""
                SELECT tipo, COUNT(*) AS n FROM faltas
                WHERE alumno_id=? AND periodo_id=? GROUP BY tipo
            """, (alumno_id, periodo_id))
        else:
            cur.execute("SELECT tipo, COUNT(*) AS n FROM faltas WHERE alumno_id=? GROUP BY tipo",
                        (alumno_id,))
        r = {row["tipo"]: row["n"] for row in cur.fetchall()}
    return {
        FALTA_LEVE: r.get(FALTA_LEVE, 0),
        FALTA_GRAVE: r.get(FALTA_GRAVE, 0),
        FALTA_MUY_GRAVE: r.get(FALTA_MUY_GRAVE, 0),
    }


# ═══════════════════════════════════════════════════════════════════
# 🆕 SANCIONES ACTIVAS (para bloquear escaneo QR)
# ═══════════════════════════════════════════════════════════════════
def obtener_sancion_activa(alumno_id):
    """
    Devuelve la sanción activa hoy (suspensión o separación) o None.
    Se usa en registrar_entrada() para bloquear el escaneo.
    """
    hoy = _hoy_str()
    with cursor() as (con, cur):
        cur.execute("""
            SELECT * FROM medidas_sancionadoras
            WHERE alumno_id=?
              AND activa=1
              AND tipo IN ('suspension_temporal','separacion_definitiva')
              AND fecha_inicio <= ?
              AND (fecha_fin IS NULL OR fecha_fin >= ?)
            ORDER BY id DESC LIMIT 1
        """, (alumno_id, hoy, hoy))
        f = cur.fetchone()
        return dict(f) if f else None


# ═══════════════════════════════════════════════════════════════════
# REGISTRAR FALTA + SUGERENCIAS
# ═══════════════════════════════════════════════════════════════════
def registrar_falta(alumno_id, tipo, inciso, nombre_falta, descripcion,
                    usuario, periodo_id=None, notificar_familia=False):
    """Registra la falta. NO aplica castigos. Devuelve sugerencias."""
    with cursor() as (con, cur):
        cur.execute("""
            INSERT INTO faltas
                (alumno_id, fecha, tipo, inciso, nombre_falta, descripcion,
                 registrada_por, timestamp, periodo_id, notificada_familia,
                 fecha_notificacion)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            alumno_id, _hoy_str(), tipo, inciso, nombre_falta, descripcion,
            usuario["usuario"], _timestamp_str(), periodo_id,
            1 if notificar_familia else 0,
            _timestamp_str() if notificar_familia else None,
        ))
        falta_id = cur.lastrowid

    ciclo = obtener_ciclo_abierto(alumno_id, periodo_id)
    if not ciclo:
        crear_ciclo(alumno_id, periodo_id)
        ciclo = obtener_ciclo_abierto(alumno_id, periodo_id)

    escalado = None
    if tipo == FALTA_LEVE:
        with cursor() as (con, cur):
            cur.execute("UPDATE ciclos_disciplina SET leves_acumuladas = leves_acumuladas + 1 WHERE id=?",
                        (ciclo["id"],))
            cur.execute("SELECT leves_acumuladas FROM ciclos_disciplina WHERE id=?",
                        (ciclo["id"],))
            leves = cur.fetchone()["leves_acumuladas"]
        if leves >= LEVES_PARA_GRAVE:
            escalado = FALTA_GRAVE
            with cursor() as (con, cur):
                cur.execute("""
                    INSERT INTO faltas
                        (alumno_id, fecha, tipo, inciso, nombre_falta,
                         descripcion, registrada_por, timestamp, periodo_id)
                    VALUES (?, ?, 'grave', '7.3.1.b',
                            'Reincidencia en faltas leves', ?, ?, ?, ?)
                """, (alumno_id, _hoy_str(),
                      f"Escalamiento: {leves} leves acumuladas.",
                      usuario["usuario"], _timestamp_str(), periodo_id))
            cerrar_ciclo(ciclo["id"], "cerrado_medida")

    elif tipo == FALTA_GRAVE:
        with cursor() as (con, cur):
            cur.execute("UPDATE ciclos_disciplina SET graves_acumuladas = graves_acumuladas + 1 WHERE id=?",
                        (ciclo["id"],))
            cur.execute("SELECT graves_acumuladas FROM ciclos_disciplina WHERE id=?",
                        (ciclo["id"],))
            graves = cur.fetchone()["graves_acumuladas"]
        if graves >= GRAVES_PARA_MUY_GRAVE:
            escalado = FALTA_MUY_GRAVE
            with cursor() as (con, cur):
                cur.execute("""
                    INSERT INTO faltas
                        (alumno_id, fecha, tipo, inciso, nombre_falta,
                         descripcion, registrada_por, timestamp, periodo_id)
                    VALUES (?, ?, 'muy_grave', '5.13',
                            'Reincidencia en faltas graves', ?, ?, ?, ?)
                """, (alumno_id, _hoy_str(),
                      f"Escalamiento: {graves} graves acumuladas.",
                      usuario["usuario"], _timestamp_str(), periodo_id))
            cerrar_ciclo(ciclo["id"], "cerrado_medida")

    # Devolver sugerencias al director
    sugerencias = generar_sugerencias(
        alumno_id, tipo, escalado, periodo_id)

    return {
        "ok": True,
        "falta_id": falta_id,
        "escalado": escalado,
        "sugerencias": sugerencias,
    }


# ═══════════════════════════════════════════════════════════════════
# 🆕 MOTOR DE SUGERENCIAS (el director decide)
# ═══════════════════════════════════════════════════════════════════
def generar_sugerencias(alumno_id, tipo, escalado, periodo_id):
    """
    Devuelve un dict con las medidas que la norma permite sugerir
    según el tipo de falta + historial del alumno.
    """
    sugerencias = {
        "tipo": tipo,
        "escalado": escalado,
        "permitidas": [],   # medidas que la norma permite
        "prioritarias": [], # las más recomendadas
        "no_recomendadas": [],  # aún no aplican
        "alertas": [],      # cosas críticas
    }

    hist = contar_faltas_por_tipo(alumno_id, periodo_id)
    medidas_prev = medidas_de_alumno(alumno_id)
    susp_count = 0
    if not medidas_prev.empty:
        susp_count = int((medidas_prev["tipo"] == "suspension_temporal").sum())

    # ── FALTA LEVE ─────────────────────────────────────────────
    if tipo == FALTA_LEVE and not escalado:
        sugerencias["permitidas"] = [
            ("Amonestación verbal", "8.3.1.a", "correctiva"),
            ("Disposición de reparación del daño", "8.3.1.b", "correctiva"),
            ("Restricción temporal de recreación", "8.3.1.c", "correctiva"),
            ("Separación temporal del aula", "8.3.1.d", "correctiva"),
            ("Comunicación a la familia", "9.1.a", "formativa"),
        ]
        sugerencias["prioritarias"] = [
            ("Amonestación verbal", "8.3.1.a"),
            ("Comunicación a la familia", "9.1.a"),
        ]

    # ── FALTA GRAVE (o escalada por reincidencia) ──────────────
    elif tipo == FALTA_GRAVE or escalado == FALTA_GRAVE:
        sugerencias["permitidas"] = [
            ("Amonestación verbal", "8.3.1.a", "correctiva"),
            ("Reparación del daño", "8.3.1.b", "correctiva"),
            ("Restricción temporal de recreación", "8.3.1.c", "correctiva"),
            ("Separación temporal del aula", "8.3.1.d", "correctiva"),
            ("Suspensión temporal 1-3 días", "8.3.2.a", "sancionadora"),
            ("Acción reparadora", "5.1", "formativa"),
            ("Reunión formal con la familia", "9.1.b", "formativa"),
        ]
        sugerencias["prioritarias"] = [
            ("Suspensión temporal 1-3 días", "8.3.2.a"),
            ("Reunión formal con la familia", "9.1.b"),
            ("Acción reparadora", "5.1"),
        ]
        sugerencias["no_recomendadas"] = [
            "Matrícula condicional: solo aplica con 2+ suspensiones, "
            "nota desaprobada, o familia no colabora (8.3.2.b).",
            "Separación definitiva: solo para faltas muy graves o 3 "
            "suspensiones en el año (8.3.2.c).",
        ]

    # ── FALTA MUY GRAVE (o escalada) ──────────────────────────
    elif tipo == FALTA_MUY_GRAVE or escalado == FALTA_MUY_GRAVE:
        sugerencias["permitidas"] = [
            ("Suspensión temporal 1-3 días", "8.3.2.a", "sancionadora"),
            ("Matrícula condicional", "8.3.2.b", "sancionadora"),
            ("Separación definitiva (evaluar)", "8.3.2.c", "sancionadora"),
            ("Acción reparadora", "5.1", "formativa"),
            ("Reunión formal con la familia", "9.1.b", "formativa"),
        ]
        sugerencias["prioritarias"] = [
            ("Suspensión temporal 1-3 días", "8.3.2.a"),
            ("Matrícula condicional", "8.3.2.b"),
        ]
        sugerencias["alertas"].append(
            "⚠️ Comunicar a Fiscalía y SARA si la falta constituye delito (7.5).")
        sugerencias["alertas"].append(
            "⚠️ Si se aplica separación definitiva, comunicar a la UGEL (8.3.2.c).")

    # ── Reglas adicionales ─────────────────────────────────────
    if susp_count >= 2:
        sugerencias["prioritarias"].append(
            ("Matrícula condicional", "8.3.2.b")
        )
        sugerencias["alertas"].append(
            f"⚠️ El alumno ya tiene {susp_count} suspensiones este año. "
            "Matrícula condicional es altamente recomendada.")

    if susp_count >= 3:
        sugerencias["prioritarias"].append(
            ("Separación definitiva (evaluar)", "8.3.2.c")
        )
        sugerencias["alertas"].append(
            f"🚨 {susp_count} suspensiones. La norma permite evaluar "
            "separación definitiva (8.3.2.c).")

    return sugerencias


# ═══════════════════════════════════════════════════════════════════
# ACCIÓN REPARADORA Y PERDÓN
# ═══════════════════════════════════════════════════════════════════
def registrar_accion_reparadora(alumno_id, falta_id, descripcion, usuario,
                                periodo_id=None):
    with cursor() as (con, cur):
        cur.execute("""
            INSERT INTO acciones_reparadoras
                (alumno_id, falta_id, descripcion, fecha,
                 registrada_por, timestamp, periodo_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, (alumno_id, falta_id, descripcion, _hoy_str(),
              usuario["usuario"], _timestamp_str(), periodo_id))
    return True, "Acción reparadora registrada."


def perdonar_y_reabrir(alumno_id, usuario, periodo_id=None, motivo=""):
    ciclo = obtener_ciclo_abierto(alumno_id, periodo_id)
    if ciclo:
        with cursor() as (con, cur):
            cur.execute("""
                INSERT INTO desbloqueos
                    (alumno_id, ciclo_id, motivo, liberado_por,
                     timestamp, periodo_id)
                VALUES (?, ?, ?, ?, ?, ?)
            """, (alumno_id, ciclo["id"], motivo or "Perdón disciplinario",
                  usuario["usuario"], _timestamp_str(), periodo_id))
            cur.execute("""
                UPDATE ciclos_disciplina
                SET estado='cerrado_perdon', cerrado_en=? WHERE id=?
            """, (_timestamp_str(), ciclo["id"]))

    nuevo = crear_ciclo(alumno_id, periodo_id)

    try:
        from app import liberar_bloqueo
        liberar_bloqueo(alumno_id, usuario, motivo or "Perdón disciplinario")
    except Exception as e:
        log.warning(f"No se pudo liberar bloqueo de asistencia: {e}")

    total = contar_desbloqueos(alumno_id, periodo_id)
    return True, (f"Perdón aplicado (desbloqueo #{total}). "
                  f"Nuevo ciclo #{nuevo} con {OPORTUNIDADES_POR_CICLO} oportunidades.")


# ═══════════════════════════════════════════════════════════════════
# MEDIDAS SANCIONADORAS
# ═══════════════════════════════════════════════════════════════════
def aplicar_suspension(alumno_id, dias, motivo, usuario, periodo_id=None):
    if dias < 1 or dias > 3:
        return False, "La suspensión debe ser entre 1 y 3 días (8.3.2.a)."
    hoy = _ahora().date()
    fin = hoy + timedelta(days=dias)
    with cursor() as (con, cur):
        cur.execute("""
            INSERT INTO medidas_sancionadoras
                (alumno_id, tipo, fecha_inicio, fecha_fin, dias, motivo,
                 aplicada_por, timestamp, activa, periodo_id)
            VALUES (?, 'suspension_temporal', ?, ?, ?, ?, ?, ?, 1, ?)
        """, (alumno_id, hoy.isoformat(), fin.isoformat(), dias,
              motivo, usuario["usuario"], _timestamp_str(), periodo_id))
    return True, f"Suspensión de {dias} día(s) aplicada. El alumno NO podrá escanear su QR durante ese periodo."


def aplicar_matricula_condicional(alumno_id, motivo, usuario, periodo_id=None):
    with cursor() as (con, cur):
        cur.execute("""
            INSERT INTO medidas_sancionadoras
                (alumno_id, tipo, fecha_inicio, motivo, aplicada_por,
                 timestamp, activa, periodo_id)
            VALUES (?, 'matricula_condicional', ?, ?, ?, ?, 1, ?)
        """, (alumno_id, _hoy_str(), motivo, usuario["usuario"],
              _timestamp_str(), periodo_id))
    return True, "Matrícula condicional aplicada."


def aplicar_separacion_definitiva(alumno_id, motivo, usuario, periodo_id=None):
    with cursor() as (con, cur):
        cur.execute("""
            INSERT INTO medidas_sancionadoras
                (alumno_id, tipo, fecha_inicio, motivo, aplicada_por,
                 timestamp, activa, periodo_id)
            VALUES (?, 'separacion_definitiva', ?, ?, ?, ?, 1, ?)
        """, (alumno_id, _hoy_str(), motivo, usuario["usuario"],
              _timestamp_str(), periodo_id))
    return True, ("Separación definitiva aplicada. Comunicar a la familia "
                  "y a la UGEL (8.3.2.c).")


# ═══════════════════════════════════════════════════════════════════
# CONSULTAS
# ═══════════════════════════════════════════════════════════════════
def listar_observados(periodo_id=None):
    q = """
        SELECT a.id AS alumno_id, a.dni,
               a.apellido_paterno || ' ' || COALESCE(a.apellido_materno,'') || ', ' || a.nombres AS alumno,
               g.nombre AS grado, s.nombre AS seccion, t.nombre AS turno,
               COALESCE(c.numero_ciclo, 0) AS ciclo,
               COALESCE(c.leves_acumuladas, 0) AS leves_ciclo,
               COALESCE(c.graves_acumuladas, 0) AS graves_ciclo,
               (SELECT COUNT(*) FROM faltas f WHERE f.alumno_id=a.id
                  AND f.periodo_id = COALESCE(?, f.periodo_id)
                  AND f.tipo='leve') AS total_leves,
               (SELECT COUNT(*) FROM faltas f WHERE f.alumno_id=a.id
                  AND f.periodo_id = COALESCE(?, f.periodo_id)
                  AND f.tipo='grave') AS total_graves,
               (SELECT COUNT(*) FROM faltas f WHERE f.alumno_id=a.id
                  AND f.periodo_id = COALESCE(?, f.periodo_id)
                  AND f.tipo='muy_grave') AS total_muy_graves,
               (SELECT COUNT(*) FROM desbloqueos d WHERE d.alumno_id=a.id
                  AND d.periodo_id = COALESCE(?, d.periodo_id)) AS desbloqueos,
               CASE WHEN b.id IS NOT NULL THEN 'Bloqueado' ELSE 'Observado' END AS estado
        FROM alumnos a
        JOIN secciones s ON a.seccion_id = s.id
        JOIN grados g ON s.grado_id = g.id
        JOIN turnos t ON s.turno_id = t.id
        LEFT JOIN ciclos_disciplina c
               ON c.alumno_id = a.id AND c.estado = 'abierto'
              AND (? IS NULL OR c.periodo_id = ?)
        LEFT JOIN bloqueos b ON b.alumno_id = a.id AND b.activo = 1
        WHERE a.activo = 1
          AND (b.id IS NOT NULL
               OR c.leves_acumuladas > 0
               OR c.graves_acumuladas > 0
               OR EXISTS (SELECT 1 FROM faltas f2
                          WHERE f2.alumno_id = a.id
                            AND (? IS NULL OR f2.periodo_id = ?)))
        ORDER BY g.nombre, s.nombre, a.apellido_paterno
    """
    return leer_df(q, (periodo_id, periodo_id, periodo_id, periodo_id,
                       periodo_id, periodo_id, periodo_id, periodo_id))


def historial_faltas(alumno_id):
    return leer_df("""
        SELECT f.fecha, f.tipo, f.inciso, f.nombre_falta,
               f.descripcion, f.registrada_por,
               CASE WHEN f.notificada_familia=1 THEN 'Sí' ELSE 'No' END AS notificada
        FROM faltas f WHERE f.alumno_id=?
        ORDER BY f.fecha DESC, f.id DESC
    """, (alumno_id,))


def historial_desbloqueos(alumno_id):
    return leer_df("""
        SELECT timestamp AS fecha, motivo, liberado_por
        FROM desbloqueos WHERE alumno_id=?
        ORDER BY id DESC
    """, (alumno_id,))


def medidas_de_alumno(alumno_id):
    return leer_df("""
        SELECT id, tipo, fecha_inicio, COALESCE(fecha_fin,'-') AS fecha_fin,
               dias, motivo, aplicada_por,
               CASE WHEN activa=1 THEN 'Activa' ELSE 'Cumplida' END AS estado
        FROM medidas_sancionadoras WHERE alumno_id=?
        ORDER BY fecha_inicio DESC
    """, (alumno_id,))


def acciones_reparadoras_de_alumno(alumno_id):
    return leer_df("""
        SELECT fecha, descripcion, registrada_por,
               CASE WHEN completada=1 THEN 'Completada' ELSE 'Pendiente' END AS estado
        FROM acciones_reparadoras WHERE alumno_id=?
        ORDER BY fecha DESC
    """, (alumno_id,))


def ciclos_de_alumno(alumno_id):
    return leer_df("""
        SELECT numero_ciclo AS ciclo, leves_acumuladas AS leves,
               graves_acumuladas AS graves, iniciado_en,
               COALESCE(cerrado_en,'-') AS cerrado_en, estado
        FROM ciclos_disciplina WHERE alumno_id=?
        ORDER BY numero_ciclo DESC
    """, (alumno_id,))


# ═══════════════════════════════════════════════════════════════════
# CÁMARA QR
# ═══════════════════════════════════════════════════════════════════
def _camara_qr(key):
    try:
        from qr_scanner_component import qr_scanner
    except ImportError:
        st.error("Falta qr_scanner_component.py")
        return None

    mount_id = st.session_state.get(f"_qr_mount_{key}", 0)
    result = qr_scanner(key=f"qr_{key}_{mount_id}", on_scan=lambda: None)

    if result is None or not getattr(result, "qr_dni", None):
        return None

    dni = str(result.qr_dni).strip()
    proc_key = f"_qr_procesado_{key}"
    ult = st.session_state.get(proc_key, {})
    import time as _t
    if ult.get("dni") == dni and (_t.time() - ult.get("ts", 0)) < 3.0:
        return None
    st.session_state[proc_key] = {"dni": dni, "ts": _t.time()}
    st.session_state[f"_qr_mount_{key}"] = mount_id + 1
    return dni


def _buscar_alumno_por_dni(dni):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.id, a.dni, a.nombres, a.apellido_paterno, a.apellido_materno,
                   a.seccion_id,
                   s.nombre AS seccion, g.nombre AS grado,
                   t.id AS turno_id, t.nombre AS turno
            FROM alumnos a
            JOIN secciones s ON a.seccion_id = s.id
            JOIN grados g ON s.grado_id = g.id
            JOIN turnos t ON s.turno_id = t.id
            WHERE a.dni=? AND a.activo=1
        """, (dni,))
        f = cur.fetchone()
    return dict(f) if f else None


# ═══════════════════════════════════════════════════════════════════
# VISTA PRINCIPAL
# ═══════════════════════════════════════════════════════════════════
def vista_disciplina():
    st.title("Disciplina Escolar")
    st.caption("D.S. 021-2026-MINEDU — El sistema SUGIERE, el director DECIDE.")

    usuario = st.session_state["user"]
    try:
        from app import obtener_periodo_activo
        per = obtener_periodo_activo()
        periodo_id = per["id"] if per else None
    except Exception:
        periodo_id = None

    if periodo_id is None:
        st.warning("No hay periodo activo.")
        return

    tabs = st.tabs([
        "Observados", "Registrar incidencia", "Medidas", "Catálogo", "Prohibiciones"
    ])

    with tabs[0]:
        _tab_observados(usuario, periodo_id)
    with tabs[1]:
        _tab_registrar_incidencia(usuario, periodo_id)
    with tabs[2]:
        _tab_medidas(usuario, periodo_id)
    with tabs[3]:
        _tab_catalogo(usuario)
    with tabs[4]:
        _tab_prohibiciones()


# ── TAB 1: OBSERVADOS ─────────────────────────────────────────────
def _tab_observados(usuario, periodo_id):
    st.subheader("Lista de observados")

    df = listar_observados(periodo_id)
    if df.empty:
        st.success("No hay alumnos observados ni bloqueados.")
        return

    st.dataframe(df, width='stretch', hide_index=True)

    st.markdown("---")
    ops = {
        f"{r['alumno']} ({r['grado']} {r['seccion']}) — "
        f"obs:{r['desbloqueos']} | L:{r['total_leves']} G:{r['total_graves']} MG:{r['total_muy_graves']}":
        r["alumno_id"]
        for _, r in df.iterrows()
    }
    sel = st.selectbox("Alumno", list(ops.keys()), key="disc_obs_sel")
    alumno_id = ops[sel]
    alum_row = df[df["alumno_id"] == alumno_id].iloc[0]

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Ciclo actual", alum_row["ciclo"])
    c2.metric("Leves en ciclo", alum_row["leves_ciclo"])
    c3.metric("Graves en ciclo", alum_row["graves_ciclo"])
    c4.metric("Desbloqueos", alum_row["desbloqueos"])

    st.markdown("---")
    c_perdon, c_repar, c_ver = st.columns(3)

    with c_perdon:
        st.markdown("**Perdonar / Desbloquear**")
        motivo_perdon = st.text_input("Motivo del perdón",
                                      key="disc_perdon_motivo")
        if st.button("Perdonar y dar 3 oportunidades",
                     type="primary", use_container_width=True,
                     key="disc_btn_perdon"):
            ok, msg = perdonar_y_reabrir(alumno_id, usuario, periodo_id,
                                         motivo_perdon)
            if ok:
                st.toast(msg)
                st.rerun()

    with c_repar:
        st.markdown("**Acción reparadora**")
        desc_accion = st.text_input("Descripción", key="disc_accion_desc")
        if st.button("Registrar acción reparadora",
                     use_container_width=True, key="disc_btn_accion"):
            if not desc_accion.strip():
                st.error("Describe la acción.")
            else:
                ok, msg = registrar_accion_reparadora(
                    alumno_id, None, desc_accion.strip(), usuario, periodo_id)
                if ok:
                    st.toast(msg)
                    st.rerun()

    with c_ver:
        st.markdown("**Ver detalle completo**")
        if st.button("Ver perfil del alumno", use_container_width=True,
                     key="disc_btn_ver_perfil"):
            st.session_state["disc_perfil_alumno_id"] = alumno_id
            st.rerun()

    if st.session_state.get("disc_perfil_alumno_id") == alumno_id:
        st.markdown("---")
        _render_perfil_disciplinario(alumno_id, usuario, periodo_id)


# ── TAB 2: REGISTRAR INCIDENCIA ──────────────────────────────────
def _tab_registrar_incidencia(usuario, periodo_id):
    st.subheader("Registrar incidencia")

    modo = st.radio("Método", ["Escanear QR", "Buscar por lista"],
                    horizontal=True, key="disc_modo_reg")

    if modo == "Escanear QR":
        st.caption("Apunta la cámara al QR del alumno.")
        dni = _camara_qr("disc_inc")
        if dni:
            alumno = _buscar_alumno_por_dni(dni)
            if not alumno:
                st.error(f"No hay alumno con DNI {dni}")
            else:
                st.session_state["disc_alumno_reg_id"] = alumno["id"]
    else:
        from app import _selector_grado_seccion, alumnos_de_seccion
        idsec = _selector_grado_seccion("disc_reg_sel", "Elige el grado")
        if idsec:
            df_al = alumnos_de_seccion(idsec)
            if not df_al.empty:
                ops = {r["nombre_completo"]: r["id"] for _, r in df_al.iterrows()}
                sel = st.selectbox("Alumno", list(ops.keys()),
                                   key="disc_reg_alumno_sel")
                st.session_state["disc_alumno_reg_id"] = ops[sel]

    alumno_id = st.session_state.get("disc_alumno_reg_id")
    if not alumno_id:
        st.info("Escanea un QR o selecciona un alumno.")
        return

    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
            FROM alumnos a
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE a.id=?
        """, (alumno_id,))
        al = cur.fetchone()
    if not al:
        st.warning("Alumno no encontrado.")
        return

    nombre = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}"
    st.markdown(f"**{nombre}** — {al['grado']} {al['seccion']} — Turno {al['turno']}")
    st.markdown("---")

    tipo = st.selectbox("Tipo de falta",
                        [FALTA_LEVE, FALTA_GRAVE, FALTA_MUY_GRAVE],
                        format_func=lambda x: {
                            FALTA_LEVE: "🟡 Leve",
                            FALTA_GRAVE: "🟠 Grave",
                            FALTA_MUY_GRAVE: "🔴 Muy grave",
                        }[x],
                        key="disc_reg_tipo")

    catalogo = obtener_catalogo(tipo)
    if not catalogo:
        st.warning(f"No hay faltas tipificadas para {tipo}.")
        return

    ops_cat = {f"{c['inciso']} — {c['nombre']}": c for c in catalogo}
    sel_cat = st.selectbox("Falta específica", list(ops_cat.keys()),
                           key="disc_reg_falta")
    falta_data = ops_cat[sel_cat]

    with st.expander("Ver descripción"):
        st.write(falta_data.get("descripcion", ""))

    desc_extra = st.text_area("Detalle de los hechos (opcional)",
                              key="disc_reg_desc_extra")

    notificar = st.checkbox("¿Ya se notificó a la familia? (9.1)",
                            key="disc_reg_notificar")

    if st.button("Registrar falta y ver sugerencias",
                 type="primary", use_container_width=True,
                 key="disc_reg_btn"):
        desc_final = falta_data.get("descripcion", "")
        if desc_extra.strip():
            desc_final += f"\n\nDetalle: {desc_extra.strip()}"

        res = registrar_falta(
            alumno_id, tipo, falta_data.get("inciso", ""),
            falta_data.get("nombre", ""), desc_final,
            usuario, periodo_id, notificar_familia=notificar)

        # Mostrar sugerencias en pantalla
        st.session_state["disc_sugerencias_actuales"] = {
            "alumno_id": alumno_id,
            "res": res,
        }
        st.rerun()

    # Panel de sugerencias
    sug_data = st.session_state.get("disc_sugerencias_actuales")
    if sug_data and sug_data["alumno_id"] == alumno_id:
        st.markdown("---")
        _render_panel_sugerencias(sug_data["res"], usuario, periodo_id)


# ── 🆕 PANEL DE SUGERENCIAS ──────────────────────────────────────
def _render_panel_sugerencias(res, usuario, periodo_id):
    sug = res["sugerencias"]
    escalado = res.get("escalado")

    st.success(f"✅ Falta registrada (ID #{res['falta_id']}).")

    if escalado:
        if escalado == FALTA_GRAVE:
            st.warning("⚠️ REINCIDENCIA: 3 faltas leves → FALTA GRAVE")
        elif escalado == FALTA_MUY_GRAVE:
            st.error("🚨 REINCIDENCIA: 3 faltas graves → FALTA MUY GRAVE")

    st.markdown("## 📋 Sugerencias según la norma")
    st.caption("El sistema SUGIERE. El director DECIDE qué aplicar.")

    # Medidas prioritarias
    if sug["prioritarias"]:
        st.markdown("### ⭐ Medidas prioritarias (recomendadas)")
        for nombre_m, base in sug["prioritarias"]:
            st.markdown(f"- **{nombre_m}** — *{base}*")

    # Medidas permitidas
    if sug["permitidas"]:
        st.markdown("### ✅ Medidas permitidas")
        for item in sug["permitidas"]:
            if len(item) == 3:
                nombre_m, base, cat = item
                st.markdown(f"- {nombre_m} — *{base}* ({cat})")
            else:
                nombre_m, base = item
                st.markdown(f"- {nombre_m} — *{base}*")

    # No recomendadas
    if sug["no_recomendadas"]:
        st.markdown("### ⚠️ No recomendadas por ahora")
        for n in sug["no_recomendadas"]:
            st.markdown(f"- {n}")

    # Alertas
    if sug["alertas"]:
        st.markdown("### 🚨 Alertas")
        for a in sug["alertas"]:
            st.warning(a)

    st.markdown("---")
    st.info(
        "👉 Ve a la pestaña **Medidas** para aplicar la sanción decidida. "
        "El sistema registra la trazabilidad automáticamente.")

    if st.button("Cerrar panel", key="disc_cerrar_sug"):
        st.session_state.pop("disc_sugerencias_actuales", None)
        st.session_state.pop("disc_alumno_reg_id", None)
        st.rerun()


# ── TAB 3: MEDIDAS ───────────────────────────────────────────────
def _tab_medidas(usuario, periodo_id):
    st.subheader("Aplicar medida sancionadora")
    st.caption("Solo el director (o Admin) aplica medidas (9.b).")

    if usuario["rol"] not in ("Admin", "Direccion"):
        st.warning("Solo Dirección o Admin pueden aplicar medidas.")
        return

    from app import _selector_grado_seccion, alumnos_de_seccion
    idsec = _selector_grado_seccion("disc_med_sel", "Elige el grado")
    if not idsec:
        return
    df_al = alumnos_de_seccion(idsec)
    if df_al.empty:
        return

    ops = {r["nombre_completo"]: r["id"] for _, r in df_al.iterrows()}
    sel = st.selectbox("Alumno", list(ops.keys()), key="disc_med_alumno")
    alumno_id = ops[sel]

    # Mostrar sugerencias de nuevo por si quiere revisarlas
    hist = contar_faltas_por_tipo(alumno_id, periodo_id)
    st.caption(f"Historial: L={hist[FALTA_LEVE]} G={hist[FALTA_GRAVE]} "
               f"MG={hist[FALTA_MUY_GRAVE]}")

    # Sanción activa hoy
    sancion = obtener_sancion_activa(alumno_id)
    if sancion:
        st.error(
            f"🚫 Este alumno tiene una sanción activa: **{sancion['tipo']}** "
            f"({sancion['fecha_inicio']} → {sancion.get('fecha_fin') or 'sin fin'}). "
            f"No podrá escanear su QR hasta que termine.")

    medida = st.radio(
        "Medida",
        [MEDIDA_SUSPENSION, MEDIDA_MATRICULA, MEDIDA_SEPARACION],
        format_func=lambda x: {
            MEDIDA_SUSPENSION: "Suspensión temporal (1-3 días)",
            MEDIDA_MATRICULA: "Matrícula condicional",
            MEDIDA_SEPARACION: "Separación definitiva",
        }[x],
        key="disc_med_tipo")

    motivo = st.text_area("Motivo *", key="disc_med_motivo")

    dias = 1
    if medida == MEDIDA_SUSPENSION:
        dias = st.number_input("Días (1-3)", min_value=1, max_value=3,
                               value=1, key="disc_med_dias")

    if st.button("Aplicar medida", type="primary",
                 use_container_width=True, key="disc_btn_medida"):
        if not motivo.strip():
            st.error("El motivo es obligatorio.")
            return

        if medida == MEDIDA_SUSPENSION:
            ok, msg = aplicar_suspension(alumno_id, dias, motivo,
                                         usuario, periodo_id)
            st.warning(msg)
            st.rerun()
        elif medida == MEDIDA_MATRICULA:
            ok, msg = aplicar_matricula_condicional(alumno_id, motivo,
                                                    usuario, periodo_id)
            st.toast(msg)
            st.rerun()
        elif medida == MEDIDA_SEPARACION:
            ok, msg = aplicar_separacion_definitiva(alumno_id, motivo,
                                                    usuario, periodo_id)
            st.error(msg)
            st.rerun()


# ── TAB 4: CATÁLOGO + CUSTOM ────────────────────────────────────
def _tab_catalogo(usuario):
    st.subheader("Catálogo de faltas")

    tab_base, tab_custom, tab_add = st.tabs(
        ["Catálogo base (D.S.)", "Mis faltas personalizadas", "➕ Agregar nueva falta"])

    with tab_base:
        with st.expander(f"🟡 Leves ({len(CATALOGO_LEVES)})"):
            for c in CATALOGO_LEVES:
                st.markdown(f"**{c['inciso']}** — {c['nombre']}")
                st.caption(c["descripcion"])
                st.markdown("---")
        with st.expander(f"🟠 Graves ({len(CATALOGO_GRAVES)})"):
            for c in CATALOGO_GRAVES:
                st.markdown(f"**{c['inciso']}** — {c['nombre']}")
                st.caption(c["descripcion"])
                st.markdown("---")
        with st.expander(f"🔴 Muy graves ({len(CATALOGO_MUY_GRAVES)})"):
            for c in CATALOGO_MUY_GRAVES:
                st.markdown(f"**{c['inciso']}** — {c['nombre']}")
                st.caption(c["descripcion"])
                st.markdown("---")

    with tab_custom:
        custom = obtener_catalogo_custom()
        if not custom:
            st.info("Aún no has agregado faltas personalizadas.")
        else:
            st.write(f"Tienes **{len(custom)}** falta(s) personalizada(s).")
            for f in custom:
                with st.container(border=True):
                    c1, c2 = st.columns([4, 1])
                    c1.markdown(f"**[{f['tipo'].replace('_',' ').title()}]** "
                                f"`{f.get('inciso') or '-'}` — **{f['nombre']}**")
                    c1.caption(f.get("descripcion") or "")
                    if c2.button("Eliminar", key=f"del_custom_{f['id']}"):
                        eliminar_falta_custom(f["id"])
                        st.toast("Eliminada.")
                        st.rerun()

    with tab_add:
        st.markdown("**Agregar falta al catálogo**")
        with st.form("form_add_custom"):
            tipo_n = st.selectbox(
                "Gravedad",
                [FALTA_LEVE, FALTA_GRAVE, FALTA_MUY_GRAVE],
                format_func=lambda x: {
                    FALTA_LEVE: "🟡 Leve",
                    FALTA_GRAVE: "🟠 Grave",
                    FALTA_MUY_GRAVE: "🔴 Muy grave",
                }[x],
                key="add_custom_tipo")
            inciso = st.text_input("Inciso (opcional)", key="add_custom_inciso")
            nombre = st.text_input("Nombre *", key="add_custom_nombre")
            descripcion = st.text_area("Descripción", key="add_custom_desc")
            base_legal = st.text_input("Base legal (opcional)",
                                       key="add_custom_base")
            if st.form_submit_button("Agregar falta", type="primary"):
                ok, msg = crear_falta_custom(
                    tipo_n, inciso, nombre, descripcion, base_legal, usuario)
                if ok:
                    st.success(msg)
                    st.rerun()
                else:
                    st.error(msg)


# ── TAB 5: PROHIBICIONES ────────────────────────────────────────
def _tab_prohibiciones():
    st.subheader("Prohibiciones absolutas (numeral X)")
    st.caption("Ninguna de estas puede usarse como medida disciplinaria.")
    for p in PROHIBICIONES:
        st.markdown(f"**{p['inciso']} — {p['titulo']}**")
        st.caption(p["descripcion"])
        st.markdown("---")


# ── PERFIL DISCIPLINARIO ────────────────────────────────────────
def _render_perfil_disciplinario(alumno_id, usuario, periodo_id):
    with cursor() as (con, cur):
        cur.execute("""
            SELECT a.*, s.nombre AS seccion, g.nombre AS grado, t.nombre AS turno
            FROM alumnos a
            JOIN secciones s ON a.seccion_id=s.id
            JOIN grados g ON s.grado_id=g.id
            JOIN turnos t ON s.turno_id=t.id
            WHERE a.id=?
        """, (alumno_id,))
        al = cur.fetchone()
    if not al:
        st.warning("Alumno no encontrado.")
        return

    nombre = f"{al['apellido_paterno']} {al['apellido_materno'] or ''}, {al['nombres']}"

    with st.container(border=True):
        c_izq, c_der = st.columns([3, 2])
        with c_izq:
            st.markdown(f"### {nombre}")
            st.caption(f"{al['grado']} {al['seccion']} — Turno {al['turno']} — "
                       f"DNI {al['dni']}")
            st.markdown("**Apoderado**")
            st.caption(f"{al['nombre_apoderado'] or '-'} — "
                       f"Tel: {al['telefono_apoderado'] or '-'}")
        with c_der:
            st.markdown("**Escanear QR para actualizar**")
            dni = _camara_qr(f"disc_perfil_{alumno_id}")
            if dni:
                st.success(f"DNI escaneado: {dni}")

    sancion = obtener_sancion_activa(alumno_id)
    if sancion:
        st.error(
            f"🚫 SANCIÓN ACTIVA: **{sancion['tipo']}** "
            f"({sancion['fecha_inicio']} → {sancion.get('fecha_fin') or 'sin fin'}). "
            f"Motivo: {sancion.get('motivo') or '-'}")

    faltas = contar_faltas_por_tipo(alumno_id, periodo_id)
    desbloqueos = contar_desbloqueos(alumno_id, periodo_id)
    ciclo = obtener_ciclo_abierto(alumno_id, periodo_id)

    c1, c2, c3, c4, c5 = st.columns(5)
    c1.metric("Leves", faltas[FALTA_LEVE])
    c2.metric("Graves", faltas[FALTA_GRAVE])
    c3.metric("Muy graves", faltas[FALTA_MUY_GRAVE])
    c4.metric("Desbloqueos", desbloqueos)
    c5.metric("Ciclo", ciclo["numero_ciclo"] if ciclo else "-")

    st.markdown("---")
    tabs_det = st.tabs(["Faltas", "Desbloqueos", "Reparadoras", "Ciclos", "Medidas"])

    with tabs_det[0]:
        df_f = historial_faltas(alumno_id)
        if df_f.empty:
            st.info("Sin faltas.")
        else:
            st.dataframe(df_f, width='stretch', hide_index=True)

    with tabs_det[1]:
        df_d = historial_desbloqueos(alumno_id)
        if df_d.empty:
            st.info("Sin desbloqueos.")
        else:
            st.dataframe(df_d, width='stretch', hide_index=True)

    with tabs_det[2]:
        df_a = acciones_reparadoras_de_alumno(alumno_id)
        if df_a.empty:
            st.info("Sin acciones reparadoras.")
        else:
            st.dataframe(df_a, width='stretch', hide_index=True)

    with tabs_det[3]:
        df_c = ciclos_de_alumno(alumno_id)
        if df_c.empty:
            st.info("Sin ciclos.")
        else:
            st.dataframe(df_c, width='stretch', hide_index=True)

    with tabs_det[4]:
        df_m = medidas_de_alumno(alumno_id)
        if df_m.empty:
            st.info("Sin medidas.")
        else:
            st.dataframe(df_m, width='stretch', hide_index=True)

    if st.button("← Volver", key="disc_perfil_volver"):
        st.session_state.pop("disc_perfil_alumno_id", None)
        st.rerun()
