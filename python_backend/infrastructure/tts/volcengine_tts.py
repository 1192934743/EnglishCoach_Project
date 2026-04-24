"""
Volcengine TTS Provider - ByteDance Volcengine TTS implementation.

This module provides the VolcengineTTSProvider class that implements the BaseTTSProvider
interface, using Volcengine's TTS service from audio_service.py.
"""
import logging
from typing import Optional, Any

from fastapi import WebSocket

from .base import BaseTTSProvider

logger = logging.getLogger("EnglishCoach")


class VolcengineTTSProvider(BaseTTSProvider):
    """
    Volcengine TTS Provider.

    Implements streaming TTS synthesis using ByteDance Volcengine TTS service.
    Delegates to the working implementation in audio_service.py.
    """

    async def warm_up(self) -> None:
        """
        Warm up the Volcengine TTS engine.

        Initializes the TTS connection pool from audio_service.py.
        """
        from core.audio_service import warm_tts_pool
        try:
            await warm_tts_pool()
            logger.info("[VolcengineTTS] Provider warmed up successfully")
        except Exception as e:
            logger.warning(f"[VolcengineTTS] Warm-up warning: {e}")

    async def close(self) -> None:
        """
        Close the Volcengine TTS provider.
        """
        from core.audio_service import get_tts_pool
        pool = get_tts_pool()
        if pool:
            await pool.close()
            logger.info("[VolcengineTTS] Provider closed")

    async def synthesize_single(
        self,
        text: str,
        websocket: WebSocket,
        ws_lock: Any,
        config: dict,
        latency_hooks: Optional[dict] = None,
        voice_id: Optional[str] = None,
    ) -> None:
        """
        Synthesize a single text segment and stream audio to websocket.

        Uses the working implementation from audio_service.py.

        Args:
            text: The text to synthesize
            websocket: FastAPI WebSocket connection
            ws_lock: asyncio.Lock for thread-safe operations
            config: Configuration dictionary
            latency_hooks: Optional latency tracking dict (t0, turn_id, _tts_first_pcm_logged)
            voice_id: Optional voice identifier to override the default voice in config
        """
        from core.audio_service import run_tts_to_ws
        effective_config = dict(config)
        if voice_id:
            effective_config["VOLC_VOICE"] = voice_id
        try:
            await run_tts_to_ws(text, websocket, ws_lock, effective_config, latency_hooks)
        except Exception as e:
            logger.warning(f"[VolcengineTTS] Single synthesis error: {e}")

    async def synthesize_queue(
        self,
        websocket: WebSocket,
        ws_lock: Any,
        config: dict,
        segment_queue: Any,
        latency_hooks: Optional[dict] = None,
        voice_id: Optional[str] = None,
    ) -> None:
        """
        Consume text segments from queue and synthesize sequentially.

        Uses the working implementation from audio_service.py.

        Args:
            websocket: FastAPI WebSocket connection
            ws_lock: asyncio.Lock for thread-safe operations
            config: Configuration dictionary
            segment_queue: asyncio.Queue containing text segments (None = end signal)
            latency_hooks: Optional latency tracking dict
            voice_id: Optional voice identifier to override the default voice in config
        """
        from core.audio_service import run_tts_turn_reused_from_queue
        effective_config = dict(config)
        if voice_id:
            effective_config["VOLC_VOICE"] = voice_id
        logger.info(f"[VolcengineTTS] Using VOLC_VOICE={effective_config.get('VOLC_VOICE')}, resource_id={effective_config.get('VOLC_RESOURCE_ID_TTS')}")
        try:
            await run_tts_turn_reused_from_queue(
                websocket, ws_lock, effective_config, segment_queue, latency_hooks
            )
        except Exception as e:
            logger.warning(f"[VolcengineTTS] Queue synthesis error: {e}")
