"""Run with: PYTHONPATH=src python -m unittest -v tests.test_activity"""

import asyncio
import base64
import copy
import json
import re
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from nacl.signing import SigningKey
from yt_dlp import YoutubeDL

from syncnest import activity
from syncnest.playback import PlaybackQueues
from syncnest.stream import Rooms, extract_video, ffmpeg_args, normalize_video_url
from tests.test_queue import FakeSession

URL = 'https://www.youtube.com/watch?v=aqz-KE-bpKQ'
NEW_PLATFORM_URLS = [
    'https://odysee.com/@example:a/video:b',
    'https://www.bitchute.com/video/UGlrF9o9b-Q',
    'https://rumble.com/vdmum1-moose-the-dog-helps-girls-dig-a-snow-fort.html',
]
KICK_URLS = [
    'https://kick.com/example-channel',
    'https://kick.com/example-channel/videos/5c697a87-afce-4256-b01f-3c8fe71ef5cb',
    'https://kick.com/example-channel/clips/clip_01GYXVB5Y8PWAPWCWMSBCFB05X',
]
PLATFORM_URLS = [
    URL, 'https://vimeo.com/76979871', 'https://www.dailymotion.com/video/x84sh87',
    'https://www.twitch.tv/videos/123456789', 'https://clips.twitch.tv/FancyClip',
    'https://www.twitch.tv/example_channel', 'https://archive.org/details/Cops1922',
    'https://archive.org/download/Cops1922/Cops-v2.mp4',
    *NEW_PLATFORM_URLS,
    *KICK_URLS,
]


async def until(predicate):
    async with asyncio.timeout(5):
        while not predicate():
            await asyncio.sleep(0.01)


class StreamTests(unittest.IsolatedAsyncioTestCase):
    def test_short_probe_requires_known_video_and_audio_codecs(self):
        known = {'url': 'https://example.test/video', 'vcodec': 'avc1.640028',
                 'acodec': 'mp4a.40.2'}
        for source, fast in [(known, True), (known | {'acodec': 'none'}, True),
                             (known | {'vcodec': 'none'}, True),
                             (known | {'acodec': None}, False),
                             (known | {'vcodec': ''}, False), ({'url': known['url']}, False)]:
            with self.subTest(source=source):
                args = ffmpeg_args(source, Path('/tmp/hls'))
                self.assertEqual('-analyzeduration' in args, fast)
        # Probe each input independently: an unknown audio source keeps full probing.
        args = ffmpeg_args({'requested_formats': [known, {'url': 'https://example.test/audio'}]},
                           Path('/tmp/hls'))
        self.assertEqual(args.count('-analyzeduration'), 1)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    async def test_paced_stream_starts_faster_and_retains_video_and_audio(self):
        from benchmarks.benchmark_speed import paced_stream_startup
        result = await paced_stream_startup()
        for sample in result.values():
            self.assertEqual(sample['codecs'], ['aac', 'h264'])
        self.assertLess(result['known_codecs']['first_segment_ms'],
                        result['default_probe']['first_segment_ms'] * 0.75)

    def test_compatible_audio_is_copied_and_other_audio_is_encoded(self):
        compatible = {'url': 'https://example.test/audio', 'acodec': 'mp4a.40.2',
                      'audio_channels': 2, 'asr': 44100, 'abr': 128}
        for separate in (False, True):
            for changes, expected in [({}, 'copy'), ({'acodec': 'opus'}, 'aac'),
                                      ({'audio_channels': 6}, 'aac'), ({'asr': 96000}, 'aac'),
                                      ({'abr': 256}, 'aac'), ({'audio_channels': None}, 'aac')]:
                with self.subTest(separate=separate, changes=changes):
                    source = compatible | changes
                    info = {'requested_formats': [{'url': 'https://example.test/video'}, source]} if separate else source
                    args = ffmpeg_args(info, Path('/tmp/hls'))
                    self.assertEqual(args[args.index('-c:a') + 1], expected)

    async def test_registration_preserves_activity_entry_point(self):
        from syncnest.bot import bot

        application = SimpleNamespace(flags=SimpleNamespace(embedded=True))
        with patch.object(bot, 'application_info', AsyncMock(return_value=application)), \
             patch.object(bot.http, 'upsert_global_command', AsyncMock()) as register, \
             patch.object(bot.http, 'bulk_upsert_global_commands', AsyncMock()) as bulk:
            await bot.setup_hook()
            self.assertEqual({call.args[1]['name'] for call in register.call_args_list},
                             {'play', 'resolution', 'stop', 'help', 'queue', 'resume', 'skip'})
            play_command = next(call.args[1] for call in register.call_args_list if call.args[1]['name'] == 'play')
            self.assertEqual(play_command['options'][0]['name'], 'url')
            self.assertFalse(play_command['options'][0]['required'])
            attachment = next((o for o in play_command['options'] if o['name'] == 'file'), None)
            self.assertIsNotNone(attachment)
            self.assertEqual(attachment['type'], 11)
            self.assertFalse(attachment['required'])
            option = next(o for o in play_command['options'] if o['name'] == 'resolution')
            self.assertFalse(option['required'])
            self.assertIn({'name': '1080p', 'value': '1080'}, option['choices'])
            resolution = next(call.args[1] for call in register.call_args_list if call.args[1]['name'] == 'resolution')
            self.assertTrue(resolution['options'][0]['required'])
            queue = next(call.args[1] for call in register.call_args_list if call.args[1]['name'] == 'queue')
            delete = next(option for option in queue['options'] if option['name'] == 'delete')
            self.assertEqual(delete['type'], 3)
            self.assertFalse(delete['required'])
            self.assertLessEqual(len(delete['description']), 100)
            description = play_command['options'][0]['description']
            self.assertLessEqual(len(description), 100)
            for term in ('Public', 'video/live', '/help'):
                self.assertIn(term, description)
            bulk.assert_not_called()

    def test_supported_video_urls(self):
        cases = {
            'https://youtu.be/aqz-KE-bpKQ?t=5': URL,
            'https://www.youtube.com/shorts/aqz-KE-bpKQ': URL,
            'https://vimeo.com/76979871/abcdef1234': 'https://vimeo.com/76979871/abcdef1234',
            'https://player.vimeo.com/video/76979871?h=abcdef1234': 'https://player.vimeo.com/video/76979871?h=abcdef1234',
            'https://vimeo.com/channels/staffpicks/76979871': 'https://vimeo.com/channels/staffpicks/76979871',
            'https://vimeo.com/groups/shortfilms/videos/76979871': 'https://vimeo.com/groups/shortfilms/videos/76979871',
            'https://vimeo.com/showcase/123/video/76979871': 'https://vimeo.com/showcase/123/video/76979871',
            'http://dai.ly/x84sh87': 'https://www.dailymotion.com/video/x84sh87',
            'https://www.dailymotion.com/video/x84sh87_title?playlist=x123': 'https://www.dailymotion.com/video/x84sh87',
            'https://www.dailymotion.com/embed/video/x84sh87': 'https://www.dailymotion.com/video/x84sh87',
            'https://twitch.tv/example/clip/FancyClip-a1_B2?foo=bar': 'https://clips.twitch.tv/FancyClip-a1_B2',
            'https://m.twitch.tv/videos/123456789?t=5m': 'https://www.twitch.tv/videos/123456789',
            'https://twitch.tv/Example_Channel': 'https://www.twitch.tv/example_channel',
            'https://www.archive.org/embed/Cops1922': 'https://archive.org/details/Cops1922',
            'https://archive.org/details/item/My%20Film.mp4': 'https://archive.org/details/item/My%20Film.mp4',
            'https://archive.org/download/item/My%20Film+Part1.mp4?download=1': 'https://archive.org/download/item/My%20Film%2BPart1.mp4',
            'http://www.odysee.com/@example:a/video:b/?signature=abc&signature_ts=123':
                'https://odysee.com/@example:a/video:b?signature=abc&signature_ts=123',
            'https://odysee.com/video:abcdef': 'https://odysee.com/video:abcdef',
            'https://odysee.com/@example/video': 'https://odysee.com/@example/video',
            'https://odysee.com/video/abcdef': 'https://odysee.com/video/abcdef',
            'https://odysee.com/$/embed/video/abcdef': 'https://odysee.com/$/embed/video/abcdef',
            'https://odysee.com/$/download/video/abcdef': 'https://odysee.com/$/download/video/abcdef',
            'https://odysee.com/@example%3Aa/video%3Ab': 'https://odysee.com/@example:a/video:b',
            'https://odysee.com/video#abcdef': 'https://odysee.com/video:abcdef',
            'https://odysee.com/@example:a/caf%C3%A9:b': 'https://odysee.com/@example:a/caf%C3%A9:b',
            'https://old.bitchute.com/video/UGlrF9o9b-Q/': NEW_PLATFORM_URLS[1],
            'https://bitchute.com/embed/UGlrF9o9b-Q/?foo=bar': NEW_PLATFORM_URLS[1],
            'http://www.rumble.com/vDMUM1?key=value': 'https://rumble.com/vDMUM1?key=value',
            'https://rumble.com/embed/v5pv5f/': 'https://rumble.com/embed/v5pv5f',
            'https://www.rumble.com/embed/ufe9n.v5pv5f/?pub=4': 'https://rumble.com/embed/ufe9n.v5pv5f?pub=4',
            'http://www.kick.com/Example-Channel/?foo=bar': KICK_URLS[0],
            'https://kick.com/Example-Channel/videos/5C697A87-AFCE-4256-B01F-3C8FE71EF5CB/?t=30': KICK_URLS[1],
            'https://www.kick.com/Example-Channel/?foo=bar&clip=clip_01GYXVB5Y8PWAPWCWMSBCFB05X#share': KICK_URLS[2],
            'https://kick.com/example-channel?clip=clip_01GYXVB5Y8PWAPWCWMSBCFB05X': KICK_URLS[2],
            'https://kick.com/Example-Channel/clips/clip_01GYXVB5Y8PWAPWCWMSBCFB05X/': KICK_URLS[2],
        }
        cases.update({url: url for url in PLATFORM_URLS})
        for supplied, expected in cases.items():
            with self.subTest(url=supplied):
                actual = normalize_video_url(supplied)
                self.assertEqual(actual, expected)
                self.assertEqual(normalize_video_url(actual), actual)

    def test_supported_urls_select_expected_extractors(self):
        from yt_dlp.extractor.archiveorg import ArchiveOrgIE
        from yt_dlp.extractor.dailymotion import DailymotionIE
        from yt_dlp.extractor.twitch import TwitchClipsIE, TwitchStreamIE, TwitchVodIE
        from yt_dlp.extractor.vimeo import VimeoIE
        from yt_dlp.extractor.lbry import LBRYIE
        from yt_dlp.extractor.bitchute import BitChuteIE
        from yt_dlp.extractor.rumble import RumbleIE, RumbleEmbedIE
        from yt_dlp.extractor.kick import KickIE, KickVODIE, KickClipIE

        for url, extractor in [
            (PLATFORM_URLS[1], VimeoIE), (PLATFORM_URLS[2], DailymotionIE),
            (PLATFORM_URLS[3], TwitchVodIE), (PLATFORM_URLS[4], TwitchClipsIE),
            (PLATFORM_URLS[5], TwitchStreamIE), (PLATFORM_URLS[6], ArchiveOrgIE),
            ('https://player.vimeo.com/video/76979871?h=abcdef1234', VimeoIE),
            ('https://archive.org/details/item/My%20Film.mp4', ArchiveOrgIE),
            (NEW_PLATFORM_URLS[0], LBRYIE), (NEW_PLATFORM_URLS[1], BitChuteIE),
            (NEW_PLATFORM_URLS[2], RumbleIE), ('https://rumble.com/embed/v5pv5f', RumbleEmbedIE),
            ('https://odysee.com/$/embed/video/abcdef', LBRYIE),
            *zip(KICK_URLS, [KickIE, KickVODIE, KickClipIE]),
        ]:
            with self.subTest(url=url):
                self.assertTrue(extractor.suitable(normalize_video_url(url)))

    def test_restricted_extraction_still_routes_all_supported_sites(self):
        routes = list(zip(PLATFORM_URLS[:7], [
            'Youtube', 'Vimeo', 'Dailymotion', 'TwitchVod', 'TwitchClips', 'TwitchStream', 'ArchiveOrg',
        ])) + list(zip(NEW_PLATFORM_URLS, ['LBRY', 'BitChute', 'Rumble'])) + [
            ('https://rumble.com/embed/v5pv5f', 'RumbleEmbed'),
            ('https://odysee.com/$/embed/video/abcdef', 'LBRY'),
            *zip(KICK_URLS, ['Kick', 'KickVOD', 'KickClip']),
        ]
        for url, expected in routes:
            with self.subTest(url=url):
                def extract(ydl, supplied, download):
                    actual = next(key for key, ie in ydl._ies.items() if ie.suitable(supplied))
                    self.assertEqual(actual, expected)
                    # Generic remains available for redirects to media CDNs.
                    self.assertEqual(next(key for key, ie in ydl._ies.items()
                                          if ie.suitable('https://cdn.example.test/video.mp4')), 'Generic')
                    return {'url': 'https://cdn.example.test/video.mp4'}

                with patch.object(YoutubeDL, 'extract_info', autospec=True, side_effect=extract):
                    extract_video(url)

    def test_kick_live_vod_and_clip_api_results_reach_ffmpeg(self):
        from yt_dlp.extractor.kick import KickIE, KickVODIE, KickClipIE

        manifest = 'https://cdn.example.test/kick/index.m3u8'
        formats = [{'url': manifest, 'ext': 'mp4', 'height': 720,
                    'vcodec': 'avc1.42001f', 'acodec': 'mp4a.40.2', 'protocol': 'm3u8_native'}]
        vod = '5c697a87-afce-4256-b01f-3c8fe71ef5cb'
        fixtures = [
            (KICK_URLS[0], KickIE, {'v2/channels/example-channel': {
                'livestream': {'id': 1, 'slug': 'fixture', 'session_title': 'Live fixture'},
                'playback_url': manifest,
            }}),
            (KICK_URLS[1], KickVODIE, {
                f'v1/stream/{vod}/playback': {'video_session': {'creator_id': '7'},
                                              'playback_url': {'vod': manifest}},
                'v1/channels/7/videos': {'data': [{'id': vod, 'title': 'VOD fixture', 'status': 'public'}]},
            }),
            (KICK_URLS[2], KickClipIE, {'v2/clips/clip_01GYXVB5Y8PWAPWCWMSBCFB05X': {
                'clip': {'clip_url': manifest, 'title': 'Clip fixture'},
            }}),
        ]
        for url, extractor, responses in fixtures:
            with self.subTest(url=url), \
                    patch.object(extractor, '_call_api', side_effect=lambda path, *_, **__: responses[path]) as api, \
                    patch.object(extractor, '_extract_m3u8_formats', return_value=copy.deepcopy(formats)) as hls:
                info = extract_video(url)
                self.assertEqual([call.args[0] for call in api.call_args_list], list(responses))
                hls.assert_called_once()
                self.assertEqual(info['url'], manifest)
                self.assertIn(manifest, ffmpeg_args(info, Path('/tmp/hls')))

        # Kick also returns clips as direct MP4 files rather than HLS.
        source = 'https://cdn.example.test/kick/clip.mp4'
        with patch.object(KickClipIE, '_call_api', return_value={
            'clip': {'clip_url': source, 'title': 'Direct clip fixture'},
        }), patch.object(KickClipIE, '_extract_m3u8_formats') as hls:
            info = extract_video(KICK_URLS[2])
            hls.assert_not_called()
            self.assertEqual(info['url'], source)

    async def test_kick_offline_channel_reports_error_and_releases_room(self):
        from yt_dlp.extractor.kick import KickIE
        from yt_dlp.utils import DownloadError

        with patch.object(KickIE, '_call_api', return_value={'livestream': None}):
            with self.assertRaisesRegex(DownloadError, 'not currently live'):
                extract_video(KICK_URLS[0])
            room = Rooms().prepare(1, KICK_URLS[0])
            await room.task
            self.assertEqual(room.status, 'error')
            self.assertFalse(room.active)
            self.assertIsNone(room.directory)

    def test_rumble_video_page_resolves_through_embed_extractor(self):
        from yt_dlp.extractor.rumble import RumbleIE, RumbleEmbedIE

        embed = 'https://rumble.com/embed/v5pv5f'
        source = 'https://cdn.example.test/rumble.mp4'
        with patch.object(RumbleIE, '_real_extract', return_value={
            '_type': 'url_transparent', 'ie_key': 'RumbleEmbed', 'url': embed,
        }) as page, patch.object(RumbleEmbedIE, '_real_extract', return_value={
            'id': 'v5pv5f', 'title': 'Fixture', 'url': source, 'ext': 'mp4',
        }) as embedded:
            info = extract_video(NEW_PLATFORM_URLS[2])
        page.assert_called_once_with(NEW_PLATFORM_URLS[2])
        embedded.assert_called_once_with(embed)
        self.assertEqual(info['url'], source)
        self.assertIn(source, ffmpeg_args(info, Path('/tmp/hls')))

    def test_new_platform_extraction_preserves_required_media_headers(self):
        from yt_dlp.extractor.lbry import LBRYIE
        from yt_dlp.extractor.bitchute import BitChuteIE

        for url, extractor, referer in [
            (NEW_PLATFORM_URLS[0], LBRYIE, 'https://odysee.com/'),
            (NEW_PLATFORM_URLS[1], BitChuteIE, 'https://www.bitchute.com/'),
        ]:
            with self.subTest(url=url), patch.object(extractor, '_real_extract', return_value={
                'id': 'fixture', 'title': 'Fixture', 'http_headers': {'Referer': referer},
                'formats': [{'url': 'https://cdn.example.test/video.mp4', 'ext': 'mp4'}],
            }):
                info = extract_video(url)
                args = ffmpeg_args(info, Path('/tmp/hls'))
                self.assertIn(f'Referer: {referer}\r\n', args[args.index('-headers') + 1])

    def test_direct_archive_file_skips_extraction_and_preserves_filename(self):
        url = 'https://archive.org/download/item/My%20Film%2BPart1.mp4'
        with patch('yt_dlp.YoutubeDL') as downloader:
            info = extract_video(url)
            downloader.assert_not_called()
        self.assertEqual(info, {'url': url, 'title': 'My Film+Part1.mp4'})
        self.assertIn('async:' + url, ffmpeg_args(info, Path('/tmp/hls')))
        # The shortcut must still pass the same validation as /play.
        for invalid in ['https://archive.org/download/item/audio.mp3',
                        'https://evil.test/download/item/video.mp4']:
            with self.subTest(url=invalid), self.assertRaises(ValueError):
                extract_video(invalid)

    def test_rejects_unsupported_hosts_collections_and_invalid_urls(self):
        for url in ['file:///etc/passwd', 'https://youtube.com.evil.test/watch?v=aqz-KE-bpKQ',
                    'https://youtube.com/playlist?list=anything', 'https://youtube.com/watch?v=bad',
                    'https://vimeo.com.evil.test/123', 'https://dailymotion.com@evil.test/video/x1',
                    'https://user:pass@vimeo.com/123', 'https://vimeo.com:8443/123',
                    'https://vimeo.com/showcase/123', 'https://vimeo.com/channels/staffpicks',
                    'https://www.dailymotion.com/playlist/x123', 'https://dai.ly/',
                    'https://twitch.tv/directory', 'https://twitch.tv/example/videos',
                    'https://twitch.tv/collections/abc', 'https://twitch.tv/search?term=video',
                    'https://clips.twitch.tv/', 'https://archive.org/search?query=films',
                    'https://archive.org/download/item', 'https://archive.org/download/item/audio.mp3',
                    'https://archive.org/download/item/../film.mp4',
                    'https://archive.org/details/%2e%2e/film.mp4',
                    'https://archive.org/download/item/%0afilm.mp4',
                    'https://vi\nmeo.com/123', 'https://vimeo.com\\@evil.test/123',
                    'https://127.0.0.1/movie.mp4', 'https://example.com/movie.mp4',
                    'https://odysee.com/', 'https://odysee.com/@example:a',
                    'https://odysee.com/$/playlist/abcdef', 'https://odysee.com/$/search?q=video',
                    'https://odysee.com/search?q=video', 'https://odysee.com/@example:a/video:b/extra',
                    'https://odysee.com/video:xyz', 'https://odysee.com/%2e%2e',
                    'https://odysee.com/video%0A:b', 'https://odysee.com.evil.test/@example:a/video:b',
                    'https://user:pass@odysee.com/video:b',
                    'https://www.bitchute.com/', 'https://bitchute.com/channel/example',
                    'https://bitchute.com/playlist/abc', 'https://bitchute.com/video/',
                    'https://bitchute.com/video/example/extra', 'https://bitchute.com.evil.test/video/example',
                    'https://rumble.com/', 'https://rumble.com/videos', 'https://rumble.com/c/example',
                    'https://rumble.com/user/example', 'https://rumble.com/search/video?q=foo',
                    'https://rumble.com/browse/live', 'https://rumble.com/embed/',
                    'https://rumble.com/v123-title.html/extra', 'https://rumble.com.evil.test/v123-title.html',
                    'https://rumble.com:8443/v123-title.html',
                    'https://kick.com/', 'https://kick.com/categories', 'https://kick.com/search?q=foo',
                    'https://kick.com/auth', 'https://kick.com/example-channel/videos',
                    'https://kick.com/example-channel/clips', 'https://kick.com/example-channel/videos/not-a-uuid',
                    'https://kick.com/example-channel/clips/not-a-clip',
                    'https://kick.com/example-channel?clip=', 'https://kick.com/example-channel?clip=invalid',
                    'https://kick.com/example-channel?clip=clip_A&clip=clip_B',
                    'https://kick.com/video/5c697a87-afce-4256-b01f-3c8fe71ef5cb',
                    'https://kick.com.evil.test/example-channel', 'https://user:pass@kick.com/example-channel',
                    'https://kick.com:8443/example-channel', 'https://kick.com/example-channel/extra']:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_video_url(url)

    def test_format_selection_for_combined_split_unknown_and_silent_media(self):
        video = {'format_id': 'video', 'url': 'https://example.test/video',
                 'vcodec': 'vp9', 'acodec': 'none', 'height': 720, 'ext': 'webm'}
        audio = {'format_id': 'audio', 'url': 'https://example.test/audio',
                 'vcodec': 'none', 'acodec': 'opus', 'ext': 'webm'}
        combined = {'format_id': 'combined', 'url': 'https://example.test/combined',
                    'vcodec': 'h264', 'acodec': 'aac', 'height': 720, 'ext': 'mp4'}
        unknown = {'format_id': 'archive', 'url': 'https://example.test/archive.mp4', 'ext': 'mp4'}
        fixtures = [([video, audio], 2), ([combined], 1), ([unknown], 1), ([video], 1)]
        for formats, count in fixtures:
            with self.subTest(formats=formats):
                info = {'id': 'fixture', 'title': 'Fixture', 'formats': copy.deepcopy(formats)}

                def extract(ydl, url, download):
                    self.assertFalse(download)
                    return ydl.process_ie_result(info, download=False)

                # Real yt-dlp format selection, with only network extraction replaced.
                with patch.object(YoutubeDL, 'extract_info', autospec=True, side_effect=extract):
                    result = extract_video(PLATFORM_URLS[6])
                self.assertEqual(len(result.get('requested_formats', [result])), count)
                args = ffmpeg_args(result, Path('/tmp/hls'))
                self.assertIn('0:v:0', args)
                self.assertIn('1:a:0' if count == 2 else '0:a:0?', args)

    def test_extraction_rejects_playlists_audio_only_and_non_http_sources(self):
        for info, message in [
            ({'entries': [{'id': 'one'}, {'id': 'two'}]}, 'multiple videos'),
            ({'url': 'https://example.test/audio', 'vcodec': 'none'}, 'No playable video'),
            ({'url': 'file:///etc/passwd'}, 'No HTTP video stream'),
        ]:
            with self.subTest(info=info), patch('yt_dlp.YoutubeDL') as downloader:
                downloader.return_value.__enter__.return_value.extract_info.return_value = info
                with self.assertRaisesRegex(ValueError, message):
                    extract_video(PLATFORM_URLS[6])

    async def test_busy_and_stop_while_loading(self):
        rooms = Rooms()
        async def loading(*args):
            await asyncio.Future()

        with patch('syncnest.stream.asyncio.to_thread', side_effect=loading):
            room = rooms.prepare(1, URL)
            await asyncio.sleep(0)
            with self.assertRaises(ValueError):
                rooms.prepare(1, URL)
            await asyncio.gather(room.stop(), room.stop())
            self.assertFalse(room.active)
            self.assertEqual(room.status, 'stopped')
            self.assertIsNone(room.directory)

    async def test_video_starts_automatically_and_stop_cleans_up(self):
        finished = asyncio.Event()
        process = Mock(returncode=None, stderr=asyncio.StreamReader())
        process.stderr.feed_eof()

        async def wait():
            await finished.wait()
            return process.returncode

        def terminate():
            process.returncode = -15
            finished.set()

        process.wait = wait
        process.terminate = terminate

        async def spawn(*args, **kwargs):
            Path(args[-1]).write_text('#EXTM3U\n#EXTINF:1.000,\n000000.ts\n')
            return process

        with patch('syncnest.stream.extract_video', return_value={'url': 'https://example.test/video'}), \
             patch('asyncio.create_subprocess_exec', side_effect=spawn):
            room = Rooms().prepare(1, URL)
            await until(lambda: room.status == 'playing')
            directory = room.directory
            self.assertIsNotNone(room.source_info)
            await room.stop()
            self.assertIsNone(room.source_info)
            self.assertEqual(room.source_extracted_at, 0)
            self.assertFalse(directory.exists())
            self.assertEqual(process.returncode, -15)

    async def test_play_in_voice_chat_launches_activity(self):
        from syncnest.bot import play

        channel = SimpleNamespace(id=42, members=[SimpleNamespace(bot=False)],
                                  permissions_for=lambda _: SimpleNamespace(create_instant_invite=True))
        interaction = SimpleNamespace(user=SimpleNamespace(id=7), channel_id=42,
                                      response=SimpleNamespace(launch_activity=AsyncMock()))
        for url in PLATFORM_URLS:
            with self.subTest(url=url):
                room = FakeSession()
                rooms = Mock(channels={}, prepare=Mock(return_value=room))
                queues = PlaybackQueues(rooms, SimpleNamespace(stop=AsyncMock()))
                interaction.response.launch_activity.reset_mock()
                try:
                    with patch('syncnest.bot.voice_channel', return_value=channel), \
                         patch('syncnest.bot.rooms', rooms), patch('syncnest.bot.playback', queues):
                        await play.callback(interaction, url)
                    rooms.prepare.assert_called_once_with(42, url, '720')
                    interaction.response.launch_activity.assert_awaited_once()
                finally:
                    await queues.close()

    async def test_stop_during_ffmpeg_creation_reaps_process(self):
        started, release = asyncio.Event(), asyncio.Event()
        process = Mock(returncode=None, wait=AsyncMock(return_value=-15))
        directory = None

        async def spawn(*args, **kwargs):
            nonlocal directory
            directory = Path(args[-1]).parent
            started.set()
            await release.wait()
            return process

        with patch('syncnest.stream.extract_video', return_value={'url': 'https://example.test/video'}), \
             patch('asyncio.create_subprocess_exec', side_effect=spawn):
            room = Rooms().prepare(1, URL)
            await asyncio.wait_for(started.wait(), 1)
            stopping = asyncio.create_task(room.stop())
            await asyncio.sleep(0)
            self.assertFalse(stopping.done())
            release.set()
            await asyncio.wait_for(stopping, 1)
            process.terminate.assert_called_once()
            process.wait.assert_awaited_once()
            self.assertFalse(directory.exists())
            self.assertFalse(room.active)

    async def test_cleanup_kills_ffmpeg_when_termination_times_out(self):
        started, release = asyncio.Event(), asyncio.Event()
        process = Mock(returncode=None, wait=AsyncMock(side_effect=[TimeoutError(), -9]))

        async def spawn(*args, **kwargs):
            started.set()
            await release.wait()
            return process

        with patch('syncnest.stream.extract_video', return_value={'url': 'https://example.test/video'}), \
             patch('asyncio.create_subprocess_exec', side_effect=spawn):
            room = Rooms().prepare(1, URL)
            await asyncio.wait_for(started.wait(), 1)
            stopping = asyncio.create_task(room.stop())
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(stopping, 1)
            process.terminate.assert_called_once()
            process.kill.assert_called_once()
            self.assertEqual(process.wait.await_count, 2)
            self.assertIsNone(room.directory)

    async def test_extraction_overlaps_invite_and_failed_invite_stops_it(self):
        from syncnest.bot import play

        for fail in (False, True):
            with self.subTest(invite_fails=fail):
                room = FakeSession()
                room.stop = AsyncMock(wraps=room.stop)
                rooms = Mock(channels={}, prepare=Mock(return_value=room))
                queues = PlaybackQueues(rooms, SimpleNamespace(stop=AsyncMock()))
                interaction = SimpleNamespace(
                    user=SimpleNamespace(id=7), channel_id=99, response=SimpleNamespace(defer=AsyncMock()),
                    edit_original_response=AsyncMock(),
                )

                async def invite(**kwargs):
                    interaction.response.defer.assert_awaited_once()
                    rooms.prepare.assert_called_once_with(42, URL, '720')
                    if fail:
                        raise discord.HTTPException(Mock(status=403, reason='Forbidden'), 'No permission')
                    return SimpleNamespace(url='https://discord.gg/test')

                channel = SimpleNamespace(id=42, create_invite=invite, members=[SimpleNamespace(bot=False)],
                                          permissions_for=lambda _: SimpleNamespace(create_instant_invite=True))
                try:
                    with patch('syncnest.bot.voice_channel', return_value=channel), \
                         patch('syncnest.bot.rooms', rooms), patch('syncnest.bot.playback', queues):
                        await play.callback(interaction, URL)
                    self.assertEqual(room.stop.await_count, int(fail))
                    interaction.edit_original_response.assert_awaited_once()
                finally:
                    await queues.close()

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    async def test_prefilled_stream_contains_video_and_audio_and_stays_bounded(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.mp4'
            generation = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '30',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(source),
            )
            self.assertEqual(await generation.wait(), 0)
            output = root / 'hls'
            output.mkdir()
            process = await asyncio.create_subprocess_exec(
                *ffmpeg_args({'url': str(source)}, output),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await until(lambda: (output / '000002.ts').exists())
                self.assertIsNone(process.returncode)  # The whole 30 seconds were not read ahead.
                self.assertLessEqual(len(list(output.glob('*.ts'))), 10)
                probe = await asyncio.create_subprocess_exec(
                    'ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name', '-of', 'json',
                    str(output / '000000.ts'), stdout=asyncio.subprocess.PIPE,
                )
                result, _ = await probe.communicate()
                self.assertEqual({s['codec_name'] for s in json.loads(result)['streams']}, {'h264', 'aac'})
            finally:
                if process.returncode is None:
                    process.terminate()
                await process.wait()

    async def test_extraction_error(self):
        with patch('syncnest.stream.extract_video', side_effect=ValueError('Unavailable')):
            room = Rooms().prepare(1, URL)
            await room.task
            self.assertEqual(room.status, 'error')
            self.assertIsNotNone(room.error)

    async def test_archive_playlist_error_reaches_player(self):
        message = 'This link contains multiple videos. Use a link to one video or an Internet Archive video file.'
        with patch('syncnest.stream.extract_video', side_effect=ValueError(message)):
            room = Rooms().prepare(1, PLATFORM_URLS[6])
            await room.task
            self.assertEqual(room.snapshot()['error'], message)
            self.assertEqual(room.status, 'error')

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    async def test_silent_video_produces_playable_hls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'silent.mp4'
            generation = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
                '-t', '2', '-c:v', 'libx264', '-preset', 'ultrafast', str(source),
            )
            self.assertEqual(await generation.wait(), 0)
            args = ffmpeg_args({'url': str(source), 'vcodec': 'h264', 'acodec': 'none'}, root)
            args.remove('-re')
            index = args.index('-readrate_initial_burst')
            del args[index:index + 2]
            conversion = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
            self.assertEqual(await conversion.wait(), 0)
            self.assertTrue((root / 'index.m3u8').is_file())
            decode = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-i', str(root / '000000.ts'), '-f', 'null', '-')
            self.assertEqual(await decode.wait(), 0)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'), 'FFmpeg required')
    async def test_copied_audio_plays_with_combined_and_separate_inputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'source.mp4'
            generation = await asyncio.create_subprocess_exec(
                'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=320x180:rate=30',
                '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=44100', '-t', '4',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-c:a', 'aac', str(source),
            )
            self.assertEqual(await generation.wait(), 0)
            audio = {'url': str(source), 'vcodec': 'h264', 'acodec': 'mp4a.40.2', 'audio_channels': 1,
                     'asr': 44100, 'abr': 128}
            for separate in (False, True):
                with self.subTest(separate=separate):
                    output = root / str(separate)
                    output.mkdir()
                    info = {'requested_formats': [{'url': str(source), 'vcodec': 'h264', 'acodec': 'none'},
                                                  audio | {'vcodec': 'none'}]} if separate else audio
                    args = ffmpeg_args(info, output)
                    # Offline fixture: process quickly, without real-time input pacing.
                    while '-re' in args:
                        args.remove('-re')
                        index = args.index('-readrate_initial_burst')
                        del args[index:index + 2]
                    conversion = await asyncio.create_subprocess_exec(
                        *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    self.assertEqual(await conversion.wait(), 0)
                    segment = output / '000000.ts'
                    probe = await asyncio.create_subprocess_exec(
                        'ffprobe', '-v', 'error', '-show_entries',
                        'stream=codec_name,sample_rate,channels', '-of', 'json', str(segment),
                        stdout=asyncio.subprocess.PIPE,
                    )
                    result, _ = await probe.communicate()
                    self.assertEqual(probe.returncode, 0)
                    streams = json.loads(result)['streams']
                    self.assertEqual({s['codec_name'] for s in streams}, {'h264', 'aac'})
                    sound = next(s for s in streams if s['codec_name'] == 'aac')
                    self.assertEqual((sound['sample_rate'], sound['channels']), ('44100', 1))
                    decode = await asyncio.create_subprocess_exec(
                        'ffmpeg', '-v', 'error', '-i', str(segment), '-f', 'null', '-')
                    self.assertEqual(await decode.wait(), 0)

    async def test_ffmpeg_start_error_cleans_buffer(self):
        directory = None

        async def fail(*args, **kwargs):
            nonlocal directory
            directory = Path(args[-1]).parent
            self.assertTrue(directory.is_dir())
            raise OSError('FFmpeg unavailable')

        with patch('syncnest.stream.extract_video', return_value={'url': 'https://example.test/video'}), \
             patch('asyncio.create_subprocess_exec', side_effect=fail):
            room = Rooms().prepare(1, URL)
            await room.task
            self.assertEqual(room.status, 'error')
            self.assertFalse(directory.exists())


class ActivityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.users = ['7']
        self.api_requests = []
        self.require_parallel_checks = False
        self.disconnect_during_lookup = False
        self.oauth_status = 200
        self.oauth_body = {'access_token': 'test-access', 'expires_in': 3600}
        self.oauth_headers = {}
        checks = set()
        both_checks = asyncio.Event()
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id, self.channel.name = 42, 'Test voice'
        self.member = SimpleNamespace(voice=SimpleNamespace(channel=self.channel))
        self.channel.guild.get_member.return_value = self.member
        self.bot = SimpleNamespace(application_id=100, get_channel=lambda _: self.channel,
                                   voice_states_ready=True)
        self.rooms = Rooms()

        async def discord_api(request):
            self.api_requests.append(request.path)
            if request.path == '/oauth2/token':
                return web.json_response(self.oauth_body, status=self.oauth_status,
                                         headers=self.oauth_headers)
            if self.require_parallel_checks:
                checks.add(request.path)
                if len(checks) == 2:
                    both_checks.set()
                await both_checks.wait()
            if request.path == '/users/@me':
                return web.json_response({'id': '7'})
            if self.disconnect_during_lookup:
                self.bot.voice_states_ready = False
            return web.json_response({'users': self.users, 'location': {'channel_id': '42'}})

        mock_api = web.Application()
        mock_api.router.add_route('*', '/{path:.*}', discord_api)
        self.discord_server = TestServer(mock_api)
        await self.discord_server.start_server()
        self.api_patch = patch('syncnest.activity.API', str(self.discord_server.make_url('')).rstrip('/'))
        self.api_patch.start()
        self.client = TestClient(TestServer(activity.create_app(self.bot, self.rooms, 'test-token', 'test-secret')))
        await self.client.start_server()
        async def loading(*args):
            await asyncio.Future()

        self.extraction = patch('syncnest.stream.asyncio.to_thread', side_effect=loading)
        self.extraction.start()

    async def asyncTearDown(self):
        await self.rooms.close()
        await self.client.close()
        await self.discord_server.close()
        self.api_patch.stop()
        self.extraction.stop()

    async def login(self):
        response = await self.client.post('/api/login', json={'code': 'test-code', 'instance_id': 'instance'})
        self.assertEqual(response.status, 200)
        return {'X-SyncNest-Session': (await response.json())['session']}

    async def test_auth_required_and_instance_membership_verified(self):
        response = await self.client.get('/api/state')
        self.assertEqual(response.status, 401)
        self.users = ['someone-else']
        response = await self.client.post('/api/login', json={'code': 'code', 'instance_id': 'instance'})
        self.assertEqual(response.status, 403)

    async def test_login_requires_ready_gateway_and_recovers(self):
        self.bot.voice_states_ready = False
        for prefix in ('', '/.proxy'):
            response = await self.client.post(prefix + '/api/login',
                                             json={'code': 'code', 'instance_id': 'instance'})
            self.assertEqual(response.status, 503)
        self.assertEqual(self.api_requests, [])
        self.bot.voice_states_ready = True
        await self.login()

    async def test_login_rechecks_gateway_after_discord_lookup(self):
        self.disconnect_during_lookup = True
        response = await self.client.post('/api/login', json={'code': 'code', 'instance_id': 'instance'})
        self.assertEqual(response.status, 503)
        self.assertNotIn('session', await response.json())

    async def test_invalid_login_payloads_never_contact_discord(self):
        for payload in [None, [], 1, {}, {'code': 7, 'instance_id': 'i'},
                        {'code': '', 'instance_id': 'i'}, {'code': '  ', 'instance_id': 'i'},
                        {'code': 'c', 'instance_id': ''}, {'code': 'c', 'instance_id': '  '},
                        {'code': 'c', 'instance_id': 'x' * 201},
                        {'code': 'c', 'instance_id': '.'}, {'code': 'c', 'instance_id': '..'}]:
            with self.subTest(payload=payload):
                response = await self.client.post('/api/login', data=json.dumps(payload),
                                                  headers={'Content-Type': 'application/json'})
                self.assertEqual(response.status, 400)
        self.assertEqual(self.api_requests, [])

    async def test_malformed_and_oversized_login_requests(self):
        response = await self.client.post('/api/login', data='{broken')
        self.assertEqual(response.status, 400)
        response = await self.client.post('/api/login', data='x' * 4097)
        self.assertEqual(response.status, 413)
        self.assertEqual(self.api_requests, [])

    async def test_expired_session_denies_state_and_media(self):
        clock = SimpleNamespace(monotonic=lambda: 100)
        with patch('syncnest.activity.time', clock):
            headers = await self.login()
            clock.monotonic = lambda: 3701
            for path in ['/api/state', '/.proxy/api/state', '/media/room/index.m3u8']:
                self.assertEqual((await self.client.get(path, headers=headers)).status, 401)

    async def test_login_requires_actual_voice_membership(self):
        for voice in [None, SimpleNamespace(channel=SimpleNamespace(id=99))]:
            self.member.voice = voice
            response = await self.client.post('/api/login', json={'code': 'c', 'instance_id': 'i'})
            self.assertEqual(response.status, 403)

    async def test_private_files_are_not_exposed(self):
        for prefix in ['', '/.proxy']:
            for path in ['/.env', '/bot.py', '/static/../.env', '/static/']:
                response = await self.client.get(prefix + path)
                self.assertIn(response.status, [403, 404])
                self.assertNotIn('test-secret', await response.text())

    async def test_identity_and_activity_checks_run_concurrently(self):
        self.require_parallel_checks = True
        await asyncio.wait_for(self.login(), 5)

    async def test_failed_oauth_never_uses_bot_credentials_for_instance_lookup(self):
        self.oauth_status = 400
        response = await self.client.post('/api/login', json={'code': 'invalid', 'instance_id': 'instance'})
        self.assertEqual(response.status, 403)
        self.assertEqual(self.api_requests, ['/oauth2/token'])

    async def test_login_attempts_are_limited_across_proxy_aliases_and_recover(self):
        clock = SimpleNamespace(monotonic=lambda: 100)
        with patch('syncnest.activity.time', clock), patch('syncnest.activity.LOGIN_LIMIT', 2):
            self.oauth_status = 400
            for prefix in ('', '/.proxy'):
                response = await self.client.post(prefix + '/api/login', json={'code': 'invalid', 'instance_id': 'i'})
                self.assertEqual(response.status, 403)
            requests = len(self.api_requests)
            response = await self.client.post('/.proxy/api/login', json={'code': 'code', 'instance_id': 'i'})
            self.assertEqual(response.status, 429)
            self.assertGreater(int(response.headers['Retry-After']), 0)
            self.assertEqual(len(self.api_requests), requests)
            clock.monotonic = lambda: 200
            self.oauth_status = 200
            await self.login()

    async def test_discord_retry_after_blocks_further_calls_until_cooldown_ends(self):
        clock = SimpleNamespace(monotonic=lambda: 100)
        with patch('syncnest.activity.time', clock):
            for index, header in enumerate(('60', None, 'nan')):
                with self.subTest(header=header):
                    now = 100 + index * 200
                    clock.monotonic = lambda: now
                    self.oauth_status = 429
                    self.oauth_body = {'retry_after': 60}
                    self.oauth_headers = {'Retry-After': header} if header else {}
                    response = await self.client.post('/api/login', json={'code': 'c', 'instance_id': 'i'})
                    self.assertEqual(response.status, 429)
                    delay = int(response.headers['Retry-After'])
                    self.assertEqual(delay, 1 if header == 'nan' else 60)
                    requests = len(self.api_requests)
                    self.oauth_status = 200
                    self.oauth_body = {'access_token': 'test-access', 'expires_in': 3600}
                    response = await self.client.post('/api/login', json={'code': 'c', 'instance_id': 'i'})
                    self.assertEqual(response.status, 429)
                    self.assertEqual(len(self.api_requests), requests)
                    clock.monotonic = lambda: now + delay + 1
                    await self.login()

    async def test_login_contains_initial_playback_state(self):
        room = self.rooms.prepare(42, URL)
        response = await self.client.post('/api/login', json={'code': 'test-code', 'instance_id': 'instance'})
        self.assertEqual((await response.json())['room']['id'], room.id)
        self.assertIn('no-store', response.headers['Cache-Control'])

    async def test_page_has_public_config_and_versioned_compressed_assets(self):
        response = await self.client.get('/')
        page = await response.text()
        self.assertIn('<title>SyncNest</title>', page)
        self.assertIn('name="discord-client-id" content="100"', page)
        self.assertNotIn('test-secret', page)
        self.assertNotIn('test-token', page)
        # Native autoplay must not bypass the SDK authentication gate.
        self.assertNotRegex(page, r'<video\b[^>]*\bautoplay\b')
        self.assertIn('no-store', response.headers['Cache-Control'])
        url = re.search(r'src="([^"]+app\.js\?v=[a-f0-9]+)"', page).group(1)
        asset = await self.client.get(url, headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(asset.headers['Content-Encoding'], 'gzip')
        self.assertIn('immutable', asset.headers['Cache-Control'])
        self.assertEqual(await asset.read(), (activity.WEB / 'dist' / 'app.js').read_bytes())
        for chunk in (activity.WEB / 'dist' / 'chunks').glob('*.js'):
            for prefix in ('', '/.proxy'):
                response = await self.client.get(f'{prefix}/static/chunks/{chunk.name}',
                                                 headers={'Accept-Encoding': 'gzip'})
                self.assertEqual(response.status, 200)
                self.assertEqual(response.headers['Content-Encoding'], 'gzip')
                self.assertIn('immutable', response.headers['Cache-Control'])
                self.assertEqual(await response.read(), chunk.read_bytes())
        unversioned = await self.client.get('/static/app.js')
        self.assertIn('no-store', unversioned.headers['Cache-Control'])

    async def test_brotli_player_assets_are_served_with_cache_and_encoding_headers(self):
        paths = [activity.WEB / 'dist' / 'app.js', activity.WEB / 'dist' / 'style.css',
                 *(activity.WEB / 'dist' / 'chunks').glob('*.js')]
        for path in paths:
            url = path.relative_to(activity.WEB / 'dist').as_posix()
            response = await self.client.get('/.proxy/static/' + url,
                                              headers={'Accept-Encoding': 'br, gzip'},
                                              auto_decompress=False)
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers['Content-Encoding'], 'br')
            self.assertIn('Accept-Encoding', response.headers['Vary'])
            self.assertEqual(await response.read(), path.with_suffix(path.suffix + '.br').read_bytes())
            if url.startswith('chunks/'):
                self.assertIn('immutable', response.headers['Cache-Control'])

    async def test_state_and_leaving_voice(self):
        headers = await self.login()
        room = self.rooms.prepare(42, URL)
        response = await self.client.get('/api/state', headers=headers)
        self.assertEqual((await response.json())['room']['id'], room.id)
        await room.stop()
        response = await self.client.get('/api/state', headers=headers)
        self.assertEqual((await response.json())['room']['status'], 'stopped')
        self.member.voice = None
        response = await self.client.get('/api/state', headers=headers)
        self.assertEqual(response.status, 403)

    async def test_gateway_outage_blocks_state_and_media_until_cache_recovers(self):
        headers = await self.login()
        with tempfile.TemporaryDirectory() as directory:
            room = self.rooms.prepare(42, URL)
            await asyncio.sleep(0)
            room.directory = Path(directory)
            (room.directory / 'index.m3u8').write_text('#EXTM3U\n')
            paths = ['/api/state', f'/media/{room.id}/index.m3u8']
            self.bot.voice_states_ready = False
            for prefix in ('', '/.proxy'):
                for path in paths:
                    self.assertEqual((await self.client.get(prefix + path, headers=headers)).status, 503)
            self.bot.voice_states_ready = True
            for path in paths:
                self.assertEqual((await self.client.get(path, headers=headers)).status, 200)
            self.member.voice = None
            for path in paths:
                self.assertEqual((await self.client.get(path, headers=headers)).status, 403)

    async def test_waiting_state_rechecks_gateway_readiness(self):
        headers = await self.login()
        request = asyncio.create_task(self.client.get('/api/state?revision=0', headers=headers))
        try:
            await until(lambda: 42 in self.rooms.events)
            self.bot.voice_states_ready = False
            response = await asyncio.wait_for(request, 2)
            self.assertEqual(response.status, 503)
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    async def test_state_waits_and_wakes_all_viewers_on_room_change(self):
        headers = await self.login()
        response = await self.client.get('/api/state', headers=headers)
        revision = (await response.json())['revision']
        requests = [asyncio.create_task(self.client.get(
            f'/api/state?revision={revision}', headers=headers)) for _ in range(2)]
        try:
            await until(lambda: 42 in self.rooms.events)
            await asyncio.sleep(0.05)
            self.assertTrue(all(not request.done() for request in requests))
            room = self.rooms.prepare(42, URL)
            for response in await asyncio.wait_for(asyncio.gather(*requests), 1):
                data = await response.json()
                self.assertEqual(data['room']['id'], room.id)
                self.assertGreater(data['revision'], revision)
        finally:
            for request in requests:
                request.cancel()
            await asyncio.gather(*requests, return_exceptions=True)

    async def test_waiting_state_revokes_access_after_leaving(self):
        headers = await self.login()
        request = asyncio.create_task(self.client.get('/api/state?revision=0', headers=headers))
        try:
            await until(lambda: 42 in self.rooms.events)
            self.member.voice = None
            response = await asyncio.wait_for(request, 2)
            self.assertEqual(response.status, 403)
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    async def test_waiting_state_returns_periodic_heartbeat(self):
        headers = await self.login()
        with patch('syncnest.activity.STATE_WAIT_SECONDS', 0.05):
            response = await asyncio.wait_for(self.client.get(
                '/api/state?revision=0', headers=headers), 1)
        self.assertEqual(response.status, 200)
        self.assertEqual(await response.json(), {'room': None, 'revision': 0})

    async def test_other_channel_does_not_wake_waiter(self):
        headers = await self.login()
        request = asyncio.create_task(self.client.get('/api/state?revision=0', headers=headers))
        try:
            await until(lambda: 42 in self.rooms.events)
            self.rooms.prepare(99, URL)
            await asyncio.sleep(0.05)
            self.assertFalse(request.done())
            self.rooms.prepare(42, URL)
            self.assertEqual((await asyncio.wait_for(request, 1)).status, 200)
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    async def test_stop_wakes_waiter_and_includes_final_status(self):
        headers = await self.login()
        room = self.rooms.prepare(42, URL)
        await asyncio.sleep(0)
        request = asyncio.create_task(self.client.get(
            f'/api/state?revision={self.rooms.revision(42)}', headers=headers))
        try:
            await until(lambda: 42 in self.rooms.events)
            await room.stop()
            response = await asyncio.wait_for(request, 1)
            self.assertEqual((await response.json())['room']['status'], 'stopped')
        finally:
            request.cancel()
            await asyncio.gather(request, return_exceptions=True)

    async def test_media_bound_to_authorized_channel(self):
        headers = await self.login()
        with tempfile.TemporaryDirectory() as directory:
            room = self.rooms.prepare(42, URL)
            await asyncio.sleep(0)
            room.directory = Path(directory)
            (room.directory / 'index.m3u8').write_text('#EXTM3U\n')
            url = f'/media/{room.id}/index.m3u8'
            self.assertEqual((await self.client.get(url)).status, 401)
            response = await self.client.get(url, headers=headers)
            self.assertEqual(response.status, 200)
            self.assertIn('no-store', response.headers['Cache-Control'])
            self.assertEqual((await self.client.get('/media/other/index.m3u8', headers=headers)).status, 404)
            self.assertEqual((await self.client.get(f'/media/{room.id}/.env', headers=headers)).status, 404)
            self.member.voice.channel = SimpleNamespace(id=99)
            self.assertEqual((await self.client.get(url, headers=headers)).status, 403)

    async def test_media_and_state_requests_are_rate_limited_per_session(self):
        clock = SimpleNamespace(monotonic=lambda: 100)
        with patch('syncnest.activity.time', clock):
            headers, other = await self.login(), await self.login()
            statuses = [(await self.client.get('/api/state?revision=stale', headers=headers)).status
                        for _ in range(activity.SESSION_BURST + 1)]
            self.assertEqual(statuses[:-1], [200] * activity.SESSION_BURST)
            self.assertEqual(statuses[-1], 429)
            # Budgets are per session, and they refill with time.
            self.assertEqual((await self.client.get('/api/state?revision=x', headers=other)).status, 200)
            self.assertEqual((await self.client.get('/media/room/index.m3u8', headers=headers)).status, 429)
            clock.monotonic = lambda: 101
            self.assertEqual((await self.client.get('/api/state?revision=x', headers=headers)).status, 200)

    async def test_each_user_keeps_only_their_newest_sessions(self):
        sessions = [await self.login() for _ in range(activity.SESSIONS_PER_USER + 1)]
        self.assertEqual((await self.client.get('/api/state?revision=x', headers=sessions[0])).status, 401)
        for headers in sessions[1:]:
            self.assertEqual((await self.client.get('/api/state?revision=x', headers=headers)).status, 200)

    async def test_extra_concurrent_polls_return_without_waiting(self):
        headers = await self.login()
        revision = (await (await self.client.get('/api/state', headers=headers)).json())['revision']
        url = f'/api/state?revision={revision}'
        held = [asyncio.create_task(self.client.get(url, headers=headers))
                for _ in range(activity.POLLS_PER_SESSION)]
        await asyncio.sleep(0.2)
        extra = await asyncio.wait_for(self.client.get(url, headers=headers), 1)
        self.assertEqual(extra.status, 200)
        self.assertFalse(any(task.done() for task in held))
        self.rooms.changed(42)
        await asyncio.wait_for(asyncio.gather(*held), 2)


class ProxyAuthenticationTests(ActivityTests):
    async def asyncSetUp(self):
        self.key = SigningKey.generate()
        await super().asyncSetUp()
        await self.client.close()
        self.client = TestClient(TestServer(activity.create_app(
            self.bot, self.rooms, 'test-token', 'test-secret', self.key.verify_key.encode().hex())))
        await self.client.start_server()

    def signed(self, *, encoding='hex', created=None, expires=None, tamper=False):
        created = int(time.time()) if created is None else created
        payload = json.dumps({'created_at': created, 'expires_at': expires or created + 300,
                              'user_id': '7'}).encode()
        signature = self.key.sign(payload).signature
        if tamper:
            payload = payload.replace(b'"7"', b'"8"')
        return {'X-Signature-Ed25519': signature.hex() if encoding == 'hex' else base64.b64encode(signature).decode(),
                'X-Signature-Timestamp': str(created),
                'X-Discord-Proxy-Payload': base64.b64encode(payload).decode()}

    async def login(self):
        response = await self.client.post('/api/login', headers=self.signed(),
                                          json={'code': 'test-code', 'instance_id': 'instance'})
        self.assertEqual(response.status, 200)
        return {'X-SyncNest-Session': (await response.json())['session']}

    async def test_unsigned_or_forged_logins_never_reach_discord_or_the_login_budget(self):
        now = int(time.time())
        forged = [{}, self.signed(tamper=True), self.signed(expires=now - 1),
                  self.signed() | {'X-Signature-Timestamp': str(now + 5)},
                  self.signed() | {'X-Signature-Ed25519': 'zz'},
                  self.signed() | {'X-Discord-Proxy-Payload': '%%%'},
                  self.signed(encoding='base64', tamper=True)]
        with patch('syncnest.activity.LOGIN_LIMIT', 1):
            for headers in forged:
                with self.subTest(headers=headers):
                    response = await self.client.post('/api/login', headers=headers,
                                                      json={'code': 'c', 'instance_id': 'i'})
                    self.assertEqual(response.status, 401)
            self.assertEqual(self.api_requests, [])
            # Rejected requests spent nothing: one signed login still fits the limit of one.
            response = await self.client.post('/api/login', headers=self.signed(encoding='base64'),
                                              json={'code': 'c', 'instance_id': 'i'})
            self.assertEqual(response.status, 200)

    # Inherited tests post unsigned logins directly; they cover the unsigned path above.
    test_auth_required_and_instance_membership_verified = None
    test_login_requires_ready_gateway_and_recovers = None
    test_login_rechecks_gateway_after_discord_lookup = None
    test_invalid_login_payloads_never_contact_discord = None
    test_malformed_and_oversized_login_requests = None
    test_login_requires_actual_voice_membership = None
    test_failed_oauth_never_uses_bot_credentials_for_instance_lookup = None
    test_login_attempts_are_limited_across_proxy_aliases_and_recover = None
    test_discord_retry_after_blocks_further_calls_until_cooldown_ends = None
    test_login_contains_initial_playback_state = None
    # These replace the module clock with a monotonic-only fake; the base class covers them.
    test_expired_session_denies_state_and_media = None
    test_media_and_state_requests_are_rate_limited_per_session = None


if __name__ == '__main__':
    unittest.main()
