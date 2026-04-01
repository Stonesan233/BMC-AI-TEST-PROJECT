# -*- coding: utf-8 -*-
"""
BMC CLI 命令文档 DOCX 解析器

适用文档: Atlas 系列 iBMC 用户指南 (.docx 格式)

文档特征:
  - 所有段落均为 "Normal" 样式，标题仅通过字号区分
  - 字号层级: 72pt (章) > 18pt (子节) > 16pt (命令标题) > 13pt (标签) > 10.5pt (正文)
  - 命令标题 (16pt) 有时为"合并段落": 标题+功能描述在同一段落 (run 级字号区分)
  - 标签段 (13pt): "命令功能", "命令格式", "参数说明", "使用指南", "使用实例"
  - 命令示例存储在表格中 (通常 1 行 x 1 列，含命令行输出)
  - 参数说明存储在表格中 (通常 N 行 x 2~3 列)
  - 大量噪声表格 (页眉页脚、文档版本信息)

分块策略:
  - 以单个完整 CLI 命令为最小原子 chunk (chunk_type="command")
  - 一个 chunk 包含: 命令标题 + 功能描述 + 语法 + 参数表 + 使用指南 + 使用实例
  - 使用 CommandCollector 收集每个命令的完整信息
  - 子节标题 (18pt) 单独生成 chunk (chunk_type="section")
  - 合并段落 (标题 + 描述在同一行) 通过 run 级字号解析

Metadata:
  - doc_type, file_name, chunk_id, chunk_type, section (继承自 BaseParser)
  - command_name: 主命令名 (如 "ipmcset", "ipmcget")
  - subcommand: 子命令 (如 "-d adduser")
  - syntax: 完整命令格式
  - parameters: 参数说明 (截断)
  - example: 使用实例 (截断)
  - interactive: 是否需要交互输入 (true/false)
  - chinese_name / english_name: 命令中英文名
  - description: 功能描述 (用于检索)

同义词注入:
  - 在 chunk text 中主动补充同义词，提高检索召回率
  - 例: "添加用户" -> "adduser", "创建用户", "new user", "新增用户"

依赖: python-docx>=0.8.11
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from src.rag.parsers.base_parser import BaseParser

logger = logging.getLogger("rag.parser.docx_cli")

# ============================================================================
# 常量
# ============================================================================

# ---------------------------------------------------------------------------
# 字号阈值 (points)
# ---------------------------------------------------------------------------
FONT_SIZE_CHAPTER = 72.0        # >= 72pt: 章标题 (如 "7 CLI 介绍")
FONT_SIZE_SECTION = 18.0        # >= 18pt: 子节标题 (如 "7.3 iBMC 命令")
FONT_SIZE_COMMAND = 16.0        # >= 16pt: 命令标题 (如 "7.3.2 设置IPv4信息（ipaddr）")
FONT_SIZE_LABEL = 12.5          # >= 12.5pt: 标签 ("命令功能", "参数说明" 等)
# < 12.5pt: 正文

# ---------------------------------------------------------------------------
# 噪声过滤
# ---------------------------------------------------------------------------
_HEADER_FOOTER_KEYWORDS = [
    "文档版本",
    "用户指南",
    "iBMC用户指南",
    "华为技术有限公司",
    "版权所有",
    "Atlas 900",
]
_TOC_DOT_PATTERN = re.compile(r"\.{4,}")

# ---------------------------------------------------------------------------
# 需要跳过的非命令章节 (在 CLI 章节 7 内)
# ---------------------------------------------------------------------------
_SKIP_SECTION_TITLES = {
    "7.1 CLI 说明",
    "7.2 登录CLI",
    "7.1.1 格式说明",
    "7.1.2 帮助",
    "7.2.1 通过管理网口登录CLI",
    "7.2.2 通过串口登录CLI",
}

# ---------------------------------------------------------------------------
# 标签检测
# ---------------------------------------------------------------------------
_LABELS = {
    "命令功能": "function",
    "命令格式": "syntax",
    "参数说明": "parameters",
    "使用指南": "usage",
    "使用实例": "example",
    "前提条件": "prerequisite",
    "操作步骤": "steps",
    "后续处理": "followup",
}

# ---------------------------------------------------------------------------
# 命令提取正则
# ---------------------------------------------------------------------------
# 匹配: ipmcget, ipmcset, ipmitool, clearcmos, help, who,退出
_CLI_COMMAND_PATTERN = re.compile(
    r"\b(ipmcset|ipmcget|ipmitool|help|who|clearcmos|"
    r"mcinfo|smcinfo|ping|ifconfig|top|free|df|ps|kill|cat|cd|ls|rm|"
    r"tar|unzip|reboot|poweroff|exit|logout)\b",
    re.IGNORECASE,
)
# 匹配子命令: -d xxx, -t xxx, -v xxx
_SUBCOMMAND_PATTERN = re.compile(r"-[dtv]\s+(\S+)")
# 匹配完整语法行
_SYNTAX_PATTERN = re.compile(
    r"^(ipmcset|ipmcget|ipmitool)\s+.*$",
    re.MULTILINE,
)

# ---------------------------------------------------------------------------
# 交互式检测关键词
# ---------------------------------------------------------------------------
_INTERACTIVE_KEYWORDS = [
    "Input your password",
    "Password:",
    "Confirm password:",
    "New password:",
    "请输入",
    "请确认",
    "continue?",
    "(y/n)",
    "[y/N]",
    "[Y/n]",
]

# 使用指南中的交互式关键词 (描述性文本)
_INTERACTIVE_USAGE_KEYWORDS = [
    "需要输入当前管理员",
    "需要输入密码",
    "需要输入用户",
    "交互式",
    "密码输入",
    "需要确认",
    "输入新密码",
]

# ---------------------------------------------------------------------------
# 同义词映射 (命令名/操作 -> 同义词)
# ---------------------------------------------------------------------------
_SYNONYM_MAP = {
    "添加用户": "adduser 创建用户 新增用户 new user 添加账号 创建账号",
    "删除用户": "deluser 删除账号 移除用户 remove user 删除账号",
    "修改密码": "password 改密码 更改密码 change password 设置密码 set password",
    "设置权限": "privilege 权限 角色 permission role 设置角色",
    "查询用户": "userlist list 用户列表 查看用户 show user list users",
    "锁定用户": "lock 禁用用户 block user disable user",
    "解锁用户": "unlock 解除锁定 enable user unblock user",
    "查询版本": "version 版本信息 固件版本 firmware version",
    "设置IP": "ipaddr 设置IP地址 set ip address 配置网络",
    "查询IP": "ipinfo IP信息 show ip address 查看IP",
    "重启": "reboot reset 重置 重新启动 restart",
    "关机": "poweroff 关闭电源 power off shutdown",
    "开机": "poweron 打开电源 power on start",
    "查询传感器": "sensor 传感器列表 查看传感器 sensor list",
    "查询日志": "sel 系统日志 event log 系统事件",
    "查询电源": "power 电源状态 查看电源 power status",
    "设置启动": "bootdevice 启动设备 启动顺序 boot order boot device",
    "配置NTP": "ntp 时间同步 time sync ntp server",
    "导入配置": "config import 导入 import config",
    "导出配置": "config export 导出 export config",
    "恢复出厂": "factoryreset 恢复出厂 reset factory default",
}


# ============================================================================
# 命令收集器 (CommandCollector)
# ============================================================================

@dataclass
class CommandCollector:
    """
    单个 CLI 命令的收集器。

    在主循环中，当检测到新命令标题 (16pt) 时，flush 当前收集器生成 chunk，
    然后重置收集器开始收集下一条命令。
    """
    title: str = ""
    chinese_name: str = ""
    english_name: str = ""
    function_lines: List[str] = field(default_factory=list)
    syntax_lines: List[str] = field(default_factory=list)
    parameter_tables: List[str] = field(default_factory=list)
    usage_lines: List[str] = field(default_factory=list)
    example_tables: List[str] = field(default_factory=list)
    prerequisite_lines: List[str] = field(default_factory=list)
    steps_lines: List[str] = field(default_factory=list)
    current_label: str = ""
    is_interactive: bool = False
    command_name: str = ""
    subcommand: str = ""

    # -- 属性 --

    @property
    def is_collecting(self) -> bool:
        """是否正在收集一个命令."""
        return len(self.title) > 0

    @property
    def has_real_content(self) -> bool:
        """是否收集到了除标题外的实质内容."""
        return bool(
            self.function_lines
            or self.syntax_lines
            or self.parameter_tables
            or self.usage_lines
            or self.example_tables
        )

    # -- 操作 --

    def set_label(self, text: str) -> None:
        """更新当前标签."""
        self.current_label = ""
        for label_text, label_key in _LABELS.items():
            if label_text in text:
                self.current_label = label_key
                return

    def add_text(self, text: str) -> None:
        """根据当前标签将文本归入对应部分."""
        label = self.current_label
        if label == "function":
            self.function_lines.append(text)
        elif label == "syntax":
            self.syntax_lines.append(text)
        elif label == "usage":
            self.usage_lines.append(text)
        elif label == "prerequisite":
            self.prerequisite_lines.append(text)
        elif label == "steps":
            self.steps_lines.append(text)
        else:
            # 无标签时归入使用指南 (通用文本)
            self.usage_lines.append(text)

    def add_table(self, table_text: str) -> None:
        """将表格内容归入当前命令."""
        label = self.current_label
        if label == "parameters":
            self.parameter_tables.append(table_text)
        elif label == "example":
            self.example_tables.append(table_text)
            # 检查是否包含交互式关键词
            if not self.is_interactive:
                for kw in _INTERACTIVE_KEYWORDS:
                    if kw.lower() in table_text.lower():
                        self.is_interactive = True
                        break
        elif label == "syntax":
            # 有时命令格式也会以表格形式出现
            self.syntax_lines.append(table_text)
        else:
            self.example_tables.append(table_text)

    def update_command_info(self, text: str) -> None:
        """从文本中提取命令名和子命令，并检测交互式特征."""
        if not self.command_name:
            m = _CLI_COMMAND_PATTERN.search(text)
            if m:
                self.command_name = m.group(1).lower()
        if not self.subcommand:
            m = _SUBCOMMAND_PATTERN.search(text)
            if m:
                self.subcommand = f"-d {m.group(1)}"

        # 检查交互式 (示例输出中的提示符)
        if not self.is_interactive:
            for kw in _INTERACTIVE_KEYWORDS:
                if kw.lower() in text.lower():
                    self.is_interactive = True
                    break

        # 检查交互式 (使用指南中的描述性文本)
        if not self.is_interactive:
            for kw in _INTERACTIVE_USAGE_KEYWORDS:
                if kw in text:
                    self.is_interactive = True
                    break

    def reset(self) -> None:
        """重置收集器."""
        self.title = ""
        self.chinese_name = ""
        self.english_name = ""
        self.function_lines.clear()
        self.syntax_lines.clear()
        self.parameter_tables.clear()
        self.usage_lines.clear()
        self.example_tables.clear()
        self.prerequisite_lines.clear()
        self.steps_lines.clear()
        self.current_label = ""
        self.is_interactive = False
        self.command_name = ""
        self.subcommand = ""


# ============================================================================
# DocxCLIParser
# ============================================================================

class DocxCLIParser(BaseParser):
    """
    BMC CLI 命令文档 DOCX 解析器.

    通过遍历 XML body 子元素保持段落与表格的交错顺序，
    基于字号检测标题层级，以单个完整命令为原子单位分块。

    核心特性:
      - CommandCollector 封装命令生命周期管理
      - 标签状态机: 命令功能 -> 命令格式 -> 参数说明 -> 使用指南 -> 使用实例
      - 合并段落处理 (标题 + 描述在同一段落)
      - 交互式命令自动检测
      - 同义词注入提升检索召回
    """

    async def parse(self, file_path: str) -> List[Dict[str, Any]]:
        """
        解析 CLI DOCX 文档，返回命令级分块列表。

        Args:
            file_path: DOCX 文件路径

        Returns:
            分块列表，每个元素包含 text 和 metadata
        """
        path = Path(file_path)
        if not path.exists():
            raise FileNotFoundError(f"文件不存在: {file_path}")
        if path.suffix.lower() != ".docx":
            raise ValueError(f"仅支持 .docx 文件，收到: {path.suffix}")

        logger.info(f"开始解析: {path.name}")
        doc = Document(str(path))
        file_name = path.name

        chunks: List[Dict[str, Any]] = []
        chunk_counter: int = 0

        # 解析状态
        current_section: str = ""
        in_cli_section: bool = False
        collector = CommandCollector()

        # ------------------------------------------------------------------
        # 主循环
        # ------------------------------------------------------------------
        for elem_type, elem in self._iter_body_elements(doc):

            if elem_type == "paragraph":
                para = Paragraph(elem, doc)
                level, text = self._classify_paragraph(para)

                if level == "empty":
                    continue

                # ---- 检测是否进入 CLI 章节 (第7章) ----
                if level == "chapter":
                    if self._is_cli_section_start(text):
                        in_cli_section = True
                        chunk_counter += 1
                        chunks.append(
                            self._build_chunk(
                                text=f"章节: {text}",
                                file_name=file_name,
                                chunk_id=f"chapter_{chunk_counter:05d}",
                                chunk_type="chapter",
                                section=text,
                                doc_type="cli",
                            )
                        )
                        continue
                    # 检测是否离开 CLI 章节 (第8章及之后)
                    if in_cli_section and self._is_cli_section_end(text):
                        # flush 最后一条命令
                        chunk_counter = self._flush_collector(
                            collector, chunks, file_name,
                            current_section, chunk_counter,
                        )
                        collector.reset()
                        in_cli_section = False
                        continue
                    continue

                if not in_cli_section:
                    continue

                # ---- 子节标题 (18pt): 如 "7.3 iBMC 命令" ----
                if level == "section":
                    # flush 当前命令
                    chunk_counter = self._flush_collector(
                        collector, chunks, file_name,
                        current_section, chunk_counter,
                    )
                    collector.reset()
                    current_section = text
                    chunk_counter += 1
                    chunks.append(
                        self._build_chunk(
                            text=f"章节: {text}",
                            file_name=file_name,
                            chunk_id=f"section_{chunk_counter:05d}",
                            chunk_type="section",
                            section=text,
                            doc_type="cli",
                        )
                    )
                    logger.debug(f"子节标题: {text[:80]}")
                    continue

                # ---- 命令标题 (16pt) ----
                if level == "command":
                    # 检查是否为需要跳过的非命令子节
                    if self._should_skip_title(text):
                        chunk_counter = self._flush_collector(
                            collector, chunks, file_name,
                            current_section, chunk_counter,
                        )
                        collector.reset()
                        continue

                    # flush 上一个完整命令
                    chunk_counter = self._flush_collector(
                        collector, chunks, file_name,
                        current_section, chunk_counter,
                    )
                    collector.reset()

                    # 解析命令标题
                    self._parse_command_title(text, collector)

                    # 检查是否为合并段落 (标题内含功能描述)
                    merged = self._parse_merged_paragraph(para)
                    if merged:
                        for merge_level, merge_text in merged:
                            if merge_level == "function":
                                collector.function_lines.append(merge_text)
                            elif merge_level == "syntax":
                                collector.syntax_lines.append(merge_text)
                                collector.update_command_info(merge_text)
                            elif merge_level == "body":
                                collector.usage_lines.append(merge_text)

                    logger.debug(f"新命令: {collector.title[:80]}")
                    continue

                # ---- 标签 (13pt): "命令功能", "命令格式" 等 ----
                if level == "label":
                    # 可能标签文本本身包含命令格式
                    collector.set_label(text)
                    # 有些标签行同时包含命令格式: "命令格式\nipmcset -d xxx"
                    syntax_in_label = self._extract_syntax_from_text(text)
                    if syntax_in_label:
                        collector.syntax_lines.append(syntax_in_label)
                        collector.update_command_info(syntax_in_label)
                    continue

                # ---- 正文 ----
                if level in ("body", "note"):
                    if self._is_noise_paragraph(text):
                        continue
                    collector.add_text(text)
                    collector.update_command_info(text)

            elif elem_type == "table":
                if not in_cli_section:
                    continue
                table = Table(elem, doc)

                if self._is_noise_table(table):
                    continue

                table_text = self._format_table(table)
                collector.add_table(table_text)
                collector.update_command_info(table_text)

        # ------------------------------------------------------------------
        # flush 最后一条命令
        # ------------------------------------------------------------------
        chunk_counter = self._flush_collector(
            collector, chunks, file_name,
            current_section, chunk_counter,
        )

        logger.info(f"解析完成: {file_name}, 共 {len(chunks)} 个 chunks")
        return chunks

    # ======================================================================
    # flush 辅助
    # ======================================================================

    def _flush_collector(
        self,
        collector: CommandCollector,
        chunks: List[Dict[str, Any]],
        file_name: str,
        section: str,
        chunk_counter: int,
    ) -> int:
        """将 collector 中的命令数据 flush 为一个 chunk."""
        if not collector.is_collecting:
            return chunk_counter

        title = collector.title.strip()
        if len(title) < 5:
            return chunk_counter

        chunk_counter += 1

        # -- 构建 text --
        parts: List[str] = [f"命令: {title}"]

        # 同义词注入
        synonyms = self._get_synonyms(title, collector)
        if synonyms:
            parts.append(f"同义词: {synonyms}")

        func = "\n".join(collector.function_lines).strip()
        if func:
            parts.append(f"功能描述:\n{func}")

        syntax = "\n".join(collector.syntax_lines).strip()
        if syntax:
            parts.append(f"命令格式:\n{syntax}")

        if collector.parameter_tables:
            parts.append("参数说明:\n" + "\n\n".join(collector.parameter_tables))

        usage = "\n".join(collector.usage_lines).strip()
        if usage:
            parts.append(f"使用指南:\n{usage}")

        if collector.example_tables:
            parts.append("使用实例:\n" + "\n\n".join(collector.example_tables))

        if collector.is_interactive:
            parts.append("注意: 此命令为交互式命令，执行后需要输入密码或确认信息。")

        full_text = "\n\n".join(parts)
        if len(full_text.strip()) < 10:
            return chunk_counter

        # -- 构建 metadata --
        extra: Dict[str, Any] = {}

        if collector.command_name:
            extra["command_name"] = collector.command_name
        else:
            # 从标题提取
            cn = self._extract_command_name_from_title(title)
            if cn:
                extra["command_name"] = cn

        if collector.subcommand:
            extra["subcommand"] = collector.subcommand
        else:
            sc = self._extract_subcommand_from_title(title)
            if sc:
                extra["subcommand"] = sc

        if syntax:
            extra["syntax"] = syntax[:500]

        if collector.parameter_tables:
            params_text = "\n".join(collector.parameter_tables)
            extra["parameters"] = params_text[:500]

        if collector.example_tables:
            example_text = "\n".join(collector.example_tables)
            extra["example"] = example_text[:500]

        extra["interactive"] = collector.is_interactive

        if collector.chinese_name:
            extra["chinese_name"] = collector.chinese_name
        if collector.english_name:
            extra["english_name"] = collector.english_name

        desc = func or title
        extra["description"] = desc[:200]

        section_path = (
            f"{section} > {title}"
            if section and title
            else (title or section)
        )

        chunks.append(
            self._build_chunk(
                text=full_text,
                file_name=file_name,
                chunk_id=f"cmd_{chunk_counter:05d}",
                chunk_type="command",
                section=section_path[:500],
                doc_type="cli",
                **extra,
            )
        )

        logger.debug(
            f"Flush: [cmd_{chunk_counter:05d}] "
            f"cmd={extra.get('command_name', '-')} "
            f"sub={extra.get('subcommand', '-')} "
            f"interactive={collector.is_interactive} "
            f"| {title[:60]}"
        )
        return chunk_counter

    # ======================================================================
    # 内部方法
    # ======================================================================

    @staticmethod
    def _iter_body_elements(doc: Document):
        """遍历文档 body 的直接子元素，保持段落和表格的交错顺序."""
        body = doc.element.body
        for child in body:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "p":
                yield "paragraph", child
            elif tag == "tbl":
                yield "table", child

    @staticmethod
    def _get_font_size_pt(para: Paragraph) -> Optional[float]:
        """提取段落的字号 (points), 优先 run font.size, 回退 style font.size."""
        for run in para.runs:
            if run.text.strip() and run.font.size:
                return run.font.size.pt
        if para.style and para.style.font and para.style.font.size:
            return para.style.font.size.pt
        return None

    def _classify_paragraph(self, para: Paragraph) -> Tuple[str, str]:
        """
        根据字号对段落分类.

        Returns:
            (level, text) level 为:
              "chapter"    - 章标题 (>= 72pt)
              "section"    - 子节标题 (>= 18pt)
              "command"    - 命令标题 (>= 16pt)
              "label"      - 标签 (>= 12.5pt)
              "body"       - 正文 (10.5pt 等)
              "note"       - 备注/说明 (9.5pt 及以下)
              "empty"      - 空段落
        """
        text = para.text.strip()
        if not text:
            return "empty", text

        size_pt = self._get_font_size_pt(para)
        if size_pt is None:
            return "body", text

        if size_pt >= FONT_SIZE_CHAPTER:
            return "chapter", text
        elif size_pt >= FONT_SIZE_SECTION:
            return "section", text
        elif size_pt >= FONT_SIZE_COMMAND:
            return "command", text
        elif size_pt >= FONT_SIZE_LABEL:
            return "label", text
        elif size_pt >= 9.5:
            return "body", text
        else:
            return "note", text

    @staticmethod
    def _is_cli_section_start(text: str) -> bool:
        """检测是否为 CLI 章节起始标题."""
        # "7 CLI 介绍" 或 "7CLI介绍" 等
        text_stripped = text.replace(" ", "")
        return bool(re.match(r"^7[Cc][Ll][Ii]", text_stripped))

    @staticmethod
    def _is_cli_section_end(text: str) -> bool:
        """检测是否离开 CLI 章节 (第8章及之后)."""
        text_stripped = text.replace(" ", "")
        return bool(re.match(r"^(8|9|10|11)", text_stripped))

    @staticmethod
    def _should_skip_title(text: str) -> bool:
        """检测是否为需要跳过的非命令标题 (如格式说明、登录方式)."""
        for skip_title in _SKIP_SECTION_TITLES:
            if skip_title in text:
                return True
        return False

    @staticmethod
    def _parse_command_title(text: str, collector: CommandCollector) -> None:
        """
        解析命令标题，提取编号、中文名、英文名。

        标题格式示例:
          "7.3.2 设置iBMC 管理网口的IPv4 信息（ipaddr）"
          "7.9.1 查询所有用户信息（userlist/list）"
          "7.9.2 添加新用户（adduser）"
        """
        collector.title = text.strip()

        # 提取中英文: "编号 中文名（英文名）"
        m = re.match(
            r"^(?:\d+[\.\d]*\s+)?"
            r"([^\(（]+?)"
            r"(?:[\(（](.+?)[\)）])?\s*$",
            text.strip(),
        )
        if m:
            chinese = m.group(1).strip() if m.group(1) else ""
            english = m.group(2).strip() if m.group(2) else ""

            # 清理中文部分 (去掉编号前缀)
            chinese_clean = re.sub(r"^\d+[\.\d]*\s+", "", chinese).strip()
            collector.chinese_name = chinese_clean
            collector.english_name = english

            # 从英文名提取子命令
            if english and not collector.subcommand:
                # "userlist/list" -> subcommand 提取
                # "adduser" -> 作为 subcommand
                # "ipaddr" -> 作为 subcommand
                collector.subcommand = f"-d {english.split('/')[0]}"
        else:
            collector.chinese_name = text.strip()

    @staticmethod
    def _parse_merged_paragraph(para: Paragraph) -> Optional[List[Tuple[str, str]]]:
        """
        解析合并段落: 标题(16pt) + 描述(10.5pt) 在同一段落。

        当检测到段落的 run 之间存在 >=16pt 和 <16pt 的 run 时，
        视为合并段落，按 run 字号分组。

        Returns:
            [(level, text), ...] 或 None (非合并段落)
        """
        runs = para.runs
        if len(runs) < 2:
            return None

        # 检查是否存在 >=16pt 和 <16pt 的 run
        has_title_size = False
        has_body_size = False
        for run in runs:
            if not run.text.strip():
                continue
            if run.font.size:
                pt = run.font.size.pt
                if pt >= FONT_SIZE_COMMAND:
                    has_title_size = True
                elif pt < FONT_SIZE_COMMAND:
                    has_body_size = True

        if not (has_title_size and has_body_size):
            return None

        # 按字号分组
        groups: List[Tuple[str, str]] = []
        current_texts: List[str] = []
        current_level = ""

        for run in runs:
            text = run.text.strip()
            if not text:
                continue
            pt = run.font.size.pt if run.font.size else 10.5

            if pt >= FONT_SIZE_COMMAND:
                level = "title"
            elif pt >= FONT_SIZE_LABEL:
                level = "syntax"
            else:
                level = "body"

            if level != current_level and current_texts:
                groups.append((current_level, " ".join(current_texts)))
                current_texts = []
            current_level = level
            current_texts.append(text)

        if current_texts:
            groups.append((current_level, " ".join(current_texts)))

        # 只返回非 title 部分 (title 已在外层处理)
        return [(lvl, txt) for lvl, txt in groups if lvl != "title"]

    @staticmethod
    def _extract_syntax_from_text(text: str) -> Optional[str]:
        """从文本中提取命令格式行."""
        for line in text.split("\n"):
            line = line.strip()
            if _SYNTAX_PATTERN.match(line):
                return line
        return None

    @staticmethod
    def _extract_command_name_from_title(title: str) -> Optional[str]:
        """从标题中提取命令名."""
        m = _CLI_COMMAND_PATTERN.search(title)
        if m:
            return m.group(1).lower()
        # 尝试从英文名提取 (括号中的内容)
        m = re.search(r"[\(（]([a-zA-Z]+)[\)）]", title)
        if m:
            name = m.group(1).lower()
            if name not in ("get", "set", "list", "and", "the", "for", "cli"):
                return name
        return None

    @staticmethod
    def _extract_subcommand_from_title(title: str) -> Optional[str]:
        """从标题中提取子命令标识."""
        # 从英文名提取: 如 "adduser", "ipaddr", "ipinfo"
        m = re.search(r"[\(（]([a-zA-Z]+(?:/[a-zA-Z]+)?)[\)）]", title)
        if m:
            subcmd = m.group(1).lower()
            if subcmd not in ("cli", "get", "set"):
                return f"-d {subcmd.split('/')[0]}"
        # 从括号中提取更复杂的: "userlist/list" -> "-d userlist"
        m = re.search(r"[\(（]((?:\w|-)+\s+(?:\w|-)+)[\)）]", title)
        if m:
            return m.group(1).strip()
        return None

    @staticmethod
    def _get_synonyms(title: str, collector: CommandCollector) -> str:
        """根据命令内容生成同义词文本."""
        synonyms_set: Set[str] = set()

        # 从标题和描述中匹配同义词
        search_text = f"{title} {' '.join(collector.function_lines)}"

        for keyword, synonym_str in _SYNONYM_MAP.items():
            if keyword in search_text:
                for s in synonym_str.split():
                    synonyms_set.add(s)

        # 从英文名添加
        if collector.english_name:
            synonyms_set.add(collector.english_name.lower())

        # 从命令名添加
        if collector.command_name:
            synonyms_set.add(collector.command_name)

        # 过滤掉过短的
        synonyms = [s for s in synonyms_set if len(s) > 1]
        return " ".join(sorted(synonyms)) if synonyms else ""

    @staticmethod
    def _is_noise_table(table: Table) -> bool:
        """检测噪声表格 (页眉页脚 / TOC / 版本信息)."""
        num_rows = len(table.rows)
        if num_rows == 0:
            return True

        all_text = " ".join(
            cell.text.strip()
            for row in table.rows
            for cell in row.cells
        )

        # 单行或双行 + 页眉页脚关键词
        if num_rows <= 2:
            for kw in _HEADER_FOOTER_KEYWORDS:
                if kw in all_text:
                    return True

        # TOC 条目 (连续省略号)
        if _TOC_DOT_PATTERN.search(all_text):
            return True

        # 版本信息表格
        if "文档版本" in all_text and num_rows <= 3:
            return True

        # X-Auth-Token / HTTP 头部表格
        if "X-Auth-Token" in all_text and num_rows <= 3:
            return True

        return False

    @staticmethod
    def _is_noise_paragraph(text: str) -> bool:
        """检测页眉页脚噪声段落."""
        for kw in _HEADER_FOOTER_KEYWORDS:
            if kw in text:
                return True
        # 跳过文档版本行
        if text.startswith("文档版本") or text.startswith("发布日期"):
            return True
        return False

    @staticmethod
    def _format_table(table: Table) -> str:
        """将表格转换为可读文本."""
        rows_text: List[str] = []
        seen: Set[int] = set()

        for row in table.rows:
            cells_text: List[str] = []
            for cell in row.cells:
                tc_id = id(cell._tc)
                if tc_id in seen:
                    continue
                seen.add(tc_id)
                cells_text.append(cell.text.strip().replace("\n", " "))
            if cells_text:
                rows_text.append(" | ".join(cells_text))

        return "\n".join(rows_text)
