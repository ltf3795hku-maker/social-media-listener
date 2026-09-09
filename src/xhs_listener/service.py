"""Application service used by the Streamlit UI."""
from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

from xhs_listener.io_utils import read_json, read_jsonl_lenient
from xhs_listener.paths import data_root, runs_db_path, runs_dir
from xhs_listener.run_manager import (
    ManagedRunRequest,
    RunManager,
    RunStore,
    estimate_analysis_tokens_for_counts,
    estimate_request_size,
    preflight_request,
)


if load_dotenv is not None:
    project_env = Path(__file__).resolve().parents[2] / ".env"
    # Prefer project .env values even when shell/system env contains blank placeholders.
    # 注意：.env 与 data/ 都在 .gitignore 里，不会随部署上传，
    # 所以 Azure 上生效的始终是 App Settings 注入的真实环境变量。
    load_dotenv(project_env, override=True)
    load_dotenv(override=True)


# 报告库是共享的：sqlite、run 目录、report.html/json 全部落在同一个 data root。
# 具体位置由 paths.resolve_data_root 统一决定，不在各处零散判断。
DATA_DIR = data_root()
STORE = RunStore(runs_db_path())
MANAGER = RunManager(STORE)
STORE.mark_interrupted_runs()
_LOCK = threading.Lock()
_ACTIVE_THREADS: dict[int, threading.Thread] = {}
_REDACTED_VALUE = "[REDACTED]"
DEFAULT_TIKHUB_API_TOKEN = ""
DEFAULT_LLM_PROVIDER = "azure"
DEFAULT_AZURE_OPENAI_ENDPOINT = "https://fbe-icdevai03-openai-chan19-use2.openai.azure.com/"
DEFAULT_AZURE_OPENAI_API_KEY = ""
DEFAULT_AZURE_OPENAI_API_VERSION = "2025-01-01-preview"
DEFAULT_AZURE_OPENAI_DEPLOYMENT = "gpt-4.1-mini"


# Service 层只做三件事：
# 1. 把前端 payload 转成 ManagedRunRequest。
# 2. 启动后台线程执行采集或分析。
# 3. 把 run / report / notes 这些结果整理成前端好展示的形状。


def create_run(payload: dict[str, Any]) -> dict[str, Any]:
    request = _managed_request(payload)
    run = MANAGER.create_run(request)

    # 采集放到后台线程里跑，HTTP 请求立即返回 run id，前端靠轮询看进度。
    thread = threading.Thread(target=_execute_run, args=(int(run["id"]), request), daemon=True)
    with _LOCK:
        _ACTIVE_THREADS[int(run["id"])] = thread
    thread.start()
    return _enrich_run_for_ui(STORE.get_run(int(run["id"])))


def analyze_existing_run(run_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    run = get_run(run_id)
    if run["status"] not in {"collected", "succeeded", "failed", "stopped"}:
        raise ValueError("run must finish collection before analysis")
    force_reanalyze = bool(payload.get("force_reanalyze"))
    start_step = _analysis_start_step(run, str(payload.get("resume_from") or "").strip())
    request = _managed_request_from_run(run, payload)
    budget_error = _analysis_budget_error(run, request)
    if budget_error:
        raise ValueError(budget_error)

    # 分析必须等采集结束后才能启动，因为 process/analyze/report 都依赖 run_dir 里的文件。
    thread = threading.Thread(target=_execute_analysis, args=(run_id, request, force_reanalyze, start_step), daemon=True)
    with _LOCK:
        _ACTIVE_THREADS[run_id] = thread
    thread.start()
    return _enrich_run_for_ui(STORE.get_run(run_id))


def generate_english_report(run_id: int) -> dict[str, Any]:
    """按需生成英文版报告：只翻译已经生成好的中文 report.json，同步执行。

    比 create_run/analyze_existing_run 简单：不需要后台线程 + 轮询，因为
    只是翻译已有内容，不是重新采集/分析，一次调用通常几秒到几十秒。
    """

    run = get_run(run_id)
    if not run.get("report_html"):
        raise ValueError("Chinese report has not been generated yet")
    request = _managed_request_from_run(run, {})
    updated = MANAGER.generate_english_report(run_id, request)
    return _enrich_run_for_ui(updated)


def list_runs(limit: int = 50) -> list[dict[str, Any]]:
    _mark_detached_running_runs()
    return [_enrich_run_for_ui(run) for run in STORE.list_runs(limit=limit)]


def get_run(run_id: int) -> dict[str, Any]:
    try:
        _mark_detached_running_runs()
        return _enrich_run_for_ui(STORE.get_run(run_id))
    except KeyError as exc:
        raise KeyError(str(exc)) from exc


def get_run_notes(run_id: int) -> dict[str, Any]:
    run = get_run(run_id)
    rows = _note_preview_rows(run)
    return {"run": run, "rows": rows, "count": len(rows)}


def list_reports(limit: int = 50) -> list[dict[str, Any]]:
    return [_enrich_run_for_ui(run) for run in STORE.list_reports(limit=limit)]


def _execute_run(run_id: int, request: ManagedRunRequest) -> None:
    try:
        preflight_error = preflight_request(request)
        if preflight_error is not None:
            MANAGER.stop_run(
                run_id,
                preflight_error["reason"],
                error=preflight_error["message"],
                usage_tokens=0,
            )
            return

        STORE.update_run(run_id, status="running")
        STORE.append_event(run_id, "collection queued")
        context_request = request
        manager = RunManager(STORE)
        _run_collect_existing(manager, run_id, context_request)
    finally:
        with _LOCK:
            _ACTIVE_THREADS.pop(run_id, None)


def _execute_analysis(
    run_id: int,
    request: ManagedRunRequest,
    force_reanalyze: bool = False,
    start_step: str = "",
) -> None:
    try:
        STORE.append_event(run_id, "analysis queued")
        if force_reanalyze:
            _clear_analysis_caches(run_id)
        manager = RunManager(STORE)
        _run_analysis_existing(manager, run_id, request, start_step=start_step)
    finally:
        with _LOCK:
            _ACTIVE_THREADS.pop(run_id, None)


def _run_collect_existing(manager: RunManager, run_id: int, request: ManagedRunRequest) -> None:
    """Run collect only using an already-created row from POST /runs."""

    from xhs_listener.run_manager import COLLECT_STEPS, RunStoppedError, _now

    STORE.update_run(run_id, status="running", started_at=STORE.get_run(run_id).get("started_at") or _now())
    try:
        context = {"request": request, "run_id": run_id, "run_dir": None, "llm_client": None}
        for step in COLLECT_STEPS:
            manager._run_step_with_retry(run_id, step, request.max_step_retries, context, {})
        if STORE.get_run(run_id).get("status") == "stopped":
            return
        STORE.update_run(run_id, status="collected", current_step=None, finished_at=_now())
        STORE.append_event(run_id, "collection completed")
    except RunStoppedError:
        STORE.append_event(run_id, "collection stopped after current step", level="warning")
    except Exception as exc:  # noqa: BLE001
        STORE.update_run(
            run_id,
            status="failed",
            current_step=None,
            error=str(exc),
            finished_at=_now(),
        )
        STORE.append_event(run_id, f"collection failed: {exc}", level="error")


def _run_analysis_existing(
    manager: RunManager,
    run_id: int,
    request: ManagedRunRequest,
    *,
    start_step: str = "",
) -> None:
    """Run process/analyze/report using a collected run directory."""

    from xhs_listener.run_manager import BudgetExceededError

    try:
        run = manager.run_analysis(run_id, request, start_step=start_step or None)
        if run.get("status") == "succeeded":
            STORE.append_event(run_id, "analysis and report finished")
    except BudgetExceededError as exc:
        manager.stop_run(run_id, "budget_exceeded", error=str(exc))
        STORE.append_event(run_id, f"analysis stopped: {exc}", level="error")
    except Exception as exc:  # noqa: BLE001
        STORE.update_run(run_id, status="failed", current_step=None, error=str(exc))
        STORE.append_event(run_id, f"analysis failed: {exc}", level="error")


def _analysis_start_step(run: dict[str, Any], requested: str = "") -> str:
    if not requested or requested == "process":
        return "process"
    steps = ("process", "analyze", "top10_comments", "competitors", "report") if run.get("mode") == "broad_scan" else ("process", "analyze", "report")
    if requested != "auto":
        if requested not in steps:
            raise ValueError(f"unknown resume step: {requested}")
        return requested

    error = str(run.get("error") or "").lower()
    for step in steps:
        if f"{step} failed" in error or f"{step} attempt" in error:
            return step

    run_dir = run.get("run_dir")
    if not run_dir:
        return "process"
    run_path = Path(str(run_dir))
    required_files = {
        "process": ("processed_notes.jsonl", "processing.json"),
        "analyze": ("analysis.json",),
        "top10_comments": ("top10_comment_analysis.json",),
        "competitors": ("competitor_analysis.json",),
        "report": ("report.json", "report.html"),
    }
    for step in steps:
        files = required_files.get(step, ())
        if files and not all((run_path / name).exists() for name in files):
            return step
    return "report"


def _clear_analysis_caches(run_id: int) -> None:
    run = STORE.get_run(run_id)
    run_dir = run.get("run_dir")
    if not run_dir:
        return
    run_path = Path(str(run_dir))
    removed: list[str] = []
    for name in (
        "relevance_annotations.jsonl",
        "annotations.jsonl",
        "annotations.partial.jsonl",
        "annotations.partial.done.jsonl",
        "analysis.json",
        "analysis_log.jsonl",
        "top10_comment_analysis.json",
        "top10_comment_analysis.partial.json",
        "competitor_analysis.json",
        "competitor_analysis.partial.json",
    ):
        path = run_path / name
        if path.exists():
            path.unlink()
            removed.append(name)
    if removed:
        STORE.append_event(run_id, f"force reanalysis cleared caches: {', '.join(removed)}")


def _managed_request(payload: dict[str, Any]) -> ManagedRunRequest:
    if load_dotenv is not None:
        # Refresh runtime env so updated .env values are picked up without service restart.
        project_env = Path(__file__).resolve().parents[2] / ".env"
        load_dotenv(project_env, override=True)

    def env_or_default(key: str, default: str = "") -> str:
        value = (os.getenv(key) or "").strip()
        return value or default

    data = dict(payload)
    allowed = set(ManagedRunRequest.__dataclass_fields__)
    data = {key: value for key, value in data.items() if key in allowed}
    if not data.get("api_token"):
        data["api_token"] = env_or_default("TIKHUB_API_TOKEN", DEFAULT_TIKHUB_API_TOKEN)
    data.setdefault("llm_provider", env_or_default("LLM_PROVIDER", DEFAULT_LLM_PROVIDER))
    data.setdefault("azure_openai_endpoint", env_or_default("AZURE_OPENAI_ENDPOINT", DEFAULT_AZURE_OPENAI_ENDPOINT))
    data.setdefault("azure_openai_api_key", env_or_default("AZURE_OPENAI_API_KEY", DEFAULT_AZURE_OPENAI_API_KEY))
    data.setdefault("azure_openai_api_version", env_or_default("AZURE_OPENAI_API_VERSION", DEFAULT_AZURE_OPENAI_API_VERSION))
    data.setdefault("azure_openai_deployment", env_or_default("AZURE_OPENAI_DEPLOYMENT", DEFAULT_AZURE_OPENAI_DEPLOYMENT))
    data.setdefault("budget_tokens", 0)
    data.setdefault("collect_budget_units", 0)
    data.setdefault("tikhub_host", env_or_default("TIKHUB_HOST", "https://api.tikhub.io"))
    data.setdefault("output_root", str(runs_dir()))
    return ManagedRunRequest(**data)


def _managed_request_from_run(run: dict[str, Any], payload: dict[str, Any]) -> ManagedRunRequest:
    if load_dotenv is not None:
        # Refresh runtime env so updated .env values are picked up without service restart.
        project_env = Path(__file__).resolve().parents[2] / ".env"
        load_dotenv(project_env, override=True)

    def env_or_default(key: str, default: str = "") -> str:
        value = (os.getenv(key) or "").strip()
        return value or default

    config = {
        key: ("" if value == _REDACTED_VALUE else value)
        for key, value in dict(run.get("config") or {}).items()
    }
    config.update(payload)
    # Re-analysis should use latest credentials from payload/.env instead of stale
    # values stored with the original run configuration.
    config["api_token"] = payload.get("api_token") or env_or_default("TIKHUB_API_TOKEN", DEFAULT_TIKHUB_API_TOKEN)
    config["llm_provider"] = payload.get("llm_provider") or env_or_default("LLM_PROVIDER", DEFAULT_LLM_PROVIDER)
    config["azure_openai_endpoint"] = payload.get("azure_openai_endpoint") or env_or_default("AZURE_OPENAI_ENDPOINT", DEFAULT_AZURE_OPENAI_ENDPOINT)
    config["azure_openai_api_key"] = payload.get("azure_openai_api_key") or env_or_default("AZURE_OPENAI_API_KEY", DEFAULT_AZURE_OPENAI_API_KEY)
    config["azure_openai_api_version"] = payload.get("azure_openai_api_version") or env_or_default("AZURE_OPENAI_API_VERSION", DEFAULT_AZURE_OPENAI_API_VERSION)
    config["azure_openai_deployment"] = payload.get("azure_openai_deployment") or env_or_default("AZURE_OPENAI_DEPLOYMENT", DEFAULT_AZURE_OPENAI_DEPLOYMENT)
    config["openai_api_key"] = payload.get("openai_api_key") or env_or_default("OPENAI_API_KEY", "")
    config["openai_base_url"] = payload.get("openai_base_url") or env_or_default("OPENAI_BASE_URL", "")
    config["openai_model"] = payload.get("openai_model") or env_or_default("OPENAI_MODEL", "")
    config["budget_tokens"] = int(payload.get("budget_tokens") or config.get("budget_tokens") or 0)
    return _managed_request(config)


def _analysis_budget_error(run: dict[str, Any], request: ManagedRunRequest) -> str:
    notes_count = int(run.get("collect_notes_count") or 0)
    comments_count = int(run.get("collect_comments_count") or 0)
    if notes_count:
        estimate = estimate_analysis_tokens_for_counts(request, notes_count, comments_count)
    else:
        # 还没有采集产物时退回配置上限估算。
        estimate = estimate_request_size(request)
    required = int(estimate.get("estimated_analysis_tokens") or 0)
    budget = int(request.budget_tokens or 0)
    spent = int(run.get("usage_tokens") or 0)
    remaining = max(0, budget - spent) if budget else 0
    if budget and required and remaining < required:
        return (
            "llm_budget_too_low "
            f"spent={spent} budget={budget} remaining={remaining} "
            f"estimated_required={required}"
        )
    return ""


def _mark_detached_running_runs() -> None:
    # 服务重启后，内存里的后台线程会消失；这里把数据库里还显示 running 的旧任务标为 stopped。
    with _LOCK:
        active_ids = set(_ACTIVE_THREADS)
    for run in STORE.list_runs(limit=200):
        if run["status"] not in {"queued", "running"}:
            continue
        if int(run["id"]) in active_ids:
            continue
        STORE.append_event(int(run["id"]), "marked stopped because no backend worker is attached", level="warning")
        MANAGER.stop_run(int(run["id"]), "worker_not_attached")


def _note_preview_rows(run: dict[str, Any]) -> list[dict[str, Any]]:
    run_dir = run.get("run_dir")
    if not run_dir:
        return []
    path = Path(run_dir)
    # notes.jsonl 只在采集全部结束后才写；采集进行中只有 notes.partial.jsonl
    # （每完成一条笔记详情就更新一次），前端要看到实时进度就得读这份。
    notes_path = path / "notes.jsonl"
    if not notes_path.exists():
        notes_path = path / "notes.partial.jsonl"
    notes, note_errors = read_jsonl_lenient(notes_path)
    if note_errors:
        STORE.append_event(
            int(run["id"]),
            f"note preview skipped {len(note_errors)} malformed JSONL line(s)",
            level="warning",
        )
    processed_ids = {
        str(row.get("note_id"))
        for row in read_jsonl_lenient(path / "processed_notes.jsonl")[0]
    }
    annotations = {
        str(row.get("note_id")): row
        for row in read_jsonl_lenient(path / "annotations.jsonl")[0]
    }
    has_processed_file = (path / "processed_notes.jsonl").exists()
    rows: list[dict[str, Any]] = []

    # 前端帖子表同时展示采集状态和分析标注；
    # 如果还没 process，就只根据采集标签判断 collected/out_of_scope。
    for note in notes:
        note_id = str(note.get("note_id") or "")
        annotation = annotations.get(note_id, {})
        if has_processed_file:
            status = "processed" if note_id in processed_ids else "filtered"
        else:
            status = "collected" if note.get("is_scope_relevant") is not False and note.get("is_valid") is not False else "out_of_scope"
        rows.append(
            {
                "note_id": note_id,
                "status": status,
                "title": note.get("title") or "",
                "body_preview": str(note.get("body") or "")[:220],
                "post_url": note.get("post_url") or "",
                "like_count": note.get("like_count") or 0,
                "comment_count": note.get("comment_count") or 0,
                "collect_count": note.get("collect_count") or 0,
                "share_count": note.get("share_count") or 0,
                "is_scope_relevant": note.get("is_scope_relevant"),
                "skip_reasons": ",".join(note.get("skip_reasons") or []),
                "hku_relevance": annotation.get("hku_relevance") or "",
                "topic_relevance": annotation.get("topic_relevance") or "",
                "theme": annotation.get("theme") or "",
                "content_type": annotation.get("content_type") or "",
                "primary_narrative": annotation.get("primary_narrative") or "",
                "narrative_stance": annotation.get("narrative_stance") or "",
                "has_uncertainty": annotation.get("has_uncertainty"),
                "signal_label": annotation.get("signal_label") or "",
            }
        )
    return rows


def _enrich_run_for_ui(run: dict[str, Any]) -> dict[str, Any]:
    """给前端补充两类成本：TikHub 请求成本与 LLM token 成本。"""

    out = dict(run)
    collection = _read_collection_summary(out.get("run_dir"))
    request_count = int(collection.get("api_request_count") or 0)
    out["collect_request_count"] = request_count
    out["collect_unit_cost_usd"] = float(collection.get("api_unit_cost_usd") or 0.01)
    out["collect_cost_usd"] = round(request_count * out["collect_unit_cost_usd"], 2)
    out["collect_notes_count"] = int(collection.get("notes_count") or 0)
    out["collect_comments_count"] = int(collection.get("comments_count") or 0)
    out["llm_usage_tokens"] = int(out.get("usage_tokens") or 0)
    done, total = _competitor_progress(out.get("run_dir"))
    out["competitor_schools_done"] = done
    out["competitor_schools_total"] = total
    return out


def _competitor_progress(run_dir: Any) -> tuple[int, int]:
    """竞对周报进度：已完成学校数 / 总学校数。

    competitor_collection.json 只在 7 所学校全部跑完才写；跑到一半只有
    competitor_collection.partial.json（每完成一所学校就更新一次），前端
    要在 competitors 步骤里看到"已完成 N/7 所"就得读这份。
    """

    if not run_dir:
        return 0, 0
    base = Path(str(run_dir))
    for name in ("competitor_collection.json", "competitor_collection.partial.json"):
        path = base / name
        if not path.exists():
            continue
        try:
            payload = read_json(path)
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        schools = payload.get("schools")
        schools_config = payload.get("schools_config")
        done = len(schools) if isinstance(schools, list) else 0
        total = len(schools_config) if isinstance(schools_config, list) else 0
        return done, total
    return 0, 0


def _read_collection_summary(run_dir: Any) -> dict[str, Any]:
    if not run_dir:
        return {}
    base = Path(str(run_dir))
    path = base / "collection.json"
    if path.exists():
        try:
            payload = read_json(path)
        except Exception:
            return {}
        quality = payload.get("quality") if isinstance(payload, dict) else {}
        if not isinstance(quality, dict):
            quality = {}
        request_count = quality.get("api_request_count") or _estimate_requests_from_collection(payload)
        return {
            "api_request_count": request_count,
            "api_unit_cost_usd": quality.get("api_unit_cost_usd", 0.01),
            "notes_count": payload.get("notes_count", quality.get("notes_saved", 0)),
            "comments_count": payload.get("comments_count", quality.get("comments_saved", 0)),
        }
    # collection.json 要等采集全部结束才会写；采集进行中只能读
    # collection.partial.json（每完成一页/一条笔记就更新一次），
    # 这样前端在采集过程中也能看到已找到的帖子数和请求数。
    partial_path = base / "collection.partial.json"
    if not partial_path.exists():
        return {}
    try:
        partial = read_json(partial_path)
    except Exception:
        return {}
    if not isinstance(partial, dict):
        return {}
    return {
        "api_request_count": int(partial.get("api_request_count") or 0),
        "api_unit_cost_usd": 0.01,
        "notes_count": int(partial.get("notes_count") or 0),
        "comments_count": int(partial.get("comments_count") or 0),
    }


def _estimate_requests_from_collection(payload: dict[str, Any]) -> int:
    logs = payload.get("logs") if isinstance(payload, dict) else []
    errors = payload.get("errors") if isinstance(payload, dict) else []
    if not isinstance(logs, list):
        logs = []
    if not isinstance(errors, list):
        errors = []
    notes_count = int(payload.get("notes_count") or 0)
    search_attempts = sum(1 for line in logs if "search page=" in str(line) and "backend=" in str(line))
    comment_attempts = sum(1 for line in logs if "collect comments note_id=" in str(line))
    return search_attempts + notes_count + comment_attempts + len(errors)
