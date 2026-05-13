import os
import glob
import json
import csv


def build_ml_dataset():
    history_dir = 'live_history_v2'
    output_csv = 'ml_dataset_clean.csv'
    files = glob.glob(os.path.join(history_dir, '*.jsonl'))

    print(f"Procesando {len(files)} archivos en {history_dir}...")

    # Definimos las columnas que extraeremos para el Machine Learning
    headers = [
        "match_id", "minute",
        "current_goals", "current_corners", "current_cards",
        "xg_local", "xg_visitante", "possession_home",

        # Metricas tacticas del motor
        "danger_rate", "tension_index", "urgency_factor",

        # Feature Engineering — v1 (Ratios por minuto)
        "attacks_per_minute", "xg_per_minute", "possession_dominance",

        # Feature Engineering — v2 (Contexto del Marcador)
        #   goal_diff: Diferencia CON SIGNO (local - visitante).
        #              Positivo = local gana, Negativo = local pierde.
        #              El abs() destruia la informacion tactica — ahora usamos el signo.
        "goal_diff",

        # Feature Engineering — v3 (Danger Rate con contexto)
        #   danger_rate_recent: "Tormenta Aguda" — presion en la ventana reciente.
        #                        Captura freneticos finales de partido que el danger_rate
        #                        global aplasta por el denominador alto.
        #   contextual_danger:  "Desesperacion Dirigida" — danger_rate ponderado por la
        #                        urgencia tactica real del marcador y el tiempo.
        #                        Un danger_rate de 0.18 perdiendo 0-1 en min 80
        #                        vale MAS que el mismo valor ganando 2-0 en min 20.
        "danger_rate_recent",
        "contextual_danger",

        # Cuotas (Mercados)
        "goals_line", "goals_over_odds", "goals_under_odds",
        "corners_line", "corners_over_odds", "corners_under_odds",

        # Variable Objetivo Cruda
        "final_goals", "final_corners", "final_cards",

        # Target Labeling (Para Clasificacion Binaria 1=Gana, 0=Pierde)
        "label_goals_over", "label_corners_over"
    ]

    valid_matches = 0
    discarded_matches = 0
    total_snapshots_extracted = 0

    with open(output_csv, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=headers)
        writer.writeheader()

        for fpath in files:
            # 1. Primera pasada rapida para verificar si tiene "match_closure"
            has_closure = False
            final_totals = {}

            with open(fpath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in reversed(lines):  # Buscamos de abajo hacia arriba (mas rapido)
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        if record.get('record_type') == 'match_closure':
                            has_closure = True
                            final_totals = record.get('final_totals', {})
                            break
                    except json.JSONDecodeError:
                        continue

            # 2. Si no tiene cierre, descartar el archivo por completo (Data Poisoning Drop)
            if not has_closure:
                discarded_matches += 1
                continue

            valid_matches += 1
            match_id = (
                os.path.basename(fpath).split('_')[1]
                if '_' in os.path.basename(fpath) else "unknown"
            )

            # 3. Segunda pasada: Extraer los features de cada snapshot
            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if record.get('record_type') in ('snapshot', 'live_snapshot'):
                        snap     = record.get('snapshot', {})
                        state    = snap.get('state', {})
                        model    = record.get('model', {})
                        used_mkts = record.get('used_markets', {})

                        # ── Variables base ────────────────────────────────────
                        minute        = float(state.get('minuto', 0))
                        goals_home    = float(state.get('goles_local', 0))
                        goals_away    = float(state.get('goles_visitante', 0))
                        danger_rate   = float(model.get('danger_rate', 0))

                        # ── Feature v2: goal_diff CON SIGNO ───────────────────
                        # No usamos abs(). El signo es la informacion tactica.
                        # Positivo: local gana (danger_rate es del rival desesperado)
                        # Negativo: local pierde (danger_rate es presion ofensiva real)
                        goal_diff = goals_home - goals_away

                        # ── Feature v3a: Danger Rate Reciente ("Tormenta Aguda") ──
                        # Usa los tiros de la ventana reciente divididos entre esa
                        # ventana corta — no entre el minuto total del partido.
                        # Esto captura el frentico final que el danger_rate global aplasta.
                        tiros_recientes    = float(state.get('tiros_recientes', 0))
                        ventana_reciente   = float(state.get('ventana_reciente_min', 0))
                        tiros_puerta_local  = float(state.get('tiros_puerta_local', 0))
                        tiros_puerta_away   = float(state.get('tiros_puerta_visitante', 0))

                        if ventana_reciente > 0 and tiros_recientes > 0:
                            # Usamos la misma formula del motor pero sobre la ventana corta
                            danger_rate_recent = tiros_recientes / ventana_reciente
                        else:
                            # Fallback: repetir el danger_rate global
                            danger_rate_recent = danger_rate

                        # ── Feature v3b: Contextual Danger ("Desesperacion Dirigida") ──
                        # Pondera el danger_rate por la urgencia tactica y temporal.
                        #
                        # desperation_factor:
                        #   Rango [-1, +1]
                        #   -1 = local pierde por 2+ → el danger_rate es presion real
                        #   0  = empate → danger_rate es neutro
                        #   +1 = local gana por 2+ → el danger_rate es del rival, baja importancia
                        #   El factor se aplica NEGATIVAMENTE porque si el local gana, el peligro
                        #   es del contrario (contraataque), no presion propia.
                        #
                        # time_weight:
                        #   Crece linealmente desde 1.0 (min 45) hasta 1.5 (min 90).
                        #   Un danger_rate de 0.18 en el min 85 es MAS importante
                        #   que el mismo valor en el min 25. Antes del min 45, es 1.0.
                        desperation_factor = max(-1.0, min(1.0, -goal_diff / 2.0))
                        time_weight        = min(1.5, 1.0 + max(0.0, minute - 45.0) / 90.0)
                        contextual_danger  = danger_rate * (1.0 + desperation_factor * 0.4) * time_weight

                        # ── Cuotas ────────────────────────────────────────────
                        goals_line       = used_mkts.get('goals', {}).get('line', 0) if used_mkts.get('goals') else 0
                        goals_over_odds  = used_mkts.get('goals', {}).get('over', 0) if used_mkts.get('goals') else 0
                        goals_under_odds = used_mkts.get('goals', {}).get('under', 0) if used_mkts.get('goals') else 0
                        corners_line         = used_mkts.get('corners', {}).get('line', 0) if used_mkts.get('corners') else 0
                        corners_over_odds    = used_mkts.get('corners', {}).get('over', 0) if used_mkts.get('corners') else 0
                        corners_under_odds   = used_mkts.get('corners', {}).get('under', 0) if used_mkts.get('corners') else 0

                        # ── Filtros de Integridad ─────────────────────────────
                        go = goals_over_odds
                        gu = goals_under_odds

                        # Descartar cuotas Dummy de SofaScore (1.85/1.85 o 1.90/1.90)
                        if (go == 1.85 and gu == 1.85) or (go == 1.90 and gu == 1.90):
                            continue

                        # Descartar cuotas en cero (scraper no envio datos)
                        if go == 0 or gu == 0:
                            continue

                        # ── Escribir fila ─────────────────────────────────────
                        row = {
                            "match_id":            match_id,
                            "minute":              minute,
                            "current_goals":       goals_home + goals_away,
                            "current_corners":     state.get('corners', 0),
                            "current_cards":       state.get('amarillas', 0) + (state.get('rojas', 0) * 2),
                            "xg_local":            state.get('xg_local', 0),
                            "xg_visitante":        state.get('xg_visitante', 0),
                            "possession_home":     state.get('posesion_local', 50),

                            "danger_rate":         danger_rate,
                            "tension_index":       model.get('tension_index', 0),
                            "urgency_factor":      model.get('urgency_factor', 1.0),

                            "attacks_per_minute":  (state.get('dangerous_attacks_home', 0) + state.get('dangerous_attacks_away', 0)) / max(1, minute),
                            "xg_per_minute":       (state.get('xg_local', 0) + state.get('xg_visitante', 0)) / max(1, minute),
                            "possession_dominance": abs(state.get('posesion_local', 50) - 50),

                            "goal_diff":           goal_diff,
                            "danger_rate_recent":  round(danger_rate_recent, 5),
                            "contextual_danger":   round(contextual_danger, 5),

                            "goals_line":          goals_line,
                            "goals_over_odds":     go,
                            "goals_under_odds":    gu,
                            "corners_line":        corners_line,
                            "corners_over_odds":   corners_over_odds,
                            "corners_under_odds":  corners_under_odds,

                            "final_goals":         final_totals.get('goals', 0),
                            "final_corners":       final_totals.get('corners', 0),
                            "final_cards":         final_totals.get('cards', 0),

                            "label_goals_over":    1 if final_totals.get('goals', 0) > goals_line else 0,
                            "label_corners_over":  1 if final_totals.get('corners', 0) > corners_line else 0,
                        }

                        writer.writerow(row)
                        total_snapshots_extracted += 1

                except Exception:
                    pass

    print("\nDataset limpio generado con exito!")
    print(f"Archivo creado: {output_csv}")
    print("-" * 30)
    print(f"Partidos validos procesados: {valid_matches}")
    print(f"Partidos huerfanos DESCARTADOS: {discarded_matches}")
    print(f"Total de Filas (Samples) para ML: {total_snapshots_extracted}")
    print(f"Columnas por muestra: {len(headers)}")


if __name__ == "__main__":
    build_ml_dataset()
