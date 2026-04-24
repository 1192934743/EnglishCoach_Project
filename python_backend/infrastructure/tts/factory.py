"""
TTS Factory - Factory Pattern implementation for TTS provider instantiation.

This module provides a factory class that creates TTS provider instances based on
configuration, enabling dynamic TTS engine switching at runtime.
"""
import logging
from typing import Optional

from .base import BaseTTSProvider

logger = logging.getLogger("EnglishCoach")

_tts_factory_instance: Optional["TTSFactory"] = None


class TTSFactory:
    """
    Factory class for creating and managing TTS provider instances.

    This factory maintains a registry of available TTS providers and creates
    instances based on engine names. It supports:
    - Default engine selection
    - Per-user engine selection
    - Singleton provider instances (reused across requests)
    """

    def __init__(self, config: dict):
        """
        Initialize the TTS factory.

        Args:
            config: Application configuration dictionary
        """
        self._config = config
        self._providers: dict[str, BaseTTSProvider] = {}
        self._provider_classes: dict[str, type] = {}

    def register(self, name: str, provider_class: type) -> None:
        """
        Register a TTS provider class.

        Args:
            name: Unique identifier for the provider (e.g., "azure", "volcengine")
            provider_class: The provider class (must inherit from BaseTTSProvider)
        """
        if not issubclass(provider_class, BaseTTSProvider):
            raise TypeError(f"{provider_class.__name__} must inherit from BaseTTSProvider")
        self._provider_classes[name] = provider_class
        logger.info(f"[TTSFactory] Registered provider: {name}")

    def _ensure_provider(self, name: str) -> Optional[BaseTTSProvider]:
        """
        Lazily create and cache a provider instance.

        Args:
            name: Provider name

        Returns:
            Provider instance or None if registration failed
        """
        if name not in self._provider_classes:
            logger.warning(f"[TTSFactory] Unknown TTS provider: {name}")
            return None

        if name not in self._providers:
            provider_class = self._provider_classes[name]
            self._providers[name] = provider_class()
            logger.info(f"[TTSFactory] Created instance for provider: {name}")

        return self._providers[name]

    def get_provider(self, engine_name: Optional[str] = None) -> Optional[BaseTTSProvider]:
        """
        Get a TTS provider instance by name.

        Args:
            engine_name: Name of the TTS engine. If None, uses the default engine
                        from config["DEFAULT_TTS_ENGINE"].

        Returns:
            TTS provider instance or None if the engine is not registered.
        """
        if engine_name is None:
            engine_name = self._config.get("DEFAULT_TTS_ENGINE", "azure")
            logger.debug(f"[TTSFactory] No engine specified, using default: {engine_name}")

        return self._ensure_provider(engine_name)

    async def warm_up_all(self) -> None:
        """
        Warm up all registered TTS providers.

        This initializes connections and validates credentials for each provider.
        Should be called once at application startup.
        """
        logger.info("[TTSFactory] Warming up all TTS providers...")
        for name, provider_class in self._provider_classes.items():
            try:
                provider = self._ensure_provider(name)
                if provider is not None:
                    await provider.warm_up()
                    logger.info(f"[TTSFactory] ✓ {name} warmed up successfully")
            except Exception as e:
                logger.warning(f"[TTSFactory] ⚠ {name} warm-up failed: {e}")

    async def close_all(self) -> None:
        """
        Gracefully close all TTS provider resources.

        Should be called during application shutdown.
        """
        logger.info("[TTSFactory] Closing all TTS providers...")
        for name, provider in self._providers.items():
            try:
                await provider.close()
                logger.info(f"[TTSFactory] ✓ {name} closed successfully")
            except Exception as e:
                logger.warning(f"[TTSFactory] ⚠ {name} close failed: {e}")
        self._providers.clear()


def init_tts_factory(config: dict) -> TTSFactory:
    """
    Initialize the global TTS factory instance.

    This function should be called once at application startup.

    Args:
        config: Application configuration dictionary

    Returns:
        The initialized TTSFactory instance
    """
    global _tts_factory_instance
    _tts_factory_instance = TTSFactory(config)

    # Import and register all available providers
    try:
        from .azure_tts import AzureTTSProvider
        _tts_factory_instance.register("azure", AzureTTSProvider)
    except ImportError as e:
        logger.warning(f"[TTSFactory] Could not register Azure provider: {e}")

    try:
        from .volcengine_tts import VolcengineTTSProvider
        _tts_factory_instance.register("volcengine", VolcengineTTSProvider)
    except ImportError as e:
        logger.warning(f"[TTSFactory] Could not register Volcengine provider: {e}")

    logger.info("[TTSFactory] Factory initialized successfully")
    return _tts_factory_instance


def get_tts_factory() -> Optional[TTSFactory]:
    """
    Get the global TTS factory instance.

    Returns:
        The TTSFactory instance or None if not initialized.
    """
    return _tts_factory_instance
