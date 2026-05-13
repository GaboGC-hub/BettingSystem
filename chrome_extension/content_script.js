/**
 * content_script.js — EV Futbol Live Bridge
 * World: ISOLATED (contexto protegido de la extensión)
 *
 * Rol: puente entre el MAIN world (inject.js) y el background.js
 * - Escucha mensajes postMessage de inject.js
 * - Los valida y reenvía a background.js via chrome.runtime.sendMessage
 * - Para SofaScore: fetch directo a la API interna (content scripts pueden
 *   hacer fetch cross-origin si el host está en host_permissions)
 * - Maneja gracefully "Extension context invalidated" (recarga de extensión)
 */
(function () {
  'use strict';

  const TAB_URL = window.location.href;

  // ── Helper: envío seguro a background.js ────────────────────────────────
  function safeSend(message) {
    if (!chrome.runtime?.id) return;
    try {
      chrome.runtime.sendMessage(message);
    } catch (e) {
      if (e.message && e.message.includes('Extension context invalidated')) {
        console.warn('[EVFL] Contexto invalidado — recarga la pestaña (F5) para restaurar el bridge.');
      } else {
        console.error('[EVFL] Error enviando mensaje:', e.message);
      }
    }
  }

  // ── Escuchar mensajes de inject.js (MAIN world) ──────────────────────────
  window.addEventListener('message', function (event) {
    if (event.source !== window) return;
    if (!event.data || !event.data.__evfl_frame) return;

    const payload = event.data.payload;
    if (!payload) return;

    // Ignorar heartbeats muy cortos (< 10 chars = ping/pong del sitio)
    if (payload.type === 'text' && payload.data.length < 10) return;

    const enriched = {
      ...payload,
      tab_url: TAB_URL,
    };

    safeSend({ __evfl_frame: true, payload: enriched });
  });

  console.debug('[EVFL] ✅ Odds Bridge activo en', TAB_URL);

  // ═══════════════════════════════════════════════════════════════════════════
  // ── SofaScore: Fetch directo a la API interna ─────────────────────────────
  // Los content scripts pueden hacer fetch cross-origin si el host está en
  // host_permissions (https://*.sofascore.com/* ya está incluido).
  // Las peticiones llevan las cookies del usuario → respuesta 200 OK normal.
  // NO generamos nuevas cargas — SofaScore hace estas peticiones internamente
  // cada ~30s para mantener las stats actualizadas.
  // ═══════════════════════════════════════════════════════════════════════════
  async function _fetchSofaScoreStats() {
    try {
      const idMatch = TAB_URL.match(/#id:(\d+)/);
      if (!idMatch) {
        console.debug('[EVFL] SofaScore: no se encontró event_id en URL', TAB_URL);
        return;
      }
      const eventId = parseInt(idMatch[1], 10);

      // Lanzar las 3 peticiones en paralelo.
      // /event/{id} → nombres de equipos, torneo, minuto, estado del partido
      const [statsRes, incidentsRes, eventRes] = await Promise.all([
        fetch(`https://api.sofascore.com/api/v1/event/${eventId}/statistics`),
        fetch(`https://api.sofascore.com/api/v1/event/${eventId}/incidents`),
        fetch(`https://api.sofascore.com/api/v1/event/${eventId}`),
      ]);

      // Capturar metadatos del evento (equipos, torneo, minuto)
      let eventText = '{}';
      if (eventRes.ok) {
        eventText = await eventRes.text();
      }

      if (statsRes.ok) {
        const statsText = await statsRes.text();
        safeSend({
          __evfl_frame: true,
          payload: {
            type:       'sofascore_stats',
            source_id:  'EXT_SOFASCORE',
            tab_url:    TAB_URL,
            data:       statsText,
            event_data: eventText,   // ← nombres de equipos, torneo, minuto
            event_id:   eventId,
            ts:         Date.now(),
          }
        });
        console.debug('[EVFL] 📊 SofaScore stats enviadas event_id=' + eventId);
      } else {
        console.warn('[EVFL] SofaScore /statistics respondió:', statsRes.status);
      }

      if (incidentsRes.ok) {
        const incText = await incidentsRes.text();
        safeSend({
          __evfl_frame: true,
          payload: {
            type:      'sofascore_incidents',
            source_id: 'EXT_SOFASCORE',
            tab_url:   TAB_URL,
            data:      incText,
            event_id:  eventId,
            ts:        Date.now(),
          }
        });
      }

    } catch (e) {
      console.error('[EVFL] Error fetching SofaScore API:', e);
    }
  }

  // ── Activar según el sitio ───────────────────────────────────────────────
  const _isSofaScore = TAB_URL.includes('sofascore.com');

  if (_isSofaScore && TAB_URL.includes('/football/match/')) {
    // Primera llamada a los 1.5s (document_idle garantiza que la página ya cargó),
    // luego cada 15s para mantener los datos frescos durante el partido.
    setTimeout(_fetchSofaScoreStats, 1500);
    setInterval(_fetchSofaScoreStats, 45000);  // 45s para evitar rate-limit/ban
    console.log('[EVFL] 📡 SofaScore API Fetcher activo → event_id en', TAB_URL);
  }

  // ── Scraper DOM fallback para PS3838 ─────────────────────────────────────
  // Como fallback a la interceptación de red, leemos el DOM directamente.
  if (TAB_URL.includes('ps3838.com') || TAB_URL.includes('pinnacle.com')) {
    setInterval(() => {
      try {
        const possibleRows = document.querySelectorAll('tr, div[class*="row"], div[class*="Event"], div[class*="Match"], a[href*="/matchup/"]');
        
        const matchContainers = Array.from(possibleRows).filter(row => {
             const txt = row.textContent;
             return txt && txt.length > 10 && /\d\.\d{2}/.test(txt);
        });

        const uniqueContainers = [];
        matchContainers.forEach(c => {
             if (!uniqueContainers.some(p => p.contains(c) && p !== c)) {
                 uniqueContainers.push(c);
             }
        });

        const allMatches = [];

        uniqueContainers.forEach(container => {
            const linesGoles = [];
            const linesCorners = [];
            const textContent = (container.textContent || '').toLowerCase();
            
            const textNodes = [];
            const walk = document.createTreeWalker(container, NodeFilter.SHOW_TEXT, null, false);
            let n;
            while(n = walk.nextNode()) {
                const t = n.textContent.trim();
                if (t.length > 2 && !/^[\d.,]+$/.test(t) && !t.toLowerCase().includes('over') && !t.toLowerCase().includes('under') && !t.toLowerCase().includes('más') && !t.toLowerCase().includes('menos')) {
                    textNodes.push(t);
                }
            }
            const homeTeam = textNodes[0] || 'Unknown';
            const awayTeam = textNodes[1] || 'Unknown';

            const buttons = container.querySelectorAll('button, .price, .market-btn, [role="button"], span');
            
            buttons.forEach(btn => {
                const text = (btn.textContent || '').trim().toLowerCase();
                const aria = (btn.getAttribute('aria-label') || '').trim().toLowerCase();
                const fullText = text + ' ' + aria;
                
                let market = null;
                if (fullText.includes('corner') || fullText.includes('córner') || fullText.includes('esquina')) {
                    market = linesCorners;
                } else if (fullText.includes('goal') || fullText.includes('goles') || fullText.includes('total') || /^(o|u)\s*\d/.test(fullText) || /^(over|under)\s*\d/.test(fullText)) {
                    market = linesGoles;
                } else if (textContent.includes('total')) {
                     if (/^(o|u)\s*\d/.test(fullText) || /^(over|under)\s*\d/.test(fullText) || /^(más|menos)\s*\d/.test(fullText)) {
                         market = linesGoles;
                     }
                }

                if (market) {
                    const lineMatch = fullText.match(/(?:over|under|o|u|más de|menos de|más|menos)\s*([0-9.]+)/i);
                    const allNumbers = fullText.match(/[0-9.]+/g);
                    
                    if (lineMatch && allNumbers && allNumbers.length >= 2) {
                        const linea = parseFloat(lineMatch[1]);
                        const isOver = /(over|o|más|mas)/i.test(fullText);
                        const isUnder = /(under|u|menos)/i.test(fullText);
                        
                        const odds = parseFloat(allNumbers[allNumbers.length - 1]);
                        
                        if (odds > 1.0 && odds < 20.0) {
                            let lineObj = market.find(l => l.linea === linea);
                            if (!lineObj) {
                                lineObj = { linea: linea, over: 0, under: 0 };
                                market.push(lineObj);
                            }
                            
                            if (isOver) lineObj.over = odds;
                            if (isUnder) lineObj.under = odds;
                        }
                    }
                }
            });

            const validGoles = linesGoles.filter(l => l.over > 0 && l.under > 0);
            const validCorners = linesCorners.filter(l => l.over > 0 && l.under > 0);

            if (validGoles.length > 0) {
                allMatches.push({ market: "Goles", lines: validGoles, home: homeTeam, away: awayTeam });
            }
            if (validCorners.length > 0) {
                allMatches.push({ market: "Corners", lines: validCorners, home: homeTeam, away: awayTeam });
            }
        });

        allMatches.forEach(m => {
            safeSend({
              __evfl_frame: true,
              payload: {
                type: "dom_scrape",
                source_id: "EXT_WS_PS3838",
                market_name: m.market,
                lines: m.lines,
                tab_url: TAB_URL,
                home_team: m.home,
                away_team: m.away,
                ts: Date.now()
              }
            });
        });

      } catch (e) {
        console.error('[EVFL DOM Scraper] Error:', e);
      }
    }, 5000);
  }

  // ── Simulación de Interacción Humana (Anti-Anomaly Detection) ──────────────
  setInterval(() => {
    try {
        // Scroll aleatorio de pocos pixeles
        const scrollAmount = Math.floor(Math.random() * 20) - 10;
        window.scrollBy({ top: scrollAmount, behavior: 'smooth' });
        
        // Simular movimiento de ratón
        const event = new MouseEvent('mousemove', {
            view: window,
            bubbles: true,
            cancelable: true,
            clientX: Math.floor(Math.random() * window.innerWidth),
            clientY: Math.floor(Math.random() * window.innerHeight)
        });
        document.dispatchEvent(event);
        console.debug('[EVFL] 🤖 Simulación humana ejecutada para mantener la sesión viva.');
    } catch(e) {}
  }, 1000 * 60 * 7); // Cada 7 minutos

})();
