import json

from bs4 import BeautifulSoup

from mangastar_multisource.adapters.base import extract_story_status
from mangastar_multisource.domain.models import SourceWorkSnapshot
from mangastar_multisource.domain.status import (
    normalize_story_status,
    story_status_from_payload,
)
from mangastar_multisource.infrastructure.repositories import MySqlSourceRepository


def test_normalizer_covers_documented_english_and_arabic_forms():
    values = {
        "ongoing": ("ONGOING", "on going", "مستمر", "مستمرة", "مستمره"),
        "completed": ("completed", "finished", "مكتمل", "مكتملة", "منتهيه"),
        "hiatus": ("hiatus", "on hold", "متوقف", "متوقفة", "في استراحة"),
    }

    for expected, candidates in values.items():
        for candidate in candidates:
            assert normalize_story_status(candidate) == expected


def test_normalizer_accepts_api_status_objects_and_rejects_non_target_states():
    assert normalize_story_status({"id": 2, "name": "ONGOING"}) == "ongoing"
    assert story_status_from_payload({"state": "مكتملة"}) == "completed"
    assert story_status_from_payload({"status": {"label": "متوقفة"}}) == "hiatus"
    assert story_status_from_payload({"status": "coming"}) is None
    assert story_status_from_payload({"status": "لم يتم نشره"}) is None
    assert story_status_from_payload({"title": "An ongoing title"}) is None


def test_html_status_extraction_uses_labelled_work_metadata():
    soup = BeautifulSoup(
        """
        <div class="summary_content_item">
          <div class="summary-heading">الحالة</div>
          <div class="summary-content">مكتملة</div>
        </div>
        """,
        "html.parser",
    )

    assert extract_story_status(soup) == "مكتملة"
    assert normalize_story_status(extract_story_status(soup)) == "completed"


def test_html_status_extraction_does_not_use_translation_status():
    soup = BeautifulSoup(
        """
        <div class="summary_content_item">
          <div class="summary-heading">Translation Status</div>
          <div class="summary-content">Completed</div>
        </div>
        """,
        "html.parser",
    )

    assert extract_story_status(soup) is None


def test_html_status_extraction_handles_teamx_plain_text_rows():
    soup = BeautifulSoup(
        "<div class='full-list-info'><li>الحالة: متوقفة</li><li>الكاتب: شخص</li></div>",
        "html.parser",
    )

    assert extract_story_status(soup) == "متوقفة"


class _CreateSeriesCursor:
    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def execute(self, query, args=None):
        self.calls.append((query, args))

    def fetchall(self):
        return []

    def fetchone(self):
        for query, _ in reversed(self.calls):
            if "SELECT next_id FROM ms_canonical_id_sequences" in query:
                return None
        return None


def test_new_series_creation_writes_normalized_story_status_only():
    cursor = _CreateSeriesCursor()
    repository = object.__new__(MySqlSourceRepository)
    work = {
        "id": 77,
        "source_key": "mangaswat",
        "source_work_key": "/series/77",
        "source_url": "https://meshmanga.com/series/demo/",
        "title_raw": "Demo",
        "summary_raw": None,
        "cover_url": None,
        "alt_titles_json": "[]",
        "tags_json": "[]",
        "payload_json": json.dumps({"status": {"name": "مكتملة"}}, ensure_ascii=False),
    }

    series_id = repository._create_canonical_series_cursor(
        cursor,
        work,
        match_status="auto_created",
        match_score=0.2,
    )

    assert series_id == 1
    insert_query, insert_args = next(
        (query, args)
        for query, args in cursor.calls
        if "INSERT INTO series" in query
    )
    assert "translation_status, story_status" in insert_query
    assert insert_args[4] == "completed"
    assert "translation_status" not in " ".join(
        query for query, _ in cursor.calls if "UPDATE series" in query
    )


def test_source_story_status_reads_legacy_state_key():
    snapshot = SourceWorkSnapshot(
        source_key="teamx",
        source_work_key="/demo",
        source_url="https://example.test/demo",
        title="Demo",
        payload={"state": "في استراحة"},
    )

    assert MySqlSourceRepository._source_story_status(snapshot) == "hiatus"


def test_existing_series_update_targets_story_status_only():
    cursor = _CreateSeriesCursor()

    MySqlSourceRepository._update_series_story_status_cursor(cursor, 42, "ongoing")

    query, args = cursor.calls[-1]
    assert "SET story_status=%s" in query
    assert "translation_status" not in query
    assert args == ("ongoing", 42, "ongoing")
