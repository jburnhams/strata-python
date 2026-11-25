import pytest

from strata_python.wer import Edit, find, normalise, word_errors


def test_normalises_case_punctuation_and_apostrophes() -> None:
    assert normalise("Don\u2019t STOP\u2014the well-known 'quote', please!") == [
        "don't", "stop", "the", "well", "known", "quote", "please",
    ]  # fmt: skip
    assert normalise("  snake_case\tand\nlines ") == ["snake", "case", "and", "lines"]
    assert normalise("") == []
    assert normalise("a + b = c; 50% #tag") == ["a", "b", "c", "50", "tag"]


def test_keeps_combining_marks_inside_a_word() -> None:
    assert normalise("\u0915\u0940 \u0915\u0947") == [
        "\u0915\u0940",
        "\u0915\u0947",
    ]  # Devanagari "ki", "ke": the vowel signs stay
    assert word_errors("\u0915\u0940", "\u0915\u0947").wer == 1.0


def test_an_exact_transcript_has_no_errors() -> None:
    e = word_errors("The quick brown fox.", "the quick, brown fox")
    assert (e.wer, e.errors, e.missed, e.extra) == (0.0, 0, [], [])
    assert all(x.op == "ok" for x in e.edits)


def test_names_each_kind_of_error() -> None:
    e = word_errors("the quick brown fox jumps", "a quick fox jumps high")
    assert e.edits == [
        Edit("substitute", "the", "a"),
        Edit("ok", "quick", "quick"),
        Edit("delete", "brown", None),
        Edit("ok", "fox", "fox"),
        Edit("ok", "jumps", "jumps"),
        Edit("insert", None, "high"),
    ]
    assert e.wer == pytest.approx(3 / 5)
    assert e.missed == ["the", "brown"]
    assert e.extra == ["high"]


def test_nothing_said_or_heard() -> None:
    assert word_errors("", "").wer == 0.0
    assert word_errors("", "um").wer == 1.0
    assert word_errors("hello there", "").missed == ["hello", "there"]
    assert word_errors("one", "one two three four").wer == 3.0  # more heard than meant passes 1


def test_finds_the_reading_inside_a_longer_transcript() -> None:
    restart = find("The quick brown fox.", ["The", "quick,", "sorry.", "The", "quick", "brown", "fox."])
    assert (restart.start, restart.end, restart.errors.wer) == (3, 7, 0.0)
    chatter = find("Hello there.", ["okay", "hello", "there", "right", "next", "one"])
    assert (chatter.start, chatter.end) == (1, 3)
    # Twice read in full: the later reading.
    assert (find("a cat", ["a", "cat", "a", "cat"]).start, find("a cat", ["a", "cat", "a", "cat"]).end) == (2, 4)


def test_finds_a_misreading_and_keeps_its_errors() -> None:
    found = find("The quick brown fox", ["um", "the", "quick", "red", "fox", "thanks"])
    assert (found.start, found.end) == (1, 5)
    assert found.errors.missed == ["brown"]
    # A heard word that normalises to two maps back to its source, and one that is only punctuation to none.
    split = find("a well known song", ["—", "a", "well-known", "song"])
    assert (split.start, split.end, split.errors.wer) == (1, 4, 0.0)
    # A run never starts inside a heard word: "known" alone is the first word, not the end of "well-known".
    inside = find("known", ["known", "well-known"])
    assert (inside.start, inside.end, inside.errors.wer) == (0, 1, 0.0)


def test_finds_nothing_in_an_empty_or_unrelated_transcript() -> None:
    nothing = find("Hello there", [])
    assert (nothing.start, nothing.end, nothing.errors.wer) == (0, 0, 1.0)
    unrelated = find("Hello there", ["completely", "different"])
    assert unrelated.errors.wer == 1.0
    assert find("", ["anything"]).start == find("", ["anything"]).end
