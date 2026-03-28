# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - Test_Exec Agent

职责：理解测试用例 -> 调用 BMC 接口执行 -> 收集证据 -> 生成 ExecutionRecord。
严格遵守 "只执行，不判断" 原则。

技术方案：
- AsyncOpenAI + stream=True，支持 OpenAI-compatible API
- 多轮 Tool Calling 循环（redfish / ipmi / ssh / rag）
- Jinja2 渲染 system prompt（src/prompts/exec_system.txt）
- 执行完成后自动保存 ExecutionRecord（file_handler）

配置格式：

    agents:
      exec:
        base_url: "https://open.bigmodel.cn/api/paas/v4"
        api_key: "xxx.xxx"
        model: "glm-5"
"""

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from jinja2 import Template
from openai import AsyncOpenAI
from pydantic import ValidationError

from src.core.schemas import ExecutionRecord, StepRecord, StepStatus
from src.utils.file_handler import save_execution_record


# ======================================================================
# 模块级工具
# ======================================================================

def extract_json_from_response(text: str) -> Optional[str]:
    """
    从 LLM 输出中提取 JSON。

    策略：
    1. ```json ... ``` 代码块
    2. 最外层 { } 配对
    """
    match = re.search(r"```json\s*\n?(.*?)\n?\s*```", text, re.DOTALL)
    if match:
        return match.group(1).strip()

    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        return text[start : end + 1]

    return None


# ======================================================================
# OpenAI function calling 工具定义
# ======================================================================

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "redfish_request",
            "description": "发送 Redfish API 请求到 BMC。用于查询或修改 BMC 资源。",
            "parameters": {
                "type": "object",
                "properties": {
                    "endpoint": {
                        "type": "string",
                        "description": "Redfish 端点路径，如 /redfish/v1/AccountService/Accounts",
                    },
                    "method": {
                        "type": "string",
                        "enum": ["GET", "POST", "PATCH", "DELETE"],
                        "description": "HTTP 方法",
                    },
                    "body": {
                        "type": "object",
                        "description": "请求体（POST/PATCH 时使用，可选）",
                    },
                },
                "required": ["endpoint", "method"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ipmi_command",
            "description": "执行 IPMI 命令。用于传统 BMC 管理操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "IPMI 命令，如 'chassis status'",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 30",
                        "default": 30,
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "ssh_exec",
            "description": "通过 SSH 在远程主机上执行命令。用于 BMC Shell 或主机控制台操作。",
            "parameters": {
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "目标主机 IP"},
                    "port": {
                        "type": "integer",
                        "description": "SSH 端口（BMC Shell 默认 22，主机控制台默认 2200）",
                        "default": 22,
                    },
                    "user": {"type": "string", "description": "用户名"},
                    "password": {"type": "string", "description": "密码"},
                    "command": {"type": "string", "description": "要执行的命令"},
                    "timeout": {
                        "type": "integer",
                        "description": "超时时间（秒），默认 30",
                        "default": 30,
                    },
                },
                "required": ["host", "user", "password", "command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "bmc_command_rag",
            "description": (
                "根据自然语言描述检索最匹配的 BMC 命令模板。"
                "当步骤描述模糊、缺少具体命令或参数时，必须优先调用此工具。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "operation_description": {
                        "type": "string",
                        "description": "操作的自然语言描述，如 '使用 CLI 新增用户'",
                    },
                    "interface_hint": {
                        "type": "string",
                        "enum": ["redfish", "cli", "ipmi", "any"],
                        "default": "any",
                        "description": "接口类型提示",
                    },
                    "top_k": {
                        "type": "integer",
                        "default": 3,
                        "description": "返回结果数量",
                    },
                },
                "required": ["operation_description"],
            },
        },
    },
]


# ======================================================================
# ExecAgent
# ======================================================================

class ExecAgent:
    """
    Test_Exec Agent - 测试执行引擎

    通过 OpenAI-compatible API 与 LLM 交互，使用 Tool Calling 执行 BMC 操作。
    支持流式输出，实时展示执行过程。
    """

    SYSTEM_PROMPT_PATH = Path("src/prompts/exec_system.txt")
    MAX_TOOL_ROUNDS = 20

    def __init__(self, config: dict):
        exec_cfg = config["agents"]["exec"]

        self.client = AsyncOpenAI(
            base_url=exec_cfg["base_url"],
            api_key=exec_cfg["api_key"],
        )
        self.model = exec_cfg["model"]
        self.config = config
        self.shared_dir = config.get("storage", {}).get("shared_dir", "./shared")

        # 预加载 system prompt 模板
        self._system_template = Template(
            self.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        )

        # Tool 分发表
        self._tool_handlers = {
            "redfish_request": self._tool_redfish_request,
            "ipmi_command": self._tool_ipmi_command,
            "ssh_exec": self._tool_ssh_exec,
            "bmc_command_rag": self._tool_bmc_command_rag,
        }

        print(f"[Exec] Agent 初始化完成 (model={self.model})")

    # ==================================================================
    # 公开接口
    # ==================================================================

    async def execute(self, case: dict, config: dict) -> ExecutionRecord:
        """
        执行单个测试用例。

        流程：渲染 prompt -> LLM 对话（含 Tool Calling）-> 解析 ExecutionRecord -> 保存
        """
        case_name = case.get("name", case.get("用例_名称", "unknown"))
        print(f"\n[Exec] 开始执行: {case_name}")

        started_at = datetime.now()

        # 构建消息
        messages = [
            {"role": "system", "content": self._render_system_prompt(case)},
            {"role": "user", "content": self._build_user_message(case)},
        ]

        # LLM 对话
        try:
            final_content = await self._run_conversation(messages)
        except Exception as e:
            print(f"[Exec] 执行异常: {e}")
            record = self._build_failure_record(case, started_at, error=str(e))
            self._save_record(record)
            return record

        # 解析 ExecutionRecord
        record = self._parse_record(final_content, case, started_at)

        # 自动保存
        self._save_record(record)

        print(f"[Exec] 执行完成: {case_name} -> {record.overall_status}")
        return record

    async def execute_batch(self, cases: list, config: dict) -> list:
        """批量执行测试用例（串行）。"""
        print(f"\n[Exec] 批量执行 {len(cases)} 个用例")

        records = []
        for i, case in enumerate(cases):
            print(f"\n[Exec] --- 用例 {i + 1}/{len(cases)} ---")
            try:
                records.append(await self.execute(case, config))
            except Exception as e:
                print(f"[Exec] 用例执行失败: {e}")
                records.append(
                    self._build_failure_record(case, datetime.now(), error=str(e))
                )
        return records

    # ==================================================================
    # Prompt
    # ==================================================================

    def _render_system_prompt(self, case: dict) -> str:
        """Jinja2 渲染 system prompt，注入环境 + 用例信息。"""
        target = self.config.get("target", {})
        return self._system_template.render(
            bmc_host=target.get("bmc_host", "unknown"),
            bmc_user=target.get("bmc_user", "unknown"),
            os_host=target.get("os_host"),
            os_user=target.get("os_user"),
            case=case,
            case_id=case.get("case_id", case.get("用例_编号", "")),
            case_name=case.get("name", case.get("用例_名称", "")),
            test_steps=case.get("测试步骤", []),
            expected_result=case.get("预期结果", []),
            precondition=case.get("预置条件", []),
            batch_cases=None,
        )

    def _build_user_message(self, case: dict) -> str:
        """构建 user message，移除内部字段后格式化为 JSON。"""
        case_name = case.get("name", case.get("用例_名称", "unknown"))
        case_copy = {k: v for k, v in case.items() if not k.startswith("_")}
        case_json = json.dumps(case_copy, ensure_ascii=False, indent=2, default=str)
        return (
            f"请执行以下测试用例：\n\n"
            f"用例名称: {case_name}\n\n"
            f"用例内容:\n{case_json}\n\n"
            f"执行完成后，请输出完整的 ExecutionRecord JSON。"
        )

    # ==================================================================
    # LLM 对话循环
    # ==================================================================

    async def _run_conversation(self, messages: list) -> str:
        """
        LLM 对话主循环。

        每轮：流式接收 -> 如有 tool_calls 则执行 -> 继续
        无 tool_calls 时返回最终文本。
        """
        for round_num in range(self.MAX_TOOL_ROUNDS):
            print(f"\n[Exec] --- 第 {round_num + 1} 轮 ---")

            text, tool_calls, finish_reason = await self._stream_response(messages)

            # 无 tool call -> 返回
            if finish_reason != "tool_calls" or not tool_calls:
                return text

            # 执行 tool calls 并注入结果
            await self._process_tool_calls(messages, text, tool_calls)

        print("[Exec] 达到最大对话轮次限制")
        return text

    async def _stream_response(self, messages: list) -> tuple:
        """
        流式接收一轮 LLM 响应。

        实时打印文本内容（[Exec] 前缀），累积 tool call 分片。

        Returns:
            (text_content, tool_calls_map, finish_reason)
        """
        stream = await self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            tools=TOOL_DEFINITIONS,
            stream=True,
        )

        text = ""
        tool_calls: Dict[int, dict] = {}
        finish_reason = None

        async for chunk in stream:
            if not chunk.choices:
                continue

            choice = chunk.choices[0]
            delta = choice.delta

            # 文本 -> 实时打印
            if delta.content:
                print(delta.content, end="", flush=True)
                text += delta.content

            # Tool call 分片 -> 累积
            if delta.tool_calls:
                for tc in delta.tool_calls:
                    idx = tc.index
                    if idx not in tool_calls:
                        tool_calls[idx] = {"id": "", "name": "", "arguments": ""}
                    if tc.id:
                        tool_calls[idx]["id"] = tc.id
                    if tc.function:
                        if tc.function.name:
                            tool_calls[idx]["name"] = tc.function.name
                        if tc.function.arguments:
                            tool_calls[idx]["arguments"] += tc.function.arguments

            if choice.finish_reason:
                finish_reason = choice.finish_reason

        print()  # 流式输出换行
        return text, tool_calls, finish_reason

    async def _process_tool_calls(self, messages: list, text: str, tool_calls_map: dict) -> None:
        """
        执行 tool calls 并将 assistant + tool 消息注入 messages。

        Args:
            messages: 对话消息列表（就地修改）
            text: 本轮 assistant 文本内容
            tool_calls_map: {index: {id, name, arguments}}
        """
        # 构建 assistant message
        assistant_calls = []
        for idx in sorted(tool_calls_map.keys()):
            tc = tool_calls_map[idx]
            assistant_calls.append({
                "id": tc["id"],
                "type": "function",
                "function": {"name": tc["name"], "arguments": tc["arguments"]},
            })

        messages.append({
            "role": "assistant",
            "content": text or None,
            "tool_calls": assistant_calls,
        })

        # 逐个执行 tool
        for tc_data in assistant_calls:
            tool_name = tc_data["function"]["name"]
            tool_call_id = tc_data["id"]

            try:
                args = json.loads(tc_data["function"]["arguments"])
            except json.JSONDecodeError:
                args = {}

            args_preview = json.dumps(args, ensure_ascii=False)[:120]
            print(f"  [Exec] [Tool Call] {tool_name}({args_preview})")

            result = await self._dispatch_tool(tool_name, args)
            print(f"  [Exec] [Tool Result] {str(result)[:200]}")

            messages.append({
                "role": "tool",
                "tool_call_id": tool_call_id,
                "content": result,
            })

    # ==================================================================
    # Tool 分发
    # ==================================================================

    async def _dispatch_tool(self, tool_name: str, args: dict) -> str:
        """
        分发 tool call 到对应 handler。

        TODO: 替换占位实现为真实集成
        - redfish_request -> src/tools/redfish.py (requests/httpx)
        - ipmi_command    -> src/tools/ipmi.py   (subprocess + ipmitool)
        - ssh_exec        -> src/tools/ssh.py     (paramiko / asyncssh)
        - bmc_command_rag -> src/tools/rag.py     (向量数据库 + Embedding)
        """
        handler = self._tool_handlers.get(tool_name)
        if not handler:
            return json.dumps({"error": f"未知工具: {tool_name}"}, ensure_ascii=False)

        try:
            return await handler(args)
        except Exception as e:
            return json.dumps({"error": str(e)}, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 占位 Tool 实现
    # ------------------------------------------------------------------

    async def _tool_redfish_request(self, args: dict) -> str:
        """占位：Redfish 请求。TODO: 集成 src/tools/redfish.py"""
        endpoint = args.get("endpoint", "/redfish/v1")
        method = args.get("method", "GET").upper()

        # 按端点+方法返回差异化模拟响应
        if "AccountService/Accounts" in endpoint:
            if method == "GET":
                body = {
                    "@odata.id": "/redfish/v1/AccountService/Accounts",
                    "Members": [{"@odata.id": "/redfish/v1/AccountService/Accounts/2"}],
                    "Members@odata.count": 1,
                }
            elif method == "DELETE":
                body = {"@MessageId": "Base.1.0.Success", "Message": "Deleted."}
            else:
                body = {"@MessageId": "Base.1.0.Success", "Message": "Success."}
        elif "Systems/system" in endpoint:
            body = {"PowerState": "On", "Status": {"Health": "OK"}}
        else:
            body = {"ServiceVersion": "1.0.0", "Status": {"Health": "OK"}}

        return json.dumps({"http_status": 200, "body": body}, ensure_ascii=False, indent=2)

    async def _tool_ipmi_command(self, args: dict) -> str:
        """占位：IPMI 命令。TODO: 集成 src/tools/ipmi.py"""
        cmd = args.get("command", "")
        return json.dumps(
            {"command": cmd, "exit_code": 0, "stdout": f"OK: {cmd}", "stderr": ""},
            ensure_ascii=False,
        )

    async def _tool_ssh_exec(self, args: dict) -> str:
        """占位：SSH 执行。TODO: 集成 src/tools/ssh.py"""
        cmd = args.get("command", "")
        host = args.get("host", "unknown")
        return json.dumps(
            {"host": host, "command": cmd, "exit_code": 0, "stdout": f"OK on {host}: {cmd}", "stderr": ""},
            ensure_ascii=False,
        )

    async def _tool_bmc_command_rag(self, args: dict) -> str:
        """占位：BMC 命令 RAG。TODO: 集成 src/tools/rag.py"""
        operation = args.get("operation_description", "")
        hint = args.get("interface_hint", "any")
        templates = []

        # 关键词匹配模拟
        if "用户" in operation or "user" in operation.lower():
            templates.append({
                "interface_type": "redfish",
                "recommended_command": "POST /redfish/v1/AccountService/Accounts",
                "parameters": {"UserName": "test_user", "Password": "Test@123", "RoleId": "Operator"},
                "notes": "RoleId 可选 Administrator / Operator / ReadOnly",
            })
        if "电源" in operation or "power" in operation.lower():
            templates.append({
                "interface_type": "redfish",
                "recommended_command": "POST /redfish/v1/Systems/system/Actions/ComputerSystem.Reset",
                "parameters": {"ResetType": "On"},
            })
        if not templates:
            templates.append({
                "interface_type": hint if hint != "any" else "redfish",
                "recommended_command": "（未找到匹配模板）",
                "notes": "RAG 知识库中暂无匹配",
            })

        return json.dumps(
            {"operation_description": operation, "results": templates, "total_matches": len(templates)},
            ensure_ascii=False,
            indent=2,
        )

    # ==================================================================
    # 输出解析
    # ==================================================================

    def _parse_record(self, content: str, case: dict, started_at: datetime) -> ExecutionRecord:
        """
        从 LLM 输出解析 ExecutionRecord。

        尝试：直接解析 -> 修复后解析 -> fallback
        """
        json_str = extract_json_from_response(content)

        if json_str:
            # 第一次尝试：直接解析
            try:
                record = ExecutionRecord.model_validate(json.loads(json_str))
                print("[Exec] 成功解析 ExecutionRecord")
                return record
            except (json.JSONDecodeError, ValidationError):
                pass

            # 第二次尝试：补全必填字段后重试
            try:
                data = json.loads(json_str)
                record = self._repair_and_validate(data, case, started_at)
                if record:
                    print("[Exec] 修复后解析成功")
                    return record
            except Exception:
                pass

        # Fallback
        print("[Exec] 未找到有效 JSON，生成 fallback 记录")
        return self._build_failure_record(
            case, started_at,
            raw_output=content,
            step_desc="Agent 输出解析失败，原始输出已保存",
            error_msg="模型输出无法解析为 ExecutionRecord JSON",
        )

    def _repair_and_validate(self, data: dict, case: dict, started_at: datetime) -> Optional[ExecutionRecord]:
        """补全缺失的必填字段后验证。"""
        defaults = {
            "execution_id": f"exec_{datetime.now().strftime('%Y%m%d_%H%M%S')}",
            "case_id": case.get("case_id", case.get("用例_编号", "unknown")),
            "case_name": case.get("name", case.get("用例_名称", "unknown")),
            "started_at": started_at.isoformat(),
            "completed_at": datetime.now().isoformat(),
            "overall_status": "completed",
        }
        for key, value in defaults.items():
            data.setdefault(key, value)

        try:
            return ExecutionRecord.model_validate(data)
        except ValidationError as e:
            print(f"[Exec] 修复后仍无法解析: {e}")
            return None

    # ==================================================================
    # Failure 记录 & 持久化
    # ==================================================================

    def _build_failure_record(
        self,
        case: dict,
        started_at: datetime,
        raw_output: str = "",
        step_desc: str = "执行过程中发生异常",
        error_msg: str = "",
    ) -> ExecutionRecord:
        """
        构建 fallback / error 记录。

        统一处理两种失败场景：
        - 解析失败：raw_output 保留原始 LLM 输出
        - 执行异常：error_msg 保留异常信息
        """
        completed_at = datetime.now()
        ts = completed_at.strftime("%Y%m%d_%H%M%S")

        step = StepRecord(
            step_id="step_failure",
            description=step_desc,
            tool="unknown",
            interface_preference="unknown",
            expected="ExecutionRecord JSON",
            actual=f"异常: {error_msg}" if error_msg else "解析失败",
            raw_stdout=raw_output,
            raw_stderr=error_msg,
            evidence=[],
            status=StepStatus.FAILED,
            error_message=error_msg or "执行失败",
            started_at=started_at,
            completed_at=completed_at,
        )

        return ExecutionRecord(
            execution_id=f"exec_{ts}_failure",
            case_id=case.get("case_id", case.get("用例_编号", "unknown")),
            case_name=case.get("name", case.get("用例_名称", "unknown")),
            environment={
                "bmc_host": self.config.get("target", {}).get("bmc_host", "unknown"),
                "bmc_user": self.config.get("target", {}).get("bmc_user", "unknown"),
            },
            test_case_info={"source_path": case.get("_source_path", ""), "failure": True},
            prerequisites=[],
            steps=[step],
            started_at=started_at,
            completed_at=completed_at,
            overall_status="failed",
        )

    def _save_record(self, record: ExecutionRecord) -> None:
        """自动保存 ExecutionRecord 到共享目录。"""
        try:
            save_execution_record(record, self.shared_dir)
        except Exception as e:
            print(f"[Exec] 保存记录失败（不影响返回）: {e}")
