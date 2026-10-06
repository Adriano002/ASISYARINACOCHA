# qr_scanner_component.py
# Escaner QR optimizado: maxima velocidad + todas las funcionalidades v54.
# - Deteccion en vivo (200ms)
# - Click inmediato al leer QR
# - Boton pausar
# - Rotacion de AudioContext (audio estable)
# - Anti-rebote doble
# - Pausa al cambiar pestaña
import streamlit as st

QR_SCANNER_COMPONENT = st.components.v2.component(
    name="mi_qr_scanner_v55",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <div id="qr-header">
            <div id="qr-titulo">Escaneo QR</div>
            <button id="qr-toggle" type="button">⏸ Pausar</button>
        </div>
        <video id="qr-video" playsinline autoplay muted></video>
        <div id="qr-status">Iniciando camara...</div>
        <div id="qr-error" style="display:none;"></div>
    </div>
    """,
    css="""
    #qr-wrapper {
        width: 100%;
        max-width: 400px;
        margin: 0 auto;
        position: relative;
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
    #qr-video {
        border-radius: 10px;
        overflow: hidden;
        border: 2px solid #E65100;
        background: #000;
        width: 100%;
        height: 300px;
        object-fit: cover;
        display: block;
    }
    #qr-video.pausado {
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
    """,
    js="""
    export default function(component) {
        const { setTriggerValue, data } = component;

        const DNI_REGEX = /\\b(\\d{8})\\b/;
        const SCAN_INTERVAL_MS = 200;      // ⚡ OPTIMIZADO (era 500)
        const ANTI_REBOTE_MS = 1800;       // ⚡ OPTIMIZADO (era 3500)
        const ARRANQUE_ESPERA_MS = 2000;
        const SONIDO_CHECK_MS = 200;       // ⚡ OPTIMIZADO (era 150)

        // Persistencia por usuario (en sessionStorage del navegador)
        const KEY_PAUSADO = 'qr_scanner_pausado';

        let stream = null;
        let detector = null;
        let videoElement = null;
        let scanLoop = null;
        let sonidoLoop = null;
        let ultimoDni = null;
        let ultimoTimestampDni = 0;
        let iniciado = false;
        let polyfillCargado = false;
        let intentosFallidos = 0;
        let arranqueTimestamp = 0;
        let ultimoSonidoNonce = 0;
        let ultimoDniEnviado = null;
        let contadorFrameSinQR = 0;
        const MAX_INTENTOS = 5;
        const MAX_FRAMES_SIN_QR = 2;       // ⚡ OPTIMIZADO (era 3)

        // Estado de pausa (persistente por sesión)
        let pausado = sessionStorage.getItem(KEY_PAUSADO) === '1';

        // ══════════════════════════════════════════════════════
        // AUDIO - con rotación de AudioContext
        // ══════════════════════════════════════════════════════
        let audioCtx = null;
        let sonidosReproducidos = 0;
        const MAX_SONIDOS_POR_CTX = 40;
        const MIN_MS_ENTRE_SONIDOS = 150;  // ⚡ OPTIMIZADO (era 200)

        function crearAudioContext() {
            try {
                if (audioCtx && audioCtx.state !== 'closed') {
                    try { audioCtx.close(); } catch(e) {}
                }
                const Ctx = window.AudioContext || window.webkitAudioContext;
                if (!Ctx) return null;
                audioCtx = new Ctx();
                sonidosReproducidos = 0;
                return audioCtx;
            } catch(e) {
                audioCtx = null;
                return null;
            }
        }

        function getAudioCtx() {
            if (!audioCtx || audioCtx.state === 'closed') {
                return crearAudioContext();
            }
            return audioCtx;
        }

        function asegurarAudioActivo() {
            try {
                let ctx = getAudioCtx();
                if (!ctx) return null;
                if (sonidosReproducidos >= MAX_SONIDOS_POR_CTX) {
                    ctx = crearAudioContext();
                }
                if (!ctx) return null;
                if (ctx.state === 'suspended') {
                    ctx.resume().catch(() => {});
                    setTimeout(() => {
                        if (audioCtx && audioCtx.state !== 'running') {
                            crearAudioContext();
                        }
                    }, 50);
                }
                return ctx;
            } catch(e) {
                return crearAudioContext();
            }
        }

        function _tono(freq, dur, tipo, vol, delay) {
            const ctx = asegurarAudioActivo();
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
                osc.onended = () => {
                    try { osc.disconnect(); g.disconnect(); } catch(e) {}
                };
                sonidosReproducidos++;
            } catch(e) {}
        }

        function sonidoClick() {
            _tono(880, 0.05, 'sine', 0.25, 0);
        }
        function sonidoPuntual() {
            _tono(523, 0.10, 'sine', 0.40, 0);
            _tono(659, 0.10, 'sine', 0.40, 0.10);
            _tono(784, 0.15, 'sine', 0.40, 0.20);
        }
        function sonidoTardanza() {
            _tono(440, 0.15, 'sine', 0.40, 0);
            _tono(330, 0.25, 'sine', 0.40, 0.18);
        }
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

        function _reproducirAhora(kind) {
            try {
                switch (kind) {
                    case "puntual":     sonidoPuntual();     break;
                    case "tardanza":    sonidoTardanza();    break;
                    case "duplicado":   sonidoDuplicado();   break;
                    case "bloqueado":   sonidoBloqueado();   break;
                    case "click":       sonidoClick();       break;
                    case "error":
                    default:            sonidoError();       break;
                }
            } catch(e) {}
        }

        function reproducir(kind) {
            try {
                const ahora = Date.now();
                // El click NO respeta el MIN_MS (es feedback inmediato)
                if (kind !== "click") {
                    if (ahora - ultimoMsSonido < MIN_MS_ENTRE_SONIDOS) return;
                }
                ultimoMsSonido = ahora;
                asegurarAudioActivo();
                _reproducirAhora(kind);
            } catch(e) {}
        }

        let ultimoMsSonido = 0;

        window.__qrFeedback = reproducir;
        try { window.parent.__qrFeedback = reproducir; } catch(e) {}

        window.addEventListener('message', (event) => {
            try {
                if (event.data && event.data.type === 'qr_sound' && event.data.kind) {
                    reproducir(event.data.kind);
                }
            } catch(e) {}
        });

        function desbloquearAudio() {
            try {
                if (!audioCtx || audioCtx.state === 'closed') crearAudioContext();
                if (audioCtx && audioCtx.state === 'suspended') {
                    audioCtx.resume().catch(() => {});
                }
            } catch(e) {}
        }

        ['click', 'touchstart', 'touchend', 'keydown', 'mousedown', 'pointerdown']
            .forEach(evt => document.addEventListener(evt, desbloquearAudio, { passive: true }));
        window.addEventListener('focus', desbloquearAudio);
        window.addEventListener('pageshow', desbloquearAudio);

        // ══════════════════════════════════════════════════════
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
        function limpiarError() {
            const e = document.getElementById('qr-error');
            if (e) { e.style.display = 'none'; }
        }

        // ══════════════════════════════════════════════════════
        // BOTÓN PAUSAR / ACTIVAR
        // ══════════════════════════════════════════════════════
        function actualizarBoton() {
            const btn = document.getElementById('qr-toggle');
            const video = document.getElementById('qr-video');
            if (!btn) return;
            if (pausado) {
                btn.textContent = '▶ Activar camara';
                btn.classList.add('pausado');
                if (video) video.classList.add('pausado');
            } else {
                btn.textContent = '⏸ Pausar';
                btn.classList.remove('pausado');
                if (video) video.classList.remove('pausado');
            }
        }

        function togglePausa() {
            pausado = !pausado;
            sessionStorage.setItem(KEY_PAUSADO, pausado ? '1' : '0');
            actualizarBoton();
            if (pausado) {
                detenerCamara();
                setStatus('Camara pausada. Presiona Activar para reanudar.');
            } else {
                setStatus('Reanudando camara...');
                setTimeout(() => { iniciarScanner(); }, 200);
            }
        }

        // ══════════════════════════════════════════════════════
        function detenerCamara() {
            if (scanLoop) { clearTimeout(scanLoop); scanLoop = null; }
            if (stream) {
                try {
                    stream.getTracks().forEach(t => { try { t.stop(); } catch(e) {} });
                } catch(e) {}
                stream = null;
            }
            if (videoElement) {
                try {
                    videoElement.pause();
                    videoElement.srcObject = null;
                    videoElement.removeAttribute('src');
                    videoElement.load();
                } catch(e) {}
            }
            iniciado = false;
            arranqueTimestamp = 0;
            ultimoDni = null;
            ultimoTimestampDni = 0;
            ultimoDniEnviado = null;
            contadorFrameSinQR = 0;
        }

        function cargarPolyfill() {
            return new Promise((resolve, reject) => {
                if (polyfillCargado) { resolve(); return; }
                if (typeof BarcodeDetector !== 'undefined') {
                    polyfillCargado = true; resolve(); return;
                }
                setStatus('Cargando motor de escaneo...');
                const script = document.createElement('script');
                script.src = 'https://cdn.jsdelivr.net/npm/@sec-ant/barcode-detector@1.3/dist/iife/side-effects.min.js';
                script.async = true;
                script.onload = () => { polyfillCargado = true; resolve(); };
                script.onerror = () => reject(new Error('No se pudo cargar polyfill.'));
                document.head.appendChild(script);
            });
        }

        async function iniciarScanner() {
            if (iniciado) return;
            if (pausado) { setStatus('Camara pausada.'); return; }
            limpiarError();
            detenerCamara();
            videoElement = document.getElementById('qr-video');
            if (!videoElement) { setError('Video no encontrado.'); return; }

            try {
                await cargarPolyfill();
                if (typeof BarcodeDetector === 'undefined') {
                    setError('Navegador no soporta escaneo.');
                    return;
                }
                detector = new BarcodeDetector({ formats: ['qr_code'] });

                setStatus('Solicitando camara...');
                stream = await navigator.mediaDevices.getUserMedia({
                    video: {
                        facingMode: 'environment',
                        width: { ideal: 1280 },
                        height: { ideal: 720 }
                    },
                    audio: false
                });

                videoElement.srcObject = stream;
                videoElement.setAttribute('playsinline', 'true');
                videoElement.setAttribute('muted', 'true');
                await videoElement.play();

                iniciado = true;
                intentosFallidos = 0;
                arranqueTimestamp = Date.now();
                setStatus('Camara activa. Apunta al QR.');
                bucleEscaneo();

            } catch (err) {
                detenerCamara();
                intentosFallidos++;
                const esNotReadable = err.name === 'NotReadableError' ||
                                     (err.message && err.message.toLowerCase().includes('could not start'));
                if (esNotReadable && intentosFallidos < MAX_INTENTOS) {
                    setStatus('Camara ocupada. Reintentando (' + intentosFallidos + '/' + MAX_INTENTOS + ')...');
                    setTimeout(() => { iniciarScanner(); }, 2000);
                    return;
                }
                if (esNotReadable) setError('No se pudo acceder a la camara.');
                else if (err.name === 'NotAllowedError') setError('Permiso de camara denegado.');
                else if (err.name === 'NotFoundError') setError('No hay camara.');
                else setError('Error: ' + (err.message || err.name));
            }
        }

        function bucleEscaneo() {
            if (!iniciado || !videoElement || !detector) return;
            if (pausado) { scanLoop = setTimeout(bucleEscaneo, SCAN_INTERVAL_MS); return; }

            if (videoElement.readyState >= 2) {
                detector.detect(videoElement)
                    .then(barcodes => {
                        if (!iniciado || pausado) return;
                        if (Date.now() - arranqueTimestamp < ARRANQUE_ESPERA_MS) return;

                        if (barcodes && barcodes.length > 0) {
                            contadorFrameSinQR = 0;
                            const codigo = barcodes[0];
                            const texto = codigo.rawValue;
                            const match = texto.match(DNI_REGEX);
                            if (match) {
                                const dni = match[1];
                                const ahora = Date.now();

                                if (dni === ultimoDni && (ahora - ultimoTimestampDni) < ANTI_REBOTE_MS) {
                                    return;
                                }

                                ultimoDni = dni;
                                ultimoTimestampDni = ahora;

                                if (dni !== ultimoDniEnviado) {
                                    ultimoDniEnviado = dni;
                                    desbloquearAudio();
                                    // ═══ SONIDO CLICK INMEDIATO (no espera a Python) ═══
                                    reproducir("click");
                                    setStatus('QR: ' + dni);
                                    setTriggerValue("qr_dni", dni);
                                }
                            }
                        } else {
                            contadorFrameSinQR++;
                            if (contadorFrameSinQR >= MAX_FRAMES_SIN_QR) {
                                ultimoDniEnviado = null;
                            }
                        }
                    })
                    .catch(err => {});
            }
            if (iniciado && !pausado) scanLoop = setTimeout(bucleEscaneo, SCAN_INTERVAL_MS);
        }

        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') {
                detenerCamara();
                setStatus('Camara en pausa.');
            } else if (document.visibilityState === 'visible') {
                if (!iniciado && !pausado) {
                    setTimeout(() => { if (!iniciado && !pausado) iniciarScanner(); }, 1000);
                }
            }
        });
        window.addEventListener('beforeunload', detenerCamara);

        // ══════════════════════════════════════════════════════
        // SONIDO PENDIENTE DESDE PYTHON
        // ══════════════════════════════════════════════════════
        function revisarSonidoPendiente() {
            try {
                if (!data) return;
                const nonce = data.sonido_nonce || 0;
                const kind = data.sonido_kind || '';
                if (nonce > 0 && nonce !== ultimoSonidoNonce && kind) {
                    ultimoSonidoNonce = nonce;
                    reproducir(kind);
                }
            } catch(e) {}
        }

        // ══════════════════════════════════════════════════════
        // INICIALIZACIÓN
        // ══════════════════════════════════════════════════════
        setTimeout(() => {
            const btn = document.getElementById('qr-toggle');
            if (btn) btn.addEventListener('click', togglePausa);
            actualizarBoton();

            if (pausado) {
                setStatus('Camara pausada. Presiona Activar para reanudar.');
            } else {
                iniciarScanner();
            }

            revisarSonidoPendiente();
            if (sonidoLoop) clearInterval(sonidoLoop);
            sonidoLoop = setInterval(revisarSonidoPendiente, SONIDO_CHECK_MS);
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
