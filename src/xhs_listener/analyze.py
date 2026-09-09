"""第三步：对 processed 数据做 LLM 标注和聚合分析。"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from xhs_listener.analysis_prompts import (
    BROAD_POST_ANNOTATION_FIELDS,
    RELEVANCE_FIELDS,
    SIGNAL_TYPE_RULES,
    TOPIC_POST_ANNOTATION_FIELDS,
    build_broad_analysis_prompt,
    build_topic_analysis_prompt,
)
from xhs_listener.io_utils import read_json, read_jsonl, write_json, write_jsonl
from xhs_listener.json_utils import walk_nodes
from xhs_listener.log_utils import emit_log, finish_log_queue, timestamped
from xhs_listener.models import HKU_SCOPE_PATTERN
from xhs_listener.number_utils import to_int


THEME_VALUES = {
    "Admissions",
    "Course_Selection",
    "Teaching_Quality",
    "Academic_Workload",
    "Programme_Experience",
    "Career_Outcomes",
    "Internships",
    "Student_Services",
    "Accommodation",
    "Campus_Life",
    "Scholarships",
    "Reputation",
    "Other",
}

CONTENT_TYPE_VALUES = {
    "complaint",
    "concern",
    "question",
    "information_sharing",
    "positive_advocacy",
    "other",
}

HKU_ALIASES = ("university of hong kong", "香港大学", "hku.hk", "港大", "hku")


# ---------------------------------------------------------------------------
# Broad Scan monitoring scope
#
# 设计原则：
# 1. 明确提到 HKU Business School / 经管学院 / 商科 -> direct
# 2. 没提学院，但明确提到 HKU + 监测范围内的具体硕士项目 -> indirect
# 3. “港大 + 硕士 / 申请 / 就业 / 体验 / 学费”等泛表达本身不够
#
# 这样可以安全使用“港大硕士 / 港大体验 / 港大就业”这类 broad retrieval
# keyword，而不会把文学院、法律、医学等无关内容大量带进后续 LLM。
# ---------------------------------------------------------------------------

HKUBS_DIRECT_TERMS = (
    "hkubs",
    "hku business school",
    "香港大学商学院",
    "港大商学院",
    "香港大学经管学院",
    "港大经管学院",
    "香港大学经管",
    "港大经管",
    "hku 商学院",
    "hku商学院",
    "hku 经管",
    "hku经管",
    "hku 商科",
    "hku商科",
    "港大商科",
    "香港大学商科",
)

HKUBS_CONTEXT_TERMS = (
    "business school",
    "商学院",
    "商科",
    "经管学院",
    "经管",
)


# 市场部提供的 Programme-related monitoring scope。
#
# key = canonical programme label，方便之后如果要增加 programme-level reporting
# aliases = 小红书上可能出现的中英文、简称、旧称或非正式叫法
#
# 注意：
# - 不使用单独的 “BA” / “CS” 等过短缩写，避免误判。
# - “理学硕士 / MSc / Master” 不单独作为 programme evidence，因为范围过宽。
# - 最终仍要求帖子本身同时存在 HKU/港大语境。
MONITORED_PROGRAMMES: dict[str, tuple[str, ...]] = {

    # ------------------------------------------------------------------
    # HKU Business School / business-related programmes
    # ------------------------------------------------------------------

    "Accounting Analytics": (
        "master of accounting analytics",
        "accounting analytics",
        "maa",
        "会计数据分析硕士",
        "会计分析硕士",
        "香港大学会计数据分析硕士",
        "港大会计数据分析硕士",
        "hku会计数据分析硕士",
        "香港大学会计分析硕士",
        "港大会计分析硕士",
        "hku会计分析硕士",
    ),

    "Artificial Intelligence in Business": (
        "master of artificial intelligence in business",
        "artificial intelligence in business",
        "maib",
        "商业人工智能",
        "商业人工智能硕士",
        "香港大学商业人工智能",
        "香港大学商业人工智能硕士",
        "港大商业人工智能",
        "港大商业人工智能硕士",
        "hku商业人工智能",
        "hku商业人工智能硕士",
        "hkumaib",
        "hku maib",
        "港大maib",
        "港大商业ai",
        "hku business ai",
    ),

    "Finance in Financial Technology": (
        "master of finance in financial technology",
        "finance in financial technology",
        "mffintech",
        "金融学金融科技硕士",
        "金融学(金融科技)硕士",
        "金融科技金融学硕士",
        "香港大学金融学(金融科技)硕士",
        "港大金融学(金融科技)硕士",
        "hku金融学(金融科技)硕士",
        "香港大学金融科技金融学硕士",
        "港大金融科技金融学硕士",
        "hku金融科技金融学硕士",
    ),

    "Sustainable Accounting and Finance": (
        "master of sustainable accounting and finance",
        "sustainable accounting and finance",
        "msaf",
        "可持续会计及金融硕士",
        "可持续会计与金融硕士",
        "香港大学可持续会计及金融硕士",
        "港大可持续会计及金融硕士",
        "hku可持续会计及金融硕士",
        "香港大学可持续会计与金融硕士",
        "港大可持续会计与金融硕士",
        "hku可持续会计与金融硕士",
    ),

    "Business Analytics": (
        "master of science in business analytics",
        "msc business analytics",
        "mscba",
        "msc(ba)",
        "business analytics",
        "商业分析理科硕士",
        "商业分析理学硕士",
        "商业分析学硕士",
        "商业分析硕士",
        "香港大学商业分析理科硕士",
        "香港大学商业分析理学硕士",
        "香港大学商业分析学硕士",
        "香港大学商业分析硕士",
        "港大商业分析理科硕士",
        "港大商业分析理学硕士",
        "港大商业分析学硕士",
        "港大商业分析硕士",
        "hku商业分析理科硕士",
        "hku商业分析理学硕士",
        "hku商业分析学硕士",
        "港大mscba",
        "hku mscba",
        "hkuba硕士",
        "港大ba硕士",
        "港大ba",
        "hku ba",
    ),

    "Marketing": (
        "master of science in marketing",
        "msc marketing",
        "mscmktg",
        "msc(mktg)",
        "marketing",
        "市场营销理科硕士",
        "市场营销理学硕士",
        "市场营销学硕士",
        "市场营销硕士",
        "香港大学市场营销理科硕士",
        "香港大学市场营销理学硕士",
        "香港大学市场营销学硕士",
        "香港大学市场营销硕士",
        "港大市场营销理科硕士",
        "港大市场营销理学硕士",
        "港大市场营销学硕士",
        "港大市场营销硕士",
        "hku市场营销理科硕士",
        "hku市场营销理学硕士",
        "hku市场营销学硕士",
        "hku市场营销硕士",
        "港大marketing",
        "hku marketing",
    ),

    "Family Wealth Management": (
        "master of family wealth management",
        "family wealth management",
        "mfwm",
        "fwm",
        "家族财富管理硕士",
        "香港大学家族财富管理硕士",
        "港大家族财富管理硕士",
        "hku家族财富管理硕士",
        "港大fwm",
        "hku fwm",
    ),

    "Wealth Management": (
        "master of wealth management",
        "wealth management",
        "mwm",
        "财富管理硕士",
        "香港大学财富管理硕士",
        "港大财富管理硕士",
        "hku财富管理硕士",
    ),

    "Global Management": (
        "master of global management",
        "global management",
        "mgm",
        "环球管理",
        "环球管理学硕士",
        "环球管理硕士",
        "全球管理",
        "全球管理硕士",
        "香港大学环球管理学硕士",
        "香港大学环球管理硕士",
        "香港大学全球管理硕士",
        "港大环球管理学硕士",
        "港大环球管理硕士",
        "港大全球管理硕士",
        "hku环球管理学硕士",
        "hku环球管理硕士",
        "hku全球管理硕士",
        "港大mgm",
        "hku mgm",
    ),

    "Economics": (
        "master of economics",
        "mecon",
        "经济学硕士",
        "香港大学经济学硕士",
        "港大经济学硕士",
        "hku经济学硕士",
        "港大经济硕士",
        "hku economics",
    ),

    "Finance": (
        "master of finance",
        "finance",
        "mfin",
        "金融",
        "金融学硕士",
        "金融硕士",
        "香港大学金融学硕士",
        "香港大学金融硕士",
        "港大金融学硕士",
        "港大金融硕士",
        "hku金融学硕士",
        "hku金融硕士",
        "港大mfin",
        "hku mfin",
    ),

    "Accounting": (
        "master of accounting",
        "macct",
        "会计学硕士",
        "会计硕士",
        "香港大学会计学硕士",
        "香港大学会计硕士",
        "港大会计学硕士",
        "港大会计硕士",
        "hku会计学硕士",
        "hku会计硕士",
        "港大macct",
        "hku macct",
    ),

    "Business Administration": (
        "master of business administration",
        "business administration",
        "mba",
        "imba",
        "工商管理硕士",
        "香港大学工商管理硕士",
        "港大工商管理硕士",
        "hku工商管理硕士",
        "hku mba",
        "港大mba",
        "香港大学mba",
        "香港大学imba",
        "港大imba",
        "hku imba",
        "港大复旦mba",
        "港大复旦imba",
        "复旦hku imba",
        "hku-fudan imba",
    ),

    "Management": (
        "管理学硕士",
        "香港大学管理学硕士",
        "港大管理学硕士",
        "hku管理学硕士",
    ),

    # ------------------------------------------------------------------
    # Other programmes explicitly included in the monitoring brief
    # ------------------------------------------------------------------

    "Social Data Analytics": (
        "master of social sciences in social data analytics",
        "social data analytics",
        "msocsc sda",
        "msocsc(sda)",
        "社会数据分析硕士",
        "香港大学社会数据分析硕士",
        "港大社会数据分析硕士",
        "hku社会数据分析硕士",
    ),

    "Artificial Intelligence and Society": (
        "artificial intelligence and society",
        "人工智能与社会硕士",
        "香港大学人工智能与社会硕士",
        "港大人工智能与社会硕士",
        "hku人工智能与社会硕士",
    ),

    "Artificial Intelligence": (
        "master of science in artificial intelligence",
        "msc artificial intelligence",
        "msc(ai)",
        "mscai",
        "人工智能硕士",
        "ai硕士",
        "香港大学人工智能硕士",
        "港大人工智能硕士",
        "hku人工智能硕士",
        "港大ai硕士",
        "hkuai硕士",
        "hku ai硕士",
    ),

    "Robotics and Intelligent Systems": (
        "master of science in engineering in robotics and intelligent systems",
        "robotics and intelligent systems",
        "msceng ris",
        "msc(eng)(ris)",
        "机器人与智能系统硕士",
        "智能机器人工程硕士",
        "香港大学机器人与智能系统硕士",
        "港大机器人与智能系统硕士",
        "hku机器人与智能系统硕士",
        "香港大学智能机器人工程硕士",
        "港大智能机器人工程硕士",
        "hku智能机器人工程硕士",
    ),

    "Financial Technology and Data Analytics": (
        "master of science in financial technology and data analytics",
        "financial technology and data analytics",
        "mscftda",
        "msc(ftda)",
        "金融科技与数据分析硕士",
        "香港大学金融科技与数据分析硕士",
        "港大金融科技与数据分析硕士",
        "hku金融科技与数据分析硕士",
    ),

    "Data Science": (
        "master of data science",
        "mdasc",
        "data science",
        "数据科学硕士",
        "香港大学数据科学硕士",
        "港大数据科学硕士",
        "hku数据科学硕士",
        "港大data science",
        "hku data science",
    ),

    "Statistics": (
        "master of statistics",
        "mstat",
        "statistics硕士",
        "统计学硕士",
        "香港大学统计学硕士",
        "港大统计学硕士",
        "hku统计学硕士",
        "港大mstat",
        "hku mstat",
    ),

    "Sociology": (
        "master of social sciences in sociology",
        "sociology硕士",
        "社会学硕士",
        "香港大学社会学硕士",
        "港大社会学硕士",
        "hku社会学硕士",
        "港大sociology",
        "hku sociology",
    ),

    "Management and Marketing": (
        "management and marketing",
        "管理与营销理学硕士",
        "香港大学管理与营销理学硕士",
        "港大管理与营销理学硕士",
        "hku管理与营销理学硕士",
    ),
}


# Topic Scan 同义词保持原设计，不参与 Broad programme scope 判断。
# Broad relevance 逻辑已改变，因此必须升级 schema version，
# 防止旧 relevance/annotation cache 被错误复用。
ANNOTATION_SCHEMA_VERSION = "broad_theme_v3"
TOPIC_ANNOTATION_SCHEMA_VERSION = "topic_narrative_v5"
# 医疗 / 产科内容即便带“港大”也在商学院范围外（例如港大 ICM 产房参观）。
HKUBS_OFFSCOPE_TERMS = (
    "产房", "产检", "孕", "待产", "妇产", "产科", "分娩", "医院", "门诊", "病房",
)

def _annotation_schema_version(scan_mode: str) -> str:
    return ANNOTATION_SCHEMA_VERSION if scan_mode == "broad_scan" else TOPIC_ANNOTATION_SCHEMA_VERSION

def analyze_run(
    run_dir: str | Path,
    client: Optional[Any] = None,
    log_queue: Optional[Any] = None,
    stop_checker: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    try:
        return _analyze_run_impl(run_dir, client, log_queue, stop_checker)
    except Exception as exc:
        try:
            write_json(
                Path(run_dir) / "analysis_error.json",
                {
                    "stage": "analysis",
                    "error_type": type(exc).__name__,
                    "error": str(exc),
                    "failed_at": datetime.now().isoformat(timespec="seconds"),
                },
            )
        except Exception:  # noqa: BLE001
            pass
        raise
    finally:
        finish_log_queue(log_queue)


def _analyze_run_impl(
    run_dir: str | Path,
    client: Optional[Any] = None,
    log_queue: Optional[Any] = None,
    stop_checker: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """分析一个 run 目录；前端无论采集/上传来源，最后都应整理成这个目录结构。"""

    run_path = Path(run_dir)
    logs: list[str] = []
    if client is None:
        from xhs_listener.llm_client import LLMClient

        llm = LLMClient()
    else:
        llm = client
    _log(logs, log_queue, f"Analysis start run_dir={run_path}")

    processing = read_json(run_path / "processing.json")
    notes = read_jsonl(run_path / "processed_notes.jsonl")
    comments = read_jsonl(run_path / "processed_comments.jsonl")
    if not notes:
        raise ValueError("processed_notes.jsonl is empty; run process step first")

    records = _build_annotation_records(notes, comments)
    _log(logs, log_queue, f"Annotation records={len(records)}")
    scan_mode = str(processing.get("scan_mode") or "")

    # 第一轮 LLM：逐条标注。这里关心每条笔记是什么、是否相关、表达了什么信号。
    # 如果上次因为预算中断但 annotations 已完整落盘，就直接复用，避免重复烧 token。
    cached_annotations = _load_cached_annotations(run_path / "annotations.jsonl", records, scan_mode, logs, log_queue)
    if cached_annotations is not None:
        annotations = cached_annotations
        usage_rows: list[dict[str, Any]] = []
    else:
        annotations, usage_rows = annotate_records(records, processing, llm, logs, log_queue, run_path, stop_checker)
    annotations = _validate_annotation_evidence_quotes(annotations, notes)
    if cached_annotations is None:
        write_jsonl(run_path / "annotations.jsonl", annotations)
        partial_path = run_path / "annotations.partial.jsonl"
        if partial_path.exists():
            partial_path.replace(run_path / "annotations.partial.done.jsonl")

    topic_terms = [] if scan_mode == "broad_scan" else _topic_terms_from_keyword(str(processing.get("keyword") or ""))
    relevant_ids = _analysis_relevant_ids(annotations, scan_mode)
    notes_for_analysis = [note for note in notes if str(note.get("note_id")) in relevant_ids]
    comments_for_analysis = [comment for comment in comments if str(comment.get("note_id")) in relevant_ids]

    # Broad 合并 signal_label；Topic 改为合并 primary_narrative。
    # 两种模式只运行其中一个轻量 label-only 调用，不增加额外 LLM 阶段。
    relevant_annotations = [row for row in annotations if str(row.get("note_id")) in relevant_ids]
    if scan_mode == "broad_scan":
        label_map = _merge_signal_labels(relevant_annotations, llm, logs, log_queue, usage_rows)
        narrative_map: dict[str, str] = {}
    else:
        label_map = {}
        narrative_map = _merge_narrative_labels(
            relevant_annotations,
            llm,
            logs,
            log_queue,
            usage_rows,
            notes_for_analysis,
        )

    # 代码聚合：Broad 保留信号/主题全景；Topic 只保留 narrative + comments + uncertainty。
    if scan_mode == "broad_scan":
        signal_table = build_signal_table(notes_for_analysis, annotations, label_map)
        # Alerts / Positive Signals 的候选完全由代码选定，最终 LLM 只能给已选中的 signal 写文字。
        alert_table = build_alert_table(signal_table)
        positive_signal_table = build_positive_signal_table(signal_table)
        theme_table = build_theme_table(notes_for_analysis, annotations)
        discussion_table = build_discussion_table(notes_for_analysis, annotations, scan_mode, narrative_map)
        narrative_table: list[dict[str, Any]] = []
        uncertainty_table: list[dict[str, Any]] = []
        comment_signal_table = build_comment_signal_table(comments_for_analysis, notes_for_analysis, annotations)
        _log(
            logs,
            log_queue,
            f"Broad alert candidates={len(alert_table)} positive candidates={len(positive_signal_table)} signals={len(signal_table)}",
        )
    else:
        signal_table = []
        alert_table = []
        positive_signal_table = []
        theme_table = []
        discussion_table = []
        narrative_table = build_discussion_table(notes_for_analysis, annotations, scan_mode, narrative_map)
        uncertainty_table = []
        comment_signal_table = []
    narrative_comment_table = build_narrative_comment_table(
        comments_for_analysis,
        notes_for_analysis,
        annotations,
        scan_mode,
        narrative_map,
    )
    if scan_mode != "broad_scan":
        uncertainty_table = build_uncertainty_table(notes_for_analysis, annotations, narrative_comment_table)
    time_coverage = _time_coverage(notes_for_analysis)
    _log(
        logs,
        log_queue,
        f"Semantic filter notes={len(notes)}->{len(notes_for_analysis)} comments={len(comments)}->{len(comments_for_analysis)}",
    )

    analysis_result = run_analysis_agent(
        notes_for_analysis,
        comments_for_analysis,
        annotations,
        processing,
        signal_table,
        alert_table,
        positive_signal_table,
        theme_table,
        discussion_table,
        narrative_table,
        uncertainty_table,
        comment_signal_table,
        narrative_comment_table,
        llm,
        time_coverage,
    )
    usage_rows.append(analysis_result.pop("_usage", {}))
    result = {
        "keyword": processing.get("keyword"),
        "run_dir": str(run_path),
        "input_notes": len(notes),
        "input_comments": len(comments),
        "annotated_notes": len(annotations),
        "analysis_notes": len(notes_for_analysis),
        "analysis_comments": len(comments_for_analysis),
        "annotation_summary": _annotation_summary(relevant_annotations, scan_mode),
        "annotation_summary_relevant": _annotation_summary(relevant_annotations, scan_mode),
        "annotation_summary_all": _annotation_summary(annotations, scan_mode),
        "topic_filter": {
            "terms": topic_terms,
            "mode": "broad_hku_signal_scan" if scan_mode == "broad_scan" else "topic_relevance_filter",
        },
        "report_mode": "broad_report" if scan_mode == "broad_scan" else "topic_report",
        "signal_table": signal_table,
        "alert_table": alert_table,
        "positive_signal_table": positive_signal_table,
        "theme_table": theme_table,
        "discussion_table": discussion_table,
        "narrative_table": narrative_table,
        "uncertainty_table": uncertainty_table,
        "comment_signal_table": comment_signal_table,
        "narrative_comment_table": narrative_comment_table,
        "time_coverage": time_coverage,
        "analysis": analysis_result,
        "logs": logs,
    }

    write_json(run_path / "analysis.json", result)
    write_json(run_path / "llm_usage.json", {"usage": _merge_usage_rows(run_path / "llm_usage.json", usage_rows)})
    write_jsonl(run_path / "analysis_log.jsonl", [{"message": line} for line in logs])
    return result


def annotate_records(
    records: list[dict[str, Any]],
    processing: dict[str, Any],
    client: Any,
    logs: list[str],
    log_queue: Optional[Any],
    run_path: Path,
    stop_checker: Optional[Callable[[], None]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """两阶段 LLM 标注：先相关性过闸，再只对相关帖子做完整业务标签。"""

    scan_mode = str(processing.get("scan_mode") or "")
    batch_size = _annotation_batch_size(scan_mode)
    gate_batch_size = _relevance_batch_size(scan_mode)

    relevance_rows, relevance_usage = annotate_relevance_records(
        records,
        processing,
        client,
        logs,
        log_queue,
        run_path,
        gate_batch_size,
        stop_checker,
    )
    usage_rows: list[dict[str, Any]] = relevance_usage
    relevance_by_id = {str(row.get("note_id") or ""): row for row in relevance_rows}
    relevant_records = [
        row
        for row in records
        if _is_relevant_for_detail(relevance_by_id.get(str(row.get("note_id") or "")), scan_mode)
    ]
    blocked_count = len(records) - len(relevant_records)
    _log(logs, log_queue, f"Relevance gate kept={len(relevant_records)} blocked={blocked_count}")
    relevant_ids = {str(row.get("note_id") or "") for row in relevant_records}

    partial_path = run_path / "annotations.partial.jsonl"
    outputs = _load_partial_annotations(partial_path, records, scan_mode=scan_mode)
    output_by_id = {str(row.get("note_id") or ""): row for row in outputs}
    for record in records:
        note_id = str(record.get("note_id") or "")
        if note_id in output_by_id or note_id in relevant_ids:
            continue
        output_by_id[note_id] = _annotation_from_relevance(relevance_by_id.get(note_id), note_id, scan_mode)

    pending_records = [row for row in relevant_records if str(row.get("note_id") or "") not in output_by_id]
    total_batches = (len(pending_records) + batch_size - 1) // batch_size if pending_records else 0
    if outputs:
        _log(logs, log_queue, f"Reused partial annotations count={len(outputs)}")

    for start in range(0, len(pending_records), batch_size):
        _check_stop(stop_checker)
        batch = pending_records[start : start + batch_size]
        batch_no = start // batch_size + 1
        _log(logs, log_queue, f"Detailed annotation batch {batch_no}/{max(total_batches, 1)} size={len(batch)}")
        detailed_batch = [
            {**row, "relevance_gate": relevance_by_id.get(str(row.get("note_id") or ""), {})}
            for row in batch
        ]
        annotation_fields = BROAD_POST_ANNOTATION_FIELDS if scan_mode == "broad_scan" else TOPIC_POST_ANNOTATION_FIELDS
        signal_rules = SIGNAL_TYPE_RULES if scan_mode == "broad_scan" else ""

        # prompt 明确要求只输出 JSON 数组；返回后仍会做 note_id 校验和 fallback，
        # 防止模型漏标、重复标或返回不存在的 note_id。
        prompt = f"""
你是小红书 HKU 公开内容洞察的数据标注助手。
输入的帖子已全部通过相关性判断（见每条的 relevance_gate 字段）。
请保留 relevance_gate 中的 hku_relevance/topic_relevance，不要重判，只补充业务标签。
不要处理没有出现在输入中的 note_id。

{annotation_fields}
{signal_rules}

只输出 JSON 数组，不要解释。

输入数据：
{json.dumps(detailed_batch, ensure_ascii=False)}
"""
        payload = _llm_json_call(client, prompt, "detailed_annotation", usage_rows)
        if not isinstance(payload, list):
            raise RuntimeError("Annotation response must be a JSON list")
        batch_outputs = _validate_annotation_batch(batch, payload, scan_mode, logs, log_queue, batch_no, relevance_by_id)
        outputs.extend(batch_outputs)
        output_by_id.update({str(row.get("note_id") or ""): row for row in batch_outputs})
        # 每个 batch 完成就落盘；后续重跑可复用，避免“已花 token 但无文件”。
        write_jsonl(partial_path, [output_by_id[str(row.get("note_id") or "")] for row in records if str(row.get("note_id") or "") in output_by_id])
        _log(logs, log_queue, f"Detailed annotation saved partial={len(output_by_id)}/{len(records)}")

    return [output_by_id[str(row.get("note_id") or "")] for row in records], usage_rows


def _relevance_batch_size(scan_mode: str) -> int:
    default = "15" if scan_mode == "broad_scan" else "5"
    return max(1, int(os.getenv("XHS_RELEVANCE_BATCH_SIZE", os.getenv("XHS_ANNOTATION_BATCH_SIZE", default))))


def _annotation_batch_size(scan_mode: str) -> int:
    default = "6" if scan_mode == "broad_scan" else "3"
    return max(1, int(os.getenv("XHS_ANNOTATION_BATCH_SIZE", default)))


def annotate_relevance_records(
    records: list[dict[str, Any]],
    processing: dict[str, Any],
    client: Any,
    logs: list[str],
    log_queue: Optional[Any],
    run_path: Path,
    batch_size: int,
    stop_checker: Optional[Callable[[], None]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """第一阶段：HKU relevance 用代码规则，Topic Scan 再用轻量 gate 判断主题相关。"""

    scan_mode = str(processing.get("scan_mode") or "")
    path = run_path / "relevance_annotations.jsonl"
    outputs = _load_partial_relevance(path, records, scan_mode)
    output_by_id = {str(row.get("note_id") or ""): row for row in outputs}

    code_gated = 0
    for record in records:
        note_id = str(record.get("note_id") or "")
        if not note_id or note_id in output_by_id:
            continue
        hku_row = _code_hku_relevance(record, scan_mode)
        if scan_mode == "broad_scan" or hku_row["hku_relevance"] == "unrelated":
            output_by_id[note_id] = hku_row
            code_gated += 1
    if code_gated:
        write_jsonl(path, [output_by_id[str(row.get("note_id") or "")] for row in records if str(row.get("note_id") or "") in output_by_id])
        _log(logs, log_queue, f"HKU relevance code-gated={code_gated} (no LLM for HKU relevance)")

    pending_records = []
    for row in records:
        note_id = str(row.get("note_id") or "")
        if note_id in output_by_id:
            continue
        hku_row = _code_hku_relevance(row, scan_mode)
        pending_records.append({**row, "code_relevance": hku_row})
    total_batches = (len(pending_records) + batch_size - 1) // batch_size if pending_records else 0
    usage_rows: list[dict[str, Any]] = []
    if outputs:
        _log(logs, log_queue, f"Reused relevance gate count={len(outputs)}")

    for start in range(0, len(pending_records), batch_size):
        _check_stop(stop_checker)
        batch = pending_records[start : start + batch_size]
        gate_batch = [_compact_relevance_record(row) for row in batch]
        batch_no = start // batch_size + 1
        _log(logs, log_queue, f"Relevance gate batch {batch_no}/{max(total_batches, 1)} size={len(batch)}")
        prompt = f"""
你是小红书 HKU 公开内容洞察的 Relevance Gate。

范围说明：
{_annotation_relevance_rules(processing)}

{RELEVANCE_FIELDS}

输入中的 code_relevance 是代码规则已经判断好的 HKU 相关性。你只判断 topic_relevance。
输出时保留同一个 note_id，并只输出 topic_relevance / relevance_reason；不要重判 hku_relevance。

只输出 JSON 数组，不要解释。

输入数据：
{json.dumps(gate_batch, ensure_ascii=False)}
"""
        payload = _llm_json_call(client, prompt, "relevance_gate", usage_rows)
        if not isinstance(payload, list):
            raise RuntimeError("Relevance gate response must be a JSON list")
        batch_outputs = _validate_relevance_batch(batch, payload, scan_mode, logs, log_queue, batch_no)
        output_by_id.update({str(row.get("note_id") or ""): row for row in batch_outputs})
        write_jsonl(path, [output_by_id[str(row.get("note_id") or "")] for row in records if str(row.get("note_id") or "") in output_by_id])
        _log(logs, log_queue, f"Relevance gate saved partial={len(output_by_id)}/{len(records)}")

    return [output_by_id[str(row.get("note_id") or "")] for row in records], usage_rows


def _compact_relevance_record(row: dict[str, Any]) -> dict[str, Any]:
    """Topic gate 只接收判断相关性需要的短文本，避免发送详细标注字段。"""

    text = str(row.get("text") or "")
    return {
        "note_id": row.get("note_id"),
        "title": str(row.get("title") or "")[:160],
        "text": _compact_topic_gate_text(text),
        "tags": list(row.get("tags") or [])[:10],
        "code_relevance": row.get("code_relevance"),
    }


def _compact_topic_gate_text(text: str, *, head: int = 520, tail: int = 360) -> str:
    cleaned = re.sub(r"\s+", " ", str(text or "")).strip()
    if len(cleaned) <= head + tail + 20:
        return cleaned
    return f"{cleaned[:head]} ... {cleaned[-tail:]}"


def run_analysis_agent(
    notes: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    processing: dict[str, Any],
    signal_table: list[dict[str, Any]],
    alert_table: list[dict[str, Any]],
    positive_signal_table: list[dict[str, Any]],
    theme_table: list[dict[str, Any]],
    discussion_table: list[dict[str, Any]],
    narrative_table: list[dict[str, Any]],
    uncertainty_table: list[dict[str, Any]],
    comment_signal_table: list[dict[str, Any]],
    narrative_comment_table: list[dict[str, Any]],
    client: Any,
    time_coverage: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Dispatch final analysis to the mode-specific prompt."""

    if time_coverage is None:
        time_coverage = _time_coverage(notes)
    scan_mode = str(processing.get("scan_mode") or "")
    if scan_mode == "broad_scan":
        return run_broad_analysis(
            notes,
            comments,
            annotations,
            processing,
            signal_table,
            alert_table,
            positive_signal_table,
            theme_table,
            discussion_table,
            comment_signal_table,
            narrative_comment_table,
            client,
            time_coverage,
        )
    return run_topic_analysis(
        notes,
        annotations,
        processing,
        narrative_table,
        uncertainty_table,
        narrative_comment_table,
        client,
        time_coverage,
    )


def run_broad_analysis(
    notes: list[dict[str, Any]],
    comments: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    processing: dict[str, Any],
    signal_table: list[dict[str, Any]],
    alert_table: list[dict[str, Any]],
    positive_signal_table: list[dict[str, Any]],
    theme_table: list[dict[str, Any]],
    discussion_table: list[dict[str, Any]],
    comment_signal_table: list[dict[str, Any]],
    narrative_comment_table: list[dict[str, Any]],
    client: Any,
    time_coverage: dict[str, Any],
) -> dict[str, Any]:
    """Broad report keeps the full brand-monitoring context.

    alert_table / positive_signal_table 是 Alerts 与 Positive Signals 的唯一候选来源；
    完整 signal_table 仍然送入，供 key findings 和上下文使用。
    """

    annotation_by_id = {str(row.get("note_id")): row for row in annotations}
    notes_by_id = {str(row.get("note_id")): row for row in notes}
    max_notes = int(os.getenv("XHS_ANALYSIS_MAX_NOTES", "80"))

    # 控制 prompt 大小：只把互动分最高的若干条完整样本送入聚合分析。
    top_notes = sorted(notes, key=_engagement_score, reverse=True)[:max_notes]
    top_posts = []
    for note in top_notes:
        note_id = str(note.get("note_id"))
        anno = annotation_by_id.get(note_id, {})
        top_posts.append(
            {
                "note_id": note_id,
                "title": note.get("title"),
                "content_full": str(note.get("content_full") or "")[:600],
                "author_name": note.get("author_name"),
                "published_at": note.get("published_at"),
                "published_at_raw": note.get("published_at_raw"),
                "collected_at": note.get("collected_at"),
                "engagement": _engagement_score(note),
                "annotation": anno,
            }
        )
    sample_comments = [
        {
            "note_id": comment.get("note_id"),
            "post_url": notes_by_id.get(str(comment.get("note_id") or ""), {}).get("post_url"),
            "post_title": notes_by_id.get(str(comment.get("note_id") or ""), {}).get("title"),
            "post_theme": annotation_by_id.get(str(comment.get("note_id") or ""), {}).get("theme"),
            "post_primary_narrative": annotation_by_id.get(str(comment.get("note_id") or ""), {}).get("primary_narrative"),
            "post_signal": annotation_by_id.get(str(comment.get("note_id") or ""), {}).get("signal_label"),
            "post_content_type": annotation_by_id.get(str(comment.get("note_id") or ""), {}).get("content_type"),
            "post_sentiment": annotation_by_id.get(str(comment.get("note_id") or ""), {}).get("sentiment"),
            "content": comment.get("content"),
            "like_count": comment.get("like_count"),
        }
        for comment in sorted(comments, key=lambda row: to_int(row.get("like_count")), reverse=True)[:200]
    ]

    prompt = build_broad_analysis_prompt(
        processing=processing,
        time_coverage=time_coverage,
        top_posts=top_posts,
        signal_table=signal_table,
        alert_table=alert_table,
        positive_signal_table=positive_signal_table,
        theme_table=theme_table,
        discussion_table=discussion_table,
        comment_signal_table=comment_signal_table,
        narrative_comment_table=narrative_comment_table,
        sample_comments=sample_comments,
    )
    usage_rows: list[dict[str, Any]] = []
    payload = _llm_json_call(client, prompt, "analysis", usage_rows)
    if not isinstance(payload, dict):
        raise RuntimeError("Analysis response must be a JSON object")
    payload["_usage"] = usage_rows[-1] if usage_rows else {}
    return payload


def _validate_topic_analysis_payload(
    payload: dict[str, Any],
    narrative_table: list[dict[str, Any]],
    uncertainty_table: list[dict[str, Any]],
) -> dict[str, Any]:
    """Reject orphan LLM rows and attach exact code-side ids without fuzzy title matching."""

    result = dict(payload)
    clusters_by_id = {str(row.get("cluster_id") or ""): row for row in narrative_table}
    clusters_by_label = {str(row.get("label") or "").strip(): row for row in narrative_table}
    narratives: list[dict[str, Any]] = []
    seen_clusters: set[str] = set()
    for row in payload.get("main_narratives") or []:
        if not isinstance(row, dict):
            continue
        cluster = clusters_by_id.get(str(row.get("cluster_id") or ""))
        if cluster is None:
            cluster = clusters_by_label.get(str(row.get("narrative") or "").strip())
        cluster_id = str((cluster or {}).get("cluster_id") or "")
        if not cluster_id or cluster_id in seen_clusters:
            continue
        seen_clusters.add(cluster_id)
        current = dict(row)
        current["cluster_id"] = cluster_id
        current["narrative"] = cluster.get("label")
        narratives.append(current)
    result["main_narratives"] = narratives

    uncertainties_by_id = {str(row.get("uncertainty_id") or ""): row for row in uncertainty_table}
    uncertainty_rows: list[dict[str, Any]] = []
    seen_uncertainties: set[str] = set()
    for row in payload.get("questions_uncertainties") or []:
        if not isinstance(row, dict):
            continue
        source = uncertainties_by_id.get(str(row.get("uncertainty_id") or ""))
        uncertainty_id = str((source or {}).get("uncertainty_id") or "")
        if not uncertainty_id or uncertainty_id in seen_uncertainties:
            continue
        seen_uncertainties.add(uncertainty_id)
        current = dict(row)
        current["uncertainty_id"] = uncertainty_id
        uncertainty_rows.append(current)
    result["questions_uncertainties"] = uncertainty_rows
    result["executive_summary"] = str(result.get("executive_summary") or "")[:400]
    raw_limitations = result.get("data_limitations") if isinstance(result.get("data_limitations"), list) else []
    result["data_limitations"] = [str(item)[:240] for item in raw_limitations if str(item).strip()]
    return result


def run_topic_analysis(
    notes: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    processing: dict[str, Any],
    narrative_table: list[dict[str, Any]],
    uncertainty_table: list[dict[str, Any]],
    narrative_comment_table: list[dict[str, Any]],
    client: Any,
    time_coverage: dict[str, Any],
) -> dict[str, Any]:
    """Topic report only sees narrative, comment, uncertainty, and limited evidence."""

    if not notes or not narrative_table:
        return _empty_topic_analysis()

    annotation_by_id = {str(row.get("note_id")): row for row in annotations}
    max_notes = int(os.getenv("XHS_ANALYSIS_MAX_NOTES", "80"))
    top_notes = sorted(notes, key=_engagement_score, reverse=True)[:max_notes]
    post_evidence = []
    for note in top_notes:
        note_id = str(note.get("note_id"))
        anno = annotation_by_id.get(note_id, {})
        post_evidence.append(
            {
                "note_id": note_id,
                "title": note.get("title"),
                "content_full": str(note.get("content_full") or "")[:600],
                "published_at": note.get("published_at"),
                "engagement": _engagement_score(note),
                "annotation": {
                    "content_type": anno.get("content_type"),
                    "sentiment": anno.get("sentiment"),
                    "primary_narrative": anno.get("primary_narrative"),
                    "narrative_labels": anno.get("narrative_labels"),
                    "narrative_stance": anno.get("narrative_stance"),
                    "has_uncertainty": anno.get("has_uncertainty"),
                    "uncertainty_type": anno.get("uncertainty_type"),
                    "uncertainty_text": anno.get("uncertainty_text"),
                    "author_type": anno.get("author_type"),
                    "signal_label": anno.get("signal_label"),
                    "evidence_quote": anno.get("evidence_quote"),
                },
            }
        )
    prompt = build_topic_analysis_prompt(
        processing=processing,
        time_coverage=time_coverage,
        post_evidence=post_evidence,
        narrative_table=narrative_table,
        uncertainty_table=uncertainty_table,
        narrative_comment_table=narrative_comment_table,
    )
    usage_rows: list[dict[str, Any]] = []
    payload = _llm_json_call(client, prompt, "analysis", usage_rows)
    if not isinstance(payload, dict):
        raise RuntimeError("Analysis response must be a JSON object")
    payload = _validate_topic_analysis_payload(payload, narrative_table, uncertainty_table)
    payload["_usage"] = usage_rows[-1] if usage_rows else {}
    return payload


def _empty_topic_analysis() -> dict[str, Any]:
    """Deterministic Topic output when no relevant evidence exists."""

    return {
        "executive_summary": "本轮没有足够相关样本形成专题结论。",
        "main_narratives": [],
        "questions_uncertainties": [],
        "appendix": {"supporting_evidence": []},
        "data_limitations": ["本轮未找到足够相关的 Topic 样本，未生成主要叙事或信息不确定点。"],
        "_usage": {},
    }


def _llm_json_call(
    client: Any,
    prompt: str,
    phase: str,
    usage_rows: list[dict[str, Any]],
    retries: int = 1,
) -> Any:
    """调用 LLM 并解析 JSON；模型偶发输出坏 JSON 时重试该次调用，而不是让整个 analyze 步骤失败重跑。"""

    last_exc: Optional[Exception] = None
    for _ in range(retries + 1):
        response = client.get_response([{"role": "user", "content": prompt}])
        if isinstance(response, str):
            raise RuntimeError(response)
        usage = client.usage_dict(response) or {}
        usage["phase"] = phase
        usage_rows.append(usage)
        try:
            return client.extract_json(response.choices[0].message.content)
        except (json.JSONDecodeError, ValueError) as exc:
            last_exc = exc
    raise RuntimeError(f"{phase} returned invalid JSON after {retries + 1} attempt(s): {last_exc}")


def _build_annotation_records(notes: list[dict[str, Any]], comments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # 评论不进入帖子级 annotation，避免 primary_narrative / relevance 被评论内容带偏。
    # 评论会在 annotation 完成后按 note_id 挂回 narrative_comment_table。
    records: list[dict[str, Any]] = []
    for note in notes:
        note_id = str(note.get("note_id") or "")
        tags = _extract_record_tags(note)
        records.append(
            {
                "note_id": note_id,
                "keyword": str(note.get("keyword") or "")[:240],
                "author_name": str(note.get("author_name") or "")[:120],
                "title": str(note.get("title") or "")[:200],
                "text": str(note.get("content_full") or "")[:700],
                "tags": tags[:20],
                "engagement": _engagement_score(note),
            }
        )
    return records


def _extract_record_tags(note: dict[str, Any]) -> list[str]:
    raw = note.get("raw")
    if not isinstance(raw, dict):
        return []
    tag_keys = {
        "tag",
        "tags",
        "tag_name",
        "tagName",
        "hashtag",
        "hashtags",
        "topic",
        "topics",
        "topic_name",
        "topicName",
        "name",
    }
    tags: list[str] = []
    for node in walk_nodes(raw):
        if isinstance(node, dict):
            for key in tag_keys:
                value = node.get(key)
                if isinstance(value, str) and value.strip() and value.strip() not in tags:
                    tags.append(value.strip())
        elif isinstance(node, str) and node.startswith("#") and node.strip() not in tags:
            tags.append(node.strip())
        if len(tags) >= 40:
            break
    return tags


def _load_cached_annotations(
    path: Path,
    records: list[dict[str, Any]],
    scan_mode: str,
    logs: list[str],
    log_queue: Optional[Any],
) -> Optional[list[dict[str, Any]]]:
    """复用完整 annotations，支持预算中断后从 analyze 聚合阶段继续。"""

    if not path.exists():
        return None
    cached = read_jsonl(path)
    input_by_id = {str(row.get("note_id") or ""): row for row in records if row.get("note_id")}
    cached_by_id: dict[str, dict[str, Any]] = {}
    for row in cached:
        if not isinstance(row, dict):
            continue
        note_id = str(row.get("note_id") or "").strip()
        if note_id in input_by_id and _cached_annotation_has_current_schema(row, scan_mode):
            cached_by_id[note_id] = _normalize_annotation(row, note_id, scan_mode)
    missing_ids = sorted(set(input_by_id) - set(cached_by_id))
    if missing_ids:
        _log(logs, log_queue, f"Cached annotations incomplete missing={len(missing_ids)}; rerun annotation")
        return None
    _log(logs, log_queue, f"Reused cached annotations count={len(cached_by_id)}")
    return [cached_by_id[note_id] for note_id in input_by_id]


def _load_partial_annotations(path: Path, records: list[dict[str, Any]], scan_mode: str) -> list[dict[str, Any]]:
    """读取已完成的 partial annotations；只复用当前输入里仍存在且 schema 有效的行。"""

    if not path.exists():
        return []
    input_ids = {str(row.get("note_id") or "") for row in records if row.get("note_id")}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in read_jsonl(path):
        if not isinstance(row, dict):
            continue
        note_id = str(row.get("note_id") or "").strip()
        if not note_id or note_id not in input_ids or note_id in seen:
            continue
        if not _cached_annotation_has_current_schema(row, scan_mode):
            continue
        rows.append(_normalize_annotation(row, note_id, scan_mode))
        seen.add(note_id)
    return rows


def _cached_annotation_has_current_schema(row: dict[str, Any], scan_mode: str) -> bool:
    """只复用当前 HKU schema；旧版 topic_relevance/voice_type 标注会自动重跑。"""

    if row.get("annotation_schema_version") != _annotation_schema_version(scan_mode):
        return False
    required_relevance = ("note_id", "hku_relevance", "topic_relevance")
    if not all(row.get(field) not in (None, "") for field in required_relevance):
        return False
    hku_relevance = str(row.get("hku_relevance") or "").strip().lower()
    topic_relevance = str(row.get("topic_relevance") or "").strip().lower()
    if _should_blank_business_fields(hku_relevance, topic_relevance, scan_mode):
        return True
    if scan_mode == "broad_scan":
        required_business = ("content_type", "theme", "sentiment", "author_type", "signal_label", "signal_types", "risk_level", "risk_type")
        return all(row.get(field) not in (None, "") for field in required_business) and bool(row.get("signal_types"))
    required_business = (
        "content_type",
        "sentiment",
        "primary_narrative",
        "narrative_stance",
        "author_type",
        "signal_label",
    )
    return all(row.get(field) not in (None, "") for field in required_business) and isinstance(row.get("has_uncertainty"), bool)


def _load_partial_relevance(path: Path, records: list[dict[str, Any]], scan_mode: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    input_ids = {str(row.get("note_id") or "") for row in records if row.get("note_id")}
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in read_jsonl(path):
        if not isinstance(row, dict):
            continue
        note_id = str(row.get("note_id") or "").strip()
        if not note_id or note_id not in input_ids or note_id in seen:
            continue
        if row.get("annotation_schema_version") != _annotation_schema_version(scan_mode):
            continue
        rows.append(_normalize_relevance(row, note_id, scan_mode))
        seen.add(note_id)
    return rows


def _annotation_relevance_rules(processing: dict[str, Any]) -> str:
    """按扫描模式解释 HKU 相关性；Topic 的二层过滤由代码配置完成。"""

    scan_mode = str(processing.get("scan_mode") or "")
    if scan_mode == "broad_scan":
        # Broad Scan 的相关性全部由代码判定，这个分支实际不会进入 LLM prompt；保留最简说明以防万一。
        return "- 当前是 Broad Scan：HKU 相关性已由代码规则判定，请直接沿用输入中的 code_relevance。".strip()
    full_query = str(processing.get("keyword") or "").strip()
    topic_terms = _topic_terms_from_keyword(full_query)
    return """
- 当前是 Topic Scan，本次完整搜索主题是：{full_query}。HKU 相关性已由代码规则判定（见 code_relevance），你只判断 topic_relevance。
- topic_relevance 必须判断内容是否与“完整搜索主题背后的用户搜索意图”相关，而不是逐字匹配完整短语，也不是只看拆出来的某个词。
- 判断时保留主题对象与场景边界：例如搜索“港大 Capstone”，必须能支持“港大语境下的 Capstone”；只谈泛 Capstone 或只谈泛港大都应标 unrelated。
- 允许中文社媒常见的同义、近义、反向、经验性表达进入 direct/indirect；例如用户搜一个评价、体验、费用、申请、课程、就业、避坑类主题时，原文用不同说法表达同一对象下的经验、疑问、正负评价、成本变化、注意事项，也应视为相关。
- topic_relevance=direct: 原文明确讨论完整主题，或明确出现主题对象 + 主题场景/意图的组合，即使措辞与 query 不完全一致。
- topic_relevance=indirect: 原文没有完整写出 query，但标题、正文、标签或具体对象能合理支持它与完整主题意图相关。
- topic_relevance=unrelated: 与本次完整搜索主题无关；这种内容不会进入 Topic Report。
- 宁可排除只沾到学校名的泛内容；但不要因为没有逐字出现 query 中的评价词/场景词，就排除同一对象下明显相关的经验或讨论。
- 下面的 topic terms 仅用于理解主题对象和场景，不可单独替代完整 query 判断：{terms}。
""".format(full_query=full_query or "未填写", terms=", ".join(topic_terms) or "无明确 topic term").strip()


def _validate_annotation_batch(
    batch: list[dict[str, Any]],
    payload: Any,
    scan_mode: str,
    logs: list[str],
    log_queue: Optional[Any],
    batch_no: int,
    relevance_by_id: Optional[dict[str, dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """校验 LLM 标注返回：去掉非法/重复 note_id，漏标则补 fallback。"""

    input_by_id = {str(row.get("note_id") or ""): row for row in batch if row.get("note_id")}
    input_ids = set(input_by_id)
    output_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: list[str] = []
    unknown_ids: list[str] = []

    rows = payload if isinstance(payload, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        note_id = str(row.get("note_id") or "").strip()
        if note_id not in input_ids:
            unknown_ids.append(note_id or "<missing>")
            continue
        if note_id in output_by_id:
            duplicate_ids.append(note_id)
            continue
        output_by_id[note_id] = _normalize_annotation(row, note_id, scan_mode, (relevance_by_id or {}).get(note_id))

    missing_ids = sorted(input_ids - set(output_by_id))
    if unknown_ids or duplicate_ids or missing_ids:
        _log(
            logs,
            log_queue,
            "Annotation batch "
            f"{batch_no} id_mismatch missing={missing_ids} duplicate={duplicate_ids} unknown={unknown_ids}",
        )

    for note_id in missing_ids:
        output_by_id[note_id] = _fallback_annotation(input_by_id[note_id], scan_mode, (relevance_by_id or {}).get(note_id))

    return [output_by_id[note_id] for note_id in input_by_id]


def _validate_relevance_batch(
    batch: list[dict[str, Any]],
    payload: Any,
    scan_mode: str,
    logs: list[str],
    log_queue: Optional[Any],
    batch_no: int,
) -> list[dict[str, Any]]:
    input_by_id = {str(row.get("note_id") or ""): row for row in batch if row.get("note_id")}
    input_ids = set(input_by_id)
    output_by_id: dict[str, dict[str, Any]] = {}
    duplicate_ids: list[str] = []
    unknown_ids: list[str] = []

    rows = payload if isinstance(payload, list) else []
    for row in rows:
        if not isinstance(row, dict):
            continue
        note_id = str(row.get("note_id") or "").strip()
        if note_id not in input_ids:
            unknown_ids.append(note_id or "<missing>")
            continue
        if note_id in output_by_id:
            duplicate_ids.append(note_id)
            continue
        output_by_id[note_id] = _normalize_relevance(row, note_id, scan_mode, input_by_id[note_id].get("code_relevance"))

    missing_ids = sorted(input_ids - set(output_by_id))
    if unknown_ids or duplicate_ids or missing_ids:
        _log(
            logs,
            log_queue,
            "Relevance gate batch "
            f"{batch_no} id_mismatch missing={missing_ids} duplicate={duplicate_ids} unknown={unknown_ids}",
        )

    for note_id in missing_ids:
        output_by_id[note_id] = _fallback_relevance(input_by_id[note_id], scan_mode)

    return [output_by_id[note_id] for note_id in input_by_id]


def _normalize_relevance(
    row: dict[str, Any],
    note_id: str,
    scan_mode: str,
    code_relevance: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    hku_relevance = str((code_relevance or {}).get("hku_relevance") or row.get("hku_relevance") or "").strip().lower()
    raw_topic_relevance = str(row.get("topic_relevance") or "").strip().lower()
    if hku_relevance not in {"direct", "indirect", "unrelated"}:
        hku_relevance = "unrelated"
    if raw_topic_relevance in {"direct", "indirect", "unrelated"}:
        topic_relevance = raw_topic_relevance
    elif scan_mode == "broad_scan":
        topic_relevance = ""
    else:
        topic_relevance = "unrelated"
    if topic_relevance not in {"direct", "indirect", "unrelated"}:
        topic_relevance = "indirect" if scan_mode == "broad_scan" and hku_relevance in {"direct", "indirect"} else "unrelated"
    if hku_relevance == "unrelated":
        topic_relevance = "unrelated"
    return {
        "note_id": note_id,
        "hku_relevance": hku_relevance,
        "topic_relevance": topic_relevance,
        "relevance_reason": str(row.get("relevance_reason") or row.get("reason") or "")[:80],
        "hku_match_reason": str((code_relevance or {}).get("hku_match_reason") or row.get("hku_match_reason") or "")[:160],
        "gate_source": "code_hku" if scan_mode == "broad_scan" else "code_hku_topic_gate",
        "annotation_schema_version": _annotation_schema_version(scan_mode),
    }


def _fallback_relevance(record: dict[str, Any], scan_mode: str) -> dict[str, Any]:
    hku_row = _code_hku_relevance(record, scan_mode)
    topic_relevance = "indirect" if scan_mode == "broad_scan" and hku_row["hku_relevance"] != "unrelated" else "unrelated"
    return {
        "note_id": str(record.get("note_id") or ""),
        "hku_relevance": hku_row["hku_relevance"],
        "topic_relevance": topic_relevance,
        "relevance_reason": "Topic gate 未返回该 note_id，系统保守判断为不进入专题分析。" if scan_mode != "broad_scan" else hku_row["relevance_reason"],
        "hku_match_reason": hku_row.get("hku_match_reason", ""),
        "gate_source": hku_row.get("gate_source", "code_hku"),
        "annotation_schema_version": _annotation_schema_version(scan_mode),
        "fallback": True,
    }


def _is_relevant_for_detail(row: Optional[dict[str, Any]], scan_mode: str) -> bool:
    if not row:
        return False
    hku_rel = str(row.get("hku_relevance") or "").strip().lower()
    if hku_rel not in {"direct", "indirect"}:
        return False
    if scan_mode == "broad_scan":
        return True
    topic_rel = str(row.get("topic_relevance") or "").strip().lower()
    return topic_rel in {"direct", "indirect"}


def _annotation_from_relevance(row: Optional[dict[str, Any]], note_id: str, scan_mode: str) -> dict[str, Any]:
    relevance = _normalize_relevance(row or {"note_id": note_id}, note_id, scan_mode)
    return {
        **relevance,
        **_blank_business_fields(),
    }


def _normalize_annotation(
    row: dict[str, Any],
    note_id: str,
    scan_mode: str,
    relevance: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """补齐标注字段，避免后续 summary/report 因字段缺失变形。"""

    hku_relevance = str((relevance or {}).get("hku_relevance") or row.get("hku_relevance") or "").strip().lower()
    topic_relevance = str((relevance or {}).get("topic_relevance") or row.get("topic_relevance") or "").strip().lower()
    if not topic_relevance:
        topic_relevance = "" if scan_mode == "broad_scan" else "unrelated"
    is_broad = scan_mode == "broad_scan"
    content_type = _normalize_content_type(row.get("content_type"))
    signal_types = _normalize_signal_types(row.get("signal_types")) if is_broad else []
    author_type = _normalize_author_type(row.get("author_type"), signal_types)
    signal_label = _normalize_signal_label(row.get("signal_label") or row.get("primary_narrative") or "uncategorized_signal")
    risk_level = _normalize_risk_level(row.get("risk_level")) if is_broad else ""
    narrative_labels = row.get("narrative_labels") if isinstance(row.get("narrative_labels"), list) else []
    narrative_labels = [_normalize_optional_label(item) for item in narrative_labels[:3] if _normalize_optional_label(item)]
    has_uncertainty = bool(row.get("has_uncertainty"))

    normalized = {
        "note_id": note_id,
        "hku_relevance": hku_relevance or "indirect",
        "topic_relevance": topic_relevance or "unrelated",
        "relevance_reason": str((relevance or {}).get("relevance_reason") or row.get("relevance_reason") or "")[:80],
        "hku_match_reason": str((relevance or {}).get("hku_match_reason") or row.get("hku_match_reason") or "")[:160],
        "hku_relevance_reason": str((relevance or {}).get("hku_match_reason") or row.get("hku_relevance_reason") or row.get("relevance_reason") or "")[:160],
        "gate_source": str((relevance or {}).get("gate_source") or row.get("gate_source") or ""),
        "content_type": content_type,
        "theme": _normalize_theme(row.get("theme")) if is_broad else "",
        "sentiment": str(row.get("sentiment") or "neutral"),
        "author_type": author_type,
        "discussion_topic": "",
        "subtopic_label": "",
        "primary_narrative": _normalize_optional_label(row.get("primary_narrative")) if not is_broad else "",
        "narrative_labels": narrative_labels if not is_broad else [],
        "narrative_stance": _normalize_narrative_stance(row.get("narrative_stance")) if not is_broad else "",
        "has_uncertainty": has_uncertainty if not is_broad else False,
        "uncertainty_type": _normalize_optional_label(row.get("uncertainty_type")) if has_uncertainty and not is_broad else None,
        "uncertainty_text": str(row.get("uncertainty_text") or "")[:120] if has_uncertainty and not is_broad else None,
        "signal_label": signal_label,
        "issue_label": "",
        "risk_level": risk_level,
        "risk_type": _normalize_risk_type(row.get("risk_type"), is_broad) if is_broad else "",
        "risk_reason": str(row.get("risk_reason") or "")[:120] if is_broad else "",
        "signal_types": signal_types,
        "signal_type": signal_types[0] if signal_types else "",
        "evidence_quote": str(row.get("evidence_quote") or ""),
        "entities": [],
        "reason": [],
        "annotation_schema_version": _annotation_schema_version(scan_mode),
    }
    if normalized["hku_relevance"] not in {"direct", "indirect", "unrelated"}:
        normalized["hku_relevance"] = "indirect"
    if normalized["topic_relevance"] not in {"direct", "indirect", "unrelated"}:
        normalized["topic_relevance"] = "unrelated"
    if normalized["sentiment"] not in {"positive", "neutral", "negative"}:
        normalized["sentiment"] = "neutral"
    if _should_blank_business_fields(normalized["hku_relevance"], normalized["topic_relevance"], scan_mode):
        normalized.update(_blank_business_fields())
    return normalized


def _validate_annotation_evidence_quotes(
    annotations: list[dict[str, Any]],
    notes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep only meaningful verbatim evidence that can be found in the source post."""

    notes_by_id = {str(note.get("note_id") or ""): note for note in notes}
    validated: list[dict[str, Any]] = []
    for row in annotations:
        current = dict(row)
        quote = " ".join(str(current.get("evidence_quote") or "").split()).strip()
        note = notes_by_id.get(str(current.get("note_id") or ""), {})
        source = " ".join(
            str(note.get("content_full") or f"{note.get('title') or ''} {note.get('body') or ''}").split()
        )
        compact_quote = re.sub(r"\s+", "", quote)
        compact_source = re.sub(r"\s+", "", source)
        meaningful = len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", quote)) >= 2
        current["evidence_quote"] = quote if meaningful and compact_quote in compact_source else ""
        current["evidence_verified"] = bool(current["evidence_quote"])
        validated.append(current)
    return validated


def _should_blank_business_fields(hku_relevance: str, topic_relevance: str, scan_mode: str) -> bool:
    """不相关样本不做业务标签，避免污染后续聚合。"""

    hku_rel = str(hku_relevance or "").strip().lower()
    topic_rel = str(topic_relevance or "").strip().lower()
    if hku_rel == "unrelated":
        return True
    return scan_mode != "broad_scan" and topic_rel == "unrelated"


def _blank_business_fields() -> dict[str, Any]:
    """统一清空不相关样本的业务标签。"""

    return {
        "content_type": "",
        "theme": "",
        "sentiment": "",
        "author_type": "",
        "discussion_topic": "",
        "subtopic_label": "",
        "primary_narrative": "",
        "narrative_labels": [],
        "narrative_stance": "",
        "has_uncertainty": False,
        "uncertainty_type": None,
        "uncertainty_text": None,
        "signal_label": "",
        "issue_label": "",
        "risk_level": "",
        "risk_type": "",
        "risk_reason": "",
        "signal_types": [],
        "signal_type": "",
        "evidence_quote": "",
        "entities": [],
        "reason": [],
    }


def _fallback_annotation(
    record: dict[str, Any],
    scan_mode: str,
    relevance: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """详细标注漏标时的兜底：尊重 relevance gate，不把未详细标注样本强行升格。"""

    note_id = str(record.get("note_id") or "")
    gate = _normalize_relevance(relevance or _fallback_relevance(record, scan_mode), note_id, scan_mode)
    if not _is_relevant_for_detail(gate, scan_mode):
        return {**gate, **_blank_business_fields(), "fallback": True}
    title = str(record.get("title") or "")
    base = {**gate, **_blank_business_fields(), "content_type": "other", "sentiment": "neutral", "author_type": "unclear"}
    if scan_mode == "broad_scan":
        base.update({"theme": "Other", "signal_label": title[:20] or "uncategorized_signal", "risk_level": "none", "risk_type": "none", "signal_types": ["other"], "signal_type": "other"})
    else:
        base.update(
            {
                "primary_narrative": "",
                "narrative_stance": "neutral",
                "signal_label": "uncategorized_signal",
            }
        )
    base.update({"annotation_schema_version": _annotation_schema_version(scan_mode), "fallback": True})
    return base


def _annotation_summary(annotations: list[dict[str, Any]], scan_mode: Optional[str] = None) -> dict[str, Any]:
    summary = {
        "hku_relevance": dict(Counter(str(row.get("hku_relevance") or "unknown") for row in annotations)),
        "topic_relevance": dict(Counter(str(row.get("topic_relevance") or "unknown") for row in annotations)),
        "sentiment": dict(Counter(str(row.get("sentiment") or "unknown") for row in annotations)),
        "author_type": dict(Counter(str(row.get("author_type") or "unknown") for row in annotations)),
        "content_type": dict(Counter(str(row.get("content_type") or "unknown") for row in annotations)),
    }
    if scan_mode == "topic_scan":
        summary.update(
            {
                "primary_narrative": dict(Counter(str(row.get("primary_narrative") or "unknown") for row in annotations)),
                "narrative_stance": dict(Counter(str(row.get("narrative_stance") or "unknown") for row in annotations)),
                "has_uncertainty": dict(Counter(str(bool(row.get("has_uncertainty"))).lower() for row in annotations)),
            }
        )
        return summary
    summary.update(
        {
            "theme": dict(Counter(str(row.get("theme") or "unknown") for row in annotations)),
            "risk_level": dict(Counter(str(row.get("risk_level") or "unknown") for row in annotations)),
            "risk_type": dict(Counter(str(row.get("risk_type") or "unknown") for row in annotations)),
            "discussion_topic": dict(Counter(str(row.get("discussion_topic") or "unknown") for row in annotations)),
            "subtopic_label": dict(Counter(str(row.get("subtopic_label") or "unknown") for row in annotations)),
            "signal_type": dict(Counter(signal_type for row in annotations for signal_type in _summary_signal_types(row))),
        }
    )
    if scan_mode is None:
        summary.update(
            {
                "primary_narrative": dict(Counter(str(row.get("primary_narrative") or "unknown") for row in annotations)),
                "narrative_stance": dict(Counter(str(row.get("narrative_stance") or "unknown") for row in annotations)),
                "has_uncertainty": dict(Counter(str(bool(row.get("has_uncertainty"))).lower() for row in annotations)),
            }
        )
    return summary


def _summary_signal_types(row: dict[str, Any]) -> list[str]:
    """summary 里保留空 signal_types，不把不相关样本误算成 other。"""

    if row.get("signal_types") == []:
        return []
    return _normalize_signal_types(row.get("signal_types") or row.get("signal_type"))


def _merge_signal_labels(
    annotations: list[dict[str, Any]],
    client: Any,
    logs: list[str],
    log_queue: Optional[Any],
    usage_rows: list[dict[str, Any]],
) -> dict[str, str]:
    """一次轻量 LLM 调用：把描述同一事件/问题的 signal_label 归并成规范信号名。

    只送 label 文本，不送帖子内容，token 成本极低；任何失败都返回空映射，
    build_signal_table 自动退回关键词规则，绝不阻塞分析。
    """

    labels: list[str] = []
    for row in annotations:
        raw = str(row.get("signal_label") or "").strip()
        if not raw:
            continue
        label = _normalize_signal_label(raw)
        if label not in labels:
            labels.append(label)
    if len(labels) <= 1:
        return {}

    prompt = f"""
以下是对同一批小红书帖子生成的 signal_label 列表。请把"描述同一事件或同一问题"的 label 归为一组，并给每组起一个规范信号名。

规则：
- labels 里只能放输入列表中的 label，必须原样复制，不要改写；每个 label 只能出现在一组里，不要遗漏。
- 规范名 10-20 字，要具体（事件/问题 + 对象），不要泛词（如"学生体验""学校动态"）。
- 信息发布类和担忧/抱怨类即使围绕同一事件，也应分成不同组（例如"学制改革官宣与解读"和"学制延长引发时间与成本担忧"）。
- 拿不准归属的 label 自己单独成组，规范名可以等于该 label。

只输出 JSON，不要解释：
{{"groups": [{{"signal": "规范信号名", "labels": ["原label1", "原label2"]}}]}}

label 列表：
{json.dumps(labels, ensure_ascii=False)}
"""
    try:
        payload = _llm_json_call(client, prompt, "signal_merge", usage_rows)
    except Exception as exc:  # noqa: BLE001
        _log(logs, log_queue, f"Signal merge failed; fallback to rule-based grouping: {exc}")
        return {}
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        _log(logs, log_queue, "Signal merge returned unexpected shape; fallback to rule-based grouping")
        return {}
    label_set = set(labels)
    mapping: dict[str, str] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = _normalize_signal_label(group.get("signal"))
        for label in group.get("labels") or []:
            normalized = _normalize_signal_label(label)
            if name and normalized in label_set and normalized not in mapping:
                mapping[normalized] = name
    _log(
        logs,
        log_queue,
        f"Signal merge labels={len(labels)} mapped={len(mapping)} groups={len(set(mapping.values()))}",
    )
    return mapping


def _merge_narrative_labels(
    annotations: list[dict[str, Any]],
    client: Any,
    logs: list[str],
    log_queue: Optional[Any],
    usage_rows: list[dict[str, Any]],
    notes: Optional[list[dict[str, Any]]] = None,
) -> dict[str, str]:
    """把语义相同的 Topic primary_narrative 合并为规范市场说法。"""

    labels: list[str] = []
    for row in annotations:
        label = _normalize_optional_label(row.get("primary_narrative"))
        if label and label not in labels:
            labels.append(label)
    if len(labels) <= 1:
        return {}

    notes_by_id = {str(note.get("note_id") or ""): note for note in (notes or [])}
    label_contexts: list[dict[str, Any]] = []
    for label in labels:
        samples = []
        for row in annotations:
            if _normalize_optional_label(row.get("primary_narrative")) != label:
                continue
            note = notes_by_id.get(str(row.get("note_id") or ""), {})
            samples.append(
                {
                    "title": str(note.get("title") or "")[:100],
                    "evidence_quote": str(row.get("evidence_quote") or "")[:120],
                    "narrative_stance": row.get("narrative_stance"),
                }
            )
            if len(samples) >= 3:
                break
        label_contexts.append({"label": label, "samples": samples})

    prompt = f"""
以下是同一 Topic Scan 中逐帖生成的 primary_narrative。请合并语义相同的市场说法。

规则：
- labels 必须原样复制输入值；每个 label 只能出现一次，不要遗漏。
- canonical_narrative 10-20 字，描述市场正在怎么说。
- 合并同一对象、同一事件或同一体验下的直接侧面，不要把“事实 / 影响 / 感受 / 追问”机械拆成多个 canonical。
- 中性信息、正面解读、负面担忧如果共享同一核心事实，可以合并为 mixed；只有事实对象不同或证据方向明显不同才分开。
- 保持真正不同的事项独立；如果对象、时间范围、比较对象或讨论场景明显不同，不要强行合并。
- 不同专业、项目、学位层级、课程代码、申请批次或事件必须保持独立，除非样本明确讨论同一个事实。
- 不要把官方信息发布、个人体验、评论追问和情绪反应混成同一个 canonical，除非它们都围绕同一核心说法。
- 无法确定是否相同的 label 保持独立。

只输出 JSON：
{{"groups": [{{"canonical_narrative": "规范说法", "labels": ["原label1", "原label2"]}}]}}

labels 与对应帖子样本：
{json.dumps(label_contexts, ensure_ascii=False)}
"""
    try:
        payload = _llm_json_call(client, prompt, "narrative_merge", usage_rows)
    except Exception as exc:  # noqa: BLE001
        _log(logs, log_queue, f"Narrative merge failed; keep original labels: {exc}")
        return {}
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        _log(logs, log_queue, "Narrative merge returned unexpected shape; keep original labels")
        return {}
    label_set = set(labels)
    mapping: dict[str, str] = {}
    for group in groups:
        if not isinstance(group, dict):
            continue
        canonical = _normalize_optional_label(group.get("canonical_narrative"))
        for label in group.get("labels") or []:
            source = _normalize_optional_label(label)
            if canonical and source in label_set and source not in mapping:
                mapping[source] = canonical
    _log(logs, log_queue, f"Narrative merge labels={len(labels)} mapped={len(mapping)} groups={len(set(mapping.values()))}")
    return mapping


def build_signal_table(
    notes: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    label_map: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """用代码聚合 repeated signals，避免 LLM 重新数 mention_count/engagement_sum。

    分组优先用 label_map（LLM 语义归并的规范名），没有映射时退回关键词规则。
    """

    notes_by_id = {str(note.get("note_id")): note for note in notes}
    annotation_by_id = {str(row.get("note_id") or ""): row for row in annotations}
    theme_volume = Counter(
        str(annotation_by_id.get(str(note.get("note_id")), {}).get("theme") or "Other")
        for note in notes
    )
    grouped: dict[str, dict[str, Any]] = {}
    for row in annotations:
        note_id = str(row.get("note_id") or "")
        if note_id not in notes_by_id:
            continue
        raw_signal_label = str(row.get("signal_label") or "").strip()
        normalized_label = _normalize_signal_label(raw_signal_label) if raw_signal_label else ""
        # 分组只认 LLM 语义归并的结果；合并不到就保留原 label 各自成组，
        # 不再用手写关键词规则猜测两个 signal 是否等价。
        label = (label_map or {}).get(normalized_label) or normalized_label or "其他分散信号"
        signal_types = _normalize_signal_types(row.get("signal_types") or row.get("signal_type"))
        theme = _normalize_theme(row.get("theme"))
        key = label
        bucket = grouped.setdefault(
            key,
            {
                "signal": label,
                "theme": theme,
                "discussion_topic": str(row.get("discussion_topic") or ""),
                "subtopic_label": str(row.get("subtopic_label") or ""),
                # 命中合并规则时，原始 LLM signal_label 收进 source_labels，方便回溯。
                "source_labels": [],
                "signal_types": [],
                "signal_type": "other",
                "mention_count": 0,
                "engagement_sum": 0,
                "engagement_score": 0,
                "theme_volume": theme_volume.get(theme, 0),
                "repeated_signal": False,
                "risk_level": "none",
                "risk_types": [],
                "risk_type": "none",
                "risk_reasons": [],
                "author_type_counts": Counter(),
                # content_types / sentiments 只记录"出现过哪些值"，无法区分 9 负 1 正和 1 负 9 正；
                # Alert / Positive 判定需要真实分布，所以额外维护 *_counts。两者都保留，旧下游逻辑不受影响。
                "content_type_counts": Counter(),
                "sentiment_counts": Counter(),
                "risk_level_counts": Counter(),
                # Alert 证据只统计"负面/敏感"那部分帖子，不能直接用整组的 mention_count / engagement_sum。
                "alert_evidence_count": 0,
                "alert_evidence_engagement_sum": 0,
                "alert_evidence_note_ids": [],
                # Positive 证据同理：同一个 signal 里可能同时存在正面与负面帖子。
                "positive_evidence_count": 0,
                "positive_evidence_engagement_sum": 0,
                "positive_evidence_note_ids": [],
                "content_types": [],
                "sentiments": [],
                "evidence_note_ids": [],
                "evidence_quotes": [],
                "evidence_items": [],
                "earliest_post_date": "",
                "latest_post_date": "",
                "recent_7d_count": 0,
                "recent_30d_count": 0,
                "dated_mention_count": 0,
                "undated_mention_count": 0,
                "_post_dates": [],
            },
        )
        raw_label = str(row.get("signal_label") or "").strip()
        original_label = _normalize_signal_label(raw_label) if raw_label else ""
        if original_label and original_label != label and original_label not in bucket["source_labels"] and len(bucket["source_labels"]) < 8:
            bucket["source_labels"].append(original_label)
        for signal_type in signal_types:
            if signal_type not in bucket["signal_types"]:
                bucket["signal_types"].append(signal_type)
        bucket["signal_type"] = "/".join(bucket["signal_types"]) if bucket["signal_types"] else "other"
        bucket["mention_count"] += 1
        score = _engagement_score(notes_by_id[note_id])
        bucket["engagement_sum"] += score
        bucket["engagement_score"] += score
        content_type = str(row.get("content_type") or "other")
        sentiment = str(row.get("sentiment") or "neutral")
        author_type = str(row.get("author_type") or "unclear")
        annotated_risk = _normalize_risk_level(row.get("risk_level"))
        annotated_risk_type = _normalize_risk_type(row.get("risk_type"), is_broad=True)
        if content_type not in bucket["content_types"]:
            bucket["content_types"].append(content_type)
        if sentiment not in bucket["sentiments"]:
            bucket["sentiments"].append(sentiment)
        bucket["author_type_counts"][author_type] += 1
        bucket["content_type_counts"][content_type] += 1
        bucket["sentiment_counts"][sentiment] += 1
        bucket["risk_level_counts"][annotated_risk] += 1
        if _is_alert_evidence(annotated_risk, sentiment, content_type):
            bucket["alert_evidence_count"] += 1
            bucket["alert_evidence_engagement_sum"] += score
            if note_id not in bucket["alert_evidence_note_ids"] and len(bucket["alert_evidence_note_ids"]) < 8:
                bucket["alert_evidence_note_ids"].append(note_id)
        if _is_positive_evidence(sentiment, content_type):
            bucket["positive_evidence_count"] += 1
            bucket["positive_evidence_engagement_sum"] += score
            if note_id not in bucket["positive_evidence_note_ids"] and len(bucket["positive_evidence_note_ids"]) < 8:
                bucket["positive_evidence_note_ids"].append(note_id)
        risk_reason = str(row.get("risk_reason") or "").strip()
        if annotated_risk != "none":
            bucket["risk_level"] = _max_risk(bucket.get("risk_level"), annotated_risk)
            if annotated_risk_type != "none" and annotated_risk_type not in bucket["risk_types"]:
                bucket["risk_types"].append(annotated_risk_type)
        if risk_reason and len(bucket["risk_reasons"]) < 4:
            bucket["risk_reasons"].append(risk_reason)
        if len(bucket["evidence_note_ids"]) < 8:
            bucket["evidence_note_ids"].append(note_id)
        quote = str(row.get("evidence_quote") or row.get("reason") or "").strip()
        if quote and len(bucket["evidence_quotes"]) < 5:
            bucket["evidence_quotes"].append(_short_evidence_quote(quote))
        if quote and len(bucket["evidence_items"]) < 5:
            bucket["evidence_items"].append(
                {
                    "quote": _short_evidence_quote(quote),
                    "note_id": note_id,
                    "post_url": str(notes_by_id[note_id].get("post_url") or ""),
                    "date": _note_date_text(notes_by_id[note_id]),
                    "engagement": score,
                }
            )
        post_date = _note_date(notes_by_id[note_id])
        if post_date is not None:
            bucket["_post_dates"].append(post_date)
        else:
            bucket["undated_mention_count"] += 1
    rows = []
    for bucket in grouped.values():
        bucket["repeated_signal"] = bucket["mention_count"] >= 2
        _finalize_signal_dates(bucket)
        bucket["risk_type"] = bucket["risk_types"][0] if bucket["risk_types"] else "none"
        # signal_id 只由 canonical signal 名决定，同一个 signal 每次聚合都得到同一个 id；
        # 报告层用它做 Alert / Positive 的精确 grounding，不能依赖数组下标或标题模糊匹配。
        bucket["signal_id"] = _stable_id("signal", str(bucket.get("signal") or ""))
        for key in ("author_type_counts", "content_type_counts", "sentiment_counts", "risk_level_counts"):
            bucket[key] = dict(bucket[key])
        rows.append(bucket)
    return sorted(rows, key=lambda item: (item["mention_count"], item["engagement_sum"]), reverse=True)


def _is_alert_evidence(risk_level: str, sentiment: str, content_type: str) -> bool:
    """这条帖子本身是否构成"负面 / 敏感"证据。

    Alert 的重复程度必须只数这类帖子：一个 signal 里 9 条正面 + 1 条负面，
    mention_count=10 并不代表"负面讨论重复出现"。
    """

    return (
        _normalize_risk_level(risk_level) != "none"
        or sentiment == "negative"
        or content_type in {"complaint", "concern"}
    )


def _is_positive_evidence(sentiment: str, content_type: str) -> bool:
    """这条帖子本身是否构成正面声誉证据（与 Alert 证据口径对称）。"""

    return sentiment == "positive" or content_type == "positive_advocacy"


_ALERT_PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def _alert_priority_for_signal(bucket: dict[str, Any]) -> str:
    """Alert 优先级 = 本轮监测中的**关注优先级**，不是现实事件的严重程度。

    high / medium / low 只回答"这周 Marketing 应该先看哪条社媒信号"，
    不代表事故等级，也不代表已确认的现实风险。

    判定只用两类确定性输入：
    - annotation 逐帖判断的 risk_level（单条内容本身的敏感度）；
    - 代码聚合的 alert_evidence_count / alert_evidence_engagement_sum
      （同类负面/敏感讨论的重复程度与热度）。

    阈值：3 条 / 800 互动 / 2 条 / 200 互动。
    """

    risk_level = _normalize_risk_level(bucket.get("risk_level"))
    alert_evidence_count = to_int(bucket.get("alert_evidence_count"))
    alert_engagement = to_int(bucket.get("alert_evidence_engagement_sum"))

    # 完全没有负面 / 敏感证据：高热度正面或中性话题不进入 Alert。
    if alert_evidence_count <= 0:
        return "none"
    # 单条内容本身已被标注为明确 high risk。
    if risk_level == "high":
        return "high"
    # 同类负面 / 敏感信号重复出现。
    if alert_evidence_count >= 3:
        return "high"
    # 负面 / 敏感内容本身获得高互动。
    if alert_engagement >= 800:
        return "high"
    if risk_level == "medium":
        return "medium"
    # 至少两条独立负面 / 敏感证据。
    if alert_evidence_count >= 2:
        return "medium"
    # 单条负面 / 敏感信号获得一定互动。
    if alert_engagement >= 200:
        return "medium"
    # 已出现，但目前证据有限。
    return "low"


def _alert_type_for_signal(bucket: dict[str, Any]) -> str:
    """Alert 分类由代码决定，不让最终 LLM 自己发明 category。

    优先复用 annotation 的 risk_type；没有明确 risk_type 时退回内容类型分布。
    """

    risk_type = _normalize_risk_type(bucket.get("risk_type"), is_broad=True)
    if risk_type != "none":
        return risk_type
    content_counts = bucket.get("content_type_counts") if isinstance(bucket.get("content_type_counts"), dict) else {}
    sentiment_counts = bucket.get("sentiment_counts") if isinstance(bucket.get("sentiment_counts"), dict) else {}
    if to_int(content_counts.get("complaint")) > 0:
        return "complaint"
    if to_int(content_counts.get("concern")) > 0:
        return "concern"
    if to_int(sentiment_counts.get("negative")) > 0:
        return "negative_discussion"
    return "other"


def _alert_triggers_for_signal(bucket: dict[str, Any]) -> list[str]:
    """机器可读的触发原因，方便 debug 和回查；不直接展示给报告读者。"""

    risk_level = _normalize_risk_level(bucket.get("risk_level"))
    alert_evidence_count = to_int(bucket.get("alert_evidence_count"))
    alert_engagement = to_int(bucket.get("alert_evidence_engagement_sum"))
    content_counts = bucket.get("content_type_counts") if isinstance(bucket.get("content_type_counts"), dict) else {}
    sentiment_counts = bucket.get("sentiment_counts") if isinstance(bucket.get("sentiment_counts"), dict) else {}

    triggers: list[str] = []
    if risk_level == "high":
        triggers.append("high_risk_annotation")
    elif risk_level == "medium":
        triggers.append("medium_risk_annotation")
    if alert_evidence_count >= 3:
        triggers.append("repeated_alert_evidence")
    if alert_engagement >= 800:
        triggers.append("high_alert_engagement")
    if to_int(sentiment_counts.get("negative")) > 0:
        triggers.append("negative_sentiment")
    if to_int(content_counts.get("complaint")) > 0:
        triggers.append("complaint")
    if to_int(content_counts.get("concern")) > 0:
        triggers.append("concern")
    return triggers


def build_alert_table(signal_table: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Alerts 的唯一候选来源：代码先选，LLM 只能给已选中的 signal 写解释文字。

    最终报告不允许 LLM 从整张 signal_table 里自行挑选或升级 Alert。
    """

    rows: list[dict[str, Any]] = []
    for signal in signal_table:
        if not isinstance(signal, dict):
            continue
        alert_priority = _alert_priority_for_signal(signal)
        if alert_priority == "none":
            continue
        rows.append(
            {
                **signal,
                "alert_priority": alert_priority,
                "alert_type": _alert_type_for_signal(signal),
                "alert_triggers": _alert_triggers_for_signal(signal),
            }
        )
    rows.sort(
        key=lambda row: (
            _ALERT_PRIORITY_RANK.get(str(row.get("alert_priority") or "none"), 0),
            to_int(row.get("alert_evidence_count")),
            to_int(row.get("alert_evidence_engagement_sum")),
        ),
        reverse=True,
    )
    return rows[:limit]


def _positive_priority_for_signal(bucket: dict[str, Any]) -> str:
    """正面声誉信号的内部排序强度；口径与 Alert 对称，只数正面证据本身。"""

    positive_count = to_int(bucket.get("positive_evidence_count"))
    positive_engagement = to_int(bucket.get("positive_evidence_engagement_sum"))
    if positive_count <= 0:
        return "none"
    if positive_count >= 3 or positive_engagement >= 800:
        return "high"
    if positive_count >= 2 or positive_engagement >= 200:
        return "medium"
    return "low"


_POSITIVE_PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def build_positive_signal_table(signal_table: list[dict[str, Any]], limit: int = 5) -> list[dict[str, Any]]:
    """Positive Reputation Signals 的唯一候选来源，结构与 alert_table 对称。

    营销号占多数的 signal 不作为真实用户正面口碑候选。
    """

    rows: list[dict[str, Any]] = []
    for signal in signal_table:
        if not isinstance(signal, dict):
            continue
        positive_priority = _positive_priority_for_signal(signal)
        if positive_priority == "none":
            continue
        author_counts = signal.get("author_type_counts") if isinstance(signal.get("author_type_counts"), dict) else {}
        marketing_count = to_int(author_counts.get("agency_marketing"))
        author_total = sum(to_int(value) for value in author_counts.values())
        # 营销内容过半时不把它当成真实学生正面口碑。
        if author_total and marketing_count * 2 > author_total:
            continue
        rows.append(
            {
                "signal_id": str(signal.get("signal_id") or ""),
                "signal": str(signal.get("signal") or ""),
                "theme": str(signal.get("theme") or ""),
                "positive_priority": positive_priority,
                "positive_evidence_count": to_int(signal.get("positive_evidence_count")),
                "positive_evidence_engagement_sum": to_int(signal.get("positive_evidence_engagement_sum")),
                "positive_evidence_note_ids": list(signal.get("positive_evidence_note_ids") or []),
                "sentiment_counts": dict(signal.get("sentiment_counts") or {}),
                "content_type_counts": dict(signal.get("content_type_counts") or {}),
                "author_type_counts": dict(author_counts),
                "mention_count": to_int(signal.get("mention_count")),
                "engagement_sum": to_int(signal.get("engagement_sum")),
                "evidence_items": list(signal.get("evidence_items") or []),
            }
        )
    rows.sort(
        key=lambda row: (
            _POSITIVE_PRIORITY_RANK.get(str(row.get("positive_priority") or "none"), 0),
            to_int(row.get("positive_evidence_count")),
            to_int(row.get("positive_evidence_engagement_sum")),
        ),
        reverse=True,
    )
    return rows[:limit]


def build_discussion_table(
    notes: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    scan_mode: str,
    narrative_map: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """Broad 按 theme、Topic 按 primary_narrative 聚合，数字由代码负责。"""

    notes_by_id = {str(note.get("note_id")): note for note in notes}
    grouped: dict[str, dict[str, Any]] = {}
    is_broad = scan_mode == "broad_scan"
    label_key = "theme" if is_broad else "primary_narrative"
    for row in annotations:
        note_id = str(row.get("note_id") or "")
        if note_id not in notes_by_id:
            continue
        if not is_broad and (row.get("fallback") or not _normalize_optional_label(row.get("primary_narrative"))):
            continue
        raw_label = _normalize_optional_label(row.get(label_key)) or _normalize_optional_label(row.get("signal_label")) or "其他讨论"
        label = (narrative_map or {}).get(raw_label, raw_label) if not is_broad else raw_label
        note = notes_by_id[note_id]
        bucket = grouped.setdefault(
            label,
            {
                "label": label,
                "field": label_key,
                "volume": 0,
                "engagement_sum": 0,
                "content_type_counts": Counter(),
                "sentiment_counts": Counter(),
                "author_type_counts": Counter(),
                "narrative_stance_counts": Counter(),
                "topic_relevance_counts": Counter(),
                "signal_labels": [],
                "signal_label_counts": Counter(),
                "issue_labels": [],
                "note_ids": [],
                "evidence_items": [],
                "earliest_post_date": "",
                "latest_post_date": "",
                "recent_7d_count": 0,
                "recent_30d_count": 0,
                "dated_mention_count": 0,
                "undated_mention_count": 0,
                "_post_dates": [],
            },
        )
        if is_broad:
            bucket.setdefault("risk_level_counts", Counter())
        bucket["volume"] += 1
        score = _engagement_score(note)
        bucket["engagement_sum"] += score
        bucket["note_ids"].append(note_id)
        bucket["topic_relevance_counts"][str(row.get("topic_relevance") or "unknown")] += 1
        bucket["content_type_counts"][str(row.get("content_type") or "other")] += 1
        bucket["sentiment_counts"][str(row.get("sentiment") or "neutral")] += 1
        bucket["author_type_counts"][str(row.get("author_type") or "unclear")] += 1
        if is_broad:
            bucket["risk_level_counts"][_normalize_risk_level(row.get("risk_level"))] += 1
        if not is_broad:
            bucket["narrative_stance_counts"][_normalize_narrative_stance(row.get("narrative_stance"))] += 1
        for list_key, value_key in (("signal_labels", "signal_label"), ("issue_labels", "issue_label")):
            value = _normalize_optional_label(row.get(value_key))
            if value and value not in bucket[list_key] and len(bucket[list_key]) < 6:
                bucket[list_key].append(value)
            if list_key == "signal_labels" and value:
                bucket["signal_label_counts"][value] += 1
        quote = str(row.get("evidence_quote") or note.get("title") or "").strip()
        if quote:
            bucket["evidence_items"].append(
                {
                    "evidence_id": _stable_id("evidence", f"{note_id}:{quote}"),
                    "quote": _short_evidence_quote(quote),
                    "note_id": note_id,
                    "post_url": str(note.get("post_url") or ""),
                    "date": _note_date_text(note),
                    "engagement": score,
                    "post_title": str(note.get("title") or "")[:160],
                    "signal_label": _normalize_optional_label(row.get("signal_label")) or "",
                    "primary_narrative": _normalize_optional_label(row.get("primary_narrative")) or "",
                }
            )
        post_date = _note_date(note)
        if post_date is not None:
            bucket["_post_dates"].append(post_date)
        else:
            bucket["undated_mention_count"] += 1
    rows: list[dict[str, Any]] = []
    for bucket in grouped.values():
        _finalize_signal_dates(bucket)
        bucket["note_ids"] = sorted(set(bucket["note_ids"]))
        bucket["cluster_id"] = _stable_id("cluster", bucket["label"])
        bucket["evidence_items"] = _rank_evidence_items(
            bucket["evidence_items"],
            limit=max(5, len(bucket["note_ids"])),
        )
        note_scores = sorted((_engagement_score(notes_by_id[note_id]) for note_id in bucket["note_ids"]), reverse=True)
        bucket["top_post_engagement"] = note_scores[0] if note_scores else 0
        bucket["top_post_share"] = round(bucket["top_post_engagement"] / bucket["engagement_sum"], 4) if bucket["engagement_sum"] else 0.0
        count_keys = ["content_type_counts", "sentiment_counts", "author_type_counts", "narrative_stance_counts", "topic_relevance_counts", "signal_label_counts"]
        if is_broad:
            count_keys.append("risk_level_counts")
        for key in count_keys:
            bucket[key] = dict(bucket[key])
        rows.append(bucket)
    return sorted(rows, key=lambda item: (item["engagement_sum"], item["volume"]), reverse=True)


def build_uncertainty_table(
    notes: list[dict[str, Any]],
    annotations: list[dict[str, Any]],
    narrative_comment_table: Optional[list[dict[str, Any]]] = None,
) -> list[dict[str, Any]]:
    """Group questions by the LLM-provided type plus exact question text.

    分组只用「LLM 给的 uncertainty_type + 归一化后的问题原文」做精确匹配：
    大小写、空白和尾部标点会被抹平，但不做任何语义近似判断。
    合并不了的就各自成组 —— 与其用手写词表猜两个问题是不是同一件事，
    不如保留 LLM 给出的分类。
    """

    notes_by_id = {str(note.get("note_id")): note for note in notes}
    grouped: list[dict[str, Any]] = []

    def bucket_for(label: str, question: str) -> dict[str, Any]:
        uncertainty_type = _normalize_optional_label(label) or "其他不确定点"
        title = _normalize_uncertainty_question(question) or uncertainty_type
        group_key = f"{_uncertainty_match_key(uncertainty_type)}|{_uncertainty_match_key(title)}"
        for existing in grouped:
            if existing["_group_key"] == group_key:
                return existing
        bucket = {
            "uncertainty_id": _stable_id("uncertainty", group_key),
            "title": title,
            "question": title,
            "uncertainty_type": uncertainty_type,
            "_group_key": group_key,
            "count": 0,
            "support_count": 0,
            "independent_source_count": 0,
            "post_count": 0,
            "comment_question_count": 0,
            "engagement_sum": 0,
            "uncertainty_texts": [],
            "note_ids": [],
            "comment_ids": [],
            "evidence_items": [],
            "_source_keys": set(),
        }
        grouped.append(bucket)
        return bucket

    for row in annotations:
        if not row.get("has_uncertainty"):
            continue
        note_id = str(row.get("note_id") or "")
        note = notes_by_id.get(note_id)
        if not note:
            continue
        uncertainty_text = str(row.get("uncertainty_text") or "").strip()
        bucket = bucket_for(
            _normalize_optional_label(row.get("uncertainty_type")) or "其他不确定点",
            uncertainty_text,
        )
        source_key = f"post:{note_id}"
        if source_key in bucket["_source_keys"]:
            continue
        bucket["_source_keys"].add(source_key)
        bucket["note_ids"].append(note_id)
        score = _engagement_score(note)
        bucket["engagement_sum"] += score
        if uncertainty_text and uncertainty_text not in bucket["uncertainty_texts"] and len(bucket["uncertainty_texts"]) < 8:
            bucket["uncertainty_texts"].append(uncertainty_text[:120])
        # 证据直接用标注阶段已做过原文校验的 evidence_quote；
        # 报告层的证据选择由 LLM 的 evidence_ids 负责，这里不再挑"最贴合"的句子。
        quote = str(row.get("evidence_quote") or "").strip() or uncertainty_text
        if quote:
            bucket["evidence_items"].append(
                {
                    "evidence_id": _stable_id("evidence", f"{note_id}:{quote}"),
                    "quote": _short_evidence_quote(quote),
                    "note_id": note_id,
                    "post_url": str(note.get("post_url") or ""),
                    "date": _note_date_text(note),
                    "engagement": score,
                    "source": "post",
                    "signal_label": str(row.get("signal_label") or ""),
                    "post_title": str(note.get("title") or "")[:160],
                }
            )
    for row in narrative_comment_table or []:
        if not isinstance(row, dict):
            continue
        question_comments = [item for item in row.get("question_comments") or [] if isinstance(item, dict)]
        if not question_comments:
            continue
        for item in question_comments:
            content = str(item.get("content") or "").strip()
            if not content:
                continue
            label = _normalize_optional_label(row.get("narrative") or row.get("label")) or "评论追问"
            bucket = bucket_for(label, content)
            note_id = str(item.get("note_id") or "")
            note = notes_by_id.get(note_id, {})
            like_count = to_int(item.get("like_count"))
            comment_id = str(item.get("comment_id") or "")
            source_key = f"post:{note_id}" if note_id else f"comment:{comment_id or content}"
            if source_key in bucket["_source_keys"]:
                continue
            bucket["_source_keys"].add(source_key)
            if note_id and note_id not in bucket["note_ids"]:
                bucket["note_ids"].append(note_id)
            bucket["comment_ids"].append(comment_id or _stable_id("comment", f"{note_id}:{content}"))
            bucket["engagement_sum"] += like_count
            text = content[:120]
            if text not in bucket["uncertainty_texts"] and len(bucket["uncertainty_texts"]) < 8:
                bucket["uncertainty_texts"].append(text)
            if content:
                bucket["evidence_items"].append(
                    {
                        "evidence_id": _stable_id("evidence", f"comment:{comment_id or note_id}:{content}"),
                        "quote": _short_evidence_quote(content),
                        "note_id": note_id,
                        "post_url": str(note.get("post_url") or ""),
                        "date": _note_date_text(note) if note else "",
                        "engagement": like_count,
                        "source": "comment",
                        "post_title": str(note.get("title") or "")[:160] if note else "",
                    }
                )
    rows: list[dict[str, Any]] = []
    for bucket in grouped:
        bucket.pop("_source_keys")
        bucket.pop("_group_key")
        bucket["note_ids"] = sorted(set(bucket["note_ids"]))
        bucket["comment_ids"] = sorted(set(bucket["comment_ids"]))
        bucket["post_count"] = len(bucket["note_ids"])
        bucket["supporting_post_ids"] = list(bucket["note_ids"])
        bucket["support_count"] = bucket["post_count"]
        bucket["independent_source_count"] = bucket["support_count"]
        bucket["count"] = bucket["support_count"]
        bucket["comment_question_count"] = len(bucket["comment_ids"])
        bucket["evidence_items"] = _rank_evidence_items(bucket["evidence_items"], limit=5)
        bucket["evidence_ids"] = [
            str(item.get("evidence_id") or "")
            for item in bucket["evidence_items"]
            if item.get("evidence_id")
        ]
        rows.append(bucket)
    return sorted(rows, key=lambda item: (item["independent_source_count"], item["engagement_sum"]), reverse=True)


def _uncertainty_match_key(value: Any) -> str:
    """精确匹配用的归一化键：只抹平大小写/空白/标点，不做任何语义判断。"""

    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", str(value or "").lower())


def _normalize_uncertainty_question(value: Any) -> str:
    text = " ".join(str(value or "").strip().split())[:120]
    return text.rstrip("。；;，,")


def build_theme_table(notes: list[dict[str, Any]], annotations: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按 HKU listener theme 做代码聚合，给报告提供稳定 volume。"""

    notes_by_id = {str(note.get("note_id")): note for note in notes}
    grouped: dict[str, dict[str, Any]] = {}
    for row in annotations:
        note_id = str(row.get("note_id") or "")
        if note_id not in notes_by_id:
            continue
        theme = _normalize_theme(row.get("theme"))
        bucket = grouped.setdefault(
            theme,
            {
                "theme": theme,
                "volume": 0,
                "theme_volume": 0,
                "engagement_sum": 0,
                "content_type_counts": Counter(),
                "sentiment_counts": Counter(),
                "evidence_quotes": [],
            },
        )
        bucket["volume"] += 1
        bucket["theme_volume"] += 1
        bucket["engagement_sum"] += _engagement_score(notes_by_id[note_id])
        bucket["content_type_counts"][str(row.get("content_type") or "other")] += 1
        bucket["sentiment_counts"][str(row.get("sentiment") or "neutral")] += 1
        quote = str(row.get("evidence_quote") or row.get("signal_label") or "").strip()
        if quote and len(bucket["evidence_quotes"]) < 5:
            bucket["evidence_quotes"].append(quote[:180])

    rows: list[dict[str, Any]] = []
    for bucket in grouped.values():
        content_counts = bucket.pop("content_type_counts")
        sentiment_counts = bucket.pop("sentiment_counts")
        bucket["dominant_content_type"] = content_counts.most_common(1)[0][0] if content_counts else "other"
        bucket["content_type_counts"] = dict(content_counts)
        bucket["sentiment_counts"] = dict(sentiment_counts)
        rows.append(bucket)
    return sorted(rows, key=lambda item: (item["volume"], item["engagement_sum"]), reverse=True)


def build_comment_signal_table(
    comments: list[dict[str, Any]],
    notes: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """用代码按 note 聚合评论信号，并挂回所属帖子的分析上下文。"""

    grouped: dict[str, dict[str, Any]] = {}
    notes_by_id = {str(row.get("note_id")): row for row in (notes or [])}
    annotation_by_id = {str(row.get("note_id")): row for row in (annotations or [])}
    for comment in comments:
        note_id = str(comment.get("note_id") or "")
        content = str(comment.get("content") or "").strip()
        if not note_id or not content:
            continue
        like_count = to_int(comment.get("like_count"))
        note = notes_by_id.get(note_id, {})
        annotation = annotation_by_id.get(note_id, {})
        bucket = grouped.setdefault(
            note_id,
            {
                "note_id": note_id,
                "post_url": str(note.get("post_url") or ""),
                "post_title": str(note.get("title") or ""),
                "post_theme": str(annotation.get("theme") or ""),
                "post_primary_narrative": str(annotation.get("primary_narrative") or ""),
                "post_signal": str(annotation.get("signal_label") or ""),
                "post_content_type": str(annotation.get("content_type") or ""),
                "post_sentiment": str(annotation.get("sentiment") or ""),
                "comment_count": 0,
                "like_sum": 0,
                "question_count": 0,
                "top_comments": [],
            },
        )
        bucket["comment_count"] += 1
        bucket["like_sum"] += like_count
        if _looks_like_question(content):
            bucket["question_count"] += 1
        bucket["top_comments"].append({"content": content[:180], "like_count": like_count})

    rows = []
    for bucket in grouped.values():
        bucket["top_comments"] = sorted(
            bucket["top_comments"],
            key=lambda item: item["like_count"],
            reverse=True,
        )[:5]
        rows.append(bucket)
    return sorted(rows, key=lambda item: (item["comment_count"], item["like_sum"]), reverse=True)


def build_narrative_comment_table(
    comments: list[dict[str, Any]],
    notes: list[dict[str, Any]] | None = None,
    annotations: list[dict[str, Any]] | None = None,
    scan_mode: str = "topic_scan",
    narrative_map: Optional[dict[str, str]] = None,
) -> list[dict[str, Any]]:
    """把一级评论挂回所属帖子的 Topic narrative；Broad 下按 theme 聚合。"""

    notes_by_id = {str(row.get("note_id")): row for row in (notes or [])}
    annotation_by_id = {str(row.get("note_id")): row for row in (annotations or [])}
    grouped: dict[str, dict[str, Any]] = {}
    is_broad = scan_mode == "broad_scan"
    for comment in comments:
        if comment.get("parent_comment_id"):
            continue
        note_id = str(comment.get("note_id") or "")
        content = str(comment.get("content") or "").strip()
        if not note_id or not content or note_id not in notes_by_id:
            continue
        annotation = annotation_by_id.get(note_id, {})
        raw_label = (
            _normalize_theme(annotation.get("theme"))
            if is_broad
            else _normalize_optional_label(annotation.get("primary_narrative"))
        )
        label = raw_label or _normalize_optional_label(annotation.get("signal_label")) or "其他讨论"
        if not is_broad:
            label = (narrative_map or {}).get(label, label)
        bucket = grouped.setdefault(
            label,
            {
                "narrative": label,
                "cluster_id": _stable_id("cluster", label),
                "comment_count": 0,
                "like_sum": 0,
                "question_count": 0,
                "note_ids": [],
                "top_comments": [],
                "question_comments": [],
            },
        )
        bucket["comment_count"] += 1
        like_count = to_int(comment.get("like_count"))
        bucket["like_sum"] += like_count
        if note_id not in bucket["note_ids"] and len(bucket["note_ids"]) < 12:
            bucket["note_ids"].append(note_id)
        compact = {
            "content": content[:180],
            "like_count": like_count,
            "note_id": note_id,
            "comment_id": str(comment.get("comment_id") or ""),
        }
        bucket["top_comments"].append(compact)
        if _looks_like_question(content):
            bucket["question_count"] += 1
            if len(bucket["question_comments"]) < 8:
                bucket["question_comments"].append(compact)

    rows: list[dict[str, Any]] = []
    for bucket in grouped.values():
        bucket["top_comments"] = sorted(
            bucket["top_comments"],
            key=lambda item: item["like_count"],
            reverse=True,
        )[:8]
        bucket["question_comments"] = sorted(
            bucket["question_comments"],
            key=lambda item: item["like_count"],
            reverse=True,
        )[:8]
        rows.append(bucket)
    return sorted(rows, key=lambda item: (item["comment_count"], item["like_sum"]), reverse=True)


def _analysis_relevant_ids(
    annotations: list[dict[str, Any]],
    scan_mode: str,
) -> set[str]:
    """最终分析样本过滤：Broad 看 HKU 相关性，Topic 看 LLM 的 topic_relevance。"""

    relevant_ids: set[str] = set()
    for row in annotations:
        note_id = str(row.get("note_id"))
        hku_rel = str(row.get("hku_relevance") or "").strip().lower()
        if hku_rel not in {"direct", "indirect"}:
            continue
        if scan_mode == "broad_scan":
            relevant_ids.add(note_id)
            continue
        topic_rel = str(row.get("topic_relevance") or "").strip().lower()
        if topic_rel in {"direct", "indirect"}:
            relevant_ids.add(note_id)
    return relevant_ids


def _topic_terms_from_keyword(keyword: str) -> list[str]:
    """从关键词中抽出非 HKU 的主题词；同义扩展只来自配置，不在函数里写死。"""

    lowered = str(keyword or "").lower().strip()
    if not lowered:
        return []
    for alias in HKU_ALIASES:
        lowered = lowered.replace(alias, " ")
    raw_terms = [part.strip(" ,，/|;；:：") for part in lowered.split()]
    deduped: list[str] = []
    for term in raw_terms:
        if not term or term in {"hk", "香港", "大学", "学校", "學校"}:
            continue
        value = term.strip().lower()
        if value and value not in deduped:
            deduped.append(value)
    return deduped


def _code_hku_relevance(record: dict[str, Any], scan_mode: str = "") -> dict[str, Any]:
    """Code-only HKU relevance gate.

    It checks title + body + tags, but not comments, so a comment cannot pull
    an otherwise unrelated post into HKU scope.

    Broad 和 Topic 都不把召回关键词算作证据。关键词由所有搜索结果继承，
    把它当证据等于“搜出来的就是相关的”循环论证。
    """

    fields = [
        ("title", record.get("title")),
        ("body", record.get("text") or record.get("content_full") or record.get("body")),
        ("tags", " ".join(str(item) for item in record.get("tags") or [])),
    ]
    if scan_mode == "broad_scan":
        combined = " ".join(str(value or "") for _, value in fields)
        relevance, reason = _hkubs_broad_relevance(combined)
        return {
            "note_id": str(record.get("note_id") or ""),
            "hku_relevance": relevance,
            "topic_relevance": "unrelated" if relevance == "unrelated" else "indirect",
            "relevance_reason": f"HKUBS code rule: {reason}",
            "hku_match_reason": f"HKUBS rule: {reason}",
            "gate_source": "code_hkubs",
            "annotation_schema_version": _annotation_schema_version(scan_mode),
        }
    for field, value in fields:
        text = str(value or "")
        if not text.strip():
            continue
        match = re.search(HKU_SCOPE_PATTERN, text)
        if match:
            return {
                "note_id": str(record.get("note_id") or ""),
                "hku_relevance": "direct",
                "topic_relevance": "indirect" if scan_mode == "broad_scan" else "unrelated",
                "relevance_reason": f"HKU code rule matched {field}: {match.group(0)}",
                "hku_match_reason": f"{field} matched HKU rule: {match.group(0)}",
                "gate_source": "code_hku",
                "annotation_schema_version": _annotation_schema_version(scan_mode),
            }
    return {
        "note_id": str(record.get("note_id") or ""),
        "hku_relevance": "unrelated",
        "topic_relevance": "unrelated",
        "relevance_reason": "HKU code rule found no match in title/body/tags.",
        "hku_match_reason": "no HKU match in title/body/tags",
        "gate_source": "code_hku",
        "annotation_schema_version": _annotation_schema_version(scan_mode),
    }

def _normalize_monitoring_text(value: Any) -> str:
    """
    Broad monitoring programme matching 专用规范化。

    - lowercase
    - 去掉空格、标点、括号、连字符等
    - 保留中文、英文字母和数字

    例如：
        "HKU MSc(BA)" -> "hkumscba"
        "港大 金融学（金融科技）硕士" -> "港大金融学金融科技硕士"
    """
    return re.sub(
        r"[^0-9a-z\u4e00-\u9fff]+",
        "",
        str(value or "").lower(),
    )


def _match_monitored_programme(text: str) -> tuple[str | None, str | None]:
    """
    判断文本是否命中 monitoring brief 中的具体 programme。

    返回：
        (canonical_programme, matched_alias)

    如果同时命中多个 alias，优先采用最长 alias，
    避免例如：
        Global Management
    被较短的：
        Management
    抢先匹配。
    """
    normalized_text = _normalize_monitoring_text(text)

    if not normalized_text:
        return None, None

    matches: list[tuple[int, str, str]] = []

    for programme, aliases in MONITORED_PROGRAMMES.items():
        for alias in aliases:
            normalized_alias = _normalize_monitoring_text(alias)

            if not normalized_alias:
                continue

            if normalized_alias.endswith("mgm") and _contains_mgm_venue_reference(text):
                continue
            if _is_short_english_abbreviation(normalized_alias):
                matched = _matches_short_programme_abbreviation(text, normalized_alias)
            else:
                matched = normalized_alias in normalized_text

            if matched:
                matches.append(
                    (
                        len(normalized_alias),
                        programme,
                        alias,
                    )
                )

    if not matches:
        return None, None

    # 最长 alias 优先，减少 programme overlap 导致的误分类。
    matches.sort(key=lambda item: item[0], reverse=True)

    _, programme, alias = matches[0]
    return programme, alias


def _is_short_english_abbreviation(normalized_alias: str) -> bool:
    """Return whether an alias needs token-aware matching instead of substring matching."""

    return bool(re.fullmatch(r"[a-z0-9]{2,4}", normalized_alias))


def _contains_mgm_venue_reference(text: str) -> bool:
    return bool(
        re.search(
            r"(?<![0-9a-z])mgm\s*(?:macau\b|澳门|酒店|casino\b|resort\b)",
            str(text or ""),
            re.IGNORECASE,
        )
    )


def _matches_short_programme_abbreviation(text: str, abbreviation: str) -> bool:
    """Match short programme abbreviations without joining unrelated words.

    A short abbreviation must be an independent ASCII token and must either be
    directly qualified by HKU/港大 or appear near programme-related context.
    This keeps genuine forms such as ``HKU MFin`` and ``MFin offer`` while
    rejecting collapsed substrings such as ``team final`` -> ``mfin`` and an
    unrelated venue mention such as ``MGM Macau``.
    """

    token_pattern = re.compile(
        rf"(?<![0-9a-z]){re.escape(abbreviation)}(?![0-9a-z])",
        re.IGNORECASE,
    )
    hku_qualifier_pattern = re.compile(
        r"(?:\bhku\b|香港大学|港大)\s*[-–—|:/]?\s*$",
        re.IGNORECASE,
    )
    programme_context_pattern = re.compile(
        r"(?:\b(?:master|msc|programme|program|degree|admission|offer|student|cohort|course|curriculum)\b|"
        r"硕士|项目|专业|课程|申请|录取|入学|就读|毕业|学费|就业)",
        re.IGNORECASE,
    )

    raw_text = str(text or "")
    for match in token_pattern.finditer(raw_text):
        prefix = raw_text[max(0, match.start() - 24):match.start()]
        if hku_qualifier_pattern.search(prefix):
            return True
        nearby_prefix = raw_text[max(0, match.start() - 24):match.start()]
        nearby_suffix = raw_text[match.end():min(len(raw_text), match.end() + 24)]
        if (
            re.search(rf"(?:{programme_context_pattern.pattern})[\s:：/|()（）-]{{0,8}}$", nearby_prefix, re.IGNORECASE)
            or re.match(rf"^[\s:：/|()（）-]{{0,8}}(?:{programme_context_pattern.pattern})", nearby_suffix, re.IGNORECASE)
        ):
            return True
    return False


def _hkubs_broad_relevance(text: str) -> tuple[str, str]:
    """
    Broad Scan monitoring scope gate。

    Broad Scan 不使用 LLM 判断 scope，只使用确定性代码规则。

    relevant 的两种情况：

    1. direct
       帖子自身标题 / 正文 / tag 明确出现：
       HKUBS / HKU Business School / 港大商学院 / 港大经管学院 /
       港大商科等明确 scope term。

    2. indirect
       帖子自身明确存在 HKU / 港大语境，
       同时命中 monitoring brief 中某一个具体 programme。

    以下内容本身不能作为 programme relevance evidence：
       硕士、申请、offer、录取、学费、奖学金、
       课程、体验、就业、实习等。

    因此：
       “港大BA真实体验” -> relevant
       “港大金融就业怎么样” -> relevant
       “港大文学硕士体验” -> unrelated
       “港大硕士申请难吗” -> unrelated
       “港大就业怎么样” -> unrelated

    搜索 keyword 本身不作为 relevance evidence；
    只判断帖子自身 title/body/tags。
    """
    raw_text = str(text or "")
    normalized_text = _normalize_monitoring_text(raw_text)

    if not normalized_text:
        return "unrelated", "帖子无可用于 scope 判断的文本"

    # ---------------------------------------------------------------
    # 1. 明确 HKUBS / Business School / 经管 / 商科表达
    # ---------------------------------------------------------------
    direct_matches: list[str] = []

    for term in HKUBS_DIRECT_TERMS:
        normalized_term = _normalize_monitoring_text(term)

        if normalized_term and normalized_term in normalized_text:
            direct_matches.append(term)

    if direct_matches:
        # 优先展示最长的 direct evidence，reason 更清楚。
        best_match = max(
            direct_matches,
            key=lambda item: len(_normalize_monitoring_text(item)),
        )

        return (
            "direct",
            f"命中明确 monitoring scope term: {best_match}",
        )

    # ---------------------------------------------------------------
    # 2. 没写商学院，但必须先确认帖子自身存在 HKU / 港大语境
    # ---------------------------------------------------------------
    programme, matched_alias = _match_monitored_programme(raw_text)
    hku_match = re.search(HKU_SCOPE_PATTERN, raw_text)
    alias_has_hku_qualifier = bool(
        matched_alias
        and _normalize_monitoring_text(matched_alias).startswith(("hku", "港大", "香港大学"))
    )

    if not hku_match and not alias_has_hku_qualifier:
        return (
            "unrelated",
            "未发现 HKU / 港大语境",
        )

    # ---------------------------------------------------------------
    # 3. HKU + 明确商学院 / 商科 / 经管语境
    # ---------------------------------------------------------------
    business_context_matches = [
        term
        for term in HKUBS_CONTEXT_TERMS
        if _normalize_monitoring_text(term) in normalized_text
    ]

    if business_context_matches:
        best_match = max(
            business_context_matches,
            key=lambda item: len(_normalize_monitoring_text(item)),
        )
        return (
            "indirect",
            f"命中 HKU + business-school context: {best_match}",
        )

    # ---------------------------------------------------------------
    # 4. HKU + monitoring programme
    # ---------------------------------------------------------------
    if programme:
        return (
            "indirect",
            f"命中 HKU + monitored programme: {programme} [{matched_alias}]",
        )

    # ---------------------------------------------------------------
    # 5. 只有港大 + 泛主题，不足以证明属于 programme monitoring scope
    # ---------------------------------------------------------------
    return (
        "unrelated",
        "存在 HKU / 港大语境，但未命中明确学院或 monitored programme",
    )

def _looks_like_question(value: str) -> bool:
    """判断一条评论是否在提问。

    保留说明：评论从不进入 LLM 标注（见 _build_annotation_records），所以这里不是
    "用词表重新解释 LLM 输出"，而是 question_comments / question_count 唯一的产生方式。
    删掉它会直接移除 Topic 的"评论追问 -> uncertainty_table"这条产品链路，
    因此在本轮清理中保留；若要去掉该功能，应连同下游一起下线。
    """

    markers = ("?", "？", "吗", "么", "咋", "怎么", "如何", "求问", "请问", "想问", "有没有", "可以吗")
    return any(marker in value for marker in markers)


def _normalize_signal_label(value: Any) -> str:
    """轻量归一 signal 名称，保留 emergent label，但减少空白造成的重复。"""

    label = " ".join(str(value or "").strip().split())
    return label[:80] or "uncategorized_signal"


def _normalize_optional_label(value: Any) -> str:
    label = " ".join(str(value or "").strip().split())
    return label[:80]


def _normalize_author_type(value: Any, signal_types: Optional[list[str]] = None) -> str:
    text = str(value or "").strip()
    if "commercial" in set(signal_types or []):
        return "agency_marketing"
    return text if text in {"real_user", "agency_marketing", "unclear"} else "unclear"


def _normalize_risk_level(value: Any) -> str:
    text = str(value or "").strip().lower()
    return text if text in {"high", "medium", "low", "none"} else "none"


def _normalize_risk_type(value: Any, is_broad: bool) -> str:
    allowed = {"misunderstanding", "cost_concern", "decision_impact", "reputation", "operational", "none"}
    if is_broad:
        allowed.add("service_friction")
    text = str(value or "none").strip().lower()
    return text if text in allowed else "none"


def _normalize_narrative_stance(value: Any) -> str:
    text = str(value or "neutral").strip().lower()
    return text if text in {"positive", "negative", "mixed", "neutral"} else "neutral"


def _max_risk(left: Any, right: Any) -> str:
    order = {"none": 0, "low": 1, "medium": 2, "high": 3}
    a = _normalize_risk_level(left)
    b = _normalize_risk_level(right)
    return a if order[a] >= order[b] else b


def _normalize_content_type(value: Any) -> str:
    text = str(value or "other").strip()
    return text if text in CONTENT_TYPE_VALUES else "other"


def _normalize_theme(value: Any) -> str:
    text = str(value or "Other").strip()
    return text if text in THEME_VALUES else "Other"


def _note_date(note: dict[str, Any]) -> Optional[datetime]:
    """只解析真实发布时间，绝不回退 collected_at。

    采集日期不是发布日期：用 collected_at 兜底会让所有信号看起来都是"最近 7 天"，
    时间结论直接失真。没有发布时间就返回 None，由调用方明确处理"日期未知"。
    """

    for key in ("published_at", "published_at_raw"):
        parsed = _parse_datetime(note.get(key))
        if parsed is not None:
            return parsed
    return None


def _time_coverage(notes: list[dict[str, Any]]) -> dict[str, Any]:
    """统计本轮分析样本里有多少帖子带可解析发布时间，供报告声明时间统计覆盖范围。"""

    dates = sorted(date for note in notes if (date := _note_date(note)) is not None)
    return {
        "notes_with_publish_date": len(dates),
        "notes_without_publish_date": len(notes) - len(dates),
        "earliest_post_date": dates[0].date().isoformat() if dates else "",
        "latest_post_date": dates[-1].date().isoformat() if dates else "",
    }


def _note_date_text(note: dict[str, Any]) -> str:
    parsed = _note_date(note)
    return parsed.date().isoformat() if parsed is not None else ""


def _parse_datetime(value: Any) -> Optional[datetime]:
    if value in (None, ""):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        try:
            number = int(text)
            if number > 10_000_000_000:
                number = number // 1000
            return datetime.fromtimestamp(number)
        except (OverflowError, OSError, ValueError):
            return None
    normalized = text.replace("Z", "+00:00")
    for candidate in (normalized, normalized.replace("/", "-")):
        try:
            return datetime.fromisoformat(candidate).replace(tzinfo=None)
        except ValueError:
            pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y年%m月%d日", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            pass
    return None


def _finalize_signal_dates(bucket: dict[str, Any]) -> None:
    dates = sorted(date for date in bucket.pop("_post_dates", []) if isinstance(date, datetime))
    bucket["dated_mention_count"] = len(dates)
    if not dates:
        return
    latest = dates[-1]
    earliest = dates[0]
    # Relative-to-sample windows are stable when an archived run is re-analysed later.
    reference = latest
    bucket["earliest_post_date"] = earliest.date().isoformat()
    bucket["latest_post_date"] = latest.date().isoformat()
    bucket["recent_7d_count"] = sum(1 for date in dates if reference - date <= timedelta(days=7))
    bucket["recent_30d_count"] = sum(1 for date in dates if reference - date <= timedelta(days=30))


def _short_evidence_quote(value: Any, limit: int = 80) -> str:
    """截断证据引文：优先在句读处断开，避免报告里出现生硬的半句话。"""

    text = " ".join(str(value or "").strip().split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in ("。", "；", "！", "？", "，", " "):
        idx = cut.rfind(sep)
        if idx >= limit // 2:
            return cut[: idx + 1].rstrip()
    return cut + "…"


def _stable_id(prefix: str, value: Any) -> str:
    normalized = re.sub(r"\s+", "", str(value or "").strip().lower())
    digest = hashlib.sha1(normalized.encode("utf-8")).hexdigest()[:12]
    return f"{prefix}_{digest}"


def _rank_evidence_items(value: Any, limit: int = 5) -> list[dict[str, Any]]:
    """Remove empty/duplicate evidence and retain the strongest source-backed quotes."""

    ranked: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in value if isinstance(value, list) else []:
        if not isinstance(item, dict):
            continue
        quote = " ".join(str(item.get("quote") or "").split()).strip()
        if len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", quote)) < 2:
            continue
        key = (str(item.get("note_id") or ""), quote)
        if key in seen:
            continue
        seen.add(key)
        current = dict(item)
        current["quote"] = quote
        current["evidence_id"] = current.get("evidence_id") or _stable_id("evidence", f"{key[0]}:{quote}")
        ranked.append(current)
    ranked.sort(key=lambda item: (to_int(item.get("engagement")), len(str(item.get("quote") or ""))), reverse=True)
    return ranked[:limit]


def _normalize_signal_types(value: Any) -> list[str]:
    """signal 是多维标签；不确定时只保留 other。"""

    allowed = {"operational", "informational", "emotional", "identity", "commercial", "other"}
    if isinstance(value, list):
        raw_items = value
    elif value:
        raw_items = [value]
    else:
        raw_items = ["other"]
    out: list[str] = []
    for item in raw_items:
        text = str(item).strip()
        if text in allowed and text not in out:
            out.append(text)
    return out or ["other"]


def _engagement_score(note: dict[str, Any]) -> int:
    return (
        to_int(note.get("like_count"))
        + 2 * to_int(note.get("collect_count"))
        + 3 * to_int(note.get("comment_count"))
        + to_int(note.get("share_count"))
    )


def _log(logs: list[str], log_queue: Optional[Any], message: str) -> None:
    line = timestamped(message)
    logs.append(line)
    emit_log(log_queue, message)


def _check_stop(stop_checker: Optional[Callable[[], None]]) -> None:
    if stop_checker is not None:
        stop_checker()


def _merge_usage_rows(path: Path, new_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    existing: list[dict[str, Any]] = []
    if path.exists():
        payload = read_json(path)
        rows = payload.get("usage") if isinstance(payload, dict) else []
        existing = [row for row in rows if isinstance(row, dict)]
    return existing + [row for row in new_rows if row]
