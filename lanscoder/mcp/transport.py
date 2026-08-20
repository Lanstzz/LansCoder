from __future__ import annotations

from contextlib import AsyncExitStack
import os
from typing import Any, Mapping, Protocol

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.client.streamable_http import streamable_http_client
from mcp.shared._httpx_utils import create_mcp_http_client

from lanscoder.mcp.models import McpLocalServerConfig, McpRemoteServerConfig, McpToolDescription


def _stdio_environment(config_environment: Mapping[str, str]) -> dict[str, str]:

    environment = dict(os.environ)
    environment.update(config_environment)
    return environment


class McpTransport(Protocol):

    async def connect(self) -> None: ...

    async def list_tools(self) -> tuple[McpToolDescription, ...]: ...

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object: ...

    async def close(self) -> None: ...


class McpTransportFactory(Protocol):

    def create(self, config: McpLocalServerConfig | McpRemoteServerConfig) -> McpTransport: ...


class SdkMcpTransportFactory:

    def create(self, config: McpLocalServerConfig | McpRemoteServerConfig) -> McpTransport:
        if isinstance(config, McpLocalServerConfig):
            return _StdioMcpTransport(config)
        return _StreamableHttpMcpTransport(config)


class _SdkMcpTransport:

    def __init__(self) -> None:
        self._stack: AsyncExitStack | None = None
        self._session: ClientSession | None = None

    async def connect(self) -> None:
        stack = AsyncExitStack()
        try:
            read_stream, write_stream = await self._open_streams(stack)
            session = await stack.enter_async_context(ClientSession(read_stream, write_stream))
            await session.initialize()
        except BaseException:
            await stack.aclose()
            raise
        self._stack = stack
        self._session = session

    async def list_tools(self) -> tuple[McpToolDescription, ...]:
        session = self._require_session()
        result = await session.list_tools()
        return tuple(
            McpToolDescription(
                name=tool.name,
                description=tool.description,
                input_schema=dict(tool.inputSchema),
            )
            for tool in result.tools
        )

    async def call_tool(self, name: str, arguments: dict[str, object]) -> object:
        return await self._require_session().call_tool(name, arguments)

    async def close(self) -> None:
        if self._stack is not None:
            stack, self._stack = self._stack, None
            self._session = None
            await stack.aclose()

    def _require_session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("MCP 传输尚未连接")
        return self._session

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        raise NotImplementedError


class _StdioMcpTransport(_SdkMcpTransport):

    def __init__(self, config: McpLocalServerConfig) -> None:
        super().__init__()
        self._config = config

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        parameters = StdioServerParameters(
            command=self._config.command[0],
            args=list(self._config.command[1:]),
            env=_stdio_environment(self._config.env),
        )
        return await stack.enter_async_context(stdio_client(parameters))


class _StreamableHttpMcpTransport(_SdkMcpTransport):

    def __init__(self, config: McpRemoteServerConfig) -> None:
        super().__init__()
        self._config = config

    async def _open_streams(self, stack: AsyncExitStack) -> tuple[Any, Any]:
        http_client = await stack.enter_async_context(create_mcp_http_client(headers=dict(self._config.headers)))
        read_stream, write_stream, _ = await stack.enter_async_context(streamable_http_client(self._config.url, http_client=http_client))
        return read_stream, write_stream
