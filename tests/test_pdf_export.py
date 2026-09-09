"""report.html -> PDF：只测缓存逻辑和错误分类，不启动真实 Chromium。"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

import pytest

from xhs_listener.pdf_export import (
    PdfExportError,
    _install_chromium,
    _looks_like_missing_browser,
    _render_pdf,
    cached_report_pdf,
)


def test_render_pdf_forces_details_open_before_printing() -> None:
    """报告里附录/方法论/受众反应默认是折叠的 <details>；浏览器打印折叠的
    <details> 时内容不会画出来（不是没生成，是被叠起来了）。PDF 是给人带走
    看的，不该比网页缺内容，所以打印前必须先把所有 <details> 强制展开，
    且这一步要在 page.pdf(...) 之前执行。"""

    source = inspect.getsource(_render_pdf)
    open_call_index = source.find("el.open = true")
    pdf_call_index = source.find("page.pdf(")
    assert open_call_index != -1, "没有找到强制展开 <details> 的代码"
    assert pdf_call_index != -1
    assert open_call_index < pdf_call_index, "必须先展开 <details> 再打印，否则内容还是会被截掉"


def test_cached_report_pdf_renders_once_and_reuses_cache(tmp_path: Path, monkeypatch) -> None:
    html_path = tmp_path / "report.html"
    html_path.write_text("<html><body>hi</body></html>", encoding="utf-8")
    pdf_path = tmp_path / "report.pdf"

    calls: list[Path] = []

    def fake_html_to_pdf(path: Path, *, auto_install: bool = True) -> bytes:
        calls.append(path)
        return b"%PDF-fake-bytes"

    monkeypatch.setattr("xhs_listener.pdf_export.html_to_pdf", fake_html_to_pdf)

    first = cached_report_pdf(html_path, pdf_path)
    assert first == b"%PDF-fake-bytes"
    assert len(calls) == 1
    assert pdf_path.exists()

    # 第二次调用：report.html 没变，应该直接读缓存，不再调用 html_to_pdf。
    second = cached_report_pdf(html_path, pdf_path)
    assert second == b"%PDF-fake-bytes"
    assert len(calls) == 1


def test_cached_report_pdf_regenerates_when_html_changes(tmp_path: Path, monkeypatch) -> None:
    html_path = tmp_path / "report.html"
    html_path.write_text("<html><body>v1</body></html>", encoding="utf-8")
    pdf_path = tmp_path / "report.pdf"
    pdf_path.write_bytes(b"stale-pdf")
    # 让缓存的 pdf 看起来比 html 旧。
    import os
    import time

    old = time.time() - 100
    os.utime(pdf_path, (old, old))

    calls: list[Path] = []

    def fake_html_to_pdf(path: Path, *, auto_install: bool = True) -> bytes:
        calls.append(path)
        return b"fresh-pdf"

    monkeypatch.setattr("xhs_listener.pdf_export.html_to_pdf", fake_html_to_pdf)

    result = cached_report_pdf(html_path, pdf_path)
    assert result == b"fresh-pdf"
    assert len(calls) == 1
    assert pdf_path.read_bytes() == b"fresh-pdf"


def test_looks_like_missing_browser_detects_playwright_install_error() -> None:
    exc = RuntimeError("Executable doesn't exist at /home/user/.cache/ms-playwright/chromium-1194/chrome-linux/chrome")
    assert _looks_like_missing_browser(exc)

    assert not _looks_like_missing_browser(RuntimeError("some other unrelated failure"))


def test_install_chromium_does_not_install_system_dependencies(monkeypatch) -> None:
    calls = []

    class CompletedProcess:
        returncode = 0
        stderr = ""

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return CompletedProcess()

    monkeypatch.setattr("xhs_listener.pdf_export.subprocess.run", fake_run)

    _install_chromium()

    assert calls == [
        (
            [sys.executable, "-m", "playwright", "install", "chromium"],
            {"capture_output": True, "text": True},
        )
    ]


def test_html_to_pdf_wraps_import_error_as_pdf_export_error(tmp_path: Path, monkeypatch) -> None:
    html_path = tmp_path / "report.html"
    html_path.write_text("<html></html>", encoding="utf-8")

    def raise_import_error(path: Path) -> bytes:
        raise ImportError("No module named 'playwright'")

    monkeypatch.setattr("xhs_listener.pdf_export._render_pdf", raise_import_error)

    from xhs_listener.pdf_export import html_to_pdf

    with pytest.raises(PdfExportError):
        html_to_pdf(html_path)
