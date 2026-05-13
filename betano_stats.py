"""
betano_stats.py — Fetcher de estadísticas en tiempo real de Betano.

Consume /api/statsstream/{id}/stats/detailed/ y fusiona los datos
con el MatchState de SofaScore. Betano manda en datos "rápidos"
(corners, tarjetas, ataques peligrosos); SofaScore conserva xG y tiros off-target.

Cache en memoria de 8 segundos para evitar saturar el endpoint.
Si falla, devuelve el state original (SofaScore puro) sin ningún error.
"""
import logging
import time
from dataclasses import replace

log = logging.getLogger("betano_stats")

_stats_cache: dict[int, tuple[dict, float]] = {}
_CACHE_TTL = 8.0  # segundos

# ─── Mapa de nombres de campo Betano → interno ──────────────────────────────
_FIELD_MAP = {
    "CornerKicks":       ("corners_local",              "corners_visitante"),
    "Corners":           ("corners_local",              "corners_visitante"),
    "YellowCards":       ("amarillas_local",            "amarillas_visitante"),
    "RedCards":          ("rojas_local",                "rojas_visitante"),
    "DangerousAttacks":  ("dangerous_attacks_home",     "dangerous_attacks_away"),
    "ShotsOnTarget":     ("tiros_puerta_local",         "tiros_puerta_visitante"),
    "Fouls":             ("faltas_local",               "faltas_visitante"),
    "Attacks":           ("centros_local",              "centros_visitante"),  # proxy
}


def _fetch_raw(betano_id: int) -> dict | None:
    """Hace la petición HTTP a la API de stats de Betano con curl_cffi."""
    try:
        from curl_cffi.requests import Session as CurlSession
        url = f"https://www.betano.co/api/statsstream/{betano_id}/stats/detailed/"
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "Referer": f"https://www.betano.co/live/sports/football/{betano_id}/",
            "X-Requested-With": "XMLHttpRequest",
        }
        with CurlSession(impersonate="chrome124") as s:
            r = s.get(url, headers=headers, timeout=4)
        if r.status_code != 200:
            log.debug(f"[BETANO STATS] HTTP {r.status_code} para event_id={betano_id}")
            return None
        return r.json()
    except ImportError:
        # curl_cffi no disponible — fallback silencioso
        try:
            import httpx
            url = f"https://www.betano.co/api/statsstream/{betano_id}/stats/detailed/"
            headers = {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
                "Accept": "application/json",
            }
            r = httpx.get(url, headers=headers, timeout=4.0)
            if r.status_code == 200:
                return r.json()
        except Exception as ex:
            log.debug(f"[BETANO STATS] httpx fallback failed: {ex}")
        return None
    except Exception as ex:
        log.debug(f"[BETANO STATS] Error HTTP para {betano_id}: {ex}")
        return None


def _parse_stats(data: dict) -> dict | None:
    """Extrae las estadísticas del JSON de Betano."""
    if not isinstance(data, dict):
        return None

    # Betano puede empacar stats en varios niveles según el endpoint
    stats_list = (
        data.get("data", {}).get("stats")
        or data.get("stats")
        or data.get("Statistics")
        or []
    )

    if not stats_list:
        return None

    result: dict = {}
    for stat in stats_list:
        if not isinstance(stat, dict):
            continue
        name = stat.get("name") or stat.get("type") or stat.get("statisticType") or ""
        # Soporte para estructura {home: x, away: y} y {value: [x, y]}
        home_val = stat.get("home") or stat.get("homeValue") or 0
        away_val = stat.get("away") or stat.get("awayValue") or 0
        if isinstance(stat.get("value"), list) and len(stat["value"]) >= 2:
            home_val, away_val = stat["value"][0], stat["value"][1]
        try:
            home_val = float(home_val)
            away_val = float(away_val)
        except (TypeError, ValueError):
            continue

        if name in _FIELD_MAP:
            h_key, a_key = _FIELD_MAP[name]
            result[h_key] = home_val
            result[a_key] = away_val

    return result if result else None


def fetch_betano_live_stats(betano_id: int) -> dict | None:
    """
    Retorna un dict con stats de Betano para betano_id, con cache de 8s.
    Retorna None si el endpoint no responde o no hay datos.
    """
    now = time.time()
    cached = _stats_cache.get(betano_id)
    if cached:
        cached_data, ts = cached
        if now - ts < _CACHE_TTL:
            return cached_data

    raw = _fetch_raw(betano_id)
    if raw is None:
        return None

    parsed = _parse_stats(raw)
    if parsed:
        _stats_cache[betano_id] = (parsed, now)
    return parsed


def fetch_and_merge_betano_stats(state, betano_id: int):
    """
    Descarga las stats de Betano y las fusiona con el MatchState existente.
    Betano manda en datos de tiempo real (corners, tarjetas, ataques).
    SofaScore conserva datos profundos (xG, tiros off-target, formación).

    Si Betano falla, devuelve el state original intacto (SofaScore puro).
    """
    b = fetch_betano_live_stats(betano_id)
    if not b:
        return state

    updates: dict = {}

    # ── Corners ──────────────────────────────────────────────────────────────
    if "corners_local" in b and "corners_visitante" in b:
        updates["corners_local"]    = b["corners_local"]
        updates["corners_visitante"] = b["corners_visitante"]
        updates["corners"]           = b["corners_local"] + b["corners_visitante"]

    # ── Tarjetas ─────────────────────────────────────────────────────────────
    if "amarillas_local" in b and "amarillas_visitante" in b:
        updates["amarillas_local"]    = b["amarillas_local"]
        updates["amarillas_visitante"] = b["amarillas_visitante"]
        updates["amarillas"]           = b["amarillas_local"] + b["amarillas_visitante"]

    if "rojas_local" in b and "rojas_visitante" in b:
        updates["rojas_local"]    = b["rojas_local"]
        updates["rojas_visitante"] = b["rojas_visitante"]
        updates["rojas"]           = b["rojas_local"] + b["rojas_visitante"]

    # ── Ataques peligrosos (Betano es más preciso, impacta cuotas directamente) ──
    if "dangerous_attacks_home" in b:
        updates["dangerous_attacks_home"] = b["dangerous_attacks_home"]
        updates["dangerous_attacks_away"] = b.get("dangerous_attacks_away", 0.0)

    # ── Tiros a puerta (desde Betano si disponibles) ─────────────────────────
    if "tiros_puerta_local" in b and "tiros_puerta_visitante" in b:
        updates["tiros_puerta_local"]    = b["tiros_puerta_local"]
        updates["tiros_puerta_visitante"] = b["tiros_puerta_visitante"]

    # ── Faltas (solo si Betano las tiene, SofaScore es respaldo) ─────────────
    if "faltas_local" in b and "faltas_visitante" in b:
        updates["faltas_local"]    = b["faltas_local"]
        updates["faltas_visitante"] = b["faltas_visitante"]
        updates["faltas"]           = b["faltas_local"] + b["faltas_visitante"]

    if not updates:
        return state

    try:
        merged = replace(state, **updates)
        msg = (
            f"[HYBRID] Stats fusionadas Betano+SofaScore para betano_id={betano_id} "
            f"| corners={updates.get('corners', '?')} "
            f"| amarillas={updates.get('amarillas', '?')} "
            f"| rojas={updates.get('rojas', '?')}"
        )
        log.info(msg)
        print(msg, flush=True)
        return merged
    except Exception as ex:
        log.warning(f"[HYBRID] replace() falló para betano_id={betano_id}: {ex}")
        return state
