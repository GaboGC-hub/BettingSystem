import os
import glob
import json
import csv
import math


def build_ml_dataset_v2():
    history_dir = 'live_history_v2'
    output_csv = 'ml_dataset_v2.csv'
    files = glob.glob(os.path.join(history_dir, '*.jsonl'))

    print(f"Procesando {len(files)} archivos en {history_dir} para ML V2...")

    headers = [
        # Identidad
        "match_id", "minute", "remaining_minutes",
        # Estado actual del partido
        "current_goals", "current_corners", "current_cards",
        "goal_diff",
        # xG y posesion
        "xg_local", "xg_visitante", "xg_total",
        "possession_home", "possession_dominance",
        # Ventana reciente (tormenta aguda)
        "xg_recientes", "corners_recientes", "faltas_recientes",
        "danger_rate_recent",
        # Metricas tacticas del motor
        "danger_rate", "tension_index", "urgency_factor",
        "phase_name",
        # Feature engineering
        "xg_per_minute", "attacks_per_minute",
        "contextual_danger",
        # Cuotas de goles
        "goals_line", "goals_over_odds", "goals_under_odds",
        "goals_implied_prob_over", "goals_implied_prob_under",
        "goals_market_spread",
        # Cuotas de corners
        "corners_line", "corners_over_odds", "corners_under_odds",
        "corners_implied_prob_over", "corners_implied_prob_under",
        "corners_market_spread",
        # Faro Pinnacle (sharp baseline)
        "pinnacle_goals_over", "pinnacle_goals_under",
        "pinnacle_goals_spread",
        # Categoricas
        "tournament_slug", "category_name",
        # Ratios compuestos
        "corners_per_xg", "xg_per_shot",
        # Attack zones (12 zonas normalizadas por equipo = 24 features)
        *[f"atk_zone_home_{i}" for i in range(12)],
        *[f"atk_zone_away_{i}" for i in range(12)],
        # Variables objetivo
        "final_goals", "final_corners", "final_cards",
        "goals_vs_line_delta",
        "label_goals_over", "label_goals_push", "label_goals_under",
        "label_corners_over", "label_corners_push", "label_corners_under",
    ]

    valid_matches = 0
    discarded_matches = 0
    total_snapshots = 0

    with open(output_csv, 'w', newline='', encoding='utf-8') as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=headers)
        writer.writeheader()

        for fpath in files:
            has_closure = False
            final_totals = {}

            with open(fpath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                for line in reversed(lines):
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

            if not has_closure:
                discarded_matches += 1
                continue

            valid_matches += 1
            match_id = (
                os.path.basename(fpath).split('_')[1]
                if '_' in os.path.basename(fpath) else "unknown"
            )

            for line in lines:
                if not line.strip():
                    continue
                try:
                    record = json.loads(line)
                    if record.get('record_type') not in ('snapshot', 'live_snapshot'):
                        continue

                    snap       = record.get('snapshot', {})
                    state      = snap.get('state', {})
                    model      = record.get('model', {})
                    used_mkts  = record.get('used_markets', {})
                    match_info = record.get('match', {})
                    league     = record.get('league_profile', {})

                    # ── Variables base ────────────────────────────────────
                    minute          = float(state.get('minuto', 0))
                    remaining       = float(model.get('remaining_minutes', max(0, 90 - minute)))
                    goals_home      = float(state.get('goles_local', 0))
                    goals_away      = float(state.get('goles_visitante', 0))
                    current_goals   = goals_home + goals_away
                    goal_diff       = goals_home - goals_away
                    danger_rate     = float(model.get('danger_rate', 0))

                    # ── Cuotas ────────────────────────────────────────────
                    goals_line        = used_mkts.get('goals', {}).get('line', 0)
                    goals_over_odds   = used_mkts.get('goals', {}).get('over', 0)
                    goals_under_odds  = used_mkts.get('goals', {}).get('under', 0)
                    corners_line       = used_mkts.get('corners', {}).get('line', 0)
                    corners_over_odds  = used_mkts.get('corners', {}).get('over', 0)
                    corners_under_odds = used_mkts.get('corners', {}).get('under', 0)

                    go = goals_over_odds
                    gu = goals_under_odds
                    co = corners_over_odds
                    cu = corners_under_odds

                    # Descartar cuotas dummy o nulas
                    if (go == 1.85 and gu == 1.85) or (go == 1.90 and gu == 1.90):
                        continue
                    if go == 0 or gu == 0:
                        continue

                    # ── Implied probabilities ─────────────────────────────
                    goals_imp_over   = round(1.0 / go, 5) if go > 1.0 else 0.0
                    goals_imp_under  = round(1.0 / gu, 5) if gu > 1.0 else 0.0
                    goals_spread     = round(abs(go - gu), 4)
                    corners_imp_over  = round(1.0 / co, 5) if co > 1.0 else 0.0
                    corners_imp_under = round(1.0 / cu, 5) if cu > 1.0 else 0.0
                    corners_spread    = round(abs(co - cu), 4)

                    # ── Pinnacle fair ─────────────────────────────────────
                    pinnacle_base = model.get('pinnacle_baseline', {}) or {}
                    pinnacle_goals = (pinnacle_base.get('GOLES') or [None])[0] if pinnacle_base.get('GOLES') else None
                    p_g_over  = float(pinnacle_goals.get('over', 0)) if pinnacle_goals else 0.0
                    p_g_under = float(pinnacle_goals.get('under', 0)) if pinnacle_goals else 0.0
                    p_g_spread = round(abs(p_g_over - p_g_under), 4) if p_g_over and p_g_under else 0.0

                    # ── Attack zones ──────────────────────────────────────
                    atk_home = state.get('attack_zones_home', ()) or ()
                    atk_away = state.get('attack_zones_away', ()) or ()
                    atk_home_flat = [float(atk_home[i]) if i < len(atk_home) else 0.0 for i in range(12)]
                    atk_away_flat = [float(atk_away[i]) if i < len(atk_away) else 0.0 for i in range(12)]

                    # ── Ventana reciente ──────────────────────────────────
                    xg_recientes      = float(state.get('xg_recientes', 0))
                    corners_recientes = float(state.get('corners_recientes', 0))
                    faltas_recientes  = float(state.get('faltas_recientes', 0))
                    ventana_reciente  = float(state.get('ventana_reciente_min', 0))

                    if ventana_reciente > 0:
                        danger_rate_recent = float(state.get('tiros_recientes', 0)) / ventana_reciente
                    else:
                        danger_rate_recent = danger_rate

                    # ── Contextual danger ─────────────────────────────────
                    desperation_factor = max(-1.0, min(1.0, -goal_diff / 2.0))
                    time_weight        = min(1.5, 1.0 + max(0.0, minute - 45.0) / 90.0)
                    contextual_danger  = round(danger_rate * (1.0 + desperation_factor * 0.4) * time_weight, 5)

                    # ── Ratios compuestos ─────────────────────────────────
                    xg_total     = float(state.get('xg_local', 0)) + float(state.get('xg_visitante', 0))
                    current_corn = float(state.get('corners', 0))
                    shots_total  = float(state.get('tiros_local', 0)) + float(state.get('tiros_visitante', 0))
                    corners_per_xg = round(current_corn / max(0.01, xg_total), 4) if xg_total > 0 else 0.0
                    xg_per_shot    = round(xg_total / max(1, shots_total), 4) if shots_total > 0 else 0.0

                    # ── Target labeling con 3 clases ──────────────────────
                    final_goals   = final_totals.get('goals', 0)
                    final_corners = final_totals.get('corners', 0)
                    final_cards   = final_totals.get('cards', 0)

                    if goals_line > 0:
                        if final_goals > goals_line:
                            lgo, lgp, lgu = 1, 0, 0
                        elif final_goals == goals_line:
                            lgo, lgp, lgu = 0, 1, 0
                        else:
                            lgo, lgp, lgu = 0, 0, 1
                        goals_delta = round(final_goals - goals_line, 2)
                    else:
                        lgo = lgp = lgu = 0
                        goals_delta = 0.0

                    if corners_line > 0:
                        if final_corners > corners_line:
                            lco, lcp, lcu = 1, 0, 0
                        elif final_corners == corners_line:
                            lco, lcp, lcu = 0, 1, 0
                        else:
                            lco, lcp, lcu = 0, 0, 1
                    else:
                        lco = lcp = lcu = 0

                    # ── Construir fila ────────────────────────────────────
                    row = {
                        "match_id":          match_id,
                        "minute":            minute,
                        "remaining_minutes": remaining,
                        "current_goals":     current_goals,
                        "current_corners":   current_corn,
                        "current_cards":     float(state.get('amarillas', 0)) + (float(state.get('rojas', 0)) * 2),
                        "goal_diff":         goal_diff,
                        "xg_local":          float(state.get('xg_local', 0)),
                        "xg_visitante":      float(state.get('xg_visitante', 0)),
                        "xg_total":          xg_total,
                        "possession_home":   float(state.get('posesion_local', 50)),
                        "possession_dominance": abs(float(state.get('posesion_local', 50)) - 50),
                        "xg_recientes":      xg_recientes,
                        "corners_recientes": corners_recientes,
                        "faltas_recientes":  faltas_recientes,
                        "danger_rate_recent": round(danger_rate_recent, 5),
                        "danger_rate":       danger_rate,
                        "tension_index":     float(model.get('tension_index', 0)),
                        "urgency_factor":    float(model.get('urgency_factor', 1.0)),
                        "phase_name":        model.get('phase_name', 'estable'),
                        "xg_per_minute":     round(xg_total / max(1, minute), 5),
                        "attacks_per_minute": round(
                            (float(state.get('dangerous_attacks_home', 0)) + float(state.get('dangerous_attacks_away', 0)))
                            / max(1, minute), 5
                        ),
                        "contextual_danger": contextual_danger,
                        "goals_line":        goals_line,
                        "goals_over_odds":   go,
                        "goals_under_odds":  gu,
                        "goals_implied_prob_over":  goals_imp_over,
                        "goals_implied_prob_under": goals_imp_under,
                        "goals_market_spread":      goals_spread,
                        "corners_line":       corners_line,
                        "corners_over_odds":  co,
                        "corners_under_odds": cu,
                        "corners_implied_prob_over":  corners_imp_over,
                        "corners_implied_prob_under": corners_imp_under,
                        "corners_market_spread":      corners_spread,
                        "pinnacle_goals_over":  p_g_over,
                        "pinnacle_goals_under": p_g_under,
                        "pinnacle_goals_spread": p_g_spread,
                        "tournament_slug": match_info.get('tournament_slug', '') or '',
                        "category_name":   match_info.get('category_name', '') or '',
                        "corners_per_xg":  corners_per_xg,
                        "xg_per_shot":     xg_per_shot,
                        **{f"atk_zone_home_{i}": atk_home_flat[i] for i in range(12)},
                        **{f"atk_zone_away_{i}": atk_away_flat[i] for i in range(12)},
                        "final_goals":   final_goals,
                        "final_corners": final_corners,
                        "final_cards":   final_cards,
                        "goals_vs_line_delta": goals_delta,
                        "label_goals_over":   lgo,
                        "label_goals_push":   lgp,
                        "label_goals_under":  lgu,
                        "label_corners_over":  lco,
                        "label_corners_push":  lcp,
                        "label_corners_under": lcu,
                    }

                    writer.writerow(row)
                    total_snapshots += 1

                except Exception:
                    pass

    print("\n✅ Dataset V2 generado con exito!")
    print(f"Archivo creado: {output_csv}")
    print("-" * 40)
    print(f"Partidos validos procesados: {valid_matches}")
    print(f"Partidos huerfanos DESCARTADOS: {discarded_matches}")
    print(f"Total de Filas (Samples) para ML: {total_snapshots}")
    print(f"Columnas por muestra: {len(headers)}")
    print("\nColumnas clave nuevas en V2:")
    print("  - remaining_minutes, phase_name, tournament_slug")
    print("  - implied_prob (over/under), market_spread")
    print("  - pinnacle_goals_over/under (faro sharp)")
    print("  - xg_recientes, corners_recientes (ventana corta)")
    print("  - attack_zones (24 columnas de posicionamiento)")
    print("  - corners_per_xg, xg_per_shot (ratios compuestos)")
    print("  - label_goals_push / label_corners_push (target 3-clases)")
    print("  - goals_vs_line_delta (target regresion)")


if __name__ == "__main__":
    build_ml_dataset_v2()
