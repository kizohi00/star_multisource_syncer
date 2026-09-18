"""Status extraction and normalization shared by every source adapter.

The canonical Manga Star column used by the syncer is ``series.story_status``.
Source sites do not use one spelling or one language, so adapters may retain
their source-shaped value in the snapshot payload, while persistence always
passes it through :func:`story_status_from_payload`.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping


_STATUS_KEYS = (
    "status",
    "state",
    "story_status",
    "storyStatus",
    "series_status",
    "seriesStatus",
    "status_name",
    "statusName",
    "status_raw",
)

_ALIASES = {
    # Ongoing
    "ongoing": "ongoing",
    "on going": "ongoing",
    "on-going": "ongoing",
    "in progress": "ongoing",
    "active": "ongoing",
    "مستمر": "ongoing",
    "مستمرة": "ongoing",
    "مستمره": "ongoing",
    "جار": "ongoing",
    "جاري": "ongoing",
    "جارية": "ongoing",
    "قيد النشر": "ongoing",
    # Completed
    "completed": "completed",
    "complete": "completed",
    "finished": "completed",
    "finish": "completed",
    "done": "completed",
    "مكتمل": "completed",
    "مكتملة": "completed",
    "مكتمله": "completed",
    "منتهي": "completed",
    "منتهية": "completed",
    "منتهيه": "completed",
    "منته": "completed",
    "انتهى": "completed",
    "انتهت": "completed",
    "تم الانتهاء": "completed",
    # Hiatus
    "hiatus": "hiatus",
    "paused": "hiatus",
    "pause": "hiatus",
    "on hold": "hiatus",
    "suspended": "hiatus",
    "متوقف": "hiatus",
    "متوقفة": "hiatus",
    "متوقفه": "hiatus",
    "في استراحة": "hiatus",
    "استراحة": "hiatus",
    "متوقف مؤقتا": "hiatus",
    "متوقفة مؤقتا": "hiatus",
}


def _normalized_text(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.replace("\u0640", "")
    text = text.replace("\u200e", "").replace("\u200f", "")
    text = re.sub(r"[_/\\:؛،,|]+", " ", text)
    text = re.sub(r"\s*-\s*", " ", text)
    return " ".join(text.casefold().split())


def _status_value(value: object) -> object:
    """Unwrap the common API status-object shapes without scanning arbitrary data."""
    if isinstance(value, Mapping):
        for key in ("name", "title", "label", "value", "status", "state"):
            candidate = value.get(key)
            if candidate not in (None, ""):
                return _status_value(candidate)
        return None
    if isinstance(value, (list, tuple)):
        for candidate in value:
            unwrapped = _status_value(candidate)
            if unwrapped not in (None, ""):
                return unwrapped
        return None
    return value


def normalize_story_status(value: object) -> str | None:
    """Return one of ``ongoing``, ``completed`` or ``hiatus``.

    Values such as Manga Swat's ``coming``/``لم يتم نشره`` deliberately return
    ``None`` because they are not one of the three statuses stored by Star.
    Unknown values also return ``None`` rather than guessing.
    """
    value = _status_value(value)
    normalized = _normalized_text(value)
    if not normalized:
        return None
    return _ALIASES.get(normalized)


def story_status_from_payload(payload: object) -> str | None:
    """Read an explicitly named source status and normalize it.

    The function intentionally checks only status-shaped keys. It must not
    search all payload text, because titles and descriptions can contain
    words such as ``ongoing`` without describing the work's state.
    """
    if not isinstance(payload, Mapping):
        return None
    for key in _STATUS_KEYS:
        if key not in payload:
            continue
        normalized = normalize_story_status(payload.get(key))
        if normalized is not None:
            return normalized
    return None
