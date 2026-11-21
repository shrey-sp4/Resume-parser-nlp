"""Education entry fields: degree, stream, institution, then degree_level.

Not built yet. School-level entries (10th, 12th) will go to the sidecar under
excluded, not into the main list.
"""

from __future__ import annotations


def parse_entry(lines: list[dict]) -> dict:
    raise NotImplementedError("education fields are built in a later step")
