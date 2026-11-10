import asyncio
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from tars_companion.audio import (ASR_PCM, TTS_PCM, MemoryAudioSource, UtteranceCollector,
                                  WavAudioSink, WavAudioSource)
from tars_companion.calibration import calibrate_frames, calibrate_source


def frame(level: int) -> bytes:
    return struct.pack("<h", level) * (ASR_PCM.frame_bytes // 2)


def write_wav(path: Path, *, rate: int = 16_000, channels: int = 1,
              width: int = 2, payload: bytes = b"\0\0" * 1600) -> None:
    with wave.open(str(path), "wb") as output:
        output.setframerate(rate)
        output.setnchannels(channels)
        output.setsampwidth(width)
        output.writeframes(payload)


class CalibrationTests(unittest.IsolatedAsyncioTestCase):
    def test_percentile_and_threshold_clamps(self):
        result = calibrate_frames([frame(100)] * 8 + [frame(500), frame(1000)])
        self.assertEqual(result.p95_rms, 500)
        self.assertEqual(result.threshold, 1500)
        self.assertEqual(calibrate_frames([frame(0)] * 10).threshold, 250)
        loud = calibrate_frames([frame(3000)] * 10)
        self.assertEqual(loud.threshold, 4000)
        self.assertIn("noise_too_high", loud.warnings)

    def test_speech_pollution_rejected_but_one_spike_tolerated(self):
        polluted = calibrate_frames([frame(100)] * 24 + [frame(3000)] * 6)
        self.assertFalse(polluted.usable)
        self.assertIn("speech_or_transient_detected", polluted.warnings)
        spike = calibrate_frames([frame(100)] * 29 + [frame(3000)])
        self.assertTrue(spike.usable)
        self.assertEqual(spike.threshold, 300)

    def test_clipping_is_detected(self):
        samples = [32767] * 320 + [100] * (1600 - 320)
        clipped = struct.pack("<1600h", *samples)
        result = calibrate_frames([clipped] + [frame(100)] * 9)
        self.assertFalse(result.usable)
        self.assertIn("input_clipping", result.warnings)

    async def test_duration_validation_and_source_calibration(self):
        source = MemoryAudioSource([frame(100)] * 15)
        result = await calibrate_source(source, 1.0)
        self.assertEqual(result.threshold, 300)
        with self.assertRaises(ValueError):
            await calibrate_source(MemoryAudioSource([]), 0.5)

    async def test_calibrated_threshold_drives_vad(self):
        calibration = calibrate_frames([frame(100)] * 10)
        frames = [frame(100)] * 3 + [frame(250)] * 2 + [frame(600)] * 2 + [frame(100)] * 7
        pcm = await UtteranceCollector(threshold=calibration.threshold).collect(MemoryAudioSource(frames))
        self.assertEqual(len(pcm), 12 * ASR_PCM.frame_bytes)
        self.assertEqual(pcm[3 * ASR_PCM.frame_bytes:4 * ASR_PCM.frame_bytes], frame(600))

    async def test_single_impulse_does_not_start_utterance(self):
        frames = [frame(100)] * 3 + [frame(2000)] + [frame(100)] * 7
        with self.assertRaises(EOFError):
            await UtteranceCollector(threshold=500).collect(MemoryAudioSource(frames))


class WavAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_valid_wav_source_and_sink(self):
        with tempfile.TemporaryDirectory() as directory:
            source_path = Path(directory) / "input.wav"
            sink_path = Path(directory) / "output.wav"
            write_wav(source_path, payload=frame(123))
            source = WavAudioSource(str(source_path))
            self.assertEqual(await source.read_frame(), frame(123))
            source.close()
            cancel = asyncio.Event()
            await WavAudioSink(str(sink_path)).play(b"\1\0" * 2400, TTS_PCM, cancel)
            with wave.open(str(sink_path), "rb") as output:
                self.assertEqual((output.getframerate(), output.getnchannels(), output.getsampwidth()),
                                 (24_000, 1, 2))
                self.assertEqual(output.readframes(2400), b"\1\0" * 2400)

    async def test_wav_rejects_wrong_format_and_empty_audio(self):
        with tempfile.TemporaryDirectory() as directory:
            wrong = Path(directory) / "wrong.wav"
            empty = Path(directory) / "empty.wav"
            write_wav(wrong, rate=8000)
            write_wav(empty, payload=b"")
            with self.assertRaisesRegex(ValueError, "16000"):
                WavAudioSource(str(wrong))
            with self.assertRaisesRegex(ValueError, "no audio"):
                WavAudioSource(str(empty))

    async def test_wav_cannot_drive_real_robot(self):
        from argparse import Namespace
        from unittest.mock import patch
        from tars_companion.voice_cli import run
        args = Namespace(input_wav="replay.wav", simulate=False, calibrate_seconds=0,
                         port="COM7", wake_word="TARS", vad_threshold=500)
        with patch("tars_companion.voice_cli.SerialTransport") as serial:
            with self.assertRaisesRegex(SystemExit, "restricted"):
                await run(args)
            serial.assert_not_called()


if __name__ == "__main__":
    unittest.main()
