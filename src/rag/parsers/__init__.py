# -*- coding: utf-8 -*-
"""
文档解析器模块

提供可扩展的 Parser 注册机制，通过 doc_type 或文件名关键词自动选择对应 Parser。

支持的 doc_type:
  - "ipmi":    IPMI 接口说明文档 (DocxIPMIParser)
  - "redfish": Redfish API 接口参考文档 (DocxRedfishParser)
  - "cli":     BMC CLI 命令用户指南文档 (DocxCLIParser)

自动检测规则 (doc_type 未指定时):
  - 文件名包含 "Redfish"   -> DocxRedfishParser
  - 文件名包含 "IPMI"      -> DocxIPMIParser
  - 文件名包含 "用户指南"   -> DocxCLIParser
  - 文件名包含 "CLI"        -> DocxCLIParser
  - 默认 (.docx 后缀)       -> DocxIPMIParser
"""

from pathlib import Path
from typing import Dict, Optional, Type

from src.rag.parsers.base_parser import BaseParser
from src.rag.parsers.docx_cli_parser import DocxCLIParser
from src.rag.parsers.docx_ipmi_parser import DocxIPMIParser
from src.rag.parsers.docx_redfish_parser import DocxRedfishParser

# 文件后缀 -> 默认 Parser 类映射
PARSER_REGISTRY: Dict[str, Type[BaseParser]] = {
    ".docx": DocxIPMIParser,
}

# 文件名关键词 -> Parser 类映射 (优先于后缀匹配)
_FILENAME_KEYWORD_MAP: Dict[str, Type[BaseParser]] = {
    "redfish": DocxRedfishParser,
    "ipmi": DocxIPMIParser,
    "用户指南": DocxCLIParser,
    "cli": DocxCLIParser,
}


def get_parser(file_path: str, doc_type: Optional[str] = None) -> BaseParser:
    """
    根据 doc_type 或文件名自动选择 Parser。

    选择优先级:
      1. doc_type 显式指定 (如 "ipmi" / "redfish" / "cli")
      2. 文件名关键词匹配 (如文件名含 "Redfish" -> DocxRedfishParser)
      3. 文件后缀默认映射 (.docx -> DocxIPMIParser)

    Args:
        file_path: 文件路径，用于提取后缀和文件名
        doc_type: 强制指定文档类型 (ipmi / redfish / cli)

    Returns:
        BaseParser 子类实例

    Raises:
        ValueError: 无法匹配到任何已注册的 Parser
    """
    # 优先级 1: 显式 doc_type
    if doc_type == "ipmi":
        return DocxIPMIParser()
    if doc_type == "redfish":
        return DocxRedfishParser()
    if doc_type == "cli":
        return DocxCLIParser()

    # 优先级 2: 文件名关键词匹配
    file_name = Path(file_path).name.lower()
    for keyword, parser_cls in _FILENAME_KEYWORD_MAP.items():
        if keyword in file_name:
            return parser_cls()

    # 优先级 3: 后缀默认映射
    ext = Path(file_path).suffix.lower()
    parser_cls = PARSER_REGISTRY.get(ext)
    if not parser_cls:
        raise ValueError(
            f"No parser registered for extension '{ext}'. "
            f"Registered: {list(PARSER_REGISTRY.keys())}"
        )
    return parser_cls()
