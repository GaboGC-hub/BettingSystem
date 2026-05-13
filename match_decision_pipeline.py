import time
import math as _math
from dataclasses import asdict, replace
from pathlib import Path

from futbol_live_betting_probabilities import (
    MarketLine, MarketSet, market_summary, apply_market_guardrails,
    run_model, no_bet_decision, fair_odds_for_live_total_market,
    build_params, build_state_from_snapshot, infer_league_profile,
    apply_league_profile, resolve_prematch_context, collect_live_markets,
    append_live_history, append_bet_record, append_match_closure,
)

# ---------------------------------------------------------------------------
# 0. De-Juicer: elimina el margen de casas recreacionales (Betano)
# ---------------------------------------------------------------------------

def _dejuice(cuota_over, cuota_under):
    """Elimina el juice de cuotas Betano y devuelve cuotas justas."""
    try:
        if not cuota_over or not cuota_under or cuota_over <= 1.0 or cuota_under <= 1.0:
            return None, None
        prob_over = 1.0 / cuota_over
        prob_under = 1.0 / cuota_under
        margen = prob_over + prob_under
        prob_real_over = prob_over / margen
        prob_real_under = prob_under / margen
        return round(1.0 / prob_real_over, 3), round(1.0 / prob_real_under, 3)
    except Exception:
        return None, None

# ---------------------------------------------------------------------------
# 1. Reasoning builder (Spanish-language explanation per market)
# ---------------------------------------------------------------------------

def build_reasoning(mkt_name, decision_dict, market, state, result_dict):
    """Genera una explicación en español del porqué del modelo para esa apuesta."""
    side = decision_dict.get("best_side", "NO BET")
    if side == "NO BET":
        ev_o = decision_dict.get("ev_over", 0)
        ev_u = decision_dict.get("ev_under", 0)
        best_ev = max(ev_o, ev_u)
        return f"Sin ventaja suficiente. Mejor EV encontrado: {best_ev:.3f} (mínimo necesario > 1.0). Modelo prefiere pasar."

    linea = decision_dict.get("linea", 0)
    mean = decision_dict.get("mean_total", 0)
    future_mean = decision_dict.get("future_mean", 0)
    prob = decision_dict.get("best_prob", 0)
    ev = decision_dict.get("best_ev", 0)
    stake = decision_dict.get("best_stake", 0)

    minuto = state.get("minuto", 0)
    remaining = result_dict.get("remaining_minutes", 90 - minuto)
    phase = result_dict.get("phase_name", "estable")

    if mkt_name == "GOLES":
        actual = state.get("goles_local", 0) + state.get("goles_visitante", 0)
        rate_str = f"xG: {state.get('xg_local',0):.2f} + {state.get('xg_visitante',0):.2f} = {state.get('xg_local',0)+state.get('xg_visitante',0):.2f}"
        items_str = f"goles actuales: {int(actual)}"
    elif mkt_name == "CORNERS":
        actual = state.get("corners", 0)
        rate_str = f"ritmo: {actual / max(1, minuto) * 90:.1f}/90 min"
        items_str = f"corners actuales: {int(actual)}"
    else:  # TARJETAS
        actual = state.get("amarillas", 0) + state.get("rojas", 0) * 2
        rate_str = f"faltas: {state.get('faltas',0):.0f}, tarjetas/falta: {state.get('amarillas',0)/max(1,state.get('faltas',1)):.2f}"
        items_str = f"tarjetas actuales: {int(actual)}"

    phase_labels = {
        "partido_caliente": "partido caliente 🔥", "persecucion": "persecución ⚡",
        "cierre_roto": "cierre roto 💥", "ida_y_vuelta": "ida y vuelta 🔄",
        "ventaja_controlada": "ventaja controlada 🛡️", "estable": "partido estable ⚖️",
    }
    phase_label = phase_labels.get(phase, phase)

    # Detectar factores especiales para el razonamiento
    special_notes = []
    total_xg = state.get("xg_local",0) + state.get("xg_visitante",0)
    total_shots = state.get("tiros_local",0) + state.get("tiros_visitante",0)

    if mkt_name == "GOLES":
        if minuto >= 70:
            special_notes.append("Time Decay activo (cansancio ⬇️)")
        if total_xg < 0.05 and total_shots >= 3:
            special_notes.append("Advertencia: xG ausente. Usando estimación por tiros ⚠️")

    if mkt_name == "CORNERS":
        if state.get("xg_recientes") is not None:
            xg_avg = total_xg / max(1, minuto)
            recent_xg = state.get("xg_recientes",0) / max(1, state.get("ventana_reciente_min",1))
            if recent_xg < (xg_avg * 0.7):
                special_notes.append("Filtro de presión activo (bajo xG reciente ⬇️)")

        actual_corners = state.get("corners", 0)
        if total_shots > 0 and (actual_corners / total_shots) < 0.15 and minuto >= 30:
            special_notes.append("Filtro Simetría Corners (ataque sin amplitud ⬇️)")

    if mkt_name == "TARJETAS":
        card_actual = state.get("amarillas", 0) + state.get("rojas", 0) * 2
        f_rate = state.get("faltas", 0) / max(1, minuto)
        if f_rate >= 0.38 and card_actual <= 2:
            special_notes.append("Factor desesperación arbitral ⬆️")

    lado = "OVER" if side == "OVER" else "UNDER"
    hacia = "superará" if side == "OVER" else "no llegará a"

    lines = [
        f"El modelo proyecta {mean:.1f} {mkt_name.lower()} totales al final ({items_str}, línea: {linea}).",
        f"En los próximos ~{remaining:.0f} min aún se esperan {future_mean:.1f} más ({rate_str}).",
        f"Fase: {phase_label}. Probabilidad de que el total {hacia} {linea}: {prob*100:.1f}%.",
        f"Cuota justa calculada: {1/max(0.001,prob):.2f} — EV real: {ev:.3f} {'✅' if ev > 1.05 else '⚠️ borde justo'}.",
        f"Kelly fraccionado sugiere apostar {stake*100:.1f}% del bankroll.",
    ]
    if special_notes:
        lines.append("🔍 Ajustes: " + ", ".join(special_notes))
    if decision_dict.get("note"):
        lines.append(f"⚠️ {decision_dict['note']}")
    return " | ".join(lines)


# ---------------------------------------------------------------------------
# 2. Standalone guardrail functions
# ---------------------------------------------------------------------------

def apply_kill_switch(decision, is_active):
    """If kill_switch_active, force NO BET on every line."""
    if is_active:
        return no_bet_decision(
            decision,
            "🔒 MODO SOLO LECTURA: Kill-Switch activado por caída del 15% del bankroll.",
        )
    return decision


def apply_circuit_breaker(decision, m_name, ctx, current_total):
    """Tracks consecutive UNDER-failures per market; blackouts after 5."""
    if "circuit_breaker" not in ctx:
        ctx["circuit_breaker"] = {
            "failures": {"GOLES": 0, "CORNERS": 0, "TARJETAS": 0},
            "blackout": {"GOLES": False, "CORNERS": False, "TARJETAS": False},
            "last_under": {"GOLES": None, "CORNERS": None, "TARJETAS": None},
        }

    cb = ctx["circuit_breaker"]
    if cb["blackout"].get(m_name):
        return no_bet_decision(decision, "Circuit Breaker: Mercado bloqueado por racha de fallos.")

    last_line = cb["last_under"].get(m_name)
    if last_line is not None and current_total > last_line:
        cb["failures"][m_name] += 1
        cb["last_under"][m_name] = None
        if cb["failures"][m_name] >= 5:
            cb["blackout"][m_name] = True
            return no_bet_decision(decision, "Circuit Breaker ACTIVADO: 5 fallos consecutivos.")

    if decision.best_side == "UNDER":
        cb["last_under"][m_name] = decision.linea

    return decision


def apply_exposure_limit(decision, m_name, ctx):
    """Max 3.0 units exposure per line."""
    if decision.best_side == "NO BET":
        return decision

    if "exposure_tracker" not in ctx:
        ctx["exposure_tracker"] = {}

    current_line = decision.linea
    tracker = ctx["exposure_tracker"].get(m_name, {"line": -1, "accumulated": 0.0, "max_seen": 0.0})

    if tracker["line"] != current_line:
        tracker = {"line": current_line, "accumulated": 0.0, "max_seen": 0.0}

    already_exposed = tracker["accumulated"]
    needed_more = max(0.0, decision.best_stake - tracker["max_seen"])

    if (already_exposed + needed_more) > 3.0:
        if already_exposed >= 3.0:
            return no_bet_decision(decision, "Stop-Loss: Exposicion maxima (3.0u) alcanzada para esta linea.")
        remaining = 3.0 - already_exposed
        decision = replace(decision, best_stake=remaining, note="Stake limitado por exposicion (max 3.0u).")
        needed_more = remaining

    tracker["accumulated"] += needed_more
    tracker["max_seen"] = max(tracker["max_seen"], decision.best_stake)
    ctx["exposure_tracker"][m_name] = tracker
    return decision


def apply_cooldown_lock(decision, m_name, ctx):
    """10-min anti-bot cooldown per market+line."""
    if decision.best_side == "NO BET":
        return decision

    if "last_bet_tracker" not in ctx:
        ctx["last_bet_tracker"] = {}

    current_time = time.time()
    tracker = ctx["last_bet_tracker"].get(m_name)

    if tracker:
        time_diff_min = (current_time - tracker["time"]) / 60.0
        if time_diff_min < 10.0 and tracker["line"] == decision.linea:
            return no_bet_decision(
                decision,
                f"Cooldown Lock: Esperando {10.0 - time_diff_min:.1f} min por regla antibot.",
            )

    ctx["last_bet_tracker"][m_name] = {
        "time": current_time,
        "line": decision.linea,
    }
    return decision


def apply_pinnacle_fair_price(decision, m_name, pf_list, m_line):
    """Blocks if softbook odds are worse than pinnacle fair odds."""
    if decision.best_side == "NO BET":
        return decision
    if not pf_list:
        return decision

    pf_match = next((p for p in pf_list if p["linea"] == decision.linea), None)
    if not pf_match:
        return decision

    fair_over, fair_under = fair_odds_for_live_total_market(
        m_name, float(pf_match["over"]), float(pf_match["under"])
    )

    if decision.best_side == "OVER":
        if m_line.over < fair_over:
            return no_bet_decision(
                decision,
                f"⚠️ BLOQUEO PINNACLE: Soft OVER ({m_line.over}) < fair sharp ({fair_over:.3f}, power/adit.).",
            )
    elif decision.best_side == "UNDER":
        if m_line.under < fair_under:
            return no_bet_decision(
                decision,
                f"⚠️ BLOQUEO PINNACLE: Soft UNDER ({m_line.under}) < fair sharp ({fair_under:.3f}, power/adit.).",
            )

    return decision


def apply_pinnacle_lag_sniffer(decision, m_name, ctx):
    """Aggressive entry trigger when Pinnacle just moved (<20s ago)."""
    if decision.best_side == "NO BET":
        return decision

    lag_ts = ctx.get("pinnacle_fair", {}).get(f"_last_updated_{m_name}", 0)
    lag_secs = time.time() - lag_ts

    if lag_ts == 0 or lag_secs > 20:
        return decision

    ev_field = "ev_over" if decision.best_side == "OVER" else "ev_under"
    ev_actual = getattr(decision, ev_field, 0.0)

    if ev_actual >= 1.02:
        note_lag = (
            f" | 🔥 LAG SNIPER: Pinnacle movió {m_name} hace {lag_secs:.0f}s "
            f"— entrada agresiva OK (EV {ev_actual:.3f} ≥ 1.02)"
        )
        return replace(decision, note=decision.note + note_lag)

    return decision


def apply_sniper_lock(decision, market_name, mkt, ctx, state):
    """Francotirador cooldown (5 min) with goal/red/line-jump reset triggers."""
    if not decision or decision.best_side == "NO BET" or not mkt:
        return decision

    now_ts = time.time()
    bets_placed = ctx.setdefault("bets_placed", {})
    tracker = bets_placed.setdefault(market_name, {})

    last_time = tracker.get("last_time", 0)
    last_side = tracker.get("last_side")
    last_line = tracker.get("last_line")
    last_goals = tracker.get("last_goals")
    last_reds = tracker.get("last_reds")

    current_goals = state.goles_local + state.goles_visitante
    current_reds = state.rojas

    # Hard Reset triggers: Goal, Red Card, or Line jump
    goal_scored = last_goals is not None and current_goals > last_goals
    red_card = last_reds is not None and current_reds > last_reds
    line_jump = last_line is not None and mkt.linea != last_line

    if (
        (now_ts - last_time < 300)
        and (decision.best_side == last_side)
        and not (goal_scored or red_card or line_jump)
    ):
        decision = replace(
            decision,
            best_side="NO BET",
            best_stake=0.0,
            note=f"[COOLDOWN] Francotirador activo: Omitiendo {decision.best_side} {mkt.linea} "
            f"(Esperando {300 - int(now_ts - last_time)}s o salto de evento)",
        )
        return decision

    tracker["last_time"] = now_ts
    tracker["last_side"] = decision.best_side
    tracker["last_line"] = mkt.linea
    tracker["last_goals"] = current_goals
    tracker["last_reds"] = current_reds
    return decision


# ---------------------------------------------------------------------------
# 3. Multi-line market evaluation (inner loop per market)
# ---------------------------------------------------------------------------

def evaluate_market(m_name, lambda_val, actual_total, ctx, base_markets, params, state,
                    faro_stale_seconds=45, scraper_log=None, kill_switch_active=False):
    best_decision = None
    best_m_line = None
    current_max_ev = -1.0

    # Sticky line for mild self-reinforcement
    if "sticky_line" not in ctx:
        ctx["sticky_line"] = {"GOLES": None, "CORNERS": None, "TARJETAS": None}
    sticky_line = ctx["sticky_line"].get(m_name)

    overrides_list = ctx.get("overrides", {}).get(m_name, [])
    pf_list = ctx.get("pinnacle_fair", {}).get(m_name, [])
    bm = getattr(base_markets, m_name.lower(), None)

    import logging
    if logging.getLogger("evfl").isEnabledFor(logging.DEBUG):
        print(f"📦 [EVALUATE] {m_name}: overrides_list={overrides_list}, pf_list={pf_list}, bm={bm}")

    # --- LÓGICA DE EXTRACCIÓN DE CUOTAS (PRIORIDAD PINNACLE → BETANO DE-JUICED) ---
    if not overrides_list:
        if pf_list:
            overrides_list = [dict(p, source_id=p.get("source_id", p.get("source", "pinnacle")), is_verified=True) for p in pf_list]
            src_lbl = overrides_list[0].get("source_id", "pinnacle")
            print(f"💰 [CUOTAS] Usando {src_lbl} como fuente primaria para {m_name}")
        elif bm:
            overrides_list = [{"linea": bm.linea, "over": bm.over, "under": bm.under, "source_id": "betano_snapshot"}]
            if m_name == "GOLES":
                print(f"⚠️ [CUOTAS] Pinnacle no disponible. Usando Dummy 1.9 para {m_name}")

    # ── BYPASS HÍBRIDO: Betano de-juiced cuando Pinnacle está offline ──
    # Si no hay Pinnacle Y los overrides son dummy/unknown, intentamos inyectar
    # cuotas de Betano (si existen en ctx) pasadas por la lavadora matemática.
    has_real_override = bool(overrides_list and overrides_list[0].get("source_id") not in ("unknown", None))
    if not has_real_override and pf_list is not None and len(pf_list) == 0:
        betano_ov = ctx.get("overrides", {}).get(m_name, [])
        if betano_ov:
            dejuiced = []
            for bl in betano_ov:
                fair_o, fair_u = _dejuice(bl.get("over"), bl.get("under"))
                if fair_o and fair_u:
                    dejuiced.append({
                        "linea": bl["linea"],
                        "over": fair_o,
                        "under": fair_u,
                        "source_id": "betano_dejuiced",
                        "is_verified": True,
                        "timestamp": time.time(),
                    })
            if dejuiced:
                overrides_list = dejuiced
                print(f"🔥 [HÍBRIDO] PS3838 offline. Usando Betano de-juiced para {m_name}: "
                      f"O={dejuiced[0]['over']} / U={dejuiced[0]['under']}")

    base_is_dummy = getattr(bm, "is_dummy", False) if bm else False
    has_real_override = bool(overrides_list and overrides_list[0].get("source_id") not in ("unknown", None))

    # Ordenar: La "Línea Principal" (menor spread) va primero.
    overrides_list = sorted(overrides_list, key=lambda x: abs(x["over"] - x["under"]))

    pinnacle_ever_active = bool(ctx.get("pinnacle_fair"))
    scraper_ts_global = float(ctx.get("scraper_ts") or 0)
    ts_market = float(
        ctx.get("pinnacle_fair", {}).get(f"_last_updated_{m_name}", 0) or scraper_ts_global
    )
    now_ts = time.time()
    pinnacle_stale = (
        pinnacle_ever_active
        and ts_market > 0
        and (now_ts - ts_market) > faro_stale_seconds
    )

    # Constantes de proximidad por mercado
    _PROX = {"GOLES": 4.0, "CORNERS": 7.0, "TARJETAS": 6.0}
    _max_dist = _PROX.get(m_name, 5.0)

    def check_line_sanity(market_type, line, actual_total):
        diff = line - actual_total
        minute = int(ctx.get("minute") or 0) if "minute" in ctx and ctx.get("minute") else 0
        
        base_thresholds = {
            "GOLES":    3.5,
            "CORNERS":  6.5,
            "TARJETAS": 4.5,
        }
        max_thresholds = {
            "GOLES":    5.5,
            "CORNERS":  12.5,
            "TARJETAS": 6.5,
        }
        
        progress = min(max(minute, 0), 80) / 80.0
        base_th = base_thresholds.get(market_type, 5.0)
        max_th = max_thresholds.get(market_type, 8.0)
        dynamic_max = max_th - (max_th - base_th) * progress

        if diff > dynamic_max:
            msg = f"⚠️ SANITY CHECK: Línea {line} es demasiado alta (actual={actual_total}, diff={diff:.1f} > max {dynamic_max:.1f} al min {minute})"
            # print(f"[SANITY] {market_type}: {msg}") # Disabled direct print to reduce noise, unless needed. We'll let it be. Let's keep print for sanity check triggers since they are rare now.
            print(f"[SANITY] {market_type}: {msg}")
            return False, msg
        if line < actual_total - 0.5:
            msg = f"⚠️ SANITY CHECK: Línea {line} ya superada por marcador ({actual_total})"
            print(f"[SANITY] {market_type}: {msg}")
            return False, msg
        return True, ""

    for line_data in overrides_list:
        if line_data["linea"] <= 0:
            continue

        # ── Filtro de Frescura de la Cuota (Freeze Protection con TTL Dinámico) ──
        is_volatile = getattr(state, "ventana_reciente_min", 0) > 0 or getattr(state, "corners_recientes", 0) > 0
        max_ttl = 40 if is_volatile else 15

        edad_cuota = time.time() - line_data.get("timestamp", 0)
        if edad_cuota > max_ttl and line_data.get("timestamp", 0) > 0:
            print(f"⚠️ {m_name}: Cuotas congeladas (T={edad_cuota:.1f}s > TTL {max_ttl}s). "
                  f"Abortando cálculo para línea {line_data['linea']}.")
            continue

        # ── Filtro de Cordura (Sanity Fix) ──
        sanity_ok, sanity_msg = check_line_sanity(m_name, line_data["linea"], actual_total)

        # ── Filtro de Certificado de Origen ────────────────────────────
        is_verified = line_data.get("is_verified", None)
        if is_verified is False:
            print(f"[ORIGIN] {m_name}: Línea {line_data['linea']} rechazada "
                  f"(is_verified=False, market='{line_data.get('market','?')}')")
            continue

        m_line = MarketLine(
            linea=line_data["linea"],
            over=line_data["over"],
            under=line_data["under"],
            source_id=line_data.get("source_id", "unknown"),
        )

        # ── Bloqueo de Cuota Dummy ──────────────────────────────────────
        if base_is_dummy and not has_real_override:
            dummy_decision = no_bet_decision(
                market_summary(m_name, lambda_val, m_line, params, state),
                f"SIN CUOTA REAL: SofaScore no publica '{m_name}' para esta liga. "
                "Esperando cuota de Pinnacle/softbook antes de apostar.",
            )
            if scraper_log is not None:
                scraper_log.snapshot_rejected(
                    "sofascore",
                    f"DUMMY {m_name}: SofaScore no publica cuota real. En espera de Pinnacle/softbook.",
                    ctx.get("_url", ""),
                )
            if best_decision is None:
                best_decision = dummy_decision
                best_m_line = m_line
            continue
        # ───────────────────────────────────────────────────────────────

        pf_match = next((p for p in pf_list if p["linea"] == line_data["linea"]), None) if pf_list else None

        decision = market_summary(m_name, lambda_val, m_line, params, state, pinnacle_fair=pf_match)

        if not sanity_ok:
            decision = no_bet_decision(decision, sanity_msg)
        else:
            # ── MODO CAUTELOSO: Faro Apagado para TARJETAS ─────────────────────
            if m_name == "TARJETAS" and pinnacle_ever_active and not pf_list:
                decision = no_bet_decision(
                    decision,
                    "FARO APAGADO: Pinnacle no ofrece tarjetas para este partido. "
                    "Sin referencia sharp → abortando apuesta de tarjetas.",
                )

            # ── SAFE MODE: Faro de ESTE mercado con lag > umbral ───────────
            if pinnacle_stale:
                lag_s = int(now_ts - ts_market)
                decision = no_bet_decision(
                    decision,
                    f"⏱️ SAFE MODE ({m_name}): Faro con {lag_s}s de lag (máx {faro_stale_seconds}s). "
                    "Kelly/stake en 0 hasta actualización.",
                )

            # Validación de Línea (Faro Pinnacle Estricto)
            if pf_list and not pf_match:
                decision = no_bet_decision(decision, "🔒 Esta línea exacta NO existe en Pinnacle (Espejo Fallido).")
            else:
                decision = apply_market_guardrails(m_name, decision, state, params, ctx.get("odds_history"))
                decision = apply_circuit_breaker(decision, m_name, ctx, actual_total)
                decision = apply_exposure_limit(decision, m_name, ctx)
                decision = apply_cooldown_lock(decision, m_name, ctx)
                decision = apply_pinnacle_fair_price(decision, m_name, pf_list, m_line)
                if m_name == "TARJETAS":
                    decision = apply_pinnacle_lag_sniffer(decision, m_name, ctx)

                # Filtro de Riesgo/Recompensa
                if decision.best_side == "OVER" and m_line.over < 1.25:
                    decision = no_bet_decision(decision, f"⚠️ Cuota OVER muy baja ({m_line.over} < 1.25)")
                elif decision.best_side == "UNDER" and m_line.under < 1.25:
                    decision = no_bet_decision(decision, f"⚠️ Cuota UNDER muy baja ({m_line.under} < 1.25)")

                # Sanity Check del Edge
                cand_ev = decision.best_ev if decision.best_side != "NO BET" else 0.0
                if cand_ev > 3.0:
                    decision = no_bet_decision(
                        decision,
                        f"⚠️ SANITY CHECK: EV absurdo ({cand_ev:.2f} > 3.0). Datos corruptos.",
                    )

                decision = apply_kill_switch(decision, kill_switch_active)

        cand_ev = decision.best_ev if decision.best_side != "NO BET" else 0.0

        if best_decision is None:
            best_decision = decision
            best_m_line = m_line
            current_max_ev = cand_ev
        else:
            is_sticky = (sticky_line == line_data["linea"])
            bonus_self = 0.02 if is_sticky else 0.0
            bonus_best = 0.02 if (sticky_line == best_decision.linea) else 0.0

            if (cand_ev + bonus_self) > (current_max_ev + bonus_best):
                best_decision = decision
                best_m_line = m_line
                current_max_ev = cand_ev

    if best_decision and best_decision.best_side != "NO BET":
        ctx["sticky_line"][m_name] = best_decision.linea

    # Fallback seguro cuando el filtro eliminó todas las líneas.
    valid_overrides = [ov for ov in overrides_list if check_line_sanity(m_name, ov["linea"], actual_total)]

    if best_decision is None:
        cuotas_recibidas = ctx.get("overrides", {})
        print(f"⚠️ [MOTOR MATEMÁTICO] Inyectando Dummy {m_name}. Overrides recibidos del parser: {cuotas_recibidas}")
        if valid_overrides:
            first = valid_overrides[0]
        else:
            first = {"linea": actual_total + 1.5, "over": 1.85, "under": 1.85}

        fallback_line = MarketLine(
            linea=first["linea"],
            over=first.get("over", 1.85),
            under=first.get("under", 1.85),
        )
        best_decision = no_bet_decision(
            market_summary(m_name, lambda_val, fallback_line, params, state),
            "Mercado bloqueado o líneas descartadas por filtros de sanidad.",
        )
        best_m_line = fallback_line

    return best_decision, best_m_line


# ---------------------------------------------------------------------------
# 4. Main entry point (replaces update_match_math)
# ---------------------------------------------------------------------------

def run_decision_pipeline(ctx, snapshot, state, base_markets, params, prematch,
                          backend_args, faro_stale_seconds=45,
                          is_locked_fn=None, sanitize_fn=None, scraper_log=None):
    print(f"🎯 [TRACER START] run_decision_pipeline invocado para minuto {getattr(state, 'minuto', '?')}")

    kill_switch_active = ctx.get("_kill_switch_active", False)

    # Aplicar override manual de stats completo si el usuario lo ingresó
    stats_ov = ctx.get("stats_override")
    if stats_ov:
        state_updates = {}
        for k, v in stats_ov.items():
            if hasattr(state, k) and v is not None:
                state_updates[k] = float(v)
        if state_updates:
            state = replace(state, **state_updates)

    result = run_model(state, base_markets, params, prematch=prematch)

    goals_decision, g_mkt = evaluate_market(
        "GOLES", result.lambda_goals, state.goles_local + state.goles_visitante,
        ctx, base_markets, params, state,
        faro_stale_seconds=faro_stale_seconds, scraper_log=scraper_log,
        kill_switch_active=kill_switch_active,
    )
    corners_decision, c_mkt = evaluate_market(
        "CORNERS", result.lambda_corners, state.corners,
        ctx, base_markets, params, state,
        faro_stale_seconds=faro_stale_seconds, scraper_log=scraper_log,
        kill_switch_active=kill_switch_active,
    )
    cards_decision, t_mkt = evaluate_market(
        "TARJETAS", result.lambda_cards, state.amarillas + (state.rojas * 2),
        ctx, base_markets, params, state,
        faro_stale_seconds=faro_stale_seconds, scraper_log=scraper_log,
        kill_switch_active=kill_switch_active,
    )

    # === SNIPER MODE (BET LOCK) ===
    goals_decision = apply_sniper_lock(goals_decision, "GOLES", g_mkt, ctx, state)
    corners_decision = apply_sniper_lock(corners_decision, "CORNERS", c_mkt, ctx, state)
    cards_decision = apply_sniper_lock(cards_decision, "TARJETAS", t_mkt, ctx, state)

    safe_snapshot = asdict(snapshot) if hasattr(snapshot, "__dataclass_fields__") else snapshot.__dict__
    safe_result = asdict(result)
    safe_result.pop("total_goals", None)
    safe_result.pop("total_corners", None)
    safe_result.pop("total_cards", None)
    state_dict = asdict(state)

    g_dict = asdict(goals_decision) if goals_decision else {
        "best_side": "NO BET", "linea": 0, "best_stake": 0, "best_ev": 0,
        "best_prob": 0, "prob_over": 0, "prob_under": 0,
        "fair_over": None, "fair_under": None, "note": "Sin datos de cuotas",
    }
    if g_mkt:
        g_dict["raw_over"] = g_mkt.over
        g_dict["raw_under"] = g_mkt.under
        g_dict["source_id"] = getattr(g_mkt, "source_id", "unknown")
    g_dict["reasoning"] = build_reasoning("GOLES", g_dict, g_mkt, state_dict, safe_result)

    c_dict = asdict(corners_decision) if corners_decision else {
        "best_side": "NO BET", "linea": 0, "best_stake": 0, "best_ev": 0,
        "best_prob": 0, "prob_over": 0, "prob_under": 0,
        "fair_over": None, "fair_under": None, "note": "Sin datos de cuotas",
    }
    if c_mkt:
        c_dict["raw_over"] = c_mkt.over
        c_dict["raw_under"] = c_mkt.under
        c_dict["source_id"] = getattr(c_mkt, "source_id", "unknown")
    c_dict["reasoning"] = build_reasoning("CORNERS", c_dict, c_mkt, state_dict, safe_result)

    t_dict = asdict(cards_decision) if cards_decision else {
        "best_side": "NO BET", "linea": 0, "best_stake": 0, "best_ev": 0,
        "best_prob": 0, "prob_over": 0, "prob_under": 0,
        "fair_over": None, "fair_under": None, "note": "Sin datos de cuotas",
    }
    if t_mkt:
        t_dict["raw_over"] = t_mkt.over
        t_dict["raw_under"] = t_mkt.under
        t_dict["source_id"] = getattr(t_mkt, "source_id", "unknown")
    t_dict["reasoning"] = build_reasoning("TARJETAS", t_dict, t_mkt, state_dict, safe_result)

    # ── Inyectar estado de Persistent Bet Lock en cada mercado ──────────────────
    if is_locked_fn is not None:
        _match_url = ctx.get("_url", "")
        for _mkt_name, _d, _mkt_obj in [
            ("Goles",    g_dict, g_mkt),
            ("Corners",  c_dict, c_mkt),
            ("Tarjetas", t_dict, t_mkt),
        ]:
            _linea = (_d.get("linea") or 0)
            _side  = _d.get("best_side", "NO BET")
            _lock  = is_locked_fn(_match_url, _mkt_name, _linea, _side if _side not in ("NO BET", "PASAR") else None)
            if _lock:
                _d["bet_lock"] = {
                    "lock_id":      _lock["lock_id"],
                    "locked_at":    _lock["locked_at"],
                    "locked_ago_s": int(time.time() - _lock["locked_at"]),
                    "expires_in_s": max(0, int(_lock["expires_at"] - time.time())),
                    "stake_usd":    _lock.get("stake_usd", 0),
                    "odds":         _lock.get("odds", 0),
                    "source":       _lock.get("source", "manual"),
                }
            else:
                _d["bet_lock"] = None

    safe_result["markets"] = {
        "goles":    {"decision": g_dict},
        "corners":  {"decision": c_dict},
        "tarjetas": {"decision": t_dict},
    }

    # ── Calidad de datos: timestamps + flags de frescura ──────────────────
    scraper_ts_val = ctx.get("scraper_ts", 0)
    pinnacle_active = bool(ctx.get("pinnacle_fair"))
    _now = time.time()
    _pf = ctx.get("pinnacle_fair", {}) or {}
    faro_pulse = {}
    for _mn in ("GOLES", "CORNERS", "TARJETAS"):
        _t = float(_pf.get(f"_last_updated_{_mn}", 0) or scraper_ts_val or 0)
        _lag = int(_now - _t) if _t > 0 else None
        _st = pinnacle_active and _t > 0 and (_now - _t) > faro_stale_seconds
        faro_pulse[_mn] = {"lag_s": _lag, "stale": _st}
    pinnacle_stale_flag = pinnacle_active and any(
        faro_pulse[m]["stale"] for m in faro_pulse
    )
    touches_real = (
        state_dict.get("touches_in_box_home", 0) +
        state_dict.get("touches_in_box_away", 0)
    ) > 0

    raw_data = {
        "snapshot": safe_snapshot,
        "state": state_dict,
        "result": safe_result,
        "phase_summary": result.phase_summary,
        "sofascore_ts": time.time(),
        "scraper_ts": scraper_ts_val,
        "data_quality": {
            "touches_real": touches_real,
            "touches_home": state_dict.get("touches_in_box_home", 0),
            "touches_away": state_dict.get("touches_in_box_away", 0),
            "pinnacle_active": pinnacle_active,
            "pinnacle_stale": pinnacle_stale_flag,
            "faro_stale_seconds": faro_stale_seconds,
            "faro_pulse": faro_pulse,
            "scraper_lag_s": int(_now - scraper_ts_val) if scraper_ts_val else None,
            "raw_over_goles":    g_dict.get("raw_over"),
            "raw_over_corners":  c_dict.get("raw_over"),
            "raw_over_tarjetas": t_dict.get("raw_over"),
        },
    }
    ctx["data"] = sanitize_fn(raw_data) if sanitize_fn else raw_data

    overridden_markets = MarketSet(
        goles=g_mkt or base_markets.goles,
        corners=c_mkt or base_markets.corners,
        tarjetas=t_mkt or base_markets.tarjetas,
    )

    if not getattr(backend_args, "no_history", False):
        snap_sig = (
            round(state.minuto, 1),
            state.goles_local, state.goles_visitante,
            state.corners, state.amarillas + state.rojas * 2,
            round(getattr(snapshot, "possession_home", 0.0), 1),
            round(getattr(snapshot, "xg_home", 0.0), 2),
            round(getattr(snapshot, "shots_on_target_home", 0.0), 0),
        )
        if ctx.get("last_snap_sig") != snap_sig:
            print(f"\n🔥 [TRIGGER DEDUPLICACIÓN] Cambio detectado. Intentando escribir Minuto {state.minuto}...")
            latency_ms = None
            scraper_ts = ctx.get("scraper_ts")
            if scraper_ts:
                latency_ms = (time.time() - scraper_ts) * 1000
            lp = ctx.get("last_league_profile")
            if not lp:
                lp = infer_league_profile(snapshot=snapshot, league_name=None)

            try:
                append_live_history(
                    Path(backend_args.history_dir), snapshot, state, overridden_markets,
                    backend_args, lp, prematch, result,
                    match_ctx=ctx.get("match_ctx"), latency_ms=latency_ms,
                )
                print(f"✅ [ÉXITO] Archivo escrito/actualizado en disco.")
                ctx["last_snap_sig"] = snap_sig
            except Exception as e:
                import traceback
                print(f"❌ [ERROR FATAL DE ESCRITURA] El archivo no se creó por: {e}")
                traceback.print_exc()

    return state, overridden_markets, result
