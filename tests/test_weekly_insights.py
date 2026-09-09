from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from xhs_listener.collect import XiaohongshuCollector
from xhs_listener.io_utils import read_json, read_jsonl, write_json, write_jsonl
from xhs_listener.models import CollectionRun
from xhs_listener.report import build_report_html
from xhs_listener.weekly_insights import (
    COMPETITOR_DETAIL_FETCH_TOP_N,
    COMPETITOR_MAX_PAGES,
    COMPETITOR_SCHOOLS,
    COMPETITOR_TOP_POSTS,
    _validate_comment_analysis,
    _validate_competitor_analysis,
    aggregate_overall_sentiment,
    collect_competitor_weekly,
)


def test_comment_fetch_supports_hot_first_page_and_keeps_required_fields(tmp_path) -> None:
    class CommentCollector(XiaohongshuCollector):
        def __init__(self) -> None:
            super().__init__(api_token="test", output_root=tmp_path)
            self.params = []

        def _api_call(self, run, path, params):  # type: ignore[override]
            self.params.append(params)
            return {
                "data": {
                    "data": {
                        "comments": [
                            {
                                "comment_id": "c1",
                                "content": "今年什么时候发 offer",
                                "like_count": 18,
                                "parent_comment_id": None,
                            }
                        ],
                        "cursor": "",
                    }
                }
            }

    collector = CommentCollector()
    run = CollectionRun(keyword="top10", run_dir=str(tmp_path))
    rows = collector.fetch_comments(
        "n1",
        comment_pages=1,
        sub_comment_pages=0,
        run=run,
        sort_strategy="like_count",
    )

    assert len(collector.params) == 1
    assert collector.params[0]["sort_strategy"] == "like_count"
    assert [(row.comment_id, row.content, row.like_count, row.parent_comment_id) for row in rows] == [
        ("c1", "今年什么时候发 offer", 18, None)
    ]
    assert any("returned=1" in line for line in run.logs)


def test_comment_bundle_validation_requires_two_distinct_grounded_comments() -> None:
    comments = [
        {"comment_id": "c1", "content": "今年什么时候发 offer", "like_count": 18},
        {"comment_id": "c2", "content": "想问 offer 发放时间", "like_count": 4},
        {"comment_id": "c3", "content": "学费真的越来越贵了", "like_count": 126},
    ]
    payload = {
        "has_clear_signal": True,
        "audience_reaction": "申请者主要追问 offer 发放时间。",
        "recurring_signals": [
            {
                "signal": "Offer timeline uncertainty",
                "support_count": 999,
                "evidence": [
                    {"comment_id": "c1", "quote": "什么时候发 offer", "like_count": 999},
                    {"comment_id": "c2", "quote": "offer 发放时间", "like_count": 999},
                ],
            },
            {
                "signal": "Invalid single-comment signal",
                "support_count": 2,
                "evidence": [
                    {"comment_id": "c3", "quote": "不存在的原话", "like_count": 126},
                    {"comment_id": "missing", "quote": "假的", "like_count": 0},
                ],
            },
        ],
        "high_engagement_viewpoint": {
            "summary": "有用户关注学费上涨。",
            "comment_id": "c3",
            "quote": "学费真的越来越贵了",
            "like_count": 999,
        },
    }

    result = _validate_comment_analysis("n1", payload, comments)

    assert result["has_clear_signal"] is True
    assert len(result["recurring_signals"]) == 1
    assert result["recurring_signals"][0]["support_count"] == 2
    assert [row["like_count"] for row in result["recurring_signals"][0]["evidence"]] == [18, 4]
    assert result["high_engagement_viewpoint"]["like_count"] == 126


def test_comment_bundle_does_not_force_reaction_without_repeated_signal() -> None:
    comments = [{"comment_id": "c1", "content": "只有一个观点", "like_count": 100}]
    result = _validate_comment_analysis(
        "n1",
        {
            "has_clear_signal": True,
            "audience_reaction": "模型试图强行总结。",
            "recurring_signals": [
                {
                    "signal": "单条不算重复",
                    "support_count": 2,
                    "evidence": [{"comment_id": "c1", "quote": "只有一个观点", "like_count": 100}],
                }
            ],
        },
        comments,
    )

    assert result["has_clear_signal"] is False
    assert result["audience_reaction"] == ""
    assert result["recurring_signals"] == []


@pytest.mark.parametrize(
    ("sentiments", "expected"),
    [
        (["positive", "positive", "positive", "negative", "neutral"], "Mostly Positive"),
        (["neutral", "neutral", "neutral", "positive", "negative"], "Mostly Neutral"),
        (["negative", "negative", "negative", "positive", "neutral"], "Mostly Negative"),
        (["positive", "positive", "neutral", "neutral", "negative"], "Mixed"),
    ],
)
def test_competitor_overall_sentiment_uses_fixed_code_rule(sentiments, expected) -> None:
    assert aggregate_overall_sentiment(sentiments) == expected


def test_competitor_analysis_preserves_top_posts_and_computes_overall() -> None:
    top_posts = [
        {"rank": index, "note_id": f"n{index}", "title": f"Post {index}", "body": "body"}
        for index in range(1, 6)
    ]
    result = _validate_competitor_analysis(
        {"school": "CUHK Business School", "keyword": "港中文商学院", "top_posts": top_posts},
        {
            "posts": [
                {"note_id": f"n{index}", "sentiment": "positive" if index <= 3 else "neutral"}
                for index in range(1, 6)
            ],
            "weekly_takeaway": "Top posts focused on admissions and campus activities.",
        },
    )

    assert result["overall_sentiment"] == "Mostly Positive"
    assert [row["note_id"] for row in result["posts"]] == [f"n{index}" for index in range(1, 6)]


def test_competitor_analysis_rejects_missing_post_sentiment() -> None:
    with pytest.raises(RuntimeError, match="missing valid sentiment"):
        _validate_competitor_analysis(
            {
                "school": "CUHK Business School",
                "keyword": "港中文商学院",
                "top_posts": [{"rank": 1, "note_id": "n1", "title": "Post", "body": "body"}],
            },
            {"posts": [], "weekly_takeaway": ""},
        )


def test_competitor_max_pages_is_pinned_to_one_page_per_school() -> None:
    """产品决定：每所学校只搜 1 页（20 条），不再搜 2 页——省下一半的详情请求成本。"""

    assert COMPETITOR_MAX_PAGES == 1


def test_competitor_collection_uses_seven_keywords_one_page_and_hard_weekly_filter(tmp_path) -> None:
    run_started_at = datetime.now()

    class CompetitorCollector(XiaohongshuCollector):
        def __init__(self) -> None:
            super().__init__(api_token="test", output_root=tmp_path)
            self.search_calls = []

        def _api_call(self, run, path, params):  # type: ignore[override]
            keyword = str(params.get("keyword") or "")
            page = int(params.get("page") or 1)
            self.search_calls.append((keyword, page, params.get("time_filter")))
            items = [
                _competitor_card(f"{keyword}-{index}", run_started_at - timedelta(days=1), likes=10 - index)
                for index in range(5)
            ]
            items.extend(
                [
                    _competitor_card(f"{keyword}-old", run_started_at - timedelta(days=8), likes=999),
                    _competitor_card(f"{keyword}-undated", None, likes=999),
                ]
            )
            return {"data": {"items": items}}

        def fetch_image_detail(self, note_id, xsec_token, run):  # type: ignore[override]
            return {}

    collector = CompetitorCollector()
    output = collect_competitor_weekly(tmp_path, collector)

    assert len(collector.search_calls) == len(COMPETITOR_SCHOOLS) * COMPETITOR_MAX_PAGES
    assert all(time_filter == "一周内" for _, _, time_filter in collector.search_calls)
    assert len(output["schools"]) == 7
    assert all(row["pages_requested"] == COMPETITOR_MAX_PAGES for row in output["schools"])
    assert all(row["pages_received"] == COMPETITOR_MAX_PAGES for row in output["schools"])
    assert all(row["outside_window_removed"] == 1 for row in output["schools"])
    assert all(row["undated_removed"] == 1 for row in output["schools"])
    assert all(len(row["top_posts"]) == 5 for row in output["schools"])
    # 窗口内 5 条候选按互动量排序，index 0（likes=10）互动量最高，排第一。
    assert all(row["top_posts"][0]["note_id"].endswith("-0") for row in output["schools"])
    assert len(read_jsonl(tmp_path / "competitor_notes.jsonl")) == 7 * 5
    assert len(read_json(tmp_path / "raw" / "competitor_search_pages.json")["pages"]) == 7 * COMPETITOR_MAX_PAGES
    assert len(read_jsonl(tmp_path / "competitor_notes.partial.jsonl")) == 7 * 5
    assert len(read_json(tmp_path / "raw" / "competitor_search_pages.partial.json")["pages"]) == 7 * COMPETITOR_MAX_PAGES
    partial = read_json(tmp_path / "competitor_collection.partial.json")
    assert partial["partial"] is True
    assert len(partial["schools"]) == 7


def test_competitor_collection_only_fetches_detail_for_top_n_by_card_engagement(tmp_path) -> None:
    """核心省钱点：窗口内候选先按搜索卡片互动量排序，只对 Top N 抓详情；

    落选的候选仍然落盘（进 competitor_notes.jsonl），只是没有详情字段。
    """

    run_started_at = datetime.now()
    candidate_count = 12
    assert candidate_count > COMPETITOR_DETAIL_FETCH_TOP_N

    class CompetitorCollector(XiaohongshuCollector):
        def __init__(self) -> None:
            super().__init__(api_token="test", output_root=tmp_path)
            self.detail_calls: list[str] = []

        def _api_call(self, run, path, params):  # type: ignore[override]
            keyword = str(params.get("keyword") or "")
            # index 0 拿最高点赞，往后递减——互动量排序应该是确定的。
            items = [
                _competitor_card(
                    f"{keyword}-{index}",
                    run_started_at - timedelta(days=1),
                    likes=candidate_count - index,
                )
                for index in range(candidate_count)
            ]
            return {"data": {"items": items}}

        def fetch_image_detail(self, note_id, xsec_token, run):  # type: ignore[override]
            self.detail_calls.append(note_id)
            return {}

    collector = CompetitorCollector()
    output = collect_competitor_weekly(tmp_path, collector)

    school_row = output["schools"][0]
    keyword = school_row["keyword"]
    expected_detail_ids = {f"{keyword}-{index}" for index in range(COMPETITOR_DETAIL_FETCH_TOP_N)}

    # 每所学校只对互动量最高的 Top N 候选抓详情，不是全部 12 条。
    school_detail_calls = [note_id for note_id in collector.detail_calls if note_id.startswith(f"{keyword}-")]
    assert set(school_detail_calls) == expected_detail_ids
    assert len(school_detail_calls) == COMPETITOR_DETAIL_FETCH_TOP_N

    assert school_row["unique_weekly_candidates"] == candidate_count
    assert school_row["detail_fetched_candidates"] == COMPETITOR_DETAIL_FETCH_TOP_N
    assert len(school_row["top_posts"]) == COMPETITOR_TOP_POSTS
    assert {row["note_id"] for row in school_row["top_posts"]} <= expected_detail_ids

    # 落选的候选（互动量排在 Top N 之外）仍然落盘，只是没有详情字段。
    school_notes = [row for row in read_jsonl(tmp_path / "competitor_notes.jsonl") if row.get("school") == school_row["school"]]
    assert len(school_notes) == candidate_count
    fetched_flags = {row["note_id"]: row.get("detail_fetched") for row in school_notes}
    assert all(fetched_flags[note_id] is True for note_id in expected_detail_ids)
    not_fetched_ids = {row["note_id"] for row in school_notes} - expected_detail_ids
    assert not_fetched_ids  # 确实存在没抓详情、只留搜索卡片数据的候选
    assert all(fetched_flags[note_id] is False for note_id in not_fetched_ids)


def test_competitor_collection_allows_empty_school_when_search_pages_fail(tmp_path) -> None:
    run_started_at = datetime.now()
    empty_keyword = COMPETITOR_SCHOOLS[0][1]

    class CompetitorCollector(XiaohongshuCollector):
        def __init__(self) -> None:
            super().__init__(api_token="test", output_root=tmp_path)

        def _api_call(self, run, path, params):  # type: ignore[override]
            keyword = str(params.get("keyword") or "")
            if keyword == empty_keyword:
                raise RuntimeError("upstream unavailable")
            page = int(params.get("page") or 1)
            return {
                "data": {
                    "items": [
                        _competitor_card(
                            f"{keyword}-{page}",
                            run_started_at - timedelta(days=1),
                            likes=page,
                        )
                    ]
                }
            }

        def fetch_image_detail(self, note_id, xsec_token, run):  # type: ignore[override]
            return {}

    output = collect_competitor_weekly(tmp_path, CompetitorCollector())

    empty_school = output["schools"][0]
    assert empty_school["school"] == COMPETITOR_SCHOOLS[0][0]
    assert empty_school["pages_requested"] == COMPETITOR_MAX_PAGES
    assert empty_school["pages_received"] == 0
    assert empty_school["top_posts"] == []
    assert "received 0" in empty_school["collection_warning"]
    assert len(output["schools"]) == len(COMPETITOR_SCHOOLS)
    assert len(read_json(tmp_path / "raw" / "competitor_search_pages.json")["pages"]) == (len(COMPETITOR_SCHOOLS) - 1) * COMPETITOR_MAX_PAGES


def test_competitor_collection_resumes_from_partial_without_recollecting_completed_school(tmp_path) -> None:
    run_started_at = datetime.now()
    first_school, first_keyword = COMPETITOR_SCHOOLS[0]
    window_start = run_started_at - timedelta(days=8)
    completed_row = {
        "school": first_school,
        "keyword": first_keyword,
        "pages_requested": 2,
        "pages_received": 2,
        "collection_warning": "",
        "raw_candidates": 2,
        "outside_window_removed": 0,
        "undated_removed": 0,
        "unique_weekly_candidates": 1,
        "top_posts": [
            {
                "rank": 1,
                "note_id": "cached-note",
                "title": "Cached title",
                "body": "Cached body",
                "author": "cached author",
                "like_count": 99,
                "comment_count": 1,
                "share_count": 0,
                "collect_count": 0,
                "engagement_score": 102,
                "post_url": "https://www.xiaohongshu.com/explore/cached-note",
            }
        ],
    }
    expected = [{"school": school, "keyword": keyword} for school, keyword in COMPETITOR_SCHOOLS]
    write_json(
        tmp_path / "competitor_collection.partial.json",
        {
            "schema_version": "competitor_weekly_v1",
            "partial": True,
            "run_started_at": run_started_at.isoformat(timespec="seconds"),
            "window_start": window_start.isoformat(timespec="seconds"),
            "window_end": run_started_at.isoformat(timespec="seconds"),
            "schools_config": expected,
            "schools": [completed_row],
            "api_request_count": 42,
            "errors": [],
            "logs": [],
        },
    )
    write_json(tmp_path / "raw" / "competitor_search_pages.partial.json", {"pages": []})
    write_jsonl(tmp_path / "competitor_notes.partial.jsonl", [])
    write_json(
        tmp_path / "collection.json",
        {
            "quality": {
                "api_request_count": 0,
                "api_unit_cost_usd": 0.01,
                "api_estimated_cost_usd": 0,
            }
        },
    )

    class CompetitorCollector(XiaohongshuCollector):
        def __init__(self) -> None:
            super().__init__(api_token="test", output_root=tmp_path)
            self.search_calls: list[str] = []

        def _api_call(self, run, path, params):  # type: ignore[override]
            keyword = str(params.get("keyword") or "")
            self.search_calls.append(keyword)
            if keyword == first_keyword:
                raise AssertionError("completed school should be reused from partial")
            return {
                "data": {
                    "items": [
                        _competitor_card(
                            f"{keyword}-{params.get('page')}",
                            run_started_at - timedelta(days=1),
                            likes=10,
                        )
                    ]
                }
            }

        def fetch_image_detail(self, note_id, xsec_token, run):  # type: ignore[override]
            return {}

    collector = CompetitorCollector()
    output = collect_competitor_weekly(tmp_path, collector)

    assert output["schools"][0]["top_posts"][0]["note_id"] == "cached-note"
    assert first_keyword not in collector.search_calls
    assert len(output["schools"]) == len(COMPETITOR_SCHOOLS)


def _competitor_card(note_id: str, published_at: datetime | None, *, likes: int) -> dict:
    note = {
        "note_id": note_id,
        "title": f"Title {note_id}",
        "desc": "Body text for competitor post.",
        "liked_count": likes,
        "comments_count": 1,
        "shared_count": 1,
    }
    if published_at is not None:
        note["timestamp"] = str(int(published_at.timestamp()))
    return {"note": note}


def test_broad_report_renders_top10_audience_signals_and_competitors_in_order() -> None:
    structured = {
        "title": "Broad Report",
        "report_mode": "broad_report",
        "generated_scope": {
            "analysis_notes": 1,
            "analysis_comments": 0,
            "top10_comments": 3,
            "earliest_post_date": "2026-08-20",
            "latest_post_date": "2026-08-26",
            "comment_collection_status": "not_requested",
        },
        "header_distributions": {"sentiment": {"positive": 1}, "content_type": {"question": 1}},
        "executive_summary": "摘要",
        "key_findings_across_themes": [],
        "top_original_posts": [
            {
                "rank": 1,
                "note_id": "n1",
                "post_title": "Offer 时间讨论",
                "author": "user",
                "sentiment": "neutral",
                "like_count": 10,
                "comment_count": 3,
                "share_count": 1,
                "audience_reaction": "申请者主要追问 offer 发放时间。",
                "recurring_signals": [{"signal": "Offer timeline uncertainty", "support_count": 2}],
                "high_engagement_viewpoint": {
                    "summary": "有用户希望官方明确时间线。",
                    "quote": "什么时候发 offer",
                    "like_count": 18,
                },
            }
        ],
        "alerts": [{"signal": "风险信号", "alert_level": "medium", "alert_type": "information_gap", "summary": "概述", "evidence": "证据"}],
        "positive_reputation_signals": [{"signal": "正面信号", "summary": "概述", "evidence": "证据"}],
        "theme_landscape": [{"theme": "Admissions", "volume": 1, "engagement_sum": 10, "summary": "招生"}],
        "competitor_weekly": [
            {
                "school": "Empty Business School",
                "overall_sentiment": "Mixed",
                "weekly_takeaway": "",
                "posts": [],
            },
            {
                "school": "CUHK Business School",
                "overall_sentiment": "Mostly Positive",
                "weekly_takeaway": "Top posts focused on admissions.",
                "posts": [
                    {
                        "rank": 1,
                        "note_id": "c1",
                        "title": "Competitor post",
                        "sentiment": "positive",
                        "like_count": 20,
                        "comment_count": 2,
                        "share_count": 1,
                    }
                ],
            }
        ],
        "appendix": {"evidence": [], "methodology": []},
        "data_limitations": [],
    }
    bundle = {"report_mode": "broad_report"}
    processing = {"scan_mode": "broad_scan"}

    html = build_report_html(structured, bundle, processing, "2026-08-26 12:00:00")

    assert "申请者主要追问 offer 发放时间" in html
    assert "Offer timeline uncertainty" in html
    assert "Empty Business School" not in html
    assert "High-engagement viewpoint" not in html
    assert "高互动观点" in html
    assert html.index("Top 10 原帖") < html.index("重点关注")
    assert html.index("重点关注") < html.index("正面与声誉信号")
    assert html.index("正面与声誉信号") < html.index("话题分布")
    assert html.index("话题分布") < html.index("竞对本周 Top 5")
    assert html.index("竞对本周 Top 5") < html.index("附录")


def test_top10_cards_show_readable_dates_not_raw_timestamps(tmp_path) -> None:
    """TikHub 的 published_at 常是 unix 时间戳，Top 10 卡片必须渲染成可读日期。"""

    from xhs_listener.io_utils import write_jsonl
    from xhs_listener.report import _broad_top_posts_from_run, _readable_post_date

    published = datetime(2026, 8, 21, 9, 30, 0)
    write_jsonl(
        tmp_path / "processed_notes.jsonl",
        [
            {
                "note_id": "n1",
                "title": "港大商学院讨论",
                "body": "正文内容足够长可以通过噪声检查。",
                "published_at": str(int(published.timestamp())),
                "like_count": 10,
                "comment_count": 3,
                "post_url": "https://www.xiaohongshu.com/explore/n1",
            }
        ],
    )
    write_jsonl(tmp_path / "annotations.jsonl", [{"note_id": "n1", "hku_relevance": "direct", "sentiment": "neutral"}])

    rows = _broad_top_posts_from_run(tmp_path)

    assert rows[0]["published_at"] == "2026-08-21"
    assert not rows[0]["published_at"].isdigit()
    # 毫秒级时间戳与 ISO 字符串同样能读；解析不了就保留原值，不猜也不留空。
    assert _readable_post_date(str(int(published.timestamp() * 1000))) == "2026-08-21"
    assert _readable_post_date("2026-08-21T09:30:00") == "2026-08-21"
    assert _readable_post_date("不是日期") == "不是日期"
    assert _readable_post_date(None) == ""


def test_data_limitations_describe_top10_only_comment_scope() -> None:
    """Broad 的评论现在只覆盖 Top 10，数据限制不能再写「未请求评论采集」。"""

    from xhs_listener.report import _monitoring_limitations

    processing = {"comment_collection_status": "not_requested"}

    without_top10 = _monitoring_limitations([], {"_top10_comment_collection": {}}, processing)
    with_top10 = _monitoring_limitations(
        [], {"_top10_comment_collection": {"comments_collected": 47}}, processing
    )

    assert any("本轮未请求评论采集" in item for item in without_top10)
    assert not any("本轮未请求评论采集" in item for item in with_top10)
    scoped = [item for item in with_top10 if "Top 10" in item]
    assert scoped, with_top10
    assert "47" in scoped[0]
    # 必须说清楚它不覆盖全样本、也不进入重点关注/正面信号聚合。
    assert "其余帖子未采集评论" in scoped[0]
    assert "不参与重点关注与正面信号的聚合" in scoped[0]


def test_new_pipeline_steps_do_not_regress_the_ui_progress_strip() -> None:
    """top10_comments / competitors 跑在 analyze 之后，进度条不能倒退回“正在搜索”。"""

    import ast
    from pathlib import Path

    from xhs_listener.run_manager import BROAD_ANALYSIS_STEPS

    source = Path(__file__).resolve().parents[1] / "streamlit_app.py"
    tree = ast.parse(source.read_text(encoding="utf-8"))
    target = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_resolve_ui_phase"
    )
    mapped = {
        node.value
        for node in ast.walk(target)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }

    for step in BROAD_ANALYSIS_STEPS:
        assert step in mapped, f"{step} 没有出现在 _resolve_ui_phase 里，会掉进 searching 兜底"
    assert "top10_comments" in mapped
    assert "competitors" in mapped
