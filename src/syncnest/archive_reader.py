"""Bounded parallel range reads for large, seekable Internet Archive files."""

import asyncio
import logging
import re
import secrets
import ssl
from functools import lru_cache
from pathlib import Path

import aiohttp
from aiohttp import web

from . import egress

log = logging.getLogger(__name__)
CHUNK_SIZE = 4 * 1024 * 1024
PARALLEL_READS = 4
MIN_FILE_SIZE = 32 * 1024 * 1024
RETRY_STATUSES = {429, 500, 502, 503, 504}


@lru_cache(maxsize=1)
def ssl_context():
    # Loading trust roots blocks the event loop; build the context once.
    context = ssl.create_default_context()
    # Some macOS Python installers omit the OS certificate bundle.
    # Add it to the default trust roots; certificate checks stay on.
    if Path('/etc/ssl/cert.pem').is_file():
        context.load_verify_locations('/etc/ssl/cert.pem')
    return context


class ArchiveReader:
    """Expose one fixed file to FFmpeg on loopback; never save a full download.

    Each response fetches at most four chunks ahead. Seeking cancels the old
    response's reads, and closing the room cancels every outstanding request.
    Unsupported range servers fall back to the original FFmpeg input.
    """

    def __init__(self, url, headers=None):
        self.url = self.original_url = url
        self.closing = False
        self.headers = dict(headers or {}) | {'Accept-Encoding': 'identity'}
        self.session = self.runner = None
        self.port = None
        self.handlers = set()
        self.lock = asyncio.Lock()
        self.size = 0
        self.validator = None

    async def __aenter__(self):
        self.closing = False
        try:
            self.session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(limit=PARALLEL_READS, ssl=ssl_context()),
                # Use the public-only egress proxy when the bot configured one.
                timeout=aiohttp.ClientTimeout(total=15), auto_decompress=False, trust_env=True)
            async with self.session.get(self.url, headers=self.headers | {'Range': 'bytes=0-0'},
                                        timeout=aiohttp.ClientTimeout(total=5)) as response:
                match = re.fullmatch(r'bytes 0-0/(\d+)', response.headers.get('Content-Range', ''))
                if response.status != 206 or not match or response.headers.get('Content-Encoding', 'identity') != 'identity':
                    raise ValueError('Server does not support exact byte ranges.')
                self.size = int(match[1])
                if self.size < MIN_FILE_SIZE:
                    raise ValueError('Small files use the direct input.')
                self.url = str(response.url)
                etag = response.headers.get('ETag', '')
                self.validator = (etag if etag and not etag.startswith('W/') else
                                  response.headers.get('Last-Modified'))
            app = web.Application()
            path = '/' + secrets.token_urlsafe(24)
            app.router.add_get(path, self.serve)
            self.runner = web.AppRunner(app, access_log=None, handler_cancellation=True, shutdown_timeout=1)
            await self.runner.setup()
            site = web.TCPSite(self.runner, '127.0.0.1', 0)
            await site.start()
            self.port = self.runner.addresses[0][1]
            # FFmpeg reaches this reader through the egress proxy, which otherwise refuses loopback.
            egress.allow_loopback(self.port)
            return f'http://127.0.0.1:{self.port}{path}'
        except asyncio.CancelledError:
            await self.close()
            raise
        except (aiohttp.ClientError, OSError, TimeoutError, ValueError):
            log.debug('Archive parallel reads unavailable; using the direct input.')
            await self.close()
            return self.original_url

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self):
        self.closing = True
        tasks = list(self.handlers)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if self.port:
            egress.revoke_loopback(self.port)
            self.port = None
        if self.runner:
            await self.runner.cleanup()
            self.runner = None
        if self.session:
            await self.session.close()
            self.session = None

    async def read_chunk(self, start, end):
        headers = self.headers | {'Range': f'bytes={start}-{end}'}
        if self.validator:
            headers['If-Range'] = self.validator
        for attempt in range(3):
            try:
                async with self.session.get(self.url, headers=headers) as response:
                    rejected = {401: web.HTTPUnauthorized, 403: web.HTTPForbidden,
                                404: web.HTTPNotFound, 410: web.HTTPGone}.get(response.status)
                    if rejected:
                        raise rejected(reason='Archive rejected this file request.')
                    if response.status == 200:
                        raise web.HTTPConflict(reason='Archive file changed during playback.')
                    if response.status in RETRY_STATUSES:
                        raise aiohttp.ClientConnectionError('Temporary Archive server error.')
                    if (response.status != 206 or response.headers.get('Content-Range') !=
                            f'bytes {start}-{end}/{self.size}' or
                            response.headers.get('Content-Encoding', 'identity') != 'identity'):
                        raise ValueError('Archive file changed or returned an invalid byte range.')
                    data = await response.content.readexactly(end - start + 1)
                    if await response.content.read(1):
                        raise ValueError('Archive returned too many bytes.')
                    return data
            except (aiohttp.ClientError, TimeoutError, asyncio.IncompleteReadError):
                if attempt == 2:
                    raise OSError('Archive range read failed after three attempts.') from None
                await asyncio.sleep(0.25 * 2 ** attempt)

    async def serve(self, request):
        if self.closing:
            raise web.HTTPServiceUnavailable()
        start, end = 0, self.size - 1
        value = request.headers.get('Range')
        if value:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', value)
            if not match or not any(match.groups()):
                raise web.HTTPRequestRangeNotSatisfiable(headers={'Content-Range': f'bytes */{self.size}'})
            first, last = match.groups()
            start = int(first) if first else max(0, self.size - int(last))
            end = min(int(last), end) if first and last else end
            if start > end:
                raise web.HTTPRequestRangeNotSatisfiable(headers={'Content-Range': f'bytes */{self.size}'})
        headers = {'Accept-Ranges': 'bytes', 'Content-Length': str(end - start + 1),
                   'Content-Type': 'application/octet-stream'}
        if value:
            headers['Content-Range'] = f'bytes {start}-{end}/{self.size}'
        response = web.StreamResponse(status=206 if value else 200, headers=headers)
        if request.method == 'HEAD':
            return response
        task = asyncio.current_task()
        previous = list(self.handlers)
        self.handlers.add(task)
        for old in previous:
            old.cancel()
        pending = {}
        try:
            # A seek may open its new connection before closing the old one.
            # Cancel that obsolete read so it cannot block the seek or prefetch.
            await asyncio.gather(*previous, return_exceptions=True)
            async with self.lock:
                next_start = start

                def prefetch():
                    nonlocal next_start
                    while len(pending) < PARALLEL_READS and next_start <= end:
                        stop = min(next_start + CHUNK_SIZE - 1, end)
                        pending[next_start] = asyncio.create_task(self.read_chunk(next_start, stop))
                        next_start = stop + 1

                prefetch()
                while start <= end:
                    data = await pending.pop(start)
                    # Refill while FFmpeg consumes the current block. Network
                    # reads keep progressing even when its socket applies backpressure.
                    prefetch()
                    if not response.prepared:
                        await response.prepare(request)
                    await response.write(data)
                    start += len(data)
                await response.write_eof()
        except asyncio.CancelledError:
            if request.transport:
                request.transport.close()
            raise
        except (OSError, ValueError, aiohttp.ClientError, web.HTTPException) as error:
            if not response.prepared:
                if isinstance(error, web.HTTPException):
                    raise
                raise web.HTTPBadGateway(reason='Archive could not deliver this range.') from None
            # Do not finish a truncated response successfully. FFmpeg can
            # reconnect at the last delivered byte using its bounded retry.
            if request.transport:
                request.transport.close()
        finally:
            for read in pending.values():
                read.cancel()
            try:
                await asyncio.gather(*pending.values(), return_exceptions=True)
            finally:
                self.handlers.discard(task)
        return response
