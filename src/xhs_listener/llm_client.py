"""LLM 客户端：从旧项目 azure_openai_client.py 精简而来。"""
from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Optional

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:
    from openai import APIConnectionError, APITimeoutError, AzureOpenAI, BadRequestError, OpenAI, RateLimitError
except ImportError:  # pragma: no cover
    APIConnectionError = APITimeoutError = BadRequestError = RateLimitError = Exception  # type: ignore
    AzureOpenAI = OpenAI = None  # type: ignore

try:
    import httpx
except ImportError:  # pragma: no cover
    httpx = None  # type: ignore


# LLMClient 是项目唯一的模型调用封装。
# analyze.py 和 report.py 只关心 get_response / extract_json / usage_dict，
# 不需要知道底层用 Azure OpenAI 还是 OpenAI。
class LLMClient:
    """统一支持 Azure OpenAI / OpenAI；默认读取 .env。"""

    def __init__(self, config: Optional[dict[str, str]] = None) -> None:
        # 先加载项目根目录和当前工作目录的 .env，方便 CLI/前端两种启动方式。
        project_env = Path(__file__).resolve().parents[2] / ".env"
        if load_dotenv is not None:
            load_dotenv(project_env)
            load_dotenv()

        self.config = {key: value for key, value in (config or {}).items() if value}
        self.provider = (self._env("LLM_PROVIDER", "azure").strip().lower() or "azure")
        # 前端运行时更需要“快点失败并把状态吐出来”，否则一个卡住的模型请求
        # 会让页面长时间停在 running。需要更长等待时可在 .env 覆盖。
        timeout_sec = float(os.getenv("AZURE_OPENAI_TIMEOUT", "60"))
        if httpx is not None:
            request_timeout: Any = httpx.Timeout(
                timeout_sec,
                connect=float(os.getenv("AZURE_OPENAI_CONNECT_TIMEOUT", "10")),
                read=timeout_sec,
                write=float(os.getenv("AZURE_OPENAI_WRITE_TIMEOUT", "20")),
                pool=float(os.getenv("AZURE_OPENAI_POOL_TIMEOUT", "10")),
            )
        else:
            request_timeout = timeout_sec
        sdk_max_retries = int(os.getenv("AZURE_OPENAI_SDK_MAX_RETRIES", "0"))

        if self.provider == "openai":
            # OpenAI 模式：适合直接使用 OPENAI_API_KEY / OPENAI_MODEL。
            if OpenAI is None:
                raise RuntimeError("openai package is required. Run: pip install openai")
            self.client = OpenAI(
                api_key=self._env("OPENAI_API_KEY"),
                base_url=self._env("OPENAI_BASE_URL", "https://api.openai.com/v1"),
                timeout=request_timeout,
                max_retries=sdk_max_retries,
            )
            self.model = self._env("OPENAI_MODEL", "gpt-4.1-mini")
        else:
            # Azure 模式：默认路径，读取 AZURE_OPENAI_* 变量和部署名。
            if AzureOpenAI is None:
                raise RuntimeError("openai package is required. Run: pip install openai")
            self.client = AzureOpenAI(
                azure_endpoint=self._env("AZURE_OPENAI_ENDPOINT"),
                api_key=self._env("AZURE_OPENAI_API_KEY"),
                api_version=self._env("AZURE_OPENAI_API_VERSION"),
                timeout=request_timeout,
                max_retries=sdk_max_retries,
            )
            self.model = self._env("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")

    def _env(self, key: str, default: str = "") -> str:
        """前端临时配置优先，其次读取本机 .env/环境变量。"""

        return self.config.get(key) or os.getenv(key, default)

    def get_response(
        self,
        messages: list[dict[str, Any]],
        max_retries: int = 2,
        base_delay: float = 2.0,
        hard_timeout_s: Optional[float] = None,
    ):
        """调用 chat completions；网络/限流错误做指数退避。

        环境变量 AZURE_OPENAI_HTTP_MAX_RETRIES 可覆盖尝试次数（至少 1 次）。
        失败时返回 "Error: ..." 字符串，调用方需检查 isinstance(response, str)。
        """

        max_retries = max(1, int(os.getenv("AZURE_OPENAI_HTTP_MAX_RETRIES", str(max_retries))))
        base_delay = float(os.getenv("AZURE_OPENAI_HTTP_BASE_DELAY", str(base_delay)))
        hard_timeout = float(hard_timeout_s or os.getenv("XHS_LLM_CALL_TIMEOUT", "75"))
        for attempt in range(max_retries):
            try:
                return self._completion_with_hard_timeout(messages, hard_timeout)
            except BadRequestError as exc:
                try:
                    message = exc.json_body.get("error", {}).get("message", str(exc))
                except Exception:
                    message = str(exc)
                return f"Error: {message}"
            except (APIConnectionError, APITimeoutError, RateLimitError) as exc:
                if attempt == max_retries - 1:
                    return f"Error: {exc}"
                time.sleep(base_delay * (2**attempt))
            except Exception as exc:  # noqa: BLE001
                if self._is_transient_network_error(exc) and attempt < max_retries - 1:
                    time.sleep(base_delay * (2**attempt))
                    continue
                return f"Error: {exc}"
        return "Error: LLM call failed without a response"

    def _completion_with_hard_timeout(self, messages: list[dict[str, Any]], timeout_sec: float) -> Any:
        """Protect the app from SDK/network calls that ignore request timeouts."""

        box: dict[str, Any] = {}

        def call() -> None:
            try:
                kwargs = {
                    "model": self.model,
                    "messages": messages,
                    "temperature": float(os.getenv("XHS_LLM_TEMPERATURE", "0")),
                }
                try:
                    box["response"] = self.client.chat.completions.create(**kwargs)
                except BadRequestError as exc:
                    # Some reasoning-model deployments accept only their default temperature.
                    if "temperature" not in str(exc).lower():
                        raise
                    kwargs.pop("temperature", None)
                    box["response"] = self.client.chat.completions.create(**kwargs)
            except BaseException as exc:  # noqa: BLE001
                box["error"] = exc

        thread = threading.Thread(target=call, daemon=True)
        thread.start()
        thread.join(timeout=max(1.0, timeout_sec))
        if thread.is_alive():
            raise TimeoutError(f"LLM call exceeded hard timeout {timeout_sec:.0f}s")
        if "error" in box:
            raise box["error"]
        return box.get("response")

    def usage_dict(self, response: Any) -> dict[str, Any]:
        """尽量从 response 中提取 token usage；没有 usage 时返回空 dict。"""

        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        return {
            "prompt_tokens": getattr(usage, "prompt_tokens", None),
            "completion_tokens": getattr(usage, "completion_tokens", None),
            "total_tokens": getattr(usage, "total_tokens", None),
            "model": self.model,
            "provider": self.provider,
        }

    @staticmethod
    def extract_json(text: str) -> Any:
        """从模型输出中提取 JSON，兼容 ```json 代码块。"""

        value = text.strip()
        if value.startswith("```"):
            value = value.strip("`").strip()
            if value.lower().startswith("json"):
                value = value[4:].strip()
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            start_candidates = [idx for idx in (value.find("{"), value.find("[")) if idx >= 0]
            if not start_candidates:
                raise
            start = min(start_candidates)
            end = max(value.rfind("}"), value.rfind("]"))
            return json.loads(value[start : end + 1])

    @staticmethod
    def _is_transient_network_error(exc: BaseException) -> bool:
        if httpx is not None:
            transient_types = (
                httpx.ConnectError,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
                httpx.RemoteProtocolError,
            )
            if isinstance(exc, transient_types):
                return True
        msg = str(exc).lower()
        return any(key in msg for key in ("connection", "timeout", "network", "reset by peer", "ssl"))
