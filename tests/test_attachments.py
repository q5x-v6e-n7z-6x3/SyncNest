"""Attachment validation and real HTTP playback without Discord credentials."""

import asyncio
import json
import shutil
import tempfile
import unittest
from email.message import Message
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from aiohttp import web
from aiohttp.test_utils import TestServer

from syncnest.stream import OdyseeIE, Rooms, extract_video, ffmpeg_args, normalize_video_url, select_video


URL = 'https://cdn.discordapp.com/ephemeral-attachments/100/200/video.mp4?ex=ffffffff&is=0&hm=fixture'


class AttachmentTests(unittest.TestCase):
    def test_odysee_original_formats_keep_safe_decoding_after_quality_selection(self):
        for extension, media_type, demuxer in (('mp4', 'video/mp4', 'mov'),
                                               ('webm', 'video/webm', 'matroska')):
            claim = {'claim_id': 'a' * 40, 'value': {
                'stream_type': 'video', 'title': 'Original video',
                'source': {'name': f'video.{extension}'}, 'video': {'width': 320, 'height': 180}}}
            url = 'https://player.odycdn.com/api/v4/streams/free/fixture/claim/blob'
            # yt-dlp responses expose email.message.Message headers.
            headers = Message()
            headers['Content-Type'] = media_type
            response = SimpleNamespace(url=url, headers=headers)
            with self.subTest(extension=extension), \
                 patch.object(OdyseeIE, '_resolve_url', return_value=claim), \
                 patch.object(OdyseeIE, '_call_api_proxy', return_value={'streaming_url': url}), \
                 patch.object(OdyseeIE, '_request_webpage', return_value=response):
                info = extract_video('https://odysee.com/fixture:a')
                self.assertEqual(info['extractor_key'], 'LBRY')
                self.assertEqual(info['format_id'], 'original')
                self.assertIn('/api/v3/', info['url'])
                for selected in (info, select_video(info, '1080')):
                    args = ffmpeg_args(selected, Path('/tmp/hls'))
                    self.assertEqual(args[args.index('-f') + 1], demuxer)
                    self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'http,https,tcp,tls,httpproxy')

    def test_odysee_original_rejects_unknown_containers_without_changing_hls(self):
        for extension in ('m3u8', 'mpd', 'unknown', None):
            with self.subTest(extension=extension), self.assertRaisesRegex(ValueError, 'Odysee'):
                ffmpeg_args({'url': 'https://player.odycdn.com/original', 'extractor_key': 'LBRY',
                             'format_id': 'original', 'ext': extension}, Path('/tmp/hls'))
        args = ffmpeg_args({'url': 'https://player.odycdn.com/index.m3u8', 'extractor_key': 'LBRY',
                            'format_id': 'hls-720', 'ext': 'mp4', 'protocol': 'm3u8_native',
                            'is_live': True}, Path('/tmp/hls'))
        # Platform manifests keep autodetection but only reach protocols the egress proxy vets.
        self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'http,https,tcp,tls,httpproxy,crypto,data')
        self.assertGreater(args.index('-f'), args.index('-i'))
        self.assertEqual(args[args.index('-f') + 1], 'hls')

    def test_archive_manifests_and_unknown_containers_are_rejected(self):
        for suffix in ('m3u8', 'mpd', 'unknown'):
            with self.subTest(suffix=suffix), self.assertRaisesRegex(ValueError, 'Archive'):
                ffmpeg_args({'url': f'https://archive.org/download/fixture/video.{suffix}'}, Path('/tmp/hls'))

    def test_archive_files_use_fixed_containers_with_restricted_protocols(self):
        for suffix, demuxer in (('mp4', 'mov'), ('mov', 'mov'), ('m4v', 'mov'),
                                ('mkv', 'matroska'), ('webm', 'matroska'), ('avi', 'avi'),
                                ('ogv', 'ogg'), ('mpg', 'mpeg'), ('mpeg', 'mpeg'), ('ts', 'mpegts')):
            url = f'https://archive.org/download/fixture/video.{suffix}'
            with self.subTest(suffix=suffix):
                args = ffmpeg_args(extract_video(url), Path('/tmp/hls'))
                self.assertIn('-protocol_whitelist', args)
                protocols = set(args[args.index('-protocol_whitelist') + 1].split(','))
                self.assertFalse(protocols & {'file', 'concat', 'data'})
                self.assertEqual(args[args.index('-f') + 1], demuxer)
                self.assertLess(args.index('-f'), args.index('-i'))

    def test_attachment_urls_keep_signatures_and_select_fixed_media_containers(self):
        for route in ('attachments', 'ephemeral-attachments'):
            for suffix, demuxer in (('mp4', 'mov'), ('mov', 'mov'), ('m4v', 'mov'),
                                    ('mkv', 'matroska'), ('webm', 'matroska'), ('avi', 'avi')):
                supplied = f'https://cdn.discordapp.com/{route}/100/200/My%20video.{suffix.upper()}?ex=ffffffff&is=0&hm=fixture'
                with self.subTest(route=route, suffix=suffix), \
                     patch('syncnest.stream.yt_dlp.YoutubeDL', side_effect=AssertionError('Attachments need no extractor')):
                    try:
                        normalized = normalize_video_url(supplied + '#share')
                    except ValueError as error:
                        self.fail(str(error))
                    self.assertEqual(normalized, supplied)
                    info = extract_video(normalized)
                    self.assertEqual(info['url'], supplied)
                    self.assertEqual(info['_input_format'], demuxer)

    def test_rejects_local_paths_spoofed_hosts_playlists_and_encoded_path_traversal(self):
        urls = [
            'file:///etc/passwd', '/tmp/video.mp4', 'https://127.0.0.1/video.mp4',
            URL.replace('https:', 'http:'), URL.replace('.com/', '.com:80/'),
            URL.replace('.com/', '.com:8443/'), URL.replace('cdn.', 'user:pass@cdn.'),
            URL.replace('cdn.discordapp.com', 'cdn.discordapp.com.evil.test'),
            URL.replace('/100/200/', '/bad/200/'), URL.replace('/100/200/', '/100/'),
            URL.replace('/ephemeral-attachments/', '/other/'),
            URL.replace('video.mp4', 'video.m3u8'), URL.replace('video.mp4', 'video.mp3'),
            URL.replace('video.mp4', '..%2fvideo.mp4'), URL.replace('video.mp4', '%5cvideo.mp4'),
            URL.replace('video.mp4', 'video%00.mp4'),
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_video_url(url)

    def test_cached_quality_selection_preserves_safe_attachment_decoding_and_seek(self):
        try:
            info = select_video(extract_video(URL), '1080')
        except ValueError as error:
            self.fail(str(error))
        args = ffmpeg_args(info, Path('/tmp/hls'), '1080', 5)
        self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'http,https,tcp,tls,httpproxy')
        self.assertEqual(args[args.index('-f') + 1], 'mov')
        self.assertLess(args.index('-protocol_whitelist'), args.index('-i'))
        self.assertEqual(args[args.index('-ss') + 1], '5.000')


@unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
class AttachmentPlaybackTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_and_odysee_original_containers_produce_video_and_audio_over_http(self):
        async def run(*args):
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                output, errors = await asyncio.wait_for(process.communicate(), 15)
                self.assertEqual(process.returncode, 0, errors.decode())
                return output
            finally:
                if process.returncode is None:
                    process.kill()
                    await process.wait()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            app = web.Application()

            async def serve(request):
                return web.FileResponse(root / request.match_info['name'])

            app.router.add_get('/{name}', serve)
            async with TestServer(app) as server:
                for extension in ('mp4', 'mov', 'm4v', 'mkv', 'webm', 'avi'):
                    with self.subTest(extension=extension):
                        source = root / f'video.{extension}'
                        codecs = (['-c:v', 'libvpx-vp9', '-deadline', 'realtime', '-c:a', 'libopus']
                                  if extension == 'webm' else
                                  ['-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac'])
                        await run('ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                                  'testsrc2=size=320x180:rate=30', '-f', 'lavfi', '-i',
                                  'sine=sample_rate=48000', '-t', '2', *codecs,
                                  *(['-f', 'mp4'] if extension == 'm4v' else []), str(source))
                        uploaded = extract_video(URL.replace('video.mp4', source.name))
                        uploaded['url'] = str(server.make_url('/' + source.name))
                        sources = [uploaded]
                        if extension in {'mp4', 'webm'}:
                            sources.append({'url': uploaded['url'], 'extractor_key': 'LBRY',
                                            'format_id': 'original', 'ext': extension})
                        for index, info in enumerate(sources):
                            output = root / f'{extension}-{index}'
                            output.mkdir()
                            await run(*ffmpeg_args(info, output, '144'))
                            result = json.loads(await run(
                                'ffprobe', '-v', 'error', '-show_entries',
                                'stream=codec_type,codec_name,width,height', '-of', 'json',
                                str(output / 'index.m3u8')))
                            video = next(s for s in result['streams'] if s['codec_type'] == 'video')
                            self.assertEqual((video['codec_name'], video['width'], video['height']),
                                             ('h264', 256, 144))
                            self.assertTrue(any(s['codec_name'] == 'aac' for s in result['streams']))

    async def test_archive_and_odysee_originals_cannot_fetch_nested_playlists_or_private_hosts(self):
        private_requests, archive_requests = [], []

        async def private(request):
            private_requests.append(request.path)
            return web.Response(body=b'private fixture')

        private_app = web.Application()
        private_app.router.add_get('/private.ts', private)
        async with TestServer(private_app) as private_server:
            async def archive(request):
                archive_requests.append(request.path)
                if request.path == '/video.mp4':
                    return web.Response(text='ffconcat version 1.0\nfile nested.m3u8\n')
                return web.Response(text='#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n'
                                         + str(private_server.make_url('/private.ts')) + '\n#EXT-X-ENDLIST\n')

            archive_app = web.Application()
            archive_app.router.add_get('/{path:.*}', archive)
            async with TestServer(archive_app) as archive_server:
                for info in (extract_video('https://archive.org/download/fixture/video.mp4'),
                             {'url': 'https://archive.org/download/fixture/video.mp4',
                              'extractor_key': 'ArchiveOrg', 'vcodec': 'h264', 'acodec': 'aac'},
                             {'url': 'https://player.odycdn.com/api/v3/streams/free/fixture/claim/blob',
                              'extractor_key': 'LBRY', 'format_id': 'original', 'ext': 'mp4'}):
                    with self.subTest(extractor=info.get('extractor_key')):
                        prefix = '' if info.get('extractor_key') == 'LBRY' else 'async:'
                        info['_input_url'] = prefix + str(archive_server.make_url('/video.mp4'))
                        with tempfile.TemporaryDirectory() as directory:
                            process = await asyncio.create_subprocess_exec(
                                *ffmpeg_args(info, Path(directory)), stderr=asyncio.subprocess.PIPE)
                            await asyncio.wait_for(process.communicate(), 10)
                            self.assertNotEqual(process.returncode, 0)
                self.assertIn('/video.mp4', archive_requests)
                self.assertEqual(private_requests, [])
                self.assertNotIn('/nested.m3u8', archive_requests)

    async def test_http_attachment_keeps_audio_quality_changes_position_and_cleanup(self):
        rooms = Rooms()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'video.mp4'
            generation = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '12',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '30', '-c:a', 'aac',
                '-movflags', '+faststart', str(source), stderr=asyncio.subprocess.PIPE)
            _, errors = await generation.communicate()
            self.assertEqual(generation.returncode, 0, errors.decode())
            app = web.Application()
            async def serve_video(request):
                return web.FileResponse(source)

            app.router.add_get('/video.mp4', serve_video)
            async with TestServer(app) as server:
                def local_source(url, resolution):
                    info = extract_video(url, resolution)
                    info['url'] = str(server.make_url('/video.mp4'))
                    return info

                with patch('syncnest.stream.extract_video', side_effect=local_source):
                    try:
                        room = rooms.prepare(42, URL, '144')
                        await asyncio.wait_for(room.ready.wait(), 15)
                        self.assertEqual(room.status, 'playing', room.error)
                        previous_directory = room.directory
                        room.source_extracted_at = 0  # Quality switches must also work after cache expiry.
                        with patch.object(room, 'playback_position', return_value=2):
                            replacement = await rooms.change_resolution(42, '360')
                        self.assertEqual(replacement.start_time, 2)
                        self.assertFalse(previous_directory.exists())
                        self.assertEqual(replacement.snapshot()['buffer_seconds'], 3)
                        probe = await asyncio.create_subprocess_exec(
                            'ffprobe', '-v', 'error', '-show_streams', '-of', 'json',
                            str(replacement.directory / 'index.m3u8'), stdout=asyncio.subprocess.PIPE)
                        output, _ = await probe.communicate()
                        self.assertEqual(probe.returncode, 0)
                        streams = json.loads(output)['streams']
                        self.assertEqual(next(s['height'] for s in streams if s['codec_type'] == 'video'), 180)
                        self.assertTrue(any(s['codec_type'] == 'audio' for s in streams))
                        current_directory = replacement.directory
                        await replacement.stop()
                        self.assertFalse(current_directory.exists())
                        self.assertIsNone(replacement.source_info)
                    finally:
                        await rooms.close()

    async def test_disguised_playlists_cannot_make_secondary_requests(self):
        requests = []

        async def serve(request):
            requests.append(request.path)
            if request.path == '/video.mp4':
                return web.Response(text='#EXTM3U\n#EXT-X-TARGETDURATION:1\n#EXTINF:1,\n/private.ts\n#EXT-X-ENDLIST\n')
            return web.Response(body=b'private fixture')

        app = web.Application()
        app.router.add_get('/{path:.*}', serve)
        async with TestServer(app) as server:
            try:
                info = extract_video(URL)
            except ValueError as error:
                self.fail(str(error))
            info['url'] = str(server.make_url('/video.mp4'))
            with tempfile.TemporaryDirectory() as directory:
                process = await asyncio.create_subprocess_exec(
                    *ffmpeg_args(info, Path(directory)), stderr=asyncio.subprocess.PIPE)
                await asyncio.wait_for(process.communicate(), 10)
                self.assertNotEqual(process.returncode, 0)
        self.assertIn('/video.mp4', requests)
        self.assertNotIn('/private.ts', requests)


if __name__ == '__main__':
    unittest.main()
