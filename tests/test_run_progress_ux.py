"""运行中状态区的渲染契约：只刷新状态区，不整页重跑。"""
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


def _rerun_calls(node: ast.AST) -> list[ast.Call]:
    return [
        call
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "rerun"
    ]


def test_polling_lives_in_a_fragment_not_in_main() -> None:
    """轮询必须挂在 fragment 上；main() 里不得再有全局轮询器。"""

    fragment = _func("_render_live_progress")
    decorators = [ast.unparse(d) for d in fragment.decorator_list]
    assert any("st.fragment" in d and "run_every" in d for d in decorators), decorators

    # 旧的整页轮询器已删除
    assert "_watch_active_runs" not in SOURCE
    main_body = ast.unparse(_func("main"))
    assert "run_every" not in main_body
    assert "_render_live_progress" not in main_body, "状态区 fragment 应挂在运行页内，而不是 main()"


def test_fragment_only_reruns_the_app_when_the_job_leaves_the_active_phase() -> None:
    """运行期间 fragment 自己 tick；只有跑完那一次才整页重跑。"""

    fragment = _func("_render_live_progress")
    calls = _rerun_calls(fragment)
    assert len(calls) == 1, "fragment 里只应有一次 st.rerun（running -> finished 的那次）"

    # 那一次 rerun 必须在 “phase 不再是活跃阶段” 的分支里
    guard = next(
        node
        for node in ast.walk(fragment)
        if isinstance(node, ast.If) and "ACTIVE_PHASES" in ast.unparse(node.test)
    )
    assert _rerun_calls(guard), "整页重跑必须由『离开活跃阶段』这个条件守住"
    assert "not in ACTIVE_PHASES" in ast.unparse(guard.test)


def test_no_rerun_is_reachable_on_a_polling_tick() -> None:
    """除完成那一次外，其余 st.rerun 都必须由用户交互（按钮/表单）触发。"""

    interactive = {
        "_go",
        "_render_app_nav",
        "_render_home_surface",
        "_render_run_content",
        "_render_login_gate",
        "_render_english_report_section",
    }
    for node in ast.walk(TREE):
        if not isinstance(node, ast.FunctionDef) or not _rerun_calls(node):
            continue
        if node.name == "_render_live_progress":
            continue
        assert node.name in interactive, f"{node.name} 里的 st.rerun 可能在轮询路径上"


def test_auto_start_analysis_does_not_force_a_full_rerun() -> None:
    """采集完成后接力启动分析，靠 fragment 的下一个 tick 读到新状态即可。"""

    assert not _rerun_calls(_func("_auto_start_analysis"))


def test_progress_area_reports_real_pipeline_state_without_fake_percentages() -> None:
    """展示真实阶段/步骤/已用时；后端没有可靠百分比就不编造百分比。"""

    from xhs_listener.run_manager import ANALYSIS_STEPS, BROAD_ANALYSIS_STEPS, COLLECT_STEPS

    body = ast.unparse(_func("_progress_meta")) + ast.unparse(_func("_pipeline_steps"))
    assert "current_step" in body
    assert "elapsed" in body.lower()
    for fake in ("percent", "%d%%", "progress_value"):
        assert fake not in body

    steps_src = ast.unparse(_func("_pipeline_steps"))
    for name in ("COLLECT_STEPS", "ANALYSIS_STEPS", "BROAD_ANALYSIS_STEPS"):
        assert name in steps_src
    # 步骤总数取自真实流水线定义
    assert len(COLLECT_STEPS) + len(BROAD_ANALYSIS_STEPS) == 6
    assert len(COLLECT_STEPS) + len(ANALYSIS_STEPS) == 4


def test_header_and_search_form_are_outside_the_polling_fragment() -> None:
    """页头、导航、搜索表单都不在状态区 fragment 内，运行期间不会重绘。"""

    fragment = ast.unparse(_func("_render_live_progress")) + ast.unparse(_func("_render_progress_body"))
    for outside in ("_render_app_nav", "_render_home_surface", "_render_login_gate", "page-title"):
        assert outside not in fragment


def test_live_progress_fragment_also_refreshes_the_results_section() -> None:
    """状态区 fragment 每次 tick 也要重新渲染帖子列表/侧栏，否则"已找到帖子"

    会一直停在页面加载那一刻的快照（很可能是 0），因为采集/分析进行中
    再也没有整页重跑去刷新它了。
    """

    fragment_src = ast.unparse(_func("_render_live_progress"))
    assert "_render_results_section" in fragment_src
    # 结果区必须用 fragment 自己重新读到的 rows，而不是外层传进来的旧快照。
    assert "_note_rows_for_run" in fragment_src

    results_section = _func("_render_results_section")
    assert not _rerun_calls(results_section)
    for outside in ("_render_app_nav", "_render_home_surface", "_render_login_gate"):
        assert outside not in ast.unparse(results_section)


def test_static_results_section_only_renders_once_run_leaves_active_phase() -> None:
    """任务运行中，结果区交给 fragment 负责；外层只在预览/结束态渲染一次。"""

    content_src = ast.unparse(_func("_render_run_content"))
    assert "_render_results_section" in content_src
    guard = next(
        node
        for node in ast.walk(_func("_render_run_content"))
        if isinstance(node, ast.If) and "_render_results_section" in ast.unparse(node)
    )
    assert "ACTIVE_PHASES" in ast.unparse(guard.test)


def test_failed_broad_run_can_resume_from_failed_competitor_step() -> None:
    """失败的周报不应废掉已完成的 process/analyze/top10 产物。"""

    from xhs_listener.service import _analysis_start_step

    run = {
        "mode": "broad_scan",
        "error": "competitors failed after 2 attempt(s): Competitor search requested 2 pages but received 0 for CUHK Business School",
        "run_dir": "data/runs/example",
    }

    assert _analysis_start_step(run, "auto") == "competitors"


def test_failed_and_stopped_runs_are_reachable_from_the_reports_library() -> None:
    """失败/stopped 的任务既进不了报告库（没有 report_html），也曾经不在
    "进行中的搜索" 列表里（旧过滤条件只认 queued/running/collected）——
    结果是完全点不进去，"从中断处继续"按钮永远够不着。这里锁住：失败/
    stopped 也必须出现在这个列表里。"""

    library = _func("_render_reports_library")

    pending_assign = next(
        node
        for node in ast.walk(library)
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and node.targets[0].id == "pending"
    )
    assert isinstance(pending_assign.value, ast.ListComp)
    filter_src = ast.unparse(pending_assign.value)
    for status in ("queued", "running", "collected", "failed", "stopped"):
        assert status in filter_src, f"{status} 应该能在报告库里点进去续跑/查看"


def test_resume_button_reruns_the_same_run_not_a_new_one() -> None:
    """"从中断处继续"必须是续跑同一个 run_id、force_reanalyze=False，
    而不是新开一个 run —— 否则等于重新收集，白花钱。"""

    content_src = ast.unparse(_func("_render_run_content"))
    assert "analyze_existing_run(int(run['id'])" in content_src
    assert "'force_reanalyze': False" in content_src
    assert "'resume_from': 'auto'" in content_src
    assert "create_run" not in content_src
