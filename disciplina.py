# ============================================================
# disciplina.py
# Módulo completo de Gestión de Disciplina Escolar
# D.S. 8801855-2026-MINEDU (Lineamientos de disciplina)
# Con escáner QR para DNI
# ============================================================
import sqlite3
from datetime import date
import pandas as pd
import streamlit as st

DB_PATH = "asisyarinacocha.db"  # ⚠️ cambia por tu ruta real


def _conn():
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


# ============================================================
# COMPONENTE QR SCANNER
# ============================================================
QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v18",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <div id="qr-reader"></div>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
    </div>
    """,
    css="""
    #qr-wrapper { width: 100%; max-width: 340px; margin: 0 auto; }
    #qr-reader {
        border-radius: 12px; overflow: hidden;
        border: 3px solid #E65100; background: #000;
        height: 320px; min-height: 320px;
        display: flex; align-items: center; justify-content: center;
    }
    #qr-reader video {
        border-radius: 8px;
        width: 100% !important; height: 100% !important;
        object-fit: cover;
    }
    #qr-status {
        text-align: center; font-size: 13px; margin-top: 8px;
        color: #666;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    #qr-error {
        text-align: center; font-size: 13px; margin-top: 8px;
        color: #C62828; font-weight: 600;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        padding: 10px; border: 1px solid #C62828; border-radius: 6px;
        background: #f8d7da;
    }
    """,
    js="""
    export default function(component) {
        const { setTriggerValue } = component;
        let scanner = null;
        let iniciado = false;
        let pausado = false;
        let isRestarting = false;
        const COOLDOWN_MS = 5000;
        let ultimoDniEmitido = null;
        let ultimoTimestampEmision = 0;
        let audioCtx = null;

        function getAudioCtx() {
            if (!audioCtx) {
                try { audioCtx = new (window.AudioContext || window.webkitAudioContext)(); } catch (e) {}
            }
            if (audioCtx && audioCtx.state === 'suspended') audioCtx.resume().catch(() => {});
            return audioCtx;
        }
        function _tono(freq, dur, tipo, vol, delay) {
            const ctx = getAudioCtx(); if (!ctx) return;
            const t0 = ctx.currentTime + (delay || 0);
            const osc = ctx.createOscillator(); const g = ctx.createGain();
            osc.connect(g); g.connect(ctx.destination);
            osc.type = tipo || 'sine';
            osc.frequency.setValueAtTime(freq, t0);
            g.gain.setValueAtTime(0, t0);
            g.gain.linearRampToValueAtTime(vol || 0.35, t0 + 0.015);
            g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
            osc.start(t0); osc.stop(t0 + dur + 0.02);
        }
        function sonidoClick() { _tono(880, 0.05, 'sine', 0.25, 0); }
        function sonidoPuntual() { _tono(523,0.10,'sine',0.40,0); _tono(659,0.10,'sine',0.40,0.10); _tono(784,0.15,'sine',0.40,0.20); }
        function sonidoTardanza() { _tono(440,0.30,'sine',0.35,0); }
        function sonidoDuplicado() { _tono(220,0.18,'square',0.45,0); _tono(220,0.18,'square',0.45,0.22); }
        function sonidoError() { _tono(180,0.15,'sawtooth',0.45,0); _tono(250,0.15,'sawtooth',0.45,0.15); _tono(330,0.15,'sawtooth',0.45,0.30); _tono(440,0.25,'sawtooth',0.45,0.45); }
        function sonidoBloqueado() { _tono(160,0.15,'sawtooth',0.45,0); _tono(120,0.15,'sawtooth',0.45,0.18); _tono(90,0.30,'sawtooth',0.45,0.36); }
        function reproducir(kind) {
            try {
                getAudioCtx();
                switch (kind) {
                    case "click": sonidoClick(); break;
                    case "puntual": sonidoPuntual(); break;
                    case "tardanza": sonidoTardanza(); break;
                    case "duplicado": sonidoDuplicado(); break;
                    case "bloqueado": sonidoBloqueado(); break;
                    case "error": default: sonidoError(); break;
                }
            } catch (e) {}
        }
        window.__qrFeedback = reproducir;
        try { window.parent.__qrFeedback = reproducir; } catch (e) {}

        function setStatus(t) { const el = document.getElementById('qr-status'); if (el) { el.textContent = t; el.style.display = 'block'; } }
        function setError(t) {
            const e = document.getElementById('qr-error'); const s = document.getElementById('qr-status');
            if (e) { e.textContent = t; e.style.display = 'block'; }
            if (s) s.style.display = 'none';
        }
        function destruirScanner() {
            pausado = false;
            if (scanner) { try { scanner.stop().then(() => scanner.clear()).catch(()=>{}); } catch (e) {} scanner = null; }
            iniciado = false; ultimoDniEmitido = null; ultimoTimestampEmision = 0;
        }
        function iniciarScanner() {
            if (iniciado || isRestarting) return;
            if (typeof Html5Qrcode === 'undefined') { setError('Libreria QR no cargada.'); return; }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) { setError('Tu navegador no soporta acceso a camara, o no estas en HTTPS.'); return; }
            const reader = document.getElementById('qr-reader');
            if (!reader) { setError('Contenedor qr-reader no existe.'); return; }
            destruirScanner();
            reader.innerHTML = '';
            iniciado = true;
            getAudioCtx();
            setTimeout(() => {
                if (!iniciado) return;
                try {
                    scanner = new Html5Qrcode("qr-reader", { verbose: false });
                    scanner.start(
                        { facingMode: "environment" },
                        { fps: 20, qrbox: { width: 250, height: 250 } },
                        (texto) => {
                            try {
                                const m = texto.match(/\\b(\\d{8})\\b/);
                                if (!m) return;
                                const dni = m[1];
                                const ahora = Date.now();
                                const esMismoDni = (dni === ultimoDniEmitido);
                                const dentroCooldown = (ahora - ultimoTimestampEmision) < COOLDOWN_MS;
                                if (esMismoDni && dentroCooldown) return;
                                ultimoDniEmitido = dni; ultimoTimestampEmision = ahora;
                                try { getAudioCtx(); _tono(880, 0.05, 'sine', 0.25, 0); } catch(e) {}
                                setStatus('QR: ' + dni);
                                setTriggerValue("qr_dni", dni);
                            } catch (e) {}
                        },
                        () => {}
                    ).catch((e) => {
                        const msg = (e && e.message) ? e.message : String(e);
                        setError('Error camara: ' + msg);
                        iniciado = false;
                    });
                    setStatus('Camara activa.');
                } catch (e) {
                    const msg = (e && e.message) ? e.message : String(e);
                    setError('Error al iniciar: ' + msg);
                    iniciado = false;
                }
            }, 500);
        }
        function pausarScanner() {
            if (!scanner || !iniciado || pausado) return;
            try { scanner.pause(true); pausado = true; setStatus('Camara en pausa.'); } catch (e) { destruirScanner(); }
        }
        function reanudarScanner() {
            if (!scanner || !iniciado) { iniciarScanner(); return; }
            try { scanner.resume(); pausado = false; setStatus('Camara activa.'); } catch (e) { destruirScanner(); iniciarScanner(); }
        }
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') pausarScanner();
            else if (document.visibilityState === 'visible') reanudarScanner();
        });
        window.addEventListener('blur', () => { if (document.visibilityState === 'hidden') pausarScanner(); });
        window.addEventListener('focus', () => { if (document.visibilityState === 'visible') reanudarScanner(); });
        window.addEventListener('error', function(e) {
            if (e.message && (e.message.includes('NotReadableError') || e.message.includes('Could not start video source'))) {
                if (isRestarting) return;
                isRestarting = true;
                setStatus('Reiniciando cámara (ocupada)...');
                destruirScanner();
                setTimeout(() => { isRestarting = false; iniciarScanner(); }, 1500);
            }
        }, true);
        window.addEventListener('beforeunload', destruirScanner);
        if (window.Html5Qrcode) { iniciarScanner(); return; }
        if (window.__qrV18Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (window.Html5Qrcode) {
                    clearInterval(t); window.__qrV18Cargando = false; iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t); window.__qrV18Cargando = false; setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV18Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js';
        s.async = true;
        s.onload = () => {
            window.__qrV18Cargando = false;
            setTimeout(() => {
                if (typeof Html5Qrcode === 'undefined') { setError('Libreria cargada pero sin Html5Qrcode.'); return; }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => { window.__qrV18Cargando = false; setError('Error al cargar html5-qrcode del CDN.'); };
        document.head.appendChild(s);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None):
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
    )


# ============================================================
# CATÁLOGO DE FALTAS
# ============================================================
FALTAS_LEVES = {
    "incumplir_normas": "Incumplir normas de convivencia del RI",
    "higiene_presentacion": "Falta de higiene/presentación (Art. 6.7)",
    "tardanza_menor_3": "Tardanza injustificada (< 3 por bimestre)",
}

FALTAS_GRAVES = {
    "danio_propiedad_escolar": "Deteriorar mobiliario/materiales sin vandalismo",
    "reincidencia_leves": "3+ faltas leves en el año",
    "uso_indebido_celulares": "Uso de celular/dispositivos sin autorización",
    "tardanza_reiterada": "Más de 3 tardanzas injustificadas por bimestre",
    "agresion_fisica_leve": "Empujones/forcejeos sin lesión",
    "agresion_psicologica": "Cualquier agresión psicológica",
    "sustancias_prohibidas": "Posesión/consumo sin comercialización",
    "justificativos_adulterados": "Justificativos con firma/contenido falso",
    "vulnera_intimidad": "Amenazas, humillación, divulgación de datos",
    "incitacion_conductas_peligrosas": "Retos, apuestas, conductas virales peligrosas",
    "atentar_imagen_institucional": "Afectar imagen de la IIEE",
}

FALTAS_MUY_GRAVES = {
    "odio_apologia_violencia": "Símbolos/discursos de odio o apología",
    "discriminacion": "Trato desigual por cualquier motivo",
    "connotacion_sexual": "Acoso o violencia sexual",
    "acoso_escolar": "Bullying/ciberbullying con afectación",
    "alteracion_orden": "Boicot, toma de instalaciones",
    "danio_intencional": "Sabotaje/destrucción de infraestructura",
    "extorsion_cupos": "Cobro ilegal de cupos",
    "falsa_denuncia": "Difamación u obstrucción docente",
    "falsificacion_documentos": "Alterar documentos oficiales",
    "uso_info_privada": "Difusión de datos sensibles",
    "fraude_tecnologico": "Hackeo/suplantación en evaluaciones",
    "captacion_delictiva": "Organizar apuestas/pandillaje",
    "comercializacion_sustancias": "Vender/facilitar drogas o alcohol",
    "portar_armas": "Armas u objetos punzocortantes",
    "suplantacion_identidad": "Perfiles falsos en redes",
    "artefactos_explosivos": "Bombas molotov, fuegos artificiales",
}


# ============================================================
# HELPERS
# ============================================================
def clasificar_gravedad(categoria: str) -> str:
    if categoria in FALTAS_GRAVES: return "grave"
    if categoria in FALTAS_MUY_GRAVES: return "muy_grave"
    return "leve"


def nota_conducta_por_faltas(n_leves, n_graves, n_muy_graves) -> int:
    nota = 20 - (n_leves * 1) - (n_graves * 3) - (n_muy_graves * 6)
    return max(0, min(20, nota))


# ============================================================
# CRUD
# ============================================================
def db_registrar_falta(dni, categoria, descripcion, registrado_por):
    tipo = clasificar_gravedad(categoria)
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO faltas
               (dni_estudiante, tipo, categoria, descripcion, registrado_por)
               VALUES (?,?,?,?,?)""",
            (dni, tipo, categoria, descripcion, registrado_por),
        )
        falta_id = cur.lastrowid
        conn.commit()
    return falta_id, tipo


def db_faltas_por_estudiante(dni):
    with _conn() as conn:
        return pd.read_sql_query(
            """SELECT id, tipo, categoria, fecha, descripcion, estado
               FROM faltas WHERE dni_estudiante=? ORDER BY fecha DESC""",
            conn, params=(dni,),
        )


def db_guardar_nota_conducta(dni, periodo, nota, descripcion):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO notas_conducta (dni_estudiante, periodo, nota, descripcion)
               VALUES (?,?,?,?)
               ON CONFLICT(dni_estudiante, periodo)
               DO UPDATE SET nota=excluded.nota,
                             descripcion=excluded.descripcion,
                             actualizado_en=CURRENT_TIMESTAMP""",
            (dni, periodo, nota, descripcion),
        )
        conn.commit()


def db_notas_por_estudiante(dni):
    with _conn() as conn:
        return pd.read_sql_query(
            """SELECT periodo, nota, descripcion, actualizado_en
               FROM notas_conducta WHERE dni_estudiante=? ORDER BY periodo""",
            conn, params=(dni,),
        )


def db_registrar_medida(falta_id, tipo, detalle, aplicada_por):
    with _conn() as conn:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO medidas (falta_id, tipo, detalle, aplicada_por)
               VALUES (?,?,?,?)""",
            (falta_id, tipo, detalle, aplicada_por),
        )
        conn.commit()


def db_medidas_por_falta(falta_id):
    with _conn() as conn:
        return pd.read_sql_query(
            """SELECT tipo, detalle, fecha_inicio, fecha_fin, aplicada_por
               FROM medidas WHERE falta_id=? ORDER BY fecha_inicio DESC""",
            conn, params=(falta_id,),
        )


def resumen_estudiante(dni):
    df = db_faltas_por_estudiante(dni)
    n_leves = int((df.tipo == "leve").sum()) if not df.empty else 0
    n_graves = int((df.tipo == "grave").sum()) if not df.empty else 0
    n_muy = int((df.tipo == "muy_grave").sum()) if not df.empty else 0
    return {
        "leves": n_leves,
        "graves": n_graves,
        "muy_graves": n_muy,
        "nota_sugerida": nota_conducta_por_faltas(n_leves, n_graves, n_muy),
        "reincidente_leves": n_leves >= 3,
        "requiere_direccion": (n_graves + n_muy) >= 1,
    }


# ============================================================
# VISTA: REGISTRAR FALTA (con QR scanner)
# ============================================================
def vista_registrar_falta():
    st.markdown("### ➕ Registrar falta")

    # ---- Estado para el DNI escaneado ----
    if "disc_dni_escaneado" not in st.session_state:
        st.session_state.disc_dni_escaneado = ""

    # ---- Cámara QR ----
    with st.expander("📷 Escanear DNI con cámara", expanded=True):
        def _on_qr_scan():
            dni_qr = st.session_state.get("qr_dni")
            if dni_qr:
                st.session_state.disc_dni_escaneado = dni_qr

        qr_scanner(key="disciplina_qr", on_scan=_on_qr_scan)

    # ---- Formulario ----
    with st.form("form_falta", clear_on_submit=True):
        c1, c2 = st.columns(2)
        dni = c1.text_input("DNI del estudiante", value=st.session_state.disc_dni_escaneado)
        registrado_por = c2.text_input("Registrado por (docente/auxiliar)")

        grupo = st.radio("Gravedad", ["Leve", "Grave", "Muy grave"], horizontal=True)
        if grupo == "Leve":
            opciones = FALTAS_LEVES
        elif grupo == "Grave":
            opciones = FALTAS_GRAVES
        else:
            opciones = FALTAS_MUY_GRAVES

        categoria = st.selectbox(
            "Categoría",
            options=list(opciones.keys()),
            format_func=lambda k: f"{k} — {opciones[k]}",
        )
        descripcion = st.text_area("Descripción de los hechos")
        ok = st.form_submit_button("Registrar falta", type="primary")

    if ok:
        if not dni or not registrado_por:
            st.error("Completa DNI y quién registra.")
        else:
            falta_id, tipo = db_registrar_falta(dni, categoria, descripcion, registrado_por)
            st.success(f"✅ Falta registrada (ID {falta_id}) · Gravedad: **{tipo.upper()}**")
            if tipo != "leve":
                st.warning("⚠️ **Art. 6.3:** Comunicar de inmediato a Dirección.")
            st.session_state.disc_dni_escaneado = ""


# ============================================================
# VISTA: PANEL DEL ESTUDIANTE (con QR scanner)
# ============================================================
def vista_panel_estudiante():
    st.markdown("### 📊 Panel del estudiante")

    if "disc_dni_buscar" not in st.session_state:
        st.session_state.disc_dni_buscar = ""

    with st.expander("📷 Escanear DNI con cámara", expanded=False):
        def _on_qr_scan():
            dni_qr = st.session_state.get("qr_dni_panel")
            if dni_qr:
                st.session_state.disc_dni_buscar = dni_qr

        qr_scanner(key="disciplina_qr_panel", on_scan=_on_qr_scan)

    dni_buscar = st.text_input(
        "DNI del estudiante",
        value=st.session_state.disc_dni_buscar,
        key="disc_buscar_input",
    )
    if not dni_buscar:
        return
    df = db_faltas_por_estudiante(dni_buscar)
    if df.empty:
        st.info("Sin faltas registradas para este DNI.")
        return
    st.dataframe(df, use_container_width=True)
    info = resumen_estudiante(dni_buscar)
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Leves", info["leves"])
    c2.metric("Graves", info["graves"])
    c3.metric("Muy graves", info["muy_graves"])
    c4.metric("Nota sugerida", info["nota_sugerida"])
    if info["reincidente_leves"]:
        st.error("🔁 **Reincidencia (§7.3.1.b):** 3+ faltas leves → se tipifica como GRAVE.")
    if info["requiere_direccion"]:
        st.warning("⚖️ Aplicar medidas correctivas y/o de protección (§10).")


# ============================================================
# VISTA: NOTAS DE CONDUCTA
# ============================================================
def vista_notas_conducta():
    st.markdown("### 📈 Notas de conducta")
    c1, c2, c3 = st.columns(3)
    dni_n = c1.text_input("DNI", key="nota_dni")
    periodo = c2.selectbox("Periodo", ["B1", "B2", "B3", "B4"], key="nota_per")
    nota = c3.number_input("Nota (0-20)", 0, 20, 15, key="nota_val")
    descripcion = st.text_area(
        "Descripción cualitativa (obligatoria si nota < 11)", key="nota_desc"
    )

    colA, colB = st.columns(2)
    if colA.button("💾 Guardar nota", type="primary"):
        if not dni_n:
            st.error("Ingresa el DNI.")
        elif nota < 11 and not descripcion.strip():
            st.error("**Art. 5.7:** Nota desaprobatoria exige descripción cualitativa.")
        else:
            db_guardar_nota_conducta(dni_n, periodo, nota, descripcion)
            st.success("✅ Nota de conducta actualizada.")

    if colB.button("📋 Ver notas del estudiante"):
        if dni_n:
            st.dataframe(db_notas_por_estudiante(dni_n), use_container_width=True)


# ============================================================
# VISTA: MEDIDAS
# ============================================================
def vista_medidas():
    st.markdown("### ⚖️ Medidas")
    c1, c2 = st.columns(2)
    falta_id = c1.number_input("ID de falta", min_value=1, step=1)
    tipo_medida = c2.selectbox(
        "Tipo de medida",
        ["correctiva", "proteccion", "sancionadora"],
        help="Art. 5.9 (correctiva), 5.8 (protección), 5.10 (sancionadora)",
    )
    detalle = st.text_area("Detalle de la medida")
    aplicada_por = st.text_input("Aplicada por")

    if st.button("Registrar medida", type="primary"):
        if not detalle.strip():
            st.error("El detalle es obligatorio.")
        else:
            db_registrar_medida(falta_id, tipo_medida, detalle, aplicada_por)
            st.success("✅ Medida registrada.")
            st.dataframe(db_medidas_por_falta(falta_id), use_container_width=True)


# ============================================================
# VISTA PRINCIPAL
# ============================================================
def render_disciplina():
    st.markdown("## 📋 Gestión de Disciplina Escolar")
    st.caption("D.S. 8801855-2026-MINEDU · Restitución del Principio de Autoridad Docente")
    st.info(
        "ℹ️ **Art. 6.5:** Este módulo NO constituye procedimiento administrativo sancionador. "
        "Las medidas son de naturaleza educativa y formativa."
    )
    tab1, tab2, tab3, tab4 = st.tabs(
        ["➕ Registrar falta", "📊 Panel del estudiante", "📈 Notas de conducta", "⚖️ Medidas"]
    )
    with tab1:
        vista_registrar_falta()
    with tab2:
        vista_panel_estudiante()
    with tab3:
        vista_notas_conducta()
    with tab4:
        vista_medidas()


# ============================================================
# MAIN
# ============================================================
if __name__ == "__main__":
    st.set_page_config(page_title="Disciplina Escolar", page_icon="📋", layout="wide")
    render_disciplina()
