# -*- coding: utf-8 -*-
"""
统一配置加载与验证模块

职责:
  - 加载 config.yaml
  - 解析环境变量引用（${ENV_VAR}）并读取 .env 文件
  - 验证 providers 和 models 结构完整性
  - 提供类型安全的访问接口

用法:
  from src.core.config import load_config
  cfg = load_config("config/config.yaml")
  exec_cfg = cfg.models.exec
  provider  = cfg.get_provider(exec_cfg.provider)
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger("core.config")


# ======================================================================
# Pydantic 配置模型
# ======================================================================

class ProviderConfig(BaseModel):
    """单个服务提供者配置"""
    base_url: str = Field(..., description="OpenAI-compatible API base URL")
    api_key: str = Field(..., description="API Key（支持 ${ENV_VAR} 格式）")
    timeout: float = Field(default=120.0, description="HTTP 超时（秒）")


class ComponentModel(BaseModel):
    """组件模型配置（Exec / Judge / Embedding / Rewrite 通用）"""
    provider: str = Field(..., description="引用 providers 中的名称")
    model: str = Field(..., description="模型标识")
    temperature: float = Field(default=0.1, description="采样温度")
    max_tokens: int = Field(default=4096, description="最大输出 token")
    dimension: Optional[int] = Field(default=None, description="向量维度（仅 Embedding）")


class ModelsConfig(BaseModel):
    """所有组件的模型配置"""
    exec: ComponentModel
    judge: ComponentModel
    embedding: ComponentModel
    rewrite: ComponentModel


class RAGConfig(BaseModel):
    """RAG 检索模块配置"""
    enabled: bool = True
    chroma_path: str = "./shared/rag_index"
    collection_name: str = "openubmc_rag"
    alpha: float = 0.7
    top_k: int = 3
    default_interface: str = "auto"
    auto_detect: bool = True
    enable_rewrite: bool = True
    rewrite_weight: float = 2.0
    rewrite_top_k: int = 5
    rerank_enabled: bool = False


class AppConfig(BaseModel):
    """应用顶层配置"""
    providers: Dict[str, ProviderConfig]
    models: ModelsConfig
    agent: Dict[str, Any] = Field(default_factory=dict)
    rag: RAGConfig = Field(default_factory=RAGConfig)
    target: Dict[str, Any] = Field(default_factory=dict)
    storage: Dict[str, Any] = Field(default_factory=dict)
    logging: Dict[str, Any] = Field(default_factory=dict)


# ======================================================================
# 环境变量解析
# ======================================================================

_ENV_VAR_PATTERN = re.compile(r"\$\{(\w+)\}")


def resolve_env_value(value: str) -> str:
    """
    解析字符串中的 ${ENV_VAR} 引用。

    优先从 os.environ 读取，然后从 .env 文件读取。
    未找到时返回原始字符串（发出警告）。
    """
    match = _ENV_VAR_PATTERN.fullmatch(value.strip())
    if not match:
        return value

    env_var = match.group(1)
    resolved = os.environ.get(env_var, "")
    if resolved:
        return resolved

    # 尝试从 .env 文件加载
    resolved = _read_dotenv_var(env_var)
    if resolved:
        return resolved

    logger.warning(f"环境变量 {env_var} 未设置，请在 .env 文件或系统环境变量中配置")
    return value


def _read_dotenv_var(key: str) -> str:
    """从项目根目录 .env 文件中读取指定变量。"""
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        # 也尝试 src/core 的上两级目录（项目根）
        env_path = Path(__file__).resolve().parents[2] / ".env"
    if not env_path.exists():
        return ""

    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        if k.strip() == key:
            return v.strip().strip("'\"")
    return ""


# ======================================================================
# 配置加载
# ======================================================================

def load_config(config_path: str = "config/config.yaml") -> AppConfig:
    """
    加载并验证配置文件。

    优先加载 config.yaml（真实配置，gitignored），
    不存在时回退到 config_example.yaml（模板）。

    Args:
        config_path: 配置文件路径（相对于项目根目录）

    Returns:
        AppConfig 实例

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置验证失败
    """
    path = Path(config_path)
    if not path.exists():
        # 回退到 example
        example_path = Path("config/config_example.yaml")
        if example_path.exists():
            logger.warning(
                f"配置文件 {config_path} 不存在，"
                f"使用模板 {example_path}（请复制为 config.yaml 并填入实际配置）"
            )
            path = example_path
        else:
            raise FileNotFoundError(
                f"配置文件不存在: {config_path}，"
                f"模板也不存在: {example_path}"
            )

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)

    if not raw or not isinstance(raw, dict):
        raise ValueError("配置文件内容为空或格式错误")

    # 解析 providers 中的环境变量
    providers_raw = raw.get("providers", {})
    if not providers_raw:
        raise ValueError("config.yaml 中缺少 providers 配置段")

    resolved_providers = {}
    for name, prov in providers_raw.items():
        if not isinstance(prov, dict):
            raise ValueError(f"providers.{name} 必须是字典")
        api_key_raw = prov.get("api_key", "")
        resolved_providers[name] = ProviderConfig(
            base_url=prov.get("base_url", ""),
            api_key=resolve_env_value(api_key_raw),
            timeout=float(prov.get("timeout", 120.0)),
        )

    # 验证 providers 的 base_url
    for name, prov in resolved_providers.items():
        if not prov.base_url:
            raise ValueError(f"providers.{name}.base_url 不能为空")

    # 解析 models
    models_raw = raw.get("models", {})
    if not models_raw:
        raise ValueError("config.yaml 中缺少 models 配置段")

    models = ModelsConfig(
        exec=_parse_component(models_raw.get("exec", {})),
        judge=_parse_component(models_raw.get("judge", {})),
        embedding=_parse_component(models_raw.get("embedding", {})),
        rewrite=_parse_component(models_raw.get("rewrite", {})),
    )

    # 验证 models 中引用的 provider 是否存在
    for comp_name in ("exec", "judge", "embedding", "rewrite"):
        comp: ComponentModel = getattr(models, comp_name)
        if comp.provider not in resolved_providers:
            raise ValueError(
                f"models.{comp_name}.provider = '{comp.provider}' "
                f"但在 providers 中未定义。可用的 provider: {list(resolved_providers.keys())}"
            )

    # 解析 RAG
    rag_raw = raw.get("rag", {})
    rag = RAGConfig(**{k: v for k, v in rag_raw.items() if k in RAGConfig.model_fields})

    cfg = AppConfig(
        providers=resolved_providers,
        models=models,
        agent=raw.get("agent", {}),
        rag=rag,
        target=raw.get("target", {}),
        storage=raw.get("storage", {}),
        logging=raw.get("logging", {}),
    )

    logger.info(
        f"配置加载完成: providers={list(cfg.providers.keys())}, "
        f"exec={cfg.models.exec.provider}/{cfg.models.exec.model}, "
        f"judge={cfg.models.judge.provider}/{cfg.models.judge.model}, "
        f"embed={cfg.models.embedding.provider}/{cfg.models.embedding.model}, "
        f"rewrite={cfg.models.rewrite.provider}/{cfg.models.rewrite.model}"
    )
    return cfg


def _parse_component(raw: dict) -> ComponentModel:
    """从原始字典解析 ComponentModel，设置合理默认值。"""
    return ComponentModel(
        provider=raw.get("provider", "default"),
        model=raw.get("model", ""),
        temperature=float(raw.get("temperature", 0.1)),
        max_tokens=int(raw.get("max_tokens", 4096)),
        dimension=raw.get("dimension"),
    )
