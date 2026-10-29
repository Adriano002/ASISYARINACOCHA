# sync.py
import boto3
import os
import sqlite3
from datetime import datetime, timezone, timedelta

# ══════════════════════════════════════════════════════════════════
# CONFIGURACIÓN DIRECTA (Tus datos hardcodeados)
# ══════════════════════════════════════════════════════════════════
R2_ENDPOINT = "https://00a60225d4827f47d407b4e2357b9e73.r2.cloudflarestorage.com"
R2_ACCESS_KEY = "6510e1ad498b1879e4747acd846a3fbd"
R2_SECRET_KEY = "46bccd0959c4104b7e728267de533fe17f1baa0605028eb4b1960db0394cc1a7"
BUCKET_NAME = "asisyarina"


def _get_r2_client():
    return boto3.client(
        's3',
        endpoint_url=R2_ENDPOINT,
        aws_access_key_id=R2_ACCESS_KEY,
        aws_secret_access_key=R2_SECRET_KEY
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

        fecha_str = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d_%H-%M")
        nombre_archivo_fecha = f"asistencia_{fecha_str}.db"
        s3.upload_file(ruta_local, BUCKET_NAME, nombre_archivo_fecha)

        s3.upload_file(ruta_local, BUCKET_NAME, "asistencia.db")

        print(f"Backup exitoso: {nombre_archivo_fecha} y asistencia.db")

    except Exception as e:
        print(f"Error subiendo a R2: {e}")
        raise e


# ══════════════════════════════════════════════════════════════════
# 2. LISTAR BACKUPS EN R2
# ══════════════════════════════════════════════════════════════════
def listar_backups_r2():
    try:
        s3 = _get_r2_client()

        response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="asistencia_")

        backups = []
        if 'Contents' in response:
            for obj in response['Contents']:
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
# 4. LIMPIAR BACKUPS ANTIGUOS EN R2
# ══════════════════════════════════════════════════════════════════
def limpiar_backups_antiguos_r2(meses_a_mantener):
    try:
        s3 = _get_r2_client()

        fecha_limite = datetime.now(timezone.utc) - timedelta(days=meses_a_mantener * 30)

        response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="asistencia_")
        borrados = 0

        if 'Contents' in response:
            for obj in response['Contents']:
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
                    print(f"✅ Local más reciente que R2. NO descargar (protección activa).")
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
