# 🕷️ EV Futbol Live — Chrome Extension Odds Bridge

## Instalación (5 minutos)

### 1. Abrir el Chrome de las apuestas (el separado)
En la barra de direcciones escribe:
```
chrome://extensions
```

### 2. Activar "Modo desarrollador"
Toggle arriba a la derecha → **ON**

### 3. Cargar la extensión
- Clic en **"Cargar descomprimida"** (Load unpacked)
- Seleccionar la carpeta:
  ```
  C:\Users\Gabo\Documents\Betting System\chrome_extension\
  ```

### 4. Verificar instalación
Deberías ver la tarjeta **"EV Futbol Live — Odds Bridge"** con estado **verde**.

---

## Cómo usar

### Flujo normal:
1. **Arranca el servidor Python** (`start_app.py`)
2. **Abre PS3838** en el Chrome de apuestas — navega al partido en vivo
3. La extensión se conecta automáticamente a `ws://localhost:8000/ws/extension`
4. En la terminal del servidor verás:
   ```
   [EXT WS] ✅ Extension conectada desde 127.0.0.1
   [EXT WS ⚡] Pinnacle Goles: 4 líneas → .../partido/...
   ```
5. En el dashboard, el badge del **Panel Sensores** cambia de `— Sin Faro` a `✅ LIVE`

### Verificar conexión desde el dashboard:
```
http://localhost:8000/api/extension/status
→ {"connected": true, "connections": 1}
```

---

## Debug: ingeniería inversa de nuevos sitios

Si el formato de WS de un sitio no es reconocido, se loguea en:
```
C:\Users\Gabo\Documents\Betting System\extension_discovery.log
```

Cada línea es un JSON con:
- `tag`: tipo de frame desconocido (ej. `PINNACLE_UNKNOWN_JSON`)
- `preview`: primeros 300 caracteres del mensaje
- `ws_url`: URL del WebSocket del sitio
- `tab_url`: URL de la pestaña

Comparte ese archivo para que añadamos el parser específico.

---

## Arquitectura técnica

```
PS3838 tab (Chrome de apuestas)
    │
    │  WebSocket nativo del sitio
    │
    ▼
inject.js [MAIN world]
  └── Proxy(WebSocket) con Reflect.construct
  └── Capture-phase listener (pre-page handlers)
  └── Soporte: text / ArrayBuffer / Blob / TypedArray
  └── window.postMessage({ __evfl_frame: true, payload })
    │
    ▼
content_script.js [ISOLATED world]
  └── window.addEventListener('message')
  └── chrome.runtime.sendMessage → background.js
    │
    ▼
background.js [Service Worker]
  └── UNA conexión persistente: ws://localhost:8000/ws/extension
  └── Buffer de 1000 frames si el servidor no está disponible
  └── Reconexión automática cada 3s
  └── Keepalive ping cada 20s
    │
    ▼
server.py /ws/extension
  └── extension_ws_parser.py → detecta sitio → parsea formato
  └── Escribe en ctx['pinnacle_fair'] (mismo formato que scraper_service)
  └── ctx['scraper_ts'] = time.time() → resetea el Safe Mode counter
```

## Anti-detección

- `inject.js` usa **Proxy + Reflect.construct** en vez de `window.WebSocket = function(){}`
- `Function.prototype.toString` override → `WebSocket.toString()` devuelve código nativo
- **Capture-phase listener** → captura antes que cualquier handler del sitio
- `world: "MAIN"` en manifest → el hook corre antes de cualquier script de la página
- La extensión es indistinguible de un usuario navegando normalmente
