# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - Test_Exec Agent

职责：理解测试用例 -> 调用 BMC 接口执行 -> 收集证据 -> 生成 ExecutionRecord。
严格遵守 "只执行，不判断" 原则。

技术方案：
- 使用 AsyncOpenAI 客户端，支持 OpenAI-compatible API（GLM-5、MiniMax 等）
- 流式输出（stream=True），实时打印执行过程
- Tool Calling 循环，支持 redfish_request / ipmi_command / ssh_exec / bmc_command_rag
- Jinja2 渲染 system prompt（从 src/prompts/exec_system.txt 加载）

配置格式（config.yaml）：

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

from src.core.schemas import (
    Evidence,
    ExecutionRecord,
    StepRecord,
    StepStatus,
)


class ExecAgent:
    """
    Test_Exec Agent - 测试执行引擎

    通过 OpenAI-compatible API 与 LLM 交互，使用 Tool Calling 执行 BMC 操作。
    支持流式输出，实时展示执行过程。
    """

    SYSTEM_PROMPT_PATH = Path("src/prompts/exec_system.txt")

    # 最大 Tool Calling 轮次（防止无限循环）
    MAX_TOOL_ROUNDS = 20

    # ------------------------------------------------------------------
    # OpenAI function calling 格式的工具定义
    # ------------------------------------------------------------------
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
                        "host": {
                            "type": "string",
                            "description": "目标主机 IP",
                        },
                        "port": {
                            "type": "integer",
                            "description": "SSH 端口（BMC Shell 默认 22，主机控制台默认 2200）",
                            "default": 22,
                        },
                        "user": {
                            "type": "string",
                            "description": "用户名",
                        },
                        "password": {
                            "type": "string",
                            "description": "密码",
                        },
                        "command": {
                            "type": "string",
                            "description": "要执行的命令",
                        },
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

    # ==================================================================
    # 初始化
    # ==================================================================

    def __init__(self, config: dict):
        """
        初始化 Exec Agent。

        Args:
            config: 全局配置字典，需包含 agents.exec.base_url / api_key / model
        """
        exec_cfg = config["agents"]["exec"]

        self.client = AsyncOpenAI(
            base_url=exec_cfg["base_url"],
            api_key=exec_cfg["api_key"],
        )
        self.model = exec_cfg["model"]
        self.config = config

        # 预加载 system prompt 模板
        self._system_template = Template(
            self.SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")
        )

        print(f"[Exec] Agent 初始化完成 (model={self.model})")

    # ==================================================================
    # 公开接口
    # ==================================================================

    async def execute(self, case: dict, config: dict) -> ExecutionRecord:
        """
        执行单个测试用例，返回 ExecutionRecord。

        流程：
        1. 使用 Jinja2 渲染 system prompt（回填用例和环境信息）
        2. 构建 user message
        3. 调用 LLM（stream=True），实时打印流式输出
        4. 处理 Tool Calling 循环
        5. 从最终输出中提取 ExecutionRecord JSON
        6. 返回 ExecutionRecord 对象

        Args:
            case: 测试用例字典（从 YAML 加载）
            config: 全局配置字典

        Returns:
            ExecutionRecord: 完整的执行记录
        """
        case_name = case.get("name", case.get("用例_名称", "unknown"))
        print(f"\n[Exec] 开始执行: {case_name}")

        started_at = datetime.now()

        # 1. 渲染 system prompt
        system_prompt = self._render_system_prompt(case)

        # 2. 构建 user message
        user_message = self._build_user_message(case)

        # 3. 初始化消息列表
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_message},
        ]

        # 4. 运行对话（含 Tool Calling 循环）
        try:
            final_content = await self._run_conversation(messages)
        except Exception as e:
            print(f"[Exec] 执行异常: {e}")
            return self._build_error_record(case, str(e), started_at)

        # 5. 从输出中提取 ExecutionRecord
        record = self._extract_execution_record(final_content, case, started_at)

        print(f"[Exec] 执行完成: {case_name} -> {record.overall_status}")
        return record

    async def execute_batch(
        self, cases: list, config: dict
    ) -> list:
        """
        批量执行测试用例。

        当前实现为串行调用 execute()，后续可优化为并行或 batch 模式。

        Args:
            cases: 测试用例列表
            config: 全局配置字典

        Returns:
            list[ExecutionRecord]: 执行记录列表
        """
        print(f"\n[Exec] 批量执行 {len(cases)} 个用例")

        records = []
        for i, case in enumerate(cases):
            print(f"\n[Exec] --- 用例 {i + 1}/{len(cases)} ---")
            try:
                record = await self.execute(case, config)
                records.append(record)
            except Exception as e:
                print(f"[Exec] 用例执行失败: {e}")
                records.append(
                    self._build_error_record(case, str(e), datetime.now())
                )

        return records

    # ==================================================================
    # Prompt 构建
    # ==================================================================

    def _render_system_prompt(self, case: dict) -> str:
        """
        使用 Jinja2 渲染 system prompt。

        将用例信息和环境变量注入模板，生成完整的 system prompt。
        """
        target = self.config.get("target", {})

        return self._system_template.render(
            # 环境信息
            bmc_host=target.get("bmc_host", "unknown"),
            bmc_user=target.get("bmc_user", "unknown"),
            os_host=target.get("os_host"),
            os_user=target.get("os_user"),
            # 单用例信息
            case=case,
            case_id=case.get("case_id", case.get("用例_编号", "")),
            case_name=case.get("name", case.get("用例_名称", "")),
            test_steps=case.get("测试步骤", []),
            expected_result=case.get("预期结果", []),
            precondition=case.get("预置条件", []),
            # 批量模式（单用例时不传）
            batch_cases=None,
        )

    def _build_user_message(self, case: dict) -> str:
        """
        构建 user message（用例内容）。

        将用例格式化为 JSON 供模型理解，移除内部字段（以 _ 开头的）。
        """
        case_name = case.get("name", case.get("用例_名称", "unknown"))
        # 移除内部字段
        case_copy = {k: v for k, v in case.items() if not k.startswith("_")}
        case_json = json.dumps(case_copy, ensure_ascii=False, indent=2, default=str)

        return (
            f"请执行以下测试用例：\n\n"
            f"用例名称: {case_name}\n\n"
            f"用例内容:\n{case_json}\n\n"
            f"执行完成后，请输出完整的 ExecutionRecord JSON。"
        )

    # ==================================================================
    # LLM 对话循环（流式 + Tool Calling）
    # ==================================================================

    async def _run_conversation(self, messages: list) -> str:
        """
        运行 LLM 对话循环。

        支持多轮 Tool Calling：
        1. 发送消息 -> 流式接收响应
        2. 如果模型返回 tool_calls -> 执行工具 -> 将结果加入消息 -> 继续对话
        3. 如果模型返回普通文本 -> 结束循环，返回文本

        Args:
            messages: 对话消息列表

        Returns:
            str: 模型最终输出的文本内容
        """
        text_content = ""

        for round_num in range(self.MAX_TOOL_ROUNDS):
            print(f"\n[Exec] --- 第 {round_num + 1} 轮 ---")

            stream = await self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=self.TOOL_DEFINITIONS,
                stream=True,
            )

            # 累积本轮响应
            text_content = ""
            tool_calls_map: Dict[int, dict] = {}
            finish_reason = None

            async for chunk in stream:
                if not chunk.choices:
                    continue

                choice = chunk.choices[0]
                delta = choice.delta

                # ---- 文本内容：实时打印 ----
                if delta.content:
                    print(delta.content, end="", flush=True)
                    text_content += delta.content

                # ---- Tool Call：累积分片 ----
                if delta.tool_calls:
                    for tc in delta.tool_calls:
                        idx = tc.index
                        if idx not in tool_calls_map:
                            tool_calls_map[idx] = {
                                "id": "",
                                "name": "",
                                "arguments": "",
                            }
                        if tc.id:
                            tool_calls_map[idx]["id"] = tc.id
                        if tc.function:
                            if tc.function.name:
                                tool_calls_map[idx]["name"] = tc.function.name
                            if tc.function.arguments:
                                tool_calls_map[idx]["arguments"] += tc.function.arguments

                # ---- 结束原因 ----
                if choice.finish_reason:
                    finish_reason = choice.finish_reason

            print()  # 流式输出后换行

            # 无 tool call -> 返回最终文本
            if finish_reason != "tool_calls" or not tool_calls_map:
                return text_content

            # ---- 处理 Tool Calls ----

            # 构建 assistant message（含 tool_calls）
            assistant_tool_calls = []
            for idx in sorted(tool_calls_map.keys()):
                tc = tool_calls_map[idx]
                assistant_tool_calls.append(
                    {
                        "id": tc["id"],
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": tc["arguments"],
                        },
                    }
                )

            messages.append(
                {
                    "role": "assistant",
                    "content": text_content or None,
                    "tool_calls": assistant_tool_calls,
                }
            )

            # 执行每个 tool call，将结果加入消息
            for tc_data in assistant_tool_calls:
                tool_name = tc_data["function"]["name"]
                tool_call_id = tc_data["id"]

                # 解析参数
                try:
                    args = json.loads(tc_data["function"]["arguments"])
                except json.JSONDecodeError:
                    args = {}

                args_preview = json.dumps(args, ensure_ascii=False)[:120]
                print(f"  [Tool Call] {tool_name}({args_preview})")

                # 执行工具
                try:
                    result = await self._handle_tool_call(tool_name, args)
                except Exception as e:
                    result = json.dumps({"error": str(e)}, ensure_ascii=False)

                result_preview = str(result)[:200]
                print(f"  [Tool Result] {result_preview}")

                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "content": result,
                    }
                )

        # 达到最大轮次
        print("[Exec] 达到最大对话轮次限制，使用当前输出")
        return text_content

    # ==================================================================
    # Tool 执行（占位实现，结构已就绪）
    # ==================================================================

    async def _handle_tool_call(self, tool_name: str, arguments: dict) -> str:
        """
        处理单个 tool call。

        当前为占位实现，返回模拟数据。后续集成真实工具时替换此方法。

        TODO: 集成真实工具
        - redfish_request -> src/tools/redfish.py
        - ipmi_command   -> src/tools/ipmi.py
        - ssh_exec       -> src/tools/ssh.py
        - bmc_command_rag -> src/tools/rag.py

        Args:
            tool_name: 工具名称
            arguments: 工具参数字典

        Returns:
            str: 工具执行结果（JSON 字符串）
        """
        if tool_name == "redfish_request":
            return await self._tool_redfish_request(arguments)
        elif tool_name == "ipmi_command":
            return await self._tool_ipmi_command(arguments)
        elif tool_name == "ssh_exec":
            return await self._tool_ssh_exec(arguments)
        elif tool_name == "bmc_command_rag":
            return await self._tool_bmc_command_rag(arguments)
        else:
            return json.dumps({"error": f"未知工具: {tool_name}"}, ensure_ascii=False)

    async def _tool_redfish_request(self, args: dict) -> str:
        """
        占位：Redfish 请求。

        TODO: 集成真实 Redfish 客户端 (requests/httpx + HTTPS)
        """
        endpoint = args.get("endpoint", "/redfish/v1")
        method = args.get("method", "GET").upper()
        body = args.get("body")

        # 模拟响应
        mock_responses = {
            ("/redfish/v1", "GET"): {
                "@odata.type": "#Service.v1_0_0.Service",
                "ServiceVersion": "1.0.0",
                "Status": {"Health": "OK", "State": "Enabled"},
            },
        }

        # Accounts 相关端点
        if "AccountService/Accounts" in endpoint and method == "GET":
            mock_data = {
                "@odata.type": "#AccountService.AccountService",
                "@odata.id": "/redfish/v1/AccountService/Accounts",
                "Members": [
                    {"@odata.id": "/redfish/v1/AccountService/Accounts/2"}
                ],
                "Members@odata.count": 1,
            }
        elif "AccountService/Accounts" in endpoint and method in ("POST", "PATCH"):
            mock_data = {
                "@MessageId": "Base.1.0.Success",
                "Message": "The resource has been created successfully."
                if method == "POST"
                else "The resource has been updated successfully.",
            }
        elif "AccountService/Accounts" in endpoint and method == "DELETE":
            mock_data = {
                "@MessageId": "Base.1.0.Success",
                "Message": "The resource has been deleted successfully.",
            }
        elif "Systems/system" in endpoint and method == "GET":
            mock_data = {
                "@odata.type": "#ComputerSystem.v1_0_0.ComputerSystem",
                "PowerState": "On",
                "Status": {"Health": "OK", "State": "Enabled"},
            }
        else:
            mock_data = mock_responses.get(
                (endpoint, method),
                {"@odata.type": "#Common.v1_0_0.Common", "Status": "OK"},
            )

        return json.dumps(
            {
                "http_status": 200,
                "headers": {"Content-Type": "application/json"},
                "body": mock_data,
            },
            ensure_ascii=False,
            indent=2,
        )

    async def _tool_ipmi_command(self, args: dict) -> str:
        """
        占位：IPMI 命令。

        TODO: 集成真实 IPMI 工具 (subprocess 调用 ipmitool)
        """
        command = args.get("command", "")

        return json.dumps(
            {
                "command": command,
                "exit_code": 0,
                "stdout": f"IPMI command '{command}' completed successfully.",
                "stderr": "",
            },
            ensure_ascii=False,
            indent=2,
        )

    async def _tool_ssh_exec(self, args: dict) -> str:
        """
        占位：SSH 执行。

        TODO: 集成真实 SSH 客户端 (paramiko / asyncssh)
        """
        command = args.get("command", "")
        host = args.get("host", "unknown")

        return json.dumps(
            {
                "host": host,
                "command": command,
                "exit_code": 0,
                "stdout": f"SSH command executed successfully on {host}: {command}",
                "stderr": "",
            },
            ensure_ascii=False,
            indent=2,
        )

    async def _tool_bmc_command_rag(self, args: dict) -> str:
        """
        占位：BMC 命令 RAG 检索。

        TODO: 集成真实 RAG 系统（向量数据库 + Embedding 模型）

        当前返回模拟的命令模板，基于操作描述的关键词匹配。
        """
        operation = args.get("operation_description", "")
        interface_hint = args.get("interface_hint", "any")

        # 简单关键词匹配模拟
        templates = []

        if "用户" in operation or "user" in operation.lower():
            if interface_hint in ("cli", "any"):
                templates.append(
                    {
                        "interface_type": "cli",
                        "recommended_command": "account adduser --username test_user --password Test@123 --role Operator",
                        "notes": "新增用户需要指定用户名、密码和角色",
                        "boundary_handling": "添加前必须检查用户数量是否达到上限（15个）",
                    }
                )
            if interface_hint in ("redfish", "any"):
                templates.append(
                    {
                        "interface_type": "redfish",
                        "recommended_command": "POST /redfish/v1/AccountService/Accounts",
                        "parameters": {
                            "UserName": "test_user",
                            "Password": "Test@123",
                            "RoleId": "Operator",
                        },
                        "notes": "Redfish POST 创建用户，RoleId 可选 Administrator / Operator / ReadOnly",
                    }
                )

        if "电源" in operation or "power" in operation.lower():
            templates.append(
                {
                    "interface_type": "redfish",
                    "recommended_command": "POST /redfish/v1/Systems/system/Actions/ComputerSystem.Reset",
                    "parameters": {"ResetType": "On"},
                    "notes": "ResetType 可选 On/Off/GracefulShutdown/ForceRestart",
                }
            )

        if not templates:
            templates.append(
                {
                    "interface_type": interface_hint if interface_hint != "any" else "redfish",
                    "recommended_command": "（未找到匹配模板，请根据操作描述自行生成命令）",
                    "notes": "RAG 知识库中暂无匹配的历史命令",
                }
            )

        return json.dumps(
            {
                "operation_description": operation,
                "interface_hint": interface_hint,
                "results": templates,
                "total_matches": len(templates),
            },
            ensure_ascii=False,
            indent=2,
        )

    # ==================================================================
    # 输出解析
    # ==================================================================

    def _extract_execution_record(
        self, content: str, case: dict, started_at: datetime
    ) -> ExecutionRecord:
        """
        从模型输出中提取 ExecutionRecord。

        处理以下情况：
        1. 纯 JSON
        2. JSON 包裹在 ```json ... ``` 中
        3. JSON 前后有额外文本
        4. 无有效 JSON -> 返回包含原始输出的 fallback 记录
        """
        json_str = self._find_json(content)

        if json_str:
            try:
                data = json.loads(json_str)
                record = ExecutionRecord.model_validate(data)
                print(f"[Exec] 成功解析 ExecutionRecord")
                return record
            except (json.JSONDecodeError, ValidationError) as e:
                print(f"[Exec] ExecutionRecord 解析失败: {e}")
                # 尝试修复常见问题后重试
                record = self._try_repair_and_validate(data, case, started_at)
                if record:
                    return record

        # Fallback: 将原始输出保存到记录中
        print("[Exec] 未找到有效 JSON，生成 fallback 记录")
        return self._build_fallback_record(content, case, started_at)

    @staticmethod
    def _find_json(text: str) -> Optional[str]:
        """
        从文本中提取 JSON 字符串。

        尝试以下策略：
        1. 提取 ```json ... ``` 代码块
        2. 查找最外层 { } 配对
        """
        # 策略 1: Markdown 代码块
        pattern = r"```json\s*\n?(.*?)\n?\s*```"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 策略 2: 查找最外层 { }
        # 找到第一个 { 和最后一个 } 之间的内容
        start = text.find("{")
        end = text.rfind("}")
        if start != -1 and end > start:
            return text[start : end + 1]

        return None

    def _try_repair_and_validate(
        self, data: dict, case: dict, started_at: datetime
    ) -> Optional[ExecutionRecord]:
        """
        尝试修复常见问题并验证 ExecutionRecord。

        常见问题：
        - 缺少必填字段
        - 日期格式不正确
        - steps 中的字段类型不匹配
        """
        try:
            # 确保必填字段存在
            completed_at = datetime.now()
            if "execution_id" not in data:
                data["execution_id"] = f"exec_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
            if "case_id" not in data:
                data["case_id"] = case.get("case_id", case.get("用例_编号", "unknown"))
            if "case_name" not in data:
                data["case_name"] = case.get("name", case.get("用例_名称", "unknown"))
            if "started_at" not in data:
                data["started_at"] = started_at.isoformat()
            if "completed_at" not in data:
                data["completed_at"] = completed_at.isoformat()
            if "overall_status" not in data:
                data["overall_status"] = "completed"

            return ExecutionRecord.model_validate(data)
        except (ValidationError, Exception) as e:
            print(f"[Exec] 修复后仍然无法解析: {e}")
            return None

    # ==================================================================
    # Fallback 记录构建
    # ==================================================================

    def _build_fallback_record(
        self, raw_output: str, case: dict, started_at: datetime
    ) -> ExecutionRecord:
        """
        当无法从模型输出中解析 ExecutionRecord 时，构建一条 fallback 记录。

        将原始输出保存到 raw_stdout 中，确保不丢失任何信息。
        """
        completed_at = datetime.now()
        case_name = case.get("name", case.get("用例_名称", "unknown"))
        case_id = case.get("case_id", case.get("用例_编号", "unknown"))
        execution_id = f"exec_{completed_at.strftime('%Y%m%d_%H%M%S')}_fallback"

        fallback_step = StepRecord(
            step_id="step_fallback",
            description="Agent 输出解析失败，原始输出已保存",
            tool="unknown",
            interface_preference="unknown",
            expected="ExecutionRecord JSON",
            actual="解析失败",
            raw_stdout=raw_output,
            raw_stderr="",
            evidence=[],
            status=StepStatus.FAILED,
            error_message="模型输出无法解析为 ExecutionRecord JSON",
            started_at=started_at,
            completed_at=completed_at,
        )

        return ExecutionRecord(
            execution_id=execution_id,
            case_id=case_id,
            case_name=case_name,
            environment={
                "bmc_host": self.config.get("target", {}).get("bmc_host", "unknown"),
                "bmc_user": self.config.get("target", {}).get("bmc_user", "unknown"),
            },
            test_case_info={"source_path": case.get("_source_path", ""), "fallback": True},
            prerequisites=[],
            steps=[fallback_step],
            started_at=started_at,
            completed_at=completed_at,
            overall_status="failed",
        )

    def _build_error_record(
        self, case: dict, error_msg: str, started_at: datetime
    ) -> ExecutionRecord:
        """
        当执行过程中发生异常时，构建一条错误记录。
        """
        completed_at = datetime.now()
        case_name = case.get("name", case.get("用例_名称", "unknown"))
        case_id = case.get("case_id", case.get("用例_编号", "unknown"))
        execution_id = f"exec_{completed_at.strftime('%Y%m%d_%H%M%S')}_error"

        error_step = StepRecord(
            step_id="step_error",
            description="执行过程中发生异常",
            tool="unknown",
            interface_preference="unknown",
            expected="正常执行",
            actual=f"异常: {error_msg}",
            raw_stdout="",
            raw_stderr=error_msg,
            evidence=[],
            status=StepStatus.FAILED,
            error_message=error_msg,
            started_at=started_at,
            completed_at=completed_at,
        )

        return ExecutionRecord(
            execution_id=execution_id,
            case_id=case_id,
            case_name=case_name,
            environment={
                "bmc_host": self.config.get("target", {}).get("bmc_host", "unknown"),
                "bmc_user": self.config.get("target", {}).get("bmc_user", "unknown"),
            },
            test_case_info={"source_path": case.get("_source_path", ""), "error": True},
            prerequisites=[],
            steps=[error_step],
            started_at=started_at,
            completed_at=completed_at,
            overall_status="failed",
        )
