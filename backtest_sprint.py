import glob
import json
import os
from collections import defaultdict
from dataclasses import asdict

from futbol_live_betting_probabilities import (
    MatchState,
    build_params,
    PARAMETER_PRESETS,
    MarketLine,
    run_model,
    market_summary,
    apply_market_guardrails
)

def sprint_backtest(history_dir: str = "live_history"):
    files = glob.glob(os.path.join(history_dir, "*.jsonl"))
    
    total_over_corners_before = 0
    total_over_corners_now = 0
    wins_now = 0

    params = PARAMETER_PRESETS["balanced"]

    for file_path in files:
        with open(file_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
            
        snapshots = [json.loads(line) for line in lines if "record_type" not in json.loads(line)]
        if not snapshots:
            continue
            
        final_state = snapshots[-1].get("snapshot", {}).get("state", {})
        final_corners = final_state.get("corners", 0)
        
        # Track if we bet OVER inside this match originally and now
        bet_over_before = False
        bet_over_now = False
        over_line = 0

        for snap in snapshots:
            state_dict = snap.get("snapshot", {}).get("state", {})
            
            # Reconstruction backwards compatible
            state = MatchState(
                minuto=state_dict.get("minuto", 0),
                goles_local=state_dict.get("goles_local", 0),
                goles_visitante=state_dict.get("goles_visitante", 0),
                amarillas=state_dict.get("amarillas", 0),
                rojas=state_dict.get("rojas", 0),
                faltas=state_dict.get("faltas", 0),
                corners=state_dict.get("corners", 0),
                xg_local=state_dict.get("xg_local", 0),
                xg_visitante=state_dict.get("xg_visitante", 0),
                tiros_local=state_dict.get("tiros_local", 0),
                tiros_visitante=state_dict.get("tiros_visitante", 0),
                tiros_puerta_local=state_dict.get("tiros_puerta_local", 0),
                tiros_puerta_visitante=state_dict.get("tiros_puerta_visitante", 0),
                posesion_local=state_dict.get("posesion_local", 50),
                corners_local=state_dict.get("corners_local", 0),
                corners_visitante=state_dict.get("corners_visitante", 0),
                faltas_local=state_dict.get("faltas_local", 0),
                faltas_visitante=state_dict.get("faltas_visitante", 0),
                amarillas_local=state_dict.get("amarillas_local", 0),
                amarillas_visitante=state_dict.get("amarillas_visitante", 0),
                rojas_local=state_dict.get("rojas_local", 0),
                rojas_visitante=state_dict.get("rojas_visitante", 0),
                corners_recientes=state_dict.get("corners_recientes", 0),
                xg_recientes=state_dict.get("xg_recientes", 0),
                tiros_recientes=state_dict.get("tiros_recientes", 0),
                ventana_reciente_min=state_dict.get("ventana_reciente_min", 0),
                faltas_recientes=state_dict.get("faltas_recientes", 0),
                tarjetas_recientes=state_dict.get("tarjetas_recientes", 0),
                centros_local=state_dict.get("centros_local", 0),
                centros_visitante=state_dict.get("centros_visitante", 0),
                centros_recientes=state_dict.get("centros_recientes", 0),
                touches_in_box_home=state_dict.get("touches_in_box_home", 0),
                touches_in_box_away=state_dict.get("touches_in_box_away", 0),
                big_chances_missed_home=state_dict.get("big_chances_missed_home", 0),
                big_chances_missed_away=state_dict.get("big_chances_missed_away", 0),
            )
            
            # Check what old model said (from json)
            old_decisions = snap.get("model", {}).get("decisions", {})
            if old_decisions.get("CORNERS", {}).get("best_side") == "OVER":
                bet_over_before = True
            
            markets_snap = snap.get("snapshot", {}).get("markets", {})
            if not markets_snap: continue
            
            mc = markets_snap.get("corners")
            if not mc: continue
            
            class DummyMarkets:
                goles = None
                corners = MarketLine(linea=mc.get("linea",0), over=mc.get("over",0), under=mc.get("under",0))
                tarjetas = None
                
            markets = DummyMarkets()
            
            # Run new model
            try:
                result = run_model(state, markets, params, prematch=None)
                decision = market_summary("CORNERS", result.total_corners, markets.corners, params, state)
                decision = apply_market_guardrails("CORNERS", decision, state, params, None)
                
                if decision.best_side == "OVER":
                    bet_over_now = True
                    over_line = decision.linea
            except Exception:
                pass
                
        if bet_over_before:
            total_over_corners_before += 1
        
        if bet_over_now:
            total_over_corners_now += 1
            if final_corners > over_line:
                wins_now += 1

    print("===== SPRINT BACKTEST (WPF & SIEGE) =====")
    print(f"Total Partidos analizados: {len(files)}")
    print(f"Señales OVER Corners ANTES: {total_over_corners_before}")
    print(f"Señales OVER Corners AHORA: {total_over_corners_now}")
    if total_over_corners_now > 0:
        winrate = (wins_now / total_over_corners_now) * 100
        print(f"Wins OVER Corners: {wins_now} ({winrate:.1f}%)")
    else:
        print("Wins OVER Corners: 0 (No se disparó apostar ningún OVER)")
    print("Mision: Reducir los OVER Corners perdedores y aumentar la calidad del filtro")

if __name__ == "__main__":
    sprint_backtest()
