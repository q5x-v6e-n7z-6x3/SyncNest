"""One native music connection per guild, sharing the media worker limits."""

import asyncio
import logging
import shlex
import subprocess
import threading

import discord

from . import stream
from .music_sources import spotify_url

log = logging.getLogger(__name__)


def normalize_music_url(url):
    return spotify_url(url) or stream.normalize_video_url(url, audio_only=True)


def resolve_future(future, value=None):
    if not future.done():
        future.set_result(value)


class MusicVoiceClient(discord.VoiceClient):
    async def disconnect(self, *, force=False):
        # discord.py leaves reconnect tasks alive when disconnecting in backoff.
        pending = [task for task in (self._connection._connector, self._connection._runner)
                   if task and task is not asyncio.current_task() and not task.done()]
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        await super().disconnect(force=force)


class VoiceAudio(discord.FFmpegOpusAudio):
    def __init__(self, info):
        self.loop = asyncio.get_running_loop()
        self.started = self.loop.create_future()
        self._heard_packet = False
        self._cleanup_lock = threading.Lock()
        self._closed = False
        inputs = stream.ffmpeg_input_args(info)
        if inputs.count('-i') != 1:
            raise ValueError('Choose one audio stream.')
        super().__init__(inputs[-1], bitrate=128, stderr=subprocess.DEVNULL,
                         before_options=shlex.join(['-nostdin', *inputs[:-2]]),
                         options='-map 0:a:0 -vn')

    def read(self):
        packet = super().read()
        # Discord expects audio frames, without the Ogg stream's codec headers.
        while packet.startswith((b'OpusHead', b'OpusTags')):
            packet = super().read()
        if packet and not self._heard_packet:
            self._heard_packet = True
            self.loop.call_soon_threadsafe(resolve_future, self.started)
        return packet

    def cleanup(self):
        # Discord's audio thread and an explicit stop can finish together.
        with self._cleanup_lock:
            if not self._closed:
                process = getattr(self, '_process', None)
                try:
                    super().cleanup()
                finally:
                    # Natural FFmpeg exits can leave its pipe open in discord.py.
                    if process:
                        for pipe in (process.stdin, process.stdout, process.stderr):
                            if pipe:
                                pipe.close()
                    self._closed = True


class MusicSession:
    def __init__(self, channel, url):
        self.channel, self.url = channel, url
        self.status, self.error = 'loading', None
        self.retryable = False
        self.title, self.spotify_title, self.matched_url = 'Loading music…', None, None
        self.voice = self.source = None
        self.connecting = self.cleaning = False
        self.ready = asyncio.Event()
        self.task = stream.start_stream_task(self.run)

    @property
    def active(self):
        return not self.task.done()

    async def run(self):
        loop = asyncio.get_running_loop()
        finished = loop.create_future()

        def after(error):
            if not loop.is_closed():
                loop.call_soon_threadsafe(resolve_future, finished, error)

        try:
            info = await stream.extract_video_async(self.url, 'best', audio_only=True)
            self.title = str(info.get('title') or 'Music')[:200]
            self.spotify_title, self.matched_url = info.get('spotify_title'), info.get('matched_url')
            self.connecting = True
            self.voice = await self.channel.connect(cls=MusicVoiceClient, timeout=20, reconnect=True, self_deaf=True)
            self.source = VoiceAudio(info)
            self.voice.play(self.source, after=after)
            done, _ = await asyncio.wait({self.source.started, finished}, timeout=20,
                                         return_when=asyncio.FIRST_COMPLETED)
            if self.source.started not in done:
                raise ValueError('The source did not produce playable audio. Try another public link.')
            self.status = 'playing'
            self.ready.set()
            error = await finished
            if error:
                raise ValueError('The music connection ended unexpectedly. Use /play to try again.')
            self.status = 'finished'
        except asyncio.CancelledError:
            self.status = 'stopped'
            raise
        except ValueError as error:
            self.error, self.status = str(error), 'error'
            self.retryable = isinstance(error, stream.MediaBusy)
        except Exception as error:
            log.warning('Music playback failed (%s).', type(error).__name__)
            self.error = 'Could not play this music. Check Connect/Speak permissions and try another public link.'
            self.status = 'error'
        finally:
            self.cleaning = True
            try:
                voice = self.voice or (self.channel.guild.voice_client if self.connecting else None)
                try:
                    if voice:
                        voice.stop()
                    if self.source:
                        await asyncio.to_thread(self.source.cleanup)
                finally:
                    if voice:
                        await voice.disconnect(force=True)
            finally:
                self.ready.set()

    async def stop(self):
        if not self.cleaning and not self.task.cancelling():
            self.task.cancel()
        await asyncio.shield(asyncio.gather(self.task, return_exceptions=True))
        self.ready.set()


class MusicRooms:
    def __init__(self):
        self.sessions = {}

    def get(self, channel_id):
        return next((session for session in self.sessions.values()
                     if session.channel.id == channel_id and session.active), None)

    def prepare(self, channel, url):
        url = normalize_music_url(url)
        current = self.sessions.get(channel.guild.id)
        if current and current.active or channel.guild.voice_client is not None:
            raise ValueError('Music is already playing or loading in this server. Use /stop in that voice channel first.')
        session = MusicSession(channel, url)
        self.sessions[channel.guild.id] = session

        def release(task):
            if self.sessions.get(channel.guild.id) is session:
                self.sessions.pop(channel.guild.id)
            session.ready.set()
            if not task.cancelled():
                task.exception()

        session.task.add_done_callback(release)
        return session

    async def stop(self, channel_id):
        session = self.get(channel_id)
        if session:
            await session.stop()
        return session is not None

    async def close(self):
        await asyncio.gather(*(session.stop() for session in list(self.sessions.values())))
