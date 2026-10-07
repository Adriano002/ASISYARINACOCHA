# sincronizacion con cloudflare
import logging
import streamlit as st
from pathlib import Path
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
    """Descarga la base de datos local"""
    try:
        cliente = _client()
        bucket = st.secrets["r2"]["bucket"]
        key = st.secrets["r2"].get("key", "asistencia.db")
        destino.parent.mkdir(parents=True, exist_ok=True)
        cliente.download_file(bucket, key, str(destino))
        log.info(f"BD descargada: {destino} ({destino.stat().st_size} bytes)")
        return True
    except Exception as e:
        log.warning(f"descargar_bd: {e}")
        return False


def subir_bd(origen: Path):
    """Sube la BD local a R2."""
    try:
        if not origen.exists():
            log.warning("No existe BD local para subir")
            return False
        cliente = _client()
        bucket = st.secrets["r2"]["bucket"]
        key = st.secrets["r2"].get("key", "asistencia.db")
        cliente.upload_file(str(origen), bucket, key)
        log.info(f"BD subida a R2: {origen.stat().st_size} bytes")
        return True
    except Exception as e:
        log.error(f"subir_bd: {e}")
        return False


def boton_backup():
    """SUBIR ASISYARINA MANUALMENTE"""
    import streamlit as st
    from db import DB_PATH, reset_pool

    st.markdown("### Backup manual")

    if DB_PATH.exists():
        tam = DB_PATH.stat().st_size / 1024
        st.success(f"BD local: {DB_PATH} ({tam:.1f} KB)")
    else:
        st.error("No hay Bade de Datos local")

    c1, c2 = st.columns(2)

    with c1:
        if st.button("Subir base de datos al almacenamiento", type="primary", width="stretch"):
            with st.spinner("Subiendo..."):
                if subir_bd(DB_PATH):
                    st.success("Asisyarinasubido correctamente")
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

    if DB_PATH.exists():
        with open(DB_PATH, "rb") as f:
            st.download_button(
                "📥 Descargar BD local (.db)",
                f.read(),
                file_name="asistencia_backup.db",
                mime="application/octet-stream",
                width="stretch",
            )
