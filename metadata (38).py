from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from datetime import datetime, timezone
from difflib import SequenceMatcher
from urllib.parse import urlparse

from ..domain.models import CanonicalSeries, MatchResult, SourceWorkSnapshot


_PUNCTUATION_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")
_ARABIC_DIGITS = str.maketrans("٠١٢٣٤٥٦٧٨٩", "0123456789")


_FEATURE_WEIGHTS = {
    "title": 0.58,
    "summary": 0.12,
    "cover": 0.10,
    # Credits are often transliterated, incomplete, or omitted by sources.
    # Treat them as weak positive evidence, never as a decisive conflict.
    "authors": 0.025,
    "painter": 0.03,
    "tags": 0.05,
    "type": 0.02,
    "publisher": 0.015,
    "chapter_range": 0.015,
    # Activity helps distinguish same-title/subtitle candidates, but it is
    # still weaker than identity metadata because translations may lag.
    "activity": 0.08,
}


def normalize_title(value: str | None) -> str:
    if not value:
        return ""
    value = unicodedata.normalize("NFKC", value).translate(_ARABIC_DIGITS).casefold()
    value = "".join(char for char in unicodedata.normalize("NFKD", value) if not unicodedata.combining(char))
    value = value.replace("&", " and ")
    value = _PUNCTUATION_RE.sub(" ", value)
    return _SPACE_RE.sub(" ", value).strip()


def _token_score(left: str, right: str) -> float:
    left_tokens, right_tokens = set(left.split()), set(right.split())
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _text_similarity(left: str | None, right: str | None) -> float | None:
    left_normalized, right_normalized = normalize_title(left), normalize_title(right)
    if not left_normalized or not right_normalized:
        return None
    return _normalized_similarity(left_normalized, right_normalized)


def _best_text_similarity(left_values: Iterable[str], right_values: Iterable[str]) -> float | None:
    left_values = tuple(value for value in left_values if normalize_title(value))
    right_values = tuple(value for value in right_values if normalize_title(value))
    if not left_values or not right_values:
        return None
    return max(
        (_text_similarity(left, right) or 0.0 for left in left_values for right in right_values),
        default=0.0,
    )


def _tag_similarity(left_values: Iterable[str], right_values: Iterable[str]) -> float | None:
    left = {normalize_title(value) for value in left_values if normalize_title(value)}
    right = {normalize_title(value) for value in right_values if normalize_title(value)}
    if not left or not right:
        return None
    return len(left & right) / len(left | right)


def _cover_similarity(left: str | None, right: str | None) -> float | None:
    if not left or not right:
        return None
    left_url, right_url = urlparse(left), urlparse(right)
    if left == right:
        return 1.0
    left_name = normalize_title(left_url.path.rsplit("/", 1)[-1])
    right_name = normalize_title(right_url.path.rsplit("/", 1)[-1])
    if not left_name or not right_name:
        return None
    # Different hosts often retain the same original filename. This is only
    # positive evidence; unrelated filenames never reduce the score.
    return 0.9 if left_name == right_name else None


def _payload_values(payload: dict, keys: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            values.append(value.strip())
        elif isinstance(value, (list, tuple)):
            values.extend(str(item).strip() for item in value if str(item).strip())
    return tuple(values)


def _chapter_range_similarity(source, candidate) -> float | None:
    source_numbers = [chapter.number for chapter in source.chapters if chapter.number is not None]
    latest = max(source_numbers, default=None)
    candidate_latest = candidate.latest_chapter_number
    if latest is None or candidate_latest is None:
        return None
    difference = abs(float(latest - candidate_latest))
    if difference == 0:
        return 1.0
    if difference <= 2:
        return 0.85
    if difference <= 10:
        return 0.65
    if difference <= 50:
        return 0.35
    return 0.0


def _coerce_datetime(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.startswith("0000-00-00"):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _activity_similarity(source, candidate) -> float | None:
    source_dates = [
        parsed
        for chapter in source.chapters
        if (parsed := _coerce_datetime(chapter.published_at)) is not None
    ]
    candidate_date = _coerce_datetime(candidate.latest_chapter_at)
    if not source_dates or not candidate_date:
        return None
    source_date = max(source_dates)
    if source_date.tzinfo is None:
        source_date = source_date.replace(tzinfo=timezone.utc)
    if candidate_date.tzinfo is None:
        candidate_date = candidate_date.replace(tzinfo=timezone.utc)
    days = abs((source_date - candidate_date).total_seconds()) / 86400
    if days <= 7:
        return 1.0
    if days <= 30:
        return 0.9
    if days <= 90:
        return 0.65
    if days <= 180:
        return 0.35
    if days <= 365:
        return 0.15
    # Old canonical activity is a weak negative prior only. It must never
    # reject a valid translation that is catching up. Keep a neutral floor so
    # exact identity evidence is not pushed below the auto-match threshold.
    return 0.55


def _activity_breaks_tie(
    top: tuple[CanonicalSeries, float, dict],
    second: tuple[CanonicalSeries, float, dict],
) -> bool:
    """Allow a cautious auto-link when exact identity has a clear activity lead."""
    top_evidence = top[2]
    second_evidence = second[2]
    top_features = top_evidence.get("feature_scores", {})
    second_features = second_evidence.get("feature_scores", {})
    top_title = float(top_features.get("title", 0.0))
    top_activity = top_features.get("activity")
    second_activity = second_features.get("activity")
    if top_title < 0.999 or top_activity is None or second_activity is None:
        return False
    if top[1] < 0.89 or "summary_conflict" in top_evidence.get("conflicts", ()):
        return False
    # A recent canonical latest chapter versus a clearly stale candidate is
    # useful disambiguation, but a small date difference must not bypass the
    # normal score margin.
    return float(top_activity) - float(second_activity) >= 0.25


def title_similarity(left: str, right: str) -> float:
    left, right = normalize_title(left), normalize_title(right)
    return _normalized_similarity(left, right)


def _normalized_similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0.0
    if left == right:
        return 1.0
    if left in right or right in left:
        return 0.96
    sequence = SequenceMatcher(None, left, right).ratio()
    tokens = _token_score(left, right)
    return round((sequence * 0.65) + (tokens * 0.35), 6)


class MetadataMatcher:
    def __init__(self, *, auto_threshold: float = 0.94, margin: float = 0.08) -> None:
        self.auto_threshold = auto_threshold
        self.margin = margin
        self._normalized_titles: dict[int, str] = {}
        self._token_index: dict[str, set[int]] = {}

    def prepare(self, candidates: list[CanonicalSeries]) -> None:
        """Precompute canonical title data once per polling run."""
        self._normalized_titles.clear()
        self._token_index.clear()
        for candidate in candidates:
            titles = (candidate.title, *candidate.alternative_titles)
            normalized = " ".join(normalize_title(title) for title in titles if normalize_title(title))
            self._normalized_titles[candidate.id] = normalized
            for token in {
                token
                for title in titles
                for token in normalize_title(title).split()
                if len(token) >= 3
            }:
                self._token_index.setdefault(token, set()).add(candidate.id)

    def candidate_ids_for_work(
        self,
        work: SourceWorkSnapshot,
        candidates: list[CanonicalSeries],
        *,
        allow_full_scan: bool = True,
    ) -> set[int]:
        if not self._normalized_titles:
            self.prepare(candidates)
        source_tokens = {
            token
            for title in (work.title, *work.alternative_titles)
            for token in normalize_title(title).split()
            if len(token) >= 3
        }
        token_hits = sorted(
            (
                (len(ids), ids)
                for token in source_tokens
                if (ids := self._token_index.get(token))
            ),
            key=lambda item: item[0],
        )
        if not token_hits:
            # A full catalog scan here is both expensive and low-confidence;
            # leave cross-script cases for explicit enrichment/visual review.
            return set()
        candidate_ids = set().union(*(ids for _, ids in token_hits))
        if len(candidate_ids) <= 5000:
            return candidate_ids
        # Common words such as "the" can make a union too broad. Prefer the
        # rarest few token buckets, which retain the useful title signal while
        # bounding the number of metadata comparisons.
        rare_candidate_ids = set().union(*(ids for _, ids in token_hits[:3]))
        return rare_candidate_ids if len(rare_candidate_ids) <= 5000 else set()

    def _candidate_pool(self, work: SourceWorkSnapshot, candidates: list[CanonicalSeries]) -> list[CanonicalSeries]:
        candidate_ids = self.candidate_ids_for_work(work, candidates)
        return [candidate for candidate in candidates if candidate.id in candidate_ids]

    def rank(self, work: SourceWorkSnapshot, candidates: list[CanonicalSeries]) -> list[tuple[CanonicalSeries, float, dict]]:
        pool = self._candidate_pool(work, candidates)
        source_titles = (work.title, *work.alternative_titles)
        ranked: list[tuple[CanonicalSeries, float, dict]] = []
        for candidate in pool:
            candidate_titles = (candidate.title, *candidate.alternative_titles)
            title_score = _best_text_similarity(source_titles, candidate_titles) or 0.0
            source_authors = tuple(work.author_names) or _payload_values(
                work.payload, ("author", "authors", "author_name", "author_names")
            )
            source_painters = tuple(work.painter_names) or _payload_values(
                work.payload, ("painter", "artist", "painter_name", "painter_names")
            )
            source_type = work.type_name or str(work.payload.get("type") or "").strip() or None
            source_publisher = work.publisher_name or str(work.payload.get("publisher") or "").strip() or None
            candidate_authors = (candidate.author_name,) if candidate.author_name else ()
            candidate_painters = (candidate.painter_name,) if candidate.painter_name else ()

            author_similarity = _best_text_similarity(source_authors, candidate_authors)
            # A mismatching credit is not reliable negative evidence. Keep
            # only a positive/near-positive author signal in the score.
            if author_similarity is not None and author_similarity < 0.20:
                author_similarity = None

            features: dict[str, float | None] = {
                "title": title_score,
                "summary": _text_similarity(work.summary, candidate.summary),
                "cover": _cover_similarity(work.cover_url, candidate.cover),
                "authors": author_similarity,
                "painter": _best_text_similarity(source_painters, candidate_painters),
                "tags": _tag_similarity(work.tags, candidate.tags),
                "type": _text_similarity(source_type, candidate.type_name),
                "publisher": _text_similarity(source_publisher, candidate.publisher_name),
                "chapter_range": _chapter_range_similarity(work, candidate),
                "activity": _activity_similarity(work, candidate),
            }
            available_weight = sum(
                _FEATURE_WEIGHTS[name] for name, value in features.items() if value is not None
            )
            score = (
                sum(_FEATURE_WEIGHTS[name] * float(value) for name, value in features.items() if value is not None)
                / available_weight
                if available_weight
                else 0.0
            )
            conflicts: list[str] = []
            if (
                features["title"] is not None
                and features["title"] < 0.96
                and features["summary"] is not None
                and features["summary"] < 0.12
            ):
                conflicts.append("summary_conflict")
            if conflicts:
                score = min(score, 0.74)
            evidence = {
                "source_titles": source_titles,
                "canonical_title": candidate.title,
                "canonical_alternative_titles": candidate.alternative_titles,
                "feature_scores": {name: value for name, value in features.items() if value is not None},
                "weights_used": {name: _FEATURE_WEIGHTS[name] for name, value in features.items() if value is not None},
                "available_weight": round(available_weight, 6),
                "conflicts": conflicts,
            }
            ranked.append((candidate, score, evidence))
        ranked.sort(key=lambda item: (-item[1], item[0].id))
        return ranked

    def decide(self, work: SourceWorkSnapshot, candidates: list[CanonicalSeries]) -> tuple[MatchResult, list[tuple[CanonicalSeries, float, dict]]]:
        ranked = self.rank(work, candidates)
        if not ranked:
            return MatchResult("unmatched", None, 0.0, 0.0, {"reason": "no_candidates"}), ranked
        top = ranked[0]
        second_score = ranked[1][1] if len(ranked) > 1 else 0.0
        margin = top[1] - second_score
        activity_tiebreak = len(ranked) > 1 and _activity_breaks_tie(top, ranked[1])
        if top[1] >= self.auto_threshold and (margin >= self.margin or top[1] == 1.0 or activity_tiebreak):
            evidence = dict(top[2])
            evidence["decision_reason"] = (
                "exact_title_and_activity_gap" if activity_tiebreak else "threshold_and_margin"
            )
            return MatchResult("matched", top[0].id, top[1], margin, evidence), ranked
        if top[1] >= 0.60:
            return MatchResult("candidate", None, top[1], margin, top[2]), ranked
        return MatchResult("unmatched", None, top[1], margin, top[2]), ranked
