#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from scipy.stats import nbinom, poisson


try:
    from futbol_live_bridge import (
        PrematchContext,
        PrematchWorkbook,
        SofaScoreMonitor,
        load_prematch_context_from_json,
        merge_prematch_contexts,
    )
except ImportError:  # pragma: no cover - optional live bridge
    PrematchContext = None  # type: ignore[assignment]
    PrematchWorkbook = None  # type: ignore[assignment]
    SofaScoreMonitor = None  # type: ignore[assignment]
    load_prematch_context_from_json = None  # type: ignore[assignment]
    merge_prematch_contexts = None  # type: ignore[assignment]


@dataclass(frozen=True)
class MatchState:
    minuto: float
    goles_local: float
    goles_visitante: float
    amarillas: float
    rojas: float
    faltas: float
    corners: float
    xg_local: float
    xg_visitante: float
    tiros_local: float
    tiros_visitante: float
    tiros_puerta_local: float
    tiros_puerta_visitante: float
    posesion_local: float
    corners_local: float = 0.0
    corners_visitante: float = 0.0
    faltas_local: float = 0.0
    faltas_visitante: float = 0.0
    amarillas_local: float = 0.0
    amarillas_visitante: float = 0.0
    rojas_local: float = 0.0
    rojas_visitante: float = 0.0
    corners_recientes: float = 0.0
    xg_recientes: float = 0.0
    tiros_recientes: float = 0.0
    ventana_reciente_min: float = 0.0
    faltas_recientes: float = 0.0
    tarjetas_recientes: float = 0.0
    centros_local: float = 0.0
    centros_visitante: float = 0.0
    centros_recientes: float = 0.0
    touches_in_box_home: float = 0.0
    touches_in_box_away: float = 0.0
    dangerous_attacks_home: float = 0.0
    dangerous_attacks_away: float = 0.0
    big_chances_missed_home: float = 0.0
    big_chances_missed_away: float = 0.0
    referee_name: str | None = None
    urgency_multiplier: float = 1.0
    defensive_yellows: float = 0.0


@dataclass(frozen=True)
class MarketLine:
    linea: float
    over: float
    under: float
    is_dummy: bool = False  # True cuando no hay cuota real de mercado (fallback estimado)
    source_id: str = "betano"  # Origen: "pinnacle", "betano", "betplay", etc.


@dataclass(frozen=True)
class MarketSet:
    goles: MarketLine
    corners: MarketLine
    tarjetas: MarketLine


@dataclass(frozen=True)
class DemoScenario:
    key: str
    nombre: str
    descripcion: str
    match_state: MatchState
    markets: MarketSet


@dataclass(frozen=True)
class MatchPhase:
    nombre: str
    resumen: str
    goal_modifier: float
    corner_modifier: float
    card_modifier: float
    open_bias: float
    cool_bias: float


@dataclass(frozen=True)
class LeagueProfile:
    key: str
    label: str
    goal_baseline_per90: float
    corner_baseline_per90: float
    card_baseline_per90: float
    goal_multiplier: float = 1.0
    corner_multiplier: float = 1.0
    card_multiplier: float = 1.0


@dataclass(frozen=True)
class RefereeProfile:
    name: str
    card_multiplier: float
    foul_tolerance_multiplier: float
    strictness_label: str

REFEREE_PROFILE_MAP = {
    "dario herrera": RefereeProfile("Dario Herrera", 1.25, 0.85, "Gatillo Facil"),
    "facundo tello": RefereeProfile("Facundo Tello", 1.15, 0.90, "Estricto"),
    "fernando rapallini": RefereeProfile("Fernando Rapallini", 1.10, 0.95, "Firme"),
    "andres merlos": RefereeProfile("Andres Merlos", 1.20, 0.88, "Riguroso"),
    "michael oliver": RefereeProfile("Michael Oliver", 0.85, 1.15, "Permisivo"),
    "anthony taylor": RefereeProfile("Anthony Taylor", 0.95, 1.05, "Tolerante"),
    "jesus gil manzano": RefereeProfile("Jesus Gil Manzano", 1.30, 0.80, "Muy Estricto"),
    "mateu lahoz": RefereeProfile("Mateu Lahoz", 1.20, 0.90, "Protagónico"),
    "wilmar roldan": RefereeProfile("Wilmar Roldan", 1.10, 0.95, "Firme"),
}

def get_referee_profile(referee_name: str | None) -> RefereeProfile | None:
    if not referee_name:
        return None
    normalized = referee_name.lower().strip()
    # Partial matching for common names
    for key, profile in REFEREE_PROFILE_MAP.items():
        if key in normalized or normalized in key:
            return profile
    return None

def calc_fair_odds_pinnacle(over_odds: float, under_odds: float) -> tuple[float, float]:
    """
    Descuenta el margen (Vig) de Pinnacle o cualquier casa para obtener la probabilidad real.
    Devuelve (fair_over_odds, fair_under_odds)
    """
    if over_odds <= 1.0 or under_odds <= 1.0:
        return over_odds, under_odds
    prob_over = 1.0 / over_odds
    prob_under = 1.0 / under_odds
    total_prob = prob_over + prob_under
    fair_prob_over = prob_over / total_prob
    fair_prob_under = prob_under / total_prob
    return (1.0 / fair_prob_over), (1.0 / fair_prob_under)


def power_method_devig_two_way(
    over_odds: float, under_odds: float, max_iter: int = 80
) -> tuple[float, float, float | None]:
    """
    Power method (Zhang et al.): hallar k > 0 con (1/Oo)^k + (1/Ou)^k = 1.
    Probabilidades sin vig: p_i = (1/O_i)^k. Cuotas justas = 1/p_i.
    Mejor que la normalización aditiva en extremos (cuotas muy asimétricas).
    """
    if over_odds <= 1.0 or under_odds <= 1.0:
        return over_odds, under_odds, None
    q1 = 1.0 / over_odds
    q2 = 1.0 / under_odds
    s = q1 + q2
    if s <= 1.0 + 1e-9:
        p1 = q1 / s
        p2 = q2 / s
        return (1.0 / p1), (1.0 / p2), 1.0

    def f(k: float) -> float:
        return q1**k + q2**k - 1.0

    lo, hi = 1e-9, 80.0
    while f(hi) > 0.0 and hi < 1e5:
        hi *= 1.5
    if f(hi) > 0.0:
        fo, fu = calc_fair_odds_pinnacle(over_odds, under_odds)
        return fo, fu, None

    for _ in range(max_iter):
        mid = 0.5 * (lo + hi)
        if f(mid) > 0.0:
            lo = mid
        else:
            hi = mid
    k = 0.5 * (lo + hi)
    p1 = q1**k
    p2 = q2**k
    if p1 <= 0.0 or p2 <= 0.0:
        fo, fu = calc_fair_odds_pinnacle(over_odds, under_odds)
        return fo, fu, None
    return (1.0 / p1), (1.0 / p2), k


def fair_odds_for_live_total_market(
    market_name: str, raw_over: float, raw_under: float
) -> tuple[float, float]:
    """Goles y corners: power method. Tarjetas: vig aditivo (2 vías)."""
    if market_name in ("GOLES", "CORNERS"):
        fo, fu, _ = power_method_devig_two_way(raw_over, raw_under)
        return fo, fu
    return calc_fair_odds_pinnacle(raw_over, raw_under)


@dataclass(frozen=True)
class ModelParams:
    nombre: str
    seed: int
    sims: int
    edge_threshold: float
    kelly_fraction: float
    max_stake: float
    goal_multiplier: float
    corner_multiplier: float
    card_multiplier: float
    goal_xg_weight: float
    goal_danger_weight: float
    goal_baseline_weight: float
    shot_on_target_weight: float
    shot_weight: float
    goal_baseline_per90: float
    corner_observed_weight: float
    corner_danger_weight: float
    corner_baseline_weight: float
    corner_baseline_per90: float
    card_fouls_weight: float
    card_conversion_weight: float
    card_cards_weight: float
    card_baseline_weight: float
    card_baseline_per90: float
    close_game_goal_boost: float
    late_goal_boost: float
    trailing_goal_boost: float
    late_corner_boost: float
    close_game_card_boost: float
    late_card_boost: float
    red_card_tension_boost: float
    max_goal_lambda: float
    max_corner_lambda: float
    max_card_lambda: float
    base_extra_time: float
    corner_nb_theta: float
    card_nb_theta: float
    ev_p25_min: float
    siege_touch_threshold: float
    siege_expm1_scale: float
    siege_boost_cap: float
    siege_raw_mult: float
    siege_missed_mult: float
    wpf_high: float
    wpf_low: float


@dataclass(frozen=True)
class ModelResult:
    state: MatchState
    markets: MarketSet
    params: ModelParams
    phase_name: str
    phase_summary: str
    remaining_minutes: float
    danger_rate: float
    tension_index: float
    urgency_factor: float
    lambda_goals: float
    lambda_corners: float
    lambda_cards: float
    acceleration_weight: float
    cooldown_weight: float
    neutral_weight: float
    total_goals: np.ndarray
    total_corners: np.ndarray
    total_cards: np.ndarray
    wpf_home: float
    wpf_away: float
    siege_home: float
    siege_away: float
    tightrope_boost: float


@dataclass(frozen=True)
class MarketDecision:
    linea: float
    prob_over: float
    prob_under: float
    prob_push: float
    fair_over: float
    fair_under: float
    ev_over: float
    ev_under: float
    best_side: str
    best_ev: float
    best_prob: float
    best_stake: float
    mean_total: float
    future_mean: float
    note: str
    best_ev_p25: float


@dataclass
class StressAccumulator:
    bets: int = 0
    stake: float = 0.0
    profit: float = 0.0
    wins: int = 0
    brier_sum: float = 0.0
    brier_count: int = 0


def _make_market(linea: float, over: float, under: float, source_id: str = "unknown") -> MarketLine:
    return MarketLine(linea=float(linea), over=float(over), under=float(under), source_id=source_id)


DEMO_SCENARIOS = {
    "1": DemoScenario(
        key="1",
        nombre="partido_equilibrado_67",
        descripcion="Juego parejo, ritmo alto y mercados bastante eficientes.",
        match_state=MatchState(
            minuto=67,
            goles_local=1,
            goles_visitante=1,
            amarillas=4,
            rojas=0,
            faltas=23,
            corners=7,
            xg_local=1.05,
            xg_visitante=0.94,
            tiros_local=11,
            tiros_visitante=9,
            tiros_puerta_local=4,
            tiros_puerta_visitante=3,
            posesion_local=53,
        ),
        markets=MarketSet(
            goles=_make_market(2.5, 1.95, 1.87),
            corners=_make_market(9.5, 1.98, 1.82),
            tarjetas=_make_market(5.5, 1.91, 1.91),
        ),
    ),
    "2": DemoScenario(
        key="2",
        nombre="presion_local_58",
        descripcion="El local pierde y ataca mucho; ideal para testear goles y corners.",
        match_state=MatchState(
            minuto=58,
            goles_local=0,
            goles_visitante=1,
            amarillas=2,
            rojas=0,
            faltas=16,
            corners=6,
            xg_local=0.88,
            xg_visitante=0.72,
            tiros_local=13,
            tiros_visitante=6,
            tiros_puerta_local=5,
            tiros_puerta_visitante=2,
            posesion_local=64,
        ),
        markets=MarketSet(
            goles=_make_market(2.5, 2.04, 1.79),
            corners=_make_market(9.5, 1.86, 1.96),
            tarjetas=_make_market(4.5, 1.83, 2.00),
        ),
    ),
    "3": DemoScenario(
        key="3",
        nombre="final_caliente_79",
        descripcion="Cierre tenso con rojas, faltas y partido todavia abierto.",
        match_state=MatchState(
            minuto=79,
            goles_local=2,
            goles_visitante=1,
            amarillas=6,
            rojas=1,
            faltas=29,
            corners=9,
            xg_local=1.70,
            xg_visitante=1.20,
            tiros_local=15,
            tiros_visitante=10,
            tiros_puerta_local=6,
            tiros_puerta_visitante=4,
            posesion_local=48,
        ),
        markets=MarketSet(
            goles=_make_market(3.5, 1.92, 1.92),
            corners=_make_market(10.5, 1.88, 1.94),
            tarjetas=_make_market(9.5, 1.95, 1.87),
        ),
    ),
}


PARAMETER_PRESETS = {
    "conservative": ModelParams(
        nombre="conservative",
        seed=7,
        sims=30000,
        edge_threshold=1.10,
        kelly_fraction=0.08,
        max_stake=0.03,
        goal_multiplier=0.95,
        corner_multiplier=0.96,
        card_multiplier=0.92,
        goal_xg_weight=0.44,
        goal_danger_weight=0.07,
        goal_baseline_weight=0.49,
        shot_on_target_weight=0.74,
        shot_weight=0.26,
        goal_baseline_per90=2.55,
        corner_observed_weight=0.18,
        corner_danger_weight=0.08,
        corner_baseline_weight=0.74,
        corner_baseline_per90=9.60,
        card_fouls_weight=0.07,
        card_conversion_weight=0.05,
        card_cards_weight=0.12,
        card_baseline_weight=0.10,
        card_baseline_per90=4.80,
        close_game_goal_boost=1.05,
        late_goal_boost=1.07,
        trailing_goal_boost=1.03,
        late_corner_boost=1.03,
        close_game_card_boost=1.04,
        late_card_boost=1.08,
        red_card_tension_boost=1.06,
        max_goal_lambda=2.50,
        max_corner_lambda=5.20,
        max_card_lambda=3.20,
        base_extra_time=4.00,
        corner_nb_theta=18.0,
        card_nb_theta=15.0,
        ev_p25_min=1.03,
        siege_touch_threshold=10.0,
        siege_expm1_scale=0.12,
        siege_boost_cap=0.50,
        siege_raw_mult=0.08,
        siege_missed_mult=0.20,
        wpf_high=0.70,
        wpf_low=0.30,
    ),
    "balanced": ModelParams(
        nombre="balanced",
        seed=7,
        sims=30000,
        edge_threshold=1.07,
        kelly_fraction=0.10,
        max_stake=0.04,
        goal_multiplier=1.00,
        corner_multiplier=1.00,
        card_multiplier=1.00,
        goal_xg_weight=0.46,
        goal_danger_weight=0.08,
        goal_baseline_weight=0.46,
        shot_on_target_weight=0.72,
        shot_weight=0.28,
        goal_baseline_per90=2.60,
        corner_observed_weight=0.20,
        corner_danger_weight=0.10,
        corner_baseline_weight=0.70,
        corner_baseline_per90=9.80,
        card_fouls_weight=0.08,
        card_conversion_weight=0.06,
        card_cards_weight=0.15,
        card_baseline_weight=0.10,
        card_baseline_per90=4.90,
        close_game_goal_boost=1.08,
        late_goal_boost=1.10,
        trailing_goal_boost=1.05,
        late_corner_boost=1.05,
        close_game_card_boost=1.06,
        late_card_boost=1.12,
        red_card_tension_boost=1.10,
        max_goal_lambda=2.90,
        max_corner_lambda=5.80,
        max_card_lambda=3.80,
        base_extra_time=4.00,
        corner_nb_theta=12.0,
        card_nb_theta=10.0,
        ev_p25_min=1.02,
        siege_touch_threshold=10.0,
        siege_expm1_scale=0.12,
        siege_boost_cap=0.50,
        siege_raw_mult=0.08,
        siege_missed_mult=0.20,
        wpf_high=0.70,
        wpf_low=0.30,
    ),
    "aggressive": ModelParams(
        nombre="aggressive",
        seed=7,
        sims=35000,
        edge_threshold=1.04,
        kelly_fraction=0.12,
        max_stake=0.05,
        goal_multiplier=1.07,
        corner_multiplier=1.06,
        card_multiplier=1.08,
        goal_xg_weight=0.48,
        goal_danger_weight=0.09,
        goal_baseline_weight=0.43,
        shot_on_target_weight=0.70,
        shot_weight=0.30,
        goal_baseline_per90=2.65,
        corner_observed_weight=0.23,
        corner_danger_weight=0.11,
        corner_baseline_weight=0.66,
        corner_baseline_per90=10.00,
        card_fouls_weight=0.09,
        card_conversion_weight=0.07,
        card_cards_weight=0.18,
        card_baseline_weight=0.11,
        card_baseline_per90=5.00,
        close_game_goal_boost=1.10,
        late_goal_boost=1.14,
        trailing_goal_boost=1.08,
        late_corner_boost=1.08,
        close_game_card_boost=1.08,
        late_card_boost=1.16,
        red_card_tension_boost=1.14,
        max_goal_lambda=3.20,
        max_corner_lambda=6.50,
        max_card_lambda=4.30,
        base_extra_time=4.20,
        corner_nb_theta=8.0,
        card_nb_theta=6.0,
        ev_p25_min=1.02,
        siege_touch_threshold=10.0,
        siege_expm1_scale=0.12,
        siege_boost_cap=0.50,
        siege_raw_mult=0.08,
        siege_missed_mult=0.20,
        wpf_high=0.70,
        wpf_low=0.30,
    ),
}


SESSION_STATE_PATH = Path(__file__).with_name(".futbol_live_state.json")
LIVE_HISTORY_DIR = Path(__file__).with_name("live_history")


GLOBAL_LEAGUE_PROFILE = LeagueProfile(
    key="global",
    label="Global",
    goal_baseline_per90=2.60,
    corner_baseline_per90=9.80,
    card_baseline_per90=4.90,
)


LEAGUE_PROFILE_MAP = {
    "premier league": LeagueProfile("premier-league", "Premier League", 2.95, 10.10, 4.20, 1.04, 1.03, 0.96),
    "bundesliga": LeagueProfile("bundesliga", "Bundesliga", 3.08, 9.70, 3.90, 1.07, 0.99, 0.90),
    "2 bundesliga": LeagueProfile("2-bundesliga", "2. Bundesliga", 3.02, 10.00, 4.15, 1.06, 1.02, 0.93),
    "la liga": LeagueProfile("la-liga", "La Liga", 2.48, 9.05, 5.35, 0.95, 0.95, 1.08),
    "serie a": LeagueProfile("serie-a", "Serie A", 2.72, 9.45, 4.95, 1.00, 0.98, 1.02),
    "ligue 1": LeagueProfile("ligue-1", "Ligue 1", 2.68, 9.35, 4.25, 0.99, 0.97, 0.96),
    "eredivisie": LeagueProfile("eredivisie", "Eredivisie", 3.18, 10.05, 3.85, 1.08, 1.01, 0.89),
    "liga mx": LeagueProfile("liga-mx", "Liga MX", 2.86, 9.20, 5.05, 1.02, 0.96, 1.05),
    "championship": LeagueProfile("championship", "Championship", 2.56, 10.35, 4.15, 0.96, 1.04, 0.95),
    "primeira liga": LeagueProfile("primeira-liga", "Primeira Liga", 2.44, 9.40, 5.50, 0.94, 0.96, 1.10),
    "super lig": LeagueProfile("super-lig", "Super Lig", 2.92, 9.65, 5.25, 1.03, 0.99, 1.07),
    "brasileirao serie a": LeagueProfile("brasileirao-serie-a", "Brasileirao Serie A", 2.40, 10.05, 5.05, 0.93, 1.02, 1.04),
    "primera division": LeagueProfile("argentina-primera", "Primera Division", 2.22, 9.35, 5.80, 0.90, 0.97, 1.12),
    "amateur": LeagueProfile("amateur", "Amateur", 3.00, 9.30, 4.50, 1.05, 0.96, 0.94),
}


LEAGUE_PROFILE_KEYWORDS = [
    ("premier league", LEAGUE_PROFILE_MAP["premier league"]),
    ("bundesliga", LEAGUE_PROFILE_MAP["bundesliga"]),
    ("2. bundesliga", LEAGUE_PROFILE_MAP["2 bundesliga"]),
    ("2 bundesliga", LEAGUE_PROFILE_MAP["2 bundesliga"]),
    ("la liga", LEAGUE_PROFILE_MAP["la liga"]),
    ("serie a", LEAGUE_PROFILE_MAP["serie a"]),
    ("ligue 1", LEAGUE_PROFILE_MAP["ligue 1"]),
    ("eredivisie", LEAGUE_PROFILE_MAP["eredivisie"]),
    ("liga mx", LEAGUE_PROFILE_MAP["liga mx"]),
    ("championship", LEAGUE_PROFILE_MAP["championship"]),
    ("primeira liga", LEAGUE_PROFILE_MAP["primeira liga"]),
    ("super lig", LEAGUE_PROFILE_MAP["super lig"]),
    ("brasileirao", LEAGUE_PROFILE_MAP["brasileirao serie a"]),
    ("primera division", LEAGUE_PROFILE_MAP["primera division"]),
    ("amateur", LEAGUE_PROFILE_MAP["amateur"]),
]


def pedir_float(prompt: str, default: float | None = None) -> float:
    suffix = f" [{default}]" if default is not None else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    if not raw:
        if default is None:
            return 0.0
        return float(default)
    try:
        return float(raw.replace(",", "."))
    except ValueError:
        return float(default or 0.0)


def pedir_opcion(prompt: str, validas: set[str], default: str) -> str:
    raw = input(f"{prompt} [{default}]: ").strip().lower()
    if not raw:
        return default
    if raw in validas:
        return raw
    return default


def pedir_texto(prompt: str, default: str | None = None) -> str:
    suffix = f" [{default}]" if default else ""
    raw = input(f"{prompt}{suffix}: ").strip()
    if raw:
        return raw
    return default or ""


def normalize_label(value: str | None) -> str:
    if not value:
        return ""
    text = value.lower().replace("_", " ").replace("-", " ")
    return " ".join(text.split())


def load_session_state() -> dict[str, object]:
    if not SESSION_STATE_PATH.exists():
        return {}
    try:
        return json.loads(SESSION_STATE_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def save_session_state(state: dict[str, object]) -> None:
    current = load_session_state()
    current.update(state)
    try:
        SESSION_STATE_PATH.write_text(
            json.dumps(current, indent=2, ensure_ascii=True),
            encoding="utf-8",
        )
    except OSError:
        return


def safe_slug(value: str, fallback: str = "match") -> str:
    normalized = normalize_label(value)
    cleaned = "".join(char if char.isalnum() else "-" for char in normalized)
    compact = "-".join(part for part in cleaned.split("-") if part)
    return compact or fallback


def market_line_to_dict(market: MarketLine) -> dict[str, float | bool | str]:
    return {
        "line": float(market.linea),
        "over": float(market.over),
        "under": float(market.under),
        "is_dummy": bool(market.is_dummy),
        "source_id": market.source_id,
    }


def live_market_to_dict(market: object | None) -> dict[str, float | int | None] | None:
    if market is None:
        return None
    return {
        "line": float(getattr(market, "line")),
        "over": float(getattr(market, "over")),
        "under": float(getattr(market, "under")),
        "provider_id": getattr(market, "provider_id", None),
    }


def prematch_to_dict(prematch: PrematchContext | None) -> dict[str, object] | None:
    if prematch is None:
        return None
    payload = asdict(prematch)
    payload["notes"] = list(payload.get("notes", ()))
    return payload


def history_file_path(history_dir: Path, snapshot: object) -> Path:
    match_slug = safe_slug(
        f"{getattr(snapshot, 'home_team', 'home')} vs {getattr(snapshot, 'away_team', 'away')}",
        "match",
    )
    file_name = f"{getattr(snapshot, 'event_id', 'match')}_{match_slug}.jsonl"
    return history_dir / file_name


def infer_league_profile(snapshot: object | None = None, league_name: str | None = None) -> LeagueProfile:
    candidates = [
        league_name,
        getattr(snapshot, "tournament", None) if snapshot is not None else None,
        getattr(snapshot, "tournament_slug", None) if snapshot is not None else None,
        getattr(snapshot, "category_name", None) if snapshot is not None else None,
        getattr(snapshot, "country_name", None) if snapshot is not None else None,
    ]
    normalized = [normalize_label(str(candidate)) for candidate in candidates if candidate]
    for item in normalized:
        if item in LEAGUE_PROFILE_MAP:
            return LEAGUE_PROFILE_MAP[item]
    for item in normalized:
        for keyword, profile in LEAGUE_PROFILE_KEYWORDS:
            if keyword in item:
                return profile
    return GLOBAL_LEAGUE_PROFILE


def apply_league_profile(params: ModelParams, profile: LeagueProfile) -> ModelParams:
    if profile.key == GLOBAL_LEAGUE_PROFILE.key:
        return params
    baseline_blend = 0.55
    multiplier_blend = 0.50
    return replace(
        params,
        goal_baseline_per90=(params.goal_baseline_per90 * (1.0 - baseline_blend)) + (profile.goal_baseline_per90 * baseline_blend),
        corner_baseline_per90=(params.corner_baseline_per90 * (1.0 - baseline_blend)) + (profile.corner_baseline_per90 * baseline_blend),
        card_baseline_per90=(params.card_baseline_per90 * (1.0 - baseline_blend)) + (profile.card_baseline_per90 * baseline_blend),
        goal_multiplier=params.goal_multiplier * ((1.0 - multiplier_blend) + (profile.goal_multiplier * multiplier_blend)),
        corner_multiplier=params.corner_multiplier * ((1.0 - multiplier_blend) + (profile.corner_multiplier * multiplier_blend)),
        card_multiplier=params.card_multiplier * ((1.0 - multiplier_blend) + (profile.card_multiplier * multiplier_blend)),
    )


def apply_referee_profile(params: ModelParams, profile: RefereeProfile | None) -> ModelParams:
    if profile is None:
        return params
    return replace(
        params,
        card_multiplier=params.card_multiplier * profile.card_multiplier,
        card_fouls_weight=params.card_fouls_weight * profile.foul_tolerance_multiplier
    )


def build_params(preset_name: str, seed: int | None = None, sims: int | None = None) -> ModelParams:
    base = PARAMETER_PRESETS[preset_name]
    if seed is None and sims is None:
        return base
    return replace(
        base,
        seed=base.seed if seed is None else seed,
        sims=base.sims if sims is None else sims,
    )


def build_state_from_snapshot(snapshot: object, previous_state: MatchState | None = None) -> MatchState:
    state = MatchState(
        minuto=float(getattr(snapshot, "minute")),
        goles_local=float(getattr(snapshot, "goals_home")),
        goles_visitante=float(getattr(snapshot, "goals_away")),
        amarillas=float(getattr(snapshot, "yellows_total")),
        rojas=float(getattr(snapshot, "reds_total")),
        faltas=float(getattr(snapshot, "fouls_total")),
        corners=float(getattr(snapshot, "corners_total")),
        xg_local=float(getattr(snapshot, "xg_home")),
        xg_visitante=float(getattr(snapshot, "xg_away")),
        tiros_local=float(getattr(snapshot, "shots_home")),
        tiros_visitante=float(getattr(snapshot, "shots_away")),
        tiros_puerta_local=float(getattr(snapshot, "shots_on_target_home")),
        tiros_puerta_visitante=float(getattr(snapshot, "shots_on_target_away")),
        posesion_local=float(getattr(snapshot, "possession_home")),
        corners_local=float(getattr(snapshot, "corners_home", 0.0)),
        corners_visitante=float(getattr(snapshot, "corners_away", 0.0)),
        faltas_local=float(getattr(snapshot, "fouls_home", 0.0)),
        faltas_visitante=float(getattr(snapshot, "fouls_away", 0.0)),
        amarillas_local=float(getattr(snapshot, "yellows_home", 0.0)),
        amarillas_visitante=float(getattr(snapshot, "yellows_away", 0.0)),
        rojas_local=float(getattr(snapshot, "reds_home", 0.0)),
        rojas_visitante=float(getattr(snapshot, "reds_away", 0.0)),
        centros_local=float(getattr(snapshot, "crosses_home", getattr(snapshot, "centros_local", 0.0))),
        centros_visitante=float(getattr(snapshot, "crosses_away", getattr(snapshot, "centros_visitante", 0.0))),
        touches_in_box_home=float(getattr(snapshot, "touches_in_box_home", 0.0)),
        touches_in_box_away=float(getattr(snapshot, "touches_in_box_away", 0.0)),
        dangerous_attacks_home=float(getattr(snapshot, "dangerous_attacks_home", 0.0)),
        dangerous_attacks_away=float(getattr(snapshot, "dangerous_attacks_away", 0.0)),
        big_chances_missed_home=float(getattr(snapshot, "big_chances_missed_home", 0.0)),
        big_chances_missed_away=float(getattr(snapshot, "big_chances_missed_away", 0.0)),
        referee_name=getattr(snapshot, "referee", getattr(snapshot, "referee_name", None)),
        urgency_multiplier=float(getattr(snapshot, "urgency_multiplier", 1.0)),
        defensive_yellows=float(getattr(snapshot, "defensive_yellows", 0.0)),
    )
    if previous_state is None:
        return state

    delta_min = state.minuto - previous_state.minuto
    if delta_min < 1.0 or delta_min > 20.0:
        return state

    current_cards = state.amarillas + state.rojas
    previous_cards = previous_state.amarillas + previous_state.rojas
    return replace(
        state,
        corners_recientes=max(0.0, state.corners - previous_state.corners),
        xg_recientes=max(0.0, (state.xg_local + state.xg_visitante) - (previous_state.xg_local + previous_state.xg_visitante)),
        tiros_recientes=max(0.0, (state.tiros_local + state.tiros_visitante) - (previous_state.tiros_local + previous_state.tiros_visitante)),
        ventana_reciente_min=delta_min,
        faltas_recientes=max(0.0, state.faltas - previous_state.faltas),
        tarjetas_recientes=max(0.0, current_cards - previous_cards),
        centros_recientes=max(0.0, (state.centros_local + state.centros_visitante) - (previous_state.centros_local + previous_state.centros_visitante)),
    )


def build_markets_from_snapshot(snapshot: object, state: MatchState, pinnacle_fair: dict | None = None) -> MarketSet:
    goal_market = getattr(snapshot, "goals_market")
    corner_market = getattr(snapshot, "corners_market")
    card_market = getattr(snapshot, "cards_market")
    pf = pinnacle_fair or {}

    def _extract_best_line(pf_mkt_data, default_line):
        if not pf_mkt_data:
            return {"linea": default_line, "over": 1.90, "under": 1.90, "is_dummy": True, "source_id": "unknown"}
        if isinstance(pf_mkt_data, list) and len(pf_mkt_data) > 0:
            best = sorted(pf_mkt_data, key=lambda x: abs(float(x.get("over", 1.90)) - float(x.get("under", 1.90))))[0]
            return {
                "linea": float(best.get("linea", default_line)),
                "over": float(best.get("over", 1.90)),
                "under": float(best.get("under", 1.90)),
                "is_dummy": False,
                "source_id": str(best.get("source_id", "betano"))
            }
        if isinstance(pf_mkt_data, dict):
            return {
                "linea": float(pf_mkt_data.get("linea", default_line)),
                "over": float(pf_mkt_data.get("over", 1.90)),
                "under": float(pf_mkt_data.get("under", 1.90)),
                "is_dummy": False,
                "source_id": str(pf_mkt_data.get("source_id", pf_mkt_data.get("source", "betano")))
            }
        return {"linea": default_line, "over": 1.90, "under": 1.90, "is_dummy": True, "source_id": "unknown"}

    pf_goles = _extract_best_line(pf.get("GOLES") or pf.get("goles"), state.goles_local + state.goles_visitante + 1.5)
    pf_corners = _extract_best_line(pf.get("CORNERS") or pf.get("corners"), state.corners + 2.5)
    pf_tarjetas = _extract_best_line(pf.get("TARJETAS") or pf.get("tarjetas"), state.amarillas + state.rojas + 2.5)

    print(f"📸 [SNAPSHOT] goal_market={goal_market}, corner_market={corner_market}, card_market={card_market}")

    return MarketSet(
        goles=(
            _make_market(goal_market.line, goal_market.over, goal_market.under)
            if goal_market is not None
            else MarketLine(
                linea=pf_goles["linea"], over=pf_goles["over"], under=pf_goles["under"],
                is_dummy=pf_goles["is_dummy"], source_id=pf_goles["source_id"]
            )
        ),
        corners=(
            _make_market(corner_market.line, corner_market.over, corner_market.under)
            if corner_market is not None
            else MarketLine(
                linea=pf_corners["linea"], over=pf_corners["over"], under=pf_corners["under"],
                is_dummy=pf_corners["is_dummy"], source_id=pf_corners["source_id"]
            )
        ),
        tarjetas=(
            _make_market(card_market.line, card_market.over, card_market.under)
            if card_market is not None
            else MarketLine(
                linea=pf_tarjetas["linea"], over=pf_tarjetas["over"], under=pf_tarjetas["under"],
                is_dummy=pf_tarjetas["is_dummy"], source_id=pf_tarjetas["source_id"]
            )
        ),
    )


def live_odds_source_text(args: argparse.Namespace) -> str:
    if getattr(args, "odds_source", "manual") == "sofascore":
        return "Cuotas: SofaScore"
    return "Cuotas: manuales tuyas"


def collect_live_markets(
    args: argparse.Namespace,
    snapshot: object,
    state: MatchState,
    previous_markets: MarketSet | None = None,
    pinnacle_fair: dict | None = None,
) -> MarketSet:
    snapshot_markets = build_markets_from_snapshot(snapshot, state, pinnacle_fair)
    if getattr(args, "odds_source", "manual") == "sofascore":
        return snapshot_markets

    defaults = previous_markets or snapshot_markets
    print("\nCUOTAS MANUALES")
    print("  Ingresa tus lineas y cuotas de la casa que estes usando.")
    print("  Enter mantiene el valor actual para ir rapido.")
    if previous_markets is None and (
        getattr(snapshot, "goals_market", None) is not None
        or getattr(snapshot, "corners_market", None) is not None
        or getattr(snapshot, "cards_market", None) is not None
    ):
        print("  Los valores por defecto vienen solo como referencia rapida del mercado live.")
    return pedir_markets(defaults)


def prematch_summary_text(prematch: PrematchContext | None) -> str:
    if prematch is None:
        return ""

    chunks = []
    if prematch.goal_total is not None:
        chunks.append(f"goles pre {prematch.goal_total:.2f}")
    if prematch.corner_total is not None:
        chunks.append(f"corners pre {prematch.corner_total:.2f}")
    if prematch.card_total is not None:
        chunks.append(f"tarjetas pre {prematch.card_total:.2f}")
    if prematch.goal_signal != 0.0:
        chunks.append(f"senal goles {prematch.goal_signal:+.2f}")
    if prematch.corner_signal != 0.0:
        chunks.append(f"senal corners {prematch.corner_signal:+.2f}")
    if prematch.card_signal != 0.0:
        chunks.append(f"senal tarjetas {prematch.card_signal:+.2f}")

    notes = list(getattr(prematch, "notes", ())[:2])
    note_text = f" | notas: {'; '.join(notes)}" if notes else ""
    if not chunks:
        return f"Prematch {prematch.source}{note_text}"
    return f"Prematch {prematch.source} -> " + " | ".join(chunks) + note_text


def blend_lambda_with_prematch(
    base_lambda: float,
    current_total: float,
    prematch_total: float | None,
    signal: float,
    minute: float,
    min_scale: float,
    max_scale: float,
) -> float:
    if prematch_total is None:
        if signal == 0.0:
            return base_lambda
        return base_lambda * (1.0 + (signal * 0.05))

    progress = clamp(minute / 90.0, 0.0, 1.0)
    blend_weight = clamp(0.30 - (0.18 * progress), 0.10, 0.30)
    prematch_remaining = max(0.0, prematch_total - current_total)
    prematch_remaining *= 1.0 + (signal * 0.08)
    blended_lambda = (base_lambda * (1.0 - blend_weight)) + (prematch_remaining * blend_weight)
    scale = blended_lambda / max(0.05, base_lambda)
    return base_lambda * clamp(scale, min_scale, max_scale)


def maybe_require_live_bridge() -> None:
    if SofaScoreMonitor is None:
        raise RuntimeError(
            "El modo SofaScore necesita futbol_live_bridge.py con Playwright disponible."
        )


def print_demo_list() -> None:
    print("\nEscenarios demo disponibles:")
    for key, scenario in DEMO_SCENARIOS.items():
        print(f"  {key}. {scenario.nombre} -> {scenario.descripcion}")


def maybe_default_prematch_xlsx() -> str | None:
    candidate = r"c:\Users\Gabo\Downloads\Futbol 2026.xlsx"
    try:
        with open(candidate, "rb"):
            return candidate
    except OSError:
        return None


def parse_watch_pref(raw_value: str, default_seconds: int) -> tuple[bool, int]:
    raw = raw_value.strip().lower()
    if not raw:
        return True, max(10, default_seconds)
    if raw in {"n", "no", "0"}:
        return False, max(10, default_seconds)
    if raw in {"s", "si", "y", "yes"}:
        return True, max(10, default_seconds)
    try:
        return True, max(10, int(float(raw)))
    except ValueError:
        return True, max(10, default_seconds)


def configure_interactive_mode(args: argparse.Namespace) -> None:
    has_explicit_mode = bool(
        args.demo
        or args.sofascore_url
        or args.stress_test
        or args.list_demos
    )
    session = load_session_state()
    if has_explicit_mode:
        if not args.prematch_xlsx and isinstance(session.get("prematch_xlsx"), str):
            args.prematch_xlsx = str(session["prematch_xlsx"])
        if args.odds_source == "manual" and isinstance(session.get("odds_source"), str):
            args.odds_source = str(session["odds_source"])
        if args.poll_seconds == 45 and session.get("poll_seconds") is not None:
            stored_poll = session.get("poll_seconds")
            if isinstance(stored_poll, (int, float)):
                args.poll_seconds = int(stored_poll)
        return

    if args.odds_source == "manual" and isinstance(session.get("odds_source"), str):
        args.odds_source = str(session["odds_source"])
    if args.poll_seconds == 45 and session.get("poll_seconds") is not None:
        stored_poll = session.get("poll_seconds")
        if isinstance(stored_poll, (int, float)):
            args.poll_seconds = int(stored_poll)

    last_link = str(session.get("last_sofascore_url") or "")
    args.sofascore_url = pedir_texto("Pega el link del partido de SofaScore", last_link or None)
    odds_choice = pedir_opcion("Cuotas desde manual o sofascore", {"manual", "sofascore"}, args.odds_source)
    args.odds_source = odds_choice
    watch_raw = pedir_texto(
        "Refresh automatico (Enter=s, n=no, o escribe segundos)",
        str(args.poll_seconds),
    )
    args.watch, args.poll_seconds = parse_watch_pref(watch_raw, int(args.poll_seconds))

    default_xlsx = maybe_default_prematch_xlsx()
    remembered_xlsx = str(session.get("prematch_xlsx") or "")
    if not args.prematch_xlsx and remembered_xlsx:
        args.prematch_xlsx = remembered_xlsx
    elif not args.prematch_xlsx and default_xlsx:
        use_xlsx = pedir_opcion("Usar archivo prematch detectado", {"s", "n"}, "s")
        if use_xlsx == "s":
            args.prematch_xlsx = default_xlsx
    elif not args.prematch_xlsx:
        manual_xlsx = pedir_texto("Ruta o link de Google Sheets prematch (Enter para omitir)")
        args.prematch_xlsx = manual_xlsx or None


def seleccionar_demo_interactivo() -> DemoScenario | None:
    print_demo_list()
    print("  4. manual -> cargar todo a mano")
    choice = pedir_opcion("Modo", {"1", "2", "3", "4"}, "1")
    if choice == "4":
        return None
    return DEMO_SCENARIOS[choice]


def pedir_match_state(defaults: MatchState | None = None) -> MatchState:
    d = defaults
    print("\n[1/3] Situacion actual")
    minuto = pedir_float("Minuto", d.minuto if d else None)
    goles_local = pedir_float("Goles local", d.goles_local if d else 0.0)
    goles_visitante = pedir_float("Goles visitante", d.goles_visitante if d else 0.0)
    amarillas = pedir_float("Amarillas", d.amarillas if d else 0.0)
    rojas = pedir_float("Rojas", d.rojas if d else 0.0)
    faltas = pedir_float("Faltas totales", d.faltas if d else 0.0)
    corners = pedir_float("Corners totales", d.corners if d else 0.0)

    print("\n[2/3] Presion real")
    xg_local = pedir_float("xG local", d.xg_local if d else 0.0)
    xg_visitante = pedir_float("xG visitante", d.xg_visitante if d else 0.0)
    tiros_local = pedir_float("Tiros totales local", d.tiros_local if d else 0.0)
    tiros_visitante = pedir_float("Tiros totales visitante", d.tiros_visitante if d else 0.0)
    tiros_puerta_local = pedir_float("Tiros a puerta local", d.tiros_puerta_local if d else 0.0)
    tiros_puerta_visitante = pedir_float("Tiros a puerta visitante", d.tiros_puerta_visitante if d else 0.0)
    posesion_local = pedir_float("Posesion local %", d.posesion_local if d else 50.0)

    return MatchState(
        minuto=minuto,
        goles_local=goles_local,
        goles_visitante=goles_visitante,
        amarillas=amarillas,
        rojas=rojas,
        faltas=faltas,
        corners=corners,
        xg_local=xg_local,
        xg_visitante=xg_visitante,
        tiros_local=tiros_local,
        tiros_visitante=tiros_visitante,
        tiros_puerta_local=tiros_puerta_local,
        tiros_puerta_visitante=tiros_puerta_visitante,
        posesion_local=float(posesion_local),
        urgency_multiplier=1.0,
        defensive_yellows=0.0
    )


def pedir_markets(defaults: MarketSet | None = None) -> MarketSet:
    d = defaults
    print("\n[3/3] Cuotas")
    goles = _make_market(
        pedir_float("Linea goles", d.goles.linea if d else None),
        pedir_float("Cuota over goles", d.goles.over if d else None),
        pedir_float("Cuota under goles", d.goles.under if d else None),
    )
    corners = _make_market(
        pedir_float("Linea corners", d.corners.linea if d else None),
        pedir_float("Cuota over corners", d.corners.over if d else None),
        pedir_float("Cuota under corners", d.corners.under if d else None),
    )
    tarjetas = _make_market(
        pedir_float("Linea tarjetas", d.tarjetas.linea if d else None),
        pedir_float("Cuota over tarjetas", d.tarjetas.over if d else None),
        pedir_float("Cuota under tarjetas", d.tarjetas.under if d else None),
    )
    return MarketSet(goles=goles, corners=corners, tarjetas=tarjetas)


def preparar_inputs(args: argparse.Namespace) -> tuple[MatchState, MarketSet, str]:
    if args.demo:
        scenario = DEMO_SCENARIOS[args.demo]
        if args.once:
            return scenario.match_state, scenario.markets, scenario.nombre
        print(f"\nUsando demo base: {scenario.nombre}")
        print(f"Descripcion: {scenario.descripcion}")
        return (
            pedir_match_state(scenario.match_state),
            pedir_markets(scenario.markets),
            scenario.nombre,
        )

    scenario = seleccionar_demo_interactivo()
    if scenario is None:
        if not args.league_name:
            league_name = pedir_texto("Liga/torneo (Enter = global)")
            args.league_name = league_name or None
        return pedir_match_state(), pedir_markets(), "manual"

    print(f"\nDemo seleccionado: {scenario.nombre}")
    return (
        pedir_match_state(scenario.match_state),
        pedir_markets(scenario.markets),
        scenario.nombre,
    )


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def normalize_script_weights(acceleration: float, cooldown: float) -> tuple[float, float, float]:
    acceleration = max(0.0, acceleration)
    cooldown = max(0.0, cooldown)
    total = acceleration + cooldown
    if total > 0.85:
        scale = 0.85 / total
        acceleration *= scale
        cooldown *= scale
    neutral = max(0.15, 1.0 - acceleration - cooldown)
    final_total = acceleration + cooldown + neutral
    return acceleration / final_total, cooldown / final_total, neutral / final_total


def infer_match_phase(
    state: MatchState,
    danger_rate: float,
    tension_index: float,
    total_xg: float,
    total_shots: float,
    score_diff: float,
) -> MatchPhase:
    if state.minuto < 18 and total_xg < 0.45 and total_shots < 6:
        return MatchPhase(
            nombre="estudio",
            resumen="Fase actual: partido de estudio, con poca informacion todavia.",
            goal_modifier=0.90,
            corner_modifier=0.92,
            card_modifier=0.86,
            open_bias=-0.04,
            cool_bias=0.08,
        )

    if state.minuto >= 75 and score_diff <= 1 and (danger_rate >= 0.18 or total_xg >= 2.0):
        return MatchPhase(
            nombre="cierre_roto",
            resumen="Fase actual: cierre roto, partido abierto y con espacio para mas caos.",
            goal_modifier=1.18,
            corner_modifier=1.10,
            card_modifier=1.10,
            open_bias=0.14,
            cool_bias=-0.05,
        )

    if state.rojas > 0 or state.amarillas >= 5 or tension_index >= 0.10:
        return MatchPhase(
            nombre="partido_caliente",
            resumen="Fase actual: partido caliente, con tension que puede romper el guion.",
            goal_modifier=1.04,
            corner_modifier=1.03,
            card_modifier=1.24,
            open_bias=0.06,
            cool_bias=-0.02,
        )

    if state.minuto >= 45 and score_diff >= 2:
        return MatchPhase(
            nombre="ventaja_controlada",
            resumen="Fase actual: ventaja amplia, con riesgo real de que el partido se enfrie.",
            goal_modifier=0.90,
            corner_modifier=1.02,
            card_modifier=0.90,
            open_bias=-0.03,
            cool_bias=0.12,
        )

    if state.minuto >= 55 and score_diff == 1:
        return MatchPhase(
            nombre="persecucion",
            resumen="Fase actual: un equipo persigue el marcador y eso suele empujar el ritmo.",
            goal_modifier=1.10,
            corner_modifier=1.14,
            card_modifier=1.02,
            open_bias=0.08,
            cool_bias=-0.03,
        )

    if danger_rate >= 0.18 and total_xg >= 1.50:
        return MatchPhase(
            nombre="ida_y_vuelta",
            resumen="Fase actual: partido de ida y vuelta, con transiciones y ritmo alto.",
            goal_modifier=1.14,
            corner_modifier=1.08,
            card_modifier=1.00,
            open_bias=0.10,
            cool_bias=-0.03,
        )

    return MatchPhase(
        nombre="estable",
        resumen="Fase actual: partido estable, sin una senal fuerte de ruptura o enfriamiento.",
        goal_modifier=1.00,
        corner_modifier=1.00,
        card_modifier=1.00,
        open_bias=0.00,
        cool_bias=0.00,
    )


def simulate_mixed_poisson(
    rng: np.random.Generator,
    sims: int,
    weights: tuple[float, float, float],
    scenario_lambdas: tuple[float, float, float],
    current_total: float,
    script_idx: np.ndarray | None = None,
) -> np.ndarray:
    if script_idx is None:
        script_idx = rng.choice(3, size=sims, p=weights)
    lambda_vector = np.choose(
        script_idx,
        [
            np.full(sims, scenario_lambdas[0]),
            np.full(sims, scenario_lambdas[1]),
            np.full(sims, scenario_lambdas[2]),
        ],
    )
    return rng.poisson(lambda_vector) + current_total


def simulate_mixed_nbinom(
    rng: np.random.Generator,
    sims: int,
    weights: tuple[float, float, float],
    scenario_lambdas: tuple[float, float, float],
    current_total: float,
    script_idx: np.ndarray,
    theta: float,
) -> np.ndarray:
    if theta <= 1e-9:
        return simulate_mixed_poisson(
            rng, sims, weights, scenario_lambdas, current_total, script_idx
        )
    lambda_vector = np.choose(
        script_idx,
        [
            np.full(sims, scenario_lambdas[0]),
            np.full(sims, scenario_lambdas[1]),
            np.full(sims, scenario_lambdas[2]),
        ],
    )
    lambda_vector = np.maximum(lambda_vector, 1e-6)
    p_nb = theta / (theta + lambda_vector)
    inc = nbinom.rvs(theta, p_nb, size=sims, random_state=rng)
    return inc.astype(np.float64) + current_total


def favorite_lead_persistence(prematch, state: MatchState) -> float:
    if prematch is None:
        return 1.0
    ho = getattr(prematch, "home_win_odds", None)
    aw = getattr(prematch, "away_win_odds", None)
    if ho is None or aw is None or ho <= 1.0 or aw <= 1.0:
        return 1.0
    ih, ia = 1.0 / ho, 1.0 / aw
    s = ih + ia
    ph, pa = ih / s, ia / s
    asym = clamp(max(ph, pa) - 0.5, 0.0, 0.45) / 0.45
    if ph > pa + 0.08:
        fav = 1
    elif pa > ph + 0.08:
        fav = -1
    else:
        return 1.0
    lead = state.goles_local - state.goles_visitante
    if fav == 1 and lead >= 2:
        return 1.0 + 0.18 * asym
    if fav == -1 and lead <= -2:
        return 1.0 + 0.18 * asym
    return 1.0


def late_card_spike_profile(state: MatchState, phase_name: str) -> tuple[float, float]:
    if state.minuto < 70:
        return 0.0, 0.0

    score_diff = abs(state.goles_local - state.goles_visitante)
    current_cards = state.amarillas + state.rojas
    fouls_rate = state.faltas / max(25.0, state.minuto)
    late_phase = clamp((state.minuto - 72.0) / 20.0, 0.0, 1.0)

    spike_prob = 0.04 + (0.05 * late_phase)
    spike_lambda = 0.75 + (0.35 * late_phase)

    if current_cards <= 1:
        spike_prob += 0.08
        spike_lambda += 0.18
    if current_cards >= 4:
        spike_prob += 0.04
        spike_lambda += 0.14
    if state.faltas >= 18 or fouls_rate >= 0.30:
        spike_prob += 0.05
    if score_diff <= 1:
        spike_prob += 0.06
        spike_lambda += 0.18
    if state.rojas > 0:
        spike_prob += 0.08
        spike_lambda += 0.24
    if phase_name in {"partido_caliente", "cierre_roto"}:
        spike_prob += 0.08
        spike_lambda += 0.22
    if state.minuto >= 84:
        spike_prob += 0.05
        spike_lambda += 0.20

    return clamp(spike_prob, 0.0, 0.34), clamp(spike_lambda, 0.20, 2.40)


def run_model(
    state: MatchState,
    markets: MarketSet,
    params: ModelParams,
    prematch: PrematchContext | None = None,
) -> ModelResult:
    total_goals_now = state.goles_local + state.goles_visitante
    extra_time = (
        params.base_extra_time
        + total_goals_now * 0.45
        + state.rojas * 1.60
        + state.amarillas * 0.06
    )
    remaining_minutes = max(1.0, (90.0 + extra_time) - state.minuto)

    rate_window = max(25.0, state.minuto)
    total_shots = state.tiros_local + state.tiros_visitante
    total_sot = state.tiros_puerta_local + state.tiros_puerta_visitante
    total_xg = state.xg_local + state.xg_visitante

    danger_rate = (
        (total_sot * params.shot_on_target_weight)
        + (total_shots * params.shot_weight)
    ) / rate_window
    xg_rate = total_xg / rate_window
    corners_rate = state.corners / rate_window
    fouls_rate = state.faltas / rate_window
    card_conversion = state.amarillas / max(1.0, state.faltas)
    cards_rate = state.amarillas / rate_window
    recent_window = max(1.0, state.ventana_reciente_min or 0.0)
    recent_xg_rate = (
        (state.xg_recientes / recent_window)
        if state.xg_recientes is not None and state.ventana_reciente_min is not None
        else xg_rate
    )
    recent_tiros_rate = (
        (state.tiros_recientes / recent_window)
        if state.tiros_recientes is not None and state.ventana_reciente_min is not None
        else (total_shots / rate_window)
    )
    recent_danger_rate = ((recent_tiros_rate * params.shot_weight) * 2.0)
    recent_fouls_rate = (
        (state.faltas_recientes / recent_window)
        if state.faltas_recientes is not None and state.ventana_reciente_min is not None
        else fouls_rate
    )
    recent_cards_rate = (
        (state.tarjetas_recientes / recent_window)
        if state.tarjetas_recientes is not None and state.ventana_reciente_min is not None
        else cards_rate
    )
    recent_corners_rate = (
        (state.corners_recientes / recent_window)
        if state.corners_recientes is not None and state.ventana_reciente_min is not None
        else corners_rate
    )
    effective_conversion = card_conversion
    if fouls_rate >= 0.30:
        effective_conversion = max(effective_conversion, 0.28)

    base_tension_index = (
        fouls_rate * params.card_fouls_weight
        + effective_conversion * params.card_conversion_weight
        + cards_rate * params.card_cards_weight
    )
    tension_index = base_tension_index
    if state.ventana_reciente_min is not None:
        recent_tension_index = (
            recent_fouls_rate * (params.card_fouls_weight * 1.35)
            + effective_conversion * params.card_conversion_weight
            + recent_cards_rate * (params.card_cards_weight * 1.60)
        )
        if recent_tension_index > (base_tension_index * 1.40):
            tension_index = (base_tension_index * 0.30) + (recent_tension_index * 0.70)
        else:
            tension_index = (base_tension_index * 0.72) + (recent_tension_index * 0.28)

    score_diff = abs(state.goles_local - state.goles_visitante)
    close_game = score_diff <= 1
    phase = infer_match_phase(
        state,
        danger_rate,
        tension_index=tension_index,
        total_xg=total_xg,
        total_shots=total_shots,
        score_diff=score_diff,
    )
    trailing_team_boost = 1.0
    if score_diff == 1 and state.minuto >= 55:
        trailing_team_boost = params.trailing_goal_boost

    possession_pressure = 1.0 + (abs(state.posesion_local - 50.0) / 50.0) * 0.06
    urgency_factor = 1.0
    if close_game and state.minuto >= 60:
        urgency_factor *= params.close_game_goal_boost
    if state.minuto >= 75:
        urgency_factor *= params.late_goal_boost
    if state.ventana_reciente_min is not None:
        if recent_xg_rate > (xg_rate * 1.5) and recent_tiros_rate > 0.4:
            urgency_factor *= 1.15
        elif recent_xg_rate > (xg_rate * 1.2):
            urgency_factor *= 1.05

    # FASE 1: Factor de desesperación (Tabla de Posiciones)
    urgency_factor *= state.urgency_multiplier

    # Guardrail Data Integrity: 
    # Si el proveedor no da xG (0.00) pero hay tiros, usamos un fallback basado en tiros.
    # Evita que el modelo se quede 'ciego' con UNDERs falsos (ej. Arsenal-City).
    if total_xg < 0.05 and total_shots >= 3:
        defensive_yel = getattr(state, "defensive_yellows", 0.0) or 0.0
        off_target_weight = 0.04 if defensive_yel > 0 else 0.02
        # Fallback: 0.10 xG por tiro a puerta, ajustamos desviados según "Cuerda Floja"
        fallback_xg = (state.tiros_puerta_local + state.tiros_puerta_visitante) * 0.10
        fallback_xg += (total_shots - (state.tiros_puerta_local + state.tiros_puerta_visitante)) * off_target_weight
        xg_rate = fallback_xg / max(1, state.minuto)

    touches_area = state.touches_in_box_home + state.touches_in_box_away
    ocasiones_falladas = state.big_chances_missed_home + state.big_chances_missed_away

    # Si hay TIB en la data, lo usamos como un bono de "penetración profunda" (Mayor peso ahora: x0.4)
    tib_bonus_home = max(0.0, state.touches_in_box_home - (params.siege_touch_threshold / 2)) * 0.4
    tib_bonus_away = max(0.0, state.touches_in_box_away - (params.siege_touch_threshold / 2)) * 0.4

    # El SIEGE base ahora usa: (Ataques Peligrosos / Minuto) * Posesión
    current_min = max(1.0, state.minuto)
    
    # Intensidad de ataque (ataques peligrosos por minuto)
    intensity_home = state.dangerous_attacks_home / current_min
    intensity_away = state.dangerous_attacks_away / current_min

    # ── Guardrail SIEGE: si dangerous_attacks=0 pero hay tiros o TIB, ──────
    # el modelo no puede quedar ciego. Calculamos presión mínima desde
    # posesión + tiros como proxy de intensidad táctica real.
    if intensity_home == 0.0 and intensity_away == 0.0:
        # Proxy de presión desde tiros por minuto + TIB
        shots_total = state.tiros_local + state.tiros_visitante
        if shots_total > 0:
            total_shots_rate = shots_total / current_min
            intensity_home = total_shots_rate * (state.posesion_local / 100.0) * 2.5
            intensity_away = total_shots_rate * ((100.0 - state.posesion_local) / 100.0) * 2.5
        elif state.touches_in_box_home > 0 or state.touches_in_box_away > 0:
            # Solo TIB disponible → presión mínima proporcional
            tib_total = max(1.0, state.touches_in_box_home + state.touches_in_box_away)
            intensity_home = (state.touches_in_box_home / tib_total) * 0.8
            intensity_away = (state.touches_in_box_away / tib_total) * 0.8
    
    # Factor de control (posesión)
    control_home = state.posesion_local / 50.0 # 1.0 = equilibrado, >1.0 = dominio
    control_away = (100.0 - state.posesion_local) / 50.0
    
    # Reducimos drásticamente el peso de Betano Dangerous Attacks y la Posesión (de 1.5 a 0.7)
    siege_base_home = intensity_home * control_home * 0.7
    siege_base_away = intensity_away * control_away * 0.7
    
    # Aumentamos x2.5 el peso del BCM de SofaScore para inyectar real asedio táctico
    bcm_home_bonus = state.big_chances_missed_home * (params.siege_missed_mult * 2.5)
    bcm_away_bonus = state.big_chances_missed_away * (params.siege_missed_mult * 2.5)

    siege_home = siege_base_home + tib_bonus_home + bcm_home_bonus
    siege_away = siege_base_away + tib_bonus_away + bcm_away_bonus
    siege_index = siege_home + siege_away # Combinado global para impactar Poissons

    
    siege_boost = 1.0 + clamp(
        math.expm1(siege_index * params.siege_expm1_scale), 0.0, params.siege_boost_cap
    )

    # Ritmo base genético lineal (por minuto)
    base_genetic = params.goal_baseline_per90 / 90.0
    
    # Decaimiento No Lineal Asimétrico (Late-Game Desperation Loop)
    if state.minuto >= 75.0 and abs(state.goles_local - state.goles_visitante) == 1:
        # Inyección del pico exponencial desde min 75 hasta fin del partido
        # Limitado (clamp) por seguridad para evitar infinito estadístico en alargues extremos
        late_panic_boost = clamp(math.exp((state.minuto - 75.0) * 0.08), 1.0, 4.5)
        base_genetic *= late_panic_boost

    goal_rate = (
        xg_rate * params.goal_xg_weight
        + danger_rate * params.goal_danger_weight
        + base_genetic * params.goal_baseline_weight
    ) * siege_boost
    decay_factor = 1.0
    if state.minuto >= 70:
        # A min 90 el factor es ~0.82 (caída del 18% por cansancio físico)
        decay_factor = clamp(1.0 - (state.minuto / 500.0), 0.80, 1.0)

    # --- FACTOR CUERDA FLOJA (Amonestados Defensivos) ---
    # Un defensa o centrocampista con amarilla entra en "Modo Pasivo":
    # no puede ir al suelo ni presionar agresivamente.
    # → El atacante rompe más hacia el área (Goles +15% por amonestado)
    # → El lateral deja cruzar el extremo sin tacklear (Corners +10% por amonestado)
    defensive_yel = getattr(state, "defensive_yellows", 0.0) or 0.0
    tightrope_goal_boost = clamp(1.0 + defensive_yel * 0.15, 1.0, 1.35)   # Techo 35% con 2+ amarillos
    tightrope_corner_boost = clamp(1.0 + defensive_yel * 0.10, 1.0, 1.25) # Techo 25%

    # --- MODELO ADITIVO (GOLES) ---
    base_goals = goal_rate * remaining_minutes * params.goal_multiplier
    momentum_goals = 0.0
    
    if trailing_team_boost > 1.0:
        momentum_goals += 0.15
    if possession_pressure > 1.0:
        momentum_goals += 0.10
    if urgency_factor > 1.0:
        momentum_goals += 0.15 * ((urgency_factor - 1.0) / 0.25)
    momentum_goals += (defensive_yel * 0.05)
    if siege_boost > 1.0:
        momentum_goals += min(0.35, (siege_boost - 1.0) * 0.5)

    lambda_goals = clamp(
        (base_goals + momentum_goals) * phase.goal_modifier * decay_factor,
        0.05,
        params.max_goal_lambda,
    )

    # 1. Simetría Dinámica (Corners): 
    # Si el ratio tiros/corners es muy bajo, el ataque es estéril o centralizado.
    # Escalamos penalización: de 1.0 (ratio 0.18) a 0.65 (ratio 0.05).
    corner_shot_ratio = (state.corners_local + state.corners_visitante) / max(1, total_shots)
    c_weight_adj = 1.0
    if state.minuto >= 25:
        if corner_shot_ratio < 0.18:
            # Penalización gradual: a menor ratio, menos importancia al danger_rate en corners
            c_weight_adj = clamp(0.65 + (corner_shot_ratio / 0.18) * 0.35, 0.65, 1.0)

    corner_rate = (
        corners_rate * params.corner_observed_weight
        + (danger_rate * c_weight_adj) * params.corner_danger_weight
        + (params.corner_baseline_per90 / 90.0) * params.corner_baseline_weight
    ) * siege_boost
    # --- MODELO ADITIVO (CORNERS) ---
    base_corners = corner_rate * remaining_minutes * params.corner_multiplier
    momentum_corners = 0.0
    
    # 1.5 Wing Pressure Factor (WPF) - Ataque Lateral / Presión
    centros_totales = state.centros_local + state.centros_visitante
    
    if centros_totales > 0:
        wpf_home = state.centros_local / max(1.0, state.tiros_local)
        wpf_away = state.centros_visitante / max(1.0, state.tiros_visitante)
    else:
        # Fallback de WPF basado en Ataques Peligrosos y Control
        wpf_home = (state.dangerous_attacks_home / max(1.0, state.tiros_local)) * (state.posesion_local / 100.0)
        wpf_away = (state.dangerous_attacks_away / max(1.0, state.tiros_visitante)) * ((100.0 - state.posesion_local) / 100.0)
    
    wpf = wpf_home + wpf_away

    if state.ventana_reciente_min is not None and recent_window > 0:
        wpf_reciente = state.centros_recientes / max(1.0, (state.tiros_recientes * 2.0))
        wpf = (wpf * 0.4) + (wpf_reciente * 0.6)
        
    if wpf > params.wpf_high:
        momentum_corners += 0.40
    elif wpf < params.wpf_low and state.minuto >= 30:
        momentum_corners -= 0.50 # Castigo a señales falsas
    
    # 1.6 Factor Cuerda Floja (Corners)
    momentum_corners += (defensive_yel * 0.10)
    
    # 2. Bono de Desesperación (Trailing Team)
    if (score_diff == 1 or score_diff == 0) and state.minuto >= 75:
        momentum_corners += 0.30
        if state.centros_recientes > 2.0:
            momentum_corners += 0.40

    # 3. Penalización por Empate Tardío (Conservadurismo)
    if state.goles_local == state.goles_visitante and state.minuto >= 85:
        momentum_corners -= 0.35

    # 4. Filtro de Posesión Estéril (REFINADO)
    tiro_sucio_ratio = total_shots / (total_xg + 0.1)
    if tiro_sucio_ratio > 18 and total_shots >= 8:
        momentum_corners += 0.40
    
    high_possession = state.posesion_local > 62 or state.posesion_local < 38
    if high_possession and state.ventana_reciente_min is not None:
        if recent_xg_rate < 0.10 and recent_tiros_rate < 0.15 and state.minuto >= 45:
             momentum_corners -= 0.40

    # 5. Bono de Asedio (Liderazgo por 2+)
    if score_diff >= 2 and state.minuto >= 70:
        momentum_corners += 0.45

    if state.minuto >= 70 and total_shots >= 16:
        momentum_corners += (params.late_corner_boost - 1.0) * 1.5
        
    if state.ventana_reciente_min is not None:
        if recent_xg_rate < (xg_rate * 0.7) and state.minuto >= 60:
            momentum_corners -= 0.25
        
        if recent_corners_rate >= 0.16:
            momentum_corners += 0.30
        elif recent_corners_rate >= 0.11:
            momentum_corners += 0.15

    lambda_corners = clamp(
        (base_corners + momentum_corners) * phase.corner_modifier * decay_factor,
        0.05,
        params.max_corner_lambda,
    )

    # --- MODELO ADITIVO (TARJETAS) ---
    card_rate = tension_index + (params.card_baseline_per90 / 90.0) * params.card_baseline_weight
    base_cards = card_rate * remaining_minutes * params.card_multiplier
    momentum_cards = 0.0

    # FASE 1: Factor de urgencia de tabla
    momentum_cards += (state.urgency_multiplier - 1.0) * 1.5

    # FASE 1: Factor de Agresividad Defensiva
    if state.defensive_yellows >= 4:
        momentum_cards += 0.45
    elif state.defensive_yellows >= 2:
        momentum_cards += 0.20

    if close_game:
        momentum_cards += (params.close_game_card_boost - 1.0) * 1.0
    if state.rojas > 0:
        momentum_cards += (params.red_card_tension_boost - 1.0) * 1.0

    # Curva cuadratica de escalada de tarjetas en el tramo final
    if state.minuto >= 60:
        endgame_progress = clamp((state.minuto - 60.0) / 30.0, 0.0, 1.0)
        surge_mult = 2.0 * (endgame_progress ** 2.5)
        momentum_cards += surge_mult
    elif state.minuto >= 45:
        momentum_cards += 0.15

    # Factor de desesperación
    current_cards = state.amarillas + state.rojas
    if state.minuto >= 80 and close_game and current_cards <= 2:
        momentum_cards += 0.25

    # En juegos muy fisicos, escalar por tasa de faltas
    if fouls_rate >= 0.35:
        momentum_cards += 0.35
    elif fouls_rate >= 0.30:
        momentum_cards += 0.18

    # Factor de desesperación y arbitrariedad
    if fouls_rate >= 0.38 and current_cards <= 2 and state.minuto >= 50:
        momentum_cards += 0.50 

    if state.ventana_reciente_min is not None:
        if recent_fouls_rate >= 0.24:
            momentum_cards += 0.15
        elif recent_fouls_rate >= 0.18:
            momentum_cards += 0.08
        if recent_cards_rate >= 0.08:
            momentum_cards += 0.20
        elif recent_cards_rate >= 0.04:
            momentum_cards += 0.10

    base_expected_cards = (base_cards + momentum_cards) * phase.card_modifier * decay_factor
    
    # MATEMATICA DEL RIESGO: Piso de Peligro al final del partido.
    if state.minuto >= 85 and close_game:
        base_expected_cards = max(base_expected_cards, 0.65)
        
    lambda_cards = clamp(
        base_expected_cards,
        0.05,
        params.max_card_lambda,
    )

    # En el segundo tiempo mezclamos dos posibles guiones:
    # 1) el partido se abre y acelera
    # 2) el partido se enfria y cae el ritmo
    second_half_progress = clamp((state.minuto - 45.0) / 45.0, 0.0, 1.0)
    late_phase = clamp((state.minuto - 70.0) / 20.0, 0.0, 1.0)

    acceleration_weight = 0.0
    cooldown_weight = 0.0
    if second_half_progress > 0:
        if score_diff <= 1:
            acceleration_weight += 0.16 + (0.10 * second_half_progress)
        elif score_diff == 2:
            acceleration_weight += 0.07 + (0.05 * second_half_progress)

        acceleration_weight += clamp((danger_rate - 0.16) * 1.60, 0.0, 0.14)
        if state.ventana_reciente_min is not None:
            acceleration_weight += clamp((recent_danger_rate - danger_rate) * 2.50, 0.0, 0.12)
        acceleration_weight += clamp((tension_index - 0.07) * 2.00, 0.0, 0.10)
        acceleration_weight += 0.08 * late_phase

        if score_diff >= 2:
            cooldown_weight += 0.18 + (0.12 * second_half_progress)
        if state.minuto >= 60 and danger_rate < 0.16:
            cooldown_weight += 0.10
        if state.minuto >= 70 and total_xg < 1.40:
            cooldown_weight += 0.08
        if state.ventana_reciente_min is not None and recent_xg_rate < (xg_rate * 0.5):
            cooldown_weight += 0.12
        if state.amarillas == 0 and fouls_rate < 0.32:
            cooldown_weight += 0.06

    acceleration_weight += phase.open_bias
    cooldown_weight += phase.cool_bias
    acceleration_weight, cooldown_weight, neutral_weight = normalize_script_weights(
        acceleration_weight,
        cooldown_weight,
    )

    goal_open_multiplier = 1.18 if close_game else 1.10
    goal_cool_multiplier = 0.78 if score_diff >= 2 else 0.87
    corner_open_multiplier = 1.18 if state.minuto >= 60 else 1.12
    corner_cool_multiplier = 0.92 if score_diff >= 2 else 0.95
    card_open_multiplier = 1.18 if (close_game or state.rojas > 0 or state.amarillas >= 4) else 1.08
    card_cool_multiplier = 0.72 if state.amarillas == 0 else 0.84

    fav_relief = favorite_lead_persistence(prematch, state)
    goal_open_lambda = clamp(lambda_goals * goal_open_multiplier, 0.05, params.max_goal_lambda)
    goal_cool_lambda = clamp(lambda_goals * goal_cool_multiplier * fav_relief, 0.05, params.max_goal_lambda)
    goal_neutral_lambda = lambda_goals

    corner_open_lambda = clamp(lambda_corners * corner_open_multiplier, 0.05, params.max_corner_lambda)
    corner_cool_lambda = clamp(lambda_corners * corner_cool_multiplier * fav_relief, 0.05, params.max_corner_lambda)
    corner_neutral_lambda = lambda_corners

    card_open_lambda = clamp(lambda_cards * card_open_multiplier, 0.05, params.max_card_lambda)
    card_cool_lambda = clamp(lambda_cards * card_cool_multiplier, 0.05, params.max_card_lambda)
    card_neutral_lambda = lambda_cards

    # REGLA 3: Ajuste de Lambdas por Roja (Modo Emergencia)
    if state.rojas > 0:
        goal_neutral_lambda *= 0.80  # Caida ofensiva general
        card_neutral_lambda *= 1.15  # Aumento de tension


    # Muy temprano, el partido todavia da poca evidencia real.
    if state.minuto < 15:
        goal_open_lambda *= 0.94
        goal_cool_lambda *= 0.94
        goal_neutral_lambda *= 0.94
    if state.minuto < 15 and state.corners <= 1 and total_shots < 6:
        corner_open_lambda *= 0.84
        corner_cool_lambda *= 0.84
        corner_neutral_lambda *= 0.84
    if state.minuto < 20:
        if state.amarillas == 0:
            card_open_lambda *= 0.50
            card_cool_lambda *= 0.50
            card_neutral_lambda *= 0.50
        elif state.amarillas == 1:
            card_open_lambda *= 0.72
            card_cool_lambda *= 0.72
            card_neutral_lambda *= 0.72
    if state.minuto < 12 and state.faltas < 5:
        card_open_lambda *= 0.80
        card_cool_lambda *= 0.80
        card_neutral_lambda *= 0.80

    if prematch is not None:
        current_goals = state.goles_local + state.goles_visitante
        current_cards = state.amarillas + state.rojas
        goal_scale = blend_lambda_with_prematch(
            lambda_goals,
            current_goals,
            prematch.goal_total,
            prematch.goal_signal,
            state.minuto,
            0.82,
            1.22,
        ) / max(0.05, lambda_goals)
        corner_scale = blend_lambda_with_prematch(
            lambda_corners,
            state.corners,
            prematch.corner_total,
            prematch.corner_signal,
            state.minuto,
            0.82,
            1.24,
        ) / max(0.05, lambda_corners)
        card_scale = blend_lambda_with_prematch(
            lambda_cards,
            current_cards,
            prematch.card_total,
            prematch.card_signal,
            state.minuto,
            0.80,
            1.25,
        ) / max(0.05, lambda_cards)

        goal_open_lambda *= goal_scale
        goal_cool_lambda *= goal_scale
        goal_neutral_lambda *= goal_scale
        corner_open_lambda *= corner_scale
        corner_cool_lambda *= corner_scale
        corner_neutral_lambda *= corner_scale
        card_open_lambda *= card_scale
        card_cool_lambda *= card_scale
        card_neutral_lambda *= card_scale

    weights = (acceleration_weight, cooldown_weight, neutral_weight)
    lambda_goals = (
        (goal_open_lambda * acceleration_weight)
        + (goal_cool_lambda * cooldown_weight)
        + (goal_neutral_lambda * neutral_weight)
    )
    lambda_corners = (
        (corner_open_lambda * acceleration_weight)
        + (corner_cool_lambda * cooldown_weight)
        + (corner_neutral_lambda * neutral_weight)
    )
    lambda_cards = (
        (card_open_lambda * acceleration_weight)
        + (card_cool_lambda * cooldown_weight)
        + (card_neutral_lambda * neutral_weight)
    )

    rng = np.random.default_rng(params.seed)
    script_idx = rng.choice(3, size=params.sims, p=weights)
    total_goals = simulate_mixed_poisson(
        rng,
        params.sims,
        weights,
        (goal_open_lambda, goal_cool_lambda, goal_neutral_lambda),
        total_goals_now,
        script_idx,
    )
    total_corners = simulate_mixed_nbinom(
        rng,
        params.sims,
        weights,
        (corner_open_lambda, corner_cool_lambda, corner_neutral_lambda),
        state.corners,
        script_idx,
        params.corner_nb_theta,
    )
    total_cards = simulate_mixed_nbinom(
        rng,
        params.sims,
        weights,
        (card_open_lambda, card_cool_lambda, card_neutral_lambda),
        state.amarillas + (state.rojas * 2),
        script_idx,
        params.card_nb_theta,
    )
    card_spike_prob, card_spike_lambda = late_card_spike_profile(state, phase.nombre)
    if card_spike_prob > 0.0:
        spike_mask = rng.random(params.sims) < card_spike_prob
        if np.any(spike_mask):
            total_cards = total_cards + (rng.poisson(card_spike_lambda, size=params.sims) * spike_mask)
        lambda_cards += card_spike_prob * card_spike_lambda

    return ModelResult(
        state=state,
        markets=markets,
        params=params,
        phase_name=phase.nombre,
        phase_summary=phase.resumen,
        remaining_minutes=remaining_minutes,
        danger_rate=danger_rate,
        tension_index=tension_index,
        urgency_factor=urgency_factor * trailing_team_boost,
        lambda_goals=lambda_goals,
        lambda_corners=lambda_corners,
        lambda_cards=lambda_cards,
        acceleration_weight=acceleration_weight,
        cooldown_weight=cooldown_weight,
        neutral_weight=neutral_weight,
        total_goals=total_goals,
        total_corners=total_corners,
        total_cards=total_cards,
        wpf_home=wpf_home,
        wpf_away=wpf_away,
        siege_home=siege_home,
        siege_away=siege_away,
        tightrope_boost=tightrope_goal_boost,
    )


def no_bet_decision(decision: MarketDecision, note: str) -> MarketDecision:
    return replace(
        decision,
        best_side="NO BET",
        best_prob=0.0,
        best_stake=0.0,
        note=note,
    )


def market_current_total(market_name: str, state: MatchState) -> float:
    if market_name == "GOLES":
        return state.goles_local + state.goles_visitante
    if market_name == "CORNERS":
        return state.corners
    return state.amarillas + (state.rojas * 2)


def events_needed_for_over(linea: float, current_total: float) -> int:
    current_int = int(round(current_total))
    target_total = int(np.floor(linea + 1e-9)) + 1
    return max(0, target_total - current_int)


def apply_market_guardrails(
    market_name: str,
    decision: MarketDecision,
    state: MatchState,
    params: ModelParams,
    odds_history: dict = None,
    corner_ctx: dict = None,
) -> MarketDecision:
    """
    Aplica reglas de negocio o 'guardrails' duros para vetar apuestas que son muy peligrosas
    estadísticamente, independientemente del lambda de Poisson puro.
    Incluye 'Follow the Money' (Smart Money Tracker).
    """
    score_diff = abs(state.goles_local - state.goles_visitante)
    
    # -------------------------------------------------------------
    # FASE 2: SMART MONEY RADAR (FOLLOW THE MONEY)
    # -------------------------------------------------------------
    if odds_history and decision.best_side != "NO BET":
        history = odds_history.get(market_name, [])
        if len(history) >= 2:
            # Buscar el registro más viejo en la ventana de 5 min y el más actual
            oldest = history[0]
            newest = history[-1]
            
            # Solo comparar si hablan de la misma línea asiatica (para no confundir a la IA si cambió de 2.5 a 3.0)
            if oldest["linea"] == newest["linea"] and newest["linea"] > 0:
                dt_minutes = (newest["timestamp"] - oldest["timestamp"]) / 60.0
                
                # Desplome violento de la cuota OVER
                if oldest["over"] > 1.0 and newest["over"] > 1.0:
                    roc_over = (newest["over"] - oldest["over"]) / oldest["over"]
                    if roc_over <= -0.15: # Cayó un 15%
                        if decision.best_side == "UNDER":
                            return no_bet_decision(decision, f"⚠️ SMART MONEY: Cuota OVER se desplomó un {abs(roc_over*100):.1f}% en {dt_minutes:.1f} min. UNDER Cancelado.")
                        elif decision.best_side == "OVER":
                            decision = replace(decision, note=decision.note + f" | 🐋 SMART MONEY A FAVOR: OVER cayó {abs(roc_over*100):.1f}%.")
                            
                # Desplome violento de la cuota UNDER
                if oldest["under"] > 1.0 and newest["under"] > 1.0:
                    roc_under = (newest["under"] - oldest["under"]) / oldest["under"]
                    if roc_under <= -0.15:
                        if decision.best_side == "OVER":
                            return no_bet_decision(decision, f"⚠️ SMART MONEY: Cuota UNDER se desplomó un {abs(roc_under*100):.1f}% en {dt_minutes:.1f} min. OVER Cancelado.")
                        elif decision.best_side == "UNDER":
                            decision = replace(decision, note=decision.note + f" | 🐋 SMART MONEY A FAVOR: UNDER cayó {abs(roc_under*100):.1f}%.")

    total_shots = state.tiros_local + state.tiros_visitante
    total_sot = state.tiros_puerta_local + state.tiros_puerta_visitante
    total_xg = state.xg_local + state.xg_visitante
    total_cards_now = state.amarillas + (state.rojas * 2)
    total_goals_now = state.goles_local + state.goles_visitante
    current_total = market_current_total(market_name, state)
    score_diff = abs(state.goles_local - state.goles_visitante)
    remaining_minutes = max(
        1.0,
        (
            90.0
            + params.base_extra_time
            + total_goals_now * 0.45
            + state.rojas * 1.60
            + state.amarillas * 0.06
        ) - state.minuto,
    )
    note = decision.note

    # REGLA 0: Stale Line / Resolved Market
    if decision.best_side != "NO BET" and decision.linea > 0 and current_total > decision.linea:
        return no_bet_decision(decision, f"Linea caducada o resuelta: el total actual ({current_total}) ya supero la linea ({decision.linea}).")

    # REGLA 1: Lock-Out Period Inicial (Silencio Estadistico 0-20')
    if state.minuto < 20.0:
        return no_bet_decision(decision, "Fase de silencio (min < 20): esperando estabilidad estadistica.")

    # REGLA 1.5: Lock-Out Period Final (Bloqueo 85+ para Corners/Tarjetas)
    if state.minuto >= 85.0 and market_name in ("CORNERS", "TARJETAS"):
        return no_bet_decision(decision, f"Fin de partido (min >= 85): Mercado de {market_name} bloqueado por riesgo extremo y volatilidad VAR.")

    if decision.best_side != "NO BET" and decision.best_prob < 0.60:
        return no_bet_decision(decision, "La ventaja del modelo es demasiado pequena.")

    if market_name == "GOLES":
        # ── PODA ESTRICTA: OVER Goles ──
        # Subir el límite base de probabilidad para filtrar escenarios mediocres.
        if decision.best_side == "OVER" and decision.best_prob < 0.65:
            return no_bet_decision(decision, "Probabilidad de OVER Goles insuficiente (< 65%). Se requiere mayor certeza (Edge) para arriesgar.")
            
        if state.minuto < 12 and total_shots < 4 and total_xg < 0.35 and decision.best_side != "NO BET":
            decision = replace(
                decision,
                best_stake=min(decision.best_stake, 0.015),
                note="Poco ritmo todavia: lectura util, pero con cautela.",
            )
        if state.minuto >= 70 and decision.best_side == "OVER":
            needed_events = events_needed_for_over(decision.linea, current_total)
            strong_open_context = (
                (score_diff <= 1 and state.rojas == 0) # Si hay roja, el contexto 'abierto' se invalida por seguridad
                or total_xg >= 2.40
                or total_shots >= 24
                or total_sot >= 7
            )
            # REGLA 3: Filtro de Correlacion Negativa tras Roja (Tardio)
            if state.rojas > 0 and state.minuto >= 70:
                 return no_bet_decision(decision, "Modo Emergencia: con tarjeta roja en el tramo final, priorizo el Under por seguridad tactica.")

            if current_total <= decision.linea and needed_events <= 1:
                if decision.future_mean < 0.95 or decision.ev_over < (params.edge_threshold + 0.04):
                    return no_bet_decision(
                        decision,
                        "Solo falta un gol para el over, pero el mercado ya descuenta buena parte de esa cercania.",
                    )
                if not strong_open_context and state.minuto >= 75:
                    return no_bet_decision(
                        decision,
                        "Solo queda un gol para cobrar, pero no veo suficiente empuje nuevo para justificarlo.",
                    )
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.01),
                    note="Mercado muy pegado a la linea: mantengo el over solo con stake muy pequeno.",
                )

    if market_name == "CORNERS":
        # ── PODA ESTRICTA: OVER Corners ──
        # El modelo DEBE exigir un número mínimo innegociable de Tiros a Puerta y Centros.
        if decision.best_side == "OVER":
            total_centros = getattr(state, 'centros_local', 0) + getattr(state, 'centros_visitante', 0)
            if total_sot < 3 or total_centros < 4:
                 return no_bet_decision(decision, f"Falso Positivo de Corners: Faltan llegadas peligrosas de verdad (Tiros Puerta: {total_sot}, Centros: {total_centros}). OVER Cancelado.")
        if state.minuto < 15 and state.corners <= 1 and total_shots < 6:
            if decision.best_side == "UNDER" and decision.best_prob >= 0.78:
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.015),
                    note="Corners aun verdes: solo iria muy suave.",
                )
            else:
                return no_bet_decision(decision, "Muy temprano para confiar en corners.")

        # ── GUARDRAIL: FILTRO DE TECHO (Ceiling Filter) ────────────────────────
        # Si el marcador de corners ya se acercó demasiado a la línea en la
        # recta final, el OVER tiene un riesgo asimétricamente alto:
        # la línea ya se "cargó" pero el partido puede no producir más corners.
        if (
            decision.best_side == "OVER"
            and state.minuto > 70
            and decision.linea > 0
            and current_total >= (decision.linea - 1.5)
        ):
            return no_bet_decision(
                decision,
                f"🚧 TECHO DE CORNERS: min {state.minuto:.0f}' con {current_total:.0f} corners actuales "
                f"vs linea {decision.linea} — el Over ya está demasiado cargado para el tiempo restante.",
            )

        # ── GUARDRAIL: FILTRO DE ESCALADA (Escalation Lock) ────────────────────
        # Si la última apuesta registrada fue un OVER a una línea >= la actual y
        # el marcador de corners no ha aumentado desde entonces, bloqueamos.
        # Esto evita el efecto "zigzag" donde el modelo sigue escalando la línea
        # sin que el partido produzca corners nuevos que lo justifiquen.
        if corner_ctx is not None and decision.best_side == "OVER":
            last = corner_ctx.get("last_over_bet")  # {"linea": float, "corners_at_bet": float}
            if last is not None:
                corners_since_last_bet = current_total - last["corners_at_bet"]
                line_escalated = decision.linea > last["linea"]
                if line_escalated and corners_since_last_bet <= 0:
                    return no_bet_decision(
                        decision,
                        f"🔒 ESCALADA BLOQUEADA: la linea subio de {last['linea']} a {decision.linea} "
                        f"pero no han caido nuevos corners desde la ultima apuesta OVER "
                        f"(corners actuales: {current_total:.0f}).",
                    )
        if state.minuto >= 75 and decision.best_side == "OVER":
            needed_events = events_needed_for_over(decision.linea, current_total)
            strong_corner_context = state.corners >= 8 or total_shots >= 20 or score_diff <= 1
            if current_total <= decision.linea and needed_events <= 1:
                if decision.future_mean < 1.25 or decision.ev_over < (params.edge_threshold + 0.03):
                    return no_bet_decision(
                        decision,
                        "Solo falta un corner, pero el mercado ya tiene bastante descontada esa cercania.",
                    )
                if not strong_corner_context and state.minuto >= 80:
                    return no_bet_decision(
                        decision,
                        "Solo queda un corner para cobrar, pero el ritmo ya no empuja tanto ese over.",
                    )
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.012),
                    note="Solo falta un corner: dejo el over, pero con stake muy controlado.",
                )
        if decision.best_side == "UNDER":
            needed_events = events_needed_for_over(decision.linea, current_total)
            corner_under_risk = 0
            if remaining_minutes >= 24:
                corner_under_risk += 1
            if needed_events <= 4:
                corner_under_risk += 1
            if decision.future_mean >= max(2.40, needed_events - 1.0):
                corner_under_risk += 1
            if total_shots >= 16 or total_sot >= 6:
                corner_under_risk += 1
            if score_diff <= 1 or (score_diff == 2 and state.minuto < 75):
                corner_under_risk += 1
            if state.corners_recientes is not None and state.corners_recientes >= 1:
                corner_under_risk += 1

            if remaining_minutes >= 24 and needed_events <= 4 and corner_under_risk >= 3:
                return no_bet_decision(
                    decision,
                    "Under corners aun fragil: queda tiempo suficiente para que salgan 3-4 corners mas.",
                )
            if needed_events <= 4 and corner_under_risk >= 2:
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.01),
                    note="Under corners expuesto: con este tiempo restante, 3-4 corners mas siguen siendo muy posibles.",
                )

    if market_name == "TARJETAS":
        # REGLA 2: Filtro de Hostilidad (Foul Acceleration) y Volatilidad estricta
        if decision.best_side == "UNDER":
            if state.faltas_recientes >= 3 and state.ventana_reciente_min > 0 and state.ventana_reciente_min <= 5:
                return no_bet_decision(decision, "Volatilidad de Faltas: >=3 faltas en <5 min. UNDER bloqueado.")
                
            # MURO CIEGO (FIN DE PARTIDO Y MARCADOR APRETADO)
            # Evaluar la temperatura el partido para decidir donde poner el muro
            muro_minuto = 82
            if hasattr(state, 'tension_index') and getattr(state, 'tension_index', 0) > 0.12:
                muro_minuto = 78 # Adelantamos el muro si hay mucha friccion
                
            if state.minuto >= muro_minuto and score_diff <= 1:
                return no_bet_decision(decision, f"NO BET: Volatilidad de fin de partido ({state.minuto}'). Muro Ciego activado para UNDER de Tarjetas.")
            
            # PRE-AVISO DE TENSION (minuto 73+)
            if state.minuto >= 73 and state.minuto < muro_minuto and score_diff <= 1 and decision.best_stake > 0.01:
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.01), # Corte bestial del stake
                    note=f"Pre-Aviso de Tension ({state.minuto}'): El UNDER entra en zona roja. Reduccion preventiva del stake.",
                )

        foul_rate_global = state.faltas / max(1.0, state.minuto)
        foul_acceleration = 0.0
        if state.ventana_reciente_min > 0:
            foul_acceleration = state.faltas_recientes / state.ventana_reciente_min
        
        is_hostile = foul_rate_global > 0.35 or (foul_acceleration > 0.50 and state.ventana_reciente_min >= 8.0)
        
        if is_hostile and decision.best_side == "UNDER":
             return no_bet_decision(decision, f"Hostilidad alta ({foul_rate_global:.2f} f/min): bloqueo el Under de tarjetas.")

        if state.minuto < 25 and total_cards_now <= 1:
            if decision.best_side != "NO BET":
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.01),
                    note="Mercado de tarjetas aun inmaduro: ir muy pequeno o pasar.",
                )
        if state.minuto < 60 and decision.best_side == "UNDER" and decision.best_stake > 0.02:
            decision = replace(
                decision,
                best_stake=0.02,
                note="Antes del 60': aunque el under guste, no lo trato como entrada fuerte.",
            )
        if decision.best_side == "UNDER" and decision.linea <= 1.5 and state.minuto >= 55:
            low_line_risk = 0
            if state.minuto >= 65:
                low_line_risk += 1
            if total_shots >= 14 or total_xg >= 1.50:
                low_line_risk += 1
            if score_diff <= 1:
                low_line_risk += 1
            if state.faltas >= 6:
                low_line_risk += 1
            if low_line_risk >= 2:
                return no_bet_decision(
                    decision,
                    "Linea 1.5 de tarjetas demasiado fragil para este tramo del partido: prefiero pasar.",
                )
            decision = replace(
                decision,
                best_stake=min(decision.best_stake, 0.01),
                note="Linea 1.5 de tarjetas: aunque guste el under, solo pisaria muy pequeno.",
            )
        if state.minuto >= 70 and decision.best_side == "OVER":
            needed_events = events_needed_for_over(decision.linea, current_total)
            strong_card_context = total_cards_now >= 4 or state.faltas >= 22 or state.rojas > 0 or score_diff <= 1
            if current_total <= decision.linea and needed_events <= 1:
                if decision.future_mean < 0.90 or decision.ev_over < (params.edge_threshold + 0.04):
                    return no_bet_decision(
                        decision,
                        "Solo falta una tarjeta para el over, pero el mercado ya cobra caro esa cercania.",
                    )
                if not strong_card_context and state.minuto >= 75:
                    return no_bet_decision(
                        decision,
                        "Solo queda una tarjeta para cobrar, pero no hay suficiente calor nuevo para entrar.",
                    )
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.01),
                    note="Solo falta una tarjeta: mantengo el over solo con stake muy pequeno.",
                )
        if state.minuto >= 70 and decision.best_side == "UNDER":
            needed_events = events_needed_for_over(decision.linea, current_total)
            late_under_risk = 0
            if score_diff <= 1:
                late_under_risk += 1
            if state.faltas >= 18:
                late_under_risk += 1
            if total_cards_now >= 3:
                late_under_risk += 1
            if state.minuto >= 84:
                late_under_risk += 1
            if total_xg >= 2.20 or total_shots >= 20:
                late_under_risk += 1

            if total_cards_now <= 1:
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.02),
                    note="Cierre tardio: aunque van pocas tarjetas, aun puede aparecer caos al final.",
                )
            if score_diff <= 1 or state.faltas >= 22:
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.015),
                    note="Final de partido: el under tarjetas pierde fuerza por posible tension de cierre.",
                )
            if needed_events <= 2:
                if late_under_risk >= 2 and state.minuto >= 78:
                    return no_bet_decision(
                        decision,
                        "Under tarjetas demasiado expuesto: una rafaga final de 1-2 tarjetas te rompe facil.",
                    )
                decision = replace(
                    decision,
                    best_stake=min(decision.best_stake, 0.01),
                    note="Under expuesto a una rafaga corta de tarjetas al final: si entro, es muy pequeno.",
                )
            if score_diff <= 1 and total_cards_now <= 1 and state.faltas >= 20:
                return no_bet_decision(decision, "Final inestable para under tarjetas: prefiero pasar.")

    if decision.best_side == "NO BET" and not decision.note:
        decision = replace(decision, note=note)
    return decision


def market_summary(
    market_name: str,
    lambda_val: float,
    market: MarketLine,
    params: ModelParams,
    state: MatchState,
    pinnacle_fair: dict | None = None,
) -> MarketDecision:
    current_total = market_current_total(market_name, state)
    mean_total = current_total + lambda_val
    
    eventos_necesarios = market.linea - current_total
    
    if eventos_necesarios < 0:
        prob_over = 1.0
        prob_under = 0.0
        prob_push = 0.0
    else:
        goles_necesarios_over = math.ceil(eventos_necesarios)
        # Probabilidad de superar (goles_necesarios_over - 1)
        prob_over = float(poisson.sf(goles_necesarios_over - 1, lambda_val))
        
        if eventos_necesarios.is_integer():
            prob_push = float(poisson.pmf(int(eventos_necesarios), lambda_val))
        else:
            prob_push = 0.0
            
        prob_under = max(0.0, 1.0 - prob_over - prob_push)

    fair_over = (1.0 / prob_over) if prob_over > 0 else float("inf")
    fair_under = (1.0 / prob_under) if prob_under > 0 else float("inf")
    
    # --- True Odds (Método Proporcional) ---
    prob_impl_over = 1.0 / market.over if market.over > 0 else 0.0
    prob_impl_under = 1.0 / market.under if market.under > 0 else 0.0
    margin = prob_impl_over + prob_impl_under
    
    true_prob_market_over = prob_impl_over / margin if margin > 0 else 0.0
    true_prob_market_under = prob_impl_under / margin if margin > 0 else 0.0
    
    # Edge real contra el mercado limpio
    true_edge_over = prob_over / true_prob_market_over if true_prob_market_over > 0 else 0.0
    true_edge_under = prob_under / true_prob_market_under if true_prob_market_under > 0 else 0.0

    # EV Tradicional (para display y Kelly)
    ev_over = prob_over * market.over
    ev_under = prob_under * market.under

    # --- Kelly Adaptativo (Anclado en Pinnacle o Softbook) ---
    if pinnacle_fair and pinnacle_fair.get("linea") == market.linea:
        raw_over = pinnacle_fair.get("over", market.over)
        raw_under = pinnacle_fair.get("under", market.under)
        ref_over, ref_under = fair_odds_for_live_total_market(
            market_name, raw_over, raw_under
        )
        kelly_over  = max(0.0, (ref_over  * prob_over  - 1.0) / max(0.01, ref_over  - 1.0))
        kelly_under = max(0.0, (ref_under * prob_under - 1.0) / max(0.01, ref_under - 1.0))
        stake_over  = min(params.max_stake, kelly_over  * params.kelly_fraction)
        stake_under = min(params.max_stake, kelly_under * params.kelly_fraction)
    else:
        # Sin Faro: usar Kelly conservador sobre la cuota de la softbook
        kelly_over = max(0.0, (market.over * prob_over - 1.0) / max(0.01, market.over - 1.0))
        kelly_under = max(0.0, (market.under * prob_under - 1.0) / max(0.01, market.under - 1.0))
        
        safe_fraction = params.kelly_fraction * 0.5
        stake_over  = min(params.max_stake, kelly_over * safe_fraction) if true_edge_over  >= params.edge_threshold else 0.0
        stake_under = min(params.max_stake, kelly_under * safe_fraction) if true_edge_under >= params.edge_threshold else 0.0

    best_side = "NO BET"
    best_ev = max(ev_over, ev_under)
    best_prob = 0.0
    best_stake = 0.0
    if true_edge_over >= true_edge_under and true_edge_over >= params.edge_threshold:
        best_side = "OVER"
        best_prob = prob_over
        best_stake = stake_over
    elif true_edge_under > true_edge_over and true_edge_under >= params.edge_threshold:
        best_side = "UNDER"
        best_prob = prob_under
        best_stake = stake_under

    # Con el modelo Poisson exacto, el EV es determinístico — no hay P25 estocástico.
    # Aplicamos el mismo umbral directamente al EV exacto calculado.
    best_ev_p25 = best_ev
    note = ""
    if params.ev_p25_min > 0.0 and best_side in ("OVER", "UNDER") and best_ev_p25 < params.ev_p25_min:
        best_side = "NO BET"
        best_ev = max(ev_over, ev_under)
        best_prob = 0.0
        best_stake = 0.0
        note = (
            f"EV exacto={best_ev_p25:.3f} < umbral {params.ev_p25_min:.2f}: señal insuficiente."
        )

    return MarketDecision(
        linea=market.linea,
        prob_over=prob_over,
        prob_under=prob_under,
        prob_push=prob_push,
        fair_over=fair_over,
        fair_under=fair_under,
        ev_over=ev_over,
        ev_under=ev_under,
        best_side=best_side,
        best_ev=best_ev,
        best_prob=best_prob,
        best_stake=best_stake,
        mean_total=mean_total,
        future_mean=max(0.0, mean_total - current_total),
        note=note,
        best_ev_p25=best_ev_p25,
    )


def confidence_label(probability: float) -> str:
    if probability >= 0.80:
        return "alta"
    if probability >= 0.65:
        return "media"
    return "baja"


def stake_label(stake: float) -> str:
    if stake >= 0.03:
        return "fuerte"
    if stake >= 0.015:
        return "suave"
    if stake > 0:
        return "muy suave"
    return "sin apuesta"


def recommendation_text(decision: MarketDecision) -> str:
    if decision.best_side == "NO BET":
        return "PASAR"
    return f"{decision.best_side} {decision.linea}"


def current_count_note(nombre: str, decision: MarketDecision, current_total: float) -> str:
    market_name = nombre.upper()
    if decision.best_side == "NO BET":
        return ""

    remaining_to_over = decision.linea - current_total
    margin_to_under_break = decision.linea - current_total

    if decision.best_side == "OVER":
        if market_name == "CORNERS" and remaining_to_over <= 2.5:
            return (
                f"Esta lectura sale bastante por inercia del conteo actual: ya van {current_total:.0f} "
                f"y solo faltan unos {max(0.0, remaining_to_over):.1f} para pasar la linea."
            )
        if market_name == "GOLES" and remaining_to_over <= 1.5:
            return (
                f"Esta lectura depende bastante de que ya estas muy cerca de la linea "
                f"con el marcador actual ({current_total:.0f}); el mercado suele descontar parte de esa cercania."
            )
        if market_name == "TARJETAS" and remaining_to_over <= 1.5:
            return (
                f"Esta lectura tambien viene por inercia del conteo actual: la linea ya esta muy cerca."
            )

    if decision.best_side == "UNDER":
        if market_name == "TARJETAS" and current_total <= max(1.0, decision.linea - 2.0):
            return (
                f"Esta lectura se apoya en que el conteo actual sigue bajo: van {current_total:.0f} "
                f"y aun hay buen margen frente a la linea."
            )
        if market_name == "CORNERS" and current_total <= max(2.0, decision.linea - 4.0):
            return (
                f"Esta lectura sale en parte porque el conteo actual sigue lejos de la linea."
            )

    return ""


def reason_text(nombre: str, decision: MarketDecision, current_total: float) -> str:
    if decision.best_side == "NO BET":
        return decision.note or "No hay ventaja clara ahora mismo."

    text = f"Probabilidad {decision.best_prob:.0%} y confianza {confidence_label(decision.best_prob)}."
    if decision.note:
        text = f"{text} {decision.note}"
    count_note = current_count_note(nombre, decision, current_total)
    if count_note:
        text = f"{text} {count_note}"
    if current_total > decision.linea:
        text = f"{text} Ojo: la linea ya queda por debajo del total actual."
    return text


def _taken_odds_for(decision: MarketDecision, market: MarketLine | None) -> dict | None:
    """Captura la cuota real disponible cuando el modelo emite una señal de BET.
    Usado para calcular Closing Line Value (CLV) en el análisis post-partido.
    Si best_side es NO BET, devuelve None (no hubo apuesta).
    """
    if decision.best_side == "NO BET" or market is None:
        return None
    if decision.best_side == "OVER":
        return {"side": "OVER", "odds": market.over, "linea": market.linea}
    if decision.best_side == "UNDER":
        return {"side": "UNDER", "odds": market.under, "linea": market.linea}
    return None


def append_live_history(
    history_dir: Path,
    snapshot: object,
    state: MatchState,
    markets: MarketSet,
    args: argparse.Namespace,
    league_profile: LeagueProfile,
    prematch: PrematchContext | None,
    result: ModelResult,
    pinnacle_fair: dict | None = None,
    match_ctx: dict | None = None,
    latency_ms: float | None = None,
) -> Path | None:
    captured_local = datetime.now().astimezone()
    history_path = history_file_path(history_dir, snapshot)

    # ── Filtro anti-zombie: rechazar ticks del descanso/inicio-2T ─────────────
    # SofaScore reporta minuto=0 con todo en cero durante el intermedio.
    # Estos ticks son ruido puro para el ML; no tienen valor estadístico.
    if (
        state.minuto == 0.0
        and state.tiros_local == 0.0
        and state.faltas == 0.0
        and state.xg_local == 0.0
        and state.corners == 0.0
    ):
        return None  # Tick del descanso – no escribir


    goals_decision = apply_market_guardrails(
        "GOLES",
        market_summary("GOLES", result.lambda_goals, markets.goles, result.params, state),
        state,
        result.params,
    )
    corner_ctx = match_ctx.get("corner_ctx", {}) if match_ctx else None

    corners_decision = apply_market_guardrails(
        "CORNERS",
        market_summary("CORNERS", result.lambda_corners, markets.corners, result.params, state),
        state,
        result.params,
        corner_ctx=corner_ctx,
    )
    # ── Actualizar Filtro de Escalada tras cada decisión OVER de corners ───────
    if corner_ctx is not None and corners_decision.best_side == "OVER":
        corner_ctx["last_over_bet"] = {
            "linea": corners_decision.linea,
            "corners_at_bet": float(state.corners),
        }
    cards_decision = apply_market_guardrails(
        "TARJETAS",
        market_summary("TARJETAS", result.lambda_cards, markets.tarjetas, result.params, state),
        state,
        result.params,
    )

    # ── Calcular Tendencia y Actualizar Buffer ─────────────────────────────
    odds_trend = {"goles": "stable", "corners": "stable", "tarjetas": "stable"}
    if match_ctx and "odds_history_buffer" in match_ctx:
        buffer = match_ctx["odds_history_buffer"]
        for m_key, mk_obj in (("goles", markets.goles), ("corners", markets.corners), ("tarjetas", markets.tarjetas)):
            if mk_obj.over > 1.0:
                q = buffer.get(m_key, [])
                q.append(mk_obj.over)
                if len(q) > 5:
                    q.pop(0)
                if len(q) >= 2:
                    if q[-1] > q[0]:
                        odds_trend[m_key] = "up"
                    elif q[-1] < q[0]:
                        odds_trend[m_key] = "down"

    # ── Faro Residual ──────────────────────────────────────────────────────
    residual_fair = None
    if match_ctx is not None:
        if pinnacle_fair is not None:
            match_ctx["last_sharp_line"] = pinnacle_fair
            residual_fair = pinnacle_fair
        else:
            residual_fair = match_ctx.get("last_sharp_line")
    else:
        residual_fair = pinnacle_fair

    payload = {
        "record_type": "snapshot",
        "captured_at_local": captured_local.isoformat(),
        "captured_at_utc": captured_local.astimezone(timezone.utc).isoformat(),
        "match": {
            "event_id": getattr(snapshot, "event_id", None),
            "match_url": getattr(snapshot, "match_url", None),
            "home_team": getattr(snapshot, "home_team", None),
            "away_team": getattr(snapshot, "away_team", None),
            "tournament": getattr(snapshot, "tournament", None),
            "tournament_slug": getattr(snapshot, "tournament_slug", None),
            "category_name": getattr(snapshot, "category_name", None),
            "country_name": getattr(snapshot, "country_name", None),
            "status_text": getattr(snapshot, "status_text", None),
        },
        "snapshot": {
            "state": asdict(state),
            "source_markets": {
                "goals": live_market_to_dict(getattr(snapshot, "goals_market", None)),
                "corners": live_market_to_dict(getattr(snapshot, "corners_market", None)),
                "cards": live_market_to_dict(getattr(snapshot, "cards_market", None)),
            },
            "notes": list(getattr(snapshot, "notes", ()) or ()),
        },
        "used_markets": {
            "odds_source": args.odds_source,
            "latency_ms": latency_ms,
            "goals": {**market_line_to_dict(markets.goles), "odds_trend": odds_trend["goles"]},
            "corners": {**market_line_to_dict(markets.corners), "odds_trend": odds_trend["corners"]},
            "cards": {**market_line_to_dict(markets.tarjetas), "odds_trend": odds_trend["tarjetas"]},
        },
        "league_profile": asdict(league_profile),
        "prematch": prematch_to_dict(prematch),
        "model": {
            "preset": result.params.nombre,
            "phase_name": result.phase_name,
            "phase_summary": result.phase_summary,
            "remaining_minutes": result.remaining_minutes,
            "danger_rate": result.danger_rate,
            "tension_index": result.tension_index,
            "urgency_factor": result.urgency_factor,
            "lambda_goals": result.lambda_goals,
            "lambda_corners": result.lambda_corners,
            "lambda_cards": result.lambda_cards,
            "weights": {
                "open": result.acceleration_weight,
                "cool": result.cooldown_weight,
                "neutral": result.neutral_weight,
            },
            "means": {
                "goals": (state.goles_local + state.goles_visitante) + result.lambda_goals,
                "corners": state.corners + result.lambda_corners,
                "cards": (state.amarillas + (state.rojas * 2)) + result.lambda_cards,
            },
            "decisions": {
                "goals": asdict(goals_decision),
                "corners": asdict(corners_decision),
                "cards": asdict(cards_decision),
            },
            "taken_odds": {
                "goals": _taken_odds_for(goals_decision, markets.goles),
                "corners": _taken_odds_for(corners_decision, markets.corners),
                "cards": _taken_odds_for(cards_decision, markets.tarjetas),
            },
            "pinnacle_baseline": residual_fair,
            "micro_stats": {
                "wpf_home": result.wpf_home,
                "wpf_away": result.wpf_away,
                "siege_home": result.siege_home,
                "siege_away": result.siege_away,
                "tightrope_boost": result.tightrope_boost,
            }
        },
        # ── Features derivadas para ML ───────────────────────────────────────
        # Los targets de entrenamiento son los "eventos restantes" (lambda).
        # Los ratios normalizan el momento del partido (independiente del minuto).
        "derived_features": {
            "score_diff": float(state.goles_local - state.goles_visitante),
            "red_card_diff": float(state.rojas_local - state.rojas_visitante),
            "xg_total": round(state.xg_local + state.xg_visitante, 3),
            "xg_diff": round(state.xg_local - state.xg_visitante, 3),
            "total_shots": float(state.tiros_local + state.tiros_visitante),
            "shot_efficiency": round(
                (state.tiros_puerta_local + state.tiros_puerta_visitante)
                / max(1.0, state.tiros_local + state.tiros_visitante),
                3,
            ),
            "foul_rate_per_min": round(
                state.faltas / max(1.0, state.minuto), 3
            ),
            "corner_rate_per_min": round(
                state.corners / max(1.0, state.minuto), 3
            ),
            # Targets ML: cuántos eventos quedan proyectados por el modelo
            "goals_remaining": round(result.lambda_goals, 4),
            "corners_remaining": round(result.lambda_corners, 4),
            "cards_remaining": round(result.lambda_cards, 4),
        },
    }

    try:
        history_dir.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:
        return None
    return history_path


def append_bet_record(
    history_dir: Path,
    snapshot: object,
    state: MatchState,
    bets: dict[str, dict],
) -> Path | None:
    """
    Registra SOLO las apuestas reales que pasaron el Sniper Lock y todos los filtros.
    Se guarda como record_type='bet' en el mismo archivo JSONL del partido.
    Por diseño, este record NO se usa para el settlement (solo para auditoría).
    El settlement se computa a partir de los records 'snapshot'.
    """
    captured_local = datetime.now().astimezone()
    history_path = history_file_path(history_dir, snapshot)

    payload = {
        "record_type": "bet",
        "captured_at_local": captured_local.isoformat(),
        "captured_at_utc": captured_local.astimezone(timezone.utc).isoformat(),
        "match": {
            "event_id": getattr(snapshot, "event_id", None),
            "home_team": getattr(snapshot, "home_team", None),
            "away_team": getattr(snapshot, "away_team", None),
            "status_text": getattr(snapshot, "status_text", None),
        },
        "minute": state.minuto,
        "score": f"{int(state.goles_local)}-{int(state.goles_visitante)}",
        "bets": {
            mn.lower(): {
                "side": d.get("best_side"),
                "linea": d.get("linea"),
                "stake": d.get("best_stake"),
                "ev": d.get("best_ev"),
                "prob": d.get("best_prob"),
                "odds": d.get("raw_over") if d.get("best_side") == "OVER" else d.get("raw_under"),
                "note": d.get("note", ""),
            }
            for mn, d in bets.items()
        },
    }

    try:
        history_dir.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=True) + "\n")
    except OSError:
        return None
    return history_path


def market_line_from_dict(payload: dict[str, object]) -> MarketLine:
    return MarketLine(
        linea=float(payload.get("line") or payload.get("linea") or 0.0),
        over=float(payload.get("over") or 0.0),
        under=float(payload.get("under") or 0.0),
        is_dummy=bool(payload.get("is_dummy", False)),
    )


def market_decision_from_dict(payload: dict[str, object]) -> MarketDecision:
    return MarketDecision(
        linea=float(payload.get("linea") or payload.get("line") or 0.0),
        prob_over=float(payload.get("prob_over") or 0.0),
        prob_under=float(payload.get("prob_under") or 0.0),
        prob_push=float(payload.get("prob_push") or 0.0),
        fair_over=float(payload.get("fair_over") or 0.0),
        fair_under=float(payload.get("fair_under") or 0.0),
        ev_over=float(payload.get("ev_over") or 0.0),
        ev_under=float(payload.get("ev_under") or 0.0),
        best_side=str(payload.get("best_side") or "NO BET"),
        best_ev=float(payload.get("best_ev") or 0.0),
        best_prob=float(payload.get("best_prob") or 0.0),
        best_stake=float(payload.get("best_stake") or 0.0),
        mean_total=float(payload.get("mean_total") or 0.0),
        future_mean=float(payload.get("future_mean") or 0.0),
        note=str(payload.get("note") or ""),
        best_ev_p25=float(payload.get("best_ev_p25") or 0.0),
    )


def summarize_accumulator(acc: StressAccumulator) -> dict[str, float | int]:
    losses = max(0, acc.bets - acc.wins)
    roi = (acc.profit / acc.stake) if acc.stake > 0 else 0.0
    hit_rate = (acc.wins / acc.bets) if acc.bets > 0 else 0.0
    return {
        "bets": acc.bets,
        "wins": acc.wins,
        "losses": losses,
        "stake": round(acc.stake, 6),
        "profit": round(acc.profit, 6),
        "roi": round(roi, 6),
        "hit_rate": round(hit_rate, 6),
    }


def append_match_closure(
    history_dir: Path,
    snapshot: object,
    state: MatchState,
) -> Path | None:
    history_path = history_file_path(history_dir, snapshot)
    if history_path.exists():
        try:
            existing_rows = [
                json.loads(line)
                for line in history_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        except (OSError, json.JSONDecodeError):
            existing_rows = []
        if any(row.get("record_type") == "match_closure" for row in existing_rows if isinstance(row, dict)):
            return history_path
    else:
        existing_rows = []

    final_goals = state.goles_local + state.goles_visitante
    final_corners = state.corners
    final_cards = state.amarillas + state.rojas
    totals_map = {
        "goals": final_goals,
        "corners": final_corners,
        "cards": final_cards,
    }
    label_map = {
        "goals": "goles",
        "corners": "corners",
        "cards": "tarjetas",
    }

    accumulators = {
        "goles": StressAccumulator(),
        "corners": StressAccumulator(),
        "tarjetas": StressAccumulator(),
        "overall": StressAccumulator(),
    }
    settled_snapshots: list[dict[str, object]] = []

    # ── Determinar modo de settlement ───────────────────────────────────────
    # Los archivos nuevos contienen records 'bet' (apuestas reales post-sniper).
    # Los archivos antiguos solo tienen 'snapshot' — se liquidan en modo legacy.
    bet_rows = [r for r in existing_rows if isinstance(r, dict) and r.get("record_type") == "bet"]
    use_bet_records = len(bet_rows) > 0

    if use_bet_records:
        # ── Modo Moderno: liquidar solo las apuestas reales ──────────────────
        mkt_key_map = {"goles": "goals", "corners": "corners", "tarjetas": "cards"}
        for row in bet_rows:
            bets_payload = row.get("bets") or {}
            minute = float(row.get("minute") or 0.0)
            snapshot_settlements: dict[str, object] = {}

            for mkt_label, bet_data in bets_payload.items():
                if not isinstance(bet_data, dict):
                    continue
                side = str(bet_data.get("side") or "NO BET")
                if side not in ("OVER", "UNDER"):
                    continue
                linea = float(bet_data.get("linea") or 0.0)
                stake = float(bet_data.get("stake") or 0.0)
                if stake <= 0 or linea <= 0:
                    continue

                # Determinar qué total real usar
                raw_key = mkt_key_map.get(mkt_label, "goals")
                actual_total = totals_map.get(raw_key, 0.0)

                # Resultado: OVER gana si actual > linea; UNDER si actual < linea; empate = push (no-bet)
                if side == "OVER":
                    won = actual_total > linea
                    push = actual_total == linea
                else:
                    won = actual_total < linea
                    push = actual_total == linea

                if push:
                    profit = 0.0
                elif won:
                    # Recuperar cuota desde EV = prob * decimal_odds → odds = ev / prob
                    _ev = float(bet_data.get("ev") or 0.0)
                    _prob = float(bet_data.get("prob") or 0.0)
                    decimal_odds = (_ev / _prob) if _prob > 0 else 1.9
                    profit = round(stake * (decimal_odds - 1.0), 6)
                else:
                    profit = round(-stake, 6)

                bucket = accumulators[mkt_label]
                bucket.bets += 1
                bucket.stake += stake
                bucket.profit += profit
                if won:
                    bucket.wins += 1

                accumulators["overall"].bets += 1
                accumulators["overall"].stake += stake
                accumulators["overall"].profit += profit
                if won:
                    accumulators["overall"].wins += 1

                snapshot_settlements[mkt_label] = {
                    "decision": f"{side} {linea}",
                    "linea": linea,
                    "stake": round(stake, 6),
                    "profit": round(profit, 6),
                    "resultado": "win" if won else "loss",
                    "actual_total": actual_total,
                }

            if snapshot_settlements:
                settled_snapshots.append({
                    "captured_at_local": row.get("captured_at_local"),
                    "minute": minute,
                    "settlements": snapshot_settlements,
                })

    else:
        # ── Modo Legacy: liquidar todos los snapshots (compatibilidad hacia atrás) ──
        for row in existing_rows:
            if not isinstance(row, dict):
                continue
            if row.get("record_type", "snapshot") != "snapshot":
                continue
            used_markets = row.get("used_markets")
            model = row.get("model")
            snapshot_payload = row.get("snapshot")
            if not isinstance(used_markets, dict) or not isinstance(model, dict) or not isinstance(snapshot_payload, dict):
                continue
            decisions = model.get("decisions")
            state_payload = snapshot_payload.get("state")
            if not isinstance(decisions, dict) or not isinstance(state_payload, dict):
                continue

            snapshot_settlements: dict[str, object] = {}
            for key in ["goals", "corners", "cards"]:
                market_payload = used_markets.get(key)
                decision_payload = decisions.get(key)
                if not isinstance(market_payload, dict) or not isinstance(decision_payload, dict):
                    continue

                market = market_line_from_dict(market_payload)
                decision = market_decision_from_dict(decision_payload)
                stake, profit, won = settle_bet(decision, market, totals_map[key])
                if stake <= 0:
                    continue

                bucket = accumulators[label_map[key]]
                bucket.bets += 1
                bucket.stake += stake
                bucket.profit += profit
                if won:
                    bucket.wins += 1

                _prob = decision.best_prob
                if _prob <= 0.0:
                    _prob = decision.prob_over if decision.best_side == "OVER" else decision.prob_under
                
                push = (totals_map[key] == market.linea)
                if not push:
                    brier_score = (_prob - (1.0 if won else 0.0)) ** 2
                    bucket.brier_sum += brier_score
                    bucket.brier_count += 1

                accumulators["overall"].bets += 1
                accumulators["overall"].stake += stake
                accumulators["overall"].profit += profit
                if won:
                    accumulators["overall"].wins += 1

                if not push:
                    accumulators["overall"].brier_sum += brier_score
                    accumulators["overall"].brier_count += 1

                snapshot_settlements[label_map[key]] = {
                    "decision": recommendation_text(decision),
                    "linea": decision.linea,
                    "stake": round(stake, 6),
                    "profit": round(profit, 6),
                    "resultado": "win" if won else "loss",
                    "actual_total": totals_map[key],
                }

            if snapshot_settlements:
                settled_snapshots.append(
                    {
                        "captured_at_local": row.get("captured_at_local"),
                        "minute": float(state_payload.get("minuto") or 0.0),
                        "settlements": snapshot_settlements,
                    }
                )

    calibration = {
        "goles_brier": round(accumulators["goles"].brier_sum / accumulators["goles"].brier_count, 4) if accumulators["goles"].brier_count > 0 else 0.0,
        "corners_brier": round(accumulators["corners"].brier_sum / accumulators["corners"].brier_count, 4) if accumulators["corners"].brier_count > 0 else 0.0,
        "tarjetas_brier": round(accumulators["tarjetas"].brier_sum / accumulators["tarjetas"].brier_count, 4) if accumulators["tarjetas"].brier_count > 0 else 0.0,
        "overall_brier": round(accumulators["overall"].brier_sum / accumulators["overall"].brier_count, 4) if accumulators["overall"].brier_count > 0 else 0.0,
    }

    closure_payload = {
        "record_type": "match_closure",
        "captured_at_local": datetime.now().astimezone().isoformat(),
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "match": {
            "event_id": getattr(snapshot, "event_id", None),
            "match_url": getattr(snapshot, "match_url", None),
            "home_team": getattr(snapshot, "home_team", None),
            "away_team": getattr(snapshot, "away_team", None),
            "tournament": getattr(snapshot, "tournament", None),
            "status_text": getattr(snapshot, "status_text", None),
        },
        "final_snapshot": {
            "state": asdict(state),
            "notes": list(getattr(snapshot, "notes", ()) or ()),
        },
        "final_totals": {
            "goals": final_goals,
            "corners": final_corners,
            "cards": final_cards,
        },
        "settlement_summary": {
            "snapshots_evaluated": sum(1 for row in existing_rows if isinstance(row, dict) and row.get("record_type", "snapshot") == "snapshot"),
            "snapshots_with_bets": len(settled_snapshots),
            "goles": summarize_accumulator(accumulators["goles"]),
            "corners": summarize_accumulator(accumulators["corners"]),
            "tarjetas": summarize_accumulator(accumulators["tarjetas"]),
            "overall": summarize_accumulator(accumulators["overall"]),
            "calibration": calibration,
        },
        "settled_snapshots": settled_snapshots,
    }

    try:
        history_dir.mkdir(parents=True, exist_ok=True)
        with history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(closure_payload, ensure_ascii=True) + "\n")
            
        # Etiquetado Automático (Shadow Mode v2)
        overall_profit = closure_payload["settlement_summary"]["overall"]["profit"]
        overall_bets = closure_payload["settlement_summary"]["overall"]["bets"]
        if overall_bets > 0:
            # Tolerancia de empate tecnico para profit muy cero (+- 0.001)
            if overall_profit > 0.001:
                prefix = "[WIN]_"
            elif overall_profit < -0.001:
                prefix = "[LOSS]_"
            else:
                prefix = "[PUSH]_"
                
            new_path = history_path.parent / f"{prefix}{history_path.name}"
            if not new_path.exists():
                history_path.rename(new_path)
                return new_path
                
    except OSError:
        return None
    return history_path

def script_text(result: ModelResult) -> str:
    state = result.state
    if state.minuto < 45:
        return "Lectura del guion: en primer tiempo pesa mas la fase actual que el cierre."

    open_pct = round(result.acceleration_weight * 100)
    cool_pct = round(result.cooldown_weight * 100)
    neutral_pct = round(result.neutral_weight * 100)

    if result.acceleration_weight >= result.cooldown_weight + 0.12:
        return (
            f"Lectura del segundo tiempo: el modelo cree mas en que el partido se abra "
            f"({open_pct}%) que en que se enfrie ({cool_pct}%)."
        )
    if result.cooldown_weight >= result.acceleration_weight + 0.12:
        return (
            f"Lectura del segundo tiempo: el modelo cree mas en que el partido se enfrie "
            f"({cool_pct}%) que en que se abra ({open_pct}%)."
        )
    return (
        f"Lectura del segundo tiempo: partido dividido entre abrirse ({open_pct}%) "
        f"y enfriarse ({cool_pct}%), con una zona media de {neutral_pct}%."
    )


def overall_call(decisions: list[tuple[str, MarketDecision]]) -> str:
    active = [(name, decision) for name, decision in decisions if decision.best_side != "NO BET"]
    if not active:
        return "Mejor decision: PASAR en todos los mercados por ahora."

    best_name, best_decision = max(active, key=lambda item: item[1].best_stake)
    return (
        f"Mejor decision: {recommendation_text(best_decision)} en {best_name} "
        f"con entrada {stake_label(best_decision.best_stake)}."
    )


def print_simple_market_block(
    nombre: str,
    summary: MarketDecision,
    mean_value: float,
    current_total: float,
) -> None:
    print(f"\n{nombre}")
    print(f"  Conteo actual: {current_total:.0f} | Proyeccion final: {mean_value:.2f} | Linea: {summary.linea}")
    print(f"  Decision: {recommendation_text(summary)}")
    if summary.best_side != "NO BET":
        print(f"  Entrada: {stake_label(summary.best_stake)} ({summary.best_stake:.1%} de banca)")
    print(f"  Motivo: {reason_text(nombre, summary, current_total)}")


def print_advanced_market_block(
    nombre: str,
    summary: MarketDecision,
    mean_value: float,
    current_total: float,
) -> None:
    print(f"\n{nombre}")
    print(
        "  media proyectada "
        f"{mean_value:.2f} | over {summary.prob_over:.1%} @ justa {summary.fair_over:.2f}"
        f" | under {summary.prob_under:.1%} @ justa {summary.fair_under:.2f}"
        f" | push {summary.prob_push:.1%}"
    )
    print(
        f"  EV over {summary.ev_over:.3f} | EV under {summary.ev_under:.3f}"
        f" | sugerencia {summary.best_side}"
    )
    if summary.best_side != "NO BET":
        print(
            f"  stake sugerido {summary.best_stake:.1%}"
            f" | prob modelo {summary.best_prob:.1%}"
            f" | EV objetivo {summary.best_ev:.3f}"
        )
    if current_total > summary.linea:
        print(
            f"  aviso: la linea {summary.linea} ya esta por debajo del total actual "
            f"({current_total:.2f}); revisa si la cuota sigue vigente."
        )


def print_live_source_block(
    snapshot: object,
    prematch: PrematchContext | None,
    odds_source_text: str,
    league_profile: LeagueProfile,
) -> None:
    print("\nFUENTE LIVE")
    print(
        f"  SofaScore | {getattr(snapshot, 'tournament')} | "
        f"{getattr(snapshot, 'home_team')} vs {getattr(snapshot, 'away_team')} | "
        f"estado {getattr(snapshot, 'status_text')}"
    )
    print(f"  {odds_source_text}")
    print(
        f"  Perfil liga: {league_profile.label} | "
        f"bases 90m -> goles {league_profile.goal_baseline_per90:.2f}, "
        f"corners {league_profile.corner_baseline_per90:.2f}, tarjetas {league_profile.card_baseline_per90:.2f}"
    )
    summary = prematch_summary_text(prematch)
    if summary:
        print(f"  {summary}")
    for note in getattr(snapshot, "notes", ())[:3]:
        print(f"  Nota: {note}")


def split_label(total: float, home: float | None, away: float | None) -> str:
    if home is not None and away is not None:
        return f"{total:.0f} ({home:.0f}-{away:.0f})"
    return f"{total:.0f}"


def print_report(result: ModelResult, label: str, advanced: bool = False) -> None:
    state = result.state
    params = result.params
    corners_text = split_label(state.corners, state.corners_local, state.corners_visitante)
    fouls_text = split_label(state.faltas, state.faltas_local, state.faltas_visitante)
    yellows_text = split_label(state.amarillas, state.amarillas_local, state.amarillas_visitante)
    reds_text = split_label(state.rojas, state.rojas_local, state.rojas_visitante)
    print("\n" + "=" * 72)
    print(f"MODELO LIVE v22 | preset={params.nombre} | escenario={label}")
    print("=" * 72)
    print(
        f"min {state.minuto:.0f} | marcador {state.goles_local:.0f}-{state.goles_visitante:.0f}"
        f" | corners {corners_text} | faltas {fouls_text}"
        f" | amarillas {yellows_text} | rojas {reds_text}"
    )
    print(
        f"xG {state.xg_local:.2f}-{state.xg_visitante:.2f}"
        f" | tiros {state.tiros_local:.0f}-{state.tiros_visitante:.0f}"
        f" | puerta {state.tiros_puerta_local:.0f}-{state.tiros_puerta_visitante:.0f}"
        f" | posesion local {state.posesion_local:.0f}%"
    )
    goals_summary = apply_market_guardrails(
        "GOLES",
        market_summary("GOLES", result.lambda_goals, result.markets.goles, params, state),
        state,
        params,
    )
    corners_summary = apply_market_guardrails(
        "CORNERS",
        market_summary("CORNERS", result.lambda_corners, result.markets.corners, params, state),
        state,
        params,
    )
    cards_summary = apply_market_guardrails(
        "TARJETAS",
        market_summary("TARJETAS", result.lambda_cards, result.markets.tarjetas, params, state),
        state,
        params,
    )

    print("\nRESUMEN SIMPLE")
    print(f"  {result.phase_summary}")
    print(f"  {script_text(result)}")
    print(f"  {overall_call([('goles', goals_summary), ('corners', corners_summary), ('tarjetas', cards_summary)])}")
    print("  Entrada fuerte = mejor señal. Entrada muy suave = solo si quieres ir con mucha cautela.")

    print_simple_market_block(
        "GOLES",
        goals_summary,
        (state.goles_local + state.goles_visitante) + result.lambda_goals,
        state.goles_local + state.goles_visitante,
    )
    print_simple_market_block(
        "CORNERS",
        corners_summary,
        state.corners + result.lambda_corners,
        state.corners,
    )
    print_simple_market_block(
        "TARJETAS",
        cards_summary,
        (state.amarillas + (state.rojas * 2)) + result.lambda_cards,
        state.amarillas + (state.rojas * 2),
    )

    if advanced:
        print("\nDETALLE TECNICO")
        print(
            f"  restante estimado {result.remaining_minutes:.1f} min"
            f" | danger_rate {result.danger_rate:.3f}"
            f" | tension {result.tension_index:.3f}"
            f" | urgencia {result.urgency_factor:.3f}"
        )
        print(
            f"  guion 2T -> se abre {result.acceleration_weight:.1%}"
            f" | se enfria {result.cooldown_weight:.1%}"
            f" | neutro {result.neutral_weight:.1%}"
        )
        print(
            f"  lambdas futuros -> goles {result.lambda_goals:.2f}"
            f" | corners {result.lambda_corners:.2f}"
            f" | tarjetas {result.lambda_cards:.2f}"
        )
        print_advanced_market_block(
            "GOLES",
            goals_summary,
            (state.goles_local + state.goles_visitante) + result.lambda_goals,
            state.goles_local + state.goles_visitante,
        )
        print_advanced_market_block(
            "CORNERS",
            corners_summary,
            state.corners + result.lambda_corners,
            state.corners,
        )
        print_advanced_market_block(
            "TARJETAS",
            cards_summary,
            (state.amarillas + (state.rojas * 2)) + result.lambda_cards,
            state.amarillas + (state.rojas * 2),
        )
    print("=" * 72)


def placeholder_markets(state: MatchState) -> MarketSet:
    current_goals = state.goles_local + state.goles_visitante
    current_cards = state.amarillas + state.rojas
    return MarketSet(
        goles=_make_market(current_goals + 1.5, 1.90, 1.90),
        corners=_make_market(state.corners + 2.5, 1.90, 1.90),
        tarjetas=_make_market(current_cards + 2.5, 1.90, 1.90),
    )


def clamp_probability(value: float) -> float:
    return clamp(value, 0.04, 0.96)


def synthetic_market_from_probs(
    line: float,
    prob_over: float,
    prob_under: float,
    rng: np.random.Generator,
) -> MarketLine:
    market_shift = rng.normal(0.0, 0.03)
    p_over_market = clamp_probability(prob_over + market_shift)
    p_under_market = clamp_probability(prob_under - market_shift)
    total = p_over_market + p_under_market
    p_over_market /= total
    p_under_market /= total
    vig = rng.uniform(0.04, 0.07)
    over_odds = max(1.20, 1.0 / (p_over_market * (1.0 + vig / 2.0)))
    under_odds = max(1.20, 1.0 / (p_under_market * (1.0 + vig / 2.0)))
    return _make_market(line, round(over_odds, 2), round(under_odds, 2))


def generate_synthetic_state(rng: np.random.Generator) -> MatchState:
    minute = float(rng.integers(8, 89))
    progress = minute / 90.0
    tempo = rng.uniform(0.78, 1.28)
    heat = rng.uniform(0.70, 1.35)
    openness = rng.uniform(0.78, 1.30)
    home_bias = rng.uniform(0.42, 0.58)

    goals_total = int(rng.poisson(2.45 * openness * progress))
    local_goals = int(rng.binomial(goals_total, home_bias)) if goals_total > 0 else 0
    away_goals = goals_total - local_goals

    shots_total = max(goals_total + 2, int(rng.poisson(23.0 * openness * progress)))
    shots_local = int(rng.binomial(shots_total, home_bias))
    shots_away = max(0, shots_total - shots_local)

    sot_rate = clamp(0.28 + rng.normal(0.04, 0.04), 0.16, 0.48)
    sot_total = int(rng.binomial(shots_total, sot_rate)) if shots_total > 0 else 0
    sot_local = int(rng.binomial(sot_total, home_bias)) if sot_total > 0 else 0
    sot_away = max(0, sot_total - sot_local)
    sot_local = min(sot_local, shots_local)
    sot_away = min(sot_away, shots_away)

    xg_total = max(
        float(goals_total) * 0.72 + shots_total * 0.055 + sot_total * 0.07 + rng.normal(0.0, 0.18),
        0.10,
    )
    xg_share = clamp(home_bias + rng.normal(0.0, 0.06), 0.25, 0.75)
    xg_local = max(0.05, xg_total * xg_share)
    xg_away = max(0.05, xg_total - xg_local)

    corners_total = int(rng.poisson(10.0 * tempo * progress))
    if abs(local_goals - away_goals) >= 2 and minute >= 55:
        corners_total += int(rng.integers(0, 3))

    fouls_total = int(rng.poisson(24.0 * heat * progress))
    cards_total = int(rng.poisson(4.4 * heat * progress))
    reds = 1 if (cards_total >= 5 and minute >= 70 and rng.random() < 0.08) else 0
    yellows = max(0, cards_total - (reds * 2))

    if fouls_total < yellows:
        fouls_total = yellows + int(rng.integers(0, 4))

    possession_local = clamp(50.0 + rng.normal(0.0, 10.0), 31.0, 69.0)

    return MatchState(
        minuto=minute,
        goles_local=float(local_goals),
        goles_visitante=float(away_goals),
        amarillas=float(yellows),
        rojas=float(reds),
        faltas=float(fouls_total),
        corners=float(corners_total),
        xg_local=float(round(xg_local, 2)),
        xg_visitante=float(round(xg_away, 2)),
        tiros_local=float(shots_local),
        tiros_visitante=float(shots_away),
        tiros_puerta_local=float(sot_local),
        tiros_puerta_visitante=float(sot_away),
        posesion_local=float(round(possession_local, 1)),
        urgency_multiplier=1.0,
        defensive_yellows=0.0
    )


def generate_synthetic_markets(
    state: MatchState,
    result: ModelResult,
    rng: np.random.Generator,
) -> MarketSet:
    current_goals = state.goles_local + state.goles_visitante
    current_cards = state.amarillas + state.rojas

    goal_line = current_goals + float(rng.choice([0.5, 1.5, 2.5]))
    corner_line = state.corners + float(rng.choice([1.5, 2.5, 3.5]))
    card_line = current_cards + float(rng.choice([1.5, 2.5, 3.5]))

    def _poisson_probs(lambda_val, line, current):
        """Calculate over/under probs using exact Poisson math."""
        needed = line - current
        if needed < 0:
            return (1.0, 0.0)
        k = math.ceil(needed)
        p_over = float(poisson.sf(k - 1, lambda_val))
        p_under = max(0.0, 1.0 - p_over)
        return (p_over, p_under)

    goal_probs = _poisson_probs(result.lambda_goals, goal_line, current_goals)
    corner_probs = _poisson_probs(result.lambda_corners, corner_line, state.corners)
    card_probs = _poisson_probs(result.lambda_cards, card_line, current_cards)

    return MarketSet(
        goles=synthetic_market_from_probs(goal_line, goal_probs[0], goal_probs[1], rng),
        corners=synthetic_market_from_probs(corner_line, corner_probs[0], corner_probs[1], rng),
        tarjetas=synthetic_market_from_probs(card_line, card_probs[0], card_probs[1], rng),
    )


def sample_hidden_totals(
    state: MatchState,
    result: ModelResult,
    rng: np.random.Generator,
) -> tuple[float, float, float]:
    score_diff = abs(state.goles_local - state.goles_visitante)
    current_goals = state.goles_local + state.goles_visitante
    current_cards = state.amarillas + state.rojas
    fouls_rate = state.faltas / max(25.0, state.minuto)

    goal_factor = rng.lognormal(mean=0.0, sigma=0.28)
    if state.minuto >= 70 and score_diff >= 2:
        goal_factor *= 0.82
    elif state.minuto >= 70 and score_diff <= 1:
        goal_factor *= 1.12
    if result.phase_name == "cierre_roto":
        goal_factor *= 1.08

    corner_factor = rng.lognormal(mean=0.0, sigma=0.24)
    if state.minuto >= 60 and score_diff >= 1:
        corner_factor *= 1.05
    if state.corners >= 8:
        corner_factor *= 1.06

    card_factor = rng.lognormal(mean=0.0, sigma=0.36)
    if state.minuto >= 70:
        card_factor *= 1.12
    if current_cards <= 1 and state.minuto >= 70:
        card_factor *= 1.14
    if result.phase_name in {"partido_caliente", "cierre_roto"}:
        card_factor *= 1.10

    future_goals = rng.poisson(clamp(result.lambda_goals * goal_factor, 0.05, 6.50))
    future_corners = rng.poisson(clamp(result.lambda_corners * corner_factor, 0.05, 9.00))
    future_cards = rng.poisson(clamp(result.lambda_cards * card_factor, 0.05, 7.50))

    late_card_spike = 0
    spike_prob, spike_lambda = late_card_spike_profile(state, result.phase_name)
    if spike_prob > 0.0 and rng.random() < spike_prob:
        late_card_spike = 1 + rng.poisson(max(0.20, spike_lambda))

    return (
        float(current_goals + future_goals),
        float(state.corners + future_corners),
        float(current_cards + future_cards + late_card_spike),
    )


def settle_bet(
    decision: MarketDecision,
    market: MarketLine,
    actual_total: float,
) -> tuple[float, float, bool]:
    if decision.best_side == "NO BET" or decision.best_stake <= 0:
        return 0.0, 0.0, False

    stake = decision.best_stake
    won = False
    profit = -stake
    if decision.best_side == "OVER" and actual_total > market.linea:
        won = True
        profit = stake * (market.over - 1.0)
    elif decision.best_side == "UNDER" and actual_total < market.linea:
        won = True
        profit = stake * (market.under - 1.0)
    return stake, profit, won


def run_stress_test(params: ModelParams, runs: int, seed: int) -> dict[str, object]:
    rng = np.random.default_rng(seed)
    fast_params = replace(params, sims=min(params.sims, 4000))
    totals = {
        "goles": StressAccumulator(),
        "corners": StressAccumulator(),
        "tarjetas": StressAccumulator(),
        "overall": StressAccumulator(),
    }

    for _ in range(runs):
        state = generate_synthetic_state(rng)
        base_result = run_model(state, placeholder_markets(state), fast_params)
        markets = generate_synthetic_markets(state, base_result, rng)
        result = replace(base_result, markets=markets)

        goal_decision = apply_market_guardrails(
            "GOLES",
            market_summary("GOLES", result.lambda_goals, markets.goles, fast_params, state),
            state,
            fast_params,
        )
        corner_decision = apply_market_guardrails(
            "CORNERS",
            market_summary("CORNERS", result.lambda_corners, markets.corners, fast_params, state),
            state,
            fast_params,
        )
        card_decision = apply_market_guardrails(
            "TARJETAS",
            market_summary("TARJETAS", result.lambda_cards, markets.tarjetas, fast_params, state),
            state,
            fast_params,
        )

        actual_goals, actual_corners, actual_cards = sample_hidden_totals(state, result, rng)
        settlements = [
            ("goles", goal_decision, markets.goles, actual_goals),
            ("corners", corner_decision, markets.corners, actual_corners),
            ("tarjetas", card_decision, markets.tarjetas, actual_cards),
        ]
        for key, decision, market, actual_total in settlements:
            stake, profit, won = settle_bet(decision, market, actual_total)
            if stake <= 0:
                continue
            totals[key].bets += 1
            totals[key].stake += stake
            totals[key].profit += profit
            totals[key].wins += int(won)
            totals["overall"].bets += 1
            totals["overall"].stake += stake
            totals["overall"].profit += profit
            totals["overall"].wins += int(won)

    summary = {}
    for key, acc in totals.items():
        roi = (acc.profit / acc.stake) if acc.stake > 0 else 0.0
        hit_rate = (acc.wins / acc.bets) if acc.bets > 0 else 0.0
        summary[key] = {
            "bets": acc.bets,
            "stake": acc.stake,
            "profit": acc.profit,
            "roi": roi,
            "hit_rate": hit_rate,
        }
    return {"preset": params.nombre, "runs": runs, "seed": seed, "summary": summary}


def print_stress_report(report: dict[str, object]) -> None:
    summary = report["summary"]
    print("\n" + "=" * 72)
    print(
        f"STRESS TEST | preset={report['preset']} | escenarios={report['runs']} | seed={report['seed']}"
    )
    print("=" * 72)
    print("  Nota: esto es ROI sintetico bajo un entorno simulado y mas caotico que el modelo.")
    print("  Sirve para encontrar sobreconfianza; no reemplaza un backtest con datos reales.")
    for key in ["overall", "goles", "corners", "tarjetas"]:
        row = summary[key]
        print(
            f"  {key.upper():9s} | bets {row['bets']:4d} | hit rate {row['hit_rate']:.1%}"
            f" | ROI {row['roi']:.1%} | profit {row['profit']:.3f}"
        )
    print("=" * 72)


def resolve_prematch_context(
    args: argparse.Namespace,
    home_team: str,
    away_team: str,
    workbook: PrematchWorkbook | None,
) -> PrematchContext | None:
    workbook_context = None
    json_context = None

    if workbook is not None:
        workbook_context = workbook.lookup(home_team, away_team)

    if args.prematch_json and load_prematch_context_from_json is not None:
        json_context = load_prematch_context_from_json(
            args.prematch_json,
            home_team=home_team,
            away_team=away_team,
            token=args.prematch_token,
        )

    if merge_prematch_contexts is None:
        return workbook_context or json_context
    return merge_prematch_contexts(workbook_context, json_context)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Modelo live de futbol con presets y demos para calibracion rapida."
    )
    parser.add_argument("--demo", choices=sorted(DEMO_SCENARIOS.keys()))
    parser.add_argument(
        "--preset",
        choices=sorted(PARAMETER_PRESETS.keys()),
        default="balanced",
        help="Preset de calibracion del modelo.",
    )
    parser.add_argument("--seed", type=int, help="Seed del simulador para resultados reproducibles.")
    parser.add_argument("--sims", type=int, help="Numero de simulaciones.")
    parser.add_argument("--league-name", help="Liga o torneo manual para ajustar baseline por perfil.")
    parser.add_argument("--once", action="store_true", help="Ejecuta una sola corrida y sale.")
    parser.add_argument("--list-demos", action="store_true", help="Lista escenarios demo y sale.")
    parser.add_argument("--advanced", action="store_true", help="Muestra tambien el detalle tecnico.")
    parser.add_argument("--sofascore-url", help="Link del partido de SofaScore para cargar live automatico.")
    parser.add_argument("--watch", action="store_true", help="Refresca el partido live de SofaScore en bucle.")
    parser.add_argument("--poll-seconds", type=int, default=10, help="Segundos entre refrescos live.")
    parser.add_argument("--sofascore-provider", type=int, default=1, help="Provider id de cuotas live en SofaScore.")
    parser.add_argument(
        "--odds-source",
        choices=["manual", "sofascore"],
        default="manual",
        help="De donde salen las cuotas en modo SofaScore. Por defecto se cargan manualmente.",
    )
    parser.add_argument("--show-browser", action="store_true", help="Abre Chromium visible para el modo SofaScore.")
    parser.add_argument("--prematch-xlsx", help="Excel prematch local o link de Google Sheets con datos del dia y rachas.")
    parser.add_argument("--prematch-json", help="JSON local o URL con sesgos prematch.")
    parser.add_argument("--prematch-token", help="Bearer token opcional para --prematch-json si es URL.")
    parser.add_argument(
        "--history-dir",
        default=str(LIVE_HISTORY_DIR),
        help="Carpeta donde se guardan snapshots live historicos en JSONL.",
    )
    parser.add_argument(
        "--no-history",
        action="store_true",
        help="No guarda snapshots live en disco.",
    )
    parser.add_argument("--stress-test", action="store_true", help="Corre pruebas masivas sinteticas.")
    parser.add_argument("--stress-runs", type=int, default=1500, help="Escenarios sinteticos para stress test.")
    parser.add_argument(
        "--stress-all-presets",
        action="store_true",
        help="Corre el stress test para conservative, balanced y aggressive.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    configure_interactive_mode(args)
    if args.list_demos:
        print_demo_list()
        return
    if args.stress_test:
        preset_names = sorted(PARAMETER_PRESETS.keys()) if args.stress_all_presets else [args.preset]
        base_seed = args.seed if args.seed is not None else 7
        for offset, preset_name in enumerate(preset_names):
            params = build_params(preset_name, base_seed + offset, args.sims)
            report = run_stress_test(params, args.stress_runs, base_seed + offset)
            print_stress_report(report)
        return

    workbook = None
    if args.prematch_xlsx:
        if PrematchWorkbook is None:
            raise RuntimeError("No se pudo cargar el lector prematch del Excel.")
        workbook = PrematchWorkbook(args.prematch_xlsx)

    try:
        if args.sofascore_url:
            active_urls = [args.sofascore_url]
            while True:
                maybe_require_live_bridge()
                monitor = SofaScoreMonitor(
                    provider_id=args.sofascore_provider,
                    headless=not args.show_browser,
                )
                save_session_state(
                    {
                        "last_sofascore_url": active_urls[0],
                        "prematch_xlsx": args.prematch_xlsx,
                        "odds_source": args.odds_source,
                        "poll_seconds": args.poll_seconds,
                    }
                )
                
                match_contexts = {
                    url: {
                        "first_pass": True,
                        "previous_markets": None,
                        "previous_state": None,
                        "history_notice_shown": False,
                        "ended": False,
                        "match_ctx": {
                            "odds_history_buffer": {"goles": [], "corners": [], "tarjetas": []},
                            "last_sharp_line": None,
                            "corner_ctx": {},  # Rastreo del Filtro de Escalada de corners
                        },
                    }
                    for url in active_urls
                }
                
                try:
                    while True:
                        active_count = 0
                        for url in list(match_contexts.keys()):
                            ctx = match_contexts[url]
                            if ctx["ended"]:
                                continue
                            active_count += 1
                            
                            params = build_params(args.preset, args.seed, args.sims)
                            try:
                                snapshot = monitor.fetch_snapshot(match_url=url, reload_page=not ctx["first_pass"])
                            except Exception as e:
                                print(f"\nError obteniendo {url}: {e}")
                                continue
                                
                            ctx["first_pass"] = False
                            state = build_state_from_snapshot(snapshot, ctx["previous_state"])
                            ctx["previous_state"] = state
                            league_profile = infer_league_profile(snapshot=snapshot, league_name=args.league_name)
                            params = apply_league_profile(params, league_profile)
                            prematch = resolve_prematch_context(
                                args,
                                getattr(snapshot, "home_team"),
                                getattr(snapshot, "away_team"),
                                workbook,
                            )
                            print_live_source_block(snapshot, prematch, live_odds_source_text(args), league_profile)
                            status_text = str(getattr(snapshot, "status_text", "")).strip().lower()
                            if status_text in {"ended", "after et", "after penalties"}:
                                if not args.no_history:
                                    closure_path = append_match_closure(Path(args.history_dir), snapshot, state)
                                    if closure_path is not None:
                                        print("\nCIERRE GUARDADO")
                                        print(f"  Estadisticas finales en {closure_path}")
                                print("\nPARTIDO CERRADO")
                                print(f"  {getattr(snapshot, 'home_team')} vs {getattr(snapshot, 'away_team')} ha terminado. No sigo calculando entradas live para este.")
                                ctx["ended"] = True
                                continue
                                
                            markets = collect_live_markets(args, snapshot, state, ctx["previous_markets"])
                            ctx["previous_markets"] = markets
                            result = run_model(state, markets, params, prematch=prematch)
                            print_report(
                                result,
                                f"{getattr(snapshot, 'home_team')} vs {getattr(snapshot, 'away_team')}",
                                advanced=args.advanced,
                            )
                            if not args.no_history:
                                history_path = append_live_history(
                                    Path(args.history_dir), snapshot, state, markets, args,
                                    league_profile, prematch, result,
                                    match_ctx=ctx["match_ctx"],
                                )
                                if history_path is not None and not ctx["history_notice_shown"]:
                                    print(f"  Historial en {history_path}")
                                    ctx["history_notice_shown"] = True
                        
                        if args.once or active_count == 0:
                            break
                        if args.watch:
                            try:
                                wait_seconds = max(10, int(args.poll_seconds))
                                print(f"\nRefrescando {active_count} partidos en {wait_seconds}s... (Escribe 'm' para abrir opciones)")
                                
                                import sys
                                import time
                                end_wait = time.time() + wait_seconds
                                menu_requested = False
                                
                                while time.time() < end_wait:
                                    if sys.platform == "win32":
                                        import msvcrt
                                        if msvcrt.kbhit():
                                            char = msvcrt.getch()
                                            if char.lower() == b'm':
                                                menu_requested = True
                                                break
                                    else:
                                        import select
                                        i, _, _ = select.select([sys.stdin], [], [], 0.5)
                                        if i:
                                            sys.stdin.readline()
                                            menu_requested = True
                                            break
                                    time.sleep(0.5)

                                if menu_requested:
                                    print("\n")
                                    resp = input("Opciones: (a=agregar partido / c=cambiar todos / n=salir / s=seguir): ").strip().lower()
                                    if resp in {"a", "agregar"}:
                                        nuevo = input("Introduce link de SofaScore a agregar: ").strip()
                                        if nuevo and nuevo not in match_contexts:
                                            match_contexts[nuevo] = {"first_pass": True, "previous_markets": None, "previous_state": None, "history_notice_shown": False, "ended": False}
                                        continue
                                    elif resp in {"c", "cambiar"}:
                                        nuevo = input("Introduce link de SofaScore nuevo (cierra los actuales): ").strip()
                                        if nuevo:
                                            active_urls = [nuevo]
                                            break
                                        continue
                                    elif resp in {"n", "no", "salir"}:
                                        return
                                    else:
                                        continue
                                else:
                                    continue
                            except KeyboardInterrupt:
                                print("\nSalida forzada por el usuario.")
                                return
                        
                        resp = input("\nSiguientes corridas? (s=seguir / a=agregar / c=cambiar / n=salir): ").strip().lower()
                        if resp in {"a", "agregar"}:
                            nuevo = input("Introduce link de SofaScore a agregar: ").strip()
                            if nuevo and nuevo not in match_contexts:
                                match_contexts[nuevo] = {"first_pass": True, "previous_markets": None, "previous_state": None, "history_notice_shown": False, "ended": False}
                            continue
                        elif resp in {"c", "cambiar"}:
                            nuevo = input("Introduce link de SofaScore nuevo (cierra los actuales): ").strip()
                            if nuevo:
                                active_urls = [nuevo]
                                break
                            continue
                        elif resp != "s":
                            return
                finally:
                    monitor.close()

                if args.once or active_count == 0:
                    break
            return

        while True:
            while True:
                params = build_params(args.preset, args.seed, args.sims)
                state, markets, label = preparar_inputs(args)
                league_profile = infer_league_profile(snapshot=None, league_name=args.league_name)
                params = apply_league_profile(params, league_profile)
                if args.league_name:
                    print(
                        f"\nPERFIL LIGA\n  {league_profile.label} | bases 90m -> "
                        f"goles {league_profile.goal_baseline_per90:.2f}, "
                        f"corners {league_profile.corner_baseline_per90:.2f}, "
                        f"tarjetas {league_profile.card_baseline_per90:.2f}"
                    )
                result = run_model(state, markets, params)
                print_report(result, label, advanced=args.advanced)

                if args.once:
                    break
                resp = input("\nSiguiente corrida? (s=seguir / c=cambiar partido manual / n=salir): ").strip().lower()
                if resp in {"c", "cambiar"}:
                    break
                elif resp != "s":
                    return
            if args.once:
                break
    finally:
        if workbook is not None:
            workbook.close()


if __name__ == "__main__":
    main()
