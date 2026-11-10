"""Local energy gate and bounded pre-roll; silence does not open a cloud session."""
import asyncio
import contextlib
import time
from collections import deque
from .audio import pcm16_rms


class LocalListeningMicrophone:
    prepared = True

    def __init__(self, source, link, *, threshold=500, wakeword=None, awake_until=0, continuous_capture=False):
        if not 100 <= threshold <= 5000: raise ValueError('invalid_vad_threshold')
        self.source, self.link, self.threshold = source, link, threshold
        self.queue = asyncio.Queue(maxsize=240)  # 24 seconds, PCM16/16k, 100ms frames
        self.ready = asyncio.Event()
        self.task = None
        self.active = False
        self.truncated = False
        self.wakeword, self.awake_until = wakeword, awake_until
        self.woken = False
        self.continuous_capture = continuous_capture

    async def prepare(self):
        self.task = asyncio.create_task(self._capture())
        ready = asyncio.create_task(self.ready.wait())
        try:
            done, _ = await asyncio.wait({ready, self.task}, return_when=asyncio.FIRST_COMPLETED)
            if self.task in done: self.task.result()
        finally:
            ready.cancel()
            await asyncio.gather(ready, return_exceptions=True)

    async def _capture(self):
        try:
            while True:
                await self.source.start_capture()
                self.active = True
                await self.link.interaction('LISTENING')
                waiting_wake = self.wakeword and time.monotonic() >= self.awake_until
                if waiting_wake: await self.link.interaction('IDLE')
                print('STANDBY: say TARS / Hello TARS to wake.' if waiting_wake else
                      'LISTENING: speak to TARS; silence stays on this computer.', flush=True)
                if self.wakeword: self.wakeword.reset()
                preroll = deque(maxlen=20 if self.wakeword else 5)
                voiced = silence = 0
                triggered = False
                # 2s warmup + <=8s idle + <=18s speech stays below firmware 30s cap.
                index = -1
                while True:
                    index += 1
                    frame = await self.source.read_frame()
                    if len(frame) != 3200: raise ValueError('invalid_local_capture_frame')
                    loud = pcm16_rms(frame) >= self.threshold
                    if not triggered:
                        preroll.append(frame)
                        voiced = voiced + 1 if loud else 0
                        waiting_wake = self.wakeword and time.monotonic() >= self.awake_until
                        keyword = await asyncio.to_thread(self.wakeword.feed, frame) if waiting_wake else ''
                        if keyword or (not waiting_wake and voiced >= 2):
                            self.woken = bool(keyword)
                            if keyword: print('AWAKE: TARS heard you.', flush=True)
                            triggered = True
                            start = index
                            for old in preroll: self.queue.put_nowait(old)
                            self.ready.set()
                        elif index >= 79 and not self.continuous_capture:
                            break
                    else:
                        self.queue.put_nowait(frame)
                        silence = 0 if loud else silence + 1
                        if silence >= 10: break
                        if index - start >= 179:
                            self.truncated = True
                            break
                self.active = False
                await self.source.stop_capture()
                if triggered:
                    if self.truncated: raise ValueError('utterance_too_long_repeat_shorter')
                    self.queue.put_nowait(None)
                    return
        finally:
            if self.active:
                self.active = False
                with contextlib.suppress(ConnectionError, TimeoutError):
                    await self.source.stop_capture()

    async def start_capture(self):
        if not self.ready.is_set(): raise RuntimeError('local_speech_not_ready')

    async def read_frame(self):
        if self.task.done(): self.task.result()
        packet = asyncio.create_task(self.queue.get())
        try:
            if not self.task.done():
                done, _ = await asyncio.wait({packet, self.task}, return_when=asyncio.FIRST_COMPLETED)
                if self.task in done: self.task.result()
            frame = await packet
            if frame is None:
                self.queue.put_nowait(None)
                await asyncio.sleep(.1)
                return bytes(3200)
            return frame
        finally:
            if not packet.done():
                packet.cancel()
                await asyncio.gather(packet, return_exceptions=True)

    async def stop_capture(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.active:
            self.active = False
            with contextlib.suppress(ConnectionError, TimeoutError):
                await self.source.stop_capture()
