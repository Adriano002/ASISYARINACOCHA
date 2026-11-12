# sync.py
import boto3
import os
import sqlite3
import streamlit as st
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from botocore.config import Config

# ══════════════════════════════════════════════════════════════════
# CREDENCIALES
# Se leen de st.secrets (donde Streamlit esté corriendo: Cloud, VPS, etc.)
# NO están escritas acá. Si faltan, la app avisa claro.
# ══════════════════════════════════════════════════════════════════
try:
    R2_ENDPOINT   = st.secrets["r2"]["endpoint"]
    R2_ACCESS_KEY = st.secrets["r2"]["access_key"]
    R2_SECRET_KEY = st.secrets["r2"]["secret_key"]
    BUCKET_NAME   = st.secrets["r2"]["bucket"]
except Exception as e:
    raise RuntimeError(
        "Faltan las credenciales de R2 en st.secrets. "
        "Configurá [r2] con endpoint, access_key, secret_key y bucket "
        "en el panel de Secrets de Streamlit Cloud (o en .streamlit/secrets.toml si es local)."
    ) from e


def _get_r2_client():
    # Reintentos automáticos por si R2 tiene un hipo de red o rate limit
    return boto3.client(
        's3',
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY,
        config=Config(retries={'max_attempts': 3, 'mode': 'standard'})
    )


# ══════════════════════════════════════════════════════════════════
# FORZAR CHECKPOINT DEL WAL ANTES DE SUBIR (PASSIVE = NO bloquea)
# ══════════════════════════════════════════════════════════════════
def _forzar_checkpoint(ruta_local):
    try:
        con = sqlite3.connect(str(ruta_local), timeout=10)
        con.execute("PRAGMA wal_checkpoint(PASSIVE);")
        con.commit()
        con.close()
    except Exception as e:
        print(f"Advertencia checkpoint WAL: {e}")


# ══════════════════════════════════════════════════════════════════
# 1. SUBIR BACKUP A R2
# ══════════════════════════════════════════════════════════════════
def subir_bd(ruta_local):
    ruta_local = str(ruta_local)
    try:
        _forzar_checkpoint(ruta_local)

        s3 = _get_r2_client()

        # Hora de Perú explícita (no restar 5 a mano)
        fecha_str = datetime.now(ZoneInfo("America/Lima")).strftime("%Y-%m-%d_%H-%M")
        nombre_archivo_fecha = f"asistencia_{fecha_str}.db"
        s3.upload_file(ruta_local, BUCKET_NAME, nombre_archivo_fecha)

        # Copia "última versión" siempre con el mismo nombre
        s3.upload_file(ruta_local, BUCKET_NAME, "asistencia.db")

        print(f"Backup exitoso: {nombre_archivo_fecha} y asistencia.db")

    except Exception as e:
        print(f"Error subiendo a R2: {e}")
        raise e


# ══════════════════════════════════════════════════════════════════
# 2. LISTAR BACKUPS EN R2 (paginado: no se corta en 1000)
# ══════════════════════════════════════════════════════════════════
def listar_backups_r2():
    try:
        s3 = _get_r2_client()

        backups = []
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix="asistencia_"):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.startswith("asistencia_") and key.endswith(".db") and key != "asistencia.db":
                    fecha_str = key.replace("asistencia_", "").replace(".db", "")
                    try:
                        fecha_obj = datetime.strptime(fecha_str, "%Y-%m-%d_%H-%M")
                        backups.append({"key": key, "fecha": fecha_obj})
                    except ValueError:
                        pass

        backups.sort(key=lambda x: x["fecha"], reverse=True)
        return backups

    except Exception as e:
        print(f"Error listando backups: {e}")
        return []


# ══════════════════════════════════════════════════════════════════
# 3. DESCARGAR UN BACKUP ESPECÍFICO
# ══════════════════════════════════════════════════════════════════
def descargar_bd_especifica(key, ruta_local):
    ruta_local = str(ruta_local)
    try:
        s3 = _get_r2_client()
        s3.download_file(BUCKET_NAME, key, ruta_local)
        print(f"Base de datos {key} descargada a {ruta_local}")
        return True
    except Exception as e:
        print(f"Error descargando backup: {e}")
        return False


# ══════════════════════════════════════════════════════════════════
# 4. LIMPIAR BACKUPS ANTIGUOS EN R2 (paginado también)
# ══════════════════════════════════════════════════════════════════
def limpiar_backups_antiguos_r2(meses_a_mantener):
    try:
        s3 = _get_r2_client()

        fecha_limite = datetime.now(timezone.utc) - timedelta(days=meses_a_mantener * 30)
        borrados = 0

        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=BUCKET_NAME, Prefix="asistencia_"):
            for obj in page.get('Contents', []):
                key = obj['Key']
                if key.startswith("asistencia_") and key.endswith(".db") and key != "asistencia.db":
                    fecha_str = key.replace("asistencia_", "").replace(".db", "")
                    try:
                        fecha_obj = datetime.strptime(fecha_str, "%Y-%m-%d_%H-%M").replace(tzinfo=timezone.utc)
                        if fecha_obj < fecha_limite:
                            s3.delete_object(Bucket=BUCKET_NAME, Key=key)
                            borrados += 1
                    except ValueError:
                        pass

        return borrados
    except Exception as e:
        print(f"Error limpiando backups: {e}")
        return -1


# ══════════════════════════════════════════════════════════════════
# 5. DESCARGAR LA BD AL INICIAR (PROTEGIDO contra sobrescrituras)
# ══════════════════════════════════════════════════════════════════
def descargar_bd_si_no_existe(ruta_local="data/asistencia.db"):
    """
    Al arrancar descarga R2 SOLO si el local NO es más reciente.
    Nunca sobrescribe datos nuevos con datos viejos.
    Si la BD descargada está corrupta, restaura el .bak y avisa.
    """
    ruta_local = str(ruta_local)
    try:
        s3 = _get_r2_client()

        # 🛡️ PROTECCIÓN: Si el local existe y es más reciente que R2, NO descargar
        if os.path.exists(ruta_local) and os.path.getsize(ruta_local) > 50_000:
            try:
                mtime_local = os.path.getmtime(ruta_local)
                obj = s3.head_object(Bucket=BUCKET_NAME, Key="asistencia.db")
                mtime_r2 = obj["LastModified"].timestamp()

                if mtime_local > mtime_r2:
                    print("✅ Local más reciente que R2. NO descargar (protección activa).")
                    return True
            except Exception as e:
                print(f"No se pudo comparar fechas: {e}")

        # Crear carpeta si no existe
        parent = os.path.dirname(ruta_local)
        if parent and not os.path.exists(parent):
            os.makedirs(parent, exist_ok=True)

        if os.path.exists(ruta_local):
            try:
                os.replace(ruta_local, ruta_local + ".bak")
            except Exception:
                pass

        s3.download_file(BUCKET_NAME, "asistencia.db", ruta_local)
        print(f"✅ BD descargada de R2 a {ruta_local}")

        # 🛡️ Verificar que lo descargado no esté corrupto
        try:
            con = sqlite3.connect(ruta_local, timeout=10)
            ok = con.execute("PRAGMA integrity_check;").fetchone()[0]
            con.close()
            if ok != "ok":
                raise Exception(f"BD descargada falló integrity_check: {ok}")
        except Exception as e:
            print(f"⚠️ {e}. Restaurando .bak...")
            if os.path.exists(ruta_local + ".bak"):
                try:
                    os.replace(ruta_local + ".bak", ruta_local)
                    print("↩️ Restaurado backup local como fallback.")
                except Exception:
                    pass
            return False

        # Todo bien: borramos el .bak
        try:
            if os.path.exists(ruta_local + ".bak"):
                os.remove(ruta_local + ".bak")
        except Exception:
            pass

        return True

    except Exception as e:
        print(f"⚠️ No se pudo descargar BD de R2: {e}")
        if os.path.exists(ruta_local + ".bak"):
            try:
                os.replace(ruta_local + ".bak", ruta_local)
                print("↩️ Restaurado backup local como fallback.")
            except Exception:
                pass
        return False
