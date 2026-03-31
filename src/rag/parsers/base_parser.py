# -*- coding: utf-8 -*-
"""
文档解析器抽象基类

所有文档解析器必须继承 BaseParser 并实现 parse() 方法。

输出格式统一: List[Dict[str, Any]]
  每个 dict 包含:
    - "text": str       -- 用于 embedding 的文本内容
    - "metadata": dict  -- 元数据，必须包含以下字段:
        - doc_type: str     (文档类型，如 "ipmi", "redfish")
        - file_name: str    (文件名)
        - chunk_id: str     (唯一分块标识)
        - chunk_type: str   (分块类型，如 "command", "chapter", "parameter_table")
        - section: str      (所属章节)
"""

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List

logger = logging.getLogger("rag.parser")


class BaseParser(ABC):
    """
    文档解析器抽象基类。

    子类必须实现 parse() 方法，返回统一格式的分块列表。
    可通过 _build_chunk() 辅助方法快速构建符合规范的分块字典。
    """

    @abstractmethod
    async def parse(self, file_path: str) -> List[Dict[str, Any]]:
        """
        解析文档文件，返回分块列表。

        Args:
            file_path: 文档文件的绝对或相对路径

        Returns:
            List[Dict[str, Any]]，每个元素包含 "text" 和 "metadata" 键。
            metadata 中至少包含: doc_type, file_name, chunk_id, chunk_type, section
        """
        ...

    def _build_chunk(
        self,
        text: str,
        file_name: str,
        chunk_id: str,
        chunk_type: str,
        section: str,
        doc_type: str,
        **extra_metadata: Any,
    ) -> Dict[str, Any]:
        """
        构建标准分块字典。

        Args:
            text: 用于 embedding 的文本内容
            file_name: 源文件名
            chunk_id: 唯一分块 ID
            chunk_type: 分块类型 (command / chapter / parameter_table 等)
            section: 所属章节
            doc_type: 文档类型 (ipmi / redfish 等)
            **extra_metadata: 额外的 metadata 字段

        Returns:
            {"text": str, "metadata": dict}
        """
        metadata: Dict[str, Any] = {
            "doc_type": doc_type,
            "file_name": file_name,
            "chunk_id": chunk_id,
            "chunk_type": chunk_type,
            "section": section,
        }
        metadata.update(extra_metadata)
        return {"text": text, "metadata": metadata}
