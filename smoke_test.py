import server
from pathlib import Path
import os, json
import futbol_live_betting_probabilities as fp

# Fake minimal classes
class DummySnapshot:
    event_id = 999999
    match_url = "https://mock.com"
    home_team = "A"
    away_team = "B"
    tournament = "Mock"
    status_text = "2nd half"
    notes = []
    goals_market = None
    corners_market = None
    cards_market = None

snap = DummySnapshot()

state = fp.MatchState(
    minuto=46.0, goles_local=1, goles_visitante=0, amarillas=1, rojas=0, faltas=5, corners=2, 
    xg_local=0.5, xg_visitante=0.1, tiros_local=3, tiros_visitante=1, tiros_puerta_local=1, tiros_puerta_visitante=0, 
    posesion_local=60, corners_local=2, corners_visitante=0, faltas_local=2, faltas_visitante=3, amarillas_local=0, 
    amarillas_visitante=1, rojas_local=0, rojas_visitante=0, corners_recientes=0,xg_recientes=0,tiros_recientes=0,
    ventana_reciente_min=0,faltas_recientes=0,tarjetas_recientes=0,centros_local=0,centros_visitante=0,
    centros_recientes=0,referee_name='Referee',urgency_multiplier=1.0,defensive_yellows=1.0, 
    touches_in_box_home=12.0, touches_in_box_away=0.0, big_chances_missed_home=1, big_chances_missed_away=0
)

import numpy as np
result = fp.ModelResult(
    state=state,
    markets=fp.MarketSet(
        goles=fp.MarketLine(2.5, 1.9, 1.9),
        corners=fp.MarketLine(9.5, 1.9, 1.9),
        tarjetas=fp.MarketLine(4.5, 1.9, 1.9)
    ),
    params=server.PARAMETER_PRESETS["balanced"],
    phase_name="Test",
    phase_summary="Test",
    remaining_minutes=44.0,
    danger_rate=0.5,
    tension_index=0.2,
    urgency_factor=1.0,
    lambda_goals=1.0,
    lambda_corners=3.0,
    lambda_cards=2.0,
    acceleration_weight=0.5,
    cooldown_weight=0.5,
    neutral_weight=0.0,
    total_goals=np.array([1, 2, 3]),
    total_corners=np.array([1, 2, 3]),
    total_cards=np.array([1, 2, 3]),
    wpf_index=0.45,
    siege_index=1.2,
    tightrope_boost=1.15
)

lp = fp.LeagueProfile('demo', 'demo', 2.5, 9.0, 4.0)

pinnacle_mock = {
    "GOLES": {"linea": 2.5, "over": 1.95, "under": 1.95},
    "_last_updated_GOLES": 123456789
}

out_file = fp.append_live_history(
    Path('live_history_v2'), 
    snap, 
    state, 
    result.markets, 
    server.backend_args, 
    lp, 
    None, 
    result, 
    pinnacle_fair=pinnacle_mock
)

with open(out_file, 'r', encoding='utf-8') as f:
    last_line = f.readlines()[-1]
    data = json.loads(last_line)
    
    print("\n")
    print("🎯 ¿Aparece la llave 'micro_stats'?:", "SI" if "micro_stats" in data["model"] else "NO")
    print("📈 Micro Stats JSON:")
    print(json.dumps(data["model"].get("micro_stats"), indent=2))
    
    print("\n")
    print("🎯 ¿Pinnacle Baseline esta presente?:", "SI" if "pinnacle_baseline" in data["model"] else "NO")
    print("📊 Pinnacle Baseline JSON:")
    print(json.dumps(data["model"].get("pinnacle_baseline"), indent=2))

    print("\n")
    print("🎯 ¿Tightrope boost esta trackeado?:", data["model"].get("micro_stats", {}).get("tightrope_boost"))
