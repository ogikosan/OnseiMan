"""Audio WAV encoding / decoding helpers shared by client and server."""
from __future__ import annotations

import io
import wave
from typing import Optional

import numpy as np

MAX_CHANNELS = 8


class AudioTooLong(ValueError):
    """max_sec より長い音声"""


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


def decode_wav_bytes(data: bytes, max_sec: Optional[float] = None) -> tuple[np.ndarray, int]:
    """Decode WAV bytes (16-bit/32-bit PCM or float via soundfile) to mono float32.

    max_sec を渡すと、それより長い音声は AudioTooLong にする。外から届いた音声（認識役）で使う:
    縮めてある WAV は小さくても開くと巨大になるので、上限の長さまでしか開かない。
    """
    try:
        import soundfile as sf

        with sf.SoundFile(io.BytesIO(data)) as f:
            sr = int(f.samplerate)
            if max_sec is None:
                audio = f.read(dtype="float32")
            else:
                if f.channels > MAX_CHANNELS or sr <= 0:
                    raise ValueError("Unsupported WAV")
                limit = int(max_sec * sr)
                # 先頭に書いてある長さは当てにしない。上限より 1 つ多く読んで、読めたら長すぎる
                audio = f.read(frames=limit + 1, dtype="float32")
                if len(audio) > limit:
                    raise AudioTooLong()
        if audio.ndim > 1:
            audio = audio.mean(axis=1).astype(np.float32)
        return audio, sr
    except AudioTooLong:
        raise
    except Exception:
        pass

    with wave.open(io.BytesIO(data), "rb") as wf:
        sr = wf.getframerate()
        n_channels = wf.getnchannels()
        sampwidth = wf.getsampwidth()
        if max_sec is not None:
            if n_channels > MAX_CHANNELS or sr <= 0:
                raise ValueError("Unsupported WAV")
            if wf.getnframes() > int(max_sec * sr):
                raise AudioTooLong()
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
