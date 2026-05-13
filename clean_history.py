"""
clean_history.py — Limpieza retroactiva de live_history_v2.

Ejecutar UNA SOLA VEZ con el servidor y orquestador APAGADOS.

Uso:
  python clean_history.py            # Limpieza real
  python clean_history.py --dry-run  # Solo muestra qué se haría
"""
import json
import sys
import re
from pathlib import Path
from collections import defaultdict

HISTORY_DIR = Path(__file__).parent / "live_history_v2"
DRY_RUN = "--dry-run" in sys.argv

# ─── helpers ────────────────────────────────────────────────────────────────

def _load_rows(path: Path) -> list[dict]:
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            pass  # Línea corrupta: ignorar
    return rows


def _is_zombie_snapshot(row: dict) -> bool:
    """Snapshot del descanso: minuto=0 + stats de juego todas en cero."""
    if row.get("record_type", "snapshot") != "snapshot":
        return False
    state = (row.get("snapshot") or {}).get("state") or {}
    if state.get("minuto", -1) != 0.0:
        return False
    # Permitir el minuto 0 real (inicio del partido) si hay algo de actividad
    if (state.get("goles_local", 0) > 0 or state.get("goles_visitante", 0) > 0):
        return False  # Ya hay goles → no es el tick inicial
    # Basura pura: todo a cero o vacío
    dead = (
        state.get("tiros_local", 0) == 0.0
        and state.get("faltas", 0) == 0.0
        and state.get("xg_local", 0) == 0.0
        and state.get("corners", 0) == 0.0
    )
    return dead


def _extract_event_id(path: Path) -> str | None:
    """Extrae el event_id del nombre del archivo."""
    m = re.match(r"(?:\[(?:WIN|LOSS)\]_)?(\d+)_", path.name)
    return m.group(1) if m else None


def _is_vs_file(path: Path) -> bool:
    return path.name.endswith("_vs.jsonl") or "_vs." in path.name


def _has_real_name(path: Path) -> bool:
    """El archivo tiene nombres de equipos reales (no solo 'vs')."""
    return not _is_vs_file(path) and not path.stem.endswith("_vs")


# ─── Paso 1: Identificar duplicados _vs.jsonl ────────────────────────────────

def find_vs_duplicates(files: list[Path]) -> list[Path]:
    """Retorna archivos _vs.jsonl que tienen un gemelo con nombre real."""
    by_eid: dict[str, list[Path]] = defaultdict(list)
    for f in files:
        eid = _extract_event_id(f)
        if eid:
            by_eid[eid].append(f)

    to_delete = []
    for eid, group in by_eid.items():
        if len(group) < 2:
            continue
        has_named = any(_has_real_name(f) for f in group)
        if has_named:
            for f in group:
                if _is_vs_file(f):
                    to_delete.append(f)
    return to_delete


# ─── Paso 2: Filtrar snapshots zombie dentro de cada archivo ─────────────────

def clean_zombie_snapshots(path: Path) -> tuple[int, int]:
    """Filtra líneas zombie de un archivo. Retorna (total, eliminados)."""
    rows = _load_rows(path)
    clean = [r for r in rows if not _is_zombie_snapshot(r)]
    removed = len(rows) - len(clean)
    if removed > 0 and not DRY_RUN:
        path.write_text(
            "\n".join(json.dumps(r, ensure_ascii=True) for r in clean) + "\n",
            encoding="utf-8",
        )
    return len(rows), removed


# ─── Paso 3: Eliminar archivos `match_*` genéricos ──────────────────────────

def find_generic_match_files(files: list[Path]) -> list[Path]:
    return [f for f in files if f.name.startswith("match_")]


# ─── Paso 4: Eliminar archivos que quedaron vacíos tras el filtro ──────────

def find_empty_files(files: list[Path]) -> list[Path]:
    empty = []
    for f in files:
        rows = _load_rows(f)
        snapshots = [r for r in rows if r.get("record_type", "snapshot") == "snapshot"]
        has_closure = any(r.get("record_type") == "match_closure" for r in rows)
        if not snapshots and not has_closure:
            empty.append(f)
    return empty


# ─── Main ────────────────────────────────────────────────────────────────────

def main():
    if not HISTORY_DIR.exists():
        print(f"❌ Directorio no encontrado: {HISTORY_DIR}")
        return

    mode_label = "🔍 DRY-RUN" if DRY_RUN else "🗑️  LIMPIEZA REAL"
    print(f"\n{'='*60}")
    print(f"  {mode_label} — live_history_v2")
    print(f"{'='*60}\n")

    all_files = list(HISTORY_DIR.glob("*.jsonl"))
    print(f"📁 Archivos totales encontrados: {len(all_files)}\n")

    # ── Paso 1: Duplicados _vs.jsonl ─────────────────────────────────────────
    vs_dupes = find_vs_duplicates(all_files)
    print(f"[1/4] Archivos _vs.jsonl duplicados: {len(vs_dupes)}")
    for f in vs_dupes:
        size_kb = f.stat().st_size / 1024
        print(f"  🗑️  {f.name}  ({size_kb:.1f} KB)")
        if not DRY_RUN:
            f.unlink()

    # ── Paso 2: Snapshots zombie (minuto=0 con stats vacías) ─────────────────
    # Recalcular lista de archivos activos (sin los ya borrados)
    active_files = [f for f in all_files if f not in vs_dupes]
    # Excluir archivos `match_*` del filtro de snapshots por ahora
    active_data = [f for f in active_files if not f.name.startswith("match_")]

    total_removed_zombies = 0
    print(f"\n[2/4] Buscando snapshots zombie minuto=0 en {len(active_data)} archivos...")
    for f in active_data:
        total, removed = clean_zombie_snapshots(f)
        if removed > 0:
            print(f"  🧟 {f.name}: {removed}/{total} snapshots zombie eliminados")
            total_removed_zombies += removed
    if total_removed_zombies == 0:
        print("  ✅ Sin snapshots zombie encontrados.")

    # ── Paso 3: Archivos `match_*` genéricos ─────────────────────────────────
    generic = find_generic_match_files(active_files)
    print(f"\n[3/4] Archivos 'match_*' genéricos (zombie de prueba): {len(generic)}")
    for f in generic:
        size_kb = f.stat().st_size / 1024
        print(f"  🗑️  {f.name}  ({size_kb:.1f} KB)")
        if not DRY_RUN:
            f.unlink()

    # ── Paso 4: Archivos vacíos tras el filtrado ──────────────────────────────
    # Recalcular
    remaining = [
        f for f in active_files
        if f not in generic and f not in vs_dupes
    ]

    empty_files = find_empty_files(remaining)
    print(f"\n[4/4] Archivos vacíos tras filtrado: {len(empty_files)}")
    for f in empty_files:
        print(f"  🗑️  {f.name}")
        if not DRY_RUN:
            f.unlink()

    # ── Resumen ───────────────────────────────────────────────────────────────
    total_deleted = len(vs_dupes) + len(generic) + len(empty_files)
    print(f"\n{'='*60}")
    if DRY_RUN:
        print(f"  ✅ DRY-RUN completado. Se eliminarían:")
    else:
        print(f"  ✅ Limpieza completada:")
    print(f"     Archivos eliminados: {total_deleted}")
    print(f"     Snapshots zombie purgados: {total_removed_zombies}")
    print(f"{'='*60}\n")

    if DRY_RUN:
        print("💡 Ejecuta sin --dry-run para aplicar los cambios.\n")


if __name__ == "__main__":
    main()
