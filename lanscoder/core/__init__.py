"""LansCoder 三层解耦核心 API(L1 agent_loop / L2 Agent / L3 create_agent_session)。

Step 1 先承接装配根(`core.runtime`);L1/L2/L3 公共面在 Step 2 落地。
约束: core 永不 import app。
"""
