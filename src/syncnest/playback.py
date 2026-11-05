"""A bounded mixed-media queue for each voice channel."""

import asyncio
import logging
import re
from collections import deque
from dataclasses import dataclass, field, replace

from .music import normalize_music_url
from .stream import normalize_video_url, resolution_height

log = logging.getLogger(__name__)
QUEUE_LIMIT = 25
SERVER_QUEUE_LIMIT = 100


@dataclass(frozen=True)
class QueueItem:
    url: str
    mode: str
    resolution: str = '720'
    label: str = 'Media'
    channel_id: int = 0
    announce: bool = False
    # Who queued the item, and whether they may hand out the server invite an Activity link needs.
    requester_id: int = 0
    invite: bool = False


@dataclass
class ChannelQueue:
    channel: object
    items: deque = field(default_factory=deque)
    paused: bool = False
    error: str | None = None
    current_item: QueueItem | None = None
    current: object = None
    runner: asyncio.Task | None = None
    changing: asyncio.Task | None = None
    stopping: asyncio.Task | None = None

    @property
    def waiting_count(self):
        # Keep room to restore a loading item if extraction capacity is exhausted.
        loading = self.current and (self.current.status in {'loading', 'starting'}
                                   or getattr(self.current, 'retryable', False))
        return len(self.items) + bool(loading)


class PlaybackQueues:
    def __init__(self, rooms, music, *, can_start=lambda: True, channel_lookup=None, notify=None):
        self.rooms, self.music = rooms, music
        self.can_start, self.notify = can_start, notify
        self.channel_lookup = channel_lookup
        self.channels = {}
        self.notices = set()
        self.closed = False

    def get(self, channel_id):
        return self.channels.get(channel_id)

    def add(self, channel, item):
        if self.closed:
            raise ValueError('The bot is shutting down. Try again after it restarts.')
        if item.mode not in {'video', 'music'}:
            raise ValueError('Choose video or music mode.')
        resolution_height(item.resolution)
        url = normalize_music_url(item.url) if item.mode == 'music' else normalize_video_url(item.url)
        state = self.channels.get(channel.id)
        if state and state.waiting_count >= QUEUE_LIMIT:
            raise ValueError(f'This queue is full ({QUEUE_LIMIT} waiting items).')
        # Count per server: paused queues in abandoned channels must not block other servers.
        if sum(queue.waiting_count for queue in self.channels.values()
               if queue.channel.guild.id == channel.guild.id) >= SERVER_QUEUE_LIMIT:
            raise ValueError("This server's queue limit is full. Try again after items finish.")
        if not state:
            state = self.channels[channel.id] = ChannelQueue(channel)
        else:
            state.channel = channel
        waiting = bool(state.runner or state.stopping or state.paused)
        item = replace(item, url=url, channel_id=channel.id, announce=waiting)
        state.items.append(item)
        try:
            self._start(state)
        except ValueError:
            state.items.pop()
            self._discard_empty(state)
            raise
        return (None, len(state.items)) if waiting else (state.current, 0)

    def _start(self, state):
        if state.paused or state.runner or state.stopping or not state.items or self.closed:
            return
        if not self.can_start():
            raise ValueError('Discord is reconnecting. Use /resume when it is ready.')
        if self.channel_lookup:
            channel = self.channel_lookup(state.channel.id)
            if not channel:
                raise ValueError('This voice channel is no longer available.')
            state.channel = channel
        if not any(not member.bot for member in state.channel.members):
            raise ValueError('Nobody is in this voice channel. Rejoin and use /resume.')
        item = state.items[0]
        if item.mode == 'music':
            permissions = state.channel.permissions_for(state.channel.guild.me)
            if not permissions.connect or not permissions.speak:
                raise ValueError('Give the bot Connect and Speak permissions, then use /resume.')
            session = self.music.prepare(state.channel, item.url)
        else:
            session = self.rooms.prepare(state.channel.id, item.url, item.resolution)
        state.items.popleft()
        state.current_item, state.current = item, session
        state.error = None
        state.runner = asyncio.create_task(self._watch(state, item))
        state.runner.add_done_callback(lambda task: self._finished(state, task))

    async def _watch(self, state, item):
        session = state.current
        announced = False
        while True:
            await session.ready.wait()
            if (item.announce and not announced and session.status in {'playing', 'ending', 'error'}
                    and not (session.status == 'error' and state.changing)
                    and not getattr(session, 'retryable', False)):
                await self._notify(item, session)
                announced = True
            await asyncio.shield(asyncio.gather(session.task, return_exceptions=True))
            # A quality change replaces the encoder, not the logical queue item.
            if state.changing:
                changing = state.changing
                result, = await asyncio.shield(asyncio.gather(changing, return_exceptions=True))
                if not isinstance(result, BaseException):
                    state.current = result
                if state.changing is changing:
                    state.changing = None
            if state.current is session:
                if getattr(session, 'retryable', False) and not state.stopping:
                    state.items.appendleft(replace(item, announce=True))
                    state.paused, state.error = True, session.error
                    await self._notify(item, error=session.error)
                return
            session = state.current

    def _finished(self, state, task):
        if not task.cancelled():
            error = task.exception()
            if error:
                log.warning('Queue watcher failed (%s).', type(error).__name__)
                state.paused, state.error = True, 'Playback could not advance. Use /resume to try the next item.'
                if not state.stopping:
                    self._interrupt(state)
                return
        if state.stopping:
            return
        state.runner = state.current = state.current_item = None
        self._continue(state)

    def _continue(self, state):
        if self.channels.get(state.channel.id) is not state:
            return
        try:
            self._start(state)
        except ValueError as error:
            state.paused, state.error = True, str(error)
            notice = asyncio.create_task(self._notify(state.items[0], error=state.error))
            self.notices.add(notice)
            notice.add_done_callback(self.notices.discard)
        self._discard_empty(state)

    def _discard_empty(self, state):
        if not state.items and not state.runner and not state.stopping:
            self.channels.pop(state.channel.id, None)

    async def _notify(self, item, session=None, error=None):
        if self.notify:
            try:
                await asyncio.wait_for(self.notify(item, session, error), 10)
            except Exception as failure:
                log.warning('Queue announcement failed (%s).', type(failure).__name__)

    def _interrupt(self, state):
        if state.stopping:
            return state.stopping

        async def cleanup():
            if state.changing:
                state.changing.cancel()
                await asyncio.gather(state.changing, return_exceptions=True)
                state.changing = None
            sessions = [state.current] if state.current else []
            if state.current_item and state.current_item.mode == 'video':
                replacement = self.rooms.channels.get(state.channel.id)
                if replacement and replacement not in sessions:
                    sessions.append(replacement)
            for session in sessions:
                await session.stop()
            if state.runner:
                state.runner.cancel()
                await asyncio.gather(state.runner, return_exceptions=True)

        def finished(task):
            if not task.cancelled():
                error = task.exception()
                if error:
                    state.paused = True
                    log.warning('Queue cleanup failed (%s).', type(error).__name__)
            state.stopping = state.runner = state.current = state.current_item = None
            self._continue(state)

        state.stopping = asyncio.create_task(cleanup())
        state.stopping.add_done_callback(finished)
        return state.stopping

    async def pause(self, channel_id, *, expected_item=None):
        state = self.get(channel_id)
        if expected_item is not None and (not state or state.current_item is not expected_item):
            return False
        if state:
            state.paused, state.error = True, None
            await asyncio.shield(self._interrupt(state))
            return True
        room = self.rooms.channels.get(channel_id)
        stopped_room = bool(room and room.active)
        if stopped_room:
            await room.stop()
        stopped_music = await self.music.stop(channel_id)
        return stopped_room or stopped_music

    async def skip(self, channel_id):
        state = self.get(channel_id)
        if not state or not state.current or state.paused:
            raise ValueError('Nothing is playing. Use /resume if the queue is paused.')
        await asyncio.shield(self._interrupt(state))

    def resume(self, channel):
        state = self.get(channel.id)
        if not state or not state.items:
            raise ValueError('No queued items. Add a source with /play.')
        if not state.paused:
            raise ValueError('The queue is already playing. Use /skip to advance.')
        state.paused, state.error = False, None
        state.channel = channel
        try:
            self._start(state)
        except ValueError as error:
            state.paused, state.error = True, str(error)
            raise

    def clear(self, channel_id, allowed=None):
        state = self.get(channel_id)
        if not state:
            return 0
        kept = [item for item in state.items if allowed and not allowed(item)]
        count = len(state.items) - len(kept)
        state.items.clear()
        state.items.extend(kept)
        self._discard_empty(state)
        return count

    def remove(self, channel_id, selection, allowed=None):
        state = self.get(channel_id)
        if not state or not state.items:
            raise ValueError('There are no waiting items to delete. Use /skip for current playback.')
        if not isinstance(selection, str) or not selection.strip() or len(selection) > 100:
            raise ValueError('Specify queue positions such as 2, 1,3 or 2-5 (up to 100 characters).')
        positions = set()
        count = len(state.items)
        for part in selection.split(','):
            match = re.fullmatch(r'\s*([0-9]+)(?:\s*-\s*([0-9]+))?\s*', part)
            if not match:
                raise ValueError('Use queue positions such as 2, 1,3 or 2-5. Nothing was deleted.')
            start, end = int(match[1]), int(match[2] or match[1])
            if not 1 <= start <= end <= count:
                raise ValueError(f'Choose waiting positions from 1 to {count}, with ranges in ascending order. '
                                 'Nothing was deleted.')
            positions.update(range(start, end + 1))
        positions = sorted(positions)
        if allowed and not all(allowed(state.items[position - 1]) for position in positions):
            raise ValueError('You can delete only items you added, unless you have Move Members '
                             'or the person who added them has left. Nothing was deleted.')
        for position in reversed(positions):
            del state.items[position - 1]
        if 1 in positions:
            state.error = None
        self._discard_empty(state)
        return positions

    async def change_resolution(self, channel_id, quality):
        state = self.get(channel_id)
        if not state or not state.current_item or state.current_item.mode != 'video':
            return await self.rooms.change_resolution(channel_id, quality)
        if state.changing:
            raise ValueError('A resolution change is already in progress. Try again shortly.')
        if state.paused or state.stopping:
            raise ValueError('Nothing is playing. Use /resume to continue the queue.')
        changing = state.changing = asyncio.create_task(self.rooms.change_resolution(channel_id, quality))

        def applied(task):
            if not task.cancelled() and task.exception() is None:
                state.current = task.result()
            if state.changing is task:
                state.changing = None

        changing.add_done_callback(applied)
        try:
            return await asyncio.shield(changing)
        except asyncio.CancelledError:
            # Stop or skip cancelled the change, not this command: report it instead of dying silently.
            if changing.cancelled() and not asyncio.current_task().cancelling():
                raise ValueError('The video was stopped or changed during the resolution switch.') from None
            raise

    async def close(self):
        self.closed = True
        await asyncio.gather(*(self.pause(channel_id) for channel_id in list(self.channels)))
        for task in self.notices:
            task.cancel()
        await asyncio.gather(*self.notices, return_exceptions=True)
        self.channels.clear()
