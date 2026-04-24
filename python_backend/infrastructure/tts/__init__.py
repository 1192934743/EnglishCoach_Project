"""
TTS Infrastructure - Plugin-based TTS engine architecture.

This package provides a plugin-based architecture for text-to-speech (TTS) engines,
following the Strategy and Factory design patterns.

Available providers:
- AzureTTSProvider: Microsoft Azure Neural TTS
- VolcengineTTSProvider: ByteDance Volcengine TTS with connection pooling

Usage:
    from infrastructure.tts import init_tts_factory, get_tts_factory

    # Initialize at startup
    factory = init_tts_factory(config)

    # Warm up all providers
    await factory.warm_up_all()

    # Get a specific provider
    provider = get_tts_factory().get_provider("azure")

    # Use the provider
    await provider.synthesize_single(text, websocket, ws_lock, config)

    # Shutdown
    await factory.close_all()
"""

from .base import BaseTTSProvider
from .factory import TTSFactory, init_tts_factory, get_tts_factory

__all__ = [
    "BaseTTSProvider",
    "TTSFactory",
    "init_tts_factory",
    "get_tts_factory",
]
