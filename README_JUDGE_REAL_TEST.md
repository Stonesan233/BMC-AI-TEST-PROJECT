# Judge Agent 真实 LLM 测试指南

本文档说明如何配置和使用真实 LLM（Qwen3-235B-A22B）进行 Judge Agent 端到端测试。

## 1. 配置 LLM

### 方式一：环境变量（推荐）

```bash
# 阿里百炼 Qwen3
export QWEN3_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
export QWEN3_API_KEY=sk-xxx
export QWEN3_MODEL=qwen3-235b-a22b

# 或智谱 GLM-5
export GLM_API_KEY=your-glm-key
export GLM_MODEL=glm-5
```

### 方式二：config.yaml

编辑 `config/config.yaml`：

```yaml
providers:
  dashscope:
    base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: "${DASHSCOPE_API_KEY}"
    models:
      qwen3-235b-a22b:
        temperature: 0.0
        max_tokens: 8192

models:
  judge:
    provider: "dashscope"
    model: "qwen3-235b-a22b"
```

并在 `.env` 中设置：
```
DASHSCOPE_API_KEY=sk-xxx
```

## 2. 运行测试

### 运行所有真实 LLM 测试

```bash
pytest tests/test_judge_agent_real_llm.py -v
```

### 运行单个测试

```bash
# 严格判断测试（Vendor 大小写不匹配）
pytest tests/test_judge_agent_real_llm.py::test_real_qwen3_strict_verdict -v

# 明显失败测试
pytest tests/test_judge_agent_real_llm.py::test_real_qwen3_clear_fail -v

# v1 迁移测试
pytest tests/test_judge_agent_real_llm.py::test_real_qwen3_v1_migration -v

# 错误处理测试
pytest tests/test_judge_agent_real_llm.py::test_real_qwen3_error_handling -v
```

### 端到端测试

```bash
# 使用真实 Judge 进行完整流程
python -m src.main --cases testcases/your_case.yaml --judge
```

## 3. 验证检查清单

测试完成后，打开生成的审计报告检查：

### 3.1 严格判断验证

- [ ] **case_pass_strict.json**: Vendor 大小写不匹配 (Huawei vs HUAWEI) → **FAIL**
- [ ] 置信度 > 0.8
- [ ] false_pass_risk ∈ {medium, high}
- [ ] risk_notes 非空且有价值

### 3.2 报告结构验证

- [ ] 单个 `audit_report.md` 文件
- [ ] 包含 Judge 结论（PASS/FAIL）
- [ ] 包含 risk_notes
- [ ] 包含断言判断表格
- [ ] 包含证据摘要
- [ ] 包含改进建议

### 3.3 流水线验证

- [ ] Exec → Judge 流水线无断点
- [ ] v1 记录自动迁移到 v2.1
- [ ] 超时/错误正确处理
- [ ] --no-judge 模式正常工作

## 4. 预期测试结果

### case_pass_strict.json (Vendor 大小写不匹配)

```
overall_result: FAIL
confidence: 0.95
false_pass_risk: high
risk_notes:
  - Vendor 字段大小写不匹配（expected: Huawei, actual: HUAWEI）
  - 建议 Exec 阶段进行大小写不敏感比较
```

### case_fail_clear.json (HTTP 400 错误)

```
overall_result: FAIL
confidence: 0.98
step_results:
  - step_001: PASS
  - step_002: FAIL (HTTP 400)
  - step_003: FAIL (前置失败)
```

### case_v1_compatible.json (v1 迁移)

```
- 自动迁移到 schema_version=2.1
- consolidated_audit_draft 自动生成
- 判断结果正常
```

### case_error_timeout.json (超时)

```
overall_result: FAIL
confidence: 0.0
false_pass_risk: high
- Judge 正确处理超时场景
```

## 5. 常见问题

### Q: 测试被跳过

A: 检查环境变量或 config.yaml 是否配置了有效的 API Key：
```bash
echo $QWEN3_API_KEY
```

### Q: 报告中文乱码

A: 确保使用 UTF-8 编码读取报告：
```python
with open(path, encoding="utf-8") as f:
    content = f.read()
```

### Q: 模型响应超时

A: 增加 timeout 配置：
```bash
export QWEN3_TIMEOUT=300
```

或修改 config.yaml：
```yaml
providers:
  dashscope:
    timeout: 300
```

## 6. 相关文件

- `tests/test_judge_agent_real_llm.py` - 真实 LLM 集成测试
- `tests/test_judge_agent.py` - 单元测试
- `tests/fixtures/execution_records/` - 测试夹具
- `src/config/llm_config.py` - LLM 配置管理
- `src/agents/judge_agent.py` - Judge Agent 实现
