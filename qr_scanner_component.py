import streamlit as st

# ─────────────────────────────────────────────────────────────
# Componente de escáner QR hecho a mano para Streamlit.
# Usa html5-qrcode (cargado desde CDN) dentro de un iframe
# aislado, y devuelve el DNI leído (8 dígitos) a Python
# mediante setTriggerValue("qr_dni", dni).
# ─────────────────────────────────────────────────────────────
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
    /* Contenedor general: lo achicamos para que no invada toda la pantalla */
    #qr-wrapper {
        width: 100%;
        max-width: 340px;
        margin: 0 auto;
    }

    /* Caja donde se dibuja el video de la cámara */
    #qr-reader {
        border-radius: 12px;
        overflow: hidden;
        border: 3px solid #E65100;
        background: #000;
        height: 320px;        /* altura fija para que no se estire raro */
        min-height: 320px;
        display: flex;
        align-items: center;
        justify-content: center;
    }

    /* El video en sí: que llene la caja sin deformarse */
    #qr-reader video {
        border-radius: 8px;
        width: 100% !important;
        height: 100% !important;
        object-fit: cover;
    }

    /* Mensaje de estado (abajo del video) */
    #qr-status {
        text-align: center;
        font-size: 13px;
        margin-top: 8px;
        color: #666;
        font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    }

    /* Caja roja de error */
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
        // Esta función la inyecta Streamlit para poder mandar valores de vuelta a Python
        const { setTriggerValue } = component;

        // ── Variables de estado del escáner ──
        let scanner = null;          // instancia de Html5Qrcode
        let iniciado = false;        // ¿la cámara ya arrancó alguna vez?
        let pausado = false;         // ¿está en pausa ahora mismo?
        let isRestarting = false;    // bandera para no reiniciar en bucle

        // ── Anti-duplicados ──
        // Si el mismo DNI se lee dos veces seguidas en menos de 5 segundos,
        // lo ignoramos. Así evitamos que el mismo QR dispare 20 eventos
        // por segundo mientras está frente a la cámara.
        const COOLDOWN_MS = 5000;
        let ultimoDniEmitido = null;
        let ultimoTimestampEmision = 0;

        // ═══════════════════════════════════════════════
        //  AUDIO
        //  Usamos WebAudio para generar tonos "a mano",
        //  sin archivos mp3 ni nada externo. Cada tipo
        //  de sonido es una combinación de notas.
        // ═══════════════════════════════════════════════
        let audioCtx = null;

        // Crea (o reutiliza) el AudioContext. Los navegadores
        // lo suspenden hasta que hay interacción del usuario,
        // así que lo reanudamos si hace falta.
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

        // Toca UNA nota: frecuencia, duración, tipo de onda,
        // volumen y un pequeño retraso (para armar arpegios).
        function _tono(freq, dur, tipo, vol, delay) {
            const ctx = getAudioCtx();
            if (!ctx) return;
            const t0 = ctx.currentTime + (delay || 0);
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.connect(g); g.connect(ctx.destination);
            osc.type = tipo || 'sine';
            osc.frequency.setValueAtTime(freq, t0);
            // Envolvente: sube rápido y decae exponencialmente
            g.gain.setValueAtTime(0, t0);
            g.gain.linearRampToValueAtTime(vol || 0.35, t0 + 0.015);
            g.gain.exponentialRampToValueAtTime(0.001, t0 + dur);
            osc.start(t0);
            osc.stop(t0 + dur + 0.02);
        }

        // ── Definición de cada "efecto" sonoro ──
        // click: un pitidito corto (feedback al leer un QR)
        function sonidoClick() { _tono(880, 0.05, 'sine', 0.25, 0); }

        // puntual: arpegio alegre ascendente (llegó a tiempo)
        function sonidoPuntual() {
            _tono(523, 0.10, 'sine', 0.40, 0);
            _tono(659, 0.10, 'sine', 0.40, 0.10);
            _tono(784, 0.15, 'sine', 0.40, 0.20);
        }

        // tardanza: una sola nota más grave y larga
        function sonidoTardanza() { _tono(440, 0.30, 'sine', 0.35, 0); }

        // duplicado: dos pulsos graves y secos (doble "bip bip")
        function sonidoDuplicado() {
            _tono(220, 0.18, 'square', 0.45, 0);
            _tono(220, 0.18, 'square', 0.45, 0.22);
        }

        // error: escala ascendente tipo "alarma"
        function sonidoError() {
            _tono(180, 0.15, 'sawtooth', 0.45, 0);
            _tono(250, 0.15, 'sawtooth', 0.45, 0.15);
            _tono(330, 0.15, 'sawtooth', 0.45, 0.30);
            _tono(440, 0.25, 'sawtooth', 0.45, 0.45);
        }

        // bloqueado: tonos descendentes (acceso denegado)
        function sonidoBloqueado() {
            _tono(160, 0.15, 'sawtooth', 0.45, 0);
            _tono(120, 0.15, 'sawtooth', 0.45, 0.18);
            _tono(90, 0.30, 'sawtooth', 0.45, 0.36);
        }

        // Despachador: recibe un nombre y toca el sonido correspondiente.
        // Si algo falla, se traga el error (no queremos romper el escáner por audio).
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

        // Exponemos esta función en window (y en el padre si se puede)
        // para que Python/Streamlit la pueda invocar desde JS y disparar sonidos.
        window.__qrFeedback = reproducir;
        try {
            window.parent.__qrFeedback = reproducir;
        } catch (e) {}

        // ═══════════════════════════════════════════════
        //  UTILIDADES DE UI
        // ═══════════════════════════════════════════════

        // Muestra un texto en la línea de estado (debajo del video)
        function setStatus(t) {
            const el = document.getElementById('qr-status');
            if (el) { el.textContent = t; el.style.display = 'block'; }
        }

        // Muestra el cartel rojo de error y oculta el status
        function setError(t) {
            const e = document.getElementById('qr-error');
            const s = document.getElementById('qr-status');
            if (e) { e.textContent = t; e.style.display = 'block'; }
            if (s) s.style.display = 'none';
        }

        // Apaga el escáner y resetea el estado interno.
        // Se llama antes de reiniciar y al descargar la página.
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

        // ═══════════════════════════════════════════════
        //  SCANNER (arranque y lectura de QR)
        // ═══════════════════════════════════════════════
        function iniciarScanner() {
            // Guardas: no arrancar dos veces ni mientras se reinicia
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

            // Limpieza previa por si había algo colgado
            destruirScanner();
            reader.innerHTML = '';
            iniciado = true;

            // "Desbloqueamos" el audio en el primer arranque
            getAudioCtx();

            // Pequeño delay para asegurarnos de que el DOM está listo
            setTimeout(() => {
                if (!iniciado) return;

                try {
                    scanner = new Html5Qrcode("qr-reader", { verbose: false });
                    scanner.start(
                        { facingMode: "environment" },  // cámara trasera si existe
                        { fps: 20, qrbox: { width: 250, height: 250 } }, // 20 lecturas/seg

                        // ── Callback de QR detectado ──
                        (texto) => {
                            try {
                                // Buscamos un bloque de 8 dígitos en el texto del QR.
                                // Muchos QR traen basura alrededor (URLs, prefijos, etc.).
                                const m = texto.match(/\\b(\\d{8})\\b/);
                                if (!m) return;
                                const dni = m[1];

                                // Anti-rebote: si es el mismo DNI y estamos dentro
                                // del cooldown, no lo volvemos a emitir.
                                const ahora = Date.now();
                                const esMismoDni = (dni === ultimoDniEmitido);
                                const dentroCooldown = (ahora - ultimoTimestampEmision) < COOLDOWN_MS;
                                if (esMismoDni && dentroCooldown) return;

                                // Guardamos el DNI y la hora de esta emisión
                                ultimoDniEmitido = dni;
                                ultimoTimestampEmision = ahora;

                                // Feedback: pitidito + texto en pantalla
                                try { getAudioCtx(); _tono(880, 0.05, 'sine', 0.25, 0); } catch(e) {}
                                setStatus('QR: ' + dni);

                                // ¡Acá está la magia! Mandamos el DNI a Python.
                                setTriggerValue("qr_dni", dni);
                            } catch (e) {}
                        },

                        // ── Callback de error de escaneo (frames sin QR) ──
                        // Lo dejamos vacío a propósito: falla todo el tiempo,
                        // no queremos llenar la consola de ruido.
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

        // ═══════════════════════════════════════════════
        //  VISIBILITY: pausar cuando la pestaña se oculta
        //  (ahorra batería y evita que la cámara quede colgada)
        // ═══════════════════════════════════════════════
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
            // Si por alguna razón el scanner ya no existe, arrancamos de cero
            if (!scanner || !iniciado) {
                iniciarScanner();
                return;
            }
            try {
                scanner.resume();
                pausado = false;
                setStatus('Camara activa.');
            } catch (e) {
                // Si resume falla, mejor destruir y volver a empezar
                destruirScanner();
                iniciarScanner();
            }
        }

        // Eventos del navegador que nos dicen si la pestaña está visible
        document.addEventListener('visibilitychange', () => {
            if (document.visibilityState === 'hidden') pausarScanner();
            else if (document.visibilityState === 'visible') reanudarScanner();
        });

        // Algunos navegadores no disparan visibilitychange pero sí blur/focus
        window.addEventListener('blur', () => {
            if (document.visibilityState === 'hidden') pausarScanner();
        });
        window.addEventListener('focus', () => {
            if (document.visibilityState === 'visible') reanudarScanner();
        });

        // ═══════════════════════════════════════════════
        //  FIX PARA EL NotReadableError
        //  A veces la cámara queda "ocupada" (por otra pestaña,
        //  por el sistema operativo, etc.) y el navegador tira
        //  NotReadableError. Acá lo detectamos y reiniciamos
        //  el escáner después de un pequeño delay.
        // ═══════════════════════════════════════════════
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

        // ═══════════════════════════════════════════════
        //  CARGA DE LA LIBRERÍA html5-qrcode
        //  Solo la cargamos una vez; si ya está, arrancamos.
        // ═══════════════════════════════════════════════
        window.addEventListener('beforeunload', destruirScanner);

        // Caso 1: la librería ya está en window
        if (window.Html5Qrcode) {
            iniciarScanner();
            return;
        }

        // Caso 2: otra instancia ya la está cargando -> esperamos
        if (window.__qrV18Cargando) {
            let n = 0;
            const t = setInterval(() => {
                n++;
                if (window.Html5Qrcode) {
                    clearInterval(t);
                    window.__qrV18Cargando = false;
                    iniciarScanner();
                } else if (n > 100) {  // ~10 segundos de timeout
                    clearInterval(t);
                    window.__qrV18Cargando = false;
                    setError('Timeout cargando libreria.');
                }
            }, 100);
            return;
        }

        // Caso 3: la cargamos nosotros desde el CDN
        window.__qrV18Cargando = true;
        const s = document.createElement('script');
        s.src = 'https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js';
        s.async = true;
        s.onload = () => {
            window.__qrV18Cargando = false;
            // Pequeño delay por si el módulo se define en microtask
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


# ─────────────────────────────────────────────────────────────
# Wrapper de Python: así se usa desde cualquier página
# de Streamlit de forma limpia.
#
# Ejemplo:
#   qr_scanner(key="scan_puerta", on_scan=mi_callback)
# ─────────────────────────────────────────────────────────────
def qr_scanner(key="qr_scanner", on_scan=None):
    # Si no pasan callback, usamos uno vacío para no romper nada
    if on_scan is None:
        on_scan = lambda: None
    return QR_SCANNER_COMPONENT(
        key=key,
        on_qr_dni_change=on_scan,
    )
