# Sistema de Asistencia - I.E. Yarinacocha

Sistema de control de asistencia escolar con QR, reportes en PDF/Excel
y persistencia en PostgreSQL (Neon).

## Requisitos
- Python 3.10+
- Cuenta en [Neon](https://neon.tech)

## Instalación
1. Clonar el repo
2. `pip install -r requirements.txt`
3. Crear `.streamlit/secrets.toml` con las credenciales de Neon
4. `streamlit run app.py`

## Credenciales
Usuario inicial: `admin` (password temporal en `logs/app.log`)