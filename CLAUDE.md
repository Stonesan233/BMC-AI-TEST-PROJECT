# openUBMC AI 测试框架 - 架构设计文档

> 本文档面向框架开发者，详细说明系统设计决策与实现细节。

---

## 1. 设计目标

### 1.1 核心痛点

> **「能按步骤执行用例，但无法可靠判断执行结果」**

- HTTP 200 但状态未变 → 假 PASS
- 模型宽松解读输出 → 假 PASS
- 预置条件未满足 → 无效测试

### 1.2 设计原则

1. **执行与判断彻底解耦**
2. **宁可错杀，不可放过**
3. **证据驱动，禁止推测**
4. **极简优先**

### 1.3 编码规范

1. **禁止在代码中使用 Emoji**
   - 原因：跨平台兼容性问题（Windows/Linux 终端显示不一致）
   - 替代方案：使用纯文本符号如 `[OK]`、`[FAIL]`、`[PASS]`、`-->` 等
   - 适用范围：所有 Python 代码、配置文件、报告生成

---

## 2. 极简双 Agent 架构

### 2.1 架构总览

```
┌──────────────────────────────────────────────────────────┐
│                启动脚本 (main.py)                        │
│                                                          │
│   职责：                                                  │
│   • 解析配置文件                                          │
│   • 读取测试用例                                          │
│   • 调用 Test_Exec Agent                                 │
│   • 读取 Execution Record                                │
│   • 调用 Test_Judge Agent                                │
│   • 输出最终结果                                          │
│                                                          │
│   特点：                                                  │
│   • 不是 Agent，是普通 Python 脚本                        │
│   • 无 Prompt，无 Tool Calling                           │
│   • 无状态，无复杂逻辑                                    │
└───────────────────────┬──────────────────────────────────┘
                        │
          ┌─────────────┴─────────────┐
          ▼                           ▼
┌─────────────────────┐              ┌─────────────────────┐
│     Test_Exec       │              │     Test_Judge      │
│    (执行 Agent)      │   共享目录   │    (判断 Agent)      │
│                     │   JSON文件   │                     │
│   • 理解用例        │  ─────────►  │   • 验证预置条件     │
│   • 调用 Tool       │              │   • 严格对比验证     │
│   • 收集证据        │              │   • 输出判断结果     │
│   • 生成 Record     │              │                     │
│                     │              │                     │
│   禁止：判断结果     │              │   禁止：执行操作     │
│                     │              │                     │
│   MiniMax-M2.5      │              │   Qwen3-235B-A22B   │
└─────────────────────┘              └─────────────────────┘
```

### 2.2 为什么启动脚本不是 Agent

**启动脚本的职责：**

```
✅ 解析配置文件
✅ 读取测试用例
✅ 调用 Test_Exec API
✅ 读取 Execution Record
✅ 调用 Test_Judge API
✅ 输出最终结果
❌ 不做任何智能决策
❌ 不使用 Prompt
❌ 不使用 Tool Calling
```

**设计理由：**

| 如果启动脚本是 Agent | 当前设计 |
|---------------------|---------|
| 需要 Prompt | 无需 Prompt |
| 需要模型推理 | 纯逻辑串联 |
| 增加调试复杂度 | 问题定位清晰 |
| 增加延迟 | 毫秒级响应 |

### 2.3 为什么采用双 Agent

| 方案 | 拒绝理由 |
|------|---------|
| 单 Agent 自我判断 | 执行与判断耦合 |
| 多 Subagent | 复杂度高 |
| 三 Agent | 过度设计 |

**双 Agent 优势：**

1. 独立判断：Judge 不接触被测系统
2. 专模专用：Exec 用 MiniMax，Judge 用 Qwen
3. 极简可靠：职责清晰，易于调试

---

## 3. openUBMC 技术背景

### 3.1 架构模型

| 模型 | 作用 |
|------|------|
| MDS | 微组件描述模型，管理生命周期、接口、依赖 |
| MDB Interface | 协作资源树模型，基于 D-Bus |
| Interface Adapter | 北向协议适配（Redfish/SNMP/CLI） |
| Device Interface | 南向设备规范 |

### 3.2 主要接口

| 接口 | 端口 | 说明 |
|------|------|------|
| Redfish | 443 | RESTful API |
| SSH (BMC) | 22 | BMC Shell |
| SSH (Host) | 2200 | 主机控制台 |
| IPMI | 623 | 传统 IPMI |

---

## 4. 组件详细设计

### 4.1 启动脚本（main.py）

**极轻量 Python 脚本，只做流程串联：**

1. 解析命令行参数
2. 读取配置文件
3. 读取测试用例
4. 调用 Test_Exec Agent API
5. 从共享目录读取 Execution Record
6. 调用 Test_Judge Agent API
7. 输出最终结果

**设计原则：无 Prompt、无 Tool Calling、无状态、无复杂逻辑**

### 4.2 Test_Exec（执行 Agent）

**职责边界：**

```
✅ 解析测试用例，理解执行意图
✅ 按步骤调用 Tool（Redfish/IPMI/SSH）
✅ 收集每一步的执行证据
✅ 生成 Execution Record
✅ 写入共享目录
❌ 不进行任何结果判断
❌ 不决定 PASS/FAIL
```

### 4.3 Test_Judge（判断 Agent）

**职责边界：**

```
✅ 接收 Execution Record（只读）
✅ 验证预置条件是否满足
✅ 对每一步进行 expected vs actual 对比
✅ 分析证据链完整性
✅ 输出 TestResult
❌ 不执行任何操作
❌ 不调用任何工具
```

### 4.4 RAG 模块（必须功能）

#### 为什么需要 RAG

实际测试用例输入质量普遍较差，经常只给出模糊的操作描述（如"使用 CLI 新增用户"、"修改用户权限"等），而不提供具体命令。

**实验表明：**
- 无 RAG 时，Test_Exec 执行率约为 10%
- 加入 RAG 后，执行率可提升至 90% 左右

因此 RAG 是本框架的**必须功能**。

#### RAG 的设计与集成

**位置**：作为 Test_Exec Agent 的专用 Tool（名称：`bmc_command_rag`）

**主要功能**：
- 根据步骤的自然语言描述，检索知识库中最相似的历史成功命令模板
- 支持 Redfish、CLI、IPMI 等多种接口
- 返回推荐命令、参数说明、注意事项和边界处理经验

**知识库内容**：
- 历史成功 Execution Record 中的命令片段
- openUBMC 常用操作命令模板库
- Redfish 端点与参数最佳实践
- 用户管理、环境恢复等边界场景处理经验

**调用时机**：
Test_Exec 在解析每一步骤时，如果命令不明确或需要生成具体指令，优先调用 `bmc_command_rag` Tool。

**配置开关**：
可在 config.yaml 中设置 `rag.enabled: true`（默认开启）

#### RAG Tool 定义

```python
class BMCCommandRAGTool(BaseTool):
    """BMC 命令检索工具"""

    @property
    def name(self) -> str:
        return "bmc_command_rag"

    @property
    def description(self) -> str:
        return """根据自然语言描述检索最匹配的 BMC 命令模板。

输入：操作描述（如"使用 CLI 新增用户"）
输出：
- recommended_command: 推荐命令
- interface_type: 接口类型（Redfish/CLI/IPMI）
- parameters: 参数说明
- notes: 注意事项
- boundary_handling: 边界处理经验
"""

    @property
    def parameters_schema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "operation_description": {
                    "type": "string",
                    "description": "操作的自然语言描述"
                },
                "interface_hint": {
                    "type": "string",
                    "enum": ["redfish", "cli", "ipmi", "any"],
                    "default": "any",
                    "description": "接口类型提示"
                },
                "top_k": {
                    "type": "integer",
                    "default": 3,
                    "description": "返回结果数量"
                }
            },
            "required": ["operation_description"]
        }
```

#### RAG 实现方案

**向量存储**：使用轻量级向量数据库（如 Chroma 或 FAISS）

**Embedding 模型**：可选用
- 本地模型：bge-small-zh（轻量，适合内网部署）
- 远程 API：如有可用的 Embedding 服务

**索引更新策略**：
- 每次成功执行后，自动将命令片段加入知识库
- 定期清理低质量模板
- 支持手动导入标准命令模板

---

## 5. 数据结构

### 5.1 ExecutionRecord

```python
class Evidence(BaseModel):
    evidence_id: str
    step_id: str
    evidence_type: str
    content: str
    metadata: Dict[str, Any] = {}
    captured_at: datetime

class StepRecord(BaseModel):
    step_id: str
    description: str
    tool: str
    endpoint: Optional[str] = None
    method: Optional[str] = None
    command: Optional[str] = None
    expected: Any
    actual: Optional[Any] = None
    evidence: List[Evidence] = []
    status: str  # "completed", "failed"
    http_status: Optional[int] = None
    error_message: Optional[str] = None
    started_at: datetime
    completed_at: datetime

class ExecutionRecord(BaseModel):
    execution_id: str
    case_id: str
    case_name: str
    environment: Dict[str, Any]
    prerequisites: List[Dict[str, Any]]
    steps: List[StepRecord]
    started_at: datetime
    completed_at: datetime
    overall_status: str
```

### 5.2 TestResult

```python
class StepJudgment(BaseModel):
    step_id: str
    result: str  # "PASS", "FAIL"
    confidence: float
    reason: str
    expected_match: bool
    concerns: List[str] = []

class TestResult(BaseModel):
    execution_id: str
    case_id: str
    case_name: str
    overall_result: str  # "PASS", "FAIL"
    confidence: float
    step_results: List[StepJudgment]
    prerequisite_check: Dict[str, Any]
    environment_recovery: Dict[str, Any]
    judge_notes: List[str]
```

---

## 6. 数据交接

### 6.1 共享目录结构

```
shared/
├── execution_records/
│   └── exec_xxx.json      ← Exec 写入，Judge 读取
├── test_results/
│   └── exec_xxx.json      ← Judge 写入
└── evidence/
    └── exec_xxx/
        ├── step_001.json
        └── step_002.json
```

### 6.2 为什么用文件

| 对比项 | 文件 | 消息队列 |
|-------|------|---------|
| 依赖 | 无 | Redis/RabbitMQ |
| 复杂度 | 低 | 中 |
| 可读性 | 直接查看 | 需工具 |

---

## 7. Prompt 三层设计

### 7.1 架构

```
┌─────────────────────────────────────────────────────────┐
│  Layer 1: System Prompt                                 │
│  ├── 角色定义                                           │
│  ├── 核心原则（宁可错杀）                                │
│  └── 能力边界                                           │
├─────────────────────────────────────────────────────────┤
│  Layer 2: Domain Prompt                                 │
│  ├── openUBMC 架构知识                                   │
│  ├── 严格判断规则                                       │
│  ├── 用户管理边界                                       │
│  └── 环境恢复要求                                       │
├─────────────────────────────────────────────────────────┤
│  Layer 3: Task Prompt                                   │
│  ├── 当前 Execution Record                              │
│  └── 具体判断任务                                       │
└─────────────────────────────────────────────────────────┘
```

### 7.2 Judge System Prompt（Layer 1 + Layer 2）

```text
# 角色定义
你是 openUBMC 测试结果的严格判断引擎。你的唯一职责是根据 Execution Record 判断测试是否真正通过。

# 核心行为准则

## 立即执行
- 收到任务后立即开始判断，不要询问、不要解释、不要确认
- 直接输出判断结果，不做任何额外说明

## 宁可错杀，不可放过
- 必须完整执行预置条件并严格验证结果，绝不在实际结果不符合预期时给出 PASS
- 任何不确定情况，优先判定为 FAIL
- 证据不足 → FAIL
- 状态不明确 → FAIL
- 响应格式异常 → FAIL

# 严格判断规则

## 预置条件验证
预置条件必须先执行并验证，否则直接 FAIL：
1. BMC 网络必须可达
2. 认证必须成功
3. 服务必须可用
4. 所有前置依赖必须满足

**重要**：预置条件验证失败时，必须立即 FAIL，不继续执行后续步骤。

## 步骤判断（每一步都必须严格验证）

### 判断顺序
1. 步骤未执行 → FAIL
2. HTTP 状态码非 2xx → FAIL
3. actual 为空或 null → FAIL
4. expected 与 actual 不一致 → FAIL
5. 证据缺失 → FAIL
6. 证据内容与 actual 不一致 → FAIL
7. 以上都不满足 → PASS

### 字段匹配规则
- 精确匹配：expected 与 actual 必须完全一致
- 部分匹配：expected 中的字段在 actual 中存在且值一致
- 忽略字段：@odata.context, @odata.etag 等元数据字段可忽略
- 类型严格："1" 与 1 视为不匹配

## 用户管理边界（关键规则）

### 添加用户前必须检查
1. 必须先查询当前用户列表
2. 统计用户数量（排除 ID 为 2 的 Administrator）
3. 如果用户数 ≥ 15，必须先清理测试用户
4. 如果用户列表已满且无法清理 → FAIL，并说明"用户列表已满"

### 用户 ID 规则
- ID 2 为 Administrator，不可删除、不可修改关键属性
- 测试用户 ID 应在允许范围内

## 环境恢复（测试结束后必须执行）

### 用户恢复
- 只保留 ID 2 的 Administrator 用户
- 删除所有测试过程中创建的用户
- 如果删除失败，必须在结果中标注"环境未完全恢复"

### 配置恢复
- 修改的密码必须恢复原值
- 修改的权限必须恢复原状态
- 修改的配置项必须恢复原设置

### 恢复验证
- 环境恢复后必须验证恢复结果
- 如果环境未恢复，必须标注警告

# openUBMC 领域知识

## Redfish 响应判断
- HTTP 2xx 表示请求成功，但不代表业务成功
- 成功操作返回 {"@MessageId": "Base.1.0.Success"} 或类似消息
- 需要检查实际资源状态是否改变
- 错误响应包含 "error" 字段

## 电源状态
- PowerState 有效值：On, Off, PoweringOn, PoweringOff
- 开机命令后需验证 PowerState 确实变为 On
- 关机命令后需验证 PowerState 确实变为 Off

## 传感器数据
- Status.Health 应为 OK 或 Warning
- Reading 值应在正常范围内
- Status.State 应为 Enabled

# 输出格式

只输出一个标准 JSON，不要有任何其他内容：

{
  "execution_id": "<execution_id>",
  "case_id": "<case_id>",
  "case_name": "<case_name>",
  "overall_result": "PASS 或 FAIL",
  "confidence": <0.0-1.0>,
  "step_results": [
    {
      "step_id": "<step_id>",
      "result": "PASS 或 FAIL",
      "confidence": <0.0-1.0>,
      "reason": "<判断理由，中文>",
      "expected_match": <true 或 false>,
      "concerns": ["<关注点>"]
    }
  ],
  "prerequisite_check": {
    "result": "PASS 或 FAIL",
    "failed_items": ["<失败的预置条件>"]
  },
  "environment_recovery": {
    "recovered": <true 或 false>,
    "warnings": ["<未恢复项>"]
  },
  "judge_notes": ["<判断说明>"]
}

# 最终强调
1. 立即执行测试，不要询问或解释任何内容
2. 宁可错杀，不可放过
3. 必须完整执行预置条件并严格验证结果
4. 绝不在实际结果不符合预期时给出 PASS
5. 用户管理边界严格遵守
6. 环境必须恢复
7. 只输出一个 JSON
```

### 7.3 Task Prompt（Layer 3）

```text
请根据以下 Execution Record 进行判断：

{execution_record_json}
```

### 7.4 Exec System Prompt

```text
# 角色定义
你是 openUBMC 自动化测试的执行引擎。你的职责是：
1. 准确理解测试用例的执行意图
2. 按步骤调用相应的 BMC 接口
3. 忠实记录每一步的执行结果和证据
4. 生成 Execution Record

# 核心原则
- 只执行，不判断：你的工作是把用例跑完，收集证据，判断交给 Judge
- 忠实记录：无论结果是否符合预期，都要完整记录
- 完整证据：每一步都必须有证据

# openUBMC 接口
- Redfish：主要管理 API，HTTPS 端口 443
- IPMI：传统管理接口，UDP 端口 623
- SSH：BMC Shell 端口 22，主机控制台端口 2200

# 可用工具
{tools_description}

# 当前任务
{current_case}
```

---

## 8. 假 PASS 防控

| 层级 | 措施 |
|------|------|
| 执行层 | 强制证据收集、原始响应保留 |
| 数据层 | JSON 文件、完整性检查 |
| 判断层 | 严格 Prompt、宁可错杀 |
| 用户边界 | 用户数检查、环境恢复验证 |

| 场景 | 对策 |
|------|------|
| HTTP 200 但状态未变 | 检查 actual 字段 |
| 返回成功但操作失败 | 再次查询验证 |
| 用户列表已满 | FAIL 并说明原因 |
| 环境未恢复 | 标注警告 |

---

## 9. 部署

### 9.1 硬件拓扑

```
执行服务器 (192.168.1.100)     判断服务器 (192.168.1.101)
┌─────────────────────┐       ┌─────────────────────┐
│  昇腾 910C × 16卡    │       │  昇腾 910C × 16卡    │
│  MiniMax-M2.5       │       │  Qwen3-235B-A22B    │
└──────────┬──────────┘       └──────────┬──────────┘
           │                             │
           └───────── NFS/共享目录 ───────┘
                         │
                ┌────────┴────────┐
                │  openUBMC 设备   │
                └─────────────────┘
```

### 9.2 模型配置

| Agent | 模型 | 服务器 |
|-------|------|--------|
| Test_Exec | MiniMax-M2.5 | 192.168.1.100 |
| Test_Judge | Qwen3-235B-A22B | 192.168.1.101 |

---

## 10. 目录结构

```
openubmc-ai-test/
├── README.md
├── CLAUDE.md
├── main.py                    # 启动脚本（不是 Agent）
├── config/
│   └── config.yaml
├── src/
│   ├── exec_agent.py
│   ├── judge_agent.py
│   ├── tools/
│   │   ├── base.py
│   │   ├── redfish.py
│   │   ├── ipmi.py
│   │   └── ssh.py
│   └── prompts/
│       ├── exec_system.txt
│       └── judge_system.txt
├── testcases/
└── shared/
    ├── execution_records/
    ├── test_results/
    └── evidence/
```

---

## 11. 参考资料

- [openUBMC 架构简介](https://www.openubmc.cn/docs/zh/development/design_reference/architecture.html)
- [Redfish 规范 (DMTF)](https://www.dmtf.org/standards/redfish)

---

## 附录：术语表

| 术语 | 定义 |
|------|------|
| openUBMC | 基于微组件架构的 BMC 管理软件 |
| MDS | 微组件描述模型 |
| MDB | 协作资源树模型 |
| 启动脚本 | 极轻量 Python 脚本，只做流程串联，不是 Agent |
| Exec Agent | 执行 Agent |
| Judge Agent | 判断 Agent |
| ExecutionRecord | 执行记录 |
| TestResult | 判断结果 |
