# -*- coding: utf-8 -*-
"""
IPMI 接口文档 DOCX 解析器 (v2 重构版)

适用文档: 华为 iBMC IPMI 接口说明文档 (.docx 格式)

文档特征:
  - 所有段落均为 "Normal" 样式，标题仅通过字号区分
  - 字号层级: 22pt/72pt (章) > 18pt (命令节) > 16pt (子节) > 13pt (标签) > 10.5pt (正文)
  - 段落和表格在文档中交错排列，必须遍历 XML body 子元素保持顺序
  - 命令节内部结构: 标题 -> 命令功能描述 -> 参数表(含 NetFn/CMD) -> 响应说明 -> 响应表

分块策略 (v2):
  - 以单个完整 IPMI 命令为最小原子 chunk
  - 一个 chunk 包含: 命令标题 + 完整功能描述 + 参数表 + 响应说明 + 响应表 + 使用示例
  - 使用状态机精确跟踪命令生命周期:
      TITLE -> DESCRIPTION -> PARAM_TABLE -> RESPONSE_TABLE -> EXAMPLE -> TITLE(下一条)
  - 命令标题支持多行续行合并
  - 章标题单独生成 chunk (用于检索时的上下文定位)

NetFn/CMD 提取 (v2):
  - 从参数表前几行提取 (主要来源)
  - 从命令标题文本中提取 (如 "NetFn=30h CMD=93h")
  - 从正文描述中提取 (如 "网络功能码30h 命令字92h")
  - 支持 "30h", "0x30", "NetFn 30h" 等多种写法

Metadata (v2):
  - doc_type, file_name, chunk_id, chunk_type, section (继承自 BaseParser)
  - command_code: 如 "30h 93h"
  - netfn: 如 "30h"
  - cmd: 如 "93h"
  - english_name: 如 "Get CPU Reading"
  - chinese_name: 如 "获取CPU读数"
  - full_command: 完整命令标题
  - table_type: "parameter" / "response" / "both"
  - description: 命令功能描述 (用于检索)

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

logger = logging.getLogger("rag.parser.docx_ipmi")

# ============================================================================
# 常量
# ============================================================================

# ---------------------------------------------------------------------------
# 字号阈值 (points)
# ---------------------------------------------------------------------------
FONT_SIZE_CHAPTER = 18.0       # >= 18pt: 章/节标题
FONT_SIZE_SUBSECTION = 16.0    # >= 16pt: 子节标题
FONT_SIZE_LABEL = 12.5         # >= 12.5pt: 标签 ("命令功能", "参数说明" 等)
# < 12.5pt: 正文

# ---------------------------------------------------------------------------
# 噪声过滤
# ---------------------------------------------------------------------------
_HEADER_FOOTER_KEYWORDS = [
    "文档版本",
    "iBMC IPMI 接口参考",
    "iBMC IPMI 接口说明",
    "华为技术有限公司",
    "版权所有",
]
_TOC_DOT_PATTERN = re.compile(r"\.{4,}")

# ---------------------------------------------------------------------------
# 需要跳过的非命令章节
# ---------------------------------------------------------------------------
_SKIP_SECTIONS = {"目录", "前言", "安全声明", "修改记录"}

# ---------------------------------------------------------------------------
# NetFn/CMD 提取正则
# ---------------------------------------------------------------------------
# 匹配: 30h, 93H, 0x30, 0Ch
_HEX_SUFFIX_PATTERN = re.compile(r"\b([0-9A-Fa-f]{1,2})[hH]\b")
_HEX_PREFIX_PATTERN = re.compile(r"\b0[xX]([0-9A-Fa-f]{1,2})\b")
# 匹配标题/正文中的: NetFn 30h, CMD 93h, NetFn=30h
_NETFN_EXPLICIT = re.compile(
    r"[Nn]et[Ff]n\s*[=:：]?\s*([0-9A-Fa-f]{1,2})[hH]?", re.IGNORECASE
)
_CMD_EXPLICIT = re.compile(
    r"\bCMD\s*[=:：]?\s*([0-9A-Fa-f]{1,2})[hH]?", re.IGNORECASE
)
# 匹配标题/正文中的: 网络功能码30h, 命令字92h
_NETFN_CN = re.compile(r"网络功能码\s*([0-9A-Fa-f]{1,2})[hH]")
_CMD_CN = re.compile(r"命令字\s*([0-9A-Fa-f]{1,2})[hH]")
# 从命令标题中提取中文名和英文名
# 格式: "3.1 获取BusinessPort 的BDF 和MAC 信息（Get BusinessPort's BDF and MAC Info）"
_TITLE_CN_EN = re.compile(
    r"^(?:\d+[\.\d]*\s+)?"          # 可选的编号 (3.1)
    r"([^\(（]+?)"                  # 中文名 (懒惰匹配)
    r"(?:[\(（](.+?)[\)）])?$"       # 可选的英文名 (括号内)
)

# ---------------------------------------------------------------------------
# 标签 -> 表格类型映射
# ---------------------------------------------------------------------------
_LABEL_TABLE_TYPE_MAP = [
    (["响应", "response"], "response"),
    (["参数", "parameter"], "parameter"),
    (["使用示例", "示例", "example"], "example"),
]


# ============================================================================
# 命令收集器 (CommandCollector)
# ============================================================================

@dataclass
class CommandCollector:
    """
    单个 IPMI 命令的收集器。

    在主循环中，当检测到新命令标题时，flush 当前收集器生成 chunk，
    然后重置收集器开始收集下一条命令。
    """
    title_parts: List[str] = field(default_factory=list)
    description_lines: List[str] = field(default_factory=list)
    param_tables: List[str] = field(default_factory=list)
    response_tables: List[str] = field(default_factory=list)
    example_parts: List[str] = field(default_factory=list)
    netfn: Optional[str] = None
    cmd: Optional[str] = None
    current_label: str = ""
    current_table_type: str = ""  # "parameter" | "response" | "example" | ""

    # -- 属性 --

    @property
    def full_title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def is_collecting(self) -> bool:
        """是否正在收集一个命令 (至少有标题)."""
        return len(self.title_parts) > 0

    @property
    def has_real_content(self) -> bool:
        """是否收集到了除标题外的实质内容."""
        return bool(
            self.description_lines
            or self.param_tables
            or self.response_tables
            or self.example_parts
            or self.netfn
            or self.cmd
        )

    @property
    def is_title_only(self) -> bool:
        """是否只有标题，没有收集到表格/NetFn/描述等实质内容."""
        return self.is_collecting and not self.has_real_content

    # -- 操作 --

    def append_title(self, text: str) -> None:
        """追加命令标题 (支持多行续行)."""
        self.title_parts.append(text.strip())

    def append_description(self, text: str) -> None:
        self.description_lines.append(text)

    def set_label(self, text: str) -> None:
        """更新当前标签，影响后续表格分类."""
        self.current_label = text
        for keywords, table_type in _LABEL_TABLE_TYPE_MAP:
            if any(kw in text.lower() for kw in keywords):
                self.current_table_type = table_type
                return
        # 非特殊标签不改变表格类型

    def add_table(self, table_text: str, netfn: Optional[str], cmd: Optional[str]) -> None:
        """将表格内容归入当前命令."""
        if netfn and not self.netfn:
            self.netfn = netfn
        if cmd and not self.cmd:
            self.cmd = cmd

        table_type = self.current_table_type
        if not table_type:
            # 默认: 有 NetFn 的表为参数表
            table_type = "parameter" if netfn else "generic"

        if table_type == "response":
            self.response_tables.append(table_text)
        elif table_type == "parameter":
            self.param_tables.append(table_text)
        elif table_type == "example":
            self.example_parts.append(table_text)
        else:
            self.description_lines.append(f"[表格]\n{table_text}")

    def update_netfn_cmd_from_text(self, text: str) -> None:
        """从正文/标题中尝试提取 NetFn/CMD."""
        if not self.netfn:
            m = _NETFN_EXPLICIT.search(text) or _NETFN_CN.search(text)
            if m:
                self.netfn = f"{m.group(1).upper()}h"
        if not self.cmd:
            m = _CMD_EXPLICIT.search(text) or _CMD_CN.search(text)
            if m:
                self.cmd = f"{m.group(1).upper()}h"

    def reset(self) -> None:
        """重置收集器."""
        self.title_parts.clear()
        self.description_lines.clear()
        self.param_tables.clear()
        self.response_tables.clear()
        self.example_parts.clear()
        self.netfn = None
        self.cmd = None
        self.current_label = ""
        self.current_table_type = ""


# ============================================================================
# DocxIPMIParser
# ============================================================================

class DocxIPMIParser(BaseParser):
    """
    IPMI 接口说明 DOCX 文档解析器 (v2).

    通过遍历 XML body 子元素保持段落与表格的交错顺序，
    基于字号检测标题层级，以单个完整命令为原子单位分块。

    核心改进:
      - CommandCollector 封装命令生命周期管理
      - 状态机精确跟踪: 标题 -> 描述 -> 参数表 -> 响应表 -> 示例
      - 多来源 NetFn/CMD 提取 (表格 + 标题 + 正文)
      - 丰富 metadata (english_name, chinese_name, full_command)
    """

    async def parse(self, file_path: str) -> List[Dict[str, Any]]:
        """
        解析 IPMI DOCX 文档，返回命令级分块列表。

        Args:
            file_path: DOCX 文件路径

        Returns:
            分块列表，每个元素包含 text 和 metadata

        Raises:
            FileNotFoundError: 文件不存在
            ValueError: 非 .docx 文件
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
        current_chapter: str = ""
        skipping: bool = False
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

                # ---- 跳过非命令章节 ----
                if level in ("chapter", "command"):
                    is_skip = level == "chapter" and any(
                        s in text for s in _SKIP_SECTIONS
                    )
                    if is_skip:
                        skipping = True
                        chunk_counter = self._flush_collector(
                            collector, chunks, file_name,
                            current_chapter, chunk_counter,
                        )
                        collector.reset()
                        logger.debug(f"跳过章节: {text[:60]}")
                        continue
                    if level == "chapter":
                        skipping = False

                if skipping:
                    continue

                # ---- 章标题 ----
                if level == "chapter":
                    chunk_counter = self._flush_collector(
                        collector, chunks, file_name,
                        current_chapter, chunk_counter,
                    )
                    collector.reset()
                    current_chapter = text
                    chunk_counter += 1
                    chunks.append(
                        self._build_chunk(
                            text=f"章节: {text}",
                            file_name=file_name,
                            chunk_id=f"chapter_{chunk_counter:05d}",
                            chunk_type="chapter",
                            section=text,
                            doc_type="ipmi",
                        )
                    )
                    logger.debug(f"章标题: {text[:60]}")

                # ---- 命令标题 (核心分块边界) ----
                elif level == "command":
                    # 多行标题续行: 当前命令只有标题没有实质内容
                    if collector.is_title_only:
                        collector.append_title(text)
                        collector.update_netfn_cmd_from_text(text)
                        logger.debug(f"标题续行: {collector.full_title[:80]}")
                        continue

                    # flush 上一个完整命令
                    chunk_counter = self._flush_collector(
                        collector, chunks, file_name,
                        current_chapter, chunk_counter,
                    )
                    collector.reset()

                    # 开始新命令
                    collector.append_title(text)
                    collector.update_netfn_cmd_from_text(text)
                    logger.debug(f"新命令: {text[:80]}")

                # ---- 子节标题 ----
                elif level == "subsection":
                    if collector.is_collecting:
                        collector.append_description(f"[子节] {text}")
                    else:
                        collector.append_description(text)

                # ---- 标签 (13pt) ----
                elif level == "label":
                    collector.set_label(text)
                    collector.append_description(f"[{text}]")

                # ---- 正文 ----
                elif level == "body":
                    if self._is_noise_paragraph(text):
                        continue
                    # 尝试从正文提取 NetFn/CMD
                    collector.update_netfn_cmd_from_text(text)
                    collector.append_description(text)

            elif elem_type == "table":
                if skipping:
                    continue
                table = Table(elem, doc)

                if self._is_noise_table(table):
                    continue

                table_text = self._format_table(table)
                netfn, cmd = self._extract_netfn_cmd(table)
                collector.add_table(table_text, netfn, cmd)

        # ------------------------------------------------------------------
        # flush 最后一条命令
        # ------------------------------------------------------------------
        chunk_counter = self._flush_collector(
            collector, chunks, file_name,
            current_chapter, chunk_counter,
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
        chapter: str,
        chunk_counter: int,
    ) -> int:
        """
        将 collector 中的命令数据 flush 为一个 chunk 追加到 chunks.

        Returns:
            更新后的 chunk_counter
        """
        if not collector.is_collecting:
            return chunk_counter

        full_title = collector.full_title
        if len(full_title.strip()) < 5:
            return chunk_counter

        chunk_counter += 1

        # -- 提取中英文命令名 --
        chinese_name, english_name = self._extract_names(full_title)

        # -- 构建 text --
        parts: List[str] = [f"命令: {full_title}"]
        if collector.netfn:
            parts.append(f"NetFn: {collector.netfn}")
        if collector.cmd:
            parts.append(f"CMD: {collector.cmd}")

        desc = "\n".join(collector.description_lines).strip()
        if desc:
            parts.append(f"描述:\n{desc}")
        if collector.param_tables:
            parts.append(f"参数表:\n" + "\n\n".join(collector.param_tables))
        if collector.response_tables:
            parts.append(f"响应表:\n" + "\n\n".join(collector.response_tables))
        if collector.example_parts:
            parts.append(f"示例:\n" + "\n\n".join(collector.example_parts))

        full_text = "\n\n".join(parts)
        if len(full_text.strip()) < 10:
            return chunk_counter

        # -- 构建 metadata --
        extra: Dict[str, Any] = {}
        extra["full_command"] = full_title[:500]
        if chinese_name:
            extra["chinese_name"] = chinese_name
        if english_name:
            extra["english_name"] = english_name
        if collector.netfn:
            extra["netfn"] = collector.netfn
        if collector.cmd:
            extra["cmd"] = collector.cmd
            extra["command_code"] = (
                f"{collector.netfn} {collector.cmd}"
                if collector.netfn
                else collector.cmd
            )
        # table_type
        has_param = bool(collector.param_tables)
        has_resp = bool(collector.response_tables)
        if has_param and has_resp:
            extra["table_type"] = "both"
        elif has_param:
            extra["table_type"] = "parameter"
        elif has_resp:
            extra["table_type"] = "response"

        section = (
            f"{chapter} > {full_title}"
            if chapter and full_title
            else (full_title or chapter)
        )

        chunks.append(
            self._build_chunk(
                text=full_text,
                file_name=file_name,
                chunk_id=f"cmd_{chunk_counter:05d}",
                chunk_type="command",
                section=section[:500],
                doc_type="ipmi",
                description=full_title[:200],
                **extra,
            )
        )

        # 日志: 记录 flush 详情
        nf = collector.netfn or "-"
        cc = collector.cmd or "-"
        logger.debug(
            f"Flush: [{chunk_id_format(chunk_counter)}] "
            f"NetFn={nf} CMD={cc} "
            f"params={len(collector.param_tables)} "
            f"resps={len(collector.response_tables)} "
            f"| {full_title[:60]}"
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
              "chapter"    - 章标题 (>= 18pt, 编号如 "3 xxx" 或 >= 22pt 大标题)
              "command"    - 命令节标题 (>= 18pt, 编号如 "3.1 xxx")
              "subsection" - 子节标题 (>= 16pt)
              "label"      - 标签 (>= 12.5pt)
              "body"       - 正文
              "empty"      - 空段落
        """
        text = para.text.strip()
        if not text:
            return "empty", text

        size_pt = self._get_font_size_pt(para)
        if size_pt is None:
            return "body", text

        if size_pt >= FONT_SIZE_CHAPTER:
            # 章标题: "3 系统信息命令" (数字+空格+非点号) 或 >= 22pt 非编号大标题
            if re.match(r"^\d+\s+\S", text) and "." not in text[:6]:
                return "chapter", text
            if size_pt >= 22.0 and not re.match(r"^\d+\.\d", text):
                return "chapter", text
            # 命令节标题: "3.1 获取..." (含编号点)
            return "command", text
        elif size_pt >= FONT_SIZE_SUBSECTION:
            return "subsection", text
        elif size_pt >= FONT_SIZE_LABEL:
            return "label", text
        else:
            return "body", text

    @staticmethod
    def _is_noise_table(table: Table) -> bool:
        """检测噪声表格 (页眉页脚 / TOC)."""
        num_rows = len(table.rows)
        if num_rows == 0:
            return True

        all_text = " ".join(
            cell.text.strip()
            for row in table.rows
            for cell in row.cells
        )

        # 单行 + 页眉页脚关键词
        if num_rows <= 1:
            for kw in _HEADER_FOOTER_KEYWORDS:
                if kw in all_text:
                    return True

        # TOC 条目 (连续省略号)
        if _TOC_DOT_PATTERN.search(all_text):
            return True

        return False

    @staticmethod
    def _is_noise_paragraph(text: str) -> bool:
        """检测页眉页脚噪声段落."""
        for kw in _HEADER_FOOTER_KEYWORDS:
            if kw in text:
                return True
        return False

    # ------------------------------------------------------------------
    # NetFn/CMD 提取
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_netfn_cmd(table: Table) -> Tuple[Optional[str], Optional[str]]:
        """
        从参数表前几行提取 NetFn 和 CMD.

        策略: 扫描前 5 行所有单元格, 按行查找 "NetFn" 和 "CMD" 标签,
        取标签同行或下一行的 hex 值.
        如无明确标签, 退回取前两个 hex 值.

        Returns:
            (netfn, cmd) 如 ("30h", "93h")
        """
        # 收集每行的 (label_text, hex_values)
        row_data: List[Tuple[str, List[str]]] = []
        for row_idx, row in enumerate(table.rows):
            if row_idx > 6:
                break
            cells_text = [
                cell.text.strip()
                for cell in row.cells
            ]
            row_label = " ".join(cells_text).lower()
            hex_vals: List[str] = []
            for ct in cells_text:
                hex_vals.extend(_HEX_SUFFIX_PATTERN.findall(ct))
                hex_vals.extend(_HEX_PREFIX_PATTERN.findall(ct))
            row_data.append((row_label, hex_vals))

        # 策略 1: 按 NetFn/CMD 标签查找
        netfn_val: Optional[str] = None
        cmd_val: Optional[str] = None
        for i, (label, hexes) in enumerate(row_data):
            if "netfn" in label and hexes:
                netfn_val = f"{hexes[0].upper()}h"
            if "cmd" in label and hexes:
                # 过滤掉非命令值的 hex (如 NetFn 行的值)
                filtered = [h for h in hexes if f"{h.lower()}h" != (netfn_val or "").lower()]
                if filtered:
                    cmd_val = f"{filtered[0].upper()}h"
                elif hexes:
                    cmd_val = f"{hexes[0].upper()}h"

        if netfn_val or cmd_val:
            return netfn_val, cmd_val

        # 策略 2: 退回取前两个 hex 值
        all_hexes: List[str] = []
        for _, hexes in row_data:
            all_hexes.extend(hexes)
        if len(all_hexes) >= 2:
            return f"{all_hexes[0].upper()}h", f"{all_hexes[1].upper()}h"
        if len(all_hexes) == 1:
            return f"{all_hexes[0].upper()}h", None
        return None, None

    @staticmethod
    def _extract_names(title: str) -> Tuple[Optional[str], Optional[str]]:
        """
        从命令标题中提取中文名和英文名.

        标题格式示例:
          "3.5 获取CPU 读数（Get CPU Reading）"
          "6.24 机箱控制（Chassis Control）命令功能机箱控制。"
          "17.125 获取BMC 基本信息"
          "14.129 关闭会话（Close Session）"

        Returns:
            (chinese_name, english_name)
        """
        m = _TITLE_CN_EN.match(title)
        if not m:
            return None, None

        chinese = m.group(1).strip() if m.group(1) else None
        english = m.group(2).strip() if m.group(2) else None

        # 清理中文名中的尾部噪声
        if chinese:
            chinese = chinese.rstrip("命令功能。")
            chinese = chinese.strip()

        # 清理英文名中的尾部噪声
        if english:
            # 去掉可能混入的中文
            english = re.sub(r"[^\x20-\x7e]", "", english).strip()

        return chinese or None, english or None

    @staticmethod
    def _format_table(table: Table) -> str:
        """
        将表格转换为可读文本.

        格式: "列1 | 列2", 行间换行.
        合并单元格通过 tc 元素 id 去重.
        """
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


# ============================================================================
# 辅助
# ============================================================================

def chunk_id_format(counter: int) -> str:
    return f"cmd_{counter:05d}"
