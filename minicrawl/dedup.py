"""Stage 6 — deciding when two pages are the same page.

There are three different questions here and they need three different tools:

  IDENTICAL      byte-for-byte the same text  -> a cryptographic hash
  NEARLY THE SAME  same page, a few words changed -> a similarity hash
  DECLARED THE SAME  the page says so itself   -> rel=canonical

The first is easy and the third is free. The second is the interesting one,
because equality-based structures cannot answer it at all: a hash table finds
exact matches, and "differs by six words out of two hundred" is not a match.

SIMHASH (Charikar, 2002) is a *locality-sensitive* hash — similar inputs get
similar hashes, which is the exact opposite of what a cryptographic hash
promises. Build it by:

  1. splitting the text into overlapping k-word shingles, so word ORDER
     survives (bag-of-words would call "dog bites man" and "man bites dog"
     identical);
  2. hashing each shingle to 64 bits;
  3. summing across shingles with each bit voting +weight if set, -weight if
     clear;
  4. keeping the sign of each column.

Flipping a few shingles moves a few votes, which flips at most a few bits. So
similarity becomes HAMMING DISTANCE, and "nearly the same" becomes a number.

The remaining problem is lookup. Comparing a new document against every stored
fingerprint is O(n) per page, which is the cost simhash was supposed to avoid.
BANDING fixes it: split the 64 bits into B bands and index each band
separately. Two fingerprints within B bits of each other must agree exactly on
at least one band, so a candidate lookup is a handful of dict hits, and only
the candidates get compared bit by bit.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from enum import Enum

BITS = 64
# Banding is only sound while max_distance < BANDS: two fingerprints differing
# in fewer bits than there are bands must, by the pigeonhole principle, agree
# exactly on at least one band. 8 bands of 8 bits covers distances up to 7.
# Widening the threshold without widening the bands does not make the index
# slower, it makes it WRONG -- it silently stops finding the pairs it is for.
BANDS = 8
SHINGLE_WORDS = 4
_WORD = re.compile(r"\w+")

# Below this length a simhash is mostly noise: a dozen shingles cannot populate
# 64 bit-columns meaningfully, and short pages start colliding with each other.
# Short documents get exact-duplicate detection only.
MIN_WORDS_FOR_NEAR_DUP = 25

# Length is not the only way a document can be unfit for simhash. A page can be
# enormous and still carry almost no distinct features -- one phrase repeated a
# few thousand times. Then the two or three distinct shingles have near-equal
# weights, cancel each other in every bit column, and a four-shingle difference
# decides the sign of fourteen bits. Measured, not theorised: this project's
# first generator filler was exactly that, and two pages differing by one word
# out of 7,679 came out 14 bits apart.
MIN_DISTINCT_SHINGLES = 20


class Verdict(str, Enum):
    NEW = "new"
    EXACT_DUPLICATE = "exact_duplicate"
    NEAR_DUPLICATE = "near_duplicate"
    CANONICAL_ALIAS = "canonical_alias"
    # The same URL, fetched again, yielding the same document. Not a duplicate:
    # one document, reached twice. It happens whenever several spellings
    # redirect to one place -- /a, /a/ and the end of the /r/1 chain are all the
    # same page, and calling that "a duplicate of itself" is nonsense that would
    # show up in every count on the report.
    ALREADY_SEEN = "already_seen"


@dataclass(slots=True)
class Decision:
    verdict: Verdict
    of: str | None = None       # the URL this duplicates, if any
    distance: int | None = None # hamming distance, for near duplicates

    @property
    def is_duplicate(self) -> bool:
        return self.verdict is not Verdict.NEW


def content_hash(text: str) -> str:
    """Exact identity. Whitespace-normalised so formatting is not content."""
    return hashlib.sha256(" ".join(text.split()).encode()).hexdigest()


def shingles(text: str, k: int = SHINGLE_WORDS) -> list[str]:
    """Overlapping k-word windows. Order-preserving, unlike a bag of words."""
    words = _WORD.findall(text.lower())
    if len(words) < k:
        return [" ".join(words)] if words else []
    return [" ".join(words[i:i + k]) for i in range(len(words) - k + 1)]


def simhash(text: str, bits: int = BITS) -> int:
    columns = [0] * bits
    for shingle in shingles(text):
        digest = int.from_bytes(hashlib.blake2b(shingle.encode(), digest_size=8).digest(),
                                "big")
        for position in range(bits):
            columns[position] += 1 if digest >> position & 1 else -1
    value = 0
    for position, total in enumerate(columns):
        if total > 0:
            value |= 1 << position
    return value


def hamming(a: int, b: int) -> int:
    return (a ^ b).bit_count()


def word_count(text: str) -> int:
    return len(_WORD.findall(text))


@dataclass
class DuplicateIndex:
    """Exact hashes in a dict, simhashes in banded buckets."""
    # Measured against this corpus, not copied from the paper: the declared near
    # pair sits at 4 bits and the closest non-pair at 24. The folklore constant
    # of 3 is calibrated for documents with thousands of shingles; the right
    # threshold is a property of YOUR document population and has to be measured.
    max_distance: int = 6
    min_words: int = MIN_WORDS_FOR_NEAR_DUP
    min_distinct_shingles: int = MIN_DISTINCT_SHINGLES

    _by_hash: dict[str, str] = field(default_factory=dict, repr=False)
    _bands: list[dict[int, list[tuple[int, str]]]] = field(default_factory=list, repr=False)
    _canonical: dict[str, str] = field(default_factory=dict, repr=False)
    counts: dict[str, int] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not self._bands:
            self._bands = [{} for _ in range(BANDS)]

    def add(self, url: str, text: str, canonical: str | None = None) -> Decision:
        if canonical and canonical != url:
            self._canonical[url] = canonical
            return self._tally(Decision(Verdict.CANONICAL_ALIAS, of=canonical))

        digest = content_hash(text)
        if (seen := self._by_hash.get(digest)) is not None:
            verdict = Verdict.ALREADY_SEEN if seen == url else Verdict.EXACT_DUPLICATE
            return self._tally(Decision(verdict, of=seen))
        self._by_hash[digest] = url

        if word_count(text) < self.min_words:
            return self._tally(Decision(Verdict.NEW))
        if len(set(shingles(text))) < self.min_distinct_shingles:
            # Too few distinct features for a stable fingerprint. Storing one
            # would poison the index with a value that is mostly noise.
            return self._tally(Decision(Verdict.NEW))

        fingerprint = simhash(text)
        for other, other_url in self._candidates(fingerprint):
            distance = hamming(fingerprint, other)
            if distance <= self.max_distance:
                return self._tally(
                    Decision(Verdict.NEAR_DUPLICATE, of=other_url, distance=distance))

        self._store(fingerprint, url)
        return self._tally(Decision(Verdict.NEW))

    # -- banded lookup -----------------------------------------------------
    def _band_keys(self, fingerprint: int) -> list[int]:
        width = BITS // BANDS
        return [(fingerprint >> (i * width)) & ((1 << width) - 1) for i in range(BANDS)]

    def _candidates(self, fingerprint: int):
        """Only fingerprints sharing at least one exact band. Two values within
        BANDS-1 bits must agree on some band by the pigeonhole principle: a
        difference of at most BANDS-1 bits cannot touch all BANDS bands."""
        seen: set[int] = set()
        for band, key in enumerate(self._band_keys(fingerprint)):
            for other, url in self._bands[band].get(key, ()):
                if other not in seen:
                    seen.add(other)
                    yield other, url

    def _store(self, fingerprint: int, url: str) -> None:
        for band, key in enumerate(self._band_keys(fingerprint)):
            self._bands[band].setdefault(key, []).append((fingerprint, url))

    def _tally(self, decision: Decision) -> Decision:
        self.counts[decision.verdict.value] = self.counts.get(decision.verdict.value, 0) + 1
        return decision

    @property
    def unique_documents(self) -> int:
        return self.counts.get(Verdict.NEW.value, 0)
