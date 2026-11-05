"""Discord queue controls and playback announcements."""

import math
from urllib.parse import parse_qs, unquote, urlsplit

import discord
from discord import app_commands


def label(text, limit=90):
    return discord.utils.escape_markdown(' '.join(str(text).split())[:limit])


def may_control(member, channel, item):
    """The requester, voice moderators, or anyone once the requester has left the channel."""
    if item is None or not item.requester_id or item.requester_id == member.id:
        return True
    if channel.permissions_for(member).move_members:
        return True
    return all(user.id != item.requester_id for user in channel.members)


def check_control(member, channel, item, action):
    if not may_control(member, channel, item):
        raise ValueError(f'Only the person who added this item can {action} it while they are here, '
                         'unless you have Move Members.')


def source_label(url, file=None):
    parts = urlsplit(url)
    if file:
        return getattr(file, 'filename', None) or unquote(parts.path.rsplit('/', 1)[-1])
    if parts.hostname == 'www.youtube.com' and parts.path == '/watch':
        return 'YouTube ' + parse_qs(parts.query)['v'][0]
    return (parts.hostname or '') + unquote(parts.path)


def playing_message(session, channel_id):
    message = f'Playing **{label(session.title, 200)}** in <#{channel_id}>.'
    if getattr(session, 'spotify_title', None):
        message += (f'\nSpotify match for **{label(session.spotify_title, 300)}**: '
                    f'playing the [YouTube source]({session.matched_url}).')
    return message


async def video_view(channel, application_id):
    invite = await channel.create_invite(target_type=discord.InviteTarget.embedded_application,
        target_application_id=application_id, max_age=3600, unique=False)
    view = discord.ui.View()
    view.add_item(discord.ui.Button(label='Join video player', url=invite.url))
    return view


def install_queue_commands(client, queues, voice_channel):
    async def announce(item, session=None, error=None):
        channel = client.get_channel(item.channel_id)
        if (not channel or not client.voice_states_ready
                or not any(not member.bot for member in channel.members)):
            return
        if error:
            await channel.send(f'Queue paused: {error} Use /resume to retry.')
        elif session.status == 'error':
            await channel.send(f'Could not play **{label(item.label)}**: '
                               f'{session.error or "Source unavailable."} Skipping this item.')
        elif session.status in {'playing', 'ending'}:
            message, view = playing_message(session, channel.id), None
            if item.mode == 'video':
                # An Activity link is a server invite; only mint one for requesters who may.
                if item.invite:
                    try:
                        view = await video_view(channel, client.application_id)
                    except discord.HTTPException:
                        pass
                if not view:
                    message += '\nOpen the Activity from this voice channel to watch.'
            state = queues.get(channel.id)
            if state and state.current_item is item and not state.paused:
                await channel.send(message, view=view)

    queues.notify = announce

    @client.tree.command(name='queue', description='Show this channel’s mixed queue, or delete waiting items.')
    @app_commands.guild_only()
    @app_commands.describe(page='Queue page (10 waiting items per page).',
                           clear='Remove all waiting items. Current playback continues.',
                           delete='Waiting positions to delete, e.g. 2, 1,3 or 2-5. See /queue for numbers.')
    async def queue_command(interaction: discord.Interaction, page: app_commands.Range[int, 1, 3] = 1,
                            clear: bool = False, delete: str | None = None):
        try:
            channel = voice_channel(interaction)
            if clear and delete is not None:
                raise ValueError('Use either delete or clear in one command.')
            allowed = lambda item: may_control(interaction.user, channel, item)
            if delete is not None:
                positions = queues.remove(channel.id, delete, allowed)
                numbers = ', '.join(map(str, positions))
                message = (f'Removed {len(positions)} waiting items (positions {numbers}). '
                           'Current playback is unchanged. Use /queue for updated positions.')
            elif clear:
                message = f'Removed {queues.clear(channel.id, allowed)} waiting items. Current playback is unchanged.'
                state = queues.get(channel.id)
                if state and state.items:
                    message += f' Kept {len(state.items)} added by people still here.'
            else:
                state = queues.get(channel.id)
                if not state:
                    message = 'The queue is empty. Add a source with /play.'
                else:
                    items = list(state.items)
                    pages = max(1, math.ceil(len(items) / 10))
                    if not 1 <= page <= pages:
                        raise ValueError(f'Choose a queue page between 1 and {pages}.')
                    lines = ['**Paused queue** — use /resume.' if state.paused else '**Channel queue**']
                    if state.error:
                        lines.append(label(state.error, 140))
                    if state.current:
                        lines.append(f'Now ({state.current_item.mode}): **{label(state.current.title, 80)}**')
                    start = (page - 1) * 10
                    lines.extend(f'{index + 1}. [{item.mode}] {label(item.label, 60)}'
                                 for index, item in enumerate(items[start:start + 10], start))
                    lines.append(f'{len(items)} waiting · page {page}/{pages}.'
                                 + (f' Use /queue page:{page + 1} for the next page.' if page < pages else ''))
                    message = '\n'.join(lines)
        except ValueError as error:
            message = str(error)
        await interaction.response.send_message(message, ephemeral=True)

    @client.tree.command(name='resume', description='Continue this channel’s paused video/music queue.')
    @app_commands.guild_only()
    async def resume_command(interaction: discord.Interaction):
        await control(interaction, 'resume')

    @client.tree.command(name='skip', description='Skip the current item and play the next video or track.')
    @app_commands.guild_only()
    async def skip_command(interaction: discord.Interaction):
        await control(interaction, 'skip')

    async def control(interaction, action):
        try:
            channel = voice_channel(interaction)
        except ValueError as error:
            await interaction.response.send_message(str(error), ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        try:
            if voice_channel(interaction).id != channel.id:
                raise ValueError('You changed voice channels. Run the command again in your current channel.')
            if action == 'resume':
                queues.resume(channel)
                message = 'Queue resumed. The next item is loading.'
            else:
                state = queues.get(channel.id)
                check_control(interaction.user, channel, state and state.current_item, 'skip')
                await queues.skip(channel.id)
                message = 'Skipped the current item.'
        except ValueError as error:
            message = str(error)
        await interaction.edit_original_response(content=message)
