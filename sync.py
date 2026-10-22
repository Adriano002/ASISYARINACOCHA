# sync.py
import logging
from pathlib import Path
import streamlit as st

# Importamos DB_PATH directamente de db.py para saber el nombre del archivo actual.
from db import DB_PATH, reset_pool

log = logging.getLogger("sync")

def _client():
    # ... (igual que antes) ...

def descargar_bd(destino: Path):
    try:
        cliente = _client()
        bucket = st.secrets["r2"]["bucket"]
        # Usa el nombre del archivo que se calculó en db.py
        key = destino.name  
        destino.parent.mkdir(parents=True, exist_ok=True)
        cliente.download_file(bucket, key, str(destino))
        log.info("BD descargada: %s", destino)
        return True
    except Exception as e:
        log.warning("descargar_bd: %s", e)
        return False

def subir_bd(origen: Path):
    try:
        if not origen.exists():
            return False
        cliente = _client()
        bucket = st.secrets["r2"]["bucket"]
        # Usa el nombre del archivo que se calculó en db.py
        key = origen.name
        cliente.upload_file(str(origen), bucket, key)
        log.info("BD subida a R2: %s", origen)
        return True
    except Exception as e:
        log.error("subir_bd: %s", e)
        return False

# ... el resto de sync.py (boton_backup) queda igual ...
