# EnvGuard Agent 架构设计文档

> openUBMC AI 测试框架 -- 第三 Agent 架构设计
>
> 版本: DRAFT v0.2
>
> 日期: 2026-04-14
>
> 状态: 讨论稿，待确认后开工

---

## 0. 写在前面

本文档基于以下资料编写：
- `BMC-AI-TEST-PROJECT` 当前 develop 分支代码（双 Agent 架构）
- `BMC-AI-TEST-PROJECT-RELEASE` v1.0.1 分支的 `EnvironmentRecoveryTool` 实现
- 已有设计稿 `docs/ENV-GUARD-AGENT-DESIGN.md` (DRAFT v0.1)

**本文档定位**：在 v0.1 基础上，结合 v1.0.1 实际代码的深度分析，给出更落地的架构决策。重点讨论「为什么这样设计」和「哪些地方需要取舍」。

---

## 1. 现状诊断

### 1.1 当前恢复能力分布

| 位置 | 实现 | 能力 | 痛点 |
|------|------|------|------|
| ExecAgent (develop) | `_recovery_tool = EnvironmentRecoveryTool(target)` | 每条用例执行后自动恢复 | Exec 职责不纯粹；异常退出时恢复可能被跳过 |
| EnvironmentRecoveryTool (v1.0.1) | 硬编码决策树 ~185行 | 用户清理、认证恢复、电源控制 | 无法处理未预见场景；多故障组合时策略僵化 |
| JudgeAgent | Prompt 层面要求「环境恢复」 | 仅标记 warning | 不接触系统，无法真正执行恢复 |

### 1.2 v1.0.1 恢复决策树分析

v1.0.1 的 `EnvironmentRecoveryTool` 实现了两条恢复路径：

```
recover() 入口
    |
    +-- 强制执行: _force_restore_admin_user2() + _delete_users_3_to_17()
    |
    +-- 决策树:
        |
        +-- Pure BMC 模式 (无 OS):
        |   check_bmc_auth --> 失败则仅 warning，无恢复手段
        |
        +-- OS + BMC 模式:
            check_os_ssh -->
            +-- OS 可达: check_bmc_auth --> 失败则 _os_ipmitool_reset_user2()
            +-- OS 不可达: _redfish_force_power_cycle() --> 等待开机 --> check_bmc_auth
```

**v1.0.1 做得好的**：
1. 强制清理用户 (3-17) + 恢复 Administrator — 每次都执行，确保基线干净
2. OS in-band / BMC out-of-band 双通道恢复
3. ForcePowerCycle 只影响 Host，不影响 BMC 会话
4. 超时处理完善（30s binary timeout，auth retry with deadline）

**v1.0.1 的局限**：
1. 无法诊断「为什么认证失败」—— 只是反复尝试恢复
2. 多故障并发时（如认证失败 + 用户列表满 + 网络抖动），决策树无法动态组合策略
3. 恢复结果只有 `recovered: bool + warnings: List[str]`，颗粒度不够
4. 缺少执行前检查 — 不知道环境是否就绪就开始跑用例

### 1.3 核心痛点总结

```
痛点1: Exec 职责膨胀
  Exec = 执行用例 + 环境恢复 + RAG检索 + 证据收集
  职责过多，任何一处异常都可能影响整体

痛点2: 硬编码决策树
  固定的 if-else 路径，无法应对：
  - 新固件版本接口行为变化
  - 多种故障同时发生
  - 需要智能重试策略的间歇性故障

痛点3: 恢复时机单一
  只有 post-execution，缺少 pre-check 预防

痛点4: 恢复不透明
  Judge 无法利用恢复细节做更精准的判断
```

---

## 2. 架构决策

### 2.1 决策：EnvGuard 采用「渐进式 LLM 接入」策略

| 方案 | 描述 | 优势 | 劣势 |
|------|------|------|------|
| A. 纯脚本版 | 将 v1.0.1 代码重构为独立 Agent 类，不调 LLM | 零延迟、确定性 | 无法处理未预见场景 |
| B. 全 LLM 版 | 所有诊断和决策都通过 LLM | 智能灵活 | 延迟大、成本高、可能误操作 |
| **C. 渐进式（推荐）** | 骨架独立 + 检查脚本化 + 恢复决策 LLM 化 | 兼顾稳定与智能 | 架构稍复杂 |

**推荐方案 C**，分两阶段：

**Phase 1（初始版本）**：
- Agent 骨架独立（`env_guard_agent.py`）
- 环境检查：脚本化（复用 v1.0.1 的检查逻辑）
- 恢复执行：脚本化（复用 v1.0.1 的恢复逻辑）
- 恢复策略：从 v1.0.1 决策树迁移，但重构为可组合的策略模式
- LLM 仅参与：生成诊断描述 + 恢复结果总结

**Phase 2（进化版本）**：
- 恢复决策：引入 LLM Tool Calling，让 LLM 根据环境状态选择恢复策略
- 诊断能力：LLM 分析多故障组合，制定恢复计划
- 按需恢复：Exec 遇到环境异常时，EnvGuard 通过 LLM 动态决策

### 2.2 决策：on_demand 恢复采用直接调用而非文件交换

| 方案 | 描述 | 优势 | 劣势 |
|------|------|------|------|
| 文件交换 | Exec 写 request.json，EnvGuard 轮询 | 完全解耦 | 延迟高、实现复杂 |
| **直接调用（推荐）** | Exec 直接调用 `env_guard.recover()` | 延迟低、实现简单 | Exec 依赖 EnvGuard |

**理由**：
1. 三个 Agent 都在同一个 Python 进程中运行（`main.py` 编排），不存在跨进程通信需求
2. 延迟敏感 — Exec 遇到 auth failure 需要尽快恢复，文件轮询会引入数秒延迟
3. 文件交换适合跨进程/跨机器场景，当前架构不涉及
4. 如果将来需要独立部署，可以在接口层封装文件交换，内部实现不变

### 2.3 决策：EnvGuard 与 Exec 共用工具层，通过配置区分权限

```
src/tools/
  |-- ipmi_tool.py          # 底层 IPMI 操作（Exec + EnvGuard 共用）
  |-- ssh_tool.py           # 底层 SSH 操作（Exec + EnvGuard 共用）
  |-- ssh_session_manager.py
  |-- redfish_client.py     # Redfish HTTP 客户端（Exec + EnvGuard 共用）
  |-- env_check_tools.py    # 高层环境检查封装（EnvGuard 专用）
  |-- recovery_tools.py     # 高层恢复操作封装（EnvGuard 专用）
```

**设计理由**：
- 底层工具（IPMI/SSH/Redfish）是基础设施，不应重复实现
- 高层工具（env_check/recovery）是 EnvGuard 特有的业务逻辑
- Exec 的 Tool Calling tools 和 EnvGuard 的 Tool Calling tools 可以共用底层实现，但注册不同的 tool schema

---

## 3. 三 Agent 架构

### 3.1 架构图

```
                        main.py (编排脚本，非 Agent)
                            |
        +-------------------+-------------------+
        |                   |                   |
        v                   v                   v
+----------------+   +----------------+   +----------------+
|  Test_Exec     |   |  EnvGuard      |   |  Test_Judge    |
|  (执行 Agent)  |   |  (环境 Agent)  |   |  (判断 Agent)  |
|                |   |                |   |                |
| MiniMax-M2.5   |   | Qwen3-235B    |   | Qwen3-235B    |
| Tool Calling   |   | (轻量/渐进)   |   | temperature:0  |
+-------+--------+   +-------+--------+   +-------+--------+
        |                    |                    |
        |          共享目录 (JSON)                |
        |                    |                    |
        +---> shared/execution_records/           |
        |                    |                    |
        |            shared/env_reports/   <------+
        |                    |            Judge 参考 env_report
        |                    |
        +--- auth failure ---+
             直接调用 env_guard.on_demand_recover()
```

### 3.2 职责矩阵

| 职责 | ExecAgent | EnvGuard | JudgeAgent |
|------|-----------|----------|------------|
| 理解用例并执行 | 主责 | - | - |
| 调用 BMC 工具 | 主责 | 辅助（仅检查/修复） | - |
| 收集执行证据 | 主责 | - | - |
| 生成 ExecutionRecord | 主责 | - | - |
| 执行前环境检查 | - | 主责 | - |
| 执行后环境恢复 | - | 主责 | - |
| 执行中环境急救 | 触发 | 主责 | - |
| 严格判断结果 | - | - | 主责 |
| 环境恢复二次验证 | - | 自验证 | - |
| 生成审计报告 | - | - | 主责 |

### 3.3 与 CLAUDE.md 「三 Agent 过度设计」的调和

CLAUDE.md 2.3 节拒绝了三 Agent 方案。现在的理由是：

> 不是为了三而三，而是因为环境检查/修复已经证明是必须功能。
> v1.0.1 的 `EnvironmentRecoveryTool` 存在就是证据。
> 从硬编码脚本升级为独立 Agent 是自然演进，不是过度设计。

具体区别：

| 维度 | 被拒绝的三 Agent | 现在的 EnvGuard |
|------|-----------------|----------------|
| 第三个角色 | 不明确 | 专职环境守护 |
| 增加的交互 | 复杂双向通信 | Exec 单向调用 EnvGuard |
| 解决的问题 | 不明确 | 解耦 Exec 恢复职责 + 智能恢复 |
| 验证依据 | 无 | v1.0.1 已验证恢复脚本必要性 |

---

## 4. EnvGuard Agent 详细设计

### 4.1 类骨架

```python
class EnvGuardAgent:
    """环境守护 Agent

    Phase 1: 检查和恢复逻辑为脚本化，LLM 仅做诊断描述
    Phase 2: 恢复决策引入 LLM Tool Calling
    """

    def __init__(self, config, client_factory, tools):
        self._config = config
        self._client_factory = client_factory
        self._check_tools = EnvCheckTools(tools)      # 高层环境检查封装
        self._recovery_tools = RecoveryTools(tools)    # 高层恢复操作封装
        self._llm_client = None  # Phase 2 启用

    async def pre_check(self, case: dict) -> EnvironmentReport:
        """用例执行前环境就绪检查"""
        ...

    async def post_recovery(self, exec_record: ExecutionRecord) -> EnvironmentReport:
        """用例执行后环境恢复"""
        ...

    async def on_demand_recover(self, trigger: str, context: dict) -> EnvironmentReport:
        """执行中环境急救（Exec 直接调用）"""
        ...
```

### 4.2 pre_check 流程

```
pre_check(case)
    |
    +-- 1. 收集环境状态（脚本化）
    |      - BMC 网络可达性 (Redfish GET /redfish/v1)
    |      - BMC 认证状态 (Redfish login)
    |      - 用户列表状态 (IPMI user list)
    |      - 基础服务健康 (Redfish GET /redfish/v1/Systems)
    |
    +-- 2. 生成检查报告
    |      - 每个 check_item: {status, detail, evidence}
    |
    +-- 3. 如有 FAIL 项，自动修复
    |      - 用户残留 -> cleanup_users()
    |      - 认证失败 -> restore_admin_user()
    |      - 重新检查 (最多 N 次)
    |
    +-- 4. 输出 EnvironmentReport
           - overall_status: ready / not_ready / unrecoverable
           - 写入 shared/env_reports/pre_check_{case_id}.json
```

**与 v1.0.1 的区别**：
- v1.0.1 没有前置检查，只有后置恢复
- pre_check 新增了「用户列表是否干净」的检查
- pre_check 的修复策略复用 v1.0.1 的 `_delete_users_3_to_17` 和 `_force_restore_admin_user2`

### 4.3 post_recovery 流程

```
post_recovery(exec_record)
    |
    +-- 1. 分析 ExecutionRecord，识别需恢复项
    |      - 哪些步骤修改了用户？(添加/删除/修改)
    |      - 哪些步骤修改了配置？(密码/权限/网络)
    |      - 哪些步骤涉及电源操作？
    |
    +-- 2. 强制基线恢复（来自 v1.0.1）
    |      - _force_restore_admin_user2()
    |      - _delete_users_3_to_17()
    |
    +-- 3. 目标恢复（基于 ExecutionRecord 分析）
    |      - 如果用例修改了特定配置，尝试恢复
    |      - 如果用例涉及电源操作，检查电源状态
    |
    +-- 4. 验证恢复结果
    |      - 重新运行 pre_check 检查项
    |      - 确认所有项为 PASS
    |
    +-- 5. 生成 EnvironmentReport
           - recovery_actions: 每个恢复动作的详情
           - 写入 shared/env_reports/post_recovery_{exec_id}.json
```

**与 v1.0.1 的区别**：
- v1.0.1 不分析 ExecutionRecord，盲目执行固定恢复
- post_recovery 会根据用例内容做针对性恢复
- 恢复验证更严格 — 重新检查所有项目

### 4.4 on_demand_recover 流程

```
on_demand_recover(trigger, context)
    |
    trigger 可能是:
    - "ipmi_auth_failure": IPMI 认证失败
    - "redfish_401": Redfish 未授权
    - "ssh_connection_refused": SSH 连接被拒
    - "user_list_full": 用户列表已满
    |
    +-- 根据 trigger 选择恢复策略:
    |      - auth 类 -> restore_admin_user() (复用 v1.0.1 _os_ipmitool_reset_user2)
    |      - connection 类 -> check_reachability() + power_cycle() (复用 v1.0.1)
    |      - user_full 类 -> cleanup_users() (复用 v1.0.1 _delete_users_3_to_17)
    |
    +-- 验证恢复结果
    +-- 返回 EnvironmentReport
```

**Exec 调用方式**：

```python
# exec_agent.py 中
if ipmi_auth_failed:
    env_report = await self._env_guard.on_demand_recover(
        trigger="ipmi_auth_failure",
        context={"error": str(e), "step_id": step.step_id}
    )
    if env_report.overall_status == "recovered":
        # 用恢复后的凭据重试
        result = await self._retry_step(step)
```

---

## 5. 数据结构

### 5.1 EnvironmentReport（EnvGuard 核心输出）

```python
class EnvCheckItem(BaseModel):
    """单个环境检查项"""
    check_id: str                    # "bmc_reachability", "bmc_auth", "user_list", "service_health"
    category: str                    # "connectivity" / "auth" / "user_mgmt" / "service"
    name: str                        # 中文名称
    status: Literal["pass", "fail", "warning", "skip"]
    detail: str                      # 检查详情
    evidence: Optional[str] = None   # 原始数据片段


class RecoveryAction(BaseModel):
    """恢复动作记录"""
    action_id: str
    action_type: str                 # "delete_user" / "reset_password" / "power_cycle" / "reset_config"
    target: str                      # 操作目标
    status: Literal["success", "failed", "skipped"]
    detail: str
    before_state: Optional[str] = None
    after_state: Optional[str] = None


class EnvironmentReport(BaseModel):
    """环境状态报告"""
    report_id: str
    report_type: Literal["pre_check", "post_recovery", "on_demand"]
    execution_id: Optional[str] = None
    timestamp: datetime

    overall_status: Literal[
        "ready",             # 环境就绪
        "not_ready",         # 环境未就绪（已尝试修复但失败）
        "recovered",         # 环境已恢复
        "partially_recovered", # 部分恢复
        "unrecoverable"      # 无法恢复
    ]
    check_items: List[EnvCheckItem] = []
    recovery_actions: List[RecoveryAction] = []

    # LLM 诊断（Phase 1 可选，Phase 2 必须）
    diagnosis: Optional[str] = None
    recommendations: List[str] = []

    # 元信息
    duration_seconds: float = 0.0
```

### 5.2 对现有数据模型的影响

**ExecutionRecord**：需要新增字段标记环境报告关联

```python
class ExecutionRecord(BaseModel):
    # ... 现有字段保持不变 ...
    env_report_id: Optional[str] = None  # 新增：关联的 EnvironmentReport ID
```

**TestResult**：无需修改，Judge 可以通过读取 `shared/env_reports/` 获取环境报告

### 5.3 共享目录扩展

```
shared/
  |-- execution_records/     # Exec 写入，Judge 读取
  |-- test_results/          # Judge 写入
  |-- audit_reports/         # Judge 写入
  |-- evidence/              # Exec 写入
  |-- env_reports/           # EnvGuard 写入 (新增)
  |   |-- pre_check_{case_id}.json
  |   |-- post_recovery_{exec_id}.json
  |   |-- on_demand_{timestamp}.json
```

---

## 6. 工具集设计

### 6.1 工具复用策略

```
                  +-----------------------+
                  |   底层工具 (共用)      |
                  |  IpmiTool             |
                  |  SshTool              |
                  |  RedfishClient        |
                  |  SshSessionManager    |
                  +-----------+-----------+
                              |
                +-------------+-------------+
                |                           |
    +-----------v-----------+   +-----------v-----------+
    | Exec Agent Tools      |   | EnvGuard Tools        |
    | (Tool Calling 注册)    |   | (高层封装)             |
    |                       |   |                       |
    | redfish_request       |   | env_check_tools.py    |
    | ipmi_command          |   |  - check_bmc_reach()  |
    | ssh_exec              |   |  - check_bmc_auth()   |
    | bmc_command_rag       |   |  - check_user_list()  |
    |                       |   |  - check_service()    |
    +-----------------------+   |                       |
                                | recovery_tools.py     |
                                |  - cleanup_users()    |
                                |  - restore_admin()    |
                                |  - power_control()    |
                                |  - reset_config()     |
                                +-----------------------+
```

### 6.2 EnvCheckTools（环境检查工具集）

| 方法 | 功能 | 来源 |
|------|------|------|
| `check_bmc_reachability()` | Redfish GET /redfish/v1 | 新增 |
| `check_bmc_auth()` | Redfish login/logout | 复用 v1.0.1 `_check_bmc_auth()` |
| `check_user_list()` | IPMI user list + Redfish 双通道 | 新增，组合查询 |
| `check_service_health()` | Redfish GET /redfish/v1/Systems | 新增 |

### 6.3 RecoveryTools（恢复工具集）

| 方法 | 功能 | 来源 |
|------|------|------|
| `force_restore_admin_user2()` | 强制恢复 ID 2 Administrator | 从 v1.0.1 `_force_restore_admin_user2()` 迁移 |
| `delete_users_3_to_17()` | 清理测试用户 | 从 v1.0.1 `_delete_users_3_to_17()` 迁移 |
| `os_ipmitool_reset_user2()` | OS 侧 in-band 恢复 | 从 v1.0.1 `_os_ipmitool_reset_user2()` 迁移 |
| `redfish_force_power_cycle()` | Host 电源控制 | 从 v1.0.1 `_redfish_force_power_cycle()` 迁移 |
| `wait_and_check_bmc_auth()` | 等待并验证认证恢复 | 从 v1.0.1 `_wait_and_check_bmc_auth()` 迁移 |

### 6.4 恢复策略模式

v1.0.1 用 if-else 决策树，我们重构为可组合的策略模式：

```python
class RecoveryStrategy(ABC):
    """恢复策略基类"""
    @abstractmethod
    async def execute(self, context: dict) -> RecoveryAction:
        ...

class PureBmcRecovery(RecoveryStrategy):
    """纯 BMC 模式恢复"""
    async def execute(self, context):
        # 仅检查认证，无 OS 侧恢复路径
        ...

class OsBmcRecovery(RecoveryStrategy):
    """OS + BMC 模式恢复"""
    async def execute(self, context):
        # 复用 v1.0.1 的 OS可达->认证检查->in-band恢复 路径
        ...

class AuthRecovery(RecoveryStrategy):
    """认证恢复"""
    async def execute(self, context):
        # 复用 v1.0.1 的认证恢复逻辑
        ...

class UserCleanupRecovery(RecoveryStrategy):
    """用户清理恢复"""
    async def execute(self, context):
        # 复用 v1.0.1 的用户清理逻辑
        ...
```

**好处**：
- Phase 1：策略选择仍然是硬编码（根据 os_host 是否配置）
- Phase 2：可以引入 LLM 来选择和组合策略

---

## 7. 编排流程

### 7.1 main.py 修改

```python
async def run_single_case(case, exec_agent, env_guard, judge_agent, config):
    """单用例执行流程（三 Agent 版）"""

    # Phase 1: EnvGuard 前置检查
    pre_check_report = await env_guard.pre_check(case)
    if pre_check_report.overall_status == "unrecoverable":
        logger.warning(f"[SKIP] 环境不可恢复: {pre_check_report.diagnosis}")
        return build_skip_result(case, pre_check_report)

    # Phase 2: Exec 执行用例
    # ExecAgent 不再内置恢复逻辑
    # ExecAgent 持有 env_guard 引用，遇到环境异常可直接调用
    exec_record = await exec_agent.execute(case, config)

    # Phase 3: EnvGuard 后置恢复
    recovery_report = await env_guard.post_recovery(exec_record)

    # Phase 4: Judge 判断
    # Judge 可读取 env_report 获取环境状态信息
    test_result = await judge_agent.judge_from_record(exec_record)

    return test_result
```

### 7.2 ExecAgent 改动点

```python
class ExecAgent:
    def __init__(self, ..., env_guard=None):
        self._env_guard = env_guard  # 新增：注入 EnvGuard
        # 移除: self._recovery_tool = EnvironmentRecoveryTool(target)

    async def execute(self, case, config):
        # ... 正常执行逻辑 ...

        # 遇到环境异常时：
        if self._env_guard and is_env_error(error):
            report = await self._env_guard.on_demand_recover(
                trigger=classify_error(error),
                context={"error": str(error), "step_id": step.step_id}
            )
            if report.overall_status == "recovered":
                # 重试当前步骤
                continue

        # ... 执行结束，不再调用 self._recovery_tool.recover() ...
```

### 7.3 JudgeAgent 改动点

**最小改动**：Judge 不需要直接依赖 EnvGuard。

Judge 如果需要参考环境报告，可以通过读取 `shared/env_reports/` 目录获取。这保持了 Judge 不接触外部系统的原则。

可选增强：在 Judge 的 Task Prompt 中附加环境报告摘要：

```python
# judge_agent.py
async def judge_from_record(self, exec_record, env_summary=None):
    task = f"请根据以下 Execution Record 进行判断：\n{exec_record_json}"
    if env_summary:
        task += f"\n\n## 环境状态参考\n{env_summary}"
    # ...
```

---

## 8. 配置设计

### 8.1 config.yaml 新增

```yaml
# 环境守护 Agent
env_guard:
  enabled: true
  pre_check: true
  post_recovery: true
  max_recovery_retries: 3
  recovery_timeout_seconds: 300
  skip_case_on_unrecoverable: true

  # 环境检查项开关
  checks:
    bmc_reachability: true
    bmc_auth: true
    user_cleanup: true
    service_health: true
    power_state: false         # 默认不检查

  # 恢复等待参数 (来自 v1.0.1)
  wait_min: 180               # ForcePowerCycle 后最小等待 (秒)
  wait_max: 300               # 最大等待 (秒)
  auth_retry_interval: 30     # 认证重试间隔 (秒)

models:
  exec:
    provider: "minimax"
    model: "MiniMax-M2.5"
  judge:
    provider: "dashscope"
    model: "qwen3-235b-a22b"
    temperature: 0.0
  env_guard:                   # Phase 2 启用，Phase 1 可不配
    provider: "dashscope"
    model: "qwen3-235b-a22b"
    temperature: 0.2
    max_tokens: 4096
```

---

## 9. 文件结构变更

```
BMC-AI-TEST-PROJECT/
  src/
    agents/
      |-- exec_agent.py          # 修改: 注入 env_guard，移除内置恢复
      |-- judge_agent.py         # 修改: 可选接收 env_summary
      |-- env_guard_agent.py     # 新增: EnvGuard Agent 骨架
    tools/
      |-- ipmi_tool.py           # 不变
      |-- ssh_tool.py            # 不变
      |-- ssh_session_manager.py # 不变
      |-- env_check_tools.py     # 新增: 环境检查高层封装
      |-- recovery_tools.py      # 新增: 恢复操作高层封装（从 v1.0.1 迁移）
    prompts/
      |-- exec_system.txt        # 不变
      |-- judge_system_*.txt     # 不变
      |-- env_guard_system.txt   # 新增: Phase 2 使用
    core/
      |-- schemas.py             # 修改: 新增 EnvironmentReport 等模型
      |-- config.py              # 修改: 新增 env_guard 配置解析
  main.py                        # 修改: 三 Agent 编排流程
  config/
      |-- config.yaml            # 修改: 新增 env_guard 段
  shared/
      |-- env_reports/           # 新增: 环境报告目录
```

---

## 10. 实施计划

### Phase 1: 骨架搭建（脚本化恢复）

> 目标：EnvGuard 独立运作，恢复逻辑从 v1.0.1 迁移，不引入 LLM

| 步骤 | 内容 | 预计改动 |
|------|------|---------|
| 1.1 | `schemas.py` 新增 EnvironmentReport 等数据模型 | 新增 ~80 行 |
| 1.2 | `recovery_tools.py` 从 v1.0.1 迁移恢复逻辑 | 迁移 ~200 行 |
| 1.3 | `env_check_tools.py` 环境检查封装 | 新增 ~120 行 |
| 1.4 | `env_guard_agent.py` Agent 骨架 | 新增 ~200 行 |
| 1.5 | `main.py` 接入三 Agent 编排 | 修改 ~30 行 |
| 1.6 | `exec_agent.py` 移除内置恢复，注入 env_guard | 修改 ~20 行 |
| 1.7 | `config.yaml` 新增 env_guard 配置 | 新增 ~15 行 |

### Phase 2: LLM 智能恢复（Tool Calling）

> 目标：恢复决策由 LLM 驱动，能处理未预见场景

| 步骤 | 内容 |
|------|------|
| 2.1 | `env_guard_system.txt` LLM system prompt |
| 2.2 | EnvGuard 注册 Tool Calling 工具 |
| 2.3 | LLM 驱动的诊断与恢复决策 |
| 2.4 | 多策略组合与动态调整 |

### Phase 3: 优化与增强

| 步骤 | 内容 |
|------|------|
| 3.1 | on_demand_recovery 完善错误分类 |
| 3.2 | 环境报告可视化（控制台 + 文件） |
| 3.3 | Judge 集成环境报告参考 |
| 3.4 | 恢复策略经验积累（RAG 扩展） |

---

## 11. 风险与缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| EnvGuard 恢复失败导致后续用例全部 SKIP | 连续 SKIP | `skip_case_on_unrecoverable` 可配置关闭；恢复失败时尝试降级策略 |
| 三 Agent 增加调试复杂度 | 故障定位更难 | EnvironmentReport 完整记录恢复过程；日志链路追踪 |
| 从 v1.0.1 迁移代码引入 bug | 恢复不可靠 | Phase 1 迁移后，用 v1.0.1 相同场景做回归测试 |
| 工具层复用导致 Exec 和 EnvGuard 耦合 | 修改一处影响另一处 | 共用底层工具，但高层封装各自独立；底层工具变更需双端测试 |
| Phase 2 LLM 恢复决策错误 | 可能错误操作 BMC | 关键操作（power_cycle、user_delete）增加二次确认机制 |

---

## 12. 讨论要点（待确认）

以下问题需要在开工前确认：

### Q1: 恢复策略选择
Phase 1 是否采用「完全复用 v1.0.1 决策树逻辑」？还是趁重构机会做策略模式抽象？

> 建议：先完全复用 v1.0.1，验证骨架可行后再重构策略模式。

### Q2: pre_check 检查范围
pre_check 是否需要根据用例内容动态调整检查项？还是固定检查列表？

> 建议：Phase 1 固定检查列表，Phase 2 引入 LLM 根据用例内容选择检查项。

### Q3: ExecAgent 移除恢复逻辑的时机
Phase 1 是否就移除 ExecAgent 中的 `EnvironmentRecoveryTool`？还是保留两套，EnvGuard 作为增强？

> 建议：Phase 1 就移除，避免两套恢复逻辑并存导致混乱。

### Q4: EnvGuard 模型选择
Phase 2 引入 LLM 时，是否与 Judge 共用 Qwen3-235B-A22B？还是使用更轻量模型？

> 建议：先用 Qwen3-235B-A22B (temperature: 0.2)，验证可行后再考虑降级到轻量模型。

### Q5: on_demand_recover 的错误分类
ExecAgent 中哪些错误类型应触发 on_demand_recover？初始版本需要支持哪些？

> 建议初始支持：ipmi_auth_failure, redfish_401, ssh_connection_refused, user_list_full

---

## 附录: 术语表

| 术语 | 定义 |
|------|------|
| EnvGuard Agent | 环境守护 Agent，专职环境检查与修复 |
| EnvironmentReport | 环境状态报告，EnvGuard 的核心输出 |
| pre_check | 前置检查，用例执行前的环境就绪验证 |
| post_recovery | 后置恢复，用例执行后的环境清理 |
| on_demand_recovery | 按需恢复，执行过程中的即时恢复响应 |
| RecoveryStrategy | 恢复策略模式，可组合的恢复方案抽象 |
