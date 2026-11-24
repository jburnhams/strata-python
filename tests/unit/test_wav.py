import io
import re
import wave
from pathlib import Path

import pytest

from strata_python.testing import sine_wav
from strata_python.wav import SILENCE_DB, SPEECH_RATE, WavError, WavInfo, WavLevels, measure, read_wav


def test_reads_a_mono_16_bit_wav() -> None:
    info = read_wav(sine_wav(1.5, rate=48_000))
    assert info == WavInfo(sample_rate=48_000, sample_width=2, channels=1, frames=72_000)
    assert info.seconds == 1.5


def test_reads_a_path_and_24_bit(tmp_path: Path) -> None:
    path = tmp_path / "take.wav"
    path.write_bytes(sine_wav(0.5, width=3))
    assert read_wav(path).sample_width == 3
    assert read_wav(str(path)).frames == SPEECH_RATE // 2


def test_the_tone_is_at_its_amplitude() -> None:
    with wave.open(io.BytesIO(sine_wav(0.1, amplitude=0.5, width=2)), "rb") as w:
        raw = w.readframes(w.getnframes())
    peak = max(abs(int.from_bytes(raw[i : i + 2], "little", signed=True)) for i in range(0, len(raw), 2))
    assert peak == pytest.approx(0.5 * 32767, rel=0.01)
    with wave.open(io.BytesIO(sine_wav(0.1, width=1, amplitude=0)), "rb") as w:
        assert set(w.readframes(w.getnframes())) == {128}  # 8-bit is unsigned: silence is the middle


@pytest.mark.parametrize(
    ("data", "words"),
    [
        (b"", "empty"),
        (b"not a wav at all", "not a WAV file that can be read (file does not start with RIFF id)"),
        (sine_wav(1.0)[:30], "not a WAV file that can be read (it ends early)"),
    ],
)
def test_refuses_what_is_not_a_wav(data: bytes, words: str) -> None:
    with pytest.raises(WavError, match=re.escape(words)):
        read_wav(data)


def test_a_chunk_that_claims_more_than_follows_is_refused() -> None:
    data = bytearray(sine_wav(0.5))
    data[16:20] = (10_000).to_bytes(4, "little")  # the fmt chunk now claims 10 kB
    with pytest.raises(WavError, match="not a WAV file that can be read"):
        read_wav(bytes(data))


def test_refuses_the_wrong_shape_with_a_sentence() -> None:
    with pytest.raises(WavError, match="Send a mono recording; this one has 2 channels"):
        read_wav(sine_wav(1.0, channels=2))
    with pytest.raises(WavError, match="Send a 2-channel recording; this one has 1 channels"):
        read_wav(sine_wav(1.0), channels=2)
    assert read_wav(sine_wav(1.0, channels=2), channels=None).channels == 2
    with pytest.raises(WavError, match="Send 16-bit or 24-bit samples; this recording has 8-bit"):
        read_wav(sine_wav(1.0, width=1))
    with pytest.raises(WavError, match="Record at 16000 Hz or more; this recording is 8000 Hz"):
        read_wav(sine_wav(1.0, rate=8_000))
    assert read_wav(sine_wav(1.0, rate=8_000), min_rate=8_000).sample_rate == 8_000


def test_bounds_the_length() -> None:
    with pytest.raises(WavError, match=r"0\.1 seconds long; the shortest kept is 0\.3"):
        read_wav(sine_wav(0.1), min_seconds=0.3)
    with pytest.raises(WavError, match="3 seconds long; the longest kept is 2"):
        read_wav(sine_wav(3.0), max_seconds=2)
    assert read_wav(sine_wav(2.0), min_seconds=0.3, max_seconds=2).seconds == 2.0


def test_a_recording_cut_short_after_its_header_is_refused(tmp_path: Path) -> None:
    whole = sine_wav(1.0)
    with pytest.raises(WavError, match="ends early: its header promises more sound"):
        read_wav(whole[:44], min_seconds=0.5)
    with pytest.raises(WavError, match="ends early"):
        read_wav(whole[:-1])  # half of the last frame
    path = tmp_path / "cut.wav"
    path.write_bytes(whole[:1000])
    with pytest.raises(WavError, match="ends early"):
        read_wav(path)


def test_an_empty_file_and_a_wav_with_no_frames(tmp_path: Path) -> None:
    path = tmp_path / "empty.wav"
    path.write_bytes(b"")
    with pytest.raises(WavError, match="empty"):
        read_wav(path)
    assert read_wav(sine_wav(0.0)).frames == 0


def test_measures_the_levels_of_a_take() -> None:

    levels = measure(sine_wav(1.0, amplitude=0.5, lead=0.5, tail=0.5, noise=0.001, rate=48_000))
    assert levels.seconds == 2.0
    assert levels.peak_db == pytest.approx(-6.0, abs=0.1)
    assert levels.clipped == 0
    assert levels.speech_db == pytest.approx(-9.0, abs=0.2)  # a sine's RMS is 3 dB under its peak
    assert -70 < levels.noise_db < -60  # uniform noise of peak 0.001 (-60 dBFS) has an RMS 4.8 dB lower
    assert levels.snr_db == pytest.approx(levels.speech_db - levels.noise_db)


def test_counts_clipped_samples_and_reads_24_bit(tmp_path: Path) -> None:

    path = tmp_path / "loud.wav"
    path.write_bytes(sine_wav(0.5, amplitude=1.5, width=3))
    levels = measure(path)
    assert levels.peak_db == pytest.approx(0.0, abs=0.01)
    assert levels.clipped > 1000  # a third of every cycle sits at full scale
    assert measure(sine_wav(0.5, amplitude=0.5, width=3)).peak_db == pytest.approx(-6.0, abs=0.1)


def test_silence_and_an_empty_take_read_the_floor() -> None:

    assert measure(sine_wav(0.5, amplitude=0)).noise_db == SILENCE_DB
    assert measure(sine_wav(0.0)) == WavLevels(SILENCE_DB, 0, SILENCE_DB, SILENCE_DB, 0.0)


def test_measure_refuses_what_it_cannot_decode() -> None:

    with pytest.raises(WavError, match="16-bit or 24-bit"):
        measure(sine_wav(0.1, width=1))
    with pytest.raises(WavError, match="not a WAV"):
        measure(b"RIFFnope")
    broken = bytearray(sine_wav(0.1))
    broken[24:28] = (0).to_bytes(4, "little")  # the fmt chunk's sample rate
    with pytest.raises(WavError, match="no sample rate"):
        measure(bytes(broken))
