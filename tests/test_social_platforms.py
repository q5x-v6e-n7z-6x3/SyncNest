"""Anonymous social-video routing and live playback; no platform requests."""

import copy
import io
import unittest
from pathlib import Path
from unittest.mock import patch

from yt_dlp import DownloadError, YoutubeDL

from syncnest.stream import extract_video, extraction_options, ffmpeg_args, normalize_video_url


ROUTES = [
    ('https://youtube.com/live/aqz-KE-bpKQ', 'https://www.youtube.com/watch?v=aqz-KE-bpKQ', 'Youtube'),
    ('https://youtube.com/@example/live?feature=share', 'https://www.youtube.com/@example/live', 'YoutubeTab'),
    ('https://youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa/live',
     'https://www.youtube.com/channel/UCaaaaaaaaaaaaaaaaaaaaaa/live', 'YoutubeTab'),
    ('https://youtube.com/c/example/live', 'https://www.youtube.com/c/example/live', 'YoutubeTab'),
    ('https://youtube.com/user/example/live', 'https://www.youtube.com/user/example/live', 'YoutubeTab'),
    ('https://twitter.com/example/status/1234567890?s=20', 'https://x.com/example/status/1234567890', 'Twitter'),
    ('https://mobile.x.com/i/web/status/1234567890/video/2', 'https://x.com/i/web/status/1234567890/video/2', 'Twitter'),
    ('https://x.com/i/status/1234567890', 'https://x.com/i/status/1234567890', 'Twitter'),
    ('https://x.com/i/broadcasts/1AbCdEfGhIjKl?ref=share', 'https://x.com/i/broadcasts/1AbCdEfGhIjKl', 'TwitterBroadcast'),
    ('https://mobile.twitter.com/i/broadcasts/1AbCdEfGhIjKl/', 'https://x.com/i/broadcasts/1AbCdEfGhIjKl', 'TwitterBroadcast'),
    ('https://instagram.com/p/AbCdEfG1234/?igsh=tracking', 'https://www.instagram.com/p/AbCdEfG1234/', 'Instagram'),
    ('https://www.instagram.com/example/reel/AbCdEfG1234/', 'https://www.instagram.com/reel/AbCdEfG1234/', 'Instagram'),
    ('https://instagram.com/reels/AbCdEfG1234/', 'https://www.instagram.com/reel/AbCdEfG1234/', 'Instagram'),
    ('https://instagram.com/tv/AbCdEfG1234/', 'https://www.instagram.com/tv/AbCdEfG1234/', 'Instagram'),
    ('https://tiktok.com/@example/video/1234567890?is_from_webapp=1',
     'https://www.tiktok.com/@example/video/1234567890', 'TikTok'),
    ('https://m.tiktok.com/@example.name/video/1234567890/',
     'https://www.tiktok.com/@example.name/video/1234567890', 'TikTok'),
    ('https://www.tiktok.com/share/video/1234567890', 'https://www.tiktok.com/share/video/1234567890', 'TikTok'),
    ('https://tiktok.com/embed/1234567890', 'https://www.tiktok.com/embed/1234567890', 'TikTok'),
    ('https://tiktok.com/@example/live/?enter_from=share', 'https://www.tiktok.com/@example/live', 'TikTokLive'),
    ('https://m.tiktok.com/share/live/1234567890/?language=en',
     'https://m.tiktok.com/share/live/1234567890', 'TikTokLive'),
]


class SocialPlatformTests(unittest.TestCase):
    def test_public_video_and_live_urls_normalize_and_route_to_specific_extractors(self):
        for supplied, expected, extractor in ROUTES:
            with self.subTest(url=supplied):
                try:
                    normalized = normalize_video_url(supplied)
                except ValueError as error:
                    self.fail(str(error))
                self.assertEqual(normalized, expected)
                self.assertEqual(normalize_video_url(normalized), normalized)

                def extract(ydl, url, download):
                    actual = next(key for key, ie in ydl._ies.items() if ie.suitable(url))
                    self.assertEqual(actual, extractor)
                    if extractor in {'TikTok', 'TikTokLive'}:
                        self.assertEqual(set(ydl._ies), {'TikTok', 'TikTokLive'})
                    return {'url': 'https://cdn.example.test/video.mp4'}

                with patch.object(YoutubeDL, 'extract_info', autospec=True, side_effect=extract):
                    extract_video(supplied)

    def test_rejects_profiles_collections_audio_spaces_and_host_or_path_spoofs(self):
        urls = [
            'https://youtube.com/@example', 'https://youtube.com/@example/streams',
            'https://youtube.com/channel/not-a-channel/live',
            'https://youtube.com/@example/live/extra',
            'https://x.com/example', 'https://x.com/search?q=video',
            'https://x.com/i/events/1234567890', 'https://x.com/i/spaces/1AbCdEfGhIjKl',
            'https://x.com/example/status/1234567890/video/0',
            'https://x.com/example/status/1234567890/photo/1',
            'https://x.com/i/broadcasts/', 'https://x.com/i/broadcasts/invalid-id',
            'https://x.com/i/broadcasts/1AbCdEfGhIjKl/extra',
            'https://x.com.evil.test/i/broadcasts/1AbCdEfGhIjKl',
            'https://facebook.com/example', 'https://facebook.com/watch/',
            'https://facebook.com/watch/?v=', 'https://facebook.com/watch/?v=1&v=2',
            'https://facebook.com/groups/example', 'https://facebook.com/watch/live/?v=bad',
            'https://instagram.com/example', 'https://instagram.com/explore/tags/video',
            'https://instagram.com/stories/example/123', 'https://instagram.com/reels/audio/123',
            'https://instagram.com/p/%2e%2e/', 'https://instagram.com/reel/AbCdEfG1234/extra',
            'https://x.com.evil.test/example/status/1234567890',
            'https://facebook.com.evil.test/watch/?v=1234567890',
            'https://instagram.com.evil.test/reel/AbCdEfG1234/',
            'https://user:pass@x.com/example/status/1234567890',
            'https://facebook.com:8443/watch/?v=1234567890',
            'https://tiktok.com/@example', 'https://tiktok.com/@example/photo/1234567890',
            'https://tiktok.com/@example/video/invalid', 'https://tiktok.com/@example/live/extra',
            'https://tiktok.com/@example/video/1234567890/extra',
            'https://tiktok.com/music/example-1234567890', 'https://tiktok.com/tag/example',
            'https://tiktok.com/@example/collection/example-1234567890',
            'https://tiktok.com.evil.test/@example/video/1234567890',
            'https://vm.tiktok.com/AbCdEf/', 'https://vt.tiktok.com/AbCdEf/',
            'https://www.tiktok.com/t/AbCdEf/', 'https://douyin.com/video/1234567890',
        ]
        for url in urls:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_video_url(url)

    def test_removed_platform_links_are_rejected_before_extraction(self):
        urls = [
            'https://instagram.com/example/live/',
            'https://www.instagram.com/example/live/1234567890/',
            'https://facebook.com/watch?v=1234567890&ref=share',
            'https://m.facebook.com/watch/live/?v=1234567890',
            'https://www.facebook.com/example/videos/1234567890/',
            'https://facebook.com/example/videos/fixture-title/1234567890/',
            'https://facebook.com/video.php?v=1234567890',
            'https://facebook.com/reel/1234567890/',
        ]
        with patch('syncnest.stream.yt_dlp.YoutubeDL') as downloader:
            for url in urls:
                with self.subTest(url=url), self.assertRaises(ValueError):
                    extract_video(url)
            downloader.assert_not_called()

    def test_new_extractors_preserve_live_flags_headers_and_encoder_pacing(self):
        from yt_dlp.extractor.instagram import InstagramIE
        from yt_dlp.extractor.tiktok import TikTokIE
        from yt_dlp.extractor.twitter import TwitterIE
        from yt_dlp.extractor.youtube import YoutubeIE

        cases = [
            ('https://youtube.com/live/aqz-KE-bpKQ', YoutubeIE, True),
            ('https://x.com/example/status/1234567890', TwitterIE, False),
            ('https://instagram.com/reel/AbCdEfG1234/', InstagramIE, False),
            ('https://tiktok.com/@example/video/1234567890', TikTokIE, False),
        ]
        for url, extractor, live in cases:
            fixture = {'id': 'fixture', 'title': 'Fixture', 'is_live': live,
                       'url': 'https://cdn.example.test/video.m3u8', 'ext': 'mp4',
                       'vcodec': 'h264', 'acodec': 'aac', 'height': 720,
                       'http_headers': {'Referer': 'https://example.test/'}}
            with self.subTest(url=url), \
                 patch.object(extractor, '_real_initialize'), \
                 patch.object(extractor, '_real_extract', return_value=copy.deepcopy(fixture)), \
                 patch.object(YoutubeDL, 'urlopen', side_effect=AssertionError('Unexpected platform request')):
                info = extract_video(url)
                self.assertEqual(info['is_live'], live)
                args = ffmpeg_args(info, Path('/tmp/hls'))
                self.assertEqual('-re' in args, not live)
                self.assertIn('Referer: https://example.test/\r\n', args[args.index('-headers') + 1])

    def test_youtube_live_channel_redirect_keeps_helper_extractor_enabled(self):
        from yt_dlp.extractor.youtube import YoutubeIE, YoutubeTabIE

        redirect = {'_type': 'url', 'url': 'https://www.youtube.com/watch?v=aqz-KE-bpKQ',
                    'ie_key': YoutubeIE.ie_key()}
        fixture = {'id': 'fixture', 'title': 'Fixture', 'is_live': True,
                   'url': 'https://cdn.example.test/live.m3u8', 'ext': 'mp4'}
        with patch.object(YoutubeTabIE, '_real_initialize'), patch.object(YoutubeIE, '_real_initialize'), \
             patch.object(YoutubeTabIE, '_real_extract', return_value=redirect), \
             patch.object(YoutubeIE, '_real_extract', return_value=fixture), \
             patch.object(YoutubeDL, 'urlopen', side_effect=AssertionError('Unexpected platform request')):
            self.assertTrue(extract_video('https://youtube.com/@example/live')['is_live'])

    def test_x_link_only_posts_and_external_cards_cannot_fetch_other_hosts(self):
        from yt_dlp.extractor.generic import GenericIE
        from yt_dlp.extractor.twitter import TwitterIE

        for target in ('http://127.0.0.1:8765/private', 'https://unsupported.example.test/video',
                       'https://youtube.com/watch?v=aqz-KE-bpKQ', 'https://t.co/fixture'):
            posts = [
                {'entities': {'urls': [{'expanded_url': target}]}},
                {'card': {'name': 'player', 'binding_values': {
                    'player_url': {'type': 'STRING', 'string_value': target}}}},
                {'card': {'name': 'broadcast', 'binding_values': {
                    'broadcast_url': {'type': 'STRING', 'string_value': target}}}},
            ]
            for post in posts:
                with self.subTest(target=target, card='card' in post), \
                     patch.object(TwitterIE, '_real_initialize'), \
                     patch.object(TwitterIE, '_extract_status', return_value=post), \
                     patch.object(GenericIE, '_real_extract', return_value={
                         'id': 'fixture', 'title': 'Fixture', 'url': 'https://cdn.example.test/video.mp4'}) as generic, \
                     patch.object(YoutubeDL, 'urlopen', side_effect=AssertionError('Unexpected external request')) as fetch, \
                     patch('sys.stderr', io.StringIO()), self.assertRaises(DownloadError):
                    extract_video('https://x.com/example/status/1234567890')
                generic.assert_not_called()
                fetch.assert_not_called()

    def test_x_broadcasts_and_post_cards_preserve_live_or_replay_playback(self):
        from yt_dlp.extractor.twitter import TwitterIE, TwitterBroadcastIE

        broadcast_id = '1AbCdEfGhIjKl'
        broadcast_url = f'https://x.com/i/broadcasts/{broadcast_id}'
        post = {'card': {'name': 'broadcast', 'binding_values': {
            'broadcast_url': {'type': 'STRING', 'string_value': broadcast_url}}}}
        formats = [{'url': 'https://cdn.example.test/video.m3u8', 'ext': 'mp4',
                    'vcodec': 'h264', 'acodec': 'aac', 'height': 720}]
        for state, live in (('RUNNING', True), ('ENDED', False)):
            responses = [
                {'broadcasts': {broadcast_id: {'id': broadcast_id, 'status': 'Fixture',
                                             'state': state, 'media_key': 'fixture-key'}}},
                {'source': {'noRedirectPlaybackUrl': formats[0]['url']}},
            ]
            for url in (broadcast_url, 'https://x.com/example/status/1234567890'):
                with self.subTest(state=state, url=url), \
                     patch.object(TwitterIE, '_real_initialize'), \
                     patch.object(TwitterIE, '_extract_status', return_value=copy.deepcopy(post)), \
                     patch.object(TwitterBroadcastIE, '_real_initialize'), \
                     patch.object(TwitterBroadcastIE, '_call_api', side_effect=copy.deepcopy(responses)), \
                     patch.object(TwitterBroadcastIE, '_extract_m3u8_formats', return_value=copy.deepcopy(formats)), \
                     patch.object(YoutubeDL, 'urlopen', side_effect=AssertionError('Unexpected platform request')):
                    try:
                        info = extract_video(url)
                    except (ValueError, DownloadError) as error:
                        self.fail(str(error))
                    self.assertEqual(info['is_live'], live)
                    self.assertEqual(info['live_status'], 'is_live' if live else 'was_live')
                    args = ffmpeg_args(info, Path('/tmp/hls'))
                    self.assertEqual('-re' in args, not live)
                    self.assertIn('Referer: https://www.periscope.tv/\r\n', args[args.index('-headers') + 1])

    def test_x_native_videos_and_numbered_video_selection_still_work(self):
        from yt_dlp.extractor.twitter import TwitterIE

        media = [{'id_str': str(index), 'type': 'video', 'video_info': {'variants': [
            {'url': f'https://video.twimg.com/fixture{index}.mp4',
             'content_type': 'video/mp4', 'bitrate': 1000000}]}} for index in (1, 2)]
        for suffix, entries, selected in (('', media[:1], 1), ('/video/2', media, 2)):
            with self.subTest(suffix=suffix), patch.object(TwitterIE, '_real_initialize'), \
                 patch.object(TwitterIE, '_extract_status', return_value={'extended_entities': {'media': entries}}), \
                 patch.object(YoutubeDL, 'urlopen', side_effect=AssertionError('Unexpected platform request')):
                info = extract_video('https://x.com/example/status/1234567890' + suffix)
                self.assertEqual(info['url'], f'https://video.twimg.com/fixture{selected}.mp4')

    def test_tiktok_live_public_api_formats_reach_the_live_encoder(self):
        from yt_dlp.extractor.tiktok import TikTokLiveIE

        room_id = '1234567890'
        live_info = {'data': {'status': 2, 'title': 'Fixture live', 'stream_url': {
            'hls_pull_url': 'https://cdn.example.test/live.m3u8',
            'hls_pull_url_params': '{"VCodec":"h264"}'}}}
        for url in ('https://tiktok.com/@example/live', f'https://m.tiktok.com/share/live/{room_id}'):
            with self.subTest(url=url), patch.object(TikTokLiveIE, '_download_webpage', return_value=''), \
                 patch.object(TikTokLiveIE, '_get_universal_data', return_value={
                     'webapp.user-detail': {'userInfo': {'user': {'roomId': room_id}}}}), \
                 patch.object(TikTokLiveIE, '_get_sigi_state', return_value={
                     'LiveRoom': {'liveRoomUserInfo': {'user': {'uniqueId': 'example'}}}}), \
                 patch.object(TikTokLiveIE, '_download_json', return_value=copy.deepcopy(live_info)), \
                 patch.object(YoutubeDL, 'urlopen', side_effect=AssertionError('Unexpected platform request')):
                info = extract_video(url)
                self.assertTrue(info['is_live'])
                self.assertEqual(info['live_status'], 'is_live')
                self.assertEqual(info['url'], live_info['data']['stream_url']['hls_pull_url'])
                self.assertEqual(info['uploader'], 'example')
                self.assertNotIn('-re', ffmpeg_args(info, Path('/tmp/hls')))

    def test_tiktok_offline_channel_fails_without_starting_playback(self):
        from yt_dlp.extractor.tiktok import TikTokLiveIE

        with patch.object(TikTokLiveIE, '_download_webpage', return_value=''), \
             patch.object(TikTokLiveIE, '_get_universal_data', return_value={
                 'webapp.user-detail': {'userInfo': {'user': {'roomId': '1234567890'}}}}), \
             patch.object(TikTokLiveIE, '_download_json', return_value={'data': {'status': 4}}), \
             patch.object(YoutubeDL, 'urlopen', side_effect=AssertionError('Unexpected platform request')), \
             patch('sys.stderr', io.StringIO()), self.assertRaises(DownloadError):
            extract_video('https://tiktok.com/@example/live')

    def test_platform_account_credentials_are_not_enabled(self):
        options = extraction_options('720')
        for key in ('cookiefile', 'cookiesfrombrowser', 'username', 'password', 'usenetrc'):
            self.assertFalse(options.get(key))


if __name__ == '__main__':
    unittest.main()
