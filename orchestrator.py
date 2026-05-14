"""
orchestrator.py - Francotirador de Resistencia
================================================
Orquestador local que automatiza la captura masiva de datos de partidos
en vivo para el Motor de Trading Deportivo.

Arquitectura de 3 Modulos:
  Modulo 1: Matriz de Estado (diccionario en memoria con Lock)
  Modulo 2: Radar (polling SofaScore + Betano para descubrir partidos)
  Modulo 3: Despachador (gestiona hasta MAX_WORKERS instancias de Playwright)

Uso:
  python orchestrator.py                # Modo normal
  python orchestrator.py --dry-run      # Solo detecta partidos, no abre Chrome
  python orchestrator.py --max-workers 2 # Limitar workers (default: 4)
"""

import asyncio
import argparse
import json
import logging
import os
import re
import shutil
import time
import unicodedata
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional

import requests as http_requests  # Alias para no chocar con playwright
from curl_cffi import requests as cffi_requests  # TLS fingerprinting para SofaScore
import httpx  # Cliente HTTP async para bypass nativo Playwright → Backend
from thefuzz import fuzz
import psutil
import jmespath
try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))
except ImportError:
    pass  # python-dotenv no instalado — usar vars de entorno del sistema

# ---------------------------------------------------------------------------
# Configuracion
# ---------------------------------------------------------------------------
MAX_WORKERS = 8
RADAR_INTERVAL_SEC = 300          # 5 minutos
DISPATCH_INTERVAL_SEC = 15        # Revisar la cola cada 15 segundos
KILL_SWITCH_MINUTES = 130         # Matar workers colgados
FUZZY_THRESHOLD = 68              # Minimo % de coincidencia para emparejar equipos
# Chromium requiere forward slashes en rutas de extensiones, incluso en Windows
CHROME_EXTENSION_PATH = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "chrome_extension")
).replace("\\", "/")
TEMP_PROFILES_DIR = os.path.join(os.path.dirname(__file__), "temp_profiles")

# Detectar Chrome real instalado en Windows
_chrome_candidates = [
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    os.path.expanduser(r"~\AppData\Local\Google\Chrome\Application\chrome.exe"),
]
CHROME_EXECUTABLE_PATH = next((p for p in _chrome_candidates if os.path.exists(p)), None)
BETANO_BASE_URL = "https://www.betano.co"
SOFASCORE_LIVE_URL = "https://api.sofascore.com/api/v1/sport/football/events/live"
# Endpoints de Betano.co — top-events-v2 tiene partidos globales
BETANO_LIVE_URLS = [
    "https://www.betano.co/api/home/top-events-v2/",   # principal — global
    "https://www.betano.co/api/sb/v1/events/live/?sport=football&lang=es",  # backup
]
BETANO_TOP_EVENTS_URL = "https://www.betano.co/api/home/top-events-v2/"  # compat

# Cargar configuracion de esquemas (Desacoplado)
CONFIG_PATH = os.path.join(os.path.dirname(__file__), "config_schemas.json")
try:
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        API_SCHEMA_CONFIG = json.load(f).get("betano", {})
except Exception as e:
    # Fallback default if file is missing
    API_SCHEMA_CONFIG = {
        "live_endpoints": ["danae-webapi", "/api/live", "top-events-v2", "api/sport/", "isInit=false", "includeVirtuals"],
        "paths": {
            "api_top_events_dict": "data.topEventsV2.events",
            "live_overview_events_dict": "data.liveOverview.events",
            "root_events_dict": "events",
            "data_events_dict": "data.events",
            "participants": "participants",
            "team_local_name": "participants[0].name",
            "team_away_name": "participants[1].name",
            "event_url": "url",
            "is_live": "isLive",
            "sport_id": "sportId"
        },
        "values": {
            "football_snippet": "FOOT"
        }
    }

# ---------------------------------------------------------------------------
# Logging: Dual-Handler (Archivo rotativo + Consola silenciosa)
# ---------------------------------------------------------------------------
# Regla de produccion:
#   - Archivo (orchestrator.log): nivel INFO — registro completo para auditoria.
#     Rotacion automatica: max 10MB por archivo, guarda los ultimos 5.
#     En un fin de semana intenso (400 partidos) genera ~2-3 MB. Nunca saturara el disco.
#   - Consola (terminal): nivel WARNING — solo errores criticos y kill switches.
#     Evita que conhost.exe de Windows consuma RAM imprimiendo 400 cuotas/min.
# ---------------------------------------------------------------------------
from logging.handlers import RotatingFileHandler

LOG_FILE = os.path.join(os.path.dirname(__file__), "orchestrator.log")
LOG_FORMAT = "%(asctime)s [%(levelname)-8s] %(message)s"
LOG_DATE = "%Y-%m-%d %H:%M:%S"

_log_formatter = logging.Formatter(LOG_FORMAT, datefmt=LOG_DATE)

# Handler 1: Archivo rotativo (auditoria completa)
_file_handler = RotatingFileHandler(
    LOG_FILE,
    maxBytes=50 * 1024 * 1024,  # 50 MB por archivo
    backupCount=5,               # Guarda los ultimos 5 archivos (.log.1 ... .log.5)
    encoding="utf-8",
)
_file_handler.setLevel(logging.INFO)
_file_handler.setFormatter(_log_formatter)

# Handler 2: Consola (solo warnings y errores criticos) — UTF-8 forzado via wrapper
import sys as _sys
import io as _io
_console_stream = _io.TextIOWrapper(_sys.stdout.buffer, encoding='utf-8', errors='replace') \
    if hasattr(_sys.stdout, 'buffer') else _sys.stdout
_console_handler = logging.StreamHandler(_console_stream)
_console_handler.setLevel(logging.WARNING)
_console_handler.setFormatter(_log_formatter)

# Aplicar configuracion global
logging.root.setLevel(logging.DEBUG)  # Root captura todo; los handlers filtran
logging.root.addHandler(_file_handler)
logging.root.addHandler(_console_handler)

log = logging.getLogger("orchestrator")

# Silenciar logs verbosos de librerias de terceros (playwright, asyncio, urllib3)
for _noisy_lib in ("playwright", "asyncio", "urllib3", "httpcore", "httpx"):
    logging.getLogger(_noisy_lib).setLevel(logging.ERROR)

log.info("=" * 60)
log.info("  Sistema de Logging inicializado")
log.info(f"  Archivo de log: {LOG_FILE}")
log.info(f"  Consola: solo WARNING y superiores")
log.info("=" * 60)




# =========================================================================
# MODULO 1: LA MATRIZ DE ESTADO
# =========================================================================
class MatchStatus(Enum):
    PENDIENTE = "PENDIENTE"
    EN_CURSO = "EN_CURSO"
    TERMINADO = "TERMINADO"
    ERROR = "ERROR"


@dataclass
class MatchEntry:
    match_id: str                          # ID de SofaScore
    home_team: str
    away_team: str
    status: MatchStatus = MatchStatus.PENDIENTE
    has_advanced_stats: bool = False
    worker_id: Optional[int] = None
    betano_url: Optional[str] = None       # URL completa de Betano
    betano_event_id: Optional[int] = None
    start_time: float = 0.0                # Timestamp de cuando se asigno al worker
    last_dispatched_at: float = 0.0         # Cooldown para rotacion Round-Robin
    sofascore_status: str = ""             # "inprogress", "ended", etc.


class StateMatrix:
    """Modulo 1: Base de datos en memoria protegida por Lock."""

    def __init__(self):
        self._matches: dict[str, MatchEntry] = {}
        self._lock = asyncio.Lock()

    async def upsert(self, entry: MatchEntry) -> bool:
        """Inserta o actualiza un partido. Retorna True si es nuevo."""
        async with self._lock:
            is_new = entry.match_id not in self._matches
            if is_new:
                self._matches[entry.match_id] = entry
            else:
                existing = self._matches[entry.match_id]
                # Actualizar estado de SofaScore siempre
                existing.sofascore_status = entry.sofascore_status
                existing.has_advanced_stats = entry.has_advanced_stats
                # Actualizar betano_event_id si ahora lo tenemos (Radar con catalogo completo)
                if entry.betano_event_id and not existing.betano_event_id:
                    existing.betano_event_id = entry.betano_event_id
                    existing.betano_url = entry.betano_url
                    
                    # ── BRIDGE: Notificar a server.py (Backend) sobre el enlace ──
                    try:
                        import httpx
                        async with httpx.AsyncClient() as client:
                            await client.post(
                                "http://127.0.0.1:8000/api/matches/betano-id",
                                json={
                                    "match_url": existing.sofascore_url,
                                    "betano_event_id": existing.betano_event_id,
                                    "sofascore_event_id": int(entry.id) if str(entry.id).isdigit() else None
                                },
                                timeout=2.0
                            )
                    except Exception as e:
                        log.warning(f"  Bridge HTTP a server.py fallo: {e}")

                    # Resetear a PENDIENTE para que el dispatcher pueda asignarlo ahora
                    if existing.status == MatchStatus.EN_CURSO and not existing.worker_id:
                        existing.status = MatchStatus.PENDIENTE
            return is_new


    async def get_pending(self) -> Optional[MatchEntry]:
        """Retorna el partido PENDIENTE CON betano_event_id (listo para worker).
        Ordena por last_dispatched_at para garantizar un Round-Robin perfecto (el menos reciente primero)."""
        async with self._lock:
            # Requisito adicional: Solo procesar partidos con stats avanzadas (Tirano Market Edge)
            pending = [e for e in self._matches.values() if e.status == MatchStatus.PENDIENTE and e.betano_event_id and e.has_advanced_stats]
            if not pending:
                return None
            # El primero de la lista sera el que lleve mas tiempo sin despacharse (Least Recently Used)
            pending.sort(key=lambda e: e.last_dispatched_at)
            return pending[0]

    async def set_status(self, match_id: str, status: MatchStatus, worker_id: Optional[int] = None):
        async with self._lock:
            if match_id in self._matches:
                self._matches[match_id].status = status
                if worker_id is not None:
                    self._matches[match_id].worker_id = worker_id
                if status == MatchStatus.EN_CURSO:
                    self._matches[match_id].start_time = time.time()
                    self._matches[match_id].last_dispatched_at = time.time()

    async def get_active_workers(self) -> list[MatchEntry]:
        """Retorna los partidos actualmente EN_CURSO."""
        async with self._lock:
            return [e for e in self._matches.values() if e.status == MatchStatus.EN_CURSO]

    async def get_stale_workers(self, max_minutes: float) -> list[MatchEntry]:
        """Retorna workers que llevan mas de max_minutes activos (Kill Switch)."""
        async with self._lock:
            now = time.time()
            return [
                e for e in self._matches.values()
                if e.status == MatchStatus.EN_CURSO
                and (now - e.start_time) > (max_minutes * 60)
            ]

    async def mark_ended_by_sofascore(self) -> list[MatchEntry]:
        """Marca como TERMINADO los partidos que SofaScore reporta como finalizados."""
        async with self._lock:
            ended = []
            for e in self._matches.values():
                if e.status == MatchStatus.EN_CURSO and e.sofascore_status == "finished":
                    e.status = MatchStatus.TERMINADO
                    ended.append(e)
            return ended

    async def summary(self) -> dict:
        async with self._lock:
            counts = {}
            for e in self._matches.values():
                counts[e.status.value] = counts.get(e.status.value, 0) + 1
            return counts


# =========================================================================
# MODULO 2: EL RADAR (SofaScore + Betano Fuzzy Matcher)
# =========================================================================
# _normalize is defined at line ~575 (after Betano helpers)


def fetch_sofascore_live() -> list[dict]:
    """Consulta SofaScore por los partidos de futbol en vivo.
    Usa curl_cffi con impersonate para bypassear el fingerprinting TLS de Cloudflare.
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Origin": "https://www.sofascore.com",
        "Referer": "https://www.sofascore.com/",
    }
    try:
        r = cffi_requests.get(
            SOFASCORE_LIVE_URL,
            headers=headers,
            impersonate="chrome124",
            timeout=15,
        )
        r.raise_for_status()
        r.encoding = 'utf-8'
        return r.json().get("events", [])
    except Exception as e:
        log.warning(f"Radar SofaScore fallo: {e}")
        return []


def fetch_betano_events() -> dict[int, dict]:
    """Consulta la API de Betano.co por TODOS los eventos de fútbol en vivo.
    Usa el endpoint /api/sport/football/events/?status=live en lugar de top-events
    para obtener partidos globales (no solo los 'destacados' de Colombia).
    """
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
        "Referer": "https://www.betano.co/live/",
        "Origin": "https://www.betano.co",
    }
    result = {}
    
    for url in BETANO_LIVE_URLS:
        try:
            r = http_requests.get(url, headers=headers, timeout=15)
            if r.status_code == 404:
                log.info(f"  Betano: {url.split('/')[-3]} → 404, probando siguiente...")
                continue
            r.raise_for_status()
            r.encoding = 'utf-8'
            data = r.json()
            
            # El endpoint live devuelve data.events como lista [{id, participants, url, ...}]
            events_list = None
            if isinstance(data.get("data"), dict):
                events_list = data["data"].get("events") or data["data"].get("items")
            if not events_list and isinstance(data.get("events"), list):
                events_list = data["events"]
            
            if events_list and isinstance(events_list, list):
                for ev in events_list:
                    eid = ev.get("id") or ev.get("eventId")
                    if not eid:
                        continue
                    parts = ev.get("participants", [])
                    if len(parts) >= 2:
                        home = parts[0].get("name", "")
                        away = parts[1].get("name", "")
                    else:
                        home = ev.get("homeTeam", {}).get("name", "") if isinstance(ev.get("homeTeam"), dict) else ""
                        away = ev.get("awayTeam", {}).get("name", "") if isinstance(ev.get("awayTeam"), dict) else ""
                    ev_url = ev.get("url") or ev.get("eventUrl") or ""
                    if home and away:
                        result[int(eid)] = {"id": int(eid), "home": home, "away": away, "url": ev_url, "is_live": ev.get("isLive", ev.get("status") == "live")}
                if result:
                    log.info(f"  Betano live API ({url.split('?')[0].split('/')[-3:]}): {len(result)} partidos en vivo")
                    return result

            # Fallback: formato top-events-v2 (dict de eventos)
            events_path = API_SCHEMA_CONFIG.get("paths", {}).get("api_top_events_dict", "data.topEventsV2.events")
            events_dict = jmespath.search(events_path, data) or {}
            if isinstance(events_dict, dict):
                paths = API_SCHEMA_CONFIG.get("paths", {})
                for eid_str, ev in events_dict.items():
                    home = jmespath.search(paths.get("team_local_name", ""), ev)
                    away = jmespath.search(paths.get("team_away_name", ""), ev)
                    if home and away:
                        result[int(eid_str)] = {
                            "id": int(eid_str), "home": home, "away": away,
                            "url": jmespath.search(paths.get("event_url", ""), ev) or "",
                            "is_live": True,
                        }
                if result:
                    return result

        except Exception as e:
            log.warning(f"  Betano API fallo ({url.split('/')[-3]}): {e}")
            continue
    
    if not result:
        log.warning("  Betano API: ningún endpoint respondió. Dependiendo del Discovery Worker.")
    return result


async def discover_betano_live_events(pw) -> dict[int, dict]:
    """Discovery Worker: usa Playwright para interceptar la API interna de Betano.

    Betano es una SPA que carga eventos via danae-webapi y api/live/ con fetch
    desde el browser. No hay endpoint publico — la unica forma profesional es
    actuar como Man-in-the-Middle local usando page.on('response').

    Estrategia:
      1. Lanza un Chromium headless en modo silencioso (sin extension, solo sniffer).
      2. Instala el listener page.on('response') antes de navegar.
      3. Navega a /live/ y hace scroll para forzar lazy loading.
      4. Captura los JSON de danae-webapi y api/live/overview.
      5. Extrae eventos (participantes + URL) y cierra el browser.
    """
    import json as json_mod

    captured_events: dict[int, dict] = {}

    browser = None
    try:
        browser = await pw.chromium.launch(
            executable_path=CHROME_EXECUTABLE_PATH,
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        page = await browser.new_page(
            viewport={"width": 1280, "height": 900},
        )

        def _extract_events_from_payload(data: dict):
            """Extrae eventos de la estructura de datos usando el esquema configurado con JMESPath."""
            paths = API_SCHEMA_CONFIG.get("paths", {})
            v_foot = API_SCHEMA_CONFIG.get("values", {}).get("football_snippet", "FOOT")

            # Buscar diccionarios de eventos en las rutas conocidas
            events_dicts = [
                jmespath.search(paths.get("api_top_events_dict", ""), data),
                jmespath.search(paths.get("live_overview_events_dict", ""), data),
                jmespath.search(paths.get("root_events_dict", ""), data),
                jmespath.search(paths.get("data_events_dict", ""), data)
            ]

            for events_raw in events_dicts:
                if not events_raw: continue
                
                # Convertir a lista de items para procesamiento uniforme
                items = []
                if isinstance(events_raw, dict):
                    for eid_str, ev in events_raw.items():
                        if isinstance(ev, dict):
                            ev["_eid_from_key"] = eid_str
                            items.append(ev)
                elif isinstance(events_raw, list):
                    items = events_raw

                for ev in items:
                    try:
                        eid = ev.get("id") or ev.get("eventId") or ev.get("_eid_from_key")
                        if not eid: continue
                        eid = int(eid)
                        
                        home = jmespath.search(paths.get("team_local_name", ""), ev)
                        away = jmespath.search(paths.get("team_away_name", ""), ev)
                        url = jmespath.search(paths.get("event_url", ""), ev)
                        sport = jmespath.search(paths.get("sport_id", ""), ev) or ""
                        is_live = jmespath.search(paths.get("is_live", ""), ev)
                        if is_live is None: is_live = True
                        
                        if home and away and url and (v_foot in str(sport).upper() or not sport):
                            captured_events[eid] = {
                                "id": eid,
                                "home": home,
                                "away": away,
                                "url": url,
                                "is_live": is_live,
                            }
                    except (ValueError, TypeError):
                        continue

        async def handle_response(response):
            url = response.url
            # Interceptar las APIs internas de Betano segun configuracion
            is_target = any(ep in url for ep in API_SCHEMA_CONFIG["live_endpoints"])
            if not is_target:
                return
            try:
                ct = response.headers.get("content-type", "")
                if response.status == 200 and "json" in ct:
                    text = await response.text()
                    if len(text) > 100:
                        data = json_mod.loads(text)
                        prev_count = len(captured_events)
                        _extract_events_from_payload(data)
                        new_count = len(captured_events) - prev_count
                        if new_count > 0:
                            log.info(
                                f"  Discovery: +{new_count} eventos desde {url[:80]}"
                            )
            except Exception:
                pass

        page.on("response", handle_response)

        # Navegar a la pagina de live
        try:
            await page.goto(
                "https://www.betano.com/live/",
                wait_until="domcontentloaded",
                timeout=30000,
            )
        except Exception as e:
            log.error(f"  Discovery Worker fallo: Page.goto: {e}")
            await browser.close()
            return captured_events # Betano bloqueó o no carga, retornamos dict vacío para no crashear

        # Esperar carga inicial
        await asyncio.sleep(3)

        # Scroll Fantasma: mover mouse al contenedor de partidos y girar rueda
        await page.mouse.move(400, 400)
        await asyncio.sleep(0.5)
        for i in range(6):
            await page.mouse.wheel(0, 3000)
            await asyncio.sleep(1.5)
        await page.mouse.wheel(0, -20000)
        await asyncio.sleep(1)

        # Dar tiempo extra para que lleguen las respuestas
        await asyncio.sleep(2)
        await browser.close()

        log.info(
            f"  Discovery Worker: {len(captured_events)} eventos futbol en vivo detectados"
        )

    except Exception as e:
        log.warning(f"  Discovery Worker fallo: {e}")
    finally:
        if browser:
            try:
                await browser.close()
            except Exception:
                pass

    return captured_events




def build_betano_url_from_names(home: str, away: str) -> str:
    """Construye una URL de Betano desde los nombres de los equipos.
    Betano usa el patron: /cuotas-de-partido/{home-slug}-{away-slug}/{eventId}/
    Sin eventId, construimos solo el slug para buscar despues.
    """
    import re
    def slugify(name: str) -> str:
        s = name.lower().strip()
        s = re.sub(r'[^a-z0-9\s-]', '', s)
        s = re.sub(r'[\s]+', '-', s)
        return s
    return f"/cuotas-de-partido/{slugify(home)}-{slugify(away)}/"


# Palabras genéricas a ignorar al comparar nombres de equipos
_NOISE_WORDS = {
    "fc", "cf", "ac", "sc", "rc", "cd", "sd", "ud", "rcd", "afc",
    "city", "united", "sporting", "club", "de", "do",
    "da", "del", "the", "real", "racing",
    "athletic", "wanderers", "town", "county", "albion", "rovers",
    "wolves", "rangers", "forest", "villa", "hotspur", "crystal",
    "palace", "argyle", "borough", "orient", "vale", "wycombe",
}

def _normalize(name: str) -> str:
    """Normaliza nombre de equipo: acentos->ASCII, lowercase, elimina ruido.
    'Famalicão' → 'famalicao' | 'Göztepe' → 'goztepe' | 'FC Barcelona' → 'barcelona'
    """
    if not name:
        return ""
    nfkd = unicodedata.normalize("NFKD", name)
    ascii_name = nfkd.encode("ascii", "ignore").decode("ascii")
    import re as _re2
    clean = _re2.sub(r"[^a-z0-9\s]", " ", ascii_name.lower())
    # Expandir siglas comunes para mejorar matching
    clean = clean.replace(" rsc ", " ").replace(" rcd ", " ")
    clean = clean.replace(" esp ", " espanol ").replace(" dep ", " deportivo ")
    tokens = [t for t in clean.split() if t not in _NOISE_WORDS and len(t) > 1]
    return " ".join(tokens).strip()


# ── Diccionario de Sinonimos: cachea matches exitosos para evitar re-procesar ──
_SYNONYM_PATH = os.path.join(os.path.dirname(__file__), "team_synonyms.json")

def _load_synonyms() -> dict:
    try:
        with open(_SYNONYM_PATH, "r") as f:
            return json.load(f)
    except Exception:
        return {}

def _save_synonyms(syns: dict):
    try:
        with open(_SYNONYM_PATH, "w") as f:
            json.dump(syns, f, indent=2)
    except Exception:
        pass

def fuzzy_match_betano(
    sofa_home: str,
    sofa_away: str,
    betano_events: dict[int, dict],
) -> Optional[dict]:
    """
    Motor de Emparejamiento Borroso (Fuzzy Matcher).
    1. Diccionario de sinonimos (cacheo de matches previos)
    2. partial_ratio + token_sort_ratio (fuzzy)
    3. Reverse match (swap home/away)
    """
    # ── 0. Buscar en diccionario de sinonimos ──
    syns = _load_synonyms()
    key = f"{_normalize(sofa_home)}|{_normalize(sofa_away)}"
    if key in syns:
        cached = syns[key]
        # Verificar que el betano_event_id sigue en el catalogo
        if cached["betano_eid"] in betano_events:
            log.info(f"  📖 [SYNONYM] '{sofa_home}' vs '{sofa_away}' → cacheado (betano_eid={cached['betano_eid']})")
            return {"id": cached["betano_eid"], "betano_event_id": cached["betano_eid"],
                    "home": cached.get("betano_home", sofa_home), "away": cached.get("betano_away", sofa_away),
                    "url": cached.get("url", "")}

    best_score = 0.0
    best_match = None
    best_candidate = ("", "")

    norm_home = _normalize(sofa_home)
    norm_away = _normalize(sofa_away)

    for eid, bev in betano_events.items():
        b_home = _normalize(bev.get("home", ""))
        b_away = _normalize(bev.get("away", ""))

        # Match directo
        home_score = max(fuzz.token_sort_ratio(norm_home, b_home),
                         fuzz.partial_ratio(norm_home, b_home))
        away_score = max(fuzz.token_sort_ratio(norm_away, b_away),
                         fuzz.partial_ratio(norm_away, b_away))
        direct = (home_score + away_score) / 2.0

        # Match reverso (home↔away)
        home_rev = max(fuzz.token_sort_ratio(norm_home, b_away),
                       fuzz.partial_ratio(norm_home, b_away))
        away_rev = max(fuzz.token_sort_ratio(norm_away, b_home),
                       fuzz.partial_ratio(norm_away, b_home))
        reverse = (home_rev + away_rev) / 2.0

        combined = max(direct, reverse)

        if combined > best_score:
            best_score = combined
            best_match = bev
            best_candidate = (bev.get("home", ""), bev.get("away", ""))

    if best_score >= FUZZY_THRESHOLD and best_match:
        # Cachear en diccionario de sinonimos
        syns = _load_synonyms()
        key = f"{_normalize(sofa_home)}|{_normalize(sofa_away)}"
        syns[key] = {
            "betano_home": best_candidate[0], 
            "betano_away": best_candidate[1], 
            "betano_eid": best_match.get("id"), 
            "url": best_match.get("url", ""), 
            "score": int(best_score)
        }
        _save_synonyms(syns)
        return best_match
    
    # ── Second chance: intentar con solo la ultima palabra significativa ──
    if best_score >= FUZZY_THRESHOLD - 10:
        nh_tokens = norm_home.split()
        na_tokens = norm_away.split()
        last_home = nh_tokens[-1] if nh_tokens else ""
        last_away = na_tokens[-1] if na_tokens else ""
        if last_home and last_away:
            for eid, bev in betano_events.items():
                b_home = _normalize(bev.get("home", ""))
                b_away = _normalize(bev.get("away", ""))
                bh_tokens = b_home.split()
                ba_tokens = b_away.split()
                last_bh = bh_tokens[-1] if bh_tokens else ""
                last_ba = ba_tokens[-1] if ba_tokens else ""
                if (last_home == last_bh and last_away == last_ba) or \
                   (last_home == last_ba and last_away == last_bh):
                    log.info(f"  🎯 [LAST-WORD MATCH] '{sofa_home}' vs '{sofa_away}' → '{bev.get('home','')}' vs '{bev.get('away','')}' (score={best_score:.0f}%)")
                    syns = _load_synonyms()
                    key = f"{_normalize(sofa_home)}|{_normalize(sofa_away)}"
                    syns[key] = {
                        "betano_home": bev.get("home", ""),
                        "betano_away": bev.get("away", ""),
                        "betano_eid": bev.get("id"),
                        "url": bev.get("url", ""),
                        "score": int(best_score)
                    }
                    _save_synonyms(syns)
                    return bev
    
    # Imprimir error solo una vez al final
    log.info(f"  Sin Match: '{sofa_home}' vs '{sofa_away}' (mejor: '{best_candidate[0]}' vs '{best_candidate[1]}' = {best_score:.0f}%)")
    return None

async def _registrar_betano_directo(sofa_id: str, home: str, away: str, betano_eid: int, betano_url: str):
    """Registro sin SofaScore: envia home/away/betano_id directamente al backend."""
    try:
        payload = {"home": home, "away": away, "betano_event_id": int(betano_eid), "betano_url": betano_url}
        resp = await asyncio.to_thread(
            http_requests.post,
            "http://localhost:8000/api/matches/register-direct",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            log.info(f"  📝 [DIRECT] {home} vs {away} registrado (betano_id={betano_eid})")
        else:
            log.warning(f"  ⚠️ [DIRECT] HTTP {resp.status_code}")
    except Exception as e:
        log.warning(f"  ❌ [DIRECT] Error: {e}")

async def _registrar_en_backend(sofa_id: str, home: str, away: str):
    """Llama a la API del backend para registrar el partido y resolver el Pinnacle event_id."""
    try:
        payload = {"event_id": int(sofa_id)}
        resp = await asyncio.to_thread(
            http_requests.post,
            "http://localhost:8000/api/matches/resolve",
            json=payload,
            timeout=45,
        )
        if resp.status_code == 200:
            data = resp.json()
            pid = data.get("pinnacle_event_id")
            if pid:
                log.info(f"  🔗 [BACKEND] {home} vs {away} → Pinnacle ID {pid}")
            else:
                log.info(f"  📝 [BACKEND] {home} vs {away} registrado (sin Pinnacle ID)")
            return True
        else:
            log.warning(f"  ⚠️ [BACKEND] {home} vs {away}: HTTP {resp.status_code}")
            return False
    except Exception as e:
        log.warning(f"  ❌ [BACKEND] Error registrando {home} vs {away}: {e}")
        return False


async def _registrar_betano_id_en_backend(sofa_id: str, betano_eid: int, betano_url: str | None):
    """Registra el Betano event_id en el servidor para que los SignalR del worker
    se enruten correctamente al match context del SofaScore."""
    try:
        payload = {"match_url": "", "betano_event_id": int(betano_eid), "sofascore_event_id": int(sofa_id)}
        resp = await asyncio.to_thread(
            http_requests.post,
            "http://localhost:8000/api/matches/betano-id",
            json=payload,
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            if data.get("matched_by_event_id"):
                log.info(f"  🔗 [BETANO-ID] sofascore_id={sofa_id} → betano_eid={betano_eid}")
        else:
            log.warning(f"  ⚠️ [BETANO-ID] HTTP {resp.status_code} para betano_eid={betano_eid}")
    except Exception as e:
        log.warning(f"  ❌ [BETANO-ID] Error: {e}")


async def radar_tick(state: StateMatrix, pw):
    """Un solo ciclo del Radar: detecta partidos y los inyecta en la Matriz."""
    log.info("--- Radar: Escaneando partidos en vivo ---")

    # 1. Obtener partidos de SofaScore (puede fallar por baneo)
    sofa_events = await asyncio.to_thread(fetch_sofascore_live)
    football_live = [
        e for e in sofa_events
        if e.get("status", {}).get("type") == "inprogress"
    ]
    # Separar partidos con y sin stats avanzadas (solo los primeros van a workers)
    football_with_stats = [e for e in football_live if e.get("hasEventPlayerStatistics")]
    football_no_stats = [e for e in football_live if not e.get("hasEventPlayerStatistics")]

    log.info(
        f"  SofaScore: {len(sofa_events)} en vivo, "
        f"{len(football_live)} futbol activo "
        f"({len(football_with_stats)} con stats avanzadas, {len(football_no_stats)} sin)"
    )

    # Solo los partidos con stats avanzadas van a workers (mejor calidad de datos)
    # Los sin stats se registran igual para monitoreo pero no se despachan
    quality_events = football_with_stats if football_with_stats else football_live

    # 2. Obtener catalogo de Betano SIEMPRE (incluso si SofaScore falla)
    betano_api = await asyncio.to_thread(fetch_betano_events)
    betano_discovered = await discover_betano_live_events(pw)
    betano_catalog = {**betano_api, **betano_discovered}
    server_catalog_count = 0
    try:
        resp = await asyncio.to_thread(
            http_requests.get,
            "http://localhost:8000/api/betano/catalog",
            timeout=10,
        )
        if resp.status_code == 200:
            data = resp.json()
            server_events = data.get("events", [])
            server_catalog_count = len(server_events)
            for ev in server_events:
                eid = ev.get("id")
                if eid and eid not in betano_catalog:
                    betano_catalog[eid] = {
                        "id": eid,
                        "home": ev.get("home", ""),
                        "away": ev.get("away", ""),
                        "url": ev.get("betano_url") or ev.get("url", ""),
                        "betano_event_id": eid,
                    }
            if server_events:
                log.info(f"  ServerCatalog: +{server_catalog_count} eventos de Betano desde overview")
    except Exception as e:
        log.warning(f"  Error consultando /api/betano/catalog: {e}")
    log.info(
        f"  Betano: {len(betano_api)} API + {len(betano_discovered)} Discovery + "
        f"{server_catalog_count} Server "
        f"= {len(betano_catalog)} total"
    )

    # ── FALLBACK: si SofaScore esta baneado, usar catalogo de Betano ──
    if not football_live and betano_catalog:
        log.warning("⚠️ SofaScore sin datos (baneo?). Usando Betano como fuente de partidos.")
        using_betano_fallback = True
        # Verificar live-ness via stats API en PARALELO (no secuencial)
        from curl_cffi.requests import AsyncSession as _CurlSession
        candidates = []
        for eid, info in betano_catalog.items():
            home = (info.get("home") or "").strip()
            away = (info.get("away") or "").strip()
            if not home or not away:
                continue
            if "(esports)" in f"{home} {away}".lower():
                continue
            candidates.append((eid, home, away))
        
        async def _check_live(_client, eid, home, away):
            try:
                r = await _client.get(f"https://www.betano.co/api/statsstream/{eid}/stats/detailed/")
                if r.status_code == 200:
                    return (eid, home, away)
            except Exception:
                pass
            return None
        
        async with _CurlSession(impersonate="chrome124", timeout=5) as _client:
            tasks = [_check_live(_client, eid, h, a) for eid, h, a in candidates]
            results = await asyncio.gather(*tasks)
            for r in results:
                if r:
                    eid, home, away = r
                    football_live.append({
                        "id": eid,
                        "homeTeam": {"name": home},
                        "awayTeam": {"name": away},
                        "status": {"type": "inprogress"},
                    })
        
        log.info(f"  Betano Fallback: {len(football_live)}/{len(candidates)} vivos (stats API, paralelo) | catálogo={len(betano_catalog)}")
    else:
        using_betano_fallback = False

    if not football_live:
        log.info("  No hay partidos de futbol en vivo en este momento.")
        return

    # 3. Emparejar y registrar (solo partidos con stats avanzadas)
    new_count = 0
    matched_count = 0
    for ev in quality_events:
        sofa_id = str(ev.get("id", ""))
        home = ev.get("homeTeam", {}).get("name", "?")
        away = ev.get("awayTeam", {}).get("name", "?")

        # Intentar emparejar con Betano (fuzzy match contra catalogo combinado)
        betano_match = await asyncio.to_thread(
            fuzzy_match_betano, home, away, betano_catalog
        )

        # Resolver URL final
        betano_url = None
        betano_eid = None
        if betano_match:
            match_url = betano_match.get("url", "")
            if match_url.startswith("http"):
                betano_url = match_url  # Server catalog ya envía URL completa
            elif match_url:
                betano_url = BETANO_BASE_URL + match_url
            betano_eid = betano_match.get("betano_event_id") or betano_match["id"]
            matched_count += 1
        else:
            # Fallback: construir URL desde los nombres de SofaScore
            # Betano usa el patron /cuotas-de-partido/{slug}/{eventId}/
            # Sin eventId, usamos la pagina de busqueda como target
            constructed_slug = build_betano_url_from_names(home, away)
            # No podemos navegar sin eventId — marcar como solo monitoreo
            betano_url = None

        entry = MatchEntry(
            match_id=sofa_id,
            home_team=home,
            away_team=away,
            has_advanced_stats=True,
            sofascore_status=ev.get("status", {}).get("type", ""),
            betano_url=betano_url,
            betano_event_id=betano_eid,
        )

        is_new = await state.upsert(entry)
        if is_new:
            new_count += 1
            if betano_url:
                log.info(f"  [NUEVO] {home} vs {away} -> Betano URL resuelta")
            # Registrar en el backend asegurando orden de operaciones
            if using_betano_fallback and betano_eid:
                asyncio.create_task(_registrar_betano_directo(sofa_id, home, away, betano_eid, betano_url or ""))
            else:
                # Wrap it to await registration before sending ID bridge
                async def _chain_registration():
                    success = await _registrar_en_backend(sofa_id, home, away)
                    # Si hay Betano ID, registrarlo en el servidor CUIDADOSAMENTE un poco después
                    # para dar tiempo a que el servidor inicialice _todo el contexto de SofaScore
                    if success and betano_eid and not using_betano_fallback:
                        await asyncio.sleep(2)
                        await _registrar_betano_id_en_backend(sofa_id, betano_eid, betano_url)
                
                asyncio.create_task(_chain_registration())

    log.info(
        f"  Radar completado: {new_count} nuevos, "
        f"{matched_count} emparejados con Betano"
    )


# =========================================================================
# MODULO 3: EL DESPACHADOR (Playwright Worker Manager)
# =========================================================================
class WorkerSlot:
    """Representa un slot de worker con su contexto de Playwright."""

    def __init__(self, worker_id: int):
        self.worker_id = worker_id
        self._playwright = None   # instancia de async_playwright (para launch)
        self._browser = None      # Browser (de launch())
        self.browser = None       # BrowserContext
        self.page = None
        self.match_id: Optional[str] = None
        self._shared_context = False
        self.profile_dir = os.path.join(TEMP_PROFILES_DIR, f"worker_{worker_id}")

    async def launch(self, match: MatchEntry, state: 'StateMatrix', dry_run: bool = False, shared_context=None):
        """Round-Robin Betano: abre pagina → espera → cierra → libera slot."""
        self.match_id = match.match_id

        if dry_run:
            log.info(
                f"  [DRY-RUN] Worker {self.worker_id} SIMULARIA abrir: "
                f"{match.home_team} vs {match.away_team} -> betano_event_id={match.betano_event_id}"
            )
            self.match_id = None
            return

        # ── Usar contexto compartido si existe (1 solo Chrome) ──
        if shared_context:
            self.browser = shared_context
            self._playwright = None
            self._shared_context = True
        else:
            # Fallback legacy: cada worker su propio Chrome
            from playwright.async_api import async_playwright
            os.makedirs(self.profile_dir, exist_ok=True)
            self._shared_context = False
            pw = await async_playwright().start()
            self._playwright = pw
            self.browser = await pw.chromium.launch_persistent_context(
                self.profile_dir,
                executable_path=CHROME_EXECUTABLE_PATH,
                headless=False,
                args=[
                    f"--disable-extensions-except={CHROME_EXTENSION_PATH}",
                    f"--load-extension={CHROME_EXTENSION_PATH}",
                    "--no-first-run",
                    "--disable-blink-features=AutomationControlled",
                    "--mute-audio",
                    "--start-maximized",
                ],
                viewport={"width": 1920, "height": 1080},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                ignore_https_errors=True,
            )

        self._browser = None
        self.page = await self.browser.new_page()

        # ── BYPASS NATIVO DEL WORKER: capturar respuesta REST del evento ──
        # Intercepta la respuesta HTTP de danae-webapi (event/markets/selections)
        # y la inyecta al backend SIN depender de la Chrome Extension.
        async def _interceptar_evento_nativo(response):
            url = response.url
            if response.status != 200:
                return
            ct = response.headers.get("content-type", "")
            if "json" not in ct:
                return
            if ("danae-webapi" in url or "api/live" in url):
                try:
                    json_data = await response.json()
                    keys = list(json_data.keys()) if isinstance(json_data, dict) else f"list({len(json_data)})"
                    # Solo loggear overviews y eventos individuales (no assets, settings, etc.)
                    if any(k in keys for k in ("events", "event", "sports", "markets", "selections")):
                        log.info(f"🔥 [W{self.worker_id}] JSON Betano: keys={keys} url={url[:120]}")
                    async with httpx.AsyncClient() as client:
                        res = await client.post(
                            "http://127.0.0.1:8000/api/betano/ingest",
                            json={"url": url, "data": json_data},
                            timeout=15,
                        )
                        if res.status_code != 200:
                            log.warning(f"  [W{self.worker_id}] POST ingest → {res.status_code}")
                except Exception:
                    pass  # Silencioso: errores de protocolo Playwright son normales

        self.page.on("response", _interceptar_evento_nativo)

        if match.betano_url or match.betano_event_id:
            betano_url = match.betano_url or f"https://www.betano.co/live/{match.betano_event_id}/"
            log.info(f"  [RR-BETANO] Worker {self.worker_id}: {match.home_team} vs {match.away_team} -> {betano_url[:100]}")
            try:
                await self.page.goto(betano_url, wait_until="domcontentloaded", timeout=15000)
                # Esperamos 3.5 segundos para que la extensión de Chrome se inyecte por completo.
                await self.page.wait_for_timeout(3500)
                # RECARGA FORZADA (F5) para que el "/events/{id}" inicial se vuelva a pedir
                # y la extensión YA instalada lo intercepte 100% seguro.
                log.info(f"  [RR-BETANO] Worker {self.worker_id}: Recargando página para asegurar inyección del caché JSON inicial...")
                await self.page.reload(wait_until="domcontentloaded", timeout=15000)
            except Exception as e:
                log.warning(f"  [RR-BETANO] Worker {self.worker_id}: goto fallo: {e}")
        else:
            log.info(f"  [RR-BETANO] Worker {self.worker_id}: Sin betano_event_id para {match.home_team} vs {match.away_team}")
            await self.page.goto("about:blank", wait_until="domcontentloaded", timeout=5000)

        # ── Esperar que la pagina cargue y los interceptors capturen datos tras el reload ──
        await self.page.wait_for_timeout(3000)

        # ── Simulacion humana para despertar ContentHub ──
        try:
            # 1. Quitar popups/molestias (cookies, modales) que roban el foco
            close_selectors = ["button[aria-label='Cerrar']", "button[aria-label='Close']",
                               ".cookie-consent-accept", ".modal-close", "svg.close-icon",
                               "button:has-text('Aceptar')", "button:has-text('Accept')"]
            for sel in close_selectors:
                try:
                    await self.page.click(sel, timeout=2000)
                    await self.page.wait_for_timeout(300)
                except Exception:
                    pass

            # 2. Scroll + mouse para activar listeners (onScroll, onMouseMove, IntersectionObserver)
            await self.page.mouse.move(300, 400)
            await self.page.wait_for_timeout(400)
            await self.page.mouse.wheel(0, 600)
            await self.page.wait_for_timeout(600)
            await self.page.mouse.wheel(0, -300)
            await self.page.wait_for_timeout(400)
            await self.page.mouse.move(600, 300)
            await self.page.wait_for_timeout(300)
            await self.page.mouse.wheel(0, 400)
            log.info(f"  [RR-BETANO] Worker {self.worker_id}: Interaccion humana completada.")
        except Exception as e:
            log.warning(f"  [RR-BETANO] Worker {self.worker_id}: Error en simulacion: {e}")

        # 🎯 EL PASEO POR LAS PESTAÑAS (Para despertar los WebSockets ocultos)
        log.info(f"  [RR-BETANO] Worker {self.worker_id}: 🖱️ Explorando pestañas para activar todos los mercados...")
        import re
        # Búsqueda abierta, case insensitive, sin restricciones de anchors al inicio o fin.
        pestañas_deseadas = [
            ("Goles", re.compile(r"(goles|goals)", re.IGNORECASE)),
            ("Corners", re.compile(r"(córners|corners|esquina)", re.IGNORECASE)),
            ("Tarjetas", re.compile(r"(tarjeta|amonestaci|disciplin)", re.IGNORECASE)),
        ]
        
        for nombre, regex in pestañas_deseadas:
            try:
                # get_by_role('tab') es más estable que texto flotante.
                tab = self.page.get_by_role("tab", name=regex).first
                if not await tab.is_visible():
                    tab = self.page.get_by_text(regex).first
                
                if await tab.is_visible():
                    # force=True es clave en Betano porque a veces los tabs estan 'ocultos' bajo el header o flechas del carrusel
                    await tab.scroll_into_view_if_needed(timeout=1000)
                    await tab.click(timeout=1500, force=True)
                    log.info(f"  [RR-BETANO] Worker {self.worker_id}: ✅ Pestaña '{nombre}' activada.")
                    await self.page.wait_for_timeout(2000)
                else:
                    log.info(f"  [RR-BETANO] Worker {self.worker_id}: ⚠️ El partido no ofrece la pestaña '{nombre}'.")
            except Exception:
                pass

        try:
            tab_todos = self.page.get_by_role("tab", name=re.compile(r"^(todos|principales)$", re.IGNORECASE)).first
            if not await tab_todos.is_visible():
                tab_todos = self.page.get_by_text(re.compile(r"^(todos|principales)$", re.IGNORECASE), exact=True).first
            if await tab_todos.is_visible():
                await tab_todos.scroll_into_view_if_needed(timeout=1000)
                await tab_todos.click(timeout=1000, force=True)
        except Exception:
            pass

        # Bucle Dinámico de Retención
        log.info(f"  [RR-BETANO] Worker {self.worker_id}: 📡 Inicio retención de worker...")
        start_retention = time.time()
        
        while self.page and not self.page.is_closed():
            active_sec = time.time() - start_retention
            
            # Garantía mínima de captura (12s) - fase inicial
            if active_sec < 12:
                await self.page.wait_for_timeout(2000)
                continue
            
            # Revisar si hay partidos esperando turno (Starvation)
            pending_match = await state.get_pending()
            
            if pending_match:
                # ── STAGGERED RELEASE: solo libera si este worker es el más viejo ──
                # Esto previene que todos los workers se cierren al mismo tiempo
                # cuando detectan starvation. Solo 1 se libera por ciclo.
                async with state._lock:
                    active_entries = [
                        e for e in state._matches.values()
                        if e.status == MatchStatus.EN_CURSO and e.start_time > 0
                    ]
                if active_entries:
                    oldest_match_id = min(active_entries, key=lambda e: e.start_time).match_id
                    if self.match_id == oldest_match_id and active_sec >= 15:
                        log.info(
                            f"  [RR-BETANO] Worker {self.worker_id}: Starvation detectada "
                            f"(Pendiente: {pending_match.home_team}). "
                            f"Soy el más viejo ({active_sec:.0f}s). Liberando slot."
                        )
                        break
                    # No soy el más viejo: sigo retenido, el más viejo se libera primero
            else:
                # No hay hambre: el worker se queda reteniendo la conexión viva, evitando fallbacks "Dummy"
                pass
            
            # Chequear si este partido terminó/error externamente
            async with state._lock:
                current_match = state._matches.get(self.match_id)
                status_ok = current_match and current_match.status == MatchStatus.EN_CURSO
            
            if not status_ok:
                log.info(f"  [RR-BETANO] Worker {self.worker_id}: Partido finalizado o en error externamente. Liberando slot.")
                break
                
            await self.page.wait_for_timeout(2000)

        log.warning("=" * 60)
        log.info(f"  [RR-BETANO] Worker {self.worker_id}: Cierre de página.")
        try:
            if self.page:
                await self.page.close()
        except Exception:
            pass
        self.page = None

        # Cerrar contexto SOLO si NO es compartido
        if not self._shared_context:
            try:
                await self.browser.close()
            except Exception:
                pass
            self.browser = None

            # Limpiar playwright
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None

            # Limpiar perfil temporal
            try:
                if os.path.exists(self.profile_dir):
                    shutil.rmtree(self.profile_dir, ignore_errors=True)
            except Exception as e:
                log.warning(f"  [RR-BETANO] Worker {self.worker_id}: Error borrando perfil: {e}")

        log.info(f"  [RR-BETANO] Worker {self.worker_id}: ✅ Ciclo completado para {match.home_team} vs {match.away_team}")
        self.match_id = None

    async def shutdown(self):
        """Cierra la pagina y libera recursos. No cierra el contexto compartido."""
        try:
            if self.page:
                try:
                    await self.page.close()
                except Exception:
                    pass
                self.page = None
        except Exception:
            pass

        if not self._shared_context:
            try:
                if self.browser:
                    await self.browser.close()
                    self.browser = None
            except Exception:
                pass
            if self._playwright:
                try:
                    await self._playwright.stop()
                except Exception:
                    pass
                self._playwright = None
        else:
            self.browser = None

        # Limpiar perfil temporal (solo si no es compartido)
        if not self._shared_context:
            try:
                if os.path.exists(self.profile_dir):
                    shutil.rmtree(self.profile_dir, ignore_errors=True)
            except Exception as e:
                log.warning(f"  Worker {self.worker_id}: Error borrando perfil: {e}")

        self.match_id = None
        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None


class Dispatcher:
    """Modulo 3: Gestor de concurrencia con MAX_WORKERS slots."""

    def __init__(self, state: StateMatrix, max_workers: int = MAX_WORKERS, dry_run: bool = False, shared_context=None):
        self.state = state
        self.max_workers = max_workers
        self.dry_run = dry_run
        self.shared_context = shared_context
        self.workers: list[WorkerSlot] = [
            WorkerSlot(i + 1) for i in range(max_workers)
        ]

    def _get_free_worker(self) -> Optional[WorkerSlot]:
        for w in self.workers:
            if w.match_id is None:
                return w
        return None

    def _active_count(self) -> int:
        return sum(1 for w in self.workers if w.match_id is not None)

    async def assign_tick(self):
        """Bucle de Asignacion: asigna partidos pendientes a TODOS los workers libres (paralelo)."""
        active = self._active_count()
        if active >= self.max_workers:
            return  # Todos los slots ocupados

        # Despachar tantos workers como slots libres haya (hasta max_workers)
        dispatched = 0
        while True:
            if self._active_count() >= self.max_workers:
                break

            free_worker = self._get_free_worker()
            if not free_worker:
                break

            match = await self.state.get_pending()
            if not match:
                break

            # GUARD: Sin betano_event_id → no se deberia llegar aqui (get_pending ya filtra)
            if not match.betano_event_id:
                break

            # Asignar
            log.info(
                f"[DISPATCH] Asignando Worker {free_worker.worker_id}: "
                f"{match.home_team} vs {match.away_team} (betano_event_id={match.betano_event_id})"
            )
            free_worker.match_id = match.match_id  # <--- OCUPAR EL SLOT INMEDIATAMENTE
            await self.state.set_status(
                match.match_id, MatchStatus.EN_CURSO, free_worker.worker_id
            )

            # Lanzar en paralelo (no bloquear el ciclo)
            asyncio.create_task(self._launch_worker(free_worker, match))
            dispatched += 1

    async def _launch_worker(self, free_worker, match):
        """Corrutina que lanza un worker y maneja errores."""
        try:
            await free_worker.launch(match, state=self.state, dry_run=self.dry_run, shared_context=self.shared_context)
            # Worker completado: si no terminó ni fue error, liberar el match devolviéndolo a PENDIENTE
            async with self.state._lock:
                current = self.state._matches.get(match.match_id)
                if current and current.status == MatchStatus.EN_CURSO:
                    current.status = MatchStatus.PENDIENTE
        except Exception as e:
            log.error(f"  Worker {free_worker.worker_id} fallo al lanzar: {e}")
            await self.state.set_status(match.match_id, MatchStatus.ERROR)
            await free_worker.shutdown()


    async def release_tick(self):
        """Bucle de Liberacion: cierra workers de partidos terminados."""
        # 1. Partidos que SofaScore reporta como terminados
        ended = await self.state.mark_ended_by_sofascore()
        for entry in ended:
            worker = next(
                (w for w in self.workers if w.match_id == entry.match_id), None
            )
            if worker:
                log.info(
                    f"[RELEASE] Worker {worker.worker_id}: "
                    f"{entry.home_team} vs {entry.away_team} TERMINADO"
                )
                await worker.shutdown()

        # 2. Kill Switch: partidos colgados > 130 minutos
        stale = await self.state.get_stale_workers(KILL_SWITCH_MINUTES)
        for entry in stale:
            worker = next(
                (w for w in self.workers if w.match_id == entry.match_id), None
            )
            if worker:
                log.warning(
                    f"[KILL SWITCH] Worker {worker.worker_id}: "
                    f"{entry.home_team} vs {entry.away_team} lleva >{KILL_SWITCH_MINUTES}min. Forzando cierre."
                )
                await self.state.set_status(entry.match_id, MatchStatus.ERROR)
                await worker.shutdown()

    async def status_report(self, cycle: int):
        """Escribe el estado al archivo de log (INFO) y un heartbeat a consola (WARNING).
        La consola solo muestra 1 linea cada ~5 minutos para no saturar la terminal.
        Regla: print() esta PROHIBIDO en produccion. Todo pasa por logging.
        """
        active = self._active_count()
        summary = await self.state.summary()

        # Siempre al archivo de log (nivel INFO)
        log.info(
            f"[STATUS] Workers: {active}/{self.max_workers} | Matriz: {summary}"
        )

        # A consola solo cada 20 ciclos (~5 minutos a 15s/ciclo) o si hay errores
        has_errors = summary.get("ERROR", 0) > 0
        if cycle % 20 == 0 or has_errors:
            total = sum(summary.values())
            done = summary.get("TERMINADO", 0)
            pending = summary.get("PENDIENTE", 0)
            errors = summary.get("ERROR", 0)
            log.warning(
                f"[HEARTBEAT] 🟢 Workers {active}/{self.max_workers} activos | "
                f"Partidos: {done} terminados, {active} en curso, {pending} pendientes"
                + (f" | ⚠️ {errors} ERRORES" if errors else "")
            )


# =========================================================================
# MAIN LOOP
# =========================================================================
async def main(args):
    # Banner de inicio — WARNING para que aparezca en consola aunque este silenciada
    log.warning("=" * 60)
    log.warning("  ORQUESTADOR: FRANCOTIRADOR DE RESISTENCIA")
    log.warning(f"  Max Workers : {args.max_workers}")
    log.warning(f"  Dry-Run     : {args.dry_run}")
    log.warning(f"  Log archivo : {LOG_FILE}")
    log.warning(f"  Intervalo   : Radar cada {RADAR_INTERVAL_SEC}s, Dispatch cada {DISPATCH_INTERVAL_SEC}s")
    log.warning("  Consola     : silenciada (ver orchestrator.log para detalles)")
    log.warning("=" * 60)
    # Tambien al archivo de log
    log.info("=" * 60)
    log.info("  ORQUESTADOR: FRANCOTIRADOR DE RESISTENCIA")
    log.info(f"  Max Workers: {args.max_workers} | Dry-Run: {args.dry_run}")
    log.info("=" * 60)

    state = StateMatrix()
    dispatcher = Dispatcher(state, max_workers=args.max_workers, dry_run=args.dry_run)

    # Crear directorio de perfiles temporales
    os.makedirs(TEMP_PROFILES_DIR, exist_ok=True)

    from playwright.async_api import async_playwright
    playwright_instance = await async_playwright().start()

    # ── FARO PS3838 DESACTIVADO — Betano es la fuente única de cuotas ──
    faro_context = None  # Placeholder para compatibilidad con el finally block

    # ── CONTEXTO UNICO: Faro Betano + Workers comparten el mismo Chrome ──
    # 1 solo Chrome, 1 sola ventana. La pestaña betano.co/live actúa como ancla.
    # Los workers abren/cierran tabs sin matar el contexto.
    betano_faro_profile = os.path.join(TEMP_PROFILES_DIR, "faro_betano")
    betano_faro_context = await playwright_instance.chromium.launch_persistent_context(
        betano_faro_profile,
        executable_path=CHROME_EXECUTABLE_PATH,
        headless=False,
        args=[
            f"--disable-extensions-except={CHROME_EXTENSION_PATH}",
            f"--load-extension={CHROME_EXTENSION_PATH}",
            "--disable-blink-features=AutomationControlled",
            "--mute-audio",
            "--start-maximized",
        ],
        viewport={"width": 1920, "height": 1080},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        ignore_https_errors=True,
    )
    dispatcher.shared_context = betano_faro_context
    log.warning(f"🔗 [CONTEXTO UNICO] Faro Betano + {args.max_workers} workers comparten 1 solo Chrome.")
    log.warning("🎰 [FARO BETANO] Abriendo Betano en perfil persistente (faro_betano)...")
    try:
        betano_page = betano_faro_context.pages[0] if betano_faro_context.pages else await betano_faro_context.new_page()
        await betano_page.set_extra_http_headers({
            "Accept-Language": "es-CO,es;q=0.9,en;q=0.8",
            "Upgrade-Insecure-Requests": "1",
        })

        # BYPASS NATIVO: Playwright captura el JSON de overview directamente de la red
        # y lo inyecta al backend sin depender de inject.js. Esto burla cualquier
        # limitación de CORS/fetch que la extensión pudiera tener.
        async def _interceptar_overview_nativo(response):
            url = response.url
            if "live/overview" in url and response.status == 200:
                try:
                    ct = response.headers.get("content-type", "")
                    if "json" not in ct:
                        return
                    json_data = await response.json()
                    log.warning(f"🔥 [PLAYWRIGHT] ¡JSON Masivo Atrapado! URL: {url[:120]} | len={len(str(json_data))}")
                    payload = {"url": url, "data": json_data}
                    # Reintentar si el backend aún no está listo
                    for attempt in range(3):
                        try:
                            async with httpx.AsyncClient() as client:
                                res = await client.post(
                                    "http://127.0.0.1:8000/api/betano/ingest",
                                    json=payload,
                                    timeout=30,
                                )
                                log.info(f"  [PLAYWRIGHT] Enviado al Backend. Status: {res.status_code}")
                                break  # Exito
                        except Exception as e:
                            if attempt < 2:
                                log.info(f"  [PLAYWRIGHT] Backend no listo aun (intento {attempt+1}/3). Reintentando en 2s...")
                                await asyncio.sleep(2)
                            else:
                                log.warning(f"  [PLAYWRIGHT] Error en bypass nativo tras 3 intentos: {e}")
                except Exception as e:
                    log.warning(f"  [PLAYWRIGHT] Error leyendo overview: {e}")


        betano_page.on("response", _interceptar_overview_nativo)

        await betano_page.goto(
            "https://www.betano.com/live/",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await betano_page.wait_for_timeout(5000)

        # ── SCROLL PROFUNDO: Forzar carga de los 81+ partidos via lazy loading ──
        # Betano solo carga los primeros 25 "destacados" inicialmente. El resto
        # se carga con scroll. Hacemos scroll INTERNO (dentro del contenedor de
        # partidos) moviendo el mouse al area correcta antes de girar la rueda.
        log.warning("🔄 [FARO BETANO] Posicionando mouse para scroll interno...")
        try:
            await betano_page.mouse.move(400, 400)
            await betano_page.wait_for_timeout(500)

            for i in range(6):
                await betano_page.mouse.wheel(0, 3000)
                log.info(f"  [FARO BETANO] ⬇️ Scroll interno {i+1}/6...")
                await betano_page.wait_for_timeout(1500)

            # Volver arriba
            await betano_page.mouse.wheel(0, -20000)
            await betano_page.wait_for_timeout(1000)
        except Exception as e:
            log.warning(f"  ⚠️ [FARO BETANO] Error en scroll: {e}")
        log.warning("✅ [FARO BETANO] Matriz completa cargada. Interceptando cuotas de todos los partidos.")
    except Exception as e:
        log.error(f"⚠️ [FARO BETANO] Error: {e}")

    # Esperar a que el catálogo del Faro Betano se pueble antes del primer Radar
    # El Faro envía el JSON async al backend — necesitamos ~3s para que llegue
    log.warning("[FARO BETANO] Esperando 5s para que el catalogo se pueble...")
    await asyncio.sleep(5)

    radar_counter = 0
    cycle = 0

    try:
        while True:
            cycle += 1

            # Radar: cada RADAR_INTERVAL_SEC (primera ejecucion inmediata)
            if radar_counter == 0 or radar_counter >= (RADAR_INTERVAL_SEC // DISPATCH_INTERVAL_SEC):
                await radar_tick(state, playwright_instance)
                radar_counter = 0

            radar_counter += 1

            # Despachador: Round-Robin Betano — workers abren URL → 5s → cierran → siguiente
            await dispatcher.assign_tick()
            # release_tick ya no es necesario: los workers se auto-liberan tras 5s
            # pero mantenemos kill-switch por seguridad
            await dispatcher.release_tick()

            # Status report (heartbeat a consola cada 20 ciclos, siempre al archivo)
            await dispatcher.status_report(cycle)

            # Esperar antes del siguiente ciclo
            await asyncio.sleep(DISPATCH_INTERVAL_SEC)

    except KeyboardInterrupt:
        log.warning("\n[SHUTDOWN] ⛔ CTRL+C recibido. Cerrando workers activos...")
        log.info("[SHUTDOWN] Iniciando cierre limpio...")
        for w in dispatcher.workers:
            if w.match_id:
                log.warning(f"  Cerrando Worker {w.worker_id}...")
                await w.shutdown()
        log.warning("[SHUTDOWN] ✅ Todos los workers cerrados. Hasta pronto.")
        log.info("[SHUTDOWN] Orquestador detenido de forma limpia.")
    
    finally:
        # Cerrar Faro Betano (contexto unico que usan los workers)
        try:
            await betano_faro_context.close()
            log.info("[CLEANUP] Contexto unico (Faro Betano + Workers) cerrado.")
        except Exception:
            pass
        # Detener playwright global
        try:
            await playwright_instance.stop()
        except Exception:
            pass
        log.info("[CLEANUP] Orquestador finalizado. Manteniendo perfiles en disco para persistencia PS3838.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Orquestador - Francotirador de Resistencia")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Solo detectar partidos sin abrir Chrome"
    )
    parser.add_argument(
        "--max-workers", type=int, default=MAX_WORKERS,
        help=f"Maximo de instancias simultaneas (default: {MAX_WORKERS})"
    )
    args = parser.parse_args()
    asyncio.run(main(args))
