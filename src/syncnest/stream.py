"""One shared, bounded HLS stream per voice channel."""

import asyncio
import copy
import logging
import re
import subprocess
import tempfile
import time
import uuid
from collections import deque
from contextlib import AsyncExitStack
from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qs, quote, unquote, urlsplit

import yt_dlp
from yt_dlp.extractor.lbry import LBRYIE

from .archive_reader import ArchiveReader
from .music_sources import choose_youtube_track, spotify_metadata, spotify_url

log = logging.getLogger(__name__)
MAX_EXTRACTIONS = 2
EXTRACTION_TIMEOUT_SECONDS = 60
MAX_STREAMS = 4
_extractions = set()
_streams = set()
# Preserve each platform's helper extractors and generic CDN redirects without
# initializing and scanning extractors for over a thousand unrelated sites.
TWITTER_EXTRACTORS = ['twitter', 'twitter:card', 'twitter:amplify', 'twitter:broadcast']
TIKTOK_EXTRACTORS = ['tiktok', 'tiktok:live']
ALLOWED_EXTRACTORS = [
    'youtube.*', 'vimeo.*', 'dailymotion.*', 'twitch.*', 'archive.org',
    'lbry.*', 'bitchute.*', 'rumble.*', 'kick.*', *TWITTER_EXTRACTORS,
    'instagram', *TIKTOK_EXTRACTORS, 'generic',
]
RESOLUTIONS = {'144': 144, '280': 280, '360': 360, '480': 480, '720': 720,
               '1080': 1080, '1440': 1440, '2160': 2160, 'best': None}
ATTACHMENT_FORMATS = {'.mp4': 'mov', '.mov': 'mov', '.m4v': 'mov',
                      '.mkv': 'matroska', '.webm': 'matroska', '.avi': 'avi'}
AUDIO_FORMATS = {'.mp3': 'mp3', '.m4a': 'mov', '.aac': 'aac', '.wav': 'wav',
                 '.flac': 'flac', '.ogg': 'ogg', '.opus': 'ogg'}
ARCHIVE_FORMATS = ATTACHMENT_FORMATS | {'.ogv': 'ogg', '.mpg': 'mpeg', '.mpeg': 'mpeg', '.ts': 'mpegts'}


class MediaBusy(ValueError):
    """Temporary media capacity exhaustion; the source can be retried."""


def resolution_height(resolution: str) -> int | None:
    if resolution not in RESOLUTIONS:
        raise ValueError('Choose 144p, 280p, 360p, 480p, 720p, 1080p, 1440p, 4K, or Highest possible.')
    return RESOLUTIONS[resolution]


def normalize_video_url(value: str, *, audio_only=False) -> str:
    """Accept individual videos on the supported sites, never arbitrary hosts."""
    value = value.strip()
    if any(ord(char) < 32 for char in value) or '\\' in value:
        raise ValueError('Use a full video URL without control characters or backslashes.')
    parts = urlsplit(value)
    if parts.scheme not in {'http', 'https'} or parts.username or parts.password:
        raise ValueError('Use a full video URL from a supported platform.')
    if parts.port not in {None, 80, 443}:
        raise ValueError('Use a standard video URL.')
    host = parts.hostname
    if host == 'cdn.discordapp.com':
        filename = unquote(parts.path.rsplit('/', 1)[-1])
        if (parts.scheme == 'https' and parts.port in {None, 443}
                and re.fullmatch(r'/(?:ephemeral-)?attachments/[0-9]+/[0-9]+/[^/]+', parts.path)
                and not any(ord(char) < 32 or char in '/\\' for char in filename)
                and Path(filename).suffix.lower() in (ATTACHMENT_FORMATS | AUDIO_FORMATS if audio_only else ATTACHMENT_FORMATS)):
            return parts._replace(netloc=host, fragment='').geturl()
        raise ValueError('Attach a supported video or audio file from Discord.' if audio_only else
                         'Attach an MP4, MOV, M4V, MKV, WebM, or AVI video from Discord.')
    path = parts.path.strip('/').split('/')
    video_id = ''
    if host == 'youtu.be' and len(path) == 1:
        video_id = path[0]
    elif host in {'youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com'}:
        if parts.path == '/watch':
            video_id = parse_qs(parts.query).get('v', [''])[0]
        elif len(path) == 2 and path[0] in {'shorts', 'live', 'embed'}:
            video_id = path[1]
        elif re.fullmatch(r'/(?:@[\w.-]+|channel/UC[A-Za-z0-9_-]{22}|(?:c|user)/[\w.-]+)/live',
                          parts.path.rstrip('/')):
            return 'https://www.youtube.com' + parts.path.rstrip('/')
    if video_id:
        if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id):
            raise ValueError('Provide one YouTube video link, not a playlist or search.')
        return f'https://www.youtube.com/watch?v={video_id}'

    if host in {'x.com', 'www.x.com', 'm.x.com', 'mobile.x.com',
                'twitter.com', 'www.twitter.com', 'm.twitter.com', 'mobile.twitter.com'}:
        cleaned = parts.path.rstrip('/')
        if (re.fullmatch(r'/(?:(?:i/web|[A-Za-z0-9_]+)/status|statuses)/[0-9]+(?:/video/[1-9][0-9]*)?', cleaned)
                or re.fullmatch(r'/i/broadcasts/[A-Za-z0-9_]+', cleaned)):
            return 'https://x.com' + cleaned

    if host in {'instagram.com', 'www.instagram.com'}:
        cleaned = parts.path.rstrip('/')
        match = re.fullmatch(r'(?:/(?!share/)[A-Za-z0-9_.]+)?/(p|tv|reels?)/([A-Za-z0-9_-]+)', cleaned)
        if match:
            kind = 'reel' if match[1] == 'reels' else match[1]
            return f'https://www.instagram.com/{kind}/{match[2]}/'

    if host in {'tiktok.com', 'www.tiktok.com', 'm.tiktok.com'}:
        cleaned = parts.path.rstrip('/')
        if re.fullmatch(r'/(?:@[A-Za-z0-9_.-]+/(?:video/[0-9]+|live)|(?:share/video|embed)/[0-9]+)', cleaned):
            return 'https://www.tiktok.com' + cleaned
        if host == 'm.tiktok.com' and re.fullmatch(r'/share/live/[0-9]+', cleaned):
            return 'https://m.tiktok.com' + cleaned

    if host in {'vimeo.com', 'www.vimeo.com', 'player.vimeo.com'}:
        pattern = (r'/video/[0-9]+' if host == 'player.vimeo.com' else
                   r'/(?:[0-9]+(?:/[a-f0-9]{10})?|channels/[^/]+/[0-9]+|'
                   r'groups/[^/]+/videos/[0-9]+|(?:album|showcase)/[0-9]+/video/[0-9]+)')
        if re.fullmatch(pattern, parts.path.rstrip('/')):
            # Unlisted videos need their path hash or the player's ?h= token.
            return parts._replace(scheme='https', netloc=host, fragment='').geturl()

    if host in {'dailymotion.com', 'www.dailymotion.com', 'touch.dailymotion.com',
                'geo.dailymotion.com', 'dai.ly'}:
        pattern = r'/([A-Za-z0-9]+)' if host == 'dai.ly' else r'/(?:embed/)?video/([A-Za-z0-9]+)(?:_[^/]+)?'
        match = re.fullmatch(pattern, parts.path.rstrip('/'))
        if match:
            return f'https://www.dailymotion.com/video/{match[1]}'

    if host in {'twitch.tv', 'www.twitch.tv', 'm.twitch.tv', 'go.twitch.tv', 'clips.twitch.tv'}:
        clip = (re.fullmatch(r'/([A-Za-z0-9_-]+)', parts.path.rstrip('/'))
                if host == 'clips.twitch.tv' else
                re.fullmatch(r'/(?:[A-Za-z0-9_]+/)?clip/([A-Za-z0-9_-]+)', parts.path.rstrip('/')))
        if clip:
            return f'https://clips.twitch.tv/{clip[1]}'
        if host != 'clips.twitch.tv':
            vod = re.fullmatch(r'/(?:videos|[A-Za-z0-9_]+/v(?:ideo)?)/([0-9]+)', parts.path.rstrip('/'))
            if vod:
                return f'https://www.twitch.tv/videos/{vod[1]}'
            if (len(path) == 1 and re.fullmatch(r'[A-Za-z0-9_]{1,25}', path[0])
                    and path[0].lower() not in {
                        'directory', 'downloads', 'search', 'videos', 'clips', 'collections',
                        'settings', 'inventory', 'subscriptions', 'wallet', 'login', 'signup',
                    }):
                return f'https://www.twitch.tv/{path[0].lower()}'

    if host in {'archive.org', 'www.archive.org'}:
        decoded = unquote(parts.path).strip('/')
        match = re.fullmatch(r'(details|embed|download)/([A-Za-z0-9_.-]+)(?:/(.+))?', decoded)
        if match:
            kind, item, filename = match.groups()
            if (item not in {'.', '..'} and not any(ord(char) < 32 for char in decoded)
                    and '\\' not in decoded and
                    (not filename or all(part not in {'', '.', '..'} for part in filename.split('/')))):
                if kind == 'download':
                    if not filename or Path(filename).suffix.lower() not in (ARCHIVE_FORMATS | AUDIO_FORMATS if audio_only else ARCHIVE_FORMATS):
                        raise ValueError('Use an Internet Archive video item or a direct video file link.')
                    # Keep direct file links: a derivative need not be a playlist entry.
                    return f'https://archive.org/download/{item}/{quote(filename)}'
                suffix = f'/{quote(filename)}' if filename else ''
                return f'https://archive.org/details/{item}{suffix}'

    if host in {'odysee.com', 'www.odysee.com'}:
        decoded = unquote(parts.path).rstrip('/').replace('#', ':')
        # LBRY claim IDs can appear as a URL fragment as well as after a colon.
        if re.fullmatch(r'[0-9a-f]{1,40}', parts.fragment) and ':' not in decoded.rsplit('/', 1)[-1]:
            decoded += ':' + parts.fragment
        name = r'[^$@:/?#&\s\\]+'
        claim = name + r'(?::[0-9a-f]{1,40})?'
        pattern = rf'/(?:\$/(?:embed|download)/)?(?:(?:@{claim}/)?{claim}|{name}/[0-9a-f]{{1,40}})'
        if (re.fullmatch(pattern, decoded)
                and all(part not in {'.', '..'} for part in decoded.split('/'))
                and not any(ord(char) < 32 for char in decoded)
                and decoded.lower() not in {
                    '/about', '/discover', '/search', '/settings', '/signin', '/login',
                    '/signup', '/help', '/terms', '/privacy', '/tos', '/licenses', '/invite',
                }):
            return parts._replace(scheme='https', netloc='odysee.com',
                                  path=quote(decoded, safe="/:@$!(),'"), fragment='').geturl()

    if host in {'bitchute.com', 'www.bitchute.com', 'old.bitchute.com'}:
        match = re.fullmatch(r'/(?:video|embed)/([A-Za-z0-9_-]+)', parts.path.rstrip('/'))
        if match:
            return f'https://www.bitchute.com/video/{match[1]}'

    if host in {'rumble.com', 'www.rumble.com'}:
        cleaned = parts.path.rstrip('/')
        if (re.fullmatch(r'/v(?!ideos)[A-Za-z0-9][A-Za-z0-9_.-]*', cleaned)
                or re.fullmatch(r'/embed/(?:[0-9a-z]+\.)?[0-9a-z]+', cleaned)):
            return parts._replace(scheme='https', netloc='rumble.com', path=cleaned, fragment='').geturl()

    if host in {'kick.com', 'www.kick.com'}:
        cleaned = parts.path.rstrip('/')
        channel_pattern = r'[A-Za-z0-9_-]+'
        reserved = {'video', 'videos', 'categories', 'category', 'search', 'auth', 'browse',
                    'discover', 'clips', 'login', 'signup', 'settings', 'dashboard',
                    'following', 'subscriptions', 'help', 'about', 'terms', 'privacy'}
        channel = path[0].lower()
        if channel not in reserved:
            clip = re.fullmatch(rf'/({channel_pattern})/clips/(clip_[A-Za-z0-9_-]+)', cleaned)
            if clip:
                return f'https://kick.com/{clip[1].lower()}/clips/{clip[2]}'
            vod = re.fullmatch(rf'/({channel_pattern})/videos/([0-9a-f]{{8}}-(?:[0-9a-f]{{4}}-){{3}}[0-9a-f]{{12}})',
                               cleaned, re.IGNORECASE)
            if vod:
                return f'https://kick.com/{vod[1].lower()}/videos/{vod[2].lower()}'
            if re.fullmatch(rf'/{channel_pattern}', cleaned):
                clips = parse_qs(parts.query, keep_blank_values=True).get('clip')
                if clips is None:
                    return f'https://kick.com/{channel}'
                if len(clips) == 1 and re.fullmatch(r'clip_[A-Za-z0-9_-]+', clips[0]):
                    return f'https://kick.com/{channel}/clips/{clips[0]}'
                raise ValueError('Use a Kick link to one valid clip.')

    raise ValueError('Use one public YouTube, X, Instagram, TikTok, Vimeo, Dailymotion, Twitch, Internet Archive, Odysee, BitChute, Rumble, or Kick video link, not a profile, playlist, or search.')


class OdyseeIE(LBRYIE):
    """Use the same public SDK endpoint as Odysee's current web player."""
    @classmethod
    def ie_key(cls):
        return 'LBRY'

    def _download_json(self, url_or_request, *args, **kwargs):
        if url_or_request == 'https://api.lbry.tv/api/v1/proxy':
            url_or_request = 'https://api.na-backend.odysee.com/api/v1/proxy'
        return super()._download_json(url_or_request, *args, **kwargs)


def extraction_options(resolution, *, audio_only=False):
    height = resolution_height(resolution)
    highest = 'bestvideo*+bestaudio/best/bestvideo'
    limit = f'[height<=?{height}]' if height else ''
    return {
        'format': ('bestaudio/best' if audio_only else
                   f'bestvideo*{limit}+bestaudio/best{limit}/bestvideo{limit}/{highest}' if height else highest),
        # Output is always 8-bit SDR H.264 with stereo AAC. Within a resolution,
        # prefer sources that need no tone mapping, decode cheaply, and keep
        # stereo AAC eligible for stream copy instead of a 5.1 downmix.
        'format_sort': ['res', 'fps', 'hdr:SDR', 'vcodec:h264', 'channels:2', 'acodec:aac'],
        'format_sort_force': True,
        'noplaylist': True, 'quiet': True,
        'allowed_extractors': ALLOWED_EXTRACTORS,
        'extract_flat': 'in_playlist',
        'socket_timeout': 15, 'retries': 2, 'extractor_retries': 2,
    }


def select_video(info, resolution):
    """Select from a room's fresh recording formats without another site request."""
    resolution_height(resolution)
    if not info.get('formats'):
        return copy.deepcopy(info)
    # Reprocess metadata, not the previous rendition's aggregate fields. In
    # particular, a previous split selection must not survive a combined one.
    metadata = {key: copy.deepcopy(info[key]) for key in
                ('id', 'title', 'formats', 'duration', 'is_live', 'live_status', 'http_headers',
                 'extractor', 'extractor_key')
                if key in info}
    with yt_dlp.YoutubeDL(extraction_options(resolution), auto_init=False) as ydl:
        return validate_video(ydl.process_ie_result(metadata, download=False))


def extract_video(url: str, resolution: str = '720', *, audio_only=False) -> dict:
    resolution_height(resolution)
    spotify = spotify_url(url) if audio_only else None
    url = spotify or normalize_video_url(url, audio_only=audio_only)
    parts = urlsplit(url)
    if parts.hostname == 'cdn.discordapp.com':
        return {'url': url, 'title': 'Uploaded audio' if audio_only else 'Uploaded video',
                '_input_format': (ATTACHMENT_FORMATS | AUDIO_FORMATS)[Path(unquote(parts.path)).suffix.lower()]}
    if parts.hostname == 'archive.org' and parts.path.startswith('/download/'):
        # The validated URL already points to the chosen media file. FFmpeg
        # follows Archive's CDN redirects; yt-dlp would fetch it once more first.
        return {'url': url, 'title': Path(unquote(parts.path)).name}
    options = extraction_options(resolution, audio_only=audio_only)
    if spotify:
        options['allowed_extractors'] = ['youtube.*']
    if parts.hostname == 'x.com':
        # X cards can link to arbitrary hosts; only follow X video helpers.
        options['allowed_extractors'] = TWITTER_EXTRACTORS
    elif parts.hostname in {'www.tiktok.com', 'm.tiktok.com'}:
        options['allowed_extractors'] = TIKTOK_EXTRACTORS
    with yt_dlp.YoutubeDL(options) as ydl:
        if parts.hostname == 'odysee.com':
            ydl.add_info_extractor(OdyseeIE())
        track = None
        if spotify:
            track = spotify_metadata(spotify, ydl)
            query = ' '.join(track['artists']) + ' ' + track['title'] + ' official audio'
            search = ydl.extract_info('ytsearch5:' + query, download=False)
            url = choose_youtube_track(track, search.get('entries', []) if search else [])
        info = validate_video(ydl.extract_info(url, download=False), audio_only=audio_only)
        if track:
            info['spotify_title'] = ', '.join(track['artists']) + ' - ' + track['title']
            info['matched_url'] = url
        return info


async def extract_video_async(url, resolution, *, audio_only=False):
    if len(_extractions) >= MAX_EXTRACTIONS:
        raise MediaBusy('Media loading is busy.')
    work = (asyncio.to_thread(extract_video, url, resolution, audio_only=True) if audio_only else
            asyncio.to_thread(extract_video, url, resolution))
    extraction = asyncio.create_task(work)
    _extractions.add(extraction)

    def finished(task):
        _extractions.discard(task)
        if not task.cancelled():
            task.exception()  # A stopped room no longer awaits extraction errors.

    extraction.add_done_callback(finished)
    # Stopped or timed-out extraction threads keep their slots until they finish.
    done, _ = await asyncio.wait({extraction}, timeout=EXTRACTION_TIMEOUT_SECONDS)
    if not done:
        raise TimeoutError
    return extraction.result()


def validate_video(info, *, audio_only=False):
    if info and 'entries' in info:
        raise ValueError('This link contains multiple videos. Use a link to one video or an Internet Archive video file.')
    if not info or info.get('acodec' if audio_only else 'vcodec') == 'none':
        raise ValueError('No playable audio found.' if audio_only else 'No playable video found.')
    for source in info.get('requested_formats', [info]):
        if urlsplit(source.get('url', '')).scheme not in {'http', 'https'}:
            raise ValueError('No HTTP video stream found.')
    return info


def is_archive_url(url):
    host = urlsplit(url).hostname or ''
    return host == 'archive.org' or host.endswith('.archive.org')


@lru_cache(maxsize=1)
def supports_readrate_catchup():
    # FFmpeg 7 supports the rest of the pipeline; newer builds can refill a
    # buffer after a stall. Inspect once rather than adding a process per play.
    try:
        help_text = subprocess.run(['ffmpeg', '-hide_banner', '-h', 'full'],
                                   capture_output=True, text=True, timeout=5, check=True).stdout
        return '-readrate_catchup' in help_text
    except (OSError, subprocess.SubprocessError):
        return False


def ffmpeg_input_args(info: dict, start_time: float = 0) -> list[str]:
    args = []
    sources = info.get('requested_formats', [info])
    if info.get('extractor_key') == 'KickClip':
        # Capture clips can contain a multi-second jump in both audio and
        # video timestamps. Correct it before readrate creates a false stall.
        args += ['-dts_delta_threshold', '1']
    for source in sources:
        # Fill the initial playback buffer quickly, then stay at real-time speed.
        # Only a few seconds are read ahead; old segments keep being deleted.
        if not (info.get('is_live') or info.get('live_status') == 'is_live'):
            args += ['-re', '-readrate_initial_burst', '5']
            if supports_readrate_catchup():
                args += ['-readrate_catchup', '1.5']
        args += ['-rw_timeout', '15000000']
        if urlsplit(source['url']).scheme in {'http', 'https'}:
            # Retry transient errors without restarting a recording from zero.
            # The exponential delay cap bounds retries; 403/404 and EOF are final.
            args += ['-reconnect', '1', '-reconnect_on_network_error', '1',
                     '-reconnect_on_http_error', '429,500,502,503,504',
                     '-reconnect_delay_max', '2']
        # Known codecs and self-describing Archive containers need less
        # inspection. Unknown raw media keeps full probing.
        container = Path(unquote(urlsplit(source['url']).path)).suffix.lower()
        archive_source = is_archive_url(source['url'])
        archive_container = (archive_source
                             and container in {'.mp4', '.m4v', '.mov', '.mkv', '.webm'})
        input_format = source.get('_input_format')
        if archive_source:
            input_format = (ARCHIVE_FORMATS | AUDIO_FORMATS).get(container)
            if not input_format:
                raise ValueError('Use an Archive video file in MP4, MOV, M4V, MKV, WebM, AVI, OGV, MPEG, or TS format.')
        elif info.get('extractor_key') == 'LBRY' and source.get('format_id') == 'original':
            # Odysee originals retain uploaded bytes, unlike its transcoded HLS streams.
            input_format = (ARCHIVE_FORMATS | AUDIO_FORMATS).get(f'.{source.get("ext", "")}'.lower())
            if not input_format:
                raise ValueError('Use an Odysee original in MP4, MOV, M4V, MKV, WebM, AVI, OGV, MPEG, or TS format.')
        if source.get('vcodec') and source.get('acodec') or archive_container:
            args += ['-analyzeduration', '1000000', '-probesize', '1000000']
        headers = source.get('http_headers', info.get('http_headers', {}))
        if headers:
            args += ['-headers', ''.join(f'{key}: {value}\r\n' for key, value in headers.items())]
        if input_format:
            # Uploaded bytes must not be autodetected as playlists or open host files.
            # httpproxy carries HTTPS through the bot's public-only egress proxy.
            protocols = 'http,https,tcp,tls,httpproxy' + (',async' if archive_container else '')
            args += ['-protocol_whitelist', protocols, '-f', input_format]
        elif urlsplit(source['url']).scheme in {'http', 'https'}:
            # Platform manifests open nested URLs. Allow only what the egress proxy vets:
            # tcp carries HTTP, and HLS/DASH refuse bare tcp:// entries. No udp, rtp, or file.
            args += ['-protocol_whitelist', 'http,https,tcp,tls,httpproxy,crypto,data']
        # Read Archive files ahead in a bounded background buffer so network
        # I/O does not wait for decoding or real-time pacing. Seeking still works.
        input_url = source.get('_input_url') or ('async:' + source['url'] if archive_container else source['url'])
        if start_time > 0:
            args += ['-ss', f'{start_time:.3f}']
        args += ['-i', input_url]
    return args


def ffmpeg_args(info: dict, directory: Path, resolution: str = '720', start_time: float = 0) -> list[str]:
    height = resolution_height(resolution)
    args = ['ffmpeg', '-hide_banner', '-loglevel', 'warning', '-nostdin', '-y']
    args += ffmpeg_input_args(info, start_time)
    sources = info.get('requested_formats', [info])
    # Silent films (especially Archive items) can legitimately have no audio.
    args += ['-map', '0:v:0', '-map', '1:a:0' if len(sources) == 2 else '0:a:0?']
    # One-second segments expose the first playable frames sooner.
    # Never upscale. H.264 4:2:0 requires even dimensions; highest mode otherwise
    # retains the source dimensions instead of shrinking everything to 720p.
    scale = (f"scale=w=-2:h='trunc(min({height},ih)/2)*2'" if height else
             "scale=w='trunc(iw/2)*2':h='trunc(ih/2)*2'")
    # Discard excess frames before resizing: 4K/60 inputs otherwise perform
    # twice the scaling work even though the player only receives 30 fps.
    args += [
        '-vf', 'fps=30,' + scale,
        '-c:v', 'libx264', '-preset', 'veryfast', '-tune', 'zerolatency',
        '-crf', '25', '-threads', '4' if height is None or height >= 1080 else '2',
        '-pix_fmt', 'yuv420p', '-g', '30', '-keyint_min', '30', '-sc_threshold', '0',
    ]
    audio = sources[1] if len(sources) == 2 else sources[0]
    # Stereo AAC-LC is already suitable for the player. Avoid
    # decoding/encoding it again; unknown or incompatible audio still converts.
    if (audio.get('acodec') == 'mp4a.40.2'
            and audio.get('audio_channels') in {1, 2}
            and audio.get('asr') in {44100, 48000}
            and 0 < (audio.get('abr') or 0) <= 160):
        args += ['-c:a', 'copy']
    else:
        args += ['-c:a', 'aac', '-b:a', '128k', '-ac', '2', '-ar', '48000']
    args += [
        '-f', 'hls', '-hls_time', '1', '-hls_list_size', '16', '-hls_delete_threshold', '4',
        '-hls_flags', 'delete_segments+independent_segments+program_date_time+temp_file',
        '-hls_segment_filename', str(directory / '%06d.ts'), str(directory / 'index.m3u8'),
    ]
    return args


def buffered_seconds(directory):
    try:
        playlist = (directory / 'index.m3u8').read_text()
    except OSError:
        return 0
    return sum(float(seconds) for seconds in re.findall(r'^#EXTINF:([0-9.]+)', playlist, re.MULTILINE))


def start_stream_task(run):
    if len(_streams) >= MAX_STREAMS:
        raise MediaBusy('The media server is busy. Try again shortly.')
    task = asyncio.create_task(run())
    _streams.add(task)
    task.add_done_callback(_streams.discard)
    return task


class Room:
    def __init__(self, channel_id: int, url: str, on_change, resolution: str = '720', start_time: float = 0,
                 source_info=None, source_extracted_at=0, resume_from=None):
        if len(_streams) >= MAX_STREAMS:
            raise MediaBusy('The video server is busy. Try again shortly.')
        self.id = uuid.uuid4().hex
        self.channel_id = channel_id
        self.url = url
        self.resolution = resolution
        self.start_time = start_time
        self.is_live = False
        self.changing_resolution = False
        self.stop_requests = 0
        self.pending_room = None
        self.ready = asyncio.Event()
        self.source_info = source_info
        self.source_extracted_at = source_extracted_at
        self.resume_from = resume_from
        self.warm_start = resume_from is not None
        self.title = 'Loading video…'
        self._status = 'loading'
        self.on_change = on_change
        self.error = None
        self.retryable = False
        self.directory = None
        self.process = None
        self.cleaning = False
        self.task = start_stream_task(self.run)

    @property
    def status(self):
        return self._status

    @status.setter
    def status(self, value):
        if value != self._status:
            self._status = value
            if value in {'playing', 'ending', 'finished', 'error', 'stopped'}:
                self.ready.set()
            self.on_change(self.channel_id)

    @property
    def active(self):
        return not self.task.done()

    def snapshot(self):
        return {'id': self.id, 'title': self.title, 'status': self.status, 'error': self.error,
                'buffer_seconds': 5 if self.is_live else 3}

    def playback_position(self):
        if self.is_live:
            return 0
        if self.directory:
            try:
                playlist = (self.directory / 'index.m3u8').read_text()
                sequence = re.search(r'^#EXT-X-MEDIA-SEQUENCE:(\d+)$', playlist, re.MULTILINE)
                durations = re.findall(r'^#EXTINF:([0-9.]+)', playlist, re.MULTILINE)
                # Segments start every second. The shared player follows the
                # output about three seconds behind its latest segment.
                if sequence and durations:
                    return self.start_time + max(0, int(sequence[1]) + sum(map(float, durations)) - 3)
            except (OSError, ValueError):
                pass
        return self.start_time

    async def run(self):
        started = asyncio.get_running_loop().time()
        tail = deque(maxlen=12)
        stderr_task = None
        temporary = None
        inputs = AsyncExitStack()
        try:
            if self.source_info is None:
                self.source_info = await extract_video_async(self.url, self.resolution)
                self.source_extracted_at = time.monotonic()
            info = self.source_info
            log.info('Stream extraction took %.2fs.', asyncio.get_running_loop().time() - started)
            self.title = str(info.get('title') or 'Video')[:200]
            self.is_live = bool(info.get('is_live') or info.get('live_status') == 'is_live')
            if self.is_live:
                self.start_time = 0
            elif self.resume_from:
                self.start_time = self.resume_from.playback_position()
            self.resume_from = None
            self.status = 'starting'
            # Keep cached source metadata free of temporary loopback URLs.
            info = dict(info)
            sources = [dict(source) for source in info.get('requested_formats', [info])]
            for source in sources:
                if (is_archive_url(source['url']) and
                        Path(urlsplit(source['url']).path).suffix.lower() in {'.mp4', '.m4v', '.mov', '.mkv', '.webm'}):
                    input_url = await inputs.enter_async_context(ArchiveReader(
                        source['url'], source.get('http_headers', info.get('http_headers'))))
                    if input_url != source['url']:
                        source['_input_url'] = input_url
            if 'requested_formats' in info:
                info['requested_formats'] = sources
            else:
                info = sources[0]
            temporary = tempfile.TemporaryDirectory(prefix='syncnest-stream-')
            self.directory = Path(temporary.name)
            spawning = asyncio.create_task(asyncio.create_subprocess_exec(
                *ffmpeg_args(info, self.directory, self.resolution, self.start_time),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
            ))
            try:
                self.process = await asyncio.shield(spawning)
            except asyncio.CancelledError:
                # Even a stop during process creation must reap the new FFmpeg process.
                self.process = await spawning
                raise

            async def collect_errors():
                while line := await self.process.stderr.readline():
                    # Keep diagnostics bounded and omit signed media URLs.
                    tail.append(re.sub(r'https?://\S+', '[media URL]', line.decode(errors='replace').strip()))

            stderr_task = asyncio.create_task(collect_errors())
            for _ in range(600):
                duration = buffered_seconds(self.directory)
                buffer_size = 5 if self.is_live else 3
                if duration > 0 and (not self.warm_start or duration >= buffer_size):
                    self.status = 'playing'
                    log.info('First video segment ready after %.2fs.',
                             asyncio.get_running_loop().time() - started)
                    break
                if self.process.returncode is not None:
                    break
                await asyncio.sleep(0.05)
            else:
                raise RuntimeError('FFmpeg did not produce video within 30 seconds.')
            code = await self.process.wait()
            await stderr_task
            if code:
                raise RuntimeError('The platform rejected the stream or FFmpeg failed. ' + ' '.join(tail)[-900:])
            if buffered_seconds(self.directory) <= 0:
                raise RuntimeError('The source ended before producing any video frames.')
            self.status = 'ending'
            # Let viewers consume the final segments before removing the buffer.
            await asyncio.sleep(20)
            self.status = 'finished'
        except asyncio.CancelledError:
            self.status = 'stopped'
            raise
        except asyncio.TimeoutError:
            self.error = 'Loading timed out. Use /play again.'
            self.status = 'error'
        except ValueError as error:
            self.error = str(error)
            self.retryable = isinstance(error, MediaBusy)
            self.status = 'error'
        except Exception as error:
            log.error('Video stream failed: %s', error)
            self.error = 'Could not stream this video. Use /play with its link or attach the file again; see the server log.'
            self.status = 'error'
        finally:
            self.cleaning = True
            if self.process and self.process.returncode is None:
                try:
                    self.process.terminate()
                except ProcessLookupError:
                    pass
                try:
                    await asyncio.wait_for(self.process.wait(), 5)
                except asyncio.TimeoutError:
                    self.process.kill()
                    await self.process.wait()
            if stderr_task:
                await asyncio.gather(stderr_task, return_exceptions=True)
            await inputs.aclose()
            # Stopped rooms retain their small status snapshot, not an unused
            # platform format catalog or expiring media URLs.
            self.source_info = None
            self.source_extracted_at = 0
            self.directory = None
            if temporary:
                temporary.cleanup()

    async def stop(self):
        self.stop_requests += 1
        pending, self.pending_room = self.pending_room, None
        if pending:
            await pending.stop()
        if not self.cleaning and not self.task.cancelling():
            self.task.cancel()
        # A cancelled command must not interrupt process and reader cleanup.
        await asyncio.shield(asyncio.gather(self.task, return_exceptions=True))
        self.status = 'stopped'


class Rooms:
    def __init__(self):
        self.channels: dict[int, Room] = {}
        self.revisions: dict[int, int] = {}
        self.events: dict[int, asyncio.Event] = {}

    def changed(self, channel_id):
        self.revisions[channel_id] = self.revisions.get(channel_id, 0) + 1
        event = self.events.pop(channel_id, None)
        if event:
            event.set()

    def revision(self, channel_id):
        return self.revisions.get(channel_id, 0)

    def event(self, channel_id):
        if channel_id not in self.events:
            self.events[channel_id] = asyncio.Event()
        return self.events[channel_id]

    def prepare(self, channel_id: int, url: str, resolution: str = '720', start_time: float = 0) -> Room:
        resolution_height(resolution)
        url = normalize_video_url(url)
        current = self.channels.get(channel_id)
        if current and current.active:
            raise ValueError('A video is already loaded. Stop it before loading another.')
        room = Room(channel_id, url, self.changed, resolution, start_time)
        self.channels[channel_id] = room
        self.changed(channel_id)
        return room

    async def change_resolution(self, channel_id: int, resolution: str) -> Room:
        resolution_height(resolution)
        room = self.channels.get(channel_id)
        if room and room.changing_resolution:
            raise ValueError('A resolution change is already in progress. Try again shortly.')
        if not room or not room.active:
            raise ValueError('Nothing is playing. Use /play first.')
        if room.resolution == resolution:
            return room
        room.changing_resolution = True
        replacement = None
        committed = False
        expected_stops = room.stop_requests
        try:
            if room.status in {'playing', 'ending'}:
                cached = None
                # Keep this cache local to one room and short lived. Live
                # playlists and expired recording URLs always refresh.
                if (room.source_info and not room.is_live
                        and time.monotonic() - room.source_extracted_at < 120):
                    try:
                        # Large format catalogs take milliseconds to copy and sort.
                        cached = await asyncio.to_thread(select_video, room.source_info, resolution)
                    except (ValueError, yt_dlp.utils.DownloadError):
                        pass  # Refresh metadata if a cached rendition is unusable.
                for attempt in range(2 if cached else 1):
                    replacement = Room(channel_id, room.url, lambda _: None, resolution,
                                       source_info=cached if attempt == 0 else None,
                                       source_extracted_at=room.source_extracted_at,
                                       resume_from=room)
                    room.pending_room = replacement
                    try:
                        await asyncio.wait_for(replacement.ready.wait(), 90)
                    except asyncio.TimeoutError:
                        raise ValueError('The new resolution took too long. The current video is still playing.') from None
                    if (room.stop_requests != expected_stops or not room.active
                            or self.channels.get(channel_id) is not room):
                        raise ValueError('The video was stopped or changed during the resolution switch.')
                    if replacement.status in {'playing', 'ending'} and replacement.directory:
                        break
                    await replacement.stop()
                else:
                    raise ValueError('Could not load that resolution. The current video is still playing.')
                room.pending_room = None
                self.channels[channel_id] = replacement
                replacement.on_change = self.changed
                committed = True
                self.changed(channel_id)
                await room.stop()
                return replacement
            position = room.playback_position()
            expected_stops += 1
            await room.stop()
            if room.stop_requests != expected_stops or self.channels.get(channel_id) is not room:
                raise ValueError('The video was stopped or changed during the resolution switch.')
            return self.prepare(channel_id, room.url, resolution, position)
        finally:
            room.changing_resolution = False
            if replacement and not committed:
                if room.pending_room is replacement:
                    room.pending_room = None
                await replacement.stop()

    async def close(self):
        await asyncio.gather(*(room.stop() for room in self.channels.values()))
