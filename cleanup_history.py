import os
import glob
import json

def cleanup_orphaned_files():
    history_dir = 'live_history_v2'
    files = glob.glob(os.path.join(history_dir, '*.jsonl'))

    print(f"Buscando archivos huérfanos sin 'match_closure' en {history_dir}...\n")
    
    deleted_count = 0
    kept_count = 0

    for fpath in files:
        has_closure = False
        
        try:
            with open(fpath, 'r', encoding='utf-8') as f:
                lines = f.readlines()
                # Buscar de abajo hacia arriba es más rápido para el cierre
                for line in reversed(lines):
                    if not line.strip(): continue
                    try:
                        record = json.loads(line)
                        if record.get('record_type') == 'match_closure':
                            has_closure = True
                            break
                    except json.JSONDecodeError:
                        continue
        except Exception as e:
            print(f"Error leyendo {fpath}: {e}")
            
        if not has_closure:
            try:
                os.remove(fpath)
                print(f"[OK] Eliminado: {os.path.basename(fpath)}")
                deleted_count += 1
            except Exception as e:
                print(f"[Error] Error al eliminar {os.path.basename(fpath)}: {e}")
        else:
            kept_count += 1

    print(f"\nResumen de Limpieza:")
    print(f"Partidos válidos conservados: {kept_count}")
    print(f"Archivos basura eliminados: {deleted_count}")

if __name__ == "__main__":
    cleanup_orphaned_files()
