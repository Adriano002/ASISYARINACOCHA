"""Schema PostgreSQL (Neon). Idempotente: se puede ejecutar siempre."""
import logging

from db import cursor

log = logging.getLogger("asistencia.schema")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS periodos (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    fecha_inicio DATE,
    fecha_fin DATE,
    activo INTEGER DEFAULT 0,
    fecha_cierre TIMESTAMP,
    cerrado INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS turnos (
    id SERIAL PRIMARY KEY,
    nombre TEXT UNIQUE,
    hora_entrada TIME,
    hora_salida TIME,
    tolerancia_min INTEGER DEFAULT 10
);

CREATE TABLE IF NOT EXISTS grados (
    id SERIAL PRIMARY KEY,
    nombre TEXT UNIQUE
);

CREATE TABLE IF NOT EXISTS secciones (
    id SERIAL PRIMARY KEY,
    nombre TEXT,
    grado_id INTEGER REFERENCES grados(id),
    turno_id INTEGER REFERENCES turnos(id),
    UNIQUE(nombre, grado_id, turno_id)
);

CREATE TABLE IF NOT EXISTS apoderados (
    id SERIAL PRIMARY KEY,
    nombre TEXT NOT NULL,
    telefono TEXT
);

CREATE TABLE IF NOT EXISTS alumnos (
    id SERIAL PRIMARY KEY,
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
    retirado_en TIMESTAMP
);

CREATE TABLE IF NOT EXISTS ventanas (
    id SERIAL PRIMARY KEY,
    turno_id INTEGER NOT NULL REFERENCES turnos(id),
    tipo TEXT NOT NULL CHECK(tipo IN ('clases','reforzamiento')),
    nombre TEXT NOT NULL,
    hora_apertura TIME NOT NULL,
    hora_limite_puntual TIME,
    hora_cierre TIME NOT NULL,
    tolerancia_min INTEGER DEFAULT 0,
    orden INTEGER DEFAULT 0,
    activo INTEGER DEFAULT 1
);

CREATE TABLE IF NOT EXISTS asistencias (
    id SERIAL PRIMARY KEY,
    alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
    fecha DATE NOT NULL,
    ventana_id INTEGER REFERENCES ventanas(id),
    tipo TEXT NOT NULL DEFAULT 'clases',
    hora TIME,
    estado TEXT NOT NULL,
    justificada INTEGER DEFAULT 0,
    observacion TEXT,
    origen TEXT DEFAULT 'qr',
    justificado_por TEXT,
    justificado_en TIMESTAMP,
    periodo_id INTEGER,
    dia_especial_id INTEGER,
    UNIQUE(alumno_id, fecha, tipo)
);

CREATE TABLE IF NOT EXISTS tardanzas (
    id SERIAL PRIMARY KEY,
    alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
    fecha DATE NOT NULL,
    hora TIME NOT NULL,
    numero INTEGER NOT NULL,
    accion TEXT NOT NULL,
    observacion TEXT,
    justificada INTEGER DEFAULT 0,
    origen TEXT DEFAULT 'qr',
    registrado_por TEXT,
    timestamp TIMESTAMP NOT NULL,
    periodo_id INTEGER,
    UNIQUE(alumno_id, fecha)
);

CREATE TABLE IF NOT EXISTS bloqueos (
    id SERIAL PRIMARY KEY,
    alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
    motivo TEXT,
    activo INTEGER DEFAULT 1,
    fecha_inicio DATE NOT NULL,
    fecha_fin TIMESTAMP,
    liberado_por TEXT,
    creado_por TEXT,
    origen TEXT DEFAULT 'automatico'
);

CREATE TABLE IF NOT EXISTS justificaciones_previas (
    id SERIAL PRIMARY KEY,
    alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
    fecha_objetivo DATE NOT NULL,
    tipo TEXT NOT NULL CHECK(tipo IN ('Falta')),
    motivo TEXT,
    creado_por TEXT,
    timestamp TIMESTAMP NOT NULL,
    aplicada INTEGER DEFAULT 0,
    UNIQUE(alumno_id, fecha_objetivo)
);

CREATE TABLE IF NOT EXISTS permisos (
    id SERIAL PRIMARY KEY,
    alumno_id INTEGER NOT NULL REFERENCES alumnos(id),
    fecha_inicio DATE NOT NULL,
    fecha_fin DATE NOT NULL,
    motivo TEXT,
    tipo TEXT NOT NULL DEFAULT 'permiso',
    creado_por TEXT,
    timestamp TIMESTAMP NOT NULL,
    activo INTEGER DEFAULT 1,
    periodo_id INTEGER
);

CREATE TABLE IF NOT EXISTS dias_especiales (
    id SERIAL PRIMARY KEY,
    fecha DATE NOT NULL,
    descripcion TEXT,
    turno_id INTEGER REFERENCES turnos(id),
    hora_entrada TIME,
    activo INTEGER DEFAULT 1,
    tipo TEXT DEFAULT 'evento' CHECK(tipo IN ('evento','feriado')),
    periodo_id INTEGER,
    contar_como_clases INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS dias_especiales_secciones (
    id SERIAL PRIMARY KEY,
    dia_especial_id INTEGER NOT NULL REFERENCES dias_especiales(id) ON DELETE CASCADE,
    seccion_id INTEGER NOT NULL REFERENCES secciones(id),
    UNIQUE(dia_especial_id, seccion_id)
);

CREATE TABLE IF NOT EXISTS usuarios (
    id SERIAL PRIMARY KEY,
    usuario TEXT UNIQUE NOT NULL,
    password TEXT NOT NULL,
    rol TEXT NOT NULL CHECK(rol IN ('Admin','Direccion','Auxiliar')),
    nombres TEXT NOT NULL,
    turno_asignado INTEGER REFERENCES turnos(id),
    activo INTEGER DEFAULT 1,
    intentos_fallidos INTEGER DEFAULT 0,
    bloqueado_hasta TIMESTAMP,
    debe_cambiar_password INTEGER DEFAULT 0,
    ultimo_login TIMESTAMP,
    ultimo_ip TEXT,
    es_principal INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS auxiliar_secciones (
    id SERIAL PRIMARY KEY,
    usuario_id INTEGER NOT NULL REFERENCES usuarios(id) ON DELETE CASCADE,
    seccion_id INTEGER NOT NULL REFERENCES secciones(id) ON DELETE CASCADE,
    UNIQUE(seccion_id)
);

CREATE TABLE IF NOT EXISTS auditoria (
    id SERIAL PRIMARY KEY,
    usuario TEXT,
    accion TEXT,
    fecha TIMESTAMP,
    valor_anterior TEXT,
    valor_nuevo TEXT,
    tabla_afectada TEXT,
    registro_id INTEGER,
    ip TEXT
);

CREATE TABLE IF NOT EXISTS cierres_anuales (
    id SERIAL PRIMARY KEY,
    periodo_id INTEGER NOT NULL REFERENCES periodos(id),
    fecha_cierre TIMESTAMP NOT NULL,
    generado_por TEXT,
    reporte_json TEXT
);

CREATE TABLE IF NOT EXISTS config (
    id SERIAL PRIMARY KEY,
    clave TEXT UNIQUE NOT NULL,
    valor TEXT
);

CREATE INDEX IF NOT EXISTS idx_ast_f ON asistencias(fecha);
CREATE INDEX IF NOT EXISTS idx_ast_a ON asistencias(alumno_id);
CREATE INDEX IF NOT EXISTS idx_tard_a ON tardanzas(alumno_id);
CREATE INDEX IF NOT EXISTS idx_tard_f ON tardanzas(fecha);
CREATE INDEX IF NOT EXISTS idx_al_dni ON alumnos(dni);
CREATE INDEX IF NOT EXISTS idx_al_sec ON alumnos(seccion_id);
CREATE INDEX IF NOT EXISTS idx_vent_t ON ventanas(turno_id,tipo);
CREATE INDEX IF NOT EXISTS idx_bloq_a ON bloqueos(alumno_id,activo);
CREATE INDEX IF NOT EXISTS idx_aud_f ON auditoria(fecha);
"""


def aplicar_schema():
    """Ejecuta todo el schema (idempotente)."""
    with cursor(dict_rows=False) as (con, cur):
        cur.execute(SCHEMA_SQL)
    log.info("schema aplicado")