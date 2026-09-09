"""采集进行中，前端也应该看得到已找到的帖子/日志/请求数。

背景：notes.jsonl / comments.jsonl / collection.json / competitor_collection.json
都只在对应阶段全部跑完后才落盘；采集或竞对周报中途失败（比如没钱了）之前
读不到任何东西，也传不到前端 —— 这正是 "找到帖子并没有传入前端" 的原因。

这里覆盖两层：
1. collect.py / broad_scan.py 在过程中额外写 collection.partial.json（notes.partial.jsonl /
   raw/search_pages.partial.json 已经在更早一轮加上了）；
2. service.py 读取时优先用最终产物，缺失时退回 partial 产物，这样前端轮询到的
   run 详情/帖子预览在采集进行中也是最新的。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from xhs_listener.broad_scan import broad_collect
from xhs_listener.io_utils import read_json, write_json, write_jsonl
from xhs_listener.models import BroadScanConfig, CollectConfig, KeywordConfig

from tests.test_weekly_collection import FakeCollector, RUN_START, _card

WINDOW_DAYS = 8


def test_topic_collection_writes_a_live_partial_summary(tmp_path) -> None:
    recent = RUN_START - timedelta(days=1)
    collector = FakeCollector({"港大商学院": [[_card("n1", recent), _card("n2", recent)]]}, str(tmp_path))
    config = CollectConfig(keyword="港大商学院", max_pages=1, scan_mode="topic_scan")

    collector.collect(config)

    run_dir = next(path.parent for path in tmp_path.rglob("collection.json"))
    partial = read_json(run_dir / "collection.partial.json")
    assert partial["partial"] is True
    assert partial["scan_mode"] == "topic_scan"
    assert partial["notes_count"] == 2
    assert partial["search_pages_count"] == 1
    # FakeCollector 直接替换了 _api_call，不会像真实 collector 那样递增
    # api_request_count；这里只确认字段存在、类型正确，不对具体数值断言。
    assert isinstance(partial["api_request_count"], int)
    assert isinstance(partial["logs"], list) and partial["logs"]


def test_broad_collection_writes_a_live_partial_summary(tmp_path) -> None:
    recent = RUN_START - timedelta(days=1)
    collector = FakeCollector({"港大商学院": [[_card("n1", recent)]]}, str(tmp_path))
    config = BroadScanConfig(
        keyword_pool=[KeywordConfig(keyword="港大商学院", max_pages=1)],
        output_dir=str(tmp_path),
    )

    broad_collect(collector, config)

    run_dir = next(path.parent for path in tmp_path.rglob("collection.json"))
    partial = read_json(run_dir / "collection.partial.json")
    assert partial["scan_mode"] == "broad_scan"
    assert partial["notes_count"] == 1


def test_note_preview_falls_back_to_partial_notes_while_collecting(tmp_path) -> None:
    from xhs_listener.service import _note_preview_rows

    run_dir = tmp_path / "run1"
    write_jsonl(run_dir / "notes.partial.jsonl", [{"note_id": "a", "title": "t"}])
    run = {"id": 1, "run_dir": str(run_dir)}

    rows = _note_preview_rows(run)

    assert [row["note_id"] for row in rows] == ["a"]


def test_note_preview_prefers_final_notes_once_collection_finishes(tmp_path) -> None:
    from xhs_listener.service import _note_preview_rows

    run_dir = tmp_path / "run1"
    write_jsonl(run_dir / "notes.partial.jsonl", [{"note_id": "stale", "title": "t"}])
    write_jsonl(run_dir / "notes.jsonl", [{"note_id": "final", "title": "t"}])
    run = {"id": 1, "run_dir": str(run_dir)}

    rows = _note_preview_rows(run)

    assert [row["note_id"] for row in rows] == ["final"]


def test_collection_summary_falls_back_to_partial_while_collecting(tmp_path) -> None:
    from xhs_listener.service import _read_collection_summary

    run_dir = tmp_path / "run1"
    write_json(
        run_dir / "collection.partial.json",
        {"partial": True, "notes_count": 3, "comments_count": 0, "api_request_count": 5, "logs": [], "errors": []},
    )

    summary = _read_collection_summary(str(run_dir))

    assert summary == {
        "api_request_count": 5,
        "api_unit_cost_usd": 0.01,
        "notes_count": 3,
        "comments_count": 0,
    }


def test_collection_summary_prefers_final_file_once_present(tmp_path) -> None:
    from xhs_listener.service import _read_collection_summary

    run_dir = tmp_path / "run1"
    write_json(
        run_dir / "collection.partial.json",
        {"partial": True, "notes_count": 1, "comments_count": 0, "api_request_count": 1, "logs": [], "errors": []},
    )
    write_json(
        run_dir / "collection.json",
        {"notes_count": 9, "comments_count": 2, "quality": {"api_request_count": 20}, "errors": [], "logs": []},
    )

    summary = _read_collection_summary(str(run_dir))

    assert summary["notes_count"] == 9
    assert summary["api_request_count"] == 20


def test_competitor_progress_reads_partial_school_count_mid_run(tmp_path) -> None:
    from xhs_listener.service import _competitor_progress

    run_dir = tmp_path / "run1"
    write_json(
        run_dir / "competitor_collection.partial.json",
        {
            "schools_config": [{"school": s, "keyword": s} for s in ("A", "B", "C", "D", "E", "F", "G")],
            "schools": [{"school": "A"}, {"school": "B"}, {"school": "C"}],
        },
    )

    done, total = _competitor_progress(str(run_dir))

    assert (done, total) == (3, 7)


def test_competitor_progress_prefers_final_file_once_present(tmp_path) -> None:
    from xhs_listener.service import _competitor_progress

    run_dir = tmp_path / "run1"
    write_json(
        run_dir / "competitor_collection.partial.json",
        {"schools_config": [{"school": "A"}] * 7, "schools": [{"school": "A"}] * 3},
    )
    write_json(
        run_dir / "competitor_collection.json",
        {"schools_config": [{"school": "A"}] * 7, "schools": [{"school": "A"}] * 7},
    )

    done, total = _competitor_progress(str(run_dir))

    assert (done, total) == (7, 7)


def test_competitor_progress_is_zero_without_any_collection_file(tmp_path) -> None:
    from xhs_listener.service import _competitor_progress

    assert _competitor_progress(str(tmp_path / "missing")) == (0, 0)
    assert _competitor_progress(None) == (0, 0)
