# qr_scanner_component.py
# v20: html5-qrcode (robusto, nunca se cae) + boton pausar + click inmediato.
# Basado en el v18 que funcionaba bien, con mejoras de UI del v55.
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v20",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <div id="qr-header">
            <div id="qr-titulo">Escaneo QR</div>
            <button id="qr-toggle" type="button">⏸ Pausar</button>
        </div>
        <div id="qr-reader"></div>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
    </div>
    """,
    css="""
    #qr-wrapper {
        width: 100%;
        max-width: 500px;
        margin: 0 auto;
    }
    #qr-header {
        display: flex;
        align-items: center;
        justify-content: space-between;
        margin-bottom: 8px;
        gap: 10px;
    }
    #qr-titulo {
        font-size: 15px;
        font-weight: 700;
        color: #E65100;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }
    #qr-toggle {
        background: #E65100;
        color: #FFF;
        border: none;
        border-radius: 8px;
        padding: 8px 14px;
        font-size: 13px;
        font-weight: 700;
        cursor: pointer;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
        transition: background 0.15s ease;
    }
    #qr-toggle:hover { background: #BF360C; }
    #qr-toggle.pausado { background: #22C55E; }
    #qr-toggle.pausado:hover { background: #16A34A; }
    #qr-reader {
        border-radius: 8px;
        overflow: hidden;
        border: 2px solid #E65100;
        background: #000;
        min-height: 260px;
    }
    #qr-reader video {
        border-radius: 6px;
        width: 100% !important;
        height: auto !important;
    }
    #qr-reader.pausado {
        opacity: 0.4;
        filter: grayscale(100%);
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
        padding: 10px;
        border: 1px solid #C62828;
        border-radius: 6px;
        background: #f8d7da;
    }
    /* Estilizar botones de html5-qrcode */
    #qr-reader button {
        background: #E65100 !important;
        color: white !important;
        border: none !important;
        border-radius: 6px !important;
        padding: 6px 12px !important;
        font-weight: 600 !important;
        cursor: pointer !important;
        margin: 4px !important;
        font-size: 12px !important;
    }
    #qr-reader button:hover { background: #BF360C !important; }
    #qr-reader select {
        border-radius: 6px !important;
        padding: 5px 8px !important;
        margin: 4px !important;
        border: 1px solid #ccc !important;
        font-size: 12px !important;
    }
    #qr-reader a { color: #E65100 !important; font-weight: 600 !important; }
    """,
    js="""
    export default function(component) {
        const { setTriggerValue } = component;
        let scanner = null;
        let iniciado = false;
        let pausado = false;
        let intervaloReset = null;

        // ═══ EDGE DETECTION ═══
        const COOLDOWN_MS = 1500;
        let ultimoDniEmitido = null;
        let ultimoTimestampEmision = 0;

        // ═══ AUDIO ═══
        let audioCtx = null;
        function getAudioCtx() {
            if (!audioCtx) {
                try {
                    audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                } catch (e) { console.warn('[QR] AudioContext fallo:', e); }
            }
            if (audioCtx && audioCtx.state === 'suspended') {
                audioCtx.resume().catch(() => {});
            }
            return audioCtx;
        }

        function _tono(freq, dur, tipo, vol, delay) {
            const ctx = getAudioCtx();
            if (!ctx) return;
            try {
                const t0 = ctx.currentTime + (delay || 0);
                const osc = ctx.createOscillator();
                const g = ctx.createGain();
                osc.connect(g); g.connect(ctx.destination);
                osc.type = tipo || 'sine';
                osc.frequency.setValueAtTime(freq, t0);
                g.gain.setValueAtTime(0, t0);
                g.gain.linearRampToValueAtTime(vol || 0.35, t0 + 0.015);
                g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
                osc.start(t0); osc.stop(t0 + dur + 0.02);
            } catch(e) {}
        }

        // Click corto al leer QR (feedback inmediato)
        function sonidoClick() { _tono(880, 0.05, 'sine', 0.25, 0); }

        // Sonidos de respuesta (vienen de Python via sonido_kind)
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
            } catch (e) {
                console.error('[QR] reproducir error:', e);
            }
        }

        window.__qrFeedback = reproducir;
        try { window.parent.__qrFeedback = reproducir; } catch (e) {}

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
            console.error('[QR]', t);
        }
        function limpiarError() {
            const e = document.getElementById('qr-error');
            if (e) e.style.display = 'none';
        }

        // ═══ BOTÓN PAUSAR ═══
        function actualizarBoton() {
            const btn = document.getElementById('qr-toggle');
            const reader = document.getElementById('qr-reader');
            if (!btn) return;
            if (pausado) {
                btn.textContent = '▶ Activar camara';
                btn.classList.add('pausado');
                if (reader) reader.classList.add('pausado');
            } else {
                btn.textContent = '⏸ Pausar';
                btn.classList.remove('pausado');
                if (reader) reader.classList.remove('pausado');
            }
        }

        function togglePausa() {
            pausado = !pausado;
            actualizarBoton();
            if (pausado) {
                pausarScanner();
                setStatus('Camara pausada.');
            } else {
                reanudarScanner();
            }
        }

        // ═══ DESTRUIR / PAUSAR / REANUDAR ═══
        function destruirScanner() {
            pausado = false;
            if (scanner) {
                try { scanner.clear(); } catch (e) {}
                scanner = null;
            }
            iniciado = false;
            ultimoDniEmitido = null;
            ultimoTimestampEmision = 0;
        }

        function pausarScanner() {
            if (!scanner || !iniciado) {
                pausado = true;
                return;
            }
            try {
                scanner.pause(true);
                pausado = true;
                setStatus('Camara en pausa.');
            } catch (e) {
                console.warn('[QR] pause fallo:', e);
                destruirScanner();
            }
        }

        function reanudarScanner() {
            pausado = false;
            if (!scanner || !iniciado) {
                iniciarScanner();
                return;
            }
            try {
                scanner.resume();
                setStatus('Camara activa.');
            } catch (e) {
                console.warn('[QR] resume fallo, reiniciando:', e);
                destruirScanner();
                iniciarScanner();
            }
        }

        // ═══ SCANNER ═══
        function iniciarScanner() {
            if (iniciado) return;
            if (typeof Html5QrcodeScanner === 'undefined') {
                setError('Libreria QR no cargada.');
                return;
            }
            if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
                setError('Navegador sin acceso a camara o no estas en HTTPS.');
                return;
            }
            const reader = document.getElementById('qr-reader');
            if (!reader) { setError('Contenedor no existe.'); return; }

            destruirScanner();
            reader.innerHTML = '';
            iniciado = true;
            limpiarError();
            getAudioCtx();

            setTimeout(() => {
                if (!iniciado) return;

                scanner = new Html5QrcodeScanner(
                    "qr-reader",
                    {
                        fps: 15,
                        qrbox: { width: 250, height: 250 },
                        aspectRatio: 1.0,
                        rememberLastUsedCamera: true,
                        videoConstraints: { facingMode: "environment" },
                        supportedScanTypes: [Html5QrcodeScanType.SCAN_TYPE_CAMERA]
                    },
                    false
                );

                const onScanSuccess = (texto) => {
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

                        // ═══ CLICK INMEDIATO ═══
                        reproducir("click");
                        setStatus('QR: ' + dni);
                        setTriggerValue("qr_dni", dni);
                    } catch (e) {
                        console.error('[QR] onScanSuccess:', e);
                    }
                };

                const onScanError = () => {};

                let resultado;
                try {
                    resultado = scanner.render(onScanSuccess, onScanError);
                } catch (e) {
                    setError('Error al iniciar: ' + (e.message || e));
                    iniciado = false;
                    return;
                }

                if (resultado && typeof resultado.then === 'function') {
                    resultado
                        .then(() => setStatus('Camara activa.'))
                        .catch((e) => {
                            setError('Error camara: ' + (e.message || e));
                            iniciado = false;
                        });
                } else {
                    setStatus('Camara activa.');
                }
            }, 500);
        }

        // ═══ VISIBILITY ═══
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                pausarScanner();
            } else if (document.visibilityState === 'visible') {
                if (!pausado) reanudarScanner();
            }
        });
        window.addEventListener('blur', () => {
            if (document.visibilityState === 'hidden') pausarScanner();
        });
        window.addEventListener('focus', () => {
            if (document.visibilityState === 'visible' && !pausado) reanudarScanner();
        });
        window.addEventListener('beforeunload', destruirScanner);

        // ═══ AUTO-RESET cada 5 min para prevenir cuelgues ═══
        let ultimoReset = Date.now();
        intervaloReset = setInterval(() => {
            if (!iniciado || pausado) return;
            const ahora = Date.now();
            if ((ahora - ultimoReset) >= 5 * 60 * 1000) {
                ultimoReset = ahora;
                console.log('[QR] Auto-reset preventivo');
                try {
                    destruirScanner();
                    setTimeout(() => iniciarScanner(), 500);
                } catch(e) {}
            }
        }, 30000);

        // ═══ CARGA DE LIBRERIA ═══
        if (window.__qrV20Listo && typeof Html5QrcodeScanner !== 'undefined') {
            iniciarScanner();
            return;
        }
        if (window.__qrV20Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (typeof Html5QrcodeScanner !== 'undefined') {
                    clearInterval(t);
                    window.__qrV20Listo = true;
                    window.__qrV20Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {
                    clearInterval(t);
                    window.__qrV20Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }
        window.__qrV20Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode';
        s.async = true;
        s.onload = () => {
            window.__qrV20Listo = true;
            window.__qrV20Cargando = false;
            setTimeout(() => {
                if (typeof Html5QrcodeScanner === 'undefined') {
                    setError('Libreria cargada sin Html5QrcodeScanner.');
                    return;
                }
                iniciarScanner();
            }, 50);
        };
        s.onerror = () => {
            window.__qrV20Cargando = false;
            setError('Error cargando html5-qrcode del CDN.');
        };
        document.head.appendChild(s);

        // Botón pausar
        setTimeout(() => {
            const btn = document.getElementById('qr-toggle');
            if (btn) btn.addEventListener('click', togglePausa);
            actualizarBoton();
        }, 500);
    }
    """,
)


def qr_scanner(key="qr_scanner", on_scan=None, sonido_kind="", sonido_nonce=0):
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
        data={
            "sonido_kind": sonido_kind,
            "sonido_nonce": sonido_nonce,
        },
    )
