"""Runtime compatibility patch for the EasyProxy admin dashboard.

The dual-WARP dashboard no longer renders the legacy #warp-status node, while
older admin JavaScript still tries to update it. Safari then throws because
getElementById('warp-status') returns null. Patch both status-update sites to
be null-safe before the application serves the template.
"""
from pathlib import Path


def _patch_admin_template() -> None:
    path = Path(__file__).resolve().parent / "templates" / "admin.html"
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return

    old = """        const ws = document.getElementById('warp-status');
        ws.textContent = warpStatusLabel(config.warp_status);
        ws.className = 'status ' + (config.warp_status === 'Connected' ? 'connected' : 'disconnected');"""
    new = """        const ws = document.getElementById('warp-status');
        if (ws) {
            ws.textContent = warpStatusLabel(config.warp_status);
            ws.className = 'status ' + (config.warp_status === 'Connected' ? 'connected' : 'disconnected');
        }"""

    old_refresh = """        const ws = document.getElementById('warp-status');
        ws.textContent = warpStatusLabel(data.warp_status);
        ws.className = 'status ' + (data.warp_status === 'Connected' ? 'connected' : 'disconnected');"""
    new_refresh = """        const ws = document.getElementById('warp-status');
        if (ws) {
            ws.textContent = warpStatusLabel(data.warp_status);
            ws.className = 'status ' + (data.warp_status === 'Connected' ? 'connected' : 'disconnected');
        }"""

    patched = source.replace(old, new).replace(old_refresh, new_refresh)
    if patched != source:
        try:
            path.write_text(patched, encoding="utf-8")
            print("[AdminUI] WARP status DOM guards applied")
        except OSError:
            pass


_patch_admin_template()
