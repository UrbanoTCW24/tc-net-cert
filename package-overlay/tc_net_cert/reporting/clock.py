"""Clock text for the operator. The stored stamp stays ISO."""

from __future__ import annotations

import re
from datetime import datetime

_MONTHS = ("ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic")
_ISO_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")


def human_clock(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    return f"{parsed.day} {_MONTHS[parsed.month - 1]} {parsed.year}, {parsed:%H:%M:%S}"


def readable_clocks(text: str) -> str:
    return _ISO_STAMP.sub(lambda match: human_clock(match.group(0)), text)
