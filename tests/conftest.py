"""pytest 公共配置。

当前工作区只保留 provider、tool、config 和 utils 的可运行骨架。
旧 agent/memory/context 实现已经删除，后续按新计划重新实现时再补对应测试。
"""

import pytest

from lanscoder.app.runtime import create_agent_loop


@pytest.fixture
def make_loop():
    """Fixture 返回生产构造路径 ``create_agent_loop`` 的薄包装。

    全部直接 ``AgentLoop(...)`` 构造的测试都迁移到这里；返回的 loop 已是完整注入的
    ``lanscoder.agent.loop.AgentLoop``，无需再传协作对象。
    """

    def _make_loop(*, session, provider, **overrides):
        return create_agent_loop(session=session, provider=provider, **overrides)

    return _make_loop
