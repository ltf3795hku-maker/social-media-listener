"""英文版报告：翻译已有的中文 report.json，不重新调用 analyze。"""
from __future__ import annotations

import json
import types
from pathlib import Path

import pytest

from xhs_listener.io_utils import read_json, write_json
from xhs_listener.report import (
    _apply_translations,
    _extract_translatable,
    report_run_english,
    translate_structured_report_to_english,
)


# --------------------------------------------------------------------------
# _extract_translatable / _apply_translations: 纯字符串处理，不碰 LLM
# --------------------------------------------------------------------------


def test_extract_translatable_only_picks_up_strings_with_chinese_characters() -> None:
    """note_id/URL/日期/数字/英文枚举码这些字段天然不含中文，不应该被送去翻译。"""

    structured = {
        "title": "港大商学院周报",
        "note_id": "abc123",
        "post_url": "https://example.com/note/abc123",
        "published_at": "2026-08-20",
        "sentiment": "positive",
        "like_count": 42,
        "nested": {"summary": "多条帖子讨论选课", "tags": ["school", "招生"]},
    }
    collected: list[str] = []
    template = _extract_translatable(structured, collected)

    # 含中文的叶子被替换成占位符，非中文的原样保留在 template 里。
    assert template["note_id"] == "abc123"
    assert template["post_url"] == "https://example.com/note/abc123"
    assert template["published_at"] == "2026-08-20"
    assert template["sentiment"] == "positive"
    assert template["like_count"] == 42
    assert template["nested"]["tags"][0] == "school"

    assert set(collected) == {"港大商学院周报", "多条帖子讨论选课", "招生"}


def test_apply_translations_falls_back_to_original_when_translation_missing_or_blank() -> None:
    originals = ["港大商学院周报", "多条帖子讨论选课"]
    collected: list[str] = []
    template = _extract_translatable({"title": originals[0], "summary": originals[1]}, collected)

    # 第一条翻译成功，第二条模型返回空字符串（等同于没翻译）。
    translations = ["HKU Business School weekly report", ""]
    result = _apply_translations(template, translations, collected)

    assert result["title"] == "HKU Business School weekly report"
    assert result["summary"] == originals[1]  # 空翻译 -> 保留中文原文


# --------------------------------------------------------------------------
# translate_structured_report_to_english: 用假 client，验证分批 + 容错
# --------------------------------------------------------------------------


class _EchoTranslateClient:
    """把每个中文字符串前面加上 'EN:' 模拟翻译；用来验证顺序/分批映射正确。"""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def get_response(self, messages):
        prompt = messages[0]["content"]
        batch = json.loads(prompt.split("Input array:\n", 1)[1])
        self.calls.append(batch)
        translated = [f"EN:{item}" for item in batch]
        content = json.dumps(translated, ensure_ascii=False)
        message = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)

    def usage_dict(self, response):
        return {"total_tokens": 12}

    def extract_json(self, text):
        return json.loads(text)


class _AlwaysFailClient:
    def get_response(self, messages):
        return "Error: network unreachable"

    def usage_dict(self, response):
        return {}

    def extract_json(self, text):
        raise AssertionError("should not be called when get_response returns an error string")


def test_translate_structured_report_batches_and_translates_every_string() -> None:
    structured = {
        "title": "港大商学院周报",
        "main_narratives": [{"summary": "多条帖子讨论选课"}, {"summary": "部分学生反馈课程实用"}],
    }
    client = _EchoTranslateClient()

    translated, usage_rows, coverage = translate_structured_report_to_english(structured, client, batch_size=2)

    assert translated["title"] == "EN:港大商学院周报"
    assert translated["main_narratives"][0]["summary"] == "EN:多条帖子讨论选课"
    assert translated["main_narratives"][1]["summary"] == "EN:部分学生反馈课程实用"
    assert len(usage_rows) == 2  # 3 条字符串，batch_size=2 -> 两批
    assert all(row["phase"] == "report_translate_en" for row in usage_rows)
    assert coverage == {"total_strings": 3, "translated_strings": 3}


def test_translate_structured_report_keeps_chinese_when_llm_call_fails() -> None:
    """一批翻译失败，重试次数用完后不应该让整份报告翻译报错——保留中文原文，继续往下走。"""

    structured = {"title": "港大商学院周报", "summary": "多条帖子讨论选课"}
    client = _AlwaysFailClient()

    translated, usage_rows, coverage = translate_structured_report_to_english(structured, client, max_batch_retries=1)

    assert translated == structured  # 全部保留原文
    assert usage_rows == []
    assert coverage == {"total_strings": 2, "translated_strings": 0}


class _FailOnceThenSucceedClient:
    """第一次调用失败（模拟网络抖动/限流），重试第二次成功——验证批级重试能救回来。"""

    def __init__(self) -> None:
        self.attempts = 0

    def get_response(self, messages):
        self.attempts += 1
        if self.attempts == 1:
            return "Error: temporary network hiccup"
        prompt = messages[0]["content"]
        batch = json.loads(prompt.split("Input array:\n", 1)[1])
        translated = [f"EN:{item}" for item in batch]
        content = json.dumps(translated, ensure_ascii=False)
        message = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)

    def usage_dict(self, response):
        return {"total_tokens": 5}

    def extract_json(self, text):
        return json.loads(text)


def test_translate_structured_report_retries_a_failed_batch_before_giving_up() -> None:
    """单次失败不该直接判死刑：重试一次，能救回来的批次不应该退回中文。"""

    structured = {"title": "港大商学院周报"}
    client = _FailOnceThenSucceedClient()

    translated, usage_rows, coverage = translate_structured_report_to_english(structured, client, max_batch_retries=1)

    assert client.attempts == 2  # 第一次失败，第二次重试成功
    assert translated["title"] == "EN:港大商学院周报"
    assert len(usage_rows) == 1  # 只有成功那次调用才计入 usage
    assert coverage == {"total_strings": 1, "translated_strings": 1}


def test_translate_structured_report_batch_size_mismatch_falls_back_per_string() -> None:
    """模型返回的数组比预期短：多出来的那些字符串保留中文，而不是抛异常或错位。"""

    class ShortResponseClient:
        def get_response(self, messages):
            content = json.dumps(["EN:only one"], ensure_ascii=False)
            message = types.SimpleNamespace(content=content)
            return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)

        def usage_dict(self, response):
            return {}

        def extract_json(self, text):
            return json.loads(text)

    structured = {"a": "中文一", "b": "中文二"}
    translated, _, coverage = translate_structured_report_to_english(structured, ShortResponseClient())

    assert translated["a"] == "EN:only one"
    assert translated["b"] == "中文二"  # 数组第二项缺失，保留原文
    assert coverage == {"total_strings": 2, "translated_strings": 1}


# --------------------------------------------------------------------------
# report_run_english: 端到端（不含真实分析产物，只构造 report_run_english 需要的文件）
# --------------------------------------------------------------------------


def _write_minimal_report_fixture(run_path: Path) -> None:
    write_json(run_path / "analysis.json", {"analysis": {}})
    write_json(run_path / "processing.json", {"scan_mode": "topic_scan"})
    write_json(
        run_path / "report.json",
        {
            "title": "港大商学院周报",
            "report_mode": "topic_report",
            "generated_scope": {"analysis_notes": 1, "analysis_comments": 0},
            "header_distributions": {},
            "executive_summary": "本周共收集到相关帖子若干条。",
            "main_narratives": [],
            "other_signals": [],
            "questions_uncertainties": [],
            "appendix": {"evidence": [], "methodology": []},
            "data_limitations": [],
        },
    )


def test_report_run_english_requires_chinese_report_first(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        report_run_english(tmp_path, client=_EchoTranslateClient())


def test_report_run_english_writes_translated_json_and_html(tmp_path: Path) -> None:
    _write_minimal_report_fixture(tmp_path)
    client = _EchoTranslateClient()

    result = report_run_english(tmp_path, client=client)

    assert Path(result["report_json_en"]).exists()
    assert Path(result["report_html_en"]).exists()

    structured_en = read_json(tmp_path / "report_en.json")
    assert structured_en["title"] == "EN:港大商学院周报"
    assert structured_en["executive_summary"] == "EN:本周共收集到相关帖子若干条。"

    html_en = (tmp_path / "report_en.html").read_text(encoding="utf-8")
    assert "EN:港大商学院周报" in html_en
    assert '<html lang="en">' in html_en

    # 中文版原封不动，没有被英文生成流程改动。
    structured_zh = read_json(tmp_path / "report.json")
    assert structured_zh["title"] == "港大商学院周报"

    usage = read_json(tmp_path / "llm_usage.json")
    assert usage["usage"]  # 翻译的 token 花费也要记账

    # 覆盖率元数据写进了 report_en.json，供前端判断要不要提示用户重新生成；
    # 这个 key 不影响正文渲染。
    assert structured_en["_translation_coverage"]["translated_strings"] == structured_en["_translation_coverage"]["total_strings"]
    assert result["translation_coverage"] == structured_en["_translation_coverage"]
