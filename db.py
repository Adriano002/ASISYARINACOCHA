# db.py
# Este archivo se encarga de hablar con la base de datos SQLite.
# Aquí abrimos la conexión, escribimos, leemos y cerramos.
# Todo está documentado como si le explicaras a un amigo.

import sqlite3
import queue
import threading
import streamlit as st
from pathlib import Path


# La base de datos vive en data/asistencia.db.
# Si la carpeta no existe, la creamos.
RUTA_BASE_DATOS = Path("data/asistencia.db")
RUTA_BASE_DATOS.parent.mkdir(parents=True, exist_ok=True)


# ─────────────────────────────────────────────────────────────
# CONEXIÓN A LA BASE DE DATOS
# ─────────────────────────────────────────────────────────────

@st.cache_resource
def obtener_conexion():
    """
    Abre la conexión a SQLite una sola vez y la reutiliza.
    El decorador @st.cache_resource hace que Streamlit guarde
    esta conexión en memoria y no la vuelva a abrir en cada
    interacción del usuario. Esto ahorra mucha RAM.
    """
    conexion = sqlite3.connect(
        str(RUTA_BASE_DATOS),
        timeout=30,
        check_same_thread=False,  # Necesario para compartir entre hilos
        isolation_level=None,
    )
    conexion.row_factory = sqlite3.Row

    # WAL permite leer mientras se escribe. Muy importante para
    # que el escaneo no se bloquee cuando hay varias personas.
    conexion.execute("PRAGMA journal_mode=WAL")
    conexion.execute("PRAGMA synchronous=NORMAL")
    conexion.execute("PRAGMA busy_timeout=5000")
    conexion.execute("PRAGMA foreign_keys=ON")
    return conexion


# ─────────────────────────────────────────────────────────────
# LECTURA (SELECT)
# ─────────────────────────────────────────────────────────────

def leer_dataframe(consulta, parametros=()):
    """
    Ejecuta una consulta SELECT y devuelve un DataFrame de pandas.
    Si no hay resultados, devuelve un DataFrame vacío.
    """
    import pandas as pd
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    cursor.execute(consulta, parametros)
    filas = cursor.fetchall()
    if not filas:
        return pd.DataFrame()
    return pd.DataFrame([dict(fila) for fila in filas])


def leer_uno(consulta, parametros=()):
    """
    Ejecuta una consulta SELECT y devuelve la primera fila
    como diccionario, o None si no hay resultados.
    """
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    cursor.execute(consulta, parametros)
    fila = cursor.fetchone()
    return dict(fila) if fila else None


def leer_varios(consulta, parametros=()):
    """
    Ejecuta una consulta SELECT y devuelve todas las filas
    como una lista de diccionarios.
    """
    conexion = obtener_conexion()
    cursor = conexion.cursor()
    cursor.execute(consulta, parametros)
    return [dict(fila) for fila in cursor.fetchall()]

# ESCRITURA (INSERT, UPDATE, DELETE) — CON COLA

#
# ¿Por qué una cola?
# Porque SQLite no permite que dos personas escriban al mismo
# tiempo. Si 6 auxiliares escanean a la vez, las escrituras
# chocan y la base de datos se bloquea ("database is locked").
#
# La solución es una cola: cada escritura se pone en una fila
# y un solo trabajador las procesa una por una. Es como la
# fila del banco: todos esperan su turno, pero nadie choca.
#
# El escaneo NO espera a que la escritura termine. Solo la
# encola y sigue. Por eso el escaneo es rápido.

COLA_ESCRITURA = queue.Queue()


def _trabajador_escrituras():
    """
    Este es el trabajador que procesa la cola.
    Se ejecuta en un hilo aparte, en segundo plano.
    """
    while True:
        consulta, parametros = COLA_ESCRITURA.get()
        try:
            conexion = obtener_conexion()
            conexion.execute(consulta, parametros)
            conexion.commit()
        except Exception as error:
            print(f"Error al escribir en la base de datos: {error}")
        finally:
            COLA_ESCRITURA.task_done()


# Iniciamos el trabajador una sola vez cuando se carga el módulo.
# El parámetro daemon=True hace que el hilo se cierre cuando
# la app se cierra.
HILO_ESCRITURA = threading.Thread(
    target=_trabajador_escrituras,
    daemon=True
)
HILO_ESCRITURA.start()


def escribir_seguro(consulta, parametros=()):
    """
    Encola una escritura (INSERT, UPDATE, DELETE).
    No espera a que termine. Es rápido.
    Úsalo en el flujo de escaneo.
    """
    COLA_ESCRITURA.put((consulta, parametros))


def escribir_y_esperar(consulta, parametros=()):
    """
    Encola una escritura y ESPERA a que termine.
    Devuelve el lastrowid (el ID del registro insertado).
    Úsalo solo cuando necesites el ID del nuevo registro
    (por ejemplo, al crear una incidencia y luego asociarle
    decomisos). NO lo uses en el flujo de escaneo.
    """
    conexion = obtener_conexion()
    cursor = conexion.execute(consulta, parametros)
    conexion.commit()
    return cursor.lastrowid


# ─────────────────────────────────────────────────────────────
# FUNCIONES DE FECHA Y HORA
# ─────────────────────────────────────────────────────────────

def obtener_fecha_hora_actual():
    """
    Devuelve la fecha y hora actual en hora de Perú (UTC-5).
    Uso timezone.utc porque así es como Streamlit Cloud
    maneja la hora internamente.
    """
    from datetime import datetime, timedelta, timezone
    return datetime.now(timezone.utc) - timedelta(hours=5)


def obtener_fecha_actual():
    """Devuelve solo la fecha de hoy (YYYY-MM-DD) en hora de Perú."""
    return obtener_fecha_hora_actual().strftime("%Y-%m-%d")


def obtener_hora_actual():
    """Devuelve solo la hora actual (HH:MM) en hora de Perú."""
    return obtener_fecha_hora_actual().strftime("%H:%M")


def obtener_timestamp_actual():
    """Devuelve fecha y hora completas (YYYY-MM-DD HH:MM:SS)."""
    return obtener_fecha_hora_actual().strftime("%Y-%m-%d %H:%M:%S")


# ─────────────────────────────────────────────────────────────
# FORMATO DE FECHAS Y HORAS (para mostrar en la UI)
# ─────────────────────────────────────────────────────────────

def formatear_fecha(valor):
    """Convierte una fecha a texto YYYY-MM-DD."""
    if valor is None:
        return ""
    from datetime import datetime, date
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d")
    if isinstance(valor, date):
        return valor.strftime("%Y-%m-%d")
    return str(valor)[:10]


def formatear_hora(valor):
    """Convierte una hora a texto HH:MM."""
    if valor is None:
        return ""
    from datetime import datetime, time
    if isinstance(valor, datetime):
        return valor.strftime("%H:%M")
    if isinstance(valor, time):
        return valor.strftime("%H:%M")
    texto = str(valor)
    return texto[:5] if len(texto) >= 5 else texto


def formatear_timestamp(valor):
    """Convierte un timestamp a texto YYYY-MM-DD HH:MM:SS."""
    if valor is None:
        return ""
    from datetime import datetime
    if isinstance(valor, datetime):
        return valor.strftime("%Y-%m-%d %H:%M:%S")
    return str(valor)[:19]
