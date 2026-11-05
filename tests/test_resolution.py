"""Exercise real format selection and encoded dimensions without platform requests."""

import asyncio
import copy
import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_dlp import YoutubeDL

from syncnest.stream import RESOLUTIONS, Rooms, extract_video, ffmpeg_args

URL = 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'
AUDIO = {'format_id': 'audio', 'url': 'https://example.test/audio',
         'vcodec': 'none', 'acodec': 'mp4a.40.2', 'ext': 'm4a', 'abr': 128}


def video(height, *, codec='vp9', combined=False):
    return {'format_id': f'{height}-{codec}', 'url': f'https://example.test/{height}',
            'height': height, 'width': height * 16 // 9, 'fps': 30,
            'vcodec': codec, 'acodec': 'mp4a.40.2' if combined else 'none',
            'ext': 'mp4' if codec == 'avc1.640028' else 'webm'}


def select(formats, resolution='best'):
    def extract(ydl, url, download):
        return ydl.process_ie_result({'id': 'fixture', 'title': 'Fixture',
                                     'formats': copy.deepcopy(formats)}, download=False)
    with patch.object(YoutubeDL, 'extract_info', autospec=True, side_effect=extract):
        return extract_video(URL, resolution)


class ResolutionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.rooms = Rooms()

    async def asyncTearDown(self):
        await self.rooms.close()

    async def loading_room(self, resolution='720', start_time=0):
        async def loading(*args):
            await asyncio.Future()
        extraction = patch('syncnest.stream.asyncio.to_thread', side_effect=loading)
        extraction.start()
        self.addCleanup(extraction.stop)
        room = self.rooms.prepare(42, URL, resolution, start_time)
        await asyncio.sleep(0)
        return room

    def test_highest_resolution_beats_preferred_avc_codec(self):
        info = select([video(1080, codec='avc1.640028'), video(2160), AUDIO])
        self.assertEqual(info['requested_formats'][0]['height'], 2160)

    def test_same_height_prefers_sdr_h264_and_stereo_aac_copy(self):
        # Output is 8-bit SDR: HDR would wash out, and AV1 or 5.1 cost more to decode.
        hdr = video(1080, codec='av01.0.09M.10') | {'dynamic_range': 'HDR10'}
        surround = AUDIO | {'format_id': 'surround', 'audio_channels': 6, 'abr': 384}
        stereo = AUDIO | {'format_id': 'stereo', 'audio_channels': 2, 'asr': 44100}
        info = select([hdr, video(1080, codec='av01.0.09M.08'), video(1080, codec='avc1.640028'),
                       surround, stereo], '1080')
        self.assertEqual([source['format_id'] for source in info['requested_formats']],
                         ['1080-avc1.640028', 'stereo'])
        args = ffmpeg_args(info, Path('/tmp/hls'), '1080')
        self.assertEqual(args[args.index('-c:a') + 1], 'copy')

    def test_highest_has_no_4k_ceiling(self):
        info = select([video(2160), video(4320), AUDIO])
        self.assertEqual(info['requested_formats'][0]['height'], 4320)

    def test_high_resolution_combined_stream_beats_lower_split_stream(self):
        info = select([video(720), video(2160, combined=True), AUDIO])
        self.assertEqual(info['height'], 2160)
        self.assertNotIn('requested_formats', info)

    def test_each_cap_selects_highest_source_within_limit(self):
        formats = [video(height) for height in [144, 280, 360, 480, 720, 1080, 1440, 2160, 4320]] + [AUDIO]
        for resolution, height in RESOLUTIONS.items():
            with self.subTest(resolution=resolution):
                info = select(formats, resolution)
                self.assertEqual(info['requested_formats'][0]['height'], height or 4320)
        info = select([video(360), video(720), video(2160), AUDIO], '1080')
        self.assertEqual(info['requested_formats'][0]['height'], 720)

    def test_cap_falls_back_to_available_source_for_downscaling(self):
        info = select([video(2160), AUDIO], '720')
        self.assertEqual(info['requested_formats'][0]['height'], 2160)
        args = ffmpeg_args(info, Path('/tmp/hls'), '720')
        self.assertIn('min(720,ih)', args[args.index('-vf') + 1])

    def test_caps_accept_silent_and_unknown_dimension_sources(self):
        for formats in [[video(1080)], [{'format_id': 'unknown', 'ext': 'mp4',
                                       'url': 'https://example.test/unknown'}]]:
            with self.subTest(formats=formats):
                info = select(formats, '720')
                self.assertIn('0:a:0?', ffmpeg_args(info, Path('/tmp/hls'), '720'))

    async def test_invalid_resolution_is_rejected_before_network_or_room_creation(self):
        rooms = Rooms()
        with patch('syncnest.stream.yt_dlp.YoutubeDL') as downloader:
            for value in ['garbage', '99999', '720; echo unsafe']:
                with self.subTest(value=value):
                    with self.assertRaises(ValueError):
                        extract_video(URL, value)
                    with self.assertRaises(ValueError):
                        ffmpeg_args({'url': URL}, Path('/tmp/hls'), value)
                    with self.assertRaises(ValueError):
                        rooms.prepare(1, URL, value)
            downloader.assert_not_called()
        self.assertEqual(rooms.channels, {})

    async def test_independent_rooms_pass_resolution_to_extraction_and_encoder(self):
        with patch('syncnest.stream.extract_video', return_value={'url': 'https://example.test/video'}) as extract, \
             patch('syncnest.stream.ffmpeg_args', return_value=['ffmpeg']) as encode, \
             patch('syncnest.stream.asyncio.create_subprocess_exec', side_effect=OSError('Fixture failure')):
            rooms = Rooms()
            best, capped = rooms.prepare(1, URL, 'best'), rooms.prepare(2, URL, '1080')
            await asyncio.gather(best.task, capped.task)
            self.assertEqual((best.resolution, capped.resolution), ('best', '1080'))
            self.assertEqual({call.args[1] for call in extract.call_args_list}, {'best', '1080'})
            self.assertEqual({call.args[2] for call in encode.call_args_list}, {'best', '1080'})

    async def test_default_is_720p(self):
        room = await self.loading_room()
        self.assertEqual(room.resolution, '720')

    async def test_resolution_switch_replaces_stream_at_shared_position(self):
        room = await self.loading_room(start_time=20)
        other = self.rooms.prepare(99, URL)
        with tempfile.TemporaryDirectory() as directory:
            room.directory = Path(directory)
            (room.directory / 'index.m3u8').write_text(
                '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:100\n' + '#EXTINF:1.000,\nsegment.ts\n' * 16)
            replacement = await self.rooms.change_resolution(42, '1080')
        self.assertEqual(replacement.resolution, '1080')
        self.assertEqual(replacement.url, URL)
        self.assertEqual(replacement.start_time, 133)
        self.assertNotEqual(replacement.id, room.id)
        self.assertFalse(room.active)
        self.assertIs(self.rooms.channels[99], other)
        self.assertTrue(other.active)

    async def test_live_resolution_switch_does_not_seek(self):
        room = await self.loading_room(start_time=100)
        room.is_live = True
        replacement = await self.rooms.change_resolution(42, '1080')
        self.assertEqual(replacement.start_time, 0)

    async def test_same_resolution_keeps_existing_stream(self):
        room = await self.loading_room()
        replacement = await self.rooms.change_resolution(42, '720')
        self.assertIs(replacement, room)
        self.assertTrue(room.active)

    async def test_resolution_switch_needs_active_video(self):
        with self.assertRaisesRegex(ValueError, 'Nothing is playing'):
            await self.rooms.change_resolution(42, '1080')
        room = await self.loading_room()
        await room.stop()
        with self.assertRaisesRegex(ValueError, 'Nothing is playing'):
            await self.rooms.change_resolution(42, '1080')

    async def test_stop_during_resolution_switch_prevents_restart(self):
        room = await self.loading_room()
        entered, release = asyncio.Event(), asyncio.Event()
        original = room.stop

        async def stop():
            await original()
            entered.set()
            await release.wait()

        with patch.object(room, 'stop', side_effect=stop):
            switch = asyncio.create_task(self.rooms.change_resolution(42, '1080'))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                await original()  # Simulate /stop while cleanup is pending.
                release.set()
                with self.assertRaisesRegex(ValueError, 'stopped or changed'):
                    await switch
            finally:
                release.set()
                await asyncio.gather(switch, return_exceptions=True)
        self.assertIs(self.rooms.channels[42], room)
        self.assertFalse(room.active)

    async def test_concurrent_resolution_switches_do_not_replace_each_other(self):
        room = await self.loading_room()
        entered, release = asyncio.Event(), asyncio.Event()
        original = room.stop

        async def stop():
            await original()
            entered.set()
            await release.wait()

        with patch.object(room, 'stop', side_effect=stop):
            first = asyncio.create_task(self.rooms.change_resolution(42, '1080'))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                with self.assertRaisesRegex(ValueError, 'already in progress'):
                    await self.rooms.change_resolution(42, '2160')
                release.set()
                replacement = await first
            finally:
                release.set()
                await asyncio.gather(first, return_exceptions=True)
        self.assertEqual(replacement.resolution, '1080')

    def test_seek_applies_to_both_video_and_audio_inputs(self):
        args = ffmpeg_args({'requested_formats': [video(1080), AUDIO]}, Path('/tmp/hls'), '1080', 123.5)
        self.assertEqual(args.count('-ss'), 2)
        inputs = []
        for index, argument in enumerate(args):
            if argument == '-ss':
                self.assertEqual(args[index + 1:index + 3], ['123.500', '-i'])
                inputs.append(args[index + 3])
        self.assertEqual(inputs, [video(1080)['url'], AUDIO['url']])

    @unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
    async def test_real_resolution_restart_seeks_into_recorded_source(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.mp4'
            generation = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
                '-t', '4', '-c:v', 'libx264', '-preset', 'ultrafast', str(source),
                stderr=asyncio.subprocess.PIPE)
            _, errors = await generation.communicate()
            self.assertEqual(generation.returncode, 0, errors.decode())
            output = root / 'hls'
            output.mkdir()
            conversion = await asyncio.create_subprocess_exec(
                *ffmpeg_args({'url': str(source), 'vcodec': 'h264', 'acodec': 'none'}, output, '1080', 3),
                stderr=asyncio.subprocess.PIPE)
            _, errors = await conversion.communicate()
            self.assertEqual(conversion.returncode, 0, errors.decode())
            playlist = (output / 'index.m3u8').read_text()
            durations = [float(line.split(':')[1].split(',')[0])
                         for line in playlist.splitlines() if line.startswith('#EXTINF:')]
            self.assertAlmostEqual(sum(durations), 1, delta=0.1)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    async def test_real_hls_preserves_4k_caps_and_never_upscales(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for original, modes in [((3840, 2160), [('best', (3840, 2160)), ('1080', (1920, 1080)),
                                                  ('720', (1280, 720)), ('360', (640, 360)),
                                                  ('280', (498, 280)), ('144', (256, 144))]),
                                    ((320, 180), [('1080', (320, 180))]),
                                    ((360, 640), [('480', (270, 480))])]:
                source = root / f'{original[0]}x{original[1]}.mp4'
                generation = await asyncio.create_subprocess_exec(
                    'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                    f'color=c=blue:size={original[0]}x{original[1]}:rate=30',
                    '-frames:v', '3', '-c:v', 'libx264', '-preset', 'ultrafast', str(source),
                    stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                _, errors = await generation.communicate()
                self.assertEqual(generation.returncode, 0, errors.decode())
                for resolution, dimensions in modes:
                    with self.subTest(original=original, resolution=resolution):
                        output = root / f'{source.stem}-{resolution}'
                        output.mkdir()
                        conversion = await asyncio.create_subprocess_exec(
                            *ffmpeg_args({'url': str(source), 'vcodec': 'h264', 'acodec': 'none'}, output, resolution),
                            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
                        _, errors = await conversion.communicate()
                        self.assertEqual(conversion.returncode, 0, errors.decode())
                        probe = await asyncio.create_subprocess_exec(
                            'ffprobe', '-v', 'error', '-f', 'mpegts',
                            '-show_entries', 'stream=codec_name,width,height',
                            '-of', 'json', str(output / '000000.ts'), stdout=asyncio.subprocess.PIPE)
                        data, _ = await probe.communicate()
                        self.assertEqual(probe.returncode, 0)
                        stream = json.loads(data)['streams'][0]
                        self.assertEqual((stream['width'], stream['height']), dimensions)
                        self.assertEqual(stream['codec_name'], 'h264')
