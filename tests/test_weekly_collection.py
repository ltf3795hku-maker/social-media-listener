"""Weekly collection window / page allocation / funnel statistics.

覆盖：
1. Topic 既有精确 8×24 小时报告窗口的边界（刚好在内、刚好在外）；
2. TikHub 即使传了「一周内」仍返回旧帖时，旧帖不得进入周报数据集；
3. 窗口过滤发生在详情请求之前（省 TikHub 调用），且窗口外的帖子不触发详情；
4. Broad 按关键词请求配置的页数；
5. Topic 样本量选项与 TikHub 页数对齐；
6. 漏斗统计能与最终笔记数对账。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from xhs_listener.broad_scan import broad_collect, parse_keyword_pool
from xhs_listener.collect import (
    WINDOW_INSIDE,
    WINDOW_OUTSIDE,
    WINDOW_UNDATED,
    XiaohongshuCollector,
    build_collection_funnel,
    classify_published_at,
    describe_reporting_window,
    reporting_window_bounds,
    window_is_active,
)
from xhs_listener.io_utils import read_json, read_jsonl
from xhs_listener.models import BroadScanConfig, CollectConfig, KeywordConfig

RUN_START = datetime(2026, 8, 24, 16, 41, 4)
WINDOW_DAYS = 8
WINDOW = reporting_window_bounds(RUN_START, WINDOW_DAYS)


# --------------------------------------------------------------------------
# 1. 窗口边界
# --------------------------------------------------------------------------


def test_reporting_window_is_exactly_eight_times_twenty_four_hours() -> None:
    start, end = WINDOW

    assert end == RUN_START
    assert start == datetime(2026, 8, 16, 16, 41, 4)
    assert end - start == timedelta(days=WINDOW_DAYS)
    # 窗口锚在 run 的时刻而非日期，所以起点保留了 16:41:04。
    assert start.time() == end.time()


def test_boundary_posts_immediately_inside_and_outside_the_window() -> None:
    start, end = WINDOW
    just_inside_start = start + timedelta(seconds=1)
    just_outside_start = start - timedelta(seconds=1)

    assert classify_published_at(start.isoformat(), WINDOW) == WINDOW_INSIDE
    assert classify_published_at(just_inside_start.isoformat(), WINDOW) == WINDOW_INSIDE
    assert classify_published_at(end.isoformat(), WINDOW) == WINDOW_INSIDE
    assert classify_published_at(just_outside_start.isoformat(), WINDOW) == WINDOW_OUTSIDE
    # 未来时间也在窗口外（end 之后）。
    assert classify_published_at((end + timedelta(seconds=1)).isoformat(), WINDOW) == WINDOW_OUTSIDE


def test_window_is_not_silently_widened_beyond_the_configured_days() -> None:
    # Topic 既有 8 天窗口内，7.5 天前仍保留。
    seven_and_a_half_days_ago = RUN_START - timedelta(days=7, hours=12)
    assert classify_published_at(seven_and_a_half_days_ago.isoformat(), WINDOW) == WINDOW_INSIDE
    eight_and_a_half_days_ago = RUN_START - timedelta(days=8, hours=12)
    assert classify_published_at(eight_and_a_half_days_ago.isoformat(), WINDOW) == WINDOW_OUTSIDE
    nine_days_ago = RUN_START - timedelta(days=9)
    assert classify_published_at(nine_days_ago.isoformat(), WINDOW) == WINDOW_OUTSIDE


def test_window_length_is_driven_by_the_shared_constant() -> None:
    from xhs_listener.models import REPORTING_WINDOW_DAYS

    assert REPORTING_WINDOW_DAYS == WINDOW_DAYS
    start, end = reporting_window_bounds(RUN_START)
    assert end - start == timedelta(days=REPORTING_WINDOW_DAYS)


def test_unix_timestamps_in_seconds_and_milliseconds_are_both_understood() -> None:
    inside = RUN_START - timedelta(days=1)
    outside = RUN_START - timedelta(days=400)

    assert classify_published_at(str(int(inside.timestamp())), WINDOW) == WINDOW_INSIDE
    assert classify_published_at(str(int(inside.timestamp() * 1000)), WINDOW) == WINDOW_INSIDE
    assert classify_published_at(str(int(outside.timestamp())), WINDOW) == WINDOW_OUTSIDE


def test_undated_posts_are_kept_and_counted_separately() -> None:
    """产品决定：拿不到发布时间的帖子保留，但单独计数，不当成旧帖丢掉。"""

    for value in (None, "", "0", "not-a-date"):
        assert classify_published_at(value, WINDOW) == WINDOW_UNDATED


def test_window_only_applies_when_the_search_asked_for_one_week() -> None:
    weekly = CollectConfig(keyword="港大商学院", time_filter="一周内")
    half_year = CollectConfig(keyword="港大商学院", time_filter="半年内")
    unlimited = CollectConfig(keyword="港大商学院", time_filter="不限")
    disabled = CollectConfig(keyword="港大商学院", time_filter="一周内", reporting_window_days=0)

    assert window_is_active(weekly) is True
    assert window_is_active(half_year) is False
    assert window_is_active(unlimited) is False
    assert window_is_active(disabled) is False
    # 没有窗口时一律视为窗口内，不会误杀。
    assert classify_published_at("2020-01-01", None) == WINDOW_INSIDE


def test_window_description_records_bounds_for_downstream() -> None:
    described = describe_reporting_window(WINDOW, WINDOW_DAYS)

    assert described["applied"] is True
    assert described["days"] == 8
    assert described["start"] == "2026-08-16T16:41:04"
    assert described["end"] == "2026-08-24T16:41:04"
    assert described["utc_offset"]
    assert describe_reporting_window(None, WINDOW_DAYS)["applied"] is False


# --------------------------------------------------------------------------
# 采集器测试替身
# --------------------------------------------------------------------------


def _search_payload(items: list[dict[str, Any]]) -> dict[str, Any]:
    return {"data": {"items": items}}


def _card(note_id: str, published_at: datetime | None, title: str = "港大商学院讨论") -> dict[str, Any]:
    note: dict[str, Any] = {"note_id": note_id, "title": title, "desc": "正文内容足够长可以通过噪声检查。"}
    if published_at is not None:
        note["timestamp"] = str(int(published_at.timestamp()))
    return {"note": note}


class FakeCollector(XiaohongshuCollector):
    """只替换网络层：搜索按关键词返回预置卡片，详情调用被记录下来。"""

    def __init__(self, pages_by_keyword: dict[str, list[list[dict[str, Any]]]], output_root: str) -> None:
        super().__init__(api_token="test", output_root=output_root)
        self.pages_by_keyword = pages_by_keyword
        self.detail_calls: list[str] = []
        self.search_calls: list[tuple[str, int]] = []

    def _api_call(self, run, path, params):  # type: ignore[override]
        keyword = str(params.get("keyword") or "")
        page = int(params.get("page") or 1)
        self.search_calls.append((keyword, page))
        pages = self.pages_by_keyword.get(keyword, [])
        items = pages[page - 1] if page - 1 < len(pages) else []
        return _search_payload(items)

    def fetch_image_detail(self, note_id, xsec_token, run):  # type: ignore[override]
        self.detail_calls.append(note_id)
        return {}

    def fetch_comments(self, *args, **kwargs):  # type: ignore[override]
        return []


# --------------------------------------------------------------------------
# 2 & 3. 旧帖不进入数据集，且不触发详情请求
# --------------------------------------------------------------------------


def test_old_posts_returned_despite_weekly_filter_are_excluded(tmp_path) -> None:
    inside = RUN_START - timedelta(days=2)
    old = RUN_START - timedelta(days=400)
    collector = FakeCollector(
        {
            "HKUBS": [[
                _card("n_recent", inside, "本周 HKUBS 讨论"),
                _card("n_old", old, "两年前的 HKUBS 旧帖"),
                _card("n_undated", None, "没有时间戳的 HKUBS 帖子"),
            ]]
        },
        str(tmp_path),
    )
    config = CollectConfig(
        keyword="HKUBS",
        max_pages=1,
        max_notes=20,
        time_filter="一周内",
        scope_pattern=None,
    )

    run = collector.collect(config)
    note_ids = [note.note_id for note in run.notes]

    assert "n_old" not in note_ids
    assert "n_recent" in note_ids
    # 缺时间的帖子按决定保留。
    assert "n_undated" in note_ids
    # 关键：窗口外的帖子没有触发详情请求，省下的正是这笔 TikHub 调用。
    assert "n_old" not in collector.detail_calls
    assert sorted(collector.detail_calls) == ["n_recent", "n_undated"]

    collection = read_json(next(tmp_path.rglob("collection.json")))
    assert len(read_json(next(tmp_path.rglob("raw/search_pages.partial.json")))["pages"]) == 1
    assert len(read_jsonl(next(tmp_path.rglob("notes.partial.jsonl")))) == 2
    funnel = collection["quality"]["collection_funnel"]
    assert funnel["raw_search_results"] == 3
    assert funnel["removed_outside_window"] == 1
    assert funnel["undated_kept"] == 1
    assert funnel["within_reporting_window"] == 1
    assert funnel["notes_after_collection"] == 2
    assert collection["quality"]["reporting_window"]["applied"] is True


def test_no_window_filter_when_time_filter_is_not_weekly(tmp_path) -> None:
    old = RUN_START - timedelta(days=400)
    collector = FakeCollector({"港大商学院": [[_card("n_old", old)]]}, str(tmp_path))
    config = CollectConfig(keyword="港大商学院", max_pages=1, time_filter="不限", scope_pattern=None)

    run = collector.collect(config)

    assert [note.note_id for note in run.notes] == ["n_old"]
    collection = read_json(next(tmp_path.rglob("collection.json")))
    assert collection["quality"]["reporting_window"]["applied"] is False


# --------------------------------------------------------------------------
# 4. Broad 按关键词请求配置的页数
# --------------------------------------------------------------------------


def test_broad_requests_configured_pages_per_keyword(tmp_path) -> None:
    inside = RUN_START - timedelta(days=1)
    pages_by_keyword = {
        "港大商学院": [[_card(f"a{i}", inside)] for i in range(3)],
        "香港大学商学院": [[_card(f"b{i}", inside)] for i in range(2)],
        "HKUBS": [[_card("c0", inside)]],
    }
    collector = FakeCollector(pages_by_keyword, str(tmp_path))
    config = BroadScanConfig(
        keyword_pool=[
            KeywordConfig(keyword="港大商学院", max_pages=3, max_notes=60),
            KeywordConfig(keyword="香港大学商学院", max_pages=2, max_notes=40),
            KeywordConfig(keyword="HKUBS", max_pages=1, max_notes=20),
        ],
        scope_pattern=None,
        output_dir=str(tmp_path),
    )

    broad_collect(collector, config)
    run_dir = next(path.parent for path in tmp_path.rglob("collection.json"))
    partial_pages = read_json(run_dir / "raw" / "search_pages.partial.json")["pages"]
    assert [row["keyword"] for row in partial_pages] == [
        "港大商学院",
        "港大商学院",
        "港大商学院",
        "香港大学商学院",
        "香港大学商学院",
        "HKUBS",
    ]
    assert len(read_jsonl(run_dir / "notes.partial.jsonl")) == 6

    requested = {}
    for keyword, page in collector.search_calls:
        requested.setdefault(keyword, set()).add(page)
    assert requested["港大商学院"] == {1, 2, 3}
    assert requested["香港大学商学院"] == {1, 2}
    assert requested["HKUBS"] == {1}
    assert len(collector.search_calls) == 6


def test_broad_keeps_undated_posts_like_topic_does(tmp_path) -> None:
    """缺发布时间的帖子保留并单独计数；Broad 与 Topic 必须同一口径。"""

    inside = RUN_START - timedelta(days=1)
    collector = FakeCollector(
        {"HKUBS": [[_card("dated", inside), _card("undated", None)]]},
        str(tmp_path),
    )
    config = BroadScanConfig(
        keyword_pool=[KeywordConfig(keyword="HKUBS", max_pages=1, max_notes=20)],
        scope_pattern=None,
        output_dir=str(tmp_path),
    )

    run = broad_collect(collector, config)
    collection = read_json(next(tmp_path.rglob("collection.json")))

    assert [note.note_id for note in run.notes] == ["dated", "undated"]
    assert collector.detail_calls == ["dated", "undated"]
    funnel = collection["quality"]["collection_funnel"]
    assert funnel["undated_kept"] == 1
    assert funnel["removed_undated"] == 0
    # 对账：窗口内 + 保留的无日期 - 去重 - 采集器剔除 = 最终笔记数
    assert (
        funnel["kept_after_window_filter"]
        - funnel["removed_cross_keyword_duplicate"]
        - funnel["removed_by_collector"]
        == funnel["notes_after_collection"]
        == 2
    )


def test_broad_keyword_funnel_reconciles_with_final_note_count(tmp_path) -> None:
    inside = RUN_START - timedelta(days=1)
    old = RUN_START - timedelta(days=500)
    # shared 同时出现在两个关键词下，只能算一次 unique contribution。
    pages_by_keyword = {
        "港大商学院": [[_card("shared", inside), _card("only_a", inside)]],
        "HKUBS": [[_card("shared", inside), _card("stale", old)]],
    }
    collector = FakeCollector(pages_by_keyword, str(tmp_path))
    config = BroadScanConfig(
        keyword_pool=[
            KeywordConfig(keyword="港大商学院", max_pages=1, max_notes=20),
            KeywordConfig(keyword="HKUBS", max_pages=1, max_notes=20),
        ],
        scope_pattern=None,
        output_dir=str(tmp_path),
    )

    run = broad_collect(collector, config)
    collection = read_json(next(tmp_path.rglob("collection.json")))
    keyword_funnel = {row["keyword"]: row for row in collection["quality"]["keyword_funnel"]}
    funnel = collection["quality"]["collection_funnel"]

    assert keyword_funnel["港大商学院"]["pages_requested"] == 1
    assert keyword_funnel["港大商学院"]["raw_results"] == 2
    assert keyword_funnel["港大商学院"]["unique_posts_contributed"] == 2
    assert keyword_funnel["HKUBS"]["outside_reporting_window"] == 1
    # shared 已由第一个关键词贡献，这里不重复计入。
    assert keyword_funnel["HKUBS"]["unique_posts_contributed"] == 0

    # 对账：各关键词 unique 之和 == 最终笔记数；旧帖不在其中。
    assert sum(row["unique_posts_contributed"] for row in keyword_funnel.values()) == len(run.notes)
    assert funnel["removed_outside_window"] == 1
    assert funnel["removed_cross_keyword_duplicate"] == 1
    assert funnel["notes_after_collection"] == len(run.notes) == 2
    assert "stale" not in [note.note_id for note in run.notes]
    assert "stale" not in collector.detail_calls


def test_broad_funnel_survives_processing_and_stays_reconcilable(tmp_path) -> None:
    from xhs_listener.process import process_run

    inside = RUN_START - timedelta(days=1)
    old = RUN_START - timedelta(days=500)
    collector = FakeCollector(
        {"港大商学院": [[
            _card("keep1", inside, "港大商学院选课讨论"),
            _card("keep2", inside, "港大商学院住宿讨论"),
            _card("drop", old, "港大商学院两年前旧帖"),
        ]]},
        str(tmp_path),
    )
    config = BroadScanConfig(
        keyword_pool=[KeywordConfig(keyword="港大商学院", max_pages=1, max_notes=20)],
        scope_pattern=None,
        output_dir=str(tmp_path),
    )
    run = broad_collect(collector, config)

    report = process_run(run.run_dir)
    funnel = report["collection_funnel"]

    assert report["reporting_window"]["applied"] is True
    assert funnel["raw_search_results"] == 3
    assert funnel["removed_outside_window"] == 1
    assert funnel["notes_after_collection"] == 2
    assert funnel["notes_into_processing"] == 2
    assert funnel["notes_after_processing"] == report["processed_notes"] == 2
    assert funnel["removed_in_processing"] == 0
    assert report["keyword_funnel"][0]["keyword"] == "港大商学院"
    published = [row.get("published_at") for row in read_jsonl(run.run_dir + "/processed_notes.jsonl")]
    assert all(value for value in published)


def test_historical_runs_without_window_record_are_left_untouched(tmp_path) -> None:
    """老 run 的 collection.json 没有 reporting_window：process 不回溯过滤。"""

    from xhs_listener.io_utils import write_json, write_jsonl
    from xhs_listener.process import process_run

    old = RUN_START - timedelta(days=500)
    write_json(tmp_path / "collection.json", {"keyword": "broad_scan", "scan_mode": "broad_scan", "quality": {}})
    write_jsonl(
        tmp_path / "notes.jsonl",
        [
            {"note_id": "legacy_old", "title": "两年前的帖子", "body": "正文内容足够长可以通过噪声检查。",
             "published_at": str(int(old.timestamp())), "is_valid": True},
        ],
    )
    write_jsonl(tmp_path / "comments.jsonl", [])

    report = process_run(tmp_path)

    assert report["processed_notes"] == 1
    assert report["reporting_window"]["applied"] is False
    assert report["collection_funnel"]["notes_after_processing"] == 1


# --------------------------------------------------------------------------
# 5. Topic 样本量与页数对齐
# --------------------------------------------------------------------------


def _streamlit_constants() -> dict[str, Any]:
    """streamlit 是前端依赖，测试环境不一定装；用 AST 读常量即可。"""

    import ast
    from pathlib import Path

    source = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    wanted = {"SAMPLE_SIZE_OPTIONS", "DEFAULT_TARGET_POST_COUNT", "PAGE_SIZE"}
    found: dict[str, Any] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            name = node.targets[0].id
            if name in wanted:
                found[name] = ast.literal_eval(node.value)
    return found


def test_topic_sample_sizes_map_to_whole_tikhub_pages() -> None:
    import math

    constants = _streamlit_constants()
    options = constants["SAMPLE_SIZE_OPTIONS"]
    page_size = constants["PAGE_SIZE"]

    assert options == [20, 40, 60, 80]
    assert constants["DEFAULT_TARGET_POST_COUNT"] == 40
    assert constants["DEFAULT_TARGET_POST_COUNT"] in options
    for value in options:
        # 整页：不会为了多出的几条再买一整页（25 -> 2 页、50 -> 3 页 就属于浪费）。
        assert value % page_size == 0
        pages = max(1, math.ceil(value / page_size))
        assert pages == value // page_size
        assert pages * page_size == value
    for wasteful in (25, 30, 50):
        assert wasteful not in options
        assert max(1, math.ceil(wasteful / page_size)) * page_size > wasteful


def test_broad_page_budget_is_summed_per_keyword() -> None:
    from xhs_listener.run_manager import ManagedRunRequest, estimate_request_size

    from xhs_listener.weekly_insights import COMPETITOR_MAX_PAGES

    estimate = estimate_request_size(ManagedRunRequest(mode="broad_scan", broad_config={}))

    # HKUBS 14 页 + 7 家竞对 × 1 页 = 21 次搜索请求。
    assert COMPETITOR_MAX_PAGES == 1
    assert estimate["search_requests"] == 14 + 7 * COMPETITOR_MAX_PAGES
    assert estimate["keyword_count"] == 16
    assert estimate["max_notes"] == 14 * 20
    assert estimate["competitor_max_notes"] == 7 * COMPETITOR_MAX_PAGES * 20
    assert estimate["comment_pages"] == 1
    assert estimate["max_comments"] == 10 * 20


def test_keyword_pool_json_backfills_pages_from_legacy_max_notes() -> None:
    pool = parse_keyword_pool('[{"keyword":"港大商学院","max_notes":45},{"keyword":"HKUBS","max_pages":1}]')

    assert pool[0].max_pages == 3  # 45 条需要 3 页
    assert pool[0].max_notes == 45
    assert pool[1].max_pages == 1
    assert parse_keyword_pool(["港大商学院"])[0].max_pages == 1


# --------------------------------------------------------------------------
# 6. 漏斗算术
# --------------------------------------------------------------------------


def test_collection_funnel_arithmetic_reconciles() -> None:
    funnel = build_collection_funnel(
        search_pages_requested=14,
        raw_search_results=200,
        window_counts={WINDOW_INSIDE: 150, WINDOW_OUTSIDE: 40, WINDOW_UNDATED: 10},
        notes_after_collection=120,
        duplicates_removed=35,
    )

    assert funnel["search_pages_requested"] == 14
    assert funnel["kept_after_window_filter"] == 160
    assert funnel["removed_outside_window"] == 40
    assert funnel["undated_kept"] == 10
    assert funnel["removed_cross_keyword_duplicate"] == 35
    assert funnel["removed_by_collector"] == 5
    assert (
        funnel["kept_after_window_filter"]
        - funnel["removed_cross_keyword_duplicate"]
        - funnel["removed_by_collector"]
        == funnel["notes_after_collection"]
    )


def test_all_modes_share_one_reporting_window_definition() -> None:
    """Broad / Topic / 竞对必须共用同一个窗口常量，避免两套「本周」定义并存。"""

    from xhs_listener import models
    from xhs_listener.models import REPORTING_WINDOW_DAYS, BroadScanConfig, CollectConfig
    from xhs_listener.weekly_insights import COMPETITOR_WINDOW_DAYS

    assert REPORTING_WINDOW_DAYS == 8
    assert CollectConfig(keyword="x").reporting_window_days == REPORTING_WINDOW_DAYS
    assert BroadScanConfig().reporting_window_days == REPORTING_WINDOW_DAYS
    assert COMPETITOR_WINDOW_DAYS == REPORTING_WINDOW_DAYS
    # 拆分常量已删除：再出现就说明又分叉了。
    assert not hasattr(models, "BROAD_REPORTING_WINDOW_DAYS")
