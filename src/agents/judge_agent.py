# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - Test_Judge Agent（独立判断引擎）

职责：
  - 从共享目录读取 ExecutionRecord
  - 使用 Qwen3/GLM 等大模型进行严格判断
  - 输出 TestResult（结构化 JSON）
  - 生成 audit_report.md（完整审计报告）
  - 写入 shared/audit_reports/

设计原则：
  - 宁可错杀，不可放过
  - 证据驱动，禁止推测
  - 温度 0.0（最大确定性）
  - 鲁棒的 JSON + Markdown 解析

资源所有权说明：
  - JudgeAgent 通过 ClientFactory.create_for("judge") 获取 AsyncOpenAI 客户端
  - ClientFactory 负责客户端的创建和销毁，JudgeAgent 不持有所有权
  - 调用方（main.py）负责在 finally 中调用 factory.close() 释放所有资源
"""

import asyncio
import json
import logging
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from openai import AsyncOpenAI
from pydantic import ValidationError

from src.core.config import AppConfig, get_component_config
from src.core.client_factory import ClientFactory
from src.core.schemas import (
    AssertionJudgment,
    CURRENT_SCHEMA_VERSION,
    ExecutionRecord,
    StepJudgment,
    TestResult,
    load_execution_record_with_migration,
)


logger = logging.getLogger("judge_agent")


# ======================================================================
# 通用枚举值安全提取（消除重复的 not in ("PASS", "FAIL") 验证）
# ======================================================================

def _safe_enum(value: Any, allowed: Tuple[str, ...], default: str) -> str:
    """安全提取枚举值，不在允许列表中则返回默认值。"""
    v = str(value).strip() if value else default
    return v if v in allowed else default


_PASS_FAIL = ("PASS", "FAIL")
_RISK_LEVELS = ("none", "low", "medium", "high")


# ======================================================================
# JudgeOutputParser - 输出解析器类
# ======================================================================

class JudgeOutputParser:
    """
    Judge 模型输出解析器。

    负责从 LLM 原始输出中提取结构化 JSON 和 Markdown 审计报告。
    内部采用多策略解析以适配不同模型的输出风格。

    用法：
        parser = JudgeOutputParser()
        json_obj, markdown = parser.parse(raw_text)
    """

    def parse(self, raw_text: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
        """
        解析 Judge 模型的输出，提取 JSON 和 Markdown 审计报告。

        模型输出格式：
        1. <thinking>...</thinking>（可选）
        2. JSON 对象
        3. ## AUDIT_REPORT_START ... ## AUDIT_REPORT_END

        Args:
            raw_text: 模型原始输出文本

        Returns:
            (parsed_json_dict, audit_report_markdown)
            如果解析失败，对应位置返回 None
        """
        if not raw_text or not raw_text.strip():
            logger.error("Judge 输出为空")
            return None, None

        # 去除 <thinking>...</thinking> 块
        text = re.sub(r"<thinking>.*?</thinking>", "", raw_text, flags=re.DOTALL)

        # 提取审计报告 Markdown
        audit_report = self._extract_audit_report(text)

        # 提取 JSON（多种策略）
        json_obj = self._extract_json(text)

        if json_obj is not None:
            logger.info(
                f"Judge 输出解析成功: "
                f"overall_result={json_obj.get('overall_result')}, "
                f"steps={len(json_obj.get('step_results', []))}, "
                f"has_audit_report={audit_report is not None}"
            )

        return json_obj, audit_report

    # ------------------------------------------------------------------
    # Markdown 提取
    # ------------------------------------------------------------------

    def _extract_audit_report(self, text: str) -> Optional[str]:
        """
        从模型输出中提取审计报告 Markdown。

        策略 1: ## AUDIT_REPORT_START ... ## AUDIT_REPORT_END 标记对
        策略 2: 从 "# 测试审计报告" 标题开始到 "AUDIT_REPORT_END" 或文本结尾
        """
        # 策略 1: 标准标记对
        pattern = r"##\s*AUDIT_REPORT_START\s*\n(.*?)##\s*AUDIT_REPORT_END"
        match = re.search(pattern, text, re.DOTALL)
        if match:
            return match.group(1).strip()

        # 策略 2: 宽松匹配
        lines = text.split("\n")
        report_lines: List[str] = []
        in_report = False
        for line in lines:
            if "# 测试审计报告" in line or "AUDIT_REPORT_START" in line:
                in_report = True
                continue
            if "AUDIT_REPORT_END" in line:
                break
            if in_report:
                report_lines.append(line)

        if report_lines:
            return "\n".join(report_lines).strip()

        return None

    # ------------------------------------------------------------------
    # JSON 提取
    # ------------------------------------------------------------------

    def _extract_json(self, text: str) -> Optional[Dict[str, Any]]:
        """
        从文本中提取 JSON 对象（多策略）。

        策略优先级：
        1. ```json ... ``` 代码块
        2. ``` ... ``` 代码块（无语言标记）
        3. 括号配对提取最大 { } 块
        4. 第一个 { 到最后一个 }
        """
        # 去除 audit report 部分，避免干扰 JSON 解析
        clean_text = re.sub(
            r"##\s*AUDIT_REPORT_START.*", "", text, flags=re.DOTALL
        )

        # 策略 1: ```json ... ```
        match = re.search(r"```json\s*\n?(.*?)\n?\s*```", clean_text, re.DOTALL)
        if match:
            parsed = self._try_parse_json(match.group(1).strip())
            if parsed:
                return parsed

        # 策略 2: ``` ... ```（无语言标记）
        match = re.search(r"```\s*\n?(.*?)\n?\s*```", clean_text, re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            if candidate.startswith("{"):
                parsed = self._try_parse_json(candidate)
                if parsed:
                    return parsed

        # 策略 3: 括号配对
        balanced = self._extract_balanced_json(clean_text)
        if balanced:
            parsed = self._try_parse_json(balanced)
            if parsed:
                return parsed

        # 策略 4: 暴力截取
        start = clean_text.find("{")
        end = clean_text.rfind("}")
        if start != -1 and end > start:
            parsed = self._try_parse_json(clean_text[start : end + 1])
            if parsed:
                return parsed

        logger.error("无法从 Judge 输出中提取有效 JSON")
        return None

    @staticmethod
    def _extract_balanced_json(text: str) -> Optional[str]:
        """括号配对提取顶层 JSON 对象。"""
        start = text.find("{")
        if start == -1:
            return None

        depth = 0
        in_string = False
        escape_next = False
        i = start

        while i < len(text):
            ch = text[i]
            if escape_next:
                escape_next = False
                i += 1
                continue
            if ch == "\\" and in_string:
                escape_next = True
                i += 1
                continue
            if ch == '"':
                in_string = not in_string
                i += 1
                continue
            if in_string:
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return text[start : i + 1]
            i += 1

        return None

    @staticmethod
    def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
        """尝试解析 JSON 字符串，带常见错误修复。"""
        if not text or not text.strip():
            return None

        s = text.strip()
        # 移除尾逗号
        s = re.sub(r",\s*([}\]])", r"\1", s)
        # 移除控制字符
        s = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", s)

        try:
            result = json.loads(s)
            if isinstance(result, dict):
                return result
        except json.JSONDecodeError as e:
            logger.debug(f"JSON 解析失败: {e}")

        return None


# ======================================================================
# TestResult 构建辅助
# ======================================================================

def _build_step_judgment(sr_data: Dict[str, Any]) -> StepJudgment:
    """
    从 Judge 输出中的单步数据构建 StepJudgment。

    缺省值均倾向 FAIL，遵循"宁可错杀"原则。
    """
    # 构建断言判断
    assertion_judgments = [
        AssertionJudgment(
            assertion_id=aj.get("assertion_id", ""),
            passed=bool(aj.get("passed", False)),
            actual_value=aj.get("actual_value"),
            reason=aj.get("reason", ""),
        )
        for aj in sr_data.get("assertion_judgments", [])
    ]

    return StepJudgment(
        step_id=sr_data.get("step_id", "unknown"),
        result=_safe_enum(sr_data.get("result"), _PASS_FAIL, "FAIL"),
        confidence=float(sr_data.get("confidence", 0.5)),
        reason=sr_data.get("reason", "Judge 未提供判断理由"),
        expected_match=bool(sr_data.get("expected_match", False)),
        concerns=sr_data.get("concerns", sr_data.get("关注点", [])),
        assertion_judgments=assertion_judgments,
        evidence_sufficient=bool(sr_data.get("evidence_sufficient", True)),
    )


def build_test_result_from_json(
    parsed: Dict[str, Any],
    execution_record: ExecutionRecord,
    judge_model: str,
    duration_seconds: float,
    audit_markdown: Optional[str] = None,
) -> TestResult:
    """
    从解析的 JSON 构建 TestResult。

    如果 JSON 中缺少必要字段，使用安全默认值（倾向于 FAIL）。
    """
    # 预置条件检查
    prereq_check = parsed.get("prerequisite_check", parsed.get("prerequisite", {}))
    if not isinstance(prereq_check, dict):
        prereq_check = {"result": "FAIL", "failed_items": []}

    # 环境恢复
    env_recovery = parsed.get("environment_recovery", {})
    if not isinstance(env_recovery, dict):
        env_recovery = {"recovered": False, "warnings": []}

    return TestResult(
        schema_version=CURRENT_SCHEMA_VERSION,
        execution_id=execution_record.execution_id,
        case_id=execution_record.case_id,
        case_name=execution_record.case_name,
        overall_result=_safe_enum(parsed.get("overall_result"), _PASS_FAIL, "FAIL"),
        confidence=float(parsed.get("confidence", 0.5)),
        step_results=[_build_step_judgment(sr) for sr in parsed.get("step_results", [])],
        prerequisite_check=prereq_check,
        environment_recovery=env_recovery,
        judge_notes=parsed.get("judge_notes", []),
        judge_model=judge_model,
        judge_duration_seconds=duration_seconds,
        false_pass_risk=_safe_enum(parsed.get("false_pass_risk"), _RISK_LEVELS, "none"),
        risk_notes=parsed.get("risk_notes", []),
        audit_report_markdown=audit_markdown,
    )


def build_error_test_result(
    execution_record: ExecutionRecord,
    judge_model: str,
    error_message: str,
    duration_seconds: float = 0.0,
) -> TestResult:
    """
    当 Judge 调用完全失败时，构建 ERROR 级别的 TestResult。

    所有步骤判定为 FAIL，置信度 0.0，假 PASS 风险 high。
    """
    step_results = [
        StepJudgment(
            step_id=step.step_id,
            result="FAIL",
            confidence=0.0,
            reason=f"Judge 调用失败，无法判断: {error_message}",
            expected_match=False,
            concerns=["Judge 引擎异常，结果不可信"],
            evidence_sufficient=False,
        )
        for step in execution_record.steps
    ]

    return TestResult(
        schema_version=CURRENT_SCHEMA_VERSION,
        execution_id=execution_record.execution_id,
        case_id=execution_record.case_id,
        case_name=execution_record.case_name,
        overall_result="FAIL",
        confidence=0.0,
        step_results=step_results,
        prerequisite_check={"result": "FAIL", "failed_items": ["Judge 引擎异常"]},
        environment_recovery={"recovered": False, "warnings": ["Judge 异常，环境恢复状态未知"]},
        judge_notes=[f"Judge 引擎异常: {error_message}"],
        judge_model=judge_model,
        judge_duration_seconds=duration_seconds,
        false_pass_risk="high",
        risk_notes=["Judge 引擎异常，无法进行风险评估，默认 high"],
        audit_report_markdown=None,
    )


# ======================================================================
# 审计报告文件写入
# ======================================================================

def write_audit_report(
    execution_id: str,
    markdown_content: str,
    shared_dir: str,
) -> str:
    """
    将审计报告 Markdown 写入 shared/audit_reports/ 目录。

    Args:
        execution_id: 执行记录 ID
        markdown_content: Markdown 内容
        shared_dir: 共享目录根路径

    Returns:
        报告文件路径
    """
    reports_dir = Path(shared_dir) / "audit_reports"
    reports_dir.mkdir(parents=True, exist_ok=True)

    report_path = reports_dir / f"{execution_id}_audit_report.md"

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(markdown_content)

    logger.info(f"审计报告已写入: {report_path}")
    return str(report_path)


def generate_fallback_audit_report(
    execution_record: ExecutionRecord,
    test_result: TestResult,
) -> str:
    """
    当 Judge 没有返回 Markdown 审计报告时，基于 TestResult 生成 fallback 报告。
    """
    lines = [
        f"# 测试审计报告 - {execution_record.case_name}",
        "",
        f"**执行 ID**: {execution_record.execution_id}",
        f"**用例 ID**: {execution_record.case_id}",
        f"**判断结果**: **{test_result.overall_result}** (置信度: {test_result.confidence:.2f})",
        f"**假 PASS 风险**: {test_result.false_pass_risk}",
        f"**判断模型**: {test_result.judge_model}",
        f"**判断耗时**: {test_result.judge_duration_seconds:.1f}s",
        "",
        "---",
        "",
        "## 1. 总体结论",
        "",
    ]

    if test_result.overall_result == "FAIL":
        lines.append("测试未通过。")
    else:
        lines.append("测试通过。")

    failed_steps = [sr for sr in test_result.step_results if sr.result == "FAIL"]
    if failed_steps:
        lines.append(f"存在 {len(failed_steps)} 个失败步骤。")

    lines.extend([
        "",
        "## 2. 预置条件验证",
        "",
        f"- **结果**: {test_result.prerequisite_check.get('result', 'unknown')}",
    ])
    for item in test_result.prerequisite_check.get("failed_items", []):
        lines.append(f"- **失败项**: {item}")

    lines.extend(["", "## 3. 逐步骤判断详情", ""])
    for sr in test_result.step_results:
        step_num = sr.step_id.replace("step_", "").lstrip("0") or "1"
        lines.extend([
            f"### Step {step_num}",
            f"- **结果**: {sr.result}",
            f"- **置信度**: {sr.confidence:.2f}",
            f"- **理由**: {sr.reason}",
            f"- **预期匹配**: {'是' if sr.expected_match else '否'}",
            f"- **证据充分**: {'是' if sr.evidence_sufficient else '否'}",
        ])
        if sr.concerns:
            lines.append("- **关注点**:")
            lines.extend(f"  - {c}" for c in sr.concerns)
        lines.append("")

    lines.extend([
        "## 4. 环境恢复状态",
        "",
        f"- **已恢复**: {'是' if test_result.environment_recovery.get('recovered') else '否'}",
    ])
    for w in test_result.environment_recovery.get("warnings", []):
        lines.append(f"- **警告**: {w}")

    if test_result.judge_notes:
        lines.extend(["", "## 5. Judge 说明", ""])
        lines.extend(f"- {note}" for note in test_result.judge_notes)

    lines.extend([
        "",
        "---",
        f"*报告生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}*",
        "",
    ])

    return "\n".join(lines)


# ======================================================================
# JudgeAgent 主类
# ======================================================================

class JudgeAgent:
    """
    独立判断引擎。

    使用 Qwen3/GLM 等大模型对 ExecutionRecord 进行严格判断，
    生成 TestResult 和审计报告。

    资源所有权：
        JudgeAgent 通过 ClientFactory 获取 AsyncOpenAI 客户端，
        但不拥有 ClientFactory。调用方负责 factory.close() 释放资源。

    用法：
        from src.core.config import load_config
        from src.core.client_factory import ClientFactory
        from src.agents.judge_agent import JudgeAgent

        cfg = load_config()
        factory = ClientFactory(cfg)
        agent = JudgeAgent(cfg, factory)

        result = await agent.judge(record_path)
        print(result.overall_result)

        # 调用方负责释放
        await factory.close()
    """

    def __init__(
        self,
        config: AppConfig,
        client_factory: ClientFactory,
        shared_dir: str = "./shared",
    ):
        """
        初始化 Judge Agent。

        Args:
            config: AppConfig 实例
            client_factory: ClientFactory 实例（调用方拥有，负责生命周期）
            shared_dir: 共享目录根路径
        """
        self._config = config
        self._factory = client_factory
        self._shared_dir = shared_dir

        # 加载 judge 组件配置
        self._judge_comp = get_component_config(config, "judge")
        self._client: Optional[AsyncOpenAI] = None

        # 解析器实例
        self._parser = JudgeOutputParser()

        # 加载 Prompt 模板（支持多文件组合）
        self._system_prompt = self._load_combined_system_prompt()
        self._user_template = self._load_prompt("judge_user_template_v2.1.txt")

        logger.info(
            f"JudgeAgent 初始化: provider={self._judge_comp.provider_name}, "
            f"model={self._judge_comp.model}, "
            f"base_url={self._judge_comp.base_url}"
        )

    # ------------------------------------------------------------------
    # Prompt 加载（支持模块化组合）
    # ------------------------------------------------------------------

    def _load_prompt(self, filename: str) -> str:
        """从 src/prompts/ 目录加载单个 Prompt 文件。"""
        prompt_path = Path(__file__).resolve().parent.parent / "prompts" / filename
        if not prompt_path.exists():
            raise FileNotFoundError(f"Prompt 文件不存在: {prompt_path}")
        content = prompt_path.read_text(encoding="utf-8")
        logger.debug(f"已加载 Prompt: {prompt_path} ({len(content)} 字符)")
        return content

    def _load_combined_system_prompt(self) -> str:
        """
        加载并组合系统 Prompt。

        优先加载组合文件（judge_system_v2.1.txt），
        如果拆分文件存在则按顺序组合：
          1. judge_system_core.txt       - 核心规则
          2. judge_system_openubmc.txt   - 领域知识
          3. judge_system_output.txt     - 输出格式
        """
        prompts_dir = Path(__file__).resolve().parent.parent / "prompts"

        # 检查拆分文件是否存在
        parts = [
            "judge_system_core.txt",
            "judge_system_openubmc.txt",
            "judge_system_output.txt",
        ]
        all_exist = all((prompts_dir / p).exists() for p in parts)

        if all_exist:
            sections = []
            for part_file in parts:
                content = (prompts_dir / part_file).read_text(encoding="utf-8").strip()
                sections.append(content)
            combined = "\n\n---\n\n".join(sections)
            logger.info(
                f"System Prompt 组合加载: {len(sections)} 个模块, "
                f"总长度 {len(combined)} 字符"
            )
            return combined

        # fallback: 使用单文件
        return self._load_prompt("judge_system_v2.1.txt")

    # ------------------------------------------------------------------
    # 客户端获取
    # ------------------------------------------------------------------

    def _get_client(self) -> AsyncOpenAI:
        """获取或创建 Judge 组件的 API 客户端（由 ClientFactory 管理生命周期）。"""
        if self._client is None:
            self._client = self._factory.create_for("judge")
        return self._client

    # ==================================================================
    # 核心 API
    # ==================================================================

    async def judge(self, record_path: Path) -> TestResult:
        """
        对单个 ExecutionRecord 进行判断。

        流程：
        1. 读取并解析 ExecutionRecord JSON 文件
        2. 自动迁移 v1 -> v2.1
        3. 调用 Judge 模型（三层 Prompt）
        4. 解析模型返回的 JSON + Markdown
        5. 构建 TestResult
        6. 写入 audit_report.md
        7. 返回 TestResult

        Args:
            record_path: ExecutionRecord JSON 文件路径

        Returns:
            TestResult 实例
        """
        start_time = time.time()

        # Step 1: 读取 ExecutionRecord
        execution_record = self._load_execution_record(record_path)
        if execution_record is None:
            return build_error_test_result(
                execution_record=ExecutionRecord(
                    execution_id=record_path.stem,
                    case_id="unknown",
                    case_name="unknown",
                ),
                judge_model=self._judge_comp.model,
                error_message=f"无法读取 ExecutionRecord: {record_path}",
                duration_seconds=time.time() - start_time,
            )

        logger.info(
            f"开始判断: execution_id={execution_record.execution_id}, "
            f"case_name={execution_record.case_name}, "
            f"steps={len(execution_record.steps)}, "
            f"schema_version={execution_record.schema_version}"
        )

        # Step 2-5: 调用 + 解析 + 构建
        result = await self._execute_judgment(execution_record, start_time)

        # Step 6: 写入审计报告
        self._persist_audit_report(execution_record, result)

        logger.info(
            f"判断完成: {execution_record.case_name} -> {result.overall_result} "
            f"(confidence={result.confidence:.2f}, risk={result.false_pass_risk}, "
            f"duration={result.judge_duration_seconds:.1f}s)"
        )

        return result

    async def judge_from_record(
        self,
        execution_record: ExecutionRecord,
    ) -> TestResult:
        """
        直接从 ExecutionRecord 对象进行判断（不需要先写入文件再读取）。

        Args:
            execution_record: ExecutionRecord 实例

        Returns:
            TestResult 实例
        """
        start_time = time.time()

        logger.info(
            f"开始判断(in-memory): execution_id={execution_record.execution_id}, "
            f"case_name={execution_record.case_name}, "
            f"steps={len(execution_record.steps)}"
        )

        result = await self._execute_judgment(execution_record, start_time)
        self._persist_audit_report(execution_record, result)

        logger.info(
            f"判断完成: {execution_record.case_name} -> {result.overall_result} "
            f"(confidence={result.confidence:.2f}, risk={result.false_pass_risk}, "
            f"duration={result.judge_duration_seconds:.1f}s)"
        )

        return result

    async def judge_batch(
        self,
        record_paths: List[Path],
        max_concurrency: int = 1,
    ) -> List[TestResult]:
        """
        批量判断多个 ExecutionRecord。

        Args:
            record_paths: ExecutionRecord JSON 文件路径列表
            max_concurrency: 最大并发数（默认 1，串行）

        Returns:
            TestResult 列表，顺序与输入一致
        """
        semaphore = asyncio.Semaphore(max_concurrency)

        async def _sem_judge(path: Path) -> TestResult:
            async with semaphore:
                return await self.judge(path)

        tasks = [_sem_judge(p) for p in record_paths]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # 将异常转换为 error TestResult
        final: List[TestResult] = []
        for i, r in enumerate(results):
            if isinstance(r, Exception):
                logger.error(f"批量判断第 {i} 项失败: {r}")
                final.append(build_error_test_result(
                    execution_record=ExecutionRecord(
                        execution_id=record_paths[i].stem,
                        case_id="unknown",
                        case_name=f"batch_item_{i}",
                    ),
                    judge_model=self._judge_comp.model,
                    error_message=str(r),
                ))
            else:
                final.append(r)

        logger.info(
            f"批量判断完成: {len(final)} 项, "
            f"PASS={sum(1 for r in final if r.overall_result == 'PASS')}, "
            f"FAIL={sum(1 for r in final if r.overall_result == 'FAIL')}"
        )
        return final

    # ==================================================================
    # 内部方法
    # ==================================================================

    async def _execute_judgment(
        self,
        execution_record: ExecutionRecord,
        start_time: float,
    ) -> TestResult:
        """
        核心判断流程：调用模型 -> 解析输出 -> 构建 TestResult。

        三层防护：模型调用异常 / JSON 解析失败 / TestResult 构建失败
        """
        # Step 1: 调用 Judge 模型
        try:
            raw_response = await self._call_judge_model(execution_record)
        except Exception as e:
            logger.error(f"Judge 模型调用失败: {e}")
            return build_error_test_result(
                execution_record=execution_record,
                judge_model=self._judge_comp.model,
                error_message=str(e),
                duration_seconds=time.time() - start_time,
            )

        duration = time.time() - start_time

        # Step 2: 解析模型输出
        parsed_json, audit_markdown = self._parser.parse(raw_response)

        if parsed_json is None:
            logger.error("Judge 输出解析失败，使用 ERROR fallback")
            return build_error_test_result(
                execution_record=execution_record,
                judge_model=self._judge_comp.model,
                error_message="Judge 输出解析失败，无法提取有效 JSON",
                duration_seconds=duration,
            )

        # Step 3: 构建 TestResult
        try:
            return build_test_result_from_json(
                parsed=parsed_json,
                execution_record=execution_record,
                judge_model=self._judge_comp.model,
                duration_seconds=duration,
                audit_markdown=audit_markdown,
            )
        except Exception as e:
            logger.error(f"TestResult 构建失败: {e}")
            return build_error_test_result(
                execution_record=execution_record,
                judge_model=self._judge_comp.model,
                error_message=f"TestResult 构建失败: {e}",
                duration_seconds=duration,
            )

    def _load_execution_record(self, record_path: Path) -> Optional[ExecutionRecord]:
        """从 JSON 文件加载 ExecutionRecord，支持版本自动迁移。"""
        if not record_path.exists():
            logger.error(f"ExecutionRecord 文件不存在: {record_path}")
            return None

        try:
            with open(record_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except json.JSONDecodeError as e:
            logger.error(f"ExecutionRecord JSON 解析失败: {e}")
            return None
        except Exception as e:
            logger.error(f"读取 ExecutionRecord 失败: {e}")
            return None

        try:
            return load_execution_record_with_migration(data)
        except ValidationError as e:
            logger.error(f"ExecutionRecord 验证失败: {e}")
            return None

    async def _call_judge_model(
        self,
        execution_record: ExecutionRecord,
    ) -> str:
        """
        调用 Judge 模型进行判断。

        使用三层 Prompt 结构：
        - Layer 1+2: System Prompt（角色 + 领域知识）
        - Layer 3: Task Prompt（当前 Execution Record）
        """
        client = self._get_client()

        # 构建 Task Prompt（Layer 3）
        record_json = json.dumps(
            execution_record.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            default=str,
        )
        user_message = self._user_template.replace(
            "{execution_record_json}", record_json
        )

        logger.info(
            f"调用 Judge 模型: model={self._judge_comp.model}, "
            f"record_json_len={len(record_json)}"
        )

        # 调用 API（非流式，需要完整响应以便解析）
        response = await client.chat.completions.create(
            model=self._judge_comp.model,
            messages=[
                {"role": "system", "content": self._system_prompt},
                {"role": "user", "content": user_message},
            ],
            temperature=0.0,
            max_tokens=self._judge_comp.max_tokens,
        )

        # 提取文本
        if not response.choices:
            raise ValueError("Judge 模型返回空响应")

        content = response.choices[0].message.content
        if not content:
            raise ValueError("Judge 模型返回空内容")

        # 记录 token 使用情况
        if response.usage:
            logger.info(
                f"Judge token 使用: prompt={response.usage.prompt_tokens}, "
                f"completion={response.usage.completion_tokens}, "
                f"total={response.usage.total_tokens}"
            )

        return content

    def _persist_audit_report(
        self,
        execution_record: ExecutionRecord,
        test_result: TestResult,
    ) -> None:
        """根据 TestResult 写入审计报告。优先使用 Judge 返回的 Markdown，否则 fallback。"""
        try:
            if test_result.audit_report_markdown:
                write_audit_report(
                    execution_id=execution_record.execution_id,
                    markdown_content=test_result.audit_report_markdown,
                    shared_dir=self._shared_dir,
                )
            else:
                fallback_md = generate_fallback_audit_report(execution_record, test_result)
                write_audit_report(
                    execution_id=execution_record.execution_id,
                    markdown_content=fallback_md,
                    shared_dir=self._shared_dir,
                )
        except Exception as e:
            logger.error(f"写入审计报告失败: {e}")
