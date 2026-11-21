"""Publication fields: title, year, venue, type; printed totals into stated_totals.

Not built yet. Talks, posters and newspaper articles will go to the sidecar
under excluded.non_paper_publications.
"""

from __future__ import annotations


def parse_citation(lines: list[dict]) -> dict:
    raise NotImplementedError("publication fields are built in a later step")
