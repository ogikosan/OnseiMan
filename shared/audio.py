"""Audio WAV encoding / decoding helpers shared by client and server."""
from __future__ import annotations

import io
import wave

import numpy as np


def samples_to_wav_bytes(samples: np.ndarray, sample_rate: int = 16000) -> bytes:
    """Encode mono float32 samples [-1.0, 1.0] into 16-bit PCM WAV bytes."""
    clipped = np.clip(samples, -1.0, 1.0)
    pcm16 = (clipped * 32767.0).astype(np.int16)
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm16.tobytes())
    return buf.getvalue()


def decode_wav_bytes(data: bytes) -> tuple[np.ndarray, int]:
    """Decode WAV bytes (16-bit/32-bit PCM or float via soundfile) to mono float32."""
    try:
        import soundfile as sf

        audio, sr = sf.read(io.BytesIO(data), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1).astype(np.float32)
        return audio, int(sr)
    except Exception:
        pass

    with wave.open(io.BytesIO(data), "rb") as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        frames = wf.readframes(wf.getnframes())

    if sampwidth == 2:
        audio = np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0
    elif sampwidth == 4:
        audio = np.frombuffer(frames, dtype=np.int32).astype(np.float32) / 2147483648.0
    else:
        raise ValueError(f"Unsupported WAV sample width: {sampwidth}")

    if n_channels > 1:
        audio = audio.reshape(-1, n_channels).mean(axis=1).astype(np.float32)

    return audio, int(sr)
