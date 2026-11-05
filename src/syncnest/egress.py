"""Keep media fetches on the public internet.

yt-dlp, FFmpeg, and the Archive reader send every request through this loopback
proxy. It resolves each destination itself and connects only to public
addresses, so platform redirects, playlist entries, and DNS rebinding cannot
reach loopback services, private networks, or cloud metadata endpoints.
Discord's gateway and voice traffic do not use it.
"""

import asyncio
import contextlib
import ipaddress
import logging
import os
import socket
from urllib.parse import urlsplit

log = logging.getLogger(__name__)
MAX_CONNECTIONS = 256
CONNECT_TIMEOUT_SECONDS = 15
HALF_CLOSE_SECONDS = 60
NAT64 = ipaddress.ip_network('64:ff9b::/96')
IPV4_COMPATIBLE = ipaddress.ip_network('::/96')  # Deprecated ::a.b.c.d form.
PROXY_VARIABLES = ('http_proxy', 'https_proxy', 'HTTP_PROXY', 'HTTPS_PROXY')
BYPASS_VARIABLES = ('no_proxy', 'NO_PROXY', 'all_proxy', 'ALL_PROXY')
# Loopback ports served by this process (Archive readers) that FFmpeg must reach.
_loopback_ports = set()


def allow_loopback(port):
    _loopback_ports.add(port)


def revoke_loopback(port):
    _loopback_ports.discard(port)


def public_address(value):
    address = ipaddress.ip_address(value)
    if address.version == 6:
        if address.ipv4_mapped:
            address = address.ipv4_mapped
        elif address in IPV4_COMPATIBLE:
            return False
        elif address in NAT64:
            # NAT64 gateways translate the embedded IPv4 address.
            address = ipaddress.ip_address(int(address) & 0xFFFFFFFF)
    return address.is_global and not address.is_multicast


def permitted(address, port):
    return public_address(address) or (ipaddress.ip_address(address).is_loopback and port in _loopback_ports)


def split_authority(value):
    if value.startswith('['):
        host, _, rest = value[1:].partition(']')
        port = rest.removeprefix(':')
    else:
        host, _, port = value.rpartition(':')
    if not host or not port.isdigit() or not 0 < int(port) < 65536:
        raise ValueError('Invalid proxy target.')
    return host, int(port)


class Blocked(Exception):
    pass


async def open_public(host, port):
    # Slow resolvers must not hold a handler (or an executor thread wait) indefinitely.
    infos = await asyncio.wait_for(asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM),
                                   CONNECT_TIMEOUT_SECONDS)
    # Connect to the vetted address itself; a second lookup could be rebound.
    addresses = [info[4][0] for info in infos if permitted(info[4][0], port)]
    if not addresses:
        raise Blocked(host)
    error = None
    for address in dict.fromkeys(addresses):
        try:
            return await asyncio.wait_for(asyncio.open_connection(address, port), CONNECT_TIMEOUT_SECONDS)
        except (OSError, TimeoutError) as failure:
            error = failure
    raise error


async def pipe(reader, writer):
    while data := await reader.read(65536):
        writer.write(data)
        await writer.drain()
    if writer.can_write_eof():
        writer.write_eof()


class EgressProxy:
    def __init__(self):
        self.server = None
        self.url = None
        self.handlers = set()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self.handle, '127.0.0.1', 0)
        self.url = f'http://127.0.0.1:{self.server.sockets[0].getsockname()[1]}'
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        for task in list(self.handlers):
            task.cancel()
        await asyncio.gather(*self.handlers, return_exceptions=True)
        await self.server.wait_closed()

    def route_environment(self):
        """Send yt-dlp, FFmpeg, and env-aware HTTP clients through this proxy."""
        for name in BYPASS_VARIABLES:
            os.environ.pop(name, None)
        for name in PROXY_VARIABLES:
            os.environ[name] = self.url

    async def handle(self, reader, writer):
        task = asyncio.current_task()
        self.handlers.add(task)
        upstream = None
        try:
            if len(self.handlers) > MAX_CONNECTIONS:
                return await self.reply(writer, 503, 'Busy')
            try:
                head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), CONNECT_TIMEOUT_SECONDS)
                lines = head.decode('latin-1').split('\r\n')
                method, target, version = lines[0].split(' ')
                if method == 'CONNECT':
                    host, port = split_authority(target)
                    request = None
                else:
                    parts = urlsplit(target)
                    if parts.scheme != 'http' or not parts.hostname:
                        raise ValueError('Only absolute http:// requests are proxied.')
                    host, port = parts.hostname, parts.port or 80
                    # One request per connection: a reused client connection
                    # could otherwise address a second host through this one.
                    kept = [line for line in lines[1:] if line and line.split(':', 1)[0].strip().lower() not in {
                        'connection', 'proxy-connection', 'keep-alive', 'proxy-authorization'}]
                    if not any(line.lower().startswith('host:') for line in kept):
                        kept.insert(0, f'Host: {parts.netloc}')
                    path = (parts.path or '/') + (f'?{parts.query}' if parts.query else '')
                    request = f'{method} {path} {version}\r\n' + ''.join(
                        line + '\r\n' for line in kept + ['Connection: close']) + '\r\n'
            except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
                return await self.reply(writer, 400, 'Bad Request')
            try:
                upstream = await open_public(host, port)
            except Blocked:
                log.warning('Blocked a media request to a non-public address (%s).', host)
                return await self.reply(writer, 403, 'Forbidden')
            except (OSError, TimeoutError):
                return await self.reply(writer, 502, 'Bad Gateway')
            upstream_reader, upstream_writer = upstream
            if request is None:
                writer.write(b'HTTP/1.1 200 Connection Established\r\n\r\n')
            else:
                upstream_writer.write(request.encode('latin-1'))
            await self.relay(reader, writer, upstream_reader, upstream_writer)
        except (ConnectionError, OSError):
            pass
        finally:
            for stream in (writer, upstream and upstream[1]):
                if stream:
                    stream.close()
            self.handlers.discard(task)

    @staticmethod
    async def reply(writer, status, reason):
        writer.write(f'HTTP/1.1 {status} {reason}\r\nContent-Length: 0\r\nConnection: close\r\n\r\n'.encode())
        with contextlib.suppress(ConnectionError, OSError):
            await writer.drain()

    @staticmethod
    async def relay(reader, writer, upstream_reader, upstream_writer):
        directions = {asyncio.create_task(pipe(reader, upstream_writer)),
                      asyncio.create_task(pipe(upstream_reader, writer))}
        try:
            done, pending = await asyncio.wait(directions, return_when=asyncio.FIRST_COMPLETED)
            if pending and not any(task.exception() for task in done):
                # Allow a half-closed exchange to finish, but never hold a tunnel open.
                await asyncio.wait(pending, timeout=HALF_CLOSE_SECONDS)
        finally:
            for task in directions:
                task.cancel()
            await asyncio.gather(*directions, return_exceptions=True)
