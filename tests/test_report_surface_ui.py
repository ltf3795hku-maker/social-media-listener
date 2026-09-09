"""报告页 UI 契约：预览不截断、下载 HTML、英文版按钮走翻译而不是重新收集。"""
from __future__ import annotations

import ast
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"
SOURCE = APP.read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _func(name: str) -> ast.FunctionDef:
    return next(
        node for node in ast.walk(TREE) if isinstance(node, ast.FunctionDef) and node.name == name
    )


def test_report_iframe_allows_scrolling_so_long_reports_are_not_cut_off() -> None:
    """固定高度 + 不开滚动会让长报告在 1800px 处被硬裁掉，看不到下面的内容。"""

    call = _func("_render_report_iframe")
    src = ast.unparse(call)
    assert "scrolling=True" in src


def test_download_button_produces_html_report() -> None:
    helper_src = ast.unparse(_func("_render_report_download_button"))
    assert "html_path.read_bytes()" in helper_src
    assert "text/html" in helper_src
    assert ".html" in helper_src


def test_report_surface_wires_both_download_and_english_section() -> None:
    surface_src = ast.unparse(_func("_render_report_surface"))
    assert "_render_report_download_button" in surface_src
    assert "_render_english_report_section" in surface_src


def test_english_report_button_calls_translation_not_a_new_collection() -> None:
    """生成英文报告必须走翻译已有内容的路径，不能是重新采集/重新分析。"""

    section_src = ast.unparse(_func("_render_english_report_section"))
    assert "generate_english_report" in section_src
    assert "create_run" not in section_src
    assert "analyze_existing_run" not in section_src


def test_english_report_falls_back_to_generate_button_when_missing() -> None:
    section = _func("_render_english_report_section")
    src = ast.unparse(section)
    # has_en 分支判断必须同时看 report_html_en 字段和文件是否存在，
    # 不能只信任数据库里存的路径（文件可能被手动删掉）。
    assert "report_html_en" in src
    assert ".exists()" in src


def test_english_report_section_surfaces_translation_coverage() -> None:
    """翻译有遗漏时不能悄无声息——已生成分支必须调用覆盖率提示，
    在下载按钮之前，用户点下载前就能看到。"""

    section_src = ast.unparse(_func("_render_english_report_section"))
    assert "_render_translation_coverage_note" in section_src
    assert section_src.index("_render_translation_coverage_note") < section_src.index(
        "_render_report_download_button"
    )
