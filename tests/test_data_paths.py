"""共享报告库的数据落盘位置：本地默认 / 显式覆盖 / Azure 持久化存储。

产品前提：报告库是**团队共享**的，不按用户分目录 —— 同一个部署下所有登录用户
查询同一份 sqlite、读同一批 report.html。
"""
from __future__ import annotations

from pathlib import Path

from xhs_listener.paths import (
    AZURE_DATA_DIR,
    AZURE_MARKER_ENV,
    DATA_DIR_ENV,
    LOCAL_DATA_DIR,
    is_azure_app_service,
    resolve_data_root,
    runs_db_path,
    runs_dir,
)


def test_local_development_uses_the_repo_data_directory() -> None:
    assert resolve_data_root({}) == LOCAL_DATA_DIR == Path("data")
    assert is_azure_app_service({}) is False


def test_explicit_data_dir_wins_everywhere() -> None:
    assert resolve_data_root({DATA_DIR_ENV: "/custom/dir"}) == Path("/custom/dir")
    # 即使在 Azure 上，显式配置也优先。
    assert resolve_data_root({DATA_DIR_ENV: "/custom/dir", AZURE_MARKER_ENV: "app"}) == Path("/custom/dir")
    # 空字符串不算配置，回落到默认。
    assert resolve_data_root({DATA_DIR_ENV: "   "}) == LOCAL_DATA_DIR


def test_azure_app_service_uses_persistent_home_storage() -> None:
    env = {AZURE_MARKER_ENV: "socialmediahearing"}

    assert is_azure_app_service(env) is True
    root = resolve_data_root(env)
    assert root == AZURE_DATA_DIR == Path("/home/data")
    # 必须在 /home 下：只有这里才跨重启与跨部署保留。
    # 站点目录 /home/site/wwwroot 每次部署都会被覆盖，不能放数据。
    assert str(root).startswith("/home/")
    assert "wwwroot" not in str(root)


def test_all_artifacts_share_one_data_root(tmp_path) -> None:
    env = {DATA_DIR_ENV: str(tmp_path)}

    assert runs_db_path(env) == tmp_path / "runs.sqlite3"
    assert runs_dir(env) == tmp_path / "runs"
    assert runs_db_path(env).parent == runs_dir(env).parent == resolve_data_root(env)


def test_run_store_and_report_paths_share_the_same_root(tmp_path, monkeypatch) -> None:
    """RunStore 与 run 产物目录必须落在同一个 data root，报告才打得开。"""

    from xhs_listener.run_manager import ManagedRunRequest, RunStore

    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))

    store = RunStore()
    assert store.db_path == tmp_path / "runs.sqlite3"
    assert store.db_path.exists()

    # 未显式指定 output_root 时，run 目录默认落在同一个 root 下。
    request = ManagedRunRequest(mode="topic_scan")
    assert request.output_root == ""
    assert runs_dir().parent == store.db_path.parent


def test_shared_library_is_not_partitioned_per_user(tmp_path, monkeypatch) -> None:
    """两个不同会话/用户读到的必须是同一个库，不存在按用户分目录。"""

    from xhs_listener.run_manager import ManagedRunRequest, RunStore

    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path))

    user_a_store = RunStore()
    created = user_a_store.create_run(ManagedRunRequest(mode="topic_scan"))
    user_a_store.update_run(
        created["id"], status="succeeded", report_html=str(tmp_path / "runs" / "r1" / "report.html")
    )

    # 另一个用户的会话新建自己的 RunStore，仍指向同一个文件。
    user_b_store = RunStore()
    assert user_b_store.db_path == user_a_store.db_path
    assert [row["id"] for row in user_b_store.list_reports()] == [created["id"]]


def test_data_root_is_resolved_lazily_not_frozen_at_import(monkeypatch, tmp_path) -> None:
    """路径解析必须读调用时的环境，否则 Azure App Settings 不会生效。"""

    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "first"))
    assert resolve_data_root() == tmp_path / "first"

    monkeypatch.setenv(DATA_DIR_ENV, str(tmp_path / "second"))
    assert resolve_data_root() == tmp_path / "second"
