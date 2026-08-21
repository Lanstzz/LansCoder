from __future__ import annotations

import ipaddress
from urllib import error, parse, request

from lanscoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from lanscoder.utils.introspection import tool_from_function
from lanscoder.utils.text import truncate

DEFAULT_FETCH_TIMEOUT_SECONDS = 20
DEFAULT_MAX_CHARS = 20000


def create_fetch_tool() -> Tool:

    def fetch(url: str, timeout_seconds: int = DEFAULT_FETCH_TIMEOUT_SECONDS, max_chars: int = DEFAULT_MAX_CHARS) -> ToolResult:
        """读取单个 http/https URL 的文本响应；不做网页搜索。"""

        parsed = parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            return make_error_result("fetch", "只支持 http 和 https URL")
        if _is_private_or_local_url(parsed):
            return make_error_result("fetch", "拒绝访问本机、内网或链路本地地址")
        if timeout_seconds <= 0:
            return make_error_result("fetch", "timeout_seconds 必须大于 0")
        if max_chars <= 0:
            return make_error_result("fetch", "max_chars 必须大于 0")

        req = request.Request(url, headers={"User-Agent": "LansCoder/0.1"})
        try:
            with request.urlopen(req, timeout=timeout_seconds) as response:
                body = response.read()
                status = getattr(response, "status", None)
                headers = dict(response.getheaders())
        except error.URLError as exc:
            return make_error_result("fetch", f"请求失败：{exc}")

        text = body.decode("utf-8", errors="replace")
        content, truncated = truncate(text, max_chars, suffix="\n\n[响应已截断]")

        return make_text_result(
            "fetch",
            content,
            url=url,
            status=status,
            headers=headers,
            truncated=truncated,
        )

    return tool_from_function(fetch)


def _is_private_or_local_url(parsed: parse.ParseResult) -> bool:
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if not hostname:
        return True
    if hostname in {"localhost", "ip6-localhost"} or hostname.endswith(".localhost"):
        return True
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local or address.is_multicast or address.is_unspecified or address.is_reserved
