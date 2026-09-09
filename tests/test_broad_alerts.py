"""Broad Mode Alert 全链路测试：annotation → signal_table → alert_table → report grounding。

覆盖两件事：
1. Alert 候选与优先级完全由代码判定（高互动正面/中性话题绝不进入 Alert）；
2. 报告层只接受能在 alert_table 精确命中的 signal_id，并覆盖 LLM 返回的等级/名称。
"""
from __future__ import annotations

from xhs_listener.analysis_prompts import BROAD_MODE_SECTION, build_broad_analysis_prompt
from xhs_listener.analyze import (
    _alert_priority_for_signal,
    _alert_type_for_signal,
    build_alert_table,
    build_positive_signal_table,
    build_signal_table,
)
from xhs_listener.report import (
    _ensure_report_defaults,
    _fallback_alerts_from_table,
    _monitoring_alerts,
    build_report_html,
)


SIGNAL_LABEL = "新生礼盒寄送延迟"
OTHER_LABEL = "校园开放日活动安排"


def _notes(count: int, like_count: int = 10) -> list[dict[str, object]]:
    return [{"note_id": str(index), "like_count": like_count} for index in range(1, count + 1)]


def _annotations(
    count: int,
    *,
    sentiment: str,
    content_type: str,
    risk_level: str = "none",
    risk_type: str = "none",
    label: str = SIGNAL_LABEL,
    start: int = 1,
) -> list[dict[str, object]]:
    return [
        {
            "note_id": str(index),
            "signal_label": label,
            "content_type": content_type,
            "sentiment": sentiment,
            "risk_level": risk_level,
            "risk_type": risk_type,
            "signal_types": ["informational"],
        }
        for index in range(start, start + count)
    ]


def _single_signal(notes: list[dict[str, object]], annotations: list[dict[str, object]]) -> dict[str, object]:
    table = build_signal_table(notes, annotations, {SIGNAL_LABEL: SIGNAL_LABEL, OTHER_LABEL: OTHER_LABEL})
    assert len(table) == 1
    return table[0]


# --------------------------------------------------------------------------
# Alert priority rules
# --------------------------------------------------------------------------


def test_single_high_risk_post_is_high_alert_even_with_low_engagement() -> None:
    signal = _single_signal(
        _notes(1, like_count=3),
        _annotations(1, sentiment="neutral", content_type="concern", risk_level="high", risk_type="operational"),
    )

    assert signal["alert_evidence_count"] == 1
    assert _alert_priority_for_signal(signal) == "high"


def test_repeated_low_risk_complaints_become_high_alert() -> None:
    signal = _single_signal(
        _notes(3, like_count=2),
        _annotations(3, sentiment="negative", content_type="complaint", risk_level="low"),
    )

    assert signal["alert_evidence_count"] == 3
    assert signal["risk_level"] == "low"
    assert _alert_priority_for_signal(signal) == "high"


def test_two_concern_posts_are_medium_alert() -> None:
    signal = _single_signal(
        _notes(2, like_count=5),
        _annotations(2, sentiment="neutral", content_type="concern"),
    )

    assert signal["alert_evidence_count"] == 2
    assert _alert_priority_for_signal(signal) == "medium"


def test_single_weak_negative_post_is_low_alert() -> None:
    signal = _single_signal(
        _notes(1, like_count=5),
        _annotations(1, sentiment="negative", content_type="other"),
    )

    assert signal["alert_evidence_count"] == 1
    assert _alert_priority_for_signal(signal) == "low"


def test_single_medium_risk_post_is_medium_alert() -> None:
    signal = _single_signal(
        _notes(1, like_count=4),
        _annotations(1, sentiment="neutral", content_type="other", risk_level="medium", risk_type="cost_concern"),
    )

    assert _alert_priority_for_signal(signal) == "medium"


def test_positive_viral_topic_never_enters_alert_table() -> None:
    notes = _notes(10, like_count=5000)
    annotations = _annotations(10, sentiment="positive", content_type="positive_advocacy")
    table = build_signal_table(notes, annotations, {SIGNAL_LABEL: SIGNAL_LABEL})

    assert table[0]["mention_count"] == 10
    assert table[0]["engagement_sum"] == 50000
    assert table[0]["alert_evidence_count"] == 0
    assert _alert_priority_for_signal(table[0]) == "none"
    assert build_alert_table(table) == []


def test_neutral_viral_information_topic_never_enters_alert_table() -> None:
    notes = _notes(10, like_count=5000)
    annotations = _annotations(10, sentiment="neutral", content_type="information_sharing")
    table = build_signal_table(notes, annotations, {SIGNAL_LABEL: SIGNAL_LABEL})

    assert table[0]["engagement_sum"] == 50000
    assert table[0]["alert_evidence_count"] == 0
    assert build_alert_table(table) == []


def test_mixed_signal_uses_alert_evidence_count_not_mention_count() -> None:
    notes = _notes(10, like_count=10)
    annotations = _annotations(9, sentiment="positive", content_type="positive_advocacy")
    annotations += _annotations(1, sentiment="negative", content_type="other", start=10)
    table = build_signal_table(notes, annotations, {SIGNAL_LABEL: SIGNAL_LABEL})
    signal = table[0]

    assert signal["mention_count"] == 10
    assert signal["sentiment_counts"] == {"positive": 9, "negative": 1}
    assert signal["alert_evidence_count"] == 1
    # 10 条讨论里只有 1 条负面，不能因为 mention_count=10 就升级为 high。
    assert _alert_priority_for_signal(signal) == "low"


def test_signal_id_is_stable_for_the_same_canonical_label() -> None:
    notes = _notes(2)
    annotations = _annotations(2, sentiment="negative", content_type="complaint")
    first = build_signal_table(notes, annotations, {SIGNAL_LABEL: SIGNAL_LABEL})
    second = build_signal_table(notes, annotations, {SIGNAL_LABEL: SIGNAL_LABEL})
    other = build_signal_table(
        notes,
        _annotations(2, sentiment="negative", content_type="complaint", label=OTHER_LABEL),
        {OTHER_LABEL: OTHER_LABEL},
    )

    assert first[0]["signal_id"] == second[0]["signal_id"]
    assert first[0]["signal_id"]
    assert first[0]["signal_id"] != other[0]["signal_id"]


def test_alert_type_prefers_risk_type_then_content_type() -> None:
    with_risk_type = _single_signal(
        _notes(1),
        _annotations(1, sentiment="negative", content_type="complaint", risk_level="medium", risk_type="cost_concern"),
    )
    without_risk_type = _single_signal(
        _notes(1),
        _annotations(1, sentiment="negative", content_type="complaint"),
    )
    plain_negative = _single_signal(
        _notes(1),
        _annotations(1, sentiment="negative", content_type="other"),
    )

    assert _alert_type_for_signal(with_risk_type) == "cost_concern"
    assert _alert_type_for_signal(without_risk_type) == "complaint"
    assert _alert_type_for_signal(plain_negative) == "negative_discussion"


def test_alert_table_is_sorted_by_code_priority_and_capped() -> None:
    notes = [{"note_id": f"n{index}", "like_count": 10} for index in range(1, 5)]
    annotations = [
        {"note_id": "n1", "signal_label": "A 信号", "sentiment": "negative", "content_type": "other", "risk_level": "none"},
        {"note_id": "n2", "signal_label": "B 信号", "sentiment": "negative", "content_type": "complaint", "risk_level": "high"},
        {"note_id": "n3", "signal_label": "C 信号", "sentiment": "neutral", "content_type": "concern", "risk_level": "none"},
        {"note_id": "n4", "signal_label": "C 信号", "sentiment": "neutral", "content_type": "concern", "risk_level": "none"},
    ]
    label_map = {"A 信号": "A 信号", "B 信号": "B 信号", "C 信号": "C 信号"}
    table = build_alert_table(build_signal_table(notes, annotations, label_map))

    assert [row["signal"] for row in table] == ["B 信号", "C 信号", "A 信号"]
    assert [row["alert_priority"] for row in table] == ["high", "medium", "low"]
    assert build_alert_table(build_signal_table(notes, annotations, label_map), limit=1)[0]["signal"] == "B 信号"


def test_alert_triggers_record_machine_readable_reasons() -> None:
    signal = _single_signal(
        _notes(3, like_count=2),
        _annotations(3, sentiment="negative", content_type="complaint", risk_level="low"),
    )
    row = build_alert_table([signal])[0]

    assert "repeated_alert_evidence" in row["alert_triggers"]
    assert "negative_sentiment" in row["alert_triggers"]
    assert "complaint" in row["alert_triggers"]


# --------------------------------------------------------------------------
# Positive signal table
# --------------------------------------------------------------------------


def test_positive_signal_table_counts_only_positive_evidence() -> None:
    notes = _notes(10, like_count=10)
    annotations = _annotations(9, sentiment="positive", content_type="positive_advocacy")
    annotations += _annotations(1, sentiment="negative", content_type="other", start=10)
    table = build_positive_signal_table(build_signal_table(notes, annotations, {SIGNAL_LABEL: SIGNAL_LABEL}))

    assert table[0]["positive_evidence_count"] == 9
    assert table[0]["positive_priority"] == "high"
    assert table[0]["signal_id"]


def test_marketing_dominated_signal_is_not_a_positive_reputation_candidate() -> None:
    notes = _notes(3, like_count=10)
    annotations = [
        {
            "note_id": str(index),
            "signal_label": SIGNAL_LABEL,
            "sentiment": "positive",
            "content_type": "positive_advocacy",
            "author_type": "agency_marketing",
            "signal_types": ["commercial"],
        }
        for index in range(1, 4)
    ]

    assert build_positive_signal_table(build_signal_table(notes, annotations, {SIGNAL_LABEL: SIGNAL_LABEL})) == []


# --------------------------------------------------------------------------
# Report grounding
# --------------------------------------------------------------------------


def _bundle(alert_table: list[dict[str, object]]) -> dict[str, object]:
    return {"report_mode": "broad_report", "alert_table": alert_table, "signal_table": list(alert_table)}


ALERT_ROW = {
    "signal_id": "signal_abc123",
    "signal": "新生礼盒寄送延迟",
    "alert_priority": "medium",
    "alert_type": "service_friction",
    "alert_triggers": ["concern"],
    "alert_evidence_count": 2,
    "alert_evidence_engagement_sum": 40,
    "mention_count": 2,
    "engagement_sum": 40,
    "risk_reasons": ["多条帖子提到礼盒长时间未寄出"],
    "evidence_items": [{"quote": "到现在还没收到礼盒", "note_id": "n1", "post_url": "https://example.com/n1", "engagement": 20}],
}


def test_alert_with_valid_signal_id_is_kept() -> None:
    rows = _monitoring_alerts(
        {"alerts": [{"signal_id": "signal_abc123", "summary": "多条帖子讨论礼盒寄送进度。", "evidence": "到现在还没收到礼盒"}]},
        _bundle([ALERT_ROW]),
    )

    assert len(rows) == 1
    assert rows[0]["signal_id"] == "signal_abc123"
    assert rows[0]["summary"] == "多条帖子讨论礼盒寄送进度。"


def test_hallucinated_signal_id_is_dropped() -> None:
    rows = _monitoring_alerts(
        {
            "alerts": [
                {"signal_id": "signal_does_not_exist", "signal": "凭空发明的事项", "summary": "编造内容"},
                {"signal_id": "signal_abc123", "summary": "真实信号"},
            ]
        },
        _bundle([ALERT_ROW]),
    )

    assert [row["signal_id"] for row in rows] == ["signal_abc123"]
    assert all("凭空发明的事项" not in row["signal"] for row in rows)


def test_code_overrides_llm_alert_level_and_name() -> None:
    rows = _monitoring_alerts(
        {
            "alerts": [
                {
                    "signal_id": "signal_abc123",
                    "signal": "LLM 自己改写的标题",
                    "alert_level": "high",
                    "alert_type": "reputation",
                    "summary": "摘要",
                }
            ]
        },
        _bundle([ALERT_ROW]),
    )

    assert rows[0]["alert_level"] == "medium"
    assert rows[0]["alert_type"] == "service_friction"
    assert rows[0]["signal"] == "新生礼盒寄送延迟"


def test_duplicate_signal_id_is_emitted_once() -> None:
    rows = _monitoring_alerts(
        {
            "alerts": [
                {"signal_id": "signal_abc123", "summary": "第一次"},
                {"signal_id": "signal_abc123", "summary": "第二次"},
            ]
        },
        _bundle([ALERT_ROW]),
    )

    assert len(rows) == 1
    assert rows[0]["summary"] == "第一次"


def test_empty_llm_alerts_fall_back_to_alert_table() -> None:
    rows = _monitoring_alerts({"alerts": []}, _bundle([ALERT_ROW]))

    assert len(rows) == 1
    assert rows[0]["signal"] == "新生礼盒寄送延迟"
    assert rows[0]["alert_level"] == "medium"
    assert rows[0] == _fallback_alerts_from_table(_bundle([ALERT_ROW]))[0]
    # fallback summary 必须是代码事实句，不得复用 annotation 的推演式 risk_reason。
    assert "本轮共 2 篇帖子提及该信号" in rows[0]["summary"]
    assert "其中 2 篇构成负面或敏感证据" in rows[0]["summary"]
    assert "多条帖子提到礼盒长时间未寄出" not in rows[0]["summary"]
    for banned in ("可能影响", "可能导致", "心理健康", "竞争力", "决策质量"):
        assert banned not in rows[0]["summary"]


def test_empty_alert_table_produces_no_alerts() -> None:
    assert _monitoring_alerts({"alerts": [{"signal_id": "x", "summary": "y"}]}, _bundle([])) == []


def test_alert_rows_are_sorted_by_code_priority_not_llm_order() -> None:
    high_row = {**ALERT_ROW, "signal_id": "signal_high", "signal": "高优先信号", "alert_priority": "high", "alert_evidence_count": 4}
    rows = _monitoring_alerts(
        {"alerts": [{"signal_id": "signal_abc123", "summary": "中"}, {"signal_id": "signal_high", "summary": "高"}]},
        _bundle([ALERT_ROW, high_row]),
    )

    assert [row["alert_level"] for row in rows] == ["high", "medium"]


def test_broad_report_defaults_expose_alerts_and_drop_legacy_key() -> None:
    report = _ensure_report_defaults(
        {},
        analysis={"alerts": [{"signal_id": "signal_abc123", "summary": "礼盒寄送进度被反复追问。"}]},
        processing={"scan_mode": "broad_scan"},
        analysis_bundle=_bundle([ALERT_ROW]),
    )

    assert report["alerts"][0]["signal"] == "新生礼盒寄送延迟"
    assert "risk_urgency_matters" not in report
    assert "content_type_sentiment_summary" not in report


def test_positive_signals_require_signal_id_grounding() -> None:
    bundle = {
        "report_mode": "broad_report",
        "alert_table": [],
        "positive_signal_table": [
            {"signal_id": "signal_pos", "signal": "课程实践性获认可", "positive_evidence_count": 3, "evidence_items": [{"quote": "很实用", "note_id": "n2"}]}
        ],
    }
    report = _ensure_report_defaults(
        {},
        analysis={
            "positive_reputation_signals": [
                {"signal_id": "signal_pos", "signal": "LLM 改写的名字", "summary": "学生反馈课程实践性强。"},
                {"signal_id": "signal_missing", "signal": "凭空正面信号", "summary": "编造"},
            ]
        },
        processing={"scan_mode": "broad_scan"},
        analysis_bundle=bundle,
    )

    assert [row["signal"] for row in report["positive_reputation_signals"]] == ["课程实践性获认可"]
    assert report["positive_reputation_signals"][0]["summary"] == "学生反馈课程实践性强。"


def test_positive_signals_fall_back_to_table_when_llm_returns_nothing() -> None:
    bundle = {
        "report_mode": "broad_report",
        "alert_table": [],
        "positive_signal_table": [
            {"signal_id": "signal_pos", "signal": "课程实践性获认可", "positive_evidence_count": 3, "evidence_items": [{"quote": "很实用", "note_id": "n2"}]}
        ],
    }
    report = _ensure_report_defaults(
        {},
        analysis={"positive_reputation_signals": []},
        processing={"scan_mode": "broad_scan"},
        analysis_bundle=bundle,
    )

    assert report["positive_reputation_signals"][0]["signal"] == "课程实践性获认可"
    assert report["positive_reputation_signals"][0]["evidence"] == "很实用"


def test_key_findings_require_two_real_themes() -> None:
    bundle = {
        "report_mode": "broad_report",
        "alert_table": [],
        "theme_table": [{"theme": "Admissions"}, {"theme": "Course_Selection"}],
        "signal_table": [{"signal_id": "signal_abc123"}],
    }
    report = _ensure_report_defaults(
        {},
        analysis={
            "key_findings_across_themes": [
                {
                    "finding": "跨主题的流程疑问",
                    "supporting_themes": ["Admissions", "Course_Selection"],
                    "supporting_signal_ids": ["signal_abc123", "signal_ghost"],
                },
                {"finding": "只有单一主题支持", "supporting_themes": ["Admissions"]},
                {"finding": "完全没有 theme 支持"},
                {"finding": "引用了不存在的 theme", "supporting_themes": ["Admissions", "Not_A_Theme"]},
            ]
        },
        processing={"scan_mode": "broad_scan"},
        analysis_bundle=bundle,
    )

    assert [row["finding"] for row in report["key_findings_across_themes"]] == ["跨主题的流程疑问"]
    # 不存在的 signal_id 被剔除，有效的保留。
    assert report["key_findings_across_themes"][0]["supporting_signal_ids"] == ["signal_abc123"]


def test_key_findings_return_empty_rather_than_inventing_cross_theme_claims() -> None:
    bundle = {
        "report_mode": "broad_report",
        "alert_table": [],
        "theme_table": [{"theme": "Admissions"}, {"theme": "Course_Selection"}],
        "discussion_table": [{"label": "选课讨论", "evidence_items": [{"quote": "waiting list"}]}],
    }
    report = _ensure_report_defaults(
        {},
        analysis={"key_findings_across_themes": [{"finding": "没有 theme 支持"}]},
        processing={"scan_mode": "broad_scan"},
        analysis_bundle=bundle,
    )

    assert report["key_findings_across_themes"] == []


# --------------------------------------------------------------------------
# Rendering + historical compatibility
# --------------------------------------------------------------------------


def _render(structured: dict[str, object], locale: str = "zh") -> str:
    """当前 run 只产出 report.json + report.html，Markdown 渲染已移除。"""

    return build_report_html(structured, {}, {}, "2026-08-26", locale=locale)


def test_alerts_render_with_attention_wording_not_risk_wording() -> None:
    structured = {
        "title": "Broad 报告",
        "report_mode": "broad_report",
        "generated_scope": {"analysis_notes": 3, "analysis_comments": 0},
        "header_distributions": {},
        "executive_summary": "摘要",
        "alerts": [
            {
                "signal_id": "signal_abc123",
                "signal": "新生礼盒寄送延迟",
                "alert_level": "medium",
                "alert_type": "service_friction",
                "summary": "多条帖子讨论礼盒寄送进度。",
                "evidence": "到现在还没收到礼盒",
                "supporting_quotes": [{"quote": "到现在还没收到礼盒", "note_id": "n1", "post_url": "https://example.com/n1"}],
            }
        ],
        "positive_reputation_signals": [],
        "theme_landscape": [],
        "appendix": {"evidence": [], "methodology": []},
        "data_limitations": [],
    }
    html_report = _render(structured)

    assert "<h2>重点关注</h2>" in html_report
    assert "新生礼盒寄送延迟" in html_report
    assert "服务摩擦" in html_report
    assert "持续观察" in html_report
    for banned in ("风险预警", "风险等级", "风险类型", "高风险", "中风险"):
        assert banned not in html_report


def test_english_alert_section_uses_alert_vocabulary() -> None:
    structured = {
        "title": "Broad report",
        "report_mode": "broad_report",
        "generated_scope": {"analysis_notes": 1, "analysis_comments": 0},
        "header_distributions": {},
        "executive_summary": "summary",
        "alerts": [
            {
                "signal_id": "signal_abc123",
                "signal": "Gift box delivery delay",
                "alert_level": "high",
                "alert_type": "service_friction",
                "summary": "Multiple posts discuss delivery progress.",
                "evidence": "still not received",
                "supporting_quotes": [],
            }
        ],
        "positive_reputation_signals": [],
        "theme_landscape": [],
        "appendix": {"evidence": [], "methodology": []},
        "data_limitations": [],
    }
    html_report = _render(structured, locale="en")

    assert "<h2>Alerts</h2>" in html_report
    assert "Service friction" in html_report
    assert "Priority" in html_report
    assert "Risk Alerts" not in html_report


# --------------------------------------------------------------------------
# Prompt contract
# --------------------------------------------------------------------------


def test_broad_mode_section_describes_alerts_not_risk_matters() -> None:
    for expected in ("alerts", "signal_id", "alert_level", "alert_type", "alert_table", "positive_signal_table", "supporting_themes"):
        assert expected in BROAD_MODE_SECTION
    for removed in ("risk_urgency_matters", "why_it_matters", "content_type_sentiment_summary", "真正风险"):
        assert removed not in BROAD_MODE_SECTION
    assert "不得新增、合并、重复或改写等级" in BROAD_MODE_SECTION


def test_broad_prompt_includes_alert_and_positive_candidate_tables() -> None:
    prompt = build_broad_analysis_prompt(
        processing={"scan_mode": "broad_scan"},
        time_coverage={},
        top_posts=[],
        signal_table=[{"signal_id": "signal_abc123", "signal": "新生礼盒寄送延迟"}],
        alert_table=[{"signal_id": "signal_abc123", "alert_priority": "medium"}],
        positive_signal_table=[{"signal_id": "signal_pos", "signal": "课程实践性获认可"}],
        theme_table=[],
        discussion_table=[],
        comment_signal_table=[],
        narrative_comment_table=[],
        sample_comments=[],
    )

    assert "代码聚合 alert_table" in prompt
    assert "代码聚合 positive_signal_table" in prompt
    assert "代码聚合 signal_table" in prompt
    assert "risk_urgency_matters" not in prompt
    assert "why_it_matters" not in prompt


# --------------------------------------------------------------------------
# End-to-end: analyze_run -> analysis.json -> report_run -> report.json/html
# --------------------------------------------------------------------------


def test_broad_run_end_to_end_produces_grounded_alerts(tmp_path) -> None:
    import json
    import types

    from xhs_listener.analyze import analyze_run
    from xhs_listener.io_utils import write_json, write_jsonl
    from xhs_listener.report import report_run

    notes = [
        {
            "note_id": f"n{index}",
            "keyword": "港大商学院",
            "title": "港大商学院新生礼盒到底什么时候寄",
            "body": "很多同学到现在还没收到礼盒，客服也没有明确答复。",
            "content_full": "港大商学院新生礼盒到底什么时候寄 很多同学到现在还没收到礼盒，客服也没有明确答复。",
            "like_count": 40,
            "collect_count": 0,
            "comment_count": 2,
            "share_count": 0,
            "published_at": "2026-08-20",
            "post_url": f"https://www.xiaohongshu.com/explore/n{index}",
        }
        for index in range(1, 4)
    ]
    notes.append(
        {
            "note_id": "n9",
            "keyword": "港大商学院",
            "title": "港大商学院课程实践性很强",
            "body": "课程案例很实用，收获很大。",
            "content_full": "港大商学院课程实践性很强 课程案例很实用，收获很大。",
            "like_count": 5000,
            "collect_count": 0,
            "comment_count": 0,
            "share_count": 0,
            "published_at": "2026-08-21",
            "post_url": "https://www.xiaohongshu.com/explore/n9",
        }
    )
    write_jsonl(tmp_path / "processed_notes.jsonl", notes)
    write_jsonl(tmp_path / "processed_comments.jsonl", [])
    write_json(tmp_path / "processing.json", {"scan_mode": "broad_scan", "keyword": "broad_scan"})

    annotation_payload = [
        {
            "note_id": f"n{index}",
            "content_type": "concern",
            "theme": "Student_Services",
            "sentiment": "negative",
            "author_type": "real_user",
            "signal_label": "新生礼盒寄送延迟",
            "risk_level": "low",
            "risk_type": "service_friction",
            "risk_reason": "多条帖子提到礼盒长时间未寄出",
            "signal_types": ["operational"],
            "evidence_quote": "到现在还没收到礼盒",
        }
        for index in range(1, 4)
    ] + [
        {
            "note_id": "n9",
            "content_type": "positive_advocacy",
            "theme": "Programme_Experience",
            "sentiment": "positive",
            "author_type": "real_user",
            "signal_label": "课程实践性获认可",
            "risk_level": "none",
            "risk_type": "none",
            "signal_types": ["informational"],
            "evidence_quote": "课程案例很实用",
        }
    ]

    class ScriptedClient:
        """按调用顺序返回：detailed_annotation → signal_merge → 最终分析。"""

        def __init__(self) -> None:
            self.prompts: list[str] = []

        def get_response(self, messages, *args, **kwargs):
            prompt = messages[0]["content"]
            self.prompts.append(prompt)
            if "数据标注助手" in prompt:
                sent = [row for row in annotation_payload if f'"{row["note_id"]}"' in prompt]
                content = json.dumps(sent, ensure_ascii=False)
            elif "signal_label 列表" in prompt:
                content = json.dumps(
                    {
                        "groups": [
                            {"signal": "新生礼盒寄送延迟", "labels": ["新生礼盒寄送延迟"]},
                            {"signal": "课程实践性获认可", "labels": ["课程实践性获认可"]},
                        ]
                    },
                    ensure_ascii=False,
                )
            else:
                alert_table = json.loads(prompt.split("代码聚合 alert_table")[1].split("：\n", 1)[1].split("\n\n", 1)[0])
                positive_table = json.loads(
                    prompt.split("代码聚合 positive_signal_table")[1].split("：\n", 1)[1].split("\n\n", 1)[0]
                )
                content = json.dumps(
                    {
                        "executive_summary": "本轮讨论集中在新生礼盒寄送与课程体验。",
                        "theme_landscape": [{"theme": "Student_Services", "summary": "学生服务讨论", "evidence": "礼盒"}],
                        "key_findings_across_themes": [],
                        "alerts": [
                            {
                                "signal_id": alert_table[0]["signal_id"],
                                "signal": "模型自己改写的标题",
                                "alert_level": "high",
                                "alert_type": "reputation",
                                "summary": "多条帖子追问礼盒寄送进度，评论区也在等待明确答复。",
                                "comment_signal": "",
                                "evidence": "到现在还没收到礼盒",
                                "supporting_quotes": [{"quote": "到现在还没收到礼盒", "note_id": "n1"}],
                            },
                            {"signal_id": "signal_hallucinated", "summary": "凭空发明的 Alert"},
                        ],
                        "positive_reputation_signals": [
                            {
                                "signal_id": positive_table[0]["signal_id"],
                                "signal": "模型改写的正面标题",
                                "summary": "学生反馈课程案例实用。",
                                "comment_signal": "",
                                "evidence": "课程案例很实用",
                            }
                        ],
                        "appendix": {"supporting_evidence": [], "methodology": []},
                        "data_limitations": [],
                    },
                    ensure_ascii=False,
                )
            message = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)

        def usage_dict(self, response):
            return {}

        def extract_json(self, text):
            return json.loads(text)

    bundle = analyze_run(tmp_path, client=ScriptedClient())

    assert [row["signal"] for row in bundle["alert_table"]] == ["新生礼盒寄送延迟"]
    assert bundle["alert_table"][0]["alert_priority"] == "high"  # 3 条重复负面证据
    assert bundle["alert_table"][0]["alert_type"] == "service_friction"
    assert [row["signal"] for row in bundle["positive_signal_table"]] == ["课程实践性获认可"]

    report_run(tmp_path)
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))

    assert "risk_urgency_matters" not in report
    assert len(report["alerts"]) == 1
    assert report["alerts"][0]["signal"] == "新生礼盒寄送延迟"
    assert report["alerts"][0]["alert_type"] == "service_friction"
    assert "模型自己改写的标题" not in json.dumps(report, ensure_ascii=False)
    assert "凭空发明的 Alert" not in json.dumps(report, ensure_ascii=False)
    assert report["positive_reputation_signals"][0]["signal"] == "课程实践性获认可"

    html_report = (tmp_path / "report.html").read_text(encoding="utf-8")
    assert "重点关注" in html_report
    assert "风险预警" not in html_report
    assert "服务摩擦" in html_report




def test_comment_signal_is_matched_by_note_id_not_by_title_similarity() -> None:
    """评论聚合行按 note_id 挂到 signal 上；标题不像也能挂上，标题像但 note 不同则不挂。"""

    source = {**ALERT_ROW, "alert_evidence_note_ids": ["n1"]}
    bundle = {
        **_bundle([source]),
        "narrative_comment_table": [
            {
                "narrative": "完全不同的标题",
                "cluster_id": "cluster_x",
                "note_ids": ["n1"],
                "comment_count": 6,
                "question_count": 2,
                "top_comments": [{"content": "到底什么时候寄", "like_count": 9}],
            },
            {
                "narrative": "新生礼盒寄送延迟",
                "cluster_id": "cluster_y",
                "note_ids": ["n999"],
                "comment_count": 99,
                "question_count": 99,
                "top_comments": [],
            },
        ],
    }

    rows = _monitoring_alerts({"alerts": [{"signal_id": "signal_abc123", "summary": "摘要"}]}, bundle)

    # 命中的是 note_id 相同的那一行（6 条），不是标题一模一样的那一行（99 条）。
    assert rows[0]["comment_count"] == 6
    assert rows[0]["comment_question_count"] == 2


def test_comment_signal_is_empty_when_no_note_id_matches() -> None:
    """挂不上就留空，不用标题词重合去猜。"""

    bundle = {
        **_bundle([{**ALERT_ROW, "alert_evidence_note_ids": ["n1"]}]),
        "narrative_comment_table": [
            {"narrative": "新生礼盒寄送延迟", "cluster_id": "c", "note_ids": ["other"], "comment_count": 50}
        ],
    }

    rows = _monitoring_alerts({"alerts": [{"signal_id": "signal_abc123", "summary": "摘要"}]}, bundle)

    assert rows[0]["comment_count"] == 0
    assert rows[0]["comment_signal"] == ""


def test_alert_supporting_quotes_fall_back_to_grounded_evidence_items() -> None:
    bundle = _bundle([ALERT_ROW])

    rows = _monitoring_alerts({"alerts": [{"signal_id": "signal_abc123", "summary": "摘要"}]}, bundle)
    quote = rows[0]["supporting_quotes"][0]

    assert quote["note_id"] == "n1"
    assert quote["post_url"] == "https://example.com/n1"
    assert quote["quote"] == "到现在还没收到礼盒"
