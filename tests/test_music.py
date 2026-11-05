"""Public music resolution, native voice playback, and cleanup."""

import asyncio
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from aiohttp import web
from aiohttp.test_utils import TestServer
from yt_dlp import YoutubeDL
import discord
from discord.voice_state import ConnectionFlowState, VoiceConnectionState

from syncnest import bot, music, music_sources, stream
from syncnest.playback import PlaybackQueues
from tests.test_queue import FakeSession

SPOTIFY = 'https://open.spotify.com/track/4cOdK2wGLETKBW3PvgPWqT'
YOUTUBE = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'
TRACK = {'title': 'Never Gonna Give You Up', 'artists': ['Rick Astley'], 'duration': 213.573}


class MusicSourceTests(unittest.TestCase):
    def test_music_links_normalize_without_accepting_collections_or_spoofed_hosts(self):
        self.assertEqual(music.normalize_music_url(SPOTIFY + '?si=share'), SPOTIFY)
        self.assertEqual(music.normalize_music_url(SPOTIFY.replace('/track/', '/intl-de/track/')), SPOTIFY)
        self.assertEqual(music.normalize_music_url(YOUTUBE.replace('www.', 'music.')), YOUTUBE)
        for url in (SPOTIFY.replace('/track/', '/album/'), SPOTIFY.replace('/track/', '/playlist/'),
                    SPOTIFY.replace('.com/', '.com.evil.test/'), SPOTIFY.replace('.com/', '.com:8443/'),
                    SPOTIFY.replace('https://', 'https://user:pass@'), 'https://spotify.link/abc',
                    SPOTIFY.replace('/track/', '/track/../'), 'file:///etc/passwd'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                music.normalize_music_url(url)

    def test_spotify_metadata_is_bounded_and_only_reads_the_canonical_public_embed(self):
        entity = {'type': 'track', 'name': TRACK['title'], 'artists': [{'name': 'Rick Astley'}],
                  'duration': 213573, 'uri': 'spotify:track:4cOdK2wGLETKBW3PvgPWqT'}
        payload = {'props': {'pageProps': {'state': {'data': {'entity': entity}}}}}
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock()
        response.read.return_value = ('<script id="__NEXT_DATA__" type="application/json">'
                                      + json.dumps(payload) + '</script>').encode()
        downloader = Mock()
        downloader.urlopen.return_value = response
        self.assertEqual(music_sources.spotify_metadata(SPOTIFY, downloader), TRACK)
        self.assertEqual(downloader.urlopen.call_args.args[0].url,
                         SPOTIFY.replace('/track/', '/embed/track/'))
        self.assertLessEqual(response.read.call_args.args[0], 1048577)
        response.read.return_value = b'private or unavailable'
        with self.assertRaisesRegex(ValueError, 'Spotify'):
            music_sources.spotify_metadata(SPOTIFY, downloader)

    def test_spotify_match_checks_artist_title_duration_and_version(self):
        good = {'id': 'dQw4w9WgXcQ', 'title': 'Rick Astley - Never Gonna Give You Up (Official Video)',
                'channel': 'Rick Astley', 'duration': 213}
        candidates = [good | {'id': 'abcdefghijk', 'title': good['title'] + ' karaoke'},
                      good | {'id': 'abcdefghijl', 'duration': 900}, good]
        self.assertEqual(music_sources.choose_youtube_track(TRACK, candidates), YOUTUBE)
        for changes in ({'title': 'Someone Else - Another Song'}, {'duration': None},
                        {'title': 'Rick Astley - Never Gonna Give You Up live'},
                        {'id': '../../evil'}, {'is_live': True}):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, 'match'):
                music_sources.choose_youtube_track(TRACK, [good | changes])

    def test_matching_preserves_non_latin_track_and_artist_names(self):
        track = {'title': '春よ来い', 'artists': ['松任谷由実'], 'duration': 240}
        entry = {'id': 'abcdefghijk', 'title': '松任谷由実 - 春よ来い', 'duration': 240}
        self.assertEqual(music_sources.choose_youtube_track(track, [entry]),
                         'https://www.youtube.com/watch?v=abcdefghijk')

    def test_audio_selection_accepts_audio_only_formats_and_spotify_labels_youtube(self):
        audio = {'id': 'fixture', 'title': 'Track', 'url': 'https://cdn.example.test/audio.webm',
                 'vcodec': 'none', 'acodec': 'opus'}
        with patch.object(YoutubeDL, 'extract_info', return_value=audio):
            info = stream.extract_video(YOUTUBE.replace('www.', 'music.'), 'best', audio_only=True)
            self.assertEqual(info['acodec'], 'opus')
        search = {'entries': [{'id': 'dQw4w9WgXcQ', 'title': 'Rick Astley Never Gonna Give You Up',
                               'duration': 213}]}
        with patch('syncnest.stream.spotify_metadata', return_value=TRACK), \
             patch.object(YoutubeDL, 'extract_info', side_effect=[search, audio]) as extract:
            info = stream.extract_video(SPOTIFY, 'best', audio_only=True)
        self.assertEqual(info['spotify_title'], 'Rick Astley - Never Gonna Give You Up')
        self.assertEqual(info['matched_url'], YOUTUBE)
        self.assertTrue(extract.call_args_list[0].args[0].startswith('ytsearch5:'))
        self.assertEqual(extract.call_args_list[1].args[0], YOUTUBE)
        self.assertEqual(stream.extraction_options('best', audio_only=True)['format'], 'bestaudio/best')

    def test_audio_attachments_keep_fixed_decoders_and_video_mode_still_rejects_them(self):
        for extension, demuxer in stream.AUDIO_FORMATS.items():
            url = 'https://cdn.discordapp.com/attachments/100/200/song' + extension
            with self.subTest(extension=extension):
                info = stream.extract_video(url, 'best', audio_only=True)
                args = stream.ffmpeg_input_args(info)
                self.assertEqual(args[args.index('-f') + 1], demuxer)
                self.assertEqual(args[args.index('-protocol_whitelist') + 1], 'http,https,tcp,tls,httpproxy')
                with self.assertRaises(ValueError):
                    stream.normalize_video_url(url)


class FakeAudio:
    def __init__(self, info):
        self.started = asyncio.get_running_loop().create_future()
        self.cleaned = False

    def cleanup(self):
        self.cleaned = True


class MusicLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.manager = music.MusicRooms()
        self.guild = SimpleNamespace(id=1, voice_client=None)
        self.channel = SimpleNamespace(id=42, guild=self.guild, connect=AsyncMock())
        self.voice = SimpleNamespace(channel=self.channel, stop=Mock(), disconnect=AsyncMock())

        async def connect(**kwargs):
            self.guild.voice_client = self.voice
            return self.voice

        def play(source, after):
            self.voice.source, self.voice.after = source, after
            source.started.set_result(None)

        self.channel.connect.side_effect = connect
        self.voice.play = Mock(side_effect=play)
        self.extract = patch('syncnest.stream.extract_video_async', AsyncMock(return_value={
            'url': 'https://cdn.example.test/audio.webm', 'title': 'Track', 'acodec': 'opus'}))
        self.extract.start()
        self.audio = patch('syncnest.music.VoiceAudio', FakeAudio)
        self.audio.start()
        self.addCleanup(self.extract.stop)
        self.addCleanup(self.audio.stop)

    async def asyncTearDown(self):
        await self.manager.close()

    async def started(self):
        session = self.manager.prepare(self.channel, YOUTUBE)
        await asyncio.wait_for(session.ready.wait(), 2)
        self.assertEqual(session.status, 'playing', session.error)
        return session

    async def test_music_joins_voice_and_finish_disconnects_and_cleans_source(self):
        session = await self.started()
        self.channel.connect.assert_awaited_once_with(cls=music.MusicVoiceClient, timeout=20,
                                                    reconnect=True, self_deaf=True)
        thread = threading.Thread(target=self.voice.after, args=(None,))
        thread.start()
        thread.join()
        await asyncio.wait_for(session.task, 2)
        self.assertTrue(self.voice.source.cleaned)
        self.voice.disconnect.assert_awaited_once_with(force=True)
        self.assertIsNone(self.manager.get(42))

    async def test_second_channel_in_same_guild_is_busy_but_another_guild_can_play(self):
        await self.started()
        other = SimpleNamespace(id=77, guild=self.guild)
        with self.assertRaisesRegex(ValueError, 'busy|already'):
            self.manager.prepare(other, YOUTUBE)
        another = SimpleNamespace(id=99, guild=SimpleNamespace(id=2, voice_client=None),
                                  connect=AsyncMock(return_value=self.voice))
        second = self.manager.prepare(another, YOUTUBE)
        await second.stop()
        self.assertIsNotNone(self.manager.get(42))

    async def test_stop_is_channel_scoped_and_disconnects_without_restarting(self):
        session = await self.started()
        self.assertFalse(await self.manager.stop(77))
        self.assertTrue(await self.manager.stop(42))
        self.assertTrue(session.task.done())
        self.assertTrue(self.voice.source.cleaned)
        self.voice.disconnect.assert_awaited_once()

    async def test_stop_during_extraction_does_not_join_voice_later(self):
        blocked = asyncio.Event()

        async def extract(*args, **kwargs):
            blocked.set()
            await asyncio.Future()

        with patch('syncnest.stream.extract_video_async', side_effect=extract):
            session = self.manager.prepare(self.channel, YOUTUBE)
            await blocked.wait()
            await self.manager.stop(42)
        self.channel.connect.assert_not_awaited()
        self.assertIsNone(self.manager.get(42))
        self.assertTrue(session.ready.is_set())

    async def test_stop_during_voice_handshake_disconnects_partial_connection(self):
        entered = asyncio.Event()

        async def connect(**kwargs):
            self.guild.voice_client = self.voice
            entered.set()
            await asyncio.Future()

        self.channel.connect.side_effect = connect
        self.manager.prepare(self.channel, YOUTUBE)
        await entered.wait()
        await self.manager.stop(42)
        self.voice.disconnect.assert_awaited_once_with(force=True)

    async def test_music_shares_video_capacity_and_releases_it_on_stop(self):
        with patch('syncnest.stream.MAX_STREAMS', 1):
            await self.started()
            with self.assertRaisesRegex(ValueError, 'busy'):
                stream.Rooms().prepare(77, YOUTUBE)
            await self.manager.stop(42)
            room = stream.Rooms().prepare(77, YOUTUBE)
            await room.stop()

    async def test_cancelled_stop_keeps_disconnection_and_cleanup_running(self):
        entered, release = asyncio.Event(), asyncio.Event()

        async def disconnect(**kwargs):
            entered.set()
            await release.wait()

        self.voice.disconnect.side_effect = disconnect
        session = await self.started()
        stopping = asyncio.create_task(self.manager.stop(42))
        try:
            await entered.wait()
            stopping.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stopping
            self.assertFalse(session.task.done())
            release.set()
            await asyncio.wait_for(asyncio.gather(session.task, return_exceptions=True), 2)
            self.assertTrue(self.voice.source.cleaned)
            self.assertIsNone(self.manager.get(42))
        finally:
            release.set()
            await asyncio.gather(stopping, return_exceptions=True)

    async def test_encoder_failure_disconnects_and_releases_the_session(self):
        with patch('syncnest.music.VoiceAudio', side_effect=ValueError('No audio found.')):
            session = self.manager.prepare(self.channel, YOUTUBE)
            await asyncio.wait_for(session.ready.wait(), 2)
        self.assertEqual(session.status, 'error')
        self.voice.disconnect.assert_awaited_once_with(force=True)
        await session.task
        self.assertIsNone(self.manager.get(42))


class VoiceDisconnectTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_finishes_pending_reconnect_before_releasing_the_client(self):
        voice = object.__new__(music.MusicVoiceClient)
        voice._player = None
        voice.channel = SimpleNamespace(_get_voice_client_key=lambda: (1, None))
        voice.client = SimpleNamespace(_connection=SimpleNamespace(_remove_voice_client=Mock()))
        state = object.__new__(VoiceConnectionState)
        voice._connection, state.voice_client = state, voice
        state._state = ConnectionFlowState.connected
        state._connected = threading.Event()
        state._state_event, state._disconnected = asyncio.Event(), asyncio.Event()
        state._expecting_disconnect = False
        state._socket_reader, state.socket = Mock(), Mock()
        state.ws = SimpleNamespace(poll_event=AsyncMock(side_effect=asyncio.TimeoutError), close=AsyncMock())
        state._voice_disconnect = AsyncMock(side_effect=state._disconnected.set)
        state._connect = AsyncMock()
        state._connector = None
        state.timeout = 1
        backoff = asyncio.Event()

        def delay(_):
            backoff.set()
            return 60

        with patch('discord.voice_state.ExponentialBackoff.delay', delay), \
             self.assertLogs('discord.voice_state', level='ERROR'):
            state._runner = asyncio.create_task(state._poll_voice_ws(reconnect=True))
            try:
                await asyncio.wait_for(backoff.wait(), 1)
                await voice.disconnect(force=True)
                self.assertTrue(state._runner.done(), 'A stopped client still has a pending reconnect.')
                state._connect.assert_not_awaited()
                voice.client._connection._remove_voice_client.assert_called_with(1)
            finally:
                state._runner.cancel()
                await asyncio.gather(state._runner, return_exceptions=True)


class MusicCommandTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id = 42
        self.channel.guild = SimpleNamespace(id=1, me=Mock(), voice_client=None)
        self.channel.members = [SimpleNamespace(bot=False)]
        self.channel.permissions_for.return_value = SimpleNamespace(connect=True, speak=True, create_instant_invite=True, move_members=False)
        self.member = Mock(spec=discord.Member)
        self.member.voice = SimpleNamespace(channel=self.channel)
        self.interaction = SimpleNamespace(user=self.member, channel_id=42,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(), launch_activity=AsyncMock()),
            edit_original_response=AsyncMock())
        self.session = FakeSession()
        self.music = SimpleNamespace(get=Mock(return_value=None),
            prepare=Mock(return_value=self.session), stop=AsyncMock(return_value=False))
        self.rooms = SimpleNamespace(channels={}, prepare=Mock(), change_resolution=AsyncMock())

        def video(channel_id, url, resolution):
            session = FakeSession()
            self.rooms.channels[channel_id] = session
            return session

        self.rooms.prepare.side_effect = video
        for target, value in (('music', self.music), ('rooms', self.rooms)):
            handle = patch.object(bot, target, value)
            handle.start()
            self.addCleanup(handle.stop)
        handle = patch.object(bot.bot, 'voice_states_ready', True)
        handle.start()
        self.addCleanup(handle.stop)
        self.queues = PlaybackQueues(self.rooms, self.music)
        handle = patch.object(bot, 'playback', self.queues)
        handle.start()
        self.addCleanup(handle.stop)
        self.addAsyncCleanup(self.queues.close)
        self.addAsyncCleanup(self.session.stop)

    async def test_music_links_and_explicit_mode_play_through_voice_without_activity(self):
        for url, mode in ((YOUTUBE, 'music'), (YOUTUBE.replace('www.', 'music.'), None), (SPOTIFY, None)):
            with self.subTest(url=url):
                self.music.prepare.reset_mock()
                await bot.play.callback(self.interaction, url=url, mode=mode)
                self.music.prepare.assert_called_once_with(self.channel, music.normalize_music_url(url))
                self.assertIn('Playing', self.interaction.edit_original_response.call_args.kwargs['content'])
                await self.queues.pause(42)
                self.session = FakeSession()
                self.music.prepare.return_value = self.session
                self.addAsyncCleanup(self.session.stop)
        self.interaction.response.launch_activity.assert_not_awaited()
        self.rooms.prepare.assert_not_called()

    async def test_spotify_reply_explicitly_identifies_the_youtube_match(self):
        self.session.spotify_title, self.session.matched_url = 'Artist - Song', YOUTUBE
        await bot.play.callback(self.interaction, url=SPOTIFY)
        reply = self.interaction.edit_original_response.call_args.kwargs['content']
        self.assertIn('Spotify', reply)
        self.assertIn('YouTube', reply)
        self.assertIn(YOUTUBE, reply)

    async def test_music_requires_voice_permissions_and_queues_behind_video(self):
        self.channel.permissions_for.return_value = SimpleNamespace(connect=True, speak=False, create_instant_invite=True, move_members=False)
        await bot.play.callback(self.interaction, url=YOUTUBE, mode='music')
        self.music.prepare.assert_not_called()
        self.assertIn('Speak', self.interaction.response.send_message.call_args.args[0])
        self.channel.permissions_for.return_value = SimpleNamespace(connect=True, speak=True, create_instant_invite=True, move_members=False)
        await bot.play.callback(self.interaction, url=YOUTUBE)
        await bot.play.callback(self.interaction, url=YOUTUBE, mode='music')
        self.music.prepare.assert_not_called()
        self.assertEqual(self.queues.get(42).items[0].mode, 'music')

    async def test_music_never_starts_for_a_caller_who_left_during_defer(self):
        async def leave(**kwargs):
            self.member.voice = None
        self.interaction.response.defer.side_effect = leave
        await bot.play.callback(self.interaction, url=YOUTUBE, mode='music')
        self.music.prepare.assert_not_called()

    async def test_active_music_queues_video_and_blocks_resolution_and_stop_disconnects(self):
        await bot.play.callback(self.interaction, url=YOUTUBE, mode='music')
        self.music.get.return_value = self.session
        await bot.play.callback(self.interaction, url=YOUTUBE)
        self.rooms.prepare.assert_not_called()
        await bot.resolution_command.callback(self.interaction, '1080')
        self.rooms.change_resolution.assert_not_awaited()
        await bot.stop.callback(self.interaction)
        self.assertTrue(self.session.task.done())
        self.assertTrue(self.queues.get(42).paused)
        self.assertIn('/resume', self.interaction.edit_original_response.call_args.kwargs['content'])

    async def test_last_human_leaving_stops_music_too(self):
        self.channel.members = [SimpleNamespace(bot=True)]
        await bot.bot.on_voice_state_update(self.member, SimpleNamespace(channel=self.channel),
                                           SimpleNamespace(channel=None))
        self.music.stop.assert_awaited_once_with(42)

    async def test_bot_removed_from_voice_stops_its_music_session(self):
        self.channel.members = [SimpleNamespace(bot=False)]
        self.session.cleaning = False
        self.music.get.return_value = self.session
        with patch.object(bot.bot._connection, 'user', SimpleNamespace(id=100)):
            await bot.bot.on_voice_state_update(SimpleNamespace(id=100),
                SimpleNamespace(channel=self.channel), SimpleNamespace(channel=None))
        self.music.stop.assert_awaited_once_with(42)

    async def test_audio_attachment_can_choose_music_without_a_mime_type(self):
        file = SimpleNamespace(url='https://cdn.discordapp.com/attachments/100/200/song.mp3',
                               content_type=None, size=100)
        await bot.play.callback(self.interaction, file=file)
        self.music.prepare.assert_called_once_with(self.channel, file.url)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class VoiceEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_audio_produces_opus_packets_and_cleanup_reaps_ffmpeg(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'song.wav'
            process = await asyncio.create_subprocess_exec('ffmpeg', '-v', 'error', '-f', 'lavfi',
                '-i', 'sine=frequency=440:sample_rate=48000', '-t', '1', str(path))
            self.assertEqual(await process.wait(), 0)
            app = web.Application()
            async def serve(request):
                return web.FileResponse(path)
            app.router.add_get('/song.wav', serve)
            async with TestServer(app) as server:
                source = music.VoiceAudio({'url': str(server.make_url('/song.wav')), '_input_format': 'wav'})
                encoder = source._process
                try:
                    packet = await asyncio.wait_for(asyncio.to_thread(source.read), 5)
                    self.assertTrue(packet)
                    self.assertFalse(packet.startswith((b'OpusHead', b'OpusTags')))
                    self.assertTrue(source.is_opus())
                    await asyncio.wait_for(source.started, 1)
                    while await asyncio.wait_for(asyncio.to_thread(source.read), 5):
                        pass
                    await asyncio.wait_for(asyncio.to_thread(encoder.wait), 5)
                finally:
                    await asyncio.to_thread(source.cleanup)
                self.assertIsNotNone(encoder.poll())
                self.assertTrue(encoder.stdout.closed)
