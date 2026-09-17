"""Supabase-backed run metadata and durable run artifacts."""
from __future__ import annotations

import io
import json
import os
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from xhs_listener.paths import data_root


RUN_COLUMNS = {
    "mode",
    "status",
    "current_step",
    "run_dir",
    "config",
    "step_attempts",
    "budget_tokens",
    "usage_tokens",
    "stop_reason",
    "error",
    "report_json",
    "report_html",
    "report_json_en",
    "report_html_en",
    "events",
    "created_at",
    "started_at",
    "finished_at",
    "updated_at",
    "artifact_key",
}


class SupabaseRunStore:
    """RunStore-compatible repository using Supabase PostgREST and Storage."""

    def __init__(self) -> None:
        url = (os.getenv("SUPABASE_URL") or "").strip()
        key = (os.getenv("SUPABASE_SECRET_KEY") or "").strip()
        bucket = (os.getenv("SUPABASE_STORAGE_BUCKET") or "reports").strip()
        if not url or not key:
            raise RuntimeError("SUPABASE_URL and SUPABASE_SECRET_KEY are required")
        try:
            from supabase import Client, create_client
        except ImportError as exc:  # pragma: no cover - dependency installation issue
            raise RuntimeError("supabase package is required for cloud storage") from exc
        self.client: Client = create_client(url, key)
        self.bucket = bucket
        self.cache_root = data_root() / "cloud_runs"
        self.cache_root.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _init_schema(self) -> None:
        response = self.client.table("runs").select("id").limit(1).execute()
        if response.data is None:  # pragma: no cover - defensive client behavior
            raise RuntimeError("Supabase runs table is unavailable")

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat(timespec="seconds")

    @staticmethod
    def _row(row: dict[str, Any]) -> dict[str, Any]:
        out = dict(row)
        for field, default in (("config", {}), ("step_attempts", {}), ("events", [])):
            value = out.get(field)
            if isinstance(value, str):
                try:
                    out[field] = json.loads(value or json.dumps(default))
                except json.JSONDecodeError:
                    out[field] = default
            elif value is None:
                out[field] = default
        return out

    def _with_artifacts(self, row: dict[str, Any]) -> dict[str, Any]:
        row = self._row(row)
        key = str(row.get("artifact_key") or "")
        if key:
            self.ensure_artifacts(row)
            local_dir = self.cache_root / str(row["id"])
            row["run_dir"] = str(local_dir)
            for field, filename in (
                ("report_json", "report.json"),
                ("report_html", "report.html"),
                ("report_json_en", "report_en.json"),
                ("report_html_en", "report_en.html"),
            ):
                if (local_dir / filename).exists():
                    row[field] = str(local_dir / filename)
        return row

    def create_run(self, request: Any) -> dict[str, Any]:
        now = self._now()
        payload = {
            "mode": request.mode,
            "status": "queued",
            "config": _redact_config(request),
            "step_attempts": {},
            "budget_tokens": int(request.budget_tokens or 0),
            "usage_tokens": 0,
            "events": [],
            "created_at": now,
            "updated_at": now,
        }
        response = self.client.table("runs").insert(payload).execute()
        return self._with_artifacts(response.data[0])

    def get_run(self, run_id: int) -> dict[str, Any]:
        response = self.client.table("runs").select("*").eq("id", int(run_id)).limit(1).execute()
        if not response.data:
            raise KeyError(f"run {run_id} not found")
        return self._with_artifacts(response.data[0])

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        response = (
            self.client.table("runs")
            .select("*")
            .order("created_at", desc=True)
            .order("id", desc=True)
            .limit(int(limit))
            .execute()
        )
        return [self._with_artifacts(row) for row in (response.data or [])]

    def list_reports(self, limit: int = 50) -> list[dict[str, Any]]:
        response = (
            self.client.table("runs")
            .select("*")
            .eq("status", "succeeded")
            .not_.is_("report_html", "null")
            .order("finished_at", desc=True)
            .order("id", desc=True)
            .limit(int(limit))
            .execute()
        )
        return [self._with_artifacts(row) for row in (response.data or [])]

    def update_run(self, run_id: int, **fields: Any) -> dict[str, Any]:
        if fields:
            if "config_json" in fields:
                fields["config"] = fields.pop("config_json")
            if "step_attempts_json" in fields:
                fields["step_attempts"] = _decode_json_field(fields.pop("step_attempts_json"), {})
            if "events_json" in fields:
                fields["events"] = _decode_json_field(fields.pop("events_json"), [])
            fields = {key: value for key, value in fields.items() if key in RUN_COLUMNS}
            fields["updated_at"] = self._now()
            response = self.client.table("runs").update(fields).eq("id", int(run_id)).select("*").execute()
            if not response.data:
                raise KeyError(f"run {run_id} not found")
        return self.get_run(run_id)

    def increment_step_attempt(self, run_id: int, step: str) -> int:
        run = self.get_run(run_id)
        attempts = dict(run.get("step_attempts") or {})
        attempts[step] = int(attempts.get(step, 0)) + 1
        self.update_run(run_id, step_attempts=attempts)
        return attempts[step]

    def append_event(self, run_id: int, message: str, step: Optional[str] = None, level: str = "info") -> dict[str, Any]:
        run = self.get_run(run_id)
        events = list(run.get("events") or [])
        events.append({"time": self._now(), "level": level, "step": step, "message": message})
        return self.update_run(run_id, events=events[-300:])

    def mark_interrupted_runs(self, reason: str = "server_restarted") -> int:
        now = self._now()
        response = (
            self.client.table("runs")
            .update({"status": "stopped", "stop_reason": reason, "error": reason, "current_step": None, "finished_at": now, "updated_at": now})
            .in_("status", ["queued", "running"])
            .select("id")
            .execute()
        )
        return len(response.data or [])

    def sync_artifacts(self, run_id: int, run_dir: str | Path) -> str:
        source = Path(run_dir)
        if not source.exists():
            raise FileNotFoundError(source)
        archive = io.BytesIO()
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as bundle:
            for path in source.rglob("*"):
                if path.is_file():
                    bundle.write(path, path.relative_to(source).as_posix())
        key = f"runs/{int(run_id)}.zip"
        self.client.storage.from_(self.bucket).upload(key, archive.getvalue(), {"upsert": "true", "content-type": "application/zip"})
        self.update_run(run_id, artifact_key=key)
        return key

    def ensure_artifacts(self, run: dict[str, Any]) -> Path | None:
        key = str(run.get("artifact_key") or "")
        if not key:
            return None
        target = self.cache_root / str(run["id"])
        if (target / "report.html").exists():
            return target
        target.mkdir(parents=True, exist_ok=True)
        payload = self.client.storage.from_(self.bucket).download(key)
        with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as handle:
            handle.write(payload)
            archive_path = handle.name
        try:
            with zipfile.ZipFile(archive_path) as bundle:
                bundle.extractall(target)
        finally:
            Path(archive_path).unlink(missing_ok=True)
        return target


def _redact_config(request: Any) -> dict[str, Any]:
    from dataclasses import asdict
    from xhs_listener.run_manager import _redact_config as redact

    return redact(asdict(request))


def _decode_json_field(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default
