"""第二步：把采集阶段已打标签的数据筛成分析可用数据。"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from xhs_listener.collect import _counts_from_tree, _published_at_from_tree
from xhs_listener.io_utils import read_json, read_jsonl, write_json, write_jsonl
from xhs_listener.number_utils import to_int


# 处理层是“确定性闸门”：不调用外部接口，不调用 LLM。
# 它只根据采集标签、字段完整度、去重规则和数字转换，产出后续分析稳定可用的数据。
def process_run(run_dir: str | Path) -> dict[str, Any]:
    """处理一个采集 run 目录，输出 processed_notes/comments 和 processing.json。"""

    run_path = Path(run_dir)
    collection = read_json(run_path / "collection.json")
    raw_notes = read_jsonl(run_path / "notes.jsonl")
    raw_comments = read_jsonl(run_path / "comments.jsonl")

    kept_notes: list[dict[str, Any]] = []
    removed_notes: list[dict[str, Any]] = []
    seen_note_ids: set[str] = set()
    seen_content_fingerprints: set[str] = set()

    # 笔记先清洗再筛选；被过滤的样本保留 note_id 和原因，方便前端解释“为什么少了”。
    for note in raw_notes:
        cleaned, reasons = _clean_note(note, seen_note_ids, seen_content_fingerprints)
        if reasons:
            removed_notes.append(
                {
                    "note_id": note.get("note_id"),
                    "reasons": reasons,
                    "collector_reasons": note.get("skip_reasons") or [],
                }
            )
            continue
        kept_notes.append(cleaned)

    kept_note_ids = {str(note["note_id"]) for note in kept_notes}
    kept_comments: list[dict[str, Any]] = []
    removed_comments: list[dict[str, Any]] = []
    seen_comment_keys: set[tuple[str, str, str]] = set()

    # 评论只保留属于 processed notes 的内容，避免分析阶段看到已经被剔除帖子的评论。
    for comment in raw_comments:
        cleaned, reasons = _clean_comment(comment, kept_note_ids, seen_comment_keys)
        if reasons:
            removed_comments.append(
                {
                    "note_id": comment.get("note_id"),
                    "comment_id": comment.get("comment_id"),
                    "reasons": reasons,
                }
            )
            continue
        kept_comments.append(cleaned)

    report = {
        "keyword": collection.get("keyword"),
        "scan_mode": collection.get("scan_mode", "topic_scan"),
        "keyword_pool": collection.get("keyword_pool", []),
        "run_dir": str(run_path),
        "raw_notes": len(raw_notes),
        "processed_notes": len(kept_notes),
        "removed_notes": len(removed_notes),
        "raw_comments": len(raw_comments),
        "processed_comments": len(kept_comments),
        "removed_comments": len(removed_comments),
        "removed_note_reasons": _count_reasons(removed_notes),
        "removed_comment_reasons": _count_reasons(removed_comments),
        "removed_note_samples": removed_notes[:20],
        "removed_comment_samples": removed_comments[:20],
        "collection_quality": collection.get("quality") if isinstance(collection.get("quality"), dict) else {},
        "comment_collection_status": _comment_collection_status(collection),
        # 周报窗口与采集漏斗原样透传：窗口过滤在采集阶段就已完成，
        # 处理阶段不重算，历史 run（collection.json 里没有 reporting_window）因此保持原样。
        "reporting_window": _reporting_window(collection),
        "collection_funnel": _collection_funnel(collection, len(raw_notes), len(kept_notes)),
        "keyword_funnel": _keyword_funnel(collection),
    }

    write_jsonl(run_path / "processed_notes.jsonl", kept_notes)
    write_jsonl(run_path / "processed_comments.jsonl", kept_comments)
    write_json(run_path / "processing.json", report)
    return report


def _clean_note(
    note: dict[str, Any],
    seen_note_ids: set[str],
    seen_content_fingerprints: set[str] | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """按采集标签筛选笔记，再做必要字段检查、文本清洗和数值转换。"""

    reasons: list[str] = []
    note_id = _clean_text(note.get("note_id"))
    if not note_id:
        reasons.append("missing_note_id")
    elif note_id in seen_note_ids:
        reasons.append("duplicate_note_id")
    else:
        seen_note_ids.add(note_id)

    if note.get("is_valid") is False:
        reasons.append("collector_invalid")
    # Scope relevance is only a collection hint. Final HKU/topic relevance is
    # decided in analyze.py by code HKU rules plus the Topic Scan gate.

    title = _clean_text(note.get("title"))
    body = _clean_text(note.get("body"))
    content_full = _clean_text(f"{title}\n{body}")
    if not title and not body:
        reasons.append("missing_title_and_body")
    elif _is_noise_text(content_full):
        reasons.append("noise_content")
    elif seen_content_fingerprints is not None:
        fingerprint = _content_fingerprint(content_full)
        if fingerprint in seen_content_fingerprints:
            reasons.append("duplicate_content")
        else:
            seen_content_fingerprints.add(fingerprint)

    # 发布时间回填：旧采集代码可能没提取 published_at，但 raw 响应里通常有真实时间戳。
    # 这里确定性地从 raw 树补一次，让时间统计不依赖采集端版本。
    published_at = _clean_text(note.get("published_at"))
    if not published_at:
        published_at = _clean_text(_published_at_from_tree(note.get("raw")))
    published_at_raw = _clean_text(note.get("published_at_raw")) or published_at

    # 互动计数回填：旧采集代码漏提取的 comments_count/shared_count 等，从 raw 树补回，
    # 让旧 run 重新分析时 engagement 口径也正确。
    raw_counts = _counts_from_tree(note.get("raw"))

    def count_of(field: str) -> int:
        value = note.get(field)
        if value is None:
            value = raw_counts.get(field)
        return to_int(value)

    cleaned = {
        **note,
        "note_id": note_id,
        "title": title,
        "body": body,
        "content_full": content_full,
        "author_id": _clean_text(note.get("author_id")) or None,
        "author_name": _clean_text(note.get("author_name")) or None,
        "like_count": count_of("like_count"),
        "comment_count": count_of("comment_count"),
        "collect_count": count_of("collect_count"),
        "share_count": count_of("share_count"),
        "published_at": published_at or None,
        "published_at_raw": published_at_raw or None,
    }
    return cleaned, reasons


def _quality(collection: dict[str, Any]) -> dict[str, Any]:
    quality = collection.get("quality")
    return quality if isinstance(quality, dict) else {}


def _reporting_window(collection: dict[str, Any]) -> dict[str, Any]:
    """采集时记录的周报窗口；历史 run 没有这个字段，返回 applied=False。"""

    window = _quality(collection).get("reporting_window")
    if isinstance(window, dict):
        return window
    return {"applied": False, "days": 0, "start": "", "end": "", "utc_offset": ""}


def _collection_funnel(collection: dict[str, Any], raw_notes: int, processed_notes: int) -> dict[str, Any]:
    """把采集漏斗和处理阶段的去重/清洗接起来，便于一次性对账。"""

    funnel = _quality(collection).get("collection_funnel")
    merged = dict(funnel) if isinstance(funnel, dict) else {}
    merged["notes_into_processing"] = int(raw_notes)
    merged["removed_in_processing"] = int(raw_notes) - int(processed_notes)
    merged["notes_after_processing"] = int(processed_notes)
    return merged


def _keyword_funnel(collection: dict[str, Any]) -> list[dict[str, Any]]:
    rows = _quality(collection).get("keyword_funnel")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _content_fingerprint(value: str) -> str:
    """Exact-content dedupe across different note ids, ignoring whitespace/punctuation."""

    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", value.lower())


def _comment_collection_status(collection: dict[str, Any]) -> str:
    """Distinguish zero collected comments from comments that were never collected."""

    quality = collection.get("quality") if isinstance(collection.get("quality"), dict) else {}
    if not quality:
        return "unknown"
    if not quality.get("comment_fetch_enabled"):
        return "not_requested"
    comment_errors = [
        row for row in collection.get("errors") or []
        if isinstance(row, dict) and "comment" in str(row.get("stage") or "").lower()
    ]
    saved = to_int(quality.get("comments_saved"))
    if comment_errors:
        return "partial" if saved else "collection_failed"
    if to_int(quality.get("comment_eligible_notes")) == 0:
        return "skipped_by_policy"
    return "collected"


def _clean_comment(
    comment: dict[str, Any],
    kept_note_ids: set[str],
    seen_comment_keys: set[tuple[str, str, str]],
) -> tuple[dict[str, Any], list[str]]:
    """评论只保留属于 processed notes 的内容，再做字段检查和去重。"""

    reasons: list[str] = []
    note_id = _clean_text(comment.get("note_id"))
    comment_id = _clean_text(comment.get("comment_id"))
    content = _clean_text(comment.get("content"))

    if not note_id:
        reasons.append("missing_note_id")
    elif note_id not in kept_note_ids:
        reasons.append("comment_without_processed_note")
    if not comment_id:
        reasons.append("missing_comment_id")
    if _clean_text(comment.get("parent_comment_id")):
        reasons.append("non_direct_comment")
    if not content:
        reasons.append("missing_content")
    elif _is_noise_text(content):
        reasons.append("noise_content")

    dedup_key = (note_id, comment_id, content)
    if dedup_key in seen_comment_keys:
        reasons.append("duplicate_comment")
    else:
        seen_comment_keys.add(dedup_key)

    cleaned = {
        "note_id": note_id,
        "comment_id": comment_id,
        "content": content,
        "like_count": to_int(comment.get("like_count")),
    }
    return cleaned, reasons


def _clean_text(value: Any) -> str:
    """统一文本空白：去首尾空白，合并连续空格/换行。"""

    if value is None:
        return ""
    text = str(value).replace("\u00a0", " ").strip()
    return re.sub(r"\s+", " ", text)


def _is_noise_text(value: str) -> bool:
    """识别纯链接、纯符号这类不适合进入分析的文本。"""

    text = value.strip()
    if not text:
        return True
    if re.fullmatch(r"(?:https?://\S+|www\.\S+)", text, flags=re.I):
        return True
    return re.fullmatch(r"\W+", text) is not None


def _count_reasons(rows: list[dict[str, Any]]) -> dict[str, int]:
    """统计移除原因，写入 processing.json 方便前端展示。"""

    counts: dict[str, int] = {}
    for row in rows:
        for reason in row.get("reasons") or []:
            counts[reason] = counts.get(reason, 0) + 1
    return counts
