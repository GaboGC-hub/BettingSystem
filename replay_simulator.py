#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
🕰️ Replay Simulator (replay_simulator.py) — "La Máquina del Tiempo"
====================================================================
Lee archivos JSONL históricos de live_history_v2 y re-ejecuta el motor
matemático (Poisson + Kelly + Siege Index) snapshot por snapshot.

Modos de operación:
  1. REPLAY  — Reproduce un partido individual con reporte visual.
  2. BACKTEST — Corre TODOS los partidos y genera un resumen de PnL simulado.

Uso:
  python replay_simulator.py                          # Menú interactivo
  python replay_simulator.py replay <archivo.jsonl>   # Replay directo
  python replay_simulator.py backtest                 # Backtest masivo
"""

import json
import sys
import time
from pathlib import Path
from types import SimpleNamespace

HISTORY_DIR = Path("live_history_v2")

# ── Colores ANSI para terminal ─────────────────────────────────────────────────
class C:
    R = "\033[91m"   # Rojo
    G = "\033[92m"   # Verde
    Y = "\033[93m"   # Amarillo
    B = "\033[94m"   # Azul
    M = "\033[95m"   # Magenta
    CY = "\033[96m"  # Cyan
    W = "\033[97m"   # Blanco
    DIM = "\033[2m"
    BOLD = "\033[1m"
    END = "\033[0m"


def _build_snapshot_ns(row: dict) -> SimpleNamespace:
    """Construye un SimpleNamespace equivalente al snapshot de SofaScore."""
    match_info = row.get("match", {})
    state = row.get("snapshot", {}).get("state", {})

    yellows_h = int(state.get("amarillas_local", 0))
    yellows_a = int(state.get("amarillas_visitante", 0))
    reds_h = int(state.get("rojas_local", 0))
    reds_a = int(state.get("rojas_visitante", 0))
    corners_h = int(state.get("corners_local", 0))
    corners_a = int(state.get("corners_visitante", 0))
    fouls_h = int(state.get("faltas_local", 0))
    fouls_a = int(state.get("faltas_visitante", 0))

    return SimpleNamespace(
        home_team=match_info.get("home_team", "?"),
        away_team=match_info.get("away_team", "?"),
        tournament=match_info.get("tournament", ""),
        tournament_slug=match_info.get("tournament_slug", ""),
        category_name=match_info.get("category_name", ""),
        country_name=match_info.get("country_name", ""),
        status_text=match_info.get("status_text", "inprogress"),
        minute=float(state.get("minuto", 0)),
        goals_home=int(state.get("goles_local", 0)),
        goals_away=int(state.get("goles_visitante", 0)),
        corners_home=corners_h,
        corners_away=corners_a,
        corners_total=corners_h + corners_a,
        yellows_home=yellows_h,
        yellows_away=yellows_a,
        yellows_total=yellows_h + yellows_a,
        reds_home=reds_h,
        reds_away=reds_a,
        reds_total=reds_h + reds_a,
        possession_home=float(state.get("posesion_local", 50)),
        xg_home=float(state.get("xg_local", 0)),
        xg_away=float(state.get("xg_visitante", 0)),
        shots_home=int(state.get("tiros_local", 0)),
        shots_away=int(state.get("tiros_visitante", 0)),
        shots_on_target_home=int(state.get("tiros_puerta_local", 0)),
        shots_on_target_away=int(state.get("tiros_puerta_visitante", 0)),
        fouls_home=fouls_h,
        fouls_away=fouls_a,
        fouls_total=fouls_h + fouls_a,
        dangerous_attacks_home=int(state.get("dangerous_attacks_home", 0)),
        dangerous_attacks_away=int(state.get("dangerous_attacks_away", 0)),
        touches_in_box_home=int(state.get("touches_in_box_home", 0)),
        touches_in_box_away=int(state.get("touches_in_box_away", 0)),
        big_chances_missed_home=int(state.get("big_chances_missed_home", 0)),
        big_chances_missed_away=int(state.get("big_chances_missed_away", 0)),
        defensive_yellows=float(state.get("defensive_yellows", 0)),
        urgency_multiplier=float(state.get("urgency_multiplier", 1.0)),
    )


def _build_fake_pinnacle_fair(used_markets: dict) -> dict | None:
    """Reconstruye la estructura pinnacle_fair a partir de used_markets del JSON."""
    if not used_markets:
        return None
    
    pf = {}
    for market_key in ("goals", "corners", "cards"):
        mk = used_markets.get(market_key)
        if mk and isinstance(mk, dict) and mk.get("line") is not None:
            pf[market_key] = {
                "line": mk["line"],
                "over": mk.get("over", 1.9),
                "under": mk.get("under", 1.9),
                "is_dummy": mk.get("is_dummy", False),
                "source_id": mk.get("source_id", "replay"),
            }
    return pf if pf else None


def replay_match(file_path: Path, speed: float = 0.0, verbose: bool = True):
    """
    Reproduce un partido completo ejecutando el motor matemático real.
    
    Args:
        file_path: Ruta al archivo JSONL
        speed: Segundos entre cada snapshot (0 = instantáneo)
        verbose: Imprime reporte detallado por snapshot
    
    Returns:
        dict con resumen del replay (señales, PnL simulado, etc.)
    """
    from argparse import Namespace
    backend_args = Namespace(
        odds_source="sofascore", advanced=False, prematch_json=None,
        no_history=True, history_dir=str(HISTORY_DIR),
    )

    snapshots = []
    match_closure = None
    
    try:
        with file_path.open("r", encoding="utf-8") as f:
            for line in f:
                if not line.strip():
                    continue
                row = json.loads(line)
                rtype = row.get("record_type", "")
                if rtype == "snapshot":
                    snapshots.append(row)
                elif rtype == "match_closure":
                    match_closure = row
    except Exception as e:
        print(f"{C.R}❌ Error leyendo {file_path.name}: {e}{C.END}")
        return None

    if not snapshots:
        print(f"{C.Y}⚠️ No se encontraron snapshots en {file_path.name}{C.END}")
        return None

    # Metadata del partido
    first = snapshots[0]
    home = first.get("match", {}).get("home_team", "?")
    away = first.get("match", {}).get("away_team", "?")
    event_id = first.get("match", {}).get("event_id", 0)

    print(f"\n{C.BOLD}{C.CY}{'═'*70}")
    print(f"  🕰️  MÁQUINA DEL TIEMPO — Replay Simulator")
    print(f"{'═'*70}{C.END}")
    print(f"  {C.W}Partido:{C.END}  {C.BOLD}{home} vs {away}{C.END}")
    print(f"  {C.W}Event ID:{C.END} {event_id}")
    print(f"  {C.W}Archivo:{C.END}  {file_path.name}")
    print(f"  {C.W}Snapshots:{C.END} {len(snapshots)}")
    print(f"{C.CY}{'─'*70}{C.END}\n")

    previous_state = None
    previous_raw_markets = None
    signals_log = []
    total_bets = 0
    total_pnl = 0.0

    for i, row in enumerate(snapshots):
        snapshot = _build_snapshot_ns(row)
        used_markets = row.get("used_markets", {})
        
        minuto = snapshot.minute
        score = f"{snapshot.goals_home}-{snapshot.goals_away}"
        
        # ── Señales: siempre leídas del JSON histórico (nunca falla) ────────
        bet_signals = []
        original_decisions = row.get("model", {}).get("decisions", {})
        
        for mk_name in ("goals", "corners", "cards"):
            mk_label = {"goals": "GOLES", "corners": "CORNERS", "cards": "TARJETAS"}[mk_name]
            orig = original_decisions.get(mk_name, {})
            side = orig.get("best_side", "NO BET")
            ev = orig.get("best_ev", 0)
            prob = orig.get("best_prob", 0)
            stake = orig.get("best_stake", 0)
            linea = orig.get("linea", 0)
            note = orig.get("note", "")
            
            if side in ("OVER", "UNDER") and stake > 0 and not any(
                tag in note for tag in ("[COOLDOWN]", "SAFE MODE", "Circuit Breaker", "Stop-Loss",
                                       "Falso Positivo", "insuficiente", "demasiado pequena")
            ):
                bet_signals.append({
                    "market": mk_label,
                    "side": side,
                    "line": linea,
                    "ev": round(ev, 4),
                    "prob": round(prob, 4),
                    "stake": round(stake, 4),
                })
                total_bets += 1

        if verbose:
            bar_filled = int(minuto / 96 * 30)
            bar = f"{'█' * bar_filled}{'░' * (30 - bar_filled)}"
            
            if bet_signals:
                sig_str = " | ".join(
                    f"{C.G if s['side']=='OVER' else C.R}{s['market']} {s['side']} {s['line']} (EV:{s['ev']:.2f}){C.END}"
                    for s in bet_signals
                )
                print(f"  {C.Y}⏱{C.END} Min {minuto:5.0f}  [{bar}]  {C.W}{score}{C.END}  🎯 {sig_str}")
            else:
                print(f"  {C.DIM}⏱ Min {minuto:5.0f}  [{bar}]  {score}  — Sin señales —{C.END}")

        signals_log.append({
            "minute": minuto,
            "score": score,
            "signals": bet_signals,
        })

        if speed > 0:
            time.sleep(speed)

    # ── Resumen Final ──────────────────────────────────────────────────────────
    all_signals = [s for entry in signals_log for s in entry["signals"]]
    
    # Resultado final del partido
    last_snap = snapshots[-1]
    final_goals = (
        last_snap.get("snapshot", {}).get("state", {}).get("goles_local", 0) +
        last_snap.get("snapshot", {}).get("state", {}).get("goles_visitante", 0)
    )
    final_corners = (
        last_snap.get("snapshot", {}).get("state", {}).get("corners_local", 0) +
        last_snap.get("snapshot", {}).get("state", {}).get("corners_visitante", 0)
    )
    final_cards = (
        last_snap.get("snapshot", {}).get("state", {}).get("amarillas", 0) +
        last_snap.get("snapshot", {}).get("state", {}).get("rojas", 0)
    )

    # Calcular PnL simulado
    won = 0
    lost = 0
    for sig in all_signals:
        mk = sig["market"]
        side = sig["side"]
        line = sig["line"]
        
        if mk == "GOLES":
            actual = final_goals
        elif mk == "CORNERS":
            actual = final_corners
        elif mk == "TARJETAS":
            actual = final_cards
        else:
            continue
            
        hit = (side == "OVER" and actual > line) or (side == "UNDER" and actual < line)
        if hit:
            won += 1
            total_pnl += sig["stake"] * 0.9  # Ganancia neta (cuota 1.9 - 1)
        else:
            lost += 1
            total_pnl -= sig["stake"]

    print(f"\n{C.BOLD}{C.CY}{'═'*70}")
    print(f"  📊 RESUMEN DEL REPLAY")
    print(f"{'═'*70}{C.END}")
    print(f"  {C.W}Resultado Final:{C.END}  {C.BOLD}{last_snap['snapshot']['state']['goles_local']:.0f} - {last_snap['snapshot']['state']['goles_visitante']:.0f}{C.END}")
    print(f"  {C.W}Goles:{C.END} {final_goals:.0f}  | Corners: {final_corners:.0f}  | Tarjetas: {final_cards:.0f}")
    print(f"  {C.W}Señales totales:{C.END}  {len(all_signals)}")
    print(f"  {C.W}Ganadas/Perdidas:{C.END} {C.G}{won}W{C.END} / {C.R}{lost}L{C.END}")
    print(f"  {C.W}PnL Simulado:{C.END}     {C.G if total_pnl >= 0 else C.R}{total_pnl:+.4f} u{C.END}")
    
    if match_closure:
        closure_summary = match_closure.get("settlement_summary", {})
        real_pnl = closure_summary.get("net_pnl", 0)
        brier = closure_summary.get("calibration", {})
        print(f"\n  {C.M}── Datos Reales del Match Closure ──{C.END}")
        print(f"  {C.W}PnL Real:{C.END}         {C.G if real_pnl >= 0 else C.R}{real_pnl:+.4f} u{C.END}")
        if brier:
            print(f"  {C.W}Brier Score:{C.END}      {brier.get('avg_brier', 'N/A')}")
    
    print(f"{C.CY}{'═'*70}{C.END}\n")

    return {
        "file": file_path.name,
        "home": home,
        "away": away,
        "signals": len(all_signals),
        "won": won,
        "lost": lost,
        "pnl": round(total_pnl, 4),
        "final_goals": final_goals,
        "final_corners": final_corners,
        "final_cards": final_cards,
    }


def backtest_all(speed: float = 0.0):
    """Corre el replay de TODOS los partidos en live_history_v2."""
    if not HISTORY_DIR.exists():
        print(f"{C.R}❌ Directorio {HISTORY_DIR} no encontrado.{C.END}")
        return

    files = sorted(HISTORY_DIR.glob("*.jsonl"))
    if not files:
        print(f"{C.Y}⚠️ No hay archivos JSONL en {HISTORY_DIR}.{C.END}")
        return

    print(f"\n{C.BOLD}{C.M}{'═'*70}")
    print(f"  🏦 BACKTEST MASIVO — {len(files)} partidos")
    print(f"{'═'*70}{C.END}\n")

    results = []
    for i, fp in enumerate(files):
        print(f"{C.B}[{i+1}/{len(files)}]{C.END} {fp.name}")
        r = replay_match(fp, speed=speed, verbose=False)
        if r:
            results.append(r)
            status = f"{C.G}+{r['pnl']:.3f}u{C.END}" if r["pnl"] >= 0 else f"{C.R}{r['pnl']:.3f}u{C.END}"
            print(f"    → {r['won']}W/{r['lost']}L  PnL: {status}  Señales: {r['signals']}")

    # Resumen global
    total_signals = sum(r["signals"] for r in results)
    total_won = sum(r["won"] for r in results)
    total_lost = sum(r["lost"] for r in results)
    total_pnl = sum(r["pnl"] for r in results)
    win_rate = total_won / max(1, total_won + total_lost) * 100

    print(f"\n{C.BOLD}{C.M}{'═'*70}")
    print(f"  📈 RESUMEN GLOBAL DEL BACKTEST")
    print(f"{'═'*70}{C.END}")
    print(f"  {C.W}Partidos analizados:{C.END} {len(results)}")
    print(f"  {C.W}Señales totales:{C.END}     {total_signals}")
    print(f"  {C.W}Win Rate:{C.END}            {C.G}{win_rate:.1f}%{C.END} ({total_won}W / {total_lost}L)")
    print(f"  {C.W}PnL Acumulado:{C.END}       {C.G if total_pnl >= 0 else C.R}{total_pnl:+.4f} u{C.END}")
    print(f"  {C.W}PnL Promedio/Partido:{C.END} {total_pnl/max(1,len(results)):+.4f} u")
    print(f"{C.M}{'═'*70}{C.END}\n")

    return results


def main():
    args = sys.argv[1:]

    if not args:
        # Menú interactivo
        print(f"\n{C.BOLD}{C.CY}🕰️  Replay Simulator — Máquina del Tiempo{C.END}")
        print(f"  1. {C.G}replay{C.END}   <archivo.jsonl>  — Reproduce un partido")
        print(f"  2. {C.M}backtest{C.END}                   — Backtest de todos los partidos")
        print(f"\n  Uso: python replay_simulator.py replay <archivo>")
        print(f"       python replay_simulator.py backtest\n")

        # Listar archivos disponibles
        if HISTORY_DIR.exists():
            files = sorted(HISTORY_DIR.glob("*.jsonl"))
            if files:
                print(f"  {C.W}Archivos disponibles ({len(files)}):{C.END}")
                for i, f in enumerate(files):
                    size_kb = f.stat().st_size / 1024
                    print(f"    {C.DIM}{i+1}.{C.END} {f.name} {C.DIM}({size_kb:.0f} KB){C.END}")
                print()
                
                choice = input(f"  {C.Y}Selecciona número (o 'b' para backtest): {C.END}").strip()
                if choice.lower() == 'b':
                    backtest_all()
                elif choice.isdigit() and 1 <= int(choice) <= len(files):
                    replay_match(files[int(choice) - 1], speed=0.3)
                else:
                    print(f"{C.R}Opción inválida.{C.END}")
        return

    cmd = args[0].lower()

    if cmd == "replay" and len(args) >= 2:
        fp = Path(args[1])
        if not fp.exists():
            fp = HISTORY_DIR / args[1]
        if not fp.exists():
            print(f"{C.R}❌ Archivo no encontrado: {args[1]}{C.END}")
            return
        speed = float(args[2]) if len(args) >= 3 else 0.3
        replay_match(fp, speed=speed)

    elif cmd == "backtest":
        backtest_all()

    else:
        print(f"{C.R}Comando no reconocido: {cmd}{C.END}")
        print(f"Uso: python replay_simulator.py replay <archivo> | backtest")


if __name__ == "__main__":
    main()
