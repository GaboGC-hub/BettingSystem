"""
extension_ws_parser.py
Parsers de mensajes WebSocket interceptados por la Chrome Extension.

Cada sitio usa un protocolo diferente. Este módulo:
1. Detecta el sitio por la tab_url
2. Intenta parsear el mensaje
3. Devuelve un dict normalizado `{ market_name, lines[], source_id }` o None

Formato normalizado de línea:
    { "linea": float, "over": float, "under": float }

Nota sobre binarios:
  Algunos sitios (ej. Kambi) pueden usar protobuf o msgpack en formato binario.
  El frame llega como base64 bajo frame["encoding"]=="base64" y frame["data"].
  Para decodificarlos hay que identificar el protocolo inspeccionando el tráfico
  con DevTools → Network → WS. Hasta entonces caen al logger de descubrimiento.
"""

from __future__ import annotations
import base64
import json
import logging
import re
import time
from pathlib import Path
from typing import Optional

try:
    import lz4.frame as _lz4_frame
    _HAS_LZ4 = True
except ImportError:
    _HAS_LZ4 = False

logger = logging.getLogger("evfl.extension")

# Archivo de descubrimiento: frames desconocidos se loguean aquí para ingeniería inversa
DISCOVERY_LOG = Path("extension_discovery.log")


# ─── Dispatch principal ───────────────────────────────────────────────────────

def parse_extension_frame(frame: dict) -> Optional[dict]:
    """
    Punto de entrada. Recibe un frame del background.js y devuelve:
        {
          "match_url":   str | None,   # URL de SofaScore asociada (si puede inferirse)
          "source_id":   str,          # "pinnacle" | "betano" | "betplay"
          "market_name": str,          # "Goles" | "Corners" | "Tarjetas"
          "lines":       list[dict],   # [{"linea": float, "over": float, "under": float}]
        }
    O None si el frame no es relevante / no puede parsearse.
    """
    tab_url   = frame.get("tab_url", "")
    ws_url    = frame.get("ws_url",  "")
    data_type = frame.get("type",    "text")
    ts        = frame.get("ts",      int(time.time() * 1000))

    if "ping" in frame.get("type", ""):
        return None  # ignorar pings del propio background.js

    if data_type == "hard_reset":
        return {"frame_type": "hard_reset", "url": frame.get("url", tab_url)}

    if data_type == "PINNACLE_RAW_DATA":
        return [{
            "frame_type": "PINNACLE_RAW_DATA",
            "source_id": "EXT_WS_PS3838",
            "payload": frame.get("payload", {}),
            "tab_url": tab_url,
            "ws_url": ws_url
        }]

    # ── Routing por sitio ────────────────────────────────────────────────────
    if data_type == "dom_scrape":
        # Es un frame ya estructurado por el DOM scraper de la extensión
        return [{
            "source_id": frame.get("source_id"),
            "market_name": frame.get("market_name"),
            "lines": frame.get("lines", []),
            "suspended": frame.get("suspended", False),
            "match_url": None,
            "home_team": frame.get("home_team", ""),
            "away_team": frame.get("away_team", ""),
        }]

    if data_type == "stats_scrape":
        # Frame de estadísticas en vivo desde el DOM de SofaScore o el widget de Betano.
        # Ya viene estructurado, solo lo normalizamos para que el server.py lo identifique.
        stats = frame.get("stats", {})
        return [{
            "source_id": frame.get("source_id"),   # EXT_SOFASCORE | EXT_BETANO_STATS
            "frame_type": "stats_scrape",
            "stats": stats,
            "match_url": stats.get("match_url"),
            "home_team": stats.get("home_team", ""),
            "away_team": stats.get("away_team", ""),
        }]

    if data_type == "sofascore_stats":
        # Frame de la API interna de SofaScore interceptada por content_script.js.
        # "data" = JSON de /statistics, "event_data" = JSON de /event/{id} (equipos, torneo, minuto)
        try:
            import json as _j
            raw = _j.loads(frame.get("data", "{}"))
            event_id = frame.get("event_id")

            # ── Extraer metadatos del partido desde event_data ────────────────
            home_team = ""
            away_team = ""
            tournament = ""
            minute     = 0
            try:
                ev_raw = _j.loads(frame.get("event_data", "{}"))
                ev = ev_raw.get("event", {})
                home_team  = ev.get("homeTeam", {}).get("name", "")
                away_team  = ev.get("awayTeam", {}).get("name", "")
                tournament = ev.get("tournament", {}).get("name", "")
                # Minuto: SofaScore guarda el tiempo jugado en seconds
                t = ev.get("time", {})
                status_code = ev.get("status", {}).get("code", 0)
                status_text = ev.get("status", {}).get("type", "inprogress")
                minute = t.get("played", t.get("minute", 0)) or 0
                if minute == 0 and status_code == 31:
                    minute = 45  # Halftime
                elif minute == 0 and status_code in (6, 7): # In progress
                    ts = t.get("currentPeriodStartTimestamp")
                    if ts:
                        m = int((time.time() - ts) / 60)
                        if status_code == 7: m += 45
                        minute = m
            except Exception:
                pass

            # ── Estadísticas por periodo ──────────────────────────────────────
            # SofaScore devuelve grupos de estadísticas para cada periodo.
            # Buscamos el periodo "ALL" o el primero disponible.
            all_groups = []
            for period_block in raw.get("statistics", []):
                period = str(period_block.get("period", "")).upper()
                if period in ("ALL", "1ST", "2ND", ""):
                    all_groups.extend(period_block.get("groups", []))
                    if period == "ALL":
                        break  # Preferir ALL si existe

            # Flatten todos los statisticsItems
            stat_map = {}  # name.lower() → {"home": val, "away": val}
            for group in all_groups:
                for item in group.get("statisticsItems", []):
                    name = str(item.get("name", "")).lower().strip()
                    try:
                        h = float(str(item.get("home", "0")).replace("%", "").strip() or 0)
                        a = float(str(item.get("away", "0")).replace("%", "").strip() or 0)
                    except Exception:
                        h, a = 0.0, 0.0
                    stat_map[name] = {"home": h, "away": a}

            def _get(keys, default=0.0):
                for k in keys:
                    if k in stat_map:
                        return stat_map[k]
                return {"home": default, "away": default}

            poss   = _get(["ball possession", "possession"])
            shots  = _get(["total shots", "shots"])
            sot    = _get(["shots on target", "on target"])
            crns   = _get(["corner kicks", "corners"])
            fouls  = _get(["fouls", "fauls"])
            ylws   = _get(["yellow cards", "yellow"])
            reds   = _get(["red cards", "red"])
            xg     = _get(["expected goals", "xg"])
            tib    = _get(["touches in penalty area", "touches in box", "box touches"])
            da     = _get(["dangerous attacks", "dangerous attack"])

            stats_out = {
                "event_id":                 event_id,
                "home_team":                home_team,
                "away_team":                away_team,
                "tournament":               tournament,
                "minute":                   float(minute),
                "possession_home":          poss["home"],
                "shots_home":               shots["home"],
                "shots_away":               shots["away"],
                "shots_on_target_home":     sot["home"],
                "shots_on_target_away":     sot["away"],
                "corners_home":             crns["home"],
                "corners_away":             crns["away"],
                "corners_total":            crns["home"] + crns["away"],
                "fouls_home":               fouls["home"],
                "fouls_away":               fouls["away"],
                "fouls_total":              fouls["home"] + fouls["away"],
                "yellows_home":             ylws["home"],
                "yellows_away":             ylws["away"],
                "yellows_total":            ylws["home"] + ylws["away"],
                "reds_home":                reds["home"],
                "reds_away":                reds["away"],
                "reds_total":               reds["home"] + reds["away"],
                "xg_home":                  xg["home"],
                "xg_away":                  xg["away"],
                "touches_in_box_home":      tib["home"],
                "touches_in_box_away":      tib["away"],
                "dangerous_attacks_home":   da["home"],
                "dangerous_attacks_away":   da["away"],
                "status_code":              status_code,
                "status_text":              status_text,
            }
            return [{
                "source_id":  "EXT_SOFASCORE",
                "frame_type": "stats_scrape",
                "stats":      stats_out,
                "match_url":  None,
                "home_team":  home_team,
                "away_team":  away_team,
            }]
        except Exception as e:
            print(f"[EXT PARSER] Error parseando sofascore_stats: {e}")
            return []

    if data_type == "sofascore_incidents":
        # Incidentes: goles, tarjetas, sustituciones.
        # Formato: { "incidents": [{ "incidentType": "goal", "time": 45, "isHome": true, ... }] }
        # Extraemos marcador y tarjetas del listado de incidentes.
        try:
            import json as _j
            raw = _j.loads(frame.get("data", "{}"))
            incidents = raw.get("incidents", [])
            goals_home = goals_away = 0
            yellows_home = yellows_away = 0
            reds_home = reds_away = 0

            for inc in incidents:
                itype = str(inc.get("incidentType", "")).lower()
                is_home = bool(inc.get("isHome", False))
                if itype in ("goal", "owngoal") and itype != "missedpenalty":
                    if itype == "owngoal":
                        is_home = not is_home  # gol en propia → suma al contrario
                    if is_home: goals_home += 1
                    else:       goals_away += 1
                elif itype == "card":
                    color = str(inc.get("incidentClass", "")).lower()
                    if "yellow" in color:
                        if is_home: yellows_home += 1
                        else:       yellows_away += 1
                    elif "red" in color or "yellowred" in color:
                        if is_home: reds_home += 1
                        else:       reds_away += 1

            return [{
                "source_id":  "EXT_SOFASCORE",
                "frame_type": "stats_scrape",
                "stats": {
                    "event_id":      frame.get("event_id"),
                    "goals_home":    goals_home,
                    "goals_away":    goals_away,
                    "yellows_home":  yellows_home,
                    "yellows_away":  yellows_away,
                    "yellows_total": yellows_home + yellows_away,
                    "reds_home":     reds_home,
                    "reds_away":     reds_away,
                    "reds_total":    reds_home + reds_away,
                },
                "match_url":  None,
                "home_team":  "",
                "away_team":  "",
            }]
        except Exception as e:
            print(f"[EXT PARSER] Error parseando sofascore_incidents: {e}")
            return []

    if any(h in tab_url for h in ("ps3838.com", "pinnacle.com")):
        return _parse_pinnacle(frame, data_type) or []

    elif any(h in tab_url for h in ("betano.com", "betano.com.co", "betano.co")):
        return _parse_betano(frame, data_type) or []

    elif "betplay.com.co" in tab_url or "kambi.com" in tab_url:
        return _parse_kambi(frame, data_type) or []

    else:
        # Sitio desconocido — loguear para descubrimiento
        _log_discovery("UNKNOWN_SITE", frame)
        return []


# ─── Parser PS3838 / Pinnacle ─────────────────────────────────────────────────
# Pinnacle/PS3838 envía actualizaciones de cuotas vía WebSocket en formato JSON.
# El protocolo tiene mensajes de tipo "odds" con una estructura de mercados anidada.
# Estructura observada (puede cambiar):
# {
#   "msgType": "oddsChange",  (o "PriceUpdate", "fixture", etc.)
#   "events": [
#     {
#       "eventId": int,
#       "periods": {
#         "0": {   // periodo (0=full match, 1=1H, 2=2H)
#           "totals": {
#             "2.5": { "over": 1.87, "under": 2.05 },
#             ...
#           }
#         }
#       }
#     }
#   ]
# }

_PINNACLE_MARKET_MAP = {
    # Nombres de mercado en el WS de Pinnacle → nombre interno del sistema
    "totals":    "Goles",      # totals del partido
    "corners":   "Corners",    # si Pinnacle tiene mercado de corners explícito
    "cards":     "Tarjetas",
}

def _parse_pinnacle(frame: dict, data_type: str) -> Optional[dict]:
    raw = _decode_text(frame, data_type)
    if raw is None:
        _log_discovery("PINNACLE_BINARY", frame)
        return None

    try:
        msg = json.loads(raw)
    except json.JSONDecodeError:
        return None  # no es JSON, ignorar

    msg_type = str(msg.get("msgType") or msg.get("type") or "").lower() if isinstance(msg, dict) else ""

    results = []

    # ── Estructura tipo 1: "oddsChange" con "events" ─────────────────────────
    if isinstance(msg, dict):
        for event in (msg.get("events") or []):
            periods = event.get("periods") or {}
            for period_key, period_data in periods.items():
                if str(period_key) != "0":
                    continue  # solo full time

            for ws_market, internal_market in _PINNACLE_MARKET_MAP.items():
                totals_data = period_data.get(ws_market) or period_data.get("totals")
                if not totals_data:
                    continue
                market_status = str(totals_data.get("status", "")).lower()
                period_status = str(period_data.get("status", "")).lower()
                is_suspended = (period_status in ("2", "suspended", "closed") or
                                market_status in ("2", "suspended", "closed") or
                                totals_data.get("isSuspended") is True or
                                period_data.get("isSuspended") is True)

                lines = _extract_pinnacle_lines(totals_data)
                if lines or is_suspended:
                    results.append({
                        "source_id":   "pinnacle",
                        "market_name": internal_market,
                        "lines":       [] if is_suspended else lines,
                        "suspended":   is_suspended,
                        "match_url":   None,  # no podemos inferir la URL de SofaScore desde el frame
                    })

    # ── Estructura tipo 2: formato plano ─────────────────────────────────────
    if not results and isinstance(msg, dict) and "totals" in msg:
        status = str(msg["totals"].get("status", "")).lower()
        is_suspended = (status in ("2", "suspended", "closed") or msg["totals"].get("isSuspended") is True)
        lines = _extract_pinnacle_lines(msg["totals"])
        if lines or is_suspended:
            results.append({
                "source_id":   "pinnacle",
                "market_name": "Goles",
                "lines":       [] if is_suspended else lines,
                "suspended":   is_suspended,
                "match_url":   None,
            })

    # ── Estructura tipo 3: Formato Compacto (Array) de PS3838 ─────────────────
    def process_match_array(match_arr, internal_market, totals_index):
        if len(match_arr) < 9 or not isinstance(match_arr[8], dict): return
        home = str(match_arr[1]).replace(" (Corners)", "").strip()
        away = str(match_arr[2]).replace(" (Corners)", "").strip()
        periods = match_arr[8]
        if '0' not in periods: return
        p_data = periods['0']
        if not isinstance(p_data, list) or len(p_data) <= totals_index: return
        
        totals = p_data[totals_index]
        if not isinstance(totals, list): return
        
        lines = []
        for t in totals:
            if not isinstance(t, list) or len(t) < 4: continue
            try:
                linea = float(t[1])
                over = float(t[2])
                under = float(t[3])
                lines.append({'linea': linea, 'over': over, 'under': under})
            except (ValueError, TypeError):
                continue
        if lines:
            results.append({
                'source_id': 'pinnacle',
                'market_name': internal_market,
                'lines': lines,
                'suspended': False,
                'match_url': None,
                'home_team': home,
                'away_team': away
            })

    if "l" in msg and isinstance(msg["l"], list):
        for s in msg["l"]:
            if isinstance(s, list) and len(s) > 2 and isinstance(s[2], list):
                for le in s[2]:
                    if isinstance(le, list) and len(le) > 2 and isinstance(le[2], list):
                        for m in le[2]: 
                            if isinstance(m, list): process_match_array(m, "Goles", 1)

    if "ce" in msg and isinstance(msg["ce"], list):
        for item in msg["ce"]:
            if isinstance(item, list) and len(item) >= 9 and isinstance(item[8], dict):
                process_match_array(item, "Corners", 3)

    if "u" in msg and isinstance(msg["u"], list):
        # Enviar un latido (heartbeat) para mantener el faro vivo en server.py
        # Esto soluciona el problema de que Pinnacle solo envia cuotas cuando cambian,
        # lo que causaba que server.py lo diera por muerto tras 15 segundos.
        # Fallback de URL a TODOS los partidos que coincidan si no hay equipo especificado.
        results.append({
            'source_id': 'pinnacle',
            'market_name': 'HEARTBEAT',
            'lines': [{'linea': 0, 'over': 0, 'under': 0}],
            'suspended': False,
            'match_url': None,
            'home_team': 'Unknown',
            'away_team': 'Unknown'
        })

    if not results and raw:
        # Frame JSON desconocido de Pinnacle → loguear para ingeniería inversa
        _log_discovery("PINNACLE_UNKNOWN_JSON", frame, preview=raw)

    return results


def _extract_pinnacle_lines(totals_data: dict) -> list[dict]:
    """Convierte el dict de totals de Pinnacle en lista normalizada."""
    lines = []
    for linea_str, prices in totals_data.items():
        try:
            linea = float(linea_str)
            over  = float(prices.get("over",  prices.get("o", 0)))
            under = float(prices.get("under", prices.get("u", 0)))
            if over > 1.0 and under > 1.0:
                lines.append({"linea": linea, "over": over, "under": under})
        except (ValueError, TypeError, AttributeError):
            continue
    return sorted(lines, key=lambda x: x["linea"])


# ─── Parser Betano ────────────────────────────────────────────────────────────
# Betano usa GraphQL subscriptions sobre WebSocket o su protocolo propio.
# Estructura aproximada (requiere verificación con DevTools):
# {
#   "type": "data",
#   "payload": {
#     "data": {
#       "markets": [
#         { "name": "Total Goals", "selections": [ {"name":"Over 2.5","odds":1.87}, ... ] }
#       ]
#     }
#   }
# }

_BETANO_MARKET_KEYWORDS = {
    # Ingles (contenthub)
    "goal":   "GOLES",
    "corner": "CORNERS",
    "card":   "TARJETAS",
    # Espanol (REST /events/{id})
    "gol":    "GOLES",
    "esqu":   "CORNERS",
    "córner": "CORNERS",
    "tarjeta": "TARJETAS",
    "amarilla": "TARJETAS",
    "roja":   "TARJETAS",
    "amonestaci": "TARJETAS",
    "disciplin": "TARJETAS",
}

# Cache por eventId: {selectionId → {market_name, linea, side}}
# Se rellena con el frame type=16 (market init) y se usa para resolver type=100
_betano_selection_cache: dict[int, dict[str, dict]] = {}
# Cache de mercados activos por eventId: {marketId → {name, isOpen}}
_betano_market_cache: dict[int, dict[int, dict]] = {}
# Timestamps de última inserción por eventId para purga TTL
_cache_event_ts: dict[int, float] = {}
_CACHE_TTL_SECONDS = 900  # 15 minutos
# Catálogo global: {betano_event_id: {id, home, away, url}} poblado desde overview
_betano_event_catalog: dict[int, dict] = {}


def _prune_stale_caches():
    """Elimina entradas de cache con más de _CACHE_TTL_SECONDS sin actividad."""
    now = time.time()
    stale_ids = [eid for eid, ts in _cache_event_ts.items() if (now - ts) > _CACHE_TTL_SECONDS]
    for eid in stale_ids:
        _betano_selection_cache.pop(eid, None)
        _betano_market_cache.pop(eid, None)
        _cache_event_ts.pop(eid, None)
    if stale_ids:
        logger.debug(f"🧹 [CACHE TTL] {len(stale_ids)} eventIds purgados de caches Betano")


# Contador para throttle de logs debug
_debug_counter = 0

def _populate_betano_caches_from_rest(data: dict) -> None:
    """
    Puebla _betano_market_cache y _betano_selection_cache desde el JSON
    de /events/{id} (formato singular: event + markets + selections a nivel top).
    Sin estos caches, ContentHub type=100 (deltas) no puede resolver IDs.
    """
    global _debug_counter
    event = data.get("event", {})
    markets_raw = data.get("markets", [])
    selections_raw = data.get("selections", [])

    if not isinstance(event, dict):
        return
    eid = event.get("id") or event.get("eventId")
    if not eid:
        return
    try:
        eid = int(eid)
    except (ValueError, TypeError):
        return

    # Indexar selecciones por ID para lookup rapido
    sel_by_id: dict[str, dict] = {}
    if isinstance(selections_raw, dict):
        for k, v in selections_raw.items():
            if isinstance(v, dict):
                sel_by_id[str(k)] = v
    elif isinstance(selections_raw, list):
        for s in selections_raw:
            if isinstance(s, dict):
                sid = str(s.get("id", ""))
                if sid:
                    sel_by_id[sid] = s

    # Normalizar mercados: puede venir como lista o como dict indexado por ID
    markets_list = []
    if isinstance(markets_raw, dict):
        markets_list = list(markets_raw.values())
    elif isinstance(markets_raw, list):
        markets_list = markets_raw

    # print(f"🧠 [CACHE REST] eventId={eid}: raw → {len(markets_list)} mercados, "
    #       f"{len(sel_by_id)} selecciones indexadas")

    if eid not in _betano_market_cache:
        _betano_market_cache[eid] = {}
    if eid not in _betano_selection_cache:
        _betano_selection_cache[eid] = {}
    _cache_event_ts[eid] = time.time()

    for mkt in markets_list:
        if not isinstance(mkt, dict):
            continue
        mkt_id = mkt.get("id")
        mkt_name = str(mkt.get("name") or "").lower()
        if any(x in mkt_name for x in ["1st", "2nd", "mitad", "half", "equipo", "1.er", "2.º", "1er", "2do", "1°", "2°", "próximo", "next", "tiempo", "local", "visitante", "home", "away", "exactos", "exacto", "impar", "doble", "ambos", "resultado", "minuto", "margen"]):
            continue
        internal = _detect_market_name(mkt_name, _BETANO_MARKET_KEYWORDS)
        if not internal:
            # if _debug_counter % 20 == 0:
            #     print(f"[BETANO_DEBUG] Mercado REST ignorado (sin keyword): name='{mkt_name}' id={mkt_id}")
            # _debug_counter += 1
            continue

        if mkt_id:
            _betano_market_cache[eid][mkt_id] = {
                "name": internal,
                "is_open": mkt.get("isOpen", True),
            }

        # Resolver selecciones — soporta todos los formatos traicioneros de Betano
        # Trampa 1: selectionIdList puede ser string "123,456" en vez de lista
        raw_refs = mkt.get("selectionIdList") or mkt.get("selectionIds") or mkt.get("selections") or mkt.get("outcomes") or []

        # Si es string tipo "123,456", convertirlo a lista
        if isinstance(raw_refs, str):
            mkt_sels = [x.strip() for x in raw_refs.split(",") if x.strip()]
        elif isinstance(raw_refs, list):
            mkt_sels = raw_refs
        elif isinstance(raw_refs, dict):
            mkt_sels = list(raw_refs.values())
        else:
            mkt_sels = []

        # Resolver referencias: IDs → buscar en sel_by_id, dicts con price → usar directo
        resolved = []
        for ref in mkt_sels:
            # Trampa 2: seleccion embebida con price → usarla directo
            if isinstance(ref, dict) and "price" in ref:
                resolved.append(ref)
                continue
            # Referencia por ID (int, str, o dict con id)
            ref_str = ""
            if isinstance(ref, dict) and "id" in ref:
                ref_str = str(ref["id"])
            elif isinstance(ref, (str, int)):
                ref_str = str(ref)
            if ref_str and ref_str in sel_by_id:
                resolved.append(sel_by_id[ref_str])
            elif ref_str:
                print(f"❌ [CRUCE FALLIDO] El mercado '{internal}' pide el ID: '{ref_str}'")
                print(f"   -> IDs disponibles en global (muestra de 5): {list(sel_by_id.keys())[:5]}")
        mkt_sels = resolved

        sels_resolved = 0
        for i, sel_ref in enumerate(mkt_sels):
            if not isinstance(sel_ref, dict):
                continue
            sel_id = str(sel_ref.get("id", ""))
            sel_name = str(sel_ref.get("name") or "").lower()
            sel_price = float(sel_ref.get("odds") or sel_ref.get("price") or 0)
            if not sel_id or sel_price <= 1.0:
                continue
            # Intentar extraer linea y side del nombre
            m_line = re.search(r"\d+\.\d+", sel_name)
            linea = float(m_line.group()) if m_line else 0.0

            # Fallback: handicap de la selección o del mercado cuando el nombre no tiene número
            if linea == 0.0:
                h = sel_ref.get("handicap") if sel_ref.get("handicap") is not None else mkt.get("handicap")
                if h is not None:
                    try:
                        linea = abs(float(h))
                    except (ValueError, TypeError):
                        linea = 0.0

            # Detectar side por keywords (siempre, no solo cuando regex matchea)
            is_over = any(x in sel_name for x in ["over", "más", "mas", "+"])
            is_under = any(x in sel_name for x in ["under", "menos", "-"])
            side = "over" if is_over else ("under" if is_under else None)

            # Fallback posicional si no se detectó side por keywords
            if not side and linea > 0:
                if len(mkt_sels) >= 2:
                    side = "under" if i == 0 else "over"
                else:
                    side = "over"
            if side and linea > 0:
                _betano_selection_cache[eid][sel_id] = {
                    "market_name": internal,
                    "linea": linea,
                    "side": side,
                }
                sels_resolved += 1

        if mkt_sels and sels_resolved == 0:
            pass
            # Solo mostrar cada 20 mercados para no inundar logs
            # if _debug_counter % 20 == 0:
            #     sample = str(mkt_sels[0])[:100] if mkt_sels else "vacio"
            #     print(f"[BETANO_DEBUG] Mercado '{mkt_name}' ({internal}): {len(mkt_sels)} refs, 0 resueltas. Ejemplo: {sample}")
            # _debug_counter += 1

        if not mkt_sels:
            pass
            # if _debug_counter % 20 == 0:
            #     print(f"[BETANO_DEBUG] Mercado '{mkt_name}' ({internal}): SIN selecciones. Keys del mercado: {list(mkt.keys())[:10]}")
            # _debug_counter += 1

    count_mkts = len(_betano_market_cache.get(eid, {}))
    count_sels = len(_betano_selection_cache.get(eid, {}))
    # print(f"🧠 [CACHE REST] eventId={eid}: {count_mkts} mercados, {count_sels} selecciones cacheadas para ContentHub")


def _parse_betano_overview(data: dict) -> list[dict]:
    """Extrae eventos del formato overview/latest o /events/{id} de Betano."""
    results = []
    events_list = None

    # ── Formato de partido UNICO (/events/{id}) ──
    # Betano manda { event: {...}, markets: [...], selections: [...] }
    # El catálogo masivo usa "events" (plural), este usa "event" (singular).
    if "event" in data and isinstance(data["event"], dict):
        event = data["event"]
        # Inyectar markets/selections del top-level dentro del event para reusar logica
        if "markets" not in event and "markets" in data:
            event["markets"] = data["markets"]
        if "selections" not in event and "selections" in data:
            event["selections"] = data["selections"]
        events_list = [event]
        # Poblar caches de IDs para que ContentHub type=100 pueda resolver deltas
        _populate_betano_caches_from_rest(data)

    # ── Formato catálogo masivo (overview/latest) ──
    if not events_list:
        # Extraer eventos CON contexto de sport desde sports.byId → zoneIdList → data.zones
        sports_by_id = data.get("sports", {}).get("byId", {})
        zones_dict = data.get("zones", {})
        if sports_by_id and zones_dict:
            extracted_with_sport = []
            for sport_id, sport_obj in sports_by_id.items():
                if not isinstance(sport_obj, dict):
                    continue
                sport_tag = str(sport_id).upper()
                for zid in sport_obj.get("zoneIdList", []):
                    zone = zones_dict.get(str(zid), {})
                    if not isinstance(zone, dict):
                        continue
                    zone_events = zone.get("events", {})
                    if isinstance(zone_events, dict):
                        for ev in zone_events.values():
                            if isinstance(ev, dict):
                                ev["_sport"] = sport_tag
                                extracted_with_sport.append(ev)
                    elif isinstance(zone_events, list):
                        for ev in zone_events:
                            if isinstance(ev, dict):
                                ev["_sport"] = sport_tag
                                extracted_with_sport.append(ev)
            if extracted_with_sport:
                events_list = extracted_with_sport
                foot_count = sum(1 for e in events_list if e.get('_sport') == 'FOOT')
                print(f"🔍 [SPORT MAP] {len(events_list)} eventos con sport (FOOT={foot_count}, otros={len(events_list)-foot_count})")

        # Fallback: busqueda recursiva sin contexto de sport
        if not events_list:
            keys_to_search = ("events", "items", "liveEvents", "matches", "fixtures", "data", "list", "topEventsV2")

            def _find_events_recursive(obj, depth=0):
                if depth > 5: return None
                if isinstance(obj, list) and obj and isinstance(obj[0], dict):
                    sample = obj[0]
                    if (sample.get("id") or sample.get("eventId")) and (sample.get("participants") or sample.get("homeTeam") or sample.get("home_team")):
                        return obj
                if isinstance(obj, dict):
                    if obj and all(isinstance(v, dict) and (v.get("id") or v.get("eventId")) for v in obj.values()):
                        return list(obj.values())
                    for k in keys_to_search:
                        v = obj.get(k)
                        if v:
                            res = _find_events_recursive(v, depth + 1)
                            if res: return res
                    for v in obj.values():
                        if isinstance(v, (dict, list)):
                            res = _find_events_recursive(v, depth + 1)
                            if res: return res
                return None

            events_list = _find_events_recursive(data)
    
    if not events_list:
        print(f"⚠️ [BETANO_PARSER] No se encontró lista de eventos en el payload. Keys top-level: {list(data.keys())}")
        return []

    logger.info(f"✅ [BETANO_PARSER] Detectados {len(events_list)} eventos en payload.")
    top_selections = data.get("selections", {}) if isinstance(data, dict) else {}
    top_markets = data.get("markets", {}) if isinstance(data, dict) else {}
    results = _parse_betano_overview_list(events_list, top_selections, top_markets)
    print(f"✅ [PARSER OVERVIEW] {len(events_list)} eventos → {len(results)} líneas de cuotas | Catálogo total: {len(_betano_event_catalog)} partidos")
    return results


def _parse_betano_overview_list(events_list: list, top_selections: dict = None, top_markets: dict = None) -> list[dict]:
    """Convierte una lista de eventos de Betano en resultados normalizados."""
    if top_selections is None:
        top_selections = {}
    if top_markets is None:
        top_markets = {}
    results = []
    for ev in events_list:
        if not isinstance(ev, dict):
            continue
        eid = ev.get("id") or ev.get("eventId")
        if not eid:
            continue

        # Extraer participantes
        parts = ev.get("participants", [])
        if len(parts) >= 2:
            home = str(parts[0].get("name", ""))
            away = str(parts[1].get("name", ""))
        elif isinstance(ev.get("homeTeam"), dict) and isinstance(ev.get("awayTeam"), dict):
            home = str(ev["homeTeam"].get("name", ""))
            away = str(ev["awayTeam"].get("name", ""))
        elif isinstance(ev.get("home_team"), dict) and isinstance(ev.get("away_team"), dict):
            home = str(ev["home_team"].get("name", ""))
            away = str(ev["away_team"].get("name", ""))
        else:
            continue

        # Poblar catálogo global para el radar (Zero-Touch Matching)
        url_parcial = ev.get("url", "")
        if url_parcial and not url_parcial.startswith("http"):
            url_full = f"https://www.betano.co{url_parcial}"
        elif url_parcial:
            url_full = url_parcial
        else:
            url_full = f"https://www.betano.co/live/{eid}/"
        sport = ev.get("_sport") or ev.get("sportId") or ev.get("sport") or ev.get("sport_id") or ""
        _betano_event_catalog[int(eid)] = {
            "id": int(eid),
            "home": home,
            "away": away,
            "url": url_full,
            "betano_url": url_full,
            "betano_event_id": int(eid),
            "is_live": True,
            "sport": str(sport).upper() if sport else "",
        }

        # Extraer mercados: marketIdList del evento → lookup en top_markets
        markets = ev.get("markets") or ev.get("market") or []
        if not markets:
            mkt_ids = ev.get("marketIdList") or []
            if mkt_ids and isinstance(top_markets, dict):
                markets = [top_markets.get(str(mid)) for mid in mkt_ids if top_markets.get(str(mid))]
            elif mkt_ids and isinstance(top_markets, list):
                markets = [m for m in top_markets if isinstance(m, dict) and m.get("id") in mkt_ids]
        if isinstance(markets, dict):
            markets = list(markets.values())
        
        if not markets:
            global _debug_counter
            if _debug_counter % 50 == 0:
                print(f"🔍 [OVERVIEW DEBUG] evento {eid} ({home} vs {away}): 0 mercados. "
                      f"top_markets={len(top_markets)}, ev marketIdList={ev.get('marketIdList', [])[:5]}")
            _debug_counter += 1

        for mkt in (markets if isinstance(markets, list) else []):
            if not isinstance(mkt, dict):
                continue
            mkt_name_raw = str(mkt.get("name") or mkt.get("title") or "").lower()
            if any(x in mkt_name_raw for x in ["1st", "2nd", "mitad", "half", "equipo", "1.er", "2.º", "1er", "2do", "1°", "2°", "próximo", "next", "tiempo", "local", "visitante", "home", "away", "exactos", "exacto", "impar", "doble", "ambos", "resultado", "minuto", "margen"]):
                continue
            internal = _detect_market_name(mkt_name_raw, _BETANO_MARKET_KEYWORDS)
            if not internal:
                continue

            selections = mkt.get("selections") or mkt.get("outcomes") or []
            # Resolver selectionIdList → referencias a top_selections
            if not selections:
                raw_refs = mkt.get("selectionIdList") or mkt.get("selectionIds") or []
                # Trampa: selectionIdList puede ser string "123,456"
                if isinstance(raw_refs, str):
                    sel_ids = [x.strip() for x in raw_refs.split(",") if x.strip()]
                else:
                    sel_ids = raw_refs if isinstance(raw_refs, list) else []
                resolved = []
                for ref in sel_ids:
                    # Trampa 2: dict con price → embebido, usar directo
                    if isinstance(ref, dict) and "price" in ref:
                        resolved.append(ref)
                        continue
                    sid_str = str(ref.get("id", "")) if isinstance(ref, dict) else str(ref)
                    sel = None
                    if isinstance(top_selections, dict):
                        sel = top_selections.get(sid_str)
                    elif isinstance(top_selections, list):
                        for s in top_selections:
                            if isinstance(s, dict) and str(s.get("id", "")) == sid_str:
                                sel = s
                                break
                    if sel:
                        resolved.append(sel)
                    elif sid_str:
                        sample_keys = list(top_selections.keys())[:5] if isinstance(top_selections, dict) else [str(s.get("id", "")) for s in (top_selections if isinstance(top_selections, list) else [])][:5]
                        print(f"❌ [CRUCE FALLIDO] El mercado '{internal}' pide el ID: '{sid_str}'")
                        print(f"   -> IDs disponibles en global (muestra de 5): {sample_keys}")

                selections = resolved
            lines = {}
            # _dbg_market = internal in ("GOLES", "CORNERS", "TARJETAS")
            # if _dbg_market and selections:
            #     first = selections[0] if isinstance(selections[0], dict) else {}
            #     print(f"🔍 [PARSER-DBG] MKT={mkt_name_raw[:40]} | keys={list(first.keys())[:12]} | price={first.get('price')} | odds={first.get('odds')} | odd={first.get('odd')} | priceStr={first.get('priceStr')} | name={first.get('name')}")
            for sel in selections:
                if not isinstance(sel, dict):
                    continue
                odds = sel.get("odds") or sel.get("price") or 0
                try:
                    odds = float(odds)
                except (ValueError, TypeError):
                    continue
                if odds <= 1.0:
                    continue
                name = str(sel.get("name") or sel.get("label") or "").lower()
                
                # Intentar extraer linea del nombre principal
                m = re.search(r"\d+\.\d+", name)
                linea = float(m.group()) if m else None
                
                # Fallback: Extraer del handicap si el nombre no tiene el número explícito
                if linea is None:
                    try:
                        h = sel.get("handicap") if sel.get("handicap") is not None else mkt.get("handicap")
                        if h is not None:
                            linea = abs(float(h)) # abs() es CLAVE: a veces Betano manda under como -5.5
                    except (ValueError, TypeError):
                        pass

                if linea is None or linea == 0.0:
                    continue

                # Evitamos falsos positivos priorizando palabras completas
                is_over = any(word in name for word in ["over", "más", "mas", "above"]) or ("+" in name and "over" not in name)
                is_under = any(word in name for word in ["under", "menos", "below"]) or ("-" in name and "under" not in name)

                # Si ambos fallan pero sólo es "1" o "2", a veces Betano asume O/U posicional
                if not is_over and not is_under:
                    # En mercados de goles/tarjetas el ID o orden puede importar, pero 
                    # si tiene "menos" o "mas" se atrapa aquí
                    if "menos" in name or "under" in name:
                        is_under = True
                    elif "mas" in name or "más" in name or "over" in name:
                        is_over = True

                if linea not in lines:
                    lines[linea] = {"linea": linea, "over": 0.0, "under": 0.0, "is_verified": True}
                
                # Si name tiene "menos" y "mas" por alguna razón loca, prevalece el que tenga más sentido
                if is_over and not is_under:
                    lines[linea]["over"] = odds
                elif is_under and not is_over:
                    lines[linea]["under"] = odds
                elif is_over and is_under:
                    # Si tiene ambos, ej "No más / Menos", asume Under (muy raro)
                    if "menos" in name or "under" in name:
                        lines[linea]["under"] = odds
                else:
                    # Fallback final por posición de la cuota/id si no tiene nombre claro
                    pass

            valid_lines = [v for v in sorted(lines.values(), key=lambda x: x["linea"]) if v["over"] > 1.0 and v["under"] > 1.0]
            if internal == "TARJETAS" and not valid_lines and lines:
                print(f"🚨 [DROPPED TARJETAS] mkt={mkt_name_raw} lines={lines}")
            if valid_lines:
                results.append({
                    "source_id":   "betano",
                    "market_name": internal,
                    "lines":       valid_lines,
                    "home_team":   home,
                    "away_team":   away,
                    "match_url":   str(eid),
                })

    return results


def _normalize_signalr_payload(msg) -> list:
    """
    Middleware universal: extrae argumentos de cualquier formato SignalR de Betano.
    
    Soporta todos los formatos detectados hasta ahora:
    - Respuesta a invocacion (R):  {'C':..., 'R': <str|list>}  → estado base del partido
    - Formato moderno directo:     {'target': 'NewLiveEventDiff', 'arguments': [...]}
    - Formato moderno en sobre:    {'C':..., 'M': [{'target': '...', 'arguments': [...]}]}
    - Formato clasico en sobre:    {'C':..., 'M': [{'M': '...', 'A': [...]}]}
    
    Centraliza TODO el desempaquetado SignalR en un solo lugar.
    """
    extracted_args = []
    valid_targets = {"NewLiveEventDiff", "NewLiveOverviewDiffs", "OnActiveLiveEventVersion"}

    # Normalizar a lista para procesamiento uniforme
    msg_list = msg if isinstance(msg, list) else [msg]

    for envelope in msg_list:
        if not isinstance(envelope, dict):
            continue

        # ── 1. Capturar respuesta a invocacion (R): estado base del partido ──
        if "R" in envelope:
            r_data = envelope["R"]
            if isinstance(r_data, list):
                extracted_args.extend(r_data)
            else:
                extracted_args.append(r_data)
            print(f"🔓 [SOBRE ABIERTO] Respuesta 'R' capturada → {len(extracted_args)} args acumulados")
            continue  # R y M son mutuamente excluyentes en SignalR

        # ── 2. Detectar y ABRIR el sobre de mensajes {'M': [...]} ──
        inner_messages = []
        if "M" in envelope and isinstance(envelope["M"], list):
            inner_messages = envelope["M"]
        else:
            inner_messages = [envelope]

        # ── 3. Extraer argumentos del contenido interno ──
        for item in inner_messages:
            if not isinstance(item, dict):
                continue
            # Formato moderno (ASP.NET Core): target + arguments
            if item.get("target") in valid_targets:
                args = item.get("arguments", [])
                extracted_args.extend(args)
                print(f"🔓 [SOBRE ABIERTO] Senal '{item['target']}' → {len(args)} argumentos (formato moderno)")
            # Formato clasico (ASP.NET legacy): M + A
            elif item.get("M") in valid_targets:
                args = item.get("A", [])
                extracted_args.extend(args)
                print(f"🔓 [SOBRE ABIERTO] Senal '{item['M']}' → {len(args)} argumentos (formato clasico)")

    return extracted_args


def _parse_betano(frame: dict, data_type: str) -> list[dict]:
    # Podar caches cada ~50 frames (~cada 1-2 min con tráfico normal)
    if int(time.time()) % 50 == 0:
        _prune_stale_caches()
    ws_url = frame.get("ws_url", "UNKNOWN_URL")
    print(f"🕵️ [BETANO_DEBUG_WS] LLegó un websocket de Betano: {ws_url[:100]}")
    logger.info(f"[BETANO_DEBUG_ALL] Frame recibido desde: {ws_url[:100]}")
    raw = _decode_text(frame, data_type)
    if raw is None:
        _log_discovery("BETANO_BINARY", frame)
        return []

    # SignalR messages are delimited by 0x1e
    raw = raw.strip('\x1e')
    # --- DIAGNÓSTICO DE SCROLL (isInit=false) ---
    try:
        msg = json.loads(raw)
        if "isInit=false" in ws_url or "overview" in ws_url:
            print(f"🔥 [ADUANA ABIERTA] Frame de OVERVIEW/SCROLL recibido. URL: {ws_url[:150]}")
            if isinstance(msg, dict):
                keys = list(msg.keys())
                print(f"  Keys principales: {keys}")
                if "data" in msg and isinstance(msg['data'], dict):
                    print(f"  Keys de 'data': {list(msg['data'].keys())}")
            elif isinstance(msg, list):
                print(f"  Es una LISTA de {len(msg)} elementos.")
    except json.JSONDecodeError:
        logger.warning(f"❌ [BETANO_PARSER] Error decodificando JSON desde {ws_url[:100]}")
        return []

    # --- Ignorar eventos irrelevantes tempranamente ---
    if isinstance(msg, dict):
        if msg.get("target") in ("MatchEvent",):
            return []
        if msg.get("type") in (3, 6):  # SignalR keepalive and empty results
            return []

    # --- Decodificar SignalR (middleware universal) ---
    extracted_args = _normalize_signalr_payload(msg)

    if extracted_args:
        all_results = []
        decompression_done = False

        for arg in extracted_args:
            # ── Bypass: dict/list ya parseado ('R') → contenthub_diff directo ──
            if isinstance(arg, dict):
                decompression_done = True
                results = _parse_betano_contenthub_diff(arg, frame.get("tab_url", ""))
                if results:
                    all_results.extend(results)
                continue

            if isinstance(arg, list):
                decompression_done = True
                for item in arg:
                    if isinstance(item, dict):
                        r = _parse_betano_contenthub_diff(item, frame.get("tab_url", ""))
                        if r:
                            all_results.extend(r)
                continue

            if not isinstance(arg, str):
                continue

            # ── String: base64 → LZ4 / GZIP / fallback ──
            try:
                decoded_bytes = base64.b64decode(arg + "==")

                # ── LZ4 Frame (magic = 04 22 4D 18) ──────────────────────
                if _HAS_LZ4 and decoded_bytes[:4] == b'\x04\x22\x4d\x18':
                    decompression_done = True
                    json_str = _lz4_frame.decompress(decoded_bytes).decode('utf-8', errors='ignore')
                    json_str = json_str.strip('\x1e\x00\r\n')
                    decompressed = json.loads(json_str)
                    if isinstance(decompressed, dict):
                        decompressed = [decompressed]
                    print(f"📦 [LZ4 OK] {len(decompressed)} diffs descomprimidos del ContentHub")
                    for diff in decompressed:
                        r = _parse_betano_contenthub_diff(diff, frame.get("tab_url", ""))
                        if r:
                            all_results.extend(r)
                    continue

                # ── GZIP (magic = 1F 8B) ─────────────────────────────────
                elif decoded_bytes[:2] == b'\x1f\x8b':
                    decompression_done = True
                    import gzip
                    json_str = gzip.decompress(decoded_bytes).decode('utf-8', errors='ignore')
                    json_str = json_str.strip('\x1e\x00\r\n')
                    msg = json.loads(json_str)
                    break

                # ── Fallback: JSON plano embebido ────────────────────────
                else:
                    start_idx = -1
                    for i, b in enumerate(decoded_bytes):
                        if b in (123, 91):  # '{' o '['
                            start_idx = i
                            break
                    if start_idx != -1:
                        decompression_done = True
                        json_str = decoded_bytes[start_idx:].decode('utf-8', errors='ignore')
                        msg = json.loads(json_str)
                        break
            except Exception as e:
                _log_discovery("BETANO_SIGNALR_ERR", frame, preview=str(e))
                continue

        if all_results:
            return all_results

        # ── Post-descompresión ──
        if "contenthub" in ws_url:
            if not decompression_done:
                print(f"⚠️ [BETANO_PARSER] {len(extracted_args)} args extraídos pero "
                      f"ninguno pudo procesarse. URL: {ws_url[:80]}")
            return []

    # ── Sin args extraídos Y contenthub → protocolo (connect/negotiate), ignorar ──
    if "contenthub" in ws_url:
        return []

    # ── Overview / REST / GraphQL (solo para URLs de overview) ──
    if isinstance(msg, dict):
        # Ignorar pushLiveVisualizationsInfo en el formato antiguo
        if "M" in msg and isinstance(msg["M"], list):
            for m_item in msg["M"]:
                if isinstance(m_item, dict) and m_item.get("M") == "pushLiveVisualizationsInfo":
                    return []

    # Protocolo GraphQL WS / REST / JSON Decodificado
    # Guard: si msg quedó como lista (JSON array de Betano), no tiene .get()
    if not isinstance(msg, dict):
        return []

    payload = msg.get("payload") or msg
    data    = payload.get("data") if isinstance(payload, dict) else payload
    if data is None:
        data = payload
    if isinstance(data, list) and len(data) > 0:
        # Paso directo a lista: overview de scroll (isInit=false) devuelve array plano
        results = _parse_betano_overview_list(data)
        if results:
            return results
    if not isinstance(data, dict):
        return []

    # ── Formato overview/latest anidado ──────────────────────────────────────
    overview_results = _parse_betano_overview(data)
    if overview_results:
        return overview_results

    # Soporte para formato REST normalizado y GraphQL anidado
    markets_raw = data.get("markets") or data.get("market") or []
    selections_dict = data.get("selections", {})

    if isinstance(markets_raw, dict):
        markets_raw = list(markets_raw.values())

    if isinstance(selections_dict, list):
        selections_dict = {str(s.get("id", i)): s for i, s in enumerate(selections_dict) if isinstance(s, dict)}

    results = []

    for mkt in markets_raw:
        if not isinstance(mkt, dict):
            continue

        mkt_name_raw = str(mkt.get("name") or mkt.get("title") or "").lower()
        
        # Filtros de exclusión rigurosos (mitades, equipos específicos)
        if any(x in mkt_name_raw for x in ["1st", "2nd", "mitad", "half", "-", "equipo"]):
            continue

        internal_name = _detect_market_name(mkt_name_raw, _BETANO_MARKET_KEYWORDS)
        if not internal_name:
            continue

        mkt_status = str(mkt.get("status", "")).lower()
        is_suspended = (mkt_status in ("suspended", "closed", "2") or mkt.get("suspended") is True)

        # Resolver selecciones: embebidas (GraphQL) o referenciadas por ID (REST)
        mkt_selections = mkt.get("selections") or mkt.get("outcomes") or []
        resolved_selections = []
        for sel in mkt_selections:
            if isinstance(sel, str) or isinstance(sel, int):
                # Referencia por ID (REST)
                if str(sel) in selections_dict:
                    resolved_selections.append(selections_dict[str(sel)])
            elif isinstance(sel, dict):
                # Objeto embebido (GraphQL)
                resolved_selections.append(sel)

        mkt["resolved_selections"] = resolved_selections

        try:
            ev_id = int(data.get("id") or data.get("eventId") or 0)
        except (ValueError, TypeError):
            ev_id = None
        lines = _extract_betano_lines(mkt, event_id=ev_id, internal_name=internal_name)
        if lines or is_suspended:
            results.append({
                "source_id":   "betano",
                "market_name": internal_name,
                "lines":       [] if is_suspended else lines,
                "suspended":   is_suspended,
                "match_url":   str(event_id) if event_id else None,
            })

    if results:
        return results

    _log_discovery("BETANO_UNKNOWN", frame, preview=raw[:300])
    return []



def _parse_betano_contenthub_diff(diff: dict, tab_url: str) -> list[dict]:
    """
    Procesa un objeto LZ4-descomprimido del contenthub de Betano.
    """
    if not isinstance(diff, dict):
        return []

    event_id = diff.get("eventId")
    msg_type = diff.get("type")
    payload  = diff.get("payload") or {}

    if not event_id:
        return []

    # Diagnostico: que tipo de diff estamos recibiendo
    mkt_count = len(payload.get("marketChanges", [])) if isinstance(payload, dict) else 0
    sel_count = len(payload.get("selectionChanges", [])) if isinstance(payload, dict) else 0
    print(f"🔍 [DIFF RECIBIDO] eventId={event_id} type={msg_type} mktChanges={mkt_count} selChanges={sel_count} keys={list(diff.keys())[:6]}")

    results = []

    # ── type=16 o type=101: estructura completa de mercados ──────────────────
    # payload.marketChanges = [{id, name, isOpen, selections:[{id, name, price}]}]
    if msg_type in (16, 101, 102):
        market_changes = payload.get("marketChanges") or []
        for mkt in market_changes:
            if not isinstance(mkt, dict):
                continue
            mkt_id   = mkt.get("id")
            mkt_name = str(mkt.get("name") or "").lower()
            is_open  = mkt.get("isOpen", True)

            # Filtrar mercados de mitades y equipos
            if any(x in mkt_name for x in ["1st", "2nd", "mitad", "half"]):
                continue

            internal = _detect_market_name(mkt_name, _BETANO_MARKET_KEYWORDS)

            # Guardar en cache de mercados
            if event_id not in _betano_market_cache:
                _betano_market_cache[event_id] = {}
            if mkt_id:
                _betano_market_cache[event_id][mkt_id] = {
                    "name": internal,
                    "is_open": is_open,
                }
            _cache_event_ts[event_id] = time.time()

            if not internal:
                continue

            # Poblar cache de selecciones
            selections = mkt.get("selections") or []
            if event_id not in _betano_selection_cache:
                _betano_selection_cache[event_id] = {}
            _cache_event_ts[event_id] = time.time()

            # Construir líneas directamente desde este frame
            lines: dict[float, dict] = {}
            for sel in selections:
                if not isinstance(sel, dict):
                    continue
                sel_id    = str(sel.get("id") or "")
                sel_name  = str(sel.get("name") or "").lower()
                sel_price = float(sel.get("price") or 0)
                # Extraer línea del nombre (ej: "Over 2.5" → 2.5)
                m = re.search(r"\d+\.\d+", sel_name)
                linea = float(m.group()) if m else None

                # Fallback: handicap de la selección o del mercado
                if linea is None:
                    try:
                        h = sel.get("handicap") if sel.get("handicap") is not None else mkt.get("handicap")
                        if h is not None:
                            linea = abs(float(h))
                    except (ValueError, TypeError):
                        pass

                if linea is None or linea == 0.0:
                    continue

                is_over  = any(x in sel_name for x in ["over", "más", "mas", "+"])
                is_under = any(x in sel_name for x in ["under", "menos", "-"])
                side = "over" if is_over else ("under" if is_under else None)
                # Cachear selección para type=100
                if sel_id:
                    _betano_selection_cache[event_id][sel_id] = {
                        "market_name": internal,
                        "linea": linea,
                        "side": side,
                    }
                if side and sel_price > 1.0:
                    if linea not in lines:
                        lines[linea] = {"linea": linea, "over": 0.0, "under": 0.0, "is_verified": True}
                    lines[linea][side] = sel_price

            valid_lines = [v for v in sorted(lines.values(), key=lambda x: x["linea"])
                           if v["over"] > 1.0 and v["under"] > 1.0]
            is_suspended = not is_open or msg_type in (101, 102)
            if valid_lines or is_suspended:
                results.append({
                    "source_id":   "betano",
                    "market_name": internal,
                    "lines":       [] if is_suspended else valid_lines,
                    "suspended":   is_suspended,
                    "match_url":   str(event_id) if event_id else None,
                })

    # ── type=100: selectionChanges — solo delta de precios ───────────────────
    # payload.selectionChanges = { "selectionId": [{id, price}] }
    elif msg_type == 100:
        selection_changes = payload.get("selectionChanges") or {}
        sel_cache = _betano_selection_cache.get(event_id, {})

        # Acumular cambios por mercado
        market_updates: dict[str, dict[float, dict]] = {}

        for sel_id_str, updates in selection_changes.items():
            cached = sel_cache.get(str(sel_id_str))
            if not cached:
                continue
            internal = cached["market_name"]
            linea    = cached["linea"]
            side     = cached["side"]
            if not internal or not side or not linea:
                continue

            for upd in (updates if isinstance(updates, list) else [updates]):
                price = float(upd.get("price") or 0)
                if price <= 1.0:
                    continue
                if internal not in market_updates:
                    market_updates[internal] = {}
                if linea not in market_updates[internal]:
                    current_over = 0.0
                    current_under = 0.0
                    for _sid, _c in sel_cache.items():
                        if _c.get("market_name") == internal and _c.get("linea") == linea:
                            if _c.get("side") == "over": current_over = _c.get("last_price", 0.0)
                            if _c.get("side") == "under": current_under = _c.get("last_price", 0.0)
                    market_updates[internal][linea] = {"linea": linea, "over": current_over, "under": current_under, "is_verified": True}
                market_updates[internal][linea][side] = price
                # Actualizar cache con el nuevo precio
                cached_linea = _betano_selection_cache[event_id].get(str(sel_id_str), {})
                if cached_linea:
                    _betano_selection_cache[event_id][str(sel_id_str)]["last_price"] = price

        for internal, lines_dict in market_updates.items():
            valid = [v for v in sorted(lines_dict.values(), key=lambda x: x["linea"])
                     if v["over"] > 1.0 and v["under"] > 1.0]
            if valid:
                results.append({
                    "source_id":   "betano",
                    "market_name": internal,
                    "lines":       valid,
                    "suspended":   False,
                    "match_url":   str(event_id) if event_id else None,
                })

    # ── type=301: liveData (score + reloj) — no es cuota, ignorar ────────────
    elif msg_type == 301:
        pass  # Podría usarse en el futuro para cruzar score en tiempo real

    if results:
        for r in results:
            print(f"💰 [PARSER BETANO] Cuotas extraídas para eventId={event_id}: {r['market_name']} -> {len(r['lines'])} líneas")
        print(f"🔥 [PARSER SALIDA] Enviando al motor estas cuotas: {results}")

    return results


def _extract_betano_lines(mkt: dict, event_id: Optional[int] = None, internal_name: Optional[str] = None) -> list[dict]:

    selections = mkt.get("resolved_selections") or []
    lines = {}
    
    # DEBUG para ver por qué falla
    # print(f"[BETANO_DEBUG] Evaluando mercado con {len(selections)} selecciones:")


    for sel in selections:
        sel_id = str(sel.get("id") or "")
        name  = str(sel.get("name") or "").lower()
        odds  = sel.get("odds") or sel.get("price") or 0
        try:
            odds = float(odds)
        except (ValueError, TypeError):
            continue
            
        print(f"  -> Sel: '{name}', odds: {odds}")

        # Extraer la línea numérica del nombre de la selección
        m = re.search(r"\d+\.\d+", name)
        linea = float(m.group()) if m else None

        # Fallback: handicap de la selección o del mercado
        if linea is None:
            try:
                h = sel.get("handicap") if sel.get("handicap") is not None else mkt.get("handicap")
                if h is not None:
                    linea = abs(float(h))
            except (ValueError, TypeError):
                pass

        if linea is None or linea == 0.0:
            continue

        if linea not in lines:
            lines[linea] = {"linea": linea, "over": 0.0, "under": 0.0, "is_verified": True}

        is_over = any(x in name for x in ["over", "más", "mas", "mas de", "+"])
        is_under = any(x in name for x in ["under", "menos", "menos de", "-"])

        side = None
        if is_over:
            lines[linea]["over"] = odds
            lines[linea]["is_verified"] = True
            side = "over"
        elif is_under:
            lines[linea]["under"] = odds
            lines[linea]["is_verified"] = True
            side = "under"

        if event_id and internal_name and side and sel_id:
            if event_id not in _betano_selection_cache:
                _betano_selection_cache[event_id] = {}
            _betano_selection_cache[event_id][sel_id] = {
                "market_name": internal_name,
                "linea": linea,
                "side": side,
                "last_price": odds,
            }

    return [v for v in sorted(lines.values(), key=lambda x: x["linea"]) if v["over"] > 1.0 and v["under"] > 1.0]


# ─── Parser Kambi (Betplay / bicdn.com) ──────────────────────────────────────
#
# Protocolo REAL observado en bicdn.com (2025):
#
#   Frame Socket.IO:  42["message","[{...},{...}]"]
#                     ^^  ^^^^^^^^  ^^^^^^^^^^^^^
#                     |   evento    payload (JSON string, hay que re-parsear)
#                     prefijo EIO
#
#   Tipos de mensaje (campo "mt"):
#     mt=6   boa   BetOffer Added   → cuotas de un mercado nuevo
#     mt=8   bosu  BetOffer Status Update → suspended/open
#     mt=11  boou  BetOffer Odds Update  → CAMBIO DE CUOTA (el que nos interesa)
#     mt=15  mcu   Match Clock Update   → minuto/score (no cuotas)
#
#   Estructura de mt=11 (boou):
#   {
#     "t": "1777166329254",      ← timestamp
#     "mt": 11,
#     "boou": {
#       "eventId": 1025985787,   ← ID del partido en Kambi
#       "betOfferId": 263448...,
#       "outcomes": [
#         { "id": ..., "odds": 1870, "type": "OT_ONE" },   ← Over (milliodds)
#         { "id": ..., "odds": 2090, "type": "OT_TWO" },   ← Under
#       ]
#     }
#   }
#
#   Estructura de mt=6 (boa) — BetOffer Added (cuotas completas):
#   {
#     "mt": 6,
#     "boa": {
#       "betOffer": {
#         "id": ..., "eventId": ...,
#         "criterion": { "label": "Over/Under 2.5" },
#         "outcomes": [
#           { "id": ..., "odds": 1870, "label": "Over", "line": 2500 },
#           { "id": ..., "odds": 2090, "label": "Under", "line": 2500 },
#         ]
#       }
#     }
#   }
#
#   Cuotas en milliodds: divide entre 1000. Ej: 1870 → 1.870

_KAMBI_MARKET_KEYWORDS = {
    "corners":     "Corners",
    "corner":      "Corners",
    "esquina":     "Corners",
    "cards":       "Tarjetas",
    "card":        "Tarjetas",
    "tarjeta":     "Tarjetas",
    "goles":       "Goles",
    "goal":        "Goles",
}

# Cache: betOfferId → (market_name, linea) para resolver mt=11 sin label
_kambi_offer_cache: dict[int, dict] = {}


def _parse_kambi(frame: dict, data_type: str) -> list[dict]:
    raw = _decode_text(frame, data_type)
    if raw is None:
        _log_discovery("KAMBI_BINARY", frame)
        return []

    if data_type == "kambi_api":
        # API Response capturada vía fetch interception
        try:
            api_data = json.loads(raw)
            # Extraer betOffers array del response HTTP
            if isinstance(api_data, dict):
                bet_offers = api_data.get("betOffers") or api_data.get("events", [])
            else:
                bet_offers = api_data
            
            if not isinstance(bet_offers, list):
                bet_offers = [bet_offers]
                
            updates = []
            for offer in bet_offers:
                if isinstance(offer, dict) and "id" in offer:
                    # Simular un mt=6 (BetOffer Added) para usar la misma lógica
                    updates.append({"mt": 6, "boa": {"betOffer": offer}})
        except json.JSONDecodeError:
            return []
    else:
        # Ignorar mensajes de control de Socket.IO (0, 3, 40, etc.)
        if not raw.startswith("42["):
            return []  # handshake / heartbeat
    
        # Strip prefijo "42"
        try:
            parts = json.loads(raw[2:])          # ["message", "STRING"]
        except json.JSONDecodeError:
            _log_discovery("KAMBI_PARSE_ERROR", frame, preview=raw[:200])
            return []
    
        if not isinstance(parts, list) or len(parts) < 2:
            return []
    
        # El payload es un JSON string que contiene un array de updates
        payload_raw = parts[1]
        if isinstance(payload_raw, str):
            try:
                updates = json.loads(payload_raw)
            except json.JSONDecodeError:
                _log_discovery("KAMBI_PAYLOAD_PARSE", frame, preview=str(payload_raw)[:200])
                return []
        elif isinstance(payload_raw, list):
            updates = payload_raw
        elif isinstance(payload_raw, dict):
            updates = [payload_raw]
        else:
            return []

    if not isinstance(updates, list):
        updates = [updates]

    results = []
    # Procesar cada update del batch
    for upd in updates:
        if not isinstance(upd, dict):
            continue
        mt = upd.get("mt")

        # mt=6: BetOffer Added — cuotas completas con label del mercado
        if mt == 6:
            result = _parse_kambi_boa(upd.get("boa", {}))
            if result:
                results.append(result)

        # mt=11: BetOffer Odds Update — solo cambio de precio
        elif mt == 11:
            result = _parse_kambi_boou(upd.get("boou", {}))
            if result:
                results.append(result)
                
    return results


def _parse_kambi_boa(boa: dict) -> Optional[dict]:
    """BetOffer Added — tiene label del mercado y cuotas completas."""
    bet_offer = boa.get("betOffer") or boa
    if not isinstance(bet_offer, dict):
        return None

    criterion = bet_offer.get("criterion") or {}
    label = str(criterion.get("label") or criterion.get("englishLabel") or "").lower()
    
    # DEBUG offline: descomentar para loguear frames desconocidos de Kambi
    # try:
    #     import json as _j
    #     with open("kambi_boa_debug.jsonl", "a", encoding="utf-8") as f:
    #         f.write(_j.dumps(bet_offer) + "\n")
    # except Exception:
    #     pass
    
    # Filtros de exclusión rigurosos (Tirano: mitades, equipos específicos, props)
    if any(x in label for x in ["1st", "2nd", "mitad", "half", "-", "equipo", "home", "away"]):
        return None
        
    market_name = _detect_market_name(label, _KAMBI_MARKET_KEYWORDS)
    if not market_name:
        return None

    outcomes = bet_offer.get("outcomes") or []
    lines = _extract_kambi_lines_full(outcomes)
    if not lines:
        return None

    # Cachear para resolver mt=11 futuros
    offer_id = bet_offer.get("id")
    event_id = bet_offer.get("eventId")
    
    if offer_id and lines:
        best = lines[0]  # línea principal
        _kambi_offer_cache[offer_id] = {
            "market_name": market_name,
            "linea": best["linea"],
            "event_id": event_id
        }

    return {
        "source_id":   "betplay",
        "market_name": market_name,
        "lines":       lines,
        "match_url":   str(event_id) if event_id else None,
    }


def _parse_kambi_boou(boou: dict) -> Optional[dict]:
    """BetOffer Odds Update — solo tiene IDs y nuevas cuotas, sin label."""
    if not isinstance(boou, dict):
        return None

    outcomes = boou.get("outcomes") or []
    if not outcomes:
        return None

    # Intentar resolver el mercado desde el cache de boa
    offer_id = boou.get("betOfferId")
    cached   = _kambi_offer_cache.get(offer_id) if offer_id else None

    if not cached:
        # IMPORTANTE: Si no conocemos este ID, NO PODEMOS asumir que son Goles.
        # Podría ser un mercado de props o equipo que el filtro Tirano rechazó previamente.
        # Asumir que es Goles envenena la data con cuotas de mercados secundarios.
        return None

    lines = _extract_kambi_lines_full(outcomes)
    if not lines:
        return None

    market_name = cached["market_name"]
    event_id = cached.get("event_id")

    return {
        "source_id":   "betplay",
        "market_name": market_name,
        "lines":       lines,
        "match_url":   str(event_id) if event_id else None,
    }


def _extract_kambi_lines_full(outcomes: list) -> list[dict]:
    """
    Extrae pares over/under desde una lista de outcomes de Kambi.
    Kambi usa milliodds (1870 = 1.870) y tiene 'line' en millipoints (2500 = 2.5).
    """
    lines: dict[float, dict] = {}

    for o in outcomes:
        if not isinstance(o, dict):
            continue

        raw_odds = o.get("odds") or o.get("decimalOdds") or 0
        try:
            # Si es milliodds (> 100), dividir por 1000
            odds = float(raw_odds) / 1000.0 if float(raw_odds) > 100 else float(raw_odds)
        except (ValueError, TypeError):
            continue

        if odds < 1.01:
            continue

        # Línea en millipoints (2500 → 2.5) o directa
        raw_line = o.get("line")
        if raw_line is not None:
            try:
                linea = float(raw_line) / 1000.0 if float(raw_line) > 100 else float(raw_line)
            except (ValueError, TypeError):
                linea = None
        else:
            linea = None

        # Si no hay 'line', intentar extraer del label
        if linea is None:
            label = str(o.get("label") or o.get("type") or "").lower()
            m = re.search(r"(\d+\.?\d*)", label)
            linea = float(m.group(1)) if m else None

        if linea is None:
            continue

        if linea not in lines:
            lines[linea] = {"linea": linea, "over": 0.0, "under": 0.0}

        # Determinar side: primero por type (OT_ONE=Over, OT_TWO=Under), luego por label
        otype = str(o.get("type") or "").upper()
        label = str(o.get("label") or "").lower()
        is_over  = "OT_ONE" in otype or "over" in label or "más" in label
        is_under = "OT_TWO" in otype or "under" in label or "menos" in label

        if is_over:
            lines[linea]["over"] = odds
        elif is_under:
            lines[linea]["under"] = odds
        else:
            # Sin etiqueta clara: asignar al primero disponible
            if lines[linea]["over"] == 0.0:
                lines[linea]["over"] = odds
            elif lines[linea]["under"] == 0.0:
                lines[linea]["under"] = odds

    return [v for v in sorted(lines.values(), key=lambda x: x["linea"]) if v["over"] > 1.01 and v["under"] > 1.01]


# ─── Utilidades ───────────────────────────────────────────────────────────────

def _decode_text(frame: dict, data_type: str) -> Optional[str]:
    """Devuelve el payload como string, o None si es binario opaco."""
    if data_type == "text":
        return frame.get("data", "")
    if data_type == "binary":
        encoding = frame.get("encoding", "base64")
        raw_b64  = frame.get("data", "")
        if not raw_b64:
            return None
        try:
            raw_bytes = base64.b64decode(raw_b64)
            # Intentar decodificar como UTF-8 (muchos "binarios" son JSON comprimido en UTF-8)
            return raw_bytes.decode("utf-8")
        except (Exception,):
            return None  # verdaderamente binario (protobuf, msgpack, etc.)
    return None


def _detect_market_name(text: str, keywords: dict) -> Optional[str]:
    """Detecta el nombre interno del mercado desde un string de texto libre."""
    text_lower = text.lower()
    
    if "tarjeta" in text_lower:
        if any(x in text_lower for x in ["roja", "jugador", "equipo", "1er", "1ra", "2da", "mitad", "primera", "segunda"]):
            return None

    for kw, name in keywords.items():
        if kw in text_lower:
            return name
    return None


def _log_discovery(tag: str, frame: dict, preview: str = "") -> None:
    """Loguea frames desconocidos para facilitar la ingeniería inversa."""
    try:
        with open(DISCOVERY_LOG, "a", encoding="utf-8") as f:
            entry = {
                "tag":     tag,
                "ts":      time.time(),
                "tab_url": frame.get("tab_url", ""),
                "ws_url":  frame.get("ws_url", ""),
                "type":    frame.get("type", ""),
                "preview": preview or (frame.get("data", "")[:150] if frame.get("type") == "text" else "<binary>"),
            }
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass
