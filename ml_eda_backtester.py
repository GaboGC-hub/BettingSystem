import pandas as pd
import numpy as np

def run_eda_and_backtest():
    print("[INIT] Iniciando Exploración de Datos (EDA) y Backtesting...\n")
    
    # Cargar el dataset limpio
    try:
        df = pd.read_csv('ml_dataset_clean.csv')
    except Exception as e:
        print(f"[ERROR] Error al cargar el CSV: {e}")
        return

    if df.empty:
        print("El dataset está vacío.")
        return

    # ---------------------------------------------------------
    # 1. Análisis Exploratorio (EDA) - Correlación de Pearson
    # ---------------------------------------------------------
    print("1. ANÁLISIS DE CORRELACIÓN (PEARSON) vs GOLES FINALES")
    print("-" * 60)
    
    # Seleccionamos las variables numéricas que nos interesan
    features = [
        'minute', 'current_goals', 'xg_local', 'xg_visitante',
        'danger_rate', 'tension_index', 'urgency_factor',
        'attacks_per_minute', 'xg_per_minute', 'possession_dominance',
        # v2 - Nuevas features de contexto
        'goal_diff', 'danger_rate_recent', 'contextual_danger',
        'goals_line', 'goals_over_odds'
    ]
    
    correlations = {}
    for feat in features:
        if feat in df.columns:
            # Calcular correlación de Pearson ignorando NaN
            corr = df[feat].corr(df['final_goals'])
            correlations[feat] = corr
            
    # Ordenar por valor absoluto de correlación
    sorted_corr = sorted(correlations.items(), key=lambda x: abs(x[1]) if pd.notnull(x[1]) else 0, reverse=True)
    
    for feat, corr in sorted_corr:
        if pd.notnull(corr):
            print(f"   {feat:22s} : {corr:+.4f}")
        else:
            print(f"   {feat:22s} : NaN")

    print("\nInterpretación:")
    print("   Cercano a +1: Fuerte relación directa (Sube la variable -> Suben los goles)")
    print("   Cercano a -1: Fuerte relación inversa (Sube la variable -> Bajan los goles)")
    print("   Cercano a  0: Sin poder predictivo estadístico.\n")

    # ---------------------------------------------------------
    # 2. Backtester de Reglas Fijas (Simulador)
    # ---------------------------------------------------------
    print("2. BACKTESTER: REGLAS ESTÁTICAS (BASELINE)")
    print("-" * 60)
    
    # Agrupar por partido para evaluar resultados a nivel de partido, no de snapshot
    # Queremos saber si la regla "disparó" al menos una vez en el partido, y si ganamos.
    grouped = df.groupby('match_id')

    # Regla A: Apostar OVER si attacks_per_minute > 1.2 y cuota > 1.6
    rule_a_bets = 0
    rule_a_wins = 0

    # Regla B: Apostar OVER si contextual_danger > 0.18 y xG/min > 0.02
    rule_b_bets = 0
    rule_b_wins = 0

    # Regla C: Apostar OVER si danger_rate_recent > danger_rate * 1.5 (frenesi final)
    rule_c_bets = 0
    rule_c_wins = 0

    for match_id, match_df in grouped:
        # Evaluar Regla A
        snapshots_rule_a = match_df[(match_df['attacks_per_minute'] > 1.2) & (match_df['goals_over_odds'] > 1.6)]
        if not snapshots_rule_a.empty:
            rule_a_bets += 1
            # Si en el último snapshot del partido la etiqueta es 1, ganamos
            if snapshots_rule_a.iloc[-1]['label_goals_over'] == 1:
                rule_a_wins += 1

        # Evaluar Regla B: contextual_danger > 0.18 (urgencia tactica alta)
        if 'contextual_danger' in match_df.columns:
            snapshots_rule_b = match_df[
                (match_df['contextual_danger'] > 0.18) & (match_df['xg_per_minute'] > 0.02)
            ]
            if not snapshots_rule_b.empty:
                rule_b_bets += 1
                if snapshots_rule_b.iloc[-1]['label_goals_over'] == 1:
                    rule_b_wins += 1

        # Evaluar Regla C: danger_rate_recent > danger_rate * 1.5 (frenesi final detectable)
        if 'danger_rate_recent' in match_df.columns:
            snapshots_rule_c = match_df[
                (match_df['danger_rate_recent'] > match_df['danger_rate'] * 1.5) &
                (match_df['minute'] >= 60)  # Solo segunda mitad, donde el frenesi importa
            ]
            if not snapshots_rule_c.empty:
                rule_c_bets += 1
                if snapshots_rule_c.iloc[-1]['label_goals_over'] == 1:
                    rule_c_wins += 1

    # Imprimir resultados del backtest
    def print_result(name, bets, wins):
        if bets > 0:
            wr = (wins / bets) * 100
            print(f"   {name}: {bets} apuestas -> {wins} aciertos ({wr:.1f}% Win Rate)")
        else:
            print(f"   {name}: No generó apuestas (Condiciones muy estrictas).")

    print_result("REGLA A (Ataques > 1.2/min + Cuota > 1.6)         ", rule_a_bets, rule_a_wins)
    print_result("REGLA B (contextual_danger > 0.18 + xG/min > 0.02)", rule_b_bets, rule_b_wins)
    print_result("REGLA C (Tormenta Aguda: recent > global*1.5)      ", rule_c_bets, rule_c_wins)

    print("\nNota: Tu modelo de Machine Learning deberá superar estos porcentajes para ser útil.")

if __name__ == "__main__":
    run_eda_and_backtest()
