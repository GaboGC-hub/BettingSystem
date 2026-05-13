/**
 * inject.js — EV Futbol Live WebSocket Interceptor
 * World: MAIN (runs in the same JS context as the betting site's scripts)
 *
 * Técnicas anti-detección usadas:
 * 1. Proxy con Reflect.construct → instanceof WebSocket sigue siendo true
 * 2. Function.prototype.toString override → toString() devuelve código nativo
 * 3. Object.defineProperty con descriptor original → hasOwnProperty checks pasan
 * 4. Capture-phase listener en instancias → captura ANTES que cualquier listener de la página
 * 5. Soporte completo: text, ArrayBuffer, Blob
 * 6. Inyección MAIN world → inmune a Content Security Policy del sitio
 */
(function (globalThis) {
  'use strict';

  // ── 0. Guardar referencias nativas ANTES de que cualquier script del sitio corra ──
  const _NativeWS          = globalThis.WebSocket;
  const _origFnToString    = Function.prototype.toString;
  const _origDefProperty   = Object.defineProperty;
  const _origAddEL         = EventTarget.prototype.addEventListener;
  const _reflect_construct = Reflect.construct;

  // Señal para evitar doble hook (si el script se carga más de una vez)
  if (globalThis.__evfl_hooked__) return;
  _origDefProperty(globalThis, '__evfl_hooked__', { value: true, configurable: false });

  // ── 1. Override Function.prototype.toString para ocultar la Proxy ───────────
  // Cuando el sitio haga: WebSocket.toString() o Function.prototype.toString.call(WebSocket)
  // obtendrá el string nativo, no "function () { [native code] }" del Proxy.
  const _wsNativeString = _origFnToString.call(_NativeWS);

  _origDefProperty(Function.prototype, 'toString', {
    value: function toString() {
      if (this === WSProxy) return _wsNativeString;
      return _origFnToString.call(this);
    },
    writable: true,
    configurable: true,
    enumerable: false,
  });

  // ── 2. Proxy sobre el constructor de WebSocket ───────────────────────────────
  const WSProxy = new Proxy(_NativeWS, {
    /**
     * construct trap: se llama cada vez que el sitio hace `new WebSocket(url, protocols)`
     * Reflect.construct garantiza:
     *   - La instancia resultante pasa `instanceof WebSocket`
     *   - El prototipo es correcto
     *   - No hay trazas de Proxy en la instancia misma
     */
    construct(Target, args, NewTarget) {
      const instance = _reflect_construct(Target, args, NewTarget);
      _hookInstance(instance, args[0] || '');
      return instance;
    },

    // Pass-through transparente para todo lo demás
    get(target, prop, receiver) {
      const val = Reflect.get(target, prop, receiver);
      // Bind funciones nativas para mantener su contexto
      return typeof val === 'function' ? val.bind(target) : val;
    },
  });

  // ── 3. Reemplazar window.WebSocket con el descriptor original preservado ─────
  // Usamos getOwnPropertyDescriptor para copiar el descriptor original y solo
  // cambiar el valor, así hasOwnProperty y property checks pasan.
  const _originalDescriptor = Object.getOwnPropertyDescriptor(globalThis, 'WebSocket') || {
    writable: true, configurable: true, enumerable: false,
  };
  _origDefProperty(globalThis, 'WebSocket', {
    ..._originalDescriptor,
    value: WSProxy,
  });

  // ── 4. Hookear una instancia concreta (WebSockets) ───────────────────────────
  function _hookInstance(ws, wsUrl) {
    // Capture-phase: nuestro listener corre ANTES que los del sitio
    _origAddEL.call(ws, 'message', function _evflCapture(event) {
      _interceptMessage(event.data, wsUrl);
    }, /* capture = */ true);
  }

  // ── 4.5. Hookear Fetch para Betano (REST/GraphQL) y PS3838 (XHR polling) ────
  // Betano usa polling HTTP. PS3838 no usa WebSockets — todas sus cuotas vienen por Fetch/XHR.
  const _origFetch = globalThis.fetch;
  const _hostname = globalThis.location?.hostname || '';
  const _isPinnacle = _hostname.includes('ps3838') || _hostname.includes('pinnacle');
  const _isBetano = _hostname.includes('betano');
  const _isSofaScore = _hostname.includes('sofascore');

  if (_origFetch) {
    globalThis.fetch = async function (...args) {
      const response = await _origFetch.apply(this, args);
      const url = typeof args[0] === 'string' ? args[0] : (args[0] && args[0].url) ? args[0].url : '';
      
      // ── PS3838/Pinnacle: Interceptor Quirúrgico (Anti-Crash HTTP/2) ──
      // REGLA DE ORO: Solo clonamos compact/events (cuotas reales).
      // No tocamos account-balance, keep-alive, wstoken ni ningún stream de auth.
      // Esto evita corromper los multiplexados HTTP/2 que causan la pantalla en blanco.
      if (_isPinnacle) {
        try {
          const url_str = args[0] instanceof Request ? args[0].url : String(url);
          // ── DIAGNÓSTICO: Log de TODAS las URLs de fetch en PS3838 ──
          // Esto nos permite identificar el endpoint real de cuotas si cambió.
          // Se imprime en la consola del Faro PS3838 (F12).
          if (url_str.includes('/api/') || url_str.includes('/guest/') || url_str.includes('/edge/') ||
              url_str.includes('/sports-service') || url_str.includes('/live') || url_str.includes('/odds') ||
              url_str.includes('/compact') || url_str.includes('/straight') || url_str.includes('/events')) {
            console.log('🔍 [EVFL PS3838 URL]', url_str.substring(0, 200));
          }
          // Filtro de cuotas: ampliado para capturar cualquier formato nuevo
          if (url_str.includes('compact/events') || url_str.includes('/straight') || url_str.includes('/live-events') ||
              url_str.includes('guest/live') || url_str.includes('/api/live') || url_str.includes('/odds/live') ||
              url_str.includes('/markets/live') || url_str.includes('/v3/') || url_str.includes('/oddsupdate')) {
            // Clonar y procesar en segundo plano — sin await que bloquee React
            response.clone().json().then(data => {
              if (data && (data.leagues || data.events || data.sports || Array.isArray(data) || data.l || data.ce)) {
                console.log('💰 [EVFL] ¡ORO ENCONTRADO (Cuotas Reales)! URL:', url_str);
                _emit({ type: 'PINNACLE_RAW_DATA', payload: data, ws_url: url_str });
              }
            }).catch(() => {}); // Silencioso si no es JSON
          }
          // Todo lo demás (auth, balance, keep-alive) → pasa sin ser tocado
        } catch (e) { /* nunca romper la página */ }
      }

      // ── Betano: filtro ampliado para capturar overview/latest y danae-webapi ──
      if (_isBetano && (
          url.includes('api/sport/') || url.includes('api/graphql') || url.includes('api/events') ||
          url.includes('overview/latest') || url.includes('danae-webapi') || url.includes('contenthub') ||
          url.includes('betoffer') || url.includes('live-overview') ||
          url.includes('isInit=false') || url.includes('includeVirtuals=')
      )) {
         const clone = response.clone();
         clone.text().then(text => {
             if (text && text.length > 50) {
                 _emit({ type: 'text', data: text, ws_url: url });
             }
         }).catch(() => {});

         // ── ZERO-TOUCH OVERCLOCKING ──
         // Solo repetimos el endpoint overview/latest que devuelve todos los eventos.
         // Ignoramos otras llamadas (api/sport, graphql, etc.)
         if (url.includes('overview/latest')) {
             globalThis.__evfl_last_betano_args = args;
         }
         if (!globalThis.__evfl_betano_poll) {
             console.log("[EVFL Inject] 🚀 Iniciando Overclocking de Fetch (Betano) a 3000ms");
             globalThis.__evfl_betano_poll = setInterval(() => {
                 if (document.visibilityState !== 'visible') return;
                 if (globalThis.__evfl_last_betano_args) {
                     _origFetch.apply(globalThis, globalThis.__evfl_last_betano_args)
                         .then(r => r.text())
                         .then(text => {
                             if (text && text.length > 50) {
                                 let ws_url = typeof globalThis.__evfl_last_betano_args[0] === 'string' 
                                     ? globalThis.__evfl_last_betano_args[0] 
                                     : (globalThis.__evfl_last_betano_args[0].url || '');
                                 _emit({ type: 'text', data: text, ws_url: ws_url });
                             }
                         }).catch(() => {});
                 }
             }, 3000);

             window.addEventListener('beforeunload', () => {
                 if (globalThis.__evfl_betano_poll) {
                     clearInterval(globalThis.__evfl_betano_poll);
                     globalThis.__evfl_betano_poll = null;
                 }
             });
             window.addEventListener('pagehide', () => {
                 if (globalThis.__evfl_betano_poll) {
                     clearInterval(globalThis.__evfl_betano_poll);
                     globalThis.__evfl_betano_poll = null;
                 }
             });
         }
      }

      // ── Kambi (Betplay): interceptar llamadas REST iniciales ──
      const _isKambi = _hostname.includes('betplay') || _hostname.includes('kambi');
      if (_isKambi && url.includes('/betoffer/event/')) {
          const clone = response.clone();
          clone.text().then(text => {
              if (text && text.length > 50) {
                  _emit({ type: 'kambi_api', data: text, ws_url: url });
              }
          }).catch(() => {});
      }
      // ── SofaScore: interceptar llamadas internas a la API de estadísticas ──
      // SofaScore ya hace estas peticiones para mostrar la página — nosotros solo las leemos.
      // Endpoints útiles: /statistics (posesión, tiros, corners), /incidents (goles, tarjetas)
      if (_isSofaScore) {
        const isStats   = url.includes('/api/v1/event/') && url.includes('/statistics');
        const isIncident = url.includes('/api/v1/event/') && url.includes('/incidents');
        if (isStats || isIncident) {
          const clone = response.clone();
          clone.text().then(text => {
            if (text && text.length > 50) {
              // Extraer event_id de la URL: /api/v1/event/15832834/statistics
              const evMatch = url.match(/\/event\/(\d+)\//);  
              const eventId = evMatch ? parseInt(evMatch[1]) : null;
              _emit({
                type:      isStats ? 'sofascore_stats' : 'sofascore_incidents',
                data:      text,
                ws_url:    url,
                event_id:  eventId,
              });
            }
          }).catch(() => {});
        }
      }

      return response;
    };
  }


  const _origXHR = globalThis.XMLHttpRequest;
  if (_origXHR) {
    const _origOpen = _origXHR.prototype.open;
    const _origSend = _origXHR.prototype.send;
    
    _origXHR.prototype.open = function(method, url, ...rest) {
      this.__evfl_url = typeof url === 'string' ? url : '';
      return _origOpen.apply(this, [method, url, ...rest]);
    };
    
    let error404Count = 0;
    _origXHR.prototype.send = function(...args) {
      this.addEventListener('readystatechange', function() {
        // Auto-Recarga en caso de 404s múltiples (Betano session expired)
        if (this.readyState === 4 && this.status === 404 && (this.responseURL || '').includes('danae-webapi')) {
            error404Count++;
            if (error404Count > 3) {
                console.log("🔥 [EVFL] Múltiples 404 detectados. Sesión muerta. Recargando...");
                // Notify backend of hard reset
                globalThis.postMessage({ 
                    __evfl_frame: true, 
                    payload: { type: 'hard_reset', url: window.location.href }
                }, '*');
                setTimeout(() => window.location.reload(), 200); 
            }
        } else if (this.readyState === 4 && this.status === 200) {
            error404Count = 0;
        }
      });

      this.addEventListener('load', function() {
        const url = this.__evfl_url || '';
        let shouldCapture = false;

        // PS3838/Pinnacle: capturar todas las respuestas JSON relevantes
        if (_isPinnacle) {
          const ct = this.getResponseHeader('content-type') || '';
          // ── DIAGNÓSTICO: Log de TODAS las URLs de XHR en PS3838 ──
          if (url.includes('/api/') || url.includes('/guest/') || url.includes('/edge/') ||
              url.includes('/matchup') || url.includes('/straight') || url.includes('/odds') ||
              url.includes('/markets') || url.includes('/live') || url.includes('/sports-service/') ||
              url.includes('/compact') || url.includes('/events')) {
            console.log('🔍 [EVFL PS3838 XHR]', url.substring(0, 200), '| CT:', ct, '| Len:', (this.responseText||'').length);
          }
          if (ct.includes('json') || url.includes('/api/') || url.includes('/guest/') || url.includes('/edge/') ||
              url.includes('/matchup') || url.includes('/straight') || url.includes('/odds') ||
              url.includes('/markets') || url.includes('/live') || url.includes('/sports-service/')) {
            shouldCapture = true;
          }
          // Si es JSON y parece datos de cuotas, emitir como PINNACLE_RAW_DATA
          if (ct.includes('json') && this.responseText && this.responseText.length > 100) {
            try {
              const jsonData = JSON.parse(this.responseText);
              if (jsonData && (jsonData.leagues || jsonData.events || jsonData.sports || jsonData.l || jsonData.ce || Array.isArray(jsonData))) {
                console.log('💰 [EVFL XHR] ¡ORO ENCONTRADO! URL:', url);
                _emit({ type: 'PINNACLE_RAW_DATA', payload: jsonData, ws_url: url });
                shouldCapture = false;
              }
            } catch(e) {}
          }
        }

        // Betano: filtrar por endpoints relevantes incluyendo isInit=false y includeVirtuals=
        if (_isBetano && (
            url.includes('api/sport/') || url.includes('api/graphql') || url.includes('api/events') ||
            url.includes('overview/latest') || url.includes('danae-webapi') || url.includes('contenthub') ||
            url.includes('betoffer') || url.includes('live-overview') ||
            url.includes('isInit=false') || url.includes('includeVirtuals=')
        )) {
          shouldCapture = true;
        }

        // SofaScore: interceptar estadísticas e incidentes
        if (_isSofaScore) {
          const isStats   = url.includes('/api/v1/event/') && url.includes('/statistics');
          const isIncident = url.includes('/api/v1/event/') && url.includes('/incidents');
          if (isStats || isIncident) {
            shouldCapture = true;
            if (this.responseText && this.responseText.length > 50) {
              const evMatch = url.match(/\/event\/(\d+)\//);
              const eventId = evMatch ? parseInt(evMatch[1]) : null;
              _emit({
                type:      isStats ? 'sofascore_stats' : 'sofascore_incidents',
                data:      this.responseText,
                ws_url:    url,
                event_id:  eventId,
              });
              shouldCapture = false;
            }
          }
        }

        // Emisión genérica: si shouldCapture y hay texto JSON válido
        if (shouldCapture) {
          const rt = this.responseType || '';
          if ((rt === '' || rt === 'text') && this.responseText && this.responseText.length > 50) {
            const trimmed = this.responseText.trim();
            if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
              _emit({ type: 'text', data: this.responseText, ws_url: url });
            }
          }
        }
      });

      return _origSend.apply(this, args);
    };
  }

  // ── 5. Interceptar el mensaje y normalizar text/binary ───────────────────────
  function _interceptMessage(data, wsUrl) {
    if (typeof data === 'string') {
      // Texto plano / JSON
      _emit({ type: 'text', data, ws_url: wsUrl });

    } else if (data instanceof ArrayBuffer) {
      // Binario directo: codificar a base64
      _emit({ type: 'binary', encoding: 'base64', data: _ab2b64(data), ws_url: wsUrl });

    } else if (data instanceof Blob) {
      // Blob: leer como ArrayBuffer (async, sin bloquear el hilo principal)
      data.arrayBuffer().then(function (buf) {
        _emit({ type: 'binary', encoding: 'base64', data: _ab2b64(buf), ws_url: wsUrl });
      }).catch(function () { /* silenciar */ });

    } else if (data && typeof data === 'object' && data.buffer instanceof ArrayBuffer) {
      // TypedArray (Uint8Array, Int16Array, etc.)
      _emit({ type: 'binary', encoding: 'base64', data: _ab2b64(data.buffer), ws_url: wsUrl });
    }
  }

  // ── 6. Emitir hacia content_script.js vía postMessage ───────────────────────
  // postMessage es la única forma segura de cruzar la barrera MAIN ↔ ISOLATED
  function _emit(payload) {
    payload.ts = Date.now();
    // Usar globalThis.postMessage para evitar redirección de postMessage monkey-patched
    globalThis.postMessage({ __evfl_frame: true, payload }, '*');
  }

  // ── 7. Utilidad: ArrayBuffer → base64 (chunked para buffers grandes) ─────────
  function _ab2b64(buffer) {
    const bytes = new Uint8Array(buffer);
    const CHUNK = 8192;
    let binary = '';
    for (let i = 0; i < bytes.length; i += CHUNK) {
      binary += String.fromCharCode.apply(null, bytes.subarray(i, i + CHUNK));
    }
    return globalThis.btoa(binary);
  }

}(typeof globalThis !== 'undefined' ? globalThis : window));
