"""Stage 17a — turning text into terms.

Everything an index can find is decided here. A term the tokeniser throws away
is a term no query can ever match, so this file is quietly the most
consequential in the search path — and the easiest to get subtly wrong in a way
that only shows up as "why does that search return nothing".

The rules are deliberately plain, and each one is a decision with a cost:

  CASE-FOLD.        `Crawler` and `crawler` are one term. Cost: acronyms
                    collide with words (US/us), which real engines fix with
                    case-sensitive fallbacks nobody here needs yet.

  SPLIT ON NON-WORD.  Unicode-aware, so `naïve` and `日本語` survive. Splitting
                    on ASCII only would silently drop most of the web.

  KEEP DIGITS.      Version numbers, years and error codes are exactly what
                    people search for.

  NO STEMMING.      `crawling` and `crawler` stay different terms. Stemming
                    raises recall and costs precision, and — more to the point
                    here — a stemmer is a large table of rules whose effects
                    are invisible in the index. BM25's job is easier to see
                    without one. Named as a gap rather than smuggled in.

  NO STOPWORD LIST. BM25 already handles common words correctly: a term in
                    every document has an IDF near zero and contributes almost
                    nothing. Deleting stopwords is an optimisation from an era
                    of expensive disks, and it breaks phrase queries ("to be or
                    not to be" is entirely stopwords).
"""
from __future__ import annotations

import re
import unicodedata

# \w is Unicode-aware in Python 3, so this keeps letters, digits and
# underscores in any script. The apostrophe is folded away rather than split
# on, so "don't" is one term rather than "don" and "t".
_WORD = re.compile(r"[^\W_]+(?:['’][^\W_]+)*", re.UNICODE)

MAX_TERM_CHARS = 64          # a "word" longer than this is markup or a hash


def normalize(text: str) -> str:
    """NFKC so that visually identical text is the same text.

    Without it, `ﬁle` (the ligature) and `file` are different terms, and a
    document using typographic ligatures becomes unsearchable by anything a
    person would type.
    """
    return unicodedata.normalize("NFKC", text).casefold()


def tokens(text: str) -> list[str]:
    """The terms of a document or a query. Order preserved."""
    if not text:
        return []
    return [t for t in _WORD.findall(normalize(text)) if len(t) <= MAX_TERM_CHARS]


def counted(text: str) -> dict[str, int]:
    """Term frequencies for one document."""
    frequencies: dict[str, int] = {}
    for term in tokens(text):
        frequencies[term] = frequencies.get(term, 0) + 1
    return frequencies
