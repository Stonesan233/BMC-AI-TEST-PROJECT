# -*- coding: utf-8 -*-
"""
Hybrid Retriever: 向量检索 + BM25 关键词检索 + RRF 融合

架构:
  1. 向量检索: Chroma cosine 相似度
  2. BM25 关键词检索: 内置 BM25Okapi 实现
  3. RRF (Reciprocal Rank Fusion) 融合两路排序

用法:
  retriever = HybridRetriever(chroma_path="./shared/rag_index")
  results = retriever.search("获取CPU温度", query_embedding=emb, top_k=3)

依赖: chromadb, jieba (可选, 无 jieba 时退化为字符级分词)
"""

import logging
import math
import re
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

import chromadb

logger = logging.getLogger("rag.retriever")

# ---------------------------------------------------------------------------
# jieba 可选依赖
# ---------------------------------------------------------------------------
try:
    import jieba
    jieba.setLogLevel(logging.WARNING)
    _HAS_JIEBA = True
except ImportError:
    _HAS_JIEBA = False


class HybridRetriever:
    """
    混合检索器: 向量 + BM25 + RRF 融合.

    Args:
        chroma_path: Chroma 持久化目录
        collection_name: Collection 名称
        alpha: 向量检索权重 (0.0-1.0), BM25 权重 = 1-alpha
               1.0 = 纯向量, 0.0 = 纯关键词, 0.7 = 混合(默认)
    """

    def __init__(
        self,
        chroma_path: str = "./shared/rag_index",
        collection_name: str = "openubmc_rag",
        alpha: float = 0.7,
    ):
        self.alpha = alpha

        self.chroma_client = chromadb.PersistentClient(path=chroma_path)
        self.collection = self.chroma_client.get_collection(collection_name)
        logger.info(f"Chroma: {collection_name} ({self.collection.count()} chunks)")

        self._load_documents()
        self._build_bm25_index()

    # ==================================================================
    # 文档加载
    # ==================================================================

    def _load_documents(self):
        """从 Chroma 加载全部文档, 用于 BM25 索引."""
        total = self.collection.count()
        ids, docs, metas = [], [], []
        batch = 200
        offset = 0
        while offset < total:
            result = self.collection.get(
                include=["documents", "metadatas"],
                limit=batch,
                offset=offset,
            )
            ids.extend(result["ids"])
            docs.extend(result["documents"])
            metas.extend(result["metadatas"])
            offset += batch

        self.doc_ids = ids
        self.documents = docs
        self.metadatas = metas
        self.id_to_idx = {did: i for i, did in enumerate(ids)}
        logger.info(f"Loaded {len(ids)} documents for BM25")

    # ==================================================================
    # 分词
    # ==================================================================

    @staticmethod
    def _tokenize(text: str) -> List[str]:
        """
        中英文混合分词.

        有 jieba: jieba.cut_for_search (搜索模式, 产出更多子词)
        无 jieba: 字符级 (中文字符 + 英文单词 + hex 值)
        """
        text_lower = text.lower().strip()

        if _HAS_JIEBA:
            words = [w.strip() for w in jieba.cut_for_search(text_lower) if w.strip()]
        else:
            # 退化: 提取英文/数字串 + 中文字符
            words = re.findall(r'[a-z0-9]+|[\u4e00-\u9fff]', text_lower)

        return [
            w for w in words
            if w and (w.isalnum() or '\u4e00' <= w[0] <= '\u9fff')
        ]

    # ==================================================================
    # BM25 索引
    # ==================================================================

    def _build_bm25_index(self, k1: float = 1.5, b: float = 0.75):
        """
        构建 BM25 索引.

        参数:
          k1: 词频饱和参数 (默认 1.5)
          b: 文档长度归一化参数 (默认 0.75)
        """
        self.k1 = k1
        self.b = b

        self.tokenized_docs = [self._tokenize(doc) for doc in self.documents]

        # IDF: log((N - df + 0.5) / (df + 0.5) + 1)
        N = len(self.tokenized_docs)
        df: Dict[str, int] = {}
        for tokens in self.tokenized_docs:
            for token in set(tokens):
                df[token] = df.get(token, 0) + 1

        self.idf: Dict[str, float] = {}
        for token, freq in df.items():
            self.idf[token] = math.log((N - freq + 0.5) / (freq + 0.5) + 1)

        # 平均文档长度
        total_len = sum(len(t) for t in self.tokenized_docs)
        self.avgdl = total_len / max(N, 1)

        # 预计算每个文档的词频和长度
        self.doc_tf = [Counter(tokens) for tokens in self.tokenized_docs]
        self.doc_len = [len(tokens) for tokens in self.tokenized_docs]

        logger.info(
            f"BM25: {N} docs, {len(self.idf)} tokens, avgdl={self.avgdl:.1f}"
        )

    def _bm25_search(
        self,
        query: str,
        top_k: int,
        chunk_type: Optional[str] = None,
    ) -> List[Tuple[int, float]]:
        """
        BM25 关键词检索.

        Returns:
            [(doc_idx, bm25_score)] 按 score 降序
        """
        query_tokens = self._tokenize(query)
        if not query_tokens:
            return []

        scores: List[Tuple[int, float]] = []
        for i in range(len(self.documents)):
            # 按 chunk_type 过滤
            if chunk_type:
                if self.metadatas[i].get("chunk_type") != chunk_type:
                    continue

            score = 0.0
            tf_map = self.doc_tf[i]
            dl = self.doc_len[i]

            for qt in query_tokens:
                if qt not in self.idf:
                    continue
                tf = tf_map.get(qt, 0)
                if tf == 0:
                    continue
                idf_val = self.idf[qt]
                num = tf * (self.k1 + 1)
                den = tf + self.k1 * (1 - self.b + self.b * dl / self.avgdl)
                score += idf_val * num / den

            if score > 0:
                scores.append((i, score))

        scores.sort(key=lambda x: -x[1])
        return scores[:top_k]

    # ==================================================================
    # 向量检索
    # ==================================================================

    def _vector_search(
        self,
        query_embedding: List[float],
        top_k: int,
        chunk_type: Optional[str] = None,
    ) -> List[Tuple[int, float]]:
        """
        Chroma 向量检索.

        Returns:
            [(doc_idx, cosine_distance)] 按 distance 升序
        """
        where_filter = {"chunk_type": chunk_type} if chunk_type else None
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=top_k,
            where=where_filter,
        )

        rank_list: List[Tuple[int, float]] = []
        for doc_id, dist in zip(results["ids"][0], results["distances"][0]):
            idx = self.id_to_idx.get(doc_id)
            if idx is not None:
                rank_list.append((idx, dist))
        return rank_list

    # ==================================================================
    # RRF 融合
    # ==================================================================

    @staticmethod
    def _rrf_fuse(
        vector_ranks: List[Tuple[int, float]],
        keyword_ranks: List[Tuple[int, float]],
        alpha: float = 0.7,
        k: int = 60,
    ) -> List[Tuple[int, float]]:
        """
        Reciprocal Rank Fusion.

        score(d) = alpha / (k + rank_v(d)) + (1-alpha) / (k + rank_k(d))

        Args:
            vector_ranks: [(idx, dist)] 向量检索排序
            keyword_ranks: [(idx, bm25_score)] BM25 检索排序
            alpha: 向量权重 (0.0-1.0)
            k: RRF 常数 (默认 60)
        """
        scores: Dict[int, float] = {}

        for rank, (idx, _) in enumerate(vector_ranks):
            scores[idx] = scores.get(idx, 0.0) + alpha / (k + rank + 1)

        beta = 1 - alpha
        for rank, (idx, _) in enumerate(keyword_ranks):
            scores[idx] = scores.get(idx, 0.0) + beta / (k + rank + 1)

        return sorted(scores.items(), key=lambda x: -x[1])

    # ==================================================================
    # Public API
    # ==================================================================

    def search(
        self,
        query: str,
        query_embedding: Optional[List[float]] = None,
        top_k: int = 5,
        alpha: Optional[float] = None,
        chunk_type: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        """
        混合检索: 向量 + BM25 + RRF.

        Args:
            query: 查询文本
            query_embedding: 预计算的查询向量 (None 则仅关键词检索)
            top_k: 返回结果数
            alpha: 向量权重 (None = 使用默认)
                   1.0 = 纯向量, 0.0 = 纯关键词
            chunk_type: 过滤 chunk_type (如 "command")

        Returns:
            [{"id", "document", "metadata", "score"}]
        """
        a = alpha if alpha is not None else self.alpha
        expand = top_k * 3  # 扩大候选集再截断

        # 向量检索
        vector_ranks: List[Tuple[int, float]] = []
        if query_embedding is not None and a > 0:
            vector_ranks = self._vector_search(query_embedding, expand, chunk_type)

        # BM25 关键词检索
        keyword_ranks: List[Tuple[int, float]] = []
        if a < 1.0:
            keyword_ranks = self._bm25_search(query, expand, chunk_type)

        # 融合
        if vector_ranks and keyword_ranks:
            fused = self._rrf_fuse(vector_ranks, keyword_ranks, alpha=a)
        elif vector_ranks:
            fused = [(idx, -dist) for idx, dist in vector_ranks]
        elif keyword_ranks:
            fused = keyword_ranks
        else:
            return []

        results: List[Dict[str, Any]] = []
        for idx, score in fused[:top_k]:
            results.append({
                "id": self.doc_ids[idx],
                "document": self.documents[idx],
                "metadata": self.metadatas[idx],
                "score": score,
            })

        return results
