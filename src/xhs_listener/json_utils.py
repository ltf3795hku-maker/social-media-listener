from __future__ import annotations

from collections.abc import Iterable
from typing import Any, Optional


# TikHub 的响应层级经常不稳定，所以这些工具都偏“宽松”：
# 不假设固定路径，而是递归遍历、按候选字段名找 note/comment/session 信息。
def to_data_dict(payload: Any) -> dict[str, Any]:
    """TikHub 响应常包一层 data；这里统一取可继续遍历的 dict。"""

    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload
    return {"raw_data": payload}


def walk_nodes(value: Any) -> Iterable[Any]:
    """递归遍历 dict/list 树，适合 TikHub 返回结构不稳定时兜底找字段。"""

    yield value
    if isinstance(value, dict):
        for item in value.values():
            yield from walk_nodes(item)
    elif isinstance(value, list):
        for item in value:
            yield from walk_nodes(item)


def pick_first(obj: Any, keys: list[str]) -> Any:
    """按候选字段名顺序取第一个非空值。"""

    if not isinstance(obj, dict):
        return None
    for key in keys:
        value = obj.get(key)
        if value is not None and value != "":
            return value
    return None


def extract_note_id(node: Any) -> Optional[str]:
    """从搜索卡片或嵌套 note 节点里提取 note_id。"""

    if not isinstance(node, dict):
        return None
    direct = pick_first(node, ["note_id", "noteId", "id", "item_id"])
    if direct:
        return str(direct)
    note = node.get("note")
    if isinstance(note, dict):
        nested = pick_first(note, ["note_id", "noteId", "id", "item_id"])
        if nested:
            return str(nested)
    return None


def extract_comment_id(node: Any) -> Optional[str]:
    """从评论节点里提取 comment_id。"""

    value = pick_first(node, ["comment_id", "commentId", "id"]) if isinstance(node, dict) else None
    return str(value) if value is not None else None


def extract_search_session(payload: Any) -> tuple[Optional[str], Optional[str]]:
    """App-V2 翻页需要 search_id/search_session_id，从响应树里尽量提取。"""

    fallback: tuple[Optional[str], Optional[str]] = (None, None)
    for node in walk_nodes(payload):
        if not isinstance(node, dict):
            continue
        search_id = node.get("search_id") or node.get("searchId")
        session_id = (
            node.get("search_session_id")
            or node.get("searchSessionId")
            or node.get("session_id")
            or node.get("sessionId")
        )
        if isinstance(search_id, str) and isinstance(session_id, str):
            if search_id.strip() and session_id.strip():
                pair = (search_id.strip(), session_id.strip())
                if "items" in node or "notes" in node or "page" in node:
                    return pair
                fallback = pair
    return fallback
