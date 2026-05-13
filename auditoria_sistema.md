# 📊 Auditoría del Sistema — EV Futbol Live
**Fecha:** 25 de abril 2026 | **Versión auditada:** Sesión actual

---

## 1. Mapa del Sistema

```
┌─────────────────────────────────────────────────────────────────────────┐
│                        EV FUTBOL LIVE — STACK                          │
├──────────────┬──────────────┬──────────────────┬────────────────────────┤
│   DATOS IN   │   MOTOR      │   SERVIDOR       │   CLIENTES             │
│              │              │                  │                        │
│ SofaScore    │ Poisson +    │ FastAPI          │ Dashboard React        │
│ (Playwright) │ Kelly        │ server.py        │ (Vite, localhost:5173) │
│              │ fractional   │ 20 endpoints     │                        │
│ PS3838 API   │              │ WebSocket        │ Chrome Extension       │
│ (oficial)    │ futbol_live_ │ /ws/extension    │ (odds bridge)          │
│              │ betting_     │                  │                        │
│ PS3838 WS    │ probabilities│ background_loop  │                        │
│ (extensión)  │ .py — 143KB  │ (polling 10s)    │                        │
│              │              │                  │                        │
│ Betano WS    │              │ bet_lock.py      │                        │
│ (extensión)  │              │ (persistencia)   │                        │
│              │              │                  │                        │
│ Betplay WS   │              │                  │                        │
│ (extensión)  │              │                  │                        │
└──────────────┴──────────────┴──────────────────┴────────────────────────┘
```

---

## 2. Inventario de Archivos

### Backend Python
| Archivo | Tamaño | Función | Estado |
|---|---|---|---|
| `server.py` | 62 KB | API FastAPI, motor de decisiones, loops | ✅ Activo |
| `futbol_live_bridge.py` | 50 KB | Scraper SofaScore via Playwright | ✅ Activo |
| `futbol_live_betting_probabilities.py` | **143 KB** | Modelo Poisson, Kelly, markets | ✅ Activo |
| `scraper_service.py` | 31 KB | Odds scraper (PS3838 API + Betano + Betplay) | ✅ Activo |
| `ps3838_api_client.py` | 26 KB | Cliente API oficial PS3838 | ✅ Activo |
| `auth_ps3838.py` | 12 KB | Login automático + gestión de sesión | ✅ Activo |
| `bet_lock.py` | 7.6 KB | Persistent Bet State (nuevo) | ✅ Activo |
| `extension_ws_parser.py` | 14 KB | Parser WS de PS3838/Betano/Kambi | ✅ Activo |
| `analyze_history.py` | 9.8 KB | Análisis de historial | ✅ Activo |
| `backtest_metrics.py` | 8 KB | Métricas de backtesting | ✅ Activo |
| `start_app.py` | 1.5 KB | Launcher del sistema | ✅ Activo |
| `basketball.py` | 24 KB | Módulo baloncesto (sin uso activo) | ⚠️ Dormido |

### Chrome Extension
| Archivo | Función |
|---|---|
| `manifest.json` | MV3, permisos para PS3838/Betano/Betplay |
| `inject.js` | Hook WebSocket **indetectable** (Proxy+Reflect, MAIN world) |
| `content_script.js` | Bridge ISOLATED → background |
| `background.js` | WebSocket persistente a `ws://localhost:8000/ws/extension` |

### Frontend React
| Archivo | Tamaño | Función |
|---|---|---|
| `index.css` | 39 KB | Design system completo (dark theme, tokens) |
| `MatchCard.jsx` | 34 KB | Tarjeta de partido + signals + sensors + bet lock |
| `App.jsx` | 20 KB | Shell, URL bar con scrapers inline, sidebar feed |
| `AttackHeatmap.jsx` | 15 KB | Pitch-Sense SVG cenital (real vs estimado) |
| `BankrollModal.jsx` | 2.4 KB | Config de bankroll y Kelly |

---

## 3. API Surface (20 endpoints)

### Partidos
| Método | Endpoint | Función |
|---|---|---|
| `GET` | `/api/matches` | Lista todos los partidos + datos del modelo |
| `POST` | `/api/matches` | Agrega partido por URL de SofaScore |
| `DELETE` | `/api/matches` | Elimina partido |
| `POST` | `/api/matches/resolve` | Zero-Touch: resuelve URL → ID automático |
| `POST` | `/api/matches/scraper` | Lanza scrapers de cuotas |
| `POST` | `/api/matches/refresh` | Fuerza actualización inmediata |
| `POST` | `/api/matches/override` | Inyecta cuotas manuales |
| `POST` | `/api/matches/simulate` | Calculadora Fantasma (sin alterar estado) |
| `POST` | `/api/matches/xg` | Override de xG |
| `POST` | `/api/matches/override_stats` | Override de stats (goles, corners, etc.) |

### Pinnacle / PS3838
| Método | Endpoint | Función |
|---|---|---|
| `GET` | `/api/ps3838/status` | Estado del cliente API |
| `POST` | `/api/ps3838/save-session` | Guarda sesión de browser |

### Chrome Extension
| Método | Endpoint | Función |
|---|---|---|
| `WS` | `/ws/extension` | Receptor de frames WS (persistente) |
| `GET` | `/api/extension/status` | ¿Extensión conectada? |

### Bet Lock (nuevo)
| Método | Endpoint | Función |
|---|---|---|
| `GET` | `/api/bets/locks` | Lista locks activos |
| `POST` | `/api/bets/lock` | Registra apuesta → lock |
| `DELETE` | `/api/bets/lock/{id}` | Libera lock individual |
| `DELETE` | `/api/bets/locks/match` | Libera todos los locks de un partido |

### Config
| Método | Endpoint | Función |
|---|---|---|
| `GET/POST` | `/api/settings` | Bankroll, Kelly %, preset, poll interval |

---

## 4. Flujo de Datos Completo

```
SofaScore (partido en vivo)
    │
    │  Playwright headless
    │  fetch_snapshot() — estadísticas, goles, corners, xG, attack zones
    ▼
futbol_live_bridge.py → LiveSnapshot (dataclass)
    │
    ▼
futbol_live_betting_probabilities.py
    build_state_from_snapshot()
    run_model() — Poisson, probabilidades, EV
    market_summary() — Kelly, línea óptima
    │
    ├── PS3838 API oficial (< 1s lag)
    │       ↓
    ├── PS3838 browser fallback (2-5s lag)
    │       ↓
    └── Chrome Extension WS hook (< 100ms lag) ← NUEVO
            │
            ▼
         ctx["pinnacle_fair"] ← Faro de valor
            │
            ▼
         apply_pinnacle_fair_price_limit() → Edge real
            │
            ▼
         apply_sniper_lock() (in-memory, 5min cooldown)
            │
            ▼
         bet_lock.is_locked() (persistent, 90min) ← NUEVO
            │
            ▼
server.py → GET /api/matches → JSON al frontend
    │
    ▼
React Dashboard
    └── MarketSignal
        ├── Señal activa → "🔒 Aposté" button
        ├── Safe Mode banner (Faro > 45s stale)
        ├── Sensor debug panel (lags en tiempo real)
        └── Pitch-Sense SVG heatmap
```

---

## 5. Estado del Modelo

### Fuentes de cuotas (prioridad descendente)
| Fuente | Lag | Confiabilidad | Estado |
|---|---|---|---|
| PS3838 API oficial | < 1s | 🟢 Máxima | ✅ Activa si .env tiene credenciales |
| Chrome Ext. WS | < 100ms | 🟢 Alta | ⚠️ Extensión creada, falta instalar |
| PS3838 browser | 2–5s | 🟡 Media | ✅ Fallback activo |
| Betano/Betplay WS (ext.) | < 100ms | 🟢 Alta | ⚠️ Parsers en beta (descubrimiento activo) |
| Betano/Betplay browser | 5–10s | 🟡 Media | ✅ Fallback activo |

### Mercados cubiertos
| Mercado | Modelo | Faro | Estado |
|---|---|---|---|
| Goles (O/U) | ✅ Poisson + Kelly | ✅ PS3838 | Operativo |
| Corners | ✅ Distribución propia | ✅ PS3838 | Operativo |
| Tarjetas | ✅ Modelo táctico | ⚠️ PS3838 (CONMEBOL a veces sin mercado) | Parcial |

---

## 6. Historial de Partidos

**10 partidos registrados → 7.9 MB de datos JSONL**

| Resultado | Partidos |
|---|---|
| ✅ WIN | 2 |
| ❌ LOSS | 4 |
| 📋 Sin etiquetar | 4 |

> [!WARNING]
> El historial tiene solo **6 partidos etiquetados** (WIN/LOSS). Los 4 sin etiquetar no están siendo incorporados al análisis de backtesting. Pendiente etiquetar manualmente.

---

## 7. Resguardos de Seguridad Implementados

| Guardia | Mecanismo | Umbral |
|---|---|---|
| **Safe Mode** | Si Pinnacle lag > umbral → Kelly = 0 | 45 segundos |
| **Sniper Lock** (in-memory) | Cooldown entre señales iguales | 5 minutos |
| **Persistent Bet Lock** (disco) | Bloqueo físico UI post-apuesta | 90 minutos |
| **Circuit Breaker** | Máximo de stakes simultáneos | Config en settings |
| **Exposure Limit** | Límite de bankroll en riesgo | Config en settings |
| **Kill Switch** | Desactiva todas las señales | Manual desde Config |
| **Data Quality Flag** | Badge `⚠ ESTIMADO` en heatmap | Automático |

---

## 8. Deuda Técnica y Hallazgos

### 🔴 Crítico
| # | Problema | Impacto |
|---|---|---|
| 1 | Chrome Extension **no instalada** — el WS bridge no está activo | PS3838/Betano siguen en modo browser (lento) |
| 2 | `active_bets.json` no existe aún — el Bet Lock no ha sido usado | Primera apuesta lo creará automáticamente |
| 3 | Win rate empírico: **33%** (2W/4L) — muestra demasiado pequeña | No se puede evaluar el modelo aún |

### 🟡 Mejoras Pendientes
| # | Problema | Impacto |
|---|---|---|
| 4 | Los 4 partidos sin etiquetar en `live_history_v2/` no tienen WIN/LOSS | Backtesting incompleto |
| 5 | `extension_ws_parser.py` tiene parsers en **modo descubrimiento** para Betano/Betplay — los formatos exactos de WS no están verificados | Cuotas de softbooks vía extensión no parsean todavía |
| 6 | `basketball.py` (24KB) está dormido — sin integración al dashboard | Deuda de código |
| 7 | `scraper_service.py`: el bug de `greenlet.error` puede seguir ocurriendo si se llama desde contextos async | Pendiente migrar a Playwright async completo |

### 🟢 Funcionando bien
- Modelo Poisson + Kelly fractional calibrado
- Pipeline SofaScore → LiveSnapshot → señales < 10s end-to-end
- PS3838 API oficial activa (credenciales en `.env`)
- Safe Mode 45s operativo
- Dashboard en tiempo real con polling 2.5s
- Pitch-Sense SVG heatmap con dato real vs estimado
- Zero-Touch resolve (aunque con errores 502 esporádicos)

---

## 9. Próximas Acciones Recomendadas

### Prioridad 1 — Esta semana
1. **Instalar la Chrome Extension** en el Chrome de apuestas (`chrome://extensions` → cargar descomprimida → `chrome_extension/`)
2. **Etiquetar los 4 partidos sin resultado** en `live_history_v2/` (añadir `[WIN]` o `[LOSS]` al nombre del archivo)
3. **Monitorear `extension_discovery.log`** durante 2–3 partidos para mapear el formato real de WS de Betano/Betplay

### Prioridad 2 — Próxima semana
4. Actualizar `extension_ws_parser.py` con los formatos reales descubiertos
5. Correr `analyze_history.py` cuando tengamos ≥10 partidos etiquetados
6. Evaluar si el `greenlet.error` de Playwright sigue siendo un problema con la nueva arquitectura

### Prioridad 3 — Opcional
7. Eliminar o activar `basketball.py`
8. Migrar `scraper_service.py` de Playwright sync a async para eliminar los errores de greenlet
