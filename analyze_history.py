"""
analyze_history.py
==================
Análisis post-partido del historial de apuestas.

Incluye:
  - Tasa de aciertos por mercado / lado (OVER | UNDER)
  - EV promedio por mercado
  - Closing Line Value (CLV) — la métrica de oro del trader cuantitativo

Uso:
    python analyze_history.py [--dir live_history] [--out result.txt] [--min-minute 10]
"""
import json
import os
import glob
import argparse
from collections import defaultdict


def analyze_history(history_dir: str = "live_history", out_path: str = "result.txt", min_minute: int = 10):
    files = glob.glob(os.path.join(history_dir, "*.jsonl"))

    total_matches = len(files)
    total_bets = 0
    winning_bets = 0
    losing_bets = 0

    market_stats = defaultdict(lambda: {
        "bets": 0, "wins": 0, "losses": 0,
        "avg_ev": 0.0, "avg_prob": 0.0,
        "OVER_bets": 0, "OVER_wins": 0,
        "UNDER_bets": 0, "UNDER_wins": 0,
    })

    # --- CLV tracking ---
    # Para cada mercado guardamos los CLV individuales (solo si tenemos closing line)
    clv_data: dict[str, list[float]] = defaultdict(list)

    for file_path in files:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        if not lines:
            continue

        extracted_snapshots = []
        closure_record = None
        for line in lines:
            try:
                data = json.loads(line)
                if data.get("record_type") == "match_closed":
                    closure_record = data
                else:
                    extracted_snapshots.append(data)
            except Exception:
                pass

        if not extracted_snapshots:
            continue

        # --- Estado final del partido ---
        # Preferimos el registro de cierre (más preciso); si no existe, usamos el último snapshot
        if closure_record:
            final_state = closure_record.get("final_state", {})
        else:
            final_state = extracted_snapshots[-1].get("snapshot", {}).get("state", {})

        if not final_state:
            continue

        final_goals   = final_state.get("goles_local", 0) + final_state.get("goles_visitante", 0)
        final_corners = final_state.get("corners", 0)
        final_cards   = final_state.get("amarillas", 0) + final_state.get("rojas", 0)

        # --- Cuota de cierre (closing line) de Pinnacle/PS3838 via taken_odds del último snapshot BET ---
        # Estrategia: el último registro que tenga taken_odds con cuota es la "closing line"
        closing_lines: dict[str, dict] = {}  # market -> {"side", "odds", "linea"}
        for snap in reversed(extracted_snapshots):
            taken = snap.get("model", {}).get("taken_odds", {})
            for mkt in ("goals", "corners", "cards"):
                if mkt not in closing_lines and taken.get(mkt):
                    closing_lines[mkt] = taken[mkt]
            if len(closing_lines) == 3:
                break

        # --- Unique bets deduplication ---
        unique_bets_in_match: set[str] = set()

        for snap in extracted_snapshots:
            state = snap.get("snapshot", {}).get("state", {})
            minute = state.get("minuto", 0)
            if minute < min_minute:
                continue

            markets = snap.get("model", {}).get("decisions", {})
            taken_odds_snap = snap.get("model", {}).get("taken_odds", {})

            for mkt_name, decision in markets.items():
                if not decision:
                    continue
                side = decision.get("best_side", "NO BET")
                if side in ("NO BET", "PASAR"):
                    continue

                linea = decision.get("linea")
                prob  = decision.get("best_prob", 0)
                ev    = decision.get("best_ev", 0)

                bet_signature = f"{mkt_name}_{side}_{linea}"
                if bet_signature in unique_bets_in_match:
                    continue
                unique_bets_in_match.add(bet_signature)

                # --- Resultado real ---
                won = False
                lost = False
                if mkt_name == "goals":
                    if side == "OVER" and final_goals > linea:    won = True
                    elif side == "UNDER" and final_goals < linea: won = True
                    else:                                          lost = True
                elif mkt_name == "corners":
                    if side == "OVER" and final_corners > linea:    won = True
                    elif side == "UNDER" and final_corners < linea: won = True
                    else:                                            lost = True
                elif mkt_name == "cards":
                    if side == "OVER" and final_cards > linea:    won = True
                    elif side == "UNDER" and final_cards < linea: won = True
                    else:                                          lost = True

                # Contadores
                s = market_stats[mkt_name]
                s["bets"] += 1
                s["avg_ev"] += ev
                s["avg_prob"] += prob
                if side == "OVER":
                    s["OVER_bets"] += 1
                    if won: s["OVER_wins"] += 1
                else:
                    s["UNDER_bets"] += 1
                    if won: s["UNDER_wins"] += 1
                if won:
                    winning_bets += 1
                    s["wins"] += 1
                elif lost:
                    losing_bets += 1
                    s["losses"] += 1
                total_bets += 1

                # --- CLV: solo calculamos una vez por partido/mercado/lado ---
                taken = taken_odds_snap.get(mkt_name)
                closing = closing_lines.get(mkt_name)
                if (taken and closing
                        and taken.get("side") == side
                        and taken.get("linea") == closing.get("linea")
                        and taken.get("linea") == linea):
                    odds_taken   = taken.get("odds", 0)
                    odds_closing = closing.get("odds", 0)
                    if odds_taken > 1.0 and odds_closing > 1.0:
                        # CLV positivo = apostamos a mejor cuota que el cierre → ventaja real
                        clv = (odds_taken / odds_closing) - 1.0
                        clv_data[mkt_name].append(clv)

    # --- Salida ---
    lines_out: list[str] = []
    lines_out.append("=" * 60)
    lines_out.append("INFORME DE RENDIMIENTO DEL SISTEMA")
    lines_out.append("=" * 60)
    lines_out.append(f"Partidos analizados  : {total_matches}")
    lines_out.append(f"Señales únicas       : {total_bets}")

    if total_bets > 0:
        overall_wr = winning_bets / total_bets * 100
        lines_out.append(f"Win Rate global      : {overall_wr:.2f}%  ({winning_bets}W – {losing_bets}L)")
        lines_out.append("")
        lines_out.append("-" * 60)

        for mkt, stats in market_stats.items():
            if stats["bets"] == 0:
                continue
            win_rate  = stats["wins"] / stats["bets"] * 100
            avg_ev    = stats["avg_ev"] / stats["bets"]
            avg_prob  = stats["avg_prob"] / stats["bets"]
            over_wr   = (stats["OVER_wins"] / stats["OVER_bets"] * 100) if stats["OVER_bets"] > 0 else 0
            under_wr  = (stats["UNDER_wins"] / stats["UNDER_bets"] * 100) if stats["UNDER_bets"] > 0 else 0

            lines_out.append(f"MERCADO: {mkt.upper()}")
            lines_out.append(f"  Win Rate   : {win_rate:.2f}%  ({stats['wins']}W – {stats['losses']}L)")
            lines_out.append(f"  OVER  bets : {stats['OVER_bets']:3d}  ({over_wr:.1f}% ganadas)")
            lines_out.append(f"  UNDER bets : {stats['UNDER_bets']:3d}  ({under_wr:.1f}% ganadas)")
            lines_out.append(f"  Avg EV     : {avg_ev:.4f}   Avg Prob: {avg_prob:.2%}")

            # CLV
            clv_list = clv_data.get(mkt, [])
            if clv_list:
                avg_clv = sum(clv_list) / len(clv_list) * 100
                pos_clv = sum(1 for c in clv_list if c > 0)
                lines_out.append(
                    f"  CLV (n={len(clv_list):2d}) : {avg_clv:+.2f}%  "
                    f"({pos_clv}/{len(clv_list)} apuestas por encima del cierre)"
                )
                if avg_clv > 2:
                    lines_out.append("  ✅ CLV positivo sostenido — sistema con ventaja real sobre el mercado.")
                elif avg_clv > 0:
                    lines_out.append("  🟡 CLV ligeramente positivo — continuar monitoreando.")
                else:
                    lines_out.append("  🔴 CLV negativo — el mercado te está cerrando. Revisa el modelo.")
            else:
                lines_out.append("  CLV       : Sin datos suficientes de cuota de cierre.")
            lines_out.append("")

        lines_out.append("-" * 60)
        lines_out.append("GUÍA CLV (Closing Line Value)")
        lines_out.append("  CLV = (Cuota_Tomada / Cuota_Cierre) - 1")
        lines_out.append("  CLV > 0%  → Apostaste mejor que el mercado al cierre → ventaja real a largo plazo")
        lines_out.append("  CLV < 0%  → El mercado sabía más que tú → ajusta el modelo")
        lines_out.append("  CLV > 3%  → Nivel profesional de selección de líneas")

    output_text = "\n".join(lines_out)
    with open(out_path, "w", encoding="utf-8") as out:
        out.write(output_text)
    print(output_text)
    return output_text


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Análisis CLV del historial de apuestas")
    parser.add_argument("--dir",        default="live_history", help="Directorio de archivos .jsonl")
    parser.add_argument("--out",        default="result.txt",   help="Archivo de salida")
    parser.add_argument("--min-minute", type=int, default=10,   help="Minuto mínimo para considerar señales")
    args = parser.parse_args()
    analyze_history(history_dir=args.dir, out_path=args.out, min_minute=args.min_minute)
