import logging
import sys
import os
import asyncio
import aiohttp
import ipaddress
import socket
import urllib.parse
from aiohttp import web

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from services.proxy import HLSProxy
from services.proxy_shared import check_password
from config import PORT, RECORDINGS_DIR, APP_VERSION
from services.dual import service as dual_service
from services.recording_manager import RecordingManager
from routes.recordings import setup_recording_routes

logger = logging.getLogger(__name__)
DUAL_WARP_ACTIVE_FILE = "/tmp/easyproxy-warp-active"


def _read_file(path):
    with open(path, 'r', encoding='utf-8') as f:
        return f.read()


async def _tcp_up(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=timeout)
        writer.close()
        await writer.wait_closed()
        return True
    except (OSError, asyncio.TimeoutError):
        return False


async def _warp_trace(port: int) -> dict:
    if not await _tcp_up("127.0.0.1", port):
        return {"healthy": False, "ip": "", "warp": "off"}
    try:
        proc = await asyncio.create_subprocess_exec(
            "curl", "-fsS", "--max-time", "5", "--socks5-hostname", f"127.0.0.1:{port}",
            "https://www.cloudflare.com/cdn-cgi/trace",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=6)
        values = {}
        for line in out.decode("utf-8", "replace").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key] = value
        warp = values.get("warp", "off")
        return {"healthy": proc.returncode == 0 and warp in {"on", "plus"}, "ip": values.get("ip", ""), "warp": warp}
    except Exception:
        return {"healthy": False, "ip": "", "warp": "off"}


async def _dual_warp_state() -> dict:
    primary, secondary = await asyncio.gather(_warp_trace(1081), _warp_trace(1082))
    relay = await _tcp_up("127.0.0.1", 1080)
    try:
        active = _read_file(DUAL_WARP_ACTIVE_FILE).strip().lower()
    except OSError:
        active = ""
    if active not in {"primary", "secondary"}:
        active = "unknown"
    active_healthy = primary["healthy"] if active == "primary" else secondary["healthy"] if active == "secondary" else False
    return {
        "primary": primary["healthy"], "primary_ip": primary["ip"], "primary_warp": primary["warp"],
        "secondary": secondary["healthy"], "secondary_ip": secondary["ip"], "secondary_warp": secondary["warp"],
        "relay": relay, "active": active, "active_healthy": active_healthy,
        "failover_ready": relay and primary["healthy"] and secondary["healthy"],
    }


def _normalize_public_url(raw: str) -> str:
    raw = (raw or "").strip()
    if not raw:
        raise ValueError("URL vuoto")
    if "://" not in raw:
        raw = "https://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("Sono ammessi solo URL HTTP/HTTPS validi")
    if parsed.username or parsed.password:
        raise ValueError("Credenziali nell'URL non ammesse")
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


async def _assert_public_hostname(url: str) -> None:
    host = urllib.parse.urlsplit(url).hostname
    if not host:
        raise ValueError("Hostname mancante")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("DNS non risolto")
    addresses = {item[4][0].split('%', 1)[0] for item in infos}
    if not addresses:
        raise ValueError("DNS senza indirizzi")
    for text in addresses:
        try:
            ip = ipaddress.ip_address(text)
        except ValueError:
            raise ValueError("Indirizzo DNS non valido")
        if not ip.is_global:
            raise ValueError("Host locale/privato non ammesso")


def _classify_http(status: int) -> tuple[str, str]:
    if 200 <= status < 400:
        return "ok", "Raggiungibile"
    if status in {401, 403, 407, 429}:
        return "restricted", "Risponde ma limita/rifiuta la richiesta"
    if 400 <= status < 500:
        return "client_error", "Risponde con errore HTTP client"
    if status >= 500:
        return "server_error", "Errore del server remoto/intermedio"
    return "network_error", "Nessuna risposta HTTP"


async def _probe_video_host(url: str, route: str) -> dict:
    cmd = ["curl", "-sS", "-I", "--connect-timeout", "4", "--max-time", "8", "-o", "/dev/null",
           "-w", "%{http_code}\t%{time_total}\t%{remote_ip}"]
    if route == "warp_primary":
        cmd += ["--socks5-hostname", "127.0.0.1:1081"]
    elif route == "warp_secondary":
        cmd += ["--socks5-hostname", "127.0.0.1:1082"]
    cmd.append(url)
    started = asyncio.get_running_loop().time()
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout=10)
        elapsed_ms = round((asyncio.get_running_loop().time() - started) * 1000)
        text = out.decode("utf-8", "replace").strip()
        parts = text.split("\t") if text else []
        status = int(parts[0]) if parts and parts[0].isdigit() else 0
        remote_ip = parts[2] if len(parts) > 2 else ""
        kind, label = _classify_http(status)
        if proc.returncode != 0 and not status:
            detail = " ".join(err.decode("utf-8", "replace").split())[:180]
            return {"route": route, "status": 0, "kind": "network_error", "label": "Errore di rete/TLS/DNS", "latency_ms": elapsed_ms, "remote_ip": remote_ip, "detail": detail}
        return {"route": route, "status": status, "kind": kind, "label": label, "latency_ms": elapsed_ms, "remote_ip": remote_ip, "detail": ""}
    except asyncio.TimeoutError:
        return {"route": route, "status": 0, "kind": "timeout", "label": "Timeout", "latency_ms": 10000, "remote_ip": "", "detail": "Tempo massimo superato"}
    except Exception as exc:
        return {"route": route, "status": 0, "kind": "network_error", "label": "Errore di rete", "latency_ms": 0, "remote_ip": "", "detail": type(exc).__name__}


async def _test_video_url(raw: str) -> dict:
    try:
        url = _normalize_public_url(raw)
        await _assert_public_hostname(url)
    except ValueError as exc:
        return {"input": raw, "url": "", "error": str(exc), "checks": []}
    checks = await asyncio.gather(
        _probe_video_host(url, "direct"),
        _probe_video_host(url, "warp_primary"),
        _probe_video_host(url, "warp_secondary"),
    )
    return {"input": raw, "url": url, "error": "", "checks": list(checks)}


def create_app():
    dual_cache_dir = os.path.join(RECORDINGS_DIR, "dual_data")
    proxy = HLSProxy()
    app = web.Application(middlewares=[dual_service.dual_middleware], client_max_size=4 * 1024 * 1024)
    app['proxy'] = proxy
    app['dual_service'] = dual_service
    dual_service.install(app, dual_cache_dir)
    recording_manager = RecordingManager(recordings_dir=RECORDINGS_DIR)
    app['recording_manager'] = recording_manager

    async def handle_root_with_dual_warp(request):
        response = await proxy.handle_root(request)
        if response.status != 200 or not response.text:
            return response
        try:
            state = await _dual_warp_state()
            p = "✓" if state["primary"] else "✕"
            s = "✓" if state["secondary"] else "✕"
            r = "✓" if state["relay"] else "✕"
            old = f'<div class="warp-chip">WARP: {proxy.warp_status}</div>'
            new = f'<div class="warp-chip" title="Dual WARP health">DUAL WARP · P {p} · S {s} · RELAY {r} · ACTIVE {state["active"].upper()}</div>'
            response.text = response.text.replace(old, new)
        except Exception as exc:
            logger.debug("Unable to enrich homepage with dual-WARP state: %s", exc)
        return response

    async def handle_dual_warp_status(request):
        state = await _dual_warp_state()
        state["status"] = "ok" if state["relay"] and state["active_healthy"] else "degraded"
        return web.json_response(state)

    async def handle_video_host_test(request):
        if not check_password(request):
            return web.json_response({"error": "Unauthorized"}, status=401)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response({"error": "JSON non valido"}, status=400)
        urls = payload.get("urls", [])
        if isinstance(urls, str):
            urls = [urls]
        urls = [str(x).strip() for x in urls if str(x).strip()][:8]
        if not urls:
            return web.json_response({"error": "Inserisci almeno un sito o URL"}, status=400)
        results = await asyncio.gather(*[_test_video_url(url) for url in urls])
        return web.json_response({"results": list(results), "note": "Diagnostica di connettività: non tenta di aggirare controlli di accesso o protezioni del sito."})

    async def handle_admin_with_dual_warp(request):
        response = await proxy.handle_admin(request)
        if response.status != 200 or not response.text:
            return response
        script = r'''
<script>
(function () {
  function esc(v) { return String(v || '—').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function adminApi(path) {
    const u = new URL(path, location.origin);
    const pw = new URLSearchParams(location.search).get('api_password');
    if (pw) u.searchParams.set('api_password', pw);
    return u;
  }
  async function refreshDualWarpPanel() {
    const box = document.getElementById('dual-warp-runtime');
    if (!box) return;
    try {
      const r = await fetch(adminApi('/api/warp/dual-status'), {cache:'no-store'});
      const d = await r.json();
      const badge = (ok, active) => `<span class="badge ${ok ? 'badge-on' : 'badge-off'}">${ok ? 'ONLINE' : 'OFFLINE'}${active ? ' · ATTIVO' : ''}</span>`;
      box.innerHTML = `<div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:10px;margin:10px 0 14px">
        <div class="resource-item"><div class="resource-name">WARP Primario</div><div style="margin:8px 0">${badge(d.primary,d.active==='primary')}</div><div class="help-text" style="margin:0">IP: <strong>${esc(d.primary_ip)}</strong></div></div>
        <div class="resource-item"><div class="resource-name">WARP Secondario</div><div style="margin:8px 0">${badge(d.secondary,d.active==='secondary')}</div><div class="help-text" style="margin:0">IP: <strong>${esc(d.secondary_ip)}</strong></div></div>
        <div class="resource-item"><div class="resource-name">Failover</div><div style="margin:8px 0">${badge(d.failover_ready,false)}</div><div class="help-text" style="margin:0">Relay: ${d.relay ? '✓ pronto' : '✕ non disponibile'}</div></div>
      </div>`;
      const title = document.getElementById('dual-warp-title');
      if (title) title.textContent = d.failover_ready ? 'Dual Cloudflare WARP · Failover pronto' : 'Dual Cloudflare WARP · Degradato';
    } catch (e) { box.innerHTML = '<p class="status disconnected">Stato Dual-WARP non disponibile</p>'; }
  }
  function statusBadge(c) {
    const ok = c.kind === 'ok';
    const warn = ['restricted','client_error'].includes(c.kind);
    const bg = ok ? 'rgba(16,185,129,.15)' : warn ? 'rgba(245,158,11,.15)' : 'rgba(239,68,68,.15)';
    const fg = ok ? 'var(--accent-success)' : warn ? 'var(--accent-warning)' : 'var(--accent-error)';
    return `<span style="display:inline-block;padding:3px 8px;border-radius:6px;background:${bg};color:${fg};font-size:.7rem;font-weight:700">${esc(c.label)}${c.status ? ' · HTTP '+c.status : ''}</span>`;
  }
  function routeName(r) { return ({direct:'DIRECT',warp_primary:'WARP P',warp_secondary:'WARP S'})[r] || r; }
  async function runVideoHostTests() {
    const input = document.getElementById('video-host-input');
    const out = document.getElementById('video-host-results');
    const btn = document.getElementById('video-host-test-btn');
    const urls = input.value.split(/[\n,]+/).map(x => x.trim()).filter(Boolean).slice(0,8);
    if (!urls.length) { out.innerHTML = '<p class="status disconnected">Inserisci almeno un sito o URL.</p>'; return; }
    btn.disabled = true; btn.textContent = 'Test in corso…';
    out.innerHTML = '<p class="help-text">Controllo DIRECT, WARP primario e WARP secondario…</p>';
    try {
      const r = await fetch(adminApi('/api/admin/video-hosts/test'), {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({urls})});
      const d = await r.json();
      if (!r.ok) throw new Error(d.error || 'Test non riuscito');
      out.innerHTML = d.results.map(item => {
        if (item.error) return `<div class="resource-item" style="margin-top:10px"><strong>${esc(item.input)}</strong><div class="status disconnected">${esc(item.error)}</div></div>`;
        const checks = item.checks.map(c => `<div style="padding:8px 0;border-top:1px solid var(--border-primary)"><strong>${routeName(c.route)}</strong> ${statusBadge(c)}<div class="help-text" style="margin:5px 0 0">${c.latency_ms} ms${c.remote_ip ? ' · remoto '+esc(c.remote_ip) : ''}${c.detail ? ' · '+esc(c.detail) : ''}</div></div>`).join('');
        return `<div class="resource-item" style="margin-top:10px"><div style="overflow-wrap:anywhere"><strong>${esc(item.url)}</strong></div>${checks}</div>`;
      }).join('');
    } catch(e) { out.innerHTML = `<p class="status disconnected">${esc(e.message)}</p>`; }
    finally { btn.disabled = false; btn.textContent = '🔎 Testa siti'; }
  }
  function install() {
    const warpToggle = document.getElementById('warp-toggle');
    const card = warpToggle && warpToggle.closest('.card');
    if (card && !document.getElementById('dual-warp-runtime')) {
      const h2 = card.querySelector('h2');
      if (h2) h2.innerHTML = '<span class="icon">🌐</span> <span id="dual-warp-title">Dual Cloudflare WARP</span>';
      const runtime = document.createElement('div'); runtime.id = 'dual-warp-runtime'; runtime.innerHTML = '<p class="help-text">Verifica dei due tunnel WARP…</p>';
      const firstRow = card.querySelector('.row'); card.insertBefore(runtime, firstRow || null);
      refreshDualWarpPanel(); setInterval(refreshDualWarpPanel, 30000);
    }
    if (!document.getElementById('video-host-card')) {
      const grid = document.querySelector('.grid-2');
      if (grid) {
        const section = document.createElement('section'); section.id = 'video-host-card'; section.className = 'card card-wide';
        section.innerHTML = `<h2><span class="icon">🎬</span> Test host video</h2>
          <p class="help-text">Verifica raggiungibilità, HTTP e latenza separatamente via DIRECT, WARP Primario e WARP Secondario. Non modifica il routing e non tenta di superare protezioni del sito.</p>
          <textarea id="video-host-input" placeholder="https://esempio.com\nhttps://altro-host.com" style="width:100%;min-height:88px"></textarea>
          <div style="margin-top:10px"><button id="video-host-test-btn" class="btn btn-primary" onclick="runVideoHostTests()">🔎 Testa siti</button></div>
          <div id="video-host-results" style="margin-top:8px"></div>`;
        const warpCard = document.getElementById('warp-toggle')?.closest('.card');
        if (warpCard) warpCard.insertAdjacentElement('afterend', section); else grid.appendChild(section);
      }
    }
  }
  window.runVideoHostTests = runVideoHostTests;
  window.refreshDualWarpPanel = refreshDualWarpPanel;
  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', install); else install();
})();
</script>
'''
        response.text = response.text.replace('</body>', script + '</body>')
        return response

    app.router.add_get('/', handle_root_with_dual_warp)
    app.router.add_get('/api/warp/dual-status', handle_dual_warp_status)
    app.router.add_post('/api/admin/video-hosts/test', handle_video_host_test)
    app.router.add_get('/docs', proxy.handle_docs)
    app.router.add_get('/redoc', proxy.handle_redoc)
    app.router.add_get('/openapi.json', proxy.handle_openapi)
    app.router.add_get('/favicon.ico', proxy.handle_favicon)
    static_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    if not os.path.exists(static_path): os.makedirs(static_path)
    app.router.add_static('/static', static_path)
    app.router.add_get('/builder', proxy.handle_builder)
    app.router.add_get('/playlist/builder', proxy.handle_builder)
    app.router.add_get('/url-generator', proxy.handle_url_generator)
    app.router.add_get('/info', proxy.handle_info_page)
    app.router.add_get('/api/info', proxy.handle_api_info)
    app.router.add_get('/api/memory/profile', proxy.handle_memory_profile)
    app.router.add_post('/api/memory/profile/reset', proxy.handle_memory_profile_reset)
    app.router.add_get('/key', proxy.handle_key_request)
    app.router.add_get('/proxy/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/proxy/hls/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/proxy/mpd/manifest.m3u8', proxy.handle_proxy_request)
    app.router.add_get('/proxy/mpd/manifest.mpd', proxy.handle_proxy_request)
    app.router.add_get('/proxy/mpd/segment/{session_id}/{tail:.*}', proxy.handle_dash_segment)
    app.router.add_get('/proxy/stream', proxy.handle_proxy_request)
    app.router.add_get('/extractor', proxy.handle_extractor_request)
    for ext in ('video','video.m3u8','video.mp4','video.mpd','video.ts','video.m4s','video.vtt','video.aac','video.m4a','video.webm','video.mkv','video.avi','video.mov'):
        app.router.add_get('/extractor/' + ext, proxy.handle_extractor_request)
    for ext in ('segment.ts','segment.m4s','segment.mp4','segment.vtt'):
        app.router.add_get('/proxy/hls/' + ext, proxy.handle_proxy_request)
    app.router.add_get('/playlist', proxy.handle_playlist_request)
    app.router.add_get('/segment/{tail:.*}', proxy.handle_ts_segment)
    app.router.add_get('/decrypt/segment.mp4', proxy.handle_decrypt_segment)
    app.router.add_get('/decrypt/segment.ts', proxy.handle_decrypt_segment)
    app.router.add_get('/license', proxy.handle_license_request)
    app.router.add_post('/license', proxy.handle_license_request)
    app.router.add_post('/generate_urls', proxy.handle_generate_urls)
    app.router.add_get('/proxy/ip', proxy.handle_proxy_ip)
    app.router.add_get('/health', lambda r: web.json_response({"status": "ok", "version": APP_VERSION}))
    app.router.add_get('/api/dual/memory', dual_service.handle_memory)
    app.router.add_get('/api/sidecar/memory', dual_service.handle_memory)
    app.router.add_post('/dual/sync/links', proxy.handle_dual_sync_links)
    app.router.add_get('/dual/menifest.m3u8', proxy.handle_dual_server_m3u8)
    app.router.add_get('/dual/manifest.m3u8', proxy.handle_dual_server_m3u8)
    app.router.add_get('/admin', handle_admin_with_dual_warp)
    app.router.add_get('/admin/login', proxy.handle_admin_login)
    app.router.add_post('/api/admin/login', proxy.handle_admin_api_login)
    app.router.add_get('/admin/logout', proxy.handle_admin_logout)
    app.router.add_post('/api/admin/diagnostics', proxy.handle_admin_diagnostics)
    app.router.add_get('/api/admin/config', proxy.handle_admin_api_get)
    app.router.add_post('/api/admin/config', proxy.handle_admin_api_update)
    app.router.add_get('/api/admin/config/download', proxy.handle_admin_api_download)
    app.router.add_post('/api/admin/config/upload', proxy.handle_admin_api_upload)
    app.router.add_post('/api/admin/warp/toggle', proxy.handle_admin_api_warp_toggle)
    app.router.add_post('/api/admin/warp/reconnect', proxy.handle_admin_api_warp_reconnect)
    app.router.add_post('/api/admin/extractor/proxy', proxy.handle_admin_api_extractor_proxy)
    app.router.add_post('/api/admin/speedtest', proxy.handle_admin_api_speedtest)
    setup_recording_routes(app, recording_manager)
    app.router.add_route('OPTIONS', '/{tail:.*}', proxy.handle_options)

    async def cleanup_handler(app): await proxy.cleanup()
    app.on_cleanup.append(cleanup_handler)
    async def on_startup(app):
        asyncio.create_task(proxy.start_tasks())
        asyncio.create_task(recording_manager.cleanup_loop())
    app.on_startup.append(on_startup)
    async def on_shutdown(app): await recording_manager.shutdown()
    app.on_shutdown.append(on_shutdown)
    return app


app = create_app()


def main():
    if sys.platform == 'win32': logging.getLogger('asyncio').setLevel(logging.CRITICAL)
    logger.info("🚀 Starting HLS Proxy Server...")
    logger.info("📡 Server available at: http://localhost:%s", PORT)
    web.run_app(app, host='0.0.0.0', port=PORT)


if __name__ == '__main__':
    main()
