"""One offline Stage 4 turn. No cloud provider, audio device or USB is opened."""
from __future__ import annotations
import asyncio
import wave
from .audio import ASR_PCM, UtteranceCollector, make_test_tone
from .body_audio import MockBodyAudioChannel, BodyAudioSource, BodyAudioSink
from .brain import TarsBrain
from .client import TarsClient
from .llm import ScriptedProvider
from .simulator import SimulatedTarsDevice
from .speech import create_offline_speech, SpeechOptions
from .transport import memory_transport_pair
from .voice import VoiceSession


async def run_mock(text: str = "介绍一下你的任务", output_wav: str | None = None):
    pc, peer = memory_transport_pair()
    device = SimulatedTarsDevice(peer)
    await device.start()
    client = TarsClient(pc)
    channel = MockBodyAudioChannel()
    try:
        await client.start()
        asr, tts = create_offline_speech(mock=True, text=text, options=SpeechOptions("auto", "mock-tone"))
        brain = TarsBrain(client, provider=ScriptedProvider("Mock TARS reply. Audio framework ready; no cloud or physical movement."))
        # Finite voiced frames + silence exercise the existing VAD collector.
        voiced = make_test_tone(100, volume_pct=10)
        for frame in [voiced, voiced, bytes(ASR_PCM.frame_bytes), bytes(ASR_PCM.frame_bytes)]:
            channel.feed_capture(frame)
        session = VoiceSession(brain, asr, tts, BodyAudioSink(channel),
            UtteranceCollector(silence_ms=200), dialogue_publisher=client.display_dialogue,
            interaction_publisher=client.interaction)
        turn = await session.run_turn(BodyAudioSource(channel))
        if output_wav:
            with wave.open(output_wav, "wb") as wav:
                wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(16000)
                wav.writeframes(bytes(channel.played))
        print("MOCK ONLY: no cloud, physical microphone, speaker, USB or Arm.")
        print("States: " + " -> ".join(device.interaction_history))
        print("ASR: " + turn.transcript)
        print("TARS: " + turn.response.text)
        print(f"PCM bytes: {len(channel.played)}; error={turn.speech_error}; armed={device.armed}")
        return turn
    finally:
        await client.close()
        await device.close()

