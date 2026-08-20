"""MCP 管理器:管理 MCP 服务器的连接生命周期、工具目录与跨线程调用,运行在独立事件循环线程。"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import threading
import time
from concurrent.futures import Future, TimeoutError as FutureTimeoutError
from dataclasses import replace
from typing import Coroutine, Literal, Mapping

from lanscoder.mcp.config import resolve_environment_placeholders
from lanscoder.mcp.models import McpConfigError, McpLocalServerConfig, McpRemoteServerConfig, McpServerStatus, McpToolDescription
from lanscoder.mcp.transport import McpTransport, McpTransportFactory, SdkMcpTransportFactory

McpServerConfig = McpLocalServerConfig | McpRemoteServerConfig


class McpManager:
    """管理 MCP 服务器:连接/重连/关闭,维护状态与工具目录,支持跨线程调用工具。"""

    def __init__(
        self,
        configs: tuple[McpServerConfig, ...],
        transport_factory: McpTransportFactory | None = None,
        environment: Mapping[str, str] | None = None,
        retry_attempts: int = 3,
        retry_delay_seconds: float = 1.0,
    ) -> None:
        self._configs = {config.name: config for config in configs}
        self._factory = transport_factory or SdkMcpTransportFactory()
        self._environment = os.environ if environment is None else environment
        self._retry_attempts = max(1, retry_attempts)
        self._retry_delay_seconds = max(0.0, retry_delay_seconds)
        self._lock = threading.RLock()
        self._statuses = {config.name: McpServerStatus(config.name, "disabled" if not config.enabled else "failed") for config in configs}
        self._transports: dict[str, McpTransport] = {}
        self._catalogs: dict[str, tuple[McpToolDescription, ...]] = {}
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run_loop, name="lanscoder-mcp", daemon=True)
        self._thread.start()
        self._closed = False
        """启动独立事件循环线程并初始化各服务器的初始状态。"""
        self._connection_thread: threading.Thread | None = None
        self._pending_futures: set[Future[object]] = set()

    def connect_all(self) -> None:
        """同步连接全部启用的服务器(并行线程并等待全部完成)。"""

        workers = [threading.Thread(target=self._connect_one, args=(config,), daemon=True) for config in self._configs.values() if config.enabled]
        for config in self._configs.values():
            if not config.enabled:
                self._set_status(config.name, "disabled")
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join()

    def connect_all_in_background(self) -> None:
        """在后台线程里连接全部启用的服务器。"""

        with self._lock:
            if self._connection_thread is not None and self._connection_thread.is_alive():
                return
            for config in self._configs.values():
                if config.enabled:
                    self._set_status(config.name, "connecting")
            self._connection_thread = threading.Thread(
                target=self.connect_all,
                name="lanscoder-mcp-connect",
                daemon=True,
            )
            self._connection_thread.start()

    def reconnect(self, name: str | None = None) -> bool:
        """重新连接指定或全部启用的服务器。"""

        with self._lock:
            if self._closed:
                return False
            if name is None:
                configs = tuple(config for config in self._configs.values() if config.enabled)
            else:
                config = self._configs.get(name)
                configs = (config,) if config is not None and config.enabled else ()
        if not configs:
            return False
        for config in configs:
            threading.Thread(
                target=self._reconnect_one,
                args=(config,),
                name=f"lanscoder-mcp-reconnect-{config.name}",
                daemon=True,
            ).start()
        return True

    def statuses(self) -> tuple[McpServerStatus, ...]:
        """返回全部服务器的状态。"""

        with self._lock:
            return tuple(self._statuses[name] for name in self._configs)

    def doctor(self, name: str) -> McpServerStatus | None:

        with self._lock:
            return self._statuses.get(name)

    def tools(self) -> tuple[tuple[str, McpToolDescription], ...]:
        """返回全部服务器发现到的工具(服务器名 + 工具描述)。"""

        with self._lock:
            return tuple((name, tool) for name in self._configs for tool in self._catalogs.get(name, ()))

    def call_tool(self, server: str, tool: str, arguments: dict[str, object]) -> object:
        """调用指定服务器的工具,超时与失败统一转 RuntimeError。"""

        with self._lock:
            config = self._configs.get(server)
            transport = self._transports.get(server)
            catalog = self._catalogs.get(server, ())
        if config is None or transport is None or not any(item.name == tool for item in catalog):
            raise RuntimeError("MCP 工具不可用")
        try:
            return self._submit(transport.call_tool(tool, arguments), config.timeout_ms)
        except FutureTimeoutError as error:
            raise RuntimeError("MCP 请求超时") from error
        except Exception as error:
            raise RuntimeError("MCP 工具调用失败") from error

    def close(self) -> None:
        """关闭全部传输、取消待处理任务并停止事件循环线程。"""

        with self._lock:
            if self._closed:
                return
            self._closed = True
            transports = tuple(self._transports.items())
            pending_futures = tuple(self._pending_futures)
            connection_thread = self._connection_thread
            self._transports.clear()
            self._catalogs.clear()
        for future in pending_futures:
            future.cancel()
        if connection_thread is not None and connection_thread is not threading.current_thread():
            connection_thread.join(timeout=1)
        for name, transport in transports:
            try:
                self._submit(transport.close(), 1000)
            except Exception:
                pass
            self._set_status(name, "failed", error="MCP 已断开")
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=1)

    def _connect_one(self, config: McpServerConfig) -> None:
        """连接单个服务器,带重试与状态更新。"""
        self._set_status(config.name, "connecting")
        for attempt in range(self._retry_attempts):
            if self._closed:
                return
            try:
                resolved = self._resolve_config(config)
                transport = self._factory.create(resolved)
                tools = self._submit(self._initialize(transport), config.timeout_ms)
            except McpConfigError as error:
                self._set_status(config.name, "failed", error=str(error))
                return
            except FutureTimeoutError:
                error = "MCP 请求超时"
            except Exception:
                error = "MCP 连接失败"
            else:
                with self._lock:
                    self._transports[config.name] = transport
                    filtered_tools = self._allowed_tools(config, tools)
                    self._catalogs[config.name] = filtered_tools
                self._set_status(config.name, "connected", tool_count=len(filtered_tools))
                return
            if attempt + 1 < self._retry_attempts:
                time.sleep(self._retry_delay_seconds)
        self._set_status(config.name, "failed", error=error)

    def _reconnect_one(self, config: McpServerConfig) -> None:
        """先关闭旧传输,再重连单个服务器。"""

        with self._lock:
            transport = self._transports.pop(config.name, None)
            self._catalogs.pop(config.name, None)
        if transport is not None:
            try:
                self._submit(transport.close(), 1000)
            except Exception:
                pass
        self._connect_one(config)

    async def _initialize(self, transport: McpTransport) -> tuple[McpToolDescription, ...]:
        """异步连接并枚举服务器工具,失败时关闭传输。"""
        try:
            await transport.connect()
            return await transport.list_tools()
        except BaseException:
            await transport.close()
            raise

    def _resolve_config(self, config: McpServerConfig) -> McpServerConfig:
        """把配置里的环境变量占位符解析为实际值。"""
        if isinstance(config, McpLocalServerConfig):
            return replace(
                config,
                command=tuple(resolve_environment_placeholders(config.command, self._environment)),
                env=resolve_environment_placeholders(config.env, self._environment),
            )
        headers = resolve_environment_placeholders(config.headers, self._environment)
        if config.bearer_token_env_var is not None:
            token = resolve_environment_placeholders(f"{{env:{config.bearer_token_env_var}}}", self._environment)
            headers["Authorization"] = f"Bearer {token}"
        return replace(
            config,
            url=resolve_environment_placeholders(config.url, self._environment),
            headers=headers,
        )

    def _set_status(
        self,
        name: str,
        state: Literal["disabled", "connecting", "connected", "failed"],
        tool_count: int = 0,
        error: str | None = None,
    ) -> None:
        with self._lock:
            self._statuses[name] = McpServerStatus(name, state, tool_count, error)

    def _submit(self, coroutine: Coroutine[object, object, object], timeout_ms: int) -> object:
        """把协程提交到 MCP 事件循环并等待结果,带超时与取消。"""
        future: Future[object] = asyncio.run_coroutine_threadsafe(self._with_timeout(coroutine, timeout_ms), self._loop)
        with self._lock:
            self._pending_futures.add(future)
        try:
            return future.result(timeout=timeout_ms / 1000 + 0.2)
        except (FutureTimeoutError, TimeoutError) as error:
            future.cancel()
            raise FutureTimeoutError from error
        finally:
            with self._lock:
                self._pending_futures.discard(future)

    async def _with_timeout(self, coroutine: Coroutine[object, object, object], timeout_ms: int) -> object:
        return await asyncio.wait_for(coroutine, timeout=timeout_ms / 1000)

    @staticmethod
    def _allowed_tools(config: McpServerConfig, tools: tuple[McpToolDescription, ...]) -> tuple[McpToolDescription, ...]:
        """按 allowed_tools 通配模式过滤工具。"""
        if config.allowed_tools is None:
            return tools
        return tuple(tool for tool in tools if any(fnmatch.fnmatchcase(tool.name, pattern) for pattern in config.allowed_tools))

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()
