"""Read public Spotify track metadata and find a conservative YouTube match."""

import json
import math
import re
import unicodedata
from urllib.parse import urlsplit

from yt_dlp.networking import Request


def spotify_url(value):
    parts = urlsplit(value.strip())
    if parts.hostname != 'open.spotify.com':
        return None
    match = re.fullmatch(r'/(?:intl-[a-z]{2}/)?track/([A-Za-z0-9]{22})/?', parts.path)
    if (parts.scheme not in {'https', 'http'} or parts.username or parts.password
            or parts.port not in {None, 80, 443} or not match
            or '\\' in value or any(ord(char) < 32 for char in value)):
        raise ValueError('Use a Spotify link to one track, not an album or playlist.')
    return f'https://open.spotify.com/track/{match[1]}'


def spotify_metadata(url, downloader):
    canonical = spotify_url(url)
    if not canonical:
        raise ValueError('Use a Spotify track link.')
    with downloader.urlopen(Request(canonical.replace('/track/', '/embed/track/'))) as response:
        page = response.read(1048577)
    match = re.search(rb'<script\b[^>]*\bid=["\']__NEXT_DATA__["\'][^>]*>(.*?)</script>', page, re.S)
    try:
        if len(page) > 1048576 or not match:
            raise ValueError
        entity = json.loads(match[1])['props']['pageProps']['state']['data']['entity']
        title = entity['name']
        artists = [artist['name'] for artist in entity['artists']]
        duration = float(entity['duration']) / 1000
        if (entity['type'] != 'track' or entity['uri'] != 'spotify:track:' + canonical.rsplit('/', 1)[1]
                or not isinstance(title, str) or not title.strip() or len(title) > 200
                or not artists or len(artists) > 20
                or any(not isinstance(name, str) or not name.strip() or len(name) > 100 for name in artists)
                or not math.isfinite(duration) or not 0 < duration <= 86400):
            raise ValueError
        return {'title': title, 'artists': artists, 'duration': duration}
    except (ValueError, KeyError, TypeError):
        raise ValueError('Spotify track details are unavailable. Use its public YouTube Music link instead.') from None


def words(value):
    text = ''.join(char for char in unicodedata.normalize('NFKD', value).casefold()
                   if not unicodedata.combining(char))
    return set(re.findall(r'[^\W_]+', text))


def choose_youtube_track(track, entries):
    title = words(track['title']) - {'feat', 'ft', 'featuring'}
    artist = words(track['artists'][0])
    variants = {'cover', 'karaoke', 'remix', 'live', 'instrumental', 'slowed', 'sped'}
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        candidate = words(entry.get('title') or '')
        credits = candidate | words(entry.get('channel') or '') | words(entry.get('uploader') or '')
        duration = entry.get('duration')
        if (title and artist and title <= candidate and artist <= credits
                and not (candidate & variants) - words(track['title'])
                and isinstance(duration, (float, int)) and math.isfinite(duration)
                and abs(duration - track['duration']) <= max(8, track['duration'] * 0.08)
                and not entry.get('is_live') and re.fullmatch(r'[A-Za-z0-9_-]{11}', entry.get('id') or '')):
            return 'https://www.youtube.com/watch?v=' + entry['id']
    raise ValueError('No close public YouTube match found for this Spotify track. Use a YouTube Music link instead.')
