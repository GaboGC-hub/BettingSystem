"""
scraper_logger.py
=================
Logger dedicado para auditar los datos que reciben los scrapers en tiempo real.
Genera un archivo de log rotativo en `logs/scrapers/` con entradas estructuradas
por fuente: sofascore, betano, betplay, pinnacle/ps3838.

Uso:
    from scraper_logger import scraper_log
    scraper_log.odds("pinnacle", "GOLES", lines, match_url)
    scraper_log.stats("sofascore", stats_dict, match_url)
    scraper_log.event("betano", "SUSPENDED", {"market": "CORNERS"}, match_url)
"""

import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Forzar UTF-8 en consola Windows para que los caracteres especiales no fallen
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

# ── Configuración ────────────────────────────────────────────────────────────
LOGS_DIR = Path(__file__).parent / "logs" / "scrapers"
MAX_LINES_PER_FILE = 5_000   # Rota el archivo al superar este límite
EMIT_CONSOLE = True          # También imprime un resumen en la consola

_lock = threading.Lock()
_line_counts: dict[str, int] = {}


# ── Colores ANSI para consola ─────────────────────────────────────────────────
class C:
    RESET  = "\033[0m"
    BOLD   = "\033[1m"
    RED    = "\033[91m"
    GREEN  = "\033[92m"
    YELLOW = "\033[93m"
    BLUE   = "\033[94m"
    CYAN   = "\033[96m"
    GRAY   = "\033[90m"
    GOLD   = "\033[33m"

SOURCE_COLOR = {
    "sofascore": C.BLUE,
    "betano":    C.YELLOW,
    "betplay":   C.GREEN,
    "pinnacle":  C.GOLD,
    "ps3838":    C.GOLD,
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _local_ts() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _get_log_path(source: str) -> Path:
    """Devuelve la ruta del log actual para una fuente, rotando si es necesario."""
    LOGS_DIR.mkdir(parents=True, exist_ok=True)
    today = datetime.now().strftime("%Y-%m-%d")
    path = LOGS_DIR / f"{source}_{today}.jsonl"
    return path


def _write(source: str, entry: dict) -> None:
    """Escribe una entrada JSON en el archivo de log correspondiente."""
    path = _get_log_path(source)
    entry["_ts"] = _now_iso()
    entry["source"] = source
    line = json.dumps(entry, ensure_ascii=False)

    with _lock:
        count = _line_counts.get(str(path), 0)
        if count >= MAX_LINES_PER_FILE:
            # Rotar: renombrar el archivo actual
            rot = path.with_suffix(f".{datetime.now().strftime('%H%M%S')}.jsonl")
            try:
                path.rename(rot)
            except Exception:
                pass
            _line_counts[str(path)] = 0

        with open(path, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _line_counts[str(path)] = _line_counts.get(str(path), 0) + 1


def _match_label(match_url: str | None) -> str:
    if not match_url:
        return "?"
    # Intentar extraer nombre del partido de la URL de SofaScore
    try:
        slug = match_url.split("/match/")[-1].split("/")[0]
        return slug[:45]
    except Exception:
        return match_url[-45:]


def _console(source: str, msg: str, level: str = "INFO") -> None:
    if not EMIT_CONSOLE:
        return
    color = SOURCE_COLOR.get(source.lower(), C.GRAY)
    ts    = _local_ts()
    lvl   = {"OK": C.GREEN, "WARN": C.YELLOW, "ERR": C.RED, "INFO": C.GRAY}.get(level, C.GRAY)
    print(f"{C.GRAY}[{ts}]{C.RESET} {color}{C.BOLD}[{source.upper():<9}]{C.RESET} {lvl}{msg}{C.RESET}")


# ── API Pública ────────────────────────────────────────────────────────────────

class ScraperLogger:
    """Punto de entrada único para el logging de scrapers."""

    def odds(
        self,
        source: str,
        market: str,
        lines: list[dict],
        match_url: str | None = None,
        suspended: bool = False,
    ) -> None:
        """Registra cuotas recibidas de un scraper (Betano, Betplay, Pinnacle)."""
        label = _match_label(match_url)
        if suspended:
            entry = {
                "event": "SUSPENDED",
                "market": market,
                "match": label,
            }
            _write(source, entry)
            _console(source, f"[SUSP] {market:<10} | {label}", "WARN")
            return

        best = lines[0] if lines else {}
        summary = (
            f"{market:<10} | linea={best.get('linea','?')} "
            f"over={best.get('over','?')} under={best.get('under','?')} "
            f"({len(lines)} lineas) | {label}"
        )
        entry = {
            "event":  "ODDS",
            "market": market,
            "lines":  lines,
            "match":  label,
        }
        _write(source, entry)
        _console(source, f"[ODDS] {summary}", "OK")

    def stats(
        self,
        source: str,
        stats: dict[str, Any],
        match_url: str | None = None,
    ) -> None:
        """Registra un paquete de estadísticas (SofaScore / extensión DOM)."""
        label = _match_label(match_url)
        minute = stats.get("minute") or stats.get("minuto") or "?"
        goals  = f"{stats.get('goals_home', stats.get('goles_local', '?'))}-{stats.get('goals_away', stats.get('goles_visitante', '?'))}"
        corners = stats.get("corners_total", stats.get("corners", "?"))
        cards   = stats.get("yellows_total", stats.get("amarillas", "?"))

        entry = {
            "event":   "STATS",
            "match":   label,
            "minute":  minute,
            "score":   goals,
            "corners": corners,
            "cards":   cards,
            "raw":     {k: v for k, v in stats.items() if not isinstance(v, dict)},
        }
        _write(source, entry)
        _console(
            source,
            f"[STATS] min={minute:<5} score={goals}  corners={corners}  cards={cards} | {label}",
            "INFO",
        )

    def event(
        self,
        source: str,
        event_type: str,
        data: dict | None = None,
        match_url: str | None = None,
        level: str = "INFO",
    ) -> None:
        """Registra un evento genérico (conexión, desconexión, heartbeat, error)."""
        label = _match_label(match_url)
        entry = {
            "event": event_type,
            "match": label,
            **(data or {}),
        }
        _write(source, entry)
        icons = {
            "CONNECT": "[CONN]", "DISCONNECT": "[DISC]", "HEARTBEAT": "[HB]",
            "ERROR": "[ERR]", "AUTO_REGISTER": "[NEW]", "WARN": "[WARN]",
        }
        icon = icons.get(event_type.upper(), "[??]")
        _console(source, f"{icon} {event_type}  {json.dumps(data or {})[:80]} │ {label}", level)

    def connection(self, source: str, connected: bool, detail: str = "") -> None:
        """Shortcut para eventos de conexión/desconexión."""
        ev = "CONNECT" if connected else "DISCONNECT"
        lvl = "OK" if connected else "ERR"
        self.event(source, ev, {"detail": detail}, level=lvl)

    def heartbeat(self, source: str, match_url: str | None = None) -> None:
        """Heartbeat/keepalive — solo escribe al archivo, no a consola."""
        entry = {"event": "HEARTBEAT", "match": _match_label(match_url)}
        _write(source, entry)

    def snapshot_rejected(self, source: str, reason: str, match_url: str | None = None) -> None:
        """Registra cuando un snapshot es rechazado por filtros de sanidad."""
        label = _match_label(match_url)
        entry = {"event": "REJECTED", "reason": reason, "match": label}
        _write(source, entry)
        _console(source, f"🚫 RECHAZADO: {reason} │ {label}", "WARN")


# ── Instancia global ───────────────────────────────────────────────────────────
scraper_log = ScraperLogger()
