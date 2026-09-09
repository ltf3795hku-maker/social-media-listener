"""生成英文报告的编排层：DB 迁移 + RunManager.generate_english_report + service 包一层。"""
from __future__ import annotations

import json
import sqlite3
import types
from pathlib import Path

import pytest

from xhs_listener.io_utils import write_json
from xhs_listener.run_manager import ManagedRunRequest, RunManager, RunStore


class _EchoTranslateClient:
    def __init__(self, config=None) -> None:
        self.config = config

    def get_response(self, messages):
        prompt = messages[0]["content"]
        batch = json.loads(prompt.split("Input array:\n", 1)[1])
        content = json.dumps([f"EN:{item}" for item in batch], ensure_ascii=False)
        message = types.SimpleNamespace(content=content)
        return types.SimpleNamespace(choices=[types.SimpleNamespace(message=message)], usage=None)

    def usage_dict(self, response):
        return {"total_tokens": 5, "phase": "report_translate_en"}

    def extract_json(self, text):
        return json.loads(text)


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
    (run_path / "report.html").write_text("<html lang=\"zh-CN\"><body>港大商学院周报</body></html>", encoding="utf-8")


# --------------------------------------------------------------------------
# DB schema migration
# --------------------------------------------------------------------------


def test_fresh_runs_table_has_english_report_columns(tmp_path: Path) -> None:
    store = RunStore(tmp_path / "runs.sqlite3")
    with store._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert {"report_json_en", "report_html_en"}.issubset(columns)


def test_legacy_database_without_english_columns_gets_migrated(tmp_path: Path) -> None:
    """模拟用户已经在用的旧 runs.sqlite3（还没有英文报告这两列）：
    RunStore 打开时应该自动 ALTER TABLE 补上，不需要用户手动迁移，也不能丢数据。"""

    db_path = tmp_path / "legacy.sqlite3"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE runs (
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
        conn.execute(
            "INSERT INTO runs (mode, status, config_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            ("broad_scan", "succeeded", "{}", "2026-08-01T00:00:00", "2026-08-01T00:00:00"),
        )

    store = RunStore(db_path)  # 打开已有的旧库，触发迁移
    with store._connect() as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(runs)")}
    assert {"report_json_en", "report_html_en"}.issubset(columns)

    # 老数据没丢。
    run = store.get_run(1)
    assert run["mode"] == "broad_scan"
    assert run.get("report_json_en") is None


# --------------------------------------------------------------------------
# RunManager.generate_english_report
# --------------------------------------------------------------------------


def test_generate_english_report_requires_chinese_report_first(tmp_path: Path, monkeypatch) -> None:
    manager = RunManager(RunStore(tmp_path / "runs.sqlite3"))
    run = manager.store.create_run(ManagedRunRequest(mode="topic_scan"))
    manager.store.update_run(run["id"], run_dir=str(tmp_path), status="collected")

    monkeypatch.setattr("xhs_listener.llm_client.LLMClient", _EchoTranslateClient)

    with pytest.raises(RuntimeError):
        manager.generate_english_report(run["id"], ManagedRunRequest(mode="topic_scan"))


def test_generate_english_report_writes_files_and_updates_run_row(tmp_path: Path, monkeypatch) -> None:
    manager = RunManager(RunStore(tmp_path / "runs.sqlite3"))
    run = manager.store.create_run(ManagedRunRequest(mode="topic_scan"))
    manager.store.update_run(
        run["id"],
        run_dir=str(tmp_path),
        status="succeeded",
        report_json=str(tmp_path / "report.json"),
        report_html=str(tmp_path / "report.html"),
    )
    _write_minimal_report_fixture(tmp_path)

    monkeypatch.setattr("xhs_listener.llm_client.LLMClient", _EchoTranslateClient)

    updated = manager.generate_english_report(run["id"], ManagedRunRequest(mode="topic_scan"))

    assert updated["report_json_en"] == str(tmp_path / "report_en.json")
    assert updated["report_html_en"] == str(tmp_path / "report_en.html")
    assert (tmp_path / "report_en.json").exists()
    assert (tmp_path / "report_en.html").exists()
    # 中文报告本身不受影响。
    assert updated["report_html"] == str(tmp_path / "report.html")


# --------------------------------------------------------------------------
# service.generate_english_report：薄封装，用临时 STORE/MANAGER 避免碰真实数据目录
# --------------------------------------------------------------------------


def test_service_generate_english_report_requires_chinese_report(tmp_path: Path, monkeypatch) -> None:
    from xhs_listener import service

    store = RunStore(tmp_path / "runs.sqlite3")
    manager = RunManager(store)
    monkeypatch.setattr(service, "STORE", store)
    monkeypatch.setattr(service, "MANAGER", manager)

    run = store.create_run(ManagedRunRequest(mode="topic_scan"))
    store.update_run(run["id"], run_dir=str(tmp_path), status="collected")

    with pytest.raises(ValueError):
        service.generate_english_report(run["id"])


def test_service_generate_english_report_happy_path(tmp_path: Path, monkeypatch) -> None:
    from xhs_listener import service

    store = RunStore(tmp_path / "runs.sqlite3")
    manager = RunManager(store)
    monkeypatch.setattr(service, "STORE", store)
    monkeypatch.setattr(service, "MANAGER", manager)
    monkeypatch.setattr("xhs_listener.llm_client.LLMClient", _EchoTranslateClient)

    run = store.create_run(ManagedRunRequest(mode="topic_scan"))
    store.update_run(
        run["id"],
        run_dir=str(tmp_path),
        status="succeeded",
        report_json=str(tmp_path / "report.json"),
        report_html=str(tmp_path / "report.html"),
    )
    _write_minimal_report_fixture(tmp_path)

    result = service.generate_english_report(run["id"])

    assert result["report_html_en"] == str(tmp_path / "report_en.html")
