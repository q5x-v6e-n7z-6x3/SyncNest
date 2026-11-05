"""Offline regressions for source freshness, seamless changes, and HTTP recovery."""
import asyncio
import copy
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from yt_dlp.extractor.lbry import LBRYIE

from syncnest.stream import OdyseeIE, Room, Rooms, extract_video, ffmpeg_args, select_video
from tests.test_resolution import AUDIO, URL, select, video


class SourceRecoveryTests(unittest.TestCase):
    def test_odysee_proxy_moves_to_current_endpoint_and_keeps_request(self):
        extractor = OdyseeIE()
        with patch.object(LBRYIE, '_download_json', return_value={'result': 'ok'}) as request:
            result = extractor._download_json('https://api.lbry.tv/api/v1/proxy', 'claim',
                                             data=b'{"method":"get"}', headers={'Referer': 'https://odysee.com/'})
        self.assertEqual(result, {'result': 'ok'})
        self.assertEqual(request.call_args.args[0], 'https://api.na-backend.odysee.com/api/v1/proxy')
        self.assertEqual(request.call_args.kwargs['data'], b'{"method":"get"}')
        self.assertEqual(request.call_args.kwargs['headers']['Referer'], 'https://odysee.com/')

    def test_odysee_live_api_is_not_rewritten(self):
        with patch.object(LBRYIE, '_download_json', return_value={}) as request:
            OdyseeIE()._download_json('https://api.odysee.live/livestream/is_live', 'claim')
        self.assertEqual(request.call_args.args[0], 'https://api.odysee.live/livestream/is_live')

    def test_odysee_routes_through_override(self):
        with patch.object(OdyseeIE, '_real_extract', return_value={
            'id': 'claim', 'title': 'Fixture', 'url': 'https://example.test/video.mp4', 'ext': 'mp4',
        }) as extract:
            info = extract_video('https://odysee.com/@example:a/video:b')
        extract.assert_called_once()
        self.assertEqual(info['id'], 'claim')

    def test_cached_split_selection_can_switch_to_combined_and_back(self):
        original = select([video(720), video(2160, combined=True), AUDIO], '720')
        original['extractor_key'] = 'KickClip'
        before = copy.deepcopy(original)
        with patch('syncnest.stream.yt_dlp.YoutubeDL.extract_info') as network:
            high = select_video(original, 'best')
            low = select_video(high, '720')
        network.assert_not_called()
        self.assertNotIn('requested_formats', high)
        self.assertEqual(high['height'], 2160)
        self.assertEqual(high['extractor_key'], 'KickClip')
        self.assertEqual(low['requested_formats'][0]['height'], 720)
        self.assertEqual(original, before)

    def test_cached_selection_does_not_initialize_unused_site_extractors(self):
        original = select([video(720), video(1080), AUDIO], '720')
        with patch('syncnest.stream.yt_dlp.YoutubeDL.add_default_info_extractors',
                   side_effect=AssertionError('Cached formats do not need site extractors')):
            result = select_video(original, '1080')
        self.assertEqual(result['requested_formats'][0]['height'], 1080)

    def test_live_inputs_are_not_throttled_and_recordings_can_catch_up(self):
        for live in (True, False):
            with self.subTest(live=live):
                with patch('syncnest.stream.supports_readrate_catchup', return_value=True):
                    args = ffmpeg_args({'url': 'https://example.test/media.m3u8', 'is_live': live}, Path('/tmp/hls'))
                self.assertEqual('-re' in args, not live)
                self.assertEqual('-readrate_catchup' in args, not live)
                if not live:
                    self.assertGreater(float(args[args.index('-readrate_catchup') + 1]), 1)
        args = ffmpeg_args({'url': 'https://example.test/media.m3u8', 'live_status': 'is_live'}, Path('/tmp/hls'))
        self.assertNotIn('-re', args)

    def test_older_ffmpeg_build_keeps_playback_without_new_option(self):
        with patch('syncnest.stream.supports_readrate_catchup', return_value=False):
            args = ffmpeg_args({'url': 'https://example.test/video.mp4'}, Path('/tmp/hls'))
        self.assertNotIn('-readrate_catchup', args)
        self.assertIn('-re', args)

    def test_cached_format_selection_rejects_non_http_rendition(self):
        info = select([video(720), AUDIO], '720')
        info['formats'].append(video(2160, combined=True) | {'url': 'file:///private/video.mp4'})
        with self.assertRaisesRegex(ValueError, 'HTTP video stream'):
            select_video(info, 'best')

    def test_archive_read_ahead_and_probe_do_not_apply_to_similar_host(self):
        for host, expected in [('archive.org', True), ('dn800303.us.archive.org', True),
                               ('evilarchive.org', False)]:
            with self.subTest(host=host):
                args = ffmpeg_args({'url': f'https://{host}/file.mp4'}, Path('/tmp/hls'))
                self.assertEqual(args[args.index('-i') + 1].startswith('async:'), expected)
                self.assertEqual('-analyzeduration' in args, expected)


class WarmSwitchTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rooms = Rooms()
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.entered, self.release = asyncio.Event(), asyncio.Event()
        self.failures = 0
        self.candidates = []

        async def run(room):
            try:
                if room.resolution != '720':
                    self.candidates.append(room)
                    self.entered.set()
                    await self.release.wait()
                    if self.failures:
                        self.failures -= 1
                        room.status = 'error'
                        return
                room.directory = self.root / room.id
                room.directory.mkdir()
                room.status = 'playing'
                await asyncio.Future()
            finally:
                room.directory = None

        self.run_patch = patch.object(Room, 'run', run)
        self.run_patch.start()
        self.original = self.rooms.prepare(42, URL)
        await self.original.ready.wait()
        self.original.source_info = select([video(720), video(1080), AUDIO], '720')
        self.original.source_extracted_at = time.monotonic()

    async def asyncTearDown(self):
        await self.rooms.close()
        self.run_patch.stop()
        self.temporary.cleanup()

    async def start_switch(self):
        task = asyncio.create_task(self.rooms.change_resolution(42, '1080'))
        await asyncio.wait_for(self.entered.wait(), 1)
        return task

    async def test_current_stream_is_kept_until_replacement_is_ready(self):
        task = await self.start_switch()
        self.assertIs(self.rooms.channels[42], self.original)
        self.assertTrue(self.original.active)
        self.assertEqual(self.original.status, 'playing')
        self.release.set()
        replacement = await task
        self.assertIs(self.rooms.channels[42], replacement)
        self.assertEqual(replacement.status, 'playing')
        self.assertFalse(self.original.active)
        self.assertIsNone(self.original.pending_room)
        self.assertEqual(replacement.source_info['requested_formats'][0]['height'], 1080)

    async def test_failed_replacement_preserves_current_video(self):
        self.failures = 2
        task = await self.start_switch()
        self.release.set()
        with self.assertRaisesRegex(ValueError, 'current video is still playing'):
            await task
        self.assertIs(self.rooms.channels[42], self.original)
        self.assertTrue(self.original.active)
        self.assertFalse(self.original.changing_resolution)
        self.assertTrue(all(not room.active for room in self.candidates))

    async def test_failed_cached_url_is_retried_with_fresh_extraction(self):
        self.failures = 1
        task = await self.start_switch()
        self.release.set()
        replacement = await task
        self.assertEqual(len(self.candidates), 2)
        self.assertIsNone(replacement.source_info)
        self.assertFalse(self.candidates[0].active)
        self.assertTrue(replacement.active)

    async def test_expired_or_live_source_is_not_reused(self):
        for live, age in [(False, 121), (True, 0)]:
            with self.subTest(live=live):
                self.original.is_live = live
                self.original.source_extracted_at = time.monotonic() - age
                self.failures = 1
                self.entered.clear()
                self.release.clear()
                self.candidates.clear()
                task = await self.start_switch()
                self.assertIsNone(self.candidates[0].source_info)
                self.release.set()
                with self.assertRaises(ValueError):
                    await task
                self.assertEqual(len(self.candidates), 1)

    async def test_stop_cancels_pending_replacement_without_restart(self):
        task = await self.start_switch()
        await self.original.stop()
        with self.assertRaisesRegex(ValueError, 'stopped or changed'):
            await task
        self.assertFalse(self.original.active)
        self.assertTrue(all(not room.active for room in self.candidates))
        self.assertIs(self.rooms.channels[42], self.original)

    async def test_close_cleans_up_both_encoders_during_switch(self):
        task = await self.start_switch()
        await self.rooms.close()
        with self.assertRaises(ValueError):
            await task
        self.assertTrue(all(not room.active for room in self.candidates))
        self.assertFalse(self.original.active)

    async def test_cancelled_command_keeps_current_stream_and_cleans_candidate(self):
        task = await self.start_switch()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(self.original.active)
        self.assertIs(self.rooms.channels[42], self.original)
        self.assertTrue(all(not room.active for room in self.candidates))

    async def test_second_switch_is_rejected_while_current_stream_keeps_playing(self):
        task = await self.start_switch()
        with self.assertRaisesRegex(ValueError, 'already in progress'):
            await self.rooms.change_resolution(42, 'best')
        self.release.set()
        await task

    async def test_quality_replacement_counts_toward_process_stream_limit(self):
        self.release.set()
        with patch('syncnest.stream.MAX_STREAMS', 1):
            with self.assertRaisesRegex(ValueError, 'busy'):
                await self.rooms.change_resolution(42, '1080')
        self.assertIs(self.rooms.channels[42], self.original)
        self.assertTrue(self.original.active)
        self.assertFalse(self.original.changing_resolution)
        self.assertIsNone(self.original.pending_room)


class ResourceLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancelled_stop_does_not_interrupt_encoder_cleanup(self):
        terminated, release = asyncio.Event(), asyncio.Event()
        process = Mock(returncode=None, stderr=asyncio.StreamReader())
        process.terminate.side_effect = terminated.set

        async def reap():
            await release.wait()
            process.returncode = 0
            process.stderr.feed_eof()
            return 0

        process.wait = AsyncMock(side_effect=reap)
        with patch('syncnest.stream.asyncio.create_subprocess_exec', return_value=process), \
             patch('syncnest.stream.ffmpeg_args', return_value=['ffmpeg']):
            room = Room(42, URL, lambda _: None, source_info={'url': 'https://example.test/video.mp4'})
            stopping = None
            try:
                async with asyncio.timeout(1):
                    while room.process is None:
                        await asyncio.sleep(0)
                directory = room.directory
                stopping = asyncio.create_task(room.stop())
                await asyncio.wait_for(terminated.wait(), 1)
                stopping.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await stopping
                self.assertFalse(room.task.done(), 'Cancelling a command must not cancel cleanup.')
                release.set()
                await asyncio.wait_for(asyncio.gather(room.task, return_exceptions=True), 1)
                self.assertEqual(process.returncode, 0)
                self.assertIsNone(room.directory)
                self.assertIsNone(room.source_info)
                self.assertFalse(directory.exists())
            finally:
                release.set()
                process.stderr.feed_eof()
                await room.stop()
                if stopping:
                    await asyncio.gather(stopping, return_exceptions=True)

    async def test_stream_limit_is_shared_and_released_after_stop_or_failure(self):
        rooms, other = Rooms(), Rooms()

        async def idle(room):
            await asyncio.Future()

        try:
            with patch('syncnest.stream.MAX_STREAMS', 1), patch.object(Room, 'run', idle):
                first = rooms.prepare(1, URL)
                with self.assertRaisesRegex(ValueError, 'busy'):
                    other.prepare(2, URL)
                await first.stop()  # Also releases a room cancelled before run starts.
                second = other.prepare(2, URL)
                self.assertTrue(second.active)
                await second.stop()
            with patch('syncnest.stream.MAX_STREAMS', 1), \
                 patch('syncnest.stream.extract_video', side_effect=ValueError('Fixture unavailable')):
                failed = rooms.prepare(1, URL)
                await failed.task
                self.assertEqual(failed.status, 'error')
                retry = other.prepare(2, URL)
                await retry.task
                self.assertEqual(retry.error, 'Fixture unavailable')
        finally:
            await rooms.close()
            await other.close()

    async def test_stopped_or_timed_out_extraction_keeps_its_slot_until_worker_finishes(self):
        errors = []
        loop = asyncio.get_running_loop()
        self.addCleanup(loop.set_exception_handler, loop.get_exception_handler())
        loop.set_exception_handler(lambda _, context: errors.append(context))
        for cancel in ('stop', 'timeout'):
            with self.subTest(cancel=cancel):
                rooms = Rooms()
                started = asyncio.Event()
                release = threading.Event()
                attempts = 0

                def blocked(*args):
                    nonlocal attempts
                    attempts += 1
                    loop.call_soon_threadsafe(started.set)
                    if attempts == 1:
                        release.wait(5)
                    raise ValueError('Fixture unavailable')

                try:
                    with patch('syncnest.stream.MAX_EXTRACTIONS', 1), \
                         patch('syncnest.stream.EXTRACTION_TIMEOUT_SECONDS', .05), \
                         patch('syncnest.stream.extract_video', side_effect=blocked) as extract:
                        first = rooms.prepare(1, URL)
                        await asyncio.wait_for(started.wait(), 1)
                        if cancel == 'stop':
                            await first.stop()
                        else:
                            await asyncio.sleep(.1)
                            self.assertEqual(first.status, 'error')
                            self.assertIn('timed out', first.error)
                        second = rooms.prepare(1, URL)
                        await second.task
                        self.assertIn('busy', second.error)
                        self.assertEqual(extract.call_count, 1)
                        release.set()
                        async with asyncio.timeout(2):
                            while True:
                                retry = rooms.prepare(1, URL)
                                await retry.task
                                if retry.error == 'Fixture unavailable':
                                    break
                                await asyncio.sleep(.01)
                        self.assertEqual(extract.call_count, 2)
                        self.assertEqual(errors, [])
                finally:
                    release.set()
                    await rooms.close()


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class HTTPRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_transient_500_recovers_and_persistent_errors_are_bounded(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'fixture.mp4'
            generation = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=60',
                '-f', 'lavfi', '-i', 'sine=sample_rate=48000',
                '-t', '3', '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac',
                '-movflags', '+faststart', str(source),
                stderr=asyncio.subprocess.PIPE)
            _, errors = await generation.communicate()
            self.assertEqual(generation.returncode, 0, errors.decode())
            counts = {}

            async def serve(request):
                kind = request.match_info['kind']
                counts[kind] = counts.get(kind, 0) + 1
                if kind in {'once', 'drop'} and counts[kind] > 1:
                    return web.FileResponse(source)
                if kind == 'drop':
                    data = source.read_bytes()
                    response = web.StreamResponse(headers={
                        'Content-Type': 'video/mp4', 'Content-Length': str(len(data)), 'Accept-Ranges': 'bytes'})
                    await response.prepare(request)
                    await response.write(data[:len(data) // 2])
                    request.transport.close()
                    return response
                return web.Response(status=403 if kind == 'forbidden' else 500)

            app = web.Application()
            app.router.add_get('/{kind}.mp4', serve)
            async with TestServer(app) as server:
                for kind, successful in [('once', True), ('drop', True), ('always', False), ('forbidden', False)]:
                    output = root / kind
                    output.mkdir()
                    with patch('syncnest.stream.is_archive_url', return_value=True):
                        args = ffmpeg_args({'url': str(server.make_url(f'/{kind}.mp4'))}, output, start_time=1)
                    process = await asyncio.create_subprocess_exec(
                        *args,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                    try:
                        _, errors = await asyncio.wait_for(process.communicate(), 10)
                    finally:
                        if process.returncode is None:
                            process.kill()
                            await process.wait()
                    self.assertEqual(process.returncode == 0, successful, errors.decode())
                    if successful:
                        self.assertGreaterEqual(counts[kind], 2)
                        decode = await asyncio.create_subprocess_exec(
                            'ffmpeg', '-v', 'error', '-i', str(output / '000000.ts'), '-f', 'null', '-',
                            stderr=asyncio.subprocess.PIPE)
                        _, errors = await decode.communicate()
                        self.assertEqual(decode.returncode, 0, errors.decode())
                        probe = await asyncio.create_subprocess_exec(
                            'ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name,avg_frame_rate', '-of', 'csv=p=0',
                            str(output / '000000.ts'), stdout=asyncio.subprocess.PIPE)
                        tracks, _ = await probe.communicate()
                        self.assertIn(b'h264', tracks)
                        self.assertIn(b'aac', tracks)
                        self.assertIn(b'30/1', tracks)
                    elif kind == 'forbidden':
                        self.assertEqual(counts[kind], 1)
                    else:
                        self.assertLessEqual(counts[kind], 4)


    async def test_real_quality_handoff_has_playable_buffer_and_reaps_old_encoder(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'fixture.mp4'
            generation = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
                '-t', '15', '-c:v', 'libx264', '-preset', 'ultrafast', str(source),
                stderr=asyncio.subprocess.PIPE)
            _, errors = await generation.communicate()
            self.assertEqual(generation.returncode, 0, errors.decode())
            rooms = Rooms()
            try:
                with patch('syncnest.stream.extract_video', return_value={
                    'url': str(source), 'title': 'Fixture', 'vcodec': 'h264', 'acodec': 'none',
                }) as extract:
                    original = rooms.prepare(42, URL)
                    await asyncio.wait_for(original.ready.wait(), 10)
                    old_directory = original.directory
                    replacement = await asyncio.wait_for(rooms.change_resolution(42, '1080'), 10)
                self.assertEqual(extract.call_count, 1)
                self.assertFalse(original.active)
                self.assertFalse(old_directory.exists())
                self.assertIs(rooms.channels[42], replacement)
                self.assertGreaterEqual((replacement.directory / 'index.m3u8').read_text().count('#EXTINF:'), 3)
                decode = await asyncio.create_subprocess_exec(
                    'ffmpeg', '-v', 'error', '-i', str(replacement.directory / '000000.ts'), '-f', 'null', '-',
                    stderr=asyncio.subprocess.PIPE)
                _, errors = await decode.communicate()
                self.assertEqual(decode.returncode, 0, errors.decode())
                # A seek past EOF can make FFmpeg exit successfully with no
                # frames. That must not replace a playable current stream.
                with patch.object(replacement, 'playback_position', return_value=20), \
                     patch('syncnest.stream.extract_video', return_value={
                         'url': str(source), 'title': 'Fixture', 'vcodec': 'h264', 'acodec': 'none'}):
                    with self.assertRaisesRegex(ValueError, 'current video is still playing'):
                        await asyncio.wait_for(rooms.change_resolution(42, '720'), 10)
                self.assertIs(rooms.channels[42], replacement)
                self.assertTrue(replacement.active)
            finally:
                await rooms.close()
            self.assertFalse(replacement.active)


    async def test_kick_timestamp_jump_is_removed_without_losing_audio_or_video(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'jump.ts'
            generation = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=160x90:rate=30:duration=4',
                '-f', 'lavfi', '-i', 'sine=sample_rate=48000:duration=4',
                '-vf', "setpts='PTS+4/TB*gte(T,2)'", '-af', "asetpts='PTS+4/TB*gte(T,2)'",
                '-fps_mode', 'passthrough', '-c:v', 'libx264', '-preset', 'ultrafast',
                '-c:a', 'aac', '-f', 'mpegts', str(source), stderr=asyncio.subprocess.PIPE)
            _, errors = await generation.communicate()
            self.assertEqual(generation.returncode, 0, errors.decode())
            durations = {}
            for corrected in (False, True):
                output = root / str(corrected)
                output.mkdir()
                info = {'url': str(source), 'vcodec': 'h264', 'acodec': 'aac'}
                if corrected:
                    info['extractor_key'] = 'KickClip'
                args = ffmpeg_args(info, output)
                args.remove('-re')
                for option in ('-readrate_initial_burst', '-readrate_catchup'):
                    if option in args:
                        index = args.index(option)
                        del args[index:index + 2]
                encode = await asyncio.create_subprocess_exec(
                    *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, errors = await encode.communicate()
                self.assertEqual(encode.returncode, 0, errors.decode())
                playlist = (output / 'index.m3u8').read_text()
                durations[corrected] = sum(float(line.split(':')[1].split(',')[0])
                                          for line in playlist.splitlines() if line.startswith('#EXTINF:'))
                if corrected:
                    decode = await asyncio.create_subprocess_exec(
                        'ffmpeg', '-v', 'error', '-i', str(output / 'index.m3u8'), '-f', 'null', '-',
                        stderr=asyncio.subprocess.PIPE)
                    _, errors = await decode.communicate()
                    self.assertEqual(decode.returncode, 0, errors.decode())
            self.assertAlmostEqual(durations[False], 8, delta=0.15)
            self.assertAlmostEqual(durations[True], 4, delta=0.15)
