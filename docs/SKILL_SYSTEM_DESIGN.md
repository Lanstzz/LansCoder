# Skill System Design

## 概述

Skill System 是 FirstCoder 的可复用工作流指令系统。Skills 不是简单的提示词扩展，而是被**发现、路由、加载**的模块化指令，并在 session 日志中留下**可审计的痕迹**。

## 核心组件

### SkillCatalog

```python
class SkillCatalog:
    """技能目录，包含所有已发现的技能"""
    skills: List[SkillDefinition]
    required_files: Dict[str, List[str]]  # skill_path -> [required_file_paths]
```

**职责**：
- 聚合项目级和全局级技能
- 提供技能查询和过滤接口
- 管理技能的依赖文件

### SkillDefinition

```python
@dataclass
class SkillDefinition:
    path: Path
    name: str
    description: str
    confidence: float  # 匹配置信度 0.0-1.0
    source: SkillSource  # PROJECT 或 GLOBAL
    required_files: List[str]  # 必读文件路径
    metadata: Dict[str, Any]  # 元数据（触发词、适用范围等）
```

### SkillSource

```python
class SkillSource(Enum):
    PROJECT = "project"      # 项目级技能
    GLOBAL = "global"        # 全局技能
    MACHINE = "machine"      # 机器级技能
```

## 技能发现

### 发现路径

| 来源 | 路径 | 优先级 |
|------|------|--------|
| 项目 markdown skill | `<project-root>/skills/*.md` | 最高 |
| 项目 agent skill | `<project-root>/.agents/skills/*/SKILL.md` | 高 |
| 机器级 agent skill | `~/.agents/skills/*/SKILL.md` | 中 |
| 机器级 markdown skill | `~/.firstcoder/skills/*.md` | 低 |

### 发现流程

```python
def discover_skills(project_root: Path) -> SkillCatalog:
    """1. 扫描项目级技能目录
       2. 扫描全局技能目录
       3. 加载每个技能的元数据
       4. 构建技能目录
       5. 返回 SkillCatalog"""
```

**关键设计**：
- 项目级技能优先于全局技能
- 全局技能不能覆盖项目规则、权限策略或 sandbox 边界
- 每个技能都会被赋予一个置信度分数，用于后续路由

## 技能路由

### 匹配算法

```python
def route_skill(user_message: str, catalog: SkillCatalog) -> Optional[SkillDefinition]:
    """1. 检查高置信度匹配（confidence >= 0.8）
       2. 如果匹配到，返回对应技能
       3. 如果没有匹配，返回 None"""
```

### 匹配策略

| 策略 | 说明 | 置信度 |
|------|------|--------|
| 元数据匹配 | 基于技能的触发词、适用范围等元数据 | 高 |
| 内容匹配 | 基于用户消息与技能描述的语义相似度 | 中 |
| 上下文匹配 | 基于当前 session 和历史交互 | 低 |

### 路由结果

```python
@dataclass
class SkillRouteResult:
    skill: SkillDefinition
    reason: str  # 路由原因
    confidence: float  # 匹配置信度
    required_files: List[str]  # 需要加载的必读文件
```

## 技能加载

### 加载流程

```python
def load_skill(skill: SkillDefinition, required_files: List[str]) -> SkillContext:
    """1. 读取技能文件内容
       2. 计算内容哈希
       3. 加载必读文件（如果有）
       4. 将技能注入 provider 请求前的上下文
       5. 记录审计事件"""
```

### 审计事件

```json
// 技能被选中
{"type": "skill_selected", "skill_path": "skills/example.md", "reason": "metadata_match"}

// 技能被加载
{"type": "skill_loaded", "skill_path": "skills/example.md", "content_hash": "..."}

// 必读文件被加载
{"type": "skill_required_file_loaded", "file_path": "docs/policy.md", "content_hash": "..."}
```

### 注入时机

技能在**第一次 provider request 之前**加载，确保模型能够看到技能的完整指令。

## 技能冲突解决

### 优先级规则

1. **项目级技能 > 全局技能** — 项目特定的工作流优先
2. **后加载的技能 > 先加载的技能** — 如果在同一级别有多个匹配
3. **高置信度 > 低置信度** — 匹配更准确的技能优先

### 冲突检测

```python
def detect_conflicts(catalog: SkillCatalog) -> List[Conflict]:
    """检查技能之间的冲突：
       - 同名技能
       - 重叠的触发词
       - 相互矛盾的配置"""
```

## 技能示例

### 项目级技能

```markdown
<!-- skills/code-review.md -->
---
name: code-review
description: 执行代码审查工作流
trigger_words: ["review", "审查", "code review"]
---

# Code Review Skill

当用户要求代码审查时：

1. 识别最近修改的文件
2. 检查代码质量和最佳实践
3. 提供改进建议
4. 生成审查报告
```

### 全局技能

```markdown
<!-- ~/.agents/skills/lark-doc/SKILL.md -->
---
name: lark-doc
description: 飞书文档操作
trigger_words: ["文档", "doc", "飞书文档"]
required_files: ["references/lark-doc-detail.md"]
---

# Lark Doc Skill

...
```

## 扩展性

### 添加新技能

1. 创建技能文件（`.md`）
2. 添加元数据（name, description, trigger_words）
3. 放置在合适的目录（项目级或全局级）
4. 重启 FirstCoder 或重新加载技能

### 创建自定义路由策略

1. 实现 `SkillRouter` 协议
2. 注册新的匹配算法
3. 更新配置以使用新路由

### 添加技能验证

1. 实现 `SkillValidator` 协议
2. 检查技能语法和元数据
3. 验证必读文件存在性

## 设计决策记录

| 决策 | 理由 |
|------|------|
| 技能可审计 | 便于调试和追踪 agent 行为 |
| 项目级优先 | 项目特定工作流应该覆盖通用规则 |
| 内容哈希 | 快速检测技能变更，避免重复加载 |
| 置信度评分 | 支持模糊匹配，提高技能发现的灵活性 |
| 必读文件机制 | 允许技能引用外部参考文档，保持技能文件简洁 |
