"""Offline component measurements: PYTHONPATH=src python -m benchmarks.benchmark_speed (FFmpeg required).

Includes a paced local HTTP stream; excludes real platforms and Discord startup.
"""

import asyncio
import json
import statistics
import tempfile
import time
from pathlib import Path

from aiohttp import web
from aiohttp.test_utils import TestServer
from yt_dlp import YoutubeDL

from syncnest.stream import ALLOWED_EXTRACTORS, ffmpeg_args, select_video


def extractor_initialization():
    results = {}
    for label, extra in [('all_sites', {}), ('supported_sites', {'allowed_extractors': ALLOWED_EXTRACTORS})]:
        samples = []
        for _ in range(11):
            start = time.perf_counter()
            with YoutubeDL({'quiet': True, **extra}) as downloader:
                count = len(downloader._ies)
            samples.append((time.perf_counter() - start) * 1000)
        results[label] = {'median_ms': round(statistics.median(samples[1:]), 2), 'extractors': count}
    return results


def cached_quality_selection():
    formats = [
        {'format_id': str(height), 'url': f'https://example.test/{height}.mp4',
         'height': height, 'vcodec': 'avc1.640028', 'acodec': 'mp4a.40.2', 'ext': 'mp4'}
        for height in (144, 280, 360, 480, 720, 1080, 1440, 2160, 4320)
    ]
    info = {'id': 'fixture', 'title': 'Fixture', 'formats': formats}
    samples = []
    for _ in range(11):
        start = time.perf_counter()
        selected = select_video(info, '1080')
        samples.append((time.perf_counter() - start) * 1000)
        assert selected['height'] == 1080
    return {'median_ms': round(statistics.median(samples[1:]), 3), 'formats': len(formats)}


async def make_source(root):
    source = root / 'source.ts'
    generation = await asyncio.create_subprocess_exec(
        'ffmpeg', '-v', 'error', '-f', 'lavfi', '-i', 'testsrc2=size=1280x720:rate=30',
        '-f', 'lavfi', '-i', 'sine=frequency=440:sample_rate=48000', '-t', '12',
        '-c:v', 'libx264', '-preset', 'ultrafast', '-g', '30', '-pix_fmt', 'yuv420p',
        '-c:a', 'aac', str(source),
    )
    if await generation.wait():
        raise RuntimeError('FFmpeg could not generate the benchmark fixture.')
    return source


async def measure_startup(info, directory):
    directory.mkdir()
    start = time.perf_counter()
    process = await asyncio.create_subprocess_exec(
        *ffmpeg_args(info, directory),
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    output = asyncio.create_task(process.communicate())
    try:
        first = None
        async with asyncio.timeout(15):
            while not (directory / '000002.ts').exists():
                if first is None and (directory / '000000.ts').exists():
                    first = (time.perf_counter() - start) * 1000
                if output.done():
                    raise RuntimeError((await output)[1].decode(errors='replace'))
                await asyncio.sleep(0.005)
        buffered = (time.perf_counter() - start) * 1000
        return {'first_segment_ms': round(first or buffered, 2),
                'three_segments_ms': round(buffered, 2)}
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
        await output


async def stream_startup():
    with tempfile.TemporaryDirectory(prefix='syncnest-speed-') as temporary:
        root = Path(temporary)
        source = await make_source(root)
        info = {'url': str(source), 'vcodec': 'h264', 'acodec': 'mp4a.40.2',
                'audio_channels': 1, 'asr': 48000, 'abr': 128}
        samples = [await measure_startup(info, root / str(trial)) for trial in range(3)]
    return {key: round(statistics.median(sample[key] for sample in samples), 2) for key in samples[0]}


async def paced_stream_startup():
    with tempfile.TemporaryDirectory(prefix='syncnest-paced-speed-') as temporary:
        root = Path(temporary)
        data = (await make_source(root)).read_bytes()

        async def serve(request):
            response = web.StreamResponse(headers={'Content-Type': 'video/mp2t'})
            await response.prepare(request)
            try:
                chunk = 32 * 188
                for offset in range(0, len(data), chunk):
                    await response.write(data[offset:offset + chunk])
                    await asyncio.sleep(chunk * 12 / len(data))
                await response.write_eof()
            except (ConnectionResetError, asyncio.CancelledError):
                pass
            return response

        app = web.Application()
        app.router.add_get('/live.ts', serve)
        results = {}
        async with TestServer(app) as server:
            audio = {'url': str(server.make_url('/live.ts')), 'acodec': 'mp4a.40.2',
                     'audio_channels': 1, 'asr': 48000, 'abr': 128}
            for label, info in [('default_probe', audio), ('known_codecs', audio | {'vcodec': 'h264'})]:
                directory = root / label
                result = await measure_startup(info, directory)
                probe = await asyncio.create_subprocess_exec(
                    'ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name', '-of', 'json',
                    str(directory / '000000.ts'), stdout=asyncio.subprocess.PIPE,
                )
                raw, _ = await probe.communicate()
                if probe.returncode:
                    raise RuntimeError('FFprobe could not inspect the first segment.')
                result['codecs'] = sorted(stream['codec_name'] for stream in json.loads(raw)['streams'])
                results[label] = result
    return results


async def main():
    print(json.dumps({'extractor_initialization': extractor_initialization(),
                      'cached_quality_selection': cached_quality_selection(),
                      'local_720p_hls': await stream_startup(),
                      'paced_http_720p_hls': await paced_stream_startup()}, indent=2))


if __name__ == '__main__':
    asyncio.run(main())
