"""LoCoBench-Agent driver:用 ``LansCoderAgent`` 运行场景并输出结果。

用法(Phase 1 最小闭环,1 个 easy 场景):

    .venv/bin/python -m benchmark.locobench.driver \
        --locobench-root /tmp/LoCoBench-Agent \
        --difficulty easy --scenario-count 1 \
        --model-ref deepseek/deepseek-v4-flash \
        --output-dir benchmark/runs/locobench/smoke-easy

Phase 2 起可指定被测压缩策略(--compaction-strategy
no_compact / l1_l2 / l1_l2_l3,默认 l1_l2_l3),并在输出目录额外产出
``analysis.json``(三套 token 口径 + CompactionEvent 压缩行为聚合)。

harness 侧上下文管理用 ``--context-management none``(让 LansCoder
``ContextWindowManager`` 独占上下文管理,因果干净)。LoCoBench-Agent 数据
(clone 到工具目录)路径由 ``--locobench-root`` 指向;本模块只写 driver 代码,
不改 ``lanscoder/`` 核心。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("locobench.driver")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="LoCoBench-Agent × LansCoder driver")
    parser.add_argument("--locobench-root", required=True, help="LoCoBench-Agent clone 目录(工具目录,不进仓库)")
    parser.add_argument("--difficulty", choices=["easy", "medium", "hard", "expert"], help="难度过滤")
    parser.add_argument("--category", help="任务类别过滤(如 security_analysis)")
    parser.add_argument("--scenario-id", help="指定单个场景 ID")
    parser.add_argument("--scenario-count", type=int, default=1, help="场景数量上限")
    parser.add_argument("--max-turns", type=int, default=20, help="每场景最大回合数")
    parser.add_argument("--max-tool-rounds", type=int, default=60, help="LansCoder 每回合最大工具轮")
    parser.add_argument("--model-ref", default="deepseek/deepseek-v4-flash", help="LansCoder 模型引用(默认 deepseek)")
    parser.add_argument("--context-window", type=int, default=1_000_000, help="LansCoder context_window 覆盖")
    parser.add_argument(
        "--compaction-strategy",
        choices=["no_compact", "l1_l2", "l1_l2_l3"],
        default="l1_l2_l3",
        help="LansCoder 被测压缩策略(默认 l1_l2_l3 = 全量)",
    )
    parser.add_argument("--context-management", choices=["none", "basic", "adaptive"], default="none", help="harness 上下文管理(默认 none,由 LansCoder 独占)")
    parser.add_argument("--initial-context-mode", choices=["full", "minimal", "empty"], default="minimal", help="harness 初始上下文模式")
    parser.add_argument("--output-dir", required=True, help="结果输出目录(建议 benchmark/runs/locobench/<run>)")
    return parser.parse_args(argv)


def _write_locobench_config(args: argparse.Namespace, output_dir: Path) -> Path:
    """在输出目录生成 LoCoBench YAML 配置(数据路径指向工具目录,绝对路径)。"""

    root = Path(args.locobench_root).resolve()
    data = {
        "data": {
            "output_dir": str(root / "data" / "output"),
            "generated_dir": str(root / "data" / "generated"),
        },
        "agent": {
            "enable_agent_mode": True,
            "max_turns_per_session": args.max_turns,
            "allowed_directories": ["."],
            "readonly_mode": False,
            "max_file_size_mb": 10,
            "enable_network_access": False,
            "context_management_strategy": args.context_management,
            "enable_file_system_tools": True,
            "enable_compiler_tools": True,
            "enable_debugger_tools": True,
            "enable_ide_simulator": True,
        },
    }
    config_path = output_dir / "locobench_config.yaml"
    config_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return config_path


def _register_tools(config: Any) -> None:
    """把 LoCoBench 工具注册进全局 registry(harness 按场景名取工具)。"""

    from locobench.core.tool_registry import get_tool_registry, register_tool
    from locobench.tools import CalculatorTool, CompilerTool, DebuggerTool, EchoTool, FileSystemTool, IDESimulatorTool

    _ = get_tool_registry()
    register_tool(EchoTool())
    register_tool(CalculatorTool())
    if config.agent.enable_file_system_tools:
        register_tool(FileSystemTool(
            allowed_directories=config.agent.allowed_directories,
            readonly_mode=config.agent.readonly_mode,
            max_file_size=config.agent.max_file_size_mb * 1024 * 1024,
        ))
    if config.agent.enable_compiler_tools:
        register_tool(CompilerTool(
            allowed_directories=config.agent.allowed_directories,
            enable_network=config.agent.enable_network_access,
        ))
    if config.agent.enable_debugger_tools:
        register_tool(DebuggerTool(allowed_directories=config.agent.allowed_directories))
    if config.agent.enable_ide_simulator:
        register_tool(IDESimulatorTool(allowed_directories=config.agent.allowed_directories))


def _build_interactive_scenario(data: dict[str, Any], max_turns: int):
    """把转换后的场景缓存 dict 构造成 harness 的 InteractiveScenario。"""

    from locobench.core.agent_session import ConversationPhase
    from locobench.core.task import DifficultyLevel, TaskCategory
    from locobench.generation.interactive_scenario_generator import InteractiveScenario

    phases = [ConversationPhase(**phase) for phase in data.get("conversation_phases", [])]
    category_str = data.get("category", "interactive_code_exploration")
    try:
        category_enum = TaskCategory(category_str)
    except ValueError:
        category_enum = TaskCategory.INTERACTIVE_CODE_EXPLORATION

    # 官方 CLI 把项目文件嵌套在 initial_context.project_files 下
    # (harness 的 _populate_initial_context 依赖这个结构);大文件截断防 8MB 消息超限。
    project_files = data.get("project_files", [])
    limited: dict[str, str] = {}
    for file_data in project_files:
        if not isinstance(file_data, dict):
            continue
        path = file_data.get("path", "")
        content = file_data.get("content", "")
        if len(content) > 8_000_000:
            logger.warning("project file %s 过大(%d chars),截断", path, len(content))
            content = content[:8_000_000] + "\n\n[Content truncated]"
        limited[path] = content

    initial_context: dict[str, Any] = {"project_files": limited}
    if data.get("project_spec"):
        initial_context["project_spec"] = data["project_spec"]
    if data.get("project_name"):
        initial_context["project_name"] = data["project_name"]

    return InteractiveScenario(
        scenario_id=data["scenario_id"],
        title=data.get("title", ""),
        description=data.get("description", ""),
        category=category_enum,
        difficulty=DifficultyLevel(str(data.get("difficulty", "medium")).lower()),
        initial_context=initial_context,
        context_files=data.get("context_files", []),
        working_directory=data.get("working_directory", "."),
        conversation_phases=phases,
        global_success_criteria=[],
        available_tools=data.get("available_tools", []),
        max_turns=max_turns,  # 用 CLI 上限控制预算(不改场景语义)
        max_duration_minutes=data.get("max_duration_minutes", 30),
        context_window_tokens=data.get("context_window_tokens", 500_000),
    )


def _load_scenarios(
    config: Any,
    *,
    difficulty: str | None,
    category: str | None,
    scenario_id: str | None,
    scenario_count: int,
    max_turns: int,
) -> list[Any]:
    """从 converter 缓存按 难度/类别/ID 过滤并构造场景。"""

    from locobench.generation.scenario_converter import get_scenario_converter

    converter = get_scenario_converter(config)
    cached = converter.load_all_cached_scenarios()
    logger.info("converter cache 共 %d 个已转换场景", len(cached))

    selected: list[Any] = []
    for data in cached:
        if difficulty and str(data.get("difficulty", "")).lower() != difficulty.lower():
            continue
        if category and category not in str(data.get("category", "")):
            continue
        if scenario_id and data.get("scenario_id") != scenario_id:
            continue
        scenario = _build_interactive_scenario(data, max_turns=max_turns)
        selected.append(scenario)
        logger.info(
            "选中场景 %s (difficulty=%s category=%s)",
            scenario.scenario_id,
            scenario.difficulty.value,
            scenario.category.value,
        )
        if len(selected) >= scenario_count:
            break

    if not selected:
        raise SystemExit(f"没有匹配的场景: difficulty={difficulty} category={category} id={scenario_id}")
    return selected


def _create_provider(model_ref: str):
    """按 LansCoder 全局/项目配置创建 provider。"""

    from lanscoder.config.models import build_model_catalog
    from lanscoder.config.settings import load_config
    from lanscoder.providers.factory import create_provider_for_model

    app_config = load_config()
    catalog = build_model_catalog(
        global_config=app_config.global_config,
        project_config=app_config.project_config,
    )
    profile = catalog.require(model_ref)
    logger.info("provider: %s model=%s base_url=%s", profile.provider.id, profile.model_id, profile.provider.base_url)
    return create_provider_for_model(profile)


def _result_to_dict(result: Any) -> dict[str, Any]:
    return {
        "agent_name": result.agent_name,
        "scenario_id": result.scenario_id,
        "session_id": result.session_id,
        "overall_score": result.overall_score,
        "lcba_comprehension": result.lcba_comprehension,
        "lcba_efficiency": result.lcba_efficiency,
        "total_turns": result.total_turns,
        "session_duration": result.session_duration,
        "session_status": result.session_status,
        "category_scores": {k.value: v for k, v in result.category_scores.items()},
        "tool_usage_log": result.tool_usage_log,
        "modified_files": list(result.modified_files.keys()),
        "error_log": result.error_log,
    }


def _flush_scenario_stats(agent: Any) -> None:
    """把当前场景的 turn/compaction 统计落盘(在 agent.clear_history 前调用)。"""

    try:
        agent.flush_scenario_stats()
    except Exception as exc:  # noqa: BLE001
        logger.warning("flush scenario stats failed: %s", exc)


async def run(args: argparse.Namespace) -> int:
    from locobench.core.config import Config
    from locobench.evaluation.robust_agent_evaluator import RobustAgentEvaluator

    from benchmark.locobench.lanscoder_agent import LansCoderAgent

    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    config_path = _write_locobench_config(args, output_dir)
    config = Config.from_yaml(str(config_path))
    config.create_directories()
    _register_tools(config)

    scenarios = _load_scenarios(
        config,
        difficulty=args.difficulty,
        category=args.category,
        scenario_id=args.scenario_id,
        scenario_count=args.scenario_count,
        max_turns=args.max_turns,
    )

    provider = _create_provider(args.model_ref)
    agent = LansCoderAgent(
        name=f"lanscoder-{args.model_ref.split('/')[-1]}",
        config={
            "provider": provider,
            "data_root": str(output_dir / "sessions"),
            "locobench_root": str(Path(args.locobench_root).resolve()),
            "context_window": args.context_window,
            "max_tool_rounds": args.max_tool_rounds,
            "compaction_strategy": args.compaction_strategy,
        },
    )

    evaluator = RobustAgentEvaluator(
        config,
        agent_name=agent.name,
        enable_semantic_search=False,
        enable_enhanced_summarization=False,
        initial_context_mode=args.initial_context_mode,
    )

    logger.info("开始评估 %d 个场景 (context-management=%s)", len(scenarios), args.context_management)
    results = await evaluator.evaluate_agents(
        [agent],
        scenarios,
        resume=False,
        max_concurrent_scenarios=1,
    )
    _flush_scenario_stats(agent)

    summary = {agent_name: [_result_to_dict(r) for r in agent_results] for agent_name, agent_results in results.items()}
    (output_dir / "results.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("results.json 已写入 %s", output_dir / "results.json")

    stats_file = output_dir / "sessions" / "scenario_stats.jsonl"
    transcript = {"scenarios": [], "note": "per-turn token 口径分开: context_tokens 为 harness 启发式; tokens_used 为 provider 真实 usage; 压缩事件见 compaction_events"}
    if stats_file.exists():
        for line in stats_file.read_text(encoding="utf-8").splitlines():
            if line.strip():
                transcript["scenarios"].append(json.loads(line))
    (output_dir / "transcript.json").write_text(
        json.dumps(transcript, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("transcript.json 已写入 %s", output_dir / "transcript.json")

    from benchmark.locobench.analyze import analyze

    analysis = analyze(transcript)
    (output_dir / "analysis.json").write_text(
        json.dumps(analysis, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "analysis.json 已写入 %s (策略=%s, 场景=%d, 压缩 attempts=%d)",
        output_dir / "analysis.json",
        args.compaction_strategy,
        len(analysis["per_scenario"]),
        analysis["summary"]["compaction"].get("attempts", 0),
    )

    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
