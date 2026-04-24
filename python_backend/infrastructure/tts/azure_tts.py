"""
Azure Neural TTS Provider - Microsoft Azure TTS implementation.

This module provides the AzureTTSProvider class that implements the BaseTTSProvider
interface, using Azure Cognitive Services Neural TTS for speech synthesis.
"""
import asyncio
import re
import threading
import time
from typing import Optional, Any

import azure.cognitiveservices.speech as speechsdk
from fastapi import WebSocket

from .base import BaseTTSProvider


def _sanitize_tts_text(text: str) -> str:
    """Remove control characters that cannot be used for TTS synthesis."""
    if not text:
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()


class AzureTTSProvider(BaseTTSProvider):
    """
    Azure Neural TTS Provider.

    Implements streaming TTS synthesis using Azure Cognitive Services.
    Uses the synthesizing event + asyncio.Queue for thread-safe streaming.

    Design principles:
    - Uses synthesizing event + asyncio.Queue for true thread-safe streaming
    - No external connection pool (Azure SDK handles WebSocket reuse internally)
    - synthesize_single: point-to-point single sentence synthesis
    - synthesize_queue: queue consumer for streaming LLM output
    """

    async def warm_up(self) -> None:
        """
        Warm up the Azure TTS engine.

        Azure Neural TTS doesn't require explicit warm-up as the SDK handles
        connection pooling internally. This method validates the configuration.
        """
        import logging
        logger = logging.getLogger("EnglishCoach")
        logger.info("[AzureTTS] Ready (SDK handles connection pooling internally)")

    async def close(self) -> None:
        """
        Close the Azure TTS provider.

        Azure SDK manages connections internally, no cleanup needed.
        """
        pass

    async def synthesize_single(
        self,
        text: str,
        websocket: WebSocket,
        ws_lock: asyncio.Lock,
        config: dict,
        latency_hooks: Optional[dict] = None,
        voice_id: Optional[str] = None,
    ) -> None:
        """
        Synthesize a single text segment and stream audio to websocket.

        This is the point-to-point synthesis entry point, used for click-to-speak
        functionality. The tts_finished signal is sent by the caller in their
        finally block.

        Args:
            text: The text to synthesize
            websocket: FastAPI WebSocket connection
            ws_lock: asyncio.Lock for thread-safe operations
            config: Configuration dictionary with AZURE_SPEECH_KEY, AZURE_SPEECH_REGION, AZURE_VOICE
            latency_hooks: Optional latency tracking dict (t0, turn_id, _tts_first_pcm_logged)
            voice_id: Optional voice identifier to override the default voice in config
        """
        import logging
        logger = logging.getLogger("EnglishCoach")

        azure_key = config.get("AZURE_SPEECH_KEY", "").strip()
        azure_region = config.get("AZURE_SPEECH_REGION", "").strip()
        voice_name = voice_id if voice_id else config.get("AZURE_VOICE", "en-GB-RyanNeural").strip()

        if not azure_key:
            logger.error("[AzureTTS] Missing AZURE_SPEECH_KEY, please check config.env")
            return

        try:
            speech_config = speechsdk.SpeechConfig(
                subscription=azure_key, region=azure_region
            )
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm
            )
            speech_config.speech_synthesis_voice_name = voice_name

            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=speech_config, audio_config=None
            )

            audio_queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            synthesis_done = threading.Event()

            def evt_synthesizing(evt):
                audio_data = evt.result.audio_data
                if audio_data:
                    loop.call_soon_threadsafe(audio_queue.put_nowait, audio_data)

            def evt_completed(evt):
                synthesis_done.set()
                loop.call_soon_threadsafe(audio_queue.put_nowait, None)

            def evt_canceled(evt):
                synthesis_done.set()
                loop.call_soon_threadsafe(audio_queue.put_nowait, None)

            synthesizer.synthesizing.connect(evt_synthesizing)
            synthesizer.synthesis_completed.connect(evt_completed)
            synthesizer.synthesis_canceled.connect(evt_canceled)

            clean_text = _sanitize_tts_text(text)
            if not clean_text:
                return

            synthesis_done.clear()
            synthesizer.start_speaking_text_async(clean_text)

            first_yielded = False
            while True:
                try:
                    chunk = await asyncio.wait_for(audio_queue.get(), timeout=1.0)
                    if chunk is None:
                        break

                    if not first_yielded and latency_hooks is not None:
                        first_yielded = True
                        latency_hooks["_tts_first_pcm_logged"] = True
                        t0 = latency_hooks.get("t0")
                        tid = latency_hooks.get("turn_id", "?")
                        if isinstance(t0, (int, float)):
                            ms = (time.perf_counter() - float(t0)) * 1000.0
                            logger.info(
                                "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                                tid, "07_first_pcm_to_client", ms, len(chunk),
                            )

                    async with ws_lock:
                        await websocket.send_bytes(chunk)

                except asyncio.TimeoutError:
                    if synthesis_done.is_set() and audio_queue.empty():
                        break

        except Exception as e:
            logger.warning("[AzureTTS] Single synthesis interrupted (frontend may have disconnected): %s", e)

    async def synthesize_queue(
        self,
        websocket: WebSocket,
        ws_lock: asyncio.Lock,
        config: dict,
        segment_queue: asyncio.Queue,
        latency_hooks: Optional[dict] = None,
        voice_id: Optional[str] = None,
    ) -> None:
        """
        Consume text segments from queue and synthesize sequentially.

        This is the queue consumer entry point used for streaming LLM output.
        The engine is initialized outside the loop - the same SpeechSynthesizer
        instance is reused across all segments for low-latency synthesis.
        The tts_finished signal is sent by the caller in their finally block.

        Args:
            websocket: FastAPI WebSocket connection
            ws_lock: asyncio.Lock for thread-safe operations
            config: Configuration dictionary with AZURE_SPEECH_KEY, AZURE_SPEECH_REGION, AZURE_VOICE
            segment_queue: asyncio.Queue containing text segments (None = end signal)
            latency_hooks: Optional latency tracking dict
            voice_id: Optional voice identifier to override the default voice in config
        """
        import logging
        logger = logging.getLogger("EnglishCoach")

        azure_key = config.get("AZURE_SPEECH_KEY", "").strip()
        azure_region = config.get("AZURE_SPEECH_REGION", "").strip()
        voice_name = voice_id if voice_id else config.get("AZURE_VOICE", "en-GB-RyanNeural").strip()

        if not azure_key:
            logger.error("[AzureTTS] Missing AZURE_SPEECH_KEY, please check config.env")
            return

        try:
            speech_config = speechsdk.SpeechConfig(
                subscription=azure_key, region=azure_region
            )
            speech_config.set_speech_synthesis_output_format(
                speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm
            )
            speech_config.speech_synthesis_voice_name = voice_name

            synthesizer = speechsdk.SpeechSynthesizer(
                speech_config=speech_config, audio_config=None
            )

            audio_queue: asyncio.Queue = asyncio.Queue()
            loop = asyncio.get_running_loop()
            synthesis_done = threading.Event()

            def evt_synthesizing(evt):
                audio_data = evt.result.audio_data
                if audio_data:
                    loop.call_soon_threadsafe(audio_queue.put_nowait, audio_data)

            def evt_completed(evt):
                synthesis_done.set()
                loop.call_soon_threadsafe(audio_queue.put_nowait, None)

            def evt_canceled(evt):
                synthesis_done.set()
                loop.call_soon_threadsafe(audio_queue.put_nowait, None)

            synthesizer.synthesizing.connect(evt_synthesizing)
            synthesizer.synthesis_completed.connect(evt_completed)
            synthesizer.synthesis_canceled.connect(evt_canceled)

            while True:
                text_chunk = await segment_queue.get()
                if text_chunk is None:
                    segment_queue.task_done()
                    break

                clean_text = _sanitize_tts_text(text_chunk)
                if not clean_text:
                    segment_queue.task_done()
                    continue

                logger.info(f"[TRACK_TTS] Starting Azure synthesis for text={clean_text[:40]}")
                synthesis_done.clear()
                synthesizer.start_speaking_text_async(clean_text)

                last_chunk_time = time.perf_counter()
                chunk_seq = 0
                first_yielded = False
                while True:
                    try:
                        chunk = await asyncio.wait_for(audio_queue.get(), timeout=1.0)
                        if chunk is None:
                            logger.info(f"[TRACK_TTS] Sentence synthesis complete, total chunks={chunk_seq}")
                            break

                        now = time.perf_counter()
                        interval_ms = (now - last_chunk_time) * 1000.0
                        last_chunk_time = now
                        chunk_seq += 1
                        if interval_ms > 200:
                            logger.warning(f"[TRACK_TTS] Audio stream stall detected! interval={interval_ms:.1f}ms chunk_seq={chunk_seq} len={len(chunk)}")

                        if not first_yielded and latency_hooks is not None:
                            first_yielded = True
                            latency_hooks["_tts_first_pcm_logged"] = True
                            t0 = latency_hooks.get("t0")
                            tid = latency_hooks.get("turn_id", "?")
                            if isinstance(t0, (int, float)):
                                ms = (time.perf_counter() - float(t0)) * 1000.0
                                logger.info(
                                    "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                                    tid, "07_first_pcm_to_client", ms, len(chunk),
                                )

                        async with ws_lock:
                            await websocket.send_bytes(chunk)

                    except asyncio.TimeoutError:
                        if synthesis_done.is_set() and audio_queue.empty():
                            break

                segment_queue.task_done()

        except Exception as e:
            logger.warning("[AzureTTS] Queue synthesis error: %s", e)
