#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
ML Feature Extractor (ml_feature_extractor.py)
----------------------------------------------
Lee los registros JSONL de `live_history_v2`, deriva features tacticos 
(tib_ratio, danger_conversion, momentum_xg_5m) y arma un dataset tabular 
seguro sin Time-Series Leakage.

El target principal es: `target_goal_15m` (¿Hubo al menos 1 gol en los proximos 15 min?).
"""

import json
from pathlib import Path
import csv

HISTORY_DIR = Path("live_history_v2")
OUTPUT_CSV = Path("ml_dataset_live.csv")

def extract_features(row_dict: dict) -> dict:
    state = row_dict.get("snapshot", {}).get("state", {})
    model = row_dict.get("model", {})
    
    minuto = float(state.get("minuto", 0.0))
    goles_local = float(state.get("goles_local", 0.0))
    goles_visit = float(state.get("goles_visitante", 0.0))
    total_goals = goles_local + goles_visit
    
    posesion_local = float(state.get("posesion_local", 50.0))
    posesion_visit = 100.0 - posesion_local if posesion_local > 0 else 50.0
    
    tib_home = float(state.get("touches_in_box_home", 0.0))
    tib_away = float(state.get("touches_in_box_away", 0.0))
    
    da_home = float(state.get("dangerous_attacks_home", 0.0))
    da_away = float(state.get("dangerous_attacks_away", 0.0))
    
    tp_home = float(state.get("tiros_puerta_local", 0.0))
    tp_away = float(state.get("tiros_puerta_visitante", 0.0))
    
    xg_total = float(state.get("xg_local", 0.0)) + float(state.get("xg_visitante", 0.0))
    
    # Feature Derivation
    tib_ratio_home = tib_home / max(posesion_local, 1.0)
    tib_ratio_away = tib_away / max(posesion_visit, 1.0)
    
    danger_conversion_home = tp_home / max(da_home, 1.0)
    danger_conversion_away = tp_away / max(da_away, 1.0)
    
    used_markets = row_dict.get("used_markets", {})
    latency_ms = float(used_markets.get("latency_ms", 0.0))
    is_dummy = False
    for mk in ["goals", "corners", "cards"]:
        m_data = used_markets.get(mk)
        if isinstance(m_data, dict) and m_data.get("is_dummy"):
            is_dummy = True
            break
    
    valid_for_ml = 1 if (not is_dummy and latency_ms < 200.0) else 0

    return {
        "event_id": row_dict.get("match", {}).get("event_id", ""),
        "minuto": minuto,
        "valid_for_ml": valid_for_ml,
        "latency_ms": round(latency_ms, 2),
        "total_goals": total_goals,
        "xg_total": xg_total,
        "score_diff": goles_local - goles_visit,
        "red_card_diff": float(state.get("rojas_local", 0.0)) - float(state.get("rojas_visitante", 0.0)),
        "total_shots": float(state.get("tiros_local", 0.0)) + float(state.get("tiros_visitante", 0.0)),
        
        "tib_ratio_home": round(tib_ratio_home, 4),
        "tib_ratio_away": round(tib_ratio_away, 4),
        "danger_conversion_home": round(danger_conversion_home, 4),
        "danger_conversion_away": round(danger_conversion_away, 4),
        
        "poisson_lambda_goals": round(float(model.get("lambda_goals", 0.0)), 4),
        "poisson_lambda_corners": round(float(model.get("lambda_corners", 0.0)), 4),
        "poisson_lambda_cards": round(float(model.get("lambda_cards", 0.0)), 4),
        "danger_rate": round(float(model.get("danger_rate", 0.0)), 4),
        "tension_index": round(float(model.get("tension_index", 0.0)), 4),
    }

def process_file(file_path: Path) -> list:
    lines = []
    
    # Check if match_closure is present
    has_closure = False
    
    try:
        with file_path.open("r", encoding="utf-8") as f:
            for text in f:
                if not text.strip():
                    continue
                row = json.loads(text)
                if row.get("record_type") == "snapshot":
                    lines.append(row)
                elif row.get("record_type") == "match_closure":
                    has_closure = True
    except Exception as e:
        print(f"Error reading {file_path.name}: {e}")
        return []
        
    dataset_rows = []
    
    for i, row in enumerate(lines):
        feat = extract_features(row)
        current_min = feat["minuto"]
        current_goals = feat["total_goals"]
        current_xg = feat["xg_total"]
        
        # 1. Momentum xG 5m
        # Buscar la fila más cercana a (current_min - 5)
        past_xg = current_xg
        for past_idx in range(i-1, -1, -1):
            past_feat = extract_features(lines[past_idx])
            if current_min - past_feat["minuto"] >= 5.0:
                past_xg = past_feat["xg_total"]
                break
            # Si no hay data de hace >5m exacta, tomamos la más antigua posible si estamos entre 0 y 5 min
            past_xg = past_feat["xg_total"]
            
        momentum_xg_5m = current_xg - past_xg
        feat["momentum_xg_5m"] = round(momentum_xg_5m, 4)
        
        # 2. Target Goal 15m
        target_goal_15m = 0
        valid_target = False
        
        # Look ahead exactly in the stream
        for future_idx in range(i+1, len(lines)):
            future_feat = extract_features(lines[future_idx])
            future_min = future_feat["minuto"]
            
            if future_min - current_min <= 15.0:
                if future_feat["total_goals"] > current_goals:
                    target_goal_15m = 1
                    valid_target = True
                    break
            else:
                # Ya sobrepasamos los 15 minutos sin goles nuevos
                valid_target = True
                break
                
        # Si corrimos todo el loop temporal y no llegamos al offset de 15 minutos:
        if not valid_target:
            # Check si el partido se acabó (min > 90 o hay closure)
            last_min = extract_features(lines[-1])["minuto"] if lines else current_min
            if current_min >= 75.0 and (last_min >= 90.0 or has_closure):
                # El partido terminó y estaban en los últimos 15 min
                valid_target = True
                
        if valid_target:
            feat["target_goal_15m"] = target_goal_15m
            dataset_rows.append(feat)
            
    return dataset_rows

def main():
    print(f"Buscando archivos temporales en {HISTORY_DIR}...")
    if not HISTORY_DIR.exists():
        print("El directorio de historial no existe.")
        return
        
    all_rows = []
    files_processed = 0
    
    for file_path in HISTORY_DIR.glob("*.jsonl"):
        rows = process_file(file_path)
        all_rows.extend(rows)
        files_processed += 1
        
    if not all_rows:
        print("No se extrajeron vectores válidos.")
        return
        
    keys = list(all_rows[0].keys())
    
    with OUTPUT_CSV.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys)
        writer.writeheader()
        writer.writerows(all_rows)
        
    print(f"¡Éxito! CSV Generado: {OUTPUT_CSV}")
    print(f"Archivos procesados: {files_processed}")
    print(f"Filas generadas (Vectores): {len(all_rows)}")

if __name__ == "__main__":
    main()
