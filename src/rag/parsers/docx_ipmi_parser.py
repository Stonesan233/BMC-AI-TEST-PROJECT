# -*- coding: utf-8 -*-
"""
IPMI 接口文档 DOCX 解析器

适用文档: 华为 iBMC IPMI 接口说明文档 (.docx 格式)

文档特征:
  - 所有段落均为 "Normal" 样式，标题仅通过字号区分
  - 字号层级: 22pt/72pt (章) > 18pt (命令节) > 16pt (子节) > 13pt (标签) > 10.5pt (正文)
  - 段落和表格在文档中交错排列，必须遍历 XML body 子元素保持顺序
  - 命令节内部结构: 标题 -> 命令功能描述 -> 参数表(含 NetFn/CMD) -> 响应说明 -> 响应表

分块策略:
  - 按命令粒度分块: 每条 IPMI 命令 (如 "3.1 获取BusinessPort...") 生成一个 chunk
  - chunk 包含该命令的完整信息: 功能描述 + 参数表 + 响应表
  - 章标题单独生成 chunk (用于检索时的上下文定位)
  - 每个 chunk 附加富 metadata: netfn, cmd, command_code, section, description 等

依赖: python-docx>=0.8.11
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

from src.rag.parsers.base_parser import BaseParser

logger = logging.getLogger("rag.parser.docx_ipmi")

# ---------------------------------------------------------------------------
# 常量: 字号阈值 (points)
# ---------------------------------------------------------------------------
FONT_SIZE_CHAPTER = 18.0       # >= 18pt: 章/节标题
FONT_SIZE_SUBSECTION = 16.0    # >= 16pt: 子节标题
FONT_SIZE_LABEL = 12.5         # >= 12.5pt: 标签 ("命令功能", "参数说明" 等)
# < 12.5pt: 正文

# ---------------------------------------------------------------------------
# 常量: 噪声过滤
# ---------------------------------------------------------------------------
_HEADER_FOOTER_KEYWORDS = [
    "文档版本",
    "iBMC IPMI 接口参考",
    "iBMC IPMI 接口说明",
    "华为技术有限公司",
    "版权所有",
]
_TOC_DOT_PATTERN = re.compile(r"\.{4,}")

# 提取十六进制值 (如 30h, 93h, 0x30)
_HEX_PATTERN = re.compile(r"([0-9A-Fa-f]{1,2})[hH]")

# 需要跳过的非命令章节标题
_SKIP_SECTIONS = {"目录", "前言", "安全声明", "修改记录"}


class DocxIPMIParser(BaseParser):
    """
    IPMI 接口说明 DOCX 文档解析器。

    通过遍历 XML body 子元素保持段落与表格的交错顺序，
    基于字号检测标题层级，按命令粒度智能分块。
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

        # ------------------------------------------------------------------
        # 状态变量
        # ------------------------------------------------------------------
        current_chapter: str = ""
        current_command: str = ""
        current_description: List[str] = []
        current_label: str = ""
        current_netfn: Optional[str] = None
        current_cmd: Optional[str] = None
        current_params_parts: List[str] = []
        skipping_section: bool = False  # 是否在跳过非命令章节
        current_response_parts: List[str] = []
        current_table_type: str = ""  # "parameter" | "response"
        chunk_counter: int = 0
        chunks: List[Dict[str, Any]] = []

        # ------------------------------------------------------------------
        # 主循环: 遍历 XML body 子元素 (段落 + 表格交错)
        # ------------------------------------------------------------------
        for elem_type, elem in self._iter_body_elements(doc):

            if elem_type == "paragraph":
                para = Paragraph(elem, doc)
                level, text = self._classify_paragraph(para)

                if level == "empty":
                    continue

                # -- 跳过非命令章节 (目录/前言/安全声明等) --
                if level == "chapter" or (level == "command" and not current_params_parts):
                    # 检测是否为需要跳过的章节
                    section_name = text.strip()
                    is_skip = any(s in section_name for s in _SKIP_SECTIONS)
                    if level == "chapter" and is_skip:
                        skipping_section = True
                        # flush 当前命令
                        chunk = self._flush_command_chunk(
                            file_name=file_name,
                            chapter=current_chapter,
                            command=current_command,
                            description=current_description,
                            params_parts=current_params_parts,
                            response_parts=current_response_parts,
                            netfn=current_netfn,
                            cmd=current_cmd,
                            chunk_counter=chunk_counter,
                        )
                        if chunk:
                            chunks.append(chunk)
                            chunk_counter += 1
                        # 重置命令级状态
                        current_command = ""
                        current_description = []
                        current_params_parts = []
                        current_response_parts = []
                        current_netfn = None
                        current_cmd = None
                        current_label = ""
                        current_table_type = ""
                        logger.debug(f"跳过章节: {text[:60]}")
                        continue
                    elif level == "chapter":
                        skipping_section = False

                if skipping_section:
                    continue

                # -- 章标题 (>= 18pt) --
                if level == "chapter":
                    # 先 flush 当前正在收集的命令
                    chunk = self._flush_command_chunk(
                        file_name=file_name,
                        chapter=current_chapter,
                        command=current_command,
                        description=current_description,
                        params_parts=current_params_parts,
                        response_parts=current_response_parts,
                        netfn=current_netfn,
                        cmd=current_cmd,
                        chunk_counter=chunk_counter,
                    )
                    if chunk:
                        chunks.append(chunk)
                        chunk_counter += 1

                    # 重置命令级状态
                    current_command = ""
                    current_description = []
                    current_params_parts = []
                    current_response_parts = []
                    current_netfn = None
                    current_cmd = None
                    current_label = ""
                    current_table_type = ""

                    # 章标题独立 chunk
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

                # -- 命令节标题 (18pt) --
                elif level == "command":
                    # 检测多行标题续行: 如果当前命令还没有收集到任何表格或描述，
                    # 说明上一条 command 段落是标题的第一行，本行是标题的续行
                    if (current_command
                            and not current_params_parts
                            and not current_response_parts
                            and not current_netfn
                            and not any(
                                t.startswith("[")
                                for t in current_description
                                if t.strip()
                            )):
                        # 合并标题: 追加到当前命令标题
                        current_command = f"{current_command} {text}"
                        logger.debug(f"标题续行: {current_command[:80]}")
                        continue

                    # flush 前一条命令
                    chunk = self._flush_command_chunk(
                        file_name=file_name,
                        chapter=current_chapter,
                        command=current_command,
                        description=current_description,
                        params_parts=current_params_parts,
                        response_parts=current_response_parts,
                        netfn=current_netfn,
                        cmd=current_cmd,
                        chunk_counter=chunk_counter,
                    )
                    if chunk:
                        chunks.append(chunk)
                        chunk_counter += 1

                    # 开始新命令
                    current_command = text
                    current_description = []
                    current_params_parts = []
                    current_response_parts = []
                    current_netfn = None
                    current_cmd = None
                    current_label = ""
                    current_table_type = ""
                    logger.debug(f"命令: {text[:80]}")

                # -- 子节标题 (16pt) --
                elif level == "subsection":
                    if current_command:
                        current_description.append(f"[子节] {text}")
                    # 如果没有当前命令，子节可能是独立的分块点
                    else:
                        current_description.append(text)

                # -- 标签 (13pt) --
                elif level == "label":
                    current_label = text
                    # 标签影响后续表格的分类
                    if "响应" in text or "response" in text.lower():
                        current_table_type = "response"
                    elif "参数" in text or "parameter" in text.lower():
                        current_table_type = "parameter"
                    # 标签本身也加入描述
                    current_description.append(f"[{text}]")

                # -- 正文 (<= 11pt) --
                elif level == "body":
                    if self._is_noise_paragraph(text):
                        continue
                    current_description.append(text)

            elif elem_type == "table":
                table = Table(elem, doc)

                # 跳过噪声表格
                if self._is_noise_table(table):
                    continue

                # 提取表格内容
                table_text = self._format_table(table)

                # 尝试提取 NetFn/CMD
                netfn, cmd = self._extract_netfn_cmd(table)
                if netfn and not current_netfn:
                    current_netfn = netfn
                if cmd and not current_cmd:
                    current_cmd = cmd

                # 根据 last_label 决定表格类型
                if current_table_type == "response":
                    current_response_parts.append(table_text)
                elif current_table_type == "parameter":
                    current_params_parts.append(table_text)
                else:
                    # 默认: 如果含 NetFn 则为参数表
                    if netfn:
                        current_params_parts.append(table_text)
                    else:
                        current_description.append(f"[表格]\n{table_text}")

        # ------------------------------------------------------------------
        # flush 最后一条命令
        # ------------------------------------------------------------------
        chunk = self._flush_command_chunk(
            file_name=file_name,
            chapter=current_chapter,
            command=current_command,
            description=current_description,
            params_parts=current_params_parts,
            response_parts=current_response_parts,
            netfn=current_netfn,
            cmd=current_cmd,
            chunk_counter=chunk_counter,
        )
        if chunk:
            chunks.append(chunk)

        logger.info(f"解析完成: {file_name}, 共 {len(chunks)} 个 chunks")
        return chunks

    # ======================================================================
    # 内部方法
    # ======================================================================

    @staticmethod
    def _iter_body_elements(doc: Document):
        """
        遍历文档 body 的直接子元素，保持段落和表格的交错顺序。

        Yields:
            (element_type, xml_element) 元组
            element_type: "paragraph" 或 "table"
        """
        body = doc.element.body
        for child in body:
            tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if tag == "p":
                yield "paragraph", child
            elif tag == "tbl":
                yield "table", child

    @staticmethod
    def _get_font_size_pt(para: Paragraph) -> Optional[float]:
        """
        提取段落的字号 (points)。

        优先取第一个非空 run 的 font.size，回退到 style.font.size。
        python-docx 中 font.size 以 EMU 半磅为单位，需 / 2 转为 pt。
        """
        for run in para.runs:
            if run.text.strip() and run.font.size:
                return run.font.size.pt
        # 回退到段落样式
        if para.style and para.style.font and para.style.font.size:
            return para.style.font.size.pt
        return None

    def _classify_paragraph(self, para: Paragraph) -> Tuple[str, str]:
        """
        根据字号对段落分类。

        Returns:
            (level, text) 其中 level 为:
              "chapter"    - 章标题 (>= 18pt 且匹配编号模式)
              "command"    - 命令节标题 (>= 18pt)
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
            # 区分 "章标题" 和 "命令节标题"
            # 章标题: "3 系统信息命令", "2 命令索引" (数字+空格+中文)
            # 命令节标题: "3.1 获取..." (含小数点的编号)
            if re.match(r"^\d+\s+", text) and "." not in text[:5]:
                return "chapter", text
            # 也可能是纯大标题如 "前言"
            if size_pt >= 22.0 and not re.match(r"^\d+\.", text):
                return "chapter", text
            return "command", text
        elif size_pt >= FONT_SIZE_SUBSECTION:
            return "subsection", text
        elif size_pt >= FONT_SIZE_LABEL:
            return "label", text
        else:
            return "body", text

    @staticmethod
    def _is_noise_table(table: Table) -> bool:
        """
        检测是否为噪声表格 (页眉、页脚、TOC 等)。

        判定规则:
          - 1 行表格 + 含页眉页脚关键词 -> 噪声
          - 含连续省略号 "..." -> TOC 条目
        """
        num_rows = len(table.rows)
        if num_rows == 0:
            return True

        # 收集所有单元格文本
        all_text = " ".join(
            cell.text.strip()
            for row in table.rows
            for cell in row.cells
        )

        # 单行表格检查
        if num_rows <= 1:
            for kw in _HEADER_FOOTER_KEYWORDS:
                if kw in all_text:
                    return True

        # TOC 条目 (含连续省略号)
        if _TOC_DOT_PATTERN.search(all_text):
            return True

        return False

    @staticmethod
    def _is_noise_paragraph(text: str) -> bool:
        """检测是否为页眉页脚噪声段落。"""
        for kw in _HEADER_FOOTER_KEYWORDS:
            if kw in text:
                return True
        return False

    @staticmethod
    def _extract_netfn_cmd(table: Table) -> Tuple[Optional[str], Optional[str]]:
        """
        从参数表/响应表中提取 NetFn 和 CMD 十六进制值。

        查找前 4 行中的十六进制模式 (如 30h, 93h)。
        通常 NetFn 在第一行数据，CMD 在第二行。

        Returns:
            (netfn, cmd) 如 ("30h", "93h")，未找到则返回 (None, None)
        """
        hex_values: List[str] = []
        for row_idx, row in enumerate(table.rows):
            if row_idx > 4:
                break
            for cell in row.cells:
                cell_text = cell.text.strip()
                matches = _HEX_PATTERN.findall(cell_text)
                hex_values.extend(matches)

        if len(hex_values) >= 2:
            return f"{hex_values[0].upper()}h", f"{hex_values[1].upper()}h"
        elif len(hex_values) == 1:
            return f"{hex_values[0].upper()}h", None
        return None, None

    @staticmethod
    def _format_table(table: Table) -> str:
        """
        将表格转换为可读文本，用于 embedding。

        格式: 每行 "列1: 值 | 列2: 值"，行间换行分隔。
        处理合并单元格: 通过跟踪已处理的 tc 元素去重。
        """
        rows_text: List[str] = []
        seen_tc_elements: Set[int] = set()

        for row in table.rows:
            cells_text: List[str] = []
            for cell in row.cells:
                # 通过 XML 元素 id 去重 (合并单元格会导致重复)
                tc_elem = cell._tc
                tc_id = id(tc_elem)
                if tc_id in seen_tc_elements:
                    continue
                seen_tc_elements.add(tc_id)

                cell_text = cell.text.strip().replace("\n", " ")
                cells_text.append(cell_text)

            if cells_text:
                rows_text.append(" | ".join(cells_text))

        return "\n".join(rows_text)

    def _flush_command_chunk(
        self,
        file_name: str,
        chapter: str,
        command: str,
        description: List[str],
        params_parts: List[str],
        response_parts: List[str],
        netfn: Optional[str],
        cmd: Optional[str],
        chunk_counter: int,
    ) -> Optional[Dict[str, Any]]:
        """
        将当前累积的命令信息构建为一个 chunk。

        Returns:
            分块字典，或 None (内容太少时跳过)
        """
        if not command and not description and not params_parts and not response_parts:
            return None

        # 组装用于 embedding 的文本
        parts: List[str] = []
        if command:
            parts.append(f"命令: {command}")
        if netfn:
            parts.append(f"NetFn: {netfn}")
        if cmd:
            parts.append(f"CMD: {cmd}")

        desc_text = "\n".join(description).strip()
        if desc_text:
            parts.append(f"描述:\n{desc_text}")

        if params_parts:
            params_text = "\n\n".join(params_parts)
            parts.append(f"参数表:\n{params_text}")

        if response_parts:
            resp_text = "\n\n".join(response_parts)
            parts.append(f"响应表:\n{resp_text}")

        full_text = "\n\n".join(parts)
        if len(full_text.strip()) < 10:
            return None

        # 构建 metadata
        extra: Dict[str, Any] = {}
        if netfn:
            extra["netfn"] = netfn
        if cmd:
            extra["cmd"] = cmd
            extra["command_code"] = f"{netfn} {cmd}" if netfn else cmd
        if params_parts:
            extra["table_type"] = "parameter_table"
        if response_parts:
            extra["table_type"] = (
                "parameter_and_response_table"
                if params_parts
                else "response_table"
            )

        section = f"{chapter} > {command}" if chapter and command else (command or chapter)

        return self._build_chunk(
            text=full_text,
            file_name=file_name,
            chunk_id=f"cmd_{chunk_counter:05d}",
            chunk_type="command",
            section=section,
            doc_type="ipmi",
            description=command[:200] if command else "",
            **extra,
        )
