"""One page as seen by the stage 1 methods: the pdfplumber page, the pdfminer layout page,
the settings, and a cache of each method's output (so the page checks can compare methods
without running any method twice)."""

from __future__ import annotations

from typing import Any, Callable


class PageSource:
    def __init__(self, page: int, plumber_page: Any, miner_page: Any, params: dict,
                 extractors: dict[str, Callable[["PageSource"], Any]]):
        self.page = page
        self.plumber_page = plumber_page
        self.miner_page = miner_page
        self.params = params  # thresholds.json: stage1_extract and stage1_checks merged
        self._extractors = extractors
        self._cache: dict[str, Any] = {}

    @property
    def size(self) -> tuple[float, float]:
        for p in (self.plumber_page, self.miner_page):
            if p is not None:
                return round(float(p.width), 2), round(float(p.height), 2)
        return 0.0, 0.0

    def get(self, method: str):
        """The MethodOutput of one method on this page, computed once."""
        if method not in self._cache:
            self._cache[method] = self._extractors[method](self)
        return self._cache[method]
