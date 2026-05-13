"""
full_backtest.py
================
Simula el nuevo motor (WPF + Siege Index) sobre el historial de 57 partidos
y genera un reporte completo de performance.

Uso:
    .venv\\Scripts\\python.exe full_backtest.py [--dir live_history]
"""
import json
import os
import glob
import argparse
from collections import defaultdict

import math

from backtest_metrics import run_history_metrics

from futbol_live_betting_probabilities import (
    ModelParams,
    MatchState, MarketLine, PARAMETER_PRESETS,
    run_model, market_summary, apply_market_guardrails
)

# Mapeo: key en JSONL -> key interna del motor
MARKET_KEY_MAP = {
    "goals":   "GOLES",
    "corners": "CORNERS",
    "cards":   "TARJETAS",
}
MARKET_FINAL_KEY = {
    "GOLES":    ("goles_local", "goles_visitante"),
    "CORNERS":  ("corners",),
    "TARJETAS": ("amarillas", "rojas"),
}


def get_final_total(mkt_name: str, final_state: dict) -> float:
    if not final_state:
        return 0.0
    if mkt_name == "GOLES":
        return (final_state.get("goles_local") or 0) + (final_state.get("goles_visitante") or 0)
    if mkt_name == "CORNERS":
        return final_state.get("corners") or 0
    # TARJETAS: amarillas + rojas*2
    return (final_state.get("amarillas") or 0) + (final_state.get("rojas") or 0) * 2


def sf(v, default=0.0):
    """Safe float — handles None from older history records."""
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
    """Objeto liviano que simula el MarketSet esperado por run_model."""
    def __init__(self, goles=None, corners=None, tarjetas=None):
        self.goles = goles
        self.corners = corners
        self.tarjetas = tarjetas


def full_backtest(history_dir: str = "live_history",
                  out_path: str = "full_backtest_report.md",
                  min_minute: int = 10):

    files = glob.glob(os.path.join(history_dir, "*.jsonl"))
    total_matches = len(files)

    params = PARAMETER_PRESETS["balanced"]

    total_bets = 0
    winning_bets = 0
    losing_bets = 0

    market_stats = defaultdict(lambda: {
        "bets": 0, "wins": 0, "losses": 0,
        "total_ev": 0.0, "total_prob": 0.0,
        "OVER_bets": 0, "OVER_wins": 0,
        "UNDER_bets": 0, "UNDER_wins": 0,
    })
    clv_data = defaultdict(list)

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

        # --- Final state ---
        if closure_record:
            # Support both schema variants
            final_state = (
                closure_record.get("final_state")
                or closure_record.get("final_snapshot", {}).get("state")
                or {}
            )
        else:
            final_state = snapshots[-1].get("snapshot", {}).get("state", {})

        if not final_state:
            continue

        # --- Closing lines de Pinnacle (último taken_odds disponible) ---
        closing_lines = {}
        for snap in reversed(snapshots):
            taken = snap.get("model", {}).get("taken_odds", {})
            for raw_key in ("goals", "corners", "cards"):
                mkt = MARKET_KEY_MAP[raw_key]
                if mkt not in closing_lines and taken.get(raw_key):
                    closing_lines[mkt] = taken[raw_key]
            if len(closing_lines) == 3:
                break

        unique_bets = set()

        for snap in snapshots:
            state_dict = snap.get("snapshot", {}).get("state", {})
            minute = state_dict.get("minuto", 0)
            if minute < min_minute:
                continue

            # --- Construir mercados desde source_markets ---
            src_mkts = snap.get("snapshot", {}).get("source_markets", {})
            if not src_mkts:
                continue

            def to_market(raw_key):
                m = src_mkts.get(raw_key)
                if not m:
                    return None
                linea = m.get("linea") or m.get("line") or 0
                over  = m.get("over", 0)
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
                prob  = decision.best_prob
                ev    = decision.best_ev

                sig = f"{mkt_name}_{side}_{linea}"
                if sig in unique_bets:
                    continue
                unique_bets.add(sig)

                final_total = get_final_total(mkt_name, final_state)
                won  = False
                lost = False
                if side == "OVER":
                    won = final_total > linea
                elif side == "UNDER":
                    won = final_total < linea
                if not won:
                    lost = True

                s = market_stats[mkt_name]
                s["bets"] += 1
                s["total_ev"] += ev
                s["total_prob"] += prob

                if side == "OVER":
                    s["OVER_bets"] += 1
                    if won: s["OVER_wins"] += 1
                else:
                    s["UNDER_bets"] += 1
                    if won: s["UNDER_wins"] += 1

                if won:
                    winning_bets += 1
                    s["wins"] += 1
                if lost:
                    losing_bets += 1
                    s["losses"] += 1
                total_bets += 1

                # CLV
                raw_key_map = {"GOLES": "goals", "CORNERS": "corners", "TARJETAS": "cards"}
                taken = taken_odds_snap.get(raw_key_map[mkt_name])
                closing = closing_lines.get(mkt_name)
                if (taken and closing
                        and taken.get("side") == side
                        and taken.get("linea") == linea
                        and closing.get("linea") == linea):
                    ot = taken.get("odds", 0)
                    oc = closing.get("odds", 0)
                    if ot > 1.0 and oc > 1.0:
                        clv_data[mkt_name].append((ot / oc) - 1.0)

    # --- Generar Markdown ---
    lo = []
    lo.append("# REPORTE DE PERFORMANCE: NUEVO MOTOR (WPF + SIEGE INDEX)\n")
    lo.append(f"| Métrica | Valor |")
    lo.append(f"|---|---|")
    lo.append(f"| Partidos analizados | {total_matches} |")
    lo.append(f"| Señales únicas disparadas | {total_bets} |")
    if total_bets > 0:
        wr = winning_bets / total_bets * 100
        lo.append(f"| **Win Rate global** | **{wr:.1f}%** ({winning_bets}W – {losing_bets}L) |")
    lo.append("")

    EMOJIS = {"GOLES": "⚽", "CORNERS": "🚩", "TARJETAS": "🟨"}

    for mkt, stats in market_stats.items():
        if stats["bets"] == 0:
            continue
        wr  = stats["wins"] / stats["bets"] * 100
        owr = (stats["OVER_wins"] / stats["OVER_bets"] * 100) if stats["OVER_bets"] > 0 else 0.0
        uwr = (stats["UNDER_wins"] / stats["UNDER_bets"] * 100) if stats["UNDER_bets"] > 0 else 0.0
        avg_ev   = stats["total_ev"] / stats["bets"]
        avg_prob = stats["total_prob"] / stats["bets"]

        lo.append(f"## {EMOJIS.get(mkt, '')} {mkt}")
        lo.append(f"| | Bets | Win Rate |")
        lo.append(f"|---|---|---|")
        lo.append(f"| **TOTAL** | {stats['bets']} | **{wr:.1f}%** ({stats['wins']}W – {stats['losses']}L) |")
        lo.append(f"| OVER | {stats['OVER_bets']} | {owr:.1f}% ({stats['OVER_wins']}W) |")
        lo.append(f"| UNDER | {stats['UNDER_bets']} | {uwr:.1f}% ({stats['UNDER_wins']}W) |")
        lo.append(f"\n- **Avg EV**: {avg_ev:.4f} | **Avg Prob**: {avg_prob:.1%}")

        clv_list = clv_data.get(mkt, [])
        if clv_list:
            avg_clv = sum(clv_list) / len(clv_list) * 100
            pos_n   = sum(1 for c in clv_list if c > 0)
            icon    = "✅" if avg_clv > 2 else ("🟡" if avg_clv > 0 else "🔴")
            lo.append(f"- **CLV** (n={len(clv_list)}): {avg_clv:+.2f}%  ({pos_n}/{len(clv_list)} por encima del cierre) {icon}")
        else:
            lo.append("- **CLV**: Sin datos de cuota de cierre en el historial.")
        lo.append("")

    mhist = run_history_metrics(params, history_dir, min_minute)
    lo.append("")
    lo.append("## Calibraci?n (historial)")
    lo.append(f"| Brier (promedio) | {mhist['brier']:.4f} |")
    lo.append(f"| Sharpe (retornos unitarios aprox.) | {mhist['sharpe']:.4f} |")
    lo.append(f"| Apuestas usadas en m?tricas | {mhist['n_bets']} |")
    lo.append("")
    lo.append("---")
    lo.append("*Nota: CLV positivo > 2% = ventaja matemática real sobre el mercado profesional.*")

    output = "\n".join(lo)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(output)

    print(output)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dir", default="live_history")
    parser.add_argument("--out", default="full_backtest_report.md")
    parser.add_argument("--min-minute", type=int, default=10)
    args = parser.parse_args()
    full_backtest(args.dir, args.out, args.min_minute)