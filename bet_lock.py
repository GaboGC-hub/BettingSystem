"""
bet_lock.py — Persistent Bet State Lock + Control System + Trade Blotter
========================================================================
Resuelve el problema del "Sniper Lock de Software":
  - Las apuestas activas se persisten en active_bets.json
  - Sobreviven reinicios del script
  - El lock se identifica por (match_url + market + linea + side)
  - Una vez colocada una apuesta, el bot NO puede repetirla hasta que:
      a) Se resuelva manualmente desde el dashboard, o
      b) Pasen auto_expire_minutes (configurable, por defecto 90 min)

Control dinámico sin reinicio:
  - control.json se recarga en cada intento de lock
  - Botón de pánico: bot_status = "PAUSED" → rechaza todos los locks
  - Max open bets por partido: max_open_bets_per_match
  - EV mínimo dinámico: min_ev_threshold

Trade Blotter:
  - Cada lock exitoso escribe una fila en trade_blotter.csv
  - Columnas: timestamp, match_url, market_and_line, odds_taken,
    model_prob, ev_neto, stake, status
"""

from __future__ import annotations

import csv
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

LOCK_FILE = Path(os.path.dirname(__file__)) / "active_bets.json"
CONTROL_FILE = Path(os.path.dirname(__file__)) / "control.json"
BLOTTER_FILE = Path(os.path.dirname(__file__)) / "trade_blotter.csv"
AUTO_EXPIRE_MINUTES = 90
COOLDOWN_SECONDS = 300          # 5 minutos de enfriamiento entre apuestas del mismo partido
MAX_LIABILITY_PER_MATCH = 0.0   # 0 = sin limite; configurable via control.json
_lock = threading.Lock()


def load_control() -> dict:
    if not CONTROL_FILE.exists():
        return {"bot_status": "RUNNING", "min_ev_threshold": 0.08, "max_open_bets_per_match": 2}
    try:
        with open(CONTROL_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"bot_status": "RUNNING", "min_ev_threshold": 0.08, "max_open_bets_per_match": 2}


def save_control(data: dict) -> None:
    with open(CONTROL_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _load() -> dict[str, dict]:
    if not LOCK_FILE.exists():
        return {}
    try:
        with open(LOCK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save(locks: dict[str, dict]) -> None:
    tmp = LOCK_FILE.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(locks, f, ensure_ascii=False, indent=2)
    os.replace(tmp, LOCK_FILE)


def _append_blotter(
    match_url: str,
    market: str,
    linea: float,
    side: str,
    odds: float,
    model_prob: float,
    ev_neto: float,
    stake_usd: float,
    status: str,
) -> None:
    exists = BLOTTER_FILE.exists()
    with open(BLOTTER_FILE, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        if not exists:
            writer.writerow(["timestamp", "match_url", "market_and_line", "odds_taken",
                             "model_prob", "ev_neto", "stake", "status"])
        writer.writerow([
            time.strftime("%Y-%m-%d %H:%M:%S"),
            match_url,
            f"{market} {side} {linea}",
            f"{odds:.3f}",
            f"{model_prob:.4f}" if model_prob else "",
            f"{ev_neto:.4f}" if ev_neto else "",
            f"{stake_usd:.2f}",
            status,
        ])


def _find_active(
    locks: dict,
    match_url: str,
    market: str,
    linea: float,
    side: Optional[str] = None,
) -> Optional[dict]:
    now = time.time()
    for l in locks.values():
        if l["expires_at"] < now:
            continue
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
    model_prob: float = 0.0,
    ev_neto: float = 0.0,
) -> Optional[str]:
    """
    Registra una apuesta activa. Devuelve el lock_id, o None si fue rechazada.

    Control dinámico:
      - Si bot_status = "PAUSED", rechaza y loguea.
      - Si ya hay >= max_open_bets_per_match locks activos para este match_url, rechaza.
    """
    ctrl = load_control()

    if ctrl.get("bot_status", "RUNNING") == "PAUSED":
        print(f"[BET LOCK] ⏸️  Bot PAUSADO — lock rechazado para {match_url[-40:]}")
        _append_blotter(match_url, market, linea, side, odds, model_prob, ev_neto, stake_usd, "BLOCKED:PAUSED")
        return None

    with _lock:
        locks = _load()
        now = time.time()

        active_for_match = [l for l in locks.values()
                           if l["match_url"] == match_url and l["expires_at"] >= now]
        max_open = int(ctrl.get("max_open_bets_per_match", 2))
        if len(active_for_match) >= max_open:
            print(f"[BET LOCK] 🚫 Exposure limit ({max_open}) alcanzado para {match_url[-40:]}")
            _append_blotter(match_url, market, linea, side, odds, model_prob, ev_neto, stake_usd, "BLOCKED:EXPOSURE")
            return None

        # ── Cooldown: no permitir otra apuesta en el mismo partido dentro de COOLDOWN_SECONDS ──
        if active_for_match:
            last_lock_ts = max(l["locked_at"] for l in active_for_match)
            if now - last_lock_ts < COOLDOWN_SECONDS:
                remaining = int(COOLDOWN_SECONDS - (now - last_lock_ts))
                print(f"[BET LOCK] ⏳ Cooldown activo ({remaining}s restantes) para {match_url[-40:]}")
                _append_blotter(match_url, market, linea, side, odds, model_prob, ev_neto, stake_usd, "BLOCKED:COOLDOWN")
                return None

        # ── Max Liability: rechazar si el stake total del partido supera el limite ──
        max_liability = float(ctrl.get("max_liability_per_match", MAX_LIABILITY_PER_MATCH))
        if max_liability > 0:
            total_staked = sum(l.get("stake_usd", 0) for l in active_for_match)
            if total_staked + stake_usd > max_liability:
                print(f"[BET LOCK] 💰 Max liability (${max_liability:.0f}) excedida para {match_url[-40:]} (actual=${total_staked:.0f} + nueva=${stake_usd:.0f})")
                _append_blotter(match_url, market, linea, side, odds, model_prob, ev_neto, stake_usd, "BLOCKED:LIABILITY")
                return None

        existing = _find_active(locks, match_url, market, linea, side)
        if existing:
            return existing["lock_id"]

        lock_id = str(uuid.uuid4())[:8]
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
            "model_prob": float(model_prob),
            "ev_neto":    float(ev_neto),
        }
        _save(locks)
        _append_blotter(match_url, market, linea, side, odds, model_prob, ev_neto, stake_usd, "EXECUTED")
        print(f"[BET LOCK] 🔒 {market} {side} {linea} → lock_id={lock_id} ({match_url[-40:]})")
        return lock_id


def release_lock(lock_id: str) -> bool:
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
    with _lock:
        locks = _load()
        return _find_active(locks, match_url, market, linea, side)


def get_all_locks(match_url: Optional[str] = None) -> list[dict]:
    with _lock:
        locks = _load()
        now = time.time()

        expired = [lid for lid, l in locks.items() if l["expires_at"] < now]
        if expired:
            for lid in expired:
                locks.pop(lid)
            _save(locks)

        active = list(locks.values())
        if match_url:
            active = [l for l in active if l["match_url"] == match_url]

        for l in active:
            l["locked_ago_s"] = int(now - l["locked_at"])
            l["expires_in_s"] = max(0, int(l["expires_at"] - now))

        return sorted(active, key=lambda x: x["locked_at"], reverse=True)


def cleanup_expired() -> int:
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


def read_blotter(limit: int = 200) -> list[dict]:
    if not BLOTTER_FILE.exists():
        return []
    rows = []
    with open(BLOTTER_FILE, "r", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            rows.append(row)
    return rows[-limit:]
