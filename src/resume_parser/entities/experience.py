"""Experience entry fields: job_title, institution, dates as printed, then experience_type.

Not built yet. Parsed start and end dates will go to the sidecar.
"""

from __future__ import annotations


def parse_entry(lines: list[dict]) -> dict:
    raise NotImplementedError("experience fields are built in a later step")
