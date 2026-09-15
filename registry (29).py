from __future__ import annotations

from collections.abc import Iterable

from ..config import Settings
from ..domain.ports import SourceAdapter
from .azorafly import AzoraFlyAdapter
from .madara import AsqAdapter, SparkMangaAdapter
from .mangatek import MangaTekAdapter
from .mangaswat import MangaSwatAdapter
from .teamx import TeamXNovelAdapter


def build_adapters(settings: Settings) -> tuple[SourceAdapter, ...]:
    adapters: tuple[SourceAdapter, ...] = (
        AzoraFlyAdapter(timeout_seconds=settings.http_timeout_seconds, user_agent=settings.http_user_agent),
        AsqAdapter(timeout_seconds=settings.http_timeout_seconds, user_agent=settings.http_user_agent),
        MangaTekAdapter(
            timeout_seconds=settings.http_timeout_seconds,
            user_agent=settings.http_user_agent,
            cookie=settings.mangatek_cookie,
        ),
        SparkMangaAdapter(
            timeout_seconds=settings.http_timeout_seconds,
            user_agent=settings.http_user_agent,
        ),
        MangaSwatAdapter(
            timeout_seconds=settings.http_timeout_seconds,
            user_agent=settings.http_user_agent,
        ),
        TeamXNovelAdapter(timeout_seconds=settings.http_timeout_seconds, user_agent=settings.http_user_agent),
    )
    return adapters


def select_adapters(adapters: Iterable[SourceAdapter], selected: set[str] | None) -> tuple[SourceAdapter, ...]:
    if not selected:
        return tuple(adapters)
    result = tuple(adapter for adapter in adapters if adapter.key in selected)
    unknown = selected - {adapter.key for adapter in result}
    if unknown:
        raise ValueError(f"Unknown source key(s): {', '.join(sorted(unknown))}")
    return result
