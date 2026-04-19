# -*- coding: utf-8 -*-
"""
openUBMC AI 测试框架 - LLM 配置管理模块

支持多种 LLM Provider：
- 阿里百炼 DashScope (Qwen3)
- 智谱 GLM
- MiniMax
- 自定义 OpenAI-compatible

用法：
    from src.config.llm_config import load_judge_llm_config, get_judge_client

    # 方式1: 从环境变量加载
    config = load_judge_llm_config()  # 自动读取 QWEN3_* 或 GLM_* 环境变量

    # 方式2: 直接指定
    config = JudgeLLMConfig(
        provider="dashscope",
        base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        api_key="${DASHSCOPE_API_KEY}",
        model="qwen3-235b-a22b",
        temperature=0.0,
        max_tokens=8192,
    )
"""

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import httpx
from openai import AsyncOpenAI

logger = logging.getLogger("llm_config")


# ============================================================
# 配置模型
# ============================================================

@dataclass
class JudgeLLMConfig:
    """
    Judge 专用 LLM 配置。

    Attributes:
        provider: Provider 名称 (dashscope/glm/minimax/custom)
        base_url: API 基础地址
        api_key: API 密钥（支持 ${ENV_VAR} 格式）
        model: 模型名称
        temperature: 采样温度 (Judge 必须用 0.0)
        max_tokens: 最大输出 token 数
        timeout: HTTP 超时秒数
        verify_ssl: 是否验证 SSL 证书
    """
    provider: str = "dashscope"
    base_url: str = "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key: str = ""
    model: str = "qwen3-235b-a22b"
    temperature: float = 0.0
    max_tokens: int = 8192
    timeout: float = 120.0
    verify_ssl: bool = True

    def __post_init__(self):
        """解析环境变量引用"""
        self.api_key = _resolve_env_var(self.api_key)

    @property
    def is_configured(self) -> bool:
        """检查是否已配置有效的 API Key"""
        return bool(self.api_key and not self.api_key.startswith("${"))


# ============================================================
# 环境变量解析
# ============================================================

def _resolve_env_var(value: str) -> str:
    """解析 ${ENV_VAR} 格式的环境变量引用"""
    import re

    if not value:
        return value

    pattern = r"\$\{(\w+)\}"
    match = re.fullmatch(pattern, value.strip())

    if not match:
        return value

    env_var = match.group(1)
    resolved = os.environ.get(env_var, "")
    if resolved:
        logger.debug(f"Resolved {env_var} from environment")
        return resolved

    # 尝试从 .env 文件读取
    env_path = Path.cwd() / ".env"
    if not env_path.exists():
        env_path = Path(__file__).resolve().parents[2] / ".env"

    if env_path.exists():
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            if k.strip() == env_var:
                return v.strip().strip("'\"")

    logger.warning(f"Environment variable {env_var} not set")
    return value


# ============================================================
# 配置加载
# ============================================================

def is_llm_configured() -> bool:
    """检查是否配置了有效的 LLM（环境变量或 config.yaml）"""
    import os

    # 检查环境变量
    if os.environ.get("QWEN3_API_KEY"):
        return True
    if os.environ.get("GLM_API_KEY"):
        return True

    # 检查 config.yaml
    try:
        from src.core.config import load_config
        from pathlib import Path
        cfg = load_config(str(Path.cwd() / "config" / "config.yaml"))
        provider = cfg.providers.get(cfg.models.judge.provider)
        if provider and provider.api_key and not provider.api_key.startswith("${"):
            return True
    except Exception:
        pass

    return False


def load_judge_llm_config() -> JudgeLLMConfig:
    """
    加载 Judge LLM 配置。

    优先级：
    1. QWEN3_* 环境变量（最高优先级）
    2. GLM_* 环境变量（次优先级）
    3. 默认值（DashScope qwen3-235b-a22b）

    环境变量：
        QWEN3_BASE_URL: API 地址
        QWEN3_API_KEY: API 密钥
        QWEN3_MODEL: 模型名称
        QWEN3_TEMPERATURE: 温度 (默认 0.0)
        QWEN3_MAX_TOKENS: 最大 token 数 (默认 8192)

        GLM_BASE_URL: API 地址
        GLM_API_KEY: API 密钥
        GLM_MODEL: 模型名称 (默认 glm-5)
    """
    # 优先使用 QWEN3 环境变量
    if os.environ.get("QWEN3_API_KEY"):
        logger.info("使用 Qwen3 环境变量配置")
        return JudgeLLMConfig(
            provider="dashscope",
            base_url=os.environ.get("QWEN3_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
            api_key=os.environ.get("QWEN3_API_KEY", ""),
            model=os.environ.get("QWEN3_MODEL", "qwen3-235b-a22b"),
            temperature=float(os.environ.get("QWEN3_TEMPERATURE", "0.0")),
            max_tokens=int(os.environ.get("QWEN3_MAX_TOKENS", "8192")),
        )

    # 次选 GLM 环境变量
    if os.environ.get("GLM_API_KEY"):
        logger.info("使用 GLM 环境变量配置")
        return JudgeLLMConfig(
            provider="glm",
            base_url=os.environ.get("GLM_BASE_URL", "https://open.bigmodel.cn/api/paas/v4"),
            api_key=os.environ.get("GLM_API_KEY", ""),
            model=os.environ.get("GLM_MODEL", "glm-5"),
            temperature=float(os.environ.get("GLM_TEMPERATURE", "0.0")),
            max_tokens=int(os.environ.get("GLM_MAX_TOKENS", "8192")),
        )

    # 默认配置（需要 config.yaml 提供）
    logger.info("使用默认配置，请确保 config.yaml 中已配置 Judge")
    return JudgeLLMConfig()


def get_judge_client(config: Optional[JudgeLLMConfig] = None) -> AsyncOpenAI:
    """
    获取 Judge LLM 客户端。

    Args:
        config: JudgeLLMConfig，若为 None 则从环境变量加载

    Returns:
        AsyncOpenAI 客户端实例
    """
    if config is None:
        config = load_judge_llm_config()

    if not config.is_configured:
        raise ValueError(
            "Judge LLM 未配置有效 API Key。"
            "请设置 QWEN3_API_KEY 或 GLM_API_KEY 环境变量，"
            "或在 config.yaml 中配置 provider 和 model。"
        )

    http_client = httpx.AsyncClient(
        timeout=config.timeout,
        verify=config.verify_ssl,
        trust_env=False,
    )

    client = AsyncOpenAI(
        api_key=config.api_key,
        base_url=config.base_url,
        http_client=http_client,
    )

    logger.info(
        f"Judge 客户端初始化: provider={config.provider}, "
        f"model={config.model}, base_url={config.base_url}"
    )

    return client


def get_judge_config_from_yaml(config_path: str = "config/config.yaml") -> Optional[JudgeLLMConfig]:
    """
    从 config.yaml 加载 Judge 配置。

    Args:
        config_path: 配置文件路径

    Returns:
        JudgeLLMConfig 或 None（若未配置）
    """
    import yaml
    from src.core.config import load_config as load_app_config

    try:
        app_config = load_app_config(config_path)
        judge_model = app_config.models.judge
        provider = app_config.providers.get(judge_model.provider)

        if provider is None:
            logger.warning(f"Provider {judge_model.provider} not found in config")
            return None

        if not provider.api_key or provider.api_key.startswith("${"):
            logger.warning(f"Provider {judge_model.provider} API key not configured")
            return None

        model_spec = provider.models.get(judge_model.model)
        if model_spec is None:
            logger.warning(f"Model {judge_model.model} not found in provider")
            return None

        return JudgeLLMConfig(
            provider=judge_model.provider,
            base_url=provider.base_url,
            api_key=provider.api_key,
            model=judge_model.model,
            temperature=model_spec.temperature,
            max_tokens=model_spec.max_tokens,
            timeout=provider.timeout,
            verify_ssl=provider.verify_ssl,
        )

    except Exception as e:
        logger.warning(f"加载 config.yaml 失败: {e}")
        return None
