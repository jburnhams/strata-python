"""A WAV upload checked before it is kept: the header read with the standard library, refused with a sentence.

An app that records in the browser (strata-react's `useRecorder` writes 16-bit PCM mono WAV) or takes WAV files in
gets raw bytes it must not trust: a truncated upload, a stereo file, 8-bit audio, a click of Record then Stop. Here:

    info = read_wav(data, max_seconds=60)                # WavInfo: sample_rate, sample_width, channels, frames, seconds
    read_wav(path, channels=None, min_rate=8_000)        # any channel count, telephone rate and up

`WavError` is a `ValueError`, so `web.add_error_handlers` answers it as a 400 with the sentence. Only the header is read
and the frames counted (`wave` checks the data chunk's length); nothing is decoded, so it costs the same for an hour as
for a second. 32-bit float WAV is refused by `wave` itself (it reads PCM only), which is why the widths default to the
two a voice is recorded in."""

from __future__ import annotations

import io
import os
import wave
from collections.abc import Collection
from dataclasses import dataclass
from pathlib import Path

#: Below 16 kHz the higher formants and fricatives a voice model needs are gone.
SPEECH_RATE = 16_000


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
    data = source if isinstance(source, bytes) else Path(source).read_bytes()
    if not data:
        raise WavError("The recording is empty: nothing was recorded.")
    try:
        with wave.open(io.BytesIO(data), "rb") as w:
            found = WavInfo(w.getframerate(), w.getsampwidth(), w.getnchannels(), w.getnframes())
    except (wave.Error, EOFError, RuntimeError) as e:  # RuntimeError: a chunk that claims more bytes than follow
        raise WavError(f"The recording is not a WAV file that can be read ({str(e) or 'it ends early'}).") from None
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
