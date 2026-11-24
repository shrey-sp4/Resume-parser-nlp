"""Okapi BM25, written out (no package): rank documents for a bag of query terms.

    idf(t) = ln(1 + (N - n_t + 0.5) / (n_t + 0.5))
    score(d) = sum over query terms t of idf(t) * f(t,d) * (k1 + 1) / (f(t,d) + k1 * (1 - b + b * |d| / avgdl))
"""

from __future__ import annotations

import math
import re
from collections import Counter

from shortlisting.tiers.text import STOPWORDS, ascii_lower

ENGLISH_STOP = frozenset("""a about above after again against all also am an and any are as at be because been before being
below between both but by can could did do does doing down during each few from further had has have having he her here hers
him his how i if into is it its itself just me more most my no nor not now of off once only or other our out over own same she
should so some such than that the their them then there these they this those through too under until up very was we were what
when where which while who whom why will with would you your using use based new via study towards""".split()) | STOPWORDS


def tokenize(text: str) -> list[str]:
    """Lower-case ASCII words of 2+ characters, stop words dropped, a plural 's' folded."""
    out = []
    for w in re.findall(r"[a-z0-9]{2,}", ascii_lower(text)):
        if w in ENGLISH_STOP:
            continue
        if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
            w = w[:-1]
        out.append(w)
    return out


class BM25:
    def __init__(self, docs: list[list[str]], k1: float = 1.5, b: float = 0.75):
        self.docs = [Counter(d) for d in docs]
        self.lengths = [len(d) for d in docs]
        self.n = len(docs)
        self.avgdl = (sum(self.lengths) / self.n) if self.n else 0.0
        self.k1, self.b = k1, b
        df = Counter(t for d in self.docs for t in d)
        self.idf = {t: math.log(1 + (self.n - c + 0.5) / (c + 0.5)) for t, c in df.items()}

    def score(self, query: list[str], i: int) -> float:
        d, dl = self.docs[i], self.lengths[i]
        if not dl or not self.avgdl:
            return 0.0
        s = 0.0
        for t in set(query):
            f = d.get(t, 0)
            if f:
                s += self.idf.get(t, 0.0) * f * (self.k1 + 1) / (f + self.k1 * (1 - self.b + self.b * dl / self.avgdl))
        return s

    def scores(self, query: list[str]) -> list[float]:
        return [self.score(query, i) for i in range(self.n)]
