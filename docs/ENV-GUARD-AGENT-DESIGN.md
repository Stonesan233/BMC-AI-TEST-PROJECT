# EnvGuard Agent 设计文档

> openUBMC AI 测试框架 -- 第三 Agent 架构设计
>
> 版本: DRAFT v0.1
>
> 日期: 2026-04-14

---

## 1. 背景与动机

### 1.1 现状

当前框架采用 **双 Agent 架构**（Exec + Judge），环境恢复能力分散在两处：

| 位置 | 实现方式 | 问题 |
|------|---------|------|
| ExecAgent (主线) | `execute()` 末尾调用 `EnvironmentRecoveryTool.recover()` | Exec 职责不纯粹，兼具执行与恢复 |
| v1.0.1 分支 | `EnvironmentRecoveryTool` 独立脚本 | 硬编码决策树，无法处理未预见的故障场景 |
| JudgeAgent | Prompt 中要求"环境恢复" | Judge 不接触系统，无法真正执行恢复，仅标记警告 |

### 1.2 痛点

1. **ExecAgent 职责膨胀**: 执行 + 恢复耦合，测试失败时恢复逻辑可能被跳过（异常栈未到 `recover()` 就已退出）
2. **硬编码恢复策略**: v1.0.1 的 `EnvironmentRecoveryTool` 是纯脚本，按固定决策树执行，无法应对以下场景:
   - BMC 固件升级后接口行为变化
   - 用户管理接口返回非预期错误码
   - 网络/SSH 间歇性不可达（需要智能重试策略）
   - 多种故障同时发生（如认证失败 + 用户列表满）
3. **恢复时机单一**: 仅在用例执行后恢复，缺少用例执行前的前置检查和预防
4. **恢复结果不透明**: Exec 内调用恢复后仅 log warning，恢复质量和细节无法被 Judge 利用

### 1.3 设计目标

引入第三个 Agent -- **EnvGuard Agent**，专职环境检查与修复：

- 执行前: 检查环境就绪状态，发现并修复遗留脏状态
- 执行中: 响应 ExecAgent 的环境异常求助（如 IPMI 认证失败）
- 执行后: 验证环境恢复完整性，生成环境状态报告

---

## 2. 三 Agent 架构总览

### 2.1 架构图

```
                           main.py (编排器)
                               |
           +-------------------+-------------------+
           |                   |                   |
           v                   v                   v
   +----------------+   +----------------+   +----------------+
   |  Test_Exec     |   |  EnvGuard      |   |  Test_Judge    |
   |  (执行 Agent)  |   |  (环境 Agent)  |   |  (判断 Agent)  |
   |                |   |                |   |                |
   | MiniMax-M2.5   |   | Qwen3-235B    |   | Qwen3-235B    |
   |                |   | (轻量)         |   | (严格)         |
   +-------+--------+   +-------+--------+   +-------+--------+
           |                    |                    |
           |     共享目录 (JSON 文件交换)             |
           |                    |                    |
           +---> shared/ <------|-----------> shared/
           |     execution_     |            test_results/
           |     records/       |            audit_reports/
           |                    |
           |                    v
           |            shared/env_reports/
           |            (环境状态报告)
           |
           +--- IPMI auth failure ---+
                                    |
                              EnvGuard 恢复
```

### 2.2 职责矩阵

| 职责 | ExecAgent | EnvGuard Agent | JudgeAgent |
|------|-----------|----------------|------------|
| 理解用例并执行 | [OK] | - | - |
| 调用 BMC 工具 | [OK] | [OK] (仅检查/修复) | - |
| 收集执行证据 | [OK] | - | - |
| 生成 ExecutionRecord | [OK] | - | - |
| 环境前置检查 | - | [OK] | - |
| 环境故障诊断 | - | [OK] | - |
| 环境修复执行 | - | [OK] | - |
| 环境恢复验证 | - | [OK] | [OK] (二次验证) |
| 严格判断结果 | - | - | [OK] |
| 生成审计报告 | - | - | [OK] |

### 2.3 与原"三 Agent 过度设计"论点的关系

CLAUDE.md 2.3 节曾拒绝三 Agent 方案，当时的理由是"过度设计"。现在重新引入，理由如下：

| 维度 | 旧三 Agent 方案 | 新 EnvGuard 方案 |
|------|----------------|-----------------|
| 第三个角色 | 不明确 | 专职环境守护，职责清晰 |
| 增加的复杂度 | 交互关系复杂 | EnvGuard 与 Exec 是单向请求/响应 |
| 解决的痛点 | 不明确 | 解决 Exec 职责膨胀 + 硬编码恢复 |
| 实际需求 | 可选 | v1.0.1 已验证恢复脚本必要性，升级为 Agent 是自然演进 |

**核心论点**: 不是为了三而三，而是因为环境检查/修复已经证明是必须功能（v1.0.1 分支的存在就是证据），从脚本升级为 Agent 是为了让它更智能、更可靠。

---

## 3. EnvGuard Agent 详细设计

### 3.1 职责边界

```
[OK]  执行前环境就绪检查
[OK]  检测并清理遗留测试数据（用户、配置变更等）
[OK]  修复环境异常（认证失败、服务不可达等）
[OK]  生成环境状态报告
[OK]  响应 ExecAgent 的恢复请求
[FAIL] 不执行测试用例
[FAIL] 不判断测试结果
[FAIL] 不修改 ExecutionRecord
```

### 3.2 触发时机

EnvGuard 在三个时机介入：

```
时间线: ───[1]──────────────────[2]──────────────────[3]──────>

[1] 用例执行前: pre_check()
    检查 BMC 可达性、认证、用户列表、基础服务
    如有异常，自动修复后再检查
    输出: EnvironmentReport (PASS/FAIL)

[2] 用例执行中: on_demand_recovery()
    ExecAgent 通过共享目录写入 recovery_request.json
    EnvGuard 检测并执行恢复
    触发条件: IPMI auth failure / Redfish 401 / SSH 拒绝连接

[3] 用例执行后: post_recovery()
    接收 ExecutionRecord，分析环境恢复需求
    执行恢复操作（清理用户、重置配置等）
    验证恢复结果
    输出: EnvironmentReport (recovered/not_recovered + warnings)
```

### 3.3 核心工作流

```
                    +-----------------+
                    |   pre_check()   |
                    +--------+--------+
                             |
                    +--------v--------+    FAIL
                    |  环境是否就绪?  +----------+
                    +--------+--------+          |
                             | PASS              |
                             |          +--------v--------+
                    +--------v--------+  |  诊断 + 自动修复  |
                    |  允许 Exec 执行  |  +--------+--------+
                    +-----------------+           |
                                                  |
                                      +-----------v-----------+
                                      | 重新检查环境 (最多N次)  |
                                      +-----------+-----------+
                                                  |
                                        修复成功?  |
                                     +--+        +--+
                                    YES           NO
                                     |             |
                              允许执行           跳过用例
                                                标记 SKIP


                    +-----------------+
                    | post_recovery() |
                    +--------+--------+
                             |
                    +--------v--------+
                    | 分析 ExecRecord |
                    | 识别需恢复项    |
                    +--------+--------+
                             |
                    +--------v--------+
                    | 执行恢复操作    |
                    | (清理/重置/验证)|
                    +--------+--------+
                             |
                    +--------v--------+
                    | 生成 EnvReport  |
                    | (供 Judge 参考)  |
                    +-----------------+
```

### 3.4 LLM 驱动的智能诊断

v1.0.1 的 `EnvironmentRecoveryTool` 使用硬编码决策树：

```python
# v1.0.1: 固定决策树
if not self.os_host:
    # Pure BMC mode
    auth_ok = await self._check_bmc_auth()
else:
    os_ok = await self._check_os_ssh()
    if os_ok:
        auth_ok = await self._check_bmc_auth()
        if not auth_ok:
            auth_ok = await self._os_ipmitool_reset_user2()
    else:
        power_ok = await self._redfish_force_power_cycle()
        ...
```

EnvGuard Agent 将采用 LLM 驱动的诊断方式：

```python
# EnvGuard: LLM 诊断 + 工具执行
async def diagnose_and_recover(self, env_state: dict) -> EnvReport:
    # 1. 收集环境状态（通过工具）
    env_data = await self._collect_environment_state()

    # 2. LLM 分析问题 + 制定恢复计划
    recovery_plan = await self._llm_diagnose(env_data, env_state)

    # 3. 按计划执行恢复操作（通过工具）
    for action in recovery_plan.actions:
        result = await self._execute_recovery_action(action)

    # 4. 验证恢复结果
    post_state = await self._collect_environment_state()

    # 5. LLM 确认恢复是否成功
    report = await self._llm_verify_recovery(env_data, post_state)

    return report
```

**优势**:
- 能处理未预见的故障组合
- 能根据错误信息动态调整恢复策略
- 能解释故障原因，而非仅记录 "recovered: false"

### 3.5 模型选型

| 考虑因素 | 选择 |
|---------|------|
| 复杂度 | 中等（诊断 + 决策，不需要像 Judge 那样严格推理） |
| 延迟要求 | 较宽松（恢复不在关键路径，100s 级可接受） |
| 准确性要求 | 中等（恢复失败可重试，不直接影响测试判定） |

**推荐**: 与 Judge 共用 Qwen3-235B-A22B，但使用不同的 temperature (0.2) 允许一定创造性。

或考虑使用更轻量的模型（如 Qwen3-30B）以降低成本和延迟，因为环境恢复的逻辑复杂度远低于严格判断。

**配置示例**:

```yaml
models:
  exec:
    provider: "minimax"
    model: "MiniMax-M2.5"
  judge:
    provider: "dashscope"
    model: "qwen3-235b-a22b"
    temperature: 0.0
  env_guard:
    provider: "dashscope"
    model: "qwen3-235b-a22b"
    temperature: 0.2
    max_tokens: 4096
```

---

## 4. 数据结构设计

### 4.1 EnvironmentReport（新增）

EnvGuard 的核心输出，记录环境状态和恢复操作。

```python
class EnvCheckItem(BaseModel):
    """单个环境检查项"""
    check_id: str                    # 检查项 ID，如 "bmc_reachability"
    category: str                    # 分类: connectivity / auth / user_mgmt / service
    name: str                        # 检查项名称（中文）
    status: Literal["pass", "fail", "warning", "skip"]
    detail: str                      # 详细信息
    evidence: Optional[str] = None   # 原始数据片段


class RecoveryAction(BaseModel):
    """恢复动作记录"""
    action_id: str
    action_type: str                 # delete_user / reset_password / power_cycle / reset_config / other
    target: str                      # 操作目标
    status: Literal["success", "failed", "skipped"]
    detail: str                      # 执行详情
    before_state: Optional[str] = None   # 操作前状态
    after_state: Optional[str] = None    # 操作后状态


class EnvironmentReport(BaseModel):
    """环境状态报告"""
    schema_version: Literal["1.0"] = "1.0"
    report_id: str                   # 报告唯一 ID
    report_type: Literal["pre_check", "post_recovery", "on_demand"]
    execution_id: Optional[str]      # 关联的执行记录 ID
    timestamp: datetime

    # 检查结果
    overall_status: Literal["ready", "not_ready", "recovered", "partially_recovered", "unrecoverable"]
    check_items: List[EnvCheckItem]

    # 恢复动作（仅在 post_recovery / on_demand 时有值）
    recovery_actions: List[RecoveryAction]

    # LLM 诊断结论
    diagnosis: Optional[str]         # LLM 对环境状态的诊断描述
    recommendations: List[str]       # 建议（如需人工介入）

    # 元信息
    guard_model: str                 # 使用的模型
    duration_seconds: float
```

### 4.2 RecoveryRequest（新增）

ExecAgent 向 EnvGuard 发起的恢复请求。

```python
class RecoveryRequest(BaseModel):
    """恢复请求"""
    request_id: str
    execution_id: str
    trigger: str                     # 触发原因，如 "ipmi_auth_failure"
    context: Dict[str, Any]          # 上下文信息（错误详情等）
    timestamp: datetime
```

### 4.3 共享目录扩展

```
shared/
+-- execution_records/     # Exec 写入，Judge 读取
+-- test_results/          # Judge 写入
+-- audit_reports/         # Judge 写入
+-- env_reports/           # EnvGuard 写入 (新增)
|   +-- pre_check_{case_id}.json
|   +-- post_recovery_{exec_id}.json
|   +-- on_demand_{request_id}.json
+-- recovery_requests/     # Exec 写入，EnvGuard 读取 (新增)
    +-- {request_id}.json
```

---

## 5. 工具集设计

EnvGuard Agent 可调用的工具（通过 LLM Tool Calling）：

### 5.1 工具列表

| 工具名 | 功能 | 复用 |
|--------|------|------|
| `env_redfish_request` | Redfish API 请求（检查/修改资源） | 复用 ExecAgent 的 `_tool_redfish_request` |
| `env_ipmi_command` | IPMI 命令执行 | 复用 ExecAgent 的 `_tool_ipmi_command` |
| `env_ssh_exec` | SSH 命令执行 | 复用 ExecAgent 的 `_tool_ssh_exec` |
| `env_check_bmc_auth` | BMC 认证状态检查 | 新增，封装 Redfish login 验证 |
| `env_list_users` | 获取用户列表（IPMI + Redfish 双通道） | 新增，组合查询 |
| `env_cleanup_users` | 清理指定范围的用户（默认 3-17） | 从 v1.0.1 `_delete_users_3_to_17` 演进 |
| `env_power_control` | 电源控制（开机/关机/强制重启） | 从 v1.0.1 `_redfish_force_power_cycle` 演进 |
| `env_reset_user` | 重置指定用户的名称/密码/权限 | 从 v1.0.1 `_force_restore_admin_user2` 演进 |
| `env_check_service` | 检查 BMC 服务状态 | 新增，综合可达性检查 |

### 5.2 工具实现策略

**复用优先**: `env_redfish_request`、`env_ipmi_command`、`env_ssh_exec` 直接复用 ExecAgent 的实现，通过继承或组合方式共享。

**高层封装**: `env_list_users`、`env_cleanup_users` 等是在底层工具上的高层封装，提供面向环境检查的语义化接口。

**设计原则**: 工具只提供能力，诊断和决策由 LLM 完成。EnvGuard 不硬编码恢复决策树。

---

## 6. 编排流程设计

### 6.1 main.py 修改

```python
async def run_single_case(case, exec_agent, env_guard, judge_agent, config):
    """
    单用例执行流程 (三 Agent 版)
    """

    # Phase 1: EnvGuard 前置检查
    env_report = await env_guard.pre_check(case)
    if env_report.overall_status == "unrecoverable":
        # 环境不可恢复，跳过此用例
        return build_skip_result(case, env_report)

    # Phase 2: Exec 执行用例
    exec_record = await exec_agent.execute(case, config)
    # 注意: ExecAgent 不再自己调用 recovery_tool

    # Phase 3: EnvGuard 后置恢复
    recovery_report = await env_guard.post_recovery(exec_record)

    # Phase 4: Judge 判断 (可参考 env_report)
    test_result = await judge_agent.judge_from_record(
        exec_record,
        env_context=recovery_report  # 新增: 传入环境报告
    )

    return test_result
```

### 6.2 on_demand_recovery 流程

ExecAgent 在执行过程中遇到环境异常时：

```
ExecAgent                     shared/                  EnvGuard
   |                            |                         |
   | 检测到 IPMI auth failure    |                         |
   |                            |                         |
   |---写 recovery_request.json-->|                        |
   |                            |                         |
   |                            |<--轮询/Watchdog检测------|
   |                            |                         |
   |                            |---执行恢复操作-------->|
   |                            |                         |
   |                            |<--写 recovery_response--|
   |                            |                         |
   |<--读取恢复结果--------------|                         |
   |                            |                         |
   | 用恢复后的凭据重试           |                         |
```

**备选方案（更简单）**: ExecAgent 直接调用 `env_guard.recover()` 方法，不通过文件交换。这样延迟更低，但增加耦合。建议作为初始实现方案，后续可改为异步文件交换。

---

## 7. Prompt 设计

### 7.1 System Prompt

```text
# 角色定义
你是 openUBMC 测试框架的环境守护引擎 (EnvGuard Agent)。
你的职责是确保 BMC 测试环境始终处于干净、可用状态。

# 核心原则
- 预防优于修复: 尽可能在执行前发现并解决问题
- 最小干预: 只做必要的恢复操作，不主动修改非相关配置
- 验证闭环: 每个恢复操作后必须验证结果
- 如实报告: 恢复失败时不隐瞒，明确报告不可恢复

# 工作模式
1. pre_check: 执行前检查环境就绪状态
2. post_recovery: 执行后清理环境
3. on_demand: 响应 ExecAgent 的恢复请求

# 环境检查清单
## 必查项
- BMC 网络可达性 (Redfish / IPMI)
- BMC 认证状态 (Administrator 用户可用)
- 用户列表状态 (仅保留 ID 2 Administrator)
- BMC 服务可用性 (Redfish API 响应正常)

## 选查项 (根据用例需求)
- 电源状态 (如用例不涉及电源操作，需确认处于稳定状态)
- 网络配置 (如用例涉及网络修改)
- SEL 日志 (可选，用于故障诊断)

# 恢复策略
## 用户管理
- 仅保留 ID 2 的 Administrator 用户
- 清理 ID 3-17 的所有用户
- 如 Administrator 被修改，通过 IPMI in-band 或 out-of-band 恢复

## 认证恢复
- 优先 IPMI in-band 恢复 (OS 可达时)
- 备选 IPMI out-of-band 恢复 (纯 BMC 模式)
- 最后手段: Redfish ForcePowerCycle 后重试

## 配置恢复
- 仅恢复被测试修改的配置项
- 不确定是否被修改时，不操作

# 输出格式
输出标准 JSON (EnvironmentReport):
{
  "report_id": "...",
  "report_type": "pre_check | post_recovery | on_demand",
  "overall_status": "ready | not_ready | recovered | partially_recovered | unrecoverable",
  "check_items": [...],
  "recovery_actions": [...],
  "diagnosis": "LLM 诊断描述",
  "recommendations": ["建议列表"]
}
```

### 7.2 Task Prompt

```text
## 任务: {task_type}

{context}

请执行环境{task_type}，输出 EnvironmentReport JSON。
```

---

## 8. 配置设计

### 8.1 config.yaml 新增段

```yaml
# 环境守护 Agent 配置
env_guard:
  enabled: true                       # 是否启用 EnvGuard
  pre_check: true                     # 执行前检查
  post_recovery: true                 # 执行后恢复
  max_recovery_retries: 3             # 最大恢复重试次数
  recovery_timeout_seconds: 300       # 单次恢复超时 (5分钟)
  skip_case_on_unrecoverable: true    # 环境不可恢复时是否跳过用例

  # 环境检查项配置 (可按需开关)
  checks:
    bmc_reachability: true
    bmc_auth: true
    user_cleanup: true
    service_health: true
    power_state: false                # 默认不检查电源状态
```

### 8.2 models 段扩展

```yaml
models:
  exec: ...
  judge: ...
  env_guard:                          # 新增
    provider: "dashscope"
    model: "qwen3-235b-a22b"
    temperature: 0.2
    max_tokens: 4096
```

---

## 9. 文件结构变更

```
src/
+-- agents/
|   +-- exec_agent.py          # 修改: 移除内置恢复逻辑
|   +-- judge_agent.py         # 修改: 可接收 env_context
|   +-- env_guard_agent.py     # 新增: EnvGuard Agent
+-- tools/
|   +-- redfish.py
|   +-- ipmi_tool.py
|   +-- ssh_tool.py
|   +-- ssh_session.py
|   +-- ssh_session_manager.py
|   +-- env_check_tools.py     # 新增: 环境检查专用工具集
|   +-- recovery_tools.py      # 新增: 环境恢复专用工具集
+-- prompts/
|   +-- exec_system.txt
|   +-- judge_system_core.txt
|   +-- judge_system_output.txt
|   +-- env_guard_system.txt   # 新增: EnvGuard system prompt
|   +-- env_guard_pre_check.txt    # 新增: 前置检查 task prompt
|   +-- env_guard_post_recovery.txt # 新增: 后置恢复 task prompt
|   +-- env_guard_on_demand.txt     # 新增: 按需恢复 task prompt
+-- core/
|   +-- schemas.py             # 修改: 新增 EnvironmentReport 等模型
```

---

## 10. 实施计划

### Phase 1: 基础框架 (骨架)

1. `src/core/schemas.py` 新增 `EnvironmentReport`、`RecoveryAction`、`EnvCheckItem` 数据模型
2. `src/agents/env_guard_agent.py` 基础骨架（pre_check / post_recovery / on_demand_recovery 接口）
3. `src/prompts/env_guard_system.txt` 基础 system prompt
4. `main.py` 接入 EnvGuard（pre_check + post_recovery 编排）

### Phase 2: 工具集实现

5. `src/tools/env_check_tools.py` 环境检查工具集（复用底层工具）
6. `src/tools/recovery_tools.py` 恢复工具集（从 v1.0.1 `EnvironmentRecoveryTool` 演进）
7. LLM Tool Calling 集成

### Phase 3: 编排集成

8. `main.py` 完整三 Agent 编排流程
9. ExecAgent 移除内置恢复逻辑，改为依赖 EnvGuard
10. JudgeAgent 可选接收环境报告作为参考

### Phase 4: 优化与测试

11. on_demand_recovery 异步机制
12. 环境报告持久化与可视化
13. 集成测试

---

## 11. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| EnvGuard LLM 调用增加延迟 | 每用例增加 10-30s | 可配置关闭 pre_check；使用轻量模型 |
| LLM 诊断不准确导致错误恢复 | 可能破坏环境 | 限制恢复操作范围；关键操作需二次确认 |
| 三 Agent 增加调试复杂度 | 故障定位更难 | 完善日志；EnvironmentReport 可追溯 |
| 与 ExecAgent 的工具集重复 | 代码冗余 | 工具实现通过继承/组合复用，仅 Prompt 独立 |

---

## 12. 备选方案: 简化版（不使用 LLM）

如果认为 LLM 驱动的环境恢复过度设计，备选方案是：

**保留 v1.0.1 的脚本式恢复，但重构为独立 Agent 类（不调用 LLM）**:

```python
class EnvGuardAgent:
    """环境守护 Agent（脚本版，不使用 LLM）"""

    async def pre_check(self) -> EnvironmentReport:
        # 纯脚本逻辑，按固定检查清单执行
        ...

    async def post_recovery(self, record: ExecutionRecord) -> EnvironmentReport:
        # 纯脚本逻辑，从 v1.0.1 EnvironmentRecoveryTool 演进
        ...
```

**优势**: 零延迟增加，确定性更强
**劣势**: 无法处理未预见场景，无法智能诊断

**建议**: 初始版本采用此方案，验证 Agent 骨架和编排流程。待基础稳定后，再在恢复决策点引入 LLM。

---

## 术语表

| 术语 | 定义 |
|------|------|
| EnvGuard Agent | 环境守护 Agent，专职环境检查与修复 |
| EnvironmentReport | 环境状态报告，EnvGuard 的核心输出 |
| RecoveryRequest | 恢复请求，Exec 向 EnvGuard 发起的异步请求 |
| pre_check | 前置检查，用例执行前的环境就绪验证 |
| post_recovery | 后置恢复，用例执行后的环境清理 |
| on_demand_recovery | 按需恢复，执行过程中的即时恢复响应 |
