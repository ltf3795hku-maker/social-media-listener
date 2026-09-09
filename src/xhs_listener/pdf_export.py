"""把自包含的 report.html 渲染成 PDF。

用真实的 Chromium（Playwright）打印，而不是纯 Python 的 HTML->PDF 库——报告的
CSS 用了不少 grid/flex 布局（指标卡、图表网格、竞对卡片），只有真实浏览器
引擎能保证转出来的 PDF 和网页上看到的样式完全一致。

Chromium 二进制需要本机先装一次（大约一两百 MB，一次性，装好之后不用再装）。
第一次调用时如果发现没装，会自动帮忙跑一次 `playwright install chromium`；
装完自动重试，不需要用户去终端手动敲命令。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path


class PdfExportError(RuntimeError):
    """PDF 渲染失败——通常是 playwright 包缺失，或者 Chromium 自动安装失败。"""


def html_to_pdf(html_path: Path, *, auto_install: bool = True) -> bytes:
    """把一个自包含的 HTML 报告文件渲染成 PDF bytes。"""

    try:
        return _render_pdf(html_path)
    except ImportError as exc:
        raise PdfExportError(
            "缺少 playwright 依赖，请先运行: pip install playwright"
        ) from exc
    except Exception as exc:  # noqa: BLE001
        if not auto_install or not _looks_like_missing_browser(exc):
            raise PdfExportError(f"PDF 渲染失败：{exc}") from exc
        _install_chromium()
        try:
            return _render_pdf(html_path)
        except Exception as retry_exc:  # noqa: BLE001
            raise PdfExportError(f"安装 Chromium 后仍渲染失败：{retry_exc}") from retry_exc


def _render_pdf(html_path: Path) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pw:
        # Cloud Run / Docker: no Chrome sandbox, tiny /dev/shm.
        browser = pw.chromium.launch(
            args=["--no-sandbox", "--disable-dev-shm-usage"],
        )
        try:
            page = browser.new_page()
            # 用 file:// 打开而不是 set_content：报告目前是纯内联样式/base64
            # 图片，但走真实文件加载能顺带兼容以后万一出现的相对路径资源。
            page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
            # 报告里"附录"、方法论、单帖听众反馈这些区块在网页上是默认折叠的
            # <details>，浏览器打印/生成 PDF 时折叠的 <details> 内容不会画出来——
            # 不是"没生成"，是内容还在，只是被叠起来了。PDF 是给人带走看的，
            # 不该比网页版本缺内容，所以打印前把它们统一展开。
            page.eval_on_selector_all("details", "els => els.forEach(el => { el.open = true; })")
            pdf_bytes = page.pdf(
                format="A4",
                print_background=True,
                margin={"top": "12mm", "bottom": "14mm", "left": "10mm", "right": "10mm"},
            )
        finally:
            browser.close()
    return pdf_bytes


def _looks_like_missing_browser(exc: Exception) -> bool:
    text = str(exc).lower()
    return "executable doesn't exist" in text or "browsertype.launch" in text


def _install_chromium() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "playwright", "install", "chromium"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise PdfExportError(
            "自动安装 Chromium 失败，请在终端手动运行: python -m playwright install chromium\n"
            f"{result.stderr.strip()[-800:]}"
        )


def cached_report_pdf(html_path: Path, pdf_path: Path, *, auto_install: bool = True) -> bytes:
    """带缓存的转换：report.html 没变就直接读旧的 report.pdf，不重新起 Chromium。

    每次打开报告页面都会走一遍这里；缓存让第一次之后的访问基本是零成本的
    stat + read，而不是每次都要几秒钟重新渲染。
    """

    if pdf_path.exists() and pdf_path.stat().st_mtime >= html_path.stat().st_mtime:
        return pdf_path.read_bytes()
    pdf_bytes = html_to_pdf(html_path, auto_install=auto_install)
    pdf_path.write_bytes(pdf_bytes)
    return pdf_bytes
