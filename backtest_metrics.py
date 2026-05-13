import json
import math
import os
import glob
from futbol_live_betting_probabilities import (
    ModelParams,
    MatchState,
    MarketLine,
    run_model,
    market_summary,
    apply_market_guardrails,
)


MARKET_FINAL_KEY = {
    "GOLES": ("goles_local", "goles_visitante"),
    "CORNERS": ("corners",),
    "TARJETAS": ("amarillas", "rojas"),
}


def get_final_total(mkt_name: str, final_state: dict) -> float:
    if not final_state:
        return 0.0
    if mkt_name == "GOLES":
        return (final_state.get("goles_local") or 0) + (final_state.get("goles_visitante") or 0)
    if mkt_name == "CORNERS":
        return final_state.get("corners") or 0
    return (final_state.get("amarillas") or 0) + (final_state.get("rojas") or 0) * 2


def sf(v, default=0.0):
    try:
        return float(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def build_state(s: dict) -> MatchState:
    return MatchState(
        minuto=sf(s.get("minuto")),
        goles_local=sf(s.get("goles_local")),
        goles_visitante=sf(s.get("goles_visitante")),
        amarillas=sf(s.get("amarillas")),
        rojas=sf(s.get("rojas")),
        faltas=sf(s.get("faltas")),
        corners=sf(s.get("corners")),
        xg_local=sf(s.get("xg_local")),
        xg_visitante=sf(s.get("xg_visitante")),
        tiros_local=sf(s.get("tiros_local")),
        tiros_visitante=sf(s.get("tiros_visitante")),
        tiros_puerta_local=sf(s.get("tiros_puerta_local")),
        tiros_puerta_visitante=sf(s.get("tiros_puerta_visitante")),
        posesion_local=sf(s.get("posesion_local")) or 50.0,
        corners_local=sf(s.get("corners_local")),
        corners_visitante=sf(s.get("corners_visitante")),
        faltas_local=sf(s.get("faltas_local")),
        faltas_visitante=sf(s.get("faltas_visitante")),
        amarillas_local=sf(s.get("amarillas_local")),
        amarillas_visitante=sf(s.get("amarillas_visitante")),
        rojas_local=sf(s.get("rojas_local")),
        rojas_visitante=sf(s.get("rojas_visitante")),
        corners_recientes=sf(s.get("corners_recientes")),
        xg_recientes=sf(s.get("xg_recientes")),
        tiros_recientes=sf(s.get("tiros_recientes")),
        ventana_reciente_min=sf(s.get("ventana_reciente_min")),
        faltas_recientes=sf(s.get("faltas_recientes")),
        tarjetas_recientes=sf(s.get("tarjetas_recientes")),
        centros_local=sf(s.get("centros_local")),
        centros_visitante=sf(s.get("centros_visitante")),
        centros_recientes=sf(s.get("centros_recientes")),
        touches_in_box_home=sf(s.get("touches_in_box_home")),
        touches_in_box_away=sf(s.get("touches_in_box_away")),
        big_chances_missed_home=sf(s.get("big_chances_missed_home")),
        big_chances_missed_away=sf(s.get("big_chances_missed_away")),
    )


class SimpleMarkets:
    def __init__(self, goles=None, corners=None, tarjetas=None):
        self.goles = goles
        self.corners = corners
        self.tarjetas = tarjetas


def run_history_metrics(
    params: ModelParams,
    history_dir: str = "live_history",
    min_minute: int = 10,
) -> dict:
    files = glob.glob(os.path.join(history_dir, "*.jsonl"))
    brier_terms: list[float] = []
    returns: list[float] = []
    for file_path in files:
        with open(file_path, "r", encoding="utf-8") as f:
            raw_lines = f.readlines()
        snapshots = []
        closure_record = None
        for raw in raw_lines:
            try:
                d = json.loads(raw)
                rt = d.get("record_type", "")
                if rt in ("match_closed", "match_closure"):
                    closure_record = d
                else:
                    snapshots.append(d)
            except Exception:
                pass
        if not snapshots:
            continue
        if closure_record:
            final_state = (
                closure_record.get("final_state")
                or closure_record.get("final_snapshot", {}).get("state")
                or {}
            )
        else:
            final_state = snapshots[-1].get("snapshot", {}).get("state", {})
        if not final_state:
            continue
        unique_bets = set()
        for snap in snapshots:
            state_dict = snap.get("snapshot", {}).get("state", {})
            minute = state_dict.get("minuto", 0)
            if minute < min_minute:
                continue
            src_mkts = snap.get("snapshot", {}).get("source_markets", {})
            if not src_mkts:
                continue

            def to_market(raw_key):
                m = src_mkts.get(raw_key)
                if not m:
                    return None
                linea = m.get("linea") or m.get("line") or 0
                over = m.get("over", 0)
                under = m.get("under", 0)
                if linea > 0 and over > 1.0 and under > 1.0:
                    return MarketLine(linea=linea, over=over, under=under)
                return None

            markets = SimpleMarkets(
                goles=to_market("goals"),
                corners=to_market("corners"),
                tarjetas=to_market("cards"),
            )
            if not any([markets.goles, markets.corners, markets.tarjetas]):
                continue
            try:
                state = build_state(state_dict)
                result = run_model(state, markets, params, prematch=None)
            except Exception:
                continue
            new_decisions = {}
            if markets.goles:
                d = market_summary("GOLES", result.total_goals, markets.goles, params, state)
                new_decisions["GOLES"] = apply_market_guardrails("GOLES", d, state, params, None)
            if markets.corners:
                d = market_summary("CORNERS", result.total_corners, markets.corners, params, state)
                new_decisions["CORNERS"] = apply_market_guardrails("CORNERS", d, state, params, None)
            if markets.tarjetas:
                d = market_summary("TARJETAS", result.total_cards, markets.tarjetas, params, state)
                new_decisions["TARJETAS"] = apply_market_guardrails("TARJETAS", d, state, params, None)
            taken_odds_snap = snap.get("model", {}).get("taken_odds", {})
            for mkt_name, decision in new_decisions.items():
                side = decision.best_side
                if side in ("NO BET", "PASAR"):
                    continue
                linea = decision.linea
                probv = decision.best_prob
                sig = f"{mkt_name}_{side}_{linea}"
                if sig in unique_bets:
                    continue
                unique_bets.add(sig)
                final_total = get_final_total(mkt_name, final_state)
                won = (side == "OVER" and final_total > linea) or (
                    side == "UNDER" and final_total < linea
                )
                o = 1.0 if won else 0.0
                brier_terms.append((probv - o) ** 2)
                raw_key_map = {"GOLES": "goals", "CORNERS": "corners", "TARJETAS": "cards"}
                taken = taken_odds_snap.get(raw_key_map[mkt_name])
                odds = float(taken.get("odds", 0)) if isinstance(taken, dict) else 0.0
                if odds > 1.0:
                    stake = 0.01
                    if won:
                        returns.append(stake * (odds - 1.0))
                    else:
                        returns.append(-stake)
                else:
                    returns.append(0.01 if won else -0.01)
    n = len(brier_terms)
    brier = sum(brier_terms) / n if n else 1.0
    if len(returns) > 2:
        m = sum(returns) / len(returns)
        v = sum((r - m) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(v) if v > 0 else 0.0
        sharpe = (m / std) * math.sqrt(len(returns)) if std > 0 else 0.0
    else:
        sharpe = 0.0
    return {"n_bets": n, "brier": brier, "sharpe": sharpe, "returns": returns}
