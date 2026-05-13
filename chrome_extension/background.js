/**
 * background.js — EV Futbol Live Service Worker
 *
 * Pipeline completo:
 *   inject.js (MAIN) → postMessage → content_script.js (ISOLATED)
 *   → chrome.runtime.sendMessage → aquí → ws://localhost:8000/ws/extension
 *
 * DEBUG: Cada paso loguea en chrome://extensions → Service Worker → Console
 */

'use strict';

const SERVER_WS_URL = 'ws://localhost:8000/ws/extension';
const RECONNECT_MS  = 3000;
const MAX_QUEUE     = 1000;
const PING_MS       = 20000;

let serverWs       = null;
let reconnectTimer = null;
let pingTimer      = null;
let isConnecting   = false;
let framesSent     = 0;
const frameQueue   = [];

// ── Diagnóstico: contadores visibles en Service Worker console ───────────────
let _diag = { received: 0, sent: 0, queued: 0, errors: 0 };

// ── Conexión al servidor Python ──────────────────────────────────────────────
function connectToServer() {
  if (isConnecting) return;
  if (serverWs && (serverWs.readyState === 0 || serverWs.readyState === 1)) return;

  isConnecting = true;
  clearTimeout(reconnectTimer);
  clearInterval(pingTimer);

  console.log('[EVFL BG] 🔌 Conectando a', SERVER_WS_URL);

  try {
    serverWs = new WebSocket(SERVER_WS_URL);
  } catch (e) {
    console.warn('[EVFL BG] ❌ No se pudo crear WS:', e.message);
    isConnecting = false;
    scheduleReconnect();
    return;
  }

  serverWs.onopen = function () {
    isConnecting = false;
    console.log('[EVFL BG] ✅ Conectado. Frames en cola:', frameQueue.length);

    // Vaciar buffer acumulado
    while (frameQueue.length > 0 && serverWs.readyState === 1) {
      try {
        serverWs.send(JSON.stringify(frameQueue.shift()));
        _diag.sent++;
      } catch (e) {
        _diag.errors++;
      }
    }

    // Keepalive ping cada 20s
    pingTimer = setInterval(() => {
      if (serverWs && serverWs.readyState === 1) {
        serverWs.send(JSON.stringify({ type: 'ping', ts: Date.now() }));
      }
    }, PING_MS);
  };

  serverWs.onclose = function (evt) {
    isConnecting = false;
    clearInterval(pingTimer);
    console.log(`[EVFL BG] ❌ Desconectado (code=${evt.code}). Reconectando en ${RECONNECT_MS}ms…`);
    console.log(`[EVFL BG] Estadísticas: recibidos=${_diag.received} enviados=${_diag.sent} en_cola=${frameQueue.length} errores=${_diag.errors}`);
    scheduleReconnect();
  };

  serverWs.onerror = function () {
    // onclose sigue, no duplicar logs
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connectToServer, RECONNECT_MS);
}

// ── Recibir frames de content_script.js ─────────────────────────────────────
chrome.runtime.onMessage.addListener(function (msg, sender, sendResponse) {
  if (!msg || !msg.__evfl_frame) return;

  _diag.received++;
  const payload = msg.payload || {};

  // Log de diagnóstico para el primer frame de cada WS
  const preview = (payload.data || '').slice(0, 80);
  console.log(
    `[EVFL BG] 📦 Frame #${_diag.received} | type=${payload.type} | ws=${(payload.ws_url||'').slice(-50)} | preview=${preview}`
  );

  const frame = {
    ...payload,
    tab_id:  sender.tab?.id  ?? null,
    tab_url: payload.tab_url ?? sender.tab?.url ?? '',
  };

  if (serverWs && serverWs.readyState === 1) {
    try {
      serverWs.send(JSON.stringify(frame));
      _diag.sent++;
      if (_diag.sent % 50 === 0) {
        console.log(`[EVFL BG] 📊 recibidos=${_diag.received} enviados=${_diag.sent}`);
      }
    } catch (e) {
      _diag.errors++;
      enqueue(frame);
    }
  } else {
    _diag.queued++;
    enqueue(frame);
    connectToServer();
  }
});

function enqueue(frame) {
  frameQueue.push(frame);
  if (frameQueue.length > MAX_QUEUE) {
    frameQueue.splice(0, frameQueue.length - MAX_QUEUE);
  }
}

// ── Arrancar ─────────────────────────────────────────────────────────────────
self.addEventListener('install',  () => { self.skipWaiting(); });
self.addEventListener('activate', () => { clients.claim(); connectToServer(); });
connectToServer();

console.log('[EVFL BG] Service Worker iniciado. Versión 1.1 (debug)');
