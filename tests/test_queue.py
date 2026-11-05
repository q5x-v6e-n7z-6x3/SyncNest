"""Mixed playback order, queue controls, and media cleanup races."""

import asyncio
import shutil
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from collections import deque
from unittest.mock import ANY, AsyncMock, Mock, patch

import discord
from discord import app_commands
from aiohttp import web
from aiohttp.test_utils import TestServer

from syncnest import playback

VIDEO = 'https://www.youtube.com/watch?v=dQw4w9WgXcQ'


class FakeSession:
    def __init__(self):
        self.title, self.status, self.error = 'Fixture', 'playing', None
        self.spotify_title = self.matched_url = None
        self.ready = asyncio.Event()
        self.ready.set()
        self.end = asyncio.get_running_loop().create_future()
        self.task = asyncio.create_task(self.run())
        self.cleaned = False

    @property
    def active(self):
        return not self.task.done()

    async def run(self):
        try:
            await self.end
        finally:
            self.cleaned = True

    def finish(self, error=None):
        self.status, self.error = ('error', error) if error else ('finished', None)
        if not self.end.done():
            self.end.set_result(None)

    async def stop(self):
        self.status = 'stopped'
        self.task.cancel()
        await asyncio.gather(self.task, return_exceptions=True)


class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.started = []
        self.rooms = SimpleNamespace(channels={}, prepare=Mock())
        self.music = SimpleNamespace(prepare=Mock(), get=Mock(return_value=None), stop=AsyncMock())
        self.guild = SimpleNamespace(id=1, voice_client=None, me=object())
        self.channel = SimpleNamespace(id=42, guild=self.guild, members=[SimpleNamespace(bot=False)],
            permissions_for=lambda _: SimpleNamespace(connect=True, speak=True))

        def video(channel_id, url, resolution):
            session = FakeSession()
            self.rooms.channels[channel_id] = session
            self.started.append(('video', url, session))
            return session

        def music(channel, url):
            session = FakeSession()
            self.started.append(('music', url, session))
            return session

        self.rooms.prepare.side_effect, self.music.prepare.side_effect = video, music
        self.notices = AsyncMock()
        self.queues = playback.PlaybackQueues(self.rooms, self.music, notify=self.notices)

    async def asyncTearDown(self):
        await self.queues.close()

    def add(self, mode='video', *, channel=None, url=VIDEO):
        return self.queues.add(channel or self.channel,
                               playback.QueueItem(url, mode, label=mode + ' fixture'))

    async def until(self, condition):
        async with asyncio.timeout(2):
            while not condition():
                await asyncio.sleep(0)

    async def test_video_and_audio_share_fifo_and_wait_without_starting_work(self):
        first, position = self.add()
        self.assertEqual(position, 0)
        self.assertEqual(self.add('music')[1], 1)
        self.assertEqual(self.add('video')[1], 2)
        self.assertEqual(len(self.started), 1)
        first.finish()
        await self.until(lambda: len(self.started) == 2)
        self.assertEqual(self.started[1][0], 'music')
        self.assertTrue(first.cleaned)
        self.started[1][2].finish()
        await self.until(lambda: len(self.started) == 3)
        self.assertEqual([mode for mode, _, _ in self.started], ['video', 'music', 'video'])
        self.started[2][2].finish()
        await self.until(lambda: self.queues.get(42) is None)

    async def test_stop_discards_current_item_and_keeps_remaining_queue_paused(self):
        first, _ = self.add()
        self.add('music')
        await self.queues.pause(42)
        state = self.queues.get(42)
        self.assertTrue(first.cleaned)
        self.assertTrue(state.paused)
        self.assertIsNone(state.current)
        self.assertEqual(len(state.items), 1)
        self.assertEqual(len(self.started), 1)
        self.add('video')
        self.assertEqual(len(self.started), 1)
        self.queues.resume(self.channel)
        await self.until(lambda: len(self.started) == 2)
        self.assertEqual(self.started[1][0], 'music')

    async def test_skip_advances_but_stop_and_clear_are_channel_scoped(self):
        first, _ = self.add()
        self.add('music')
        other = SimpleNamespace(**vars(self.channel) | {'id': 77})
        self.add(channel=other)
        await self.queues.skip(42)
        await self.until(lambda: len(self.started) == 3)
        self.assertTrue(first.cleaned)
        self.assertEqual(self.queues.get(42).current_item.mode, 'music')
        self.add('video')
        self.assertEqual(self.queues.clear(42), 1)
        self.assertTrue(self.queues.get(42).current.active)
        await self.queues.pause(42)
        self.assertTrue(self.queues.get(77).current.active)

    async def test_remove_selected_waiting_items_preserves_current_and_remaining_order(self):
        first, _ = self.add()
        for mode in ('music', 'video', 'music', 'video', 'music'):
            self.add(mode)
        before = list(self.queues.get(42).items)
        other = SimpleNamespace(**vars(self.channel) | {'id': 77})
        self.add(channel=other)
        self.add('music', channel=other)
        self.assertEqual(self.queues.remove(42, '2, 4-5'), [2, 4, 5])
        remaining = list(self.queues.get(42).items)
        self.assertEqual(len(remaining), 2)
        self.assertIs(remaining[0], before[0])
        self.assertIs(remaining[1], before[2])
        self.assertIs(self.queues.get(42).current, first)
        self.assertTrue(first.active)
        self.assertEqual(len(self.queues.get(77).items), 1)
        self.assertEqual(len(self.started), 2)

    async def test_remove_deduplicates_overlapping_positions_and_ranges(self):
        self.add()
        for mode in ('music', 'video', 'music'):
            self.add(mode)
        last = self.queues.get(42).items[-1]
        self.assertEqual(self.queues.remove(42, ' 1, 1 - 2, 2 '), [1, 2])
        self.assertEqual(len(self.queues.get(42).items), 1)
        self.assertIs(self.queues.get(42).items[0], last)

    async def test_invalid_delete_selection_leaves_entire_queue_unchanged(self):
        self.add()
        for mode in ('music', 'video', 'music'):
            self.add(mode)
        before = list(self.queues.get(42).items)
        for selection in ('', ' ', '0', '-1', '4', '2-1', '1,4', '1,', '1,,2',
                          '1,2.0', '1,two', '1-2-3', '１', '1-' + '9' * 100, None):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                self.queues.remove(42, selection)
            self.assertEqual(list(self.queues.get(42).items), before)

    async def test_delete_from_paused_queue_stays_paused_and_removes_empty_queue(self):
        self.add()
        for mode in ('music', 'video', 'music'):
            self.add(mode)
        await self.queues.pause(42)
        self.queues.remove(42, '2')
        state = self.queues.get(42)
        self.assertTrue(state.paused)
        self.assertEqual([item.mode for item in state.items], ['music', 'music'])
        self.assertEqual(len(self.started), 1)
        self.queues.remove(42, '1-2')
        self.assertIsNone(self.queues.get(42))

    async def test_delete_rejects_empty_queue_and_current_item(self):
        with self.assertRaisesRegex(ValueError, 'waiting'):
            self.queues.remove(42, '1')
        first, _ = self.add()
        with self.assertRaisesRegex(ValueError, 'waiting'):
            self.queues.remove(42, '1')
        self.assertIs(self.queues.get(42).current, first)
        self.assertTrue(first.active)

    async def test_failed_queued_source_is_reported_and_next_item_runs(self):
        first, _ = self.add()
        self.add('music')
        self.add('video')
        first.finish()
        await self.until(lambda: len(self.started) == 2)
        broken = self.started[1][2]
        broken.status, broken.error = 'error', 'Public source unavailable.'
        broken.finish(error=broken.error)
        await self.until(lambda: len(self.started) == 3)
        self.assertEqual(self.started[2][0], 'video')
        await self.until(lambda: self.notices.await_count >= 1)

    async def test_queue_limits_reject_without_evicting_existing_entries(self):
        self.add()
        with patch.object(playback, 'QUEUE_LIMIT', 2):
            self.add('music')
            self.add('video')
            with self.assertRaisesRegex(ValueError, 'full|limit'):
                self.add('music')
        with patch.object(playback, 'SERVER_QUEUE_LIMIT', 2):
            other = SimpleNamespace(**vars(self.channel) | {'id': 77})
            with self.assertRaisesRegex(ValueError, 'full|limit'):
                self.add(channel=other)
            # Another server's queues never count against this one.
            elsewhere = SimpleNamespace(**vars(self.channel) | {
                'id': 88, 'guild': SimpleNamespace(id=2, voice_client=None, me=object())})
            self.assertEqual(self.add(channel=elsewhere)[1], 0)
        self.assertEqual(len(self.queues.get(42).items), 2)

    async def test_start_failure_keeps_queued_head_and_pauses_until_resume(self):
        first, _ = self.add()
        self.add('music')
        self.music.prepare.side_effect = ValueError('Music is already playing in this server.')
        first.finish()
        await self.until(lambda: self.queues.get(42).paused)
        self.assertEqual(len(self.queues.get(42).items), 1)
        self.assertIn('already playing', self.queues.get(42).error)
        self.music.prepare.side_effect = lambda *args: FakeSession()
        self.queues.resume(self.channel)
        self.assertEqual(self.queues.get(42).current_item.mode, 'music')

    async def test_empty_channel_and_stale_gateway_pause_instead_of_autoplay(self):
        for disconnect in (False, True):
            with self.subTest(disconnect=disconnect):
                first, _ = self.add()
                self.add('music')
                if disconnect:
                    self.queues.can_start = lambda: False
                else:
                    self.channel.members = []
                first.finish()
                await self.until(lambda: self.queues.get(42).paused)
                self.assertEqual(len(self.queues.get(42).items), 1)
                self.assertIsNone(self.queues.get(42).current)
                self.queues.clear(42)
                self.queues.can_start = lambda: True
                self.channel.members = [SimpleNamespace(bot=False)]

    async def test_resume_uses_fresh_channel_after_discord_rebuilds_its_cache(self):
        self.add()
        self.add('music')
        await self.queues.pause(42)
        self.channel.members = []
        fresh = SimpleNamespace(**vars(self.channel) | {'members': [SimpleNamespace(bot=False)]})
        self.queues.resume(fresh)
        self.music.prepare.assert_called_once_with(fresh, VIDEO)

    async def test_autoplay_rechecks_current_channel_cache_after_reconnect(self):
        first, _ = self.add()
        self.add('music')
        fresh = SimpleNamespace(**vars(self.channel) | {'members': []})
        self.queues.channel_lookup = lambda _: fresh
        first.finish()
        await self.until(lambda: self.queues.get(42).paused)
        self.music.prepare.assert_not_called()

    async def test_stop_wins_when_skip_cleanup_is_in_progress(self):
        first, _ = self.add()
        self.add('music')
        entered, release = asyncio.Event(), asyncio.Event()
        original = first.stop

        async def delayed_stop():
            entered.set()
            await release.wait()
            await original()

        first.stop = delayed_stop
        skipping = asyncio.create_task(self.queues.skip(42))
        await entered.wait()
        stopping = asyncio.create_task(self.queues.pause(42))
        await asyncio.sleep(0)
        release.set()
        await asyncio.gather(skipping, stopping)
        self.assertEqual(len(self.started), 1)
        self.assertTrue(self.queues.get(42).paused)

    async def test_skip_and_stop_discard_current_even_if_capacity_error_finishes_during_cleanup(self):
        for action in ('skip', 'pause'):
            with self.subTest(action=action):
                first, _ = self.add()
                self.add('music')
                entered, release = asyncio.Event(), asyncio.Event()

                async def delayed_stop():
                    entered.set()
                    await release.wait()

                first.stop = delayed_stop
                stopping = asyncio.create_task(getattr(self.queues, action)(42))
                await entered.wait()
                first.retryable = True
                first.finish(error='Media loading is busy.')
                await self.until(lambda: self.queues.get(42).runner.done())
                release.set()
                await stopping
                state = self.queues.get(42)
                paused = state.paused
                current_mode = state.current_item.mode if state.current_item else None
                pending = [item.mode for item in state.items]
                await self.queues.pause(42)
                self.queues.clear(42)
                if action == 'skip':
                    self.assertFalse(paused)
                    self.assertEqual(current_mode, 'music')
                else:
                    self.assertTrue(paused)
                    self.assertEqual(pending, ['music'])

    async def test_cancelled_stop_does_not_interrupt_media_cleanup(self):
        first, _ = self.add()
        self.add('music')
        entered, release = asyncio.Event(), asyncio.Event()
        original = first.stop

        async def delayed_stop():
            entered.set()
            await release.wait()
            await original()

        first.stop = delayed_stop
        stopping = asyncio.create_task(self.queues.pause(42))
        await entered.wait()
        stopping.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await stopping
        self.assertFalse(first.cleaned)
        release.set()
        await self.until(lambda: first.cleaned)
        self.assertTrue(self.queues.get(42).paused)
        self.assertEqual(len(self.started), 1)

    async def test_quality_replacement_stays_on_same_queue_item(self):
        first, _ = self.add()
        self.add('music')
        entered, release = asyncio.Event(), asyncio.Event()
        replacement = FakeSession()

        async def change(channel_id, quality):
            entered.set()
            await first.stop()
            await release.wait()
            self.rooms.channels[42] = replacement
            return replacement

        self.rooms.change_resolution = change
        changing = asyncio.create_task(self.queues.change_resolution(42, '1080'))
        await entered.wait()
        await asyncio.sleep(0)
        self.assertEqual(len(self.started), 1)
        release.set()
        await changing
        self.assertIs(self.queues.get(42).current, replacement)
        self.assertEqual(len(self.started), 1)
        replacement.finish()
        await self.until(lambda: len(self.started) == 2)
        self.assertEqual(self.started[1][0], 'music')

    async def test_failed_quality_change_allows_retry_without_advancing(self):
        first, _ = self.add()
        self.add('music')
        self.rooms.change_resolution = AsyncMock(side_effect=[ValueError('Unavailable quality.'), first])
        with self.assertRaisesRegex(ValueError, 'Unavailable'):
            await self.queues.change_resolution(42, '1080')
        result = await self.queues.change_resolution(42, '720')
        self.assertIs(result, first)
        self.assertEqual(len(self.started), 1)

    async def test_loading_quality_replacement_announces_ready_video_once(self):
        from syncnest import stream
        self.queues.rooms = stream.Rooms()
        first, _ = self.add('music')
        self.add('video')

        async def extract(*args, **kwargs):
            await asyncio.Future()

        with patch('syncnest.stream.extract_video_async', side_effect=extract):
            first.finish()
            await self.until(lambda: self.queues.get(42).current_item.mode == 'video')
            original = self.queues.get(42).current
            replacement = await self.queues.change_resolution(42, '1080')
            self.assertIsNot(original, replacement)
            replacement.status = 'playing'
            await self.until(lambda: self.notices.await_count > 0)
            self.assertEqual(self.notices.await_count, 1)
            self.assertIs(self.notices.call_args.args[1], replacement)

    async def test_stop_during_quality_change_cleans_up_before_resuming_queue(self):
        first, _ = self.add()
        self.add('music')
        entered = asyncio.Event()
        replacement = FakeSession()

        async def change(channel_id, quality):
            try:
                entered.set()
                await asyncio.Future()
            finally:
                await replacement.stop()

        self.rooms.change_resolution = change
        changing = asyncio.create_task(self.queues.change_resolution(42, '1080'))
        await entered.wait()
        await self.queues.pause(42)
        outcome, = await asyncio.gather(changing, return_exceptions=True)
        # The /resolution command must get an answer, not a silent cancellation.
        self.assertIsInstance(outcome, ValueError)
        self.assertIn('stopped', str(outcome))
        self.assertTrue(first.cleaned)
        self.assertTrue(replacement.cleaned)
        self.assertTrue(self.queues.get(42).paused)
        self.assertEqual(len(self.started), 1)

    async def test_shutdown_stops_media_and_never_starts_pending_items(self):
        first, _ = self.add()
        self.add('music')
        await self.queues.close()
        self.assertTrue(first.cleaned)
        self.assertEqual(len(self.started), 1)
        self.assertIsNone(self.queues.get(42))

    async def test_new_items_are_validated_before_entering_queue(self):
        self.add()
        for item in [playback.QueueItem('file:///etc/passwd', 'video'),
                     playback.QueueItem(VIDEO, 'unknown'),
                     playback.QueueItem(VIDEO, 'video', resolution='invalid')]:
            with self.subTest(item=item), self.assertRaises(ValueError):
                self.queues.add(self.channel, item)
        self.assertEqual(len(self.queues.get(42).items), 0)

    async def test_full_extraction_workers_keep_head_for_retry(self):
        from syncnest import stream
        self.queues.rooms = stream.Rooms()
        first, _ = self.add('music')
        self.add('video')
        self.add('video')
        with patch.object(stream, 'MAX_EXTRACTIONS', 0):
            first.finish()
            await self.until(lambda: self.queues.get(42) is None or self.queues.get(42).paused)
            state = self.queues.get(42)
            self.assertIsNotNone(state)
            self.assertTrue(state.paused)
            self.assertEqual(len(state.items), 2)
            self.assertIn('busy', state.error)

    async def test_initial_item_retained_after_capacity_error_announces_on_resume(self):
        from syncnest import stream
        self.queues.rooms = stream.Rooms()
        with patch.object(stream, 'MAX_EXTRACTIONS', 0):
            self.add('video')
            await self.until(lambda: self.queues.get(42).paused)
        self.queues.rooms = self.rooms
        self.notices.reset_mock()
        self.queues.resume(self.channel)
        await self.until(lambda: self.notices.await_count > 0)
        self.assertIs(self.notices.call_args.args[1], self.queues.get(42).current)


class QueueCommandTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from syncnest.queue_commands import install_queue_commands
        self.client = discord.Client(intents=discord.Intents.none())
        self.client.voice_states_ready = True
        self.client.tree = app_commands.CommandTree(self.client)
        self.channel = SimpleNamespace(id=42, members=[SimpleNamespace(bot=False)])
        self.current_channel = self.channel

        def voice(interaction):
            if self.current_channel is None:
                raise ValueError('Join a voice channel first.')
            return self.current_channel

        self.queues = SimpleNamespace(get=Mock(return_value=None), clear=Mock(return_value=2),
            remove=Mock(return_value=[2, 4, 5]),
            resume=Mock(), skip=AsyncMock())
        install_queue_commands(self.client, self.queues, voice)
        self.interaction = SimpleNamespace(user=SimpleNamespace(id=7), response=SimpleNamespace(
            send_message=AsyncMock(), defer=AsyncMock()), edit_original_response=AsyncMock())

    def command(self, name):
        return self.client.tree.get_command(name).callback

    async def test_queue_shows_mode_order_and_pause_without_signed_urls(self):
        items = [playback.QueueItem(VIDEO, 'video', label='First video'),
                 playback.QueueItem('https://cdn.discordapp.com/attachments/1/2/song.mp3?hm=secret',
                                    'music', label='song.mp3')]
        self.queues.get.return_value = SimpleNamespace(items=items, paused=True, error=None,
            current=None, current_item=None)
        await self.command('queue')(self.interaction)
        message = self.interaction.response.send_message.call_args.args[0]
        self.assertIn('Paused', message)
        self.assertLess(message.index('First video'), message.index('song.mp3'))
        self.assertNotIn('secret', message)
        self.assertIn('/resume', message)
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])

    async def test_long_queue_is_paginated_within_discord_message_limit(self):
        self.queues.get.return_value = SimpleNamespace(paused=False, error=None,
            items=[playback.QueueItem(VIDEO, 'video', label='x' * 200) for _ in range(25)],
            current=SimpleNamespace(title='Current', status='playing'),
            current_item=playback.QueueItem(VIDEO, 'video'))
        await self.command('queue')(self.interaction, page=2)
        message = self.interaction.response.send_message.call_args.args[0]
        self.assertLessEqual(len(message), 2000)
        self.assertIn('11.', message)
        self.assertIn('20.', message)
        self.assertNotIn('21.', message)

    async def test_clear_only_removes_pending_items(self):
        await self.command('queue')(self.interaction, clear=True)
        self.queues.clear.assert_called_once_with(42, ANY)
        self.assertIn('2', self.interaction.response.send_message.call_args.args[0])
        self.queues.skip.assert_not_awaited()

    async def test_delete_option_removes_selected_waiting_positions_in_callers_channel(self):
        await self.command('queue')(self.interaction, delete='2,4-5')
        self.queues.remove.assert_called_once_with(42, '2,4-5', ANY)
        message = self.interaction.response.send_message.call_args.args[0]
        self.assertIn('Removed 3', message)
        self.assertIn('2, 4, 5', message)
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])
        self.queues.clear.assert_not_called()
        self.queues.skip.assert_not_awaited()

    async def test_delete_and_clear_together_are_rejected_before_either_mutates(self):
        await self.command('queue')(self.interaction, delete='1', clear=True)
        self.queues.remove.assert_not_called()
        self.queues.clear.assert_not_called()
        self.assertIn('delete', self.interaction.response.send_message.call_args.args[0])

    async def test_delete_rejects_missing_voice_or_invalid_positions_privately(self):
        self.current_channel = None
        await self.command('queue')(self.interaction, delete='1')
        self.queues.remove.assert_not_called()
        self.current_channel = self.channel
        self.queues.remove.side_effect = ValueError('Choose positions from the waiting queue.')
        await self.command('queue')(self.interaction, delete='0')
        self.assertIn('waiting queue', self.interaction.response.send_message.call_args.args[0])
        self.assertTrue(self.interaction.response.send_message.call_args.kwargs['ephemeral'])

    async def test_resume_and_skip_recheck_membership_after_defer(self):
        async def leave(**kwargs):
            self.current_channel = None

        self.interaction.response.defer.side_effect = leave
        for name in ('resume', 'skip'):
            self.current_channel = self.channel
            await self.command(name)(self.interaction)
        self.queues.resume.assert_not_called()
        self.queues.skip.assert_not_awaited()

    async def test_skip_belongs_to_the_present_requester_or_a_voice_moderator(self):
        requester = SimpleNamespace(id=99, bot=False)
        self.channel.permissions_for = lambda member: SimpleNamespace(move_members=False)
        self.channel.members = [requester, SimpleNamespace(id=7, bot=False)]
        self.queues.get.return_value = SimpleNamespace(
            current_item=playback.QueueItem(VIDEO, 'video', requester_id=99))
        await self.command('skip')(self.interaction)
        self.queues.skip.assert_not_awaited()
        self.assertIn('Only the person who added', self.interaction.edit_original_response.call_args.kwargs['content'])
        self.channel.permissions_for = lambda member: SimpleNamespace(move_members=True)
        await self.command('skip')(self.interaction)
        self.queues.skip.assert_awaited_once_with(42)

    async def test_resume_and_skip_control_only_callers_voice_channel(self):
        await self.command('resume')(self.interaction)
        self.queues.resume.assert_called_once_with(self.channel)
        await self.command('skip')(self.interaction)
        self.queues.skip.assert_awaited_once_with(42)

    async def test_spotify_announcement_labels_the_actual_public_audio_source(self):
        session = SimpleNamespace(title='YouTube recording', status='playing',
            spotify_title='Artist - Spotify track', matched_url=VIDEO)
        item = playback.QueueItem(VIDEO, 'music', channel_id=42)
        self.queues.get.return_value = SimpleNamespace(current=session, current_item=item, paused=False)
        self.channel.send = AsyncMock()
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await self.queues.notify(item, session)
        message = self.channel.send.call_args.args[0]
        self.assertIn('Spotify', message)
        self.assertIn('YouTube source', message)
        self.assertIn(VIDEO, message)

    async def test_automatic_video_start_provides_an_activity_invite(self):
        session = SimpleNamespace(title='Video', status='playing', spotify_title=None)
        item = playback.QueueItem(VIDEO, 'video', channel_id=42, invite=True)
        self.queues.get.return_value = SimpleNamespace(current=session, current_item=item, paused=False)
        self.channel.create_invite = AsyncMock(return_value=SimpleNamespace(url='https://discord.gg/fixture'))
        self.channel.send = AsyncMock()
        with patch.object(self.client, 'get_channel', return_value=self.channel):
            await self.queues.notify(item, session)
        self.assertEqual(self.channel.send.call_args.kwargs['view'].children[0].url,
                         'https://discord.gg/fixture')

    async def test_activity_invite_survives_quality_replacement_but_not_skip(self):
        for replaced_item in (False, True):
            with self.subTest(skipped=replaced_item):
                item = playback.QueueItem(VIDEO, 'video', channel_id=42, invite=True)
                session = SimpleNamespace(title='Video', status='playing', spotify_title=None)
                state = SimpleNamespace(current=session, current_item=item, paused=False)
                self.queues.get.return_value = state
                entered, release = asyncio.Event(), asyncio.Event()

                async def invite(**kwargs):
                    entered.set()
                    await release.wait()
                    return SimpleNamespace(url='https://discord.gg/fixture')

                self.channel.create_invite = invite
                self.channel.send = AsyncMock()
                with patch.object(self.client, 'get_channel', return_value=self.channel):
                    announcing = asyncio.create_task(self.queues.notify(item, session))
                    await entered.wait()
                    state.current = SimpleNamespace(title='Replacement', status='playing')
                    if replaced_item:
                        state.current_item = playback.QueueItem(VIDEO, 'video', channel_id=42, invite=True)
                    release.set()
                    await announcing
                self.assertEqual(self.channel.send.await_count, 0 if replaced_item else 1)


class ControlPermissionTests(unittest.TestCase):
    def setUp(self):
        from syncnest.queue_commands import may_control
        self.may_control = may_control
        self.requester, self.other, self.moderator = (SimpleNamespace(id=index) for index in (1, 2, 3))
        self.channel = SimpleNamespace(members=[self.requester, self.other, self.moderator],
                                       permissions_for=lambda member: SimpleNamespace(
                                           move_members=member is self.moderator))

    def test_requester_moderator_or_anyone_after_the_requester_leaves(self):
        item = playback.QueueItem(VIDEO, 'video', requester_id=1)
        self.assertTrue(self.may_control(self.requester, self.channel, item))
        self.assertTrue(self.may_control(self.moderator, self.channel, item))
        self.assertFalse(self.may_control(self.other, self.channel, item))
        self.channel.members.remove(self.requester)
        self.assertTrue(self.may_control(self.other, self.channel, item))
        self.assertTrue(self.may_control(self.other, self.channel, None))

    def test_delete_is_all_or_nothing_and_clear_keeps_other_peoples_items(self):
        mine, theirs, also_mine = (playback.QueueItem(VIDEO, 'video', label=name, requester_id=owner)
                                   for name, owner in (('mine', 1), ('theirs', 2), ('also mine', 1)))
        queues = playback.PlaybackQueues(None, None)
        state = queues.channels[42] = playback.ChannelQueue(SimpleNamespace(id=42), deque([mine, theirs, also_mine]))
        allowed = lambda item: self.may_control(self.requester, self.channel, item)
        with self.assertRaisesRegex(ValueError, 'Nothing was deleted'):
            queues.remove(42, '1-2', allowed)
        self.assertEqual(list(state.items), [mine, theirs, also_mine])
        self.assertEqual(queues.remove(42, '3', allowed), [3])
        self.assertEqual(queues.clear(42, allowed), 1)
        self.assertEqual(list(state.items), [theirs])


class QueueBotTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from syncnest import bot
        self.bot = bot
        self.sessions = []
        self.channel = Mock(spec=discord.VoiceChannel)
        self.channel.id = 42
        self.channel.guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        self.channel.members = [SimpleNamespace(bot=False)]
        self.channel.permissions_for.return_value = SimpleNamespace(
            connect=True, speak=True, create_instant_invite=True, move_members=False)
        self.member = Mock(spec=discord.Member)
        self.member.voice = SimpleNamespace(channel=self.channel)
        self.interaction = SimpleNamespace(user=self.member, channel_id=42,
            response=SimpleNamespace(send_message=AsyncMock(), defer=AsyncMock(),
                                     launch_activity=AsyncMock()), edit_original_response=AsyncMock())
        self.rooms = SimpleNamespace(channels={}, prepare=Mock())

        def prepare(channel_id, url, resolution):
            session = FakeSession()
            self.sessions.append(session)
            self.rooms.channels[channel_id] = session
            return session

        self.rooms.prepare.side_effect = prepare
        def prepare_music(*args):
            session = FakeSession()
            self.sessions.append(session)
            return session

        self.music = SimpleNamespace(get=Mock(return_value=None),
            prepare=Mock(side_effect=prepare_music), stop=AsyncMock())
        self.queues = playback.PlaybackQueues(self.rooms, self.music)
        for name, value in [('rooms', self.rooms), ('music', self.music), ('playback', self.queues)]:
            handle = patch.object(bot, name, value, create=True)
            handle.start()
            self.addCleanup(handle.stop)
        handle = patch.object(bot.bot, 'voice_states_ready', True)
        handle.start()
        self.addCleanup(handle.stop)

    async def asyncTearDown(self):
        await self.queues.close()
        for session in self.sessions:
            await session.stop()

    async def test_play_appends_audio_behind_video_instead_of_rejecting(self):
        await self.bot.play.callback(self.interaction, url=VIDEO)
        await self.bot.play.callback(self.interaction, url=VIDEO, mode='music')
        self.music.prepare.assert_not_called()
        state = self.queues.get(42)
        self.assertIsNotNone(state)
        self.assertEqual([item.mode for item in state.items], ['music'])
        self.assertIn('Queued', self.interaction.response.send_message.call_args.args[0])

    async def test_play_queues_video_behind_audio_and_stop_preserves_it(self):
        await self.bot.play.callback(self.interaction, url=VIDEO, mode='music')
        await self.bot.play.callback(self.interaction, url=VIDEO)
        self.interaction.response.launch_activity.assert_not_awaited()
        await self.bot.stop.callback(self.interaction)
        state = self.queues.get(42)
        self.assertIsNotNone(state)
        self.assertTrue(state.paused)
        self.assertEqual([item.mode for item in state.items], ['video'])
        self.assertIn('/resume', self.interaction.edit_original_response.call_args.kwargs['content'])

    async def test_failed_old_activity_response_does_not_stop_a_newer_item(self):
        for channel_id in (42, 99):
            with self.subTest(channel_id=channel_id):
                entered, release = asyncio.Event(), asyncio.Event()

                async def fail(**kwargs):
                    entered.set()
                    await release.wait()
                    raise discord.HTTPException(SimpleNamespace(status=403, reason='Forbidden'), 'No permission')

                self.interaction.channel_id = channel_id
                self.interaction.response.launch_activity.side_effect = fail if channel_id == 42 else None
                self.channel.create_invite = AsyncMock(side_effect=fail)
                first_command = asyncio.create_task(self.bot.play.callback(self.interaction, url=VIDEO))
                try:
                    await entered.wait()
                    self.queues.add(self.channel, playback.QueueItem(VIDEO, 'music'))
                    self.queues.add(self.channel, playback.QueueItem(VIDEO, 'video'))
                    await self.queues.skip(42)
                    current = self.queues.get(42).current
                    self.assertEqual(self.queues.get(42).current_item.mode, 'music')
                    release.set()
                    await first_command
                    self.assertIs(self.queues.get(42).current, current)
                    self.assertFalse(self.queues.get(42).paused)
                    self.assertTrue(current.active)
                finally:
                    release.set()
                    await asyncio.gather(first_command, return_exceptions=True)
                    await self.queues.pause(42)
                    self.queues.clear(42)

    async def test_deleted_channel_releases_its_queue_and_media(self):
        await self.bot.play.callback(self.interaction, url=VIDEO)
        self.queues.add(self.channel, playback.QueueItem(VIDEO, 'music'))
        first = self.queues.get(42).current
        await self.bot.bot.on_guild_channel_delete(self.channel)
        self.assertIsNone(self.queues.get(42))
        self.assertFalse(first.active)

    async def test_removed_server_discards_only_its_queues(self):
        await self.bot.play.callback(self.interaction, url=VIDEO)
        self.queues.add(self.channel, playback.QueueItem(VIDEO, 'music'))
        other = SimpleNamespace(id=77, members=[SimpleNamespace(bot=False)],
            guild=SimpleNamespace(id=2, me=object(), voice_client=None))
        self.queues.add(other, playback.QueueItem(VIDEO, 'video'))
        await self.bot.bot.on_guild_remove(self.channel.guild)
        self.assertIsNone(self.queues.get(42))
        self.assertTrue(self.queues.get(77).current.active)


@unittest.skipUnless(shutil.which('ffmpeg'), 'FFmpeg required')
class QueueEncodingTests(unittest.IsolatedAsyncioTestCase):
    async def test_http_video_then_audio_uses_real_encoders_and_cleans_both(self):
        from syncnest import music, stream
        rooms, voices = stream.Rooms(), music.MusicRooms()
        guild = SimpleNamespace(id=1, me=object(), voice_client=None)
        channel = SimpleNamespace(id=42, guild=guild, members=[SimpleNamespace(bot=False)],
            permissions_for=lambda _: SimpleNamespace(connect=True, speak=True))
        voice = SimpleNamespace(stop=Mock(), disconnect=AsyncMock())
        audio_frames, readers, encoders = [], [], []

        async def connect(**kwargs):
            guild.voice_client = voice
            return voice

        async def disconnect(**kwargs):
            guild.voice_client = None

        def play(source, after):
            encoders.append(source._process)
            def read():
                try:
                    while packet := source.read():
                        if packet.startswith((b'OpusHead', b'OpusTags')):
                            raise ValueError('Codec header sent as audio.')
                        audio_frames.append(packet)
                    after(None)
                except Exception as error:
                    after(error)
            readers.append(asyncio.create_task(asyncio.to_thread(read)))

        channel.connect, voice.play = connect, play
        voice.disconnect.side_effect = disconnect
        notices = []

        async def notice(item, session=None, error=None):
            notices.append((item.mode, session, error))

        queues = playback.PlaybackQueues(rooms, voices, notify=notice)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            process = await asyncio.create_subprocess_exec('ffmpeg', '-v', 'error', '-f', 'lavfi',
                '-i', 'testsrc2=size=320x180:rate=30', '-f', 'lavfi', '-i', 'sine=sample_rate=48000',
                '-t', '1', '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '30', '-c:a', 'aac',
                '-movflags', '+faststart', str(root / 'video.mp4'))
            self.assertEqual(await process.wait(), 0)
            process = await asyncio.create_subprocess_exec('ffmpeg', '-v', 'error', '-f', 'lavfi',
                '-i', 'sine=sample_rate=48000', '-t', '1', str(root / 'audio.wav'))
            self.assertEqual(await process.wait(), 0)
            app = web.Application()
            app.router.add_static('/', root)
            async with TestServer(app) as server:
                async def extract(url, quality, *, audio_only=False):
                    return {'url': str(server.make_url('/audio.wav' if audio_only else '/video.mp4')),
                            '_input_format': 'wav' if audio_only else 'mov',
                            'title': 'Audio fixture' if audio_only else 'Video fixture'}

                with patch('syncnest.stream.extract_video_async', side_effect=extract):
                    try:
                        first, _ = queues.add(channel, playback.QueueItem(VIDEO, 'video'))
                        queues.add(channel, playback.QueueItem(VIDEO, 'music'))
                        await asyncio.wait_for(first.ready.wait(), 10)
                        self.assertIn(first.status, {'playing', 'ending'}, first.error)
                        self.assertTrue(list(first.directory.glob('*.ts')))
                        async with asyncio.timeout(35):
                            while queues.get(42):
                                await asyncio.sleep(0.02)
                        await asyncio.gather(*readers)
                        self.assertIsNone(first.directory)
                        self.assertIsNotNone(first.process.returncode)
                        self.assertGreater(len(audio_frames), 10)
                        self.assertEqual([mode for mode, _, _ in notices], ['music'])
                        self.assertIsNone(guild.voice_client)
                        self.assertIsNotNone(encoders[0].poll())
                        self.assertTrue(encoders[0].stdout.closed)
                        voice.disconnect.assert_awaited_once()
                    finally:
                        await queues.close()
                        await asyncio.gather(rooms.close(), voices.close())
