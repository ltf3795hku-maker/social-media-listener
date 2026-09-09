"""Post-ranking Broad enrichments: Top-10 comments and competitor weekly Top-5."""
from __future__ import annotations

import hashlib
import json
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from xhs_listener.analyze import _engagement_score, _llm_json_call
from xhs_listener.collect import (
    WINDOW_INSIDE,
    XiaohongshuCollector,
    classify_published_at,
    reporting_window_bounds,
)
from xhs_listener.io_utils import read_json, read_jsonl, write_json, write_jsonl
from xhs_listener.log_utils import emit_log
from xhs_listener.models import (
    REPORTING_WINDOW_DAYS,
    SEARCH_PAGE_SIZE,
    WEEKLY_TIME_FILTER,
    CollectConfig,
    CollectionRun,
    Note,
)
from xhs_listener.number_utils import to_int
from xhs_listener.report import _broad_top_posts_from_run


TOP10_COMMENT_SCHEMA_VERSION = "broad_top10_comments_v1"
COMPETITOR_SCHEMA_VERSION = "competitor_weekly_v1"
TOP10_COMMENT_PAGES = 1
TOP10_COMMENT_SORT = "like_count"
COMPETITOR_MAX_PAGES = 1
COMPETITOR_WINDOW_DAYS = REPORTING_WINDOW_DAYS
COMPETITOR_SORT_TYPE = "general"
# 窗口内候选先按搜索卡片自带的互动量排序，只对进入 Top N 的候选抓详情
# （抓详情是唯一真正花钱的一步）；落选的窗口内笔记仍然落盘，只是没有详情字段。
COMPETITOR_DETAIL_FETCH_TOP_N = 8
COMPETITOR_TOP_POSTS = 5

COMPETITOR_SCHOOLS: tuple[tuple[str, str], ...] = (
    ("CUHK Business School", "港中文商学院"),
    ("HKUST Business School", "hkust 商科"),
    ("NUS Business School", "nus 商学院"),
    ("NTU Business School", "南洋商学院"),
    ("复旦大学管理学院", "复旦管院"),
    ("清华大学经济管理学院", "清华经管"),
    ("北京大学光华管理学院", "北大光华"),
)


def collect_and_analyze_top10_comments(
    run_dir: str | Path,
    collector: XiaohongshuCollector,
    client: Any,
    *,
    log_queue: Optional[Any] = None,
    stop_checker: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Rank relevant HKUBS posts, then fetch and analyze one comment page per Top-10 post."""

    run_path = Path(run_dir)
    top_posts = _broad_top_posts_from_run(run_path)
    top_ids = [str(row.get("note_id") or "") for row in top_posts if row.get("note_id")]
    note_rows = {
        str(row.get("note_id") or ""): row
        for row in read_jsonl(run_path / "processed_notes.jsonl")
        if row.get("note_id")
    }
    collection_path = run_path / "top10_comment_collection.json"
    comments_path = run_path / "top10_comments.jsonl"
    cached_collection = _read_optional_json(collection_path)
    cached_comments = read_jsonl(comments_path) if comments_path.exists() else []
    cached_by_note: dict[str, list[dict[str, Any]]] = {}
    for row in cached_comments:
        cached_by_note.setdefault(str(row.get("note_id") or ""), []).append(row)
    completed = {
        str(item)
        for item in (cached_collection.get("completed_note_ids") or [])
        if str(item)
    }
    if cached_collection.get("schema_version") != TOP10_COMMENT_SCHEMA_VERSION:
        cached_by_note = {}
        completed = set()

    run = CollectionRun(keyword="broad_top10_comments", run_dir=str(run_path))
    comments_by_note: dict[str, list[dict[str, Any]]] = {
        note_id: list(cached_by_note.get(note_id) or []) for note_id in top_ids
    }
    per_post: list[dict[str, Any]] = []
    for rank, note_id in enumerate(top_ids, 1):
        _check_stop(stop_checker)
        if note_id not in completed:
            _emit(log_queue, f"Top 10 comments rank={rank} note_id={note_id} page=1 sort=like_count")
            error_start = len(run.errors)
            fetched = collector.fetch_comments(
                note_id,
                comment_pages=TOP10_COMMENT_PAGES,
                sub_comment_pages=0,
                run=run,
                sort_strategy=TOP10_COMMENT_SORT,
            )
            comments_by_note[note_id] = [row.to_dict() for row in fetched]
            new_errors = run.errors[error_start:]
            if any(
                row.get("stage") == "comments_web_v3" and str(row.get("note_id") or "") == note_id
                for row in new_errors
            ):
                _write_top10_comment_collection(
                    collection_path,
                    comments_path,
                    top_ids,
                    completed,
                    comments_by_note,
                    run,
                )
                _record_enrichment_requests(run_path, "top10_comments", run.api_request_count)
                raise RuntimeError(f"All comment backends failed for Top 10 note_id={note_id}")
            completed.add(note_id)
            _write_top10_comment_collection(
                collection_path,
                comments_path,
                top_ids,
                completed,
                comments_by_note,
                run,
            )
        per_post.append(
            {
                "rank": rank,
                "note_id": note_id,
                "comments_returned": len(comments_by_note.get(note_id) or []),
            }
        )

    collection = _write_top10_comment_collection(
        collection_path,
        comments_path,
        top_ids,
        completed,
        comments_by_note,
        run,
        per_post=per_post,
    )
    _record_enrichment_requests(run_path, "top10_comments", run.api_request_count)

    analysis_path = run_path / "top10_comment_analysis.json"
    partial_path = run_path / "top10_comment_analysis.partial.json"
    partial = _read_optional_json(partial_path)
    cached_rows = {
        str(row.get("note_id") or ""): row
        for row in (partial.get("posts") or [])
        if isinstance(row, dict) and row.get("note_id")
    }
    results: list[dict[str, Any]] = []
    usage_rows: list[dict[str, Any]] = []
    for rank, note_id in enumerate(top_ids, 1):
        _check_stop(stop_checker)
        comments = comments_by_note.get(note_id) or []
        digest = _comment_input_digest(note_id, comments)
        cached = cached_rows.get(note_id)
        if isinstance(cached, dict) and cached.get("input_sha256") == digest:
            result = cached
        elif not comments:
            result = _empty_comment_analysis(note_id, digest)
        else:
            _emit(log_queue, f"Top 10 comment bundle analysis rank={rank} note_id={note_id} comments={len(comments)}")
            payload = _llm_json_call(
                client,
                _top10_comment_prompt(note_rows.get(note_id) or {}, comments),
                "top10_comment_bundle",
                usage_rows,
            )
            if not isinstance(payload, dict):
                raise RuntimeError(f"Top 10 comment analysis for {note_id} must return a JSON object")
            result = _validate_comment_analysis(note_id, payload, comments)
            result["input_sha256"] = digest
        cached_rows[note_id] = result
        results.append(result)
        write_json(
            partial_path,
            {
                "schema_version": TOP10_COMMENT_SCHEMA_VERSION,
                "posts": [cached_rows[item] for item in top_ids if item in cached_rows],
            },
        )

    output = {
        "schema_version": TOP10_COMMENT_SCHEMA_VERSION,
        "scope": "hkubs_top10_only",
        "comment_pages_per_post": TOP10_COMMENT_PAGES,
        "requested_sort": TOP10_COMMENT_SORT,
        "top_note_ids": top_ids,
        "comments_collected": sum(len(comments_by_note.get(note_id) or []) for note_id in top_ids),
        "posts": results,
    }
    write_json(analysis_path, output)
    _append_usage(run_path, usage_rows)
    return {"collection": collection, "analysis": output}


def collect_competitor_weekly(
    run_dir: str | Path,
    collector: XiaohongshuCollector,
    *,
    log_queue: Optional[Any] = None,
    stop_checker: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Collect exactly one weekly search page per competitor and rank each school's Top 5."""

    run_path = Path(run_dir)
    output_path = run_path / "competitor_collection.json"
    partial_path = run_path / "competitor_collection.partial.json"
    notes_path = run_path / "competitor_notes.jsonl"
    partial_notes_path = run_path / "competitor_notes.partial.jsonl"
    pages_path = run_path / "raw" / "competitor_search_pages.json"
    partial_pages_path = run_path / "raw" / "competitor_search_pages.partial.json"
    existing = _read_optional_json(output_path)
    expected = [{"school": school, "keyword": keyword} for school, keyword in COMPETITOR_SCHOOLS]
    if (
        existing.get("schema_version") == COMPETITOR_SCHEMA_VERSION
        and existing.get("schools_config") == expected
        and notes_path.exists()
        and pages_path.exists()
    ):
        _emit(log_queue, "Reused cached competitor weekly collection")
        return existing

    run = CollectionRun(keyword="competitor_weekly", run_dir=str(run_path))
    partial = _read_optional_json(partial_path)
    if partial.get("schema_version") == COMPETITOR_SCHEMA_VERSION and partial.get("schools_config") == expected:
        run_started_at = _parse_datetime(partial.get("run_started_at")) or datetime.now()
        window = _parse_window(partial) or reporting_window_bounds(run_started_at, COMPETITOR_WINDOW_DAYS)
        run.api_request_count = int(partial.get("api_request_count") or 0)
        run.errors = [row for row in (partial.get("errors") or []) if isinstance(row, dict)]
        run.logs = [str(row) for row in (partial.get("logs") or []) if str(row).strip()]
        school_rows = [row for row in (partial.get("schools") or []) if isinstance(row, dict)]
        all_notes = read_jsonl(partial_notes_path) if partial_notes_path.exists() else []
        pages_payload = _read_optional_json(partial_pages_path)
        all_pages = [row for row in (pages_payload.get("pages") or []) if isinstance(row, dict)]
        _emit(log_queue, f"Reused partial competitor weekly collection schools={len(school_rows)}")
    else:
        run_started_at = datetime.now()
        window = reporting_window_bounds(run_started_at, COMPETITOR_WINDOW_DAYS)
        all_notes = []
        all_pages = []
        school_rows = []
    completed_schools = {str(row.get("school") or "") for row in school_rows if row.get("school")}

    for school, keyword in COMPETITOR_SCHOOLS:
        _check_stop(stop_checker)
        if school in completed_schools:
            _emit(log_queue, f"Reused competitor {school} from partial collection")
            continue
        config = CollectConfig(
            keyword=keyword,
            max_pages=COMPETITOR_MAX_PAGES,
            max_notes=COMPETITOR_MAX_PAGES * SEARCH_PAGE_SIZE,
            sort_type=COMPETITOR_SORT_TYPE,
            note_type="普通笔记",
            time_filter=WEEKLY_TIME_FILTER,
            scope_pattern=None,
            scan_mode="competitor_weekly",
            reporting_window_days=COMPETITOR_WINDOW_DAYS,
        )
        pages: list[dict[str, Any]] = []

        def persist_competitor_page(page: dict[str, Any], school_name: str = school, search_keyword: str = keyword) -> None:
            all_pages.append({"school": school_name, "keyword": search_keyword, **page})
            _write_competitor_collection_partial(partial_path, partial_notes_path, partial_pages_path, expected, run_started_at, window, school_rows, all_notes, all_pages, run)

        raw_rows = collector.search_notes(config, run, pages, on_page=persist_competitor_page)[: config.max_notes]
        collection_warning = ""
        if len(pages) < COMPETITOR_MAX_PAGES:
            collection_warning = (
                f"requested {COMPETITOR_MAX_PAGES} pages but received {len(pages)}"
            )
            _emit(log_queue, f"Competitor {school} partial collection: {collection_warning}")
        in_window: dict[str, tuple[Note, dict[str, Any]]] = {}
        outside_count = 0
        undated_count = 0
        for raw in raw_rows:
            note = collector._build_note(raw, keyword)
            if note is None:
                continue
            status = classify_published_at(note.published_at, window)
            if status != WINDOW_INSIDE:
                if status == "undated":
                    undated_count += 1
                else:
                    outside_count += 1
                continue
            in_window.setdefault(note.note_id, (note, raw))

        # 先只用搜索卡片自带的互动量数据（点赞/收藏/评论/分享）排序，不抓详情——
        # 这一步不花钱。只有排进 Top N 的候选才值得再花一次详情请求；
        # 落选的窗口内笔记仍然落盘（进 all_notes/competitor_notes.jsonl），
        # 只是没有详情字段，标题/正文停留在搜索卡片给的摘要版本。
        ranked_candidates = sorted(
            in_window.values(),
            key=lambda pair: _note_engagement_score(pair[0]),
            reverse=True,
        )
        detail_fetch_ids = {note.note_id for note, _ in ranked_candidates[:COMPETITOR_DETAIL_FETCH_TOP_N]}

        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for note, raw in ranked_candidates:
            _check_stop(stop_checker)
            fetch_detail = note.note_id in detail_fetch_ids
            if fetch_detail:
                detail = collector.fetch_image_detail(note.note_id, collector._extract_xsec_token(raw), run)
                note.raw = {"search": raw, "detail": detail, "competitor_school": school}
                collector._merge_detail(note, detail)
            else:
                note.raw = {"search": raw, "competitor_school": school}
            collector._tag_note_for_collection(note, config, seen)
            row = note.to_dict()
            row["school"] = school
            row["detail_fetched"] = fetch_detail
            normalized.append(row)
            all_notes.append(row)
            _write_competitor_collection_partial(partial_path, partial_notes_path, partial_pages_path, expected, run_started_at, window, school_rows, all_notes, all_pages, run)

        # 最终 Top 5 只从已经抓过详情的 Top N 候选里选——排序用的互动量数字
        # 这时可能已经被详情接口回填过（搜索卡片偶尔缺互动数），比第一轮排序更准。
        detail_fetched_rows = [row for row in normalized if row.get("detail_fetched")]
        ranked = sorted(detail_fetched_rows, key=_engagement_score, reverse=True)[:COMPETITOR_TOP_POSTS]
        top_posts = [_competitor_post_row(row, rank) for rank, row in enumerate(ranked, 1)]
        school_rows.append(
            {
                "school": school,
                "keyword": keyword,
                "pages_requested": COMPETITOR_MAX_PAGES,
                "pages_received": len(pages),
                "collection_warning": collection_warning,
                "raw_candidates": len(raw_rows),
                "outside_window_removed": outside_count,
                "undated_removed": undated_count,
                "unique_weekly_candidates": len(normalized),
                "detail_fetched_candidates": len(detail_fetched_rows),
                "top_posts": top_posts,
            }
        )
        completed_schools.add(school)
        _write_competitor_collection_partial(partial_path, partial_notes_path, partial_pages_path, expected, run_started_at, window, school_rows, all_notes, all_pages, run)
        _emit(
            log_queue,
            f"Competitor {school} pages={len(pages)} raw={len(raw_rows)} weekly={len(normalized)} top={len(top_posts)}",
        )

    write_json(pages_path, {"pages": all_pages})
    write_jsonl(notes_path, all_notes)
    output = {
        "schema_version": COMPETITOR_SCHEMA_VERSION,
        "run_started_at": run_started_at.isoformat(timespec="seconds"),
        "window_start": window[0].isoformat(timespec="seconds"),
        "window_end": window[1].isoformat(timespec="seconds"),
        "window_days": COMPETITOR_WINDOW_DAYS,
        "time_filter": WEEKLY_TIME_FILTER,
        "max_pages_per_keyword": COMPETITOR_MAX_PAGES,
        "sort_type": COMPETITOR_SORT_TYPE,
        "schools_config": expected,
        "schools": school_rows,
        "api_request_count": run.api_request_count,
        "errors": run.errors,
        "logs": run.logs,
    }
    write_json(output_path, output)
    _record_enrichment_requests(run_path, "competitors", run.api_request_count)
    return output


def analyze_competitor_weekly(
    run_dir: str | Path,
    client: Any,
    *,
    log_queue: Optional[Any] = None,
    stop_checker: Optional[Callable[[], None]] = None,
) -> dict[str, Any]:
    """Make one LLM call per school for the final Top 5, then aggregate sentiment in code."""

    run_path = Path(run_dir)
    collection = read_json(run_path / "competitor_collection.json")
    output_path = run_path / "competitor_analysis.json"
    partial_path = run_path / "competitor_analysis.partial.json"
    partial = _read_optional_json(partial_path)
    cached = {
        str(row.get("school") or ""): row
        for row in (partial.get("schools") or [])
        if isinstance(row, dict) and row.get("school")
    }
    usage_rows: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []

    for school_row in collection.get("schools") or []:
        _check_stop(stop_checker)
        school = str(school_row.get("school") or "")
        top_posts = [row for row in (school_row.get("top_posts") or []) if isinstance(row, dict)][:COMPETITOR_TOP_POSTS]
        digest = _competitor_input_digest(school, top_posts)
        current = cached.get(school)
        if isinstance(current, dict) and current.get("input_sha256") == digest:
            result = current
        elif not top_posts:
            result = {
                "school": school,
                "keyword": school_row.get("keyword"),
                "overall_sentiment": "Mixed",
                "weekly_takeaway": "",
                "posts": [],
                "input_sha256": digest,
            }
        else:
            _emit(log_queue, f"Competitor LLM school={school} posts={len(top_posts)}")
            payload = _llm_json_call(
                client,
                _competitor_prompt(school, top_posts),
                "competitor_weekly",
                usage_rows,
            )
            result = _validate_competitor_analysis(school_row, payload)
            result["input_sha256"] = digest
        cached[school] = result
        results.append(result)
        write_json(
            partial_path,
            {
                "schema_version": COMPETITOR_SCHEMA_VERSION,
                "schools": [cached[name] for name, _ in COMPETITOR_SCHOOLS if name in cached],
            },
        )

    output = {
        "schema_version": COMPETITOR_SCHEMA_VERSION,
        "scope": "top5_posts_per_school_only",
        "schools": results,
    }
    write_json(output_path, output)
    _append_usage(run_path, usage_rows)
    return output


def aggregate_overall_sentiment(sentiments: list[str]) -> str:
    """Aggregate post sentiments using the fixed 3-of-5 product rule."""

    counts = Counter(item for item in sentiments if item in {"positive", "neutral", "negative"})
    if counts["positive"] >= 3:
        return "Mostly Positive"
    if counts["neutral"] >= 3:
        return "Mostly Neutral"
    if counts["negative"] >= 3:
        return "Mostly Negative"
    return "Mixed"


def _write_top10_comment_collection(
    collection_path: Path,
    comments_path: Path,
    top_ids: list[str],
    completed: set[str],
    comments_by_note: dict[str, list[dict[str, Any]]],
    run: CollectionRun,
    *,
    per_post: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    rows = [row for note_id in top_ids for row in comments_by_note.get(note_id) or []]
    write_jsonl(comments_path, rows)
    output = {
        "schema_version": TOP10_COMMENT_SCHEMA_VERSION,
        "scope": "hkubs_top10_only",
        "top_note_ids": top_ids,
        "completed_note_ids": [note_id for note_id in top_ids if note_id in completed],
        "comment_pages_per_post": TOP10_COMMENT_PAGES,
        "requested_sort": TOP10_COMMENT_SORT,
        "page_size_parameter": None,
        "comments_collected": len(rows),
        "per_post": per_post or [],
        "api_request_count": run.api_request_count,
        "errors": run.errors,
        "logs": run.logs,
    }
    write_json(collection_path, output)
    return output


def _top10_comment_prompt(note: dict[str, Any], comments: list[dict[str, Any]]) -> str:
    bundle = "\n\n".join(
        f"[comment_id={row.get('comment_id')} | likes={to_int(row.get('like_count'))}]\n{str(row.get('content') or '').strip()}"
        for row in comments
    )
    return f"""你是 HKU Business School 小红书 Top Post 评论区分析器。
只分析下面这一篇帖子及其一级评论 bundle。不要输出 comment sentiment，不要把点赞数当作独立观点数量。

规则：
- recurring_signals 中每条 signal 至少由 2 个不同 comment_id 支持；否则不要输出。
- support_count 必须等于支持该 signal 的不同评论数，不得使用 like_count 代替。
- evidence.comment_id 必须来自输入；quote 必须是对应原评论中逐字存在的连续原文。
- high_engagement_viewpoint 只代表高互动观点，不代表多数意见；没有清晰观点则输出 null。
- 没有明确重复信号时 has_clear_signal=false、audience_reaction=""、recurring_signals=[]。
- 不要为了每篇帖子强行生成 insight。
- audience_reaction 只写一句简洁、证据可追溯的受众反应，不写大段总结。

只输出 JSON object：
{{
  "has_clear_signal": true,
  "audience_reaction": "string",
  "recurring_signals": [
    {{"signal": "string", "support_count": 2, "evidence": [{{"comment_id": "string", "quote": "string", "like_count": 0}}]}}
  ],
  "high_engagement_viewpoint": {{"summary": "string", "comment_id": "string", "quote": "string", "like_count": 0}}
}}

帖子：
note_id={note.get('note_id')}
title={note.get('title') or ''}
body={str(note.get('body') or note.get('content_full') or '')[:800]}

评论 bundle：
{bundle}
"""


def _validate_comment_analysis(
    note_id: str,
    payload: dict[str, Any],
    comments: list[dict[str, Any]],
) -> dict[str, Any]:
    by_id = {str(row.get("comment_id") or ""): row for row in comments if row.get("comment_id")}
    recurring: list[dict[str, Any]] = []
    for row in payload.get("recurring_signals") or []:
        if not isinstance(row, dict):
            continue
        evidence: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in row.get("evidence") or []:
            if not isinstance(item, dict):
                continue
            comment_id = str(item.get("comment_id") or "")
            source = by_id.get(comment_id)
            quote = str(item.get("quote") or "").strip()
            content = str((source or {}).get("content") or "")
            if not source or not quote or quote not in content or comment_id in seen:
                continue
            seen.add(comment_id)
            evidence.append(
                {
                    "comment_id": comment_id,
                    "quote": quote,
                    "like_count": to_int(source.get("like_count")),
                }
            )
        if len(seen) < 2:
            continue
        recurring.append(
            {
                "signal": str(row.get("signal") or "").strip()[:160],
                "support_count": len(seen),
                "evidence": evidence,
            }
        )
        if len(recurring) >= 2:
            break

    high = payload.get("high_engagement_viewpoint")
    validated_high: Optional[dict[str, Any]] = None
    if isinstance(high, dict):
        comment_id = str(high.get("comment_id") or "")
        source = by_id.get(comment_id)
        quote = str(high.get("quote") or "").strip()
        if source and quote and quote in str(source.get("content") or ""):
            validated_high = {
                "summary": str(high.get("summary") or "").strip()[:240],
                "comment_id": comment_id,
                "quote": quote,
                "like_count": to_int(source.get("like_count")),
            }

    has_clear_signal = bool(recurring)
    return {
        "note_id": note_id,
        "has_clear_signal": has_clear_signal,
        "audience_reaction": str(payload.get("audience_reaction") or "").strip()[:240] if has_clear_signal else "",
        "recurring_signals": recurring,
        "high_engagement_viewpoint": validated_high,
    }


def _empty_comment_analysis(note_id: str, digest: str) -> dict[str, Any]:
    return {
        "note_id": note_id,
        "has_clear_signal": False,
        "audience_reaction": "",
        "recurring_signals": [],
        "high_engagement_viewpoint": None,
        "input_sha256": digest,
    }


def _note_engagement_score(note: Note) -> int:
    """详情抓取前用来排序候选的互动量分数，直接读搜索卡片自带的计数字段。

    和 ``_engagement_score`` 用同一套权重，只是输入是详情合并前的 ``Note``
    对象而不是 dict，避免详情请求之前就要先转 dict。
    """

    return _engagement_score(
        {
            "like_count": note.like_count,
            "collect_count": note.collect_count,
            "comment_count": note.comment_count,
            "share_count": note.share_count,
        }
    )


def _competitor_post_row(note: dict[str, Any], rank: int) -> dict[str, Any]:
    return {
        "rank": rank,
        "note_id": str(note.get("note_id") or ""),
        "title": str(note.get("title") or "") or "无标题",
        "body": str(note.get("body") or note.get("content_full") or "")[:1200],
        "published_at": note.get("published_at"),
        "like_count": to_int(note.get("like_count")),
        "comment_count": to_int(note.get("comment_count")),
        "share_count": to_int(note.get("share_count")),
        "collect_count": to_int(note.get("collect_count")),
        "engagement_score": _engagement_score(note),
        "post_url": str(note.get("post_url") or ""),
    }


def _parse_datetime(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except ValueError:
        return None


def _parse_window(payload: dict[str, Any]) -> tuple[datetime, datetime] | None:
    start = _parse_datetime(payload.get("window_start"))
    end = _parse_datetime(payload.get("window_end"))
    if start is None or end is None:
        return None
    return start, end


def _write_competitor_collection_partial(
    partial_path: Path,
    partial_notes_path: Path,
    partial_pages_path: Path,
    schools_config: list[dict[str, str]],
    run_started_at: datetime,
    window: tuple[datetime, datetime],
    school_rows: list[dict[str, Any]],
    all_notes: list[dict[str, Any]],
    all_pages: list[dict[str, Any]],
    run: CollectionRun,
) -> None:
    write_json(partial_pages_path, {"pages": all_pages})
    write_jsonl(partial_notes_path, all_notes)
    write_json(
        partial_path,
        {
            "schema_version": COMPETITOR_SCHEMA_VERSION,
            "partial": True,
            "run_started_at": run_started_at.isoformat(timespec="seconds"),
            "window_start": window[0].isoformat(timespec="seconds"),
            "window_end": window[1].isoformat(timespec="seconds"),
            "window_days": COMPETITOR_WINDOW_DAYS,
            "time_filter": WEEKLY_TIME_FILTER,
            "max_pages_per_keyword": COMPETITOR_MAX_PAGES,
            "sort_type": COMPETITOR_SORT_TYPE,
            "schools_config": schools_config,
            "schools": school_rows,
            "api_request_count": run.api_request_count,
            "errors": run.errors,
            "logs": run.logs,
        },
    )


def _competitor_prompt(school: str, top_posts: list[dict[str, Any]]) -> str:
    inputs = [
        {"note_id": row.get("note_id"), "title": row.get("title"), "body": row.get("body")}
        for row in top_posts
    ]
    return f"""你是竞对周榜 Top Posts 标注器。输入只包含 {school} 本周按互动量选出的最多 5 篇帖子。

任务：
1. 对每篇帖子输出 sentiment：positive / neutral / negative。
2. weekly_takeaway 只用一句话概括这几篇 Top Posts 主要在讲什么。

限制：
- 不做 risk、theme、author_type 或完整 annotation。
- 不分析评论内容。
- 不得把这最多 5 篇帖子描述为该校“整体网络舆情”或全网代表性结论。
- posts 必须逐一保留输入 note_id，不得新增、遗漏或改写。

只输出 JSON object：
{{"posts": [{{"note_id": "...", "sentiment": "positive"}}], "weekly_takeaway": "..."}}

输入：
{json.dumps(inputs, ensure_ascii=False)}
"""


def _validate_competitor_analysis(school_row: dict[str, Any], payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Competitor analysis must return a JSON object")
    top_posts = [row for row in (school_row.get("top_posts") or []) if isinstance(row, dict)][:COMPETITOR_TOP_POSTS]
    input_by_id = {str(row.get("note_id") or ""): row for row in top_posts}
    output_by_id: dict[str, str] = {}
    for row in payload.get("posts") or []:
        if not isinstance(row, dict):
            continue
        note_id = str(row.get("note_id") or "")
        sentiment = str(row.get("sentiment") or "").lower()
        if note_id in input_by_id and sentiment in {"positive", "neutral", "negative"}:
            output_by_id[note_id] = sentiment
    missing = [note_id for note_id in input_by_id if note_id not in output_by_id]
    if missing:
        raise RuntimeError(f"Competitor analysis missing valid sentiment for note_ids={missing}")
    posts = [{**input_by_id[note_id], "sentiment": output_by_id[note_id]} for note_id in input_by_id]
    return {
        "school": school_row.get("school"),
        "keyword": school_row.get("keyword"),
        "overall_sentiment": aggregate_overall_sentiment([row["sentiment"] for row in posts]),
        "weekly_takeaway": str(payload.get("weekly_takeaway") or "").strip()[:320],
        "posts": posts,
    }


def _comment_input_digest(note_id: str, comments: list[dict[str, Any]]) -> str:
    payload = {
        "schema": TOP10_COMMENT_SCHEMA_VERSION,
        "note_id": note_id,
        "comments": [
            {
                "comment_id": row.get("comment_id"),
                "content": row.get("content"),
                "like_count": to_int(row.get("like_count")),
            }
            for row in comments
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _competitor_input_digest(school: str, top_posts: list[dict[str, Any]]) -> str:
    payload = {
        "schema": COMPETITOR_SCHEMA_VERSION,
        "school": school,
        "posts": [
            {"note_id": row.get("note_id"), "title": row.get("title"), "body": row.get("body")}
            for row in top_posts
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _append_usage(run_path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path = run_path / "llm_usage.json"
    existing = _read_optional_json(path)
    current = [row for row in (existing.get("usage") or []) if isinstance(row, dict)]
    write_json(path, {"usage": current + [row for row in rows if row]})


def _record_enrichment_requests(run_path: Path, name: str, added: int) -> None:
    if added <= 0:
        return
    path = run_path / "collection.json"
    payload = read_json(path)
    quality = payload.get("quality") if isinstance(payload.get("quality"), dict) else {}
    enrichments = quality.get("post_collection_enrichments") if isinstance(quality.get("post_collection_enrichments"), dict) else {}
    previous = int((enrichments.get(name) or {}).get("api_request_count") or 0)
    enrichments[name] = {"api_request_count": previous + int(added)}
    quality["post_collection_enrichments"] = enrichments
    quality["api_request_count"] = int(quality.get("api_request_count") or 0) + int(added)
    quality["api_estimated_cost_usd"] = round(int(quality["api_request_count"]) * float(quality.get("api_unit_cost_usd") or 0.01), 2)
    payload["quality"] = quality
    write_json(path, payload)


def _read_optional_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = read_json(path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _check_stop(stop_checker: Optional[Callable[[], None]]) -> None:
    if stop_checker is not None:
        stop_checker()


def _emit(log_queue: Optional[Any], message: str) -> None:
    emit_log(log_queue, message)
