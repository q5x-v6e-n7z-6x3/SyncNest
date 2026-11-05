"""Run the Discord commands and the shared video Activity server."""

import asyncio
import logging
import os
import shutil
from urllib.parse import urlsplit
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv

from . import egress
from .activity import WEB, create_app
from .music import MusicRooms, normalize_music_url
from .music_sources import spotify_url
from .playback import PlaybackQueues, QueueItem
from .queue_commands import (check_control, install_queue_commands, playing_message, source_label,
                             video_view)
from .stream import (AUDIO_FORMATS, RESOLUTIONS, Rooms, normalize_video_url, resolution_height,
                     supports_readrate_catchup)

log = logging.getLogger('syncnest')
rooms = Rooms()
music = MusicRooms()
RESOLUTION_CHOICES = [
    app_commands.Choice(name={'2160': '4K (2160p)', 'best': 'Highest possible'}.get(value, f'{value}p'),
                        value=value)
    for value in RESOLUTIONS
]


class SyncNestBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        super().__init__(intents=intents, allowed_mentions=discord.AllowedMentions.none())
        self.voice_states_ready = False
        self.tree = app_commands.CommandTree(self)

    async def setup_hook(self):
        application = await self.application_info()
        if not application.flags.embedded:
            raise SystemExit('Enable Activities in the Developer Portal first. See README.md.')
        # Register slash commands individually to preserve Discord's Launch entry.
        # Bulk tree.sync() omits that entry, which Discord refuses to remove.
        for command in self.tree.get_commands():
            await self.http.upsert_global_command(
                self.application_id, command.to_dict(self.tree),
            )

    async def on_ready(self):
        self.voice_states_ready = True
        log.info('Connected to Discord. Playback commands and the video Activity are ready.')
        # A new gateway session sends no leave events for people who left while it was down.
        empty = []
        for channel_id, state in list(playback.channels.items()):
            channel = self.get_channel(channel_id)
            if state.current and not (channel and any(not member.bot for member in channel.members)):
                empty.append(channel_id)
        await asyncio.gather(*(playback.pause(channel_id) for channel_id in empty))

    async def on_disconnect(self):
        self.voice_states_ready = False

    async def on_resumed(self):
        self.voice_states_ready = True

    async def on_guild_channel_delete(self, channel):
        if playback.get(channel.id):
            await playback.pause(channel.id)
            playback.clear(channel.id)

    async def on_guild_remove(self, guild):
        channel_ids = [state.channel.id for state in list(playback.channels.values())
                       if state.channel.guild.id == guild.id]
        await asyncio.gather(*(playback.pause(channel_id) for channel_id in channel_ids))
        for channel_id in channel_ids:
            playback.clear(channel_id)

    async def on_voice_state_update(self, member, before, after):
        if before.channel and before.channel != after.channel and self.user and member.id == self.user.id:
            session = music.get(before.channel.id)
            if session and not session.cleaning:
                await playback.pause(before.channel.id)
        # Stop consuming bandwidth when everyone has left the voice channel.
        if before.channel and before.channel != after.channel:
            if not any(not user.bot for user in before.channel.members):
                session = music.get(before.channel.id)
                if not session or not session.cleaning:
                    await playback.pause(before.channel.id)


bot = SyncNestBot()
playback = PlaybackQueues(rooms, music, can_start=lambda: bot.voice_states_ready,
                          channel_lookup=bot.get_channel)


def voice_channel(interaction):
    if not bot.voice_states_ready:
        raise ValueError('Discord is reconnecting. Try again shortly.')
    member = interaction.user
    if not isinstance(member, discord.Member) or not member.voice:
        raise ValueError('Join a voice channel first.')
    if not isinstance(member.voice.channel, discord.VoiceChannel):
        raise ValueError('Use a regular voice channel, not a Stage channel.')
    return member.voice.channel


@bot.tree.command(name='help', description='Learn how to watch videos together and use the bot commands.')
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction):
    await interaction.response.send_message(
        '**SyncNest help**\n'
        'One bot handles shared video and music in your voice channel.\n\n'
        '**Video**\n'
        'Join voice on Discord desktop or Chrome/Edge. Use `/play url:<link>` or `/play file:<video>`. '
        'Open the Activity or follow **Join video player**. Friends join it too. '
        'If sound is blocked, click **Click to play with sound**.\n\n'
        '**Music**\n'
        'Use `/play mode:music url:<link>` to have the bot join voice and play audio. '
        'YouTube Music, Spotify track links, and audio attachments select music automatically. '
        'Spotify finds and labels a public YouTube match. '
        'Only one channel per server can use music at a time. The bot needs Connect and Speak.\n\n'
        '**Queue and controls**\n'
        '`/play` adds to one mixed video/music queue while busy (25 waiting items). '
        '`/queue` shows it; `/queue delete:2` or `delete:1,3-5` removes selected waiting items; '
        '`/queue clear:true` clears all waiting items. '
        '`/skip` advances. `/stop` ends the current item and keeps the queue paused; `/resume` continues. '
        'Only the person who added an item, or members with Move Members, can skip, stop, delete, or change it '
        'while that person is in the channel. '
        'Queues are lost on restart.\n'
        '`/resolution quality:<quality>` changes video quality for everyone: '
        '144p, 280p, 360p, 480p, 720p, 1080p, 1440p, 4K, or Highest possible. '
        'Default: 720p. Quality changes keep the same queue item.\n'
        '`/help` shows this private guide, even before joining voice.\n\n'
        '**Sources**\n'
        'YouTube, YouTube Music, Spotify tracks, X, Instagram, TikTok, Vimeo, Dailymotion, '
        'Twitch, Internet Archive, Odysee, BitChute, Rumble, and Kick. '
        'Public links only; no account cookies. X videos/live broadcasts and Instagram posts/reels are supported. '
        'Login-walled content may fail. Add individual tracks/videos; no playlist imports.\n'
        'Video files: MP4, MOV, M4V, MKV, WebM, AVI. '
        'Audio files: MP3, M4A, AAC, WAV, FLAC, OGG, Opus. Discord’s upload limit applies.',
        ephemeral=True,
    )


@bot.tree.command(name='play', description='Play a video Activity or music in your voice channel.')
@app_commands.guild_only()
@app_commands.describe(url='Public video/live, YouTube Music, or Spotify track URL. See /help.')
@app_commands.describe(resolution='Maximum video resolution. Defaults to 720p.')
@app_commands.describe(file='Video or audio attachment. Choose file or url; audio files use music mode.')
@app_commands.describe(mode='Video opens the Activity; music plays through the bot in voice.')
@app_commands.choices(resolution=RESOLUTION_CHOICES)
@app_commands.choices(mode=[app_commands.Choice(name='Video (Activity)', value='video'),
                           app_commands.Choice(name='Music only', value='music')])
async def play(interaction: discord.Interaction, url: str | None = None, resolution: str = '720',
               file: discord.Attachment | None = None, mode: str | None = None):
    try:
        channel = voice_channel(interaction)
        if (url is None) == (file is None):
            raise ValueError('Choose exactly one source: a video URL or an attached video file.')
        media_type = (file.content_type or '').split(';', 1)[0].strip().lower() if file else ''
        if file:
            url = file.url
        spotify = spotify_url(url)
        if mode is None:
            mode = ('music' if spotify or urlsplit(url).hostname == 'music.youtube.com'
                    or media_type.startswith('audio/')
                    or file and Path(urlsplit(url).path).suffix.lower() in AUDIO_FORMATS else 'video')
        if mode not in {'video', 'music'}:
            raise ValueError('Choose video or music mode.')
        if file is not None:
            if file.size <= 0 or (media_type and not media_type.startswith('video/')
                                  and not (mode == 'music' and media_type.startswith('audio/'))
                                  and media_type != 'application/octet-stream'):
                raise ValueError('Choose a non-empty video or music file.' if mode == 'music' else
                                 'Choose a non-empty video file (MP4, MOV, M4V, MKV, WebM, or AVI).')
        url = normalize_music_url(url) if mode == 'music' else normalize_video_url(url)
        resolution_height(resolution)
        if mode == 'music':
            permissions = channel.permissions_for(channel.guild.me)
            if not permissions.connect or not permissions.speak:
                raise ValueError('Give the bot Connect and Speak permissions in this voice channel.')
        item = QueueItem(url, mode, resolution, source_label(url, file), requester_id=interaction.user.id,
                         invite=channel.permissions_for(interaction.user).create_instant_invite)
        if playback.get(channel.id):
            _, position = playback.add(channel, item)
            await interaction.response.send_message(queued_message(channel.id, position))
            return
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    if mode == 'music':
        await interaction.response.defer(thinking=True)
        try:
            if voice_channel(interaction).id != channel.id:
                raise ValueError('You changed voice channels. Run /play again in your current channel.')
            session, position = playback.add(channel, item)
            if position:
                await interaction.edit_original_response(content=queued_message(channel.id, position))
                return
            await session.ready.wait()
            if session.status != 'playing':
                # The queue keeps a busy item first; another /play would only add a duplicate.
                hint = ' The queue is paused with this item first; use /resume.' if getattr(session, 'retryable', False) else ''
                raise ValueError((session.error or 'Music was stopped before playback started.') + hint)
        except ValueError as error:
            await interaction.edit_original_response(content=str(error))
            return
        await interaction.edit_original_response(content=playing_message(session, channel.id))
        return
    # A launch response opens the Activity in the interaction's channel.
    # Use it directly when /play is sent in the voice channel's own chat.
    if interaction.channel_id == channel.id:
        try:
            room, position = playback.add(channel, item)
            if position:
                await interaction.response.send_message(queued_message(channel.id, position))
                return
            started_item = playback.get(channel.id).current_item
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        try:
            await interaction.response.launch_activity()
        except discord.HTTPException:
            await playback.pause(channel.id, expected_item=started_item)
            await interaction.response.send_message(
                'Discord could not launch the Activity. Check Use Activities permission and try again.',
                ephemeral=True,
            )
        return
    await interaction.response.defer(thinking=True)
    try:
        if voice_channel(interaction).id != channel.id:
            raise ValueError('You changed voice channels. Run /play again in your current channel.')
        # Extraction can run while Discord creates the invite.
        room, position = playback.add(channel, item)
        if position:
            await interaction.edit_original_response(content=queued_message(channel.id, position))
            return
        started_item = playback.get(channel.id).current_item
    except ValueError as error:
        await interaction.edit_original_response(content=str(error))
        return
    if not item.invite:
        # Activity links are server invites; do not hand one to someone who cannot create invites.
        await interaction.edit_original_response(
            content='Starting video. Open the Activity from the voice channel to watch.')
        return
    try:
        view = await video_view(channel, bot.application_id)
    except discord.HTTPException:
        await playback.pause(channel.id, expected_item=started_item)
        await interaction.edit_original_response(content=(
            'Could not open the Activity. Enable Activities for this application '
            'and give the bot Create Invite permission in the voice channel.'
        ))
        return
    await interaction.edit_original_response(
        content='Starting video. Open the Activity to watch; playback starts automatically.',
        view=view,
    )


@bot.tree.command(name='resolution', description='Change the current shared video resolution without choosing a new URL.')
@app_commands.guild_only()
@app_commands.describe(quality='Select 720p, 1080p, or another quality for everyone in your voice channel.')
@app_commands.choices(quality=RESOLUTION_CHOICES)
async def resolution_command(interaction: discord.Interaction, quality: str):
    try:
        channel = voice_channel(interaction)
        resolution_height(quality)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.defer(thinking=True, ephemeral=True)
    try:
        if voice_channel(interaction).id != channel.id:
            raise ValueError('You changed voice channels. Run /resolution again in your current channel.')
        if music.get(channel.id):
            raise ValueError('Resolution applies to videos. Music playback has no video resolution.')
        state = playback.get(channel.id)
        check_control(interaction.user, channel, state and state.current_item, 'change the resolution of')
        previous = rooms.channels.get(channel.id)
        unchanged = previous and previous.resolution == quality
        room = await playback.change_resolution(channel.id, quality)
    except ValueError as error:
        await interaction.edit_original_response(content=str(error))
        return
    label = next(choice.name for choice in RESOLUTION_CHOICES if choice.value == quality)
    await interaction.edit_original_response(content=(
        f'The video is already set to {label}.' if unchanged else
        f'Switching everyone to {label}. The player will briefly reload. '
        + ('Live playback reconnects at the live edge.' if previous.is_live else
           f'Playback resumes near {int(room.start_time)} seconds. '
           'The actual resolution depends on what the video source offers.')
    ))


@bot.tree.command(name='stop', description='Stop the current item and keep the remaining queue paused.')
@app_commands.guild_only()
async def stop(interaction: discord.Interaction):
    try:
        channel = voice_channel(interaction)
    except ValueError as error:
        await interaction.response.send_message(str(error), ephemeral=True)
        return
    await interaction.response.defer(thinking=True)
    try:
        if voice_channel(interaction).id != channel.id:
            raise ValueError('You changed voice channels. Run /stop again in your current channel.')
        state = playback.get(channel.id)
        check_control(interaction.user, channel, state and state.current_item, 'stop')
    except ValueError as error:
        await interaction.edit_original_response(content=str(error))
        return
    was_music = music.get(channel.id) is not None
    stopped = await playback.pause(channel.id)
    state = playback.get(channel.id)
    if state and state.items:
        message = f'Playback stopped. Queue paused with {len(state.items)} waiting items. Use /resume to continue.'
    else:
        message = ('Music stopped and the bot disconnected.' if was_music else
                   'Video stopped for everyone.' if stopped else 'Nothing is playing.')
    await interaction.edit_original_response(content=message)


def queued_message(channel_id, position):
    state = playback.get(channel_id)
    return (f'Queued at position **{position}** in <#{channel_id}>.'
            + (' The queue is paused; use /resume to continue.' if state and state.paused else ''))


@bot.tree.error
async def command_error(interaction, error):
    log.error('Command failed.', exc_info=(type(error), error, error.__traceback__))
    message = 'The command failed. Check the server log and try again.'
    if interaction.response.is_done():
        await interaction.edit_original_response(content=message)
    else:
        await interaction.response.send_message(message, ephemeral=True)


install_queue_commands(bot, playback, voice_channel)


async def main():
    load_dotenv()
    # FFmpeg and yt-dlp's Deno inherit the environment; keep credentials in memory only.
    token = os.environ.pop('DISCORD_TOKEN', '').strip()
    secret = os.environ.pop('DISCORD_CLIENT_SECRET', '').strip()
    if not token or not secret or token.startswith('your_') or secret.startswith('your_'):
        raise SystemExit('Set DISCORD_TOKEN and DISCORD_CLIENT_SECRET in .env. See README.md.')
    if not shutil.which('ffmpeg') or not shutil.which('deno'):
        raise SystemExit('FFmpeg or Deno is missing. Rebuild the server image: docker compose build --pull')
    if not (WEB / 'dist' / 'app.js').is_file():
        raise SystemExit('Activity assets are missing. Rebuild the server image: docker compose build --no-cache')
    proxy_auth = os.getenv('DISCORD_PROXY_AUTH', '').strip().lower() in {'1', 'true', 'yes'}
    # Probe FFmpeg's options now, not inside the first playback request.
    supports_readrate_catchup()
    async with egress.EgressProxy() as proxy, bot:
        # Media fetches may only reach public addresses; Discord traffic stays direct.
        proxy.route_environment()
        await bot.login(token)
        # Abandoned long polls and downloads must not keep running after a disconnect.
        runner = web.AppRunner(create_app(bot, rooms, token, secret,
                                          bot.application.verify_key if proxy_auth else None),
                               access_log=None, handler_cancellation=True)
        await runner.setup()
        try:
            host, port = os.getenv('HOST', '127.0.0.1'), int(os.getenv('PORT', '8080'))
            await web.TCPSite(runner, host, port).start()
            log.info('Activity server is listening on the configured interface.')
            await bot.connect()
        finally:
            await playback.close()
            await asyncio.gather(rooms.close(), music.close())
            await runner.cleanup()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(levelname)s %(name)s: %(message)s')
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
    except discord.LoginFailure:
        raise SystemExit('Discord rejected DISCORD_TOKEN. Check the bot token in .env.') from None
