"""第四步：把当前 analysis.json schema 渲染为结构化报告和 HTML。"""
from __future__ import annotations

import html
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from xhs_listener.collect import _parse_reported_datetime
from xhs_listener.models import REPORTING_WINDOW_DAYS
from xhs_listener.io_utils import read_json, read_jsonl, write_json
from xhs_listener.log_utils import emit_log, finish_log_queue, timestamped
from xhs_listener.number_utils import to_int


def _load_report_context(run_path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    """report_run 和 report_run_english 共用的上下文装配：读 analysis.json/processing.json，
    挂上帖子链接、竞对周报等报告渲染需要的附加字段。"""

    analysis_bundle = read_json(run_path / "analysis.json")
    processing = read_json(run_path / "processing.json")
    collection_path = run_path / "collection.json"
    if collection_path.exists() and not processing.get("comment_collection_status"):
        processing["comment_collection_status"] = _report_comment_status(read_json(collection_path))
    analysis_bundle["_note_post_urls"] = _note_post_url_lookup(run_path)
    analysis_bundle["_broad_top_posts"] = _broad_top_posts_from_run(run_path)
    top10_collection_path = run_path / "top10_comment_collection.json"
    analysis_bundle["_top10_comment_collection"] = (
        read_json(top10_collection_path) if top10_collection_path.exists() else {}
    )
    competitor_path = run_path / "competitor_analysis.json"
    analysis_bundle["_competitor_weekly"] = read_json(competitor_path) if competitor_path.exists() else {}
    return analysis_bundle, processing


def report_run(
    run_dir: str | Path,
    log_queue: Optional[Any] = None,
) -> dict[str, Any]:
    """把当前分析结果本地渲染为 report.json/html，不调用 LLM。"""

    run_path = Path(run_dir)
    logs: list[str] = []
    _log(logs, log_queue, f"Report start run_dir={run_path}")

    try:
        analysis_bundle, processing = _load_report_context(run_path)

        # 先拿结构化内容，再用本地模板渲染，避免直接让模型写 Markdown/HTML 导致格式漂移。
        structured = build_structured_report(analysis_bundle, processing)
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html_report = build_report_html(structured, analysis_bundle, processing, generated_at, locale="zh")

        write_json(run_path / "report.json", structured)
        (run_path / "report.html").write_text(html_report, encoding="utf-8")
        write_json(run_path / "reporting.json", {"generated_at": generated_at, "logs": logs})
        return {
            "run_dir": str(run_path),
            "report_json": str(run_path / "report.json"),
            "report_html": str(run_path / "report.html"),
            "logs": logs,
        }
    finally:
        finish_log_queue(log_queue)


# ---------------------------------------------------------------------------
# 英文版报告：不重新分析，只把已经生成好的中文 report.json 翻译成英文。
#
# 为什么这样做而不是重新跑一遍 analyze：
# - 省 token —— 分析阶段（标注 + 生成结论）才是真正烧钱的部分，报告只是渲染。
# - 中英文版本的事实/结论保持一致，不会出现两个版本各说各话。
#
# 翻译范围怎么定：只翻译"叶子字符串里含中文字符"的内容。报告 schema 里的
# 情绪/内容类型/作者类型等字段本来就是英文枚举码（渲染时才由 _label() 按
# locale 转成中文/英文显示文本），note_id/URL/日期/数字天然不含中文——
# 用"是否含中文字符"这一个信号就能同时避开这些字段，不需要维护一份容易
# 漏掉字段的白名单。
# ---------------------------------------------------------------------------

_CJK_RE = re.compile(r"[一-鿿]")
_I18N_MARKER = "__i18n_idx__"


def _extract_translatable(node: Any, collected: list[str]) -> Any:
    """深拷贝 node；把含中文字符的字符串换成占位符，同时把原文按顺序收进 collected。"""

    if isinstance(node, str):
        if _CJK_RE.search(node):
            collected.append(node)
            return {_I18N_MARKER: len(collected) - 1}
        return node
    if isinstance(node, dict):
        return {key: _extract_translatable(value, collected) for key, value in node.items()}
    if isinstance(node, list):
        return [_extract_translatable(item, collected) for item in node]
    return node


def _apply_translations(node: Any, translations: list[str], originals: list[str]) -> Any:
    """把占位符换回翻译结果；某一条翻译缺失/是空字符串时退回原文，不留空洞。"""

    if isinstance(node, dict) and set(node.keys()) == {_I18N_MARKER}:
        idx = node[_I18N_MARKER]
        translated = translations[idx].strip() if 0 <= idx < len(translations) else ""
        return translated or originals[idx]
    if isinstance(node, dict):
        return {key: _apply_translations(value, translations, originals) for key, value in node.items()}
    if isinstance(node, list):
        return [_apply_translations(item, translations, originals) for item in node]
    return node


def _translation_prompt(batch: list[str]) -> str:
    numbered = json.dumps(batch, ensure_ascii=False)
    return (
        "You are translating fragments of a Xiaohongshu (Red Note) social-listening report "
        "for HKU Business School from Chinese to English.\n"
        "Translate each string in the JSON array below into natural, professional English.\n"
        "Rules:\n"
        "- Return a JSON array with exactly the same length and order as the input; one output "
        "string per input string.\n"
        "- Preserve any URLs, @handles, note IDs, numbers and proper nouns embedded inside a "
        "string exactly as they are.\n"
        "- If a string is already in English or has nothing meaningful to translate, return it unchanged.\n"
        "- Respond with ONLY the JSON array of strings, no commentary or extra fields.\n\n"
        f"Input array:\n{numbered}"
    )


def translate_structured_report_to_english(
    structured: dict[str, Any],
    client: Any,
    *,
    batch_size: int = 60,
    max_batch_retries: int = 1,
    logs: Optional[list[str]] = None,
    log_queue: Optional[Any] = None,
) -> tuple[dict[str, Any], list[dict[str, Any]], dict[str, int]]:
    """把 structured report 里所有含中文的叶子字符串批量翻成英文。

    按 batch_size 分批调用；一批失败会原地重试 max_batch_retries 次（常见的
    网络抖动/限流靠这个就能救回来），重试完还是失败才让那一批保留中文原文——
    不让整份报告因为一次调用失败而生成不出来，但也不能悄悄地把"翻译失败"
    和"翻译成功"混在一起看不出来。

    返回 (翻译后的 structured, usage_rows, coverage)；coverage 是
    {"total_strings": N, "translated_strings": M}，调用方可以用它判断这次
    翻译是不是完整的，不完整就该提示用户可以重新生成再试一次。
    """

    logs = logs if logs is not None else []
    originals: list[str] = []
    template = _extract_translatable(structured, originals)
    translations: list[str] = list(originals)  # 默认用原文占位，逐批覆盖成功翻译的部分
    translated_indices: set[int] = set()
    usage_rows: list[dict[str, Any]] = []

    total_batches = (len(originals) + batch_size - 1) // batch_size if originals else 0
    for batch_index, start in enumerate(range(0, len(originals), batch_size), start=1):
        batch = originals[start : start + batch_size]
        if not batch:
            continue
        for attempt in range(max_batch_retries + 1):
            _log(
                logs,
                log_queue,
                f"Translating batch {batch_index}/{total_batches} ({len(batch)} strings)"
                + (f", retry {attempt}" if attempt else ""),
            )
            try:
                response = client.get_response([{"role": "user", "content": _translation_prompt(batch)}])
                if isinstance(response, str):
                    raise RuntimeError(response)
                usage = client.usage_dict(response) or {}
                usage["phase"] = "report_translate_en"
                usage_rows.append(usage)
                payload = client.extract_json(response.choices[0].message.content)
                if not isinstance(payload, list):
                    raise ValueError("translation response must be a JSON array")
                for offset, value in enumerate(payload):
                    if isinstance(value, str) and value.strip():
                        translations[start + offset] = value.strip()
                        translated_indices.add(start + offset)
                break  # 这一批成功，不用再重试
            except Exception as exc:  # noqa: BLE001
                if attempt < max_batch_retries:
                    continue
                # 重试次数用完还是失败：保留原文（已经是默认值），继续下一批，
                # 不让整份报告翻译失败。
                _log(logs, log_queue, f"Batch {batch_index} failed after {attempt + 1} attempt(s), keeping Chinese: {exc}")

    coverage = {"total_strings": len(originals), "translated_strings": len(translated_indices)}
    return _apply_translations(template, translations, originals), usage_rows, coverage


def report_run_english(
    run_dir: str | Path,
    client: Optional[Any] = None,
    log_queue: Optional[Any] = None,
) -> dict[str, Any]:
    """在已有的中文 report.json 基础上生成英文版 report_en.json/report_en.html。

    要求中文报告已经生成过（report.json 存在）；不重新调用 analyze，只翻译。
    """

    run_path = Path(run_dir)
    logs: list[str] = []
    _log(logs, log_queue, f"English report start run_dir={run_path}")

    try:
        structured_zh_path = run_path / "report.json"
        if not structured_zh_path.exists():
            raise FileNotFoundError("report.json not found; generate the Chinese report first")
        structured_zh = read_json(structured_zh_path)
        analysis_bundle, processing = _load_report_context(run_path)

        if client is None:
            from xhs_listener.llm_client import LLMClient

            client = LLMClient()

        structured_en, usage_rows, coverage = translate_structured_report_to_english(
            structured_zh, client, logs=logs, log_queue=log_queue
        )
        if coverage["total_strings"]:
            _log(
                logs,
                log_queue,
                f"Translation coverage: {coverage['translated_strings']}/{coverage['total_strings']} strings",
            )
        # 覆盖率写进 report_en.json 里一个下划线打头的 key——build_report_html 是
        # 按已知字段名取值渲染的，不会遍历/展示未知 key，所以这个 key 对报告排版
        # 完全无影响，纯粹是给前端读出来提示"这次翻译有没有漏"。
        structured_en["_translation_coverage"] = coverage
        generated_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        html_report_en = build_report_html(structured_en, analysis_bundle, processing, generated_at, locale="en")

        write_json(run_path / "report_en.json", structured_en)
        (run_path / "report_en.html").write_text(html_report_en, encoding="utf-8")

        from xhs_listener.analyze import _merge_usage_rows

        write_json(run_path / "llm_usage.json", {"usage": _merge_usage_rows(run_path / "llm_usage.json", usage_rows)})
        return {
            "run_dir": str(run_path),
            "report_json_en": str(run_path / "report_en.json"),
            "report_html_en": str(run_path / "report_en.html"),
            "usage_rows": usage_rows,
            "translation_coverage": coverage,
            "logs": logs,
        }
    finally:
        finish_log_queue(log_queue)


def build_structured_report(
    analysis_bundle: dict[str, Any],
    processing: dict[str, Any],
) -> dict[str, Any]:
    """把分析阶段的 HKU schema 直接规范化为报告 schema。

    报告层不调用 LLM：分析阶段已经负责判断，报告阶段只负责稳定渲染。
    这样可以省 token，也避免旧模板把商业噪声、主题概览、建议混在一起。
    """

    analysis = dict(analysis_bundle.get("analysis") or {})
    return _ensure_report_defaults({}, analysis, processing, analysis_bundle)


def _report_mode(structured: dict[str, Any], analysis_bundle: dict[str, Any], processing: dict[str, Any]) -> str:
    mode = _as_text(structured.get("report_mode") or analysis_bundle.get("report_mode"), "").strip()
    if mode in {"broad_report", "topic_report"}:
        return mode
    return "broad_report" if _scan_mode_from_processing(processing) == "broad_scan" else "topic_report"


def _note_post_url_lookup(run_path: Path) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for filename in ("processed_notes.jsonl", "notes.jsonl"):
        path = run_path / filename
        if not path.exists():
            continue
        for note in read_jsonl(path):
            note_id = _as_text(note.get("note_id"), "")
            post_url = _as_text(note.get("post_url"), "")
            if note_id and post_url:
                lookup.setdefault(note_id, _public_post_url(post_url, note_id))
    return lookup


_COUNT_FIELD_KEYS = {
    "like_count": {"liked_count", "like_count", "likes"},
    "comment_count": {"comments_count", "comment_count", "comments"},
    "share_count": {"shared_count", "share_count", "shares"},
}


def _raw_contains_any_key(value: Any, keys: set[str]) -> bool:
    if isinstance(value, dict):
        return any(key in value for key in keys) or any(_raw_contains_any_key(item, keys) for item in value.values())
    if isinstance(value, list):
        return any(_raw_contains_any_key(item, keys) for item in value)
    return False


def _available_count(note: dict[str, Any], field: str) -> Optional[int]:
    """Return a known engagement count while keeping unavailable metrics blank.

    Processed historical rows may contain a synthetic zero for a missing TikHub
    field. When raw data exists, only show that zero if the source actually
    contained a corresponding count key.
    """

    value = note.get(field)
    if value in (None, ""):
        return None
    count = to_int(value)
    raw = note.get("raw")
    if count == 0 and raw is not None and not _raw_contains_any_key(raw, _COUNT_FIELD_KEYS[field]):
        return None
    return count


def _short_post_excerpt(note: dict[str, Any], limit: int = 180) -> str:
    text = " ".join(_as_text(note.get("body") or note.get("content_full"), "").split())
    title = " ".join(_as_text(note.get("title"), "").split())
    if title and text.startswith(title):
        text = text[len(title) :].strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip("，。！？；,.!?; ") + "…"


def _readable_post_date(value: Any) -> str:
    """把发布时间渲染成人类可读日期。

    TikHub 的 published_at 常常是 unix 时间戳字符串，直接展示会在 Top 10 卡片上
    出现「发布时间：1787532305」这种读者无法理解的数字。解析失败时保留原值，
    不猜测、不留空。
    """

    text = _as_text(value, "").strip()
    if not text:
        return ""
    parsed = _parse_reported_datetime(text)
    return parsed.strftime("%Y-%m-%d") if parsed is not None else text


def _broad_top_posts_from_run(run_path: Path) -> list[dict[str, Any]]:
    notes_path = run_path / "processed_notes.jsonl"
    annotations_path = run_path / "annotations.jsonl"
    if not notes_path.exists() or not annotations_path.exists():
        return []
    annotations = {
        _as_text(row.get("note_id"), ""): row
        for row in read_jsonl(annotations_path)
        if _as_text(row.get("note_id"), "")
    }
    audience_path = run_path / "top10_comment_analysis.json"
    audience_payload = read_json(audience_path) if audience_path.exists() else {}
    audience_by_id = {
        _as_text(row.get("note_id"), ""): row
        for row in _as_dict_list(audience_payload.get("posts"))
        if _as_text(row.get("note_id"), "")
    }
    rows: list[dict[str, Any]] = []
    for note in read_jsonl(notes_path):
        note_id = _as_text(note.get("note_id"), "")
        annotation = annotations.get(note_id, {})
        if _as_text(annotation.get("hku_relevance"), "").lower() not in {"direct", "indirect"}:
            continue
        like_count = _available_count(note, "like_count")
        comment_count = _available_count(note, "comment_count")
        share_count = _available_count(note, "share_count")
        engagement_score = (
            to_int(note.get("like_count"))
            + 2 * to_int(note.get("collect_count"))
            + 3 * to_int(note.get("comment_count"))
            + to_int(note.get("share_count"))
        )
        audience = audience_by_id.get(note_id, {})
        rows.append(
            {
                "note_id": note_id,
                "published_at": _readable_post_date(note.get("published_at") or note.get("published_at_raw")),
                "post_title": _as_text(note.get("title"), "") or "无标题",
                "author": _as_text(note.get("author_name"), "") or "-",
                "like_count": like_count,
                "comment_count": comment_count,
                "share_count": share_count,
                "sentiment": _as_text(annotation.get("sentiment"), "neutral"),
                "excerpt": _short_post_excerpt(note),
                "post_url": _public_post_url(note.get("post_url"), note_id),
                "engagement_score": engagement_score,
                "audience_reaction": _as_text(audience.get("audience_reaction"), ""),
                "recurring_signals": _as_dict_list(audience.get("recurring_signals"))[:2],
                "high_engagement_viewpoint": (
                    audience.get("high_engagement_viewpoint")
                    if isinstance(audience.get("high_engagement_viewpoint"), dict)
                    else None
                ),
            }
        )
    rows.sort(
        key=lambda row: (
            row.get("comment_count") if row.get("comment_count") is not None else -1,
            _as_int(row.get("engagement_score")),
        ),
        reverse=True,
    )
    for rank, row in enumerate(rows[:10], 1):
        row["rank"] = rank
    return rows[:10]


def _lookup_post_url(note_id: str, analysis_bundle: dict[str, Any]) -> str:
    lookup = analysis_bundle.get("_note_post_urls") if isinstance(analysis_bundle.get("_note_post_urls"), dict) else {}
    post_url = _as_text(lookup.get(note_id), "") if isinstance(lookup, dict) else ""
    if post_url:
        return _public_post_url(post_url, note_id)
    return f"https://www.xiaohongshu.com/explore/{note_id}" if note_id else ""
def _replace_note_ids_with_urls(text: str, analysis_bundle: dict[str, Any]) -> str:
    """把文本中的裸 note_id 换成可点的原帖链接。

    不能用 \\b 词边界：中文（如"帖子6a2a…"）和 hex 之间没有词边界，会漏匹配。
    用显式 lookaround 排除已在 URL/更长 hex 串中的片段。
    """

    if not text:
        return ""

    def replace(match: re.Match[str]) -> str:
        note_id = match.group(0)
        return _lookup_post_url(note_id, analysis_bundle)

    return re.sub(r"(?<![0-9a-zA-Z/])[0-9a-f]{24}(?![0-9a-zA-Z])", replace, text)


def _as_text(value: Any, default: str = "") -> str:
    """把报告文本字段收口成 string。"""

    if value is None or value == "":
        return str(default or "")
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _as_text_list(value: Any, default: Optional[list[Any]] = None) -> list[str]:
    """把 bullet 字段收口成 string list。"""

    source = value if value not in (None, "") else default
    if source is None or source == "":
        return []
    if isinstance(source, list):
        return [_as_text(item) for item in source if item not in (None, "")]
    return [_as_text(source)]


def _as_dict_list(value: Any) -> list[dict[str, Any]]:
    """把 schema 中的数组字段收口成 list[dict]。"""

    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _report_title(processing: dict[str, Any]) -> str:
    keyword = _as_text(processing.get("keyword"), "")
    mode = _scan_mode_from_processing(processing)
    if keyword and keyword != "broad_scan":
        return f"HKU 小红书洞察报告：{keyword}"
    if mode == "broad_scan":
        return "HKUBS 小红书宽口径洞察报告"
    return "HKU 小红书洞察报告"


def _exec_key_points(structured: dict[str, Any]) -> list[str]:
    """Exec Summary 下方的核心要点：复用已有 key_findings 标题，做跨主题速览，不再单列成段。"""

    points = []
    for row in structured.get("key_findings_across_themes") or []:
        if not isinstance(row, dict):
            continue
        finding = _as_text(row.get("finding"), "").strip()
        if finding:
            points.append(finding)
    return points[:4]


def _as_int(value: Any) -> int:
    try:
        return int(float(str(value).replace(",", "")))
    except (TypeError, ValueError):
        return 0
# 只匹配合法 URL 字符，停在中文/全角括号/引号/空格处；否则一条 evidence 里多个网址会被贪婪吞成一个坏链接。
_URL_PATTERN = r"https?://[A-Za-z0-9._~:/?#&=%+\-]+"


def _public_post_url(value: Any, note_id: str = "") -> str:
    """Use shareable post links in reports and strip provider/security query params."""

    url = _as_text(value, "").strip()
    match = re.search(r"xiaohongshu\.com/explore/([0-9a-fA-F]{24})", url)
    if match:
        return f"https://www.xiaohongshu.com/explore/{match.group(1)}"
    if url:
        return url
    if note_id:
        return f"https://www.xiaohongshu.com/explore/{note_id}"
    return url


def _linkify_urls(text: str, link_label: str = "打开原帖") -> str:
    raw = str(text or "")
    parts: list[str] = []
    cursor = 0
    for match in re.finditer(_URL_PATTERN, raw):
        parts.append(html.escape(raw[cursor:match.start()]))
        url = match.group(0).rstrip('",]）)')
        suffix = match.group(0)[len(url):]
        href = html.escape(_public_post_url(url), quote=True)
        parts.append(f'<a href="{href}" target="_blank" rel="noreferrer">{html.escape(link_label)}</a>')
        parts.append(html.escape(suffix))
        cursor = match.end()
    parts.append(html.escape(raw[cursor:]))
    return "".join(parts)
def _zh_theme(value: Any) -> str:
    mapping = {
        "Admissions": "招生与录取",
        "Course_Selection": "选课与课程容量",
        "Teaching_Quality": "教学质量",
        "Academic_Workload": "学业负担",
        "Programme_Experience": "项目体验",
        "Career_Outcomes": "就业结果",
        "Internships": "实习机会",
        "Student_Services": "学生服务",
        "Accommodation": "住宿与生活成本",
        "Campus_Life": "校园生活",
        "Scholarships": "奖学金与费用",
        "Reputation": "声誉与身份认同",
        "Other": "其他",
        "Commercial_Noise": "中介营销",
    }
    text = _as_text(value, "")
    return mapping.get(text, text or "-")


def _zh_content_type(value: Any) -> str:
    mapping = {
        "complaint": "投诉/不满",
        "concern": "担忧/焦虑",
        "question": "提问/求助",
        "information_sharing": "经验/信息分享",
        "positive_advocacy": "正面推荐",
        "other": "其他",
    }
    text = _as_text(value, "")
    return mapping.get(text, text or "-")


def _zh_sentiment(value: Any) -> str:
    mapping = {"positive": "正面", "neutral": "中性", "negative": "负面"}
    text = _as_text(value, "")
    return mapping.get(text, text or "-")
def _dominant_from_counts(value: Any) -> str:
    if not isinstance(value, dict) or not value:
        return ""
    return max(value.items(), key=lambda item: _as_int(item[1]))[0]


def _metric_pairs(processing: dict[str, Any], analysis_bundle: dict[str, Any]) -> list[tuple[str, Any]]:
    summary = analysis_bundle.get("annotation_summary") if isinstance(analysis_bundle.get("annotation_summary"), dict) else {}
    return [
        ("原始笔记", processing.get("raw_notes")),
        ("处理后笔记", processing.get("processed_notes")),
        ("原始评论", processing.get("raw_comments")),
        ("处理后评论", processing.get("processed_comments")),
        ("LLM 标注笔记", analysis_bundle.get("annotated_notes")),
        ("语义过滤后笔记", analysis_bundle.get("analysis_notes")),
        ("语义过滤后评论", analysis_bundle.get("analysis_comments")),
        ("情绪分布", _distribution_text_v3(summary.get("sentiment"), _zh_sentiment)),
        ("内容类型分布", _distribution_text_v3(summary.get("content_type"), _zh_content_type)),
    ]


def _search_query_from_processing(processing: dict[str, Any]) -> str:
    """报告头部的搜索词：Topic 用关键词，Broad 用关键词池，不依赖 LLM 输出。"""

    keyword = _as_text(processing.get("keyword"), "")
    if keyword and keyword != "broad_scan":
        return keyword
    pool = processing.get("keyword_pool") if isinstance(processing.get("keyword_pool"), list) else []
    keywords = [_as_text(item.get("keyword"), "") for item in pool if isinstance(item, dict)]
    return ", ".join(value for value in keywords if value)


def _scan_mode_from_processing(processing: dict[str, Any]) -> str:
    """从处理摘要里尽量判断本轮是 Broad Scan 还是 Topic Scan。"""

    config = processing.get("config") if isinstance(processing.get("config"), dict) else {}
    mode = processing.get("scan_mode") or config.get("scan_mode")
    if mode:
        return _as_text(mode)
    keyword = _as_text(processing.get("keyword") or config.get("keyword"), "")
    if keyword == "broad_scan":
        return "broad_scan"
    return "topic_scan"


def _log(logs: list[str], log_queue: Optional[Any], message: str) -> None:
    logs.append(timestamped(message))
    emit_log(log_queue, message)


# ---------------------------------------------------------------------------
# Current-schema monitoring report adapters.
def _monitoring_key_findings(value: Any, analysis_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """跨主题结论无法像 Alert 那样 1:1 绑定单个 signal，但必须能回查到真实 theme。

    grounding 只在"本轮确实存在至少两个 theme"时生效；theme 不足两个时跨主题结论
    本来就不可能成立，此时不做丢弃，保持历史行为。
    """

    theme_names = {
        _as_text(row.get("theme"), "")
        for row in _as_dict_list(analysis_bundle.get("theme_table"))
        if _as_text(row.get("theme"), "")
    }
    signal_ids = {
        _as_text(row.get("signal_id"), "")
        for row in _as_dict_list(analysis_bundle.get("signal_table"))
        if _as_text(row.get("signal_id"), "")
    }
    enforce_themes = len(theme_names) >= 2

    rows = []
    for row in _as_dict_list(value):
        finding = _as_text(row.get("finding") or row.get("narrative") or row.get("summary"), "")
        if not finding:
            continue
        supporting_themes = [item for item in _as_text_list(row.get("supporting_themes")) if item in theme_names]
        supporting_signal_ids = [item for item in _as_text_list(row.get("supporting_signal_ids")) if item in signal_ids]
        if enforce_themes and len(set(supporting_themes)) < 2:
            continue
        rows.append(
            {
                "finding": _replace_note_ids_with_urls(finding, analysis_bundle),
                "summary": _replace_note_ids_with_urls(_as_text(row.get("summary"), ""), analysis_bundle),
                "evidence": _replace_note_ids_with_urls(_as_text(row.get("evidence"), ""), analysis_bundle),
                "finding_type": _as_text(row.get("finding_type"), ""),
                "supporting_themes": sorted(set(supporting_themes)),
                "supporting_signal_ids": supporting_signal_ids,
            }
        )
    # Key Findings 只保留少量、由当前 LLM 输出且通过稳定 theme/signal ID 校验的结论。
    return rows[:4]


_ALERT_PRIORITY_RANK = {"high": 3, "medium": 2, "low": 1, "none": 0}


def _monitoring_alerts(analysis: dict[str, Any], analysis_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Alerts 以代码聚合的 alert_table 为准，LLM 只负责给已选中的 signal 写文字。

    signal / alert_level / alert_type 一律由代码覆盖：即使 LLM 返回了别的值也不采信。
    """

    alert_rows = _as_dict_list(analysis_bundle.get("alert_table"))
    alert_by_id = {
        _as_text(row.get("signal_id"), ""): row
        for row in alert_rows
        if _as_text(row.get("signal_id"), "")
    }
    out: list[dict[str, Any]] = []
    seen_signal_ids: set[str] = set()
    for row in _as_dict_list(analysis.get("alerts")):
        signal_id = _as_text(row.get("signal_id"), "")
        source = alert_by_id.get(signal_id)
        # 幻觉 signal_id 直接丢弃；同一个 signal_id 只保留第一条。
        if source is None or signal_id in seen_signal_ids:
            continue
        seen_signal_ids.add(signal_id)
        out.append(_alert_row_from_source(source, row, analysis_bundle))
    if not out:
        return _fallback_alerts_from_table(analysis_bundle)
    return _sort_alert_rows(out)


def _sort_alert_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """排序只用代码聚合结果，不依赖 LLM 的输出顺序。"""

    return sorted(
        rows,
        key=lambda row: (
            _ALERT_PRIORITY_RANK.get(_as_text(row.get("alert_level"), "none"), 0),
            _as_int(row.get("alert_evidence_count")),
            _as_int(row.get("alert_evidence_engagement_sum")),
        ),
        reverse=True,
    )


def _alert_row_from_source(
    source: dict[str, Any],
    llm_row: dict[str, Any],
    analysis_bundle: dict[str, Any],
) -> dict[str, Any]:
    signal = _as_text(source.get("signal"), "")
    summary = _replace_note_ids_with_urls(_as_text(llm_row.get("summary"), ""), analysis_bundle)
    return {
        "signal_id": _as_text(source.get("signal_id"), ""),
        # 代码字段：不接受 LLM 覆盖。
        "signal": signal,
        "alert_level": _as_text(source.get("alert_priority"), "low"),
        "alert_type": _as_text(source.get("alert_type"), "other"),
        "alert_triggers": [_as_text(item, "") for item in source.get("alert_triggers") or []],
        "alert_evidence_count": _as_int(source.get("alert_evidence_count")),
        "alert_evidence_engagement_sum": _as_int(source.get("alert_evidence_engagement_sum")),
        "mention_count": _as_int(source.get("mention_count")),
        "engagement_sum": _as_int(source.get("engagement_sum")),
        # LLM 字段：只负责解释文字。
        "summary": summary,
        "evidence": _replace_note_ids_with_urls(_as_text(llm_row.get("evidence"), ""), analysis_bundle)
        or _evidence_from_items(source.get("evidence_items")),
        "supporting_quotes": _monitoring_quotes(llm_row.get("supporting_quotes"), analysis_bundle)
        or _quotes_from_evidence_items(source.get("evidence_items")),
        **_comment_signal_fields_for_label(source, analysis_bundle, llm_row),
    }


def _quotes_from_evidence_items(value: Any) -> list[dict[str, Any]]:
    return [
        {
            "quote": _as_text(item.get("quote"), ""),
            "note_id": _as_text(item.get("note_id"), ""),
            "post_url": _public_post_url(item.get("post_url"), _as_text(item.get("note_id"), "")),
            "date": _as_text(item.get("date"), ""),
            "engagement": _as_int(item.get("engagement")),
        }
        for item in _as_dict_list(value)[:4]
        if _as_text(item.get("quote"), "")
    ]


def _fallback_alert_summary(source: dict[str, Any]) -> str:
    """纯代码事实句，不含任何后果推演。

    刻意不复用 annotation 的 risk_reason：那是标注阶段 LLM 写的解释性文字，
    经常带“可能影响决策/心理状态”这类推断，直接搬进 Alert 会绕过报告层的写作约束。
    这里只陈述代码已经数出来的东西，具体内容交给下面的原文引用承担。
    """

    evidence_count = _as_int(source.get("alert_evidence_count"))
    mention_count = _as_int(source.get("mention_count"))
    engagement = _as_int(source.get("alert_evidence_engagement_sum"))
    parts = [f"本轮共 {mention_count} 篇帖子提及该信号"]
    if evidence_count:
        parts.append(f"其中 {evidence_count} 篇构成负面或敏感证据")
    if engagement:
        parts.append(f"这些内容加权互动合计 {engagement}")
    return "；".join(parts) + "。具体内容见下方原文引用。"


def _fallback_alerts_from_table(analysis_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """LLM 没给 alerts、或全部 grounding 失败时，直接用 alert_table 生成保守 Alert。

    summary 只陈述代码统计出的事实，不做任何现实后果推测。
    """

    rows: list[dict[str, Any]] = []
    for source in _as_dict_list(analysis_bundle.get("alert_table")):
        signal = _as_text(source.get("signal"), "")
        if not signal:
            continue
        summary = _fallback_alert_summary(source)
        rows.append(
            {
                "signal_id": _as_text(source.get("signal_id"), ""),
                "signal": signal,
                "alert_level": _as_text(source.get("alert_priority"), "low"),
                "alert_type": _as_text(source.get("alert_type"), "other"),
                "alert_triggers": [_as_text(item, "") for item in source.get("alert_triggers") or []],
                "alert_evidence_count": _as_int(source.get("alert_evidence_count")),
                "alert_evidence_engagement_sum": _as_int(source.get("alert_evidence_engagement_sum")),
                "mention_count": _as_int(source.get("mention_count")),
                "engagement_sum": _as_int(source.get("engagement_sum")),
                "summary": summary,
                "evidence": _evidence_from_items(source.get("evidence_items")),
                "supporting_quotes": _quotes_from_evidence_items(source.get("evidence_items")),
                **_comment_signal_fields_for_label(source, analysis_bundle),
            }
        )
    return _sort_alert_rows(rows)


def _report_alert_rows(structured: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only current-schema Alert rows."""

    return _as_dict_list(structured.get("alerts"))


def _monitoring_positive_signals(value: Any, analysis_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Positive Signals 与 Alerts 使用同一套 grounding：候选由代码定，signal 名由代码覆盖。"""

    table = _as_dict_list(analysis_bundle.get("positive_signal_table"))
    source_by_id = {
        _as_text(row.get("signal_id"), ""): row
        for row in table
        if _as_text(row.get("signal_id"), "")
    }
    rows: list[dict[str, Any]] = []
    seen_signal_ids: set[str] = set()
    for row in _as_dict_list(value):
        signal_id = _as_text(row.get("signal_id"), "")
        source = source_by_id.get(signal_id)
        if source is None or signal_id in seen_signal_ids:
            continue
        seen_signal_ids.add(signal_id)
        signal = _as_text(source.get("signal"), "")
        rows.append(
            {
                "signal_id": signal_id,
                "signal": signal,
                "summary": _replace_note_ids_with_urls(_as_text(row.get("summary"), ""), analysis_bundle),
                "evidence": _replace_note_ids_with_urls(_as_text(row.get("evidence"), ""), analysis_bundle)
                or _evidence_from_items(source.get("evidence_items")),
                "positive_evidence_count": _as_int(source.get("positive_evidence_count")),
                "positive_evidence_engagement_sum": _as_int(source.get("positive_evidence_engagement_sum")),
                **_comment_signal_fields_for_label(source, analysis_bundle, row),
            }
        )
    if not rows:
        return _fallback_positive_signals_from_table(analysis_bundle)
    return rows


def _fallback_positive_signals_from_table(analysis_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """LLM 没给正面信号、或全部 grounding 失败时的保守版本，只复述已聚合的证据。"""

    rows: list[dict[str, Any]] = []
    for source in _as_dict_list(analysis_bundle.get("positive_signal_table")):
        signal = _as_text(source.get("signal"), "")
        if not signal:
            continue
        rows.append(
            {
                "signal_id": _as_text(source.get("signal_id"), ""),
                "signal": signal,
                "summary": "",
                "evidence": _evidence_from_items(source.get("evidence_items")),
                "positive_evidence_count": _as_int(source.get("positive_evidence_count")),
                "positive_evidence_engagement_sum": _as_int(source.get("positive_evidence_engagement_sum")),
                **_comment_signal_fields_for_label(source, analysis_bundle),
            }
        )
    return rows


def _comment_signal_fields_for_label(
    source: dict[str, Any],
    analysis_bundle: dict[str, Any],
    row: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """按 note_id 把评论聚合行挂到 signal 上；不做标题模糊匹配。"""

    metrics = _match_comment_metrics(_source_note_ids(source), analysis_bundle)
    signal = _as_text((row or {}).get("comment_signal") or (row or {}).get("audience_signal") or (row or {}).get("comment_summary"), "")
    signal = _replace_note_ids_with_urls(signal, analysis_bundle) if signal else _comment_signal_from_metrics(metrics)
    return {
        "comment_signal": signal,
        "comment_count": _as_int(metrics.get("comment_count")),
        "comment_question_count": _as_int(metrics.get("question_count")),
    }


def _source_note_ids(source: dict[str, Any]) -> set[str]:
    """一条 signal 关联的 note_id：证据条目 + 聚合表记录的 id。"""

    note_ids = {
        _as_text(item, "")
        for key in ("alert_evidence_note_ids", "positive_evidence_note_ids", "note_ids", "evidence_note_ids")
        for item in source.get(key) or []
        if _as_text(item, "")
    }
    note_ids.update(
        _as_text(item.get("note_id"), "")
        for item in _as_dict_list(source.get("evidence_items"))
        if _as_text(item.get("note_id"), "")
    )
    return note_ids


def _match_comment_metrics(note_ids: set[str], analysis_bundle: dict[str, Any]) -> dict[str, Any]:
    """只按 note_id 交集匹配评论聚合行。

    匹配不上就返回空，宁可不展示评论侧信号，也不用标题词重合去猜哪一行是同一件事。
    """

    if not note_ids:
        return {}
    for table in (
        analysis_bundle.get("narrative_comment_table") or [],
        analysis_bundle.get("comment_signal_table") or [],
    ):
        for row in _as_dict_list(table):
            row_note_ids = {_as_text(item, "") for item in row.get("note_ids") or [] if _as_text(item, "")}
            row_note_id = _as_text(row.get("note_id"), "")
            if row_note_id:
                row_note_ids.add(row_note_id)
            if row_note_ids & note_ids:
                return row
    return {}


def _monitoring_evidence(value: Any, analysis_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for row in _as_dict_list(value):
        evidence = _as_text(row.get("evidence") or row.get("quote"), "")
        note_id = _as_text(row.get("note_id"), "")
        source = _as_text(row.get("source"), "")
        source_note_id = re.sub(r"^(?:note_id|帖子|原帖)\s*[:：-]?\s*", "", source, flags=re.I).strip(" ：:-")
        if not note_id and re.fullmatch(r"[0-9a-fA-F]{24}", source_note_id):
            note_id = source_note_id
            source = "原帖"
        embedded_url = re.search(_URL_PATTERN, source) or re.search(_URL_PATTERN, evidence)
        post_url = _as_text(row.get("post_url") or (note_id and _lookup_post_url(note_id, analysis_bundle)), "")
        if not post_url and embedded_url:
            post_url = embedded_url.group(0).rstrip('",]）)')
        post_url = _public_post_url(post_url, note_id) if post_url else ""
        source = re.sub(_URL_PATTERN, "", source).replace("note_id:", "").strip(" ：:-")
        if source in {"signal_table", "narrative_table", "theme_table", "discussion_table"}:
            source = "分析汇总"
        evidence = re.sub(_URL_PATTERN, "", evidence).strip()
        rows.append(
            {
                "source": source or "原帖",
                "evidence": evidence,
                "note_id": note_id,
                "post_url": post_url,
            }
        )
    seen_note_ids = {_as_text(row.get("note_id"), "") for row in rows if row.get("note_id")}
    evidence_sources = [
        ("原帖", analysis_bundle.get("signal_table") or [], "evidence_items"),
        ("原帖", analysis_bundle.get("narrative_table") or [], "evidence_items"),
        ("原帖", analysis_bundle.get("uncertainty_table") or [], "evidence_items"),
    ]
    for source_name, table, item_key in evidence_sources:
        for row in table:
            if not isinstance(row, dict):
                continue
            for item in row.get(item_key) or []:
                note_id = _as_text(item.get("note_id"), "")
                post_url = _as_text(item.get("post_url") or _lookup_post_url(note_id, analysis_bundle), "")
                post_url = _public_post_url(post_url, note_id) if post_url else ""
                if not note_id or note_id in seen_note_ids or not post_url:
                    continue
                rows.append(
                    {
                        "source": "评论" if item.get("source") == "comment" else source_name,
                        "evidence": _as_text(item.get("quote"), ""),
                        "note_id": note_id,
                        "post_url": post_url,
                    }
                )
                seen_note_ids.add(note_id)
                if len(rows) >= 50:
                    return _dedupe_report_evidence(rows, limit=50)
    for row in analysis_bundle.get("narrative_comment_table") or []:
        if not isinstance(row, dict):
            continue
        for item in (row.get("question_comments") or []) + (row.get("top_comments") or []):
            note_id = _as_text(item.get("note_id"), "")
            post_url = _lookup_post_url(note_id, analysis_bundle)
            post_url = _public_post_url(post_url, note_id) if post_url else ""
            if not note_id or note_id in seen_note_ids or not post_url:
                continue
            rows.append(
                {
                    "source": "评论",
                    "evidence": _as_text(item.get("content"), ""),
                    "note_id": note_id,
                    "post_url": post_url,
                }
            )
            seen_note_ids.add(note_id)
            if len(rows) >= 50:
                return _dedupe_report_evidence(rows, limit=50)
    return _dedupe_report_evidence(rows, limit=50)


def _normalize_match_label(value: Any) -> str:
    """精确比较用的归一化：只抹平大小写和空白，不做语义判断。"""

    return re.sub(r"\s+", "", _as_text(value, "").lower())


def _dedupe_report_evidence(value: Any, limit: int = 10) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for row in _as_dict_list(value):
        evidence = " ".join(_as_text(row.get("evidence"), "").split())
        if len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", evidence)) < 2:
            continue
        note_id = _as_text(row.get("note_id"), "")
        post_url = _public_post_url(row.get("post_url"), note_id) if (row.get("post_url") or note_id) else ""
        key = (note_id or post_url, evidence)
        if key in seen:
            continue
        seen.add(key)
        current = dict(row)
        current["evidence"] = evidence
        current["post_url"] = post_url
        out.append(current)
        if len(out) >= limit:
            break
    return out


def _monitoring_limitations(
    value: Any,
    analysis_bundle: dict[str, Any],
    processing: Optional[dict[str, Any]] = None,
) -> list[str]:
    items = _as_text_list(value)
    if not any("不能代表全平台" in item for item in items):
        items.append("结论仅描述本轮公开搜索结果样本，不能代表全平台全部用户或总体意见。")
    coverage = analysis_bundle.get("time_coverage") if isinstance(analysis_bundle.get("time_coverage"), dict) else {}
    missing_dates = _as_int(coverage.get("notes_without_publish_date"))
    if missing_dates and not any("发布时间" in item for item in items):
        items.append(f"有 {missing_dates} 条纳入分析帖子缺少可解析发布时间，Recent 7D 与 Latest 等时间指标只覆盖有发布时间的帖子。")
    author_counts = ((analysis_bundle.get("annotation_summary") or {}).get("author_type") or {}) if isinstance(analysis_bundle.get("annotation_summary"), dict) else {}
    if _as_int(author_counts.get("agency_marketing")) or _as_int(author_counts.get("unclear")):
        items.append("小红书存在中介/营销内容伪装成真实经验的情况；本报告仅在有强证据时标记 agency_marketing，其余保守标为 unclear，不做硬过滤。")
    signal_counts = ((analysis_bundle.get("annotation_summary") or {}).get("signal_type") or {}) if isinstance(analysis_bundle.get("annotation_summary"), dict) else {}
    commercial_count = _as_int(signal_counts.get("commercial"))
    if commercial_count and not any("商业" in item or "营销" in item for item in items):
        items.append(f"代码标注中有 {commercial_count} 条 commercial signal，可能包含商业/中介引流内容；报告只把它作为解读限制，不硬过滤。")
    missing_narratives = [
        _as_text(row.get("narrative"), "")
        for row in _as_dict_list(analysis_bundle.get("_main_narratives"))
        if row.get("llm_summary_missing")
    ]
    if missing_narratives:
        items.append(
            f"有 {len(missing_narratives)} 条主要讨论未获得 AI 摘要（AI 输出不完整），"
            "这些讨论仍按代码聚合结果展示，摘要与证据由代码兜底生成。"
        )
    comment_status = _as_text((processing or {}).get("comment_collection_status"), "unknown")
    comment_messages = {
        "not_requested": "本轮未请求评论采集；评论数 0 不表示原帖没有评论。",
        "skipped_by_policy": "本轮评论采集已启用，但没有帖子满足采集策略。",
        "collection_failed": "本轮评论采集失败，无法判断原帖评论情况。",
        "partial": "本轮只采集到部分评论，评论信号不完整。",
        "unknown": "该历史 run 缺少评论采集状态，无法区分未采集与没有评论。",
    }
    # Broad 已改为「分析完成后只对 Top 10 抓热门评论第一页」，全局 comment_pages=0，
    # 因此 processing 里的状态一定是 not_requested。若直接沿用那句话，报告会一边写
    # 「未请求评论采集」，一边在监测概览和 Top 10 卡片里展示评论数与受众反应，自相矛盾。
    top10_comments = _as_int(
        (analysis_bundle.get("_top10_comment_collection") or {}).get("comments_collected")
        if isinstance(analysis_bundle.get("_top10_comment_collection"), dict)
        else 0
    )
    if top10_comments > 0:
        comment_messages["not_requested"] = (
            f"本轮只对 Top 10 原帖各抓取热门一级评论第一页（共 {top10_comments} 条），"
            "仅用于 Top 10 卡片的受众反应；其余帖子未采集评论，评论信号不覆盖全样本，"
            "也不参与重点关注与正面信号的聚合。"
        )
    if comment_status in comment_messages and not any("评论采集" in item for item in items):
        items.append(comment_messages[comment_status])
    return items or ["样本来自本轮公开内容样本，不能代表全平台完整讨论。"]
def _monitoring_quotes(value: Any, analysis_bundle: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    evidence_by_note: dict[str, dict[str, Any]] = {}
    for signal in analysis_bundle.get("signal_table") or []:
        if not isinstance(signal, dict):
            continue
        for item in signal.get("evidence_items") or []:
            if isinstance(item, dict) and item.get("note_id"):
                evidence_by_note.setdefault(_as_text(item.get("note_id"), ""), item)
    for row in _as_dict_list(value):
        note_id = _as_text(row.get("note_id"), "")
        evidence = evidence_by_note.get(note_id, {})
        post_url = _as_text(row.get("post_url") or evidence.get("post_url") or (note_id and _lookup_post_url(note_id, analysis_bundle)), "")
        post_url = _public_post_url(post_url, note_id) if post_url else ""
        rows.append(
            {
                "quote": _as_text(row.get("quote") or evidence.get("quote"), "")[:120],
                "note_id": note_id,
                "post_url": post_url,
                "date": _as_text(row.get("date") or evidence.get("date"), ""),
                "engagement": _as_int(row.get("engagement") or evidence.get("engagement")),
            }
        )
    return rows[:4]


def _html_section(title: str, body: str) -> str:
    return f"<section><h2>{_esc(title)}</h2>{body}</section>"
def _evidence_from_items(value: Any) -> str:
    rows = _as_dict_list(value)
    if not rows:
        return ""
    return "；".join(_as_text(row.get("quote"), "") for row in rows[:2] if row.get("quote"))
def _as_risk(value: Any) -> str:
    text = _as_text(value, "none").strip().lower()
    return text if text in {"high", "medium", "low", "none"} else "none"


def _esc(value: Any) -> str:
    return html.escape(_as_text(value, ""))


# Monitoring renderer v3: Topic and Broad have intentionally different report logic.


def _ensure_report_defaults(
    payload: dict[str, Any],
    analysis: dict[str, Any],
    processing: dict[str, Any],
    analysis_bundle: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    analysis_bundle = analysis_bundle or {}
    is_topic = _report_mode(payload, analysis_bundle, processing) == "topic_report"
    appendix = analysis.get("appendix") if isinstance(analysis.get("appendix"), dict) else {}
    earliest_post_date, latest_post_date = _report_time_range(analysis_bundle)
    alerts = [] if is_topic else _monitoring_alerts(analysis, analysis_bundle)
    # 主要讨论要先算出来，data_limitations 才能如实说明"有几条没拿到 AI 摘要"。
    narratives = (
        _topic_narratives_v3(analysis.get("main_narratives") or analysis.get("topic_clusters"), analysis_bundle)
        if is_topic
        else []
    )
    analysis_bundle["_main_narratives"] = narratives
    evidence = [] if is_topic else _monitoring_evidence(
        analysis.get("evidence_highlights") or appendix.get("supporting_evidence"),
        analysis_bundle,
    )
    base = {
        "title": _report_title(processing),
        "report_mode": "topic_report" if is_topic else "broad_report",
        "generated_scope": {
            "search_query": _search_query_from_processing(processing),
            "analysis_notes": _as_int(analysis_bundle.get("analysis_notes")),
            "analysis_comments": _as_int(analysis_bundle.get("analysis_comments")),
            "input_notes": _as_int(analysis_bundle.get("input_notes")),
            "input_comments": _as_int(analysis_bundle.get("input_comments")),
            "earliest_post_date": earliest_post_date,
            "latest_post_date": latest_post_date,
            "comment_collection_status": _as_text(processing.get("comment_collection_status"), "unknown"),
            "top10_comments": _as_int(
                (analysis_bundle.get("_top10_comment_collection") or {}).get("comments_collected")
                if isinstance(analysis_bundle.get("_top10_comment_collection"), dict)
                else 0
            ),
            "engagement_formula": "点赞 + 2×收藏 + 3×评论 + 分享",
        },
        "header_distributions": {
            "sentiment": _summary_counts_v3(analysis_bundle, "sentiment"),
            "content_type": _summary_counts_v3(analysis_bundle, "content_type"),
            "author_type": _summary_counts_v3(analysis_bundle, "author_type"),
        },
        "executive_summary": _replace_note_ids_with_urls(_as_text(analysis.get("executive_summary"), ""), analysis_bundle),
        "appendix": {
            "evidence": evidence,
            "methodology": _deterministic_methodology(processing, analysis_bundle),
        },
        "data_limitations": _monitoring_limitations(analysis.get("data_limitations"), analysis_bundle, processing),
    }
    if is_topic:
        base.update(
            {
                "main_narratives": narratives,
                "other_signals": _topic_single_post_observations(
                    analysis_bundle,
                    analysis.get("main_narratives") or analysis.get("topic_clusters"),
                    narratives,
                ),
                "questions_uncertainties": _uncertainty_rows_v3(analysis.get("questions_uncertainties") or analysis.get("key_information_needs"), analysis_bundle),
            }
        )
        base["appendix"]["evidence"] = _build_topic_evidence_registry(
            analysis_bundle,
            base["main_narratives"] + base["other_signals"],
            base["questions_uncertainties"],
        )
        base["executive_summary"] = _topic_factual_summary(base, analysis_bundle)
    else:
        base["alerts"] = alerts
        base.update(
            {
                "top_original_posts": _as_dict_list(analysis_bundle.get("_broad_top_posts"))[:10],
                "competitor_weekly": _as_dict_list(
                    (analysis_bundle.get("_competitor_weekly") or {}).get("schools")
                    if isinstance(analysis_bundle.get("_competitor_weekly"), dict)
                    else []
                ),
                "theme_landscape": _theme_rows_v3(analysis.get("theme_landscape") or analysis.get("theme_snapshot"), analysis_bundle),
                "key_findings_across_themes": _monitoring_key_findings(
                    analysis.get("key_findings_across_themes") or analysis.get("key_findings") or analysis.get("key_business_narratives"),
                    analysis_bundle,
                ),
                "positive_reputation_signals": _monitoring_positive_signals(
                    analysis.get("positive_reputation_signals") or analysis.get("positive_signals"),
                    analysis_bundle,
                ),
            }
        )
    return base


def _deterministic_methodology(processing: dict[str, Any], bundle: dict[str, Any]) -> list[str]:
    if _scan_mode_from_processing(processing) == "broad_scan":
        search_scope = _search_query_from_processing(processing) or "本轮 Broad 默认关键词池"
        config = processing.get("config") if isinstance(processing.get("config"), dict) else processing
        time_filter = _as_text(config.get("time_filter") or processing.get("time_filter"), "一周内")
        note_type = _as_text(config.get("note_type") or processing.get("note_type"), "普通笔记")
        return [
            f"数据来源为本轮小红书公开搜索结果；Broad 搜索范围：{search_scope}。",
            f"HKUBS 主样本按固定关键词页数采集，发布时间“{time_filter}”、笔记类型“{note_type}”；随后按 run 起始时刻执行精确 {REPORTING_WINDOW_DAYS}×24 小时过滤，窗口外帖子不进入 Broad 候选池；缺发布时间的帖子保留并单独计数，跨关键词按 note_id 去重。",
            "代码负责字段标准化、去重、HKU Business School 范围相关性筛选及统计聚合；AI 只对纳入范围的帖子进行主题、内容类型、情绪、信号与风险语义标注。",
            "Top Original Posts 只从本轮相关帖子中选择，先按评论数降序，再按加权互动量降序；不足 10 条时展示实际数量。",
            "Top 10 确定后，每篇仅请求热门一级评论第一页；评论按帖子组成 bundle 各调用一次 AI，只用于对应 Top 10 卡片，不进入全局 Risk / Positive Signal 聚合。",
            f"竞对周榜固定监测 7 家院校、每家 1 个关键词和 2 页；精确 {REPORTING_WINDOW_DAYS}×24 小时过滤后按 note_id 去重，以同一互动量公式由代码选每家 Top 5，不抓评论正文。AI 每校只读取最终 Top 5，标注单帖情绪并写一句 Top Posts 概括；Overall Sentiment 由代码按 3/5 规则聚合。",
            "加权互动量口径：点赞 + 2×收藏 + 3×评论 + 分享。",
            f"本轮最终纳入 {_as_int(bundle.get('analysis_notes'))} 条 HKUBS 相关帖子；全局分析评论数为 {_as_int(bundle.get('analysis_comments'))}，Top 10 评论独立计数。",
        ]
    return [
        "样本来自本轮小红书公开搜索结果，不代表全平台完整讨论。",
        "分析先进行 HKU 与搜索主题相关性筛选，只对相关帖子做逐帖标注。",
        "主要讨论至少需要 2 条独立帖子支持；单帖不进入正文主要讨论。",
        f"“其他高关注信号”只收录主题直接相关、且互动量高于本轮直接相关帖中位数或含具体政策/数字信息的单帖，最多 {MAX_SINGLETON_SIGNALS} 条。",
        "信息不确定点允许单一来源，但会显式标注“单一来源、未交叉验证”。",
        "帖子数、评论数、日期和互动量由代码聚合；文本归纳只使用本轮帖子与评论。",
        "加权互动量口径：点赞 + 2×收藏 + 3×评论 + 分享。",
        f"本轮纳入 {_as_int(bundle.get('analysis_notes'))} 条相关帖子、{_as_int(bundle.get('analysis_comments'))} 条相关评论。",
    ]


def _report_time_range(bundle: dict[str, Any]) -> tuple[str, str]:
    coverage = bundle.get("time_coverage") if isinstance(bundle.get("time_coverage"), dict) else {}
    earliest = _as_text(coverage.get("earliest_post_date"), "")
    latest = _as_text(coverage.get("latest_post_date"), "")
    if earliest and latest:
        return earliest, latest
    rows = []
    for key in ("narrative_table", "discussion_table", "theme_table", "signal_table"):
        rows.extend(_as_dict_list(bundle.get(key)))
    earliest_values = sorted(_as_text(row.get("earliest_post_date"), "") for row in rows if row.get("earliest_post_date"))
    latest_values = sorted(_as_text(row.get("latest_post_date"), "") for row in rows if row.get("latest_post_date"))
    return earliest or (earliest_values[0] if earliest_values else ""), latest or (latest_values[-1] if latest_values else "")


def _summary_counts_v3(bundle: dict[str, Any], key: str) -> dict[str, int]:
    summary_source = bundle.get("annotation_summary_relevant") or bundle.get("annotation_summary")
    summary = summary_source if isinstance(summary_source, dict) else {}
    counts = summary.get(key) if isinstance(summary.get(key), dict) else {}
    return {str(label): _as_int(value) for label, value in counts.items() if label not in {"", "unknown"}}


def _distribution_text_v3(counts: Any, labeler: Any) -> str:
    rows = counts if isinstance(counts, dict) else {}
    total = sum(_as_int(value) for value in rows.values())
    if not total:
        return "-"
    return " / ".join(
        f"{labeler(label)} {_as_int(value)} ({_as_int(value) / total:.0%})"
        for label, value in sorted(rows.items(), key=lambda item: _as_int(item[1]), reverse=True)
    )


MAX_SINGLETON_SIGNALS = 3


def _median(values: list[int]) -> float:
    ordered = sorted(values)
    if not ordered:
        return 0.0
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2


def _direct_post_engagements(bundle: dict[str, Any]) -> list[int]:
    """单帖互动量的比较基准：本轮所有 direct 相关帖子（按 note 去重）。

    刻意不使用 narrative_table 的 engagement_sum，因为多帖叙事的 sum 与单帖互动量不同量纲；
    也刻意不使用全部采集帖子，因为无关的泛港校内容互动量通常远高于窄主题内容。
    """

    seen: set[str] = set()
    out: list[int] = []
    for metrics in _as_dict_list(bundle.get("narrative_table") or bundle.get("discussion_table")):
        counts = metrics.get("topic_relevance_counts") if isinstance(metrics.get("topic_relevance_counts"), dict) else {}
        if _as_int(counts.get("direct")) <= 0:
            continue
        for item in _as_dict_list(metrics.get("evidence_items")):
            note_id = _as_text(item.get("note_id"), "")
            if note_id:
                if note_id in seen:
                    continue
                seen.add(note_id)
            out.append(_as_int(item.get("engagement")))
    return out


def _is_direct_only_row(metrics: dict[str, Any]) -> bool:
    counts = metrics.get("topic_relevance_counts") if isinstance(metrics.get("topic_relevance_counts"), dict) else {}
    if not counts:
        return False
    if _as_int(counts.get("direct")) <= 0:
        return False
    return not any(
        _as_int(value) > 0
        for key, value in counts.items()
        if _as_text(key, "") not in {"direct", ""}
    )


MAX_MAIN_NARRATIVES = 6


def _required_narrative_clusters(bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministically decide which clusters must appear as main narratives.

    资格判定完全由代码聚合结果决定（多帖支持 + direct 相关 + 有可用证据），
    与 LLM 是否提到它无关；LLM 只负责给这些 cluster 写摘要和挑证据。
    """

    eligible: list[dict[str, Any]] = []
    for metrics in _as_dict_list(bundle.get("narrative_table")):
        if not _as_text(metrics.get("cluster_id"), ""):
            continue
        if _as_int(metrics.get("volume")) < 2:
            continue
        relevance_counts = metrics.get("topic_relevance_counts") if isinstance(metrics.get("topic_relevance_counts"), dict) else {}
        if relevance_counts and _as_int(relevance_counts.get("direct")) == 0:
            continue
        if not _clean_report_evidence_items(metrics.get("evidence_items"), limit=1):
            continue
        eligible.append(metrics)
    eligible.sort(key=lambda row: (_as_int(row.get("engagement_sum")), _as_int(row.get("volume"))), reverse=True)
    return eligible[:MAX_MAIN_NARRATIVES]


def _topic_narratives_v3(value: Any, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Render every required cluster; the LLM supplies wording and evidence IDs.

    LLM 漏掉某个 cluster 不会让它从报告里消失 —— 那属于 LLM 输出不完整，
    对应 cluster 仍然渲染（用确定性兜底摘要与证据）并标记 llm_summary_missing，
    由 data_limitations 如实说明。未知 cluster_id 忽略，重复只取第一条。
    """

    comments = _as_dict_list(bundle.get("narrative_comment_table"))
    comments_by_id = {_as_text(row.get("cluster_id"), ""): row for row in comments if row.get("cluster_id")}
    llm_by_id: dict[str, dict[str, Any]] = {}
    for llm_row in _as_dict_list(value):
        cluster_id = _as_text(llm_row.get("cluster_id"), "")
        if cluster_id and cluster_id not in llm_by_id:
            llm_by_id[cluster_id] = llm_row

    out: list[dict[str, Any]] = []
    for metrics in _required_narrative_clusters(bundle):
        cluster_id = _as_text(metrics.get("cluster_id"), "")
        llm_row = llm_by_id.get(cluster_id) or {}
        label = _as_text(metrics.get("label"), "")
        comment_metrics = comments_by_id.get(cluster_id) or {}
        summary, evidence_items = _topic_cluster_content(metrics, llm_row)
        if not evidence_items:
            continue
        stance = _as_text(llm_row.get("stance"), "") or _dominant_from_counts(metrics.get("narrative_stance_counts"))
        out.append(
            {
                "cluster_id": cluster_id,
                "narrative": label,
                "stance": stance or "neutral",
                "summary": summary,
                "llm_summary_missing": not llm_row,
                "comment_signal": _as_text(llm_row.get("comment_signal"), "") or _comment_signal_from_metrics(comment_metrics),
                "evidence": "",
                "evidence_items": evidence_items,
                "note_ids": sorted({_as_text(item, "") for item in metrics.get("note_ids") or [] if _as_text(item, "")}),
                "volume": _as_int(metrics.get("volume")),
                "engagement_sum": _as_int(metrics.get("engagement_sum")),
                "top_post_share": float(metrics.get("top_post_share") or 0),
                "comment_count": _as_int(comment_metrics.get("comment_count")),
                "comment_question_count": _as_int(comment_metrics.get("question_count")),
            }
        )
    # 顺序与数量都已在 _required_narrative_clusters 里确定，这里不再重排。
    return out


def _topic_single_post_observations(
    bundle: dict[str, Any],
    value: Any = None,
    narratives: Optional[list[dict[str, Any]]] = None,
    limit: int = MAX_SINGLETON_SIGNALS,
) -> list[dict[str, Any]]:
    """Render direct singleton signals selected by LLM or deterministic engagement.

    Semantic report-worthiness is supplied by analysis. The renderer only checks
    current stable IDs, direct relevance, grounded evidence and the existing
    within-run engagement threshold.
    """

    covered_ids = {
        _as_text(row.get("cluster_id"), "")
        for row in (narratives or [])
        if _as_text(row.get("cluster_id"), "")
    }
    llm_by_id = {
        _as_text(row.get("cluster_id"), ""): row
        for row in _as_dict_list(value)
        if _as_text(row.get("cluster_id"), "")
    }
    engagement_threshold = _median(_direct_post_engagements(bundle))

    candidates: list[dict[str, Any]] = []
    for metrics in _as_dict_list(bundle.get("narrative_table")):
        if _as_int(metrics.get("volume")) != 1:
            continue
        if not _is_direct_only_row(metrics):
            continue
        cluster_id = _report_cluster_id(metrics)
        if not cluster_id or cluster_id in covered_ids:
            continue
        llm_row = llm_by_id.get(cluster_id, {})
        evidence_items = _selected_evidence_items(metrics, llm_row, fallback_limit=1)
        if not evidence_items:
            continue
        engagement = _as_int(metrics.get("engagement_sum"))
        report_worthy = llm_row.get("report_worthy") is True
        high_engagement = engagement > 0 and engagement >= engagement_threshold
        if not report_worthy and not high_engagement:
            continue
        candidates.append(
            {
                "cluster_id": cluster_id,
                "narrative": _as_text(metrics.get("label"), ""),
                "stance": _as_text(llm_row.get("stance"), "")
                or _dominant_from_counts(metrics.get("narrative_stance_counts"))
                or "neutral",
                "summary": _as_text(llm_row.get("summary"), "").strip(),
                "importance_reason": _as_text(llm_row.get("importance_reason"), "").strip(),
                "volume": 1,
                "engagement_sum": engagement,
                "report_worthy": report_worthy,
                "qualified_by": "analysis" if report_worthy else "engagement",
                "latest_post_date": _as_text(metrics.get("latest_post_date"), ""),
                "evidence": _evidence_text_with_links(evidence_items),
                "evidence_items": evidence_items,
            }
        )

    candidates.sort(
        key=lambda item: (
            item["report_worthy"],
            item["engagement_sum"],
            item["latest_post_date"],
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    seen_text: set[tuple[str, str]] = set()
    for row in candidates:
        exact_key = (
            _normalize_match_label(row.get("narrative")),
            _normalize_match_label(row.get("summary")),
        )
        if exact_key in seen_text:
            continue
        seen_text.add(exact_key)
        selected.append(row)
        if len(selected) >= limit:
            break
    return selected


def _topic_cluster_content(
    metrics: dict[str, Any],
    llm_row: dict[str, Any],
) -> tuple[str, list[dict[str, Any]]]:
    """Use the LLM summary and its exact evidence IDs for one current cluster."""

    label = _as_text(metrics.get("label"), "")
    # 摘要直接用本轮 LLM 的原话，报告层不做任何语义改写。
    summary = _as_text(llm_row.get("summary"), "").strip() or f"帖子围绕{label}展开讨论。"
    selected_items = _selected_evidence_items(metrics, llm_row, fallback_limit=3)
    return summary, selected_items


def _selected_evidence_items(
    metrics: dict[str, Any],
    llm_row: dict[str, Any],
    *,
    fallback_limit: int,
) -> list[dict[str, Any]]:
    """Validate LLM-selected evidence IDs, then use a simple engagement fallback."""

    evidence_pool = _clean_report_evidence_items(metrics.get("evidence_items"), limit=100)
    by_id = {
        _as_text(item.get("evidence_id"), ""): item
        for item in evidence_pool
        if _as_text(item.get("evidence_id"), "")
    }
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for evidence_id in _as_text_list(llm_row.get("evidence_ids")):
        item = by_id.get(evidence_id)
        if item is None or evidence_id in seen:
            continue
        seen.add(evidence_id)
        selected.append(item)
        if len(selected) >= 4:
            break
    if selected:
        return selected
    return evidence_pool[:max(1, min(4, fallback_limit))]


def _report_cluster_id(row: dict[str, Any]) -> str:
    return _as_text(row.get("cluster_id"), "")


def _clean_report_evidence_items(value: Any, limit: int = 5) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for item in _as_dict_list(value):
        quote = " ".join(_as_text(item.get("quote"), "").split()).strip("“”\"")
        if len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", quote)) < 2:
            continue
        note_id = _as_text(item.get("note_id"), "")
        evidence_id = _as_text(item.get("evidence_id"), "")
        if not evidence_id or not note_id:
            continue
        key = (note_id, quote)
        if key in seen:
            continue
        seen.add(key)
        current = dict(item)
        current["quote"] = quote
        current["evidence_id"] = evidence_id
        rows.append(current)
    rows.sort(key=lambda item: (_as_int(item.get("engagement")), len(_as_text(item.get("quote"), ""))), reverse=True)
    return rows[:limit]


def _evidence_text_with_links(items: list[dict[str, Any]]) -> str:
    parts = []
    for item in items:
        quote = _as_text(item.get("quote"), "")
        url = _public_post_url(item.get("post_url"), _as_text(item.get("note_id"), ""))
        link = f"（{url}）" if url else ""
        parts.append(f"“{quote}”{link}")
    return "；".join(parts)


def _comment_signal_from_metrics(metrics: dict[str, Any]) -> str:
    count = _as_int(metrics.get("comment_count"))
    if not count:
        return ""
    question_count = _as_int(metrics.get("question_count"))
    comments = [
        _as_text(item.get("content"), "")
        for item in _as_dict_list(metrics.get("top_comments"))
        if _as_text(item.get("content"), "")
    ][:3]
    question_part = f"其中 {question_count} 条为追问型评论" if question_count else "未形成明显追问集中"
    if comments:
        return f"该叙事下收集到 {count} 条一级评论，{question_part}；代表评论包括：“{'”；“'.join(comments)}”。"
    return f"该叙事下收集到 {count} 条一级评论，{question_part}。"


def _uncertainty_rows_v3(value: Any, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    """Render only grounded uncertainties selected by the current analysis."""

    table_by_id = {
        _as_text(row.get("uncertainty_id"), ""): row
        for row in _as_dict_list(bundle.get("uncertainty_table"))
        if _as_text(row.get("uncertainty_id"), "")
    }
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for llm_row in _as_dict_list(value):
        uncertainty_id = _as_text(llm_row.get("uncertainty_id"), "")
        metrics = table_by_id.get(uncertainty_id)
        if metrics is None or uncertainty_id in seen:
            continue
        seen.add(uncertainty_id)
        count = _as_int(metrics.get("support_count") or metrics.get("independent_source_count") or metrics.get("count"))
        if count < 1:
            continue
        question = _as_text(metrics.get("title") or metrics.get("question"), "")
        if not question:
            question = f"{_as_text(metrics.get('uncertainty_type'), '其他')}相关待确认信息"
        evidence_items = _selected_evidence_items(metrics, llm_row, fallback_limit=min(3, count))
        if not evidence_items:
            continue
        summary = _as_text(llm_row.get("summary"), "").strip()
        if _normalize_match_label(summary) == _normalize_match_label(question):
            summary = ""
        out.append(
            {
                "uncertainty_id": uncertainty_id,
                "question": question,
                "uncertainty_type": _as_text(metrics.get("uncertainty_type"), ""),
                "summary": summary,
                "evidence": "",
                "evidence_items": evidence_items,
                "supporting_post_ids": sorted({_as_text(item, "") for item in metrics.get("supporting_post_ids") or metrics.get("note_ids") or [] if _as_text(item, "")}),
                "support_count": count,
                "count": count,
                "is_single_source": count < 2,
                "engagement_sum": _as_int(metrics.get("engagement_sum")),
            }
        )
    out.sort(key=lambda item: (item["count"], item["engagement_sum"]), reverse=True)
    return out[:5]


def _build_topic_evidence_registry(
    bundle: dict[str, Any],
    narratives: list[dict[str, Any]],
    uncertainties: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Build the source registry with the stable evidence IDs from analysis."""

    ordered: list[tuple[dict[str, Any], str]] = []
    for row in narratives:
        used_by = _as_text(row.get("cluster_id"), "")
        ordered.extend((item, used_by) for item in _as_dict_list(row.get("evidence_items")))
    for row in uncertainties:
        used_by = _as_text(row.get("uncertainty_id"), "")
        ordered.extend((item, used_by) for item in _as_dict_list(row.get("evidence_items")))
    registry: dict[str, dict[str, Any]] = {}
    for item, used_by in ordered:
        evidence_id = _as_text(item.get("evidence_id"), "")
        note_id = _as_text(item.get("note_id"), "")
        post_url = _public_post_url(item.get("post_url"), note_id) if (item.get("post_url") or note_id) else ""
        quote = " ".join(_as_text(item.get("quote"), "").split())
        if not evidence_id or not note_id or len(re.findall(r"[0-9A-Za-z\u4e00-\u9fff]", quote)) < 2:
            continue
        current = registry.get(evidence_id)
        if current is None:
            hint = _as_text(
                item.get("signal_label")
                or item.get("primary_narrative")
                or item.get("post_title"),
                "",
            ).strip()
            if not hint:
                hint = "相关原帖"
            current = {
                "evidence_id": evidence_id,
                # source 是读者可见的短编号，稍后按登记顺序确定性分配（E01、E02…）。
                "source": "",
                "note_id": note_id,
                "hint": _short_source_hint(hint),
                "post_url": post_url,
                "date": _as_text(item.get("date"), ""),
                "used_by": [],
            }
            registry[evidence_id] = current
        if used_by and used_by not in current["used_by"]:
            current["used_by"].append(used_by)

    rows = list(registry.values())
    # 稳定 evidence_id 负责 grounding；读者看到的是按登记顺序的短编号。
    label_by_id = {}
    for index, row in enumerate(rows, 1):
        label = f"E{index:02d}"
        row["source"] = label
        label_by_id[_as_text(row.get("evidence_id"), "")] = label

    for row in narratives + uncertainties:
        refs: list[str] = []
        labels: list[str] = []
        quotes: list[dict[str, str]] = []
        for item in _as_dict_list(row.get("evidence_items")):
            evidence_id = _as_text(item.get("evidence_id"), "")
            if evidence_id in registry and evidence_id not in refs:
                refs.append(evidence_id)
                labels.append(label_by_id[evidence_id])
                quotes.append(
                    {
                        "evidence_id": evidence_id,
                        "source": label_by_id[evidence_id],
                        "quote": _as_text(item.get("quote"), ""),
                    }
                )
        row["evidence_ids"] = refs
        row["representative_quotes"] = quotes
        row["evidence"] = " · ".join(labels)
    return rows


def _short_source_hint(value: Any, limit: int = 36) -> str:
    text = " ".join(_as_text(value, "").split()).strip(" ，。；：")
    if not text:
        return "相关原帖"
    return text if len(text) <= limit else f"{text[:limit].rstrip()}…"


def _topic_factual_summary(structured: dict[str, Any], bundle: dict[str, Any]) -> str:
    count = _as_int(bundle.get("analysis_notes"))
    rows = _as_dict_list(structured.get("main_narratives"))
    singles = _as_dict_list(structured.get("other_signals"))
    uncertainties = _as_dict_list(structured.get("questions_uncertainties"))
    if not rows:
        text = f"本轮纳入 {count} 条相关帖子，未形成至少由 2 条独立帖子支持的主要讨论。"
        if singles:
            text += f"另有 {len(singles)} 条单帖信号信息价值较高，见“其他高关注信号”。"
        return text
    lead = rows[0]
    # 摘要只讲整体格局，具体内容留给叙事卡片，避免同一句话在两个区块重复出现。
    sentences = [
        f"本轮纳入 {count} 条相关帖子，形成 {len(rows)} 个由多帖支持的主要讨论，其中互动量最高的是{lead.get('narrative')}。"
    ]
    secondary = next(
        (
            row for row in rows[1:]
            if row.get("stance") != lead.get("stance")
            and _normalize_match_label(row.get("narrative"))
            not in _normalize_match_label(lead.get("narrative"))
        ),
        None,
    )
    if secondary is not None:
        sentences.append(f"另一条主要讨论集中在{secondary.get('narrative')}。")
    if singles:
        sentences.append(f"另有 {len(singles)} 条仅单帖提及、但信息价值较高的信号。")
    if uncertainties:
        first = uncertainties[0]
        scope = "目前仅 1 条相关帖子提出，样本内没有其他讨论可交叉验证" if first.get("is_single_source") else "多条帖子共同缺少明确信息"
        sentences.append(f"关于{first.get('question')}，{scope}。")
    return "".join(sentences[:4])


def _theme_rows_v3(value: Any, bundle: dict[str, Any]) -> list[dict[str, Any]]:
    table = bundle.get("theme_table") or []
    by_theme = {_as_text(row.get("theme"), ""): row for row in table if isinstance(row, dict)}
    out = []
    for row in _as_dict_list(value):
        theme = _as_text(row.get("theme"), "")
        if not theme:
            continue
        metrics = by_theme.get(theme, {})
        out.append({"theme": theme, "summary": _as_text(row.get("summary"), ""), "evidence": _replace_note_ids_with_urls(_as_text(row.get("evidence"), ""), bundle), "volume": _as_int(metrics.get("volume")), "engagement_sum": _as_int(metrics.get("engagement_sum"))})
    if out:
        return out
    return [{"theme": row.get("theme"), "summary": "", "evidence": "；".join(row.get("evidence_quotes") or [])[:300], "volume": _as_int(row.get("volume")), "engagement_sum": _as_int(row.get("engagement_sum"))} for row in table if isinstance(row, dict)]


# content_type_sentiment_summary 已从 Broad 输出 schema 移除：它从未被 Markdown/HTML 渲染，
# 顶部的情绪与内容类型分布一直由代码统计（header_distributions）负责。
# 旧 analysis.json 里若仍带这个 key，直接忽略即可，不需要迁移历史文件。


# Locale-aware renderer. Chinese is the canonical/default report; English uses a translated
# structured JSON plus the same deterministic metrics and layout.
_REPORT_TEXT = {
    "zh": {
        "report_time": "报告时间", "keyword": "关键词", "posts": "相关帖子数", "comments": "相关评论数",
        "sentiment": "情绪分布", "content_type": "内容类型分布", "author_type": "作者类型",
        "executive_summary": "执行摘要", "key_points": "核心要点", "main_narratives": "主要讨论（按互动量排序）",
        "questions": "信息不确定点", "theme_landscape": "话题分布", "content_sentiment": "内容类型与情绪概览",
        "alerts": "重点关注", "positive_signals": "正面与声誉信号", "appendix": "附录：来源与数据说明", "broad_appendix": "附录",
        "alert_level": "关注级别", "alert_type": "信号类别",
        "monitoring_overview": "监测概览", "top_posts": "Top 10 原帖", "published_date": "发布时间", "post_title": "帖子标题", "author": "作者",
        "top10_comments": "Top 10 评论",
        "likes": "点赞", "post_comments": "评论", "shares": "分享", "excerpt": "内容摘录", "no_top_posts": "本轮没有可展示的相关原帖。",
        "evidence": "代表性证据", "source_index": "来源索引", "more_sources": "更多来源", "limitations": "数据限制", "method": "方法说明", "overview": "数据概览",
        "source": "来源", "link": "原帖", "open_post": "打开原帖", "volume": "帖子数", "engagement": "互动量",
        "post_signal": "讨论概括", "comment_signal": "评论侧信号",
        "audience_reaction": "受众反应", "audience_details": "评论区反馈详情", "recurring_concerns": "重复关注 / 提问",
        "high_engagement_viewpoint": "高互动观点", "competitor_weekly": "竞对本周 Top 5",
        "overall_sentiment": "总体情绪", "weekly_takeaway": "本周 Top Posts 概括",
        "risk": "风险等级", "risk_type": "风险类型", "why": "重要性", "negative": "负面集中",
        "positive": "正面集中", "question_focus": "问题集中", "summary": "概述", "theme": "主题",
        "empty": "暂无足够证据。", "empty_narratives": "暂无足够证据形成主要讨论叙事。",
        "empty_alerts": "本轮没有达到关注门槛的负面或敏感讨论信号。", "empty_summary": "本轮样本暂无足够证据形成摘要。",
        "metric": "指标", "value": "数值",
        "date_range": "帖子覆盖时间", "comment_status": "评论采集状态", "engagement_formula": "互动量口径",
        "single_observations": "单帖观察（不构成主要讨论）",
        "other_signals": "其他高关注信号",
        "single_source_note": "单一来源，样本内暂无其他讨论可交叉验证",
        "single_post_note": "仅 1 条帖子提及，当前样本中未形成重复讨论",
        "empty_uncertainty": "本轮未发现明显信息不确定点。",
    },
    "en": {
        "report_time": "Report time", "keyword": "Keyword", "posts": "Posts", "comments": "Comments",
        "sentiment": "Sentiment", "content_type": "Content types", "author_type": "Author types",
        "executive_summary": "Executive Summary", "key_points": "Key Points", "main_narratives": "Main Narratives",
        "questions": "Questions and Uncertainties", "theme_landscape": "Topic Distribution", "content_sentiment": "Content Type and Sentiment Summary",
        "alerts": "Alerts", "positive_signals": "Positive and Reputation Signals", "appendix": "Appendix: Sources and Data Notes", "broad_appendix": "Appendix",
        "alert_level": "Attention level", "alert_type": "Signal category",
        "monitoring_overview": "Monitoring Overview", "top_posts": "Top 10 Original Posts", "published_date": "Published date", "post_title": "Post title", "author": "Author",
        "top10_comments": "Top 10 comments",
        "likes": "Likes", "post_comments": "Comments", "shares": "Shares", "excerpt": "Excerpt", "no_top_posts": "No relevant original posts were available for display.",
        "evidence": "Representative quotes", "source_index": "Source index", "more_sources": "More sources", "limitations": "Data Limitations", "method": "Method", "overview": "Data Overview",
        "source": "Source", "link": "Post", "open_post": "Open original post", "volume": "Posts", "engagement": "Engagement",
        "post_signal": "Discussion summary", "comment_signal": "Comment signal",
        "audience_reaction": "Audience reaction", "audience_details": "Reader feedback detail", "recurring_concerns": "Recurring concerns / questions",
        "high_engagement_viewpoint": "High-engagement viewpoint", "competitor_weekly": "Competitor Weekly Top 5",
        "overall_sentiment": "Overall Sentiment", "weekly_takeaway": "Weekly Takeaway",
        "risk": "Risk level", "risk_type": "Risk type", "why": "Why it matters", "negative": "Negative concentration",
        "positive": "Positive concentration", "question_focus": "Question concentration", "summary": "Summary", "theme": "Theme",
        "empty": "Insufficient evidence.", "empty_narratives": "Insufficient evidence to identify main narratives.",
        "empty_alerts": "No negative or sensitive discussion signal met the attention threshold in this sample.", "empty_summary": "Insufficient evidence for a summary.",
        "metric": "Metric", "value": "Value",
        "date_range": "Post coverage", "comment_status": "Comment collection", "engagement_formula": "Engagement formula",
        "single_observations": "Single-post observations",
        "other_signals": "Other notable signals",
        "single_source_note": "Single source; no other discussion in this sample to corroborate it",
        "single_post_note": "Mentioned by 1 post only; no repeated discussion in this sample",
        "empty_uncertainty": "No material open questions were identified in this sample.",
    },
}


def _rt(locale: str, key: str) -> str:
    return _REPORT_TEXT.get(locale, _REPORT_TEXT["zh"]).get(key, key)


def _comment_status_text(value: Any, locale: str = "zh") -> str:
    status = _as_text(value, "unknown")
    labels = {
        "zh": {
            "collected": "已采集",
            "not_requested": "未采集",
            "skipped_by_policy": "按策略跳过",
            "collection_failed": "采集失败",
            "partial": "部分采集",
            "unknown": "状态未知",
        },
        "en": {
            "collected": "Collected",
            "not_requested": "Not requested",
            "skipped_by_policy": "Skipped by policy",
            "collection_failed": "Collection failed",
            "partial": "Partially collected",
            "unknown": "Unknown",
        },
    }
    return labels.get(locale, labels["zh"]).get(status, status)


def _report_comment_status(collection: dict[str, Any]) -> str:
    quality = collection.get("quality") if isinstance(collection.get("quality"), dict) else {}
    if not quality:
        return "unknown"
    if not quality.get("comment_fetch_enabled"):
        return "not_requested"
    comment_errors = [
        row for row in collection.get("errors") or []
        if isinstance(row, dict) and "comment" in _as_text(row.get("stage"), "").lower()
    ]
    saved = _as_int(quality.get("comments_saved"))
    if comment_errors:
        return "partial" if saved else "collection_failed"
    if _as_int(quality.get("comment_eligible_notes")) == 0:
        return "skipped_by_policy"
    return "collected"


def _label(value: Any, locale: str, kind: str) -> str:
    text = _as_text(value, "")
    mappings = {
        "sentiment": {"zh": {"positive": "正面", "neutral": "中性", "negative": "负面"}, "en": {"positive": "Positive", "neutral": "Neutral", "negative": "Negative"}},
        "stance": {"zh": {"positive": "正面", "negative": "负面", "mixed": "正负混合", "neutral": "中性"}, "en": {"positive": "Positive", "negative": "Negative", "mixed": "Mixed", "neutral": "Neutral"}},
        "risk": {"zh": {"high": "高", "medium": "中", "low": "低", "none": "无明显风险"}, "en": {"high": "High", "medium": "Medium", "low": "Low", "none": "No material risk"}},
        # Alert 等级表示"本轮监测的关注优先级"，不是现实事件严重程度，所以刻意不写成高/中/低风险。
        "alert_level": {
            "zh": {"high": "优先关注", "medium": "持续观察", "low": "日常留意", "none": "—"},
            "en": {"high": "Priority", "medium": "Monitor", "low": "Watch", "none": "—"},
        },
        "alert_type": {
            "zh": {
                "misunderstanding": "信息误解", "cost_concern": "费用关注", "decision_impact": "决策相关",
                "reputation": "声誉讨论", "operational": "运营问题", "service_friction": "服务摩擦",
                "complaint": "投诉", "concern": "担忧", "negative_discussion": "负面讨论", "other": "其他信号", "none": "其他信号",
            },
            "en": {
                "misunderstanding": "Misunderstanding", "cost_concern": "Cost concern", "decision_impact": "Decision related",
                "reputation": "Reputation", "operational": "Operational", "service_friction": "Service friction",
                "complaint": "Complaint", "concern": "Concern", "negative_discussion": "Negative discussion",
                "other": "Other signal", "none": "Other signal",
            },
        },
        "content_type": {"zh": {"complaint": "投诉/不满", "concern": "担忧/焦虑", "question": "提问/求助", "information_sharing": "经验/信息分享", "positive_advocacy": "正面推荐", "other": "其他"}, "en": {"complaint": "Complaint", "concern": "Concern", "question": "Question", "information_sharing": "Information sharing", "positive_advocacy": "Positive advocacy", "other": "Other"}},
        "author_type": {"zh": {"real_user": "真实用户", "agency_marketing": "中介/营销线索", "unclear": "不确定"}, "en": {"real_user": "Real user", "agency_marketing": "Agency or marketing", "unclear": "Unclear"}},
    }
    if kind == "theme":
        return _zh_theme(text) if locale == "zh" else text.replace("_", " ")
    return mappings.get(kind, {}).get(locale, {}).get(text, text or "-")


def _metric_pairs_localized(processing: dict[str, Any], bundle: dict[str, Any], locale: str) -> list[tuple[str, Any]]:
    zh = _metric_pairs(processing, bundle)
    if locale == "zh":
        return [(key.replace("LLM", "AI"), value) for key, value in zh]
    names = ["Raw posts", "Processed posts", "Raw comments", "Processed comments", "LLM-annotated posts", "Relevant posts", "Relevant comments", "Sentiment", "Content types"]
    return [(names[index], value) for index, (_, value) in enumerate(zh)]


def _localize_reader_text(value: Any, locale: str, key: str = "") -> Any:
    """Hide internal enum/schema labels from reader-facing Chinese report text."""

    if locale != "zh" or key in {"post_url", "post_title", "excerpt", "author", "note_id", "note_ids", "comment_ids", "cluster_id", "uncertainty_id", "evidence_id", "report_mode", "risk_level", "risk_type", "stance", "theme", "header_distributions", "generated_scope", "signal_id", "alert_level", "alert_type", "alert_triggers", "supporting_signal_ids", "supporting_themes"}:
        return value
    if isinstance(value, dict):
        return {name: _localize_reader_text(item, locale, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_localize_reader_text(item, locale, key) for item in value]
    if not isinstance(value, str):
        return value
    replacements = {
        "Course_Selection": "选课与课程容量", "Teaching_Quality": "教学质量", "Academic_Workload": "学业负担",
        "Programme_Experience": "项目体验", "Career_Outcomes": "就业结果", "Student_Services": "学生服务",
        "Campus_Life": "校园生活", "Commercial_Noise": "其他", "Admissions": "招生与录取",
        "Internships": "实习机会", "Accommodation": "住宿与生活成本", "Scholarships": "奖学金与费用",
        "Reputation": "声誉与身份认同", "hku_relevance": "HKU 相关性", "topic_relevance": "主题相关性",
        "information_sharing": "信息分享", "positive_advocacy": "正面推荐", "agency_marketing": "中介/营销线索",
        "real_user": "真实用户", "cost_concern": "费用疑虑", "decision_impact": "决策影响",
        "service_friction": "服务摩擦", "LLM": "AI",
        "unclear": "不确定", "Other": "其他",
    }
    text = value
    for source, target in replacements.items():
        text = text.replace(source, target)
    text = re.sub(r"(?<![A-Za-z_])direct(?![A-Za-z_])", "直接相关", text)
    text = re.sub(r"(?<![A-Za-z_])indirect(?![A-Za-z_])", "间接相关", text)
    return text


def build_report_html(structured: dict[str, Any], analysis_bundle: dict[str, Any], processing: dict[str, Any], generated_at: str, locale: str = "zh") -> str:
    structured = _localize_reader_text(structured, locale)
    is_topic = structured.get("report_mode") == "topic_report"
    scope, distributions = structured.get("generated_scope") or {}, structured.get("header_distributions") or {}
    open_label = _rt(locale, "open_post")

    def evidence_html(value: Any) -> str:
        return _linkify_urls(_as_text(value, "-"), open_label)

    points = _exec_key_points(structured)
    if not is_topic and points:
        exec_body = f"<ol class='executive-findings'>{''.join(f'<li>{_esc(point)}</li>' for point in points)}</ol>"
    else:
        exec_body = f"<p>{_esc(structured.get('executive_summary') or _rt(locale, 'empty_summary'))}</p>"
    if is_topic and points:
        exec_body += f"<div class='keypoints'><strong>{_esc(_rt(locale, 'key_points'))}</strong><ul>{''.join(f'<li>{_esc(point)}</li>' for point in points)}</ul></div>"
    sections = [f"<section class='executive'><div class='section-kicker'>EXECUTIVE BRIEF</div><h2>{_esc(_rt(locale, 'executive_summary'))}</h2>{exec_body}</section>"]
    if is_topic:
        rows = structured.get("main_narratives") or []
        cards = []
        for rank, row in enumerate(rows, 1):
            comment_html = ""
            if row.get("comment_signal"):
                count_text = f"{_rt(locale, 'comments')} {row.get('comment_count', 0)}"
                if row.get("comment_question_count"):
                    count_text += f" · {_rt(locale, 'question_focus')} {row.get('comment_question_count', 0)}"
                comment_html = (
                    f"<p><strong>{_esc(_rt(locale, 'comment_signal'))}:</strong> "
                    f"{_esc(row.get('comment_signal') or '')}</p>"
                    f"<p class='muted'>{_esc(count_text)}</p>"
                )
            quotes_html = "".join(
                f"<blockquote>“{_esc(item.get('quote'))}”</blockquote>"
                for item in _as_dict_list(row.get("representative_quotes"))
            )
            concentration_html = "<p class='muted'>80% 以上加权互动来自单条帖子。</p>" if float(row.get("top_post_share") or 0) >= 0.8 else ""
            cards.append(
                f"<article class='card discussion-card {'card-risk' if row.get('stance') == 'negative' else 'card-pos' if row.get('stance') == 'positive' else ''}'>"
                f"<div class='rank'>{rank:02d}</div><div class='discussion-body'><h3>{_esc(row.get('narrative'))}<span class='tag'>{_esc(_label(row.get('stance'), locale, 'stance'))}</span></h3>"
                f"<p class='muted'>{_esc(_rt(locale, 'volume'))} {row.get('volume', 0)} · {_esc(_rt(locale, 'engagement'))} {row.get('engagement_sum', 0)}</p>"
                f"<p><strong>{_esc(_rt(locale, 'post_signal'))}:</strong> {_esc(row.get('summary') or '-')}</p>{concentration_html}"
                f"{comment_html}<p><strong>{_esc(_rt(locale, 'evidence'))}:</strong></p>{quotes_html}"
                f"<p class='more-sources'><strong>{_esc(_rt(locale, 'more_sources'))}:</strong> {_esc(row.get('evidence') or '-')}</p></div></article>"
            )
        body = "".join(cards) or f"<p>{_esc(_rt(locale, 'empty_narratives'))}</p>"
        single_rows = structured.get("other_signals") or []
        if single_rows:
            # 轻量列表，不复制主要讨论的卡片样式，避免读者误当成同等强度的结论。
            items = "".join(
                f"<li><strong>{_esc(row.get('narrative'))}</strong>："
                f"{_esc(_as_text(row.get('summary'), '') or _as_text(row.get('narrative'), ''))}"
                f"<span class='muted'>（{_esc(_rt(locale, 'single_post_note'))}；"
                f"{_esc(_rt(locale, 'engagement'))} {row.get('engagement_sum', 0)}｜{_esc(row.get('evidence') or '-')}）</span></li>"
                for row in single_rows
            )
            body += (
                f"<div class='other-signals'><h3>{_esc(_rt(locale, 'other_signals'))}</h3>"
                f"<ul>{items}</ul></div>"
            )
        sections.append(f"<section class='discussions'><div class='section-kicker'>KEY DISCUSSIONS</div><h2>{_esc(_rt(locale, 'main_narratives'))}</h2>{body}</section>")
        question_cards = []
        for row in structured.get("questions_uncertainties") or []:
            quotes_html = "".join(
                f"<blockquote>“{_esc(item.get('quote'))}”</blockquote>"
                for item in _as_dict_list(row.get("representative_quotes"))
            )
            summary_html = f"<p>{_esc(row.get('summary'))}</p>" if row.get("summary") else ""
            single_source_html = (
                f"<p class='muted'>{_esc(_rt(locale, 'single_source_note'))}。</p>"
                if row.get("is_single_source")
                else ""
            )
            question_cards.append(
                f"<div class='card'><h3>{_esc(row.get('question'))}</h3>"
                f"<p class='muted'>{_esc(row.get('uncertainty_type') or '')}{' · 独立来源 ' + str(row.get('count')) if row.get('count') else ''}</p>"
                f"{single_source_html}{summary_html}<p><strong>{_esc(_rt(locale, 'evidence'))}:</strong></p>{quotes_html}"
                f"<p><strong>{_esc(_rt(locale, 'more_sources'))}:</strong> {_esc(row.get('evidence') or '-')}</p></div>"
            )
        question_body = "".join(question_cards) or f"<p>{_esc(_rt(locale, 'empty_uncertainty'))}</p>"
        sections.append(f"<section class='uncertainty'><div class='section-kicker'>QUESTIONS STILL UNCLEAR</div><h2>{_esc(_rt(locale, 'questions'))}</h2>{question_body}</section>")
    if not is_topic:
        alert_cards = []
        for row in _report_alert_rows(structured):
            comment_html = (
                f"<div class='signal-comment'><span class='signal-label'>{_esc(_rt(locale, 'comment_signal'))}</span>"
                f"<p>{_esc(row.get('comment_signal') or '')}</p></div>"
                if row.get("comment_signal")
                else ""
            )
            quotes_html = "".join(
                f"<blockquote>“{_esc(quote.get('quote'))}”"
                + (
                    f' <a href="{html.escape(_public_post_url(quote.get("post_url"), _as_text(quote.get("note_id"), "")), quote=True)}"'
                    f' target="_blank" rel="noreferrer">{_esc(open_label)}</a>'
                    if (quote.get("post_url") or quote.get("note_id"))
                    else ""
                )
                + "</blockquote>"
                for quote in _as_dict_list(row.get("supporting_quotes"))
                if _as_text(quote.get("quote"), "")
            )
            alert_level = _as_text(row.get("alert_level"), "")
            meta_html = (
                f"<div class='signal-meta'><span class='alert-badge alert-badge-{_esc(alert_level or 'low')}'>"
                f"{_esc(_label(alert_level, locale, 'alert_level'))}</span>"
                f"<span class='alert-category'>{_esc(_label(row.get('alert_type'), locale, 'alert_type'))}</span></div>"
            )
            alert_cards.append(
                f"<article class='signal-card signal-card-alert'><h3 class='signal-title'>{_esc(row.get('signal'))}</h3>"
                f"{meta_html}"
                f"<div class='signal-description'><span class='signal-label'>{_esc(_rt(locale, 'summary'))}</span>"
                f"<p>{_esc(row.get('summary') or '-')}</p></div>{comment_html}{quotes_html}"
                f"<div class='signal-evidence'><span class='signal-label'>{_esc(_rt(locale, 'evidence'))}</span>"
                f"<p>{evidence_html(row.get('evidence'))}</p></div></article>"
            )
        alert_body = f"<div class='signal-card-list'>{''.join(alert_cards)}</div>" if alert_cards else f"<p>{_esc(_rt(locale, 'empty_alerts'))}</p>"
        sections.append(_html_section(_rt(locale, "alerts"), alert_body))
        positive_cards = []
        for row in structured.get("positive_reputation_signals") or []:
            comment_html = (
                f"<div class='signal-comment'><span class='signal-label'>{_esc(_rt(locale, 'comment_signal'))}</span>"
                f"<p>{_esc(row.get('comment_signal') or '')}</p></div>"
                if row.get("comment_signal")
                else ""
            )
            positive_cards.append(
                f"<article class='signal-card signal-card-positive'><h3 class='signal-title'>{_esc(row.get('signal'))}</h3>"
                f"<div class='signal-description'><span class='signal-label'>{_esc(_rt(locale, 'summary'))}</span>"
                f"<p>{_esc(row.get('summary') or '')}</p></div>{comment_html}"
                f"<div class='signal-evidence'><span class='signal-label'>{_esc(_rt(locale, 'evidence'))}</span>"
                f"<p>{evidence_html(row.get('evidence'))}</p></div></article>"
            )
        positive_body = f"<div class='signal-card-list'>{''.join(positive_cards)}</div>" if positive_cards else f"<p>{_esc(_rt(locale, 'empty'))}</p>"
        sections.append(_html_section(_rt(locale, "positive_signals"), positive_body))
        theme_rows = "".join(
            f"<tr><td>{_esc(_label(row.get('theme'), locale, 'theme'))}</td><td class='num'>{row.get('volume', 0)}</td><td class='num'>{row.get('engagement_sum', 0)}</td><td>{_esc(row.get('summary') or '')}</td></tr>"
            for row in structured.get("theme_landscape") or []
        )
        theme_table = f"<table><thead><tr><th>{_esc(_rt(locale, 'theme'))}</th><th>{_esc(_rt(locale, 'posts'))}</th><th>{_esc(_rt(locale, 'engagement'))}</th><th>{_esc(_rt(locale, 'summary'))}</th></tr></thead><tbody>{theme_rows}</tbody></table>" if theme_rows else f"<p>{_esc(_rt(locale, 'empty'))}</p>"
        sections.append(_html_section(_rt(locale, "theme_landscape"), theme_table))
        competitor_cards = []
        for school in _as_dict_list(structured.get("competitor_weekly")):
            competitor_posts = []
            for post in _as_dict_list(school.get("posts"))[:5]:
                post_url = _public_post_url(post.get("post_url"), _as_text(post.get("note_id"), ""))
                post_title = _esc(post.get("title") or "-")
                post_title_html = (
                    f'<a href="{html.escape(post_url, quote=True)}" target="_blank" rel="noreferrer">{post_title}</a>'
                    if post_url
                    else post_title
                )
                competitor_posts.append(
                    f"<li class='competitor-post'><span class='competitor-rank'>{_esc(post.get('rank') or 0)}</span>"
                    f"<div><h4>{post_title_html}</h4><span class='competitor-sentiment'>{_esc(str(post.get('sentiment') or 'neutral').title())}</span>"
                    f"<p>{_as_int(post.get('like_count')):,} likes · {_as_int(post.get('comment_count')):,} comments · {_as_int(post.get('share_count')):,} shares</p></div></li>"
                )
            if not competitor_posts:
                continue
            competitor_cards.append(
                f"<article class='competitor-school'><h3>{_esc(school.get('school') or '-')}</h3>"
                f"<p><strong>{_esc(_rt(locale, 'overall_sentiment'))}:</strong> {_esc(school.get('overall_sentiment') or 'Mixed')}</p>"
                f"<div class='competitor-takeaway'><span class='signal-label'>{_esc(_rt(locale, 'weekly_takeaway'))}</span>"
                f"<p>{_esc(school.get('weekly_takeaway') or '-')}</p></div><ol>{''.join(competitor_posts)}</ol></article>"
            )
        if competitor_cards:
            sections.append(_html_section(_rt(locale, "competitor_weekly"), "<div class='competitor-list'>" + "".join(competitor_cards) + "</div>"))
    appendix = structured.get("appendix") or {}
    evidence_items = []
    for row in appendix.get("evidence") or []:
        post_url = row.get("post_url")
        if post_url:
            link = html.escape(_public_post_url(post_url, _as_text(row.get("note_id"), "")), quote=True)
            post_link_html = f'<a href="{link}" target="_blank" rel="noreferrer">{_esc(open_label)}</a>'
        else:
            post_link_html = ""
        evidence_items.append(
            f"<div class='evidence-item'><div class='muted'>{_esc(row.get('source') or _rt(locale, 'source'))}{' · ' + _esc(row.get('date')) if row.get('date') else ''}</div>"
            f"<p>{_esc(row.get('hint') or '-')}</p>{post_link_html}</div>"
        )
    evidence_rows = "".join(evidence_items)
    evidence_table = f"<div class='evidence-grid'>{evidence_rows}</div>" if evidence_rows else f"<p>{_esc(_rt(locale, 'empty'))}</p>"
    limits = "".join(f"<li>{_esc(item)}</li>" for item in structured.get("data_limitations") or [])
    method = "".join(f"<li>{_esc(item)}</li>" for item in appendix.get("methodology") or [])
    metrics = "".join(f"<tr><td>{_esc(key)}</td><td>{_esc(value)}</td></tr>" for key, value in _metric_pairs_localized(processing, analysis_bundle, locale))
    if is_topic:
        sections.append(
            f"<section class='sources'><div class='section-kicker'>SOURCES</div><h2>{_esc(_rt(locale, 'source_index'))}</h2>{evidence_table}</section>"
        )
        appendix_body = f"<div class='method-body'><h3>{_esc(_rt(locale, 'limitations'))}</h3><ul>{limits}</ul><h3>{_esc(_rt(locale, 'method'))}</h3><ul>{method}</ul><h3>{_esc(_rt(locale, 'overview'))}</h3><table><tbody>{metrics}</tbody></table></div>"
    else:
        appendix_body = f"<div class='method-body'><h3>{_esc(_rt(locale, 'limitations'))}</h3><ul>{limits}</ul><h3>{_esc(_rt(locale, 'method'))}</h3><ul>{method}</ul></div>"
    sections.append(f"<details class='methodology'><summary>{_esc(_rt(locale, 'appendix' if is_topic else 'broad_appendix'))}<span>＋</span></summary>{appendix_body}</details>")
    date_range = f"{scope.get('earliest_post_date') or '-'} — {scope.get('latest_post_date') or '-'}"
    if _as_int(scope.get("top10_comments")) > 0:
        comment_metric = f"<div><span>{_esc(_rt(locale, 'top10_comments'))}</span><strong>{_esc(scope.get('top10_comments'))} · {_esc('仅 Top 10' if locale == 'zh' else 'Top 10 only')}</strong></div>"
    elif scope.get("comment_collection_status") == "not_requested":
        comment_metric = f"<div><span>{_esc(_rt(locale, 'comments').replace('相关评论数', '评论'))}</span><strong>{_esc(_comment_status_text('not_requested', locale))}</strong></div>"
    else:
        comment_metric = f"<div><span>{_esc(_rt(locale, 'comments'))}</span><strong>{_esc(scope.get('analysis_comments'))} · {_esc(_comment_status_text(scope.get('comment_collection_status'), locale))}</strong></div>"
    metrics_html = (
        f"<div><span>{_esc(_rt(locale, 'posts'))}</span><strong>{_esc(scope.get('analysis_notes'))}</strong></div>"
        f"{comment_metric}"
        f"<div><span>{_esc(_rt(locale, 'date_range'))}</span><strong>{_esc(date_range)}</strong></div>"
    )
    if is_topic:
        metrics_html += (
            f"<div><span>{_esc(_rt(locale, 'sentiment'))}</span><strong>{_esc(_distribution_text_v3(distributions.get('sentiment'), lambda v: _label(v, locale, 'sentiment')))}</strong></div>"
            f"<div><span>{_esc(_rt(locale, 'main_narratives'))}</span><strong>{len(structured.get('main_narratives') or [])}</strong></div>"
        )

    def distribution_bar(counts: Any, kind: str) -> str:
        values = counts if isinstance(counts, dict) else {}
        usable = [(str(key), int(value or 0)) for key, value in values.items() if str(key) not in {"", "unknown"} and int(value or 0) > 0]
        total = sum(value for _, value in usable) or 1
        palettes = {
            "sentiment": {"neutral": "#8d9ab4", "positive": "#35a66b", "negative": "#e45858"},
            "content_type": {"information_sharing": "#16449f", "question": "#f0ad27", "concern": "#e96a54", "complaint": "#d64b4b", "positive_advocacy": "#35a66b", "other": "#8d9ab4"},
        }
        label_group = "sentiment" if kind == "sentiment" else "content_type"
        segments = "".join(
            f"<span style='width:{value / total * 100:.2f}%;background:{palettes.get(kind, {}).get(key, '#5f7fc0')}' title='{_esc(_label(key, locale, label_group))}: {value}'>{value / total * 100:.0f}%</span>"
            for key, value in usable
        )
        legend = "".join(
            f"<li><i style='background:{palettes.get(kind, {}).get(key, '#5f7fc0')}'></i>{_esc(_label(key, locale, label_group))} <b>{value}</b></li>"
            for key, value in usable
        )
        return f"<div class='stacked-bar'>{segments}</div><ul class='chart-legend'>{legend}</ul>"

    chart_panels_html = (
        f"<div class='chart-panel'><h2>{_esc(_rt(locale, 'sentiment'))}</h2>{distribution_bar(distributions.get('sentiment'), 'sentiment')}</div>"
        f"<div class='chart-panel'><h2>{_esc(_rt(locale, 'content_type'))}</h2>{distribution_bar(distributions.get('content_type'), 'content_type')}</div>"
    )
    charts_html = (
        f"<section class='charts'>{chart_panels_html}</section>"
    )
    overview_html = (
        f"<section class='monitoring-overview'><div class='section-kicker'>MONITORING OVERVIEW</div>"
        f"<h2>{_esc(_rt(locale, 'monitoring_overview'))}</h2><div class='metrics overview-metrics'>{metrics_html}</div>"
        f"<div class='chart-grid'>{chart_panels_html}</div></section>"
    )
    top_post_cards = []
    for row in structured.get("top_original_posts") or []:
        url = _public_post_url(row.get("post_url"), _as_text(row.get("note_id"), ""))
        title = _esc(row.get("post_title") or "-")
        title_html = f'<a href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">{title}</a>' if url else title
        metric_chips = "".join(
            f" · {_esc(_rt(locale, label))} {_esc(row.get(field))}"
            for field, label in (("like_count", "likes"), ("comment_count", "post_comments"), ("share_count", "shares"))
            if row.get(field) is not None
        )
        open_link = f'<a class="open-post" href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">{_esc(open_label)} →</a>' if url else ""
        # 摘录默认只显示两行（CSS line-clamp），不让单条帖子占据大段纵向空间。
        excerpt = f"<p class='post-excerpt'>{_esc(row.get('excerpt'))}</p>" if row.get("excerpt") else ""
        # 评论分析（受众反应/高频关注点/高互动观点）改成"标签: 内容"一行内联，
        # 最多两行截断（line-clamp），不再是标签独占一行 + 内容另起一块的堆叠
        # 排版——这部分默认就折叠在 <details> 里，PDF 导出前才强制展开，堆叠
        # 写法会白白多占好几行纵向空间。
        audience_parts = []
        if row.get("audience_reaction"):
            audience_parts.append(
                f"<p class='audience-line'><b>{_esc(_rt(locale, 'audience_reaction'))}:</b> "
                f"{_esc(row.get('audience_reaction'))}</p>"
            )
        recurring = _as_dict_list(row.get("recurring_signals"))[:2]
        if recurring:
            recurring_text = "；".join(
                f"{_esc(item.get('signal'))}（{_esc(item.get('support_count') or 0)}）"
                for item in recurring
            )
            audience_parts.append(
                f"<p class='audience-line'><b>{_esc(_rt(locale, 'recurring_concerns'))}:</b> {recurring_text}</p>"
            )
        high = row.get("high_engagement_viewpoint")
        if isinstance(high, dict) and high.get("summary"):
            audience_parts.append(
                f"<p class='audience-line'><b>{_esc(_rt(locale, 'high_engagement_viewpoint'))}:</b> "
                f"{_esc(high.get('summary'))}（{_esc(high.get('like_count') or 0)} likes）"
                f" — “{_esc(high.get('quote') or '')}”</p>"
            )
        audience_html = (
            f"<details class='post-audience'><summary>{_esc(_rt(locale, 'audience_details'))}</summary>"
            f"{''.join(audience_parts)}</details>"
            if audience_parts
            else ""
        )
        # 一行元信息：日期 · 作者 · 情绪 · 互动量，避免每条帖子铺开成一大块。
        meta_line = (
            f"<div class='post-meta'>{_esc(row.get('published_at') or '-')} · {_esc(row.get('author') or '-')}"
            f" · <span class='sentiment'>{_esc(_label(row.get('sentiment'), locale, 'sentiment'))}</span>"
            f"{metric_chips}</div>"
        )
        top_post_cards.append(
            f"<article class='top-post-card'><div class='top-post-rank'>{int(row.get('rank') or 0):02d}</div>"
            f"<div class='top-post-body'><h3>{title_html}{open_link}</h3>{meta_line}"
            f"{excerpt}{audience_html}</div></article>"
        )
    top_posts_body = "".join(top_post_cards) or f"<p>{_esc(_rt(locale, 'no_top_posts'))}</p>"
    top_posts_html = f"<section class='top-posts'><div class='section-kicker'>TOP ORIGINAL POSTS</div><h2>{_esc(_rt(locale, 'top_posts'))}</h2>{top_posts_body}</section>"
    lang = "zh-CN" if locale == "zh" else "en"
    return f'''<!doctype html><html lang="{lang}"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{_esc(structured.get('title') or 'HKU RED Insights Report')}</title><style>
    :root{{--navy:#0f2d73;--blue:#1f52bb;--ink:#102044;--text:#334155;--muted:#64748b;--line:#dbe3f0;--canvas:#f6f8fc;--green:#35a66b;--orange:#e8a21d;--red:#e45858;--cream:#fff9ea;--shadow:0 8px 28px rgba(15,45,115,.065)}}
    *{{box-sizing:border-box}}html{{scroll-behavior:smooth}}body{{margin:0;background:var(--canvas);color:var(--text);font-family:Inter,"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;line-height:1.5}}a{{color:var(--blue);text-decoration:none}}.web-nav{{position:sticky;top:0;z-index:10;height:64px;background:rgba(255,255,255,.94);backdrop-filter:blur(14px);border-bottom:1px solid var(--line);display:flex;align-items:center;justify-content:space-between;padding:0 max(24px,calc((100vw - 1180px)/2))}}.brand{{display:flex;align-items:center;gap:11px;color:var(--navy);font-size:13px;font-weight:800;line-height:1.15}}.brand-mark{{width:38px;height:43px;border-radius:7px 7px 11px 11px;background:linear-gradient(145deg,#173f94,#0f2d73);color:#fff;display:grid;place-items:center;font-size:11px;box-shadow:inset 0 -4px 0 #e7b646}}main{{max-width:1180px;margin:auto;padding:14px 18px 24px}}header,section,.methodology{{background:#fff;border:1px solid var(--line);border-radius:10px;padding:12px 16px;margin-bottom:8px;box-shadow:var(--shadow)}}header{{position:relative;overflow:hidden;padding:16px 18px 12px}}header:after{{content:"HKU";position:absolute;right:24px;top:-30px;color:rgba(15,45,115,.035);font-size:150px;font-weight:900}}.report-type{{font-size:11px;font-weight:800;letter-spacing:.13em;color:var(--blue)}}h1{{position:relative;font-size:22px;line-height:1.2;color:var(--ink);margin:4px 0 2px}}h2{{font-size:15px;color:var(--navy);margin:0 0 8px}}h3{{font-size:13.5px;color:var(--ink);margin:0 0 4px}}p,li{{font-size:12.5px;margin:4px 0}}.muted{{color:var(--muted);font-size:11.5px}}.section-kicker{{font-size:10px;font-weight:800;letter-spacing:.12em;color:var(--blue);margin-bottom:4px}}.metrics{{position:relative;display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));margin-top:10px;border:1px solid var(--line);border-radius:11px;background:#fff}}.metrics div{{min-width:0;padding:6px 10px;border-right:1px solid var(--line)}}.metrics div:last-child{{border-right:0}}.metrics span{{display:block;color:var(--muted);font-size:10.5px}}.metrics strong{{display:block;margin-top:2px;color:var(--ink);font-size:13.5px}}.executive{{background:linear-gradient(120deg,#f4f8ff,#fff);border-color:#cfdcf5}}.executive-findings{{margin:0;padding-left:22px}}.executive-findings li+li{{margin-top:8px}}.keypoints{{margin-top:10px;padding:8px 12px;border-radius:9px;background:rgba(255,255,255,.78);border:1px solid #dbe6f9}}.keypoints ul{{margin-bottom:0}}.monitoring-overview .overview-metrics{{margin:0 0 14px}}.charts,.chart-grid{{display:grid;grid-template-columns:1fr 1fr;gap:0;padding:0;overflow:hidden}}.chart-grid{{border:1px solid var(--line);border-radius:12px}}.chart-panel{{padding:10px 14px}}.chart-panel+ .chart-panel{{border-left:1px solid var(--line)}}.stacked-bar{{display:flex;height:28px;border-radius:8px;overflow:hidden;background:#edf1f7}}.stacked-bar span{{display:grid;place-items:center;overflow:hidden;color:#fff;font-size:11px;font-weight:750;min-width:2px}}.chart-legend{{display:flex;gap:16px;flex-wrap:wrap;list-style:none;padding:0;margin:12px 0 0}}.chart-legend li{{display:flex;align-items:center;gap:6px;color:var(--muted);font-size:11px}}.chart-legend i{{display:inline-block;width:8px;height:8px;border-radius:50%}}.chart-legend b{{color:var(--ink)}}.top-post-card{{display:grid;grid-template-columns:22px 1fr;gap:8px;padding:6px 0;border-top:1px solid var(--line);break-inside:avoid}}.top-post-card:first-of-type{{border-top:0}}.top-post-rank{{color:var(--muted);font-size:10.5px;font-weight:800;padding-top:2px;text-align:right}}.top-post-body h3{{font-size:13px;margin:0 0 2px;font-weight:650;line-height:1.3}}.post-meta{{color:var(--muted);font-size:11px;line-height:1.4}}.post-meta .sentiment{{color:#177a50;font-weight:700}}.post-excerpt{{margin:2px 0 0;color:#5b6b84;font-size:11.5px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}.post-audience{{margin:4px 0 0;padding:0;border-left:2px solid #c3d0e6;background:transparent}}.post-audience>summary{{cursor:pointer;color:var(--blue);font-size:11px;font-weight:700;padding-left:8px;list-style:none}}.post-audience[open]{{padding:4px 0 4px 10px;background:#f5f7fb}}.post-audience[open]>summary{{padding-left:0;margin-bottom:3px}}.audience-line{{margin:2px 0;font-size:12px;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}}.audience-line+.audience-line{{margin-top:3px}}.audience-line b{{color:#53627b;font-weight:800}}.open-post{{margin-left:6px;font-size:10.5px;font-weight:700;white-space:nowrap}}.signal-card-list{{display:grid;gap:8px;grid-template-columns:repeat(2,minmax(0,1fr))}}.signal-card{{padding:8px 12px 0;border:1px solid;border-left-width:4px;border-radius:5px;box-shadow:none;break-inside:avoid;overflow:hidden}}.signal-card-alert{{background:#fff9f8;border-color:#ead6d3;border-left-color:#b96862}}.signal-card-positive{{background:#f7fbf8;border-color:#d1e2d7;border-left-color:#4f8b69}}.signal-title{{margin:0 0 4px;color:var(--ink);font-size:13.5px;line-height:1.3}}.signal-meta{{display:flex;gap:6px;flex-wrap:wrap;margin:0 0 6px}}.alert-badge,.alert-category{{padding:2px 9px;border-radius:999px;font-size:10px;font-weight:750;letter-spacing:.02em}}.alert-badge{{background:#f3e0dd;color:#8d3f3a}}.alert-badge-high{{background:#f6d9d5;color:#a03a33}}.alert-badge-medium{{background:#faead0;color:#8a5b16}}.alert-badge-low{{background:#e7edf6;color:#456091}}.alert-category{{background:rgba(255,255,255,.75);color:#5b6b84;border:1px solid rgba(100,116,139,.2)}}.signal-description,.signal-comment{{margin-bottom:6px}}.signal-label{{display:block;margin-bottom:3px;color:#53627b;font-size:9.5px;font-weight:800;letter-spacing:.08em;text-transform:uppercase}}.signal-description p,.signal-comment p,.signal-evidence p{{margin:0;font-size:12px;line-height:1.45}}.signal-evidence{{margin:0 -12px;padding:6px 12px 8px;border-top:1px solid rgba(100,116,139,.15);background:rgba(241,245,249,.55);color:#657287}}.signal-evidence .signal-label{{color:#718096}}.signal-evidence a{{font-weight:700}}.competitor-list{{display:grid;gap:8px;grid-template-columns:repeat(2,minmax(0,1fr))}}.competitor-school{{padding:8px 12px;border:1px solid var(--line);border-left:3px solid var(--navy);background:#fbfcff}}.competitor-school>p{{margin:2px 0 6px}}.competitor-takeaway{{padding:6px 10px;background:#f3f6fb}}.competitor-takeaway p{{margin:0;font-size:12px}}.competitor-school ol{{list-style:none;padding:0;margin:8px 0 0}}.competitor-post{{display:grid;grid-template-columns:20px 1fr;gap:6px;padding:4px 0;border-top:1px solid var(--line)}}.competitor-rank{{font-weight:800;color:var(--navy)}}.competitor-post h4{{display:inline;margin:0 6px 0 0;font-size:12.5px}}.competitor-post p{{margin:2px 0 0;color:var(--muted);font-size:10.5px}}.competitor-sentiment{{font-size:10px;font-weight:750;color:var(--blue)}}.discussion-card{{display:grid;grid-template-columns:26px 1fr;gap:8px;padding:7px 0;border:0;border-top:1px solid var(--line);border-radius:0;margin:0;box-shadow:none;break-inside:avoid}}.discussion-card:first-of-type{{border-top:0}}.rank{{width:22px;height:22px;border-radius:6px;background:linear-gradient(145deg,var(--blue),var(--navy));color:#fff;display:grid;place-items:center;font-weight:800;font-size:10.5px}}.discussion-body h3{{font-size:13.5px;display:flex;justify-content:space-between;gap:12px}}.tag{{flex:none;display:inline-block;padding:3px 9px;border-radius:999px;background:#eef3fd;color:var(--blue);font-size:10px;font-weight:750}}.card-risk .tag{{background:#fff0ee;color:#b43e3e}}.card-pos .tag{{background:#eaf8f0;color:#177a50}}blockquote{{margin:4px 0;padding:5px 9px;border-left:3px solid #bcd0f5;background:#f6f9ff;border-radius:0 6px 6px 0;color:#19366f;font-size:11.5px;line-height:1.4}}.uncertainty{{background:var(--cream);border-color:#f1dfb4}}.uncertainty .section-kicker,.uncertainty h2{{color:#a76505}}.uncertainty .card{{background:rgba(255,255,255,.6);border:1px solid #efdcae;border-radius:9px;padding:7px 10px;margin:5px 0}}.evidence-grid{{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:5px}}.evidence-item{{border:1px solid var(--line);border-radius:7px;padding:5px 7px;break-inside:avoid}}.evidence-item p{{margin:2px 0;font-size:11px;line-height:1.35}}.evidence-item a{{font-size:11.5px;font-weight:700}}table{{width:100%;border-collapse:collapse;font-size:11.5px}}th,td{{padding:3px 7px;border:1px solid var(--line);text-align:left;vertical-align:top;line-height:1.3}}th{{background:#f5f8fe;color:var(--navy)}}.num{{text-align:right}}.methodology{{padding:0;overflow:hidden}}.methodology summary{{display:flex;justify-content:space-between;align-items:center;padding:8px 14px;color:var(--navy);font-weight:750;cursor:pointer;list-style:none}}.method-body{{padding:0 14px 10px;border-top:1px solid var(--line)}}
    @media(max-width:600px){{.web-nav{{padding:0 14px}}main{{padding:16px 10px}}header,section{{padding:18px}}h1{{font-size:26px}}.metrics{{grid-template-columns:1fr 1fr}}.metrics div:nth-child(2){{border-right:0}}.metrics div:nth-child(-n+2){{border-bottom:1px solid var(--line)}}.charts,.chart-grid{{grid-template-columns:1fr}}.chart-panel+.chart-panel{{border-left:0;border-top:1px solid var(--line)}}.top-post-card{{grid-template-columns:38px 1fr;gap:11px}}.top-post-rank{{width:36px;height:36px;border-radius:9px}}.evidence-grid{{grid-template-columns:1fr}}.discussion-body h3{{display:block}}.tag{{margin-left:8px}}.signal-card-list,.competitor-list{{grid-template-columns:1fr}}}}
    </style></head><body><nav class="web-nav"><div class="brand"><span class="brand-mark">HKU</span><span>HKU BUSINESS SCHOOL<br>香港大学经管学院 · INSIGHTS</span></div></nav><main><header><div class="report-type">XIAOHONGSHU INSIGHT REPORT</div><div class="muted">{_esc(_rt(locale, 'report_time'))}: {_esc(generated_at)}</div><h1>{_esc(structured.get('title'))}</h1>{f'<div class="metrics">{metrics_html}</div>' if is_topic else ''}</header>{''.join(sections[:1])}{charts_html if is_topic else overview_html + top_posts_html}{''.join(sections[1:])}</main></body></html>'''
