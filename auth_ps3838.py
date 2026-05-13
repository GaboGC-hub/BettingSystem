"""
auth_ps3838.py — Login automático a PS3838 con credenciales del .env
=====================================================================
Lee PS3838_USER y PS3838_PASS del archivo .env y hace el login
automáticamente usando Playwright. Guarda la sesión en ps3838_session.json
para que el scraper pueda reutilizarla sin volver a hacer login.

Uso:
    python auth_ps3838.py              → login y guarda sesión
    python auth_ps3838.py --check      → verifica si la sesión actual es válida
    python auth_ps3838.py --force      → fuerza nuevo login aunque la sesión sea válida
"""

import argparse
import json
import os
import sys
import time
from pathlib import Path

# ── Constantes ──────────────────────────────────────────────────────────────
SESSION_FILE = Path(__file__).parent / "ps3838_session.json"
ENV_FILE     = Path(__file__).parent / ".env"
PS3838_URL   = "https://www.ps3838.com/es/"

# La sesión de PS3838 dura ~48h antes de que BrowserSessionId expire.
# Refrescamos si el archivo tiene más de 20h de antigüedad para no llegar al límite.
SESSION_MAX_AGE_HOURS = 20


def _load_credentials() -> tuple[str, str]:
    """Lee PS3838_USER y PS3838_PASS del .env o del entorno del sistema."""
    user = os.environ.get("PS3838_USER", "")
    pw   = os.environ.get("PS3838_PASS", "")

    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip()
            if key == "PS3838_USER" and not user:
                user = val
            elif key == "PS3838_PASS" and not pw:
                pw = val

    if not user or not pw:
        print("[AUTH] ERROR: No se encontraron PS3838_USER y PS3838_PASS en el .env")
        sys.exit(1)

    return user, pw


def session_is_valid() -> bool:
    """Devuelve True si la sesión guardada existe y no ha caducado."""
    if not SESSION_FILE.exists():
        return False
    age_hours = (time.time() - SESSION_FILE.stat().st_mtime) / 3600
    if age_hours > SESSION_MAX_AGE_HOURS:
        print(f"[AUTH] Sesion tiene {age_hours:.1f}h de antiguedad (max {SESSION_MAX_AGE_HOURS}h) -> renovar")
        return False
    return True


def do_login(headless: bool = True) -> bool:
    """
    Realiza el login automático en ps3838.com con las credenciales del .env.
    Guarda la sesión en SESSION_FILE.
    Devuelve True si el login fue exitoso.
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("[AUTH] ERROR: Playwright no instalado. Ejecuta: pip install playwright && playwright install chromium")
        return False

    user, pw = _load_credentials()
    print(f"[AUTH] Iniciando login automatico para usuario: {user[:4]}****")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--window-size=1366,768",
                "--no-sandbox",
            ],
        )
        context = browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1366, "height": 768},
            locale="es-CO",
            timezone_id="America/Bogota",
        )
        page = context.new_page()

        try:
            # 1. Ir a PS3838
            print(f"[AUTH] Navegando a {PS3838_URL} ...")
            page.goto(PS3838_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2)

            # 2. Buscar el botón de login — PS3838 tiene "Iniciar sesión" / "Login"
            #    Intentamos varios selectores en orden de probabilidad
            login_clicked = False
            for selector in [
                "a[href*='login']",
                "button:has-text('Iniciar sesión')",
                "button:has-text('Login')",
                "a:has-text('Iniciar sesión')",
                "a:has-text('Login')",
                "[data-test='login-button']",
                ".login-button",
            ]:
                try:
                    btn = page.query_selector(selector)
                    if btn and btn.is_visible():
                        btn.click()
                        login_clicked = True
                        print(f"[AUTH] Botón de login encontrado: {selector}")
                        time.sleep(1.5)
                        break
                except Exception:
                    continue

            # Si no se encontró el botón, intentamos navegar directo al login
            if not login_clicked:
                print("[AUTH] Botón de login no encontrado, navegando directamente al formulario...")
                page.goto("https://www.ps3838.com/es/login", wait_until="domcontentloaded", timeout=20000)
                time.sleep(2)

            # 3. Rellenar el formulario de login
            #    PS3838 suele tener un modal o una página dedicada
            username_selectors = [
                "input[name='username']",
                "input[name='loginId']",
                "input[type='text'][placeholder*='suario']",
                "input[type='text'][placeholder*='sername']",
                "input[id*='username']",
                "input[id*='login']",
                "#username",
            ]
            password_selectors = [
                "input[name='password']",
                "input[type='password']",
                "input[id*='password']",
            ]

            username_input = None
            for sel in username_selectors:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        username_input = el
                        print(f"[AUTH] Campo usuario encontrado: {sel}")
                        break
                except Exception:
                    continue

            password_input = None
            for sel in password_selectors:
                try:
                    el = page.query_selector(sel)
                    if el and el.is_visible():
                        password_input = el
                        print(f"[AUTH] Campo contraseña encontrado: {sel}")
                        break
                except Exception:
                    continue

            if not username_input or not password_input:
                print("[AUTH] ERROR: No se encontraron los campos de usuario/contraseña.")
                print("[AUTH] Puede que PS3838 haya cambiado el DOM. Capturando screenshot...")
                page.screenshot(path="ps3838_login_debug.png")
                print("[AUTH] Screenshot guardado en ps3838_login_debug.png")
                browser.close()
                return False

            # Escribir credenciales con delays humanos
            username_input.click()
            time.sleep(0.3)
            username_input.fill("")
            page.keyboard.type(user, delay=60)
            time.sleep(0.5)

            password_input.click()
            time.sleep(0.3)
            password_input.fill("")
            page.keyboard.type(pw, delay=60)
            time.sleep(0.5)

            # 4. Hacer submit
            submit_selectors = [
                "button[type='submit']",
                "input[type='submit']",
                "button:has-text('Iniciar sesión')",
                "button:has-text('Entrar')",
                "button:has-text('Login')",
            ]
            submitted = False
            for sel in submit_selectors:
                try:
                    btn = page.query_selector(sel)
                    if btn and btn.is_visible():
                        btn.click()
                        submitted = True
                        print(f"[AUTH] Submit con: {sel}")
                        break
                except Exception:
                    continue

            if not submitted:
                # Último recurso: presionar Enter
                password_input.press("Enter")
                print("[AUTH] Submit con Enter (fallback)")

            # 5. Esperar a que el login complete — max 15s
            print("[AUTH] Esperando respuesta del servidor...")
            deadline = time.time() + 15
            logged_in = False
            while time.time() < deadline:
                time.sleep(1)
                # Indicadores de login exitoso
                url = page.url
                if any(kw in url for kw in ["/home", "/sports", "/live", "/account"]):
                    logged_in = True
                    break
                # También verificamos cookies
                cookies = context.cookies()
                auth_cookies = [c for c in cookies if c.get("name") in ("auth", "JSESSIONID") and "ps3838" in c.get("domain","")]
                if auth_cookies:
                    logged_in = True
                    break
                # Verificar si hay error de login
                try:
                    error = page.query_selector(".error-message, .alert-danger, [class*='error']")
                    if error and error.is_visible():
                        text = error.inner_text()
                        print(f"[AUTH] ERROR de login: {text}")
                        browser.close()
                        return False
                except Exception:
                    pass

            if not logged_in:
                print("[AUTH] No se detectó login exitoso después de 15s. Verificando URL actual...")
                print(f"[AUTH] URL actual: {page.url}")
                page.screenshot(path="ps3838_login_debug.png")
                print("[AUTH] Screenshot guardado en ps3838_login_debug.png")
                # Guardamos de todas formas por si acaso
                pass

            # 6. Esperar un poco más para que se completen las cookies de sesión
            time.sleep(3)

            # 7. Guardar sesión
            context.storage_state(path=str(SESSION_FILE))
            print(f"[AUTH] Sesion guardada en {SESSION_FILE}")
            if logged_in:
                print("[AUTH] Login exitoso detectado")
            else:
                print("[AUTH] Login no confirmado, pero sesion guardada de todas formas")

            browser.close()
            return logged_in

        except Exception as e:
            print(f"[AUTH] Excepción durante login: {e}")
            try:
                page.screenshot(path="ps3838_login_error.png")
                print("[AUTH] Screenshot de error guardado en ps3838_login_error.png")
            except Exception:
                pass
            browser.close()
            return False


def ensure_session(force: bool = False, headless: bool = True) -> bool:
    """
    API pública: garantiza que haya una sesión válida.
    Si la sesión es inválida o force=True, hace el login automático.
    
    Usado por scraper_service.py para auto-renovar sesión al inicio.
    """
    if not force and session_is_valid():
        age = (time.time() - SESSION_FILE.stat().st_mtime) / 3600
        print(f"[AUTH] Sesión existente válida (age: {age:.1f}h). Sin necesidad de login.")
        return True

    return do_login(headless=headless)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Login automático a PS3838")
    parser.add_argument("--check",   action="store_true", help="Solo verificar si la sesión es válida")
    parser.add_argument("--force",   action="store_true", help="Forzar nuevo login aunque la sesión sea válida")
    parser.add_argument("--visible", action="store_true", help="Mostrar el browser (para debug)")
    args = parser.parse_args()

    if args.check:
        valid = session_is_valid()
        print(f"[AUTH] Sesion {('VALIDA' if valid else 'EXPIRADA o inexistente')}")
        sys.exit(0 if valid else 1)

    headless = not args.visible
    ok = ensure_session(force=args.force, headless=headless)
    sys.exit(0 if ok else 1)
