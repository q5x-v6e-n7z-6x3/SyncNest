"""Opt-in real-network playback checks; never connects the bot to Discord.

Run with PYTHONPATH=src python -m benchmarks.benchmark_platforms
Tests run sequentially so encoders do not compete. Buffers are temporary, and
reports omit signed media URLs, headers, authorization data, and credentials.
"""

import argparse
import asyncio
import json
import logging
import os
import re
import signal
import statistics
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from syncnest import stream
from yt_dlp.version import __version__ as YTDLP_VERSION

SAMPLES = {
    'Vimeo': ['https://player.vimeo.com/video/859028877', 'https://vimeo.com/76979871',
              'https://player.vimeo.com/video/76979871',
              'https://player.vimeo.com/video/54469442'],
    'Dailymotion': ['https://www.dailymotion.com/video/x9yfz8u', 'https://www.dailymotion.com/video/x7svh5p'],
    'Twitch clip': ['https://www.twitch.tv/xqc/clip/CulturedAmazingKuduDatSheffy-TiZ_-ixAGYR3y2Uy',
                    'https://clips.twitch.tv/FaintLightGullWholeWheat'],
    'Twitch live': ['https://www.twitch.tv/rifftrax', 'https://www.twitch.tv/chess'],
    'Internet Archive': ['https://archive.org/download/BigBuckBunnyFULLHD60FPS/Big%20Buck%20Bunny%20-%20FULL%20HD%2060FPS.mp4'],
    'Odysee': ['https://odysee.com/@gardeningincanada:b/plants-i-will-never-grow-again.-the:e',
               'https://odysee.com/@LBRYFoundation:0/Episode-1:e'],
    'BitChute': ['https://www.bitchute.com/video/Yti_j9A-UZ4', 'https://www.bitchute.com/video/UGlrF9o9b-Q'],
    'Rumble': ['https://rumble.com/vdmum1-moose-the-dog-helps-girls-dig-a-snow-fort.html',
               'https://rumble.com/embed/v5pv5f'],
    'Kick clip': ['https://kick.com/spreen/clips/clip_01J8RGZRKHXHXXKJEHGRM932A5',
                  'https://kick.com/mxddy/clips/clip_01GYXVB5Y8PWAPWCWMSBCFB05X'],
    'Kick live': ['https://kick.com/a-log-burner', 'https://kick.com/buddha'],
}


def safe_error(value):
    text = re.sub(r'\x1b\[[0-9;]*m', '', str(value))
    text = re.sub(r'https?://\S+', '[URL]', text)
    text = re.sub(r'/(?:Users|home)/[^/\s]+|[A-Za-z]:\\Users\\[^\\\r\n]+', '[user home]', text)
    text = re.sub(r'/(?:private/)?var/folders/[^\s]+', '[temporary path]', text)
    return text[-900:]


def playlist_state(directory):
    text = (directory / 'index.m3u8').read_text()
    sequence = re.search(r'^#EXT-X-MEDIA-SEQUENCE:(\d+)', text, re.MULTILINE)
    durations = re.findall(r'^#EXTINF:([0-9.]+)', text, re.MULTILINE)
    segments = re.findall(r'^[0-9]+\.ts$', text, re.MULTILINE)
    # Production uses one-second GOPs/segments; sequence counts elapsed seconds.
    end = int(sequence[1]) + sum(map(float, durations)) if sequence else sum(map(float, durations))
    return end, list(zip(segments, map(float, durations)))


def summarize(samples):
    warm = [(wall, media) for wall, media in samples if wall >= 5]
    # Fit all samples: endpoint division can misread one-second segment steps
    # as a throughput loss depending on which side of a step it samples.
    rate = (statistics.linear_regression([wall for wall, _ in warm],
                                         [media for _, media in warm]).slope
            if len(warm) > 1 and warm[-1][0] - warm[0][0] >= 4 else None)
    changes = [samples[0][0]] if samples else []
    for previous, current in zip(samples, samples[1:]):
        if current[1] > previous[1]:
            changes.append(current[0])
    if samples:
        changes.append(samples[-1][0])
    gaps = [after - before for before, after in zip(changes, changes[1:])]
    return {'delivery_rate_x': round(rate, 3) if rate is not None else None,
            'max_segment_gap_s': round(max(gaps, default=0), 2)}


async def inspect_segment(path):
    probe = await asyncio.create_subprocess_exec(
        'ffprobe', '-v', 'error', '-show_entries', 'stream=codec_name,codec_type,width,height,avg_frame_rate',
        '-of', 'json', str(path), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        raw, errors = await asyncio.wait_for(probe.communicate(), 10)
    finally:
        if probe.returncode is None:
            probe.kill()
            await probe.wait()
    if probe.returncode:
        raise RuntimeError(safe_error(errors.decode(errors='replace')))
    tracks = json.loads(raw)['streams']
    video = next(track for track in tracks if track['codec_type'] == 'video')
    decode = await asyncio.create_subprocess_exec(
        'ffmpeg', '-v', 'error', '-i', str(path), '-f', 'null', '-',
        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, errors = await asyncio.wait_for(decode.communicate(), 10)
    finally:
        if decode.returncode is None:
            decode.kill()
            await decode.wait()
    if decode.returncode:
        raise RuntimeError(safe_error(errors.decode(errors='replace')))
    return {'width': video['width'], 'height': video['height'],
            'codecs': sorted({track['codec_name'] for track in tracks}),
            'frame_rate': video.get('avg_frame_rate'), 'segment_decoded': True}


async def cpu_sample(pid):
    process = await asyncio.create_subprocess_exec(
        'ps', '-p', str(pid), '-o', '%cpu=', '-o', 'rss=', stdout=asyncio.subprocess.PIPE,
    )
    raw, _ = await process.communicate()
    parts = raw.split()
    return (float(parts[0]), int(parts[1]) / 1024) if len(parts) == 2 else None


async def measure_stage(room, duration, stage, started):
    deadline = time.perf_counter() + 95
    while room.status not in {'playing', 'ending'}:
        if room.status in {'error', 'finished', 'stopped'} or room.task.done():
            raise RuntimeError(room.error or f'Stream stopped with status {room.status}.')
        if time.perf_counter() >= deadline:
            raise TimeoutError('No first segment within 95 seconds.')
        await asyncio.sleep(0.02)
    stage['ready_s'] = round(time.perf_counter() - started, 3)
    stage['is_live'] = room.is_live
    stage['resume_position_s'] = round(room.start_time, 3)
    print(json.dumps({'quality': stage['quality'], 'event': 'ready', 'seconds': stage['ready_s']}), flush=True)
    beginning = time.perf_counter()
    samples, cpu, memory, seen = [], [], [], set()
    published, segment_durations = set(), []
    media_end = 0
    byte_count = max_files = max_buffer = 0
    next_cpu = 0
    inspected = False
    while True:
        elapsed = time.perf_counter() - beginning
        if room.status == 'error':
            raise RuntimeError(room.error)
        if elapsed >= duration or not room.directory:
            if room.status in {'ending', 'finished'}:
                stage['source_ended'] = True
            break
        try:
            end, segments = playlist_state(room.directory)
            if not published:
                media_end = end - sum(duration for _, duration in segments)
            # Accumulate published durations before they leave the rolling list.
            # This avoids assuming every segment lasts exactly one second.
            for name, segment_seconds in segments:
                if name not in published:
                    media_end += segment_seconds
                    segment_durations.append(segment_seconds)
                    published.add(name)
            samples.append((elapsed, media_end))
            for name, _ in segments:
                if name not in seen:
                    byte_count += (room.directory / name).stat().st_size
                    seen.add(name)
            files = list(room.directory.glob('*.ts'))
            max_files = max(max_files, len(files))
            max_buffer = max(max_buffer, sum(path.stat().st_size for path in files))
            if not inspected and segments:
                stage['output'] = await inspect_segment(room.directory / segments[-1][0])
                cap = stream.resolution_height(stage['quality'])
                if cap and stage['output']['height'] > cap:
                    raise RuntimeError('Output exceeded the requested resolution cap.')
                inspected = True
        except FileNotFoundError:
            pass  # A rolling segment can be removed between listing and reading.
        if elapsed >= next_cpu and room.process and room.process.returncode is None:
            usage = await cpu_sample(room.process.pid)
            if usage:
                cpu.append(usage[0])
                memory.append(usage[1])
            next_cpu = elapsed + 2
        if room.status == 'ending':
            stage['source_ended'] = True
            break
        await asyncio.sleep(0.1)
    stage.update(summarize(samples))
    stage['segment_arrivals'] = [[round(wall, 2), round(media, 3)]
                                 for i, (wall, media) in enumerate(samples)
                                 if not i or media != samples[i - 1][1]]
    stage['delivery_rate_method'] = 'linear regression of accumulated EXTINF durations after 5-second warmup'
    stage['segment_duration_range_s'] = [min(segment_durations), max(segment_durations)] if segment_durations else None
    stage['observed_s'] = round(time.perf_counter() - beginning, 2)
    stage['median_cpu_percent'] = round(statistics.median(cpu), 1) if cpu else None
    stage['peak_encoder_memory_mb'] = round(max(memory), 1) if memory else None
    stage['max_buffer_mb'] = round(max_buffer / 1024 / 1024, 2)
    stage['max_segment_files'] = max_files
    stage['output_mbps'] = round(byte_count * 8 / samples[-1][1] / 1e6, 2) if samples and samples[-1][1] else None
    stage['measurement_complete'] = stage['observed_s'] >= duration * 0.95
    stage['status'] = 'partial' if room.is_live and stage.get('source_ended') else 'ok'
    print(json.dumps({'quality': stage['quality'], 'event': 'measured',
                      'rate_x': stage['delivery_rate_x'], 'max_gap_s': stage['max_segment_gap_s'],
                      'actual_height': stage.get('output', {}).get('height')}), flush=True)


async def worker(label, url, seconds, return_seconds, qualities=('720', '1080', '720')):
    rooms = stream.Rooms()
    result = {'sample': label, 'url': url, 'stages': []}
    original = stream.extract_video
    current = None
    errors = []

    class Errors(logging.Handler):
        def emit(self, record):
            errors.append(safe_error(record.getMessage()))
            del errors[:-6]

    handler = Errors(level=logging.ERROR)
    stream.log.addHandler(handler)

    def extraction(link, quality):
        started = time.perf_counter()
        try:
            info = original(link, quality)
            current['source_heights'] = sorted({source['height'] for source in info.get('formats', [info])
                                                if source.get('height')})
            current['selected_heights'] = [source.get('height') for source in info.get('requested_formats', [info])]
            current['source_duration_s'] = info.get('duration')
            return info
        except Exception as error:
            current['extraction_error'] = safe_error(error)
            raise
        finally:
            current['extraction_s'] = round(time.perf_counter() - started, 3)

    try:
        with patch('syncnest.stream.extract_video', side_effect=extraction):
            room = None
            for index, quality in enumerate(qualities):
                duration = return_seconds if index == len(qualities) - 1 and index else seconds
                current = {'quality': quality}
                result['stages'].append(current)
                started = time.perf_counter()
                try:
                    if room:
                        current['previous_position_s'] = round(room.playback_position(), 3)
                        room = await rooms.change_resolution(1, quality)
                        current['change_command_s'] = round(time.perf_counter() - started, 3)
                        current['source_reused'] = 'extraction_s' not in current
                        current.setdefault('extraction_s', 0)
                    else:
                        room = rooms.prepare(1, url, quality)
                    info = room.source_info
                    if info:
                        current['source_heights'] = sorted({source['height'] for source in info.get('formats', [info])
                                                           if source.get('height')})
                        current['selected_heights'] = [source.get('height') for source in info.get('requested_formats', [info])]
                        current['source_duration_s'] = info.get('duration')
                    await measure_stage(room, duration, current, started)
                except Exception as error:
                    current['status'] = 'error'
                    current['failed_after_s'] = round(time.perf_counter() - started, 3)
                    current['error'] = safe_error(error)
                    current['diagnostics'] = errors[-3:]
                    kept = rooms.channels.get(1)
                    current['previous_stream_preserved'] = bool(kept and kept is room and kept.status in {'playing', 'ending'})
                    break
            result['status'] = ('error' if any(stage['status'] == 'error' for stage in result['stages'])
                                else 'partial' if any(stage['status'] == 'partial' for stage in result['stages'])
                                else 'ok')
    finally:
        await rooms.close()
        stream.log.removeHandler(handler)
    return result


async def run_worker(label, url, args):
    with tempfile.TemporaryDirectory(prefix='syncnest-platform-check-') as temporary:
        output = Path(temporary) / 'result.json'
        process = await asyncio.create_subprocess_exec(
            sys.executable, '-m', 'benchmarks.benchmark_platforms', '--worker', label, url,
            '--seconds', str(args.seconds), '--return-seconds', str(args.return_seconds),
            '--qualities', *args.qualities,
            '--output', str(output), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )
        async def progress():
            while line := await process.stdout.readline():
                print(label + ': ' + safe_error(line.decode().strip()), flush=True)
        forwarding = asyncio.create_task(progress())
        draining = asyncio.create_task(process.stderr.read())
        try:
            try:
                await asyncio.wait_for(process.wait(), 100 * len(args.qualities) + len(args.qualities) * args.seconds + args.return_seconds)
            except TimeoutError:
                # One stalled platform must not abort the remaining samples.
                return {'sample': label, 'url': url, 'status': 'error', 'stages': [],
                        'error': 'The platform check timed out.'}
            await forwarding
            if output.exists():
                return json.loads(output.read_text())
            return {'sample': label, 'url': url, 'status': 'error', 'stages': [],
                    'error': safe_error((await draining).decode(errors='replace'))}
        finally:
            if process.returncode is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except TimeoutError:
                    os.killpg(process.pid, signal.SIGKILL)
                    await process.wait()
            await asyncio.gather(forwarding, draining, return_exceptions=True)


def save(path, report):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(report, indent=2) + '\n')
    temporary.replace(path)


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=float, default=20)
    parser.add_argument('--return-seconds', type=float, default=10)
    parser.add_argument('--sample', action='append', metavar='LABEL=URL')
    parser.add_argument('--qualities', nargs='+', choices=list(stream.RESOLUTIONS), default=['720', '1080', '720'])
    parser.add_argument('--worker', nargs=2, metavar=('LABEL', 'URL'))
    parser.add_argument('--output', type=Path, default=Path('benchmarks/results/platform-speeds.json'))
    args = parser.parse_args()
    if min(args.seconds, args.return_seconds) <= 0:
        parser.error('Observation windows must be positive.')
    if args.worker:
        save(args.output, await worker(*args.worker, args.seconds, args.return_seconds, args.qualities))
        return
    choices = {label: [url] for label, url in (value.split('=', 1) for value in args.sample)} if args.sample else SAMPLES
    choices = {label: [stream.normalize_video_url(url) for url in urls] for label, urls in choices.items()}
    report = {'tested_date_utc': datetime.now(timezone.utc).date().isoformat(), 'yt_dlp_version': YTDLP_VERSION,
              'observation_seconds': args.seconds, 'return_observation_seconds': args.return_seconds,
              'scope': 'Actual platform extraction, CDN reads, FFmpeg, and Rooms.change_resolution. '
                       'Excludes Discord authorization, HTTPS proxy/upload, and browser buffering. '
                       'Delivery rate excludes the first five seconds; gaps describe server output, '
                       'not measured browser rebuffering. One link does not represent a whole platform.',
              'results': []}
    for label, urls in choices.items():
        for url in urls:
            print(json.dumps({'sample': label, 'event': 'testing', 'url': url}), flush=True)
            result = await run_worker(label, url, args)
            report['results'].append(result)
            save(args.output, report)
            print(json.dumps({'sample': label, 'event': 'complete', 'status': result['status']}), flush=True)
            if result['status'] == 'ok':
                break
    print('Saved report: ' + args.output.name, flush=True)


if __name__ == '__main__':
    asyncio.run(main())
