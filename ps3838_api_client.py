"""
ps3838_api_client.py — Cliente oficial de la API REST de PS3838/Pinnacle
=========================================================================
Implementación hardened con cumplimiento estricto del fair-use de PS3838.

REGLAS DE FAIR-USE DE PS3838 (documentación oficial):
  1. Delta calls (con `since`):   mínimo 5 segundos entre llamadas por deporte
  2. Snapshot calls (sin `since`): máximo 1 vez por minuto (el servidor lo cachea 60s)
  3. La API es para uso PROPORCIONAL a la actividad apostadora — no pedir más
     de lo que se necesita para tomar decisiones de apuesta
  4. Máximo ~30 eventIds por request (límite implícito de la URL)
  5. En caso de 429 (Rate Limited): backoff exponencial obligatorio

CICLO DE POLLING IMPLEMENTADO:
  t=0s  → /v3/odds (goles)
  t=6s  → /v3/odds/special (corners + tarjetas)
  t=12s → /v3/odds (siguiente ciclo con `since` → delta)
  ...

  Ciclo total: 12s (~5 requests/min por endpoint)
  Muy por debajo del límite de la API.

CREDENCIALES: Leer desde .env (nunca hardcodear)
"""

import base64
import json
import logging
import os
import random
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Optional

# ─────────────────────────────────────────────────────────────────────────────
# Configuración de logging
# ─────────────────────────────────────────────────────────────────────────────
log = logging.getLogger("PS3838-API")
if not log.handlers:
    _h = logging.StreamHandler()
    _h.setFormatter(logging.Formatter("[PS3838-API] %(levelname)s %(message)s"))
    log.addHandler(_h)
log.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────────────────────────
# Constantes de fair-use
# ─────────────────────────────────────────────────────────────────────────────
PS3838_API_BASE        = "https://api.ps3838.com"
FOOTBALL_SPORT_ID      = 29

# Versiones de endpoints activos según la spec oficial (ps3838api.github.io/docs)
# /v3/odds        → deprecated:true  → reemplazado por /v4/odds
# /v3/odds/special → no existía      → el correcto es /v2/odds/special
MAIN_ODDS_ENDPOINT     = "/v4/odds"          # Get Straight Odds - v4 (activo)
SPECIAL_ODDS_ENDPOINT  = "/v2/odds/special"  # Get Special Odds - v2 (deprecated:false)

# Intervalos mínimos (fair-use oficial: 5s por sport para delta calls)
MAIN_POLL_INTERVAL_S   = 12    # /v3/odds — cada 12s (margen amplio sobre los 5s mínimos)
SPECIAL_POLL_INTERVAL_S = 12   # /v3/odds/special — idem, desfasado 6s del main
STAGGER_S              = 6     # tiempo de desfase entre los dos endpoints
JITTER_MAX_S           = 1.5   # jitter aleatorio máximo por petición

# Backoff en caso de error
BACKOFF_INITIAL_S      = 10    # primer backoff tras error
BACKOFF_MAX_S          = 120   # backoff máximo (ej: 429)
BACKOFF_MULTIPLIER     = 2.0   # factor multiplicador de backoff exponencial

# Límites operativos
MAX_EVENT_IDS_PER_REQ  = 20    # PS3838 puede fallar con URLs muy largas
REQUEST_TIMEOUT_S      = 8

# Palabras clave para clasificar mercados (ES + EN)
_CORNER_KW  = ("corner", "córner", "esquina")
_CARD_KW    = ("card", "tarjeta", "booking", "yellow", "amarilla")
_GOAL_KW    = ("goal", "gol")
_HALF_KW    = ("half", "mitad", "1st", "2nd", "primera", "segunda", "1h", "2h")


def _load_env_credentials() -> tuple[str, str]:
    """
    Carga credenciales desde el archivo .env del proyecto.
    Fallback a variables de entorno del sistema.
    """
    env_path = Path(__file__).parent / ".env"
    user = os.environ.get("PS3838_USER", "")
    pw   = os.environ.get("PS3838_PASS", "")

    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            if key == "PS3838_USER" and not user:
                user = val
            elif key == "PS3838_PASS" and not pw:
                pw = val

    return user, pw


class _RateLimiter:
    """
    Controla que no se excedan los intervalos mínimos entre peticiones.
    Thread-safe.
    """
    def __init__(self, min_interval_s: float):
        self._min = min_interval_s
        self._last_call = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        """Espera lo necesario para cumplir el intervalo mínimo + jitter."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_call
            wait_time = max(0.0, self._min - elapsed)
            # Añadir jitter para evitar patrones exactamente rítmicos
            jitter = random.uniform(0.0, JITTER_MAX_S)
            total_wait = wait_time + jitter
            if total_wait > 0:
                time.sleep(total_wait)
            self._last_call = time.monotonic()


class PS3838ApiClient:
    """
    Cliente HTTP-only para la API oficial de PS3838.

    Usa delta calls (parámetro `since`) para minimizar carga.
    Implementa backoff exponencial, rate limiting estricto y logging completo.

    Uso:
        client = PS3838ApiClient(on_update=scraper_service._update_match_overrides)
        client.watch(match_url, pinnacle_event_id=1452356789)
        client.start()
    """

    def __init__(self, on_update: Callable[[str, str, list], None],
                 username: str = "", password: str = "",
                 on_alert: Optional[Callable[[str], None]] = None):
        # Credenciales: argumento > .env > env vars
        if not username or not password:
            username, password = _load_env_credentials()

        if not username or not password:
            raise ValueError(
                "PS3838ApiClient requiere credenciales. "
                "Define PS3838_USER y PS3838_PASS en el archivo .env"
            )

        self._auth_header = (
            "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()
        )
        self._on_update = on_update
        self._on_alert  = on_alert   # callback opcional: (mensaje) -> None
        self._username  = username   # solo para logging (nunca el password)

        # Estado de partidos vigilados
        self._watched: dict[str, int] = {}  # match_url → pinnacle_event_id
        self._lock = threading.Lock()

        # Tokens delta — la API retorna solo los cambios desde este punto
        self._since_main:    Optional[int] = None
        self._since_special: Optional[int] = None

        # Rate limiters independientes por endpoint
        self._rl_main    = _RateLimiter(MAIN_POLL_INTERVAL_S)
        self._rl_special = _RateLimiter(SPECIAL_POLL_INTERVAL_S)

        # Control de backoff
        self._backoff_main    = BACKOFF_INITIAL_S
        self._backoff_special = BACKOFF_INITIAL_S

        # Contador de 401s consecutivos (3 seguidos = detener definitivamente)
        self._consecutive_401s: int = 0
        _MAX_CONSECUTIVE_401S   = 3

        # ⚠️ Bandera pública de sospecha de lista gris
        # Se activa cuando el backoff llega a BACKOFF_MAX_S.
        # Visible en GET /api/ps3838/status.
        self.greylist_suspected: bool = False

        # Estado del hilo
        self._running = False
        self._thread_main:    Optional[threading.Thread] = None
        self._thread_special: Optional[threading.Thread] = None

        log.info(f"Inicializado para usuario {self._username}")

    # ── Gestión de partidos vigilados ────────────────────────────────────────

    def watch(self, match_url: str, pinnacle_event_id: int) -> None:
        """Empieza a monitorear un partido vía API."""
        with self._lock:
            if match_url not in self._watched:
                self._watched[match_url] = pinnacle_event_id
                log.info(f"Vigilando evento {pinnacle_event_id} → {match_url[-50:]}")
                # Resetear `since` para forzar snapshot en el próximo ciclo
                # (el server devolverá todos los datos del evento nuevo)
                self._since_main    = None
                self._since_special = None

    def unwatch(self, match_url: str) -> None:
        """Deja de monitorear un partido."""
        with self._lock:
            if match_url in self._watched:
                eid = self._watched.pop(match_url)
                log.info(f"Detenido seguimiento de evento {eid}")

    def is_watching(self, match_url: str) -> bool:
        with self._lock:
            return match_url in self._watched

    def watched_count(self) -> int:
        with self._lock:
            return len(self._watched)

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True

        # Dos hilos independientes — uno para cada endpoint, desfasados STAGGER_S
        self._thread_main = threading.Thread(
            target=self._poll_loop_main,
            daemon=True,
            name="PS3838-main-odds",
        )
        self._thread_special = threading.Thread(
            target=self._poll_loop_special,
            daemon=True,
            name="PS3838-special-odds",
        )
        self._thread_main.start()
        # El hilo de special arranca desfasado para no coincidir con main
        threading.Timer(STAGGER_S, self._thread_special.start).start()

        log.info("Iniciado — dos hilos independientes (main + special), fair-use compliant")

    def stop(self) -> None:
        self._running = False
        log.info("Detenido")

    # ── HTTP Helper ───────────────────────────────────────────────────────────

    def _get(self, endpoint: str) -> dict:
        """
        Realiza una petición GET autenticada.
        Lanza urllib.error.HTTPError en caso de error HTTP.
        """
        url = f"{PS3838_API_BASE}{endpoint}"
        req = urllib.request.Request(
            url,
            headers={
                "Authorization": self._auth_header,
                "Accept":        "application/json",
                "Content-Type":  "application/json",
                # User-Agent real — evita ser identificado como bot genérico
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            },
        )
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))

    # ── Hilos de polling ──────────────────────────────────────────────────────

    def _poll_loop_main(self) -> None:
        """Hilo dedicado a /v3/odds (GOLES — period 0)."""
        log.info("Hilo main-odds iniciado")
        while self._running:
            self._rl_main.wait()  # Respeta el intervalo mínimo + jitter
            try:
                with self._lock:
                    watched = dict(self._watched)

                if not watched:
                    continue  # Sin partidos → simplemente esperar

                self._call_main_odds(watched)
                self._backoff_main = BACKOFF_INITIAL_S  # reset backoff en éxito

            except urllib.error.HTTPError as e:
                self._handle_http_error(e, "main-odds")
            except urllib.error.URLError as e:
                log.warning(f"Error de red main-odds: {e.reason}")
                time.sleep(self._backoff_main)
            except Exception as exc:
                log.error(f"Error inesperado main-odds: {exc}")
                time.sleep(self._backoff_main)

    def _poll_loop_special(self) -> None:
        """Hilo dedicado a /v3/odds/special (CORNERS + TARJETAS)."""
        log.info("Hilo special-odds iniciado")
        while self._running:
            self._rl_special.wait()
            try:
                with self._lock:
                    watched = dict(self._watched)

                if not watched:
                    continue

                self._call_special_odds(watched)
                self._backoff_special = BACKOFF_INITIAL_S

            except urllib.error.HTTPError as e:
                self._handle_http_error(e, "special-odds", is_special=True)
            except urllib.error.URLError as e:
                log.warning(f"Error de red special-odds: {e.reason}")
                time.sleep(self._backoff_special)
            except Exception as exc:
                log.error(f"Error inesperado special-odds: {exc}")
                time.sleep(self._backoff_special)

    def _fire_red_alert(self, reason: str) -> None:
        """
        ALERTA ROJA: el backoff llegó a su máximo (120s).
        Esto indica que PS3838 está respondiendo con errores persistentes,
        lo que puede significar que la IP está en lista gris o que la cuenta
        fue flaggeada. Se debe pausar manualmente y esperar.

        Acciones:
          1. Log CRITICAL en consola (visible en los logs del servidor)
          2. Activa self.greylist_suspected = True (visible en /api/ps3838/status)
          3. Llama al callback on_alert si fue provisto (lo recibe server.py para
             pushearlo al event_log del frontend)
        """
        self.greylist_suspected = True

        msg = (
            f"[ALERTA ROJA] Backoff PS3838 en maximo ({BACKOFF_MAX_S}s). "
            f"Causa: {reason}. "
            f"IP posiblemente en lista gris. "
            f"ACCION REQUERIDA: Pausa manual el servidor y espera al menos 10 minutos "
            f"antes de reiniciar. Si persiste, contacta soporte de PS3838."
        )
        # 1. Log critico en consola
        log.critical(msg)

        # 2. Callback al servidor (para event_log del frontend)
        if self._on_alert:
            try:
                self._on_alert(msg)
            except Exception:
                pass

    def _handle_http_error(self, e: urllib.error.HTTPError,
                           label: str, is_special: bool = False) -> None:
        """
        Maneja errores HTTP con backoff exponencial.
          - 304 Not Modified  → éxito silencioso (delta call normal)
          - 401 Unauthorized  → reintenta hasta 3 veces (60s entre intentos).
                               Tras 3 fallos consecutivos: alerta + detiene polling.
          - 429 Rate Limited  → backoff agresivo (×4), ALERTA ROJA si llega a 120s
          - 5xx Server Error  → backoff normal (×2), ALERTA ROJA si llega a 120s
        """
        if e.code == 304:
            return  # Sin cambios desde el último `since` — normal

        if e.code == 401:
            _MAX = 3
            self._consecutive_401s += 1
            log.error(
                f"401 Unauthorized en {label} — intento {self._consecutive_401s}/{_MAX}. "
                f"Esperando 60s antes de reintentar..."
            )
            if self._consecutive_401s >= _MAX:
                msg = (
                    f"CRITICO: {_MAX} errores 401 consecutivos en PS3838 API. "
                    f"CAUSA PROBABLE: Las credenciales WEB de ps3838.com NO son "
                    f"credenciales de API. Son distintas. "
                    f"SOLUCION: Ingresa a ps3838.com > Mi Cuenta > Acceso a API, "
                    f"activa el acceso y usa la contrasena de API (no la del login web) "
                    f"en el campo PS3838_PASS del archivo .env. "
                    f"El sistema continua con Betano como fuente de cuotas."
                )
                log.critical(msg)
                if self._on_alert:
                    try:
                        self._on_alert(msg)
                    except Exception:
                        pass
                self._running = False
            else:
                time.sleep(60)  # Espera antes del reintento
            return

        # Cualquier otra respuesta resetea el contador de 401s
        self._consecutive_401s = 0

        if e.code == 429:
            # Rate Limited — backoff agresivo (x4 = doble que los errores de servidor)
            if is_special:
                new_backoff = min(self._backoff_special * BACKOFF_MULTIPLIER * 2, BACKOFF_MAX_S)
                hit_max = (new_backoff >= BACKOFF_MAX_S and self._backoff_special < BACKOFF_MAX_S)
                self._backoff_special = new_backoff
                wait = self._backoff_special
            else:
                new_backoff = min(self._backoff_main * BACKOFF_MULTIPLIER * 2, BACKOFF_MAX_S)
                hit_max = (new_backoff >= BACKOFF_MAX_S and self._backoff_main < BACKOFF_MAX_S)
                self._backoff_main = new_backoff
                wait = self._backoff_main

            log.warning(f"429 Rate Limited en {label} — backoff {wait:.0f}s")

            if hit_max:
                self._fire_red_alert(f"429 Rate Limited persistente en {label}")

            time.sleep(wait)
            return

        if e.code in (500, 502, 503, 504):
            # Error de servidor temporal — backoff normal (x2)
            if is_special:
                new_backoff = min(self._backoff_special * BACKOFF_MULTIPLIER, BACKOFF_MAX_S)
                hit_max = (new_backoff >= BACKOFF_MAX_S and self._backoff_special < BACKOFF_MAX_S)
                self._backoff_special = new_backoff
                wait = self._backoff_special
            else:
                new_backoff = min(self._backoff_main * BACKOFF_MULTIPLIER, BACKOFF_MAX_S)
                hit_max = (new_backoff >= BACKOFF_MAX_S and self._backoff_main < BACKOFF_MAX_S)
                self._backoff_main = new_backoff
                wait = self._backoff_main

            log.warning(f"HTTP {e.code} en {label} — backoff {wait:.0f}s")

            if hit_max:
                self._fire_red_alert(f"HTTP {e.code} persistente en {label}")

            time.sleep(wait)
            return

        log.error(f"HTTP {e.code} no manejado en {label}: {e.reason}")

    # ── Llamadas a la API ─────────────────────────────────────────────────────

    def _call_main_odds(self, watched: dict) -> None:
        """
        GET /v4/odds — Full-match totals (GOLES, period 0).
        Usa delta call con `since` para mínima carga.

        Nota: eventIds usa collectionFormat:multi → parámetros repetidos en la URL,
        NO separados por coma. Ej: ?eventIds=111&eventIds=222
        """
        event_chunks = self._chunk_events(watched, MAX_EVENT_IDS_PER_REQ)

        for chunk in event_chunks:
            # collectionFormat: multi → ?eventIds=X&eventIds=Y&...
            event_params = "&".join(f"eventIds={v}" for v in chunk.values())
            since_param  = f"&since={self._since_main}" if self._since_main else ""

            endpoint = (
                f"{MAIN_ODDS_ENDPOINT}"
                f"?sportId={FOOTBALL_SPORT_ID}"
                f"&{event_params}"
                f"&oddsFormat=Decimal"
                f"&isLive=1"
                f"{since_param}"
            )

            data = self._get(endpoint)
            new_since = data.get("last")
            if new_since:
                self._since_main = new_since
                log.debug(f"main-odds since actualizado a {new_since}")

            self._process_main_odds(data, chunk)

    def _call_special_odds(self, watched: dict) -> None:
        """
        GET /v2/odds/special — Corners + Tarjetas.
        Usa delta call con `since` para mínima carga.

        Nota: /v3/odds/special no existía en la spec oficial.
        El endpoint correcto es /v2/odds/special (deprecated:false).
        eventIds también usa collectionFormat:multi aquí.
        """
        event_chunks = self._chunk_events(watched, MAX_EVENT_IDS_PER_REQ)

        for chunk in event_chunks:
            event_params = "&".join(f"eventIds={v}" for v in chunk.values())
            since_param  = f"&since={self._since_special}" if self._since_special else ""

            endpoint = (
                f"{SPECIAL_ODDS_ENDPOINT}"
                f"?sportId={FOOTBALL_SPORT_ID}"
                f"&{event_params}"
                f"&oddsFormat=Decimal"
                f"{since_param}"
            )

            data = self._get(endpoint)
            new_since = data.get("last")
            if new_since:
                self._since_special = new_since

            self._process_special_odds(data, chunk)

    # ── Parsers ───────────────────────────────────────────────────────────────

    @staticmethod
    def _chunk_events(watched: dict, size: int) -> list[dict]:
        """Divide el dict de eventos en chunks de `size` para no exceder límites de URL."""
        items = list(watched.items())
        return [dict(items[i:i+size]) for i in range(0, len(items), size)]

    def _find_match_url(self, event_id: int, watched: dict) -> Optional[str]:
        for url, eid in watched.items():
            if eid == event_id:
                return url
        return None

    def _process_main_odds(self, data: dict, watched: dict) -> None:
        """
        Procesa /v3/odds → GOLES (period 0, totals).
        Estructura: { leagues: [{ events: [{ periods: [{ totals: [...] }] }] }] }
        """
        for league in data.get("leagues", []):
            for event in league.get("events", []):
                event_id  = event.get("id")
                match_url = self._find_match_url(event_id, watched)
                if not match_url:
                    continue

                for period in event.get("periods", []):
                    if period.get("number") != 0:
                        continue  # Solo Full Match (period 0)
                    if period.get("status") not in (1, 2):
                        continue  # 1=Open, 2=Suspended

                    goal_lines = []
                    for t in period.get("totals", []):
                        pts = float(t.get("points") or 0)
                        ov  = float(t.get("over")   or 0)
                        un  = float(t.get("under")  or 0)
                        if ov > 1.0 and un > 1.0 and pts > 0:
                            goal_lines.append({"linea": pts, "over": ov, "under": un})

                    if goal_lines:
                        log.debug(f"GOLES: {len(goal_lines)} líneas → {match_url[-40:]}")
                        self._on_update(match_url, "GOLES", goal_lines)

    def _process_special_odds(self, data: dict, watched: dict) -> None:
        """
        Procesa /v3/odds/special → CORNERS + TARJETAS.
        Soporta dos formatos de la API (contestants[] y lines[]/totals[]).
        """
        for league in data.get("leagues", []):
            for event in league.get("events", []):
                event_id  = event.get("id")
                match_url = self._find_match_url(event_id, watched)
                if not match_url:
                    continue

                for special in event.get("specials", []):
                    raw_name = (special.get("name") or "").lower()

                    if any(k in raw_name for k in _HALF_KW):
                        continue  # Excluir mitades

                    if any(k in raw_name for k in _CORNER_KW):
                        market = "CORNERS"
                    elif any(k in raw_name for k in _CARD_KW):
                        market = "TARJETAS"
                    elif any(k in raw_name for k in _GOAL_KW):
                        market = "GOLES"
                    else:
                        continue

                    lines = self._extract_special_lines(special)
                    if lines:
                        log.debug(f"{market}: {len(lines)} líneas (special) → {match_url[-40:]}")
                        self._on_update(match_url, market, lines)

    @staticmethod
    def _extract_special_lines(special: dict) -> list:
        """
        Extrae pares (linea, over, under) de un mercado especial.
        Soporta formato A (contestants) y formato B (lines/totals).
        """
        lines = []

        # ── Formato A: contestants[{name: "Over 9.5", price: 1.92}, ...] ──
        contestants = special.get("contestants", [])
        if len(contestants) >= 2:
            over_price = under_price = linea = 0.0
            for c in contestants:
                name_c = (c.get("name") or "").lower()
                price  = float(c.get("price") or 0)
                m = re.search(r"(\d+(?:\.\d+)?)", name_c)
                if m:
                    linea = float(m.group(1))
                if "over" in name_c or "más" in name_c:
                    over_price = price
                elif "under" in name_c or "menos" in name_c:
                    under_price = price
            if over_price > 1.0 and under_price > 1.0 and linea > 0:
                lines.append({"linea": linea, "over": over_price, "under": under_price})
            if lines:
                return lines

        # ── Formato B: lines[] o totals[{handicap/points, over, under}] ──
        for t in special.get("lines", special.get("totals", [])):
            pts = float(t.get("handicap") or t.get("points") or 0)
            ov  = float(t.get("over")  or 0)
            un  = float(t.get("under") or 0)
            if ov > 1.0 and un > 1.0 and pts > 0:
                lines.append({"linea": pts, "over": ov, "under": un})

        return lines
