# -*- coding: utf-8 -*-
"""
文档解析器模块

提供可扩展的 Parser 注册机制，通过文件后缀自动选择对应 Parser。
"""

from pathlib import Path
from typing import Dict, Optional, Type

from src.rag.parsers.base_parser import BaseParser
from src.rag.parsers.docx_ipmi_parser import DocxIPMIParser

# 文件后缀 -> Parser 类映射（可扩展）
PARSER_REGISTRY: Dict[str, Type[BaseParser]] = {
    ".docx": DocxIPMIParser,
    # TODO: 未来新增 Redfish Parser
    # ".pdf": RedfishPDFParser,
    # ".html": RedfishHTMLParser,
    # ".yaml": YAMLTestcaseParser,
}


def get_parser(file_path: str, doc_type: Optional[str] = None) -> BaseParser:
    """
    根据 doc_type 或文件后缀自动选择 Parser。

    Args:
        file_path: 文件路径，用于提取后缀
        doc_type: 强制指定文档类型 (ipmi / redfish / yaml 等)

    Returns:
        BaseParser 子类实例

    Raises:
        ValueError: 无法匹配到任何已注册的 Parser
    """
    if doc_type == "ipmi":
        return DocxIPMIParser()
    # TODO: elif doc_type == "redfish": return RedfishParser()

    ext = Path(file_path).suffix.lower()
    parser_cls = PARSER_REGISTRY.get(ext)
    if not parser_cls:
        raise ValueError(
            f"No parser registered for extension '{ext}'. "
            f"Registered: {list(PARSER_REGISTRY.keys())}"
        )
    return parser_cls()
