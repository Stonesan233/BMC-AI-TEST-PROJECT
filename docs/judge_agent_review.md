# Judge Agent 开发审视报告

> **审视人**: 露娜大人
> **审视日期**: 2026-04-12
> **项目**: BMC-AI-TEST-PROJECT
> **目标读者**: 朝日娘

---

## 一、项目概览

BMC-AI-TEST-PROJECT 是面向 openUBMC 的智能自动化测试框架，采用**双 Agent 分离架构**：

```
Test Case YAML → Exec Agent → ExecutionRecord → Judge Agent → TestResult + Audit Report
```

**Judge Agent** 是独立的评判引擎，职责是对 Exec Agent 产出的执行记录进行严格审查，核心原则为 **"宁可错杀，不可放过"**。

---

## 二、文件清单与完成度

| 文件 | 路径 | 行数 | 状态 |
|------|------|------|------|
| 主实现 | `src/agents/judge_agent.py` | ~941 | ✅ 已完成 |
| 系统提示词 | `src/prompts/judge_system_v2.1.txt` | - | ✅ 已完成 |
| 核心规则 | `src/prompts/judge_system_core.txt` | - | ✅ 已完成 |
| 领域知识 | `src/prompts/judge_system_openubmc.txt` | - | ✅ 已完成 |
| 输出格式 | `src/prompts/judge_system_output.txt` | - | ✅ 已完成 |
| 任务模板 | `src/prompts/judge_user_template_v2.1.txt` | - | ✅ 已完成 |
| 单元测试 | `tests/test_judge_agent.py` | - | ✅ 已完成 |
| 集成测试 | `tests/test_judge_agent_real_llm.py` | - | ✅ 已完成 |

---

## 三、架构评估

### 3.1 核心类: JudgeAgent

**设计模式**: 独立评判引擎 + 异步调用

| 方法 | 功能 | 状态 |
|------|------|------|
| `judge(record_path)` | 从文件路径读取并评判 | ✅ |
| `judge_from_record(execution_record)` | 从内存对象评判 | ✅ |
| `judge_batch(record_paths)` | 批量并发评判 | ✅ |

**依赖关系**:
- `ClientFactory` → 获取 AsyncOpenAI 客户端（不拥有生命周期）
- `ExecutionRecord` (Schema v2.1) → 输入数据模型
- `TestResult` → 输出数据模型
- 支持 v1 → v2.1 自动迁移

### 3.2 辅助类: JudgeOutputParser

多策略 LLM 输出解析：
1. ` ```json ` 代码块提取
2. 通用 ` ``` ` 代码块提取
3. 括号配对 JSON 提取

**容错设计**: 解析失败时倾向返回 FAIL，符合核心原则。

### 3.3 三层 Prompt 架构

```
Layer 1+2: 系统提示词（角色设定 + 领域知识 + 核心规则 + 输出格式）
Layer 3:   任务提示词（当前 ExecutionRecord 实例）
```

**优点**: 模块化清晰，可独立迭代各层。

---

## 四、设计亮点

### 4.1 证据驱动评判
- 所有判断必须基于 ExecutionRecord 中的实际证据
- 禁止推测、推断、外推
- 证据链不完整 → FAIL

### 4.2 严格字段匹配
- 精确匹配: `"1" ≠ 1`（类型严格）
- 子集匹配: expected 是 actual 的子集即通过
- 忽略字段: `@odata.*`, `@odata.etag` 等动态字段

### 4.3 False PASS 风险评估
- `none`: 证据充分，精确匹配
- `low`: 基本证据，部分字段验证
- `medium`: 证据不完整但倾向 PASS
- `high`: 明显不足的证据或模糊预期值

### 4.4 用户管理边界规则
- 添加用户前检查用户数量
- 禁止删除 ID 2（Administrator）
- 密码修改必须验证
- 测试后必须还原环境

### 4.5 审计报告生成
- 路径: `shared/audit_reports/{execution_id}_audit_report.md`
- 内容包含: 整体结论、前置验证、逐步判断、预期vs实际对比、证据摘要、风险评级、建议

---

## 五、测试覆盖

### 5.1 单元测试 (`test_judge_agent.py`)

| 测试项 | 覆盖内容 |
|--------|----------|
| Schema 验证 | v2.1 格式校验 |
| 版本迁移 | v1 → v2.1 自动转换 |
| CLI 参数解析 | `--judge` / `--no-judge` |
| 严格评判模拟 | 不确定 → FAIL |
| 错误处理 | 异常降级 |
| 审计报告 | 格式与内容 |

### 5.2 集成测试 (`test_judge_agent_real_llm.py`)

| 测试项 | 覆盖内容 |
|--------|----------|
| 真实模型调用 | Qwen3 / GLM 端到端 |
| 厂商大小写检测 | 字段敏感度验证 |
| HTTP 错误处理 | 异常场景覆盖 |
| 迁移+真实模型 | 版本兼容性 |

---

## 六、问题清单

### BUG-001（严重）：`_client` 属性未初始化

- **位置**: `judge_agent.py` → `JudgeAgent.__init__`
- **现象**: `self._client` 在 `__init__` 中未声明，但在正常运行路径中被引用
- **影响**: 常规使用时将触发 `AttributeError` 崩溃
- **修复方案**: 在 `__init__` 中添加:
  ```python
  self._client: Optional[AsyncOpenAI] = None
  ```
- **优先级**: 🔴 最高 — 阻塞性 Bug

---

## 七、待完善项

| 项目 | 当前状态 | 建议优先级 |
|------|----------|------------|
| 多模型交叉验证 | 未规划 | P2 — 可提升评判可靠性 |
| 人工复审接口 | 未规划 | P3 — 生产环境建议 |
| 高级监控/可观测性 | 基础 scaffold | P2 — 运维需要 |
| 批量评判性能优化 | 基础实现 | P3 — 规模增大后需要 |
| 边界场景测试覆盖 | 部分 | P2 — 提升鲁棒性 |

---

## 八、评分总览

| 维度 | 评分 (10分制) | 说明 |
|------|---------------|------|
| 架构设计 | **9.0** | 清晰分层，职责分离，模块化程度高 |
| 代码实现 | **8.5** | 结构良好，有 1 处阻塞级 Bug |
| 测试质量 | **8.0** | 覆盖面好，部分边界场景缺失 |
| 文档完备 | **9.0** | 详尽的 Review 注释和 Prompt 文档 |
| 生产就绪 | **8.5** | 修复 BUG-001 后可投产 |

**综合评价**: Judge Agent 的整体开发质量上乘。架构设计尤为出色，三层 Prompt + 多策略解析 + 证据驱动评判的组合拳体系完善。修复 `_client` 初始化 Bug 后即可进入生产部署阶段。

---

*露娜大人 签发*
