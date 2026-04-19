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

# Prompt 长度预算（字符数），超过此值触发警告日志
_PROMPT_LENGTH_WARNING_THRESHOLD = 8000
_PROMPT_LENGTH_BUDGET = 12000


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

    # LLM 调用重试配置
    _LLM_MAX_RETRIES = 3       # 最大重试次数（含首次调用）
    _LLM_RETRY_BASE_DELAY = 1.0  # 重试基础延迟（秒），指数退避

    def __init__(
        self,
        config: AppConfig,
        client_factory: ClientFactory,
        shared_dir: str = "./shared",
        client: Optional[AsyncOpenAI] = None,
    ):
        """
        初始化 Judge Agent。

        Args:
            config: AppConfig 实例
            client_factory: ClientFactory 实例（调用方拥有，负责生命周期）
            shared_dir: 共享目录根路径
            client: 可选的外部 AsyncOpenAI 客户端（用于真实 LLM 测试）
                     若提供则优先使用，忽略 client_factory
        """
        self._config = config
        self._factory = client_factory
        self._shared_dir = shared_dir
        self._external_client = client  # 外部注入的真实 LLM 客户端
        self._client: Optional[AsyncOpenAI] = None  # 懒初始化：首次 _get_client() 时由 factory 创建

        # 加载 judge 组件配置
        self._judge_comp = get_component_config(config, "judge")

        # 解析器实例
        self._parser = JudgeOutputParser()

        # 加载 Prompt 模板（支持多文件组合）
        self._system_prompt = self._load_combined_system_prompt()
        self._user_template = self._load_prompt("judge_user_template_v2.1.txt")

        if client is not None:
            logger.info(
                f"JudgeAgent 初始化: 使用外部客户端, "
                f"model={self._judge_comp.model}"
            )
        else:
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
        加载并组合系统 Prompt（核心规则 + 输出格式）。

        领域知识不再在此处加载，改为按需动态注入。
        """
        prompts_dir = Path(__file__).resolve().parent.parent / "prompts"

        parts = [
            "judge_system_core.txt",
            "judge_system_output.txt",
        ]
        sections = []
        for part_file in parts:
            path = prompts_dir / part_file
            if path.exists():
                sections.append(path.read_text(encoding="utf-8").strip())

        if not sections:
            # 拆分文件均不存在，fallback 到单文件版本
            logger.warning(
                f"未找到拆分 Prompt 文件 ({', '.join(parts)}), "
                f"尝试加载单文件 fallback"
            )
            try:
                return self._load_prompt("judge_system_v2.1.txt")
            except FileNotFoundError as e:
                raise FileNotFoundError(
                    f"Prompt 文件均不可用: 拆分文件({prompts_dir}) 和 "
                    f"单文件(judge_system_v2.1.txt) 均未找到。"
                    f"请确认 src/prompts/ 目录下存在 Prompt 文件。"
                ) from e

        combined = "\n\n---\n\n".join(sections)
        logger.info(
            f"System Prompt 基础加载: {len(sections)} 个模块, "
            f"总长度 {len(combined)} 字符"
        )
        return combined

    # ------------------------------------------------------------------
    # 领域知识动态加载
    # ------------------------------------------------------------------

    # 领域模块与关键词映射
    _DOMAIN_MODULES: Dict[str, List[str]] = {
        "domain_user_mgmt": [
            "user", "account", "用户", "账户", "密码", "password",
            "权限", "role", "administrator", "添加用户", "删除用户",
        ],
        "domain_redfish": [
            "redfish", "/redfish/v1", "patch", "post", "delete",
            "http", "api", "redfish", "odata",
        ],
        "domain_power": [
            "power", "电源", "开机", "关机", "上下电", "powerstate",
            "poweringon", "poweringoff",
        ],
        "domain_sensor": [
            "sensor", "传感器", "温度", "temperature", "风扇", "fan",
            "health", "reading", "sdr",
        ],
        "domain_ipmi": [
            "ipmi", "ipmitool", "raw", "sel", "fru", "mc info",
            "chassis", "ipmi命令",
        ],
    }

    def _resolve_domain_modules(
        self, execution_record: ExecutionRecord
    ) -> List[str]:
        """
        根据 ExecutionRecord 内容动态决定需要加载的领域知识模块。

        策略：将 execution_record 序列化为文本，对每个模块的关键词
        进行匹配。命中的模块才会被加载，从而减少 prompt 长度。
        """
        try:
            record_text = json.dumps(
                execution_record.model_dump(mode="json"),
                ensure_ascii=False,
            ).lower()
        except Exception:
            # 序列化失败时加载全部模块（保守策略）
            return list(self._DOMAIN_MODULES.keys())

        matched = []
        for module_name, keywords in self._DOMAIN_MODULES.items():
            for kw in keywords:
                if kw.lower() in record_text:
                    matched.append(module_name)
                    break

        logger.info(f"领域知识动态匹配: {matched}")
        return matched

    def _load_domain_prompt(self, module_names: List[str]) -> str:
        """
        按模块名列表加载领域知识，拼接为单段文本。

        Args:
            module_names: 需要加载的领域模块名列表

        Returns:
            拼接后的领域知识文本，模块间用分隔线连接
        """
        if not module_names:
            return ""

        domain_dir = (
            Path(__file__).resolve().parent.parent / "prompts" / "domain"
        )
        sections: List[str] = []
        for name in module_names:
            path = domain_dir / f"{name}.txt"
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()
                if content:
                    sections.append(content)
            else:
                logger.warning(f"领域知识模块不存在: {path}")

        if not sections:
            return ""

        return "\n\n---\n\n".join(sections)

    # ------------------------------------------------------------------
    # ExecutionRecord 精简（IMP-003: 减少 Prompt 总长度）
    # ------------------------------------------------------------------

    # Judge 不需要的步骤字段（降低传输量）
    _STEP_STRIP_FIELDS = frozenset({
        "started_at", "completed_at", "interface_preference",
        "tool", "duration_seconds", "retry_count",
    })
    # Judge 不需要的顶层字段
    _RECORD_STRIP_FIELDS = frozenset({
        "text_summary", "test_case_info",
        "started_at", "completed_at", "schema_version",
    })
    # Evidence 中只保留 Judge 判断必需的字段
    _EVIDENCE_KEEP_FIELDS = frozenset({
        "evidence_type", "content",
    })

    # Evidence content 截断阈值（单条 evidence content 最大字符数）
    # 典型 Redfish 单资源响应 500-2000 chars，1500 覆盖绝大多数场景
    _EVIDENCE_CONTENT_MAX_LEN = 1500

    # consolidated_audit_draft 截断阈值（供 Judge 参考的审计草案最大字符数）
    # 审计草案为参考信息，2000 chars 足以保留核心摘要
    _AUDIT_DRAFT_MAX_LEN = 2000

    # Evidence content 二次截断阈值（渐进精简 Level 2 使用）
    _EV_CONTENT_REDUCED_LEN = 500

    # Level 4 中 actual 值转字符串后的最大长度（超过则截断）
    _ACTUAL_STR_MAX_LEN = 300

    # Prompt Record 预算下限（防止 system+domain 过长导致预算为负）
    _RECORD_BUDGET_FLOOR = 2000

    def _compact_record_for_judge(self, record: ExecutionRecord) -> dict:
        """
        精简 ExecutionRecord，仅保留 Judge 判断所需字段。

        移除策略:
        - 顶层: text_summary / test_case_info / 时间戳 / schema_version（保留 consolidated_audit_draft 供参考）
        - environment: 仅保留 bmc_host / bmc_user（供 Judge 定位设备）
        - 步骤: 移除时间戳、interface_preference、tool、duration 等
        - Evidence: 仅保留 evidence_type + content（去掉 metadata/captured_at/id）
        - Evidence content: 超过 1500 字符时截断并标记 [TRUNCATED]
        - EnvironmentRecoveryAction: 移除时间戳
        - 空 evidence / 空 prerequisites: 整体移除

        Args:
            record: 原始 ExecutionRecord

        Returns:
            精简后的 dict（不修改原始对象）
        """
        data = record.model_dump(mode="json")

        # 顶层字段清理
        for field in self._RECORD_STRIP_FIELDS:
            data.pop(field, None)

        # 精简 environment
        env = data.get("environment")
        if isinstance(env, dict):
            data["environment"] = {
                k: v for k, v in env.items()
                if k in ("bmc_host", "bmc_user")
            }

        # 精简 prerequisites：仅保留非空且非 completed 的项
        prereqs = data.get("prerequisites")
        if isinstance(prereqs, list) and all(
            isinstance(p, dict) and p.get("status") == "completed"
            for p in prereqs
        ):
            data.pop("prerequisites", None)

        # 精简步骤
        for step in data.get("steps", []):
            if not isinstance(step, dict):
                continue
            for field in self._STEP_STRIP_FIELDS:
                step.pop(field, None)

            # 精简 evidence
            ev_list = step.get("evidence")
            if isinstance(ev_list, list):
                compacted = []
                for ev in ev_list:
                    if not isinstance(ev, dict):
                        continue
                    item = {k: v for k, v in ev.items() if k in self._EVIDENCE_KEEP_FIELDS}
                    # 跳过过滤后为空的 evidence 条目（既无 type 也无 content）
                    if not item:
                        continue
                    # 截断过长的 evidence content
                    content = item.get("content")
                    if isinstance(content, str) and len(content) > self._EVIDENCE_CONTENT_MAX_LEN:
                        item["content"] = content[:self._EVIDENCE_CONTENT_MAX_LEN] + "\n[TRUNCATED]"
                        logger.debug(
                            f"Evidence 截断: step={step.get('step_id', '?')}, "
                            f"original={len(content)} -> {self._EVIDENCE_CONTENT_MAX_LEN} chars"
                        )
                    compacted.append(item)
                step["evidence"] = compacted if compacted else []

        # 精简 environment_recovery_actions
        for action in data.get("environment_recovery_actions", []):
            if isinstance(action, dict):
                action.pop("started_at", None)
                action.pop("completed_at", None)
                action.pop("duration_seconds", None)

        # 截断过长的 consolidated_audit_draft（参考信息，无需完整保留）
        draft = data.get("consolidated_audit_draft")
        if isinstance(draft, str) and len(draft) > self._AUDIT_DRAFT_MAX_LEN:
            data["consolidated_audit_draft"] = (
                draft[:self._AUDIT_DRAFT_MAX_LEN] + "\n[TRUNCATED]"
            )
            logger.info(
                f"consolidated_audit_draft 截断: "
                f"{len(draft)} -> {self._AUDIT_DRAFT_MAX_LEN} chars"
            )

        # 移除空的顶层列表字段
        for key in ("environment_recovery_actions",):
            if isinstance(data.get(key), list) and len(data[key]) == 0:
                data.pop(key, None)

        return data

    def _truncate_actual(self, value: Any) -> Any:
        """
        截断 actual 字段值，确保 Level 4 极限精简时不会因单个 actual 过大而超预算。

        - None / 简单类型（bool/int/float/短字符串）: 原样返回
        - 长字符串: 截断到 _ACTUAL_STR_MAX_LEN
        - dict/list: 转为字符串后截断（保留可读性，牺牲 JSON 结构）
        """
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, str):
            if len(value) <= self._ACTUAL_STR_MAX_LEN:
                return value
            return value[:self._ACTUAL_STR_MAX_LEN] + "...[TRUNCATED]"
        # dict/list: 转字符串后截断
        s = json.dumps(value, ensure_ascii=False, default=str)
        if len(s) <= self._ACTUAL_STR_MAX_LEN:
            return value  # 未超限，保持原始结构
        return s[:self._ACTUAL_STR_MAX_LEN] + "...[TRUNCATED]"

    def _progressive_strip_for_budget(self, data: dict, budget: int) -> str:
        """
        渐进式精简 Record 数据，确保 JSON 始终有效且不超过 budget。

        当紧凑 JSON 仍超出 prompt 预算时，按优先级逐步移除低重要性数据，
        而非直接截断字符串（会产生无效 JSON）。

        精简优先级（从低重要性到高）：
        1. 移除 consolidated_audit_draft（参考信息，Judge 可独立判断）
        2. evidence content 截断到 _EV_CONTENT_REDUCED_LEN (500 chars)
        3. evidence 只保留 evidence_type（Judge 仅知证据类型）
        4. 只保留步骤核心判断字段（id/status/expected/actual/error）
        5. 兜底：仅含 execution_id 的最小 JSON

        注意：此方法会修改传入的 data dict（调用方已持有局部副本）。

        Args:
            data: _compact_record_for_judge 返回的精简 dict
            budget: 目标 JSON 最大字符数

        Returns:
            不超过 budget 的有效 JSON 字符串
        """
        # Level 1: 移除 consolidated_audit_draft（参考信息，优先级最低）
        if "consolidated_audit_draft" in data:
            data.pop("consolidated_audit_draft")
            json_str = json.dumps(data, ensure_ascii=False, default=str)
            if len(json_str) <= budget:
                logger.info(
                    "渐进精简 L1: 移除 consolidated_audit_draft, "
                    f"{len(json_str)} chars <= budget={budget}"
                )
                return json_str

        # Level 2: evidence content 截断到更短
        for step in data.get("steps", []):
            if not isinstance(step, dict):
                continue
            for ev in step.get("evidence", []):
                if not isinstance(ev, dict):
                    continue
                content = ev.get("content")
                if isinstance(content, str) and len(content) > self._EV_CONTENT_REDUCED_LEN:
                    # 清理前一轮截断残留的 [TRUNCATED] 标记，避免双重标记
                    if content.endswith("[TRUNCATED]"):
                        content = content.rsplit("\n[TRUNCATED]", 1)[0]
                    ev["content"] = (
                        content[:self._EV_CONTENT_REDUCED_LEN] + "\n[TRUNCATED]"
                    )
        json_str = json.dumps(data, ensure_ascii=False, default=str)
        if len(json_str) <= budget:
            logger.info(
                f"渐进精简 L2: evidence 截断到 {self._EV_CONTENT_REDUCED_LEN} chars, "
                f"{len(json_str)} chars"
            )
            return json_str

        # Level 3: evidence 只保留 evidence_type
        for step in data.get("steps", []):
            if not isinstance(step, dict):
                continue
            step["evidence"] = [
                {"evidence_type": ev.get("evidence_type", "unknown")}
                for ev in step.get("evidence", [])
                if isinstance(ev, dict)
            ]
        json_str = json.dumps(data, ensure_ascii=False, default=str)
        if len(json_str) <= budget:
            logger.info(f"渐进精简 L3: evidence 只保留 type, {len(json_str)} chars")
            return json_str

        # Level 4: 只保留步骤核心判断字段（actual 过长时截断）
        minimal = {
            "execution_id": data.get("execution_id", "unknown"),
            "case_id": data.get("case_id", "unknown"),
            "case_name": data.get("case_name", "unknown"),
            "environment": data.get("environment", {}),
            "steps": [
                {
                    "step_id": s.get("step_id", "unknown"),
                    "status": s.get("status"),
                    "expected": s.get("expected"),
                    "actual": self._truncate_actual(s.get("actual")),
                    "error_message": s.get("error_message"),
                }
                for s in data.get("steps", [])
                if isinstance(s, dict)
            ],
            "_truncation_notice": (
                "Record truncated to fit prompt budget. "
                "Evidence and non-essential fields removed."
            ),
        }
        json_str = json.dumps(minimal, ensure_ascii=False, default=str)
        if len(json_str) <= budget:
            logger.warning(f"渐进精简 L4: 极限精简（核心字段 + actual 截断）, {len(json_str)} chars")
            return json_str

        # 兜底：仅含元信息的最小 JSON（始终不超过 budget）
        logger.error(
            f"Record 即使极限精简仍超预算: {len(json_str)} > {budget}, "
            f"使用最小元信息 JSON"
        )
        return json.dumps({
            "execution_id": data.get("execution_id", "unknown"),
            "_error": "Record too large for prompt budget, judgment may be unreliable",
        }, ensure_ascii=False)

    # ------------------------------------------------------------------
    # 客户端获取
    # ------------------------------------------------------------------

    def _get_client(self) -> AsyncOpenAI:
        """
        获取 Judge LLM 客户端。

        优先级：
        1. 外部注入的客户端（真实测试用）
        2. ClientFactory 创建的客户端
        """
        # 优先使用外部注入的客户端
        if self._external_client is not None:
            return self._external_client

        # 使用 factory 创建的客户端
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
        - Layer 1: System Prompt（核心规则 + 输出格式）
        - Layer 2: Domain Prompt（动态领域知识，按需加载）
        - Layer 3: Task Prompt（当前 Execution Record）

        内置重试机制：最多重试 _LLM_MAX_RETRIES 次（默认 2 次），
        覆盖网络抖动和偶发服务端错误。
        """
        client = self._get_client()

        # 动态加载领域知识
        domain_modules = self._resolve_domain_modules(execution_record)
        domain_text = self._load_domain_prompt(domain_modules)

        # 拼接最终 system prompt（核心规则 + 动态领域知识）
        if domain_text:
            system_prompt = f"{self._system_prompt}\n\n---\n\n# 领域知识（按需加载）\n\n{domain_text}"
        else:
            system_prompt = self._system_prompt

        # 构建 Task Prompt（Layer 3）- 使用精简后的 Record
        compact_data = self._compact_record_for_judge(execution_record)
        record_json = json.dumps(
            compact_data,
            ensure_ascii=False,
            indent=2,
            default=str,
        )

        # IMP-003: Prompt 总长度预算控制
        # 若 record_json 过长，进一步压缩为紧凑 JSON（无缩进）
        system_len = len(system_prompt)
        template_overhead = len(self._user_template) - len("{execution_record_json}")
        record_budget = _PROMPT_LENGTH_BUDGET - system_len - template_overhead

        # 下限保护：确保 record_budget 至少为 _RECORD_BUDGET_FLOOR，
        # 防止 system_prompt + domain_text 过长导致预算为负
        if record_budget < self._RECORD_BUDGET_FLOOR:
            logger.warning(
                f"Prompt 预算紧张: system={system_len}, overhead={template_overhead}, "
                f"budget={record_budget} < floor={self._RECORD_BUDGET_FLOOR}, "
                f"使用下限值"
            )
            record_budget = self._RECORD_BUDGET_FLOOR

        if len(record_json) > record_budget:
            # 紧凑模式：去除缩进
            record_json_compact = json.dumps(
                compact_data, ensure_ascii=False, default=str,
            )
            if len(record_json_compact) <= record_budget:
                record_json = record_json_compact
                logger.info(
                    f"Record JSON 切换紧凑模式: {len(record_json_compact)} chars"
                )
            else:
                # 紧凑仍超预算，渐进式精简（确保 JSON 始终有效）
                record_json = self._progressive_strip_for_budget(
                    compact_data, record_budget
                )
                logger.warning(
                    f"Record JSON 超预算，渐进精简至 {len(record_json)} chars "
                    f"(budget={record_budget})"
                )

        user_message = self._user_template.replace(
            "{execution_record_json}", record_json
        )

        # Prompt 总长度追踪（IMP-003）
        total_prompt_len = len(system_prompt) + len(user_message)
        logger.info(
            f"调用 Judge 模型: model={self._judge_comp.model}, "
            f"system_prompt={len(system_prompt)} chars, "
            f"domain_modules={domain_modules}, "
            f"record_json={len(record_json)} chars, "
            f"total_prompt={total_prompt_len} chars"
        )
        if total_prompt_len > _PROMPT_LENGTH_WARNING_THRESHOLD:
            logger.warning(
                f"Prompt 总长度 {total_prompt_len} chars "
                f"超过警告阈值 {_PROMPT_LENGTH_WARNING_THRESHOLD} chars"
            )

        # 重试调用（覆盖网络抖动和偶发服务端错误）
        last_error = None
        for attempt in range(1, self._LLM_MAX_RETRIES + 1):
            try:
                response = await client.chat.completions.create(
                    model=self._judge_comp.model,
                    messages=[
                        {"role": "system", "content": system_prompt},
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

            except Exception as e:
                last_error = e
                if attempt < self._LLM_MAX_RETRIES:
                    wait = self._LLM_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                    logger.warning(
                        f"Judge 模型调用第 {attempt} 次失败: {e}, "
                        f"{wait:.1f}s 后重试..."
                    )
                    await asyncio.sleep(wait)
                else:
                    logger.error(
                        f"Judge 模型调用重试 {self._LLM_MAX_RETRIES} 次后仍失败: {e}"
                    )

        raise last_error  # type: ignore[misc]

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
