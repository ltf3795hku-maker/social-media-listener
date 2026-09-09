from __future__ import annotations

import base64
import html
import json
import math
import os
from datetime import datetime
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import streamlit as st
import streamlit.components.v1 as components

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from xhs_listener.streamlit_secrets import copy_secrets_to_environ
from xhs_listener.ui_preview import (
    PREVIEW_STATES,
    build_preview_analysis,
    build_preview_posts,
    build_preview_report,
    build_preview_run,
    preview_mode_enabled,
)


st.set_page_config(
    page_title="HKU Business School Insights",
    page_icon="🔎",
    layout="wide",
    initial_sidebar_state="collapsed",
)


def _apply_streamlit_cloud_secrets() -> None:
    """Community Cloud injects credentials via st.secrets, not a .env file."""

    try:
        payload = st.secrets.to_dict()
    except Exception:  # pragma: no cover - missing secrets.toml locally
        return
    if payload:
        copy_secrets_to_environ(payload, os.environ, override=True)


_apply_streamlit_cloud_secrets()
if load_dotenv is not None:
    load_dotenv(ROOT / ".env", override=False)

UI_PREVIEW_MODE = preview_mode_enabled()
if UI_PREVIEW_MODE:
    backend = None
else:
    from xhs_listener import service as backend

PAGE_SIZE = 20
# 样本量选项全部对齐整页，避免为多出的几条再买一整页 TikHub 请求。
SAMPLE_SIZE_OPTIONS = [20, 40, 60, 80]
DEFAULT_TARGET_POST_COUNT = 40
TOP_COMMENT_PERCENT = 20
TOP_COMMENTS_PER_POST = 20
TOP_COMMENT_MIN_COMMENTS = 3
FULL_COMMENTS_PER_POST = 100
APP_LOGIN_USERNAME = os.getenv("APP_LOGIN_USERNAME", "").strip()
APP_LOGIN_PASSWORD = os.getenv("APP_LOGIN_PASSWORD", "").strip()
SORT_LABELS = {
    "zh": {"time_descending": "最新优先", "relevance": "热门优先"},
    "en": {"time_descending": "Newest first", "relevance": "Most popular"},
}
CONTENT_SCOPE_LABELS = {
    "zh": {"posts_only": "仅帖子（推荐）", "top_comments": "帖子 + 高讨论度评论", "all_comments": "帖子 + 全评论"},
    "en": {"posts_only": "Posts only (recommended)", "top_comments": "Posts + top comments", "all_comments": "Posts + all comments"},
}
MODE_LABELS = {
    "zh": {"topic_scan": "Topic Search", "broad_scan": "Weekly Monitoring"},
    "en": {"topic_scan": "Topic Search", "broad_scan": "Weekly Monitoring"},
}
# time_filter 的内部值是中文（后端按中文映射 TikHub 参数），界面按语言显示。
TIME_FILTER_OPTIONS = ["不限", "一天内", "一周内", "半年内"]
TIME_FILTER_LABELS = {
    "zh": {"不限": "不限", "一天内": "一天内", "一周内": "一周内", "半年内": "半年内"},
    "en": {"不限": "Any time", "一天内": "Past 24 hours", "一周内": "Past week", "半年内": "Past 6 months"},
}
STATUS_LABELS = {
    "zh": {"queued": "排队中", "running": "运行中", "collected": "样本已获取", "succeeded": "已完成", "failed": "失败", "stopped": "已停止"},
    "en": {"queued": "Queued", "running": "Running", "collected": "Collected", "succeeded": "Done", "failed": "Failed", "stopped": "Stopped"},
}

COPY = {
    "zh": {
        "load_runs_failed": "读取运行记录失败：{error}",
        "metric_requests": "TikHub 请求",
        "metric_cost": "样本获取成本",
        "metric_tokens": "AI tokens",
        "metric_step": "当前步骤",
        "log_title": "运行日志",
        "no_logs": "暂无日志",
    },
    "en": {
        "load_runs_failed": "Failed to load run history: {error}",
        "metric_requests": "TikHub requests",
        "metric_cost": "Sample acquisition cost",
        "metric_tokens": "AI tokens",
        "metric_step": "Current step",
        "log_title": "Run log",
        "no_logs": "No logs yet",
    },
}


def _lang() -> str:
    return st.session_state.get("lang") or "zh"


def _t(key: str) -> str:
    table = COPY.get(_lang(), COPY["zh"])
    return table.get(key, COPY["zh"].get(key, key))


def _status_label(status: str) -> str:
    labels = STATUS_LABELS.get(_lang(), STATUS_LABELS["zh"])
    return labels.get(str(status or ""), str(status or "-"))


def main() -> None:
    _init_state()
    _init_auth_state()
    _inject_app_styles()
    if UI_PREVIEW_MODE:
        _render_ui_preview()
        return
    if not _render_login_gate():
        st.stop()
    runs, reports = _load_runs()
    _render_app_nav()
    surface = st.session_state.get("surface", "home")
    if surface == "run":
        _render_run_surface(runs)
    elif surface == "reports":
        _render_reports_library(runs, reports)
    elif surface == "report":
        _render_report_surface(runs)
    elif surface == "help":
        _render_help_surface()
    else:
        _render_home_surface()



ACTIVE_PHASES = {"searching", "processing", "analyzing", "generating"}
POLL_SECONDS = 4


@st.fragment(run_every=POLL_SECONDS)
def _render_live_progress(run_id: int) -> None:
    """任务进行中时，只有这一小块区域在刷新。

    过去这里是一个挂在 main() 末尾的轮询器，每 4 秒调用一次裸 ``st.rerun()``。
    ``st.rerun()`` 的默认作用域是整个 app，所以每一次轮询都会把登录闸门、页头、
    导航和搜索表单整页重跑一遍 —— 这就是页头肉眼可见闪烁的原因；
    同时每次整页重跑都会重建 fragment，导致上一轮还在飞的 tick 找不到自己的 id，
    报 "fragment with id ... does not exist anymore"。

    现在这个 fragment 自己按 run_every 重新执行，**运行期间不触发任何整页重跑**：
    它只重新读取这一个 run 的状态并重绘状态区。只有在任务真正结束
    （succeeded / failed / stopped）时才调用一次 ``st.rerun()``，
    让整页加载最终报告。
    """

    run = _run_by_id(run_id)
    if run is None:
        return
    phase = _resolve_ui_phase(run)
    if phase not in ACTIVE_PHASES:
        # running -> finished：整页重跑一次，加载最终报告。
        st.rerun()
        return

    # 采集完成后由前端接力启动分析；这里直接触发，不需要整页重跑，
    # 下一个 tick 自然会读到新状态。
    _auto_start_analysis(run)
    _render_progress_body(run, phase)

    # 结果区（帖子列表 + 概览侧栏）也放进这个 fragment，跟着每次 tick 一起
    # 重新读取 —— 否则采集/分析进行中永远不会有整页重跑去刷新它，"已找到帖子"
    # 会一直停在页面刚加载时的快照（很可能是 0）。
    rows = _note_rows_for_run(run)
    relevance_complete = _has_complete_relevance_labels(rows)
    _render_results_section(run, rows, phase, relevance_complete=relevance_complete, analysis=None)


def _run_by_id(run_id: int) -> dict[str, Any] | None:
    try:
        return backend.get_run(int(run_id))
    except Exception:  # noqa: BLE001
        runs, _ = _load_runs()
        return next((row for row in runs if int(row.get("id") or 0) == int(run_id)), None)


def _render_progress_body(run: dict[str, Any], phase: str) -> None:
    """状态区正文：阶段条 + 当前状态文字 + 步骤计数 + 已用时。"""

    zh = _lang() == "zh"
    rows = _note_rows_for_run(run)
    relevance_complete = _has_complete_relevance_labels(rows)
    relevant = _relevant_note_count(rows) if relevance_complete else len(rows)
    st.markdown(
        f'<p class="run-live-summary">{html.escape(_run_phase_summary(phase, len(rows), relevant, relevance_complete))}</p>',
        unsafe_allow_html=True,
    )
    _render_progress_strip(phase)
    meta = _progress_meta(run, zh)
    if meta:
        st.markdown(f'<div class="run-live-meta">{html.escape(meta)}</div>', unsafe_allow_html=True)


def _progress_meta(run: dict[str, Any], zh: bool) -> str:
    """步骤计数与已用时；后端没有可靠百分比，这里也不编造进度百分比。"""

    parts: list[str] = []
    step = str(run.get("current_step") or "").strip()
    steps = _pipeline_steps(run)
    if step and step in steps:
        parts.append(
            (f"步骤 {steps.index(step) + 1}/{len(steps)}·{_step_label(step, zh)}")
            if zh
            else f"Step {steps.index(step) + 1}/{len(steps)} · {_step_label(step, zh)}"
        )
    if step == "competitors":
        total = int(run.get("competitor_schools_total") or 0)
        if total:
            done = int(run.get("competitor_schools_done") or 0)
            parts.append(f"已完成 {done}/{total} 所学校" if zh else f"{done}/{total} schools done")
    elapsed = _elapsed_text(run, zh)
    if elapsed:
        parts.append(elapsed)
    return " · ".join(parts)


def _pipeline_steps(run: dict[str, Any]) -> list[str]:
    from xhs_listener.run_manager import ANALYSIS_STEPS, BROAD_ANALYSIS_STEPS, COLLECT_STEPS

    analysis = BROAD_ANALYSIS_STEPS if str(run.get("mode") or "") == "broad_scan" else ANALYSIS_STEPS
    return list(COLLECT_STEPS) + list(analysis)


def _step_label(step: str, zh: bool) -> str:
    labels = {
        "collect": ("采集样本", "Collecting"),
        "process": ("整理内容", "Preparing"),
        "analyze": ("分析内容", "Analyzing"),
        "top10_comments": ("Top 10 评论", "Top 10 comments"),
        "competitors": ("竞对周榜", "Competitors"),
        "report": ("生成报告", "Generating report"),
    }
    zh_label, en_label = labels.get(step, (step, step))
    return zh_label if zh else en_label


def _elapsed_text(run: dict[str, Any], zh: bool) -> str:
    started = str(run.get("started_at") or run.get("created_at") or "")
    if not started:
        return ""
    try:
        began = datetime.fromisoformat(started)
    except ValueError:
        return ""
    seconds = int(max(0, (datetime.now() - began).total_seconds()))
    minutes, secs = divmod(seconds, 60)
    span = f"{minutes} 分 {secs} 秒" if zh else f"{minutes}m {secs}s"
    return (f"已用时 {span}") if zh else f"Elapsed {span}"


def _inject_app_styles() -> None:
    """Shared visual language for every Streamlit surface."""

    st.markdown(
        """
        <style>
        :root {
          --primary-blue:#1f5dcc; --primary-blue-hover:#194fae; --soft-blue:#eef4ff;
          --ink:#26384f; --text:#475569; --muted:#718096; --line:#dbe3f0;
          --surface:#ffffff; --canvas:#ffffff; --green:#15945f; --amber:#a56606;
          --red:#c24141; --radius:14px; --shadow:0 2px 10px rgba(38,56,79,.05);
        }
        html,body,.stApp {font-family:Inter,"Noto Sans SC","PingFang SC","Microsoft YaHei",sans-serif;color:var(--text)}
        .stApp {background:var(--canvas)}
        header[data-testid="stHeader"],section[data-testid="stSidebar"] {display:none}
        .block-container {max-width:1540px;padding:1rem 2.25rem 4rem}
        h1,h2,h3,h4 {color:var(--ink);letter-spacing:-.025em}
        h1 {font-size:2.25rem!important;font-weight:650!important}
        h2 {font-size:1.5rem!important;font-weight:650!important}
        h3 {font-size:1.05rem!important;font-weight:650!important}
        p,li {line-height:1.65}
        div[data-testid="stMarkdownContainer"] p {font-size:14px}
        .stButton>button,.stDownloadButton>button,div[data-testid="stFormSubmitButton"]>button {
          min-height:40px;border:1px solid #cfd9ea;border-radius:9px;background:#fff;color:var(--ink);
          box-shadow:none;font-weight:550;white-space:nowrap;transition:background .15s ease,border-color .15s ease,color .15s ease}
        .stButton>button:hover,.stDownloadButton>button:hover {border-color:#b8c8e2;background:#f8fafc;color:var(--primary-blue)}
        .stButton>button[kind="primary"],div[data-testid="stFormSubmitButton"]>button[kind="primary"] {
          border-color:var(--primary-blue);background:var(--primary-blue);color:#fff;box-shadow:none}
        .stButton>button[kind="primary"]:hover,div[data-testid="stFormSubmitButton"]>button[kind="primary"]:hover {
          border-color:var(--primary-blue-hover);background:var(--primary-blue-hover);color:#fff}
        .stButton>button:disabled,div[data-testid="stFormSubmitButton"]>button:disabled {opacity:.45;box-shadow:none}
        div[data-baseweb="input"],div[data-baseweb="select"]>div,div[data-baseweb="textarea"] {border-radius:10px!important;background:#fff}
        div[data-baseweb="input"]:focus-within,div[data-baseweb="select"]:focus-within {box-shadow:0 0 0 2px rgba(31,93,204,.10)!important}
        div[data-testid="stForm"] {border:1px solid var(--line);border-radius:var(--radius);padding:18px;background:#fff;box-shadow:var(--shadow)}
        div[data-testid="stExpander"] {border:1px solid #e4e9f1;border-radius:12px;background:#fff;box-shadow:none}
        div[data-testid="stMetric"] {border:1px solid var(--line);border-radius:12px;background:#fff;padding:13px 15px;box-shadow:none}
        div[data-testid="stMetricLabel"] p {font-size:12px!important;color:var(--muted)!important}
        div[data-testid="stMetricValue"] {font-size:22px!important;color:var(--ink);font-weight:650}
        div[data-testid="stAlert"] {border-radius:11px}

        .brand-lockup {display:flex;align-items:center;min-width:250px;height:52px}
        .brand-lockup img {width:230px;max-height:46px;object-fit:contain;object-position:left center}
        .account-pill {display:flex;justify-content:flex-end;align-items:center;gap:8px;color:var(--ink);font-weight:500;white-space:nowrap}
        .account-avatar {width:32px;height:32px;border-radius:50%;display:grid;place-items:center;background:var(--soft-blue);color:var(--primary-blue);font-size:15px}
        div[data-testid="stHorizontalBlock"]:has(.brand-lockup) .stButton>button {
          min-height:36px!important;padding:0 14px!important;border-color:transparent!important;border-radius:8px!important;
          background:transparent!important;box-shadow:none!important;color:var(--text)!important;font-weight:500!important}
        div[data-testid="stHorizontalBlock"]:has(.brand-lockup) .stButton>button:hover {background:#f6f8fb!important;color:var(--primary-blue)!important}
        div[data-testid="stHorizontalBlock"]:has(.brand-lockup) .stButton>button[kind="primary"] {
          background:var(--soft-blue)!important;color:var(--primary-blue)!important}

        .hero-shell {padding:70px 20px 0;background:#fff;text-align:center}
        .hero-shell h1 {margin:0 auto;color:var(--ink);font-size:38px!important;font-weight:600!important;line-height:1.2}
        .hero-shell p {margin:12px auto 0;color:var(--muted);font-size:16px!important;font-weight:400}
        div[data-baseweb="input"]:has(input[aria-label="搜索主题"]),
        div[data-baseweb="input"]:has(input[aria-label="Search topic"]) {
          min-height:60px!important;border:1px solid #d7e1f2!important;border-radius:30px!important;background:#fff!important;
          box-shadow:0 2px 6px rgba(38,56,79,.05),0 8px 22px rgba(38,56,79,.045)!important}
        div[data-baseweb="input"]:has(input[aria-label="搜索主题"]):hover,
        div[data-baseweb="input"]:has(input[aria-label="Search topic"]):hover,
        div[data-baseweb="input"]:has(input[aria-label="搜索主题"]):focus-within,
        div[data-baseweb="input"]:has(input[aria-label="Search topic"]):focus-within {border-color:#b8ccef!important;box-shadow:0 2px 8px rgba(38,56,79,.07)!important}
        input[aria-label="搜索主题"],input[aria-label="Search topic"] {min-height:58px!important;background:#fff!important;color:var(--ink)!important;font-size:16px!important}
        input[aria-label="搜索主题"]::placeholder,input[aria-label="Search topic"]::placeholder {color:#94a3b8!important;opacity:1!important}
        div[data-testid="stSegmentedControl"] button {min-height:34px!important;padding:0 13px!important;border-color:transparent!important;background:transparent!important;color:#667085!important;font-size:13px!important;font-weight:500!important;box-shadow:none!important}
        button[data-testid="stBaseButton-segmented_controlActive"] {background:var(--soft-blue)!important;color:var(--primary-blue)!important}
        button[data-testid="stBaseButton-segmented_controlActive"] p {color:var(--primary-blue)!important}
        .mode-caption {margin:5px 0 0;color:#94a3b8;font-size:11px}
        div[data-testid="stSelectbox"]:has([aria-label="样本数量"]) div[data-baseweb="select"]>div,
        div[data-testid="stSelectbox"]:has([aria-label="Sample size"]) div[data-baseweb="select"]>div {
          min-height:34px!important;height:34px!important;border:0!important;border-radius:8px!important;background:transparent!important;box-shadow:none!important;color:#667085!important;font-size:13px!important}
        div[data-testid="stPopover"] button {min-height:34px!important;border:0!important;background:transparent!important;box-shadow:none!important;color:#667085!important;font-size:13px!important;font-weight:500!important}
        div[data-testid="stPopover"] button:hover {background:#f8fafc!important;color:var(--primary-blue)!important}
        .broad-search-note {display:flex;align-items:center;gap:12px;height:60px;min-height:60px;box-sizing:border-box;border:1px solid #d7e1f2;border-radius:30px;background:#fff;padding:0 22px;color:var(--ink);box-shadow:0 2px 6px rgba(38,56,79,.05),0 8px 22px rgba(38,56,79,.045);margin-bottom:16px;}
        .broad-icon {flex:0 0 34px;width:34px;height:34px;border-radius:50%;display:grid;place-items:center;background:transparent;color:var(--muted);font-size:16px}
        .broad-search-note strong {display:block;margin:0;color:var(--ink);font-size:14px;line-height:1.3}
        .broad-search-note span {display:block;margin-top:3px;color:var(--muted);font-size:12px;line-height:1.3}
        .home-submit-anchor + div .stButton>button,.home-submit-anchor~div .stButton>button {min-height:40px}

        .section-heading {display:flex;justify-content:space-between;align-items:end;margin:30px 0 14px}
        .section-heading h2 {margin:0}.section-heading p {margin:4px 0 0;color:var(--muted)}
        .surface-card {border:1px solid #e4e9f1;border-radius:14px;background:#fff;padding:18px;box-shadow:0 1px 4px rgba(38,56,79,.035)}
        .run-live-summary {color:var(--muted);font-size:14px;margin:-8px 0 14px}
        .run-live-meta {color:var(--muted);font-size:12px;margin:8px 0 0}
        .report-teaser {min-height:150px}.eyebrow {color:var(--primary-blue);font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase}
        .report-teaser h3 {margin:9px 0 5px}.muted {color:var(--muted)}
        .page-title {padding:8px 0 12px}.page-title h1 {margin:4px 0 6px}.page-title p {margin:0;color:var(--muted);font-size:15px!important}
        .progress-shell {border:1px solid var(--line);border-radius:14px;background:#fff;padding:16px 20px 14px;margin:8px 0 18px;box-shadow:none}
        .steps {position:relative;display:grid;grid-template-columns:repeat(3,1fr)}
        .steps:before {content:"";position:absolute;top:10px;left:16.6667%;right:16.6667%;height:2px;background:var(--line)}
        .step {position:relative;padding-top:34px;color:var(--muted);font-size:13px;font-weight:600;text-align:center}
        .step-dot {position:absolute;top:0;left:50%;transform:translateX(-50%);width:22px;height:22px;border:5px solid #fff;border-radius:50%;background:#d4dce9;box-shadow:0 0 0 1px #d4dce9}
        .step.done .step-dot {background:var(--green);box-shadow:0 0 0 1px var(--green)}
        .step.active .step-dot {background:var(--primary-blue);box-shadow:0 0 0 1px var(--primary-blue)}
        .step.done,.step.active {color:var(--ink)}.progress-note {margin-top:12px;color:var(--ink);font-size:14px}
        .post-card {display:grid;grid-template-columns:52px minmax(0,1fr) auto;gap:12px;border:1px solid var(--line);border-radius:12px;background:#fff;padding:10px 14px;margin-bottom:8px;transition:border-color .15s ease,box-shadow .15s ease}
        .post-card:hover {border-color:#c5d2e6;box-shadow:var(--shadow)}
        .post-thumb {width:52px;height:52px;border-radius:9px;background:var(--soft-blue);display:grid;place-items:center;color:var(--primary-blue);font-weight:750;font-size:13px}
        .post-title,.post-title-link {color:var(--ink);font-size:15px;font-weight:650;text-decoration:none}.post-title-link:hover {color:var(--primary-blue)}
        .post-snippet {margin-top:4px;color:var(--muted);font-size:13px;line-height:1.45;display:-webkit-box;-webkit-line-clamp:1;-webkit-box-orient:vertical;overflow:hidden}
        .post-side {min-width:128px;display:flex;flex-direction:column;align-items:flex-end;justify-content:space-between;text-align:right}.post-side.no-pill {justify-content:flex-end}.post-stats {margin:0;color:var(--muted);font-size:12px;white-space:nowrap}
        .pill {display:inline-flex;padding:4px 9px;border-radius:999px;background:var(--soft-blue);color:var(--primary-blue);font-size:11px;font-weight:700}
        .pill.good {background:#eaf8f0;color:#177a50}.pill.warn {background:#fff1f1;color:var(--red)}
        .sidebar-card {border:1px solid var(--line);border-radius:13px;background:#fff;padding:18px;margin-bottom:14px;box-shadow:none}
        .sidebar-card h3 {margin:0 0 13px}.overview-row {padding:11px 0;border-top:1px solid #edf1f7}.overview-row:first-of-type {border-top:0}
        .overview-row strong {display:block;color:var(--ink);font-size:20px}.overview-row span {color:var(--muted);font-size:12px}
        .library-row {border:1px solid var(--line);border-radius:12px;background:#fff;padding:16px 18px;margin-bottom:10px}
        .library-row h3 {margin:0 0 4px}.library-meta {color:var(--muted);font-size:12px}
        .empty-state {padding:72px 8px 92px;text-align:center}.empty-state h3 {margin:0 0 6px;color:var(--ink)}.empty-state p {margin:0;color:var(--muted)}
        .report-frame-note {padding:10px 14px;border-radius:10px;background:var(--soft-blue);color:var(--ink);font-size:13px;margin-bottom:12px}
        @media(max-width:900px){.block-container{padding:1rem}.brand-lockup{min-width:auto}.brand-lockup img{width:210px}.hero-shell{padding:38px 20px 0}.hero-shell h1{font-size:34px!important}div[data-testid="stVerticalBlockBorderWrapper"]:has(.st-key-run_post_scroll),.st-key-run_post_scroll{height:auto!important;max-height:none!important;overflow:visible!important}.post-card{grid-template-columns:52px 1fr}.post-side{grid-column:2;align-items:flex-start;text-align:left}}
        </style>
        """,
        unsafe_allow_html=True,
    )


def _go(surface: str, run_id: int | None = None) -> None:
    st.session_state.surface = surface
    if run_id is not None:
        st.session_state.selected_run_id = int(run_id)
    st.rerun()


def _render_ui_preview() -> None:
    """Render local mock states without importing or calling the backend service."""

    st.session_state.authenticated = True
    st.session_state.auth_username = "preview"
    state = st.selectbox(
        "UI Preview state",
        PREVIEW_STATES,
        key="ui_preview_state",
        help="Developer-only selector. No network, database, LLM, or report-library writes occur.",
    )
    st.caption("🧪 Local UI Preview Mode · mock data only · nothing is saved")
    st.session_state.surface = "home" if state == "Home" else "report" if state == "Final Report" else "run"
    _render_app_nav()

    if state == "Home":
        _render_home_surface(preview=True)
        return
    if state == "Final Report":
        _render_preview_report()
        return

    posts = build_preview_posts()
    if state == "Searching":
        posts = posts[:8]
    elif state == "No Results":
        posts = []
    if state in {"Searching", "Search Completed / Processing", "Analyzing", "Error"}:
        posts = [
            {
                **row,
                "is_scope_relevant": None,
                "hku_relevance": "",
                "topic_relevance": "",
                "content_type": "",
                "primary_narrative": "",
                "narrative_stance": "",
                "has_uncertainty": None,
                "signal_label": "",
            }
            for row in posts
        ]
    run = build_preview_run(state)
    analysis = build_preview_analysis(posts) if state == "Analysis Completed / Generating Report" else {}
    _render_run_content(run, posts, analysis=analysis, ui_phase=state, preview=True)


def _render_preview_report() -> None:
    """Use the production report renderer and iframe with entirely in-memory data."""

    from xhs_listener.report import build_report_html

    zh = _lang() == "zh"
    structured, analysis, processing = build_preview_report()
    rendered = build_report_html(
        structured,
        analysis,
        processing,
        "2026-08-19 10:08:48",
        locale="zh" if zh else "en",
    )
    st.markdown(
        f'<div class="page-title"><h1>{html.escape(structured["title"])}</h1>'
        f'<p>{"内存 mock 数据通过正式报告 renderer 生成；不会保存到报告库。" if zh else "In-memory mock data rendered by the production report component; nothing is saved."}</p></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        f'<div class="report-frame-note">{"Preview Mode：报告布局、CSS 和生产环境一致。" if zh else "Preview Mode: report layout and CSS match production."}</div>',
        unsafe_allow_html=True,
    )
    encoded = base64.b64encode(rendered.encode("utf-8")).decode("ascii")
    _render_report_iframe(f"data:text/html;base64,{encoded}")


def _render_report_iframe(source: str) -> None:
    """Render the report document identically in production and Preview Mode.

    scrolling=True 是关键：报告长度差异很大（Topic 报告和带竞对分析的 Broad
    报告能差好几倍），固定高度 + 不开滚动会让内容在 1800px 处被硬裁掉、
    下面的内容完全看不到也没有任何提示。开了滚动之后，超出高度的部分永远
    可以滚动看到，不会被静默截断。
    """

    components.iframe(source, height=1800, scrolling=True)


def _render_app_nav() -> None:
    brand, home, reports, help_col, spacer, language, account = st.columns([3.0, 1.0, 1.25, .9, 2.2, 1.0, 1.4], vertical_alignment="center")
    with brand:
        logo = _asset_data_url("hku-business-school-logo.png")
        st.markdown(
            f'<div class="brand-lockup"><img src="{logo}" alt="HKU Business School"></div>',
            unsafe_allow_html=True,
        )
    labels = {
        "home": "首页" if _lang() == "zh" else "Home",
        "reports": "报告库" if _lang() == "zh" else "Reports Library",
        "help": "帮助" if _lang() == "zh" else "Help",
    }
    current_surface = str(st.session_state.get("surface") or "home")
    active_nav = {
        "home": "home",
        "run": "home",
        "reports": "reports",
        "report": "reports",
        "help": "help",
    }.get(current_surface, "home")
    for col, surface in ((home, "home"), (reports, "reports"), (help_col, "help")):
        with col:
            if st.button(labels[surface], type="primary" if active_nav == surface else "tertiary", use_container_width=True, key=f"nav_{surface}"):
                _go(surface)
    with language:
        next_lang = "en" if _lang() == "zh" else "zh"
        if st.button("中 / EN", use_container_width=True, key="nav_language"):
            st.session_state.lang = next_lang
            st.rerun()
    with account:
        user = st.session_state.get("auth_username") or "HKU"
        account_name = "HKU User" if user in {"", "HKU", "preview"} else user
        st.markdown(f'<div class="account-pill"><span class="account-avatar">●</span>{html.escape(account_name)}</div>', unsafe_allow_html=True)
    st.markdown('<div style="border-bottom:1px solid #dbe3f0;margin:0 0 22px"></div>', unsafe_allow_html=True)


def _render_home_surface(*, preview: bool = False) -> None:
    zh = _lang() == "zh"
    st.markdown(
        f'''<section class="hero-shell">
        <h1>{"HKU Business School 洞察" if zh else "HKU Business School Insights"}</h1>
        <p>{"搜索小红书公开讨论，自动生成洞察报告，助力决策与研究。" if zh else "Search public RED discussions and generate insight reports for decision-making and research."}</p></section>''',
        unsafe_allow_html=True,
    )
    search_left, search_center, search_right = st.columns([1, 2.5, 1], gap="small")

    with search_center:
        current_mode = str(st.session_state.get("home_search_mode") or "topic_scan")
        if current_mode == "topic_scan":
            topic = st.text_input(
                "搜索主题" if zh else "Search topic",
                key="home_topic",
                placeholder=(
                    "搜索你想了解的话题，例如：港大选课、HKU Capstone"
                    if zh
                    else "Search a topic, e.g. HKU course selection"
                ),
                label_visibility="collapsed",
                icon=":material/search:",
            ).strip()
        else:
            topic = ""
            st.markdown(
                f'''<div class="broad-search-note"><div class="broad-icon">◎</div><div>
                <strong>{"Weekly Monitoring：港大商学院周度公开讨论" if zh else "Weekly Monitoring: HKU Business School public discussion"}</strong>
                <span>{"无需输入关键词，系统使用预设 HKUBS 关键词采集本周样本。" if zh else "No keyword required. The system uses the predefined HKUBS weekly keyword set."}</span>
                </div></div>''',
                unsafe_allow_html=True,
            )

        if current_mode == "broad_scan":
            mode_col, fixed_col = st.columns([2.7, 1.4], gap="small", vertical_alignment="center")
            sample_col = None
            advanced_col = None
        else:
            mode_col, sample_col, advanced_col = st.columns([2.7, 1.25, 1.4], gap="small", vertical_alignment="center")
            fixed_col = None
        with mode_col:
            if hasattr(st, "segmented_control"):
                search_mode = st.segmented_control(
                    "搜索方式" if zh else "Search mode",
                    ["topic_scan", "broad_scan"],
                    default="topic_scan",
                    key="home_search_mode",
                    format_func=lambda value: "Topic Search" if value == "topic_scan" else "Weekly Monitoring",
                    label_visibility="collapsed",
                )
            else:
                search_mode = st.radio(
                    "搜索方式" if zh else "Search mode",
                    ["topic_scan", "broad_scan"],
                    key="home_search_mode",
                    format_func=lambda value: "Topic Search" if value == "topic_scan" else "Weekly Monitoring",
                    horizontal=True,
                    label_visibility="collapsed",
                )
            search_mode = str(search_mode or "topic_scan")
            mode_caption = (
                "临时分析一个具体话题"
                if zh and search_mode == "topic_scan"
                else "固定 HKUBS 周度社媒监测"
                if zh
                else "Ad-hoc analysis for one specific topic"
                if search_mode == "topic_scan"
                else "Recurring weekly monitoring for HKUBS"
            )
            st.markdown(f'<div class="mode-caption">{mode_caption}</div>', unsafe_allow_html=True)

        target = int(st.session_state.get("target_post_count") or DEFAULT_TARGET_POST_COUNT)
        if sample_col is not None:
            with sample_col:
                target = st.selectbox(
                    "样本数量" if zh else "Sample size",
                    SAMPLE_SIZE_OPTIONS,
                    index=SAMPLE_SIZE_OPTIONS.index(DEFAULT_TARGET_POST_COUNT),
                    format_func=lambda value: (
                        f"采集 {value} 条"
                        if zh
                        else f"Collect {value} posts"
                    ),
                    label_visibility="collapsed",
                    help=(
                        "这是采集上限，不是最终入报数量。经过既有 8 天报告窗口过滤、去重和相关性筛选后，"
                        "实际进入报告的帖子通常少于该数字。"
                        if zh
                        else "This is the collection cap, not a guarantee. After the existing 8-day reporting "
                        "reporting-window filter, deduplication and relevance filtering, fewer posts usually "
                        "reach the report."
                    ),
                )

        time_filter = "一周内"
        sort_type = "time_descending"
        content_scope = "posts_only"
        if fixed_col is not None:
            with fixed_col:
                st.caption("固定 8 天周报范围" if zh else "Fixed 8-day weekly scope")
        if advanced_col is not None:
            with advanced_col:
                with st.popover("高级选项" if zh else "Advanced options", use_container_width=True):
                    time_filter = st.selectbox(
                        "发布时间" if zh else "Posted within",
                        TIME_FILTER_OPTIONS,
                        index=TIME_FILTER_OPTIONS.index(st.session_state.time_filter),
                        format_func=lambda value: TIME_FILTER_LABELS[_lang()].get(value, value),
                    )
                    sort_type = st.selectbox(
                        "排序" if zh else "Sort",
                        ["time_descending", "relevance"],
                        index=0,
                        format_func=lambda value: SORT_LABELS[_lang()].get(value, value),
                    )
                    content_scope = st.selectbox(
                        "内容范围" if zh else "Content scope",
                        ["posts_only", "top_comments", "all_comments"],
                        format_func=lambda value: CONTENT_SCOPE_LABELS[_lang()].get(value, value),
                    )

        submit_left, submit_center, submit_right = st.columns([1.45, 1, 1.45])
        with submit_center:
            st.markdown('<span class="home-submit-anchor"></span>', unsafe_allow_html=True)
            submitted = st.button(
                "生成洞察" if zh else "Generate Insights",
                type="primary",
                use_container_width=True,
                disabled=search_mode == "topic_scan" and not bool(topic),
                key="home_search_submit",
            )

        if submitted:
            if preview:
                st.session_state.ui_preview_state = "Searching"
                st.rerun()
                return
            state_updates = dict(
                keyword=topic,
                mode=search_mode,
                time_filter=time_filter,
                sort_type=sort_type,
                content_scope=content_scope,
            )
            if search_mode == "topic_scan":
                state_updates["target_post_count"] = target
            st.session_state.update(**state_updates)
            try:
                run = backend.create_run(_strip_none(_build_payload()))
                _go("run", int(run["id"]))
            except Exception as exc:  # noqa: BLE001
                st.error(("搜索未能开始：" if zh else "Search could not start: ") + str(exc))



def _render_run_surface(runs: list[dict[str, Any]]) -> None:
    zh = _lang() == "zh"
    run = _selected_run(runs, st.session_state.get("selected_run_id"))
    if not run:
        st.info("暂无可查看的搜索。" if zh else "There is no search to show yet.")
        if st.button("返回首页" if zh else "Back home"):
            _go("home")
        return
    rows = _note_rows_for_run(run)
    _render_run_content(run, rows)


def _render_run_content(
    run: dict[str, Any],
    rows: list[dict[str, Any]],
    *,
    analysis: dict[str, Any] | None = None,
    ui_phase: str | None = None,
    preview: bool = False,
) -> None:
    """Shared production/preview renderer for all post-search states."""

    zh = _lang() == "zh"
    phase = _resolve_ui_phase(run, ui_phase)
    relevance_complete = _has_complete_relevance_labels(rows)
    if not preview:
        nav_left, nav_right = st.columns([3, 1])
        if nav_left.button("← 返回首页" if zh else "← Back to Home", key="run_back"):
            _go("home")
        if phase in ACTIVE_PHASES:
            # 手动兜底：状态区的 fragment 轮询覆盖绝大多数情况，但没有任何
            # 客户端定时器能在标签页被完全挂起/节流时存活。页面看起来卡住时，
            # 这个按钮总是有效 —— 它开一条新连接、直接从 sqlite 重读真实状态。
            if nav_right.button("🔄 " + ("刷新状态" if zh else "Refresh status"), key="run_manual_refresh", use_container_width=True):
                st.rerun()
    title = _task_display_name(run).split("·", 1)[-1].strip()
    # 标题保持静态：任务运行期间不参与刷新。
    st.markdown(f'<div class="page-title"><h1>{html.escape(title)}</h1></div>', unsafe_allow_html=True)
    if not preview and phase in ACTIVE_PHASES:
        # 只有这一块在轮询刷新；页头、导航、搜索表单都不重绘。
        _render_live_progress(int(run["id"]))
    else:
        relevant = _relevant_note_count(rows) if relevance_complete else len(rows)
        summary = _run_phase_summary(phase, len(rows), relevant, relevance_complete)
        st.markdown(f'<p class="run-live-summary">{html.escape(summary)}</p>', unsafe_allow_html=True)
        _render_progress_strip(phase)
    if phase == "error":
        st.error(str(run.get("error") or ("搜索或分析遇到问题。" if zh else "Search or analysis encountered an error.")))
        if not preview and run.get("run_dir"):
            if st.button("从中断处继续" if zh else "Continue from interrupted step", type="primary", use_container_width=True):
                try:
                    backend.analyze_existing_run(int(run["id"]), {"force_reanalyze": False, "resume_from": "auto"})
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(("继续运行失败：" if zh else "Resume failed: ") + str(exc))
    if preview or phase not in ACTIVE_PHASES:
        # 任务运行中时，结果区（帖子列表 + 概览侧栏）已经在上面的状态区 fragment
        # 里跟着每次 tick 一起重绘了；这里只在预览模式或任务已经结束时静态渲染一次。
        _render_results_section(run, rows, phase, relevance_complete=relevance_complete, analysis=analysis)
    if phase == "completed" and run.get("report_html") and not preview:
        st.success("报告已生成并保存到报告库。" if zh else "The report is ready and saved to the Reports Library.")
        c1, c2 = st.columns(2)
        if c1.button("查看洞察报告" if zh else "View Insights Report", type="primary", use_container_width=True):
            _go("report", int(run["id"]))
        if c2.button("开始新搜索" if zh else "Start New Search", use_container_width=True):
            _go("home")


def _render_results_section(
    run: dict[str, Any],
    rows: list[dict[str, Any]],
    phase: str,
    *,
    relevance_complete: bool,
    analysis: dict[str, Any] | None,
) -> None:
    """帖子列表 + 概览侧栏。

    任务运行中时，这个函数由状态区 fragment（``_render_live_progress``）每次
    轮询 tick 都重新调用一次，传入重新读取的 ``rows`` —— 这样"已找到帖子"和
    右侧概览才会跟着采集/分析实时更新，而不是停在页面加载那一刻的快照。
    任务结束（或预览模式）时，由外层静态渲染一次即可。
    """

    zh = _lang() == "zh"
    left, right = st.columns([3.25, 1], gap="large")
    with left:
        h1, h2 = st.columns([3, 1], vertical_alignment="bottom")
        if phase == "searching":
            result_heading = "已找到帖子" if zh else "Posts Found"
        else:
            result_heading = "搜索结果" if zh else "Search Results"
        h1.markdown(f"## {result_heading} ({len(rows)})")
        sort_options = ["engagement", "relevance"] if relevance_complete else ["engagement"]
        sort_key = f"run_sort_{run['id']}"
        if st.session_state.get(sort_key) not in sort_options:
            st.session_state[sort_key] = "engagement"
        sort = h2.selectbox(
            "排序" if zh else "Sort",
            sort_options,
            format_func=lambda value: ("按互动量" if value == "engagement" else "按相关度") if zh else ("Engagement" if value == "engagement" else "Relevance"),
            label_visibility="collapsed",
            key=sort_key,
        )
        ordered = sorted(rows, key=lambda row: _post_sort_key(row, sort), reverse=True)
        if not ordered:
            if phase == "no_results":
                st.warning("没有找到符合当前条件的公开帖子。可以尝试扩大时间范围或调整关键词。" if zh else "No public posts matched these filters. Try a wider date range or a different keyword.")
            elif phase != "error":
                st.info("正在搜索公开帖子，结果会自动显示在这里。" if zh else "Searching public posts. Results will appear here automatically.")
        if ordered:
            with st.container(height=560, border=False, key="run_post_scroll"):
                for row in ordered:
                    _render_post_card(row, relevance_complete=relevance_complete)
    with right:
        _render_run_sidebar(run, rows, phase, relevance_complete=relevance_complete, analysis=analysis)
        with st.expander("运行详情" if zh else "Run details", expanded=False):
            readable_events = _business_events(run)[-8:]
            if readable_events:
                st.markdown(f"##### {'业务进度' if zh else 'Progress'}")
                for item in readable_events:
                    st.caption(f"{item['time']} · {item['message']}")
            st.markdown(f"##### {'技术信息' if zh else 'Technical details'}")
            _render_technical_detail(run)


_UI_PHASE_OVERRIDES = {
    "Searching": "searching",
    "Search Completed / Processing": "processing",
    "Analyzing": "analyzing",
    "Analysis Completed / Generating Report": "generating",
    "Final Report": "completed",
    "No Results": "no_results",
    "Error": "error",
}


def _resolve_ui_phase(run: dict[str, Any], ui_phase: str | None = None) -> str:
    """Resolve one UI phase from either a preview override or real run state."""

    if ui_phase:
        override = _UI_PHASE_OVERRIDES.get(str(ui_phase), str(ui_phase).strip().lower())
        if override in {"searching", "processing", "analyzing", "generating", "completed", "no_results", "error"}:
            return override

    status = str(run.get("status") or "").lower()
    step = str(run.get("current_step") or "").lower()
    error = str(run.get("error") or "").lower()
    if status == "failed":
        credential_error = any(marker in error for marker in ("token expired", "令牌已过期", "unauthorized", "401", "403"))
        no_results = not credential_error and (
            error.strip() == "collection produced 0 notes"
            or any(marker in error for marker in ("没有找到", "no matching"))
        )
        return "no_results" if no_results else "error"
    if status == "stopped":
        return "error"
    if run.get("report_html") or status == "succeeded":
        return "completed"
    # top10_comments / competitors 跑在 analyze 之后、report 之前。
    # 不列进来的话会掉到最后的 "searching"，进度条在采集早就结束后倒退回“正在搜索”。
    if step in {"report", "top10_comments", "competitors"}:
        return "generating"
    if step == "analyze":
        return "analyzing"
    if step == "process" or status == "collected":
        return "processing"
    return "searching"


def _run_phase_summary(phase: str, total: int, relevant: int, relevance_complete: bool) -> str:
    zh = _lang() == "zh"
    excluded = max(0, total - relevant)
    if phase == "searching":
        return f"已找到 {total} 条 · 正在继续搜索" if zh else f"Found {total} posts · continuing search"
    if phase == "processing":
        return f"找到 {total} 条 · 正在整理内容" if zh else f"Found {total} posts · preparing content"
    if phase == "analyzing" and not relevance_complete:
        return f"找到 {total} 条 · 正在筛选并分析内容" if zh else f"Found {total} posts · filtering and analyzing content"
    if phase in {"analyzing", "generating"} and relevance_complete:
        return (
            f"找到 {total} 条 · 排除 {excluded} 条 · 纳入分析 {relevant} 条"
            if zh
            else f"Found {total} posts · excluded {excluded} · included {relevant}"
        )
    if phase == "generating":
        return "内容分析已完成 · 正在生成报告" if zh else "Content analysis complete · generating report"
    if phase == "completed":
        return "洞察报告已生成" if zh else "Insights report generated"
    if phase == "no_results":
        return "本次搜索没有找到符合条件的帖子" if zh else "No matching posts were found"
    return "处理遇到问题" if zh else "Processing encountered an error"


def _note_rows_for_run(run: dict[str, Any]) -> list[dict[str, Any]]:
    if not run.get("run_dir"):
        return []
    try:
        return backend.get_run_notes(int(run["id"])).get("rows") or []
    except Exception:  # noqa: BLE001
        return []


def _post_sort_key(row: dict[str, Any], sort: str) -> float:
    if sort == "relevance":
        return float(_is_relevant_row(row)) * 1_000_000 + _weighted_engagement(row)
    return _weighted_engagement(row)


def _weighted_engagement(row: dict[str, Any]) -> int:
    return int(row.get("like_count") or 0) + 2 * int(row.get("collect_count") or 0) + 3 * int(row.get("comment_count") or 0) + int(row.get("share_count") or 0)


def _render_post_card(row: dict[str, Any], *, relevance_complete: bool) -> None:
    zh = _lang() == "zh"
    title = html.escape(str(row.get("title") or ("未命名帖子" if zh else "Untitled post")))
    snippet = html.escape(str(row.get("body_preview") or "-").replace("\n", " "))
    relevant = _is_relevant_row(row) if relevance_complete else None
    url = _public_safe_url(row.get("post_url"))
    title_html = (
        f'<a class="post-title-link" href="{html.escape(url, quote=True)}" target="_blank" rel="noreferrer">{title}</a>'
        if url
        else f'<span class="post-title">{title}</span>'
    )
    pill_html = ""
    if relevant is not None:
        pill_class = "good" if relevant else "warn"
        pill_text = ("高相关" if relevant else "已排除") if zh else ("Highly relevant" if relevant else "Excluded")
        pill_html = f'<span class="pill {pill_class}">{pill_text}</span>'
    likes_title = "点赞" if zh else "Likes"
    saves_title = "收藏" if zh else "Saves"
    comments_title = "评论" if zh else "Comments"
    side_class = "post-side" if pill_html else "post-side no-pill"
    st.markdown(
        f'''<article class="post-card"><div class="post-thumb">XHS</div><div>{title_html}<div class="post-snippet">{snippet}</div></div><div class="{side_class}">{pill_html}<div class="post-stats"><span title="{likes_title}">♡ {_fmt(row.get("like_count"))}</span> &nbsp; <span title="{saves_title}">☆ {_fmt(row.get("collect_count"))}</span> &nbsp; <span title="{comments_title}">◯ {_fmt(row.get("comment_count"))}</span></div></div></article>''',
        unsafe_allow_html=True,
    )


def _public_safe_url(value: Any) -> str:
    url = str(value or "").strip()
    return url if url.startswith(("https://", "http://")) else ""


def _html_data_url(path: Path) -> str:
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:text/html;base64,{encoded}"


def _asset_data_url(name: str) -> str:
    path = ROOT / "assets" / name
    if not path.exists():
        return ""
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _auto_start_analysis(run: dict[str, Any]) -> None:
    if str(run.get("status") or "") != "collected" or not run.get("run_dir"):
        return
    started = {int(value) for value in st.session_state.get("auto_analysis_started", [])}
    run_id = int(run["id"])
    if run_id in started:
        return
    try:
        backend.analyze_existing_run(run_id, {"force_reanalyze": False})
        started.add(run_id)
        st.session_state.auto_analysis_started = sorted(started)
    except Exception as exc:  # noqa: BLE001
        st.error(("自动分析未能开始：" if _lang() == "zh" else "Automatic analysis could not start: ") + str(exc))


def _render_progress_strip(phase: str) -> None:
    zh = _lang() == "zh"
    labels = ["搜索与整理", "筛选与分析", "生成报告"] if zh else ["Search & Prepare", "Filter & Analyze", "Generate Report"]
    progress = {
        "searching": (["active", "", ""], "正在搜索相关公开帖子。" if zh else "Searching for relevant public posts."),
        "processing": (["active", "", ""], "正在清洗、去重并整理搜索结果。" if zh else "Cleaning, deduplicating and preparing search results."),
        "analyzing": (["done", "active", ""], "正在判断主题相关性，并识别主要讨论、情绪与代表性证据。" if zh else "Assessing topic relevance and identifying key discussions, sentiment and representative evidence."),
        "generating": (["done", "done", "active"], "内容分析已完成，正在生成洞察报告。" if zh else "Content analysis is complete. Generating the insights report."),
        "completed": (["done", "done", "done"], "报告已生成，并自动保存到报告库。" if zh else "The report is ready and has been saved to the Reports Library."),
        "no_results": (["done", "", ""], "搜索已完成，但没有找到符合条件的帖子。" if zh else "Search finished without matching posts."),
        "error": (["done", "active", ""], "处理遇到问题，请检查错误信息后重试。" if zh else "Something went wrong. Review the error and try again."),
    }
    states, message = progress.get(phase, progress["searching"])
    steps = "".join(f'<div class="step {state}"><span class="step-dot"></span>{label}</div>' for state, label in zip(states, labels))
    st.markdown(f'<div class="progress-shell"><div class="steps">{steps}</div><div class="progress-note">{message}</div></div>', unsafe_allow_html=True)


def _render_run_sidebar(
    run: dict[str, Any],
    rows: list[dict[str, Any]],
    phase: str,
    *,
    relevance_complete: bool,
    analysis: dict[str, Any] | None = None,
) -> None:
    zh = _lang() == "zh"
    if analysis is None and phase in {"generating", "completed"}:
        analysis = _load_run_analysis(run)
    analysis = analysis or {}
    scope = analysis.get("generated_scope") if isinstance(analysis.get("generated_scope"), dict) else {}
    if not scope and isinstance(analysis.get("time_coverage"), dict):
        scope = analysis["time_coverage"]
    earliest_date = str(scope.get("earliest_post_date") or "").strip()
    latest_date = str(scope.get("latest_post_date") or "").strip()
    date_range: str | None = None
    if earliest_date and latest_date:
        date_range = f"{earliest_date} – {latest_date}"
    elif earliest_date or latest_date:
        date_range = earliest_date or latest_date
    relevant = _relevant_note_count(rows) if relevance_complete else len(rows)
    excluded = max(0, len(rows) - relevant)
    visible_rows = [row for row in rows if _is_relevant_row(row)] if relevance_complete else rows
    engagement = sum(_weighted_engagement(row) for row in visible_rows)
    metrics: list[tuple[str, str, bool]] = []
    if phase == "searching":
        metrics = [
            (_fmt(len(rows)), "已找到帖子" if zh else "posts found", False),
            (_fmt(engagement), "当前互动量" if zh else "current engagement", False),
            ("搜索中" if zh else "Searching", "搜索状态" if zh else "search status", True),
        ]
    elif phase == "processing":
        metrics = [
            (_fmt(len(rows)), "找到帖子" if zh else "posts found", False),
            (_fmt(engagement), "当前互动量" if zh else "current engagement", False),
        ]
        if date_range:
            metrics.append((date_range, "内容时间范围" if zh else "content date range", True))
    elif phase == "analyzing":
        metrics = [
            (_fmt(relevant if relevance_complete else len(rows)), "纳入分析" if zh and relevance_complete else "待分析帖子" if zh else "included posts" if relevance_complete else "posts being analyzed", False),
        ]
        if relevance_complete:
            metrics.append((_fmt(excluded), "已排除帖子" if zh else "excluded posts", False))
        metrics.append((_fmt(engagement), "加权互动量" if zh else "weighted engagement", False))
        if date_range:
            metrics.append((date_range, "内容时间范围" if zh else "content date range", True))
        metrics.append(("分析中" if zh else "Analyzing", "当前状态" if zh else "current status", True))
    elif phase in {"generating", "completed"}:
        sentiment = _dominant_sentiment(analysis)
        metrics = [
            (_fmt(relevant), "纳入分析的帖子" if zh else "included posts", False),
            (_fmt(excluded), "已排除帖子" if zh else "excluded posts", False),
            (_fmt(engagement), "加权互动量" if zh else "weighted engagement", False),
        ]
        if date_range:
            metrics.append((date_range, "内容时间范围" if zh else "content date range", True))
        if sentiment:
            metrics.append((sentiment, "整体情绪倾向" if zh else "overall sentiment", True))
    else:
        metrics = [(_fmt(len(rows)), "找到帖子" if zh else "posts found", False)]
    overview = "".join(
        f'<div class="overview-row"><strong{(" style=\"font-size:15px\"" if compact else "")}>{html.escape(value)}</strong><span>{label}</span></div>'
        for value, label, compact in metrics
    )
    st.markdown(
        f'<div class="sidebar-card"><h3>{"本次搜索概览" if zh else "Search Overview"}</h3>{overview}</div>',
        unsafe_allow_html=True,
    )


def _dominant_sentiment(analysis: dict[str, Any]) -> str | None:
    summary = analysis.get("annotation_summary") if isinstance(analysis.get("annotation_summary"), dict) else {}
    counts = summary.get("sentiment") if isinstance(summary.get("sentiment"), dict) else {}
    if not counts:
        return None
    value = str(max(counts, key=lambda key: int(counts.get(key) or 0)))
    labels = {
        "positive": ("正面", "Positive"),
        "neutral": ("中性", "Neutral"),
        "negative": ("负面", "Negative"),
        "mixed": ("褒贬不一", "Mixed"),
    }
    zh, en = labels.get(value, (value, value))
    return zh if _lang() == "zh" else en


def _render_reports_library(runs: list[dict[str, Any]], reports: list[dict[str, Any]]) -> None:
    zh = _lang() == "zh"
    st.markdown(f'<div class="page-title"><h1>{"报告库" if zh else "Reports Library"}</h1><p>{"搜索、浏览并打开所有已保存的洞察报告。" if zh else "Search, browse and open every saved insight report."}</p></div>', unsafe_allow_html=True)
    c1, c2 = st.columns([2.6, 1.4])
    query = c1.text_input("搜索报告" if zh else "Search reports", placeholder="输入主题" if zh else "Search by topic", label_visibility="collapsed").strip().lower()
    ordering = c2.selectbox("排序" if zh else "Sort", ["newest", "oldest", "posts"], format_func=lambda value: {"newest": "最新优先" if zh else "Newest", "oldest": "最早优先" if zh else "Oldest", "posts": "帖子数" if zh else "Post count"}[value], label_visibility="collapsed")
    report_rows = list(reports)
    if query:
        report_rows = [row for row in report_rows if query in _task_display_name(row).lower()]
    if ordering == "oldest":
        report_rows.reverse()
    elif ordering == "posts":
        report_rows.sort(key=lambda row: int(row.get("collect_notes_count") or 0), reverse=True)
    if not report_rows:
        empty_title = ("没有找到匹配的报告" if zh else "No matching reports") if query else ("暂无报告" if zh else "No reports yet")
        empty_body = ("尝试修改搜索关键词。" if zh else "Try a different search term.") if query else ("生成的洞察报告会显示在这里。" if zh else "Generated insights reports will appear here.")
        st.markdown(
            f'<div class="empty-state"><h3>{empty_title}</h3><p>{empty_body}</p></div>',
            unsafe_allow_html=True,
        )
    for run in report_rows:
        text, actions = st.columns([4.8, 1.4], vertical_alignment="center")
        with text:
            st.markdown(f'<div class="library-row"><h3>{html.escape(_task_display_name(run))}</h3><div class="library-meta">{html.escape(_compact_time(run.get("finished_at") or run.get("created_at")))} · {_fmt(run.get("collect_notes_count"))} {"条相关帖子" if zh else "relevant posts"} · {_status_label(str(run.get("status") or ""))}</div></div>', unsafe_allow_html=True)
        with actions:
            if st.button("打开报告" if zh else "Open report", key=f"library_open_{run['id']}", type="primary", use_container_width=True):
                _go("report", int(run["id"]))
    with st.expander("进行中 / 未完成的搜索" if zh else "Searches in progress / unfinished", expanded=False):
        # 包含 failed/stopped：这些任务在报告库里看不到（还没有 report_html），
        # 之前唯一的入口就是这里 —— 否则用户点不进失败任务的"从中断处继续"按钮。
        pending = [
            row
            for row in runs
            if row.get("status") in {"queued", "running", "collected", "failed", "stopped"}
        ]
        if not pending:
            st.caption("暂无" if zh else "None")
        for run in pending:
            if st.button(_run_option_label(run), key=f"pending_{run['id']}"):
                _go("run", int(run["id"]))


def _report_download_name(run: dict[str, Any]) -> str:
    """报告下载文件名：任务名 + 完成时间，去掉文件系统不友好的字符。"""

    import re as _re

    stem = _re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "_", _task_display_name(run)).strip("_") or "report"
    stamp = _re.sub(r"[^0-9]", "", str(run.get("finished_at") or run.get("created_at") or ""))[:14]
    return f"{stem}_{stamp}" if stamp else stem


def _render_report_surface(runs: list[dict[str, Any]]) -> None:
    zh = _lang() == "zh"
    run = _selected_run(runs, st.session_state.get("selected_run_id"))
    if not run or not run.get("report_html"):
        st.warning("这次搜索还没有可查看的报告。" if zh else "This search does not have a report yet.")
        if run and st.button("查看分析进度" if zh else "View analysis progress"):
            _go("run", int(run["id"]))
        return
    if st.button("← 返回报告库" if zh else "← Back to Reports Library", key="report_back"):
        _go("reports")
    html_path = Path(str(run.get("report_html") or ""))
    if not html_path.exists():
        st.error("报告文件不存在。" if zh else "The report file is missing.")
        return
    title, download = st.columns([4.4, 1.4], vertical_alignment="center")
    with title:
        st.markdown(
            f'<div class="page-title"><h1>{html.escape(_task_display_name(run))}</h1>'
            f'<p>{"下载得到的是 PDF 版本，样式与下方在线报告完全一致。" if zh else "The download is a PDF version - same styling as the report shown below."}</p></div>',
            unsafe_allow_html=True,
        )
    with download:
        _render_report_download_button(
            run,
            html_path,
            key_suffix="zh",
            label="下载报告（PDF）" if zh else "Download report (PDF)",
            file_suffix="",
        )
    _render_english_report_section(run)
    _render_report_iframe(_html_data_url(html_path))


def _render_report_download_button(
    run: dict[str, Any],
    html_path: Path,
    *,
    key_suffix: str,
    label: str,
    file_suffix: str,
) -> None:
    """打印成 PDF 再给下载；report.html 没变就直接用缓存，不用每次都重新起 Chromium。

    如果本机 Chromium 还没装好或者渲染出于某种原因失败，退化成提供原始
    HTML 下载，而不是让用户什么都下不到。
    """

    zh = _lang() == "zh"
    from xhs_listener.pdf_export import PdfExportError, cached_report_pdf

    pdf_path = html_path.with_suffix(".pdf")
    try:
        with st.spinner(
            "首次生成需要准备 PDF 渲染引擎，可能需要一两分钟…" if zh
            else "First-time setup needs to prepare the PDF engine, this can take a minute or two…"
        ):
            pdf_bytes = cached_report_pdf(html_path, pdf_path)
        st.download_button(
            label,
            data=pdf_bytes,
            file_name=f"{_report_download_name(run)}{file_suffix}.pdf",
            mime="application/pdf",
            use_container_width=True,
            key=f"download_report_{key_suffix}_{run['id']}",
        )
    except PdfExportError as exc:
        st.error(("PDF 生成失败，可以先下载 HTML：" if zh else "PDF generation failed, you can download the HTML for now: ") + str(exc))
        st.download_button(
            "改为下载 HTML" if zh else "Download HTML instead",
            data=html_path.read_bytes(),
            file_name=f"{_report_download_name(run)}{file_suffix}.html",
            mime="text/html",
            use_container_width=True,
            key=f"download_report_html_fallback_{key_suffix}_{run['id']}",
        )


def _render_translation_coverage_note(run: dict[str, Any], *, zh: bool) -> None:
    """如果这次翻译有遗漏（个别批次失败后保留了中文原文），提示用户可以点
    "重新生成英文报告"再试一次——而不是让用户自己在报告里翻着找哪里还是
    中文，一头雾水。覆盖率写在 report_en.json 的 ``_translation_coverage``
    里，见 report.report_run_english。"""

    report_json_en = run.get("report_json_en")
    if not report_json_en:
        return
    path = Path(str(report_json_en))
    if not path.exists():
        return
    try:
        coverage = json.loads(path.read_text(encoding="utf-8")).get("_translation_coverage")
    except Exception:  # noqa: BLE001
        return
    if not isinstance(coverage, dict):
        return
    total = coverage.get("total_strings") or 0
    translated = coverage.get("translated_strings") or 0
    if not total or translated >= total:
        return
    missing = total - translated
    st.caption(
        f"⚠️ 本次翻译覆盖 {translated}/{total} 处，有 {missing} 处因翻译失败保留了中文原文；"
        "可点击下方“重新生成英文报告”再试一次。"
        if zh
        else f"⚠️ Translated {translated}/{total} strings; {missing} kept their original Chinese text "
        "because translation failed for that part. Click “Regenerate English report” below to retry."
    )


def _render_english_report_section(run: dict[str, Any]) -> None:
    """下载区下面的"生成英文版"小窗口：默认只有中文报告，英文版按需生成。

    英文版是翻译已有的中文 report.json，不重新跑 analyze——省 token，
    而且中英文版本的事实/结论完全一致。
    """

    zh = _lang() == "zh"
    with st.expander("英文版" if zh else "English version", expanded=False):
        st.caption(
            "由中文报告翻译得到，事实与结论保持一致；个别翻译失败的片段会保留中文原文。"
            if zh
            else "Translated from the Chinese report; a fragment keeps its original Chinese text if that part failed to translate."
        )
        en_html_path = Path(str(run.get("report_html_en") or ""))
        has_en = bool(run.get("report_html_en")) and en_html_path.exists()
        if not has_en:
            if st.button("生成英文报告" if zh else "Generate English report", key=f"gen_en_{run['id']}", type="primary"):
                try:
                    with st.spinner("正在翻译成英文…" if zh else "Translating to English…"):
                        backend.generate_english_report(int(run["id"]))
                    st.rerun()
                except Exception as exc:  # noqa: BLE001
                    st.error(("生成英文报告失败：" if zh else "Failed to generate the English report: ") + str(exc))
            return
        _render_translation_coverage_note(run, zh=zh)
        _render_report_download_button(
            run,
            en_html_path,
            key_suffix="en",
            label="下载英文报告（PDF）" if zh else "Download English report (PDF)",
            file_suffix="_en",
        )
        if st.button("重新生成英文报告" if zh else "Regenerate English report", key=f"regen_en_{run['id']}"):
            try:
                with st.spinner("正在翻译成英文…" if zh else "Translating to English…"):
                    backend.generate_english_report(int(run["id"]))
                st.rerun()
            except Exception as exc:  # noqa: BLE001
                st.error(("重新生成失败：" if zh else "Regeneration failed: ") + str(exc))


def _render_help_surface() -> None:
    zh = _lang() == "zh"
    st.markdown(f'<div class="page-title"><h1>{"帮助中心" if zh else "Help Centre"}</h1><p>{"从搜索到报告，只需三个步骤。" if zh else "From search to report in three simple steps."}</p></div>', unsafe_allow_html=True)
    cols = st.columns(3)
    items = [
        ("01", "输入主题", "输入想了解的话题，并选择帖子数量与搜索范围。") if zh else ("01", "Enter a topic", "Choose a topic, post count and search scope."),
        ("02", "自动分析", "系统会持续展示找到的帖子，并自动进入分析。") if zh else ("02", "Automatic analysis", "Review found posts while analysis continues automatically."),
        ("03", "阅读报告", "报告完成后自动保存，可在线阅读或打印为 PDF。") if zh else ("03", "Read the report", "Completed reports are saved for web reading or PDF printing."),
    ]
    for col, (number, title, body) in zip(cols, items):
        col.markdown(f'<div class="surface-card"><div class="eyebrow">{number}</div><h3>{title}</h3><p class="muted">{body}</p></div>', unsafe_allow_html=True)


def _init_state() -> None:
    defaults = {
        "lang": "zh",
        "mode": "topic_scan",
        "keyword": "港大商学院选课",
        "target_post_count": DEFAULT_TARGET_POST_COUNT,
        "sort_type": "time_descending",
        "time_filter": "一周内",
        "content_scope": "posts_only",
        "selected_run_id": None,
        "surface": "home",
        "home_topic": "",
        "auto_analysis_started": [],
    }
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)


def _init_auth_state() -> None:
    st.session_state.setdefault("authenticated", False)
    st.session_state.setdefault("auth_username", "")


def _render_login_gate() -> bool:
    if st.session_state.get("authenticated"):
        return True

    # Require explicit APP_LOGIN_USERNAME / APP_LOGIN_PASSWORD; do not fall back to insecure defaults.
    if not APP_LOGIN_USERNAME or not APP_LOGIN_PASSWORD:
        st.error("App login credentials are not configured. Please set APP_LOGIN_USERNAME and APP_LOGIN_PASSWORD in the environment before using this tool.")
        return False

    lang = _lang()
    if lang == "zh":
        title = "访问验证"
        subtitle = "请输入用户名和密码后使用系统。"
        user_label = "用户名"
        pass_label = "密码"
        login_label = "登录"
        error_text = "用户名或密码错误。"
    else:
        title = "Access Verification"
        subtitle = "Please enter a username and password to use the system."
        user_label = "Username"
        pass_label = "Password"
        login_label = "Sign in"
        error_text = "Invalid username or password."

    left, center, right = st.columns([1.2, 1, 1.2])
    with center:
        st.markdown(f"### {title}")
        st.caption(subtitle)
        with st.form("login_form", clear_on_submit=False):
            username = st.text_input(user_label, value=st.session_state.get("auth_username", "")).strip()
            password = st.text_input(pass_label, type="password")
            submitted = st.form_submit_button(login_label, type="primary", use_container_width=True)

        if submitted:
            if username == APP_LOGIN_USERNAME and password == APP_LOGIN_PASSWORD:
                st.session_state.authenticated = True
                st.session_state.auth_username = username
                st.rerun()
            else:
                st.error(error_text)
    return False


def _build_payload() -> dict[str, Any]:
    comment_settings = _comment_settings_for_scope(st.session_state.content_scope)
    common = {
        "mode": st.session_state.mode,
    }
    if st.session_state.mode == "broad_scan":
        return {
            **common,
            # Broad 采集、周窗口与 Top-10 后置评论策略均使用后端单一默认配置。
            "broad_config": {},
        }
    return {
        **common,
        "collect_config": {
            "keyword": st.session_state.keyword,
            "max_pages": _pages_for_count(st.session_state.target_post_count),
            "max_notes": int(st.session_state.target_post_count or 1),
            "sort_type": st.session_state.sort_type,
            "time_filter": st.session_state.time_filter,
            "note_type": "普通笔记",
            "comment_pages": _pages_for_count(comment_settings["comments_per_post"]),
            "sub_comment_pages": 0,
            "comment_min_likes": 0,
            "comment_min_comments": comment_settings["min_comment_count"],
            "comment_policy": comment_settings["policy"],
            "comment_top_percent": comment_settings["top_percent"],
            "fetch_comments_for_top_notes": comment_settings["top_limit"],
        },
    }


def _comment_settings_for_scope(scope: str) -> dict[str, int | str]:
    if scope == "top_comments":
        return {
            "policy": "top_notes",
            "comments_per_post": TOP_COMMENTS_PER_POST,
            "top_percent": TOP_COMMENT_PERCENT,
            "top_limit": 0,
            "min_comment_count": TOP_COMMENT_MIN_COMMENTS,
        }
    if scope == "all_comments":
        return {
            "policy": "all",
            "comments_per_post": FULL_COMMENTS_PER_POST,
            "top_percent": 100,
            "top_limit": 0,
            "min_comment_count": 0,
        }
    return {
        "policy": "none",
        "comments_per_post": 0,
        "top_percent": 0,
        "top_limit": 0,
        "min_comment_count": 0,
    }


def _pages_for_count(count: Any, page_size: int = PAGE_SIZE) -> int:
    """Translate a user-facing item count into API page count."""

    try:
        normalized = int(count or 0)
    except (TypeError, ValueError):
        normalized = 0
    if normalized <= 0:
        return 0
    return max(1, math.ceil(normalized / page_size))


def _load_runs() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    try:
        runs = backend.list_runs(limit=50)
        reports = backend.list_reports(limit=50)
    except Exception as exc:  # noqa: BLE001
        st.error(_t("load_runs_failed").format(error=exc))
        return [], []
    return runs, reports


def _run_option_label(run: dict[str, Any]) -> str:
    status = _status_label(str(run.get("status") or ""))
    created = _compact_time(run.get("created_at"))
    return f"{_task_name_with_id(run)} ({status}, {created})"


def _task_name_with_id(run: dict[str, Any]) -> str:
    return f"#{run.get('id')} · {_task_display_name(run)}"


def _task_display_name(run: dict[str, Any]) -> str:
    mode = str(run.get("mode") or "topic_scan")
    lang = _lang()
    mode_label = MODE_LABELS.get(lang, MODE_LABELS["zh"]).get(mode, mode)
    config = run.get("config") if isinstance(run.get("config"), dict) else {}

    topic = ""
    if mode == "topic_scan":
        collect = config.get("collect_config") if isinstance(config, dict) else {}
        if isinstance(collect, dict):
            topic = str(collect.get("keyword") or "").strip()
    else:
        broad = config.get("broad_config") if isinstance(config, dict) else {}
        if isinstance(broad, dict):
            raw_pool = broad.get("keyword_pool_json")
            try:
                pool = json.loads(raw_pool) if isinstance(raw_pool, str) else raw_pool
            except Exception:
                pool = []
            if isinstance(pool, list):
                keywords = [str(row.get("keyword") or "").strip() for row in pool if isinstance(row, dict)]
                keywords = [item for item in keywords if item]
                if keywords:
                    topic = ", ".join(keywords[:3])
                    if len(keywords) > 3:
                        topic = f"{topic} +{len(keywords) - 3}"

    if not topic:
        topic = _compact_time(run.get("finished_at") or run.get("created_at"))
    return f"{mode_label} · {topic}"


def _selected_run(runs: list[dict[str, Any]], selected_id: int | None) -> dict[str, Any] | None:
    if not runs:
        return None
    if selected_id is None:
        return runs[0]
    return next((run for run in runs if int(run.get("id")) == int(selected_id)), runs[0])


def _load_run_analysis(run: dict[str, Any]) -> dict[str, Any]:
    path = Path(str(run.get("run_dir") or "")) / "analysis.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _business_events(run: dict[str, Any]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for event in run.get("events") or []:
        message = _business_event_message(str(event.get("message") or ""), str(event.get("step") or ""))
        if not message:
            continue
        if out and out[-1]["message"] == message:
            continue
        out.append({"time": _compact_time(event.get("time")), "message": message})
    return out


def _business_event_message(message: str, step: str) -> str:
    text = message.lower()
    zh = _lang() == "zh"
    mapping = [
        ("collect started", "开始获取小红书公开内容样本", "Started collecting RED posts"),
        ("collection completed", "样本获取完成，正在整理文本", "Collection finished; preparing text"),
        ("collect finished", "样本获取完成，正在整理文本", "Collection finished; preparing text"),
        ("process started", "正在清洗和去重帖子", "Cleaning and deduplicating posts"),
        ("process finished", "文本整理完成", "Text preparation finished"),
        ("analysis start", "开始筛选相关内容并识别讨论信号", "Started relevance filtering and signal labeling"),
        ("hku relevance code-gated", "正在用规则筛选港大相关内容", "Filtering HKU-relevant posts"),
        ("relevance gate", "正在判断主题相关性", "Checking topic relevance"),
        ("detailed annotation", "正在识别内容类型、情绪、作者类型和讨论叙事", "Labeling content type, sentiment, author type and narratives"),
        ("signal merge", "正在合并相似讨论信号", "Merging similar discussion signals"),
        ("semantic filter", "相关内容筛选完成", "Relevant content filtering finished"),
        ("report start", "正在生成报告", "Generating report"),
        ("report finished", "报告已生成", "Report generated"),
    ]
    for needle, cn, en in mapping:
        if needle in text:
            return cn if zh else en
    if step in {"collect", "process", "analyze", "report"} and "failed" in text:
        return ("任务遇到错误，请查看技术详情" if zh else "Task hit an error; see technical details")
    return ""


def _render_technical_detail(run: dict[str, Any] | None) -> None:
    if not run:
        return
    c1, c2, c3, c4 = st.columns(4)
    c1.metric(_t("metric_requests"), _fmt(run.get("collect_request_count")))
    c2.metric(_t("metric_cost"), f"${float(run.get('collect_cost_usd') or 0):.2f}")
    c3.metric(_t("metric_tokens"), _fmt(run.get("llm_usage_tokens") or run.get("usage_tokens")))
    c4.metric(_t("metric_step"), run.get("current_step") or "-")
    attempts = run.get("step_attempts") or {}
    st.caption(
        " | ".join(
            f"{step}: {attempts.get(step, 0)}"
            for step in ("collect", "process", "analyze", "top10_comments", "competitors", "report")
        )
    )
    st.write(
        {
            "run_dir": run.get("run_dir"),
            "created_at": run.get("created_at"),
            "finished_at": run.get("finished_at"),
            "stop_reason": run.get("stop_reason"),
            "error": run.get("error"),
        }
    )
    st.markdown(f"#### {_t('log_title')}")
    events = list(run.get("events") or [])
    if not events:
        st.caption(_t("no_logs"))
        return
    for event in reversed(events[-40:]):
        level = event.get("level") or "info"
        line = f"{_compact_time(event.get('time'))} [{event.get('step') or 'run'}] {event.get('message')}"
        if level == "error":
            st.error(line)
        elif level == "warning":
            st.warning(line)
        else:
            st.caption(line)


def _strip_none(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _strip_none(item) for key, item in value.items() if item is not None}
    if isinstance(value, list):
        return [_strip_none(item) for item in value]
    return value


def _compact_time(value: Any) -> str:
    if not value:
        return "-"
    return str(value).replace("T", " ")[5:16]


def _relevant_note_count(rows: list[dict[str, Any]]) -> int:
    return sum(1 for row in rows if _is_relevant_row(row))


def _has_complete_relevance_labels(rows: list[dict[str, Any]]) -> bool:
    candidates = [
        row
        for row in rows
        if row.get("is_scope_relevant") is not False
        and str(row.get("status") or "") not in {"filtered", "out_of_scope"}
    ]
    return bool(candidates) and all(row.get("hku_relevance") or row.get("topic_relevance") for row in candidates)


def _is_relevant_row(row: dict[str, Any]) -> bool:
    if row.get("is_scope_relevant") is False:
        return False
    hku = str(row.get("hku_relevance") or "").lower()
    topic = str(row.get("topic_relevance") or "").lower()
    if hku or topic:
        # 已有 LLM 标注时只认 direct/indirect，标了 unrelated 的不再按状态兜底。
        return hku in {"direct", "indirect"} or topic in {"direct", "indirect"}
    return str(row.get("status") or "") in {"collected", "processed"}


def _fmt(value: Any) -> str:
    try:
        return f"{int(value or 0):,}"
    except Exception:
        return "0"


if __name__ == "__main__":
    main()
