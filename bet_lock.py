"""
bet_lock.py — Persistent Bet State Lock
=========================================
Resuelve el problema del "Sniper Lock de Software":
  - Las apuestas activas se persisten en active_bets.json
  - Sobreviven reinicios del script
  - El lock se identifica por (match_url + market + linea + side)
  - Una vez colocada una apuesta, el bot NO puede repetirla hasta que:
      a) Se resuelva manualmente desde el dashboard, o
      b) Pasen auto_expire_minutes (configurable, por defecto 90 min)

Integración:
  - server.py consulta is_locked() ANTES de devolver señales activas
  - El frontend recibe lock_id y locked_at en la decisión y muestra el estado
  - POST /api/bets/lock  → place_lock()
  - DELETE /api/bets/lock/{lock_id} → release_lock()
  - GET /api/bets/locks  → get_all_locks()
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

LOCK_FILE = Path(os.path.dirname(__file__)) / "active_bets.json"
AUTO_EXPIRE_MINUTES = 90   # tiempo máximo de lock (puede sobreescribirse por apuesta)
_lock = threading.Lock()   # thread-safety para lecturas/escrituras concurrentes


# ─── Estructura de un lock ────────────────────────────────────────────────────
# {
#   "lock_id":    str   (uuid4),
#   "match_url":  str   (URL de SofaScore del partido),
#   "market":     str   ("Goles" | "Corners" | "Tarjetas"),
#   "linea":      float (ej. 2.5),
#   "side":       str   ("OVER" | "UNDER"),
#   "stake_usd":  float (monto apostado en USD),
#   "odds":       float (cuota a la que se apostó),
#   "source":     str   ("pinnacle" | "betano" | "betplay" | "manual"),
#   "locked_at":  float (epoch),
#   "expires_at": float (epoch, locked_at + auto_expire_minutes*60),
#   "note":       str   (opcional),
# }


def _load() -> dict[str, dict]:
    """Carga el archivo de locks. Devuelve dict vacío si no existe o está corrupto."""
    if not LOCK_FILE.exists():
        return {}
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save(locks: dict[str, dict]) -> None:
    """Persiste el dict de locks en disco de forma atómica."""
    tmp = LOCK_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(locks, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LOCK_FILE)   # atómico en Windows y Unix


# ─── API pública ──────────────────────────────────────────────────────────────

def place_lock(
    match_url: str,
    market: str,
    linea: float,
    side: str,
    stake_usd: float = 0.0,
    odds: float = 0.0,
    source: str = "manual",
    note: str = "",
    auto_expire_minutes: int = AUTO_EXPIRE_MINUTES,
) -> str:
    """
    Registra una apuesta activa. Devuelve el lock_id.
    Si ya existe un lock idéntico (mismo match+market+linea+side), devuelve el existente.
    """
    with _lock:
        locks = _load()

        # Verificar si ya existe un lock activo para esta combinación
        existing = _find_active(locks, match_url, market, linea, side)
        if existing:
            return existing["lock_id"]

        now = time.time()
        lock_id = str(uuid.uuid4())[:8]   # corto para que sea legible en logs
        locks[lock_id] = {
            "lock_id":    lock_id,
            "match_url":  match_url,
            "market":     market,
            "linea":      float(linea),
            "side":       side.upper(),
            "stake_usd":  float(stake_usd),
            "odds":       float(odds),
            "source":     source,
            "locked_at":  now,
            "expires_at": now + auto_expire_minutes * 60,
            "note":       note,
        }
        _save(locks)
        print(f"[BET LOCK] 🔒 {market} {side} {linea} → lock_id={lock_id} ({match_url[-40:]})")
        return lock_id


def release_lock(lock_id: str) -> bool:
    """
    Libera un lock por ID.
    Devuelve True si existía, False si no se encontró.
    """
    with _lock:
        locks = _load()
        if lock_id in locks:
            entry = locks.pop(lock_id)
            _save(locks)
            print(
                f"[BET LOCK] 🔓 Liberado {entry['market']} {entry['side']} "
                f"{entry['linea']} → lock_id={lock_id}"
            )
            return True
        return False


def release_all_for_match(match_url: str) -> int:
    """Libera todos los locks de un partido. Útil cuando el partido termina."""
    with _lock:
        locks = _load()
        to_remove = [lid for lid, l in locks.items() if l["match_url"] == match_url]
        for lid in to_remove:
            locks.pop(lid)
        if to_remove:
            _save(locks)
            print(f"[BET LOCK] 🔓 {len(to_remove)} locks liberados para {match_url[-40:]}")
        return len(to_remove)


def is_locked(match_url: str, market: str, linea: float, side: Optional[str] = None) -> Optional[dict]:
    """
    Comprueba si hay un lock activo para esta combinación.
    Devuelve el dict del lock si existe (y no ha expirado), None si está libre.

    Si side=None, bloquea para CUALQUIER side (over o under) en esa línea.
    """
    with _lock:
        locks = _load()
        return _find_active(locks, match_url, market, linea, side)


def get_all_locks(match_url: Optional[str] = None) -> list[dict]:
    """
    Devuelve todos los locks activos, opcionalmente filtrados por partido.
    Limpia automáticamente los expirados.
    """
    with _lock:
        locks = _load()
        now = time.time()

        # Limpiar expirados
        expired = [lid for lid, l in locks.items() if l["expires_at"] < now]
        if expired:
            for lid in expired:
                locks.pop(lid)
            _save(locks)

        active = list(locks.values())
        if match_url:
            active = [l for l in active if l["match_url"] == match_url]

        # Añadir campo "locked_ago_s" para el frontend
        for l in active:
            l["locked_ago_s"] = int(now - l["locked_at"])
            l["expires_in_s"] = max(0, int(l["expires_at"] - now))

        return sorted(active, key=lambda x: x["locked_at"], reverse=True)


def cleanup_expired() -> int:
    """Elimina locks expirados. Llamar periódicamente desde el background loop."""
    with _lock:
        locks = _load()
        now = time.time()
        expired = [lid for lid, l in locks.items() if l["expires_at"] < now]
        if expired:
            for lid in expired:
                l = locks.pop(lid)
                print(f"[BET LOCK] ⏰ Expirado: {l['market']} {l['side']} {l['linea']}")
            _save(locks)
        return len(expired)


# ─── Utilidades internas ─────────────────────────────────────────────────────

def _find_active(
    locks: dict,
    match_url: str,
    market: str,
    linea: float,
    side: Optional[str] = None,
) -> Optional[dict]:
    """Busca un lock activo (no expirado) para la combinación dada."""
    now = time.time()
    for l in locks.values():
        if l["expires_at"] < now:
            continue  # ignorar expirados
        if l["match_url"] != match_url:
            continue
        if l["market"].lower() != market.lower():
            continue
        if abs(l["linea"] - float(linea)) > 0.01:
            continue
        if side and l["side"].upper() != side.upper():
            continue
        return l
    return None
