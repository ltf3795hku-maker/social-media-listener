"""所有运行数据落盘位置的唯一入口。

报告库是**共享**的：同一个部署下所有登录用户看到同一份历史，不做按用户分目录。
因此 sqlite、run 目录、report.html / report.json 必须落在同一个 data root 下。

选择顺序（从高到低）：

1. 显式 ``XHS_DATA_DIR``  —— 本地调试或运维想指定位置时用；
2. Azure App Service    —— 落到 ``/home/data``（``/home`` 是 App Service 唯一
   跨重启与跨部署保留的持久化共享存储，同一 App 的多个实例挂载同一份）；
3. 本地默认            —— 仓库下的 ``data/``。

这里刻意不放任何密钥：只放运行产物。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Mapping, Optional

# 本地开发默认目录（相对当前工作目录，保持既有行为）。
LOCAL_DATA_DIR = Path("data")

# Azure App Service 的持久化共享存储挂载点。
# 站点目录（/home/site/wwwroot）会被部署覆盖，只有 /home 下的其他路径能跨部署保留。
AZURE_PERSISTENT_ROOT = Path("/home")
AZURE_DATA_DIR = AZURE_PERSISTENT_ROOT / "data"

# App Service 一定会注入这个变量，用它判断是否运行在 Azure 上。
AZURE_MARKER_ENV = "WEBSITE_SITE_NAME"

DATA_DIR_ENV = "XHS_DATA_DIR"


def is_azure_app_service(env: Optional[Mapping[str, str]] = None) -> bool:
    """是否运行在 Azure App Service 上。"""

    environ = os.environ if env is None else env
    return bool(str(environ.get(AZURE_MARKER_ENV) or "").strip())


def resolve_data_root(env: Optional[Mapping[str, str]] = None) -> Path:
    """返回本次运行应使用的 data root（纯函数，便于测试）。"""

    environ = os.environ if env is None else env
    explicit = str(environ.get(DATA_DIR_ENV) or "").strip()
    if explicit:
        return Path(explicit).expanduser()
    if is_azure_app_service(environ):
        return AZURE_DATA_DIR
    return LOCAL_DATA_DIR


def data_root(env: Optional[Mapping[str, str]] = None) -> Path:
    """data root，并确保目录存在。"""

    root = resolve_data_root(env)
    root.mkdir(parents=True, exist_ok=True)
    return root


def runs_db_path(env: Optional[Mapping[str, str]] = None) -> Path:
    """共享报告库的 sqlite 路径。"""

    return data_root(env) / "runs.sqlite3"


def runs_dir(env: Optional[Mapping[str, str]] = None) -> Path:
    """run 目录根：report.html / report.json 等产物都在各自 run 目录里。"""

    return data_root(env) / "runs"
