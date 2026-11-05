"""Discord authentication, room controls, and Activity files."""

import asyncio
import base64
import hashlib
import json
import math
import re
import secrets
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import aiohttp
import discord
from aiohttp import web
from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey

API = 'https://discord.com/api/v10'
WEB = Path(__file__).resolve().parents[2] / 'web'
HTTP = web.AppKey('http', aiohttp.ClientSession)
STATE_WAIT_SECONDS = 20
LOGIN_LIMIT = 20
LOGIN_WINDOW_SECONDS = 10
# A viewer needs about three requests per second (playlist, segment, state).
SESSION_BURST = 40
SESSION_RATE = 8
SESSIONS_PER_USER = 3
POLLS_PER_SESSION = 2


@dataclass
class Session:
    channel_id: int
    user_id: int
    expires: float
    updated: float
    tokens: float = SESSION_BURST
    polls: int = 0

    def spend(self):
        now = time.monotonic()
        self.tokens = min(SESSION_BURST, self.tokens + (now - self.updated) * SESSION_RATE)
        self.updated = now
        if self.tokens < 1:
            raise web.HTTPTooManyRequests(reason='Too many player requests. Try again shortly.',
                                          headers={'Retry-After': '1'})
        self.tokens -= 1


def proxy_verifier(public_key):
    """Accept only requests that Discord's Activity proxy signed for this application."""
    key = VerifyKey(bytes.fromhex(public_key))

    def verify(request):
        try:
            signature = request.headers['X-Signature-Ed25519']
            payload = base64.b64decode(request.headers['X-Discord-Proxy-Payload'], validate=True)
            # Discord's examples disagree on hex or base64 signatures; accept either.
            key.verify(payload, bytes.fromhex(signature) if re.fullmatch(r'[0-9a-fA-F]{128}', signature)
                       else base64.b64decode(signature, validate=True))
            data = json.loads(payload)
            if (str(data['created_at']) != request.headers['X-Signature-Timestamp']
                    or not data['expires_at'] >= time.time()):
                raise ValueError
        except (KeyError, TypeError, ValueError, BadSignatureError):
            raise web.HTTPUnauthorized(reason='Open SyncNest from Discord.') from None

    return verify


def member_voice(channel, user_id):
    member = channel.guild.get_member(user_id) if channel else None
    return member.voice if member else None


def create_app(bot, rooms, token: str, client_secret: str, proxy_key: str | None = None):
    sessions = {}
    verify_proxy = proxy_verifier(proxy_key) if proxy_key else None
    login_attempts = deque()
    discord_retry_at = 0
    assets = (WEB / 'dist' / 'app.js').read_bytes() + (WEB / 'dist' / 'style.css').read_bytes()
    version = hashlib.sha256(assets).hexdigest()[:12]
    # Only files produced by the build get immutable caching by hashed name.
    chunks = {path.relative_to(WEB / 'dist').as_posix()
              for path in (WEB / 'dist' / 'chunks').glob('*.js')}
    page = (WEB / 'index.html').read_text().replace('__CLIENT_ID__', str(bot.application_id))
    page = page.replace('__ASSET_VERSION__', version)

    @web.middleware
    async def errors(request, handler):
        try:
            response = await handler(request)
        except web.HTTPException as error:
            response = web.json_response({'error': error.reason}, status=error.status)
            if 'Retry-After' in error.headers:
                response.headers['Retry-After'] = error.headers['Retry-After']
        except (ValueError, TypeError, KeyError, AttributeError):
            response = web.json_response({'error': 'Invalid request.'}, status=400)
        response.headers['Cache-Control'] = 'private, no-store'
        static_path = request.path.removeprefix('/.proxy').removeprefix('/static/')
        if (response.status == 200 and request.path.startswith(('/static/', '/.proxy/static/'))
                and (request.query.get('v') == version or static_path in chunks)):
            # The version changes with the bundle, so reopening can use the browser cache.
            response.headers['Cache-Control'] = 'private, max-age=86400, immutable'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    app = web.Application(middlewares=[errors], client_max_size=4096)

    async def http_lifetime(app):
        app[HTTP] = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        yield
        await app[HTTP].close()

    app.cleanup_ctx.append(http_lifetime)

    def check_voice_states_ready():
        if not bot.voice_states_ready:
            raise web.HTTPServiceUnavailable(reason='Discord is reconnecting. Try again shortly.')

    def check_discord_cooldown():
        remaining = math.ceil(discord_retry_at - time.monotonic())
        if remaining > 0:
            raise web.HTTPTooManyRequests(reason='Discord is busy. Try again shortly.',
                                          headers={'Retry-After': str(remaining)})

    async def discord_json(method, path, **kwargs):
        nonlocal discord_retry_at
        check_discord_cooldown()
        try:
            async with app[HTTP].request(method, API + path, **kwargs) as response:
                if response.status == 429:
                    try:
                        delay = response.headers.get('Retry-After')
                        if delay is None:
                            body = await response.json()
                            delay = body.get('retry_after', 1) if isinstance(body, dict) else 1
                        delay = float(delay)
                        if not math.isfinite(delay):
                            delay = 1
                    except (aiohttp.ClientError, TypeError, ValueError):
                        delay = 1
                    discord_retry_at = max(discord_retry_at, time.monotonic() + max(1, delay))
                    check_discord_cooldown()
                if response.status != 200:
                    raise web.HTTPForbidden(reason='Discord could not verify this Activity session. Reopen the Activity.')
                return await response.json()
        except (aiohttp.ClientError, TimeoutError):
            raise web.HTTPServiceUnavailable(reason='Discord is temporarily unavailable. Try again.')

    async def login(request):
        # Reject requests that bypass Discord before they spend the shared login budget.
        if verify_proxy:
            verify_proxy(request)
        check_voice_states_ready()
        body = await request.json()
        code, instance_id = body.get('code'), body.get('instance_id')
        if (not isinstance(code, str) or not code.strip()
                or not isinstance(instance_id, str) or not instance_id.strip()
                # Quoting keeps the ID in one path segment, but URL normalization would
                # still resolve dot segments in the bot-authenticated request path.
                or instance_id in {'.', '..'} or len(instance_id) > 200):
            raise web.HTTPBadRequest(reason='Missing Discord authorization code or Activity instance.')
        check_discord_cooldown()
        now = time.monotonic()
        while login_attempts and login_attempts[0] <= now - LOGIN_WINDOW_SECONDS:
            login_attempts.popleft()
        if len(login_attempts) >= LOGIN_LIMIT:
            remaining = math.ceil(login_attempts[0] + LOGIN_WINDOW_SECONDS - now)
            raise web.HTTPTooManyRequests(reason='Too many login attempts. Try again shortly.',
                                          headers={'Retry-After': str(remaining)})
        login_attempts.append(now)
        oauth = await discord_json('POST', '/oauth2/token', data={
            'client_id': str(bot.application_id), 'client_secret': client_secret,
            'grant_type': 'authorization_code', 'code': code,
        })
        # Only a valid authorization code may trigger a bot-authenticated lookup.
        user, instance = await asyncio.gather(
            discord_json('GET', '/users/@me', headers={
                'Authorization': f"Bearer {oauth['access_token']}"}),
            discord_json(
                'GET', f"/applications/{bot.application_id}/activity-instances/{quote(instance_id, safe='')}",
                headers={'Authorization': f'Bot {token}'},
            ),
        )
        check_voice_states_ready()
        # The SDK's channel/user fields alone are not proof of membership.
        if str(user['id']) not in instance.get('users', []):
            raise web.HTTPForbidden(reason='Join this Activity in Discord first, then try again.')
        channel_id = int(instance['location']['channel_id'])
        channel = bot.get_channel(channel_id)
        if not isinstance(channel, discord.VoiceChannel):
            raise web.HTTPForbidden(reason='Open this Activity in a server voice channel.')
        voice = member_voice(channel, int(user['id']))
        if not voice or voice.channel.id != channel_id:
            raise web.HTTPForbidden(reason='Join the voice channel before opening its player.')
        now = time.monotonic()
        for key in list(sessions):
            if sessions[key].expires < now:
                del sessions[key]
        user_id = int(user['id'])
        # Each Activity open creates a session; a user keeps only the newest few.
        owned = [key for key, entry in sessions.items() if entry.user_id == user_id]
        for key in owned[:max(0, len(owned) - SESSIONS_PER_USER + 1)]:
            del sessions[key]
        session = secrets.token_urlsafe(32)
        sessions[session] = Session(channel_id, user_id, now + min(oauth.get('expires_in', 14400), 14400), now)
        room = rooms.channels.get(channel_id)
        return web.json_response({'access_token': oauth['access_token'], 'session': session,
                                  'revision': rooms.revision(channel_id),
                                  'room': room.snapshot() if room else None})

    def authorize(request):
        entry = sessions.get(request.headers.get('X-SyncNest-Session', ''))
        if not entry or entry.expires < time.monotonic():
            raise web.HTTPUnauthorized(reason='Your session expired. Reopen the Activity.')
        check_voice_states_ready()
        voice = member_voice(bot.get_channel(entry.channel_id), entry.user_id)
        if not voice or voice.channel.id != entry.channel_id:
            raise web.HTTPForbidden(reason='You left this voice channel. Rejoin to watch.')
        return entry

    async def state(request):
        session = authorize(request)
        session.spend()
        channel_id = session.channel_id
        revision = request.query.get('revision')
        deadline = asyncio.get_running_loop().time() + STATE_WAIT_SECONDS
        # The player holds one poll at a time; extra concurrent polls answer at once.
        hold = session.polls < POLLS_PER_SESSION
        session.polls += hold
        try:
            while hold and revision == str(rooms.revision(channel_id)):
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    break
                try:
                    await asyncio.wait_for(rooms.event(channel_id).wait(), min(1, remaining))
                except asyncio.TimeoutError:
                    pass
                # A held request must still revoke access promptly after leaving.
                authorize(request)
        finally:
            session.polls -= hold
        room = rooms.channels.get(channel_id)
        return web.json_response({'revision': rooms.revision(channel_id),
                                  'room': room.snapshot() if room else None})

    async def media(request):
        session = authorize(request)
        session.spend()
        room = rooms.channels.get(session.channel_id)
        name = request.match_info['name']
        if not room or room.id != request.match_info['room_id'] or not room.directory:
            raise web.HTTPNotFound(reason='This video has ended.')
        if not re.fullmatch(r'index\.m3u8|[0-9]{6,}\.ts', name):
            raise web.HTTPNotFound()
        path = room.directory / name
        if not path.is_file():
            raise web.HTTPNotFound(reason='This video segment is not available.')
        content_type = 'application/vnd.apple.mpegurl' if name.endswith('m3u8') else 'video/mp2t'
        return web.FileResponse(path, headers={'Content-Type': content_type})

    async def index(request):
        # The application ID is public; credentials never enter the page.
        return web.Response(text=page, content_type='text/html')

    # Discord strips /.proxy before forwarding; the alias also works locally.
    for prefix in ('', '/.proxy'):
        app.router.add_get(prefix + '/', index)
        app.router.add_post(prefix + '/api/login', login)
        app.router.add_get(prefix + '/api/state', state)
        app.router.add_get(prefix + '/media/{room_id}/{name}', media)
        app.router.add_static(prefix + '/static/', WEB / 'dist', show_index=False)
    return app
