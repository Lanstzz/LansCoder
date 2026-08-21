from __future__ import annotations

import os
from urllib import error, parse, request

from lanscoder.tools.types import Tool, ToolResult, make_error_result, make_text_result
from lanscoder.utils.introspection import tool_from_function
from lanscoder.utils.json_utils import dumps_json, loads_json

EXA_MCP_URL = "https://mcp.exa.ai/mcp"
PARALLEL_MCP_URL = "https://search.parallel.ai/mcp"
DEFAULT_TIMEOUT_SECONDS = 25
DEFAULT_NUM_RESULTS = 8
DEFAULT_CONTEXT_MAX_CHARACTERS = 10000
FIRST_SEARCH_PROVIDER = "parallel"


def create_web_search_tool() -> Tool:

    def web_search(
        query: str,
        num_results: int = DEFAULT_NUM_RESULTS,
        search_type: str = "auto",
        livecrawl: str = "fallback",
        context_max_characters: int = DEFAULT_CONTEXT_MAX_CHARACTERS,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> ToolResult:
        """搜索网页资料；适合最新信息和外部文档。"""

        if not query.strip():
            return make_error_result("web_search", "query 不能为空")
        if num_results <= 0:
            return make_error_result("web_search", "num_results 必须大于 0")
        if context_max_characters <= 0:
            return make_error_result("web_search", "context_max_characters 必须大于 0")
        if timeout_seconds <= 0:
            return make_error_result("web_search", "timeout_seconds 必须大于 0")
        livecrawl = _normalize_livecrawl(livecrawl)
        if search_type not in ("auto", "fast", "deep"):
            return make_error_result("web_search", "search_type 只能是 auto、fast 或 deep")
        if livecrawl not in ("fallback", "preferred"):
            return make_error_result("web_search", "livecrawl 只能是 fallback 或 preferred")

        provider = os.environ.get("LANSCODER_WEBSEARCH_PROVIDER") or FIRST_SEARCH_PROVIDER
        providers_to_try = ["parallel", "exa"]
        if provider == "exa":
            providers_to_try = ["exa", "parallel"]

        last_error = None
        for prov in providers_to_try:
            if prov == "exa" and not os.environ.get("EXA_API_KEY"):
                continue
            try:
                if prov == "parallel":
                    result = _run_parallel_search(query, num_results, timeout_seconds)
                else:
                    result = _run_exa_search(query, num_results, search_type, livecrawl, context_max_characters, timeout_seconds)
                if result is not None:
                    return result
            except Exception as exc:
                last_error = str(exc)
                continue

        return make_error_result("web_search", f"所有搜索 provider 均不可用：{last_error or '无可用 provider'}")

    tool = tool_from_function(web_search)
    tool.definition.parameters["properties"]["search_type"]["enum"] = ["auto", "fast", "deep"]
    tool.definition.parameters["properties"]["livecrawl"]["enum"] = ["fallback", "preferred"]
    return tool


def _run_parallel_search(query: str, num_results: int, timeout: int) -> ToolResult | None:
    body = dumps_json(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search",
                "arguments": {
                    "query": query,
                    "search_queries": [query],
                    "objective": query,
                    "num_results": num_results,
                },
            },
        }
    ).encode("utf-8")
    headers = {"Content-Type": "application/json", "User-Agent": "LansCoder/0.1"}
    api_key = os.environ.get("PARALLEL_API_KEY")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        with request.urlopen(
            request.Request(PARALLEL_MCP_URL, data=body, method="POST", headers=headers),
            timeout=timeout,
        ) as resp:
            raw = resp.read()
    except (error.URLError, error.HTTPError):
        return None
    text = raw.decode("utf-8", errors="replace")
    result = parse_mcp_search_response(text)
    if not result:
        return None
    return make_text_result("web_search", result, provider="parallel", query=query)


def _normalize_livecrawl(value: str) -> str:
    normalized = str(value).strip().lower()
    if normalized in {"false", "0", "no", "off"}:
        return "fallback"
    if normalized in {"true", "1", "yes", "on"}:
        return "preferred"
    return value


def _run_exa_search(
    query: str,
    num_results: int,
    search_type: str,
    livecrawl: str,
    context_max_characters: int,
    timeout: int,
) -> ToolResult | None:
    url = _exa_mcp_url()
    body = dumps_json(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "web_search_exa",
                "arguments": {
                    "query": query,
                    "type": search_type,
                    "numResults": num_results,
                    "livecrawl": livecrawl,
                    "contextMaxCharacters": context_max_characters,
                },
            },
        }
    ).encode("utf-8")
    req = request.Request(
        url,
        data=body,
        method="POST",
        headers={"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "User-Agent": "LansCoder/0.1"},
    )
    try:
        with request.urlopen(req, timeout=timeout) as response:
            raw_body = response.read()
    except error.URLError:
        return None
    text = raw_body.decode("utf-8", errors="replace")
    result = parse_mcp_search_response(text)
    if not result:
        return None
    return make_text_result("web_search", result, provider="exa", query=query, url=_redact_url(url))


def _exa_mcp_url() -> str:

    api_key = os.getenv("EXA_API_KEY")
    if not api_key:
        return EXA_MCP_URL
    return f"{EXA_MCP_URL}?exaApiKey={parse.quote(api_key, safe='')}"


def _redact_url(url: str) -> str:

    parsed = parse.urlparse(url)
    query = parse.parse_qsl(parsed.query, keep_blank_values=True)
    redacted = [(key, "***" if key.lower() in {"exaapikey", "apikey", "api_key", "key"} else value) for key, value in query]
    return parse.urlunparse(parsed._replace(query=parse.urlencode(redacted)))


def parse_mcp_search_response(body: str) -> str | None:

    trimmed = body.strip()
    if trimmed:
        direct = _parse_payload(trimmed)
        if direct:
            return direct

    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        data = _parse_payload(line.removeprefix("data: "))
        if data:
            return data
    return None


def _parse_payload(payload: str) -> str | None:

    trimmed = payload.strip()
    if not trimmed.startswith("{"):
        return None
    try:
        data = loads_json(trimmed)
    except ValueError:
        return None

    result = data.get("result") if isinstance(data, dict) else None
    content = result.get("content") if isinstance(result, dict) else None
    if not isinstance(content, list):
        return None

    for item in content:
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str) and text:
            return text
    return None
