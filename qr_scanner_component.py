import streamlit as st
QR_SCANNER = st.components.v2.component(
    name="qr_scanner_v6",
    isolate_styles=False,
    html="""
    <div id="qr-wrapper">
        <div id="qr-reader"></div>
        <div id="qr-status">Iniciando cámara…</div>
        <div id="qr-error" style="display:none;"></div>
    </div>
    """,
    css="""
    #qr-wrapper{width:100%;max-width:520px;margin:0 auto;}
    #qr-reader{
        border:2px solid #E87B4A;border-radius:8px;
        overflow:hidden;background:#000;min-height:260px;
    }
    #qr-reader video{width:100%!important;height:auto!important;border-radius:6px;}
    #qr-status{
        text-align:center;font-size:13px;margin-top:8px;color:#666;
        font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
    }
    #qr-error{
        text-align:center;font-size:13px;margin-top:8px;color:#C62828;
        font-weight:600;font-family:-apple-system,BlinkMacSystemFont,
        'Segoe UI',Roboto,sans-serif;padding:10px;border:1px solid #C62828;
        border-radius:6px;background:#f8d7da;
    }
    #qr-reader button{
        background:#E87B4A!important;color:#fff!important;border:none!important;
        border-radius:6px!important;padding:8px 16px!important;
        font-weight:600!important;cursor:pointer!important;margin:4px!important;
    }
    #qr-reader button:hover{background:#D66535!important;}
    #qr-reader select{
        border-radius:6px!important;padding:6px 10px!important;
        margin:4px!important;border:1px solid #ccc!important;
    }
    """,
    js="""
    export default function(component){
        const { setTriggerValue } = component;

        let scanner = null;
        let iniciado = false;
        let ultimoDni = null;
        let ultimoTs = 0;
        const COOLDOWN_MS = 5000;
        let audioCtx = null;

        // ═══ AUDIO ═══
        // Creamos sonidos con Web Audio API (no necesitamos archivos mp3).
        function getAudioCtx(){
            if(!audioCtx){
                try{ audioCtx = new (window.AudioContext||window.webkitAudioContext)(); }
                catch(e){}
            }
            if(audioCtx && audioCtx.state === 'suspended'){
                audioCtx.resume().catch(()=>{});
            }
            return audioCtx;
        }
        function _tono(freq, dur, tipo, vol, delay){
            const ctx = getAudioCtx();
            if(!ctx) return;
            const t0 = ctx.currentTime + (delay||0);
            const osc = ctx.createOscillator();
            const g = ctx.createGain();
            osc.connect(g); g.connect(ctx.destination);
            osc.type = tipo||'sine';
            osc.frequency.setValueAtTime(freq, t0);
            g.gain.setValueAtTime(0, t0);
            g.gain.linearRampToValueAtTime(vol||0.35, t0+0.015);
            g.gain.exponentialRampToValueAtTime(0.001, t0+dur);
            osc.start(t0); osc.stop(t0+dur+0.02);
        }
        function sonidoClick(){ _tono(880,0.05,'sine',0.25,0); }
        function sonidoPuntual(){
            _tono(523,0.10,'sine',0.40,0);
            _tono(659,0.10,'sine',0.40,0.10);
            _tono(784,0.15,'sine',0.40,0.20);
        }
        function sonidoTardanza(){ _tono(440,0.30,'sine',0.35,0); }
        function sonidoDuplicado(){
            _tono(220,0.18,'square',0.45,0);
            _tono(220,0.18,'square',0.45,0.22);
        }
        function sonidoError(){
            _tono(180,0.15,'sawtooth',0.45,0);
            _tono(250,0.15,'sawtooth',0.45,0.15);
            _tono(330,0.15,'sawtooth',0.45,0.30);
            _tono(440,0.25,'sawtooth',0.45,0.45);
        }
        function sonidoBloqueado(){
            _tono(160,0.15,'sawtooth',0.45,0);
            _tono(120,0.15,'sawtooth',0.45,0.18);
            _tono(90,0.30,'sawtooth',0.45,0.36);
        }
        function reproducir(kind){
            try{
                getAudioCtx();
                switch(kind){
                    case "click":     sonidoClick();     break;
                    case "puntual":   sonidoPuntual();   break;
                    case "tardanza":  sonidoTardanza();  break;
                    case "duplicado": sonidoDuplicado(); break;
                    case "bloqueado": sonidoBloqueado(); break;
                    default:          sonidoError();     break;
                }
            }catch(e){}
        }
        // Exponemos la función para que Python la pueda llamar
        // desde fuera del iframe.
        window.__qrFeedback = reproducir;
        try{ window.parent.__qrFeedback = reproducir; }catch(e){}

        function setStatus(t){
            const el = document.getElementById('qr-status');
            if(el){ el.textContent = t; el.style.display = 'block'; }
        }
        function setError(t){
            const e = document.getElementById('qr-error');
            const s = document.getElementById('qr-status');
            if(e){ e.textContent = t; e.style.display = 'block'; }
            if(s) s.style.display = 'none';
        }

        // ═══ CARGAR LIBRERÍA ═══
        // Cargamos html5-qrcode desde un CDN. Solo una vez.
        function cargarLibreria(cb){
            if(window.Html5Qrcode){ cb(); return; }
            if(window.__qrLibCargando){
                const t = setInterval(()=>{
                    if(window.Html5Qrcode){
                        clearInterval(t);
                        window.__qrLibCargando = false;
                        cb();
                    }
                }, 100);
                return;
            }
            window.__qrLibCargando = true;
            const s = document.createElement('script');
            s.src = "https://unpkg.com/html5-qrcode@2.3.8/html5-qrcode.min.js";
            s.async = true;
            s.onload = () => { window.__qrLibCargando = false; cb(); };
            s.onerror = () => {
                window.__qrLibCargando = false;
                setError('No se pudo cargar la librería QR.');
            };
            document.head.appendChild(s);
        }

        // ═══ INICIAR ═══
        function iniciarScanner(){
            if(iniciado) return;
            const reader = document.getElementById('qr-reader');
            if(!reader) return;

            cargarLibreria(() => {
                if(!window.Html5Qrcode){
                    setError('Librería QR no disponible.');
                    return;
                }
                reader.innerHTML = '';
                scanner = new Html5Qrcode('qr-reader', { verbose: false });
                iniciado = true;

                scanner.start(
                    { facingMode: 'environment' },
                    { fps: 15, qrbox: { width: 220, height: 220 },
                      aspectRatio: 1.0 },
                    (texto) => {
                        try{
                            const m = String(texto).match(/\\b(\\d{8})\\b/);
                            if(!m) return;
                            const dni = m[1];
                            const ahora = Date.now();
                            const mismo = (dni === ultimoDni);
                            const dentro = (ahora - ultimoTs) < COOLDOWN_MS;
                            if(mismo && dentro) return;
                            ultimoDni = dni;
                            ultimoTs = ahora;
                            try{ getAudioCtx(); _tono(880,0.05,'sine',0.25,0); }catch(e){}
                            setStatus('QR: ' + dni);
                            setTriggerValue('qr_dni', dni);
                        }catch(e){}
                    },
                    () => {}
                ).then(() => {
                    setStatus('Cámara activa.');
                }).catch((e) => {
                    const msg = (e && e.message) ? e.message : String(e);
                    setError('Error cámara: ' + msg);
                    iniciado = false;
                });
            });
        }

        // ═══ DETENER ═══
        function detenerScanner(){
            if(!scanner) return;
            const s = scanner;
            scanner = null;
            iniciado = false;
            ultimoDni = null;
            ultimoTs = 0;
            try{
                s.stop().then(() => { try{ s.clear(); }catch(e){} }).catch(()=>{});
            }catch(e){}
        }

        // ═══ VISIBILIDAD ═══
        // Si el usuario cambia de pestaña y vuelve, reiniciamos
        // el scanner solo si se detuvo. NO usamos pause/resume
        // porque son frágiles.
        document.addEventListener('visibilitychange', () => {
            if(document.visibilityState === 'visible'){
                if(!iniciado || !scanner){
                    iniciarScanner();
                }
            }
        });

        // ═══ ARRANCAR ═══
        setTimeout(iniciarScanner, 300);

        // ═══ CLEANUP ═══
        // Streamlit llama a esta función cuando desmonta el componente.
        return () => { detenerScanner(); };
    }
    """,
)
def qr_scanner(key="qr", on_scan=None):
    """
    Renderiza el componente de cámara QR.
    Devuelve el resultado del componente (que tiene .qr_dni si escaneó).
    """
    return QR_SCANNER(key=key)
