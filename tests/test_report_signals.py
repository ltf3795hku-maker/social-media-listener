"""Topic 报告选择规则的当前契约测试。

覆盖当前产品行为（不再覆盖已删除的报告层语义词表）：
- 主要叙事仍要求 >= 2 条独立帖子支持，且必须命中真实 cluster_id；
- "其他高关注信号"保留，但语义价值由分析层的 report_worthy 决定，报告层只做
  direct 相关性 / 证据 grounding / 互动阈值 / 数量上限；
- 信息不确定点允许单一来源，报告层不再用关键词判断它是否"够具体"；
- 证据由 LLM 用 evidence_id 选择，报告层校验后渲染；无效 ID 走简单的互动量兜底。
"""
from __future__ import annotations

from xhs_listener.report import (
    MAX_SINGLETON_SIGNALS,
    _selected_evidence_items,
    _topic_narratives_v3,
    _topic_single_post_observations,
    _uncertainty_rows_v3,
)


def narrative_metrics(
    label: str,
    note_ids: list[str],
    *,
    engagement: int,
    relevance: str = "direct",
    quote: str | None = None,
    date: str = "2026-08-20",
    cluster_id: str | None = None,
) -> dict:
    volume = len(note_ids)
    return {
        "label": label,
        "cluster_id": cluster_id or f"cluster_{abs(hash(label)) % 10**8}",
        "field": "primary_narrative",
        "volume": volume,
        "engagement_sum": engagement,
        "topic_relevance_counts": {relevance: volume},
        "narrative_stance_counts": {"neutral": volume},
        "note_ids": list(note_ids),
        "latest_post_date": date,
        "earliest_post_date": date,
        "top_post_share": 0.5,
        "evidence_items": [
            {
                "evidence_id": f"evidence_{note_id}",
                "quote": quote or label,
                "note_id": note_id,
                "post_url": f"https://www.xiaohongshu.com/explore/{note_id}",
                "date": date,
                "engagement": engagement // max(1, volume),
                "post_title": label,
                "signal_label": label,
            }
            for note_id in note_ids
        ],
    }


def llm_narrative(
    metrics: dict,
    summary: str,
    stance: str = "neutral",
    *,
    evidence_ids: list[str] | None = None,
    report_worthy: bool | None = None,
) -> dict:
    row = {
        "cluster_id": metrics["cluster_id"],
        "narrative": metrics["label"],
        "stance": stance,
        "summary": summary,
        "evidence_ids": evidence_ids
        if evidence_ids is not None
        else [item["evidence_id"] for item in metrics["evidence_items"]],
    }
    if report_worthy is not None:
        row["report_worthy"] = report_worthy
    return row


def uncertainty_metrics(
    question: str,
    note_ids: list[str],
    *,
    uncertainty_type: str = "学制变动",
    engagement: int = 3,
    quote: str | None = None,
) -> dict:
    return {
        "uncertainty_id": f"uncertainty_{abs(hash(question)) % 10**8}",
        "title": question,
        "question": question,
        "uncertainty_type": uncertainty_type,
        "count": len(note_ids),
        "support_count": len(note_ids),
        "independent_source_count": len(note_ids),
        "engagement_sum": engagement,
        "note_ids": list(note_ids),
        "supporting_post_ids": list(note_ids),
        "evidence_items": [
            {
                "evidence_id": f"evidence_{note_id}",
                "quote": quote or question,
                "note_id": note_id,
                "post_url": f"https://www.xiaohongshu.com/explore/{note_id}",
                "date": "2026-08-20",
                "engagement": engagement,
                "post_title": question,
            }
            for note_id in note_ids
        ],
    }


def build(rows: list[dict], uncertainties: list[dict] | None = None) -> dict:
    return {"narrative_table": rows, "uncertainty_table": uncertainties or []}


# --- 主要叙事 ---------------------------------------------------------------


def test_repeated_narrative_becomes_main_and_not_singleton():
    metrics = narrative_metrics("港大商学院经管硕士改为两年弹性学制", ["n1", "n2", "n3"], engagement=900)
    bundle = build([metrics])

    narratives = _topic_narratives_v3([llm_narrative(metrics, "多条帖子讨论学制调整。")], bundle)
    singles = _topic_single_post_observations(bundle, [], narratives)

    assert [row["cluster_id"] for row in narratives] == [metrics["cluster_id"]]
    assert narratives[0]["volume"] == 3
    assert singles == []


def test_narrative_requires_two_supporting_posts():
    metrics = narrative_metrics("只有一条帖子的说法", ["n1"], engagement=900)
    bundle = build([metrics])

    assert _topic_narratives_v3([llm_narrative(metrics, "单帖不构成主要讨论。")], bundle) == []


def test_narrative_with_unknown_cluster_id_is_dropped():
    metrics = narrative_metrics("真实叙事", ["n1", "n2"], engagement=500)
    bundle = build([metrics])
    hallucinated = {"cluster_id": "cluster_does_not_exist", "narrative": "编造", "summary": "编造"}

    narratives = _topic_narratives_v3([hallucinated, llm_narrative(metrics, "真实内容。")], bundle)

    assert [row["cluster_id"] for row in narratives] == [metrics["cluster_id"]]


def test_narrative_summary_is_llm_wording_without_rewriting():
    """报告层不再做自动中性化改写，LLM 原话原样呈现。"""

    metrics = narrative_metrics("学费讨论", ["n1", "n2"], engagement=400)
    summary = "学费暴涨导致申请者重新评估风险。"

    narratives = _topic_narratives_v3([llm_narrative(metrics, summary)], build([metrics]))

    assert narratives[0]["summary"] == summary
    for rewritten in ("对应", "关注点", "上涨"):
        assert narratives[0]["summary"].count(rewritten) == summary.count(rewritten)
    assert "key_points" not in narratives[0]


# --- 其他高关注信号（单帖）---------------------------------------------------


def test_high_engagement_singleton_surfaces_as_signal_only():
    main = narrative_metrics("主要讨论", ["n1", "n2"], engagement=200)
    single = narrative_metrics("高互动单帖", ["n9"], engagement=5000)
    bundle = build([main, single])

    narratives = _topic_narratives_v3([llm_narrative(main, "主要讨论内容。")], bundle)
    singles = _topic_single_post_observations(bundle, [], narratives)

    assert [row["cluster_id"] for row in singles] == [single["cluster_id"]]
    assert singles[0]["volume"] == 1


def test_report_worthy_singleton_survives_low_engagement():
    """语义价值由分析层给出，报告层不再用关键词词表判断重要性。"""

    main = narrative_metrics("主要讨论", ["n1", "n2"], engagement=5000)
    single = narrative_metrics("低互动但值得报告", ["n9"], engagement=1)
    bundle = build([main, single])

    without_flag = _topic_single_post_observations(bundle, [], [])
    with_flag = _topic_single_post_observations(
        bundle, [llm_narrative(single, "分析层判定值得报告。", report_worthy=True)], []
    )

    assert single["cluster_id"] not in [row["cluster_id"] for row in without_flag]
    assert single["cluster_id"] in [row["cluster_id"] for row in with_flag]


def test_indirect_singleton_is_excluded_even_when_report_worthy():
    single = narrative_metrics("间接相关单帖", ["n9"], engagement=9000, relevance="indirect")
    bundle = build([single])

    rows = _topic_single_post_observations(
        bundle, [llm_narrative(single, "间接相关。", report_worthy=True)], []
    )

    assert rows == []


def test_singleton_output_is_capped_and_deduped():
    main = narrative_metrics("主要讨论", ["n1", "n2"], engagement=10)
    singles = [narrative_metrics(f"单帖信号{index}", [f"s{index}"], engagement=9000) for index in range(6)]
    bundle = build([main, *singles])

    narratives = _topic_narratives_v3([llm_narrative(main, "主要讨论。")], bundle)
    rows = _topic_single_post_observations(bundle, [], narratives)

    assert len(rows) <= MAX_SINGLETON_SIGNALS
    assert len({row["cluster_id"] for row in rows}) == len(rows)


def test_singleton_already_shown_as_main_narrative_is_not_repeated():
    metrics = narrative_metrics("既是叙事也是单帖", ["n1"], engagement=9000)
    bundle = build([metrics])
    already = [{"cluster_id": metrics["cluster_id"], "narrative": metrics["label"]}]

    assert _topic_single_post_observations(bundle, [], already) == []


# --- 信息不确定点 -----------------------------------------------------------


def test_single_source_uncertainty_is_kept_and_labelled():
    """单一来源的不确定点允许展示，且必须如实标注来源数。"""

    metrics = uncertainty_metrics("两年制是否影响毕业时间", ["n1"])
    bundle = build([], [metrics])

    rows = _uncertainty_rows_v3([{"uncertainty_id": metrics["uncertainty_id"], "summary": "尚无官方说明。"}], bundle)

    assert len(rows) == 1
    assert rows[0]["uncertainty_id"] == metrics["uncertainty_id"]
    assert rows[0]["count"] == 1
    assert rows[0]["support_count"] == 1
    assert rows[0]["is_single_source"] is True
    assert rows[0]["evidence_items"]


def test_generic_single_source_uncertainty_is_no_longer_keyword_gated():
    """决策 2：不再用关键词词表判断单来源问题是否"够具体"。"""

    metrics = uncertainty_metrics("这个项目值不值得申请", ["n1"], uncertainty_type="其他不确定点")
    bundle = build([], [metrics])

    rows = _uncertainty_rows_v3([{"uncertainty_id": metrics["uncertainty_id"], "summary": "样本内没有明确答案。"}], bundle)

    assert [row["uncertainty_id"] for row in rows] == [metrics["uncertainty_id"]]
    assert rows[0]["is_single_source"] is True


def test_multi_source_uncertainty_is_not_marked_single_source():
    metrics = uncertainty_metrics("学费上涨幅度是多少", ["n1", "n2", "n3"])
    bundle = build([], [metrics])

    rows = _uncertainty_rows_v3([{"uncertainty_id": metrics["uncertainty_id"], "summary": "多帖共同缺少信息。"}], bundle)

    assert rows[0]["count"] == 3
    assert rows[0]["is_single_source"] is False


def test_unknown_uncertainty_id_is_dropped():
    metrics = uncertainty_metrics("真实问题", ["n1"])
    bundle = build([], [metrics])

    rows = _uncertainty_rows_v3(
        [{"uncertainty_id": "uncertainty_ghost", "summary": "编造"}, {"uncertainty_id": metrics["uncertainty_id"], "summary": "真实"}],
        bundle,
    )

    assert [row["uncertainty_id"] for row in rows] == [metrics["uncertainty_id"]]


# --- 证据选择 ---------------------------------------------------------------


def test_llm_selected_evidence_ids_are_validated_and_rendered():
    metrics = narrative_metrics("证据选择", ["n1", "n2", "n3"], engagement=300)
    chosen = metrics["evidence_items"][2]["evidence_id"]

    items = _selected_evidence_items(metrics, {"evidence_ids": [chosen]}, fallback_limit=3)

    assert [item["evidence_id"] for item in items] == [chosen]


def test_invalid_evidence_ids_fall_back_to_engagement_order():
    metrics = narrative_metrics("证据兜底", ["n1", "n2"], engagement=300)

    items = _selected_evidence_items(metrics, {"evidence_ids": ["evidence_ghost"]}, fallback_limit=1)

    assert items, "无效 ID 时必须有确定性兜底，而不是空证据"
    assert all(item["evidence_id"].startswith("evidence_") for item in items)
    # 兜底只按互动量排序，不做语义挑选。
    assert items[0]["engagement"] >= items[-1]["engagement"]


def test_duplicate_evidence_ids_are_emitted_once():
    metrics = narrative_metrics("重复证据", ["n1", "n2"], engagement=300)
    chosen = metrics["evidence_items"][0]["evidence_id"]

    items = _selected_evidence_items(metrics, {"evidence_ids": [chosen, chosen]}, fallback_limit=2)

    assert [item["evidence_id"] for item in items] == [chosen]


# --- 主要讨论覆盖度：LLM 漏报不得让 cluster 消失 -----------------------------


def test_eligible_cluster_survives_when_llm_omits_its_cluster_id():
    """资格由代码聚合决定；LLM 没提到也照样呈现，并标记摘要缺失。"""

    covered = narrative_metrics("被 LLM 覆盖的讨论", ["n1", "n2"], engagement=900)
    omitted = narrative_metrics("LLM 漏掉的讨论", ["n3", "n4"], engagement=500)
    bundle = build([covered, omitted])

    rows = _topic_narratives_v3([llm_narrative(covered, "AI 写的摘要。")], bundle)

    ids = [row["cluster_id"] for row in rows]
    assert covered["cluster_id"] in ids
    assert omitted["cluster_id"] in ids, "有资格的 cluster 不能因为 LLM 没提就消失"
    by_id = {row["cluster_id"]: row for row in rows}
    assert by_id[covered["cluster_id"]]["llm_summary_missing"] is False
    assert by_id[omitted["cluster_id"]]["llm_summary_missing"] is True
    # 兜底摘要只复述 label，不编造事实。
    assert by_id[omitted["cluster_id"]]["summary"] == "帖子围绕LLM 漏掉的讨论展开讨论。"
    assert by_id[omitted["cluster_id"]]["evidence_items"]


def test_incomplete_llm_narrative_output_is_reported_in_data_limitations():
    from xhs_listener.report import _ensure_report_defaults

    covered = narrative_metrics("被覆盖", ["n1", "n2"], engagement=900)
    omitted = narrative_metrics("被漏掉", ["n3", "n4"], engagement=500)

    report = _ensure_report_defaults(
        {},
        analysis={"main_narratives": [llm_narrative(covered, "AI 摘要。")], "questions_uncertainties": []},
        processing={"scan_mode": "topic_scan", "keyword": "港大"},
        analysis_bundle={"report_mode": "topic_report", "narrative_table": [covered, omitted]},
    )

    assert len(report["main_narratives"]) == 2
    assert any("未获得 AI 摘要" in item for item in report["data_limitations"])


def test_duplicate_and_unknown_llm_cluster_ids_are_validated():
    metrics = narrative_metrics("唯一讨论", ["n1", "n2"], engagement=400)
    bundle = build([metrics])

    rows = _topic_narratives_v3(
        [
            {"cluster_id": "cluster_ghost", "summary": "编造的 cluster"},
            llm_narrative(metrics, "第一次出现。"),
            llm_narrative(metrics, "重复出现，应被忽略。"),
        ],
        bundle,
    )

    assert [row["cluster_id"] for row in rows] == [metrics["cluster_id"]]
    assert rows[0]["summary"] == "第一次出现。"
    assert "编造的 cluster" not in rows[0]["summary"]


def test_ineligible_clusters_are_still_excluded():
    """覆盖度提升不等于放宽资格：单帖 / 非 direct 仍不进主要讨论。"""

    single = narrative_metrics("单帖", ["n1"], engagement=9000)
    indirect = narrative_metrics("间接相关", ["n2", "n3"], engagement=9000, relevance="indirect")
    bundle = build([single, indirect])

    assert _topic_narratives_v3([], bundle) == []
