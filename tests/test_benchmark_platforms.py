"""Offline checks for the opt-in network benchmark's measurement math."""

import tempfile
import contextlib
import io
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import unittest
from pathlib import Path

from benchmarks.benchmark_platforms import measure_stage, playlist_state, safe_error, summarize


class PlatformBenchmarkTests(unittest.TestCase):
    def test_rolling_playlist_includes_deleted_segments_in_media_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'index.m3u8').write_text(
                '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:20\n'
                '#EXTINF:1.000,\n000020.ts\n#EXTINF:0.960,\n000021.ts\n')
            end, segments = playlist_state(directory)
            self.assertAlmostEqual(end, 21.96)
            self.assertEqual(segments, [('000020.ts', 1), ('000021.ts', 0.96)])

    def test_segment_steps_do_not_look_like_a_throughput_loss(self):
        samples = [(i / 10, int(i / 10 + 0.3)) for i in range(201)]
        result = summarize(samples)
        self.assertAlmostEqual(result['delivery_rate_x'], 1, delta=0.01)
        self.assertAlmostEqual(result['max_segment_gap_s'], 1, delta=0.1)

    def test_slow_stream_and_stall_are_visible(self):
        result = summarize([(i / 10, int(i / 40)) for i in range(201)])
        self.assertAlmostEqual(result['delivery_rate_x'], 0.25, delta=0.02)
        self.assertEqual(result['max_segment_gap_s'], 4)

    def test_initial_burst_is_excluded_and_short_windows_have_no_rate(self):
        samples = [(i / 10, i / 10 + (10 if i >= 40 else 0)) for i in range(201)]
        self.assertAlmostEqual(summarize(samples)['delivery_rate_x'], 1, delta=0.001)
        self.assertIsNone(summarize(samples[:60])['delivery_rate_x'])

    def test_error_messages_omit_signed_media_urls(self):
        result = safe_error('HTTP 403 from https://cdn.test/video?token=secret failed')
        self.assertNotIn('secret', result)
        self.assertIn('HTTP 403', result)


    def test_error_messages_redact_personal_filesystem_paths(self):
        paths = ['/Users/fixture-user/Project/video.mp4', '/home/fixture-user/video.mp4',
                 r'C:\Users\fixture-user\Project\video.mp4',
                 '/private/var/folders/xx/fixture-device/T/syncnest-stream/file.ts']
        for path in paths:
            with self.subTest(path=path):
                result = safe_error('HTTP 503 while opening ' + path)
                self.assertNotIn('fixture-user', result)
                self.assertNotIn('fixture-device', result)
                self.assertIn('HTTP 503', result)


class ObservationWindowTests(unittest.IsolatedAsyncioTestCase):
    async def observe(self, status='playing', is_live=False, quality='720', height=720):
        with tempfile.TemporaryDirectory() as temporary:
            directory = Path(temporary)
            (directory / 'index.m3u8').write_text(
                '#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:0.050,\n000000.ts\n')
            (directory / '000000.ts').write_bytes(b'fixture')
            room = SimpleNamespace(status=status, directory=directory, is_live=is_live,
                                   start_time=0, process=None)
            stage = {'quality': quality}
            with patch('benchmarks.benchmark_platforms.inspect_segment', AsyncMock(return_value={'height': height})), \
                 contextlib.redirect_stdout(io.StringIO()):
                await measure_stage(room, 0.25, stage, time.perf_counter())
            return stage

    async def test_segment_duration_cannot_shorten_the_requested_observation(self):
        stage = await self.observe()
        self.assertGreaterEqual(stage['observed_s'], 0.25)
        self.assertTrue(stage['measurement_complete'])
        self.assertNotIn('source_ended', stage)

    async def test_highest_quality_observation_accepts_uncapped_4k_output(self):
        stage = await self.observe(quality='best', height=2160)
        self.assertEqual(stage['output']['height'], 2160)
        self.assertTrue(stage['measurement_complete'])

    async def test_live_source_ending_is_recorded_as_an_incomplete_measurement(self):
        stage = await self.observe(status='ending', is_live=True)
        self.assertTrue(stage['source_ended'])
        self.assertFalse(stage['measurement_complete'])
        self.assertEqual(stage['status'], 'partial')


if __name__ == '__main__':
    unittest.main()
