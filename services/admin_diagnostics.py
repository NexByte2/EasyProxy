"""Small, on-demand admin checks. No AI, speed test, or arbitrary URL fetching."""
import asyncio
import re
import time
from datetime import datetime, timezone

from aiohttp import web
import config_store
from config import check_password


def warp_checks(enabled, healthy=False, reason=""):
    """Translate only known probe fields; never expose raw errors, URLs or IPs."""
    def item(label, status, message):
        return {"label": label, "status": status, "message": message}

    if not enabled:
        return [item("WARP", "info", "Disattivato nelle impostazioni. Nessun test WARP eseguito.")]
    process = re.search(r"(?:^| )process=(up|down|unknown)(?=[ (]|$)", reason)
    socks = re.search(r"(?:^| )socks=(up|down)(?= |$)", reason)
    process_state = process.group(1) if process else "unknown"
    socket_state = socks.group(1) if socks else "unknown"
    rows = [item("Processo WireProxy",
                 {"up": "ok", "down": "error"}.get(process_state, "info"),
                 {"up": "Avviato.", "down": "Non risulta avviato. Prova Riconnetti WARP.",
                  "unknown": "Stato del processo non verificabile in questo ambiente."}[process_state]),
            item("Collegamento locale WARP",
                 {"up": "ok", "down": "error"}.get(socket_state, "info"),
                 {"up": "Il servizio locale risponde.", "down": "Il servizio locale non risponde. Prova Riconnetti WARP.",
                  "unknown": "Collegamento locale non verificato."}[socket_state])]
    if healthy:
        message = "La richiesta di prova è passata attraverso WARP. Questo non verifica i singoli siti video."
    elif socket_state == "down":
        message = "Tunnel non verificato: prima occorre ripristinare il collegamento locale."
    elif "warp=off" in reason:
        message = "La risposta di prova indica che WARP non è attivo sul percorso utilizzato."
    else:
        message = "Passaggio attraverso WARP non confermato. Il test può fallire anche per timeout o indisponibilità del servizio di verifica."
    rows.append(item("Tunnel WARP", "ok" if healthy else "error", message))
    return rows


class AdminDiagnosticsMixin:
    async def handle_admin_diagnostics(self, request):
        if not check_password(request):
            return web.json_response({"error": "Accesso richiesto."}, status=401)
        headers = {"Cache-Control": "no-store"}
        enabled = bool(config_store.get("enable_warp", False))
        cached = getattr(self, "_admin_diagnostics_cache", None)
        if cached and cached[1] == enabled and time.monotonic() - cached[0] < 15:
            return web.json_response(cached[2], headers=headers)
        if not hasattr(self, "_admin_diagnostics_lock"):
            self._admin_diagnostics_lock = asyncio.Lock()
        if self._admin_diagnostics_lock.locked():
            return web.json_response({"error": "Controllo già in corso. Attendi qualche secondo."},
                                     status=429, headers={**headers, "Retry-After": "15"})
        async with self._admin_diagnostics_lock:
            healthy, reason = False, ""
            if enabled:
                try:
                    healthy, reason = await asyncio.wait_for(self._probe_warp(timeout_sec=8), timeout=16)
                except (TimeoutError, OSError):
                    reason = ""
                except Exception:
                    # Diagnostic output must never contain raw exception secrets.
                    reason = ""
            changed = enabled != bool(config_store.get("enable_warp", False))
            rows = [{"label": "EasyProxy", "status": "ok", "message": "Il server risponde al pannello amministrativo."}]
            if changed:
                rows.append({"label": "WARP", "status": "info", "message": "Impostazioni cambiate durante il test. Ripeti il controllo."})
            else:
                rows.extend(warp_checks(enabled, healthy, reason))
            report = {"checked_at": datetime.now(timezone.utc).isoformat(), "checks": rows,
                      "scope": "Controllo del server e di WARP. Nessun link video o test di velocità eseguito."}
            if not changed:
                self._admin_diagnostics_cache = (time.monotonic(), enabled, report)
            return web.json_response(report, headers=headers)
