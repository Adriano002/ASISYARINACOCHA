# sync.py - Sincronización con Cloudflare R2
import logging
from pathlib import Path

import streamlit as st

log = logging.getLogger("sync")


def _client():
    """Crea cliente boto3 apuntando a Cloudflare R2."""
    import boto3
    from botocore.config import Config

    cfg = Config(
        signature_version="s3v4",
        retries={"max_attempts": 3, "mode": "standard"},
    )
    return boto3.client(
        "s3",
        endpoint_url=st.secrets["r2"]["endpoint"],
        aws_access_key_id=st.secrets["r2"]["access_key"],
        aws_secret_access_key=st.secrets["r2"]["secret_key"],
        region_name="auto",
        config=cfg,
    )


def descargar_bd(destino: Path):
    """Descarga la base de datos desde R2."""
    try:
        cliente = _client()
        bucket = st.secrets["r2"]["bucket"]
        # Nombre fijo original
        key = "asistencia.db"
        destino.parent.mkdir(parents=True, exist_ok=True)
        cliente.download_file(bucket, key, str(destino))
        log.info("BD descargada: %s (%s bytes)", destino, destino.stat().st_size)
        return True
    except Exception as e:
        log.warning("descargar_bd: %s", e)
        return False


def subir_bd(origen: Path):
    """Sube la BD local a R2."""
    try:
        if not origen.exists():
            log.warning("No existe BD local para subir")
            return False
        cliente = _client()
        bucket = st.secrets["r2"]["bucket"]
        # Nombre fijo original
        key = "asistencia.db"
        cliente.upload_file(str(origen), bucket, key)
        log.info("BD subida a R2: %s bytes", origen.stat().st_size)
        return True
    except Exception as e:
        log.error("subir_bd: %s", e)
        return False


def boton_backup():
    """Backup manual: subir / descargar desde R2."""
    from db import DB_PATH, reset_pool

    st.markdown("### Backup manual")

    if DB_PATH.exists():
        tam = DB_PATH.stat().st_size / 1024
        st.success(f"BD local: {DB_PATH.name} ({tam:.1f} KB)")
    else:
        st.error("No hay base de datos local.")

    c1, c2 = st.columns(2)

    with c1:
        if st.button("⬆️ Subir base de datos al almacenamiento",
                     type="primary", width="stretch"):
            with st.spinner("Subiendo..."):
                if subir_bd(DB_PATH):
                    st.success("Base de datos subida correctamente.")
                else:
                    st.error("Error al subir. Revisa los logs.")

    with c2:
        if st.button("⬇️ Descargar BD desde R2", width="stretch"):
            with st.spinner("Descargando..."):
                reset_pool()
                if descargar_bd(DB_PATH):
                    reset_pool()
                    st.success("BD descargada. Recarga la página.")
                else:
                    st.error("Error al descargar.")

    st.markdown("---")
    st.caption(
        "⚠️ En Streamlit Cloud el filesystem es efímero. "
        "Haz backup periódicamente o al terminar la jornada."
    )
