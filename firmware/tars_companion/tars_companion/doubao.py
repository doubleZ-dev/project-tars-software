"""SC2.0 wire protocol and stateful PCM conversion. No network on import."""
import json
import struct
from .audio_levels import _kernel, samples, pack

URL = "wss://openspeech.bytedance.com/api/v3/realtime/dialogue"


def encode(event, payload=None, session=""):
    audio = isinstance(payload, bytes)
    body = payload if audio else json.dumps(payload or {}, ensure_ascii=False).encode()
    result = bytes((0x11, 0x24 if audio else 0x14, 0 if audio else 0x10, 0))
    result += struct.pack("!I", event)
    if event >= 100:
        sid = session.encode()
        result += struct.pack("!I", len(sid)) + sid
    return result + struct.pack("!I", len(body)) + body


def decode(data):
    if not isinstance(data, bytes) or len(data) < 8 or data[0] != 0x11 or data[3]:
        raise ValueError("invalid_doubao_header")
    kind, flags, serial, compression = data[1] >> 4, data[1] & 15, data[2] >> 4, data[2] & 15
    if kind not in (9, 11, 15) or compression not in (0, 1) or serial not in (0, 1):
        raise ValueError("unsupported_doubao_frame")
    offset = 4
    def number():
        nonlocal offset
        if offset + 4 > len(data):
            raise ValueError("truncated_doubao_frame")
        result = struct.unpack_from("!I", data, offset)[0]
        offset += 4
        return result
    def blob():
        nonlocal offset
        size = number()
        if size > len(data) - offset:
            raise ValueError("truncated_doubao_payload")
        result = data[offset:offset + size]
        offset += size
        return result
    if kind == 15:
        number()
        raise ConnectionError("doubao_server_error")  # never log remote payload/credentials
    if flags & 3 in (1, 3):
        number()
    if not flags & 4:
        raise ValueError("missing_doubao_event")
    event = number()
    identity = blob().decode()  # connection ID for 50..52; session ID otherwise
    payload = blob()
    if offset != len(data):
        raise ValueError("trailing_doubao_data")
    if compression:
        import zlib
        decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        payload = decompressor.decompress(payload, 1_048_577)
        if len(payload) > 1_048_576 or not decompressor.eof:
            raise ValueError("excessive_doubao_payload")
    if serial == 1:
        payload = json.loads(payload)
    if event in (51, 153, 599):
        raise ConnectionError("doubao_session_failed")
    return event, identity, payload


def session_config(config):
    if set(config) != {"model", "speaker", "character_manifest"}:
        raise ValueError("unexpected_doubao_configuration")
    if config["model"] != "2.2.0.0" or config["speaker"] != "S_vXjewQyk2":
        raise ValueError("model_or_speaker_change_requires_review")
    if not isinstance(config["character_manifest"], str) or not config["character_manifest"].strip():
        raise ValueError("empty_character_manifest")
    return {"tts": {"speaker": config["speaker"], "audio_config": {
                "channel": 1, "format": "pcm_s16le", "sample_rate": 24000}},
            "dialog": {"character_manifest": config["character_manifest"], "extra": {
                "model": config["model"], "input_mod": "keep_alive", "enable_volc_websearch": False}}}


class StreamingResampler:
    """24k -> 16k PCM16; 33-tap low-pass, retained phase/history across packets."""
    def __init__(self):
        self.data = []
        self.base = self.total = self.output = 0
        self.byte_tail = b""

    def feed(self, pcm, final=False):
        pcm = self.byte_tail + pcm
        self.byte_tail = pcm[len(pcm) // 2 * 2:]
        self.data.extend(samples(pcm[:len(pcm) // 2 * 2]))
        self.total += len(pcm) // 2
        if final and self.byte_tail:
            raise ValueError("unaligned_cloud_pcm")
        result = []
        while self.output < self.total * 2 // 3:
            centre, phase = divmod(self.output * 3, 2)
            if not final and centre + 16 >= self.total:
                break
            weights = _kernel(24000, phase, 2)
            result.append(sum(self.data[max(0, min(self.total - 1, centre + k - 16)) - self.base] * w
                              for k, w in enumerate(weights)))
            self.output += 1
        keep = max(self.base, self.output * 3 // 2 - 16)
        del self.data[:keep - self.base]
        self.base = keep
        return pack(result)
