"""Runtime compatibility patch for the EasyProxy admin dashboard.

The dual-WARP dashboard replaces the legacy WARP heading at runtime. Older
admin JavaScript still updates #warp-status and #warp-ip, so either lookup can
return null after the heading has been replaced. Patch both config-load and
status-refresh sites to be null-safe before the application serves the
admin template.
"""
from pathlib import Path


def _patch_admin_template() -> None:
    path = Path(__file__).resolve().parent / "templates" / "admin.html"
    try:
        source = path.read_text(encoding="utf-8")
    except OSError:
        return

    replacements = (
        (
            """        const ws = document.getElementById('warp-status');
        ws.textContent = warpStatusLabel(config.warp_status);
        ws.className = 'status ' + (config.warp_status === 'Connected' ? 'connected' : 'disconnected');""",
            """        const ws = document.getElementById('warp-status');
        if (ws) {
            ws.textContent = warpStatusLabel(config.warp_status);
            ws.className = 'status ' + (config.warp_status === 'Connected' ? 'connected' : 'disconnected');
        }""",
        ),
        (
            """        const ws = document.getElementById('warp-status');
        ws.textContent = warpStatusLabel(data.warp_status);
        ws.className = 'status ' + (data.warp_status === 'Connected' ? 'connected' : 'disconnected');""",
            """        const ws = document.getElementById('warp-status');
        if (ws) {
            ws.textContent = warpStatusLabel(data.warp_status);
            ws.className = 'status ' + (data.warp_status === 'Connected' ? 'connected' : 'disconnected');
        }""",
        ),
        (
            """        document.getElementById('warp-ip').textContent = config.warp_ip ? '(' + config.warp_ip + ')' : '';""",
            """        const warpIp = document.getElementById('warp-ip');
        if (warpIp) warpIp.textContent = config.warp_ip ? '(' + config.warp_ip + ')' : '';""",
        ),
        (
            """        document.getElementById('warp-ip').textContent = data.warp_ip ? '(' + data.warp_ip + ')' : '';""",
            """        const warpIp = document.getElementById('warp-ip');
        if (warpIp) warpIp.textContent = data.warp_ip ? '(' + data.warp_ip + ')' : '';""",
        ),
    )

    patched = source
    for old, new in replacements:
        patched = patched.replace(old, new)

    if patched != source:
        try:
            path.write_text(patched, encoding="utf-8")
            print("[AdminUI] WARP status/IP DOM guards applied")
        except OSError:
            pass


_patch_admin_template()
