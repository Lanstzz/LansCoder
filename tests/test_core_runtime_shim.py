"""Step 1 shim 防漂移: ``lanscoder.app.runtime`` 必须与 ``lanscoder.core.runtime`` 公开 API 完全一致。

装配根从 ``app/runtime.py`` 迁到 ``lanscoder/core/runtime.py`` 后,``app/runtime.py``
只做 re-export。任何在 core 侧新增/删除公开名而 shim 未同步,或两侧指向不同对象,
都会在这里被抓住。
"""

from __future__ import annotations

import lanscoder.app.runtime as app_runtime
import lanscoder.core.runtime as core_runtime


def test_shim_exports_same_public_names() -> None:
    assert sorted(app_runtime.__all__) == sorted(core_runtime.__all__)


def test_shim_names_resolve_to_core_objects() -> None:
    for name in core_runtime.__all__:
        assert getattr(app_runtime, name) is getattr(core_runtime, name), name


def test_shim_public_surface_covers_consumers() -> None:
    required = {
        "AgentChatRunner",
        "CurrentSessionState",
        "create_agent_loop",
        "register_loop_tools",
    }
    assert required <= set(core_runtime.__all__)
