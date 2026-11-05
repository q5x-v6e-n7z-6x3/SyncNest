"""Offline command and voice lifecycle regressions; no Discord credentials needed."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import discord

from syncnest import bot
from syncnest.playback import PlaybackQueues
from tests.test_queue import FakeSession

URL = 'https://youtu.be/dQw4w9WgXcQ'


class BotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id = 42
        self.channel.members = [SimpleNamespace(bot=False)]
        self.channel.guild = SimpleNamespace(id=1, voice_client=None, me=object())
        self.member = Mock(spec=discord.Member)
        self.member.voice = SimpleNamespace(channel=self.channel)
        self.interaction = SimpleNamespace(
            user=self.member, channel_id=42,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                     launch_activity=AsyncMock()),
            edit_original_response=AsyncMock())
        self.rooms = SimpleNamespace(channels={}, prepare=Mock())

        def prepare(channel_id, url, resolution):
            session = FakeSession()
            self.rooms.channels[channel_id] = session
            return session

        self.rooms.prepare.side_effect = prepare
        self.patch = patch('syncnest.bot.rooms', self.rooms)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        readiness = patch.object(bot.bot, 'voice_states_ready', True, create=True)
        readiness.start()
        self.addCleanup(readiness.stop)
        self.playback = PlaybackQueues(self.rooms, bot.music)
        queue_patch = patch.object(bot, 'playback', self.playback)
        queue_patch.start()
        self.addCleanup(queue_patch.stop)
        self.addAsyncCleanup(self.playback.close)

    async def test_voice_states_are_ready_only_after_ready_or_resumed(self):
        client = bot.SyncNestBot()
        self.assertFalse(client.voice_states_ready)
        await client.on_ready()
        self.assertTrue(client.voice_states_ready)
        await client.on_disconnect()
        self.assertFalse(client.voice_states_ready)
        await client.on_resumed()
        self.assertTrue(client.voice_states_ready)
        await client.on_disconnect()
        await client.on_ready()
        self.assertTrue(client.voice_states_ready)

    async def test_new_gateway_session_pauses_queues_whose_channel_emptied(self):
        await bot.play.callback(self.interaction, url=URL)
        room = self.rooms.channels[42]
        with patch.object(bot.bot, 'get_channel', return_value=self.channel):
            await bot.bot.on_ready()
            self.assertFalse(room.cleaned)
            self.channel.members = []  # Everyone left while the gateway was down.
            await bot.bot.on_ready()
        self.assertTrue(room.cleaned)
        self.assertIsNone(self.playback.get(42))

    async def test_stop_after_video_finished_reports_nothing_playing(self):
        await bot.play.callback(self.interaction, url=URL)
        self.rooms.channels[42].finish()
        async with asyncio.timeout(1):
            while self.playback.get(42):
                await asyncio.sleep(0)
        await bot.stop.callback(self.interaction)
        self.assertEqual(self.interaction.edit_original_response.call_args.kwargs['content'], 'Nothing is playing.')

    async def test_busy_music_play_points_to_resume_instead_of_a_duplicate_play(self):
        with patch('syncnest.stream.MAX_EXTRACTIONS', 0):
            await bot.play.callback(self.interaction, url=URL, mode='music')
            reply = self.interaction.edit_original_response.call_args.kwargs['content']
            self.assertIn('/resume', reply)
            self.assertNotIn('Try again', reply)
            self.assertEqual(len(self.playback.get(42).items), 1)

    async def test_voice_commands_reject_membership_during_gateway_reconnect(self):
        bot.bot.voice_states_ready = False
        with self.assertRaisesRegex(ValueError, 'reconnecting'):
            bot.voice_channel(self.interaction)

    async def test_help_is_private_and_works_without_voice(self):
        self.interaction.user = SimpleNamespace()
        await bot.help_command.callback(self.interaction)
        args, kwargs = self.interaction.response.send_message.call_args
        self.assertTrue(kwargs['ephemeral'])
        self.assertLessEqual(len(args[0]), 2000)
        for name in ['SyncNest', '/play', '/stop', '/help', 'YouTube', 'X', 'Instagram', 'TikTok',
                     'Vimeo', 'Kick', 'Rumble', 'Public links only', 'X videos/live broadcasts', '/play file:']:
            self.assertIn(name, args[0])
        for removed in ('Facebook', 'Instagram Live'):
            self.assertNotIn(removed, args[0])
        self.rooms.prepare.assert_not_called()

    async def test_play_rejects_missing_voice_stage_and_invalid_urls(self):
        for user, url in [(SimpleNamespace(), URL), (self.member, 'https://evil.test/v'),
                          (Mock(spec=discord.Member, voice=None), URL),
                          (Mock(spec=discord.Member, voice=SimpleNamespace(
                              channel=Mock(spec=discord.StageChannel))), URL)]:
            self.interaction.user = user
            await bot.play.callback(self.interaction, url)
            self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])
        self.rooms.prepare.assert_not_called()
        self.interaction.response.launch_activity.assert_not_awaited()

    async def test_busy_room_queues_next_item_without_replacing_current(self):
        await bot.play.callback(self.interaction, URL)
        current = self.rooms.channels[42]
        self.rooms.prepare.reset_mock()
        await bot.play.callback(self.interaction, URL)
        self.rooms.prepare.assert_not_called()
        self.assertIs(self.rooms.channels[42], current)
        self.assertEqual(len(self.playback.get(42).items), 1)
        self.assertIn('Queued', self.interaction.response.send_message.call_args.args[0])

    async def test_play_passes_resolution_from_choice_and_default(self):
        for resolution in ['best', '2160', '1080', '720']:
            with self.subTest(resolution=resolution):
                self.rooms.prepare.reset_mock()
                await bot.play.callback(self.interaction, URL, resolution)
                self.rooms.prepare.assert_called_once_with(42,
                    'https://www.youtube.com/watch?v=dQw4w9WgXcQ', resolution)
                await self.playback.pause(42)
        self.rooms.prepare.reset_mock()
        await bot.play.callback(self.interaction, URL)
        self.assertEqual(self.rooms.prepare.call_args.args[2], '720')

    async def test_play_attachment_uses_the_same_voice_launch_and_text_invite(self):
        url = 'https://cdn.discordapp.com/ephemeral-attachments/100/200/video.mp4?ex=ffffffff&hm=fixture'
        file = SimpleNamespace(url=url, size=1000, content_type='video/mp4')
        self.channel.create_invite = AsyncMock(return_value=SimpleNamespace(url='https://discord.gg/fixture'))
        for channel_id in (42, 99):
            with self.subTest(channel_id=channel_id):
                self.interaction.channel_id = channel_id
                self.rooms.prepare.reset_mock()
                await bot.play.callback(self.interaction, resolution='1080', file=file)
                self.rooms.prepare.assert_called_once_with(42, url, '1080')
                await self.playback.pause(42)
        self.interaction.response.launch_activity.assert_awaited_once()
        self.channel.create_invite.assert_awaited_once()

    async def test_play_requires_exactly_one_url_or_attachment(self):
        file = SimpleNamespace(url='https://cdn.discordapp.com/attachments/100/200/video.mp4',
                               size=1000, content_type='video/mp4')
        for kwargs in ({}, {'url': URL, 'file': file}):
            with self.subTest(kwargs=list(kwargs)):
                await bot.play.callback(self.interaction, **kwargs)
                self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])
        self.rooms.prepare.assert_not_called()
        self.interaction.response.launch_activity.assert_not_awaited()

    async def test_play_rejects_empty_nonvideo_and_untrusted_attachments_privately(self):
        url = 'https://cdn.discordapp.com/attachments/100/200/video.mp4'
        for file in (SimpleNamespace(url=url, size=0, content_type='video/mp4'),
                     SimpleNamespace(url=url, size=1000, content_type='text/html'),
                     SimpleNamespace(url='file:///etc/passwd', size=1000, content_type='video/mp4')):
            with self.subTest(url=file.url, size=file.size):
                await bot.play.callback(self.interaction, file=file)
                self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])
        self.rooms.prepare.assert_not_called()

    async def test_play_rejects_invalid_resolution_privately(self):
        await bot.play.callback(self.interaction, URL, 'invalid')
        self.rooms.prepare.assert_not_called()
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])

    async def test_resolution_command_uses_callers_voice_channel(self):
        previous = SimpleNamespace(resolution='720', is_live=False)
        self.rooms.channels[42] = previous
        self.rooms.change_resolution = AsyncMock(return_value=SimpleNamespace(start_time=120.5))
        await bot.resolution_command.callback(self.interaction, '1080')
        self.rooms.change_resolution.assert_awaited_once_with(42, '1080')
        self.interaction.response.defer.assert_awaited_once_with(thinking=True, ephemeral=True)
        message = self.interaction.edit_original_response.call_args.kwargs['content']
        self.assertIn('1080p', message)
        self.assertIn('120 seconds', message)

    async def test_resolution_command_handles_missing_video_and_voice(self):
        self.rooms.change_resolution = AsyncMock(side_effect=ValueError('Nothing is playing. Use /play first.'))
        await bot.resolution_command.callback(self.interaction, '1080')
        self.assertIn('/play', self.interaction.edit_original_response.call_args.kwargs['content'])
        self.interaction.user = SimpleNamespace()
        self.rooms.change_resolution.reset_mock()
        await bot.resolution_command.callback(self.interaction, '1080')
        self.rooms.change_resolution.assert_not_awaited()
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])

    async def test_resolution_command_rejects_invalid_quality(self):
        self.rooms.change_resolution = AsyncMock()
        await bot.resolution_command.callback(self.interaction, 'invalid')
        self.rooms.change_resolution.assert_not_awaited()

    async def test_resolution_command_already_selected_does_not_claim_reload(self):
        current = SimpleNamespace(resolution='1080', is_live=False)
        self.rooms.channels[42] = current
        self.rooms.change_resolution = AsyncMock(return_value=current)
        await bot.resolution_command.callback(self.interaction, '1080')
        self.assertEqual(self.interaction.edit_original_response.call_args.kwargs['content'],
                         'The video is already set to 1080p.')

    async def test_resolution_does_not_change_if_caller_leaves_or_moves_during_defer(self):
        self.rooms.channels[42] = SimpleNamespace(resolution='720', is_live=False)
        self.rooms.change_resolution = AsyncMock(return_value=SimpleNamespace(start_time=0))
        other_channel = Mock(spec=discord.VoiceChannel)
        other_channel.id = 77
        for voice in [None, SimpleNamespace(channel=other_channel)]:
            with self.subTest(voice=voice):
                self.member.voice = SimpleNamespace(channel=self.channel)
                self.rooms.change_resolution.reset_mock()

                async def leave(**kwargs):
                    self.member.voice = voice

                self.interaction.response.defer.side_effect = leave
                await bot.resolution_command.callback(self.interaction, '1080')
                self.rooms.change_resolution.assert_not_awaited()
                message = self.interaction.edit_original_response.call_args.kwargs['content']
                self.assertIn('voice channel', message)

    async def test_launch_failure_stops_prepared_room(self):
        room = FakeSession()
        room.stop = AsyncMock(wraps=room.stop)
        self.rooms.prepare.side_effect = None
        self.rooms.prepare.return_value = room
        self.interaction.response.launch_activity.side_effect = discord.HTTPException(
            SimpleNamespace(status=403, reason='Forbidden'), 'No permission')
        await bot.play.callback(self.interaction, URL)
        room.stop.assert_awaited_once()
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])

    async def test_text_channel_invite_failure_stops_prepared_room(self):
        self.interaction.channel_id = 99
        room = FakeSession()
        room.stop = AsyncMock(wraps=room.stop)
        self.rooms.prepare.side_effect = None
        self.rooms.prepare.return_value = room
        self.channel.create_invite = AsyncMock(side_effect=discord.HTTPException(
            SimpleNamespace(status=403, reason='Forbidden'), 'No permission'))
        await bot.play.callback(self.interaction, URL)
        room.stop.assert_awaited_once()
        self.assertIn('Create Invite', self.interaction.edit_original_response.call_args.kwargs['content'])

    async def test_play_does_not_start_if_caller_leaves_or_moves_during_defer(self):
        self.interaction.channel_id = 99
        self.channel.create_invite = AsyncMock()
        other_channel = Mock(spec=discord.VoiceChannel)
        other_channel.id = 77
        for voice in [None, SimpleNamespace(channel=other_channel)]:
            with self.subTest(voice=voice):
                self.member.voice = SimpleNamespace(channel=self.channel)

                async def leave(**kwargs):
                    self.member.voice = voice

                self.interaction.response.defer.side_effect = leave
                await bot.play.callback(self.interaction, URL)
                self.rooms.prepare.assert_not_called()
                self.channel.create_invite.assert_not_awaited()
                self.interaction.edit_original_response.assert_awaited()

    async def test_capacity_error_is_reported_for_voice_and_text_commands(self):
        self.rooms.prepare.side_effect = ValueError('The video server is busy. Try again shortly.')
        for channel_id in (42, 99):
            with self.subTest(channel_id=channel_id):
                self.interaction.channel_id = channel_id
                await bot.play.callback(self.interaction, URL)
                if channel_id == 42:
                    self.assertIn('busy', self.interaction.response.send_message.call_args.args[0])
                    self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])
                else:
                    self.assertIn('busy', self.interaction.edit_original_response.call_args.kwargs['content'])

    async def test_another_member_cannot_stop_a_present_requesters_item(self):
        self.member.id = 1
        self.channel.permissions_for.return_value = SimpleNamespace(
            connect=True, speak=True, create_instant_invite=True, move_members=False)
        self.channel.members = [SimpleNamespace(id=1, bot=False), SimpleNamespace(id=2, bot=False)]
        await bot.play.callback(self.interaction, URL)
        other = Mock(spec=discord.Member)
        other.id, other.voice = 2, SimpleNamespace(channel=self.channel)
        interaction = SimpleNamespace(user=other, channel_id=42, edit_original_response=AsyncMock(),
                                      response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock()))
        await bot.stop.callback(interaction)
        self.assertIn('Only the person who added', interaction.edit_original_response.call_args.kwargs['content'])
        self.assertIsNotNone(self.playback.get(42).current_item)
        await bot.stop.callback(self.interaction)
        self.assertIsNone(self.playback.get(42))

    async def test_requesters_without_create_invite_get_no_server_invite(self):
        self.channel.permissions_for.return_value = SimpleNamespace(
            connect=True, speak=True, create_instant_invite=False, move_members=False)
        self.channel.create_invite = AsyncMock()
        self.interaction.channel_id = 99
        await bot.play.callback(self.interaction, URL)
        self.channel.create_invite.assert_not_awaited()
        self.assertNotIn('view', self.interaction.edit_original_response.call_args.kwargs)
        self.assertFalse(self.playback.get(42).current_item.invite)

    async def test_stop_only_affects_current_voice_channel(self):
        current, other = SimpleNamespace(stop=AsyncMock(), active=True), SimpleNamespace(stop=AsyncMock(), active=True)
        self.rooms.channels = {42: current, 99: other}
        await bot.stop.callback(self.interaction)
        current.stop.assert_awaited_once()
        other.stop.assert_not_awaited()

    async def test_stop_does_not_stop_if_caller_leaves_or_moves_during_defer(self):
        current, other = SimpleNamespace(stop=AsyncMock(), active=True), SimpleNamespace(stop=AsyncMock(), active=True)
        self.rooms.channels = {42: current, 77: other}
        other_channel = Mock(spec=discord.VoiceChannel)
        other_channel.id = 77
        for voice in [None, SimpleNamespace(channel=other_channel)]:
            with self.subTest(voice=voice):
                self.member.voice = SimpleNamespace(channel=self.channel)
                current.stop.reset_mock()

                async def leave(**kwargs):
                    self.member.voice = voice

                self.interaction.response.defer.side_effect = leave
                await bot.stop.callback(self.interaction)
                current.stop.assert_not_awaited()
                other.stop.assert_not_awaited()
                message = self.interaction.edit_original_response.call_args.kwargs['content']
                self.assertIn('voice channel', message)

    async def test_stop_without_room_or_voice_is_handled(self):
        await bot.stop.callback(self.interaction)
        self.assertEqual(self.interaction.edit_original_response.call_args.kwargs['content'], 'Nothing is playing.')
        self.interaction.user = SimpleNamespace()
        await bot.stop.callback(self.interaction)
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])

    async def test_last_human_leaving_stops_even_if_bots_remain(self):
        room = SimpleNamespace(stop=AsyncMock(), active=True)
        self.rooms.channels[42] = room
        for members in [[], [SimpleNamespace(bot=True)]]:
            self.channel.members = members
            await bot.bot.on_voice_state_update(self.member, SimpleNamespace(channel=self.channel),
                                               SimpleNamespace(channel=None))
        self.assertEqual(room.stop.await_count, 2)

    async def test_remaining_humans_and_same_channel_updates_keep_playing(self):
        room = SimpleNamespace(stop=AsyncMock(), active=True)
        self.rooms.channels[42] = room
        self.channel.members = [SimpleNamespace(bot=False)]
        await bot.bot.on_voice_state_update(self.member, SimpleNamespace(channel=self.channel),
                                           SimpleNamespace(channel=None))
        self.channel.members = []
        await bot.bot.on_voice_state_update(self.member, SimpleNamespace(channel=self.channel),
                                           SimpleNamespace(channel=self.channel))
        room.stop.assert_not_awaited()


class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_credentials_leave_the_environment_that_subprocesses_inherit(self):
        with patch.dict(os.environ, {'DISCORD_TOKEN': 'your_token', 'DISCORD_CLIENT_SECRET': 'your_secret'}), \
                patch('syncnest.bot.load_dotenv'):
            with self.assertRaises(SystemExit):
                await bot.main()
            self.assertNotIn('DISCORD_TOKEN', os.environ)
            self.assertNotIn('DISCORD_CLIENT_SECRET', os.environ)

    async def test_media_clients_use_the_egress_proxy_before_discord_login(self):
        seen = {}

        async def login(token):
            seen.update({name: os.environ.get(name) for name in ('http_proxy', 'HTTPS_PROXY', 'no_proxy')})
            raise discord.LoginFailure

        with tempfile.TemporaryDirectory() as directory, \
                patch.dict(os.environ, {'DISCORD_TOKEN': 'token', 'DISCORD_CLIENT_SECRET': 'secret',
                                        'no_proxy': '*'}), \
                patch('syncnest.bot.load_dotenv'), patch('syncnest.bot.shutil.which', return_value='/bin/x'), \
                patch('syncnest.bot.supports_readrate_catchup'), patch('syncnest.bot.WEB', Path(directory)), \
                patch.object(bot.bot, 'login', side_effect=login):
            (Path(directory) / 'dist').mkdir()
            (Path(directory) / 'dist' / 'app.js').write_text('')
            with self.assertRaises(discord.LoginFailure):
                await bot.main()
        self.assertTrue(seen['http_proxy'].startswith('http://127.0.0.1:'))
        self.assertEqual(seen['HTTPS_PROXY'], seen['http_proxy'])
        self.assertIsNone(seen['no_proxy'])


if __name__ == '__main__':
    unittest.main()
