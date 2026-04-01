# -*- coding: utf-8 -*-
"""
Redfish API 接口文档 DOCX 解析器

适用文档: Atlas 系列硬件产品 iBMC 300 Redfish 接口参考 (.docx 格式)

文档特征:
  - 所有段落均为 "Normal" 样式，标题仅通过字号区分 (与 IPMI 文档一致)
  - 字号层级: 22pt (章) > 18pt (节) > 16pt (API 端点标题) > 13pt (标签) > 10.5pt (正文)
  - 大量 "合并段落": 标题(16pt) + 功能描述(10.5pt) + 命令格式(13pt) + URL(10.5pt)
    在同一段落中，通过不同 run 字号区分，共约 308 处
  - JSON 示例存储在表格中 (通常 1 行 x 1 列 或 1 行 x 2 列)
  - 约 4000+ 噪声表格 (页眉/页脚)，需要过滤

分块策略:
  - 以单个 API 端点为最小原子 chunk (chunk_type="resource")
  - 一个 chunk 包含: 端点标题 + 功能描述 + URI + HTTP 方法 + 参数表 + 请求/响应示例 + 输出说明
  - 使用 ResourceCollector 收集每个端点的完整信息
  - 章节标题单独生成 chunk (chunk_type="chapter"/"section")
  - 资源概览属性表单独生成 chunk (chunk_type="property_table")

Metadata:
  - doc_type, file_name, chunk_id, chunk_type, section (继承自 BaseParser)
  - resource_uri: 如 "/redfish/v1/Systems/{SystemId}"
  - http_method: 如 "GET,PATCH" (逗号分隔)
  - schema_name: 如 "Systems" (从 URI 提取)
  - chinese_name / english_name: 端点标题的中英文名
  - description: 功能描述文本 (用于检索)
  - example_request: 请求消息体 (截断)
  - example_response: 响应示例 (截断)

合并段落处理:
  当检测到一个段落内有存在 >=16pt 和 <16pt 的 run 时，视为合并段落:
    1. 按 run 字号分组: >=16pt 为 title, >=13pt 为 label, <13pt 为 body
    2. 按 label 文本将 body 内容归入对应子节 (命令功能/命令格式/参数等)
    3. 同时对完整文本运行 URI/HTTP 方法正则提取作为兜底

依赖: python-docx>=0.8.11
"""

import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

from src.rag.parsers.base_parser import BaseParser

logger = logging.getLogger("rag.parser.docx_redfish")

# ============================================================================
# 常量
# ============================================================================

# ---------------------------------------------------------------------------
# 字号阈值 (points)
# ---------------------------------------------------------------------------
FONT_SIZE_CHAPTER = 22.0        # >= 22pt: 章标题 ("1概述", "3接口介绍")
FONT_SIZE_SECTION = 18.0        # >= 18pt: 节标题 ("3.1 公共固定资源的操作")
FONT_SIZE_ENDPOINT = 16.0       # >= 16pt: API 端点标题 ("3.1.2 查询当前根服务资源")
FONT_SIZE_LABEL = 12.5          # >= 12.5pt: 标签 ("命令功能", "参数说明")
# < 12.5pt: 正文

# ---------------------------------------------------------------------------
# 噪声过滤
# ---------------------------------------------------------------------------
_NOISE_KEYWORDS_TABLE = [
    "文档版本",
    "版权所有",
    "Atlas 系列",
    "iBMC 300 Redfish 接口参考",
]
_NOISE_KEYWORDS_PARA = [
    "文档版本",
    "版权所有",
    "华为技术有限公司",
    "Atlas 系列硬件产品",
    "iBMC 300 Redfish 接口参考",
]
_TOC_DOT_PATTERN = re.compile(r"\.{4,}")

# ---------------------------------------------------------------------------
# 需要跳过的章节
# ---------------------------------------------------------------------------
_SKIP_SECTIONS = {"目录", "前言", "安全声明", "修改记录"}

# ---------------------------------------------------------------------------
# 非端点标题过滤 (16pt 但不是 API 端点的标题)
# ---------------------------------------------------------------------------
_NON_ENDPOINT_KEYWORDS = [
    "华为技术", "版权所有", "Atlas 系列",
    "安全声明", "读者对象", "修订记录",
    "符号约定", "术语", "附录",
]

# ---------------------------------------------------------------------------
# URI / HTTP Method 提取正则
# ---------------------------------------------------------------------------
# 匹配 "URL: https://device_ip/redfish/v1/..." 或 "URL：https://..."
_REDFISH_URI_PATTERN = re.compile(
    r"(?:URL[：:]\s*)?https?://\S+?(/redfish(?:/v1)?/[^\s,，;；\)]*)",
    re.IGNORECASE,
)
# 也匹配纯 /redfish (无子路径, 如版本查询)
_REDFISH_URI_ROOT_PATTERN = re.compile(
    r"(?:URL[：:]\s*)?https?://\S+?(/redfish)(?:\s|$)",
    re.IGNORECASE,
)
# 直接匹配 /redfish/v1/ 或 /redfish 路径
_REDFISH_PATH_PATTERN = re.compile(
    r"/redfish(?:/v1)?/[A-Za-z0-9_/\{\}\.\-\?=:]+"
    r"|/redfish(?=\s|$)"
)
# 匹配 "操作类型：GET" / "操作类型: POST"
_HTTP_METHOD_PATTERN = re.compile(
    r"操作类型[：:]\s*(GET|POST|PATCH|DELETE|PUT|HEAD)",
    re.IGNORECASE,
)
# 匹配示例表格中的 "GET https://..." 或 "PATCH https://..."
_HTTP_METHOD_IN_TABLE = re.compile(
    r"^\s*(GET|POST|PATCH|DELETE|PUT|HEAD)\s+https?://",
    re.IGNORECASE,
)

# ---------------------------------------------------------------------------
# Schema 名称提取 (从 URI 路径的第一个资源段)
# ---------------------------------------------------------------------------
_SCHEMA_FROM_URI = re.compile(r"/redfish/v1/([A-Z][A-Za-z0-9_]*)")

# ---------------------------------------------------------------------------
# URI 路径中的复数形式 -> _SYNONYM_MAP 中的标准 schema 名
# ---------------------------------------------------------------------------
_PLURAL_TO_SCHEMA = {
    "Systems": "ComputerSystem",
    "Managers": "Manager",
    "Accounts": "AccountService",
    "Sessions": "SessionService",
    "Tasks": "TaskService",
    "Events": "EventService",
    "Storages": "Storage",
    "Processors": "Processor",
    "EthernetInterfaces": "EthernetInterface",
    "Certificates": "CertificateService",
    "Logs": "LogService",
    "Sensors": "Sensor",
    "Updates": "UpdateService",
    # 以下已与 _SYNONYM_MAP key 一致，无需映射
    # "Chassis", "Thermal", "Memory", "Registries", "VirtualMedia", "Reset"
}


def _normalize_schema(raw_schema: str) -> str:
    """将 URI 路径中的资源名归一化为 _SYNONYM_MAP 中的标准 key."""
    return _PLURAL_TO_SCHEMA.get(raw_schema, raw_schema)

# URI 路径中的复数形式 -> _SYNONYM_MAP 中的单数形式映射
_PLURAL_TO_SINGULAR = {
    "Systems": "ComputerSystem",
    "Managers": "Manager",
    "Chassis": "Chassis",
    "Accounts": "AccountService",
    "Sessions": "SessionService",
    "Tasks": "TaskService",
    "Events": "EventService",
    "Logs": "LogService",
    "Sensors": "Sensor",
    "Processors": "Processor",
    "Memory": "Memory",
    "EthernetInterfaces": "EthernetInterface",
    "Storages": "Storage",
    "Updates": "UpdateService",
    "Registries": "Registries",
    "Certificates": "CertificateService",
}

# ---------------------------------------------------------------------------
# 常见 Redfish 同义词映射 (用于在 chunk text 中补充检索关键词)
# ---------------------------------------------------------------------------
_SYNONYM_MAP = {
    "PowerState": ["电源状态", "开机状态", "关机状态", "电源控制", "Power"],
    "FirmwareVersion": ["固件版本", "软件版本", "BMC版本", "Firmware"],
    "EthernetInterface": ["网络接口", "网卡", "IP地址", "Ethernet", "网络配置"],
    "AccountService": ["用户管理", "账号管理", "Account", "用户"],
    "SessionService": ["会话管理", "登录", "Session", "会话"],
    "ComputerSystem": ["系统资源", "服务器信息", "System", "系统"],
    "Chassis": ["机箱", "散热", "风扇", "Chassis", "电源"],
    "Thermal": ["温度", "散热", "风扇转速", "Thermal", "传感器"],
    "Manager": ["管理控制器", "BMC", "Manager", "管理模块"],
    "EventService": ["事件服务", "告警", "Event", "订阅"],
    "UpdateService": ["更新服务", "固件升级", "Update", "升级"],
    "TaskService": ["任务服务", "异步任务", "Task"],
    "LogService": ["日志服务", "系统日志", "Log", "SEL"],
    "Sensor": ["传感器", "Sensor", "温度", "电压", "风扇"],
    "Reset": ["重启", "复位", "Reset", "ForceRestart", "GracefulRestart"],
    "Registries": ["注册表", "消息注册", "Registry"],
    "CertificateService": ["证书服务", "HTTPS", "Certificate", "SSL"],
    "VirtualMedia": ["虚拟媒体", "VirtualMedia", "虚拟光驱"],
    "Storage": ["存储", "Storage", "磁盘", "RAID"],
    "Memory": ["内存", "Memory", "DIMM"],
    "Processor": ["处理器", "CPU", "Processor"],
    "NetworkProtocol": ["网络协议", "SNMP", "IPMI", "SSH", "NTP"],
}

# ---------------------------------------------------------------------------
# 标签 -> 子节类型映射 (用于状态机)
# ---------------------------------------------------------------------------
_LABEL_SECTION_MAP = [
    (["命令功能", "功能描述"], "description"),
    (["命令格式"], "command_format"),
    (["请求头"], "request_headers"),
    (["请求消息体"], "request_body"),
    (["参数说明", "参数"], "parameters"),
    (["使用指南", "使用说明"], "usage_guide"),
    (["使用实例", "请求样例"], "examples"),
    (["输出说明", "响应说明"], "response_fields"),
    (["响应头"], "response_headers"),
    (["响应消息体"], "response_body"),
    (["状态码"], "status_codes"),
    (["属性"], "properties"),
]

# ---------------------------------------------------------------------------
# 属性表表头特征 (用于识别资源概览表)
# ---------------------------------------------------------------------------
_PROPERTY_TABLE_HEADERS = {"URL", "属性", "说明", "操作", "允许操作", "适用的产品"}


# ============================================================================
# API 端点收集器 (ResourceCollector)
# ============================================================================

@dataclass
class ResourceCollector:
    """
    单个 Redfish API 端点的收集器。

    在主循环中，当检测到新 API 端点标题 (16pt) 时，flush 当前收集器生成 chunk，
    然后重置收集器开始收集下一个端点。

    子节状态通过 current_table_type 跟踪，由 13pt 标签文本驱动状态转换。
    """
    # -- 标题 --
    title_parts: List[str] = field(default_factory=list)

    # -- 功能描述 --
    description_lines: List[str] = field(default_factory=list)

    # -- URI 和 HTTP 方法 --
    uris: List[str] = field(default_factory=list)
    http_methods: List[str] = field(default_factory=list)

    # -- 请求信息 --
    request_headers: List[str] = field(default_factory=list)
    request_body_parts: List[str] = field(default_factory=list)

    # -- 参数表 --
    parameter_tables: List[str] = field(default_factory=list)

    # -- 使用指南 --
    usage_guide_lines: List[str] = field(default_factory=list)

    # -- 示例 --
    request_example_parts: List[str] = field(default_factory=list)
    response_example_parts: List[str] = field(default_factory=list)

    # -- 输出说明 (响应字段表) --
    response_field_tables: List[str] = field(default_factory=list)

    # -- 状态码 --
    status_code_tables: List[str] = field(default_factory=list)

    # -- 注意事项 --
    notes_lines: List[str] = field(default_factory=list)

    # -- 状态机 --
    current_table_type: str = ""  # 当前子节类型

    # -- 属性 --

    @property
    def full_title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def is_collecting(self) -> bool:
        """是否正在收集一个端点 (至少有标题)."""
        return len(self.title_parts) > 0

    @property
    def has_real_content(self) -> bool:
        """是否收集到了除标题外的实质内容."""
        return bool(
            self.description_lines
            or self.uris
            or self.http_methods
            or self.parameter_tables
            or self.request_body_parts
            or self.response_example_parts
            or self.response_field_tables
            or self.request_example_parts
        )

    @property
    def is_title_only(self) -> bool:
        """是否只有标题，没有收集到 URI/参数/示例等实质内容."""
        return self.is_collecting and not self.has_real_content

    # -- 操作 --

    def append_title(self, text: str) -> None:
        """追加端点标题 (支持多行续行)."""
        self.title_parts.append(text.strip())

    def set_subsection(self, text: str) -> None:
        """根据标签文本更新当前子节类型 (驱动状态机)."""
        for keywords, section_type in _LABEL_SECTION_MAP:
            if any(kw in text for kw in keywords):
                self.current_table_type = section_type
                return

    def add_body_text(self, text: str) -> None:
        """根据当前子节状态将正文归类到对应字段."""
        section = self.current_table_type
        if section == "description":
            self.description_lines.append(text)
        elif section == "usage_guide":
            self.usage_guide_lines.append(text)
        elif section == "command_format":
            self._extract_uri_and_method(text)
        elif section == "request_body":
            self.request_body_parts.append(text)
        elif section == "notes":
            self.notes_lines.append(text)
        else:
            # 默认归入功能描述
            self.description_lines.append(text)

    def add_table(self, table_text: str) -> None:
        """将表格内容归类到当前子节."""
        table_type = self.current_table_type

        # 检测表格特征
        is_json = table_text.strip().startswith(("{", "["))
        is_request_example = bool(_HTTP_METHOD_IN_TABLE.search(table_text))

        if table_type == "parameters":
            self.parameter_tables.append(table_text)
        elif table_type == "response_fields":
            self.response_field_tables.append(table_text)
        elif table_type == "request_body":
            self.request_body_parts.append(table_text)
        elif table_type == "response_body":
            self.response_example_parts.append(table_text)
        elif table_type == "request_headers":
            self.request_headers.append(table_text)
        elif table_type == "status_codes":
            self.status_code_tables.append(table_text)
        elif table_type == "examples" or is_request_example:
            if is_request_example:
                self.request_example_parts.append(table_text)
            elif is_json:
                self.response_example_parts.append(table_text)
            else:
                self.request_example_parts.append(table_text)
        elif is_json:
            # JSON 表格但无明确子节上下文 -> 推测为响应示例
            self.response_example_parts.append(table_text)
        else:
            # 通用表格，归入功能描述
            self.description_lines.append(f"[表格]\n{table_text}")

    def update_from_text(self, text: str) -> None:
        """从任意文本中尝试提取 URI 和 HTTP 方法."""
        self._extract_uri_and_method(text)

    def _extract_uri_and_method(self, text: str) -> None:
        """从文本中提取 URI 路径和 HTTP 方法."""
        # 提取 HTTP 方法: "操作类型：GET"
        method_match = _HTTP_METHOD_PATTERN.search(text)
        if method_match:
            method = method_match.group(1).upper()
            if method not in self.http_methods:
                self.http_methods.append(method)

        # 提取 URI 路径: "URL: https://device_ip/redfish/v1/..."
        uri_match = _REDFISH_URI_PATTERN.search(text)
        if uri_match:
            path = self._clean_uri(uri_match.group(1))
            if path and path not in self.uris:
                self.uris.append(path)
            return

        # 尝试匹配纯 /redfish (如版本查询端点)
        root_match = _REDFISH_URI_ROOT_PATTERN.search(text)
        if root_match:
            path = root_match.group(1)
            if path not in self.uris:
                self.uris.append(path)
            return

        # 兜底: 直接搜索 /redfish/v1/ 路径
        path_match = _REDFISH_PATH_PATTERN.search(text)
        if path_match:
            path = self._clean_uri(path_match.group(0))
            if path and path not in self.uris:
                self.uris.append(path)

    @staticmethod
    def _clean_uri(uri: str) -> str:
        """清理 URI 路径，去除尾部混入的中文噪声字符."""
        # 去除尾部非 URL 字符 (中文、标点等)
        cleaned = re.sub(r"[^\x20-\x7e]+$", "", uri)
        # 去除尾部特殊字符
        cleaned = cleaned.rstrip(".,;:，。；：、")
        return cleaned.strip()

    def reset(self) -> None:
        """重置收集器."""
        self.title_parts.clear()
        self.description_lines.clear()
        self.uris.clear()
        self.http_methods.clear()
        self.request_headers.clear()
        self.request_body_parts.clear()
        self.parameter_tables.clear()
        self.usage_guide_lines.clear()
        self.request_example_parts.clear()
        self.response_example_parts.clear()
        self.response_field_tables.clear()
        self.status_code_tables.clear()
        self.notes_lines.clear()
        self.current_table_type = ""


# ============================================================================
# DocxRedfishParser
# ============================================================================

class DocxRedfishParser(BaseParser):
    """
    Redfish API 接口 DOCX 文档解析器。

    核心设计:
      - ResourceCollector 封装 API 端点生命周期管理
      - 通过字号检测标题层级 (22/18/16/13/10.5pt)
      - 处理合并段落: 标题+功能+命令格式在同一段落中 (通过 run 级字号解析)
      - 从文本和表格中提取 URI、HTTP 方法、JSON 示例
      - 丰富 metadata (resource_uri, http_method, schema_name)
      - 噪声过滤: 页眉/页脚表格、TOC 条目
    """

    async def parse(self, file_path: str) -> List[Dict[str, Any]]:
        """
        解析 Redfish DOCX 文档，返回 API 端点级分块列表。

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

        logger.info(f"开始解析 Redfish 文档: {path.name}")
        doc = Document(str(path))
        file_name = path.name

        chunks: List[Dict[str, Any]] = []
        chunk_counter: int = 0

        # 解析状态
        current_chapter: str = ""
        current_section: str = ""
        skipping: bool = False
        collector = ResourceCollector()

        # ------------------------------------------------------------------
        # 主循环: 遍历 body 子元素，保持段落和表格的交错顺序
        # ------------------------------------------------------------------
        for elem_type, elem in self._iter_body_elements(doc):

            if elem_type == "paragraph":
                para = Paragraph(elem, doc)
                level, text, merged_parts = self._classify_paragraph(para)

                if level == "empty":
                    continue

                # ---- 跳过非内容章节 ----
                if level in ("chapter", "section"):
                    if any(s in text for s in _SKIP_SECTIONS):
                        skipping = True
                        chunk_counter = self._flush_collector(
                            collector, chunks, file_name,
                            current_section, chunk_counter,
                        )
                        collector.reset()
                        continue
                    skipping = False

                if skipping:
                    continue

                # ---- 章标题 (>= 22pt) ----
                if level == "chapter":
                    chunk_counter = self._flush_collector(
                        collector, chunks, file_name,
                        current_section, chunk_counter,
                    )
                    collector.reset()
                    current_chapter = text
                    current_section = text
                    chunk_counter += 1
                    chunks.append(
                        self._build_chunk(
                            text=f"章节: {text}",
                            file_name=file_name,
                            chunk_id=f"chapter_{chunk_counter:05d}",
                            chunk_type="chapter",
                            section=text,
                            doc_type="redfish",
                        )
                    )
                    logger.debug(f"章标题: {text[:60]}")

                # ---- 节标题 (>= 18pt) ----
                elif level == "section":
                    chunk_counter = self._flush_collector(
                        collector, chunks, file_name,
                        current_section, chunk_counter,
                    )
                    collector.reset()
                    current_section = (
                        f"{current_chapter} > {text}"
                        if current_chapter else text
                    )
                    chunk_counter += 1
                    chunks.append(
                        self._build_chunk(
                            text=f"章节: {current_section}",
                            file_name=file_name,
                            chunk_id=f"section_{chunk_counter:05d}",
                            chunk_type="section",
                            section=current_section,
                            doc_type="redfish",
                        )
                    )
                    logger.debug(f"节标题: {text[:60]}")

                # ---- API 端点标题 (>= 16pt) ----
                elif level == "endpoint":
                    # 过滤非端点标题 (封面/版权页等)
                    if any(kw in text for kw in _NON_ENDPOINT_KEYWORDS):
                        chunk_counter = self._flush_collector(
                            collector, chunks, file_name,
                            current_section, chunk_counter,
                        )
                        collector.reset()
                        continue

                    # 标题续行: 当前端点只有标题没有实质内容
                    if not merged_parts and collector.is_title_only:
                        collector.append_title(text)
                        collector.update_from_text(text)
                        logger.debug(f"标题续行: {collector.full_title[:80]}")
                        continue

                    # flush 上一个端点
                    chunk_counter = self._flush_collector(
                        collector, chunks, file_name,
                        current_section, chunk_counter,
                    )
                    collector.reset()

                    if merged_parts:
                        # 合并段落: 从 run 字号分组中提取标题/功能/命令格式
                        self._process_merged_paragraph(
                            collector, text, merged_parts
                        )
                    else:
                        # 独立标题
                        collector.append_title(text)
                        collector.update_from_text(text)

                    logger.debug(f"API 端点: {collector.full_title[:80]}")

                # ---- 标签 (>= 13pt): 驱动子节状态转换 ----
                elif level == "label":
                    collector.set_subsection(text)
                    # 混合标签段落 (10.5pt+13pt) 可能内含 URI/方法
                    collector.update_from_text(text)

                # ---- 正文 (< 13pt) ----
                elif level == "body":
                    if self._is_noise_paragraph(text):
                        continue
                    collector.update_from_text(text)
                    collector.add_body_text(text)

            elif elem_type == "table":
                if skipping:
                    continue
                table = Table(elem, doc)

                if self._is_noise_table(table):
                    continue

                table_text = self._format_table(table)
                if not table_text.strip():
                    continue

                # 从表格文本中提取 URI 和 HTTP 方法
                collector.update_from_text(table_text)

                # 检测示例表格中的 HTTP 方法 (如 "GET https://...")
                method_match = _HTTP_METHOD_IN_TABLE.search(table_text)
                if method_match:
                    method = method_match.group(1).upper()
                    if method not in collector.http_methods:
                        collector.http_methods.append(method)

                # 如果收集器空闲且为资源概览属性表 -> 单独生成 chunk
                if not collector.is_collecting and self._is_property_table(table):
                    chunk_counter += 1
                    chunks.append(
                        self._build_chunk(
                            text=f"资源概览:\n{table_text}",
                            file_name=file_name,
                            chunk_id=f"prop_{chunk_counter:05d}",
                            chunk_type="property_table",
                            section=current_section,
                            doc_type="redfish",
                        )
                    )
                    continue

                collector.add_table(table_text)

        # ------------------------------------------------------------------
        # flush 最后一个端点
        # ------------------------------------------------------------------
        chunk_counter = self._flush_collector(
            collector, chunks, file_name,
            current_section, chunk_counter,
        )

        logger.info(f"解析完成: {file_name}, 共 {len(chunks)} 个 chunks")
        return chunks

    # ======================================================================
    # 合并段落处理
    # ======================================================================

    def _process_merged_paragraph(
        self,
        collector: ResourceCollector,
        full_text: str,
        merged_parts: List[Tuple[str, str]],
    ) -> None:
        """
        处理合并段落: 标题(16pt) + 功能(10.5pt) + 标签(13pt) + 命令格式(10.5pt)
        在同一段落中通过不同 run 字号区分。

        merged_parts 格式: [(font_level, text), ...]
        font_level: "title" / "label" / "body"

        处理流程:
          1. 提取所有 title 部分作为端点标题
          2. 遇到 label 时驱动状态机，将后续 body 归入对应子节
          3. 对完整文本做 URI/HTTP 方法正则提取作为兜底
        """
        title_parts: List[str] = []
        current_label = ""
        body_buffer: List[str] = []

        for font_level, part_text in merged_parts:
            if font_level == "title":
                title_parts.append(part_text.strip())
            elif font_level == "label":
                # flush 前一个 label 积累的 body
                if current_label and body_buffer:
                    self._apply_merged_body(collector, current_label, body_buffer)
                current_label = part_text.strip()
                collector.set_subsection(current_label)
                body_buffer.clear()
            elif font_level == "body":
                body_buffer.append(part_text)

        # flush 最后一个 label 的 body
        if current_label and body_buffer:
            self._apply_merged_body(collector, current_label, body_buffer)
        elif body_buffer:
            for line in body_buffer:
                collector.description_lines.append(line)

        # 设置标题
        if title_parts:
            collector.append_title(" ".join(title_parts))

        # 兜底: 对完整文本做正则提取
        collector.update_from_text(full_text)

    @staticmethod
    def _apply_merged_body(
        collector: ResourceCollector,
        label: str,
        body_lines: List[str],
    ) -> None:
        """将合并段落中标签对应的正文应用到收集器."""
        body_text = "".join(body_lines).strip()
        if not body_text:
            return

        if "命令功能" in label or "功能" in label:
            collector.description_lines.append(body_text)
        elif "命令格式" in label:
            collector._extract_uri_and_method(body_text)
        elif "参数" in label:
            collector.parameter_tables.append(body_text)
        elif "请求消息体" in label or "请求" in label:
            collector.request_body_parts.append(body_text)
        elif "响应" in label or "输出" in label:
            collector.response_example_parts.append(body_text)
        elif "使用" in label:
            collector.usage_guide_lines.append(body_text)
        else:
            collector.description_lines.append(body_text)

    # ======================================================================
    # flush 辅助
    # ======================================================================

    def _flush_collector(
        self,
        collector: ResourceCollector,
        chunks: List[Dict[str, Any]],
        file_name: str,
        section: str,
        chunk_counter: int,
    ) -> int:
        """
        将 collector 中的端点数据 flush 为一个 chunk 追加到 chunks.

        v3 优化:
          - text 开头放置 URI + HTTP 方法 (提升 exact_uri 检索命中率)
          - 追加同义词行 (提升 fuzzy/scenario 检索召回)
          - metadata 新增 full_uri, uri_priority, chunk_priority
          - property_table chunk 标记低优先级

        Returns:
            更新后的 chunk_counter
        """
        if not collector.is_collecting:
            return chunk_counter

        full_title = collector.full_title
        if len(full_title.strip()) < 5:
            return chunk_counter

        chunk_counter += 1

        # -- 提取中英文标题 --
        chinese_name, english_name = self._extract_names(full_title)

        # -- 主 URI 和 schema --
        primary_uri = ""
        schema_name = ""
        if collector.uris:
            primary_uri = min(collector.uris, key=len)
            schema_match = _SCHEMA_FROM_URI.search(primary_uri)
            if schema_match:
                schema_name = _normalize_schema(schema_match.group(1))

        # -- 构建 text (URI-FIRST 布局) --
        parts: List[str] = []

        # 1) URI 行 (放在最前面, 提升 exact_uri 检索)
        if primary_uri:
            method_str = "/".join(collector.http_methods) if collector.http_methods else "GET"
            parts.append(f"URI: {primary_uri}  [{method_str}]")

        # 2) 标题行
        parts.append(f"API: {full_title}")

        # 3) 同义词行 (基于 schema_name 和关键词注入)
        synonyms = self._build_synonyms(
            full_title, chinese_name, english_name,
            primary_uri, schema_name, collector,
        )
        if synonyms:
            parts.append(f"同义词: {', '.join(synonyms)}")

        # 4) HTTP 方法
        if collector.http_methods:
            parts.append(f"HTTP 方法: {', '.join(collector.http_methods)}")

        # 5) 功能描述
        desc = "\n".join(collector.description_lines).strip()
        if desc:
            parts.append(f"功能描述:\n{desc}")

        # 6) 请求头
        if collector.request_headers:
            parts.append("请求头:\n" + "\n".join(collector.request_headers))

        # 7) 请求消息体
        if collector.request_body_parts:
            parts.append("请求消息体:\n" + "\n".join(collector.request_body_parts))

        # 8) 参数说明
        if collector.parameter_tables:
            parts.append("参数说明:\n" + "\n\n".join(collector.parameter_tables))

        # 9) 使用指南
        guide = "\n".join(collector.usage_guide_lines).strip()
        if guide:
            parts.append(f"使用指南:\n{guide}")

        # 10) 请求示例
        if collector.request_example_parts:
            parts.append("请求示例:\n" + "\n\n".join(collector.request_example_parts))

        # 11) 响应示例
        if collector.response_example_parts:
            parts.append("响应示例:\n" + "\n\n".join(collector.response_example_parts))

        # 12) 输出说明
        if collector.response_field_tables:
            parts.append("输出说明:\n" + "\n\n".join(collector.response_field_tables))

        # 13) 状态码
        if collector.status_code_tables:
            parts.append("状态码:\n" + "\n\n".join(collector.status_code_tables))

        # 14) 注意事项
        if collector.notes_lines:
            parts.append("注意事项:\n" + "\n".join(collector.notes_lines))

        full_text = "\n\n".join(parts)
        if len(full_text.strip()) < 10:
            return chunk_counter

        # -- 构建 metadata --
        extra: Dict[str, Any] = {}
        extra["full_title"] = full_title[:500]
        extra["chunk_priority"] = 1  # resource chunk 为最高优先级

        if chinese_name:
            extra["chinese_name"] = chinese_name
        if english_name:
            extra["english_name"] = english_name

        # resource_uri (主 URI, 最短)
        if primary_uri:
            extra["resource_uri"] = primary_uri
            extra["full_uri"] = primary_uri
            extra["uri_priority"] = 1

        # 额外 URI 列表
        if len(collector.uris) > 1:
            extra["all_uris"] = ", ".join(collector.uris[:5])

        # schema_name
        if schema_name:
            extra["schema_name"] = schema_name

        # http_method (逗号分隔字符串)
        if collector.http_methods:
            extra["http_method"] = ",".join(collector.http_methods)

        # description (用于检索)
        if collector.description_lines:
            extra["description"] = collector.description_lines[0][:200]

        # example_request (截断)
        if collector.request_body_parts:
            extra["example_request"] = "\n".join(
                collector.request_body_parts
            )[:500]

        # example_response (截断)
        if collector.response_example_parts:
            extra["example_response"] = "\n".join(
                collector.response_example_parts
            )[:500]

        # section path
        section_path = (
            f"{section} > {full_title}"
            if section and full_title
            else (full_title or section)
        )

        chunks.append(
            self._build_chunk(
                text=full_text,
                file_name=file_name,
                chunk_id=f"api_{chunk_counter:05d}",
                chunk_type="resource",
                section=section_path[:500],
                doc_type="redfish",
                **extra,
            )
        )

        logger.debug(
            f"Flush: [api_{chunk_counter:05d}] "
            f"URI={primary_uri or '-'} "
            f"Methods={extra.get('http_method', '-')} "
            f"| {full_title[:60]}"
        )
        return chunk_counter

    # ======================================================================
    # 同义词生成
    # ======================================================================

    @staticmethod
    def _build_synonyms(
        full_title: str,
        chinese_name: Optional[str],
        english_name: Optional[str],
        primary_uri: str,
        schema_name: str,
        collector: ResourceCollector,
    ) -> List[str]:
        """
        基于 schema_name、标题、URI 生成同义词列表, 提升 fuzzy/scenario 检索召回.

        同义词来源:
          1. _SYNONYM_MAP 中 schema_name 对应的中文/英文同义词
          2. 标题中的关键词
          3. HTTP 方法隐含的操作语义
        """
        synonyms: List[str] = []

        # 1. Schema-based 同义词
        if schema_name and schema_name in _SYNONYM_MAP:
            synonyms.extend(_SYNONYM_MAP[schema_name])

        # 2. URI 路径中的资源名片段 (如 "AccountService" -> "账号")
        if primary_uri:
            for key, vals in _SYNONYM_MAP.items():
                if key.lower() in primary_uri.lower() and key != schema_name:
                    synonyms.extend(vals[:2])  # 每个关联资源最多取 2 个

        # 3. HTTP 方法隐含的操作语义
        for method in collector.http_methods:
            if method == "POST":
                synonyms.extend(["创建", "添加", "新增", "Create"])
            elif method == "PATCH":
                synonyms.extend(["修改", "更新", "编辑", "Update", "设置"])
            elif method == "DELETE":
                synonyms.extend(["删除", "移除", "Delete"])
            elif method == "GET":
                if "查询" not in " ".join(synonyms):
                    synonyms.extend(["查询", "获取", "查看", "Get"])

        # 4. 去重并限制数量
        seen = set()
        unique: List[str] = []
        for s in synonyms:
            if s not in seen:
                seen.add(s)
                unique.append(s)
        return unique[:12]  # 最多 12 个同义词

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

    def _classify_paragraph(
        self, para: Paragraph
    ) -> Tuple[str, str, Optional[List[Tuple[str, str]]]]:
        """
        根据字号对段落分类，检测并解析合并段落。

        Returns:
            (level, text, merged_parts)
            level:
              "chapter"  - 章标题 (>= 22pt)
              "section"  - 节标题 (>= 18pt)
              "endpoint" - API 端点标题 (>= 16pt)
              "label"    - 标签 (>= 13pt)
              "body"     - 正文 (< 13pt)
              "empty"    - 空段落
            merged_parts: 仅 level=="endpoint" 且为合并段落时有值，
              格式: [(font_level, text), ...]  font_level: "title"/"label"/"body"
        """
        text = para.text.strip()
        if not text:
            return "empty", text, None

        # 收集 run 级字号信息
        run_font_sizes: List[Tuple[Optional[float], str]] = []
        for run in para.runs:
            run_text = run.text.strip()
            if not run_text:
                continue
            size = run.font.size.pt if run.font.size else None
            run_font_sizes.append((size, run_text))

        # 计算 max 字号
        max_size: Optional[float] = None
        for size, _ in run_font_sizes:
            if size is not None:
                if max_size is None or size > max_size:
                    max_size = size
        if max_size is None:
            max_size = self._get_font_size_pt(para)
        if max_size is None:
            return "body", text, None

        # ---- 层级分类 ----
        if max_size >= FONT_SIZE_CHAPTER:
            return "chapter", text, None

        if max_size >= FONT_SIZE_SECTION:
            return "section", text, None

        if max_size >= FONT_SIZE_ENDPOINT:
            # 检测合并段落: 存在 >=16pt 和 <16pt 的 run
            has_title = any(
                s is not None and s >= FONT_SIZE_ENDPOINT
                for s, _ in run_font_sizes
            )
            has_body = any(
                s is not None and s < FONT_SIZE_ENDPOINT
                for s, _ in run_font_sizes
            )
            if has_title and has_body:
                merged = self._parse_merged_runs(run_font_sizes)
                return "endpoint", text, merged
            return "endpoint", text, None

        if max_size >= FONT_SIZE_LABEL:
            return "label", text, None

        return "body", text, None

    @staticmethod
    def _parse_merged_runs(
        run_font_sizes: List[Tuple[Optional[float], str]]
    ) -> List[Tuple[str, str]]:
        """
        解析合并段落中的 run，按字号分组。

        分组规则:
          >= 16pt  -> "title"
          >= 13pt  -> "label"
          < 13pt   -> "body"
          None     -> 沿用前一个分组的类型，默认 "body"

        相邻相同类型的分组会合并。
        """
        parts: List[Tuple[str, str]] = []

        for size, run_text in run_font_sizes:
            if size is not None and size >= FONT_SIZE_ENDPOINT:
                font_level = "title"
            elif size is not None and size >= FONT_SIZE_LABEL:
                font_level = "label"
            elif size is not None:
                font_level = "body"
            else:
                # size 为 None: 沿用前一个分组的类型
                font_level = parts[-1][0] if parts else "body"

            # 合并相邻相同类型
            if parts and parts[-1][0] == font_level:
                prev_level, prev_text = parts[-1]
                parts[-1] = (prev_level, prev_text + run_text)
            else:
                parts.append((font_level, run_text))

        return parts

    @staticmethod
    def _is_noise_table(table: Table) -> bool:
        """检测噪声表格 (页眉/页脚/TOC)."""
        num_rows = len(table.rows)
        if num_rows == 0:
            return True

        all_text = " ".join(
            cell.text.strip()
            for row in table.rows
            for cell in row.cells
        )

        # 页脚: "文档版本" + "版权所有" (约 1700 个)
        if "文档版本" in all_text and "版权所有" in all_text:
            return True

        # 页眉: "Atlas 系列" + "接口参考" (约 2281 个)
        if "Atlas 系列" in all_text and "接口参考" in all_text:
            return True

        # 页眉变体: "接口介绍" (短表格)
        if num_rows <= 2 and "接口介绍" in all_text and len(all_text) < 200:
            return True

        # 纯 Auth Token 表格 (1 行 1 列, 短文本, 约 800 个)
        if num_rows == 1 and len(table.rows[0].cells) == 1:
            cell_text = table.rows[0].cells[0].text.strip()
            if cell_text.startswith("X-Auth-Token:") and len(cell_text) < 120:
                return True

        # TOC 条目 (连续省略号)
        if _TOC_DOT_PATTERN.search(all_text):
            return True

        return False

    @staticmethod
    def _is_property_table(table: Table) -> bool:
        """
        检测是否为资源概览属性表。
        特征: 表头包含 "URL" + ("属性" 或 "允许操作")
        """
        if len(table.rows) < 2:
            return False
        header_cells = [
            cell.text.strip()
            for cell in table.rows[0].cells
        ]
        header_text = " ".join(header_cells)
        has_url = "URL" in header_text
        has_attr = "属性" in header_text or "允许操作" in header_text
        return has_url and has_attr

    @staticmethod
    def _is_noise_paragraph(text: str) -> bool:
        """检测页眉页脚噪声段落."""
        for kw in _NOISE_KEYWORDS_PARA:
            if kw in text:
                return True
        return False

    @staticmethod
    def _extract_names(title: str) -> Tuple[Optional[str], Optional[str]]:
        """
        从标题中提取中文名和英文名。

        标题格式示例:
          "3.1.1 查询Redfish 版本信息"
          "3.2.3 修改指定管理资源信息（Update Manager）"

        Returns:
            (chinese_name, english_name)
        """
        # 去掉编号前缀
        cleaned = re.sub(r"^\d+[\.\d]*\s+", "", title).strip()

        # 匹配中英文
        m = re.match(
            r"([^\(（]+?)"
            r"(?:[\s]*[\(（](.+?)[\)）])?$",
            cleaned,
        )
        if not m:
            return cleaned or None, None

        chinese = m.group(1).strip() if m.group(1) else None
        english = m.group(2).strip() if m.group(2) else None

        # 清理英文名中的非 ASCII 字符
        if english:
            english = re.sub(r"[^\x20-\x7e]", "", english).strip()

        return chinese or None, english or None

    @staticmethod
    def _format_table(table: Table) -> str:
        """
        将表格转换为可读文本。

        格式: "列1 | 列2", 行间换行。
        合并单元格通过 tc 元素 id 去重。
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
