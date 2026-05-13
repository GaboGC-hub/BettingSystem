"""
scraper_service.py - Servicio de scraping de cuotas en tiempo real
====================================================================
Este archivo ha sido simplificado radicalmente. 
Gracias a la extension de Chrome (Modo Zero-Touch), el sistema ya no necesita
levantar navegadores Playwright pesados en el backend para monitorear Betano o Betplay.
Toda la interceptacion de cuotas ocurre en el navegador del usuario y se envia
por WebSocket (extension_ws_parser.py).
"""

import re

class OddsScraperService:
    def __init__(self, global_state_ref):
        self.global_state = global_state_ref
        self.active_tasks = {}
        self._api_client = None   # siempre None - PS3838 via extension Chrome
        self._api_mode = "extension"
        print("[SCRAPER] Modo Zero-Touch activo. Recolectando todas las cuotas via Chrome Extension.")

    def _handle_api_alert(self, message: str) -> None:
        pass

    def start(self):
        pass  # nada que iniciar

    def __del__(self):
        self.active_tasks.clear()

    def attach_scrapers(self, match_url: str, urls: dict):
        """Zero-Touch: Todas las casas de apuestas estan cubiertas por la extension Chrome."""
        print(f"[SCRAPER] Zero-Touch Tracking activado para: {match_url[-30:]}. Por favor asegurate de tener las URLs abiertas en Chrome con la extension.")
        self.active_tasks[match_url] = True

    def detach_scrapers(self, match_url: str):
        if match_url in self.active_tasks:
            self.active_tasks[match_url] = False

    @staticmethod
    def _extract_pinnacle_event_id(url: str) -> int | None:
        m = re.search(r"/(\d{7,12})(?:[/#?]|$)", url)
        if m:
            return int(m.group(1))
        return None
