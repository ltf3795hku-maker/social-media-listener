"""Run management layer: SQLite-backed run history, step retry, and token budget guards."""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from xhs_listener.analyze import analyze_run
from xhs_listener.broad_scan import broad_collect, parse_keyword_pool
from xhs_listener.collect import XiaohongshuCollector
from xhs_listener.io_utils import read_json
from xhs_listener.log_utils import LOG_QUEUE_END
from xhs_listener.models import BroadScanConfig, CollectConfig, CommentPolicy
from xhs_listener.paths import runs_db_path, runs_dir
from xhs_listener.process import process_run
from xhs_listener.report import report_run, report_run_english
from xhs_listener.weekly_insights import (
    COMPETITOR_MAX_PAGES,
    COMPETITOR_SCHOOLS,
    collect_and_analyze_top10_comments,
    analyze_competitor_weekly,
    collect_competitor_weekly,
)


COLLECT_STEPS = ("collect",)
ANALYSIS_STEPS = ("process", "analyze", "report")
BROAD_ANALYSIS_STEPS = ("process", "analyze", "top10_comments", "competitors", "report")
SENSITIVE_CONFIG_KEYS = {"api_token", "api-token", "tikhub_api_token", "openai_api_key", "azure_openai_api_key"}


# 这个文件是“流水线调度器”：
# SQLite 负责记录历史和状态，RunManager 负责按步骤执行，
# BudgetedLLMClient 负责在分析/报告阶段守住 token 预算。
class BudgetExceededError(RuntimeError):
    """Raised when a run's token usage exceeds its configured budget."""


class RunStoppedError(RuntimeError):
    """Raised between steps after the user requests a stop."""


@dataclass
class ManagedRunRequest:
    """Input for one managed pipeline run."""

    mode: str = "topic_scan"  # topic_scan / broad_scan
    collect_config: Optional[dict[str, Any]] = None
    broad_config: Optional[dict[str, Any]] = None
    budget_tokens: int = 0
    collect_budget_units: int = 0
    max_step_retries: int = 1
    # 默认与 service 共用同一个 data root；显式传入时以传入值为准。
    output_root: str = ""
    tikhub_host: str = "https://api.tikhub.io"
    api_token: str = ""
    llm_provider: str = ""
    azure_openai_endpoint: str = ""
    azure_openai_api_key: str = ""
    azure_openai_api_version: str = ""
    azure_openai_deployment: str = ""
    openai_api_key: str = ""
    openai_base_url: str = ""
    openai_model: str = ""
    max_pages_limit: int = 20
    max_notes_limit: int = 500
    max_comment_pages_limit: int = 5
    max_sub_comment_pages_limit: int = 2
    max_broad_keywords_limit: int = 50
    estimated_tokens_per_note: int = 2500
    estimated_tokens_per_comment: int = 220
    estimated_report_tokens: int = 0


class RunStore:
    """Small SQLite repository for managed run metadata."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = Path(db_path) if db_path else runs_db_path()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_schema(self) -> None:
        # runs 表既存任务状态，也存前端展示需要的报告路径、事件日志和步骤尝试次数。
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    current_step TEXT,
                    run_dir TEXT,
                    config_json TEXT NOT NULL,
                    step_attempts_json TEXT NOT NULL DEFAULT '{}',
                    budget_tokens INTEGER NOT NULL DEFAULT 0,
                    usage_tokens INTEGER NOT NULL DEFAULT 0,
                    stop_reason TEXT,
                    error TEXT,
                    report_json TEXT,
                    report_html TEXT,
                    events_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_status ON runs(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_runs_created_at ON runs(created_at)")
            # 老数据库没有这两列（英文报告是后加的功能）；用 ALTER TABLE 补上，
            # 已有的 runs.sqlite3 不用手动迁移。
            self._ensure_columns(conn, "runs", {"report_json_en": "TEXT", "report_html_en": "TEXT"})

    @staticmethod
    def _ensure_columns(conn: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        existing = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, coltype in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coltype}")

    def create_run(self, request: ManagedRunRequest) -> dict[str, Any]:
        now = _now()
        config = _redact_config(asdict(request))
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO runs (
                    mode, status, config_json, step_attempts_json, budget_tokens,
                    usage_tokens, created_at, updated_at
                )
                VALUES (?, 'queued', ?, '{}', ?, 0, ?, ?)
                """,
                (request.mode, json.dumps(config, ensure_ascii=False), int(request.budget_tokens), now, now),
            )
            run_id = int(cur.lastrowid)
        return self.get_run(run_id)

    def get_run(self, run_id: int) -> dict[str, Any]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM runs WHERE id = ?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(f"run {run_id} not found")
        return _row_to_dict(row)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT * FROM runs ORDER BY created_at DESC, id DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM runs
                WHERE status = 'succeeded' AND report_html IS NOT NULL
                ORDER BY finished_at DESC, id DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def update_run(self, run_id: int, **fields: Any) -> dict[str, Any]:
        if not fields:
            return self.get_run(run_id)
        fields["updated_at"] = _now()
        assignments = ", ".join(f"{key} = ?" for key in fields)
        values = list(fields.values()) + [run_id]
        with self._connect() as conn:
            conn.execute(f"UPDATE runs SET {assignments} WHERE id = ?", values)
        return self.get_run(run_id)

    def increment_step_attempt(self, run_id: int, step: str) -> int:
        run = self.get_run(run_id)
        attempts = dict(run.get("step_attempts") or {})
        attempts[step] = int(attempts.get(step, 0)) + 1
        self.update_run(run_id, step_attempts_json=json.dumps(attempts, ensure_ascii=False))
        return attempts[step]

    def append_event(self, run_id: int, message: str, step: Optional[str] = None, level: str = "info") -> dict[str, Any]:
        """Append one short event for UI progress logs."""

        run = self.get_run(run_id)
        events = list(run.get("events") or [])
        events.append({"time": _now(), "level": level, "step": step, "message": message})
        return self.update_run(run_id, events_json=json.dumps(events[-300:], ensure_ascii=False))

    def mark_interrupted_runs(self, reason: str = "server_restarted") -> int:
        """Convert stale queued/running rows into stopped rows after a server restart."""

        now = _now()
        with self._connect() as conn:
            cur = conn.execute(
                """
                UPDATE runs
                SET status = 'stopped',
                    stop_reason = ?,
                    error = COALESCE(error, ?),
                    current_step = NULL,
                    finished_at = ?,
                    updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (reason, reason, now, now),
            )
            return int(cur.rowcount or 0)


class BudgetedLLMClient:
    """LLM client wrapper that stops future calls once usage passes the run budget."""

    def __init__(
        self,
        inner: Optional[Any],
        budget_tokens: int,
        initial_usage_tokens: int = 0,
        on_usage: Optional[Callable[[int], None]] = None,
        llm_config: Optional[dict[str, str]] = None,
    ) -> None:
        self.inner = inner
        self.budget_tokens = max(0, int(budget_tokens or 0))
        self.usage_tokens = max(0, int(initial_usage_tokens or 0))
        self.on_usage = on_usage
        self.llm_config = llm_config or {}

    def get_response(self, messages: list[dict[str, Any]], *args: Any, **kwargs: Any) -> Any:
        # 每次调用前后都检查预算；如果超过预算，后续步骤会被 RunManager 标为 stopped。
        self._raise_if_over_budget()
        if self.inner is None:
            from xhs_listener.llm_client import LLMClient

            self.inner = LLMClient(self.llm_config)
        response = self.inner.get_response(messages, *args, **kwargs)
        if not isinstance(response, str):
            self._add_usage(response)
        self._raise_if_over_budget()
        return response

    def usage_dict(self, response: Any) -> dict[str, Any]:
        if hasattr(self.inner, "usage_dict"):
            return self.inner.usage_dict(response)
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        return {"total_tokens": getattr(usage, "total_tokens", None)}

    def extract_json(self, text: str) -> Any:
        if hasattr(self.inner, "extract_json"):
            return self.inner.extract_json(text)
        from xhs_listener.llm_client import LLMClient

        return LLMClient.extract_json(text)

    def _add_usage(self, response: Any) -> None:
        usage = self.usage_dict(response)
        tokens = usage.get("total_tokens")
        if tokens is None:
            tokens = _sum_token_fields(usage)
        self.usage_tokens += int(tokens or 0)
        if self.on_usage is not None:
            self.on_usage(self.usage_tokens)

    def _raise_if_over_budget(self) -> None:
        if self.budget_tokens and self.usage_tokens > self.budget_tokens:
            raise BudgetExceededError(f"budget_exceeded usage={self.usage_tokens} budget={self.budget_tokens}")


class RunEventLogQueue:
    """Bridge step-level logs into run events so Streamlit can show progress."""

    def __init__(self, store: RunStore, run_id: int, step: str) -> None:
        self.store = store
        self.run_id = int(run_id)
        self.step = step

    def put(self, message: Any) -> None:
        if message == LOG_QUEUE_END:
            return
        text = str(message or "").strip()
        if text:
            self.store.append_event(self.run_id, text, step=self.step)


class RunManager:
    """Executes xhs-listener steps under run history, retry, and budget controls."""

    def __init__(self, store: RunStore) -> None:
        self.store = store

    def create_run(self, request: ManagedRunRequest) -> dict[str, Any]:
        return self.store.create_run(request)

    def run_analysis(
        self,
        run_id: int,
        request: ManagedRunRequest,
        llm_client: Optional[Any] = None,
        step_overrides: Optional[dict[str, Callable[..., Any]]] = None,
        start_step: Optional[str] = None,
    ) -> dict[str, Any]:
        """Run process/analyze/report against an already collected run."""

        run = self.store.get_run(run_id)
        run_dir = run.get("run_dir")
        if not run_dir:
            raise RuntimeError("run_dir is missing; collect must finish before analysis")
        self.store.update_run(
            run_id,
            status="running",
            current_step=None,
            error=None,
            stop_reason=None,
            budget_tokens=int(request.budget_tokens or 0),
            started_at=run.get("started_at") or _now(),
        )
        existing_usage_tokens = int(run.get("usage_tokens") or 0)
        budget_client = BudgetedLLMClient(
            llm_client,
            budget_tokens=request.budget_tokens,
            initial_usage_tokens=existing_usage_tokens,
            on_usage=lambda total: self.store.update_run(run_id, usage_tokens=total),
            llm_config=_llm_config_from_request(request),
        )
        try:
            context: dict[str, Any] = {
                "request": request,
                "run_id": run_id,
                "run_dir": run_dir,
                "llm_client": budget_client,
            }
            # 分析阶段固定顺序：process -> analyze -> report。
            # 每一步都经过 _run_step_with_retry，所以单步失败可以只重试该步。
            steps = BROAD_ANALYSIS_STEPS if request.mode == "broad_scan" else ANALYSIS_STEPS
            if start_step:
                if start_step not in steps:
                    raise RuntimeError(f"unknown analysis start step: {start_step}")
                steps = steps[steps.index(start_step) :]
            for step in steps:
                self._run_step_with_retry(run_id, step, request.max_step_retries, context, step_overrides or {})
            if self.store.get_run(run_id).get("status") == "stopped":
                return self.store.get_run(run_id)
            return self.store.update_run(run_id, status="succeeded", current_step=None, finished_at=_now())
        except BudgetExceededError as exc:
            return self.stop_run(run_id, "budget_exceeded", error=str(exc), usage_tokens=budget_client.usage_tokens)
        except RunStoppedError:
            return self.store.get_run(run_id)
        except Exception as exc:  # noqa: BLE001
            return self.store.update_run(
                run_id,
                status="failed",
                current_step=None,
                error=str(exc),
                finished_at=_now(),
                usage_tokens=budget_client.usage_tokens,
            )

    def stop_run(self, run_id: int, reason: str, **fields: Any) -> dict[str, Any]:
        fields.update({"status": "stopped", "stop_reason": reason, "finished_at": _now()})
        return self.store.update_run(run_id, **fields)

    def _run_step_with_retry(
        self,
        run_id: int,
        step: str,
        max_retries: int,
        context: dict[str, Any],
        overrides: dict[str, Callable[..., Any]],
    ) -> None:
        attempts_allowed = max(1, int(max_retries) + 1)
        last_exc: Optional[BaseException] = None
        for _ in range(attempts_allowed):
            # 每次尝试前检查是否被用户停止；步骤内部不可强杀，只在步骤边界停止。
            self._raise_if_stopped(run_id)
            attempt = self.store.increment_step_attempt(run_id, step)
            self.store.update_run(run_id, current_step=step, error=None)
            self.store.append_event(run_id, f"{step} started", step=step)
            try:
                handler = overrides.get(step) or getattr(self, f"_step_{step}")
                handler(context)
                self._raise_if_stopped(run_id)
                self.store.append_event(run_id, f"{step} finished", step=step)
                return
            except BudgetExceededError:
                raise
            except RunStoppedError:
                raise
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                self.store.update_run(run_id, error=f"{step} attempt {attempt} failed: {exc}")
                self.store.append_event(run_id, f"{step} attempt {attempt} failed: {exc}", step=step, level="error")
        raise RuntimeError(f"{step} failed after {attempts_allowed} attempt(s): {last_exc}") from last_exc

    def _raise_if_stopped(self, run_id: int) -> None:
        run = self.store.get_run(run_id)
        if run.get("status") == "stopped":
            raise RunStoppedError(run.get("stop_reason") or "stopped")

    def _step_collect(self, context: dict[str, Any]) -> None:
        request: ManagedRunRequest = context["request"]
        collector = XiaohongshuCollector(
            api_token=request.api_token,
            host=request.tikhub_host,
            output_root=request.output_root or str(runs_dir()),
            stop_checker=lambda: self._raise_if_stopped(int(context["run_id"])),
        )
        if request.mode == "broad_scan":
            config = _build_broad_config(request.broad_config or {})
            collection = broad_collect(collector, config)
        else:
            config = _build_collect_config(request.collect_config or {})
            collection = collector.collect(config)
        context["run_dir"] = collection.run_dir
        self.store.update_run(context["run_id"], run_dir=collection.run_dir)
        self.store.append_event(
            context["run_id"],
            f"collection saved notes={len(collection.notes)} comments={len(collection.comments)}",
            step="collect",
        )
        if not collection.notes:
            error_hint = _collection_error_hint(collection.errors)
            raise RuntimeError(f"collection produced 0 notes{error_hint}")

    def _step_process(self, context: dict[str, Any]) -> None:
        run_dir = _require_run_dir(context)
        process_run(run_dir)

    def _step_analyze(self, context: dict[str, Any]) -> None:
        run_dir = _require_run_dir(context)
        analyze_run(
            run_dir,
            client=context["llm_client"],
            log_queue=RunEventLogQueue(self.store, int(context["run_id"]), "analyze"),
            stop_checker=lambda: self._raise_if_stopped(int(context["run_id"])),
        )
        self._sync_usage_from_disk(context)

    def _step_top10_comments(self, context: dict[str, Any]) -> None:
        request: ManagedRunRequest = context["request"]
        if request.mode != "broad_scan":
            return
        run_dir = _require_run_dir(context)
        queue = RunEventLogQueue(self.store, int(context["run_id"]), "top10_comments")
        collector = XiaohongshuCollector(
            api_token=request.api_token,
            host=request.tikhub_host,
            output_root=request.output_root or str(runs_dir()),
            log_queue=queue,
            stop_checker=lambda: self._raise_if_stopped(int(context["run_id"])),
        )
        collect_and_analyze_top10_comments(
            run_dir,
            collector,
            context["llm_client"],
            log_queue=queue,
            stop_checker=lambda: self._raise_if_stopped(int(context["run_id"])),
        )
        self._sync_usage_from_disk(context)

    def _step_competitors(self, context: dict[str, Any]) -> None:
        request: ManagedRunRequest = context["request"]
        if request.mode != "broad_scan":
            return
        run_dir = _require_run_dir(context)
        queue = RunEventLogQueue(self.store, int(context["run_id"]), "competitors")
        collector = XiaohongshuCollector(
            api_token=request.api_token,
            host=request.tikhub_host,
            output_root=request.output_root or str(runs_dir()),
            log_queue=queue,
            stop_checker=lambda: self._raise_if_stopped(int(context["run_id"])),
        )
        collect_competitor_weekly(
            run_dir,
            collector,
            log_queue=queue,
            stop_checker=lambda: self._raise_if_stopped(int(context["run_id"])),
        )
        analyze_competitor_weekly(
            run_dir,
            context["llm_client"],
            log_queue=queue,
            stop_checker=lambda: self._raise_if_stopped(int(context["run_id"])),
        )
        self._sync_usage_from_disk(context)

    def _step_report(self, context: dict[str, Any]) -> None:
        run_dir = _require_run_dir(context)
        result = report_run(
            run_dir,
            log_queue=RunEventLogQueue(self.store, int(context["run_id"]), "report"),
        )
        self._sync_usage_from_disk(context)
        self.store.update_run(
            context["run_id"],
            report_json=result.get("report_json"),
            report_html=result.get("report_html"),
        )

    def generate_english_report(self, run_id: int, request: ManagedRunRequest) -> dict[str, Any]:
        """按需生成英文版报告：只翻译已经生成好的中文 report.json，不重跑 analyze。

        由前端"生成英文报告"按钮同步调用（不走 collect/process/analyze 那条
        后台线程流水线），所以这里自己管一遍 LLM client + usage 同步。
        """

        run = self.store.get_run(run_id)
        run_dir = run.get("run_dir")
        if not run_dir:
            raise RuntimeError("run_dir is missing")
        if not run.get("report_html"):
            raise RuntimeError("Chinese report has not been generated yet")

        from xhs_listener.llm_client import LLMClient

        client = LLMClient(_llm_config_from_request(request))
        queue = RunEventLogQueue(self.store, run_id, "report_en")
        result = report_run_english(run_dir, client=client, log_queue=queue)
        self._sync_usage_from_disk({"run_id": run_id, "run_dir": run_dir, "llm_client": client})
        self.store.update_run(
            run_id,
            report_json_en=result.get("report_json_en"),
            report_html_en=result.get("report_html_en"),
        )
        return self.store.get_run(run_id)

    def _sync_usage_from_disk(self, context: dict[str, Any]) -> None:
        run_dir = context.get("run_dir")
        if not run_dir:
            return
        usage_path = Path(run_dir) / "llm_usage.json"
        if not usage_path.exists():
            return
        usage = _usage_total_from_file(usage_path)
        client = context.get("llm_client")
        usage = max(usage, int(getattr(client, "usage_tokens", 0)))
        if client is not None:
            client.usage_tokens = usage
        self.store.update_run(context["run_id"], usage_tokens=usage)
        if getattr(client, "budget_tokens", 0) and usage > client.budget_tokens:
            raise BudgetExceededError(f"budget_exceeded usage={usage} budget={client.budget_tokens}")


def _build_collect_config(payload: dict[str, Any]) -> CollectConfig:
    return CollectConfig(**payload)


def _build_broad_config(payload: dict[str, Any]) -> BroadScanConfig:
    keyword_pool = parse_keyword_pool(payload.pop("keyword_pool_json", None)) if "keyword_pool_json" in payload else None
    if isinstance(payload.get("comment_policy"), dict):
        payload["comment_policy"] = CommentPolicy(**payload["comment_policy"])
    if keyword_pool is not None:
        payload["keyword_pool"] = keyword_pool
    return BroadScanConfig(**payload)


def _llm_config_from_request(request: ManagedRunRequest) -> dict[str, str]:
    """提取前端临时传入的 LLM 凭证；空值会回退到 .env。"""

    return {
        "LLM_PROVIDER": request.llm_provider,
        "AZURE_OPENAI_ENDPOINT": request.azure_openai_endpoint,
        "AZURE_OPENAI_API_KEY": request.azure_openai_api_key,
        "AZURE_OPENAI_API_VERSION": request.azure_openai_api_version,
        "AZURE_OPENAI_DEPLOYMENT": request.azure_openai_deployment,
        "OPENAI_API_KEY": request.openai_api_key,
        "OPENAI_BASE_URL": request.openai_base_url,
        "OPENAI_MODEL": request.openai_model,
    }


def preflight_request(request: ManagedRunRequest) -> Optional[dict[str, str]]:
    """Reject obviously unsafe runs before collect starts."""

    try:
        estimate = estimate_request_size(request)
    except Exception as exc:  # noqa: BLE001
        return {"reason": "preflight_failed", "message": f"preflight_failed: {exc}"}

    limit_errors = _safety_limit_errors(request, estimate)
    if limit_errors:
        return {"reason": "safety_limit_exceeded", "message": "; ".join(limit_errors)}

    if request.collect_budget_units and estimate["estimated_collect_units"] > request.collect_budget_units:
        return {
            "reason": "collect_budget_exceeded",
            "message": (
                "estimated_collect_budget_exceeded "
                f"estimated={estimate['estimated_collect_units']} budget={request.collect_budget_units} "
                f"notes={estimate['max_notes']} comments={estimate['max_comments']}"
            ),
        }
    return None


def estimate_request_size(request: ManagedRunRequest) -> dict[str, int]:
    """Conservative pre-run estimate used to stop accidental huge jobs."""

    if request.mode == "broad_scan":
        config = _build_broad_config(dict(request.broad_config or {}))
        keyword_count = len(config.keyword_pool) + len(COMPETITOR_SCHOOLS)
        # 页数是 per-keyword 的：护栏必须按各关键词页数求和，
        # 否则「1 页 × 9 个关键词」会把「3+2+2+2+1×5」的真实请求量低估一半。
        search_requests = (
            sum(int(item.max_pages or config.max_pages) for item in config.keyword_pool)
            + len(COMPETITOR_SCHOOLS) * COMPETITOR_MAX_PAGES
        )
        max_pages = max((int(item.max_pages or config.max_pages) for item in config.keyword_pool), default=int(config.max_pages))
        max_notes = sum(int(item.note_cap) for item in config.keyword_pool)
        competitor_max_notes = len(COMPETITOR_SCHOOLS) * COMPETITOR_MAX_PAGES * 20
        comment_pages = 1
        sub_comment_pages = 0
        max_comments = 10 * 20
    else:
        config = _build_collect_config(dict(request.collect_config or {}))
        keyword_count = 1
        max_pages = int(config.max_pages)
        max_notes = int(config.max_notes)
        search_requests = max_pages
        comment_pages = int(config.comment_pages)
        sub_comment_pages = int(config.sub_comment_pages)
        if config.comment_policy == "top_notes" and comment_pages > 0:
            comment_notes = max(1, (max_notes * int(config.comment_top_percent or 20) + 99) // 100)
            limit = int(config.fetch_comments_for_top_notes or 0)
            comment_notes = min(max_notes, comment_notes, limit) if limit > 0 else min(max_notes, comment_notes)
        elif config.comment_policy == "all" and comment_pages > 0:
            comment_notes = max_notes
        else:
            comment_notes = max_notes if comment_pages > 0 else 0
        max_comments = comment_notes * max(0, comment_pages) * 20
        if sub_comment_pages > 0:
            max_comments += max_comments * sub_comment_pages

        competitor_max_notes = 0

    estimated_collect_units = max_notes + competitor_max_notes + max_comments
    token_estimate = estimate_analysis_tokens_for_counts(request, max_notes, max_comments)
    return {
        "keyword_count": keyword_count,
        "max_pages": max_pages,
        "search_requests": search_requests,
        "max_notes": max_notes,
        "comment_pages": comment_pages,
        "sub_comment_pages": sub_comment_pages,
        "max_comments": max_comments,
        "competitor_max_notes": competitor_max_notes,
        "estimated_collect_units": estimated_collect_units,
        **token_estimate,
    }


def estimate_analysis_tokens_for_counts(
    request: ManagedRunRequest,
    notes_count: int,
    comments_count: int,
) -> dict[str, int]:
    """按帖子/评论数量估算分析 token；采集完成后应传实际数量而非配置上限。

    Analysis uses a two-stage annotation flow: a short relevance gate for every
    note, then full business annotation only for notes that pass the gate.
    """

    notes_count = max(0, int(notes_count or 0))
    comments_count = max(0, int(comments_count or 0))
    default_relevance_batch = "15" if request.mode == "broad_scan" else "5"
    default_annotation_batch = "6" if request.mode == "broad_scan" else "3"
    relevance_batch_size = max(
        1,
        int(os.getenv("XHS_RELEVANCE_BATCH_SIZE", os.getenv("XHS_ANNOTATION_BATCH_SIZE", default_relevance_batch))),
    )
    annotation_batch_size = max(1, int(os.getenv("XHS_ANNOTATION_BATCH_SIZE", default_annotation_batch)))
    relevance_batches = (notes_count + relevance_batch_size - 1) // relevance_batch_size
    assumed_relevant_ratio = float(os.getenv("XHS_EST_RELEVANT_RATIO", "0.6"))
    assumed_relevant_ratio = min(1.0, max(0.0, assumed_relevant_ratio))
    estimated_relevant_notes = min(notes_count, max(0, int(round(notes_count * assumed_relevant_ratio))))
    detail_annotation_batches = (estimated_relevant_notes + annotation_batch_size - 1) // annotation_batch_size if estimated_relevant_notes else 0
    upper_annotation_batches = (notes_count + annotation_batch_size - 1) // annotation_batch_size
    relevance_prompt_overhead = int(os.getenv("XHS_EST_RELEVANCE_PROMPT_OVERHEAD", "350"))
    relevance_tokens_per_note = int(os.getenv("XHS_EST_RELEVANCE_TOKENS_PER_NOTE", "500"))
    annotation_prompt_overhead = int(os.getenv("XHS_EST_ANNOTATION_PROMPT_OVERHEAD", "800"))
    analysis_prompt_overhead = int(os.getenv("XHS_EST_ANALYSIS_PROMPT_OVERHEAD", "10000"))
    estimated_relevance_tokens = (
        notes_count * relevance_tokens_per_note
        + relevance_batches * relevance_prompt_overhead
    )
    estimated_annotation_tokens = (
        estimated_relevance_tokens
        + estimated_relevant_notes * int(request.estimated_tokens_per_note)
        + detail_annotation_batches * annotation_prompt_overhead
    )
    estimated_annotation_tokens_upper = (
        estimated_relevance_tokens
        + notes_count * int(request.estimated_tokens_per_note)
        + upper_annotation_batches * annotation_prompt_overhead
    )
    broad_enrichment_tokens = 0
    if request.mode == "broad_scan":
        broad_enrichment_tokens = (
            10 * int(os.getenv("XHS_EST_TOP10_COMMENT_BUNDLE_TOKENS", "1800"))
            + len(COMPETITOR_SCHOOLS) * int(os.getenv("XHS_EST_COMPETITOR_SCHOOL_TOKENS", "3000"))
        )
    estimated_analysis_tokens = (
        estimated_annotation_tokens
        + comments_count * int(request.estimated_tokens_per_comment)
        + analysis_prompt_overhead
        + broad_enrichment_tokens
        + int(request.estimated_report_tokens)
    )
    estimated_analysis_tokens_upper = (
        estimated_annotation_tokens_upper
        + comments_count * int(request.estimated_tokens_per_comment)
        + analysis_prompt_overhead
        + broad_enrichment_tokens
        + int(request.estimated_report_tokens)
    )
    return {
        "estimated_relevance_tokens": estimated_relevance_tokens,
        "estimated_relevant_notes": estimated_relevant_notes,
        "relevance_batches": relevance_batches,
        "estimated_annotation_tokens": estimated_annotation_tokens,
        "estimated_annotation_tokens_upper": estimated_annotation_tokens_upper,
        "annotation_batches": detail_annotation_batches,
        "detail_annotation_batches": detail_annotation_batches,
        "estimated_analysis_tokens": estimated_analysis_tokens,
        "estimated_analysis_tokens_upper": estimated_analysis_tokens_upper,
        "estimated_tokens": estimated_analysis_tokens,
    }


def _safety_limit_errors(request: ManagedRunRequest, estimate: dict[str, int]) -> list[str]:
    errors: list[str] = []
    if estimate["keyword_count"] > request.max_broad_keywords_limit:
        errors.append(f"keyword_count {estimate['keyword_count']} > limit {request.max_broad_keywords_limit}")
    if estimate["max_pages"] > request.max_pages_limit:
        errors.append(f"max_pages {estimate['max_pages']} > limit {request.max_pages_limit}")
    if estimate["max_notes"] > request.max_notes_limit:
        errors.append(f"max_notes {estimate['max_notes']} > limit {request.max_notes_limit}")
    if estimate["comment_pages"] > request.max_comment_pages_limit:
        errors.append(f"comment_pages {estimate['comment_pages']} > limit {request.max_comment_pages_limit}")
    if estimate["sub_comment_pages"] > request.max_sub_comment_pages_limit:
        errors.append(
            f"sub_comment_pages {estimate['sub_comment_pages']} > limit {request.max_sub_comment_pages_limit}"
        )
    return errors


def _require_run_dir(context: dict[str, Any]) -> str:
    run_dir = context.get("run_dir")
    if not run_dir:
        raise RuntimeError("run_dir is missing; collect step must complete first")
    return str(run_dir)


def _usage_total_from_file(path: str | Path) -> int:
    payload = read_json(path)
    rows = payload.get("usage") if isinstance(payload, dict) else []
    return sum(_usage_row_total(row) for row in rows if isinstance(row, dict))


def _usage_row_total(row: dict[str, Any]) -> int:
    if row.get("total_tokens") is not None:
        return int(row.get("total_tokens") or 0)
    return _sum_token_fields(row)


def _sum_token_fields(row: dict[str, Any]) -> int:
    return int(row.get("prompt_tokens") or 0) + int(row.get("completion_tokens") or 0)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    out = dict(row)
    out["config"] = json.loads(out.pop("config_json") or "{}")
    out["step_attempts"] = json.loads(out.pop("step_attempts_json") or "{}")
    out["events"] = json.loads(out.pop("events_json") or "[]")
    return out


def _redact_config(value: Any) -> Any:
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            if key.lower() in SENSITIVE_CONFIG_KEYS:
                result[key] = "[REDACTED]" if item else ""
            else:
                result[key] = _redact_config(item)
        return result
    if isinstance(value, list):
        return [_redact_config(item) for item in value]
    return value


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _collection_error_hint(errors: list[dict[str, Any]]) -> str:
    if not errors:
        return ""
    messages = [str(item.get("error") or "") for item in errors[-3:]]
    if any("令牌已过期" in message or "token" in message.lower() for message in messages):
        return ": TikHub API token expired or invalid"
    if any("403" in message for message in messages):
        return ": TikHub API returned 403"
    return f": {messages[-1][:180]}"
