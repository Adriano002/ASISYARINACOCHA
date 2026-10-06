# db.py
import os
import sqlite3
import threading
import logging
import streamlit as st
from pathlib import Path
from contextlib import contextmanager
from datetime import datetime, date, time

log = logging.getLogger("db")

# ─── CONFIGURACIÓN ─────────────────────────────────────────────────────────
DB_PATH = Path("data/asistencia.db")
DB_PATH.parent.mkdir(parents=True, exist_ok=True)

_local = threading.local()
_escrituras_contador = 0
UMBRAL_SYNC = 20


# ─── CONVERSIÓN %s → ? PARA SQLITE ─────────────────────────────────────────
def _q(sql: str) -> str:
    return sql.replace("%s", "?")


# ─── CONEXIÓN ──────────────────────────────────────────────────────────────
def obtener_conexion():
    con = getattr(_local, "con", None)
    if con is None:
        con = sqlite3.connect(
            str(DB_PATH),
            timeout=30,
            check_same_thread=False,
            isolation_level=None,
        )
        con.row_factory = sqlite3.Row
        cur = con.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.execute("PRAGMA busy_timeout=5000")
        cur.execute("PRAGMA foreign_keys=ON")
        _local.con = con
    return con


def liberar_conexion(con):
    try:
        if con.in_transaction:
            con.commit()
    except Exception:
        pass


@contextmanager
def cursor(dict_rows=True):
    con = obtener_conexion()
    cur = con.cursor()
    orig_execute = cur.execute
    orig_executemany = cur.executemany

    def execute(sql, params=()):
        return orig_execute(_q(sql), params)

    def executemany(sql, seq):
        return orig_executemany(_q(sql), seq)

    cur.execute = execute
    cur.executemany = executemany

    try:
        yield con, cur
    except Exception:
        try:
            con.rollback()
        except Exception:
            pass
        raise
    finally:
        try:
            if con.in_transaction:
                con.commit()
        except Exception:
            pass


def escribir(sql, params=()):
    global _escrituras_contador
    with cursor() as (con, cur):
        cur.execute(sql, params)
        con.commit()

    _escrituras_contador += 1
    if _escrituras_contador >= UMBRAL_SYNC:
        _escrituras_contador = 0
        try:
            from sync import subir_bd
            subir_bd(DB_PATH)
        except Exception as e:
            log.debug(f"Sync auto falló: {e}")


def leer_df(sql, params=(), con=None):
    import pandas as pd
    con_local = con or obtener_conexion()
    cur = con_local.cursor()
    cur.execute(_q(sql), params)
    filas = cur.fetchall()
    if not filas:
        cols = [d[0] for d in cur.description] if cur.description else []
        return pd.DataFrame(columns=cols)
    return pd.DataFrame([dict(r) for r in filas])


# ─── HELPERS DE CONVERSIÓN ─────────────────────────────────────────────────
def to_time(v):
    if v is None or v == "":
        return None
    if isinstance(v, time):
        return v.strftime("%H:%M")
    if isinstance(v, datetime):
        return v.strftime("%H:%M")
    return str(v)[:5]


def to_date(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def to_ts(v):
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)[:19]


def fmt_time(v, con_seg=False):
    if v is None:
        return ""
    if isinstance(v, time):
        return v.strftime("%H:%M:%S" if con_seg else "%H:%M")
    if isinstance(v, datetime):
        return v.strftime("%H:%M:%S" if con_seg else "%H:%M")
    s = str(v)
    return s[:8] if con_seg else (s[:5] if len(s) >= 5 else s)


def fmt_date(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d")
    if isinstance(v, date):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def fmt_ts(v):
    if v is None:
        return ""
    if isinstance(v, datetime):
        return v.strftime("%Y-%m-%d %H:%M:%S")
    return str(v)[:19]


def dict_fechas(d: dict, campos_time=(), campos_date=(), campos_ts=()) -> dict:
    out = dict(d)
    for k in campos_time:
        if k in out:
            out[k] = fmt_time(out[k])
    for k in campos_date:
        if k in out:
            out[k] = fmt_date(out[k])
    for k in campos_ts:
        if k in out:
            out[k] = fmt_ts(out[k])
    return out


# ─── PING / RESET ──────────────────────────────────────────────────────────
def ping():
    try:
        with cursor() as (con, cur):
            cur.execute("SELECT 1")
        return True
    except Exception as e:
        log.warning(f"ping fallo: {e}")
        reset_pool()
        try:
            with cursor() as (con, cur):
                cur.execute("SELECT 1")
            return True
        except Exception:
            return False


def reset_pool():
    con = getattr(_local, "con", None)
    if con is not None:
        try:
            con.close()
        except Exception:
            pass
        _local.con = None


# ─── DESCARGA INICIAL DESDE R2 ─────────────────────────────────────────────
def descargar_bd_si_no_existe():
    if DB_PATH.exists() and DB_PATH.stat().st_size > 0:
        return True
    try:
        from sync import descargar_bd
        ok = descargar_bd(DB_PATH)
        if ok:
            log.info("BD descargada desde R2")
            reset_pool()
        return ok
    except Exception as e:
        log.warning(f"No se pudo descargar BD desde R2: {e}")
        return False


def subir_bd_a_r2():
    try:
        from sync import subir_bd
        return subir_bd(DB_PATH)
    except Exception as e:
        log.error(f"Error subiendo BD a R2: {e}")
        return False