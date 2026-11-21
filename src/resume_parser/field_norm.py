"""Field normalization for the values the GT format needs. Not built yet.

- degree_level from config/degrees.json
- experience_type from institution words (config/institutions.json)
- publication type from the heading, then venue words
"""

from __future__ import annotations


def degree_level(degree_text: str) -> str:
    raise NotImplementedError("built together with education extraction")


def experience_type(institution_text: str) -> str:
    raise NotImplementedError("built together with experience extraction")


def publication_type(heading: str, venue: str) -> str:
    raise NotImplementedError("built together with publication extraction")
