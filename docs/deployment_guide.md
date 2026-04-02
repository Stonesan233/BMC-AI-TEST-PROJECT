# openUBMC AI 测试框架 - 内网私有化部署指南

---

## 1. 文档概述

本文档指导内网团队在私有 OpenAI-compatible 服务上完成 openUBMC AI 测试框架的私有化部署。

**框架架构**：启动脚本 `main.py` 串联双 Agent 流程——`Test_Exec`（执行 Agent）调用 BMC 接口收集证据，`Test_Judge`（判断 Agent）严格验证结果。支持 Redfish、IPMI、SSH 三种接口，内置 RAG 命令检索提升模糊用例执行率。

**适用场景**：

- 内网 Lingshu / vLLM / Ollama 等 OpenAI 兼容服务
- 支持从 Excel 批量导入测试用例
- 支持大规模用例回归验证

---

## 2. 环境准备

### 2.1 系统要求

| 项目 | 要求 |
|------|------|
| 操作系统 | Windows 10/11、Linux |
| Python | >= 3.10 |
| 网络 | 能访问内网 LLM 服务和目标 BMC |
| 磁盘 | >= 2 GB（含 RAG 索引和日志） |

### 2.2 获取代码

```bash
git clone https://your-git-server/your-org/BMC-AI-TEST-PROJECT.git
cd BMC-AI-TEST-PROJECT
```

### 2.3 安装依赖

```bash
pip install openai>=1.0 httpx pyyaml jinja2 pydantic>=2.0 pandas openpyxl chromadb
```

验证安装：

```bash
python -c "import openai, httpx, yaml, jinja2, pydantic, pandas, openpyxl, chromadb; print('[OK] 依赖安装完成')"
```

---

## 3. 目录结构说明

```
BMC-AI-TEST-PROJECT/
├── main.py                      # 启动脚本（流程串联）
├── config/
│   ├── config_example.yaml      # 配置模板（已提交 Git）
│   └── config.yaml              # 实际配置（不提交 Git）
├── .env                         # API 密钥（不提交 Git）
├── src/
│   ├── agents/
│   │   └── exec_agent.py        # Test_Exec Agent
│   ├── core/
│   │   ├── config.py            # 配置加载（Pydantic）
│   │   ├── client_factory.py    # OpenAI 客户端工厂
│   │   └── schemas.py           # 数据模型定义
│   ├── tools/
│   │   ├── redfish.py           # Redfish 工具
│   │   ├── ipmi_tool.py         # IPMI 工具
│   │   └── ssh_tool.py          # SSH 工具
│   ├── rag/                     # RAG 命令检索模块
│   └── utils/
│       └── file_handler.py      # 文件处理 + Excel 转换
├── testcases/                   # YAML 测试用例目录
├── shared/                      # 运行时共享数据
│   ├── execution_records/       # 执行记录
│   ├── test_results/            # 判断结果
│   ├── reports/                 # Markdown 报告
│   ├── evidence/                # 执行证据
│   ├── excel_cases/             # Excel 转换后的 YAML
│   └── rag_index/               # RAG 向量索引
└── logs/                        # 日志文件
```

---

## 4. 配置说明

### 4.1 创建配置文件

```bash
cp config/config_example.yaml config/config.yaml
```

### 4.2 完整配置示例

以下是可直接复制使用的 `config/config.yaml` 完整模板：

```yaml
# ======================================================================
# openUBMC AI 测试框架 - 配置文件模板
#
# 使用方法:
#   cp config/config_example.yaml config/config.yaml
#   然后修改 config.yaml 填入实际的服务地址和 API Key
#
# 设计原则:
#   1. 所有 LLM / Embedding 服务统一为 providers，支持任意 OpenAI-compatible API
#   2. 每个组件（Exec / Judge / Embedding / Rewrite）独立选择 provider + model
#   3. API Key 支持 ${ENV_VAR} 引用或直接填写
#   4. 内网私有部署友好，运维人员只需修改 providers 和 models 即可
# ======================================================================

# ----- 1. 服务提供者（可定义多个，按需引用） -----
#
# 每个 provider 包含:
#   base_url  - OpenAI 兼容 API 地址（必填）
#   api_key   - API 密钥，支持 ${ENV_VAR} 引用或直接填写（必填）
#   timeout   - HTTP 超时秒数（默认 120）
#   models    - 该 provider 下可用的模型列表及参数（必填）
#
# models 下的每个模型包含:
#   temperature  - 采样温度，0.0~1.0（默认 0.1）
#   max_tokens   - 最大输出 token 数（默认 4096）
#   dimension    - 向量维度，仅 Embedding 模型需要
#
providers:
  # ---- 默认服务（占位示例，部署时替换为实际地址） ----
  default:
    base_url: "https://your-openai-service/api/v1/"
    api_key: "${YOUR_API_KEY}"             # 推荐: 环境变量引用
    timeout: 120.0                         # HTTP 超时（秒）
    models:
      MiniMax-M2.5:                        # Exec Agent 使用
        temperature: 0.1
        max_tokens: 8192
      glm-5:                               # Judge Agent 使用
        temperature: 0.1
        max_tokens: 8192
      text-embedding-v4:                   # Embedding 使用
        dimension: 1024                    # 向量维度（必须）
      qwen3.5-plus:                        # Query Rewriter 使用
        temperature: 0.3
        max_tokens: 256

  # ---- 第二个服务（可选，如需 Exec / Judge 分用不同后端） ----
  # secondary:
  #   base_url: "https://another-service/api/v1/"
  #   api_key: "${ANOTHER_API_KEY}"
  #   timeout: 120.0
  #   models:
  #     some-model:
  #       temperature: 0.1
  #       max_tokens: 4096

# ----- 2. 组件模型分配（每个组件指定使用哪个 provider 的哪个 model） -----
#
# 只需填写两项:
#   provider  - 引用上面 providers 中的名称
#   model     - 引用该 provider.models 中的模型名称
#
models:
  # Test_Exec Agent（测试执行引擎）
  exec:
    provider: "default"
    model: "MiniMax-M2.5"

  # Test_Judge Agent（测试判断引擎）
  judge:
    provider: "default"
    model: "glm-5"

  # Embedding（向量检索用）
  embedding:
    provider: "default"
    model: "text-embedding-v4"

  # Query Rewriter（查询扩展）
  rewrite:
    provider: "default"
    model: "qwen3.5-plus"

# ----- 3. 运行参数 -----
agent:
  exec_batch_size: 1                       # Exec Agent 批量大小 (1~3)
  judge_batch_size: 1                      # Judge Agent 批量大小 (固定为 1)

# ----- 4. RAG 检索模块 -----
rag:
  enabled: true                            # 全局开关: 是否启用 RAG 命令检索
  chroma_path: "./shared/rag_index"        # Chroma 持久化目录
  collection_name: "openubmc_rag"          # Collection 名称
  alpha: 0.7                               # 向量检索权重 (0.0~1.0), BM25 权重 = 1 - alpha
  top_k: 3                                 # 返回结果数量 (建议 3~5)
  default_interface: "auto"                # 默认接口类型: auto / ipmi / redfish / both
  auto_detect: true                        # auto 模式下是否启用关键词自动检测
  enable_rewrite: true                     # 是否启用查询改写
  rewrite_weight: 2.0                      # 原始查询在 RRF 融合中的权重倍数
  rewrite_top_k: 5                         # Query Rewriter 生成的扩展查询条数
  rerank_enabled: false                    # 是否启用 Rerank 二次排序

# ----- 5. 目标 BMC 配置 -----
target:
  bmc_host: "192.168.1.100"               # Redfish / SSH 地址
  bmc_port: 443                           # Redfish HTTPS 端口
  ipmi_host: "192.168.1.100"              # IPMI 地址
  ipmi_port: 623                          # IPMI UDP 端口
  ssh_host: "192.168.1.100"               # SSH 地址
  ssh_port: 22                            # SSH 端口
  bmc_user: "Administrator"               # BMC 登录用户名
  bmc_password: "your_bmc_password"       # BMC 登录密码
  # os_host: "192.168.1.101"              # 可选: 主机控制台地址
  # os_user: "root"                       # 可选: 主机控制台用户
  # os_password: "your_os_password"       # 可选: 主机控制台密码

# ----- 6. 存储配置 -----
storage:
  shared_dir: "./shared"                  # 共享数据目录

# ----- 7. 日志配置 -----
logging:
  level: "INFO"                            # 日志级别: DEBUG / INFO / WARNING / ERROR
  file: "./logs/test_framework.log"        # 日志文件路径
```

### 4.3 内网私有化部署配置要点

**单服务场景**（如 Lingshu 平台提供所有模型）：

```yaml
providers:
  lingshu:
    base_url: "https://your-lingshu-server/api/v1/"
    api_key: "${LINGSHU_API_KEY}"
    timeout: 120.0
    models:
      your-exec-model:
        temperature: 0.1
        max_tokens: 8192
      your-judge-model:
        temperature: 0.1
        max_tokens: 8192
      your-embedding-model:
        dimension: 1024
      your-rewrite-model:
        temperature: 0.3
        max_tokens: 256

models:
  exec:
    provider: "lingshu"
    model: "your-exec-model"
  judge:
    provider: "lingshu"
    model: "your-judge-model"
  embedding:
    provider: "lingshu"
    model: "your-embedding-model"
  rewrite:
    provider: "lingshu"
    model: "your-rewrite-model"
```

**多服务场景**（Exec 和 Judge 部署在不同服务器）：

```yaml
providers:
  exec-server:
    base_url: "https://192.168.1.100:8000/v1/"
    api_key: "${EXEC_API_KEY}"
    timeout: 120.0
    models:
      your-exec-model:
        temperature: 0.1
        max_tokens: 8192

  judge-server:
    base_url: "https://192.168.1.101:8000/v1/"
    api_key: "${JUDGE_API_KEY}"
    timeout: 120.0
    models:
      your-judge-model:
        temperature: 0.1
        max_tokens: 8192

  embedding-server:
    base_url: "https://192.168.1.102:8000/v1/"
    api_key: "${EMBEDDING_API_KEY}"
    timeout: 60.0
    models:
      your-embedding-model:
        dimension: 1024

models:
  exec:
    provider: "exec-server"
    model: "your-exec-model"
  judge:
    provider: "judge-server"
    model: "your-judge-model"
  embedding:
    provider: "embedding-server"
    model: "your-embedding-model"
  rewrite:
    provider: "exec-server"
    model: "your-exec-model"
```

---

## 5. .env 文件配置

在项目根目录创建 `.env` 文件，存放 API 密钥：

```bash
# .env 文件 - API 密钥配置（此文件不会被提交到 Git）
#
# 格式: KEY=VALUE
# 在 config.yaml 中通过 ${KEY} 引用

# 单服务场景
YOUR_API_KEY=sk-your-key-here

# 多服务场景
# EXEC_API_KEY=sk-your-exec-key
# JUDGE_API_KEY=sk-your-judge-key
# EMBEDDING_API_KEY=sk-your-embedding-key
```

**安全注意事项**：

- `.env` 文件已在 `.gitignore` 中排除，不会被提交
- 也可在 `config.yaml` 中直接填写 `api_key`，但推荐使用环境变量
- 生产环境建议使用系统级环境变量而非 `.env` 文件

---

## 6. 启动框架

### 6.1 验证配置

```bash
# 检查配置文件语法
python -c "import yaml; yaml.safe_load(open('config/config.yaml')); print('[OK] 配置文件语法正确')"
```

### 6.2 构建 RAG 索引（首次部署）

```bash
# 将 openUBMC 文档放入 shared/rag_docs/ 目录后执行
python src/rag/build_index.py
```

### 6.3 运行单个用例验证

```bash
python main.py --cases testcases/get_redfish_root.yaml
```

### 6.4 运行多个用例

```bash
python main.py --cases testcases/get_redfish_root.yaml testcases/ipmi_mc_info.yaml
```

### 6.5 查看帮助

```bash
python main.py --help
```

输出：

```
usage: main.py [-h] [--config CONFIG] [--cases CASES [CASES ...]]
               [--excel EXCEL [EXCEL ...]]

openUBMC AI 测试框架

options:
  -h, --help            show this help message
  --config CONFIG, -c CONFIG
                        配置文件路径 (默认: config/config.yaml)
  --cases CASES [CASES ...]
                        YAML 测试用例路径（支持多个文件）
  --excel EXCEL [EXCEL ...]
                        Excel 用例路径（.xlsx 文件或目录，自动转换为 YAML）
```

---

## 7. 使用 Excel 用例

框架支持从 Excel 文件直接导入测试用例，自动转换为 YAML 格式。

### 7.1 Excel 表头要求

Excel 必须包含以下 5 个必填列（列名支持多种变体，自动识别）：

| 标准字段名 | 接受的列名变体 | 说明 |
|-----------|--------------|------|
| 编号 | `编号`、`用例编号`、`用例_编号`、`case_id` | 用例唯一标识 |
| 名称 | `名称`、`用例名称`、`用例_名称`、`case_name` | 用例标题 |
| 预置条件 | `预置条件`、`前置条件`、`前提条件` | 多行文本 |
| 测试步骤 | `测试步骤`、`步骤`、`操作步骤` | 多行文本 |
| 预期结果 | `预期结果`、`期望结果` | 多行文本，支持 `A) B) C)` 编号 |

可选列：

| 字段名 | 接受的列名变体 | 说明 |
|--------|--------------|------|
| 测试类型 | `测试类型`、`类型` | 默认 `功能测试` |
| 优先级 | `优先级`、`级别` | 默认 `P1` |
| 备注 | `notes`、`备注`、`说明` | 附加说明 |

### 7.2 运行方式

**单个 Excel 文件**：

```bash
python main.py --excel testcases.xlsx
```

**Excel 目录**（自动遍历目录下所有 `.xlsx` 文件）：

```bash
python main.py --excel excel_dir/
```

**混合使用 YAML 和 Excel**：

```bash
python main.py --cases testcases/ipmi_mc_info.yaml --excel testcases.xlsx
```

### 7.3 Excel 转换后的 YAML 格式

Excel 每行生成一个 YAML 文件，`预置条件`、`测试步骤`、`预期结果` 使用 `|` 块标量保留原始自然文本：

```yaml
用例_编号: REDFISH_Userinfolist_002
用例_名称: 查询账户信息URL大小写不敏感测试
测试类型: 功能测试
优先级: P1
预置条件: |
  1. BMC正常运行
  2. 管理员用户可正常创建会话
测试步骤: |
  1. 管理员用户创建会话信息，请求如下：
     URI信息: https://172.100.20.25/redfish/v1/SessionService/Sessions/
     Header信息: Content-Type application/json
     Body信息: { "UserName": "root", "Password":"Huawei12#$" }
     请求方法：POST
  2. 发送请求，查看响应报文，有结果 A）
  3. 修改URI中的路径为全大写，发送请求，有结果 B）
  4. 修改URI中的路径为混合大小写，发送请求，有结果 C）
预期结果: |
  A）请求发送成功，响应报文符合规范，响应码为201
  B）请求发送成功，响应报文符合规范，响应码为404
  C）响应体内容符合预期
notes: 大小写敏感性测试，重点验证 Redfish URL 处理
```

转换后的 YAML 文件保存在 `shared/excel_cases/` 目录。

---

## 8. 运行测试

### 8.1 基本运行

```bash
# 运行单个 YAML 用例
python main.py --cases testcases/get_redfish_root.yaml

# 运行多个 YAML 用例
python main.py --cases testcases/get_redfish_root.yaml testcases/ipmi_mc_info.yaml

# 从 Excel 运行
python main.py --excel testcases.xlsx

# 指定配置文件
python main.py -c config/config.yaml --cases testcases/sample_user_management.yaml
```

### 8.2 输出说明

运行完成后，输出文件保存在 `shared/` 目录：

```
shared/
├── execution_records/     # 执行记录（JSON）
│   └── exec_xxx.json
├── test_results/          # 判断结果（JSON）
│   └── exec_xxx.json
├── reports/               # 人可读 Markdown 报告
│   └── exec_xxx.md
├── evidence/              # 原始执行证据
│   └── exec_xxx/
│       ├── step_001.txt
│       └── step_002.txt
└── excel_cases/           # Excel 转换后的 YAML（仅 --excel 时）
    ├── TC-001.yaml
    └── TC-002.yaml
```

### 8.3 查看报告

```bash
# 查看最新报告
cat shared/reports/$(ls -t shared/reports/ | head -1)
```

---

## 9. 大规模验证建议

### 9.1 用例组织

推荐按模块分目录管理 Excel 用例：

```
testcases_excel/
├── redfish/
│   ├── user_management.xlsx
│   ├── system_info.xlsx
│   └── power_control.xlsx
├── ipmi/
│   ├── basic_commands.xlsx
│   └── sensor_reading.xlsx
└── cli/
    ├── user_ops.xlsx
    └── config_ops.xlsx
```

### 9.2 批量运行

```bash
# 按目录批量运行
python main.py --excel testcases_excel/redfish/
python main.py --excel testcases_excel/ipmi/
python main.py --excel testcases_excel/cli/

# 全量运行
python main.py --excel testcases_excel/
```

### 9.3 调优参数

在大规模场景下，建议调整以下参数：

**config.yaml**：

```yaml
agent:
  exec_batch_size: 2          # 提高到 2~3 可加速，但注意 LLM 并发压力

rag:
  top_k: 5                    # 增大检索范围，提升复杂用例执行率
  enable_rewrite: true         # 保持开启，提升模糊用例匹配率
```

### 9.4 RAG 知识库维护

```bash
# 定期重建索引（加入新文档后）
python src/rag/build_index.py

# 索引位置
ls shared/rag_index/
```

---

## 10. 常见问题排查

### 10.1 配置相关

| 错误信息 | 原因 | 解决方法 |
|---------|------|---------|
| `配置文件不存在` | 未创建 `config.yaml` | `cp config/config_example.yaml config/config.yaml` |
| `Exec Agent 初始化失败` | API 连接失败 | 检查 `base_url` 和 `api_key` 是否正确 |
| `Provider 'xxx' not found` | models 中引用了不存在的 provider | 确认 providers 和 models 名称一致 |

### 10.2 网络相关

| 错误信息 | 原因 | 解决方法 |
|---------|------|---------|
| `Connection refused` | LLM 服务未启动 | 检查 LLM 服务状态和端口 |
| `SSL: CERTIFICATE_VERIFY_FAILED` | 内网自签证书 | 框架已内置 SSL 校验关闭，确认 `base_url` 使用 `https://` |
| `Timeout` | 网络延迟或模型推理慢 | 增大 `timeout` 值（如 `300.0`） |

### 10.3 Excel 转换相关

| 错误信息 | 原因 | 解决方法 |
|---------|------|---------|
| `缺少必填列 [...]` | Excel 缺少必填列 | 检查表头是否包含：编号、名称、预置条件、测试步骤、预期结果 |
| `未生成任何 YAML 文件` | Excel 为空或格式错误 | 检查文件是否为 `.xlsx` 格式，数据行是否为空 |
| `Sheet 'xxx' 为空，跳过` | Sheet 无有效数据行 | 检查是否全为空行 |

### 10.4 BMC 连接相关

| 错误信息 | 原因 | 解决方法 |
|---------|------|---------|
| `BMC 网络不可达` | 网络不通 | `ping <bmc_host>` 验证 |
| `Authentication failed` | 密码错误 | 检查 `target.bmc_password` 配置 |
| `Connection refused` (SSH) | SSH 端口不对 | 检查 `target.ssh_port`，默认 22 |

### 10.5 查看详细日志

```bash
# 实时查看日志
tail -f logs/test_framework.log

# 设置 DEBUG 级别（config.yaml）
logging:
  level: "DEBUG"
```

---

## 11. 版本信息

| 项目 | 版本 |
|------|------|
| 框架版本 | 最新（参考 Git 最新提交） |
| Python | >= 3.10 |
| openai | >= 1.0 |
| pydantic | >= 2.0 |
| pandas | >= 2.0 |
| chromadb | >= 0.4 |

**关键依赖**：

```
openai>=1.0
httpx
pyyaml
jinja2
pydantic>=2.0
pandas
openpyxl
chromadb
```

**联系方式**：如有部署问题，请在内部 Git 仓库提交 Issue。
