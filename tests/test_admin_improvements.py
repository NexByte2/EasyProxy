"""Offline regression tests: python -m unittest discover -s tests -v."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

# Never touch a real /data configuration while importing application modules.
_boot_dir = tempfile.TemporaryDirectory()
os.environ['CONFIG_DIR'] = _boot_dir.name
import config_store
from aiohttp import FormData, web
from aiohttp.test_utils import TestClient, TestServer
from services.admin_diagnostics import warp_checks
from services.proxy_pages import HLSProxyPagesMixin


class ConfigCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.patches = [patch.object(config_store, '_CONFIG_DIR', self.directory.name),
                        patch.object(config_store, '_CONFIG_FILE', str(Path(self.directory.name) / 'config.json')),
                        patch.object(config_store, '_config_data', None)]
        for item in self.patches:
            item.start()
            self.addCleanup(item.stop)
        config_store.get_all()

    def test_previous_config_and_reload(self):
        before = config_store.get_all()
        config_store.set('log_level', 'INFO')
        self.assertEqual(config_store.get_previous(), before)
        self.assertEqual(json.loads(Path(config_store._CONFIG_FILE).read_text())['log_level'], 'INFO')
        config_store._config_data = None
        self.assertEqual(config_store.get('log_level'), 'INFO')

    def test_failed_save_keeps_disk_and_memory(self):
        before = config_store.get_all()
        real_replace = os.replace
        def fail_current(source, destination):
            if destination == config_store._CONFIG_FILE:
                raise OSError('disk full')
            return real_replace(source, destination)
        with patch.object(config_store.os, 'replace', side_effect=fail_current):
            with self.assertRaises(OSError):
                config_store.set('log_level', 'INFO')
        self.assertEqual(config_store.get_all(), before)
        self.assertEqual(json.loads(Path(config_store._CONFIG_FILE).read_text()), before)
        self.assertEqual(list(Path(self.directory.name).glob('.config-*')), [])

    def test_returned_nested_values_cannot_mutate_settings(self):
        config_store.get('global_proxies').append('secret')
        config_store.get_all()['transport_routes'].append({'url': 'example'})
        self.assertEqual(config_store.get('global_proxies'), [])
        self.assertEqual(config_store.get('transport_routes'), [])

    def test_unchanged_save_preserves_previous(self):
        before = config_store.get_all()
        config_store.set('log_level', 'INFO')
        config_store.set('log_level', 'INFO')
        self.assertEqual(config_store.get_previous(), before)

    def test_backup_round_trip(self):
        config_store.validate_import(config_store.get_all())
        before = config_store.get_all()
        config_store.set('log_level', 'INFO')
        restored = config_store.validate_import(config_store.get_previous())
        config_store.replace_all(restored)
        self.assertEqual(config_store.get_all(), before)
        self.assertEqual(config_store.get_previous()['log_level'], 'INFO')

    def test_legacy_proxy_lists_and_direct_routes(self):
        config_store.validate_import({'extractor_proxies': {'x': ['http://example.org:8080']},
                                      'transport_routes': [{'url': 'example.org', 'proxy': None}],
                                      'proxy_test_concurrency': 0})

    def test_invalid_backups(self):
        for data in [[], {}, {'enable_warp': 'false'}, {'proxy_test_timeout': -1},
                     {'proxy_test_timeout': True}, {'global_proxies': [5]},
                     {'transport_routes': [{}]}, {'extractor_proxies': {'x': [2]}},
                     {'log_level': 'garbage'}, {'api_password': 'secret'},
                     {'proxy_test_concurrency': 2.5}]:
            with self.subTest(data=data), self.assertRaises(ValueError):
                config_store.validate_import(data)


class DiagnosticsCase(unittest.TestCase):
    def test_connected_and_secret_redaction(self):
        rows = warp_checks(True, True, 'process=unknown socks=up warp=on ip=1.2.3.4 detail=https://user:secret@example.org')
        self.assertEqual(rows[-1]['status'], 'ok')
        self.assertEqual(rows[0]['status'], 'info')
        self.assertNotIn('secret', json.dumps(rows))
        self.assertNotIn('1.2.3.4', json.dumps(rows))

    def test_socket_failure_is_not_reported_as_healthy(self):
        rows = warp_checks(True, False, 'component=wireproxy_socket process=up socks=down')
        self.assertEqual(rows[0]['status'], 'ok')
        self.assertEqual(rows[1]['status'], 'error')
        self.assertEqual(rows[2]['status'], 'error')

    def test_unknown_and_disabled_do_not_claim_verified(self):
        self.assertEqual(warp_checks(False)[0]['status'], 'info')
        self.assertEqual(warp_checks(True, False, 'timeout')[-1]['status'], 'error')


class AdminCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        for item in [patch.object(config_store, '_CONFIG_DIR', self.directory.name),
                     patch.object(config_store, '_CONFIG_FILE', str(Path(self.directory.name) / 'config.json')),
                     patch.object(config_store, '_config_data', None)]:
            item.start()
            self.addCleanup(item.stop)
        config_store.get_all()
        self.proxy = HLSProxyPagesMixin()
        self.proxy._probe_warp = AsyncMock(return_value=(True, 'process=up socks=up warp=on'))
        self.proxy._invalidate_extractors = lambda: None
        def auth(request):
            return request.headers.get('x-api-password') == 'test-only'
        for name in ['services.admin_diagnostics.check_password', 'services.proxy_pages.check_password']:
            item = patch(name, side_effect=auth)
            item.start()
            self.addCleanup(item.stop)
        application = web.Application()
        application.router.add_post('/diagnostics', self.proxy.handle_admin_diagnostics)
        application.router.add_post('/upload', self.proxy.handle_admin_api_upload)
        application.router.add_get('/download', self.proxy.handle_admin_api_download)
        self.client = TestClient(TestServer(application))
        await self.client.start_server()
        self.addAsyncCleanup(self.client.close)
        self.auth = {'x-api-password': 'test-only'}

    async def test_endpoints_require_auth(self):
        for path, method in [('/diagnostics', 'post'), ('/upload', 'post'), ('/download', 'get')]:
            response = await getattr(self.client, method)(path)
            self.assertEqual(response.status, 401)
        self.proxy._probe_warp.assert_not_awaited()

    async def test_disabled_warp_never_probes(self):
        response = await self.client.post('/diagnostics', headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')
        self.proxy._probe_warp.assert_not_awaited()

    async def test_probe_cache_and_toggle_invalidation(self):
        config_store.set('enable_warp', True)
        for _ in range(2):
            response = await self.client.post('/diagnostics', headers=self.auth)
            self.assertEqual(response.status, 200)
        self.proxy._probe_warp.assert_awaited_once()
        config_store.set('enable_warp', False)
        response = await self.client.post('/diagnostics', headers=self.auth)
        self.assertEqual((await response.json())['checks'][-1]['status'], 'info')

    async def test_concurrent_checks_are_bounded(self):
        config_store.set('enable_warp', True)
        self.proxy._admin_diagnostics_lock = asyncio.Lock()
        await self.proxy._admin_diagnostics_lock.acquire()
        try:
            response = await self.client.post('/diagnostics', headers=self.auth)
            self.assertEqual(response.status, 429)
        finally:
            self.proxy._admin_diagnostics_lock.release()
        self.proxy._probe_warp.assert_not_awaited()

    async def test_probe_exception_does_not_leak_secrets(self):
        config_store.set('enable_warp', True)
        self.proxy._probe_warp.side_effect = OSError('https://user:secret@example.org')
        response = await self.client.post('/diagnostics', headers=self.auth)
        body = await response.text()
        self.assertEqual(response.status, 200)
        self.assertNotIn('secret', body)
        self.assertEqual(json.loads(body)['checks'][-1]['status'], 'error')

    async def upload(self, data):
        form = FormData()
        form.add_field('config', data, filename='backup.json', content_type='application/json')
        return await self.client.post('/upload', data=form, headers=self.auth)

    async def test_upload_rejects_malformed_and_large_files_without_changes(self):
        before = config_store.get_all()
        for body, status in [(b'{bad', 400), (b'{"enable_warp":"false"}', 400), (b' ' * (256*1024+1), 413)]:
            response = await self.upload(body)
            self.assertEqual(response.status, status)
            await response.read()
            self.assertEqual(config_store.get_all(), before)
        self.assertFalse(Path(config_store._CONFIG_FILE + '.previous').exists())

    async def test_upload_creates_downloadable_previous(self):
        before = config_store.get_all()
        response = await self.upload(b'{"log_level":"INFO"}')
        self.assertEqual(response.status, 200)
        response = await self.client.get('/download?previous=1', headers=self.auth)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), before)
        self.assertEqual(response.headers['Cache-Control'], 'no-store')

    async def test_missing_previous_is_clear_404(self):
        response = await self.client.get('/download?previous=1', headers=self.auth)
        self.assertEqual(response.status, 404)

if __name__ == '__main__':
    unittest.main()
