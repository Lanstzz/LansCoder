"""配置加载:从全局与项目 TOML 合并出 AppConfig,支持环境变量与默认路径。"""

from __future__ import annotations

import os
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
import tomllib

from lanscoder.config.models import ModelCatalog, build_model_catalog

PROJECT_CONFIG_NAME = "lanscoder.toml"


@dataclass(frozen=True, slots=True)
class AppConfig:
    """合并后的应用配置:环境快照、全局/项目 TOML 与已读取的配置路径。"""

    env: dict[str, str]
    project_config: dict[str, Any] | None = None
    global_config: dict[str, Any] | None = None
    project_config_path: Path | None = None
    global_config_path: Path | None = None

    def get_env(self, name: str, default: str | None = None) -> str | None:
        """读取环境变量。"""

        return self.env.get(name, default)

    def get_provider_bool(
        self,
        name: str,
        *,
        env: str | None = None,
        default: bool | None = None,
        provider_name: str,
    ) -> bool | None:
        """按 环境变量 → provider 配置 → 默认值 的顺序取布尔配置。"""

        if env:
            env_value = self.get_env(env)
            if env_value:
                return _bool_value_from_raw(env_value)
        value = self._provider_config_raw_value(name, provider_name=provider_name)
        if value is not None:
            return _bool_value_from_raw(value)
        return default

    def get_config_value(self, name: str, *, default: str | None = None) -> str | None:
        """取字符串配置值(项目优先于全局)。"""

        for config in (self.project_config, self.global_config):
            value = _string_value(config, name)
            if value is not None:
                return value
        return default

    def mcp_config(self) -> dict[str, Any]:
        """合并全局与项目的 MCP 服务器配置(项目覆盖全局)。"""

        merged: dict[str, Any] = {}
        for config in (self.global_config, self.project_config):
            if not config or "mcp" not in config:
                continue
            raw_mcp = config["mcp"]
            if not isinstance(raw_mcp, dict):
                raise ValueError("[mcp] 配置必须是表")
            for name, server_config in raw_mcp.items():
                merged[name] = deepcopy(server_config)
        return merged

    def model_catalog(self) -> ModelCatalog:
        """从全局与项目配置构建模型目录。"""

        return build_model_catalog(
            global_config=self.global_config,
            project_config=self.project_config,
        )

    @property
    def loaded_config_paths(self) -> list[Path]:
        """返回实际读取到的配置文件路径。"""

        return [path for path in (self.global_config_path, self.project_config_path) if path is not None]

    def _provider_config_raw_value(
        self,
        name: str,
        *,
        provider_name: str | None,
    ) -> Any | None:
        """从项目/全局配置读取指定 provider 的原始值。"""
        for config in (self.project_config, self.global_config):
            value = _provider_raw_value(config, name, provider_name=provider_name)
            if value is not None:
                return value
        return None


def load_config(
    *,
    project_root: Path | str | None = None,
    env: dict[str, str] | None = None,
) -> AppConfig:
    """加载全局与项目 TOML 配置,返回合并后的 AppConfig。"""

    env_snapshot = dict(os.environ if env is None else env)
    root = Path(project_root or os.getcwd()).resolve()
    global_path = default_global_config_path()
    project_path = root / PROJECT_CONFIG_NAME
    global_config = _read_toml_file(global_path)
    project_config = _read_toml_file(project_path)

    return AppConfig(
        env=env_snapshot,
        project_config=project_config,
        global_config=global_config,
        project_config_path=project_path if project_config is not None else None,
        global_config_path=global_path if global_config is not None else None,
    )


def default_global_config_path() -> Path:
    """返回全局配置路径(优先 XDG_CONFIG_HOME)。"""

    config_home = os.getenv("XDG_CONFIG_HOME")
    if config_home:
        return Path(config_home) / "lanscoder" / "config.toml"
    return Path.home() / ".config" / "lanscoder" / "config.toml"


def project_config_path(project_root: Path | str | None = None) -> Path:
    """返回项目配置文件的路径。"""

    return Path(project_root or os.getcwd()).resolve() / PROJECT_CONFIG_NAME


def render_default_config() -> str:
    """渲染一份起始全局配置文件文本。"""

    return "\n".join(
        [
            '# LansCoder global configuration. Project-level "./lanscoder.toml" can override it.',
            'default_model = "deepseek/deepseek-v4-flash"',
            "",
            "[providers.deepseek]",
            'type = "openai-compatible"',
            'base_url = "https://api.deepseek.com"',
            'api_key = ""',
            "parallel_tool_calls = true",
            "",
            '[models."deepseek/deepseek-v4-flash"]',
            'label = "DeepSeek V4 Flash"',
            "context_window = 1000000",
            "",
            "[permissions]",
            'mode = "ask"',
            "",
            "[ui]",
            'theme = "default"',
            "",
        ]
    )


def _read_toml_file(path: Path) -> dict[str, Any] | None:
    """读取 TOML 文件,文件不存在时返回 None。"""
    if not path.exists():
        return None
    with path.open("rb") as handle:
        data = tomllib.load(handle)
    return data


def _provider_raw_value(config: dict[str, Any] | None, name: str, *, provider_name: str | None) -> Any | None:
    """从配置中读取指定 provider 的原始配置值。"""
    if not config or not provider_name:
        return None
    providers = config.get("providers")
    if not isinstance(providers, dict):
        return None
    provider = providers.get(provider_name)
    return provider.get(name) if isinstance(provider, dict) else None


def _string_value(config: dict[str, Any] | None, name: str) -> str | None:
    """读取字符串配置值。"""
    if not config:
        return None
    value = config.get(name)
    if value is None:
        return None
    return str(value)


def _bool_value_from_raw(value: Any) -> bool:
    """把原始值解析为布尔,无法解析时抛错。"""
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"无法解析布尔配置值：{value}")
