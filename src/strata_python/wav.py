"""A WAV upload checked before it is kept: the header read with the standard library, refused with a sentence.

An app that records in the browser (strata-react's `useRecorder` writes 16-bit PCM mono WAV) or takes WAV files in
gets raw bytes it must not trust: a truncated upload, a stereo file, 8-bit audio, a click of Record then Stop. Here:

    info = read_wav(data, max_seconds=60)                # WavInfo: sample_rate, sample_width, channels, frames, seconds
    read_wav(path, channels=None, min_rate=8_000)        # any channel count, telephone rate and up
    levels = measure(data)                               # WavLevels: peak_db, clipped, noise_db, speech_db, snr_db

`WavError` is a `ValueError`, so `web.add_error_handlers` answers it as a 400 with the sentence. Only the header and
the last frame are read (a path is opened, not loaded): `wave` takes the frame count from the data chunk's declared
size, so a truncated upload still claims its full length until the last frame is looked for. Nothing is decoded, so it
costs the same for an hour as for a second. 32-bit float WAV is refused by `wave` itself (it reads PCM only), which is why the widths default to the
two a voice is recorded in.

`measure` is the one that decodes: the levels a recording studio judges a take by, for any app that keeps speech.
The noise floor and the speech level come from the loudness of 20 ms windows, the quietest tenth and the loudest
twentieth, so the gaps between words are the floor and a single cough does not set the speech level. Standard
library only (`array` and `map` keep the per-sample work in C: about 0.2 s for a minute at 48 kHz), so the whole
file is read into memory: it is for takes of seconds to minutes, not for hours."""

from __future__ import annotations

import io
import math
import operator
import os
import sys
import wave
from array import array
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO

#: Below 16 kHz the higher formants and fricatives a voice model needs are gone.
SPEECH_RATE = 16_000
#: The quietest level `measure` reports, in dBFS: digital silence reads this rather than minus infinity. Below the
#: 16-bit floor (-96 dBFS), so any real noise floor is above it.
SILENCE_DB = -100.0
#: A sample this close to full scale (within 0.1 %) counts as clipped: the browser's float-to-16-bit step and an
#: overdriven converter both flatten the wave at the top code, never a code or two short of it.
CLIP_SHARE = 0.999
WINDOW_SECONDS = 0.02


class WavError(ValueError):
    """A WAV that cannot be kept; the message says why in words a person can act on."""


@dataclass(frozen=True)
class WavInfo:
    sample_rate: int
    #: Bytes per sample: 2 is 16-bit, 3 is 24-bit.
    sample_width: int
    channels: int
    frames: int

    @property
    def seconds(self) -> float:
        return self.frames / self.sample_rate


def read_wav(
    source: bytes | str | os.PathLike[str],
    *,
    channels: int | None = 1,
    widths: Collection[int] = (2, 3),
    min_rate: int = SPEECH_RATE,
    min_seconds: float = 0.0,
    max_seconds: float | None = None,
) -> WavInfo:
    """The header of a PCM WAV (bytes or a path), or `WavError` when it is not one the app keeps.

    `channels=None` takes any count; `widths` are bytes per sample; `min_seconds`/`max_seconds` bound the length."""
    with io.BytesIO(source) if isinstance(source, bytes) else Path(source).open("rb") as f:
        found = _header(f)
    if channels is not None and found.channels != channels:
        want = "a mono" if channels == 1 else f"a {channels}-channel"
        raise WavError(f"Send {want} recording; this one has {found.channels} channels.")
    if found.sample_width not in widths:
        allowed = " or ".join(f"{w * 8}-bit" for w in sorted(widths))
        raise WavError(f"Send {allowed} samples; this recording has {found.sample_width * 8}-bit.")
    if found.sample_rate < min_rate:
        raise WavError(f"Record at {min_rate} Hz or more; this recording is {found.sample_rate} Hz.")
    if found.seconds < min_seconds:
        raise WavError(f"The recording is {found.seconds:.1f} seconds long; the shortest kept is {min_seconds:g}.")
    if max_seconds is not None and found.seconds > max_seconds:
        raise WavError(f"The recording is {found.seconds:.0f} seconds long; the longest kept is {max_seconds:g}.")
    return found


def _header(f: BinaryIO) -> WavInfo:
    if not f.read(1):
        raise WavError("The recording is empty: nothing was recorded.")
    f.seek(0)
    try:
        with wave.open(f, "rb") as w:
            found = WavInfo(w.getframerate(), w.getsampwidth(), w.getnchannels(), w.getnframes())
            if found.frames:
                w.setpos(found.frames - 1)
                complete = len(w.readframes(1)) == found.sample_width * found.channels
            else:
                complete = True
    except (wave.Error, EOFError, RuntimeError) as e:  # RuntimeError: a chunk that claims more bytes than follow
        raise WavError(f"The recording is not a WAV file that can be read ({str(e) or 'it ends early'}).") from None
    if not complete:
        raise WavError("The recording ends early: its header promises more sound than the file holds.")
    return found


@dataclass(frozen=True)
class WavLevels:
    """What a recording's level says about it, in dBFS (0 is full scale)."""

    #: The highest sample.
    peak_db: float
    #: Samples at full scale (`CLIP_SHARE`): more than a couple means the take clipped.
    clipped: int
    #: The room between words: the loudness (RMS) of the quietest tenth of 20 ms windows.
    noise_db: float
    #: The voice: the loudness of the loudest twentieth of 20 ms windows.
    speech_db: float
    seconds: float

    @property
    def snr_db(self) -> float:
        """How far the voice stands above the room, in dB."""
        return self.speech_db - self.noise_db


def _db(level: float) -> float:
    return max(SILENCE_DB, 20 * math.log10(level)) if level > 0 else SILENCE_DB


def _samples(raw: bytes, width: int) -> tuple[array[int], int]:
    """The samples as integers and their full scale. 24-bit is widened to 32-bit by putting each sample in the top
    three bytes of four (slices, no per-sample Python), which scales it by 256 and keeps the sign."""
    if width == 2:
        out = array("h", raw)
        full = 1 << 15
    else:
        wide = bytearray(len(raw) // 3 * 4)
        for i in range(3):
            wide[i + 1 :: 4] = raw[i::3]
        out = array("i", wide)
        full = 1 << 31
    if sys.byteorder == "big":  # WAV is little-endian
        out.byteswap()
    return out, full


def _percentile(sorted_values: list[float], share: float) -> float:
    return sorted_values[min(len(sorted_values) - 1, int(share * len(sorted_values)))]


def measure(source: bytes | str | os.PathLike[str]) -> WavLevels:
    """The levels of a 16- or 24-bit PCM WAV (bytes or a path), every channel together. `WavError` for anything
    `read_wav(channels=None, min_rate=1)` refuses."""
    with io.BytesIO(source) if isinstance(source, bytes) else Path(source).open("rb") as f:
        info = _header(f)
        if info.sample_width not in (2, 3):
            raise WavError(f"Send 16-bit or 24-bit samples; this recording has {info.sample_width * 8}-bit.")
        f.seek(0)
        with wave.open(f, "rb") as w:
            raw = w.readframes(info.frames)
    samples, full = _samples(raw, info.sample_width)
    if not samples:
        return WavLevels(SILENCE_DB, 0, SILENCE_DB, SILENCE_DB, 0.0)
    top, bottom = max(samples), min(samples)
    limit = CLIP_SHARE * full
    clipped = sum(1 for x in samples if abs(x) >= limit) if max(top, -bottom) >= limit else 0
    step = max(1, round(WINDOW_SECONDS * info.sample_rate)) * info.channels
    rms = sorted(
        _db(math.sqrt(sum(map(operator.mul, w, w)) / len(w)) / full)
        for w in (samples[i : i + step] for i in range(0, len(samples), step))
    )
    return WavLevels(
        peak_db=round(_db(max(top, -bottom) / full), 2),
        clipped=clipped,
        noise_db=round(_percentile(rms, 0.1), 2),
        speech_db=round(_percentile(rms, 0.95), 2),
        seconds=info.seconds,
    )
