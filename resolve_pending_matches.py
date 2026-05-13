import os
import json
import time
import glob
import re
from pathlib import Path
from types import SimpleNamespace
from curl_cffi import requests

# Importar las herramientas oficiales del sistema
from futbol_live_betting_probabilities import (
    append_match_closure, MatchState,
)

# Configuración
HISTORY_DIR = Path("live_history_v2")
SOFASCORE_API = "https://api.sofascore.com/api/v1"

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36',
    'Accept': 'application/json, text/plain, */*',
    'Accept-Language': 'es-CO,es;q=0.9,en-US;q=0.8,en;q=0.7',
    'Origin': 'https://www.sofascore.com',
}

def get_final_stats(event_id):
    """Obtiene el resultado final y estadísticas de SofaScore."""
    try:
        r_ev = requests.get(f"{SOFASCORE_API}/event/{event_id}", headers=HEADERS, impersonate="chrome124", timeout=10)
        if r_ev.status_code != 200:
            return None
        ev_data = r_ev.json().get("event", {})
        
        status = ev_data.get("status", {}).get("type")
        if status != "finished":
            print(f"  [WAIT] Evento {event_id} aun no ha terminado (status: {status})")
            return None
            
        r_stats = requests.get(f"{SOFASCORE_API}/event/{event_id}/statistics", headers=HEADERS, impersonate="chrome124", timeout=10)
        stats_data = {}
        if r_stats.status_code == 200:
            stats_data = r_stats.json()
            
        return {
            "event": ev_data,
            "statistics": stats_data
        }
    except Exception as e:
        print(f"  [ERR] Error consultando SofaScore para {event_id}: {e}")
        return None

def build_official_objects(event_id, data):
    """Construye los objetos snapshot y state que espera append_match_closure."""
    ev = data["event"]
    stats_raw = data["statistics"]
    
    home_score = ev.get("homeScore", {}).get("display", 0)
    away_score = ev.get("awayScore", {}).get("display", 0)
    
    def _parse_val(val):
        if val is None: return 0.0
        s = str(val).replace("%", "").strip()
        match = re.search(r"(\d+\.?\d*)", s)
        if match: return float(match.group(1))
        return 0.0

    stat_map = {}
    for period_block in stats_raw.get("statistics", []):
        if period_block.get("period") == "ALL":
            for group in period_block.get("groups", []):
                for item in group.get("statisticsItems", []):
                    name = str(item.get("name", "")).lower().strip()
                    stat_map[name] = {
                        "home": _parse_val(item.get("home")),
                        "away": _parse_val(item.get("away"))
                    }
            break

    def _get(keys, default=0.0):
        for k in keys:
            if k in stat_map: return stat_map[k]
        return {"home": default, "away": default}

    crns = _get(["corner kicks", "corners"])
    ylws = _get(["yellow cards", "yellow"])
    reds = _get(["red cards", "red"])
    faltas = _get(["fouls", "fauls"])
    xg = _get(["expected goals", "xg"])
    tiros = _get(["total shots", "shots"])
    sot = _get(["shots on target", "on target"])
    poss = _get(["ball possession", "possession"])

    # Objeto Snapshot (mockeado)
    snapshot = SimpleNamespace(
        event_id=event_id,
        match_url=f"https://www.sofascore.com/en-us/football/match/fake/{event_id}#id:{event_id}",
        home_team=ev.get("homeTeam", {}).get("name", ""),
        away_team=ev.get("awayTeam", {}).get("name", ""),
        tournament=ev.get("tournament", {}).get("name", ""),
        status_text="finished",
        notes=[],
        # Para que history_file_path no falle si intenta usar otros campos
        minute=90.0,
        goals_home=float(home_score),
        goals_away=float(away_score),
        yellows_total=ylws["home"] + ylws["away"],
        reds_total=reds["home"] + reds["away"],
        corners_total=crns["home"] + crns["away"]
    )
    
    # Objeto MatchState real
    state = MatchState(
        minuto=90.0,
        goles_local=float(home_score),
        goles_visitante=float(away_score),
        amarillas=ylws["home"] + ylws["away"],
        rojas=reds["home"] + reds["away"],
        faltas=faltas["home"] + faltas["away"],
        corners=crns["home"] + crns["away"],
        xg_local=xg["home"],
        xg_visitante=xg["away"],
        tiros_local=tiros["home"],
        tiros_visitante=tiros["away"],
        tiros_puerta_local=sot["home"],
        tiros_puerta_visitante=sot["away"],
        posesion_local=poss["home"]
    )
    
    return snapshot, state

def cleanup_wrong_closure(fpath):
    """Elimina la ultima linea si es un match_closure mal formado."""
    with open(fpath, "r", encoding="utf-8") as f:
        lines = f.readlines()
    if not lines: return False
    
    if '"record_type": "match_closure"' in lines[-1]:
        # Si NO tiene "settlement_summary", es la mia "mala"
        if '"settlement_summary":' not in lines[-1]:
            print(f"  [FIX] Eliminando cierre anterior mal formado...")
            with open(fpath, "w", encoding="utf-8") as f:
                f.writelines(lines[:-1])
            return True
        else:
            # Ya tiene el cierre oficial
            return "ALREADY_OFFICIAL"
    return False

def process_file(fpath):
    print(f"[*] Procesando {fpath.name}...")
    
    # 1. Limpiar o verificar
    status = cleanup_wrong_closure(fpath)
    if status == "ALREADY_OFFICIAL":
        print("  [OK] Ya tiene cierre oficial.")
        return

    # Extraer event_id
    match = re.search(r"(\d+)_", fpath.name)
    if not match: return
    event_id = int(match.group(1))
    
    # 2. Consultar SofaScore
    data = get_final_stats(event_id)
    if not data: return
    
    snapshot, state = build_official_objects(event_id, data)
    
    # 3. Llamar a la funcion oficial del sistema
    try:
        new_path = append_match_closure(HISTORY_DIR, snapshot, state)
        if new_path:
            print(f"  [DONE] Match Closure OFICIAL agregado. Resultado: {int(state.goles_local)}-{int(state.goles_visitante)}")
            print(f"  [MOVE] Archivo renombrado a: {new_path.name}")
    except Exception as e:
        print(f"  [ERR] Error en append_match_closure: {e}")

if __name__ == "__main__":
    if not HISTORY_DIR.exists():
        print("Directorio de historia no encontrado.")
    else:
        # Procesar todos los archivos (incluyendo los que ya renombre con [WIN]/[LOSS] si los hubiera)
        files = list(HISTORY_DIR.glob("*.jsonl"))
        print(f"Encontrados {len(files)} archivos.")
        for f in files:
            process_file(f)
        print("\nProceso finalizado.")
