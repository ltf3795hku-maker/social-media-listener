"""Broad Scan：用默认关键词池做日常宽口径监听，并合并重复 note。"""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any, Optional

from datetime import datetime

from xhs_listener.collect import (
    WINDOW_INSIDE,
    WINDOW_OUTSIDE,
    WINDOW_UNDATED,
    XiaohongshuCollector,
    _comment_skip_reasons,
    _write_collection_partials,
    build_collection_funnel,
    classify_published_at,
    describe_reporting_window,
    reporting_window_bounds,
    window_is_active,
)
from xhs_listener.io_utils import write_json, write_jsonl
from xhs_listener.log_utils import finish_log_queue
from xhs_listener.models import (
    SEARCH_PAGE_SIZE,
    BroadScanConfig,
    CollectConfig,
    CollectionRun,
    CommentPolicy,
    KeywordConfig,
)
from xhs_listener.number_utils import to_int

COMMENT_PAGE_SIZE = 20


# Broad Scan 是“多关键词召回、单 run 去重分析”：
# 每个关键词先各自搜索，之后按 note_id 合并，避免同一帖子被重复分析。
def broad_collect(
    collector: XiaohongshuCollector,
    config: BroadScanConfig,
    log_queue: Optional[Any] = None,
) -> CollectionRun:
    """执行 Broad Scan：多关键词搜索先去重，再统一抓详情和可选评论。"""

    run_started_at = config.run_started_at or datetime.now()
    base_config = _base_collect_config(config)
    window = (
        reporting_window_bounds(run_started_at, config.reporting_window_days)
        if window_is_active(base_config)
        else None
    )
    run_dir = collector._make_run_dir(
        CollectConfig(keyword="broad_scan", output_dir=config.output_dir, scan_mode="broad_scan")
    )
    run = CollectionRun(keyword="broad_scan", run_dir=str(run_dir))
    try:
        collector._log(run, f"run_dir={run_dir}")
        collector._log(run, f"scan_mode=broad_scan sort={config.sort} keywords={len(config.keyword_pool)}")
        if window is not None:
            collector._log(
                run,
                f"reporting window {window[0].isoformat(timespec='seconds')} .. {window[1].isoformat(timespec='seconds')}",
            )

        raw_by_note_id: dict[str, dict[str, Any]] = {}
        keywords_by_note_id: dict[str, list[str]] = {}
        search_pages: list[dict[str, Any]] = []
        seen_note_ids: set[str] = set()
        keyword_funnel: list[dict[str, Any]] = []
        totals = {WINDOW_INSIDE: 0, WINDOW_OUTSIDE: 0, WINDOW_UNDATED: 0}
        raw_search_results = 0
        duplicates_removed = 0

        # 第一阶段只收集搜索卡片，不抓详情；这样可以先跨关键词去重，再减少详情请求数。
        # 窗口过滤也放在这一阶段：窗口外的帖子根本不会进入去重集合，自然也不会触发详情请求。
        for keyword_item in config.keyword_pool:
            keyword_config = _keyword_collect_config(keyword_item, config)
            keyword_pages: list[dict[str, Any]] = []

            def persist_keyword_page(page: dict[str, Any], keyword: str = keyword_item.keyword) -> None:
                search_pages.append({"keyword": keyword, **page})
                _write_collection_partials(run_dir, search_pages=search_pages, run=run, scan_mode="broad_scan")

            keyword_raw = collector.search_notes(
                keyword_config,
                run,
                keyword_pages,
                on_page=persist_keyword_page,
            )
            raw_notes = keyword_raw[: keyword_item.note_cap]
            stats = {
                "keyword": keyword_item.keyword,
                "pages_requested": len(keyword_pages),
                "raw_results": len(raw_notes),
                "within_reporting_window": 0,
                "outside_reporting_window": 0,
                "undated": 0,
                "unique_posts_contributed": 0,
            }
            raw_search_results += len(raw_notes)
            for raw_item in raw_notes:
                note = collector._build_note(raw_item, keyword_item.keyword)
                if note is None:
                    continue
                status = classify_published_at(note.published_at, window)
                totals[status] += 1
                if status == WINDOW_INSIDE:
                    stats["within_reporting_window"] += 1
                elif status == WINDOW_UNDATED:
                    stats["undated"] += 1
                    # 产品决定：拿不到发布时间的帖子保留并单独计数，不当成旧帖丢掉。
                    # 与 Topic 侧 collect.collect 保持同一口径 —— 两边不应对
                    # 「无法证明发布时间」给出不同结论。
                else:
                    stats["outside_reporting_window"] += 1
                    continue
                if note.note_id not in raw_by_note_id:
                    raw_by_note_id[note.note_id] = raw_item
                    # unique_posts_contributed 记「首次带出这条帖子的关键词」，
                    # 这样各关键词之和正好等于去重后的总数，便于对账。
                    stats["unique_posts_contributed"] += 1
                else:
                    duplicates_removed += 1
                keywords_by_note_id.setdefault(note.note_id, [])
                if keyword_item.keyword not in keywords_by_note_id[note.note_id]:
                    keywords_by_note_id[note.note_id].append(keyword_item.keyword)
            collector._log(
                run,
                f"keyword={keyword_item.keyword!r} pages={stats['pages_requested']} raw={stats['raw_results']} "
                f"in_window={stats['within_reporting_window']} out_of_window={stats['outside_reporting_window']} "
                f"undated={stats['undated']} unique={stats['unique_posts_contributed']}",
            )
            keyword_funnel.append(stats)

        for note_id, raw_item in raw_by_note_id.items():
            # 第二阶段对去重后的 note 统一抓详情，并保留 matched_keywords 方便回看来源。
            keyword_text = ", ".join(keywords_by_note_id.get(note_id, []))
            note = collector._build_note(raw_item, keyword_text)
            if note is None:
                continue
            xsec_token = collector._extract_xsec_token(raw_item)
            detail = collector.fetch_image_detail(note.note_id, xsec_token, run)
            note.raw = {"search": raw_item, "detail": detail, "matched_keywords": keywords_by_note_id.get(note_id, [])}
            collector._merge_detail(note, detail)
            collector._tag_note_for_collection(note, _base_collect_config(config), seen_note_ids)
            run.notes.append(note)
            _write_collection_partials(
                run_dir,
                search_pages=search_pages,
                notes=[item.to_dict() for item in run.notes],
                run=run,
                scan_mode="broad_scan",
            )

        # Broad 评论必须等 relevance/annotation 与 Top 10 排名完成后再抓。
        # 这里即使收到旧前端传来的 comment_policy，也不能提前抓全量帖子评论。
        if config.comment_policy.default in {"top_notes", "all"}:
            collector._log(
                run,
                "broad scan deferred comments until post-ranking Top 10 step",
            )

        write_json(run_dir / "raw" / "search_pages.json", {"pages": search_pages})
        write_jsonl(run_dir / "notes.jsonl", [note.to_dict() for note in run.notes])
        write_jsonl(run_dir / "comments.jsonl", [comment.to_dict() for comment in run.comments])
        quality = collector._collection_quality(run, search_pages, base_config)
        quality["keyword_pool"] = [asdict(item) for item in config.keyword_pool]
        quality["comment_policy"] = asdict(config.comment_policy)
        quality["deduped_notes"] = len(run.notes)
        quality["reporting_window"] = describe_reporting_window(window, config.reporting_window_days)
        quality["collection_funnel"] = build_collection_funnel(
            search_pages_requested=len(search_pages),
            raw_search_results=raw_search_results,
            window_counts=totals,
            notes_after_collection=len(run.notes),
            duplicates_removed=duplicates_removed,
            keep_undated=True,
        )
        quality["keyword_funnel"] = keyword_funnel
        if window is not None:
            collector._log(
                run,
                f"reporting window filter kept={totals[WINDOW_INSIDE]} "
                f"removed_outside={totals[WINDOW_OUTSIDE]} undated_kept={totals[WINDOW_UNDATED]}",
            )
        write_json(
            run_dir / "collection.json",
            {
                "keyword": "broad_scan",
                "scan_mode": "broad_scan",
                "keyword_pool": [asdict(item) for item in config.keyword_pool],
                "comment_policy": asdict(config.comment_policy),
                "run_dir": run.run_dir,
                "notes_count": len(run.notes),
                "comments_count": len(run.comments),
                "quality": quality,
                "errors": run.errors,
                "logs": run.logs,
            },
        )
        return run
    finally:
        finish_log_queue(log_queue)


def parse_keyword_pool(value: Optional[str | list[Any]]) -> list[KeywordConfig]:
    """解析前端/CLI 传入的 keyword_pool JSON；空值使用默认池。"""

    if not value:
        return BroadScanConfig().keyword_pool
    if isinstance(value, list):
        payload = value
    else:
        path = Path(value)
        raw = path.read_text(encoding="utf-8") if len(value) < 240 and path.exists() else value
        payload = json.loads(raw)
    rows: list[KeywordConfig] = []
    for item in payload:
        if isinstance(item, str):
            rows.append(KeywordConfig(keyword=item))
        elif isinstance(item, dict):
            max_notes = int(item.get("max_notes", SEARCH_PAGE_SIZE))
            # 旧配置只带 max_notes；按 TikHub 每页约 20 条反推需要的页数，保持兼容。
            max_pages = int(item.get("max_pages") or max(1, -(-max_notes // SEARCH_PAGE_SIZE)))
            rows.append(KeywordConfig(keyword=str(item["keyword"]), max_pages=max_pages, max_notes=max_notes))
    return rows


def _keyword_collect_config(item: KeywordConfig, config: BroadScanConfig) -> CollectConfig:
    return CollectConfig(
        keyword=item.keyword,
        # 页数按关键词分配；config.max_pages 只在关键词没写页数时兜底。
        max_pages=int(item.max_pages or config.max_pages),
        max_notes=item.note_cap,
        sort_type=_sort_type(config.sort),
        note_type=config.note_type,
        time_filter=config.time_filter,
        scope_pattern=config.scope_pattern,
        scan_mode="broad_scan",
        reporting_window_days=config.reporting_window_days,
    )


def _base_collect_config(config: BroadScanConfig) -> CollectConfig:
    return CollectConfig(
        keyword="broad_scan",
        sort_type=_sort_type(config.sort),
        note_type=config.note_type,
        time_filter=config.time_filter,
        scope_pattern=config.scope_pattern,
        comment_pages=0,
        comment_policy="none",
        comment_top_percent=0,
        fetch_comments_for_top_notes=0,
        scan_mode="broad_scan",
        reporting_window_days=config.reporting_window_days,
    )


def _sort_type(value: str) -> str:
    return "time_descending" if value == "latest_first" else "general"


def _fetch_note_comments_by_policy(
    collector: XiaohongshuCollector,
    run: CollectionRun,
    policy: CommentPolicy,
) -> None:
    comment_pages = _pages_for_count(policy.max_comments_per_note)
    if comment_pages <= 0:
        collector._log(run, "broad scan skip comments max_comments_per_note=0")
        return
    eligible = [
        note
        for note in run.notes
        if not _comment_skip_reasons(
            note,
            CollectConfig(
                keyword="broad_scan",
                comment_min_likes=0,
                comment_min_comments=policy.min_comment_count,
            ),
        )
    ]
    if policy.default == "top_notes":
        selected_notes = _top_percent_notes(eligible, policy.top_percent, policy.fetch_comments_for_top_notes)
        if not selected_notes:
            collector._log(
                run,
                "broad scan skip comments policy=top_notes reason=no_notes_met_minimum_discussion_threshold",
            )
    else:
        selected_notes = eligible
    selected_ids = {note.note_id for note in selected_notes}
    for note in run.notes:
        if note.note_id not in selected_ids:
            if policy.default != "none":
                collector._log(run, f"broad scan skip comments note_id={note.note_id} reason=not_selected_by_{policy.default}")
            continue
        collector._log(run, f"broad scan collect comments note_id={note.note_id} policy={policy.default} comment_pages={comment_pages}")
        comments = collector.fetch_comments(note.note_id, comment_pages=comment_pages, sub_comment_pages=0, run=run)
        run.comments.extend(comments[: policy.max_comments_per_note])


def _top_percent_notes(notes: list[Any], percent: int, limit: int = 0) -> list[Any]:
    if not notes:
        return []
    normalized_percent = min(100, max(1, int(percent or 20)))
    count = max(1, (len(notes) * normalized_percent + 99) // 100)
    if limit > 0:
        count = min(count, limit)
    return sorted(notes, key=_note_engagement, reverse=True)[:count]


def _note_engagement(note: Any) -> int:
    return to_int(note.like_count) + 2 * to_int(note.collect_count) + 3 * to_int(note.comment_count) + to_int(note.share_count)


def _pages_for_count(count: int, page_size: int = COMMENT_PAGE_SIZE) -> int:
    if count <= 0:
        return 0
    return max(1, (count + page_size - 1) // page_size)
