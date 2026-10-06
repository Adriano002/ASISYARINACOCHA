"""Sincronizacion con Cloudflare R2."""
import logging
import streamlit as st

from db import subir_bd_a_r2, info_ultimo_backup

log = logging.getLogger("asistencia.sync")


def boton_backup():
    """UI para backup manual a R2."""
    st.markdown("### Guardar backup en la nube")
    st.caption("Sube la base de datos actual a Cloudflare R2. "
               "Se recomienda hacerlo al final del día.")

    if st.button("Guardar backup ahora", type="primary"):
        with st.spinner("Subiendo a R2..."):
            ok, msg = subir_bd_a_r2()
        if ok:
            st.success(f"✅ {msg}")
        else:
            st.error(f"❌ Error: {msg}")

    st.markdown("---")
    st.markdown("### Ultimo backup en la nube")
    info = info_ultimo_backup()
    if info:
        st.write(f"**Fecha**: {info['fecha']}")
        st.write(f"**Tamaño**: {info['tamano']:,} bytes")
    else:
        st.info("No hay backups previos en R2.")