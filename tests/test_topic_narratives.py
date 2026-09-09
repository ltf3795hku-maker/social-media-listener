"""Topic 叙事渲染的当前契约：正文只来自本轮数据，报告层没有语义词表。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from xhs_listener.report import _topic_narratives_v3

# 纯 CJK 词按子串查；ASCII 词必须按词边界查，否则 "SIS" 会命中 "ANALYSIS"。
LEAKED_CJK_TERMS = ("50万", "40万", "小狮子", "盲盒", "VI面试")
LEAKED_ASCII_TERMS = ("27Fall", "27fall", "SIS", "giftbox", "Victor", "HKICPA", "MSc 2.0")
LEAKED_TERMS = LEAKED_CJK_TERMS + LEAKED_ASCII_TERMS


def _leaked_terms_in(source: str) -> list[str]:
    found = [term for term in LEAKED_CJK_TERMS if term in source]
    found += [
        term
        for term in LEAKED_ASCII_TERMS
        if re.search(rf"(?<![0-9A-Za-z]){re.escape(term)}(?![0-9A-Za-z])", source)
    ]
    return found


SRC = Path(__file__).resolve().parents[1] / "src" / "xhs_listener"


def _cluster(label: str, note_ids: list[str], summary_quote: str) -> dict:
    return {
        "label": label,
        "cluster_id": f"cluster_{label}",
        "volume": len(note_ids),
        "engagement_sum": 300,
        "topic_relevance_counts": {"direct": len(note_ids)},
        "narrative_stance_counts": {"neutral": len(note_ids)},
        "note_ids": list(note_ids),
        "evidence_items": [
            {
                "evidence_id": f"evidence_{note_id}",
                "quote": summary_quote,
                "note_id": note_id,
                "post_url": f"https://www.xiaohongshu.com/explore/{note_id}",
                "engagement": 100,
                "post_title": label,
            }
            for note_id in note_ids
        ],
    }


def test_narrative_summary_comes_from_this_run_llm_text() -> None:
    metrics = _cluster("宿舍网络稳定性讨论", ["n1", "n2"], "宿舍晚上经常断网")
    llm = {"cluster_id": metrics["cluster_id"], "summary": "帖子讨论宿舍网络断线与报修等待。"}

    rows = _topic_narratives_v3([llm], {"narrative_table": [metrics]})

    assert rows[0]["summary"] == "帖子讨论宿舍网络断线与报修等待。"


def test_narrative_summary_falls_back_to_label_without_llm_text() -> None:
    metrics = _cluster("宿舍网络稳定性讨论", ["n1", "n2"], "宿舍晚上经常断网")

    rows = _topic_narratives_v3([{"cluster_id": metrics["cluster_id"]}], {"narrative_table": [metrics]})

    # 兜底句只能复述 label 本身，不得引入 label 之外的具体事实。
    assert rows[0]["summary"] == "帖子围绕宿舍网络稳定性讨论展开讨论。"


def test_unrelated_topic_report_body_contains_no_legacy_scenario_facts() -> None:
    """搜索一个与旧招生周期无关的主题，正文不得出现历史场景事实。"""

    metrics = _cluster("宿舍网络稳定性讨论", ["n1", "n2", "n3"], "报修之后两天才恢复")
    llm = {"cluster_id": metrics["cluster_id"], "summary": "帖子讨论宿舍网络断线与报修等待。"}

    rows = _topic_narratives_v3([llm], {"narrative_table": [metrics]})

    blob = " ".join(
        [rows[0]["summary"], rows[0]["narrative"], *[item["quote"] for item in rows[0]["evidence_items"]]]
    )
    for term in LEAKED_TERMS:
        assert term not in blob


def test_report_layer_has_no_scenario_semantic_vocabularies() -> None:
    """report.py 里不允许再出现按具体历史场景写死的语义词表。"""

    leaked = _leaked_terms_in((SRC / "report.py").read_text(encoding="utf-8"))
    assert leaked == [], f"report.py 仍含历史场景词：{leaked}"


def test_analysis_layer_has_no_scenario_semantic_vocabularies() -> None:
    """analyze.py 的语义再解释词表已移除；scope/采集词表另在 models/collect 中。"""

    leaked = _leaked_terms_in((SRC / "analyze.py").read_text(encoding="utf-8"))
    assert leaked == [], f"analyze.py 仍含历史场景词：{leaked}"


def test_hand_written_semantic_helpers_are_gone() -> None:
    """结构性检查：报告/分析层的手写语义系统不得复活。"""

    removed = {
        "report.py": (
            "_SIGNIFICANCE_KEYWORD_GROUPS",
            "_significance_profile",
            "_information_significance",
            "_semantic_report_tokens",
            "_evidence_angles",
            "_neutralize_topic_text",
            "_match_label_row",
            "_natural_topic_cluster_summary",
        ),
        "analyze.py": (
            "_deterministic_narrative_remerge",
            "_similar_narrative_remerge",
            "_narrative_semantic_bucket",
            "_signal_group_label",
            "_uncertainty_information_gap",
            "_canonical_uncertainty_type",
            "_best_uncertainty_evidence_quote",
        ),
    }
    for filename, names in removed.items():
        tree = ast.parse((SRC / filename).read_text(encoding="utf-8"))
        defined = {
            node.name
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        }
        defined.update(
            target.id
            for node in ast.walk(tree)
            if isinstance(node, ast.Assign)
            for target in node.targets
            if isinstance(target, ast.Name)
        )
        for name in names:
            assert name not in defined, f"{filename} 仍定义了 {name}"
