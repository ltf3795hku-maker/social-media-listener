from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


# 文件读写统一放这里，保证所有 JSON 都是 UTF-8，
# JSONL 文件不存在时返回空列表，方便流水线的可选评论步骤容错。
def read_json(path: str | Path) -> Any:
    """读取 UTF-8 JSON 文件。"""

    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_json(path: str | Path, payload: Any) -> None:
    """写入格式化 JSON；父目录不存在时自动创建。"""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def read_jsonl(path: str | Path) -> list[dict[str, Any]]:
    """读取 JSONL；文件不存在时返回空列表，方便后续步骤容错。"""

    src = Path(path)
    if not src.exists():
        return []
    rows: list[dict[str, Any]] = []
    # JSONL 只能按真实换行符切分。小红书正文里可能包含 U+2028/U+2029，
    # Python 的 splitlines() 会把它们也当换行，导致单条 JSON 被切碎。
    for line in src.read_text(encoding="utf-8").split("\n"):
        if line.strip():
            rows.append(json.loads(line))
    return rows


def read_jsonl_lenient(path: str | Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """容错读取 JSONL；坏行跳过并返回错误列表，适合前端预览。"""

    src = Path(path)
    if not src.exists():
        return [], []
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for line_no, line in enumerate(src.read_text(encoding="utf-8").split("\n"), start=1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError as exc:
            errors.append({"line": line_no, "error": str(exc)})
    return rows, errors


def write_jsonl(path: str | Path, rows: Iterable[dict[str, Any]]) -> None:
    """逐行写入 JSONL，用于 notes/comments 这类列表数据。"""

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as fh:
        for row in rows:
            line = json.dumps(row, ensure_ascii=False)
            line = line.replace("\u2028", "\\u2028").replace("\u2029", "\\u2029")
            fh.write(line + "\n")
