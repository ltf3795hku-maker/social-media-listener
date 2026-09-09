from __future__ import annotations

from pathlib import Path

from xhs_listener.collect import XiaohongshuCollector
from xhs_listener.collect import _comment_skip_reasons, _extract_comment_paging
from xhs_listener.analyze import (
    ANNOTATION_SCHEMA_VERSION,
    _annotation_summary,
    _annotation_batch_size,
    _annotation_relevance_rules,
    _build_annotation_records,
    _code_hku_relevance,
    _compact_relevance_record,
    _engagement_score,
    _merge_usage_rows,
    _normalize_annotation,
    _normalize_relevance,
    _cached_annotation_has_current_schema,
    _hkubs_broad_relevance,
    _relevance_batch_size,
    run_topic_analysis,
    run_broad_analysis,
    _validate_annotation_batch,
    _validate_annotation_evidence_quotes,
    build_signal_table,
    build_comment_signal_table,
    build_narrative_comment_table,
    build_discussion_table,
    build_uncertainty_table,
)
from xhs_listener.analysis_prompts import COMMON_ANALYSIS_RULES, build_topic_analysis_prompt
from xhs_listener.broad_scan import parse_keyword_pool
from xhs_listener.io_utils import write_json, write_jsonl
from xhs_listener.models import BroadScanConfig, CollectConfig, CollectionRun, Note
from xhs_listener.number_utils import to_int
from xhs_listener.process import process_run
from xhs_listener.report import (
    _broad_top_posts_from_run,
    _ensure_report_defaults,
    _monitoring_evidence,
    build_report_html,
)


def test_monitoring_evidence_recovers_post_link_from_source_note_id() -> None:
    note_id = "6a39fbea000000001702d84e"
    rows = _monitoring_evidence(
        [{"source": f"note_id: {note_id}", "evidence": "原文"}],
        {"_note_post_urls": {note_id: "https://example.com/post"}},
    )

    assert rows[0]["source"] == "原帖"
    assert rows[0]["note_id"] == note_id
    assert rows[0]["post_url"] == "https://example.com/post"
from xhs_listener.run_manager import ManagedRunRequest, RunManager, RunStore
from xhs_listener.tikhub import APP_SEARCH, APP_V2_IMAGE_DETAIL, APP_V2_SEARCH, WEB_V3_SEARCH


def test_default_config_is_image_note_focused() -> None:
    config = CollectConfig(keyword="港大")

    assert config.note_type == "普通笔记"
    assert config.comment_pages == 0
    assert config.sub_comment_pages == 0
    assert config.comment_min_likes == 500
    assert config.comment_min_comments == 0


def test_search_fallback_order() -> None:
    collector = XiaohongshuCollector(api_token="token")
    plan = collector._search_plan(
        CollectConfig(keyword="港大"),
        page=2,
        app_v2_search_id="sid-v2",
        app_v2_session_id="ssid-v2",
        app_search_id="sid-app",
        app_session_id="ssid-app",
    )

    assert [item["path"] for item in plan] == [APP_V2_SEARCH, APP_SEARCH, WEB_V3_SEARCH]
    assert plan[0]["params"]["search_session_id"] == "ssid-v2"


def test_image_detail_endpoint_is_new_app_v2() -> None:
    assert APP_V2_IMAGE_DETAIL == "/api/v1/xiaohongshu/app_v2/get_image_note_detail"


def test_comment_paging_decodes_json_cursor() -> None:
    paging = _extract_comment_paging(
        {"cursor": '{"cursor":"6a3939e8000000000b038f83","index":2,"pageArea":"UNFOLDED"}'},
        default_index=0,
    )

    assert paging == {"cursor": "6a3939e8000000000b038f83", "index": 2, "page_area": "UNFOLDED"}


def test_fetch_comments_keeps_only_direct_comments_from_top_level_comments() -> None:
    collector = XiaohongshuCollector(api_token="token")
    rows = collector._extract_comment_rows(
        "6a38f1b4000000000f016dab",
        {
            "comments": [
                {
                    "id": "direct-1",
                    "content": "一级评论",
                    "subComments": [{"id": "reply-1", "content": "楼中楼回复"}],
                }
            ]
        },
        direct_only=True,
    )

    assert [row.comment_id for row in rows] == ["direct-1"]
    assert rows[0].parent_comment_id is None


def test_app_v2_timestamp_fields_are_extracted() -> None:
    collector = XiaohongshuCollector(api_token="token")
    note = collector._build_note(
        {
            "note": {
                "id": "abc123",
                "title": "港大商学院",
                "desc": "正文",
                "timestamp": 1717200000,
                "user": {"id": "u1", "nickname": "作者"},
            }
        },
        keyword="港大",
    )

    assert note is not None
    assert note.published_at == "1717200000"
    assert note.published_at_raw == "1717200000"

    collector._merge_detail(note, {"data": {"items": [{"update_time": 1717286400, "desc": "更完整正文"}]}})

    assert note.published_at == "1717286400"
    assert note.published_at_raw == "1717286400"


def test_process_run_keeps_scope_hints_for_analysis_gate(tmp_path: Path) -> None:
    write_json(tmp_path / "collection.json", {"keyword": "港大"})
    write_jsonl(
        tmp_path / "notes.jsonl",
        [
            {"note_id": "1", "title": "港大", "body": "正文", "is_valid": True, "is_scope_relevant": True},
            {"note_id": "2", "title": "其他", "body": "正文", "is_valid": True, "is_scope_relevant": False},
        ],
    )
    write_jsonl(
        tmp_path / "comments.jsonl",
        [
            {"note_id": "1", "comment_id": "c1", "content": "有用"},
            {"note_id": "2", "comment_id": "c2", "content": "仍交给分析阶段判断"},
        ],
    )

    report = process_run(tmp_path)

    assert report["processed_notes"] == 2
    assert report["processed_comments"] == 2
    assert "collector_out_of_scope" not in report["removed_note_reasons"]


def test_process_run_keeps_only_direct_lightweight_comments(tmp_path: Path) -> None:
    write_json(tmp_path / "collection.json", {"keyword": "港大"})
    write_jsonl(tmp_path / "notes.jsonl", [{"note_id": "1", "title": "港大", "body": "正文"}])
    write_jsonl(
        tmp_path / "comments.jsonl",
        [
            {"note_id": "1", "comment_id": "c1", "content": "一级评论", "user_name": "用户", "raw": {"x": 1}, "like_count": "3"},
            {"note_id": "1", "comment_id": "c2", "content": "作者回复", "parent_comment_id": "c1", "like_count": 1},
        ],
    )

    report = process_run(tmp_path)
    rows = [__import__("json").loads(line) for line in (tmp_path / "processed_comments.jsonl").read_text(encoding="utf-8").splitlines()]

    assert report["processed_comments"] == 1
    assert report["removed_comment_reasons"]["non_direct_comment"] == 1
    assert rows == [{"note_id": "1", "comment_id": "c1", "content": "一级评论", "like_count": 3}]


def test_collection_quality_counts_comment_skip_reasons() -> None:
    run = CollectionRun(keyword="港大", run_dir="tmp")
    run.notes = [
        Note(note_id="1", keyword="港大", body="港大正文", like_count=800),
        Note(note_id="2", keyword="港大", body="港大正文", like_count=100),
        Note(note_id="3", keyword="港大", body="无关正文", like_count=900, is_scope_relevant=False),
    ]

    quality = XiaohongshuCollector._collection_quality(
        run,
        search_pages=[],
        config=CollectConfig(keyword="港大", comment_pages=1, comment_min_likes=500),
    )

    assert quality["comment_eligible_notes"] == 1
    assert quality["comments_skipped_before_fetch"] == 2
    assert quality["comments_skipped_by_like_threshold"] == 1
    assert quality["comments_skipped_by_reason"]["out_of_scope"] == 1


def test_comment_fetch_can_be_gated_by_comment_count() -> None:
    note = Note(note_id="1", keyword="港大", body="港大正文", like_count=800, comment_count=3)

    reasons = _comment_skip_reasons(note, CollectConfig(keyword="港大", comment_min_likes=0, comment_min_comments=10))

    assert reasons == ["below_comment_min_comments"]


def test_xhs_count_text_parses_wan_units() -> None:
    assert to_int("1,234") == 1234
    assert to_int("1万") == 10000
    assert to_int("1.2万") == 12000


def test_scope_matching_uses_tags_from_raw_payload() -> None:
    note = Note(
        note_id="1",
        keyword="港大",
        title="BA申请经验",
        body="正文很正常但没有学校名",
        raw={"detail": {"tag_list": [{"name": "港大"}]}},
    )

    XiaohongshuCollector(api_token="token")._tag_note_for_collection(
        note,
        CollectConfig(keyword="港大"),
        seen_note_ids=set(),
    )

    assert note.is_scope_relevant is True
    assert "out_of_scope" not in note.skip_reasons


def test_build_annotation_records_excludes_comment_samples() -> None:
    records = _build_annotation_records(
        [{"note_id": "1", "title": "港大", "content_full": "港大正文", "like_count": 10}],
        [{"note_id": "1", "content": "评论A"}, {"note_id": "1", "content": "评论B"}],
    )

    assert records[0]["note_id"] == "1"
    assert "comment_sample" not in records[0]


def test_compact_relevance_record_drops_heavy_annotation_fields() -> None:
    compact = _compact_relevance_record(
        {
            "note_id": "n1",
            "keyword": "港大商学院学制",
            "author_name": "作者",
            "title": "题" * 200,
            "text": "正文" * 600,
            "tags": [str(index) for index in range(20)],
            "comment_sample": "评论" * 500,
            "engagement": 999,
            "code_relevance": {"hku_relevance": "direct"},
        }
    )

    assert set(compact) == {"note_id", "title", "text", "tags", "code_relevance"}
    assert len(compact["title"]) == 160
    assert " ... " in compact["text"]
    assert compact["text"].startswith("正文正文")
    assert compact["text"].endswith("正文正文")
    assert len(compact["tags"]) == 10


def test_topic_relevance_prompt_uses_search_intent_not_literal_phrase_only() -> None:
    rules = _annotation_relevance_rules({"scan_mode": "topic_scan", "keyword": "港大商学院避雷"})

    assert "搜索意图" in rules
    assert "逐字匹配完整短语" in rules
    assert "同义、近义、反向、经验性表达" in rules
    assert "只沾到学校名的泛内容" in rules


def test_annotation_summary_counts_fields() -> None:
    summary = _annotation_summary(
        [
            {
                "hku_relevance": "direct",
                "topic_relevance": "direct",
                "sentiment": "positive",
                "content_type": "information_sharing",
                "signal_types": ["informational", "emotional"],
            },
            {
                "hku_relevance": "direct",
                "topic_relevance": "indirect",
                "sentiment": "neutral",
                "content_type": "question",
                "signal_type": "operational",
            },
        ]
    )

    assert summary["hku_relevance"]["direct"] == 2
    assert summary["topic_relevance"]["indirect"] == 1
    assert summary["content_type"]["information_sharing"] == 1
    assert summary["signal_type"]["informational"] == 1
    assert summary["signal_type"]["emotional"] == 1


def test_validate_annotation_batch_fills_missing_and_drops_bad_ids() -> None:
    logs: list[str] = []
    batch = [{"note_id": "1", "title": "A"}, {"note_id": "2", "title": "B"}]
    payload = [
        {"note_id": "1", "hku_relevance": "direct", "topic_relevance": "direct", "sentiment": "positive"},
        {"note_id": "1", "hku_relevance": "unrelated", "topic_relevance": "unrelated"},
        {"note_id": "999", "hku_relevance": "direct", "topic_relevance": "direct"},
    ]

    rows = _validate_annotation_batch(batch, payload, "topic_scan", logs, None, batch_no=1)

    assert [row["note_id"] for row in rows] == ["1", "2"]
    assert rows[0]["hku_relevance"] == "direct"
    assert rows[0]["topic_relevance"] == "direct"
    assert rows[1]["fallback"] is True
    assert rows[1]["hku_relevance"] == "unrelated"
    assert rows[1]["topic_relevance"] == "unrelated"
    assert "id_mismatch" in logs[0]


def test_analysis_batch_defaults_match_scan_modes(monkeypatch) -> None:
    monkeypatch.delenv("XHS_RELEVANCE_BATCH_SIZE", raising=False)
    monkeypatch.delenv("XHS_ANNOTATION_BATCH_SIZE", raising=False)

    assert _relevance_batch_size("topic_scan") == 5
    assert _annotation_batch_size("topic_scan") == 3
    assert _relevance_batch_size("broad_scan") == 15
    assert _annotation_batch_size("broad_scan") == 6


def test_llm_usage_rows_are_merged(tmp_path: Path) -> None:
    usage_path = tmp_path / "llm_usage.json"
    write_json(usage_path, {"usage": [{"phase": "relevance_gate", "total_tokens": 10}]})

    merged = _merge_usage_rows(usage_path, [{"phase": "analysis", "total_tokens": 20}])

    assert [row["phase"] for row in merged] == ["relevance_gate", "analysis"]
    assert sum(row["total_tokens"] for row in merged) == 30


def test_run_analysis_updates_stored_budget(tmp_path: Path) -> None:
    manager = RunManager(RunStore(tmp_path / "runs.sqlite3"))
    run = manager.store.create_run(ManagedRunRequest(mode="topic_scan", budget_tokens=100))
    manager.store.update_run(run["id"], run_dir=str(tmp_path), status="collected")

    try:
        manager.run_analysis(run["id"], ManagedRunRequest(mode="topic_scan", budget_tokens=200), step_overrides={"process": lambda context: None, "analyze": lambda context: None, "report": lambda context: None})
    except RuntimeError:
        pass

    assert manager.store.get_run(run["id"])["budget_tokens"] == 200


def test_engagement_score_uses_wan_unit_parser() -> None:
    score = _engagement_score({"like_count": "1.2万", "collect_count": "10", "comment_count": "3", "share_count": "1"})

    assert score == 12030


def test_build_signal_table_counts_mentions_and_engagement() -> None:
    notes = [
        {"note_id": "1", "like_count": 10, "collect_count": 5, "comment_count": 2, "share_count": 1},
        {"note_id": "2", "like_count": 20, "collect_count": 1, "comment_count": 0, "share_count": 0},
    ]
    annotations = [
        {"note_id": "1", "signal_label": "排队抢课攻略", "signal_types": ["informational", "operational"], "evidence_quote": "提前排队"},
        {"note_id": "2", "signal_label": "排队抢课攻略", "signal_type": "informational", "evidence_quote": "多浏览器"},
    ]

    table = build_signal_table(notes, annotations)

    # 没有 label_map 时保留标注原 label，不再用手写关键词规则改名。
    assert table[0]["signal"] == "排队抢课攻略"
    assert table[0]["signal_types"] == ["informational", "operational"]
    assert table[0]["signal_type"] == "informational/operational"
    assert table[0]["mention_count"] == 2
    assert table[0]["engagement_sum"] == 49
    assert table[0]["evidence_note_ids"] == ["1", "2"]


def test_build_comment_signal_table_counts_questions_and_top_comments() -> None:
    table = build_comment_signal_table(
        [
            {"note_id": "1", "content": "请问怎么选课？", "like_count": 5},
            {"note_id": "1", "content": "这个攻略很有用", "like_count": 12},
            {"note_id": "2", "content": "mark", "like_count": 1},
        ]
    )

    assert table[0]["note_id"] == "1"
    assert table[0]["comment_count"] == 2
    assert table[0]["like_sum"] == 17
    assert table[0]["question_count"] == 1
    assert table[0]["top_comments"][0]["content"] == "这个攻略很有用"


def test_build_narrative_comment_table_groups_direct_comments_by_topic_narrative() -> None:
    table = build_narrative_comment_table(
        [
            {"note_id": "1", "content": "请问一年能毕业吗？", "like_count": 5},
            {"note_id": "1", "content": "学费不变这个信息有用", "like_count": 12},
            {"note_id": "1", "content": "楼中楼不该进入", "parent_comment_id": "c1", "like_count": 99},
            {"note_id": "2", "content": "太贵了", "like_count": 2},
        ],
        notes=[{"note_id": "1"}, {"note_id": "2"}],
        annotations=[
            {"note_id": "1", "primary_narrative": "弹性两年制"},
            {"note_id": "2", "primary_narrative": "学费上涨"},
        ],
        scan_mode="topic_scan",
        narrative_map={"弹性两年制": "弹性一年或两年制"},
    )

    assert table[0]["narrative"] == "弹性一年或两年制"
    assert table[0]["comment_count"] == 2
    assert table[0]["question_count"] == 1
    assert table[0]["top_comments"][0]["content"] == "学费不变这个信息有用"


def test_uncertainty_table_includes_comment_questions() -> None:
    table = build_uncertainty_table(
        notes=[{"note_id": "1", "like_count": 10, "post_url": "https://example.com/n1"}],
        annotations=[
            {
                "note_id": "1",
                "has_uncertainty": True,
                "uncertainty_type": "成本",
                "uncertainty_text": "学费是否会上涨",
                "evidence_quote": "想问学费会不会变",
            }
        ],
        narrative_comment_table=[
            {
                "narrative": "学费上涨",
                "question_comments": [
                    {"note_id": "1", "content": "请问涨多少？", "like_count": 7},
                ],
            }
        ],
    )

    # 分类直接采用 LLM 给的 uncertainty_type，报告/聚合层不再做语义归并。
    assert {row["uncertainty_type"] for row in table} == {"学费上涨", "成本"}
    assert len(table) == 2
    assert sum(row["comment_question_count"] for row in table) == 1
    assert any(row["evidence_items"][0]["source"] == "comment" for row in table)


def test_report_locale_controls_fixed_labels_and_hides_bare_urls() -> None:
    structured = {
        "title": "测试报告",
        "report_mode": "broad_report",
        "generated_scope": {"search_query": "HKUBS", "analysis_notes": 2, "analysis_comments": 0},
        "header_distributions": {"sentiment": {"positive": 1, "negative": 1}, "content_type": {"question": 2}, "author_type": {"unclear": 2}},
        "executive_summary": "测试摘要",
        "theme_landscape": [{"theme": "Admissions", "summary": "申请讨论", "volume": 2, "engagement_sum": 3}],
        "content_type_sentiment_summary": {},
        "alerts": [],
        "positive_reputation_signals": [],
        "top_original_posts": [
            {
                "rank": 1,
                "note_id": "n1",
                "published_at": "2026-06-23",
                "post_title": "申请经验",
                "author": "学生A",
                "comment_count": 2,
                "sentiment": "positive",
                "excerpt": "申请过程分享",
                "post_url": "https://example.com/post",
            }
        ],
        "appendix": {"evidence": [{"source": "原帖", "evidence": "证据", "post_url": "https://example.com/post"}], "methodology": []},
        "data_limitations": [],
    }
    zh = build_report_html(structured, {}, {}, "2026-06-24", locale="zh")
    en = build_report_html(structured, {}, {}, "2026-06-24", locale="en")

    assert "<h2>话题分布</h2>" in zh
    assert "Theme Landscape" not in zh
    assert "Admissions" not in zh
    assert "招生与录取" in zh
    assert "<h2>Topic Distribution</h2>" in en
    assert "<h2>话题分布</h2>" not in en
    assert ">https://example.com/post<" not in zh
    assert "打开原帖 →</a>" in zh
    assert "Open original post →</a>" in en


def test_broad_top_posts_are_relevant_ranked_and_rendered_in_monitoring_order(tmp_path: Path) -> None:
    notes = [
        {
            "note_id": "n1",
            "title": "高评论帖子",
            "body": "这是高评论帖的正文。",
            "author_name": "作者一",
            "like_count": 0,
            "collect_count": 0,
            "comment_count": 8,
            "share_count": 0,
            "published_at": "2026-08-20",
            "post_url": "https://example.com/n1",
            "raw": {"comments_count": 8},
        },
        {
            "note_id": "n2",
            "title": "同评论高互动",
            "body": "点赞较高，应在同评论数时排前。",
            "author_name": "作者二",
            "like_count": 30,
            "collect_count": 2,
            "comment_count": 5,
            "share_count": 1,
            "published_at": "2026-08-19",
            "post_url": "https://example.com/n2",
            "raw": {"liked_count": 30, "comments_count": 5, "shared_count": 1},
        },
        {
            "note_id": "n3",
            "title": "同评论低互动",
            "body": "互动较低。",
            "author_name": "作者三",
            "like_count": 2,
            "collect_count": 0,
            "comment_count": 5,
            "share_count": 0,
            "published_at": "2026-08-18",
            "post_url": "https://example.com/n3",
            "raw": {"liked_count": 2, "comments_count": 5, "shared_count": 0},
        },
        {
            "note_id": "noise",
            "title": "无关高互动",
            "body": "不应进入相关原帖。",
            "comment_count": 999,
            "raw": {"comments_count": 999},
        },
    ]
    annotations = [
        {"note_id": "n1", "hku_relevance": "direct", "sentiment": "neutral"},
        {"note_id": "n2", "hku_relevance": "indirect", "sentiment": "positive"},
        {"note_id": "n3", "hku_relevance": "direct", "sentiment": "negative"},
        {"note_id": "noise", "hku_relevance": "unrelated", "sentiment": "neutral"},
    ]
    write_jsonl(tmp_path / "processed_notes.jsonl", notes)
    write_jsonl(tmp_path / "annotations.jsonl", annotations)

    top_posts = _broad_top_posts_from_run(tmp_path)

    assert [row["note_id"] for row in top_posts] == ["n1", "n2", "n3"]
    assert top_posts[0]["like_count"] is None
    assert top_posts[0]["share_count"] is None
    assert top_posts[0]["comment_count"] == 8
    structured = {
        "title": "Broad 报告",
        "report_mode": "broad_report",
        "generated_scope": {"analysis_notes": 3, "analysis_comments": 0, "comment_collection_status": "not_requested"},
        "header_distributions": {"sentiment": {"neutral": 1, "positive": 1, "negative": 1}, "content_type": {"information_sharing": 3}},
        "executive_summary": "摘要",
        "key_findings_across_themes": [{"finding": "发现一"}, {"finding": "发现二"}, {"finding": "发现三"}],
        "top_original_posts": top_posts,
        "alerts": [{"signal_id": "s1", "signal": "重点信号", "alert_level": "medium",
                    "alert_type": "concern", "summary": "风险摘要", "evidence": "证据"}],
        "positive_reputation_signals": [{"signal": "正面", "summary": "正面摘要", "evidence": "证据"}],
        "theme_landscape": [{"theme": "Admissions", "volume": 3, "engagement_sum": 50, "summary": "招生讨论"}],
        "appendix": {"evidence": [{"source": "原帖", "post_url": "https://example.com/legacy"}], "methodology": ["方法"]},
        "data_limitations": ["限制"],
    }
    html_report = build_report_html(structured, {}, {}, "2026-08-24", locale="zh")

    html_headings = ["执行摘要", "监测概览", "Top 10 原帖", "重点关注", "正面与声誉信号", "话题分布", "附录"]
    assert [html_report.index(label) for label in html_headings] == sorted(html_report.index(label) for label in html_headings)
    assert "来源索引" not in html_report
    assert "内容类型与情绪概览" not in html_report
    # 互动量改为内联小字，不再是 <b> 胶囊块。
    assert "· 点赞 0" not in html_report
    assert "· 评论 8" in html_report
    assert html_report.count("class='signal-card signal-card-alert'") == 1
    assert html_report.count("class='signal-card signal-card-positive'") == 1
    alert_card = html_report.split("class='signal-card signal-card-alert'", 1)[1].split("</article>", 1)[0]
    positive_card = html_report.split("class='signal-card signal-card-positive'", 1)[1].split("</article>", 1)[0]
    assert alert_card.index("signal-description") < alert_card.index("signal-evidence")
    assert positive_card.index("signal-description") < positive_card.index("signal-evidence")
    assert "风险摘要" in alert_card and "正面摘要" in positive_card
    assert "风险预警" not in html_report


def test_report_renderer_entrypoints_have_single_definition() -> None:
    import ast
    import xhs_listener.report as report_module

    tree = ast.parse(Path(report_module.__file__).read_text(encoding="utf-8"))
    names = [node.name for node in tree.body if isinstance(node, ast.FunctionDef)]
    for name in ("_ensure_report_defaults", "build_report_html"):
        assert names.count(name) == 1


def test_default_broad_scan_keyword_pool_can_be_overridden() -> None:
    pool = parse_keyword_pool('[{"keyword":"港大商学院","max_notes":3},{"keyword":"HKUBS","max_notes":2}]')

    assert [item.keyword for item in pool] == ["港大商学院", "HKUBS"]
    assert pool[0].max_notes == 3


def test_default_broad_scan_keyword_pool_allocates_pages_per_keyword() -> None:
    config = BroadScanConfig()

    assert [(row.keyword, row.max_pages) for row in config.keyword_pool] == [
        ("港大商学院", 3),
        ("香港大学商学院", 2),
        ("港大经管学院", 2),
        ("港大硕士", 2),
        ("HKU Business School", 1),
        ("HKUBS", 1),
        ("港大商科", 1),
        ("港大体验", 1),
        ("港大就业", 1),
    ]
    # "hku 商学院" 已从关键词池移除。
    assert all(row.keyword != "hku 商学院" for row in config.keyword_pool)
    # 页数是唯一采集上限：每个关键词的 note_cap 必须等于 页数 × 20，否则多买的页会被浪费。
    assert all(row.note_cap == row.max_pages * 20 for row in config.keyword_pool)
    assert sum(row.max_pages for row in config.keyword_pool) == 14
    assert config.time_filter == "一周内"
    # Broad / Topic / 竞对共用同一个报告窗口口径，不允许并存两种「本周」定义。
    assert config.reporting_window_days == 8
    assert config.note_type == "普通笔记"


def test_broad_scan_keyword_pool_accepts_string_list() -> None:
    pool = parse_keyword_pool(["港大商学院", "HKUBS"])

    assert [item.keyword for item in pool] == ["港大商学院", "HKUBS"]
    assert [item.max_notes for item in pool] == [20, 20]


def test_report_defaults_coerce_bad_llm_schema() -> None:
    report = _ensure_report_defaults(
        {
            "title": {"bad": "title"},
        },
        analysis={"risk_urgency_matters": "最高优先级", "data_limitations": ["限制"]},
        processing={},
    )

    assert report["title"] == "HKU 小红书洞察报告"
    assert "risk_urgency_matters" not in report
    assert report["data_limitations"][0] == "限制"
    assert any("不能代表全平台" in item for item in report["data_limitations"])
    assert "appendix" in report


def test_report_uses_narrative_table_for_topic_main_narratives() -> None:
    report = _ensure_report_defaults(
        {},
        analysis={
            "main_narratives": [
                {
                    "cluster_id": "cluster_faq",
                    "narrative": "选课 FAQ",
                    "stance": "neutral",
                    "summary": "学生集中问选课规则",
                    "evidence": "请问怎么选课",
                }
            ]
        },
        processing={"scan_mode": "topic_scan"},
        analysis_bundle={
            "report_mode": "topic_report",
            "narrative_table": [
                    {
                        "label": "选课 FAQ",
                        "cluster_id": "cluster_faq",
                        "volume": 7,
                        "engagement_sum": 300,
                        "recent_7d_count": 1,
                        "latest_post_date": "2026-06-01",
                        "evidence_items": [{"evidence_id": "evidence_n1", "quote": "请问怎么选课", "note_id": "n1"}],
                    }
            ]
        },
    )

    row = report["main_narratives"][0]
    assert row["volume"] == 7
    assert row["engagement_sum"] == 300
    assert row["summary"] == "学生集中问选课规则"


def test_topic_report_preserves_code_clusters_and_omits_singletons() -> None:
    report = _ensure_report_defaults(
        {},
        analysis={
            "main_narratives": [
                {
                    "cluster_id": "cluster_value",
                    "narrative": "学费上涨导致商科硕士性价比下降",
                    "stance": "negative",
                    "summary": "学费暴涨造成性价比讨论升温",
                    "evidence": "学费涨到五十万",
                },
                {
                    "cluster_id": "cluster_tuition",
                    "narrative": "港大商学院学费持续大幅上涨",
                    "stance": "negative",
                    "summary": "帖子继续讨论学费上涨和费用压力",
                    "evidence": "商学院项目费用更贵",
                },
                {
                    "cluster_id": "cluster_stable",
                    "narrative": "两年制学费保持稳定",
                    "stance": "neutral",
                    "summary": "部分帖子提到两年制项目学费未变",
                    "evidence": "两年制还是原价",
                },
                {
                    "cluster_id": "cluster_phd",
                    "narrative": "港大博士生活费用及学费带来经济压力",
                    "stance": "negative",
                    "summary": "博士群体讨论生活费用和学费的综合压力",
                    "evidence": "博士生活费和学费压力都不小",
                },
            ]
        },
        processing={"scan_mode": "topic_scan", "keyword": "港大学费"},
        analysis_bundle={
            "report_mode": "topic_report",
            "narrative_table": [
                {"label": "学费上涨导致商科硕士性价比下降", "cluster_id": "cluster_value", "volume": 2, "engagement_sum": 10, "evidence_items": [{"evidence_id": "evidence_n1", "quote": "学费涨到五十万", "note_id": "n1"}]},
                {"label": "港大商学院学费持续大幅上涨", "cluster_id": "cluster_tuition", "volume": 3, "engagement_sum": 20, "evidence_items": [{"evidence_id": "evidence_n2", "quote": "商学院项目费用更贵", "note_id": "n2"}]},
                {"label": "两年制学费保持稳定", "cluster_id": "cluster_stable", "volume": 1, "engagement_sum": 5, "evidence_items": [{"evidence_id": "evidence_n3", "quote": "两年制还是原价", "note_id": "n3"}]},
                {"label": "港大博士生活费用及学费带来经济压力", "cluster_id": "cluster_phd", "volume": 4, "engagement_sum": 15, "evidence_items": [{"evidence_id": "evidence_n4", "quote": "博士生活费和学费压力都不小", "note_id": "n4"}]},
            ],
        },
    )

    rows = report["main_narratives"]
    assert [row["narrative"] for row in rows] == [
        "港大商学院学费持续大幅上涨",
        "港大博士生活费用及学费带来经济压力",
        "学费上涨导致商科硕士性价比下降"  # 决策 4：不再自动中性化改写,
    ]
    assert [row["engagement_sum"] for row in rows] == [20, 15, 10]
    assert "single_post_observations" not in report
    assert all(row["volume"] >= 2 for row in rows)


def test_topic_evidence_registry_dedupes_posts_and_body_uses_ids_only() -> None:
    report = _ensure_report_defaults(
        {},
        analysis={
            "main_narratives": [{"cluster_id": "cluster_1", "summary": "多帖讨论 VI 邀请范围。"}],
            "questions_uncertainties": [{"uncertainty_id": "uncertainty_1", "summary": "官方未说明发放范围。"}],
        },
        processing={"scan_mode": "topic_scan", "keyword": "港大录取"},
        analysis_bundle={
            "report_mode": "topic_report",
            "analysis_notes": 3,
            "narrative_table": [
                {
                    "cluster_id": "cluster_1",
                    "label": "VI邀请范围讨论",
                    "volume": 2,
                    "engagement_sum": 20,
                    "topic_relevance_counts": {"direct": 2},
                    "signal_labels": ["VI邀请发放范围"],
                    "signal_label_counts": {"VI邀请发放范围": 2},
                    "evidence_items": [
                        {"evidence_id": "evidence_n1", "quote": "想知道收到VI面的多不多", "note_id": "n1", "post_url": "https://example.com/n1", "signal_label": "VI邀请发放范围"},
                        {"evidence_id": "evidence_n2", "quote": "没收到面试是不是没希望", "note_id": "n2", "post_url": "https://example.com/n2", "signal_label": "VI邀请发放范围"},
                    ],
                },
                {
                    "cluster_id": "cluster_single",
                    "label": "单帖",
                    "volume": 1,
                    "engagement_sum": 1,
                    "evidence_items": [{"evidence_id": "evidence_n3", "quote": "单帖原文", "note_id": "n3", "post_url": "https://example.com/n3"}],
                },
            ],
            "uncertainty_table": [
                {
                    "uncertainty_id": "uncertainty_1",
                    "title": "VI 邀请的发放范围及其与后续录取机会的关系",
                    "support_count": 2,
                    "supporting_post_ids": ["n1", "n2"],
                    "engagement_sum": 20,
                    "evidence_items": [
                        {"evidence_id": "evidence_n1", "quote": "想知道收到VI面的多不多", "note_id": "n1", "post_url": "https://example.com/n1"},
                        {"evidence_id": "evidence_n2", "quote": "没收到面试是不是没希望", "note_id": "n2", "post_url": "https://example.com/n2"},
                    ],
                }
            ],
        },
    )

    evidence = report["appendix"]["evidence"]
    # evidence_id 保持稳定（grounding），source 是读者可见的确定性短编号。
    assert [row["evidence_id"] for row in evidence] == ["evidence_n1", "evidence_n2"]
    assert [row["source"] for row in evidence] == ["E01", "E02"]
    assert len({row["note_id"] for row in evidence}) == len(evidence)
    assert report["main_narratives"][0]["evidence"] == "E01 · E02"
    assert report["questions_uncertainties"][0]["evidence"] == "E01 · E02"
    # 两条证据都由 LLM 的 evidence_ids 选中并通过校验，不再被语义挑选压成 1 条。
    assert len(report["main_narratives"][0]["representative_quotes"]) == 2
    assert len(report["questions_uncertainties"][0]["representative_quotes"]) == 2
    assert all("evidence" not in row for row in evidence)
    assert all(row["note_id"] != "n3" for row in evidence)
    assert "http" not in report["main_narratives"][0]["evidence"]
    assert "single_post_observations" not in report
    assert "按加权互动量排序" not in report["executive_summary"]
    assert "样本中反复出现的观点包括" not in report["executive_summary"]


def test_topic_evidence_fallback_uses_narrative_and_comment_tables() -> None:
    rows = _monitoring_evidence(
        [],
        {
            "signal_table": [],
            "narrative_table": [
                {
                    "label": "学费上涨",
                    "evidence_items": [{"evidence_id": "evidence_n1", "quote": "学费涨了", "note_id": "n1", "post_url": "https://example.com/n1"}],
                }
            ],
            "narrative_comment_table": [
                {"narrative": "学费上涨", "question_comments": [{"content": "请问涨多少？", "note_id": "n2"}]},
            ],
            "_note_post_urls": {"n2": "https://example.com/n2"},
        },
    )

    assert rows[0]["evidence"] == "学费涨了"
    assert any(row["source"] == "评论" and row["evidence"] == "请问涨多少？" for row in rows)


def test_topic_unrelated_annotation_blanks_all_business_fields() -> None:
    row = _normalize_annotation(
        {
            "note_id": "n1",
            "content_type": "complaint",
            "sentiment": "negative",
            "primary_narrative": "不应保留",
            "risk_level": "high",
            "risk_type": "reputation",
            "signal_label": "不应保留",
        },
        "n1",
        "topic_scan",
        {"note_id": "n1", "hku_relevance": "direct", "topic_relevance": "unrelated", "relevance_reason": "不相关"},
    )

    assert row["topic_relevance"] == "unrelated"
    assert row["content_type"] == ""
    assert row["primary_narrative"] == ""
    assert row["risk_level"] == ""
    assert row["signal_label"] == ""


def test_broad_annotation_keeps_theme_taxonomy() -> None:
    row = _normalize_annotation(
        {
            "note_id": "n2",
            "theme": "Course_Selection",
            "content_type": "question",
            "sentiment": "neutral",
            "author_type": "unclear",
            "signal_types": ["informational"],
            "risk_level": "low",
            "risk_type": "misunderstanding",
            "signal_label": "选课规则不清",
            "evidence_quote": "请问怎么选课",
        },
        "n2",
        "broad_scan",
        {"note_id": "n2", "hku_relevance": "direct", "topic_relevance": "indirect", "relevance_reason": "HKU"},
    )

    assert row["theme"] == "Course_Selection"
    assert row["primary_narrative"] == ""
    assert row["risk_type"] == "misunderstanding"


def test_published_at_prefers_publish_time_and_skips_zero_placeholder() -> None:
    from xhs_listener.collect import _published_at_from_node

    # update_time 是编辑时间，发布时间字段优先。
    assert _published_at_from_node({"time": 1717200000, "update_time": 1717286400000}) == "1717200000"
    # 0 是 TikHub 占位时间戳，不应被解析成 1970 年。
    assert _published_at_from_node({"last_update_time": 0, "update_time": 0}) is None
    assert _published_at_from_node({"time": 0, "timestamp": 1717200000}) == "1717200000"


def test_short_evidence_quote_breaks_at_sentence_boundary() -> None:
    from xhs_listener.analyze import _short_evidence_quote

    short = "宿舍条件很好"
    assert _short_evidence_quote(short) == short

    long_text = (
        "我需要在6.11之前交12w多港币的留位费，而在6.11之前我必然是没有雅思成绩的，真的压力巨大，"
        "希望学校可以给出延期政策，不然只能放弃这个offer了，而且中介也说没有别的办法，只能继续等学校回复邮件"
    )
    assert len(long_text) > 80
    trimmed = _short_evidence_quote(long_text)
    assert len(trimmed) <= 81
    # 在句读处截断，不留半句。
    assert trimmed.endswith(("。", "；", "！", "？", "，", "…"))


def test_tikhub_retry_skips_non_recoverable_4xx() -> None:
    from xhs_listener.tikhub import _should_retry

    assert _should_retry(RuntimeError("TikHub API HTTP 401 on /path. token expired")) is False
    assert _should_retry(RuntimeError("TikHub API HTTP 403 on /path.")) is False
    assert _should_retry(RuntimeError("TikHub API HTTP 429 on /path. rate limited")) is True
    assert _should_retry(RuntimeError("TikHub API HTTP 502 on /path.")) is True
    assert _should_retry(TimeoutError("timed out")) is True


def test_budgeted_llm_client_blocks_calls_once_over_budget() -> None:
    import pytest
    from xhs_listener.run_manager import BudgetedLLMClient, BudgetExceededError

    class FakeResponse:
        class usage:
            total_tokens = 120

    class FakeInner:
        def get_response(self, messages, *args, **kwargs):
            return FakeResponse()

        def usage_dict(self, response):
            return {"total_tokens": 120}

    client = BudgetedLLMClient(FakeInner(), budget_tokens=100)
    with pytest.raises(BudgetExceededError):
        client.get_response([{"role": "user", "content": "hi"}])
    assert client.usage_tokens == 120
    # 超预算后，后续调用在发起前就被拦截。
    with pytest.raises(BudgetExceededError):
        client.get_response([{"role": "user", "content": "again"}])
    assert client.usage_tokens == 120


def test_estimate_analysis_tokens_scales_with_actual_counts() -> None:
    from xhs_listener.run_manager import estimate_analysis_tokens_for_counts

    request = ManagedRunRequest(mode="topic_scan")
    small = estimate_analysis_tokens_for_counts(request, notes_count=10, comments_count=0)
    large = estimate_analysis_tokens_for_counts(request, notes_count=100, comments_count=200)

    assert small["estimated_analysis_tokens"] < large["estimated_analysis_tokens"]
    assert small["estimated_analysis_tokens"] <= small["estimated_analysis_tokens_upper"]


def test_report_defaults_prefer_code_counts_over_llm_numbers() -> None:
    report = _ensure_report_defaults(
        {},
        analysis={
            "listening_scope": {
                "search_query": "模型编的搜索词",
                "hku_relevant_notes": 999,
                "hku_relevant_comments": 888,
            }
        },
        processing={"keyword": "broad_scan", "scan_mode": "broad_scan", "keyword_pool": [{"keyword": "港大商学院"}, {"keyword": "HKUBS"}]},
        analysis_bundle={"analysis_notes": 46, "analysis_comments": 0, "report_mode": "broad_report"},
    )

    scope = report["generated_scope"]
    assert scope["analysis_notes"] == 46
    assert scope["search_query"] == "港大商学院, HKUBS"


def test_scope_pattern_matches_hkubs() -> None:
    import re
    from xhs_listener.models import HKU_SCOPE_PATTERN

    for text in ("HKUBS orientation day", "hkubs 新生群", "HKU Business School", "港大商学院选课"):
        assert re.search(HKU_SCOPE_PATTERN, text), text
    assert re.search(HKU_SCOPE_PATTERN, "普通的选课分享") is None


def test_code_hku_relevance_uses_post_tags_but_not_search_keyword() -> None:
    tag_match = _code_hku_relevance(
        {"note_id": "1", "title": "选课攻略", "text": "正文", "tags": ["HKUBS"], "keyword": "选课"},
        scan_mode="broad_scan",
    )
    keyword_only = _code_hku_relevance(
        {"note_id": "2", "title": "Capstone经验", "text": "正文", "tags": [], "keyword": "港大 Capstone"},
        scan_mode="topic_scan",
    )

    assert tag_match["hku_relevance"] == "direct"
    assert "HKUBS" in tag_match["hku_match_reason"]
    assert keyword_only["hku_relevance"] == "unrelated"
    assert "keyword" not in keyword_only["hku_match_reason"]


def test_broad_scan_code_gate_skips_llm_for_explicit_hku_posts(tmp_path: Path) -> None:
    from xhs_listener.analyze import annotate_relevance_records

    class ExplodingClient:
        def get_response(self, messages, *args, **kwargs):
            raise AssertionError("LLM should not be called when all posts code-match")

    records = [
        {"note_id": "1", "title": "HKUBS Orientation", "text": "新生活动分享", "comment_sample": "", "engagement": 10},
        {"note_id": "2", "title": "港大商学院选课", "text": "抢课经验", "comment_sample": "", "engagement": 5},
    ]
    rows, usage = annotate_relevance_records(
        records,
        {"scan_mode": "broad_scan"},
        ExplodingClient(),
        logs=[],
        log_queue=None,
        run_path=tmp_path,
        batch_size=5,
    )

    assert usage == []
    assert [row["hku_relevance"] for row in rows] == ["direct", "direct"]
    assert (tmp_path / "relevance_annotations.jsonl").exists()


def test_broad_cache_schema_rejects_previous_annotations() -> None:
    current = {
        "note_id": "1",
        "hku_relevance": "direct",
        "topic_relevance": "indirect",
        "content_type": "question",
        "theme": "Admissions",
        "sentiment": "neutral",
        "author_type": "real_user",
        "signal_label": "application_timing",
        "signal_types": ["question"],
        "risk_level": "none",
        "risk_type": "none",
    }

    assert ANNOTATION_SCHEMA_VERSION == "broad_theme_v3"
    assert _code_hku_relevance(
        {"note_id": "1", "title": "HKUBS orientation", "tags": []},
        "broad_scan",
    )["annotation_schema_version"] == "broad_theme_v3"
    assert _cached_annotation_has_current_schema(
        {**current, "annotation_schema_version": "broad_theme_v3"},
        "broad_scan",
    )
    for old_version in ("broad_theme_v1", "broad_theme_v2"):
        assert not _cached_annotation_has_current_schema(
            {**current, "annotation_schema_version": old_version},
            "broad_scan",
        )


def test_broad_programme_abbreviations_match_real_programme_context() -> None:
    examples = (
        "HKU MAA programme introduction",
        "港大 2026 MFin offer 已录取",
        "HKU MGM master programme",
        "HKU MWM degree information",
    )

    for text in examples:
        relevance, _ = _hkubs_broad_relevance(text)
        assert relevance == "indirect", text


def test_broad_programme_abbreviations_reject_known_false_positives() -> None:
    examples = (
        "HKU team final result announced",
        "港大 drama award 获奖名单",
        "HKU alumni gathering at MGM Macau",
        "HKU student trip to MGM Macau",
        "HKU MGM Macau campus visit",
    )

    for text in examples:
        relevance, _ = _hkubs_broad_relevance(text)
        assert relevance == "unrelated", text


def test_broad_finance_alias_accepts_hku_finance_context() -> None:
    for text in ("港大金融就业怎么样", "HKU finance career outcomes"):
        relevance, reason = _hkubs_broad_relevance(text)
        assert relevance == "indirect", text
        assert "Finance" in reason


def test_broad_scope_recovers_confirmed_hkubs_false_negatives() -> None:
    examples = (
        "港大商业人工智能下offer了 #商科",
        "hku yr2 寻找课友 #商学院",
        "香港大学MBA",
        "港大FWM提前批锁系统",
        "HKUMAIB好像锁了",
        "香港大学 港大复旦MBA 开学",
        "港复 IMBA 新生入学 #港大复旦imba",
        "复旦- HKU imba开学营",
        "港大全球管理课程变化",
        "香港大学 #商科 #申请季 #offer",
        "hku 香港大学 商科 本科专业",
        "hku商科 实习讨论",
        "港大27fall硕士专业汇总 商学院首轮截止",
    )

    for text in examples:
        relevance, _ = _hkubs_broad_relevance(text)
        assert relevance in {"direct", "indirect"}, text


def test_broad_scope_keeps_non_hkubs_programmes_excluded() -> None:
    examples = (
        "香港大学文学院27Fall 人工智能、伦理与社会 申请时间",
        "港大MCCC 香港大学社会学系 媒体文化创意城市",
        "港大还是港中文？武汉211计算机，准备申港硕",
        "港大计算机科学硕士申请经验",
    )

    for text in examples:
        relevance, _ = _hkubs_broad_relevance(text)
        assert relevance == "unrelated", text


def test_run_dir_uses_microseconds_and_uuid(tmp_path: Path) -> None:
    collector = XiaohongshuCollector(api_token="token", output_root=tmp_path)

    first = collector._make_run_dir(CollectConfig(keyword="港大"))
    second = collector._make_run_dir(CollectConfig(keyword="港大"))

    assert first != second
    assert first.name.count("_") >= 3
    assert len(first.name.rsplit("_", 1)[-1]) == 8


def test_signal_table_preserves_merged_source_labels() -> None:
    """合并只认 LLM 的 label_map；被合并掉的原 label 记进 source_labels 供回溯。"""

    notes = [
        {"note_id": "1", "like_count": 10},
        {"note_id": "2", "like_count": 20},
    ]
    annotations = [
        {"note_id": "1", "signal_label": "排队抢课攻略", "signal_types": ["informational"], "evidence_quote": "提前排队"},
        {"note_id": "2", "signal_label": "waiting list 太长", "signal_types": ["operational"], "evidence_quote": "排了三周"},
    ]
    label_map = {"排队抢课攻略": "选课容量与 waiting list 摩擦", "waiting list 太长": "选课容量与 waiting list 摩擦"}

    table = build_signal_table(notes, annotations, label_map)

    assert table[0]["signal"] == "选课容量与 waiting list 摩擦"
    assert "排队抢课攻略" in table[0]["source_labels"]
    assert "waiting list 太长" in table[0]["source_labels"]


def test_signal_table_keeps_labels_separate_without_llm_merge() -> None:
    """LLM 合并不到时保留各自的 label，不用手写词表猜它们等价。"""

    notes = [{"note_id": "1", "like_count": 10}, {"note_id": "2", "like_count": 20}]
    annotations = [
        {"note_id": "1", "signal_label": "排队抢课攻略", "signal_types": ["informational"]},
        {"note_id": "2", "signal_label": "waiting list 太长", "signal_types": ["operational"]},
    ]

    table = build_signal_table(notes, annotations)

    assert sorted(row["signal"] for row in table) == ["waiting list 太长", "排队抢课攻略"]


def test_report_limitations_include_code_counted_commercial_posts() -> None:
    report = _ensure_report_defaults(
        {},
        analysis={"data_limitations": ["样本量有限"]},
        processing={"keyword": "港大 Capstone", "scan_mode": "topic_scan"},
        analysis_bundle={
            "analysis_notes": 10,
            "analysis_comments": 0,
            "annotation_summary": {"signal_type": {"commercial": 3, "informational": 5}},
        },
    )

    limitations = report["data_limitations"]
    assert "样本量有限" in limitations
    assert any("3 条" in item and "商业" in item for item in limitations)


def test_note_date_never_falls_back_to_collected_at() -> None:
    from xhs_listener.analyze import _note_date

    # 没有发布时间的帖子必须返回 None，不能用采集时间冒充发布时间。
    assert _note_date({"collected_at": "2026-06-08T10:35:25"}) is None
    parsed = _note_date({"published_at": "1780749658", "collected_at": "2026-06-08T10:35:25"})
    assert parsed is not None and parsed.year == 2026 and parsed.month == 6


def test_signal_table_tracks_undated_mentions() -> None:
    notes = [
        {"note_id": "1", "like_count": 10, "published_at": "1780749658"},
        {"note_id": "2", "like_count": 5, "collected_at": "2026-06-08T10:00:00"},
    ]
    annotations = [
        {"note_id": "1", "signal_label": "排队抢课攻略", "signal_types": ["operational"]},
        {"note_id": "2", "signal_label": "排队抢课攻略", "signal_types": ["operational"]},
    ]

    table = build_signal_table(notes, annotations)

    assert table[0]["mention_count"] == 2
    assert table[0]["dated_mention_count"] == 1
    assert table[0]["undated_mention_count"] == 1


def test_process_backfills_published_at_from_raw(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    write_json(run_dir / "collection.json", {"keyword": "港大", "scan_mode": "topic_scan"})
    write_jsonl(
        run_dir / "notes.jsonl",
        [
            {
                "note_id": "1",
                "title": "港大选课",
                "body": "正文",
                "published_at": None,
                "raw": {"detail": {"note": {"time": 1780749658, "last_update_time": 0}}},
            }
        ],
    )
    write_jsonl(run_dir / "comments.jsonl", [])

    process_run(run_dir)

    import json as _json

    rows = [_json.loads(line) for line in (run_dir / "processed_notes.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[0]["published_at"] == "1780749658"


def test_all_code_gates_ignore_recall_keyword() -> None:
    # 搜索关键词由所有结果继承，Broad/Topic 都不能把它当帖子证据。
    record = {"note_id": "1", "title": "普通选课分享", "text": "和港校无关的正文", "tags": [], "keyword": "港大商学院"}

    broad = _code_hku_relevance(record, scan_mode="broad_scan")
    topic = _code_hku_relevance(record, scan_mode="topic_scan")

    assert broad["hku_relevance"] == "unrelated"
    assert topic["hku_relevance"] == "unrelated"


def test_code_hku_relevance_reads_content_full_or_body() -> None:
    row = _code_hku_relevance({"note_id": "1", "title": "", "content_full": "港大商学院学费讨论", "tags": []}, "broad_scan")

    assert row["hku_relevance"] == "direct"


def test_topic_relevance_missing_defaults_to_unrelated() -> None:
    row = _normalize_relevance(
        {},
        "n1",
        "topic_scan",
        {"note_id": "n1", "hku_relevance": "direct", "hku_match_reason": "港大"},
    )

    assert row["hku_relevance"] == "direct"
    assert row["topic_relevance"] == "unrelated"


def test_topic_narrative_map_merges_fragmented_labels() -> None:
    notes = [{"note_id": "1", "like_count": 10}, {"note_id": "2", "like_count": 20}]
    annotations = [
        {"note_id": "1", "primary_narrative": "弹性两年制", "narrative_stance": "neutral"},
        {"note_id": "2", "primary_narrative": "一年或两年弹性学制", "narrative_stance": "neutral"},
    ]
    narrative_map = {
        "弹性两年制": "弹性一年或两年制",
        "一年或两年弹性学制": "弹性一年或两年制",
    }

    table = build_discussion_table(notes, annotations, "topic_scan", narrative_map)

    assert len(table) == 1
    assert table[0]["label"] == "弹性一年或两年制"
    assert table[0]["volume"] == 2


def test_topic_narrative_table_does_not_include_risk_counts() -> None:
    notes = [{"note_id": "1", "like_count": 10}]
    annotations = [
        {
            "note_id": "1",
            "primary_narrative": "学费上涨讨论",
            "narrative_stance": "negative",
            "risk_level": "high",
        }
    ]

    table = build_discussion_table(notes, annotations, "topic_scan")

    assert "risk_level_counts" not in table[0]
    assert table[0]["narrative_stance_counts"] == {"negative": 1}


def test_topic_annotation_summary_omits_risk_counters() -> None:
    summary = _annotation_summary(
        [
            {
                "note_id": "1",
                "hku_relevance": "direct",
                "topic_relevance": "direct",
                "primary_narrative": "学费上涨讨论",
                "narrative_stance": "negative",
                "risk_level": "high",
            }
        ],
        "topic_scan",
    )

    assert "risk_level" not in summary
    assert "risk_type" not in summary
    assert summary["primary_narrative"] == {"学费上涨讨论": 1}


def test_topic_final_prompt_only_uses_topic_core_tables() -> None:
    prompt = build_topic_analysis_prompt(
        processing={"scan_mode": "topic_scan", "keyword": "港大学费"},
        time_coverage={"analysis_notes": 1},
        post_evidence=[{"note_id": "1", "title": "学费讨论"}],
        narrative_table=[{"label": "学费上涨讨论", "volume": 1}],
        uncertainty_table=[{"uncertainty_type": "成本", "count": 1}],
        narrative_comment_table=[{"label": "学费上涨讨论", "comment_count": 2}],
    )

    assert "代码聚合 narrative_table" in prompt
    assert "代码聚合 narrative_comment_table" in prompt
    assert "代码聚合 uncertainty_table" in prompt
    assert "代码聚合 signal_table" not in prompt
    assert "代码聚合 comment_signal_table" not in prompt
    assert "代码聚合 theme_table" not in prompt
    assert "评论样本" not in prompt
    assert "risk_level" not in prompt
    assert "owner" not in prompt
    assert "recommendation" not in prompt
    assert "why_it_matters" not in prompt


def test_empty_topic_analysis_is_deterministic_without_llm() -> None:
    result = run_topic_analysis(
        notes=[],
        annotations=[],
        processing={"scan_mode": "topic_scan", "keyword": "港大选课"},
        narrative_table=[],
        uncertainty_table=[],
        narrative_comment_table=[],
        client=object(),
        time_coverage={"notes_with_publish_date": 0, "notes_without_publish_date": 0},
    )

    assert result["executive_summary"] == "本轮没有足够相关样本形成专题结论。"
    assert result["main_narratives"] == []
    assert result["questions_uncertainties"] == []
    assert result["appendix"]["supporting_evidence"] == []
    assert result["data_limitations"]
    assert result["_usage"] == {}


def test_broad_analysis_does_not_apply_topic_cluster_validation() -> None:
    import json as _json
    import types

    class Client:
        def get_response(self, messages, *args, **kwargs):
            message = types.SimpleNamespace(content='{"executive_summary":"宽口径事实摘要"}')
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)

        def usage_dict(self, response):
            return {}

        def extract_json(self, text):
            return _json.loads(text)

    result = run_broad_analysis([], [], [], {}, [], [], [], [], [], [], [], Client(), {})

    assert result["executive_summary"] == "宽口径事实摘要"
    assert result["_usage"]["phase"] == "analysis"


def test_report_header_uses_relevant_annotation_summary() -> None:
    report = _ensure_report_defaults(
        {},
        analysis={},
        processing={"scan_mode": "topic_scan", "keyword": "港大选课"},
        analysis_bundle={
            "report_mode": "topic_report",
            "annotation_summary_all": {"sentiment": {"positive": 2, "unknown": 8}},
            "annotation_summary_relevant": {"sentiment": {"positive": 2}},
            "annotation_summary": {"sentiment": {"positive": 2}},
        },
    )

    assert report["header_distributions"]["sentiment"] == {"positive": 2}


def test_signal_table_prefers_llm_label_map_over_rules() -> None:
    notes = [
        {"note_id": "1", "like_count": 10},
        {"note_id": "2", "like_count": 5},
    ]
    annotations = [
        # 规则会把"面试"吸进"申请录取不确定性"，label_map 必须优先生效。
        {"note_id": "1", "signal_label": "港大商学院学制改革及面试新规解析", "signal_types": ["informational"]},
        {"note_id": "2", "signal_label": "港大硕士改为弹性两年制官宣", "signal_types": ["informational"]},
    ]
    label_map = {
        "港大商学院学制改革及面试新规解析": "港大商学院硕士学制改为弹性两年制",
        "港大硕士改为弹性两年制官宣": "港大商学院硕士学制改为弹性两年制",
    }

    table = build_signal_table(notes, annotations, label_map)

    assert len(table) == 1
    assert table[0]["signal"] == "港大商学院硕士学制改为弹性两年制"
    assert table[0]["mention_count"] == 2
    assert len(table[0]["source_labels"]) == 2


def test_replace_note_ids_works_after_chinese_prefix() -> None:
    from xhs_listener.report import _replace_note_ids_with_urls

    bundle = {"_note_post_urls": {"6a2a6406000000002201aff4": "https://example.com/post1"}}
    text = "帖子6a2a6406000000002201aff4 提到学制改革"

    assert "https://example.com/post1" in _replace_note_ids_with_urls(text, bundle)
    # 已在 URL 里的 id 不应被二次替换
    url_text = "见 https://www.xiaohongshu.com/explore/6a2a6406000000002201aff4"
    assert _replace_note_ids_with_urls(url_text, bundle).count("http") == 1


def test_llm_json_call_retries_on_bad_json() -> None:
    from xhs_listener.analyze import _llm_json_call

    class FlakyClient:
        def __init__(self):
            self.calls = 0

        def get_response(self, messages, *args, **kwargs):
            self.calls += 1
            class R:
                class choices:  # noqa: N801
                    pass
            import types
            content = '{"ok": true}' if self.calls > 1 else '{"broken": '
            msg = types.SimpleNamespace(content=content)
            choice = types.SimpleNamespace(message=msg)
            return types.SimpleNamespace(choices=[choice], usage=None)

        def usage_dict(self, response):
            return {}

        def extract_json(self, text):
            import json as _json
            return _json.loads(text)

    client = FlakyClient()
    usage_rows = []
    payload = _llm_json_call(client, "prompt", "test_phase", usage_rows)

    assert payload == {"ok": True}
    assert client.calls == 2
    assert len(usage_rows) == 2


def test_build_note_extracts_plural_count_fields() -> None:
    # TikHub app_v2 用复数 comments_count / shared_count。
    collector = XiaohongshuCollector(api_token="token")
    note = collector._build_note(
        {"note": {"id": "n1", "title": "港大", "desc": "正文", "comments_count": 7, "shared_count": 4, "liked_count": 9}},
        keyword="港大",
    )

    assert note is not None
    assert note.comment_count == 7
    assert note.share_count == 4


def test_merge_detail_backfills_counts() -> None:
    collector = XiaohongshuCollector(api_token="token")
    note = collector._build_note({"note": {"id": "n1", "title": "港大", "desc": "正文"}}, keyword="港大")
    assert note.comment_count is None

    collector._merge_detail(note, {"note_list": [{"comments_count": 12, "liked_count": 30, "desc": "完整正文"}]})

    assert note.comment_count == 12
    assert note.like_count == 30


def test_protected_counterintuitive_social_post_rules_are_preserved() -> None:
    assert "必须准确识别社媒中“欲扬先抑”或“标题党反串”的干货分享帖" in COMMON_ANALYSIS_RULES
    assert "若帖子表面使用“劝退”、“避坑”等负面词汇" in COMMON_ANALYSIS_RULES
    assert "content_type 必须为 information_sharing" in COMMON_ANALYSIS_RULES
    assert "sentiment 应为 neutral 或 positive" in COMMON_ANALYSIS_RULES


def test_annotation_evidence_must_be_verbatim_and_meaningful() -> None:
    annotations = _validate_annotation_evidence_quotes(
        [
            {"note_id": "n1", "evidence_quote": "学费上涨"},
            {"note_id": "n2", "evidence_quote": "模型改写的句子"},
            {"note_id": "n3", "evidence_quote": "！"},
        ],
        [
            {"note_id": "n1", "content_full": "今年学费上涨约一成"},
            {"note_id": "n2", "content_full": "原帖没有这句话"},
            {"note_id": "n3", "content_full": "！"},
        ],
    )

    assert annotations[0]["evidence_quote"] == "学费上涨"
    assert annotations[0]["evidence_verified"] is True
    assert annotations[1]["evidence_quote"] == ""
    assert annotations[2]["evidence_quote"] == ""


def test_discussion_table_has_stable_ids_unique_membership_and_heat_order() -> None:
    notes = [
        {"note_id": "n1", "like_count": 10, "content_full": "选课容量紧张"},
        {"note_id": "n2", "like_count": 30, "content_full": "选课容量紧张"},
        {"note_id": "n3", "like_count": 100, "content_full": "课程豁免"},
    ]
    annotations = [
        {"note_id": "n1", "primary_narrative": "选课容量", "topic_relevance": "direct", "evidence_quote": "选课容量紧张"},
        {"note_id": "n2", "primary_narrative": "选课容量", "topic_relevance": "direct", "evidence_quote": "选课容量紧张"},
        {"note_id": "n3", "primary_narrative": "课程豁免", "topic_relevance": "direct", "evidence_quote": "课程豁免"},
    ]

    rows = build_discussion_table(notes, annotations, "topic_scan")

    assert [row["engagement_sum"] for row in rows] == [100, 40]
    assert all(row["cluster_id"].startswith("cluster_") for row in rows)
    assert rows[1]["note_ids"] == ["n1", "n2"]
    assert rows[1]["top_post_share"] == 0.75


def test_process_records_comment_collection_status_and_exact_content_duplicates(tmp_path: Path) -> None:
    write_json(
        tmp_path / "collection.json",
        {
            "keyword": "港大",
            "quality": {"comment_fetch_enabled": False, "comments_saved": 0},
            "errors": [],
        },
    )
    write_jsonl(
        tmp_path / "notes.jsonl",
        [
            {"note_id": "n1", "title": "同一标题", "body": "同一正文"},
            {"note_id": "n2", "title": "同一标题", "body": "同一正文"},
        ],
    )
    write_jsonl(tmp_path / "comments.jsonl", [])

    result = process_run(tmp_path)

    assert result["comment_collection_status"] == "not_requested"
    assert result["processed_notes"] == 1
    assert result["removed_note_reasons"]["duplicate_content"] == 1


def test_unknown_comment_count_does_not_block_comment_fetch() -> None:
    # 计数未提取到（None）≠ 0：不应被 min_comments 门槛拦掉。
    note = Note(note_id="1", keyword="港大", body="港大正文", comment_count=None)
    config = CollectConfig(keyword="港大", comment_policy="top_notes", comment_min_comments=3)

    assert _comment_skip_reasons(note, config) == []

    note_zero = Note(note_id="2", keyword="港大", body="港大正文", comment_count=0)
    assert _comment_skip_reasons(note_zero, config) == ["below_comment_min_comments"]


def test_topic_report_uses_quotes_source_index_and_single_not_collected_comment_metric() -> None:
    report = _ensure_report_defaults(
        {},
        analysis={
            "main_narratives": [{"cluster_id": "gift_cluster", "summary": "多帖分享新生礼盒开箱。"}],
            "questions_uncertainties": [],
        },
        processing={"scan_mode": "topic_scan", "keyword": "港大礼盒", "comment_collection_status": "not_requested"},
        analysis_bundle={
            "report_mode": "topic_report",
            "analysis_notes": 3,
            "analysis_comments": 0,
            "narrative_table": [
                {
                    "cluster_id": "gift_cluster",
                    "label": "新生礼盒包含小狮子盲盒和耳机，学生反馈积极",
                    "volume": 3,
                    "engagement_sum": 30,
                    "topic_relevance_counts": {"direct": 3},
                    "evidence_items": [
                        {"evidence_id": "evidence_n1", "quote": "小狮子盲盒一共有四款", "note_id": "n1", "post_url": "https://example.com/n1", "post_title": "新生礼盒开箱", "date": "2026-07-01"},
                        {"evidence_id": "evidence_n2", "quote": "礼盒里还有一副耳机", "note_id": "n2", "post_url": "https://example.com/n2", "post_title": "港大礼盒", "date": "2026-07-02"},
                        {"evidence_id": "evidence_n3", "quote": "终于收到礼盒，小狮子很可爱", "note_id": "n3", "post_url": "https://example.com/n3", "post_title": "收到礼盒", "date": "2026-07-03"},
                    ],
                }
            ],
            "uncertainty_table": [],
        },
    )
    html_report = build_report_html(report, {}, {}, "2026-08-18")

    assert "帖子侧信号" not in html_report
    assert "讨论概括" in html_report
    assert "代表性证据" in html_report
    assert 1 <= len(report["main_narratives"][0]["representative_quotes"]) <= 4
    assert "来源索引" in html_report
    # 正文只放短编号，原文引用不带裸链接。
    assert "小狮子盲盒一共有四款 [打开原帖]" not in html_report
    assert report["main_narratives"][0]["evidence"].startswith("E0")
    assert "未采集" in html_report
    assert "相关评论数" not in html_report
    assert "评论采集状态" not in html_report
    assert html_report.count(">未采集</strong>") == 1
