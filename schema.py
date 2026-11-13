# schema.py
"""Schema SQLite. Idempotente: se puede ejecutar siempre."""
import logging

from db import cursor

log = logging.getLogger("asistencia.schema")


# ─────────────────────────────────────────────────────────────
# Lista de CREATE TABLE IF NOT EXISTS. Cada string es una tabla.
# Como usamos "IF NOT EXISTS", correr esto mil veces no rompe nada:
# si la tabla ya existe, se ignora; si no, se crea.
# ─────────────────────────────────────────────────────────────
TABLAS = [
    # ── Periodos académicos (años/bimestres). Se pueden cerrar. ──
    """
    CREATE TABLE IF NOT EXISTS periodos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        fecha_inicio TEXT,
        fecha_fin TEXT,
        activo INTEGER DEFAULT 0,       -- solo uno debería estar activo a la vez
        fecha_cierre TEXT,
        cerrado INTEGER DEFAULT 0
    )
    """,

    # ── Turnos (mañana/tarde). Definen horas de entrada/salida y tolerancia. ──
    """
    CREATE TABLE IF NOT EXISTS turnos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT UNIQUE,
        hora_entrada TEXT,
        hora_salida TEXT,
        tolerancia_min INTEGER DEFAULT 10
    )
    """,

    # ── Grados (1ro, 2do, ...). Catálogo simple. ──
    """
    CREATE TABLE IF NOT EXISTS grados (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT UNIQUE
    )
    """,

    # ── Secciones (A, B, C...). Cada una pertenece a un grado y un turno. ──
    """
    CREATE TABLE IF NOT EXISTS secciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT,
        grado_id INTEGER REFERENCES grados(id),
        turno_id INTEGER REFERENCES turnos(id),
        UNIQUE(nombre, grado_id, turno_id)  -- no puede repetirse la misma sección
    )
    """,

    # ── Apoderados (padres/madres). Se referencian desde alumnos. ──
    """
    CREATE TABLE IF NOT EXISTS apoderados (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        nombre TEXT NOT NULL,
        telefono TEXT
    )
    """,

    # ── Alumnos. El DNI es único. Guardamos datos "denormalizados"
    #    del apoderado por si no está cargado en la tabla aparte. ──
    """
    CREATE TABLE IF NOT EXISTS alumnos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dni TEXT UNIQUE NOT NULL,
        nombres TEXT NOT NULL,
        apellido_paterno TEXT NOT NULL,
        apellido_materno TEXT,
        seccion_id INTEGER REFERENCES secciones(id),
        apoderado_id INTEGER REFERENCES apoderados(id),
        nombre_apoderado TEXT,
        telefono_apoderado TEXT,
        periodo_id INTEGER,
        activo INTEGER DEFAULT 1,
        retirado_en TEXT
    )
    """,

    # ── Ventanas de tiempo para marcar asistencia (clases o reforzamiento).
    #    Ej: "Entrada mañana" de 07:00 a 08:00. ──
    """
    CREATE TABLE IF NOT EXISTS ventanas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        turno_id INTEGER NOT NULL REFERENCES turnos(id),
        tipo TEXT NOT NULL CHECK(tipo IN ('clases','reforzamiento')),
        nombre TEXT NOT NULL,
        hora_apertura TEXT NOT NULL,
        hora_limite_puntual TEXT,
        hora_cierre TEXT NOT NULL,
        tolerancia_min INTEGER DEFAULT 0,
        orden INTEGER DEFAULT 0,
        activo INTEGER DEFAULT 1
    )
    """,

    # ── Asistencias. UNIQUE(alumno, fecha, tipo) evita duplicados
    #    en la misma ventana del mismo día. ──
    """
    CREATE TABLE IF NOT EXISTS asistencias (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        fecha TEXT NOT NULL,
        ventana_id INTEGER REFERENCES ventanas(id),
        tipo TEXT NOT NULL DEFAULT 'clases',
        hora TEXT,
        estado TEXT NOT NULL,             -- Presente, Tarde, Falta, etc.
        justificada INTEGER DEFAULT 0,
        observacion TEXT,
        origen TEXT DEFAULT 'qr',
        justificado_por TEXT,
        justificado_en TEXT,
        periodo_id INTEGER,
        dia_especial_id INTEGER,
        UNIQUE(alumno_id, fecha, tipo)
    )
    """,

    # ── Tardanzas. UNIQUE(alumno, fecha): una tardanza por día. ──
    """
    CREATE TABLE IF NOT EXISTS tardanzas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        fecha TEXT NOT NULL,
        hora TEXT NOT NULL,
        numero INTEGER NOT NULL,          -- 1ra, 2da, 3ra tardanza...
        accion TEXT NOT NULL,             -- qué se hizo (llamada, citación...)
        observacion TEXT,
        justificada INTEGER DEFAULT 0,
        origen TEXT DEFAULT 'qr',
        registrado_por TEXT,
        timestamp TEXT NOT NULL,
        periodo_id INTEGER,
        UNIQUE(alumno_id, fecha)
    )
    """,

    # ── Bloqueos. Cuando un alumno acumula X tardanzas, se bloquea
    #    hasta que alguien lo libere manualmente. ──
    """
    CREATE TABLE IF NOT EXISTS bloqueos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        motivo TEXT,
        activo INTEGER DEFAULT 1,
        fecha_inicio TEXT NOT NULL,
        fecha_fin TEXT,
        liberado_por TEXT,
        creado_por TEXT,
        origen TEXT DEFAULT 'automatico'
    )
    """,

    # ── Justificaciones cargadas ANTES de la falta (ej: el padre avisa
    #    que mañana no va). Cuando llega la fecha, se aplica sola. ──
    """
    CREATE TABLE IF NOT EXISTS justificaciones_previas (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        fecha_objetivo TEXT NOT NULL,
        tipo TEXT NOT NULL CHECK(tipo IN ('Falta')),
        motivo TEXT,
        creado_por TEXT,
        timestamp TEXT NOT NULL,
        aplicada INTEGER DEFAULT 0,
        UNIQUE(alumno_id, fecha_objetivo)
    )
    """,

    # ── Permisos (rango de fechas en el que el alumno no asiste). ──
    """
    CREATE TABLE IF NOT EXISTS permisos (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
        fecha_inicio TEXT NOT NULL,
        fecha_fin TEXT NOT NULL,
        motivo TEXT,
        tipo TEXT NOT NULL DEFAULT 'permiso',
        creado_por TEXT,
        timestamp TEXT NOT NULL,
        activo INTEGER DEFAULT 1,
        periodo_id INTEGER
    )
    """,

    # ── Días especiales (eventos, feriados). Pueden o no contar como clases. ──
    """
    CREATE TABLE IF NOT EXISTS dias_especiales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        fecha TEXT NOT NULL,
        descripcion TEXT,
        turno_id INTEGER REFERENCES turnos(id),
        hora_entrada TEXT,
        activo INTEGER DEFAULT 1,
        tipo TEXT DEFAULT 'evento' CHECK(tipo IN ('evento','feriado')),
        periodo_id INTEGER,
        contar_como_clases INTEGER DEFAULT 0
    )
    """,

    # ── Qué secciones están afectadas por un día especial (relación N:N). ──
    """
    CREATE TABLE IF NOT EXISTS dias_especiales_secciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        dia_especial_id INTEGER NOT NULL REFERENCES dias_especiales(id) ON DELETE CASCADE,
        seccion_id INTEGER NOT NULL REFERENCES secciones(id),
        UNIQUE(dia_especial_id, seccion_id)
    )
    """,

    # ── Usuarios del sistema (Admin, Dirección, Auxiliar). ──
    #    Incluye control de intentos fallidos y bloqueo temporal. ──
    """
    CREATE TABLE IF NOT EXISTS usuarios (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT UNIQUE NOT NULL,
        password TEXT NOT NULL,
        rol TEXT NOT NULL CHECK(rol IN ('Admin','Direccion','Auxiliar')),
        nombres TEXT NOT NULL,
        turno_asignado INTEGER REFERENCES turnos(id),
        activo INTEGER DEFAULT 1,
        intentos_fallidos INTEGER DEFAULT 0,
        bloqueado_hasta TEXT,
        debe_cambiar_password INTEGER DEFAULT 0,
        ultimo_login TEXT,
        ultimo_ip TEXT,
        es_principal INTEGER DEFAULT 0
    )
    """,

    # ── Secciones asignadas a cada auxiliar (un auxiliar puede tener varias). ──
    #    UNIQUE(seccion_id) evita que dos auxiliares compartan la misma sección.
    """
    CREATE TABLE IF NOT EXISTS auxiliar_secciones (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
        seccion_id INTEGER NOT NULL REFERENCES secciones(id) ON DELETE CASCADE,
        UNIQUE(seccion_id)
    )
    """,

    # ── Auditoría: quién hizo qué, cuándo y desde qué IP. ──
    """
    CREATE TABLE IF NOT EXISTS auditoria (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        usuario TEXT,
        accion TEXT,
        fecha TEXT,
        valor_anterior TEXT,
        valor_nuevo TEXT,
        tabla_afectada TEXT,
        registro_id INTEGER,
        ip TEXT
    )
    """,

    # ── Cierres anuales: snapshot JSON con el reporte del periodo cerrado. ──
    """
    CREATE TABLE IF NOT EXISTS cierres_anuales (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        periodo_id INTEGER NOT NULL REFERENCES periodos(id),
        fecha_cierre TEXT NOT NULL,
        generado_por TEXT,
        reporte_json TEXT
    )
    """,

    # ── Configuración clave/valor (parámetros globales del sistema). ──
    """
    CREATE TABLE IF NOT EXISTS config (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        clave TEXT UNIQUE NOT NULL,
        valor TEXT
    )
    """,
]


# ─────────────────────────────────────────────────────────────
# Índices. Aceleran las consultas más frecuentes (por fecha,
# por alumno, por DNI, por ventana, etc.).
# ─────────────────────────────────────────────────────────────
INDICES = [
    "CREATE INDEX IF NOT EXISTS idx_ast_f ON asistencias(fecha)",
    "CREATE INDEX IF NOT EXISTS idx_ast_a ON asistencias(alumno_id)",
    "CREATE INDEX IF NOT EXISTS idx_tard_a ON tardanzas(alumno_id)",
    "CREATE INDEX IF NOT EXISTS idx_tard_f ON tardanzas(fecha)",
    "CREATE INDEX IF NOT EXISTS idx_al_dni ON alumnos(dni)",
    "CREATE INDEX IF NOT EXISTS idx_al_sec ON alumnos(seccion_id)",
    "CREATE INDEX IF NOT EXISTS idx_vent_t ON ventanas(turno_id, tipo)",
    "CREATE INDEX IF NOT EXISTS idx_bloq_a ON bloqueos(alumno_id, activo)",
    "CREATE INDEX IF NOT EXISTS idx_aud_f ON auditoria(fecha)",
    "CREATE INDEX IF NOT EXISTS idx_perm_a ON permisos(alumno_id, activo)",
    "CREATE INDEX IF NOT EXISTS idx_jp_a ON justificaciones_previas(alumno_id, aplicada)",
    "CREATE INDEX IF NOT EXISTS idx_dias_f ON dias_especiales(fecha, activo)",
]






def aplicar_schema():
    """Ejecuta todo el schema (idempotente)."""
    with cursor() as (con, cur):
        for sql in TABLAS:
            cur.execute(sql)
        for sql in INDICES:
            cur.execute(sql)
    # ── 🆕 SCHEMA DE DISCIPLINA (D.S. 021-2026-MINEDU) ──
    from disciplina import aplicar_schema_disciplina
    aplicar_schema_disciplina()
    log.info("schema aplicado (SQLite)")
