import streamlit as st

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
    #qr-wrapper {
        width: 100%;
        max-width: 340px;
        margin: 0 auto;
    }
    #qr-reader {
        border-radius: 12px;
        overflow: hidden;
        border: 3px solid #E65100;
        background: #000;
        height: 300px; /* 🆕 Altura perfecta: ni muy grande ni muy chica */
        min-height: 300px;
        display: flex;
        align-items: center;
        justify-content: center;
    }
    #qr-reader video {
        border-radius: 8px;
        width: 100% !important;
        height: 100% !important;
        object-fit: cover;
    }
    #qr-status {
        text-align: center;
        font-size: 13px;
        margin-top: 8px;
        color: #666;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    #qr-error {
        text-align: center;
        font-size: 13px;
        margin-top: 8px;
        color: #C62828;
        font-weight: 600;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        padding: 10px;
        border: 1px solid #C62828;
        border-radius: 6px;
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

        // ═══ AUDIO ═══
        let audioCtx = null;
        function getAudioCtx() {
            if (!audioCtx) {
                try {
                    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                } catch (e) {}
            }
            if (audioCtx && audioCtx.state === 'suspended') {
                audioCtx.resume().catch(() => {});
            }
            return audioCtx;
        }

        function _tono(freq, dur, tipo, vol, delay) {
            const ctx = getAudioCtx();
            if (!ctx) return;
            const t0 = ctx.currentTime + (delay || 0);
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.connect(g); g.connect(ctx.destination);
            osc.type = tipo || 'sine';
            osc.frequency.setValueAtTime(freq, t0);
            g.gain.setValueAtTime(0, t0);
            g.gain.linearRampToValueAtTime(vol || 0.35, t0 + 0.015);
            g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
            osc.start(t0);
            osc.stop(t0 + dur + 0.02);
        }

        function sonidoClick() { _tono(880, 0.05, 'sine', 0.25, 0); }
        function sonidoPuntual() {
            _tono(523, 0.10, 'sine', 0.40, 0);
            _tono(659, 0.10, 'sine', 0.40, 0.10);
            _tono(784, 0.15, 'sine', 0.40, 0.20);
        }
        function sonidoTardanza() { _tono(440, 0.30, 'sine', 0.35, 0); }
        function sonidoDuplicado() {
            _tono(220, 0.18, 'square', 0.45, 0);
            _tono(220, 0.18, 'square', 0.45, 0.22);
        }
        function sonidoError() {
            _tono(180, 0.15, 'sawtooth', 0.45, 0);
            _tono(250, 0.15, 'sawtooth', 0.45, 0.15);
            _tono(330, 0.15, 'sawtooth', 0.45, 0.30);
            _tono(440, 0.25, 'sawtooth', 0.45, 0.45);
        }
        function sonidoBloqueado() {
            _tono(160, 0.15, 'sawtooth', 0.45, 0);
            _tono(120, 0.15, 'sawtooth', 0.45, 0.18);
            _tono(90, 0.30, 'sawtooth', 0.45, 0.36);
        }

        function reproducir(kind) {
            try {
                getAudioCtx();
                switch (kind) {
                    case "click":       sonidoClick();       break;
                    case "puntual":     sonidoPuntual();     break;
                    case "tardanza":    sonidoTardanza();    break;
                    case "duplicado":   sonidoDuplicado();   break;
                    case "bloqueado":   sonidoBloqueado();   break;
                    case "error":
                    default:            sonidoError();       break;
                }
            } catch (e) {}
        }

        window.__qrFeedback = reproducir;
        try {
            window.parent.__qrFeedback = reproducir;
        } catch (e) {}

        // ═══ UTILIDADES ═══
        function setStatus(t) {
            const el = document.getElementById('qr-status');
            if (el) { el.textContent = t; el.style.display = 'block'; }
        }
        function setError(t) {
            const e = document.getElementById('qr-error');
            const s = document.getElementById('qr-status');
            if (e) { e.textContent = t; e.style.display = 'block'; }
            if (s) s.style.display = 'none';
        }
        function destruirScanner() {
            pausado = false;
            if (scanner) {
                try { scanner.stop().then(() => scanner.clear()).catch(()=>{}); } catch (e) {}
                scanner = null;
            }
            iniciado = false;
            ultimoDniEmitido = null;
            ultimoTimestampEmision = 0;
        }

        // ═══ SCANNER ═══
        function iniciarScanner() {
            if (iniciado || isRestarting) return;
            if (typeof Html5Qrcode === 'undefined') {
                setError('Libreria QR no cargada.');
                return;
            }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                setError('Tu navegador no soporta acceso a camara, o no estas en HTTPS.');
                return;
            }
            const reader = document.getElementById('qr-reader');
            if (!reader) {
                setError('Contenedor qr-reader no existe.');
                return;
            }

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

                                ultimoDniEmitido = dni;
                                ultimoTimestampEmision = ahora;
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

        // ═══ VISIBILITY ═══
        function pausarScanner() {
            if (!scanner || !iniciado || pausado) return;
            try {
                scanner.pause(true);
                pausado = true;
                setStatus('Camara en pausa.');
            } catch (e) {
                destruirScanner();
            }
        }

        function reanudarScanner() {
            if (!scanner || !iniciado) {
                iniciarScanner();
                return;
            }
            try {
                scanner.resume();
                pausado = false;
                setStatus('Camara activa.');
            } catch (e) {
                destruirScanner();
                iniciarScanner();
            }
        }

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') pausarScanner();
            else if (document.visibilityState === 'visible') reanudarScanner();
        });

        window.addEventListener('blur', () => {
            if (document.visibilityState === 'hidden') pausarScanner();
        });
        window.addEventListener('focus', () => {
            if (document.visibilityState === 'visible') reanudarScanner();
        });

        // ═══ FIX PARA EL NotReadableError ═══
        window.addEventListener('error', function(e) {
            if (e.message && (e.message.includes('NotReadableError') || e.message.includes('Could not start video source'))) {
                if (isRestarting) return;
                isRestarting = true;
                setStatus('Reiniciando cámara (ocupada)...');
                destruirScanner();
                setTimeout(() => {
                    isRestarting = false;
                    iniciarScanner();
                }, 1500);
            }
        }, true);

        // ═══ CARGA DE LIBRERIA ═══
        window.addEventListener('beforeunload', destruirScanner);

        if (window.Html5Qrcode) {
            iniciarScanner();
            return;
        }
        if (window.__qrV18Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (window.Html5Qrcode) {
                    clearInterval(t);
                    window.__qrV18Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV18Cargando = false;
                    setError('Timeout cargando libreria.');
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
                if (typeof Html5Qrcode === 'undefined') {
                    setError('Libreria cargada pero sin Html5Qrcode.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrV18Cargando = false;
            setError('Error al cargar html5-qrcode del CDN.');
        };
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
