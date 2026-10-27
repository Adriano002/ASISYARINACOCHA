# sync.py
# Sincronización con Cloudflare R2.
# R2 es como un disco duro en la nube donde guardamos la base de datos.
# Sirve para que si Streamlit Cloud borra el archivo local,
# podamos recuperar la base de datos desde R2.

import logging
from pathlib import Path

import streamlit as st

log = logging.getLogger("sync")


def _crear_cliente_r2():
    """
    Crea el cliente de boto3 apuntando a Cloudflare R2.
    R2 usa el mismo protocolo que Amazon S3, por eso usamos boto3.
    Los datos de conexión (endpoint, access_key, secret_key, bucket)
    están guardados en los secrets de Streamlit.
    """
    import boto3
    from botocore.config import Config

    configuracion = Config(
        signature_version="s3v4",
        retries={"max_attempts": 3, "mode": "standard"},
    )
    return boto3.client(
        "s3",
        endpoint_url=st.secrets["r2"]["endpoint"],
        aws_access_key_id=st.secrets["r2"]["access_key"],
        aws_secret_access_key=st.secrets["r2"]["secret_key"],
        region_name="auto",
        config=configuracion,
    )


def descargar_bd(destino):
    """
    Descarga la base de datos desde R2 hacia el disco local.
    Devuelve True si se descargó bien, False si algo falló.
    """
    try:
        cliente = _crear_cliente_r2()
        bucket = st.secrets["r2"]["bucket"]
        clave = "asistencia.db"

        destino = Path(destino)
        destino.parent.mkdir(parents=True, exist_ok=True)
        cliente.download_file(bucket, clave, str(destino))

        log.info("Base de datos descargada desde R2: %s", destino)
        return True
    except Exception as error:
        log.warning("No se pudo descargar la base de datos: %s", error)
        return False


def subir_bd(origen):
    """
    Sube la base de datos local hacia R2.
    Devuelve True si se subió bien, False si algo falló.
    """
    try:
        origen = Path(origen)
        if not origen.exists():
            log.warning("No existe la base de datos local para subir.")
            return False

        cliente = _crear_cliente_r2()
        bucket = st.secrets["r2"]["bucket"]
        clave = "asistencia.db"

        cliente.upload_file(str(origen), bucket, clave)

        log.info("Base de datos subida a R2: %s", origen)
        return True
    except Exception as error:
        log.error("No se pudo subir la base de datos: %s", error)
        return False


def boton_backup():
    """
    Muestra la interfaz de backup manual en Streamlit.
    El usuario puede subir o descargar la base de datos desde R2.
    """
    from db import RUTA_BASE_DATOS

    st.markdown("### Backup manual")

    # Mostrar info de la base de datos local
    if RUTA_BASE_DATOS.exists():
        tamano_kb = RUTA_BASE_DATOS.stat().st_size / 1024
        st.success(f"Base de datos local: {RUTA_BASE_DATOS.name} "
                   f"({tamano_kb:.1f} KB)")
    else:
        st.error("No hay base de datos local.")

    columna1, columna2 = st.columns(2)

    with columna1:
        if st.button("⬆️ Subir base de datos a R2",
                     type="primary", use_container_width=True):
            with st.spinner("Subiendo..."):
                if subir_bd(RUTA_BASE_DATOS):
                    st.success("Base de datos subida correctamente.")
                else:
                    st.error("Error al subir. Revisa los logs.")

    with columna2:
        if st.button("Descargar base de datos desde R2",
                     use_container_width=True):
            with st.spinner("Descargando..."):
                if descargar_bd(RUTA_BASE_DATOS):
                    st.success("Base de datos descargada. Recarga la página.")
                else:
                    st.error("Error al descargar.")

    st.markdown("---")
    st.caption(
        "En Streamlit Cloud el disco es temporal: cada vez que la app "
        "se reinicia, el archivo local se borra. Haz backup periódicamente "
        "para no perder datos."
    )
