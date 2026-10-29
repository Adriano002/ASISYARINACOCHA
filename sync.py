# sync.py
import boto3
import os
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
# 1. SUBIR BACKUP A R2 (Guarda con fecha y también sobrescribe el fijo)
# ══════════════════════════════════════════════════════════════════
def subir_bd(ruta_local):
    try:
        s3 = _get_r2_client()
        
        # 1. GUARDAMOS CON FECHA Y HORA (Historial)
        fecha_str = (datetime.now(timezone.utc) - timedelta(hours=5)).strftime("%Y-%m-%d_%H-%M")
        nombre_archivo_fecha = f"asistencia_{fecha_str}.db"
        s3.upload_file(ruta_local, BUCKET_NAME, nombre_archivo_fecha)
        
        # 2. GUARDAMOS CON NOMBRE FIJO (Para que el sistema inicie rápido)
        s3.upload_file(ruta_local, BUCKET_NAME, "asistencia.db")
        
        print(f"Backup exitoso: {nombre_archivo_fecha} y asistencia.db")
        
    except Exception as e:
        print(f"Error subiendo a R2: {e}")
        raise e

# ══════════════════════════════════════════════════════════════════
# 2. LISTAR BACKUPS DISPONIBLES EN R2
# ══════════════════════════════════════════════════════════════════
def listar_backups_r2():
    try:
        s3 = _get_r2_client()
        
        response = s3.list_objects_v2(Bucket=BUCKET_NAME, Prefix="asistencia_")
        
        backups = []
        if 'Contents' in response:
            for obj in response['Contents']:
                key = obj['Key']
                # Solo los que tienen fecha, NO el "asistencia.db" fijo
                if key.startswith("asistencia_") and key.endswith(".db") and key != "asistencia.db":
                    fecha_str = key.replace("asistencia_", "").replace(".db", "")
                    try:
                        fecha_obj = datetime.strptime(fecha_str, "%Y-%m-%d_%H-%M")
                        backups.append({"key": key, "fecha": fecha_obj})
                    except ValueError:
                        pass
                        
        # Ordenamos del más reciente al más antiguo
        backups.sort(key=lambda x: x["fecha"], reverse=True)
        return backups
        
    except Exception as e:
        print(f"Error listando backups: {e}")
        return []

# ══════════════════════════════════════════════════════════════════
# 3. DESCARGAR UN BACKUP ESPECÍFICO (Restauración)
# ══════════════════════════════════════════════════════════════════
def descargar_bd_especifica(key, ruta_local):
    try:
        s3 = _get_r2_client()
        s3.download_file(BUCKET_NAME, key, ruta_local)
        print(f"Base de datos {key} descargada a {ruta_local}")
        return True
    except Exception as e:
        print(f"Error descargando backup: {e}")
        return False

# ══════════════════════════════════════════════════════════════════
# 4. LIMPIAR BACKUPS ANTIGUOS EN R2 (Retención)
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
# 5. DESCARGAR LA BD FIJA (Para el inicio de la app)
# ══════════════════════════════════════════════════════════════════
def descargar_bd_si_no_existe(ruta_local="asistencia.db"):
    if os.path.exists(ruta_local):
        return True
    try:
        s3 = _get_r2_client()
        s3.download_file(BUCKET_NAME, "asistencia.db", ruta_local)
        print("BD fija descargada de R2 al iniciar.")
        return True
    except Exception as e:
        print(f"No se pudo descargar la BD inicial: {e}")
        return False
