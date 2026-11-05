"""The public-only egress proxy that yt-dlp, FFmpeg, and Archive reads use."""

import asyncio
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from syncnest import egress


async def exchange(proxy, request):
    host, port = proxy.url.removeprefix('http://').split(':')
    reader, writer = await asyncio.open_connection(host, int(port))
    writer.write(request.encode())
    await writer.drain()
    try:
        return await asyncio.wait_for(reader.read(), 5)
    finally:
        writer.close()


class EgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []

        async def record(request):
            self.requests.append((request.path, request.headers.get('Connection')))
            return web.Response(text='internal')

        app = web.Application()
        app.router.add_get('/{tail:.*}', record)
        self.server = TestServer(app, host='127.0.0.1')
        await self.server.start_server()
        self.proxy = await egress.EgressProxy().__aenter__()

    async def asyncTearDown(self):
        await self.proxy.__aexit__(None, None, None)
        await self.server.close()
        egress.revoke_loopback(self.server.port)

    def test_only_public_unicast_addresses_are_permitted(self):
        for address in ['127.0.0.1', '10.0.0.1', '172.16.0.1', '192.168.1.1', '169.254.169.254',
                        '100.64.0.1', '0.0.0.0', '224.0.0.1', '::1', 'fe80::1', 'fc00::1',
                        '::ffff:127.0.0.1', '::ffff:169.254.169.254', '64:ff9b::a00:1',
                        '::127.0.0.1', '::169.254.169.254', '::10.0.0.1']:
            with self.subTest(address=address):
                self.assertFalse(egress.public_address(address))
        for address in ['8.8.8.8', '1.1.1.1', '2606:4700:4700::1111', '64:ff9b::808:808']:
            with self.subTest(address=address):
                self.assertTrue(egress.public_address(address))

    async def test_loopback_and_private_targets_are_refused_before_connecting(self):
        port = self.server.port
        for request in [f'CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n',
                        f'CONNECT localhost:{port} HTTP/1.1\r\n\r\n',
                        f'CONNECT [::1]:{port} HTTP/1.1\r\n\r\n',
                        f'GET http://127.0.0.1:{port}/secret HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n',
                        'GET http://169.254.169.254/latest/meta-data/ HTTP/1.1\r\n\r\n',
                        'CONNECT 10.255.255.1:443 HTTP/1.1\r\n\r\n']:
            with self.subTest(request=request.split('\r\n')[0]):
                self.assertTrue((await exchange(self.proxy, request)).startswith(b'HTTP/1.1 403'))
        self.assertEqual(self.requests, [])

    async def test_malformed_requests_are_rejected(self):
        for request in ['nonsense\r\n\r\n', 'CONNECT example.test HTTP/1.1\r\n\r\n',
                        'GET /relative HTTP/1.1\r\n\r\n', 'GET ftp://example.test/ HTTP/1.1\r\n\r\n',
                        'CONNECT example.test:99999 HTTP/1.1\r\n\r\n']:
            with self.subTest(request=request.split('\r\n')[0]):
                self.assertTrue((await exchange(self.proxy, request)).startswith(b'HTTP/1.1 400'))

    async def test_registered_loopback_reader_is_reachable_by_tunnel_and_forwarding(self):
        port = self.server.port
        egress.allow_loopback(port)
        tunnel = await exchange(self.proxy, f'CONNECT 127.0.0.1:{port} HTTP/1.1\r\n\r\n'
                                f'GET /tunnel HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
        self.assertTrue(tunnel.startswith(b'HTTP/1.1 200 Connection Established'))
        self.assertIn(b'internal', tunnel)
        forwarded = await exchange(self.proxy, f'GET http://127.0.0.1:{port}/plain?x=1 HTTP/1.1\r\n'
                                   'Host: 127.0.0.1\r\nConnection: keep-alive\r\n\r\n')
        self.assertIn(b'internal', forwarded)
        # Forwarded requests use origin form and one request per connection.
        self.assertEqual(self.requests, [('/tunnel', 'close'), ('/plain', 'close')])

    async def test_environment_routes_clients_and_drops_bypass_lists(self):
        with patch.dict(os.environ, {'no_proxy': '*', 'NO_PROXY': '127.0.0.1', 'ALL_PROXY': 'socks5://x'}):
            self.proxy.route_environment()
            for name in egress.PROXY_VARIABLES:
                self.assertEqual(os.environ[name], self.proxy.url)
            for name in egress.BYPASS_VARIABLES:
                self.assertNotIn(name, os.environ)

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg is required')
    async def test_ffmpeg_playlist_cannot_reach_internal_services(self):
        internal, fetched = [], []

        async def secret(request):
            internal.append(request.path)
            return web.Response(text='secret')

        app = web.Application()
        app.router.add_get('/{tail:.*}', secret)
        hidden = TestServer(app, host='127.0.0.1')
        await hidden.start_server()
        # The public playlist is reachable; its segments point at an unregistered service.
        playlist = ('#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n'
                    f'http://127.0.0.1:{hidden.port}/segment.ts\n#EXTINF:1,\n'
                    'http://169.254.169.254/latest/meta-data/iam.ts\n#EXT-X-ENDLIST\n')

        async def manifest(request):
            fetched.append(request.path)
            return web.Response(text=playlist, content_type='application/vnd.apple.mpegurl')

        source = web.Application()
        source.router.add_get('/index.m3u8', manifest)
        public = TestServer(source, host='127.0.0.1')
        await public.start_server()
        egress.allow_loopback(public.port)
        try:
            environment = os.environ | {name: self.proxy.url for name in egress.PROXY_VARIABLES}
            with tempfile.TemporaryDirectory() as directory:
                await asyncio.to_thread(subprocess.run, [
                    'ffmpeg', '-hide_banner', '-loglevel', 'error', '-nostdin', '-rw_timeout', '3000000',
                    '-i', f'http://127.0.0.1:{public.port}/index.m3u8', '-c', 'copy', '-f', 'mpegts',
                    str(Path(directory) / 'out.ts')], env=environment, capture_output=True, timeout=30)
            # The playlist itself arrived through the proxy; only its internal entries were refused.
            self.assertEqual(fetched, ['/index.m3u8'])
            self.assertEqual(internal, [])
        finally:
            egress.revoke_loopback(public.port)
            await public.close()
            await hidden.close()


if __name__ == '__main__':
    unittest.main()
