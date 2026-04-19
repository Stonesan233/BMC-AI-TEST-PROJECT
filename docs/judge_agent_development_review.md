# Judge Agent 开发审查报告

> 审查日期: 2026-04-12
> 审查范围: `src/agents/judge_agent.py` 及关联模块
> Schema 版本: v2.1

---

## 1. 总体评价

Judge Agent 已具备**完整的核心功能**，代码质量较高，架构清晰，防护层次完善。整体完成度约 **85%**，距生产可用还需补充边界场景和集成验证。

| 维度 | 评级 | 说明 |
|------|------|------|
| 核心功能 | 完成 | 判断流程、JSON 解析、报告生成均实现 |
| 数据模型 | 完成 | v2.1 Schema 含 assertions / risk_notes / evidence_sufficient |
| Prompt 工程 | 完成 | 三层 Prompt 模块化组合，含 thinking 输出要求 |
| 测试覆盖 | 大部分完成 | 8 个单元测试 + 4 个真实 LLM 集成测试 |
| 错误处理 | 完成 | 三层防护：模型调用 / JSON 解析 / TestResult 构建 |
| 版本迁移 | 完成 | v1 -> v2.1 自动迁移 |
| 批量判断 | 完成 | `judge_batch` 支持并发信号量 |
| 文档 | 完成 | CLAUDE.md 架构文档详尽 |

---

## 2. 模块结构一览

```
judge_agent 相关文件
|
|-- src/agents/judge_agent.py          # 核心: 941 行, 5 个类/函数组
|   |-- JudgeOutputParser              # LLM 输出解析器 (JSON + Markdown)
|   |-- build_test_result_from_json()  # JSON -> TestResult 构建
|   |-- build_error_test_result()      # 异常 fallback TestResult
|   |-- write_audit_report()           # 审计报告文件写入
|   |-- generate_fallback_audit_report()  # fallback 报告生成
|   |-- JudgeAgent                     # 主类 (judge/judge_from_record/judge_batch)
|
|-- src/core/schemas.py               # 数据模型 (v2.1)
|-- src/core/config.py                # 配置加载
|-- src/core/client_factory.py         # AsyncOpenAI 客户端工厂
|
|-- src/prompts/
|   |-- judge_system_v2.1.txt          # 完整系统 Prompt (272 行)
|   |-- judge_system_core.txt          # 核心规则模块
|   |-- judge_system_openubmc.txt      # 领域知识模块
|   |-- judge_system_output.txt        # 输出格式模块
|   |-- judge_user_template_v2.1.txt   # 用户模板 (Layer 3)
|
|-- tests/
|   |-- test_judge_agent.py            # 单元测试 (8 个)
|   |-- test_judge_agent_real_llm.py   # 真实 LLM 测试 (4 个)
|
|-- tests/fixtures/execution_records/
|   |-- case_pass_strict.json          # 表面通过但存在不一致
|   |-- case_fail_clear.json           # 明显失败
|   |-- case_v1_compatible.json        # v1 旧格式
|   |-- case_error_timeout.json        # 超时错误
```

---

## 3. 核心流程分析

### 3.1 判断主流程

```
judge() / judge_from_record()
    |
    v
[Step 1] 加载/迁移 ExecutionRecord (v1 -> v2.1 自动)
    |
    v
[Step 2] _execute_judgment()
    |   |
    |   +--> _call_judge_model()  -- AsyncOpenAI 调用
    |   |       System Prompt (Layer 1+2)
    |   |       + User Template (Layer 3, 填入 execution_record_json)
    |   |       temperature=0.0, max_tokens from config
    |   |
    |   +--> _parser.parse(raw_response)
    |   |       去除 <thinking> 块
    |   |       提取 JSON (4 策略: code block -> balanced braces -> brute)
    |   |       提取 Markdown (AUDIT_REPORT_START/END)
    |   |
    |   +--> build_test_result_from_json()
    |           构建 TestResult, 所有缺省值倾向 FAIL
    |
    v
[Step 3] _persist_audit_report()
    |   Judge 返回的 Markdown 优先, 否则 fallback 自动生成
    |
    v
[Return] TestResult
```

### 3.2 三层 Prompt 架构

| 层级 | 文件 | 职责 |
|------|------|------|
| Layer 1: 核心规则 | `judge_system_core.txt` | 角色定义、宁可错杀原则、判断流程 5 个 Phase、字段匹配规则、假 PASS 风险评估 |
| Layer 2: 领域知识 | `judge_system_openubmc.txt` | 用户管理边界、Redfish 响应判断、电源状态、传感器数据、IPMI 命令 |
| Layer 2: 输出格式 | `judge_system_output.txt` | JSON Schema 定义、Markdown 审计报告模板、最终强调 |
| Layer 3: 任务模板 | `judge_user_template_v2.1.txt` | 判断要求、判断顺序提醒、`{execution_record_json}` 占位符 |

**加载策略**: 优先检测 3 个拆分文件是否存在，存在则按序组合；否则 fallback 到单文件 `judge_system_v2.1.txt`。

### 3.3 JSON 解析四策略

`JudgeOutputParser._extract_json()` 按优先级依次尝试：

1. ````json ... ``` `` 代码块
2. ```` ... ``` `` 代码块 (无语言标记)
3. 括号配对提取 (考虑字符串内转义)
4. 暴力截取: 第一个 `{` 到最后一个 `}`

附带修复: 移除尾逗号、移除控制字符。

---

## 4. 数据模型 (v2.1)

### 4.1 ExecutionRecord 关键字段

| 字段 | 类型 | 版本 | 说明 |
|------|------|------|------|
| `schema_version` | `Literal["2.1"]` | v2.1 新增 | 强制为 "2.1"，frozen |
| `steps[].assertions` | `List[Assertion]` | v2.1 新增 | 结构化断言列表 |
| `steps[].evidence[].text_summary` | `Optional[str]` | v2.1 新增 | 多模态证据文本摘要 |
| `steps[].keyword` | `Optional[str]` | v2.1 新增 | Robot Framework 预留 |
| `environment_recovery_actions` | `List[EnvironmentRecoveryAction]` | v2.1 新增 | 环境恢复记录 |
| `consolidated_audit_draft` | `Optional[str]` | v2.1 新增 | Exec 生成的审计草案 |

### 4.2 TestResult 关键字段

| 字段 | 类型 | 版本 | 说明 |
|------|------|------|------|
| `step_results[].assertion_judgments` | `List[AssertionJudgment]` | v2.1 新增 | 逐条断言判断 |
| `step_results[].evidence_sufficient` | `bool` | v2.1 新增 | 证据充分性 |
| `false_pass_risk` | `Literal["none","low","medium","high"]` | v2 新增 | 假 PASS 风险 |
| `risk_notes` | `List[str]` | v2.1 新增 | 风险说明 |
| `judge_model` | `str` | v2 新增 | 使用的判断模型 |
| `judge_duration_seconds` | `float` | v2 新增 | 判断耗时 |
| `audit_report_markdown` | `Optional[str]` | v2 新增 | 审计报告内容 |

### 4.3 Assertion 模型

| 字段 | 类型 | 说明 |
|------|------|------|
| `assertion_type` | Literal[8种] | field_equals / field_contains / status_code 等 |
| `operator` | Literal[11种] | eq / ne / contains / matches / gt 等 |
| `field_path` | `str` | JSONPath 风格, 如 "body.PowerState" |

---

## 5. 测试覆盖分析

### 5.1 单元测试 (`test_judge_agent.py`, 8 个)

| # | 测试名 | 覆盖点 | 状态 |
|---|--------|--------|------|
| 1 | `test_v21_schema_and_consolidated_draft` | v2.1 Schema 验证、text_summary、consolidated_audit_draft | 完成 |
| 2 | `test_v1_migration` | v1 -> v2.1 自动迁移 | 完成 |
| 3 | `test_cli_judge_flag_parsing` | --judge / --no-judge CLI 参数 | 完成 |
| 4 | `test_judge_strict_verdict_simulation` | 严格判断模拟 (Vendor 大小写不匹配) | 完成 |
| 5 | `test_judge_clear_fail_case` | 明显失败案例判断 | 完成 |
| 6 | `test_judge_error_handling` | 超时/错误处理 | 完成 |
| 7 | `test_audit_report_generation` | audit_report.md 文件生成 | 完成 |
| 8 | `test_judge_full_integration` | 完整集成测试 (需真实 API) | 完成, 有 skip 保护 |

### 5.2 真实 LLM 测试 (`test_judge_agent_real_llm.py`, 4 个)

| # | 测试名 | 覆盖点 | 状态 |
|---|--------|--------|------|
| 1 | `test_real_qwen3_strict_verdict` | 真实 LLM 严格判断 | 完成, 需 API Key |
| 2 | `test_real_qwen3_clear_fail` | 真实 LLM 明显失败 | 完成, 需 API Key |
| 3 | `test_real_qwen3_v1_migration` | 真实 LLM + v1 迁移 | 完成, 需 API Key |
| 4 | `test_real_qwen3_error_handling` | 真实 LLM + 错误处理 | 完成, 需 API Key |

### 5.2 Fixture 数据

| 文件 | 场景 |
|------|------|
| `case_pass_strict.json` | 表面 PASS 但存在字段不一致 (Vendor 大小写) |
| `case_fail_clear.json` | 明显 FAIL (HTTP 400, 步骤失败) |
| `case_v1_compatible.json` | v1 旧格式 (无 schema_version) |
| `case_error_timeout.json` | 执行超时/错误 |

---

## 6. 已发现的问题与建议

### 6.1 代码问题

| 严重度 | 位置 | 描述 |
|--------|------|------|
| **中** | `judge_agent.py:637` | `_get_client()` 引用 `self._client` 但 `__init__` 中未初始化该属性。当使用 `client_factory` 模式（非外部注入）时，首次调用会抛 `AttributeError`。需在 `__init__` 中添加 `self._client: Optional[AsyncOpenAI] = None` |
| **低** | `judge_agent.py:558` | `_load_combined_system_prompt()` 中如果 fallback 单文件也不存在，抛 `FileNotFoundError` 但缺少上下文提示 |
| **低** | `judge_agent.py:39` | import 中引用 `StepJudgment` 但该符号实际在当前文件中未使用 (由 `_build_step_judgment` 内部构建)，不影响功能 |

### 6.2 架构建议

| 优先级 | 建议 |
|--------|------|
| **高** | 增加对 `consolidated_audit_draft` 的利用 -- 当前 Judge 的 User Template 未明确提及参考 Exec Agent 的审计草案，v2.1 新增此字段的目的未完全发挥 |
| **中** | `judge_batch` 默认 `max_concurrency=1` (串行)，建议根据实际 LLM 服务能力支持配置化并发 |
| **中** | 缺少重试机制 -- LLM 调用偶发失败时无法自动重试，建议增加可配置的 retry (如 2-3 次) |
| **低** | `JudgeOutputParser._extract_balanced_json()` 的状态机实现正确但较复杂，可考虑用 `json.JSONDecoder.raw_decode()` 替代 |
| **低** | `_safe_enum()` 和 `_PASS_FAIL` / `_RISK_LEVELS` 为模块级常量，可考虑封装到枚举类中 |

### 6.3 测试补充建议

| 优先级 | 缺失场景 |
|--------|---------|
| **高** | 缺少 **真正 PASS 的严格验证** -- 所有 fixture 都是 FAIL 或 edge case，应增加一个"证据充分、完全匹配"的 PASS fixture，验证 Judge 正确放行 |
| **高** | 缺少 **多步骤用例** fixture -- 当前 fixture 基本是单步骤，应增加 3-5 步骤的复杂场景 |
| **中** | 缺少 **assertions 非空** 的 fixture -- v2.1 核心新特性之一，但测试中未充分覆盖真实断言判断 |
| **中** | 缺少 **异常 JSON 输出** 的测试 -- 如模型返回截断的 JSON、缺少必填字段的 JSON |
| **低** | 缺少 **并发安全** 测试 -- `judge_batch` 在并发下的线程/协程安全性 |

### 6.4 Prompt 工程

| 优先级 | 建议 |
|--------|------|
| **中** | `judge_user_template_v2.1.txt` 要求"不要用 ```json``` 包裹"，但 `JudgeOutputParser` 第一策略恰恰是匹配 ````json```` 包裹 -- 二者矛盾。建议统一: 要么 Prompt 要求包裹、要么 Parser 不优先匹配包裹 |
| **低** | System Prompt 中 `judge_system_v2.1.txt` (272行完整版) 和 3 个拆分文件内容有重叠。建议明确哪个是 source-of-truth，避免维护两份 |

---

## 7. 与 Exec Agent 的接口契约

### 7.1 输入 (Judge 读取)

```
shared/execution_records/exec_xxx.json   -- ExecutionRecord v2.1 JSON
```

### 7.2 输出 (Judge 写入)

```
shared/audit_reports/{execution_id}_audit_report.md   -- Markdown 审计报告
shared/test_results/{execution_id}_result.json        -- TestResult JSON (待确认是否实现)
```

### 7.3 版本兼容

| 输入版本 | 处理方式 |
|----------|---------|
| v2.1 (有 `schema_version: "2.1"`) | 直接解析 |
| v1.0 (无 `schema_version`) | 自动迁移: 补 assertions / environment_recovery_actions / consolidated_audit_draft |
| 未知版本 | 尝试迁移，失败则报错 |

---

## 8. 安全性与鲁棒性评估

| 维度 | 评估 |
|------|------|
| **注入防护** | LLM 输出不直接执行，经过 JSON 解析 + Pydantic 验证 |
| **超时控制** | 通过 `provider.timeout` 和 `max_tokens` 控制 |
| **SSL 验证** | 可配置 `verify_ssl`，内网环境可关闭 |
| **代理隔离** | `trust_env=False` 避免系统代理干扰 |
| **资源释放** | ClientFactory 集中管理，调用方负责 `factory.close()` |
| **Fail-Safe** | 所有缺省值倾向 FAIL，符合"宁可错杀"原则 |

---

## 9. 待完成项清单

| # | 项目 | 优先级 | 说明 |
|---|------|--------|------|
| 1 | 修复 `self._client` 未初始化 bug | **高** | `judge_agent.py` `__init__` 缺少 `self._client = None` |
| 2 | 增加 PASS fixture + 验证 | **高** | 正面场景覆盖 |
| 3 | 增加多步骤复杂 fixture | **高** | 覆盖 3+ 步骤的真实用例 |
| 4 | Prompt 与 Parser 对齐 JSON 格式 | **中** | 统一是否要求 code block 包裹 |
| 5 | 利用 consolidated_audit_draft | **中** | 在 User Template 中参考 Exec 的审计草案 |
| 6 | 增加重试机制 | **中** | LLM 调用可配置 retry |
| 7 | 增加 assertions 非空的测试 | **中** | v2.1 核心特性覆盖 |
| 8 | 测试结果 JSON 写入 | **低** | 当前仅写 audit_report.md，TestResult JSON 写入待确认 |
| 9 | Prompt 维护策略明确化 | **低** | 确定完整版 vs 拆分版的 source-of-truth |

---

## 10. 结论

Judge Agent 的核心判断逻辑已完整实现，v2.1 Schema 升级（assertions / risk_notes / evidence_sufficient / consolidated_audit_draft）已落地，三层 Prompt 架构模块化良好，错误处理层次清晰。最紧迫的待办是修复 `self._client` 初始化遗漏和补充正面测试场景。整体判断引擎已具备进入集成联调的条件。
