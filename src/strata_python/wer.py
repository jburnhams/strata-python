"""Word error rate: how far a transcript is from the text that was meant, and which words differ.

An app that has speech transcribed (strata-models' `transcribe` role) and knows what was said checks one against the
other: a recording studio asks again for a take whose words were not the prompt's, an eval scores a model.

    errors = word_errors("The quick brown fox.", "the quick fox")
    errors.wer                  # 0.25: one of four words left out
    errors.missed               # ['brown']: the prompt's words that were not heard (left out or heard as another)
    [(e.op, e.said, e.heard) for e in errors.edits]     # ('ok', 'the', 'the'), ..., ('delete', 'brown', None), ...

Both sides are normalised the same way first (`normalise`): lower case, Unicode NFKC, curly apostrophes made straight,
punctuation, symbols and hyphens out (apostrophes inside a word kept, so "don't" is one word; combining marks kept,
so a vowel sign stays part of its word), whitespace collapsed. Number
words are not unified: "ten" against "10" counts as an error, which a caller that writes prompts can avoid by
spelling numbers out. The alignment is the plain Levenshtein one (each substitution, deletion and insertion costs 1),
computed in O(said x heard), which is nothing for a sentence and fine for a paragraph."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

Op = Literal["ok", "substitute", "delete", "insert"]


def normalise(text: str) -> list[str]:
    """The words of `text`, the way both sides of a comparison are read."""
    t = unicodedata.normalize("NFKC", text).lower().replace("\u2019", "'").replace("\u2018", "'")
    # Punctuation, symbols, separators and controls become spaces, by Unicode category rather than `[^\w]`: `\w`
    # leaves out combining marks, so it would split a Devanagari word at its vowel sign and read different words
    # as the same. The straight apostrophe stays here; the next line keeps it only inside a word.
    t = "".join(" " if c != "'" and unicodedata.category(c)[0] in "PSZC" else c for c in t)
    t = re.sub(r"(?<!\w)'|'(?!\w)", " ", t)
    return t.split()


@dataclass(frozen=True)
class Edit:
    """One step of the alignment: a word said and heard (`ok`), heard as another (`substitute`), not heard
    (`delete`: `heard` is None) or heard but not said (`insert`: `said` is None)."""

    op: Op
    said: str | None
    heard: str | None


@dataclass(frozen=True)
class WordErrors:
    """The alignment of what was meant (`said`, normalised) with what was heard."""

    said: list[str]
    heard: list[str]
    edits: list[Edit]

    @property
    def errors(self) -> int:
        return sum(e.op != "ok" for e in self.edits)

    @property
    def wer(self) -> float:
        """Errors over the words meant: 0 is exact, and it can pass 1 when much more was heard than meant. Nothing
        meant gives 0 if nothing was heard, else 1."""
        if not self.said:
            return 0.0 if not self.heard else 1.0
        return self.errors / len(self.said)

    @property
    def missed(self) -> list[str]:
        """The words meant that were not heard as themselves, in order."""
        return [e.said for e in self.edits if e.said is not None and e.op != "ok"]

    @property
    def extra(self) -> list[str]:
        """The words heard that were not meant at all, in order."""
        return [e.heard for e in self.edits if e.op == "insert" and e.heard is not None]


def word_errors(said: str, heard: str) -> WordErrors:
    """Align the words of `said` (the text meant) with `heard` (a transcript); see the module docstring."""
    a, b = normalise(said), normalise(heard)
    # cost[i][j]: edits from the first i words of a to the first j of b.
    cost = [list(range(len(b) + 1))]
    for i, word in enumerate(a, 1):
        prev, row = cost[-1], [i]
        for j, other in enumerate(b, 1):
            row.append(min(prev[j] + 1, row[j - 1] + 1, prev[j - 1] + (word != other)))
        cost.append(row)
    edits: list[Edit] = []
    i, j = len(a), len(b)
    while i or j:  # walk back, preferring a match or substitution, so equal-cost paths read naturally
        if i and j and cost[i][j] == cost[i - 1][j - 1] + (a[i - 1] != b[j - 1]):
            edits.append(Edit("ok" if a[i - 1] == b[j - 1] else "substitute", a[i - 1], b[j - 1]))
            i, j = i - 1, j - 1
        elif i and cost[i][j] == cost[i - 1][j] + 1:
            edits.append(Edit("delete", a[i - 1], None))
            i -= 1
        else:
            edits.append(Edit("insert", None, b[j - 1]))
            j -= 1
    return WordErrors(a, b, edits[::-1])
