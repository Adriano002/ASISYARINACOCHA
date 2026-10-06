"""
Conexion PostgreSQL (Neon) con pool y helpers de conversion.
- Pool reutilizable (min=1, max=5).
- Recicla pool si Neon suspendio la conexion idle.
- Helpers `to_time`, `to_date`, `to_ts`, `fmt_*` para convertir
  entre tipos nativos de Postgres y str del codigo legacy.
"""
import logging
import os
import time as _time
from contextlib import contextmanager
from datetime import date, datetime, time as dtime

import pandas as pd
import psycopg2
import psycopg2.extras
from psycopg2.pool import SimpleConnectionPool

log = logging.getLogger("asistencia.db")

_pool: SimpleConnectionPool | None = None


# ─── CONFIG ────────────────────────────────────────────────────────────────
def _cfg():
    try:
        import streamlit as st
        s = st.secrets["neon"]
        return {
            "host": s["host"], "database": s["database"],
            "user": s["user"], "password": s["password"],
            "sslmode": s.get("sslmode", "require"),
        }
    except Exception:
        return {
            "host": os.getenv("PGHOST"),
            "database": os.getenv("PGDATABASE"),
            "user": os.getenv("PGUSER"),
            "password": os.getenv("PGPASSWORD"),
            "sslmode": os.getenv("PGSSLMODE", "require"),
        }


def _get_pool() -> SimpleConnectionPool:
    global _pool
    if _pool is None:
        c = _cfg()
        if not c["host"]:
            raise RuntimeError(
                "No hay configuracion de Neon. Crea .streamlit/secrets.toml "
                "con la seccion [neon] o define PGHOST/PGDATABASE/PGUSER/PGPASSWORD."
            )
        _pool = SimpleConnectionPool(
            minconn=1, maxconn=5,
            host=c["host"], database=c["database"],
            user=c["user"], password=c["password"],
            sslmode=c["sslmode"],
            connect_timeout=10,
        )
        log.info("Pool PostgreSQL creado -> %s/%s", c["host"], c["database"])
    return _pool


def reset_pool():
    """Cierra el pool. Util si Neon suspendio la conexion idle."""
    global _pool
    if _pool is not None:
        try:
            _pool.closeall()
        except Exception:
            pass
        _pool = None


# ─── API PUBLICA ───────────────────────────────────────────────────────────
def obtener_conexion():
    con = _get_pool().getconn()
    con.autocommit = False
    return con


def liberar_conexion(con):
    try:
        _get_pool().putconn(con)
    except Exception as e:
        log.warning("liberar_conexion: %s", e)


@contextmanager
def cursor(dict_rows=True):
    con = obtener_conexion()
    cur = con.cursor(
        cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None
    )
    try:
        yield con, cur
        con.commit()
    except psycopg2.OperationalError:
        con.rollback()
        cur.close()
        liberar_conexion(con)
        reset_pool()
        con = obtener_conexion()
        cur = con.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor if dict_rows else None
        )
        try:
            yield con, cur
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            cur.close()
            liberar_conexion(con)
        return
    except Exception:
        con.rollback()
        raise
    finally:
        try:
            cur.close()
            liberar_conexion(con)
        except Exception:
            pass


def escribir(sql, params=()):
    con = obtener_conexion()
    cur = con.cursor()
    try:
        cur.execute(sql, params)
        con.commit()
        return cur
    except Exception:
        con.rollback()
        raise
    finally:
        cur.close()
        liberar_conexion(con)


def leer_df(sql, params=(), con=None):
    con_local = con or obtener_conexion()
    try:
        return pd.read_sql(sql, con_local, params=params)
    finally:
        if con is None:
            liberar_conexion(con_local)


def ping():
    """Verifica que la conexion este viva. Si no, recicla el pool."""
    try:
        with cursor(dict_rows=False) as (con, cur):
            cur.execute("SELECT 1")
        return True
    except Exception as e:
        log.warning("ping fallo: %s -> reciclando pool", e)
        reset_pool()
        _time.sleep(0.3)
        try:
            with cursor(dict_rows=False) as (con, cur):
                cur.execute("SELECT 1")
            return True
        except Exception as e2:
            log.error("ping fallo tras reset: %s", e2)
            return False


# ─── HELPERS DE CONVERSION ─────────────────────────────────────────────────
def to_time(v) -> dtime | None:
    """Acepta time, 'HH:MM', 'HH:MM:SS' o None -> datetime.time."""
    if v is None or v == "":
        return None
    if isinstance(v, dtime):
        return v
    s = str(v).strip()
    parts = s.split(":")
    h = int(parts[0])
    m = int(parts[1]) if len(parts) > 1 else 0
    sec = int(parts[2]) if len(parts) > 2 else 0
    return dtime(h, m, sec)


def to_date(v) -> date | None:
    """Acepta date, datetime, 'YYYY-MM-DD' o None -> datetime.date."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    return datetime.strptime(str(v).strip(), "%Y-%m-%d").date()


def to_ts(v) -> datetime | None:
    """Acepta datetime, 'YYYY-MM-DD HH:MM:SS' o None -> datetime."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    s = str(v).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    raise ValueError(f"timestamp invalido: {v!r}")


def fmt_time(v, con_seg=False) -> str | None:
    """datetime.time -> 'HH:MM' (o 'HH:MM:SS')."""
    if v is None:
        return None
    if isinstance(v, dtime):
        return v.strftime("%H:%M:%S" if con_seg else "%H:%M")
    return str(v)[:8] if con_seg else str(v)[:5]


def fmt_date(v) -> str | None:
    """date -> 'YYYY-MM-DD'."""
    if v is None:
        return None
    if isinstance(v, (date, datetime)):
        return v.strftime("%Y-%m-%d")
    return str(v)[:10]


def fmt_ts(v) -> str | None:
    """datetime -> 'YYYY-MM-DD HH:MM:SS'."""
    if v is None:
        return None
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