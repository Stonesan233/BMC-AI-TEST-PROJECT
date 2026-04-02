# -*- coding: utf-8 -*-
"""
统一 AsyncOpenAI 客户端工厂

职责:
  - 根据 AppConfig 中的 provider 名称创建 AsyncOpenAI 客户端
  - 统一管理 httpx.AsyncClient 生命周期
  - 支持 Exec / Judge / Embedding / Rewrite 等组件按需获取独立客户端
  - 中心化超时和连接配置

用法:
  from src.core.config import load_config
  from src.core.client_factory import ClientFactory

  cfg = load_config()
  factory = ClientFactory(cfg)

  # 为 Exec 组件创建客户端
  exec_client = factory.create_for("exec")

  # 为 Embedding 创建客户端（复用同一 provider 的连接配置）
  embed_client = factory.create_for("embedding")

  # 清理
  await factory.close()
"""

import logging
from typing import Dict, Optional

import httpx
from openai import AsyncOpenAI

from src.core.config import AppConfig, ComponentModel, ProviderConfig

logger = logging.getLogger("core.client_factory")


class ClientFactory:
    """
    AsyncOpenAI 客户端工厂.

    每个组件（exec / judge / embedding / rewrite）可以独立创建客户端，
    绑定各自的 provider（base_url + api_key）和 model 参数。
    """

    def __init__(self, config: AppConfig):
        self._config = config
        self._http_clients: Dict[str, httpx.AsyncClient] = {}
        self._openai_clients: Dict[str, AsyncOpenAI] = {}

    # ==================================================================
    # Public API
    # ==================================================================

    def create_for(self, component: str) -> AsyncOpenAI:
        """
        为指定组件创建或获取缓存的 AsyncOpenAI 客户端。

        Args:
            component: 组件名称 ("exec" / "judge" / "embedding" / "rewrite")

        Returns:
            AsyncOpenAI 实例（已配置好 base_url, api_key, http_client）

        Raises:
            ValueError: 组件名称无效
        """
        if component in self._openai_clients:
            return self._openai_clients[component]

        comp_model = self._get_component_model(component)
        provider = self._config.providers.get(comp_model.provider)
        if provider is None:
            raise ValueError(
                f"组件 '{component}' 引用的 provider '{comp_model.provider}' 不存在"
            )

        return self._create_client(component, comp_model, provider)

    def create(
        self,
        provider_name: str,
        timeout: Optional[float] = None,
    ) -> AsyncOpenAI:
        """
        直接按 provider 名称创建客户端（不绑定特定组件）。

        适用于 build_index 等独立工具脚本。

        Args:
            provider_name: providers 配置中的名称
            timeout: HTTP 超时（None 则使用 provider 中的默认值）
        """
        cache_key = f"_direct_{provider_name}"
        if cache_key in self._openai_clients:
            return self._openai_clients[cache_key]

        provider = self._config.providers.get(provider_name)
        if provider is None:
            raise ValueError(f"provider '{provider_name}' 不存在")

        http_client = httpx.AsyncClient(timeout=timeout or provider.timeout)
        self._http_clients[cache_key] = http_client

        client = AsyncOpenAI(
            api_key=provider.api_key,
            base_url=provider.base_url,
            http_client=http_client,
        )
        self._openai_clients[cache_key] = client

        logger.info(
            f"[ClientFactory] 创建客户端: provider={provider_name}, "
            f"base_url={provider.base_url}"
        )
        return client

    async def close(self):
        """释放所有已创建的客户端资源。"""
        # 先关 OpenAI 客户端
        for name, client in self._openai_clients.items():
            try:
                await client.close()
            except Exception as e:
                logger.warning(f"关闭 OpenAI 客户端 ({name}) 失败: {e}")

        # 再关 httpx 客户端
        for name, client in self._http_clients.items():
            try:
                if not client.is_closed:
                    await client.aclose()
            except Exception as e:
                logger.warning(f"关闭 httpx 客户端 ({name}) 失败: {e}")

        self._openai_clients.clear()
        self._http_clients.clear()
        logger.info("[ClientFactory] 所有客户端已释放")

    # ==================================================================
    # 内部方法
    # ==================================================================

    def _get_component_model(self, component: str) -> ComponentModel:
        """获取指定组件的 ComponentModel。"""
        models = self._config.models
        comp_map = {
            "exec": models.exec,
            "judge": models.judge,
            "embedding": models.embedding,
            "rewrite": models.rewrite,
        }
        if component not in comp_map:
            raise ValueError(
                f"无效组件 '{component}'，可选: {list(comp_map.keys())}"
            )
        return comp_map[component]

    def _create_client(
        self,
        component: str,
        comp_model: ComponentModel,
        provider: ProviderConfig,
    ) -> AsyncOpenAI:
        """创建并缓存 AsyncOpenAI 客户端。"""
        http_client = httpx.AsyncClient(timeout=provider.timeout)
        self._http_clients[component] = http_client

        client = AsyncOpenAI(
            api_key=provider.api_key,
            base_url=provider.base_url,
            http_client=http_client,
        )
        self._openai_clients[component] = client

        logger.info(
            f"[ClientFactory] 创建客户端: component={component}, "
            f"provider={comp_model.provider}, model={comp_model.model}, "
            f"base_url={provider.base_url}"
        )
        return client
