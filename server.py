import asyncio
import threading  # solo para _resolve_sofascore_in_thread y _save_session_sync
import time
import os

# Path absoluto para archivo de diagnostico (independiente del CWD)
_DIAG_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "ext_connection.log")
import unicodedata
import uuid
from argparse import Namespace
from dataclasses import asdict
from pathlib import Path

import uvicorn
from fastapi import FastAPI, BackgroundTasks, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from contextlib import asynccontextmanager

try:
    from extension_ws_parser import parse_extension_frame
except ImportError:
    parse_extension_frame = None

from futbol_live_bridge import SofaScoreMonitor
from futbol_live_betting_probabilities import (
    build_params, build_state_from_snapshot, infer_league_profile, apply_league_profile,
    resolve_prematch_context, collect_live_markets, run_model, print_report,
    PARAMETER_PRESETS, market_summary, apply_market_guardrails,
    append_live_history, append_bet_record, append_match_closure, fair_odds_for_live_total_market,
)

from match_decision_pipeline import (
    build_reasoning,
    run_decision_pipeline,
)

# ── Calculadora de Cuotas Justas (De-Juicer para Betano) ─────────────────────
def limpiar_margen_bookie(cuota_over, cuota_under):
    """
    Toma las cuotas de una casa recreacional (Betano) y elimina el 'juice'
    para devolver las cuotas justas (Fair Odds) equivalentes a Pinnacle.
    """
    try:
        if not cuota_over or not cuota_under or cuota_over <= 1.0 or cuota_under <= 1.0:
            return None, None
        prob_over = 1.0 / cuota_over
        prob_under = 1.0 / cuota_under
        margen = prob_over + prob_under
        prob_real_over = prob_over / margen
        prob_real_under = prob_under / margen
        fair_over = round(1.0 / prob_real_over, 3)
        fair_under = round(1.0 / prob_real_under, 3)
        return fair_over, fair_under
    except Exception as e:
        print(f"Error limpiando cuotas: {e}")
        return None, None

# Safe mode: si el Faro (Pinnacle) no actualiza dentro de esta ventana, Kelly = 0 / NO BET.
FARO_STALE_SECONDS = 45

# Mocked args para funciones que esperan un Namespace del CLI
# ── NORMALIZADOR DE NOMBRES (Global para matching) ──
def _normalize_team_name(s: str) -> str:
    import unicodedata
    import re as _re
    if not s: return ""
    try:
        # Reparar mojibake
        s = s.encode('latin1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
    s = s.lower()
    s = _re.sub(r'\b(fc|afc|cf|ca|cd|nk|sk|us|fk|sc|fs|ac|as|rc|rcd|sd|ud|ad|sl|bk)\b', '', s)
    s = s.replace(" (corners)", "").replace("(corners)", "")
    for d in (" de ", " club", "deportivo ", "atletico ", "cd ", " sc"):
        s = s.replace(d, " ")
    s = _re.sub(r'[^a-z0-9\s]', '', s)
    s = _re.sub(r'\s+', ' ', s).strip()
    return s

backend_args = Namespace(
    odds_source="sofascore",
    advanced=False,
    prematch_json=None,
    no_history=False,
    history_dir=str(Path("live_history_v2")),
)

async def _supervised_polling_loop():
    """Supervisor que reinicia el polling loop si falla."""
    while True:
        try:
            await sofascore_polling_loop()
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"🔄 [POLLING SUPERVISOR] Reiniciando tras error: {e}")
            await asyncio.sleep(10)


async def _cleanup_stale_state():
    """Podar matches terminados, event_log y caches periódicamente."""
    MAX_EVENT_LOG = 1000
    STALE_MATCH_SECONDS = 3600  # 1 hora tras terminar
    while True:
        try:
            await asyncio.sleep(300)  # cada 5 minutos
            async with global_state["lock"]:
                now = time.time()
                # Podar matches terminados
                stale_urls = []
                for url, ctx in global_state["matches"].items():
                    if ctx.get("ended") and ctx.get("scraper_ts", 0) > 0:
                        if (now - ctx["scraper_ts"]) > STALE_MATCH_SECONDS:
                            stale_urls.append(url)
                for url in stale_urls:
                    del global_state["matches"][url]
                    if url in active_urls:
                        active_urls.remove(url)
                if stale_urls:
                    print(f"🧹 [CLEANUP] {len(stale_urls)} partidos terminados eliminados de memoria")
                # Podar event_log
                if len(global_state["event_log"]) > MAX_EVENT_LOG:
                    global_state["event_log"] = global_state["event_log"][-MAX_EVENT_LOG:]
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"⚠️ [CLEANUP] Error: {e}")


async def sofascore_polling_loop():
    """
    Ruta B: Server Polling DUAL.
    - Primario: Betano statsstream (cada 20s) → corners, tiros, xG, posesion, tarjetas
    - Respaldo: SofaScore (cada 300s) → minuto, incidentes, metadatos
    No ponemos los huevos en una sola canasta.
    """
    import httpx
    from curl_cffi.requests import AsyncSession as CurlSession
    import asyncio
    import json

    print("🚀 [SERVER POLLING] Dual: Betano (20s) + SofaScore (300s)...")
    
    betano_session = httpx.AsyncClient(timeout=15.0)
    sofascore_session = CurlSession(impersonate="chrome124")
    cycle = 0
    
    try:
        while True:
            try:
                cycle += 1
                await asyncio.sleep(20)  # Betano: 20s, SofaScore: cada 5 ciclos (100s)
                
                active_matches = []
                async with global_state["lock"]:
                    for url, ctx in global_state["matches"].items():
                        if not ctx.get("ended"):
                            active_matches.append((url, ctx))
                
                if not active_matches:
                    continue

                for url, ctx in active_matches:
                    betano_id = ctx.get("betano_event_id")
                    sofascore_id = ctx.get("event_id")
                    
                    # ── PRIMARIO: Betano statsstream ──────────────────────
                    if betano_id:
                        try:
                            stats_url = f"https://www.betano.co/api/statsstream/{betano_id}/stats/detailed/"
                            r = await betano_session.get(stats_url)
                            
                            if r.status_code == 200:
                                raw = r.json()
                                data = raw.get("data", {})
                                if data:
                                    home = data.get("home", {}).get("total", {})
                                    away = data.get("away", {}).get("total", {})
                                    
                                    prev_snap = ctx.get("last_snapshot")
                                    minute = getattr(prev_snap, "minute", 0) if prev_snap else 0
                                    
                                    stats = {
                                        "event_id": sofascore_id or 0,
                                        "home_team": ctx.get("home_team", ""),
                                        "away_team": ctx.get("away_team", ""),
                                        "minute": float(minute),
                                        "goals_home": home.get("goals", 0),
                                        "goals_away": away.get("goals", 0),
                                        "corners_home": home.get("corners", 0),
                                        "corners_away": away.get("corners", 0),
                                        "corners_total": home.get("corners", 0) + away.get("corners", 0),
                                        "shots_home": home.get("total_shots", 0),
                                        "shots_away": away.get("total_shots", 0),
                                        "shots_on_target_home": home.get("shots_on_target", 0),
                                        "shots_on_target_away": away.get("shots_on_target", 0),
                                        "possession_home": home.get("possession", 0),
                                        "fouls_home": home.get("fouls", 0),
                                        "fouls_away": away.get("fouls", 0),
                                        "fouls_total": home.get("fouls", 0) + away.get("fouls", 0),
                                        "yellows_home": home.get("yellow_cards", 0),
                                        "yellows_away": away.get("yellow_cards", 0),
                                        "yellows_total": home.get("yellow_cards", 0) + away.get("yellow_cards", 0),
                                        "reds_home": home.get("red_cards", 0),
                                        "reds_away": away.get("red_cards", 0),
                                        "reds_total": home.get("red_cards", 0) + away.get("red_cards", 0),
                                        "xg_home": home.get("x_goals_live", 0),
                                        "xg_away": away.get("x_goals_live", 0),
                                        "dangerous_attacks_home": home.get("dangerous_attacks", 0),
                                        "dangerous_attacks_away": away.get("dangerous_attacks", 0),
                                    }
                                    
                                    await _process_extension_frame_async(json.dumps({
                                        "type": "stats_scrape", "source_id": "EXT_BETANO_STATS",
                                        "stats": stats, "match_url": url,
                                        "home_team": ctx.get("home_team", ""),
                                        "away_team": ctx.get("away_team", ""),
                                    }))
                        except Exception as e:
                            print(f"⚠️ [POLL BETANO] {betano_id}: {e}")
                    
                    # ── RESPALDO: SofaScore (solo cada 3 ciclos = 60s) ──
                    if cycle % 3 == 0 and sofascore_id:
                        try:
                            headers = {
                                'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36',
                                'Origin': 'https://www.sofascore.com',
                                'Referer': f'https://www.sofascore.com/event/{sofascore_id}',
                            }
                            
                            # Incidentes (goles, tarjetas, minuto)
                            inc_url = f"https://api.sofascore.com/api/v1/event/{sofascore_id}/incidents"
                            r_inc = await sofascore_session.get(inc_url, headers=headers, timeout=10.0)
                            if r_inc.status_code == 200:
                                await _process_extension_frame_async(json.dumps({
                                    "type": "sofascore_incidents",
                                    "data": r_inc.text,
                                    "event_id": sofascore_id,
                                    "tab_url": f"https://www.sofascore.com/en-us/football/match/#id:{sofascore_id}"
                                }))
                            
                            # Evento (minuto, nombres, torneo)
                            r_event = await sofascore_session.get(
                                f"https://api.sofascore.com/api/v1/event/{sofascore_id}",
                                headers=headers, timeout=10.0
                            )
                            event_data = r_event.text if r_event.status_code == 200 else "{}"
                            
                            # Stats
                            r_stats = await sofascore_session.get(
                                f"https://api.sofascore.com/api/v1/event/{sofascore_id}/statistics",
                                headers=headers, timeout=10.0
                            )
                            if r_stats.status_code == 200:
                                await _process_extension_frame_async(json.dumps({
                                    "type": "sofascore_stats",
                                    "data": r_stats.text,
                                    "event_data": event_data,
                                    "event_id": sofascore_id,
                                    "tab_url": f"https://www.sofascore.com/en-us/football/match/#id:{sofascore_id}"
                                }))
                        except Exception as e:
                            print(f"⚠️ [POLL SOFASCORE] {sofascore_id}: {e}")
                    
                    import random
                    await asyncio.sleep(random.uniform(1.5, 2.5))
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"❌ [SERVER POLLING] Error: {e}")
                await asyncio.sleep(30)
    finally:
        await betano_session.aclose()
        await sofascore_session.aclose()
    
    print("🛑 [SERVER POLLING] Demonio detenido.")


def _register_task(task: asyncio.Task) -> asyncio.Task:
    global_state["_task_registry"].add(task)
    task.add_done_callback(global_state["_task_registry"].discard)
    return task


@asynccontextmanager
async def lifespan(app: FastAPI):
    global scraper_service
    if OddsScraperService:
        scraper_service = OddsScraperService(global_state)
        scraper_service.start()
    _register_task(asyncio.create_task(background_loop()))
    _register_task(asyncio.create_task(_supervised_polling_loop()))
    _register_task(asyncio.create_task(_cleanup_stale_state()))
    yield
    # Shutdown limpio
    if scraper_service:
        try:
            scraper_service._api_client and scraper_service._api_client.stop()
        except Exception:
            pass
    for task in list(global_state["_task_registry"]):
        task.cancel()

app = FastAPI(title="Futbol Live Betting API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

global_state = {
    "matches": {},
    "settings": {
        "bankroll": 1000.0,
        "kelly_fraction": 0.10,
        "preset": "balanced",
        "poll_seconds": 30,
        "initial_bankroll": 1000.0,
        "kill_switch_active": False
    },
    "lock": asyncio.Lock(),
    "event_log": [],  # list of {ts, icon, msg, type} — podado cada 1000 entradas
    "_task_registry": set(),  # {asyncio.Task} para cleanup en shutdown
}

try:
    from scraper_service import OddsScraperService
except ImportError:
    OddsScraperService = None

# Audit logger para scrapers
try:
    from scraper_logger import scraper_log
except ImportError:
    class _NoopLogger:
        def __getattr__(self, _): return lambda *a, **kw: None
    scraper_log = _NoopLogger()

# scraper_service se inicializa en el lifespan (NO a nivel de módulo)
# para evitar triple instanciación con uvicorn reload.
scraper_service = None

# Persistent Bet Lock
try:
    from bet_lock import (
        is_locked, place_lock, release_lock, release_all_for_match,
        get_all_locks, cleanup_expired as _cleanup_bet_locks,
    )
except ImportError:
    is_locked = place_lock = release_lock = release_all_for_match = get_all_locks = _cleanup_bet_locks = None


active_urls = []
monitor = None
workbook = None

import math

def sanitize_for_json(obj):
    """Recursively replace inf/nan floats with None so JSON serialization doesn't crash."""
    if isinstance(obj, dict):
        return {k: sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [sanitize_for_json(v) for v in obj]
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return None
    return obj

def update_match_math(ctx, snapshot, state, base_markets, params, prematch):
    """Thin wrapper delegating to the extracted decision pipeline."""
    ctx["_kill_switch_active"] = global_state["settings"].get("kill_switch_active", False)
    return run_decision_pipeline(
        ctx, snapshot, state, base_markets, params, prematch,
        backend_args=backend_args,
        faro_stale_seconds=FARO_STALE_SECONDS,
        is_locked_fn=is_locked,
        sanitize_fn=sanitize_for_json,
        scraper_log=scraper_log,
    )


async def background_loop():
    global monitor
    last_poll = 0
    while True:
        now = time.time()
        poll_interval = global_state["settings"]["poll_seconds"]
        
        # Leer URLs a procesar bajo lock mínimo
        async with global_state["lock"]:
            matches_to_refresh = []
            for url, ctx in global_state["matches"].items():
                if not ctx.get("ended") and (ctx.get("force_refresh") or (now - last_poll >= poll_interval)):
                    matches_to_refresh.append(url)
            
            if not matches_to_refresh:
                # Limpieza de locks expirados
                if _cleanup_bet_locks:
                    try:
                        _cleanup_bet_locks()
                    except Exception:
                        pass
                await asyncio.sleep(1)
                continue

        last_poll = now
        if monitor is None:
            monitor = SofaScoreMonitor(headless=True)

        for url in matches_to_refresh:
            # Leer ctx bajo lock, procesar fuera
            async with global_state["lock"]:
                ctx = global_state.get("matches", {}).get(url)
                if not ctx or ctx.get("ended", False):
                    continue
                ctx["_url"] = url

            EXT_FRESH_SECS = 20
            ext_ts = ctx.get("ext_sourced_ts", 0)
            # Procesar si tiene datos de extension O si tiene cuotas de Betano inyectadas
            has_betano_odds = bool(ctx.get("pinnacle_fair"))
            if (ctx.get("ext_sourced") and (now - ext_ts) < EXT_FRESH_SECS) or (has_betano_odds and ctx.get("force_refresh")):
                snapshot = ctx.get("last_snapshot")
                if snapshot is None:
                    # Si no hay snapshot pero hay cuotas, crear uno minimo
                    if has_betano_odds:
                        from types import SimpleNamespace
                        snapshot = SimpleNamespace(
                            home_team=ctx.get("home_team", "?"), away_team=ctx.get("away_team", "?"),
                            minute=0, goals_home=0, goals_away=0, corners_home=0, corners_away=0,
                            yellows_home=0, yellows_away=0, reds_home=0, reds_away=0,
                            possession_home=50, xg_home=0, xg_away=0,
                            status_text="inprogress", tournament="",
                        )
                        ctx["last_snapshot"] = snapshot
                    else:
                        continue
                try:
                    preset = global_state["settings"]["preset"]
                    from dataclasses import replace
                    params = build_params(preset, None, None)
                    params = replace(params, kelly_fraction=global_state["settings"]["kelly_fraction"], max_stake=1.0)

                    state = build_state_from_snapshot(snapshot, ctx.get("previous_state"))
                    # ── Merge Híbrido Betano (si hay betano_event_id mapeado) ──
                    _b_eid = ctx.get("betano_event_id")
                    if _b_eid:
                        try:
                            from betano_stats import fetch_and_merge_betano_stats
                            state = fetch_and_merge_betano_stats(state, int(_b_eid))
                        except Exception:
                            pass  # Fallback silencioso a SofaScore puro
                    async with global_state["lock"]:
                        ctx["previous_state"] = state

                    league_profile = infer_league_profile(snapshot=snapshot, league_name=None)
                    params = apply_league_profile(params, league_profile)

                    prematch = resolve_prematch_context(backend_args, getattr(snapshot, "home_team"), getattr(snapshot, "away_team"), workbook)

                    async with global_state["lock"]:
                        ctx["home_team"] = getattr(snapshot, "home_team", "") or ""
                        ctx["away_team"] = getattr(snapshot, "away_team", "") or ""
                        ctx["last_params"] = params
                        ctx["last_league_profile"] = league_profile
                        ctx["last_prematch"] = prematch

                    pf = ctx.get("pinnacle_fair")
                    markets = collect_live_markets(backend_args, snapshot, state, ctx.get("previous_raw_markets"), pinnacle_fair=pf)
                    async with global_state["lock"]:
                        ctx["previous_raw_markets"] = markets
                        ctx["last_snapshot"] = snapshot

                    import logging
                    if logging.getLogger("evfl").isEnabledFor(logging.DEBUG):
                        print(f"🎯 [TRACER CALL] Llamando a update_match_math desde EXT MODE para {url[-30:]}")
                    state, markets, result = update_match_math(ctx, snapshot, state, markets, params, prematch)

                    async with global_state["lock"]:
                        ctx["force_refresh"] = False
                    print(f"[EXT MODE] ✅ Recalculado desde extensión — min={getattr(snapshot,'minute',0):.0f}")
                except Exception as e:
                    import traceback; traceback.print_exc()
                    print(f"[EXT MODE] Error procesando {url}: {e}")
                continue

            # Skip SofaScore Zero-Touch
            if "sofascore.com" in url and ("#id:" not in url or ctx.get("event_id")):
                print(f"[SKIP] SofaScore Zero-Touch: esperando extensión → {url[-45:]}")
                continue
            
            # Skip URLs de softbooks — el polling loop se encarga
            # PERO no skip betano-direct (son nuestros, necesitan decision pipeline)
            if ("betano" in url or "betplay" in url) and "betano-direct" not in url:
                continue

            try:
                preset = global_state["settings"]["preset"]
                from dataclasses import replace
                params = build_params(preset, None, None)
                params = replace(params, kelly_fraction=global_state["settings"]["kelly_fraction"], max_stake=1.0)
                should_reload = ctx.get("force_refresh") or not ctx["first_pass"]
                snapshot = await asyncio.to_thread(monitor.fetch_snapshot, match_url=url, reload_page=should_reload)
                async with global_state["lock"]:
                    ctx["first_pass"] = False
                
                state = build_state_from_snapshot(snapshot, ctx.get("previous_state"))
                # ── Merge Híbrido Betano (si hay betano_event_id mapeado) ──
                _b_eid = ctx.get("betano_event_id")
                if _b_eid:
                    try:
                        from betano_stats import fetch_and_merge_betano_stats
                        state = fetch_and_merge_betano_stats(state, int(_b_eid))
                    except Exception:
                        pass  # Fallback silencioso a SofaScore puro
                async with global_state["lock"]:
                    ctx["previous_state"] = state
                
                league_profile = infer_league_profile(snapshot=snapshot, league_name=None)
                params = apply_league_profile(params, league_profile)
                
                prematch = resolve_prematch_context(backend_args, getattr(snapshot, "home_team"), getattr(snapshot, "away_team"), workbook)
                
                status_text = str(getattr(snapshot, "status_text", "")).strip().lower()
                if status_text in {"ended", "after et", "after penalties"}:
                    if not ctx.get("ended", False):
                        if not backend_args.no_history:
                            append_match_closure(Path(backend_args.history_dir), snapshot, state)
                    async with global_state["lock"]:
                        ctx["ended"] = True
                    continue
                    
                async with global_state["lock"]:
                    ctx["home_team"] = getattr(snapshot, "home_team", "") or ""
                    ctx["away_team"] = getattr(snapshot, "away_team", "") or ""

                pf = ctx.get("pinnacle_fair")
                markets = collect_live_markets(backend_args, snapshot, state, ctx.get("previous_raw_markets"), pinnacle_fair=pf)
                async with global_state["lock"]:
                    ctx["previous_raw_markets"] = markets
                    ctx["last_snapshot"] = snapshot
                    ctx["last_params"] = params
                    ctx["last_league_profile"] = league_profile
                    ctx["last_prematch"] = prematch
                
                import logging
                if logging.getLogger("evfl").isEnabledFor(logging.DEBUG):
                    print(f"🎯 [TRACER CALL] Llamando a update_match_math desde SCRAPER MODE para {url[-30:]}")
                state, markets, result = update_match_math(ctx, snapshot, state, markets, params, prematch)

                # ── APUESTA: extraer decisiones finales ──
                _data = ctx.get("data", {})
                _mkts = (_data.get("result") or {}).get("markets") or {}
                _bets_to_log: dict[str, dict] = {}
                for _mn, _mk_key in (("GOLES", "goles"), ("CORNERS", "corners"), ("TARJETAS", "tarjetas")):
                    _dec = (_mkts.get(_mk_key) or {}).get("decision") or {}
                    if _dec.get("best_side") not in ("OVER", "UNDER"):
                        continue
                    if _dec.get("best_stake", 0) <= 0:
                        continue
                    _note = str(_dec.get("note") or "")
                    if any(tag in _note for tag in ("[COOLDOWN]", "SAFE MODE", "Circuit Breaker", "Stop-Loss", "BLOQUEO PINNACLE", "Kill-Switch", "SIN CUOTA")):
                        continue
                    _bets_to_log[_mn] = _dec

                if _bets_to_log:
                    _bet_sig = tuple(
                        (mn, _bets_to_log[mn].get("best_side"), _bets_to_log[mn].get("linea"))
                        for mn in sorted(_bets_to_log.keys())
                    )
                    if ctx.get("last_bet_sig") != _bet_sig:
                        append_bet_record(Path(backend_args.history_dir), snapshot, state, _bets_to_log)
                        ctx["last_bet_sig"] = _bet_sig
                
                async with global_state["lock"]:
                    ctx["force_refresh"] = False
            except Exception as e:
                import traceback
                traceback.print_exc()
                print(f"Error procesando {url}: {e}")


class AddMatchReq(BaseModel):
    url: str

class OverrideReq(BaseModel):
    url: str
    market: str
    linea: float
    over: float
    under: float

class XgOverrideReq(BaseModel):
    url: str
    home: float
    away: float

class ResolveReq(BaseModel):
    event_id: int   # SofaScore event ID (ej: 15239011)

def _resolve_sofascore_in_thread(event_id: int) -> dict:
    from curl_cffi import requests
    
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
        'Accept': 'application/json, text/plain, */*',
        'Accept-Language': 'es-CO,es;q=0.9,en-US;q=0.8,en;q=0.7',
        'Origin': 'https://www.sofascore.com',
        'Referer': f'https://www.sofascore.com/event/{event_id}',
    }
    
    try:
        r = requests.get(f"https://api.sofascore.com/api/v1/event/{event_id}", headers=headers, impersonate="chrome124", timeout=15.0)
        if r.status_code != 200:
            return {"error": str(r.status_code)}
        r.encoding = 'utf-8'
        return r.json()
    except Exception as exc:
        return {"error": str(exc)}


@app.post("/api/matches/resolve")
async def resolve_match_urls(req: ResolveReq):
    """
    Zero-Touch Auto-Mapping:
    Dado un event_id de SofaScore, devuelve todas las URLs necesarias
    para monitorear el partido (SofaScore + Pinnacle).
    
    Usa curl_cffi en un thread aislado para no bloquear el event loop principal.
    """
    import asyncio
    import json as _json

    # ── 1. Fetch SofaScore via curl_cffi en thread aislado ──────────────────
    loop = asyncio.get_event_loop()
    result_box: dict = {}

    def _thread_fn():
        result_box["data"] = _resolve_sofascore_in_thread(req.event_id)

    t = threading.Thread(target=_thread_fn, daemon=True)
    t.start()
    # Esperar máximo 35s de forma async (sin bloquear el event loop)
    await loop.run_in_executor(None, t.join, 35)

    sf_data = result_box.get("data", {"error": "timeout — el navegador tardó más de 35s"})

    if sf_data.get("error"):
        raise HTTPException(
            status_code=502,
            detail=f"SofaScore: {sf_data['error']}. "
                   "Verifica que el event_id sea válido y el partido esté activo o programado.",
        )

    event = sf_data.get("event", {})
    home = event.get("homeTeam", {}).get("name", "")
    away = event.get("awayTeam", {}).get("name", "")
    slug = event.get("slug", "")
    custom_id = event.get("customId", "")  # Hash alfanumérico (ej: eOscob)
    tournament_name = event.get("tournament", {}).get("name", "")
    status_type = event.get("status", {}).get("type", "inprogress")

    if not home or not away:
        raise HTTPException(status_code=404, detail="Evento no encontrado o sin equipo definido.")

    # ── 2. URL de SofaScore (con hash customId para Match Closure correcto) ──
    # Formato correcto: /match/{slug}/{customId}#id:{event_id}
    if custom_id:
        sofascore_url = f"https://www.sofascore.com/en-us/football/match/{slug}/{custom_id}#id:{req.event_id}"
    else:
        sofascore_url = f"https://www.sofascore.com/en-us/football/match/{slug}#{req.event_id}"

    # ── 3. Buscar en Pinnacle via API pública ────────────────────────────────
    pinnacle_url = None
    pinnacle_event_id = None
    try:
        import urllib.request as _ureq
        pinn_api = "https://guest.api.arcadia.pinnacle.com/0.1/sports/29/matchups?primaryOnly=true&live=true"
        headers_pinn = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "application/json",
            "X-API-Key": "CmX2KcMrRmaL6qbq",
        }
        rq2 = _ureq.Request(pinn_api, headers=headers_pinn)
        with _ureq.urlopen(rq2, timeout=8) as resp2:
            pinn_data = _json.loads(resp2.read().decode())

        import unicodedata as _ud, re as _re
        def _norm(s):
            s = _ud.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii")
            return _re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()

        home_n, away_n = _norm(home), _norm(away)
        home_words = {w for w in home_n.split() if len(w) > 2}
        away_words = {w for w in away_n.split() if len(w) > 2}

        best_score = 0
        for mu in pinn_data:
            parts = mu.get("participants", [])
            if len(parts) < 2:
                continue
            h_p = set(_norm(parts[0].get("name", "")).split())
            a_p = set(_norm(parts[1].get("name", "")).split())
            score = len(home_words & h_p) + len(away_words & a_p)
            if score > best_score and score >= 2:
                best_score = score
                pinnacle_event_id = mu.get("id")

        if pinnacle_event_id:
            pinnacle_url = f"https://www.ps3838.com/es/#/league/football/all/{pinnacle_event_id}"

    except Exception:
        pass  # Pinnacle optional — usuario puede pegar la URL manualmente

    # ── Auto-registrar en global_state con todos los datos ──────────────────
    result = {
        "status": "ok",
        "event_id": req.event_id,
        "home": home,
        "away": away,
        "tournament": tournament_name,
        "is_live": status_type == "inprogress",
        "sofascore_url": sofascore_url,
        "pinnacle_url": pinnacle_url,
        "pinnacle_event_id": pinnacle_event_id,
        "betano_url": None,
    }

    # Guardar en el estado global para que las cuotas de PS3838 tengan match
    async with global_state["lock"]:
        if sofascore_url not in global_state["matches"]:
            active_urls.append(sofascore_url)
            global_state["matches"][sofascore_url] = {
                "first_pass":       True,
                "previous_markets": None,
                "previous_state":   None,
                "ended":            False,
                "data":             None,
                "overrides":        {},
                "home_team":        home,
                "away_team":        away,
                "event_id":         req.event_id,
                "pinnacle_event_id": pinnacle_event_id,
                "match_ctx": {
                    "odds_history_buffer": {"goles": [], "corners": [], "tarjetas": []},
                    "last_sharp_line": None,
                    "corner_ctx": {},
                },
            }
            print(f"📝 [AUTO-REGISTER] {home} vs {away} registrado (Pinnacle ID: {pinnacle_event_id})")

    return result


class DirectRegisterReq(BaseModel):
    home: str
    away: str
    betano_event_id: int
    betano_url: str = ""


@app.post("/api/matches/register-direct")
async def register_match_direct(req: DirectRegisterReq):
    """Registro sin SofaScore: usa team names y Betano ID directamente."""
    match_url = f"betano-direct:{req.betano_event_id}"
    async with global_state["lock"]:
        if match_url not in global_state["matches"]:
            active_urls.append(match_url)
            global_state["matches"][match_url] = {
                "first_pass": True,
                "previous_markets": None,
                "previous_state": None,
                "ended": False,
                "data": None,
                "overrides": {},
                "home_team": req.home,
                "away_team": req.away,
                "event_id": 0,
                "betano_event_id": req.betano_event_id,
                "urls": {"betano": req.betano_url or f"https://www.betano.co/live/{req.betano_event_id}/"},
                "match_ctx": {
                    "odds_history_buffer": {"goles": [], "corners": [], "tarjetas": []},
                    "last_sharp_line": None,
                    "corner_ctx": {},
                },
            }
            print(f"📝 [DIRECT REGISTER] {req.home} vs {req.away} (betano_id={req.betano_event_id})")
    return {"status": "ok", "match_url": match_url}


class BetanoIdReq(BaseModel):
    match_url: str
    betano_event_id: int
    sofascore_event_id: int | None = None


@app.post("/api/matches/betano-id")
async def set_betano_event_id(req: BetanoIdReq):
    """Asocia un Betano event_id al match registrado, para routing de cuotas Betano."""
    async with global_state["lock"]:
        if req.match_url and req.match_url in global_state["matches"]:
            ctx = global_state["matches"][req.match_url]
            ctx["betano_event_id"] = req.betano_event_id
            if "urls" not in ctx:
                ctx["urls"] = {}
            ctx["urls"]["betano"] = f"betano-event:{req.betano_event_id}"
            print(f"🔗 [BETANO-ID] Asociado por URL: {req.betano_event_id}")
            return {"status": "ok", "betano_event_id": req.betano_event_id}
        # Buscar por sofascore_event_id (el radar ya conoce el ID de SofaScore)
        if req.sofascore_event_id:
            for url, ctx in global_state["matches"].items():
                stored_eid = ctx.get("event_id")
                if stored_eid and int(stored_eid) == req.sofascore_event_id:
                    ctx["betano_event_id"] = req.betano_event_id
                    print(f"🔗 [BETANO-ID] Asociado por SofaScore ID: {req.betano_event_id} → {url[-50:]}")
                    return {"status": "ok", "matched_by_sofascore_id": True, "matched_by_event_id": True}
    return {"status": "not_found"}


@app.post("/api/betano/ingest")
async def ingest_betano_overview(payload: dict):
    """Endpoint EXCLUSIVO para el catálogo masivo (overview).
    Las cuotas en tiempo real (contenthub/signalr) viajan por WebSocket."""
    from extension_ws_parser import _parse_betano_overview, _betano_event_catalog
    data = payload.get("data", payload)
    url = payload.get("url", "")
    # Se omiten prints verbosos en base a JSON Masivo para prevenir gigabytes en log
    msg = f"🔥 [ADUANA ABIERTA] JSON Masivo detectado. URL: {url[:120]} | len={len(str(data))}"
    try:
        with open(_DIAG_LOG_PATH, "a", encoding="utf-8") as _f:
            _f.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            _f.flush()
    except Exception:
        pass

    # ── Solo catálogo. Cuotas en tiempo real → /ws/extension ──
    if "contenthub" in url or "signalr" in url:
        print(f"🚫 [ADUANA] ContentHub/SignalR rechazado en HTTP. Usa WebSocket.")
        return {"status": "rejected", "reason": "contenthub/signalr debe fluir por WebSocket"}

    try:
        resultados = _parse_betano_overview(data)
    except Exception as e:
        return {"status": "error", "message": str(e)}

    # Silenciado el log msg2 de PARSER overview para no llenar server_output.log

    if resultados:
        injected = 0
        async with global_state["lock"]:
            for r in resultados:
                if r.get("source_id") != "betano":
                    continue
                match_url = r.get("match_url", "")
                home = r.get("home_team", "")
                away = r.get("away_team", "")
                # Buscar partido registrado por betano_event_id o por nombres de equipo
                found_ctx = None
                murl_target = None
                for murl, ctx in global_state["matches"].items():
                    # Priority 1: exact betano_event_id match
                    stored_beid = str(ctx.get("betano_event_id", ""))
                    if stored_beid and stored_beid == str(match_url):
                        found_ctx = ctx
                        murl_target = murl
                        break
                    # Priority 2: team name fuzzy match
                    if home and away:
                        ctx_home = str(ctx.get("home_team", "")).lower()
                        ctx_away = str(ctx.get("away_team", "")).lower()
                        if ctx_home and ctx_away:
                            h_match = ctx_home in home.lower() or home.lower() in ctx_home
                            a_match = ctx_away in away.lower() or away.lower() in ctx_away
                            if h_match and a_match:
                                found_ctx = ctx
                                murl_target = murl
                                break
                                
                if not found_ctx:
                    continue
                
                ctx = found_ctx
                # ── Partido encontrado: inyectar cuotas ──
                lines = r.get("lines", [])
                if lines:
                    old = ctx.get("data") or {}
                    old_snap = old.get("snapshot") or {}
                    old_state = old.get("state") or {}
                    ctx["data"] = {
                        "home_team": home,
                        "away_team": away,
                        "snapshot": {
                            "home_team": home,
                            "away_team": away,
                            "minute": old_snap.get("minute", 0),
                            "goals_home": old_snap.get("goals_home", 0),
                            "goals_away": old_snap.get("goals_away", 0),
                            "corners_home": old_snap.get("corners_home", 0),
                            "corners_away": old_snap.get("corners_away", 0),
                            "yellows_home": old_snap.get("yellows_home", 0),
                            "yellows_away": old_snap.get("yellows_away", 0),
                            "reds_home": old_snap.get("reds_home", 0),
                            "reds_away": old_snap.get("reds_away", 0),
                            "possession_home": 50,
                            "event_id": ctx.get("event_id", 0),
                            "status_text": "inprogress",
                            "tournament": old_snap.get("tournament", ""),
                        },
                        "state": {
                            "goles_local": old_state.get("goles_local", 0),
                            "goles_visitante": old_state.get("goles_visitante", 0),
                            "corners": old_state.get("corners", 0),
                            "amarillas": old_state.get("amarillas", 0),
                            "rojas": old_state.get("rojas", 0),
                            "xg_local": old_state.get("xg_local", 0),
                            "xg_visitante": old_state.get("xg_visitante", 0),
                            "faltas": old_state.get("faltas", 0),
                        },
                        "result": {
                            "home_team": home,
                            "away_team": away,
                            "markets": old.get("result", {}).get("markets", {}),
                            "tension_index": old.get("result", {}).get("tension_index", 0),
                        },
                        "scraper_ts": time.time(),
                        "sofascore_ts": old.get("sofascore_ts", 0),
                        "phase_summary": old.get("phase_summary", ""),
                    }
                    mkt_key = r["market_name"].lower()
                    ctx["data"]["result"]["markets"][mkt_key] = {"lines": lines, "source": "betano"}
                    ctx["force_refresh"] = True
                    if not ctx.get("last_snapshot"):
                        from types import SimpleNamespace
                        ctx["last_snapshot"] = SimpleNamespace(
                            home_team=home, away_team=away, minute=0,
                            goals_home=0, goals_away=0, corners_home=0, corners_away=0,
                            yellows_home=0, yellows_away=0, reds_home=0, reds_away=0,
                            possession_home=50, xg_home=0, xg_away=0,
                            status_text="inprogress", tournament="",
                            yellows_total=0, reds_total=0, fouls_total=0, corners_total=0,
                            shots_home=0, shots_away=0, shots_on_target_home=0, shots_on_target_away=0,
                            goals_market=None, corners_market=None, cards_market=None
                        )
                    ctx["ext_sourced"] = True
                    ctx.setdefault("ext_sourced_ts", time.time())
                    pf = ctx.setdefault("pinnacle_fair", {})
                    best_line = min(lines, key=lambda x: abs(x["over"] - x["under"])) if lines else None
                    market_upper = r["market_name"].upper()
                    if best_line and best_line.get("over", 0) > 1.0:
                        pf[market_upper] = [{
                            "linea": best_line["linea"],
                            "over": best_line["over"],
                            "under": best_line["under"],
                            "source_id": "betano",
                            "is_verified": True,
                        }]
                        pf[f"_last_updated_{market_upper}"] = time.time()
                    injected += 1
    return {
        "status": "ok",
        "lines": len(resultados),
        "catalog_total": len(_betano_event_catalog),
    }


@app.get("/api/betano/catalog")
async def get_betano_catalog():
    """Devuelve el catalogo de Betano poblado desde los frames overview interceptados.
    El orquestador consulta este endpoint para enriquecer su radar de matching."""
    from extension_ws_parser import _betano_event_catalog
    return {
        "events": list(_betano_event_catalog.values()),
        "count": len(_betano_event_catalog),
    }


class ScraperAttachReq(BaseModel):
    url: str
    scrapers: dict
    pinnacle_event_id: int | None = None  # Opcional: si se provee, la API lo usa directamente

@app.post("/api/matches/scraper")
async def attach_scraper(req: ScraperAttachReq):
    if not scraper_service:
        return {"status": "error", "message": "Scraper service not initialized"}

    # Si se provee el event_id explícitamente, inyectarlo en la URL del scraper
    # para que OddsScraperService pueda activar la API oficial sin re-parsear la URL.
    scrapers = req.scrapers
    if req.pinnacle_event_id and scrapers.get("pinnacle") and "pinnacle_event_id" not in str(scrapers["pinnacle"]):
        # El client extrae el ID de la URL — nos aseguramos de que esté presente
        p_url = scrapers["pinnacle"]
        if str(req.pinnacle_event_id) not in p_url:
            scrapers = dict(scrapers)
            scrapers["pinnacle"] = p_url.rstrip("/") + f"/" + str(req.pinnacle_event_id)

    scraper_service.attach_scrapers(req.url, scrapers)
    api_mode = (
        scraper_service._api_client is not None
        and scraper_service._api_client.is_watching(req.url)
    )
    return {
        "status": "ok",
        "pinnacle_mode": "api_oficial" if api_mode else "browser_fallback",
    }


@app.get("/api/ps3838/status")
async def ps3838_status():
    """
    Diagnostico completo del Faro PS3838.
    Expone greylist_suspected (Alerta Roja) y niveles de backoff actuales.
    """
    if not scraper_service or not scraper_service._api_client:
        return {
            "mode": "browser_fallback",
            "api_active": False,
            "reason": "Sin credenciales PS3838_USER / PS3838_PASS en .env",
        }
    client = scraper_service._api_client
    with client._lock:
        watched = dict(client._watched)
    return {
        "mode":                "api_oficial",
        "api_active":          True,
        "watched_events":      watched,
        "delta_since_main":    client._since_main,
        "delta_since_special": client._since_special,
        "backoff_main_s":      client._backoff_main,
        "backoff_special_s":   client._backoff_special,
        # Alerta Roja: True si el backoff llegó a 120s (posible lista gris)
        "greylist_suspected":  client.greylist_suspected,
        "alert": (
            "ALERTA ROJA: IP posiblemente en lista gris de PS3838. "
            "Detén el servidor y espera 10+ min antes de reiniciar."
            if client.greylist_suspected else None
        ),
    }


@app.post("/api/ps3838/save-session")
async def save_ps3838_session():
    """
    Abre un browser Chromium visible para que hagas login manual en ps3838.com.
    Una vez que presiones Enter en la consola del servidor, guarda las cookies
    en ps3838_session.json para que el scraper las reutilice sin volver a loguearse.

    Úsalo UNA VEZ para configurar la sesión. Después el scraper usará las cookies
    automáticamente (modo browser-con-cuenta).
    """
    import asyncio
    loop = asyncio.get_event_loop()

    def _save_session_sync():
        from playwright.sync_api import sync_playwright
        import os
        session_path = os.path.join(os.path.dirname(__file__), "ps3838_session.json")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=False, args=["--window-size=1280,900"])
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
                viewport={"width": 1280, "height": 900},
            )
            page = context.new_page()
            page.goto("https://www.ps3838.com/es/", wait_until="domcontentloaded")
            print("\n" + "="*60)
            print("[PS3838] Navega en el browser y haz LOGIN en ps3838.com")
            print("[PS3838] Cuando estés logueado, vuelve aquí y presiona ENTER")
            print("="*60)
            input()
            context.storage_state(path=session_path)
            browser.close()
            print(f"[PS3838] ✅ Sesión guardada en {session_path}")
            return session_path

    result_box = {}
    def _thread_fn():
        try:
            result_box["path"] = _save_session_sync()
        except Exception as e:
            result_box["error"] = str(e)

    t = threading.Thread(target=_thread_fn, daemon=False)
    t.start()
    await loop.run_in_executor(None, t.join, 300)  # max 5 min para hacer login

    if "error" in result_box:
        raise HTTPException(status_code=500, detail=result_box["error"])
    return {"status": "ok", "session_file": result_box.get("path")}

@app.post("/api/matches/xg")
async def override_match_xg(req: XgOverrideReq):
    url = req.url
    async with global_state["lock"]:
        if url in global_state["matches"]:
            ctx = global_state["matches"][url]
            if "stats_override" not in ctx:
                ctx["stats_override"] = {}
            ctx["stats_override"]["xg_local"] = req.home
            ctx["stats_override"]["xg_visitante"] = req.away
            ctx["force_refresh"] = True
    return {"status": "ok"}

class StatsOverrideReq(BaseModel):
    url: str
    stats: dict

@app.post("/api/matches/override_stats")
async def override_match_stats(req: StatsOverrideReq):
    url = req.url
    async with global_state["lock"]:
        if url in global_state["matches"]:
            ctx = global_state["matches"][url]
            if "stats_override" not in ctx:
                ctx["stats_override"] = {}
            for k, v in req.stats.items():
                ctx["stats_override"][k] = v
            ctx["force_refresh"] = True
    return {"status": "ok"}

@app.post("/api/matches/override")
async def override_match_odds(req: OverrideReq):
    url = req.url
    async with global_state["lock"]:
        if url in global_state["matches"]:
            ctx = global_state["matches"][url]
            if "overrides" not in ctx:
                ctx["overrides"] = {}
            ctx["overrides"][req.market] = [{
                "linea": req.linea,
                "over": req.over,
                "under": req.under
            }]
            # Forzar actualización inmediata si tenemos datos previos
            if ctx.get("previous_state") and ctx.get("previous_raw_markets") and "last_params" in ctx:
                from dataclasses import replace as dr
                fresh_params = dr(
                    ctx["last_params"],
                    kelly_fraction=global_state["settings"]["kelly_fraction"],
                    max_stake=1.0
                )
                update_match_math(
                    ctx,
                    ctx["last_snapshot"],
                    ctx["previous_state"],
                    ctx["previous_raw_markets"],
                    fresh_params,
                    ctx["last_prematch"]
                )
                return {"status": "ok", "data": ctx.get("data")}
            
            ctx["force_refresh"] = True
    return {"status": "pending"}

class SimulateReq(BaseModel):
    url: str
    market: str
    linea: float
    over: float
    under: float

@app.post("/api/matches/simulate")
async def simulate_match_odds(req: SimulateReq):
    url = req.url
    async with global_state["lock"]:
        if url in global_state["matches"]:
            ctx = global_state["matches"][url]
            if ctx.get("previous_state") and ctx.get("previous_raw_markets") and "last_params" in ctx:
                from dataclasses import replace as dr
                from futbol_live_betting_probabilities import MarketLine
                
                # Copia temporal de markets para NO ensuciar el estado real
                temp_markets = dr(ctx["previous_raw_markets"])
                custom_market = MarketLine(linea=req.linea, over=req.over, under=req.under)
                
                if req.market == "GOLES": temp_markets = dr(temp_markets, goles=custom_market)
                elif req.market == "CORNERS": temp_markets = dr(temp_markets, corners=custom_market)
                elif req.market == "TARJETAS": temp_markets = dr(temp_markets, tarjetas=custom_market)
                
                fresh_params = dr(
                    ctx["last_params"],
                    kelly_fraction=global_state["settings"]["kelly_fraction"],
                    max_stake=1.0
                )
                
                # Guardamos data actual para restaurar si fuera necesario (aunque update_match_math escribe en ctx['data'])
                backup_data = ctx.get("data")
                
                # Ejecutamos con ctx real pero markets temporales
                _, _, _ = update_match_math(
                    ctx,
                    ctx["last_snapshot"],
                    ctx["previous_state"],
                    temp_markets,
                    fresh_params,
                    ctx["last_prematch"]
                )
                
                sim_result = ctx.get("data")
                # Restauramos el data original para que el dashboard no cambie permanentemente
                ctx["data"] = backup_data
                
                return {"status": "ok", "data": sim_result}
    
    return {"status": "error", "message": "Datos insuficientes para simular"}

@app.get("/api/history")
async def get_history(limit: int = 50):
    import os, glob, json
    from datetime import datetime
    
    history_dir = global_state["settings"].get("history_dir", "live_history_v2")
    if not os.path.exists(history_dir):
        return {"bets": []}
    
    files = glob.glob(os.path.join(history_dir, "*.jsonl"))
    
    bets = []
    for fpath in files:
        fname = os.path.basename(fpath)
        file_result = "pending"
        if fname.startswith("[WIN]"): file_result = "win"
        elif fname.startswith("[LOSS]"): file_result = "loss"
        
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                lines = f.readlines()
                
            # Primera pasada: Buscar settlement real
            settlements_lookup = {}
            for line in reversed(lines):
                if not line.strip(): continue
                try:
                    record = json.loads(line)
                    if record.get("record_type") == "match_closure":
                        for snap in record.get("settled_snapshots", []):
                            ts_local = snap.get("captured_at_local")
                            for mkt, s in snap.get("settlements", {}).items():
                                settlements_lookup[(ts_local, mkt)] = s
                        break  # Solo hay un match_closure
                except:
                    pass

            # Segunda pasada: Parsear lineas
            parsed_records = []
            has_explicit_bets = False
            latest_stats = {"goles": 0, "corners": 0, "tarjetas": 0}
            for line in lines:
                if not line.strip(): continue
                try:
                    rec = json.loads(line)
                    parsed_records.append(rec)
                    rt = rec.get("record_type")
                    if rt == "bet":
                        has_explicit_bets = True
                    elif rt == "snapshot":
                        snap_state = rec.get("snapshot", {}).get("state", {})
                        if snap_state:
                            goles = (snap_state.get("goles_local") or 0) + (snap_state.get("goles_visitante") or 0)
                            corners = snap_state.get("corners") or 0
                            cards = (snap_state.get("amarillas") or 0) + (snap_state.get("rojas") or 0)
                            # Merge: only overwrite a field if the new value is higher
                            # This handles the dual-snapshot pattern (one with score, one with corners)
                            if goles > latest_stats["goles"]: latest_stats["goles"] = goles
                            if corners > latest_stats["corners"]: latest_stats["corners"] = corners
                            if cards > latest_stats["tarjetas"]: latest_stats["tarjetas"] = cards
                except Exception:
                    pass

            for record in parsed_records:
                rt = record.get("record_type")
                m = record.get("match", {})
                ts_local = record.get("captured_at_local")
                
                try:
                    ts = datetime.fromisoformat(record.get("captured_at_utc", "")).timestamp()
                except Exception:
                    ts = 0

                bets_to_process = []
                
                if rt == "bet" and "bets" in record:
                    for mkt, b in record["bets"].items():
                        stake = b.get("stake", 0) * global_state["settings"].get("bankroll", 1000)
                        odds = b.get("odds")
                        if odds is None or odds == 0:
                            _ev = b.get("ev", 0)
                            _prob = b.get("prob", 0)
                            odds = (_ev / _prob) if _prob > 0 else 0
                        bets_to_process.append((mkt, b.get("side"), b.get("linea"), odds, stake, b.get("ev")))
                        
                elif not has_explicit_bets and rt == "snapshot":
                    # Compatibility with legacy files that only log 'snapshot' records
                    model = record.get("model")
                    if isinstance(model, dict):
                        decisions = model.get("decisions", {})
                        label_map = {"goals": "goles", "corners": "corners", "cards": "tarjetas"}
                        for raw_mkt, d in decisions.items():
                            if not isinstance(d, dict): continue
                            best_stake = d.get("best_stake", 0)
                            if best_stake <= 0: continue
                            
                            stake = best_stake * global_state["settings"].get("bankroll", 1000)
                            side = d.get("best_side")
                            linea = d.get("linea")
                            _ev = d.get("best_ev", 0)
                            _prob = d.get("best_prob", 0)
                            odds = (_ev / _prob) if _prob > 0 else 0
                            mkt = label_map.get(raw_mkt, raw_mkt)
                            bets_to_process.append((mkt, side, linea, odds, stake, _ev))

                for mkt, side, linea, odds, stake, ev in bets_to_process:
                    # Aplicar settlement
                    s = settlements_lookup.get((ts_local, mkt))
                    profit = None
                    bet_result = "pending"
                    
                    if s:
                        profit = s.get("profit", 0) * global_state["settings"].get("bankroll", 1000)
                        bet_result = s.get("resultado", "pending")
                    else:
                        # Evaluación en vivo para partidos no finalizados o sin closure
                        actual_total = latest_stats.get(mkt, 0)
                        if side == "OVER" and actual_total > linea:
                            bet_result = "win"
                            profit = stake * (odds - 1.0)
                        elif side == "UNDER" and actual_total > linea:
                            bet_result = "loss"
                            profit = -stake
                        
                    bets.append({
                        "ts": ts,
                        "home_team": m.get("home_team", "Unknown"),
                        "away_team": m.get("away_team", "Unknown"),
                        "minute": record.get("minute", 0) if rt == "bet" else record.get("snapshot", {}).get("state", {}).get("minuto", 0),
                        "market": mkt,
                        "side": side,
                        "linea": linea,
                        "odds": odds,
                        "stake": stake,
                        "ev": ev,
                        "profit": profit,
                        "resultado": bet_result
                    })
        except Exception:
            pass

    bets.sort(key=lambda x: x.get("ts", 0), reverse=True)
    return {"bets": bets[:limit]}

class RefreshReq(BaseModel):
    url: str

@app.post("/api/matches/refresh")
async def refresh_match(req: RefreshReq):
    url = req.url
    async with global_state["lock"]:
        if url in global_state["matches"]:
            ctx = global_state["matches"][url]
            ctx["force_refresh"] = True
            # Intentamos devolver data actual mientras se refresca de fondo
            return {"status": "refreshing", "data": ctx.get("data")}
    return {"status": "error"}

@app.post("/api/matches")
async def add_match(req: AddMatchReq):
    url = req.url
    if url not in global_state["matches"]:
        active_urls.append(url)
        global_state["matches"][url] = {
            "first_pass": True,
            "previous_markets": None,
            "previous_state": None,
            "ended": False,
            "data": None,
            "overrides": {},
            "match_ctx": {
                "odds_history_buffer": {"goles": [], "corners": [], "tarjetas": []},
                "last_sharp_line": None,
                "corner_ctx": {},
            },
        }
    return {"status": "ok"}


# ─── WebSocket: Chrome Extension Odds Bridge ──────────────────────────────────
@app.websocket("/ws/extension")
async def extension_websocket(websocket: WebSocket):
    """
    Endpoint persistente para la Chrome Extension.
    La extensión abre UNA sola conexión desde background.js y la mantiene abierta.
    Cada mensaje es un frame JSON interceptado de PS3838, Betano o Betplay.
    """
    await websocket.accept()
    client_host = websocket.client.host if websocket.client else "?"
    conn_id = str(uuid.uuid4())
    websocket.state.conn_id = conn_id
    print(f"[EXT WS] Extension conectada desde {client_host} (id={conn_id[:8]}). Log → {_DIAG_LOG_PATH}")

    try:
        with open(_DIAG_LOG_PATH, "a", encoding="utf-8") as _f:
            _f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [CONNECT] {client_host} id={conn_id[:8]}\n")
    except Exception:
        pass
    scraper_log.connection("extension", connected=True, detail=f"client={client_host}")
    _ext_ws_connected.add(conn_id)

    _frame_count = 0
    _log_diag = None
    try:
        _log_diag = open(_DIAG_LOG_PATH, "a", encoding="utf-8")
    except Exception:
        pass
    try:
        while True:
            raw = await websocket.receive_text()
            _frame_count += 1
            import json as _json
            try:
                _f = _json.loads(raw)
                _ftype = _f.get("type", "?")
                _fws = (_f.get("ws_url") or _f.get("tab_url") or "")[:130]
                if _frame_count <= 5:
                    msg = f"[EXT WS #{_frame_count}] type={_ftype} ws={_fws}"
                    print(msg)
                    if _log_diag:
                        _log_diag.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
                        _log_diag.flush()
                elif _frame_count % 200 == 0:
                    print(f"[EXT WS #{_frame_count}] type={_ftype} ...")
            except Exception:
                pass
            await _process_extension_frame_async(raw)
    except WebSocketDisconnect:
        msg = f"[EXT WS] Extension desconectada tras {_frame_count} frames"
        print(msg)
        if _log_diag:
            _log_diag.write(f"{time.strftime('%H:%M:%S')} {msg}\n")
            _log_diag.flush()
        scraper_log.connection("extension", connected=False)
    except Exception as e:
        import traceback
        print(f"[EXT WS] Error: {e}")
        traceback.print_exc()
    finally:
        _ext_ws_connected.discard(conn_id)


# Conjunto de conexiones activas de extensión (para monitoreo)
_ext_ws_connected: set = set()


async def _process_extension_frame_async(raw_text: str) -> None:
    """Parsea un frame y lo escribe en el estado global. Async-safe con asyncio.Lock."""
    if not parse_extension_frame:
        return

    try:
        import json as _json
        frame = _json.loads(raw_text)
    except Exception:
        return

    if not isinstance(frame, dict):
        return

    if frame.get("type") == "ping":
        return

    # ── ZERO-TOUCH AUTO-REGISTRATION ──
    tab_url_from_frame = frame.get("tab_url", "")
    if "betano" in tab_url_from_frame and "#id:" in tab_url_from_frame:
        async with global_state["lock"]:
            if tab_url_from_frame not in global_state["matches"]:
                try:
                    extracted_id = int(tab_url_from_frame.split("#id:")[1].split("&")[0].strip())
                    active_urls.append(tab_url_from_frame)
                    global_state["matches"][tab_url_from_frame] = {
                        "first_pass":       True,
                        "previous_markets": None,
                        "previous_state":   None,
                        "ended":            False,
                        "data":             None,
                        "overrides":        {},
                        "urls":             {"betano": tab_url_from_frame},
                        "event_id":         extracted_id,
                        "match_ctx": {
                            "odds_history_buffer": {"goles": [], "corners": [], "tarjetas": []},
                            "last_sharp_line": None,
                            "corner_ctx": {},
                        },
                    }
                    print(f"🌟 [ZERO-TOUCH REGISTRATION] Partido registrado desde extensión: {tab_url_from_frame[-55:]} (SofaID: {extracted_id})")
                except Exception as e:
                    print(f"Error parseando event_id de {tab_url_from_frame}: {e}")

    # Parsear frame fuera del lock (CPU-bound, no toca global_state)
    ws_url = frame.get("ws_url", "")
    if "/live/overview/" in ws_url or "isInit=false" in ws_url:
        print(f"🔥 [ADUANA ABIERTA] Procesando JSON masivo de Betano ({len(raw_text)} bytes)...")

    try:
        results = parse_extension_frame(frame)
    except Exception:
        if "/live/overview/" in ws_url or "isInit=false" in ws_url:
            print(f"⚠️ [ADUANA] Error en parse_extension_frame para overview de Betano ({len(raw_text)} bytes)")
        return
    if not results:
        if "/live/overview/" in ws_url or "isInit=false" in ws_url:
            print(f"⚠️ [ADUANA] parse_extension_frame retornó vacío para overview de Betano ({len(raw_text)} bytes)")
        return

    if not isinstance(results, list):
        results = [results]

    # ── Auto-emparejador de IDs de Betano (Zero-Touch para Overviews) ──
    # Si recibimos un overview con muchos partidos, intentamos asociar sus IDs a SofaScore
    is_betano_overview = any(r.get("source_id") == "betano" and str(r.get("match_url", "")).isdigit() for r in results[:10])
    if is_betano_overview:
        async with global_state["lock"]:
            matched_betano = 0
            for res in results:
                if res.get("source_id") == "betano":
                    eid = res.get("match_url")
                    bh = _normalize_team_name(res.get("home_team", ""))
                    ba = _normalize_team_name(res.get("away_team", ""))
                    if not eid or not bh: continue
                    
                    for url, ctx in global_state["matches"].items():
                        if not ctx.get("betano_event_id"):
                            sh = _normalize_team_name(ctx.get("home_team", ""))
                            sa = _normalize_team_name(ctx.get("away_team", ""))
                            if not sh: continue
                            
                            # Lógica de matching de tokens
                            e_h_tokens = set(bh.split()); e_a_tokens = set(ba.split())
                            c_h_tokens = set(sh.split()); c_a_tokens = set(sa.split())
                            h_match = len(e_h_tokens & c_h_tokens) >= 1 and (bh in sh or sh in bh or len(e_h_tokens & c_h_tokens) >= 2)
                            a_match = len(e_a_tokens & c_a_tokens) >= 1 and (ba in sa or sa in ba or len(e_a_tokens & c_a_tokens) >= 2)
                            
                            if h_match and a_match:
                                ctx["betano_event_id"] = int(eid)
                                if "urls" not in ctx: ctx["urls"] = {}
                                ctx["urls"]["betano"] = f"betano-event:{eid}"
                                matched_betano += 1
            if matched_betano > 0:
                print(f"🔗 [BETANO LINK] {matched_betano} IDs asociados automáticamente desde overview")

    for result in results:
        source_id   = result.get("source_id")
        frame_type  = result.get("frame_type", "")

        if frame_type == "hard_reset":
            target_url = result.get("url", "")
            async with global_state["lock"]:
                for url, ctx in global_state["matches"].items():
                    if target_url and (target_url in url or url in target_url or ctx.get("event_id") in target_url):
                        ctx["overrides"] = {}
                        ctx["pinnacle_fair"] = {}
                        print(f"🔥 [HARD RESET] Caché limpiado para {url[-45:]} tras 404 reload de la extensión.")
            return

        if frame_type == "PINNACLE_RAW_DATA":
            payload = result.get("payload", {})
            if isinstance(payload, dict):
                keys = list(payload.keys())
                has_data_keys = any(k in keys for k in ("l", "ce", "u", "sports", "events", "leagues", "markets"))
                if has_data_keys:
                    print(f"🔍 [PINNACLE_RAW] Keys: {keys[:8]}, l={len(payload.get('l',[]))} ce={len(payload.get('ce',[]))} events={len(payload.get('events',[]))} markets={len(payload.get('markets',[]))}")
                if "sports" in payload:
                    continue  # metadata de deportes, ignorar
            if not isinstance(payload, dict):
                continue

            # ── Parseo del formato compacto de PS3838 ──────────────────────────
            # PS3838 responde con un formato propietario de arrays anidados:
            #   payload["l"] = [sport, ..., [league, ..., [match0, match1, ...]]]
            #   payload["ce"] = [match0, match1, ...] (eventos compactos para corners)
            #   payload["u"] = [...] (heartbeat/updates)
            # Cada match es un array: [eventId, home, away, ..., {periods}]
            # periods["0"] = lista de mercados, índice 1 = Goles, 3 = Corners
            # Cada línea es: [?, points, over_price, under_price]
            extracted_by_event: dict[str, list] = {}  # eventId → [(mkt_name, lines)]

            def _proc_compact_match(match_arr, mkt_name, totals_index):
                if len(match_arr) < 9 or not isinstance(match_arr[8], dict):
                    return
                event_id = str(match_arr[0])
                periods = match_arr[8]
                if "0" not in periods:
                    return
                p_data = periods["0"]
                if not isinstance(p_data, list) or len(p_data) <= totals_index:
                    return
                totals = p_data[totals_index]
                if not isinstance(totals, list):
                    return
                lines = []
                for t in totals:
                    if not isinstance(t, list) or len(t) < 4:
                        continue
                    try:
                        linea = float(t[1])
                        over  = float(t[2])
                        under = float(t[3])
                        if over > 1.0 and under > 1.0:
                            lines.append({
                                "linea": linea, "over": over, "under": under,
                                "source_id": "pinnacle", "timestamp": time.time(),
                            })
                    except (ValueError, TypeError):
                        continue
                if lines:
                    extracted_by_event.setdefault(event_id, []).append((mkt_name, lines))

            # GOLES: vienen en payload["l"] (leagues)
            if "l" in payload and isinstance(payload["l"], list):
                for sport in payload["l"]:
                    if isinstance(sport, list) and len(sport) > 2 and isinstance(sport[2], list):
                        for league in sport[2]:
                            if isinstance(league, list) and len(league) > 2 and isinstance(league[2], list):
                                for match in league[2]:
                                    if isinstance(match, list):
                                        _proc_compact_match(match, "GOLES", 1)

            # CORNERS: vienen en payload["ce"] (compact events)
            if "ce" in payload and isinstance(payload["ce"], list):
                for item in payload["ce"]:
                    if isinstance(item, list) and len(item) >= 9:
                        _proc_compact_match(item, "CORNERS", 3)

            # ── Inyectar cuotas en los matches activos ──────────────────────────
            if len(extracted_by_event) == 0 and "u" not in payload and ("l" in payload or "ce" in payload):
                print(f"⚠️ [PINNACLE_RAW] Formato compacto detectado pero no se extrajeron líneas. "
                      f"Verifica índices de array. l_items={len(payload.get('l',[]))}, ce_items={len(payload.get('ce',[]))}")

            if "u" in payload or len(extracted_by_event) > 0:
                # ── Normalizador universal de equipos ─────────────────────────
                def _normalize_pinnacle_team(s: str) -> str:
                    if not s:
                        return ""
                    # 1. Reparar mojibake (Latin-1 → UTF-8)
                    try:
                        s = s.encode('latin1').decode('utf-8')
                    except (UnicodeEncodeError, UnicodeDecodeError):
                        pass
                    # 2. Quitar acentos
                    s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
                    s = s.lower()
                    # 3. Eliminar prefijos/sufijos de club y partículas
                    import re as _re
                    s = _re.sub(r'\b(fc|afc|cf|ca|cd|nk|sk|us|fk|sc|fs|ac|as|rc|rcd|sd|ud|ad|sl|bk)\b', '', s)
                    s = s.replace(" (corners)", "").replace("(corners)", "")
                    for d in (" de ", " club", "deportivo ", "atletico ", "cd ", " sc"):
                        s = s.replace(d, " ")
                    s = _re.sub(r'[^a-z0-9\s]', '', s)
                    s = _re.sub(r'\s+', ' ', s).strip()
                    return s

                # ── Construir índice de equipos por eventId ──────────────────
                match_teams: dict[str, tuple[str, str]] = {}
                # Extraer de 'l' (goals)
                if "l" in payload and isinstance(payload["l"], list):
                    for sport in payload["l"]:
                        if isinstance(sport, list) and len(sport) > 2 and isinstance(sport[2], list):
                            for league in sport[2]:
                                if isinstance(league, list) and len(league) > 2 and isinstance(league[2], list):
                                    for m in league[2]:
                                        if isinstance(m, list) and len(m) >= 3:
                                            eid = str(m[0])
                                            home = str(m[1]) if len(m) > 1 else ""
                                            away = str(m[2]) if len(m) > 2 else ""
                                            match_teams[eid] = (_normalize_pinnacle_team(home), _normalize_pinnacle_team(away))
                # Extraer de 'ce' (corners)
                if "ce" in payload and isinstance(payload["ce"], list):
                    for m in payload["ce"]:
                        if isinstance(m, list) and len(m) >= 3:
                            eid = str(m[0])
                            if eid not in match_teams:
                                home = str(m[1]) if len(m) > 1 else ""
                                away = str(m[2]) if len(m) > 2 else ""
                                match_teams[eid] = (_normalize_pinnacle_team(home), _normalize_pinnacle_team(away))

                # ── Motor de emparejamiento estricto ──────────────────────────
                def _fuzzy_match_team(e_home, e_away, ctx_home, ctx_away):
                    if not e_home or not ctx_home:
                        return False
                    # Tokenizar: dividir en palabras
                    e_h_tokens = set(e_home.split())
                    e_a_tokens = set(e_away.split())
                    c_h_tokens = set(ctx_home.split())
                    c_a_tokens = set(ctx_away.split())

                    # Match normal: home↔home, away↔away
                    h_match = len(e_h_tokens & c_h_tokens) >= 1 and (e_home in ctx_home or ctx_home in e_home or len(e_h_tokens & c_h_tokens) >= 2)
                    a_match = len(e_a_tokens & c_a_tokens) >= 1 and (e_away in ctx_away or ctx_away in e_away or len(e_a_tokens & c_a_tokens) >= 2)
                    if h_match and a_match:
                        return True

                    # Match invertido: home↔away, away↔home
                    h_match_r = len(e_h_tokens & c_a_tokens) >= 1 and (e_home in ctx_away or ctx_away in e_home or len(e_h_tokens & c_a_tokens) >= 2)
                    a_match_r = len(e_a_tokens & c_h_tokens) >= 1 and (e_away in ctx_home or ctx_home in e_away or len(e_a_tokens & c_h_tokens) >= 2)
                    return h_match_r and a_match_r

                ts_now = time.time()
                matched_count = 0
                async with global_state["lock"]:
                    for url, ctx in global_state["matches"].items():
                        matched = False

                        # 1. Coincidencia exacta por pinnacle_event_id
                        pinn_id = ctx.get("pinnacle_event_id")
                        if pinn_id:
                            sid = str(pinn_id)
                            if sid in extracted_by_event:
                                matched = True
                            else:
                                for eid in list(extracted_by_event.keys()):
                                    if sid in eid or eid in sid:
                                        extracted_by_event.setdefault(sid, []).extend(extracted_by_event[eid])
                                        match_teams[sid] = match_teams.get(eid, ("", ""))
                                        print(f"🔗 [FARO LINK] Pinnacle ID {sid} ≈ {eid}")
                                        matched = True
                                        break

                        # 2. Auto-emparejador por nombres de equipo (Zero-Touch)
                        if not matched and match_teams:
                            ctx_home = ctx.get("home_team", "")
                            ctx_away = ctx.get("away_team", "")
                            if ctx_home and ctx_away:
                                norm_ctx_home = _normalize_pinnacle_team(ctx_home)
                                norm_ctx_away = _normalize_pinnacle_team(ctx_away)
                                for eid, (e_home, e_away) in match_teams.items():
                                    if _fuzzy_match_team(e_home, e_away, norm_ctx_home, norm_ctx_away):
                                        if eid in extracted_by_event:
                                            ctx["pinnacle_event_id"] = eid
                                            print(f"🔗 [FARO LINK] ¡Éxito! {ctx_home} vs {ctx_away} → Pinnacle ID {eid}")
                                        matched = True
                                        matched_count += 1
                                        break

                        if not matched:
                            continue

                        sid = pinn_id if pinn_id else ctx.get("pinnacle_event_id")
                        if not sid:
                            sid = next((eid for eid in match_teams if _fuzzy_match_team(
                                match_teams[eid][0], match_teams[eid][1],
                                _normalize_pinnacle_team(ctx.get("home_team", "")),
                                _normalize_pinnacle_team(ctx.get("away_team", ""))
                            )), None)
                        sid = str(sid) if sid else None

                        if sid and sid in extracted_by_event:
                            if "pinnacle_fair" not in ctx:
                                ctx["pinnacle_fair"] = {}
                            for mkt_name, lines in extracted_by_event[sid]:
                                ctx["pinnacle_fair"][mkt_name] = lines
                                ctx["pinnacle_fair"][f"_last_updated_{mkt_name}"] = ts_now
                            ctx["scraper_ts"] = ts_now
                            if matched_count <= 3:  # solo loguear primeros 3 para no hacer spam
                                print(f"💰 [PINNACLE_RAW] {sum(len(l) for _, l in extracted_by_event[sid])} cuotas → "
                                      f"{ctx.get('home_team','?')} vs {ctx.get('away_team','?')}")
                        elif "u" in payload:
                            if "pinnacle_fair" not in ctx:
                                ctx["pinnacle_fair"] = {}
                            ctx["pinnacle_fair"]["_last_updated_GOLES"] = ts_now
                            ctx["pinnacle_fair"]["_last_updated_CORNERS"] = ts_now
                            ctx["scraper_ts"] = ts_now

                if matched_count > 0:
                    print(f"🔗 [FARO LINK] {matched_count} partidos emparejados en este ciclo")
                elif match_teams and not any(ctx.get("pinnacle_event_id") for ctx in global_state["matches"].values()):
                    # Diagnóstico: mostrar muestra de equipos sin match
                    sample_pinnacle = list(match_teams.items())[:3]
                    sample_ctx = [(url, ctx.get("home_team",""), ctx.get("away_team","")) 
                                  for url, ctx in list(global_state["matches"].items())[:3]]
                    print(f"⚠️ [FARO LINK] 0 emparejamientos. Muestra Pinnacle: {[(eid, t[0][:20], t[1][:20]) for eid, t in sample_pinnacle]}")
                    print(f"   Muestra SofaScore: {[(url[-40:], h[:20], a[:20]) for url, h, a in sample_ctx]}")
            continue

        if frame_type == "stats_scrape":
            stats      = result.get("stats", {})
            match_url  = result.get("match_url")
            ext_home   = str(result.get("home_team", "")).lower()
            ext_away   = str(result.get("away_team", "")).lower()

            def _clean(s):
                # Reparar mojibake (Latin-1 → UTF-8)
                try:
                    s = s.encode('latin1').decode('utf-8')
                except (UnicodeEncodeError, UnicodeDecodeError):
                    pass
                # Quitar acentos
                s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
                s = s.lower()
                # Eliminar sufijos/prefijos de club
                for d in (" fc", " cd", " de ", " club", "deportivo ", "atletico ",
                          " afc", " cf", " sc", " nk", " sk", " fk", " ac", " as",
                          " rc", " sd", " ud", " ad", " sl", " bk", "fs "):
                    s = s.replace(d, "")
                return s.strip()

            async with global_state["lock"]:
                # ── AUTO-REGISTRO: si la URL de SofaScore no está registrada, crearla ──
                # El usuario solo necesita abrir la pestaña de SofaScore — sin pegar links.
                if match_url and match_url not in global_state["matches"]:
                    active_urls.append(match_url)
                    global_state["matches"][match_url] = {
                        "first_pass":       True,
                        "previous_markets": None,
                        "previous_state":   None,
                        "ended":            False,
                        "data":             None,
                        "overrides":        {},
                        "match_ctx": {
                            "odds_history_buffer": {"goles": [], "corners": [], "tarjetas": []},
                            "last_sharp_line": None,
                            "corner_ctx": {},
                        },
                    }
                    home = stats.get("home_team", "?")
                    away = stats.get("away_team", "?")
                    print(f"[EXT AUTO] 🆕 Partido registrado automáticamente: {home} vs {away} → {match_url[-55:]}")
                    scraper_log.event("sofascore", "AUTO_REGISTER", {"home": home, "away": away}, match_url, level="OK")

                # Encontrar el partido activo que corresponde
                target_url = None
                event_id_from_stats = stats.get("event_id")

                # 1. Coincidencia exacta de URL
                if match_url and match_url in global_state["matches"]:
                    target_url = match_url
                # 2. Coincidencia por event_id (URL registrada contiene #id:XXXXXXX)
                if not target_url and event_id_from_stats:
                    eid_str = str(event_id_from_stats)
                    for u in global_state["matches"]:
                        if eid_str in u:
                            target_url = u
                            break
                # 3. Fuzzy match por nombre de equipo
                if not target_url:
                    for u, _ctx in global_state["matches"].items():
                        ch = _clean(str(_ctx.get("home_team", "")).lower())
                        ca = _clean(str(_ctx.get("away_team", "")).lower())
                        eh = _clean(ext_home); ea = _clean(ext_away)
                        
                        match_normal = (eh and ch and (eh in ch or ch in eh)) and (ea and ca and (ea in ca or ca in ea))
                        match_reversed = (eh and ca and (eh in ca or ca in eh)) and (ea and ch and (ea in ch or ch in ea))
                        
                        if match_normal or match_reversed:
                            target_url = u
                            break

                if target_url:
                    ctx = global_state["matches"].get(target_url)
                    if ctx and not ctx.get("ended"):
                        # Guardar stats en ctx como snapshot sintético
                        prev_snap = ctx.get("last_snapshot")

                        # --- GUARD CLAUSE INTELIGENTE ---
                        # Si el nuevo frame no tiene minuto válido (o viene como 0/?/nulo)
                        # y nosotros YA tenemos un minuto válido en memoria, heredamos el minuto
                        # a este frame basura para que los logs no asusten y el motor matemático no colapse.
                        # NO abortamos (continue) porque perderíamos las actualizaciones de GOLES de incidents.
                        incoming_minute = stats.get("minute")
                        if incoming_minute in ["?", None, "", 0, 0.0]:
                            if prev_snap and getattr(prev_snap, "minute", 0) not in ["?", None, "", 0, 0.0]:
                                stats["minute"] = prev_snap.minute
                                # print(f"🛡️ [GUARD CLAUSE] Frame fantasma curado. Heredando minuto {prev_snap.minute}")

                        # Construir namespace que imita LiveSnapshot con los campos disponibles
                        from types import SimpleNamespace
                        syn = SimpleNamespace(
                            match_url       = target_url,
                            event_id        = stats.get("event_id", getattr(prev_snap, "event_id", 0) if prev_snap else 0),
                            home_team       = stats.get("home_team") or (getattr(prev_snap, "home_team", "") if prev_snap else ""),
                            away_team       = stats.get("away_team") or (getattr(prev_snap, "away_team", "") if prev_snap else ""),
                            tournament      = getattr(prev_snap, "tournament", "") if prev_snap else "",
                            tournament_slug = getattr(prev_snap, "tournament_slug", "") if prev_snap else "",
                            category_name   = getattr(prev_snap, "category_name", "") if prev_snap else "",
                            country_name    = getattr(prev_snap, "country_name", "") if prev_snap else "",
                            status_text     = stats.get("status_text") or (getattr(prev_snap, "status_text", "inprogress") if prev_snap else "inprogress"),
                            status_code     = stats.get("status_code", getattr(prev_snap, "status_code", 0) if prev_snap else 0),
                            minute          = float(stats.get("minute") or (getattr(prev_snap, "minute", 0) if prev_snap else 0)),
                            goals_home      = float(stats.get("goals_home", getattr(prev_snap, "goals_home", 0) if prev_snap else 0)),
                            goals_away      = float(stats.get("goals_away", getattr(prev_snap, "goals_away", 0) if prev_snap else 0)),
                            yellows_home    = float(stats.get("yellows_home", getattr(prev_snap, "yellows_home", 0) if prev_snap else 0)),
                            yellows_away    = float(stats.get("yellows_away", getattr(prev_snap, "yellows_away", 0) if prev_snap else 0)),
                            yellows_total   = float(stats.get("yellows_total", getattr(prev_snap, "yellows_total", 0) if prev_snap else 0)),
                            reds_home       = float(stats.get("reds_home", getattr(prev_snap, "reds_home", 0) if prev_snap else 0)),
                            reds_away       = float(stats.get("reds_away", getattr(prev_snap, "reds_away", 0) if prev_snap else 0)),
                            reds_total      = float(stats.get("reds_total", getattr(prev_snap, "reds_total", 0) if prev_snap else 0)),
                            fouls_home      = float(stats.get("fouls_home", getattr(prev_snap, "fouls_home", 0) if prev_snap else 0)),
                            fouls_away      = float(stats.get("fouls_away", getattr(prev_snap, "fouls_away", 0) if prev_snap else 0)),
                            fouls_total     = float(stats.get("fouls_total", getattr(prev_snap, "fouls_total", 0) if prev_snap else 0)),
                            corners_home    = float(stats.get("corners_home", getattr(prev_snap, "corners_home", 0) if prev_snap else 0)),
                            corners_away    = float(stats.get("corners_away", getattr(prev_snap, "corners_away", 0) if prev_snap else 0)),
                            corners_total   = float(stats.get("corners_total", getattr(prev_snap, "corners_total", 0) if prev_snap else 0)),
                            crosses_home    = float(getattr(prev_snap, "crosses_home", 0) if prev_snap else 0),
                            crosses_away    = float(getattr(prev_snap, "crosses_away", 0) if prev_snap else 0),
                            referee         = getattr(prev_snap, "referee", None) if prev_snap else None,
                            xg_home         = float(stats.get("xg_home", getattr(prev_snap, "xg_home", 0) if prev_snap else 0)),
                            xg_away         = float(stats.get("xg_away", getattr(prev_snap, "xg_away", 0) if prev_snap else 0)),
                            shots_home      = float(stats.get("shots_home", getattr(prev_snap, "shots_home", 0) if prev_snap else 0)),
                            shots_away      = float(stats.get("shots_away", getattr(prev_snap, "shots_away", 0) if prev_snap else 0)),
                            shots_on_target_home = float(stats.get("shots_on_target_home", getattr(prev_snap, "shots_on_target_home", 0) if prev_snap else 0)),
                            shots_on_target_away = float(stats.get("shots_on_target_away", getattr(prev_snap, "shots_on_target_away", 0) if prev_snap else 0)),
                            possession_home = float(stats.get("possession_home", getattr(prev_snap, "possession_home", 50) if prev_snap else 50)),
                            touches_in_box_home  = float(stats.get("touches_in_box_home", getattr(prev_snap, "touches_in_box_home", 0) if prev_snap else 0)),
                            touches_in_box_away  = float(stats.get("touches_in_box_away", getattr(prev_snap, "touches_in_box_away", 0) if prev_snap else 0)),
                            dangerous_attacks_home = float(stats.get("dangerous_attacks_home", getattr(prev_snap, "dangerous_attacks_home", 0) if prev_snap else 0)),
                            dangerous_attacks_away = float(stats.get("dangerous_attacks_away", getattr(prev_snap, "dangerous_attacks_away", 0) if prev_snap else 0)),
                            big_chances_missed_home = float(getattr(prev_snap, "big_chances_missed_home", 0) if prev_snap else 0),
                            big_chances_missed_away = float(getattr(prev_snap, "big_chances_missed_away", 0) if prev_snap else 0),
                            attack_zones_home = getattr(prev_snap, "attack_zones_home", ()) if prev_snap else (),
                            attack_zones_away = getattr(prev_snap, "attack_zones_away", ()) if prev_snap else (),
                            goals_market    = getattr(prev_snap, "goals_market", None) if prev_snap else None,
                            corners_market  = getattr(prev_snap, "corners_market", None) if prev_snap else None,
                            cards_market    = getattr(prev_snap, "cards_market", None) if prev_snap else None,
                            urgency_multiplier = float(getattr(prev_snap, "urgency_multiplier", 1.0) if prev_snap else 1.0),
                            defensive_yellows  = float(getattr(prev_snap, "defensive_yellows", 0) if prev_snap else 0),
                            centros_local      = float(getattr(prev_snap, "centros_local", 0) if prev_snap else 0),
                            centros_visitante  = float(getattr(prev_snap, "centros_visitante", 0) if prev_snap else 0),
                            notes              = getattr(prev_snap, "notes", ()) if prev_snap else (),
                            home_win_odds      = getattr(prev_snap, "home_win_odds", None) if prev_snap else None,
                            away_win_odds      = getattr(prev_snap, "away_win_odds", None) if prev_snap else None,
                        )

                        ctx["last_snapshot"]  = syn
                        ctx["ext_sourced"]    = True
                        ctx["ext_sourced_ts"] = time.time()
                        ctx["scraper_ts"]     = time.time()
                        ctx["force_refresh"]  = True  # Disparar recálculo inmediato
                        # Guardar event_id para matching futuro y para que background_loop
                        # sepa que la extensión ya tiene este partido identificado
                        if event_id_from_stats:
                            ctx["event_id"] = event_id_from_stats

                        # ── MATCH CLOSURE: Si el partido terminó, guardar historial y marcar como ended ──
                        is_finished = (syn.status_text in ("finished", "ended", "after et", "after penalties") or syn.status_code == 100)
                        if is_finished and not ctx.get("ended", False):
                            if not backend_args.no_history:
                                try:
                                    # build_state_from_snapshot para asegurar que el state final sea coherente
                                    from futbol_live_betting_probabilities import build_state_from_snapshot as _bsfs
                                    final_state = _bsfs(syn, ctx.get("previous_state"))
                                    append_match_closure(Path(backend_args.history_dir), syn, final_state)
                                    print(f"✅ [MATCH CLOSURE] Partido finalizado y guardado: {syn.home_team} vs {syn.away_team}")
                                except Exception as e:
                                    print(f"Error en match closure (extension): {e}")
                            ctx["ended"] = True
                            if target_url in active_urls:
                                active_urls.remove(target_url)

                        # Guardar nombres de equipo para futuro matching
                        if syn.home_team: ctx["home_team"] = syn.home_team
                        if syn.away_team: ctx["away_team"] = syn.away_team

                        print(f"[EXT STATS] {source_id} min={syn.minute:.0f} shots={syn.shots_home:.0f}/{syn.shots_away:.0f} poss={syn.possession_home:.0f}% → {target_url[-45:]}")
                        scraper_log.stats(source_id or "sofascore", stats, target_url)

            continue  # No seguir al routing de cuotas

        # ─────────────────────────────────────────────────────────────────────
        market_name = result.get("market_name")
        if market_name:
            market_name = market_name.upper()
        lines       = result.get("lines", [])
        match_url   = result.get("match_url")
        suspended   = result.get("suspended", False)

        if not source_id or not market_name:
            continue

        if not lines and not suspended:
            continue

        # ── Auto-registro de URL de softbook (Betano/Betplay) ─────────────────
        # La extensión envía tab_url en cada frame. Lo guardamos en el primer
        # partido activo que no tenga esa fuente configurada, para que el routing
        # por source_id funcione correctamente en el bloque de targets abajo.
        tab_url_from_frame = frame.get("tab_url", "")
        if source_id in ("betano", "betplay") and tab_url_from_frame:
            async with global_state["lock"]:
                for _aurl in list(global_state["matches"].keys()):
                    _ctx = global_state["matches"].get(_aurl)
                    if _ctx is None:
                        continue
                    if "urls" not in _ctx:
                        _ctx["urls"] = {}
                    if not _ctx["urls"].get(source_id):
                        _ctx["urls"][source_id] = tab_url_from_frame
                        print(f"[EXT WS] Registrando fallback URL: {tab_url_from_frame[-55:]}")
                        break

        # Resolver targets bajo lock, luego procesar cuotas fuera del lock
        urls_to_update = {}  # url → ctx (referencia, sin lock)
        recalculo_pendiente = []  # (ctx, snapshot, previous_state, raw_markets, params, prematch)

        async with global_state["lock"]:
            targets = []

            if match_url:
                if match_url in global_state["matches"]:
                    targets = [match_url]
                elif source_id in ("betplay", "betano") and match_url.isdigit():
                    for url in global_state["matches"].keys():
                        if match_url in url and (source_id in url or "kambi" in url or "betplay" in url):
                            targets = [url]
                            break
                    # Buscar por betano_event_id almacenado en el contexto (registrado por el radar)
                    if not targets:
                        for url, ctx in global_state["matches"].items():
                            if str(ctx.get("betano_event_id", "")) == match_url:
                                targets = [url]
                                break

            if not targets and tab_url_from_frame:
                for url, state_data in global_state["matches"].items():
                    if state_data.get("urls", {}).get(source_id) == tab_url_from_frame:
                        targets.append(url)

            if not targets:
                ext_home = str(result.get("home_team", "")).lower()
                ext_away = str(result.get("away_team", "")).lower()
                
                if ext_home and ext_away and ext_home != "unknown":
                    def clean(s):
                        try:
                            s = s.encode('latin1').decode('utf-8')
                        except (UnicodeEncodeError, UnicodeDecodeError):
                            pass
                        s = ''.join(c for c in unicodedata.normalize('NFD', s) if unicodedata.category(c) != 'Mn')
                        s = s.lower()
                        for drop in (" fc", " cd", " de ", " club", "deportivo ", "atletico ",
                                      " afc", " cf", " sc", " nk", " sk", " fk", " ac", " as",
                                      " rc", " sd", " ud", " ad", " sl", " bk", "fs "):
                            s = s.replace(drop, "")
                        return s.strip()
                        
                    eh, ea = clean(ext_home), clean(ext_away)
                    for url, _ctx in global_state["matches"].items():
                        ch = clean(str(_ctx.get("home_team", "")).lower())
                        ca = clean(str(_ctx.get("away_team", "")).lower())
                        
                        match_normal = (eh and ch and (eh in ch or ch in eh)) and (ea and ca and (ea in ca or ca in ea))
                        match_reversed = (eh and ca and (eh in ca or ca in eh)) and (ea and ch and (ea in ch or ch in ea))
                        
                        if match_normal or match_reversed:
                            targets.append(url)

            if not targets:
                # ── LOG DE DESCUBRIMIENTO ──
                # Si la extensión ve un partido de Betano que NO tenemos en la matriz de SofaScore,
                # lo logueamos para diagnóstico. Ayuda a ver si el Radar está ciego.
                if source_id == "betano" and (result.get("home_team") or result.get("away_team")):
                    ext_h = result.get("home_team", "?")
                    ext_a = result.get("away_team", "?")
                    # Evitar spam: solo loguear si no es "Unknown"
                    if ext_h != "Unknown" and ext_a != "?":
                        print(f"📡 [EXT-DISCOVERY] Betano reportó partido fuera de radar: {ext_h} vs {ext_a} (ID: {match_url})")
                continue

            for url in targets:
                ctx = global_state["matches"].get(url)
                if not ctx:
                    continue

                if source_id in ("pinnacle", "EXT_WS_PS3838"):
                    if "pinnacle_fair" not in ctx:
                        ctx["pinnacle_fair"] = {}
                    
                    if market_name == "HEARTBEAT":
                        ctx["pinnacle_fair"]["_last_updated_GOLES"] = time.time()
                        ctx["pinnacle_fair"]["_last_updated_CORNERS"] = time.time()
                        ctx["scraper_ts"] = time.time()
                        continue
                    
                    if suspended:
                        ctx["pinnacle_fair"][market_name] = []
                        print(f"[EXT WS 🚨] Pinnacle suspendió {market_name} → {url[-40:]}")
                        scraper_log.odds("pinnacle", market_name, [], url, suspended=True)
                    else:
                        fair_lines = [
                            {
                                "linea": float(l["linea"]), 
                                "over": float(l["over"]), 
                                "under": float(l["under"]),
                                "source_id": source_id,
                                "timestamp": time.time(),
                            }
                            for l in lines
                        ]
                        ctx["pinnacle_fair"][market_name] = fair_lines
                        print(f"[EXT WS 🔟] Pinnacle {market_name}: {len(fair_lines)} líneas → {url[-40:]}")
                        scraper_log.odds("pinnacle", market_name, fair_lines, url)
                    
                    ctx["pinnacle_fair"][f"_last_updated_{market_name}"] = time.time()
                    ctx["scraper_ts"] = time.time()

                else:
                    if "overrides" not in ctx:
                        ctx["overrides"] = {}
                    
                    if suspended:
                        ctx["overrides"][market_name] = []
                        print(f"[EXT WS 🚨] {source_id.capitalize()} suspendió {market_name} → {url[-40:]}")
                        scraper_log.odds(source_id, market_name, [], url, suspended=True)
                    else:
                        soft_lines = [
                            {
                                "linea": float(l["linea"]), 
                                "over": float(l["over"]), 
                                "under": float(l["under"]),
                                "source_id": source_id,
                                "is_verified": l.get("is_verified", True),
                                "timestamp": time.time(),
                            }
                            for l in lines
                        ]
                        existing = ctx["overrides"].get(market_name, [])
                        existing_map = {e["linea"]: e for e in existing}
                        now_merge = time.time()
                        for sl in soft_lines:
                            existing_map[sl["linea"]] = sl
                        ctx["overrides"][market_name] = [
                            v for v in existing_map.values()
                            if (now_merge - v.get("timestamp", 0)) < 120
                        ]
                        print(f"[EXT WS] {source_id.capitalize()} {market_name}: {len(lines)} líneas → {url[-40:]}")
                        scraper_log.odds(source_id, market_name, soft_lines, url)

                # Recolectar para recálculo fuera del lock
                if "last_snapshot" in ctx and "previous_state" in ctx and "previous_raw_markets" in ctx and "last_params" in ctx:
                    recalculo_pendiente.append((
                        ctx,
                        ctx["last_snapshot"],
                        ctx["previous_state"],
                        ctx["previous_raw_markets"],
                        ctx["last_params"],
                        ctx.get("last_prematch"),
                    ))

        # ── Recálculo fuera del lock (no bloquea WebSocket ni polling) ──
        for (ctx, snap, prev_state, raw_mkts, params, prematch) in recalculo_pendiente:
            try:
                update_match_math(ctx, snap, prev_state, raw_mkts, params, prematch)
            except Exception as e:
                print(f"[EXT WS] Error en recalculo rápido: {e}")


@app.get("/api/extension/status")
async def extension_status():
    """Estado de la conexión de la Chrome Extension."""
    return {
        "connected": len(_ext_ws_connected) > 0,
        "connections": len(_ext_ws_connected),
    }


# ─── Persistent Bet Lock endpoints ─────────────────────────────────────────────────
class BetLockReq(BaseModel):
    match_url:   str
    market:      str           # "Goles" | "Corners" | "Tarjetas"
    linea:       float
    side:        str           # "OVER" | "UNDER"
    stake_usd:   float = 0.0
    odds:        float = 0.0
    source:      str   = "manual"
    note:        str   = ""
    auto_expire_minutes: int = 90

class BetLockReleaseReq(BaseModel):
    lock_id: str


@app.get("/api/bets/locks")
async def list_bet_locks(match_url: str = None):
    """Devuelve todos los locks activos, opcionalmente filtrados por partido."""
    if not get_all_locks:
        return {"locks": [], "error": "bet_lock module not loaded"}
    return {"locks": get_all_locks(match_url)}


@app.post("/api/bets/lock")
async def create_bet_lock(req: BetLockReq):
    """Registra una apuesta como activa. Devuelve lock_id."""
    if not place_lock:
        raise HTTPException(status_code=503, detail="bet_lock module not loaded")
    lock_id = place_lock(
        match_url=req.match_url,
        market=req.market,
        linea=req.linea,
        side=req.side,
        stake_usd=req.stake_usd,
        odds=req.odds,
        source=req.source,
        note=req.note,
        auto_expire_minutes=req.auto_expire_minutes,
    )
    return {"status": "ok", "lock_id": lock_id}


@app.delete("/api/bets/lock/{lock_id}")
async def delete_bet_lock(lock_id: str):
    """Libera un lock manualmente (apuesta resuelta o cancelada)."""
    if not release_lock:
        raise HTTPException(status_code=503, detail="bet_lock module not loaded")
    found = release_lock(lock_id)
    return {"status": "ok" if found else "not_found"}


@app.delete("/api/bets/locks/match")
async def release_match_locks(url: str):
    """Libera todos los locks de un partido (por URL de SofaScore)."""
    if not release_all_for_match:
        raise HTTPException(status_code=503, detail="bet_lock module not loaded")
    count = release_all_for_match(url)
    return {"status": "ok", "released": count}

@app.delete("/api/matches")
async def remove_match(url: str):
    async with global_state["lock"]:
        if url in global_state["matches"]:
            ctx = global_state["matches"][url]
            if not ctx.get("ended", False):
                snap = ctx.get("last_snapshot")
                state = ctx.get("previous_state")
                if snap and state and not getattr(backend_args, "no_history", False):
                    try:
                        append_match_closure(Path(backend_args.history_dir), snap, state)
                    except Exception as e:
                        print(f"Error forzando cierre de {url}: {e}")
            del global_state["matches"][url]
            if url in active_urls:
                active_urls.remove(url)
    return {"status": "ok"}

@app.get("/api/matches")
async def get_matches():
    res = {}
    async with global_state["lock"]:
        for url, ctx in global_state["matches"].items():
            if ctx.get("ended"):
                continue
            d = ctx.get("data")
            if d and d.get("snapshot") and d.get("state") and d.get("result"):
                res[url] = d
            elif ctx.get("home_team"):
                # Construir estructura completa para que el frontend renderice
                res[url] = {
                    "snapshot": {
                        "home_team": ctx.get("home_team", "?"),
                        "away_team": ctx.get("away_team", "?"),
                        "minute": 0,
                        "goals_home": 0, "goals_away": 0,
                        "corners_home": 0, "corners_away": 0,
                        "yellows_home": 0, "yellows_away": 0,
                        "reds_home": 0, "reds_away": 0,
                        "event_id": ctx.get("event_id", 0),
                        "possession_home": 50,
                    },
                    "state": {"goles_local": 0, "goles_visitante": 0, "corners": 0, "amarillas": 0, "rojas": 0},
                    "result": {"markets": {}, "home_team": ctx.get("home_team", "?"), "away_team": ctx.get("away_team", "?")},
                    "scraper_ts": time.time(),
                    "sofascore_ts": time.time(),
                }
    return res

class SettingsReq(BaseModel):
    bankroll: float
    kelly_fraction: float
    preset: str
    poll_seconds: int
    initial_bankroll: float | None = None

@app.post("/api/settings")
async def update_settings(req: SettingsReq):
    data = req.dict(exclude_unset=True)
    async with global_state["lock"]:
        global_state["settings"].update(data)
        
        current = global_state["settings"]["bankroll"]
        initial = global_state["settings"].get("initial_bankroll", current)
        if initial > 0 and current < (initial * 0.85):
            global_state["settings"]["kill_switch_active"] = True
        else:
            global_state["settings"]["kill_switch_active"] = False
        
    return {"status": "ok"}

@app.get("/api/settings")
async def get_settings():
    return global_state["settings"]

if __name__ == "__main__":
    # Redirigir print() a archivo rotativo para no perder logs ni saturar disco
    import sys as _sys
    import logging
    from logging.handlers import RotatingFileHandler

    _log_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "server_output.log")
    
    class StreamToLogger(object):
        def __init__(self, logger, log_level=logging.INFO):
            self.logger = logger
            self.log_level = log_level
        def write(self, buf):
            for line in buf.rstrip().splitlines():
                if line.strip():
                    self.logger.log(self.log_level, line.rstrip())
        def flush(self):
            pass
        def isatty(self):
            return False

    _logger = logging.getLogger("server")
    _logger.setLevel(logging.INFO)
    # 50 MB por archivo, 5 backups
    _handler = RotatingFileHandler(_log_path, maxBytes=50 * 1024 * 1024, backupCount=5, encoding="utf-8")
    _handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(_handler)

    _sys.stdout = StreamToLogger(_logger, logging.INFO)
    _sys.stderr = StreamToLogger(_logger, logging.ERROR)

    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [STARTUP] ========== server.py iniciado ==========", flush=True)

    with open(_DIAG_LOG_PATH, "a", encoding="utf-8") as _f:
        _f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} [STARTUP] server.py iniciado en puerto 8000\n")
    # reload=False es OBLIGATORIO para un sistema de apuestas:
    # reload=True hace que uvicorn importe el módulo 3 veces (proceso padre +
    # worker + hot-reload watcher), lo que instancia 3 clientes API simultáneos
    # y triplica las peticiones a PS3838 — riesgo de rate-limit y ban.
    uvicorn.run("server:app", host="0.0.0.0", port=8000, reload=False, access_log=False)
