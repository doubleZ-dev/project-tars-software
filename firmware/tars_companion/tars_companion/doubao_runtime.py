"""Opt-in paid SC2.0 test runtime. Half duplex; no model-to-motion tools."""
import asyncio
import contextlib
import json
import os
import time
import uuid
from pathlib import Path
from .audio import SoundDeviceMicrophone, PcmFormat, pcm16_rms
from .audio_levels import BodyMicrophone
from .doubao import URL, encode, decode, session_config, StreamingResampler
from .wifi_audio import LanAudioServer, NetworkJsonTransport, load_profile, PLAY_START, PLAY_PCM, PLAY_END


class DesktopMic:
    def __init__(self):
        self.mic = SoundDeviceMicrophone(pcm_format=PcmFormat(frame_ms=20))

    async def start_capture(self):
        self.mic.flush()

    async def read_frame(self):
        return await self.mic.read_frame()

    async def stop_capture(self):
        self.mic.close()
        self.mic.flush()


class Output:
    def __init__(self, link, volume):
        self.link, self.volume = link, volume
        self.stream = None
        self.started = None
        self.count = 0
        self.first_ack = None

    async def write(self, pcm):
        if not pcm:
            return
        if self.link:
            for i in range(0, len(pcm), 512):
                chunk = pcm[i:i + 512]
                # Stay below firmware 10s/12s limits, including network stalls.
                if self.started is not None and (self.count + len(chunk) > 256000 or time.monotonic() - self.started > 8):
                    await self.finish()
                if self.started is None:
                    if self.link.mode != "idle":
                        raise RuntimeError("half_duplex_busy")
                    self.link.mode = "playback"
                    await self.link.command(PLAY_START, bytes([self.volume]))
                    self.started = time.monotonic()
                await self.link.command(PLAY_PCM, chunk)
                if self.first_ack is None:
                    self.first_ack = time.monotonic()
                self.count += len(chunk)
        else:
            if self.stream is None:
                import sounddevice as sd
                self.stream = sd.RawOutputStream(samplerate=16000, channels=1, dtype="int16")
                self.stream.start()
            # Same conservative amplitude as robot; no per-packet AGC/pumping.
            from .audio_levels import samples, pack
            await asyncio.to_thread(self.stream.write, pack(v * self.volume / 100 for v in samples(pcm)))

    async def finish(self):
        if self.link and self.started is not None:
            await self.link.command(PLAY_END)
            self.link.mode = "idle"
        if self.stream:
            stream, self.stream = self.stream, None
            await asyncio.to_thread(stream.stop)
            stream.close()
        self.started = None
        self.count = 0

    async def abort(self):
        # Existing wire has no immediate PLAY_ABORT. Closing invokes firmware
        # abort/mute and invalidates all queued PCM; never resume this connection.
        if self.link:
            await self.link.close()
        if self.stream:
            self.stream.abort()
            self.stream.close()
            self.stream = None


async def receive(ws, sid=None):
    event, identity, payload = decode(await asyncio.wait_for(ws.recv(), 30))
    if sid and event >= 100 and identity != sid:
        raise ConnectionError("wrong_doubao_session")
    if event in (52, 152):
        raise ConnectionError("doubao_session_closed")
    return event, payload


async def turn(ws, sid, source, output, link=None):
    output.first_ack = None
    metrics = {"speech_end_estimate": None, "asr_end": None, "cloud_first": None, "play_submit": None}
    queue = asyncio.Queue(maxsize=500)  # 500 x 20ms cloud PCM = 10s; overflow fails closed
    sender = player = None
    async def upload():
        # Existing firmware capture is capped at 30s, including 2s settling.
        async with asyncio.timeout(25):
            while True:
                frame = await source.read_frame()
                if not frame or len(frame) % 640:
                    raise ValueError("invalid_microphone_frame")
                for offset in range(0, len(frame), 640):
                    chunk = frame[offset:offset + 640]
                    if pcm16_rms(chunk) >= 500:
                        metrics["speech_end_estimate"] = time.monotonic()
                    await asyncio.wait_for(ws.send(encode(200, chunk, sid)), 2)
                    await asyncio.sleep(.02)

    async def stop_input():
        nonlocal sender
        if sender and not sender.done():
            sender.cancel()
            await asyncio.gather(sender, return_exceptions=True)
        elif sender:
            sender.result()
        sender = None
        await source.stop_capture()

    async def playback():
        converter = StreamingResampler()
        while True:
            item = await asyncio.wait_for(queue.get(), 5)
            pcm = converter.feed(item or b"", final=item is None)
            if pcm:
                if metrics["play_submit"] is None:
                    metrics["play_submit"] = time.monotonic()
                await output.write(pcm)
            if item is None:
                await output.finish()
                return

    try:
        await asyncio.wait_for(source.start_capture(), 5)
        if link:
            await link.interaction("LISTENING")
        print("LISTENING (half duplex; /cancel ends session)", flush=True)
        sender = asyncio.create_task(upload())
        async with asyncio.timeout(90):
            while True:
                if sender and sender.done():
                    sender.result()
                    raise ConnectionError("microphone_stopped_early")
                receiving = asyncio.create_task(receive(ws, sid))
                watches = {receiving}
                if sender and not sender.done():
                    watches.add(sender)
                if player:
                    watches.add(player)
                try:
                    done, _ = await asyncio.wait(watches, return_when=asyncio.FIRST_COMPLETED)
                    for task in done - {receiving}:
                        task.result()
                        raise ConnectionError("audio_worker_stopped_early")
                    event, payload = receiving.result()
                finally:
                    if not receiving.done():
                        receiving.cancel()
                        await asyncio.gather(receiving, return_exceptions=True)
                if event == 459:
                    metrics["asr_end"] = time.monotonic()
                    await stop_input()
                    if link:
                        await link.interaction("THINKING")
                elif event == 352:
                    if not isinstance(payload, bytes) or not payload:
                        raise ValueError("invalid_cloud_audio")
                    if metrics["cloud_first"] is None:
                        metrics["cloud_first"] = time.monotonic()
                        await stop_input()
                        player = asyncio.create_task(playback())
                    for offset in range(0, len(payload), 960):
                        queue.put_nowait(payload[offset:offset + 960])
                elif event == 359:
                    await stop_input()
                    if player is None:
                        raise ValueError("empty_cloud_audio")
                    await asyncio.wait_for(queue.put(None), 2)
                    await asyncio.wait_for(player, 20)
                    break
        def elapsed(a, b):
            return round((metrics[b] - metrics[a]) * 1000, 1) if metrics[a] is not None and metrics[b] is not None else None
        print(json.dumps({"model": "2.2.0.0", "speaker": "S_vXjewQyk2",
            "input": "pcm_s16le/16000/mono", "cloud_output": "pcm_s16le/24000/mono",
            "playback": "pcm_s16le/16000/mono", "target": "robot" if link else "desktop",
            "speech_end_estimate_to_cloud_ms": elapsed("speech_end_estimate", "cloud_first"),
            "asr_end_to_cloud_ms": elapsed("asr_end", "cloud_first"),
            "cloud_to_play_submit_ms": elapsed("cloud_first", "play_submit"),
            "cloud_to_robot_first_pcm_ack_ms": round((output.first_ack - metrics["cloud_first"]) * 1000, 1)
                if output.first_ack is not None and metrics["cloud_first"] is not None else None,
            "speech_end_estimate_to_play_submit_ms": elapsed("speech_end_estimate", "play_submit"),
            "acoustic_latency_ms": None}))
        if link:
            await link.interaction("IDLE")
        await asyncio.sleep(.25)  # speaker tail before restarting capture
    except BaseException:
        await output.abort()
        raise
    finally:
        for task in (sender, player):
            if task and not task.done():
                task.cancel()
        await asyncio.gather(*(t for t in (sender, player) if t), return_exceptions=True)
        with contextlib.suppress(Exception):
            await source.stop_capture()


def preflight(args):
    config = json.loads(Path(args.doubao_config).read_text(encoding="utf-8-sig"))
    request = session_config(config)
    if args.allow_motion:
        raise ValueError("doubao_test_forbids_motion")
    if not 1 <= args.doubao_seconds <= 600:
        raise ValueError("doubao_test_duration_must_be_1_to_600_seconds")
    if args.mock:
        return request
    if not args.live or not args.approve_doubao_cost or not args.speaker_compatible:
        raise ValueError("doubao_requires_cost_approval_and_verified_speaker")
    if args.robot and not args.standalone_verified:
        raise ValueError("standalone_evaluation_required_first")
    if not os.environ.get("DOUBAO_APP_ID") or not os.environ.get("DOUBAO_ACCESS_KEY"):
        raise ValueError("DOUBAO_APP_ID_and_DOUBAO_ACCESS_KEY_required")
    return request


async def run(args):
    request = preflight(args)  # before opening any network/device
    if args.mock:
        print("Doubao configuration valid; OFFLINE ONLY. Run unittest test_doubao for simulated protocol/audio checks.")
        return
    from websockets.asyncio.client import connect
    from .robot_runtime import RuntimeClient, console
    server = client = link = output = source = None
    try:
        if args.robot:
            profile = load_profile(args.config)
            server = LanAudioServer(profile["token"])
            await server.start(profile["host"], profile["port"])
            link = await server.wait_connected(60)
            client = RuntimeClient(NetworkJsonTransport(link), allow_motion=False)
            await client.start()
            await client.disarm()
            source = BodyMicrophone(link, gain=args.mic_gain)
        else:
            source = DesktopMic()
        output = Output(link, args.volume)
        headers = {"X-Api-App-ID": os.environ["DOUBAO_APP_ID"],
                   "X-Api-Access-Key": os.environ["DOUBAO_ACCESS_KEY"],
                   "X-Api-Resource-Id": "volc.speech.dialog",
                   "X-Api-App-Key": "PlgvMymc7f3tQnJ6",  # public protocol constant, not credential
                   "X-Api-Connect-Id": str(uuid.uuid4())}
        async with connect(URL, additional_headers=headers, open_timeout=10, close_timeout=2,
                           max_size=1_048_576, max_queue=16, compression=None) as ws:
            await ws.send(encode(1))
            if (await receive(ws))[0] != 50:
                raise ConnectionError("doubao_connection_not_started")
            sid = str(uuid.uuid4())
            await ws.send(encode(100, request, sid))
            if (await receive(ws, sid))[0] != 150:
                raise ConnectionError("doubao_session_not_started")
            async def dialogue():
                async with asyncio.timeout(args.doubao_seconds):
                    while True:
                        await turn(ws, sid, source, output, link)
                        if args.once:
                            return
            async def operator():
                while True:
                    command = (await console("/cancel /stop /disarm /quit > ")).strip()
                    if command in ("/cancel", "/stop", "/disarm", "/quit", "quit", "exit"):
                        if client and command == "/stop":
                            await client.estop()
                        elif client and command == "/disarm":
                            await client.disarm()
                        return
                    print("This test mode has no motion commands.")
            jobs = [asyncio.create_task(dialogue()), asyncio.create_task(operator())]
            try:
                done, _ = await asyncio.wait(jobs, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in jobs:
                    task.cancel()
                await asyncio.gather(*jobs, return_exceptions=True)
                await output.abort()
                # FinishSession cancels this response at the service. Do not use
                # ClientInterrupt (515), which is only valid in push_to_talk mode.
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(ws.send(encode(102, {}, sid)), 1)
            # WebSocket context closes even if FinishSession fails. No retry/replay.
    finally:
        if output:
            await output.abort()
        if source:
            with contextlib.suppress(Exception):
                await source.stop_capture()
        if client:
            await client.close()
        if server:
            await server.close()
