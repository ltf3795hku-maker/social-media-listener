"""报告呈现层契约：单一呈现、紧凑版式、下载即所见。"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from xhs_listener.report import build_report_html

ROOT = Path(__file__).resolve().parents[1]


def _broad_structured(post_count: int = 10) -> dict:
    def post(index: int) -> dict:
        return {
            "rank": index,
            "note_id": f"n{index}",
            "published_at": "2026-08-21",
            "post_title": f"讨论帖 {index}",
            "author": f"作者{index}",
            "like_count": 100 + index,
            "comment_count": 20 + index,
            "share_count": index,
            "sentiment": "neutral",
            "excerpt": "较长的帖子摘录，用于验证默认只展示短摘录。" * 6,
            "post_url": f"https://www.xiaohongshu.com/explore/n{index}",
            "audience_reaction": "申请者主要追问 offer 时间。",
            "recurring_signals": [{"signal": "Offer timeline", "support_count": 3}],
            "high_engagement_viewpoint": {"summary": "学费被反复提及", "quote": "越来越贵", "like_count": 126},
        }

    return {
        "title": "Broad 报告",
        "report_mode": "broad_report",
        "generated_scope": {"analysis_notes": 86, "analysis_comments": 0},
        "header_distributions": {},
        "executive_summary": "摘要",
        "top_original_posts": [post(i) for i in range(1, post_count + 1)],
        "alerts": [
            {
                "signal_id": "s1",
                "signal": "重点关注信号",
                "alert_level": "high",
                "alert_type": "operational",
                "summary": "多条讨论围绕流程展开。",
                "evidence": "到现在还没收到通知",
                "supporting_quotes": [
                    {"quote": "锁了是不是就代表稳了？", "note_id": "n1", "post_url": "https://example.com/n1"}
                ],
            }
        ],
        "positive_reputation_signals": [{"signal_id": "p1", "signal": "正面信号", "summary": "反馈实用。", "evidence": "很实用"}],
        "theme_landscape": [{"theme": "Admissions", "summary": "招生讨论", "volume": 20, "engagement_sum": 5000}],
        "appendix": {"evidence": [], "methodology": ["方法"]},
        "data_limitations": ["限制"],
    }


# --- 单一呈现：不再有第二套打印/下载版式 ---------------------------------


def test_report_has_no_separate_print_presentation() -> None:
    html_report = build_report_html(_broad_structured(), {}, {"scan_mode": "broad_scan"}, "2026-08-26")

    for removed in ("@media print", "print-button", "window.print()", "@page", "print-extra"):
        assert removed not in html_report, f"报告里仍存在第二套呈现：{removed}"


def test_streamlit_downloads_the_same_file_it_embeds() -> None:
    """下载按钮必须是页面里嵌入的那份 report.html 打印出来的 PDF——不是另一份简化版。"""

    source = (ROOT / "streamlit_app.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    def _func(name: str) -> ast.FunctionDef:
        return next(
            node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name
        )

    surface_body = ast.get_source_segment(source, _func("_render_report_surface")) or ""
    download_body = ast.get_source_segment(source, _func("_render_report_download_button")) or ""

    # 同一个 html_path 既用于 iframe 嵌入，也传给下载按钮去生成 PDF。
    assert "_render_report_iframe(_html_data_url(html_path))" in surface_body
    assert "_render_report_download_button(" in surface_body
    # 下载按钮把传入的 html_path（而不是另一份文件）转成 PDF 给用户下载。
    assert "cached_report_pdf(html_path, pdf_path)" in download_body
    assert 'mime="application/pdf"' in download_body
    assert '.pdf"' in download_body
    # 不得再生成简化版/Markdown 版下载
    assert "build_report_markdown" not in source
    assert "report_md" not in source


# --- 紧凑版式 -------------------------------------------------------------


def test_many_evidence_posts_do_not_inflate_the_report() -> None:
    """帖子从 3 条增到 15 条时，报告体量增长应接近线性且每条占比很小。"""

    small = build_report_html(_broad_structured(3), {}, {"scan_mode": "broad_scan"}, "2026-08-26")
    large = build_report_html(_broad_structured(15), {}, {"scan_mode": "broad_scan"}, "2026-08-26")

    per_post = (len(large) - len(small)) / 12
    assert per_post < 1200, f"单条帖子占 {per_post:.0f} 字节，版式仍然过重"


def test_post_excerpt_is_clamped_and_audience_detail_is_collapsed() -> None:
    html_report = build_report_html(_broad_structured(2), {}, {"scan_mode": "broad_scan"}, "2026-08-26")

    # 摘录默认只显示两行
    assert "-webkit-line-clamp:2" in html_report
    # 受众反应默认折叠，按需展开
    assert html_report.count("<details class='post-audience'>") == 2
    # 元信息压在一行里，不再是一排胶囊
    assert ".post-signals" not in html_report
    assert "class='post-meta'" in html_report


def test_compact_layout_keeps_grounding_and_source_links() -> None:
    """紧凑化不得削弱 grounding：原帖链接与引用原话必须保留。"""

    html_report = build_report_html(_broad_structured(3), {}, {"scan_mode": "broad_scan"}, "2026-08-26")

    assert html_report.count("https://www.xiaohongshu.com/explore/") >= 3
    assert "打开原帖" in html_report
    assert "锁了是不是就代表稳了？" in html_report
    assert "https://example.com/n1" in html_report


# --- 打印/PDF 密度：移动端断点不能误伤 A4 打印宽度 ------------------------


def test_mobile_breakpoint_is_narrower_than_a4_print_content_width() -> None:
    """Playwright 用 A4 纸张打印时，正文可用宽度大约是 718px（210mm 纸宽减掉
    左右各 10mm 页边距）。之前断点是 760px，刚好比 718px 宽——结果打印/生成
    PDF 时被误判成"手机屏幕"，监测概览三个指标被挤成两栏一行、情绪分布和
    内容类型分布两张图从并排变成上下堆叠，白白多出好几页。断点必须明显
    小于 A4 可用宽度，真正的窄屏手机才会触发这套简化样式。"""

    html_report = build_report_html(_broad_structured(2), {}, {"scan_mode": "broad_scan"}, "2026-08-26")

    assert "@media(max-width:760px)" not in html_report
    match = re.search(r"@media\(max-width:(\d+)px\)", html_report)
    assert match, "找不到响应式断点"
    breakpoint_px = int(match.group(1))
    a4_print_content_width_px = 718  # 210mm 纸宽 - 20mm 左右页边距，96dpi 换算
    assert breakpoint_px < a4_print_content_width_px - 50, (
        f"断点 {breakpoint_px}px 离 A4 打印宽度太近，PDF 可能又会被误判成手机屏幕"
    )


def test_monitoring_overview_metrics_grid_does_not_force_two_columns() -> None:
    """监测概览的三个指标不该被样式表写死成两列——那样無论屏幕多宽都会
    有一项自己占一行（"一条一行"）。列数应该交给 auto-fit 网格按可用宽度
    自适应，而不是硬编码 1fr 1fr。"""

    html_report = build_report_html(_broad_structured(2), {}, {"scan_mode": "broad_scan"}, "2026-08-26")

    default_rule = re.search(r"\.metrics\{[^}]*grid-template-columns:([^;}]+)", html_report)
    assert default_rule, "找不到 .metrics 的默认网格定义"
    assert "auto-fit" in default_rule.group(1)


def test_appendix_and_audience_details_expand_when_forced_open() -> None:
    """附录、方法论、单帖受众反应在网页上默认折叠（<details> 无 open），
    这是故意的——但 PDF 导出前会把所有 <details> 强制展开（见
    pdf_export.html_to_pdf），所以这里只需要保证内容确实在 <details> 里面，
    强制展开后就不会是空的。"""

    html_report = build_report_html(_broad_structured(2), {}, {"scan_mode": "broad_scan"}, "2026-08-26")

    assert "<details" in html_report
    assert " open" not in html_report.split("<details", 1)[1].split(">", 1)[0]
    # 附录本身也是一个 <details>，且里面确实有内容（方法论 clarify 了"方法"）。
    appendix_start = html_report.find("<details class='methodology'")
    assert appendix_start != -1
    assert "方法" in html_report[appendix_start : appendix_start + 4000]


def test_audience_summary_label_does_not_duplicate_the_first_inner_block_label() -> None:
    """<details class='post-audience'> 的 <summary> 曾经和它里面第一个
    audience-block 的 label 用了同一个词（"受众反应"/"受众反应"背靠背出现），
    强制展开后这个重复在 PDF 里非常显眼、也白占一行。summary 应该用独立的
    "详情"类文案，和内部子标签区分开。"""

    html_report = build_report_html(_broad_structured(2), {}, {"scan_mode": "broad_scan"}, "2026-08-26")

    marker = "<details class='post-audience'><summary>"
    idx = html_report.find(marker)
    assert idx != -1
    summary_text = html_report[idx + len(marker) : html_report.find("</summary>", idx)]
    assert summary_text != "受众反应"
