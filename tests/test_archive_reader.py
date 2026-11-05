"""Real HTTP tests for parallel reads, seeking, fallback, and cancellation."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer

from syncnest.archive_reader import ArchiveReader


class ArchiveReaderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.data = bytes(range(256)) * 1024
        self.requests = []
        self.active = self.peak = 0
        self.fault = None
        self.gate = asyncio.Event()
        self.patches = [patch('syncnest.archive_reader.CHUNK_SIZE', 4096), patch('syncnest.archive_reader.MIN_FILE_SIZE', 0)]
        for patched in self.patches:
            patched.start()
        app = web.Application()
        app.router.add_get('/file.mkv', self.source)
        self.server = TestServer(app, handler_cancellation=True)
        await self.server.start_server()
        self.url = str(self.server.make_url('/file.mkv'))
        self.reader = ArchiveReader(self.url, {'X-Fixture': 'preserved'})
        self.client = aiohttp.ClientSession()

    async def asyncTearDown(self):
        await self.reader.close()
        await self.client.close()
        await self.server.close()
        for patched in self.patches:
            patched.stop()

    async def source(self, request):
        self.assertEqual(request.headers['X-Fixture'], 'preserved')
        self.assertEqual(request.headers['Accept-Encoding'], 'identity')
        value = request.headers['Range']
        start, end = map(int, value.removeprefix('bytes=').split('-'))
        self.requests.append((start, end, request.headers.get('If-Range')))
        if self.fault == 'ignore':
            return web.Response(body=self.data)
        if (start, end) == (0, 0):
            return web.Response(status=206, body=self.data[:1], headers={
                'Content-Range': f'bytes 0-0/{len(self.data)}', 'ETag': '"fixture"'})
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            if self.fault == 'block':
                await self.gate.wait()
            await asyncio.sleep(0.01)
            if self.fault in {'once', 'drop'}:
                fault, self.fault = self.fault, None
                if fault == 'once':
                    return web.Response(status=503)
                response = web.StreamResponse(status=206, headers={
                    'Content-Range': f'bytes {start}-{end}/{len(self.data)}',
                    'Content-Length': str(end - start + 1)})
                await response.prepare(request)
                await response.write(self.data[start:start + 5])
                request.transport.close()
                return response
            if self.fault in {'always', 'forbidden'}:
                return web.Response(status=403 if self.fault == 'forbidden' else 500)
            if self.fault == 'changed':
                return web.Response(body=self.data)
            headers = {'Content-Range': f'bytes {start}-{end}/{len(self.data)}'}
            if self.fault == 'wrong_range':
                headers['Content-Range'] = f'bytes 0-{end}/{len(self.data)}'
            if self.fault == 'compressed':
                headers['Content-Encoding'] = 'gzip'
            return web.Response(status=206, body=self.data[start:end + 1], headers=headers)
        finally:
            self.active -= 1

    async def test_full_download_is_exact_and_reads_in_parallel(self):
        async with self.reader as url:
            async with self.client.get(url) as response:
                self.assertEqual(response.status, 200)
                self.assertEqual(await response.read(), self.data)
        self.assertEqual(self.peak, 4)
        self.assertTrue(all(end - start < 4096 for start, end, _ in self.requests))
        self.assertTrue(all(validator == '"fixture"' for _, end, validator in self.requests if end))
        self.assertFalse(self.reader.handlers)
        self.assertIsNone(self.reader.session)

    async def test_prefetch_continues_while_ffmpeg_is_slow_to_consume_a_block(self):
        blocked, release = asyncio.Event(), asyncio.Event()
        original_write = web.StreamResponse.write

        async def slow_write(response, data):
            if not blocked.is_set():
                blocked.set()
                await release.wait()
            return await original_write(response, data)

        async with self.reader as url:
            with patch.object(web.StreamResponse, 'write', slow_write):
                response = await self.client.get(url)
                try:
                    await asyncio.wait_for(blocked.wait(), 2)
                    async with asyncio.timeout(2):
                        # The probe and four original reads have already run.
                        # Another source request must run while output is blocked.
                        while len(self.requests) < 6:
                            await asyncio.sleep(0.01)
                    self.assertLessEqual(self.peak, 4)
                    release.set()
                    self.assertEqual(await response.read(), self.data)
                finally:
                    release.set()
                    response.close()

    async def test_seeks_suffix_ranges_and_head_preserve_bytes(self):
        async with self.reader as url:
            for value, start, end in [('bytes=501-9000', 501, 9000),
                                       ('bytes=260000-', 260000, len(self.data)-1),
                                       ('bytes=-57', len(self.data)-57, len(self.data)-1)]:
                async with self.client.get(url, headers={'Range': value}) as response:
                    self.assertEqual(response.status, 206)
                    self.assertEqual(response.headers['Content-Range'], f'bytes {start}-{end}/{len(self.data)}')
                    self.assertEqual(await response.read(), self.data[start:end+1])
            before = len(self.requests)
            async with self.client.head(url) as response:
                self.assertEqual(response.content_length, len(self.data))
            self.assertEqual(len(self.requests), before)
            for value in ['bytes=999999-', 'bytes=10-2', 'bytes=-0', 'bytes=-', 'bytes=0-5,9-15']:
                async with self.client.get(url, headers={'Range': value}) as response:
                    self.assertEqual(response.status, 416)

    async def test_unsupported_ranges_and_small_files_keep_direct_input(self):
        self.fault = 'ignore'
        async with self.reader as url:
            self.assertEqual(url, self.url)
            self.assertIsNone(self.reader.session)
        self.fault = None
        with patch('syncnest.archive_reader.MIN_FILE_SIZE', len(self.data) + 1):
            async with self.reader as url:
                self.assertEqual(url, self.url)
                self.assertIsNone(self.reader.session)

    async def test_retry_recovers_transient_error_and_truncated_body(self):
        async with self.reader:
            for fault in ['once', 'drop']:
                self.fault = fault
                before = len(self.requests)
                self.assertEqual(await self.reader.read_chunk(19, 4099), self.data[19:4100])
                self.assertEqual(len(self.requests)-before, 2)

    async def test_persistent_errors_have_a_finite_retry_limit(self):
        async with self.reader:
            self.fault = 'always'
            before = len(self.requests)
            with self.assertRaises(OSError):
                await self.reader.read_chunk(19, 4099)
            self.assertEqual(len(self.requests)-before, 3)

    async def test_forbidden_changed_and_invalid_ranges_are_never_spliced_into_video(self):
        async with self.reader:
            for fault in ['forbidden', 'changed', 'wrong_range', 'compressed']:
                self.fault = fault
                before = len(self.requests)
                expected = {'forbidden': web.HTTPForbidden, 'changed': web.HTTPConflict}.get(fault, ValueError)
                with self.assertRaises(expected):
                    await self.reader.read_chunk(19, 4099)
                self.assertEqual(len(self.requests)-before, 1)

    async def test_permanent_error_reaches_ffmpeg_without_becoming_retryable_502(self):
        async with self.reader as url:
            self.fault = 'forbidden'
            async with self.client.get(url, headers={'Range': 'bytes=200-300'}) as response:
                self.assertEqual(response.status, 403)
            self.assertEqual(len(self.requests), 2)  # Probe and one rejected read.

    async def test_close_cancels_all_prefetch_and_stops_listener(self):
        async with self.reader as url:
            self.fault = 'block'
            request = asyncio.create_task(self.client.get(url))
            try:
                async with asyncio.timeout(2):
                    while self.active < 4:
                        await asyncio.sleep(0.01)
                self.assertEqual(len(self.requests), 5)  # Probe + four bounded reads.
                await self.reader.close()
                self.assertFalse(self.reader.handlers)
                self.assertIsNone(self.reader.session)
                self.assertIsNone(self.reader.runner)
            finally:
                request.cancel()
                await asyncio.gather(request, return_exceptions=True)

    async def test_seek_replaces_a_blocked_read_without_waiting_for_its_timeout(self):
        async with self.reader as url:
            self.fault = 'block'
            old = asyncio.create_task(self.client.get(url))
            try:
                async with asyncio.timeout(2):
                    while self.active < 4:
                        await asyncio.sleep(0.01)
                # Disable the client's automatic retry so this models a seek,
                # not two clients repeatedly replacing one another's stream.
                old.cancel()
                await asyncio.gather(old, return_exceptions=True)
                self.fault = None
                async with asyncio.timeout(2):
                    async with self.client.get(url, headers={'Range': 'bytes=8000-16000'}) as response:
                        self.assertEqual(await response.read(), self.data[8000:16001])
            finally:
                old.cancel()
                await asyncio.gather(old, return_exceptions=True)

    async def test_cancel_during_startup_closes_session(self):
        entered = asyncio.Event()
        async def blocked(site):
            entered.set()
            await asyncio.Future()
        with patch.object(web.TCPSite, 'start', blocked):
            task = asyncio.create_task(self.reader.__aenter__())
            await asyncio.wait_for(entered.wait(), 2)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNone(self.reader.session)
        self.assertIsNone(self.reader.runner)

    async def test_room_closes_reader_on_encoder_failure_without_caching_local_url(self):
        from syncnest.stream import Room
        original = {'url': 'https://archive.org/download/fixture/fixture.mkv'}
        context = AsyncMock()
        context.__aenter__.return_value = 'http://127.0.0.1:12345/private-reader'
        with patch('syncnest.stream.extract_video', return_value=original), \
             patch('syncnest.stream.ArchiveReader', return_value=context), \
             patch('syncnest.stream.asyncio.create_subprocess_exec', side_effect=OSError('fixture failure')):
            room = Room(1, original['url'], lambda _: None)
            await room.task
        self.assertEqual(room.status, 'error')
        self.assertIsNone(room.source_info)
        self.assertNotIn('_input_url', original)
        context.__aexit__.assert_awaited_once()

    async def test_real_ffmpeg_can_seek_and_keep_audio_through_reader(self):
        import shutil
        if not shutil.which('ffmpeg'):
            self.skipTest('FFmpeg required')
        from syncnest.stream import ffmpeg_args
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'fixture.mkv'
            process = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=60',
                '-f', 'lavfi', '-i', 'sine=sample_rate=48000', '-t', '6',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '60', '-c:a', 'aac', str(source),
                stderr=asyncio.subprocess.PIPE)
            _, errors = await process.communicate()
            self.assertEqual(process.returncode, 0, errors.decode())
            self.data = source.read_bytes()
            async with self.reader as url:
                info = {'url': 'https://archive.org/download/fixture/fixture.mkv', '_input_url': url}
                args = ffmpeg_args(info, root, '144', start_time=2)
                process = await asyncio.create_subprocess_exec(*args, stderr=asyncio.subprocess.PIPE)
                try:
                    _, errors = await asyncio.wait_for(process.communicate(), 15)
                finally:
                    if process.returncode is None:
                        process.kill()
                        await process.wait()
                self.assertEqual(process.returncode, 0, errors.decode())
                probe = await asyncio.create_subprocess_exec(
                    'ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name,height', '-of', 'csv=p=0',
                    str(root / '000000.ts'), stdout=asyncio.subprocess.PIPE)
                tracks, _ = await probe.communicate()
                self.assertIn(b'h264,144', tracks)
                self.assertIn(b'aac', tracks)
                self.assertIn('#EXT-X-ENDLIST', (root / 'index.m3u8').read_text())


if __name__ == '__main__':
    unittest.main()
