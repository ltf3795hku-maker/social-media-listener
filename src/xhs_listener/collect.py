from __future__ import annotations

import json
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import quote
from uuid import uuid4

from xhs_listener.io_utils import write_json, write_jsonl
from xhs_listener.json_utils import (
    extract_comment_id,
    extract_note_id,
    extract_search_session,
    pick_first,
    to_data_dict,
    walk_nodes,
)
from xhs_listener.log_utils import emit_log, finish_log_queue, timestamped
from xhs_listener.models import (
    REPORTING_WINDOW_DAYS,
    WEEKLY_TIME_FILTER,
    CollectConfig,
    CollectionRun,
    Comment,
    Note,
)
from xhs_listener.number_utils import to_int
from xhs_listener.tikhub import (
    APP_COMMENTS,
    APP_DETAIL,
    APP_SEARCH,
    APP_V2_COMMENTS,
    APP_V2_IMAGE_DETAIL,
    APP_V2_SEARCH,
    APP_V2_SUB_COMMENTS,
    TikHubClient,
    WEB_V3_COMMENTS,
    WEB_V3_DETAIL,
    WEB_V3_SEARCH,
)


# 采集层的职责边界：
# 1. 只和 TikHub 以及原始小红书数据结构打交道。
# 2. 把不稳定的 search/detail/comment 响应归一化成 Note / Comment。
# 3. 在采评论之前先打标签和做阈值判断，避免浪费 TikHub 请求额度。
class XiaohongshuCollector:
    """小红书采集主类：按关键词抓搜索结果、图文详情和可选评论。"""

    def __init__(
        self,
        api_token: str,
        host: str = "https://api.tikhub.io",
        output_root: str | Path = "data/runs",
        log_queue: Optional[Any] = None,
        stop_checker: Optional[Callable[[], None]] = None,
    ) -> None:
        self.client = TikHubClient(api_token=api_token, host=host)
        self.output_root = Path(output_root)
        self.log_queue = log_queue
        self.stop_checker = stop_checker

    def collect(self, config: CollectConfig) -> CollectionRun:
        """执行一次完整采集，并把原始页、笔记、评论和摘要落盘。"""

        run_started_at = config.run_started_at or datetime.now()
        window = (
            reporting_window_bounds(run_started_at, config.reporting_window_days)
            if window_is_active(config)
            else None
        )
        run_dir = self._make_run_dir(config)
        run = CollectionRun(keyword=config.keyword, run_dir=str(run_dir))
        try:
            self._log(run, f"run_dir={run_dir}")
            if window is not None:
                self._log(run, f"reporting window {window[0].isoformat(timespec='seconds')} .. {window[1].isoformat(timespec='seconds')}")
            self._log(
                run,
                f"keyword={config.keyword!r} max_pages={config.max_pages} max_notes={config.max_notes} "
                f"note_type={config.note_type} comment_pages={config.comment_pages} "
                f"comment_min_likes={config.comment_min_likes} "
                f"comment_min_comments={config.comment_min_comments} scope_pattern={config.scope_pattern!r}"
            )

            search_pages: list[dict[str, Any]] = []

            def persist_search_page(_: dict[str, Any]) -> None:
                _write_collection_partials(run_dir, search_pages=search_pages, run=run, scan_mode=config.scan_mode)

            all_raw_notes = self.search_notes(config, run, search_pages, on_page=persist_search_page)
            raw_search_results = len(all_raw_notes)
            raw_notes = all_raw_notes[: config.max_notes]
            seen_note_ids: set[str] = set()
            window_counts = {WINDOW_INSIDE: 0, WINDOW_OUTSIDE: 0, WINDOW_UNDATED: 0}

            # 主采集循环：搜索卡片只提供摘要，详情接口负责补全文本。
            # 评论采集放在所有帖子详情完成后统一挑选，才能支持 Top N% 高互动帖子。
            for index, raw_item in enumerate(raw_notes, start=1):
                self._check_stop()
                # 搜索结果先归一化成 Note，再用详情接口补全文本。
                note = self._build_note(raw_item, config.keyword)
                if note is None:
                    run.errors.append({"stage": "normalize_note", "index": index, "error": "missing note_id"})
                    continue

                # 窗口过滤放在详情请求之前：实测搜索卡片 100% 带得出发布时间，
                # 先筛掉窗口外的帖子可以直接省下同等数量的详情调用。
                status = classify_published_at(note.published_at, window)
                window_counts[status] += 1
                if status == WINDOW_OUTSIDE:
                    continue

                xsec_token = self._extract_xsec_token(raw_item)
                detail = self.fetch_image_detail(note.note_id, xsec_token, run)
                note.raw = {"search": raw_item, "detail": detail}
                self._merge_detail(note, detail)
                self._tag_note_for_collection(note, config, seen_note_ids)
                run.notes.append(note)
                _write_collection_partials(
                    run_dir,
                    search_pages=search_pages,
                    notes=[item.to_dict() for item in run.notes],
                    run=run,
                    scan_mode=config.scan_mode,
                )

            _fetch_comments_by_policy(
                self,
                run,
                config,
                after_note=lambda: _write_collection_partials(
                    run_dir,
                    search_pages=search_pages,
                    notes=[item.to_dict() for item in run.notes],
                    comments=[item.to_dict() for item in run.comments],
                    run=run,
                    scan_mode=config.scan_mode,
                ),
            )

            # 三类核心产物：原始搜索页、归一化笔记、归一化评论。
            write_json(run_dir / "raw" / "search_pages.json", {"pages": search_pages})
            write_jsonl(run_dir / "notes.jsonl", [note.to_dict() for note in run.notes])
            write_jsonl(run_dir / "comments.jsonl", [comment.to_dict() for comment in run.comments])
            quality = self._collection_quality(run, search_pages, config)
            quality["reporting_window"] = describe_reporting_window(window, config.reporting_window_days)
            quality["collection_funnel"] = build_collection_funnel(
                search_pages_requested=len(search_pages),
                raw_search_results=raw_search_results,
                window_counts=window_counts,
                notes_after_collection=len(run.notes),
            )
            if window is not None:
                self._log(
                    run,
                    f"reporting window filter kept={window_counts[WINDOW_INSIDE]} "
                    f"removed_outside={window_counts[WINDOW_OUTSIDE]} undated_kept={window_counts[WINDOW_UNDATED]}",
                )
            write_json(
                run_dir / "collection.json",
                {
                    "keyword": run.keyword,
                    "scan_mode": config.scan_mode,
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
            finish_log_queue(self.log_queue)

    def search_notes(
        self,
        config: CollectConfig,
        run: CollectionRun,
        search_pages: list[dict[str, Any]],
        on_page: Optional[Callable[[dict[str, Any]], None]] = None,
    ) -> list[dict[str, Any]]:
        """搜索笔记并去重；每页按 App-V2 -> App -> Web-V3 顺序 fallback。"""

        notes: dict[str, dict[str, Any]] = {}
        app_v2_search_id: Optional[str] = None
        app_v2_session_id: Optional[str] = None
        app_search_id: Optional[str] = None
        app_session_id: Optional[str] = None

        for page in range(1, config.max_pages + 1):
            self._check_stop()
            backends = self._search_plan(
                config,
                page,
                app_v2_search_id,
                app_v2_session_id,
                app_search_id,
                app_session_id,
            )
            page_payload: Optional[dict[str, Any]] = None
            page_backend = ""

            # 每一页按三套接口依次尝试，前一个接口失败才落到下一个。
            # 错误会记录到 collection.json，方便之后判断是不是 token/接口变动问题。
            for backend in backends:
                self._check_stop()
                try:
                    self._log(run, f"search page={page} backend={backend['name']}")
                    payload = self._api_call(run, backend["path"], backend["params"])
                    page_payload = to_data_dict(payload)
                    page_backend = backend["name"]
                    break
                except Exception as exc:  # noqa: BLE001
                    run.errors.append(
                        {
                            "stage": "search",
                            "page": page,
                            "backend": backend["name"],
                            "path": backend["path"],
                            "error": str(exc),
                        }
                    )

            if page_payload is None:
                self._log(run, f"search page={page} failed on all backends; stop")
                break

            page_row = {"page": page, "backend": page_backend, "payload": page_payload}
            search_pages.append(page_row)
            if on_page is not None:
                on_page(page_row)
            # App-V2 / App 翻页依赖上一页返回的 search/session 标识。
            search_id, session_id = extract_search_session(page_payload)
            if search_id and session_id and page_backend == "app_v2":
                app_v2_search_id, app_v2_session_id = search_id, session_id
            elif search_id and session_id and page_backend == "app":
                app_search_id, app_session_id = search_id, session_id

            new_count = 0
            # TikHub 返回结构可能变化，所以不用固定路径取 notes，
            # 而是递归遍历整棵响应树，发现 note_id 就收集。
            for node in walk_nodes(page_payload.get("data", page_payload)):
                if not isinstance(node, dict):
                    continue
                note_id = extract_note_id(node)
                if note_id and note_id not in notes:
                    notes[note_id] = node
                    new_count += 1
            self._log(run, f"search page={page} new_notes={new_count}")

        return list(notes.values())

    def _search_plan(
        self,
        config: CollectConfig,
        page: int,
        app_v2_search_id: Optional[str],
        app_v2_session_id: Optional[str],
        app_search_id: Optional[str],
        app_session_id: Optional[str],
    ) -> list[dict[str, Any]]:
        """组装一页搜索的三套接口参数。"""

        app_v2_params: dict[str, Any] = {
            "keyword": config.keyword,
            "page": page,
            "sort_type": _app_sort_type(config.sort_type),
            "note_type": config.note_type,
            "time_filter": config.time_filter,
        }
        if page > 1:
            app_v2_params["search_id"] = app_v2_search_id
            app_v2_params["search_session_id"] = app_v2_session_id
            app_v2_params["source"] = "search"

        app_params: dict[str, Any] = {
            "keyword": config.keyword,
            "page": page,
            "sort_type": _app_sort_type(config.sort_type),
            "filter_note_type": config.note_type,
            "filter_note_time": config.time_filter,
        }
        if page > 1:
            app_params["search_id"] = app_search_id
            app_params["session_id"] = app_session_id

        web_params = {
            "keyword": config.keyword,
            "page": page,
            "sort": _web_v3_sort(config.sort_type),
            "note_type": _web_v3_note_type(config.note_type),
        }
        return [
            {"name": "app_v2", "path": APP_V2_SEARCH, "params": app_v2_params},
            {"name": "app", "path": APP_SEARCH, "params": app_params},
            {"name": "web_v3", "path": WEB_V3_SEARCH, "params": web_params},
        ]

    def fetch_image_detail(
        self,
        note_id: str,
        xsec_token: Optional[str],
        run: CollectionRun,
    ) -> dict[str, Any]:
        """抓图文详情；新版 App-V2 失败时再尝试旧 App 和 Web-V3。"""

        try:
            payload = self._api_call(run, APP_V2_IMAGE_DETAIL, {"note_id": note_id})
            detail = _data_or_raw(payload)
            detail["_backend"] = "app_v2_image"
            return detail
        except Exception as app_v2_exc:  # noqa: BLE001
            run.errors.append({"stage": "detail_app_v2_image", "note_id": note_id, "error": str(app_v2_exc)})

        try:
            payload = self._api_call(run, APP_DETAIL, {"note_id": note_id})
            detail = _data_or_raw(payload)
            detail["_fallback_backend"] = "app"
            return detail
        except Exception as app_exc:  # noqa: BLE001
            run.errors.append({"stage": "detail_app", "note_id": note_id, "error": str(app_exc)})

        try:
            payload = self._api_call(run, WEB_V3_DETAIL, {"note_id": note_id, "xsec_token": xsec_token})
            detail = _data_or_raw(payload)
            detail["_fallback_backend"] = "web_v3"
            return detail
        except Exception as web_exc:  # noqa: BLE001
            run.errors.append({"stage": "detail_web_v3", "note_id": note_id, "error": str(web_exc)})
            return {"note_id": note_id, "detail_error": str(web_exc)}

    def fetch_comments(
        self,
        note_id: str,
        comment_pages: int,
        sub_comment_pages: int,
        run: CollectionRun,
        sort_strategy: str = "latest_v2",
    ) -> list[Comment]:
        """抓一级评论；如开启 sub_comment_pages，再抓对应二级评论。"""

        comments: list[Comment] = []
        seen: set[tuple[str, Optional[str]]] = set()
        cursor = ""
        index = 0
        page_area = "UNFOLDED"

        # 一级评论按 cursor 翻页；如果开启二级评论，再对每条一级评论补抓子评论。
        # seen 用 comment_id + parent_comment_id 去重，避免 fallback 或分页重复。
        for page in range(1, comment_pages + 1):
            payload = self._fetch_comment_page(
                note_id,
                cursor,
                index,
                page_area,
                page,
                run,
                sort_strategy=sort_strategy,
            )
            if payload is None:
                break
            data = to_data_dict(payload).get("data")
            page_comments = self._extract_comment_rows(note_id, data, direct_only=True)
            self._log(
                run,
                f"comments page note_id={note_id} page={page} sort={sort_strategy} returned={len(page_comments)}",
            )
            for comment in page_comments:
                key = (comment.comment_id, comment.parent_comment_id)
                if key in seen:
                    continue
                comments.append(comment)
                seen.add(key)
                if sub_comment_pages > 0 and comment.parent_comment_id is None:
                    for sub in self.fetch_sub_comments(note_id, comment.comment_id, sub_comment_pages, run):
                        sub_key = (sub.comment_id, sub.parent_comment_id)
                        if sub_key not in seen:
                            comments.append(sub)
                            seen.add(sub_key)

            paging = _extract_comment_paging(data, default_index=index)
            if not paging["cursor"] or paging["cursor"] == cursor:
                break
            cursor = paging["cursor"]
            index = paging["index"]
            page_area = paging["page_area"]

        return comments

    def fetch_sub_comments(
        self,
        note_id: str,
        parent_comment_id: str,
        sub_comment_pages: int,
        run: CollectionRun,
    ) -> list[Comment]:
        """抓某条一级评论下面的二级评论。"""

        comments: list[Comment] = []
        cursor = ""
        index = 1
        for page in range(1, sub_comment_pages + 1):
            try:
                payload = self._api_call(
                    run,
                    APP_V2_SUB_COMMENTS,
                    {"note_id": note_id, "comment_id": parent_comment_id, "cursor": cursor, "index": index},
                )
            except Exception as exc:  # noqa: BLE001
                run.errors.append(
                    {
                        "stage": "sub_comments_app_v2",
                        "note_id": note_id,
                        "parent_comment_id": parent_comment_id,
                        "page": page,
                        "error": str(exc),
                    }
                )
                break

            data = to_data_dict(payload).get("data")
            comments.extend(self._extract_comment_rows(note_id, data, parent_comment_id=parent_comment_id))
            paging = _extract_comment_paging(data, default_index=index)
            if not paging["cursor"] or paging["cursor"] == cursor:
                break
            cursor = paging["cursor"]
            index = paging["index"]
        return comments

    def _fetch_comment_page(
        self,
        note_id: str,
        cursor: str,
        index: int,
        page_area: str,
        page: int,
        run: CollectionRun,
        sort_strategy: str = "latest_v2",
    ) -> Optional[dict[str, Any]]:
        """抓一页一级评论；新版 App-V2 失败时 fallback 到旧接口。"""

        try:
            return self._api_call(
                run,
                APP_V2_COMMENTS,
                {
                    "note_id": note_id,
                    "cursor": cursor,
                    "index": index,
                    "pageArea": page_area,
                    "sort_strategy": sort_strategy,
                },
            )
        except Exception as app_v2_exc:  # noqa: BLE001
            run.errors.append({"stage": "comments_app_v2", "note_id": note_id, "page": page, "error": str(app_v2_exc)})

        try:
            return self._api_call(run, APP_COMMENTS, {"note_id": note_id, "sort_strategy": 1, "start": cursor})
        except Exception as app_exc:  # noqa: BLE001
            run.errors.append({"stage": "comments_app", "note_id": note_id, "page": page, "error": str(app_exc)})

        try:
            return self._api_call(run, WEB_V3_COMMENTS, {"note_id": note_id, "cursor": cursor})
        except Exception as web_exc:  # noqa: BLE001
            run.errors.append({"stage": "comments_web_v3", "note_id": note_id, "page": page, "error": str(web_exc)})
            return None

    def _build_note(self, raw_item: dict[str, Any], keyword: str) -> Optional[Note]:
        """从搜索卡片中抽取笔记基础字段。"""

        note_id = extract_note_id(raw_item)
        if not note_id:
            return None
        note = raw_item.get("note") if isinstance(raw_item.get("note"), dict) else raw_item
        user = note.get("user") or note.get("user_info") or {}
        if not isinstance(user, dict):
            user = {}
        xsec_token = self._extract_xsec_token(raw_item)
        # 先看 note 节点本身，取不到再递归整张搜索卡片；后续详情接口仍可覆盖。
        published_at_raw = _published_at_from_node(note) or _published_at_from_tree(raw_item)
        return Note(
            note_id=note_id,
            keyword=keyword,
            title=pick_first(note, ["title", "note_title", "display_title", "name"]),
            body=pick_first(note, ["desc", "description", "content"]),
            author_id=_string_or_none(pick_first(user, ["user_id", "id"])),
            author_name=pick_first(user, ["nickname", "name", "user_name"]),
            # TikHub app_v2 用复数形式 comments_count / shared_count，必须放进候选名单，
            # 否则评论数提取不到，"高讨论度评论"门槛会把所有帖子拦掉。
            like_count=pick_first(note, ["liked_count", "like_count", "likes"]),
            comment_count=pick_first(note, ["comments_count", "comment_count", "comments"]),
            collect_count=pick_first(note, ["collected_count", "collect_count"]),
            share_count=pick_first(note, ["shared_count", "share_count", "shares"]),
            post_url=build_xhs_url(note_id, xsec_token),
            published_at=published_at_raw,
            published_at_raw=published_at_raw,
            collected_at=datetime.now().isoformat(timespec="seconds"),
            raw=raw_item,
        )

    def _merge_detail(self, note: Note, detail: Any) -> None:
        """用详情接口的完整标题/正文覆盖搜索卡片里的摘要字段。"""

        if not isinstance(detail, dict) or detail.get("detail_error"):
            return
        best_desc = ""
        best_title: Optional[str] = None
        for node in walk_nodes(detail):
            if not isinstance(node, dict):
                continue
            desc = _best_text(node)
            if desc and len(desc) > len(best_desc):
                best_desc = desc
                title = pick_first(node, ["title", "display_title", "note_title", "name"])
                best_title = str(title).strip() if title else best_title
        if best_desc and (not note.body or len(best_desc) > len(note.body)):
            note.body = best_desc
        if best_title and not _is_share_title(best_title):
            note.title = best_title
        published_at_raw = _published_at_from_tree(detail)
        if published_at_raw:
            note.published_at = published_at_raw
            note.published_at_raw = published_at_raw
        # 搜索卡片的互动数可能缺失或为 0，用详情接口的数值回填。
        for field, value in _counts_from_tree(detail).items():
            if value is None:
                continue
            if getattr(note, field) in (None, "", 0, "0"):
                setattr(note, field, value)

    def _extract_comment_rows(
        self,
        note_id: str,
        raw_data: Any,
        parent_comment_id: Optional[str] = None,
        direct_only: bool = False,
    ) -> list[Comment]:
        """把 TikHub 评论响应里的所有评论节点归一化为 Comment。"""

        rows: list[Comment] = []
        candidate_nodes = _direct_comment_nodes(raw_data)
        if direct_only and not candidate_nodes:
            return []
        if not candidate_nodes:
            candidate_nodes = [node for node in walk_nodes(raw_data) if isinstance(node, dict)]
        for node in candidate_nodes:
            if not isinstance(node, dict):
                continue
            detected_parent_id = parent_comment_id or _string_or_none(pick_first(node, ["parent_comment_id", "parent_id"]))
            if direct_only and detected_parent_id:
                continue
            comment_id = extract_comment_id(node)
            content = pick_first(node, ["content", "text", "desc", "message"])
            if not comment_id or not content:
                continue
            user = node.get("user_info") or node.get("user") or {}
            if not isinstance(user, dict):
                user = {}
            rows.append(
                Comment(
                    note_id=note_id,
                    comment_id=comment_id,
                    content=str(content),
                    parent_comment_id=detected_parent_id,
                    user_id=_string_or_none(pick_first(user, ["user_id", "id"])),
                    user_name=pick_first(user, ["nickname", "name", "user_name"]),
                    like_count=pick_first(node, ["like_count", "liked_count", "likes"]),
                    published_at=_published_at_from_node(node),
                    raw=node,
                )
            )
        return rows

    @staticmethod
    def _extract_xsec_token(raw_item: dict[str, Any]) -> Optional[str]:
        """从搜索结果里找 xsec_token，用于拼接小红书网页链接。"""

        for node in walk_nodes(raw_item):
            if isinstance(node, dict):
                token = pick_first(node, ["xsec_token", "xsecToken"])
                if token:
                    return str(token)
        return None

    def _tag_note_for_collection(
        self,
        note: Note,
        config: CollectConfig,
        seen_note_ids: set[str],
    ) -> None:
        """详情补全后立刻给笔记打标签，后续评论采集直接看标签决策。"""

        if note.note_id in seen_note_ids:
            note.is_valid = False
            note.skip_reasons.append("duplicate_note_id")
        else:
            seen_note_ids.add(note.note_id)

        # search_notes 已按 note_id 去重，这里是防御性保险，避免接口结构变化时重复进入后续流程。
        content_full = f"{note.title or ''} {note.body or ''}".strip()
        scope_text = _scope_text(note)

        # 采集阶段先做轻量质量判断：空内容、纯符号、范围外。
        # 更严格的清洗留给 process.py，保持两层职责清楚。
        if len(content_full) <= 2:
            note.is_valid = False
            note.skip_reasons.append("empty_or_too_short_content")
        if _looks_like_noise(content_full):
            note.is_valid = False
            note.skip_reasons.append("noise_content")

        if config.scope_pattern:
            if re.search(config.scope_pattern, scope_text) is None:
                note.is_scope_relevant = False
                note.skip_reasons.append("out_of_scope")

    def _log(self, run: CollectionRun, message: str) -> None:
        """同时写入内存日志和前端实时日志队列。"""

        run.logs.append(timestamped(message))
        emit_log(self.log_queue, message)

    def _check_stop(self) -> None:
        if self.stop_checker is not None:
            self.stop_checker()

    def _api_call(self, run: CollectionRun, path: str, params: dict[str, Any]) -> dict[str, Any]:
        """统一记录 TikHub 请求次数；TikHub 按请求计费，前端会用它估算采集成本。"""

        run.api_request_count += 1
        return self.client.call(path, params)

    def _make_run_dir(self, config: CollectConfig) -> Path:
        """按时间戳创建本次采集输出目录。"""

        base = Path(config.output_dir) if config.output_dir else self.output_root
        run_id = f"{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}_{uuid4().hex[:8]}"
        run_dir = base / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        return run_dir

    @staticmethod
    def _collection_quality(
        run: CollectionRun,
        search_pages: list[dict[str, Any]],
        config: CollectConfig,
    ) -> dict[str, Any]:
        """生成采集阶段质量摘要，方便先判断数据是否值得进入分析。"""

        note_ids = [note.note_id for note in run.notes]
        duplicate_note_ids = sorted({note_id for note_id in note_ids if note_ids.count(note_id) > 1})
        missing_body = [note.note_id for note in run.notes if not (note.body or "").strip()]
        invalid_notes = [note.note_id for note in run.notes if not note.is_valid]
        out_of_scope_notes = [note.note_id for note in run.notes if not note.is_scope_relevant]
        detail_failed = [
            note.note_id
            for note in run.notes
            if isinstance(note.raw, dict)
            and isinstance(note.raw.get("detail"), dict)
            and note.raw["detail"].get("detail_error")
        ]
        comment_skip_reasons_by_note = [
            _comment_skip_reasons(note, config)
            for note in run.notes
        ]
        comment_skip_reason_counts: dict[str, int] = {}
        for reasons in comment_skip_reasons_by_note:
            for reason in reasons:
                comment_skip_reason_counts[reason] = comment_skip_reason_counts.get(reason, 0) + 1

        return {
            "search_pages_saved": len(search_pages),
            "api_request_count": run.api_request_count or _estimate_api_request_count(run),
            "api_unit_cost_usd": 0.01,
            "api_estimated_cost_usd": round((run.api_request_count or _estimate_api_request_count(run)) * 0.01, 2),
            "search_backends_used": sorted({str(page.get("backend")) for page in search_pages if page.get("backend")}),
            "notes_saved": len(run.notes),
            "comments_saved": len(run.comments),
            "invalid_notes": len(invalid_notes),
            "invalid_note_ids": invalid_notes[:20],
            "out_of_scope_notes": len(out_of_scope_notes),
            "out_of_scope_note_ids": out_of_scope_notes[:20],
            "comment_fetch_enabled": config.comment_pages > 0,
            "comment_min_likes": config.comment_min_likes,
            "comment_min_comments": config.comment_min_comments,
            "comment_eligible_notes": len([reasons for reasons in comment_skip_reasons_by_note if not reasons]),
            "comments_skipped_before_fetch": len([reasons for reasons in comment_skip_reasons_by_note if reasons])
            if config.comment_pages > 0
            else 0,
            "comments_skipped_by_reason": comment_skip_reason_counts if config.comment_pages > 0 else {},
            "comments_skipped_by_like_threshold": comment_skip_reason_counts.get("below_comment_min_likes", 0)
            if config.comment_pages > 0
            else 0,
            "comments_skipped_by_comment_threshold": comment_skip_reason_counts.get("below_comment_min_comments", 0)
            if config.comment_pages > 0
            else 0,
            "missing_body_count": len(missing_body),
            "missing_body_note_ids": missing_body[:20],
            "detail_failed_count": len(detail_failed),
            "detail_failed_note_ids": detail_failed[:20],
            "duplicate_note_ids": duplicate_note_ids[:20],
            "error_count": len(run.errors),
        }


def _comment_skip_reasons(note: Note, config: CollectConfig) -> list[str]:
    """说明为什么某条笔记不进入评论采集。

    计数为 None 表示"未提取到"，不等于 0：未知计数不参与阈值过滤，
    否则接口字段名一变，所有帖子都会被门槛误杀。
    """

    reasons: list[str] = []
    if not note.is_valid:
        reasons.append("invalid_note")
    if not note.is_scope_relevant:
        reasons.append("out_of_scope")
    if config.comment_policy == "all":
        return reasons
    if (
        config.comment_policy != "top_notes"
        and note.like_count is not None
        and to_int(note.like_count) < config.comment_min_likes
    ):
        reasons.append("below_comment_min_likes")
    if note.comment_count is not None and to_int(note.comment_count) < config.comment_min_comments:
        reasons.append("below_comment_min_comments")
    return reasons


def _fetch_comments_by_policy(
    collector: XiaohongshuCollector,
    run: CollectionRun,
    config: CollectConfig,
    after_note: Optional[Callable[[], None]] = None,
) -> None:
    if config.comment_pages <= 0 or config.comment_policy == "none":
        return
    eligible = [note for note in run.notes if not _comment_skip_reasons(note, config)]
    if config.comment_policy == "top_notes":
        eligible = _top_percent_notes(
            eligible,
            percent=int(config.comment_top_percent or 20),
            limit=int(config.fetch_comments_for_top_notes or 0),
        )
        if not eligible:
            collector._log(
                run,
                "skip comments policy=top_notes reason=no_notes_met_minimum_discussion_threshold",
            )
    elif config.comment_policy != "all":
        eligible = [
            note
            for note in eligible
            if (note.like_count is None or to_int(note.like_count) >= config.comment_min_likes)
            and (note.comment_count is None or to_int(note.comment_count) >= config.comment_min_comments)
        ]

    selected_ids = {note.note_id for note in eligible}
    for note in run.notes:
        collector._check_stop()
        if note.note_id not in selected_ids:
            reasons = _comment_skip_reasons(note, config) or ["not_in_top_interaction_percent"]
            collector._log(run, f"skip comments note_id={note.note_id} reasons={reasons}")
            continue
        collector._log(
            run,
            f"collect comments note_id={note.note_id} policy={config.comment_policy} "
            f"engagement={_note_engagement(note)} comment_pages={config.comment_pages}",
        )
        run.comments.extend(
            collector.fetch_comments(
                note_id=note.note_id,
                comment_pages=config.comment_pages,
                sub_comment_pages=config.sub_comment_pages,
                run=run,
            )
        )
        if after_note is not None:
            after_note()


def _top_percent_notes(notes: list[Note], percent: int, limit: int = 0) -> list[Note]:
    if not notes:
        return []
    normalized_percent = min(100, max(1, int(percent or 20)))
    count = max(1, (len(notes) * normalized_percent + 99) // 100)
    if limit > 0:
        count = min(count, limit)
    return sorted(notes, key=_note_engagement, reverse=True)[:count]


def _note_engagement(note: Note) -> int:
    return to_int(note.like_count) + 2 * to_int(note.collect_count) + 3 * to_int(note.comment_count) + to_int(note.share_count)


def _estimate_api_request_count(run: CollectionRun) -> int:
    """兼容旧 run：没有显式请求计数时，用日志和错误数粗略估算。"""

    search_attempts = sum(1 for line in run.logs if "search page=" in line and "backend=" in line)
    detail_attempts = len(run.notes)
    comment_attempts = sum(1 for line in run.logs if "collect comments note_id=" in line)
    fallback_attempts = len(run.errors)
    return search_attempts + detail_attempts + comment_attempts + fallback_attempts


def _write_collection_partials(
    run_dir: Path,
    *,
    search_pages: Optional[list[dict[str, Any]]] = None,
    notes: Optional[list[dict[str, Any]]] = None,
    comments: Optional[list[dict[str, Any]]] = None,
    run: Optional[CollectionRun] = None,
    scan_mode: str = "",
) -> None:
    if search_pages is not None:
        write_json(run_dir / "raw" / "search_pages.partial.json", {"pages": search_pages})
    if notes is not None:
        write_jsonl(run_dir / "notes.partial.jsonl", notes)
    if comments is not None:
        write_jsonl(run_dir / "comments.partial.jsonl", comments)
    if run is not None:
        # collection.json 只在采集全部结束后才写，运行期间前端没有任何东西可读。
        # 这里额外落一份 collection.partial.json，让前端能在采集进行中就看到
        # 已找到的帖子数/评论数/日志/请求数，不用等到整轮采集结束。
        write_json(
            run_dir / "collection.partial.json",
            {
                "partial": True,
                "keyword": run.keyword,
                "scan_mode": scan_mode,
                "run_dir": run.run_dir,
                "search_pages_count": len(search_pages) if search_pages is not None else 0,
                "notes_count": len(notes) if notes is not None else len(run.notes),
                "comments_count": len(comments) if comments is not None else len(run.comments),
                "errors": run.errors,
                "logs": run.logs,
                "api_request_count": run.api_request_count,
            },
        )


def _scope_text(note: Note) -> str:
    """范围匹配文本：keyword、标题、正文和常见标签/话题字段一起参与 regex。"""

    parts = [note.keyword or "", note.title or "", note.body or ""]
    raw = note.raw if isinstance(note.raw, dict) else {}
    parts.extend(_extract_scope_tags(raw))
    return " ".join(part for part in parts if str(part).strip())


def _extract_scope_tags(raw: Any) -> list[str]:
    """从 search/detail 原始响应里提取 tag/hashtag/topic 类文本。"""

    tags: list[str] = []
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
    for node in walk_nodes(raw):
        if isinstance(node, dict):
            for key in tag_keys:
                value = node.get(key)
                if isinstance(value, str) and value.strip():
                    tags.append(value.strip())
        elif isinstance(node, str) and node.startswith("#"):
            tags.append(node.strip())
    return tags


# 互动计数的候选字段名：TikHub 不同接口混用单/复数形式。
COUNT_FIELD_KEYS = {
    "like_count": ["liked_count", "like_count", "likes"],
    "comment_count": ["comments_count", "comment_count", "comments"],
    "collect_count": ["collected_count", "collect_count"],
    "share_count": ["shared_count", "share_count", "shares"],
}


def _counts_from_tree(payload: Any) -> dict[str, Any]:
    """从详情响应树里找第一个带互动计数的节点（即主笔记节点），提取四类计数。"""

    all_keys = {key for keys in COUNT_FIELD_KEYS.values() for key in keys}
    for node in walk_nodes(payload):
        if isinstance(node, dict) and any(key in node for key in all_keys):
            return {field: pick_first(node, keys) for field, keys in COUNT_FIELD_KEYS.items()}
    return {}


def _data_or_raw(payload: Any) -> dict[str, Any]:
    """把 TikHub 响应中的 data 取出来；非 dict 时包成 raw_data。"""

    data = to_data_dict(payload).get("data")
    return data if isinstance(data, dict) else {"raw_data": data}


def _extract_comment_paging(raw_data: Any, default_index: int) -> dict[str, Any]:
    """兼容新版 cursor 对象和旧版字符串 cursor。"""

    for node in walk_nodes(raw_data):
        if not isinstance(node, dict):
            continue
        cursor_obj = node.get("cursor")
        if isinstance(cursor_obj, str) and cursor_obj.strip().startswith("{"):
            try:
                decoded = json.loads(cursor_obj)
            except json.JSONDecodeError:
                decoded = None
            if isinstance(decoded, dict):
                cursor = decoded.get("cursor") or decoded.get("value")
                if cursor:
                    return {
                        "cursor": str(cursor),
                        "index": _int_or_default(decoded.get("index"), default_index),
                        "page_area": str(decoded.get("pageArea") or decoded.get("page_area") or "UNFOLDED"),
                    }
        if isinstance(cursor_obj, dict):
            cursor = cursor_obj.get("cursor") or cursor_obj.get("value")
            if cursor:
                return {
                    "cursor": str(cursor),
                    "index": _int_or_default(cursor_obj.get("index"), default_index),
                    "page_area": str(cursor_obj.get("pageArea") or cursor_obj.get("page_area") or "UNFOLDED"),
                }
    for node in walk_nodes(raw_data):
        if not isinstance(node, dict):
            continue
        cursor = node.get("cursor") or node.get("next_cursor") or node.get("start")
        if isinstance(cursor, str) and cursor.strip():
            return {
                "cursor": cursor.strip(),
                "index": _int_or_default(node.get("index"), default_index),
                "page_area": str(node.get("pageArea") or node.get("page_area") or "UNFOLDED"),
            }
    return {"cursor": "", "index": default_index, "page_area": "UNFOLDED"}


def _direct_comment_nodes(raw_data: Any) -> list[dict[str, Any]]:
    """优先取接口返回的顶层 comments，避免把内嵌楼中楼回复误算成一级评论。"""

    if isinstance(raw_data, dict):
        comments = raw_data.get("comments")
        if isinstance(comments, list):
            return [item for item in comments if isinstance(item, dict)]
    for node in walk_nodes(raw_data):
        if not isinstance(node, dict):
            continue
        comments = node.get("comments")
        if isinstance(comments, list):
            return [item for item in comments if isinstance(item, dict)]
    return []


def _best_text(node: dict[str, Any]) -> Optional[str]:
    """从详情节点中提取正文，优先拼 text_segment_list。"""

    segments = node.get("text_segment_list")
    if isinstance(segments, list):
        joined = "".join(str(seg.get("text") or seg.get("content") or "") for seg in segments if isinstance(seg, dict))
        if joined.strip():
            return joined.strip()
    for key in ("desc", "description", "content", "note_text"):
        value = node.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _web_v3_sort(sort_type: str) -> str:
    """把内部排序值映射到 Web-V3 接口参数。"""

    return {
        "general": "general",
        "relevance": "general",
        "time_descending": "time_descending",
        "popularity_descending": "popularity_descending",
    }.get(sort_type or "general", "general")


def _app_sort_type(sort_type: str) -> str:
    """把产品排序值映射到 App 接口参数。"""

    return {
        "relevance": "general",
        "general": "general",
        "time_descending": "time_descending",
        "popularity_descending": "popularity_descending",
    }.get(sort_type or "general", "general")


def _web_v3_note_type(note_type: str) -> int:
    """把中文笔记类型映射到 Web-V3 数字枚举。"""

    return {"不限": 0, "视频笔记": 2, "普通笔记": 1, "直播笔记": 0}.get(note_type or "普通笔记", 1)


def _is_share_title(value: str) -> bool:
    """过滤分享卡片标题，避免覆盖真实笔记标题。"""

    return any(marker in value for marker in ("发了一篇", "超赞的笔记", "快点来看", "discovery/item"))


def _looks_like_noise(value: str) -> bool:
    """识别纯链接、纯符号等明显无效内容。"""

    text = value.strip()
    if not text:
        return True
    if re.fullmatch(r"(?:https?://\S+|www\.\S+)", text, flags=re.I):
        return True
    return re.fullmatch(r"\W+", text) is not None


def _string_or_none(value: Any) -> Optional[str]:
    """把非空值转成字符串，空值保持 None。"""

    if value is None or value == "":
        return None
    return str(value)


# 发布时间字段优先于 update_time/updated_time 这类编辑时间字段。
# ---------------------------------------------------------------------------
# 周报窗口（reporting window）
#
# TikHub 的 time_filter="一周内" 只是搜索侧的软条件：实测 8/24 那次 Broad run
# 明明传了「一周内」，仍有 11/110 条来自两年前。所以这里再加一道基于
# published_at 的硬过滤，窗口是从 run 起始时刻往前推的精确 REPORTING_WINDOW_DAYS × 24 小时。
#
# 时区约定：全链路（_parse_reported_datetime / datetime.now / collected_at）都用
# naive 本地时间，窗口起止会连同 UTC 偏移一起写进 collection.json，
# 让下游只读记录、不再自己重算，避免部署在 UTC 机器上时边界漂移 8 小时。
# ---------------------------------------------------------------------------

WINDOW_INSIDE = "inside"
WINDOW_OUTSIDE = "outside"
WINDOW_UNDATED = "undated"


def reporting_window_bounds(
    run_started_at: datetime,
    days: int = REPORTING_WINDOW_DAYS,
) -> tuple[datetime, datetime]:
    """返回 [start, end]；end 是 run 起始时刻，start 是往前精确 days × 24 小时。"""

    return run_started_at - timedelta(days=max(1, int(days or REPORTING_WINDOW_DAYS))), run_started_at


def window_is_active(config: CollectConfig) -> bool:
    """只有搜索本身限定「一周内」时才启用硬窗口。"""

    return (
        str(getattr(config, "time_filter", "") or "") == WEEKLY_TIME_FILTER
        and int(getattr(config, "reporting_window_days", 0) or 0) > 0
    )


def classify_published_at(
    value: Any,
    window: Optional[tuple[datetime, datetime]],
) -> str:
    """把一条笔记的发布时间归入 inside / outside / undated。

    undated 表示"拿不到可解析的发布时间"，与"确认在窗口外"是两回事：
    按产品决定，undated 会被保留并单独计数，不会被当成旧帖丢掉。
    """

    if window is None:
        return WINDOW_INSIDE
    published_at = _parse_reported_datetime(value)
    if published_at is None:
        return WINDOW_UNDATED
    start, end = window
    return WINDOW_INSIDE if start <= published_at <= end else WINDOW_OUTSIDE


def _parse_reported_datetime(value: Any) -> Optional[datetime]:
    """解析 TikHub 的发布时间；与 analyze._parse_datetime 保持同一套口径。

    支持秒/毫秒级 unix 时间戳与常见字符串格式，一律返回 naive 本地时间。
    """

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
            if number <= 0:
                return None
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


def describe_reporting_window(
    window: Optional[tuple[datetime, datetime]],
    days: int = REPORTING_WINDOW_DAYS,
) -> dict[str, Any]:
    """写进 collection.json 的窗口描述；下游只读这份记录，不再自行推算。"""

    if window is None:
        return {"applied": False, "days": int(days or 0), "start": "", "end": "", "utc_offset": ""}
    start, end = window
    offset = datetime.now().astimezone().strftime("%z")
    return {
        "applied": True,
        "days": int(days or REPORTING_WINDOW_DAYS),
        "start": start.isoformat(timespec="seconds"),
        "end": end.isoformat(timespec="seconds"),
        "utc_offset": f"{offset[:3]}:{offset[3:]}" if len(offset) == 5 else offset,
    }


def build_collection_funnel(
    *,
    search_pages_requested: int,
    raw_search_results: int,
    window_counts: dict[str, int],
    notes_after_collection: int,
    duplicates_removed: int = 0,
    keep_undated: bool = True,
) -> dict[str, Any]:
    """采集阶段漏斗；主要用于调试与后续采集成本优化，不面向报告读者。

    对账关系：
        raw_search_results
          - removed_outside_window
          - removed_cross_keyword_duplicate
          - removed_by_collector (空内容/噪声/详情失败等)
          = notes_after_collection
    去重与相关性两段分别记在 processing.json 和 analysis.json，
    完整口径见 processing.json 的 collection_funnel + removed_note_reasons。
    """

    inside = int(window_counts.get(WINDOW_INSIDE, 0))
    outside = int(window_counts.get(WINDOW_OUTSIDE, 0))
    undated = int(window_counts.get(WINDOW_UNDATED, 0))
    kept_by_window = inside + (undated if keep_undated else 0)
    return {
        "search_pages_requested": int(search_pages_requested),
        "raw_search_results": int(raw_search_results),
        "within_reporting_window": inside,
        "removed_outside_window": outside,
        "undated_kept": undated if keep_undated else 0,
        "removed_undated": 0 if keep_undated else undated,
        "kept_after_window_filter": kept_by_window,
        "removed_cross_keyword_duplicate": int(duplicates_removed),
        "removed_by_collector": max(0, kept_by_window - int(duplicates_removed) - int(notes_after_collection)),
        "notes_after_collection": int(notes_after_collection),
    }


PUBLISHED_AT_KEYS = [
    "timestamp",
    "time",
    "create_time",
    "created_time",
    "publish_time",
    "published_at",
    "update_time",
    "updated_time",
]


def _published_at_from_node(node: Any) -> Optional[str]:
    """从单个 TikHub 节点提取发布时间，兼容 App-V2 timestamp/update_time 字段。

    0 是 TikHub 常见的占位时间戳，视为缺失，避免解析成 1970 年。
    """

    if not isinstance(node, dict):
        return None
    for key in PUBLISHED_AT_KEYS:
        value = node.get(key)
        if value in (None, "", 0, "0"):
            continue
        return str(value)
    return None


def _published_at_from_tree(payload: Any) -> Optional[str]:
    """从详情响应树中找第一个可用发布时间。"""

    for node in walk_nodes(payload):
        value = _published_at_from_node(node)
        if value:
            return value
    return None


def _int_or_default(value: Any, default: int) -> int:
    """cursor/index 解析失败时返回默认值。"""

    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def build_xhs_url(note_id: str, xsec_token: Optional[str]) -> str:
    """生成可打开的小红书网页笔记链接。"""

    if xsec_token:
        return f"https://www.xiaohongshu.com/explore/{note_id}?xsec_token={quote(xsec_token, safe='')}&xsec_source=pc_search"
    return f"https://www.xiaohongshu.com/explore/{note_id}"
