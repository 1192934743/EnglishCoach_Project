"""
Base TTS Provider - Abstract base class for all TTS engine implementations.

This module defines the interface that all TTS providers (Azure, Volcengine, etc.)
must implement, following the Strategy Pattern.
"""
from abc import ABC, abstractmethod
from typing import Optional, Any


class BaseTTSProvider(ABC):
    """
    Abstract base class for TTS providers.

    All TTS engine implementations must inherit from this class and implement
    the required abstract methods. This enables dynamic TTS engine switching
    based on user settings.
    """

    @abstractmethod
    async def warm_up(self) -> None:
        """
        Warm up the TTS engine (pre-initialize connections, validate credentials, etc.).
        Called once at startup for all enabled engines.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """
        Gracefully close all resources held by the TTS provider.
        Called during application shutdown.
        """
        pass

    @abstractmethod
    async def synthesize_single(
        self,
        text: str,
        websocket: Any,
        ws_lock: Any,
        config: dict,
        latency_hooks: Optional[dict] = None,
    ) -> None:
        """
        Synthesize a single text segment and stream audio to websocket.

        Args:
            text: The text to synthesize
            websocket: FastAPI WebSocket connection to send audio to
            ws_lock: asyncio.Lock for thread-safe websocket operations
            config: Configuration dictionary containing API keys and settings
            latency_hooks: Optional dict for latency tracking (e.g., t0, turn_id)
        """
        pass

    @abstractmethod
    async def synthesize_queue(
        self,
        websocket: Any,
        ws_lock: Any,
        config: dict,
        segment_queue: Any,
        latency_hooks: Optional[dict] = None,
    ) -> None:
        """
        Consume text segments from a queue and synthesize them sequentially.

        This is used for streaming TTS where text arrives incrementally from
        an LLM stream. The provider reuses a single TTS connection across
        all segments for low-latency synthesis.

        Args:
            websocket: FastAPI WebSocket connection to send audio to
            ws_lock: asyncio.Lock for thread-safe websocket operations
            config: Configuration dictionary containing API keys and settings
            segment_queue: asyncio.Queue containing text segments (None = end signal)
            latency_hooks: Optional dict for latency tracking
        """
        pass
