"""应用层运行时空壳(Step 1 shim):装配根已迁至 ``lanscoder/core/runtime.py``。

本模块只做 re-export,保持 ``from lanscoder.app.runtime import ...`` 既有调用零变化。
约束: core 永不 import app;app → core 是本仓库唯一新增依赖边。
"""

from __future__ import annotations

from lanscoder.core.runtime import *  # noqa: F401,F403
from lanscoder.core.runtime import __all__  # noqa: F401
