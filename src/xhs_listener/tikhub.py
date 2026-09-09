from __future__ import annotations

import json
import re
import time
from typing import Any, Optional
from urllib import parse, request
from urllib.error import HTTPError, URLError


# 搜索接口顺序：新版 App-V2 优先，旧 App 和 Web-V3 作为 fallback。
APP_V2_SEARCH = "/api/v1/xiaohongshu/app_v2/search_notes"
APP_SEARCH = "/api/v1/xiaohongshu/app/search_notes"
WEB_V3_SEARCH = "/api/v1/xiaohongshu/web_v3/fetch_search_notes"

# 图文详情：当前只抓普通图文笔记，不主动进入视频详情。
APP_V2_IMAGE_DETAIL = "/api/v1/xiaohongshu/app_v2/get_image_note_detail"
APP_DETAIL = "/api/v1/xiaohongshu/app/get_note_info"
WEB_V3_DETAIL = "/api/v1/xiaohongshu/web_v3/fetch_note_detail"

# 评论接口：新版 App-V2 优先；旧 App/Web-V3 保留兜底。
APP_V2_COMMENTS = "/api/v1/xiaohongshu/app_v2/get_note_comments"
APP_V2_SUB_COMMENTS = "/api/v1/xiaohongshu/app_v2/get_note_sub_comments"
APP_COMMENTS = "/api/v1/xiaohongshu/app/get_note_comments"
WEB_V3_COMMENTS = "/api/v1/xiaohongshu/web_v3/fetch_note_comments"

# TikHubClient 是项目唯一的 TikHub 出口。
# 其他模块不直接拼 HTTP 请求，统一通过这里加 token、重试、fallback 和错误脱敏。
class TikHubClient:
    """TikHub 的极简 GET JSON 客户端，统一处理 token、host fallback 和重试。"""

    def __init__(
        self,
        api_token: str,
        host: str = "https://api.tikhub.io",
        timeout_s: int = 45,
    ) -> None:
        cleaned = api_token.strip()
        if cleaned.lower().startswith("bearer "):
            cleaned = cleaned[7:].strip()
        if not cleaned:
            raise ValueError("TikHub API token is required")
        self.api_token = cleaned
        self.host_candidates = self._build_host_candidates(host.rstrip("/"))
        self.timeout_s = timeout_s
        self._opener = request.build_opener(request.ProxyHandler({}))

    @staticmethod
    def _build_host_candidates(primary: str) -> list[str]:
        """主 host 不通时，依次尝试 TikHub 常见备用域名。"""

        candidates = [primary, "https://api.tikhub.io", "https://api.tikhub.dev"]
        out: list[str] = []
        for host in candidates:
            if host and host not in out:
                out.append(host)
        return out

    def get_json(self, path: str, params: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        """发起 GET 请求，并过滤掉空参数，减少接口误判。"""

        clean = {key: value for key, value in (params or {}).items() if value is not None and value != ""}
        query = parse.urlencode(clean, doseq=True, encoding="utf-8")
        last_exc: Optional[Exception] = None

        # host_candidates 允许主域名不可用时自动尝试备用域名。
        for host in self.host_candidates:
            url = f"{host}{path}" if not query else f"{host}{path}?{query}"
            req = request.Request(url=url, method="GET", headers=self._headers())
            try:
                return self._read_json(req, path)
            except RuntimeError:
                raise
            except (URLError, TimeoutError, OSError) as exc:
                last_exc = exc
        if last_exc is not None:
            raise last_exc
        raise RuntimeError(f"TikHub request failed on {path}")

    def call(self, path: str, params: dict[str, Any], retries: int = 3) -> dict[str, Any]:
        """带简单退避的调用封装，适合批量采集时遇到临时失败。"""

        delay = 1.5
        last_exc: Optional[Exception] = None
        for attempt in range(retries):
            try:
                return self.get_json(path, params)
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if attempt == retries - 1 or not _should_retry(exc):
                    raise
                time.sleep(delay)
                delay *= 1.5
        raise last_exc  # pragma: no cover

    def _headers(self) -> dict[str, str]:
        """TikHub 鉴权和基础浏览器头。"""

        return {
            "Authorization": f"Bearer {self.api_token}",
            "Accept": "application/json",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
        }

    def _read_json(self, req: request.Request, path: str) -> dict[str, Any]:
        """读取响应 JSON；HTTP 错误会补充 TikHub 返回的 request_id/message。"""

        try:
            with self._opener.open(req, timeout=self.timeout_s) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except HTTPError as exc:
            detail = self._read_error_detail(exc)
            raise RuntimeError(f"TikHub API HTTP {exc.code} on {path}.{detail}") from exc

    @staticmethod
    def _read_error_detail(exc: HTTPError) -> str:
        """从 TikHub 错误响应里提取可排查的信息。"""

        try:
            raw = exc.read().decode("utf-8", errors="ignore")
        except Exception:  # noqa: BLE001
            return ""
        if not raw:
            return ""
        try:
            payload = json.loads(raw)
            detail = payload.get("detail", payload)
            if isinstance(detail, dict):
                message = detail.get("message_zh") or detail.get("message")
                return (
                    f" request_id={detail.get('request_id')}; "
                    f"message={_redact_sensitive(message)}; "
                    f"docs={detail.get('docs')}"
                )
            return f" detail={_redact_sensitive(detail)}"
        except Exception:  # noqa: BLE001
            return f" body={_redact_sensitive(raw[:500])}"


def _should_retry(exc: Exception) -> bool:
    """token 失效、参数错误等 4xx 重试只会浪费请求额度；429 和 5xx 才值得退避重试。"""

    match = re.search(r"HTTP (\d{3})", str(exc))
    if match:
        code = int(match.group(1))
        return code == 429 or code >= 500
    return True


def _redact_sensitive(value: Any) -> str:
    """避免把 TikHub 返回的 token/authorization 原样写入 collection.json。"""

    text = "" if value is None else str(value)
    text = re.sub(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{16,}", r"\1[REDACTED]", text)
    text = re.sub(r"(API令牌为\s*)[A-Za-z0-9._~+/=-]{16,}", r"\1[REDACTED]", text)
    text = re.sub(r"(api token is\s*)[A-Za-z0-9._~+/=-]{16,}", r"\1[REDACTED]", text, flags=re.I)
    return text
