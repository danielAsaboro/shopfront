"""Drafts state what the door recorded; they never accuse anyone.

A draft goes from a small shop to its supplier or carrier, who it still has to work with. Shopfront's
evidence (a doorbell's events, a model's reading of a frame) can show a scan doesn't match the door; it
can't show anyone lied. So no draft Shopfront writes may call anything a lie, fraud, fake, falsified,
dishonest or cheating. tests/test_wording.py checks every draft the pack builder can produce.

`accusing()` matches whole words after folding the text the way a reader would see it: Unicode NFKC
(full-width and other compatibility forms), accents and zero-width characters stripped, then common
lookalikes folded to one Latin skeleton (Cyrillic and Greek letters that look Latin, and the digits and
symbols people swap in: "l1ed", "fr@ud", "fаke" with a Cyrillic а). In that skeleton i, l, 1, | and !
are one letter, as are o and 0, so "lie" matches "1ie" and "LIE" but not "supplier".
"""

from __future__ import annotations

import re
import unicodedata

ACCUSING = ("lie", "lied", "lies", "lying", "liar", "fraud", "frauds", "fraudulent", "fraudulently", "fake",
            "faked", "fakes", "faking", "falsified", "falsify", "falsifying", "dishonest", "dishonestly",
            "cheat", "cheated", "cheating", "cheats")

# Lookalikes, folded to the Latin letter they pass for (after NFKC and lowercasing)
_CONFUSABLE = str.maketrans({
    # Cyrillic
    "а": "a", "в": "b", "е": "e", "ё": "e", "к": "k", "м": "m", "н": "h", "о": "o", "р": "p", "с": "c",
    "т": "t", "у": "y", "х": "x", "ѕ": "s", "і": "i", "ї": "i", "ј": "j", "ԁ": "d", "ӏ": "l", "ԛ": "q",
    "ԝ": "w", "һ": "h", "ɡ": "g",
    # Greek
    "α": "a", "β": "b", "ε": "e", "η": "n", "ι": "i", "κ": "k", "ν": "v", "ο": "o", "ρ": "p", "τ": "t",
    "υ": "u", "χ": "x", "ϲ": "c", "ϳ": "j",
    # Latin variants, digits and symbols
    "ı": "i", "ℓ": "l", "0": "o", "3": "e", "4": "a", "5": "s", "7": "t", "@": "a", "$": "s",
})
_SAME_AS_I = str.maketrans({"l": "i", "1": "i", "|": "i", "!": "i"})  # i/l/1/|/! read alike
_INVISIBLE = re.compile(r"[­​-‏⁠-⁤﻿]")


def fold(text: str) -> str:
    """The text as a reader sees it: NFKC, lowercase, no accents or invisible characters, lookalikes folded."""
    t = _INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).lower()
    t = "".join(c for c in unicodedata.normalize("NFKD", t) if not unicodedata.combining(c))
    return t.translate(_CONFUSABLE).translate(_SAME_AS_I)


_WORDS = re.compile(r"(?<![a-z])(" + "|".join(sorted({fold(w) for w in ACCUSING}, key=len, reverse=True))
                    + r")(?![a-z])")


def accusing(text: str) -> list[str]:
    """The accusing words in `text`, as folded, in order of appearance."""
    return _WORDS.findall(fold(text))
