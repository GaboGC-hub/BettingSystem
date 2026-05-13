import subprocess
import os
import signal
import sys
import time

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
if sys.stderr.encoding != 'utf-8':
    sys.stderr.reconfigure(encoding='utf-8')


def start_processes():
    # 1. Iniciar Backend (FastAPI)
    print("🚀 Iniciando Backend (FastAPI)...")
    backend_process = subprocess.Popen(
        [sys.executable, "server.py"],
        cwd=os.getcwd()
    )

    # 2. Iniciar Frontend (Vite)
    frontend_dir = os.path.join(os.getcwd(), "frontend")
    print("🚀 Iniciando Frontend (React/Vite)...")
    frontend_process = subprocess.Popen(
        ["npm.cmd", "run", "dev"] if os.name == 'nt' else ["npm", "run", "dev"],
        cwd=frontend_dir
    )

    print("\n✅ ¡Todo en marcha!")
    print("👉 Backend en: http://localhost:8000")
    print("👉 Frontend en: http://localhost:5173 (o el puerto que indique Vite)")
    print("\nPresiona CTRL+C para detener ambos simultáneamente.\n")

    try:
        while True:
            time.sleep(1)
            # Verificar si alguno de los procesos murió
            if backend_process.poll() is not None:
                print("❌ El proceso Backend se ha detenido.")
                break
            if frontend_process.poll() is not None:
                print("❌ El proceso Frontend se ha detenido.")
                break
    except KeyboardInterrupt:
        print("\n🛑 Deteniendo procesos...")
    finally:
        # Intentar cerrar ambos gracefully
        backend_process.terminate()
        frontend_process.terminate()
        print("👋 ¡Hasta luego!")

if __name__ == "__main__":
    start_processes()
