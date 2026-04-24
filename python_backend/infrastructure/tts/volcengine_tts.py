"""
Volcengine TTS Provider - ByteDance Volcengine TTS implementation.

This module provides the VolcengineTTSProvider class that implements the BaseTTSProvider
interface, using Volcengine's TTS service for speech synthesis with connection pooling
for low-latency streaming.
"""
import asyncio
import json
import logging
import struct
import time
import uuid
from typing import Optional, Any

import websockets
from fastapi import WebSocket

from .base import BaseTTSProvider

logger = logging.getLogger("EnglishCoach")

# Protocol constants
MAX_ID_LEN = 1024
MAX_TTS_PAYLOAD_LEN = 5 * 1024 * 1024  # Audio single frame payload limit
MAX_TTS_CONTROL_PAYLOAD_LEN = 512 * 1024  # Non-audio frame payload limit


def pack_tts_request(event_type: int, session_id: str = "", payload_dict: dict = None):
    """
    Volcengine bidirectional TTS binary protocol packing.

    Event numbers:
    - Event 1: Connection request (Client -> Server)
    - Event 2: Client sends completion, request session end (Client -> Server)
    - Event 50: Server ready for config (Server -> Client)
    - Event 100: Send business parameters like voice, sample rate (Client -> Server)
    - Event 150: Auth/config passed, session officially started (Server -> Client)
    - Event 200: Send text to synthesize (Client -> Server)
    - Event 102: Mark current text stream sending complete (Client -> Server)
    - Event 352: Send synthesized audio stream (Server -> Client)
    - Event 152: Server indicates current text synthesis complete (Server -> Client)
    - Event 52: Server confirms session closed (Server -> Client)
    """
    if payload_dict is None:
        payload_dict = {}
    payload_bytes = json.dumps(payload_dict).encode('utf-8')
    header = bytearray(4)
    header[0], header[1], header[2], header[3] = 0x11, 0x14, 0x10, 0x00

    event_bytes = struct.pack('>i', event_type)
    payload_len_bytes = struct.pack('>I', len(payload_bytes))

    if event_type in [1, 2]:
        return bytes(header) + event_bytes + payload_len_bytes + payload_bytes
    else:
        id_bytes = session_id.encode('utf-8')
        id_len_bytes = struct.pack('>I', len(id_bytes))
        return bytes(header) + event_bytes + id_len_bytes + id_bytes + payload_len_bytes + payload_bytes


def _sanitize_tts_text(text: str) -> str:
    """Remove control characters that cannot be used for TTS synthesis."""
    if not text:
        return ""
    return text.strip()


class _WarmTTSConnection:
    """
    Single Volcengine TTS WebSocket connection wrapper.

    Manages a single TTS connection with proper handshake and state management.
    """

    def __init__(self, config: dict):
        self._config = config
        self._ws = None
        self._session_id = None
        self._ready = False

    async def connect(self) -> bool:
        """Establish WebSocket connection and perform handshake."""
        volc_app_id = self._config.get("VOLC_APP_ID", "")
        volc_api_key = self._config.get("VOLC_API_KEY", "")
        volc_cluster = self._config.get("VOLC_TTS_CLUSTER", "volcengine_tts")

        if not volc_api_key:
            logger.error("[VolcengineTTS] Missing VOLC_API_KEY")
            return False

        tts_url = f"wss://openspeech.bytedance.com/api/v1/tts"
        headers = {
            "Authorization": f"Bearer {volc_api_key}",
            "x-api-key": volc_api_key,
            "X-Api-App-Id": volc_app_id,
        }

        try:
            self._ws = await websockets.connect(tts_url, additional_headers=headers)
            await self._ws.send(pack_tts_request(1))

            async for msg in self._ws:
                if isinstance(msg, bytes) and len(msg) >= 4:
                    msg_type = (msg[1] >> 4) & 0x0F
                    if msg_type == 50:
                        self._session_id = str(uuid.uuid4())
                        config_payload = {
                            "app": {"appid": volc_app_id, "token": volc_api_key, "cluster": volc_cluster},
                            "audio": {"codec": "pcm", "sample_rate": 24000, "rate": 24000},
                            "request": {"reqid": self._session_id, "operation": "submit", "text": ""},
                        }
                        await self._ws.send(pack_tts_request(100, self._session_id, config_payload))
                    elif msg_type == 150:
                        self._ready = True
                        logger.info("[VolcengineTTS] Connection ready")
                        return True

        except Exception as e:
            logger.warning(f"[VolcengineTTS] Connection failed: {e}")
            self._ws = None
            return False

        return False

    @property
    def ready(self) -> bool:
        return self._ready and self._ws is not None

    async def synthesize(
        self,
        text: str,
        websocket: WebSocket,
        ws_lock: asyncio.Lock,
        latency_hooks: Optional[dict] = None,
    ) -> bool:
        """
        Synthesize text and stream audio to websocket.

        Returns True if synthesis completed successfully, False otherwise.
        """
        if not self.ready:
            if not await self.connect():
                return False

        try:
            text_payload = {"reqid": self._session_id, "operation": "submit", "text": text}
            await self._ws.send(pack_tts_request(200, self._session_id, text_payload))
            await self._ws.send(pack_tts_request(102, self._session_id))

            first_sent = False
            async for msg in self._ws:
                if isinstance(msg, bytes) and len(msg) >= 4:
                    msg_type = (msg[1] >> 4) & 0x0F

                    if msg_type == 152:
                        logger.info("[VolcengineTTS] Synthesis complete")
                        return True

                    if msg_type == 52:
                        self._ready = False
                        return True

                    if msg_type == 35:
                        header_size = (msg[0] & 0x0F) * 4
                        if header_size + 8 > len(msg):
                            continue

                        payload_len = struct.unpack('>I', msg[header_size + 4:header_size + 8])[0]
                        if payload_len > MAX_TTS_PAYLOAD_LEN or header_size + 8 + payload_len > len(msg):
                            logger.warning(f"[VolcengineTTS] Payload too large: {payload_len}")
                            continue

                        audio_data = msg[header_size + 8:header_size + 8 + payload_len]

                        if not first_sent and latency_hooks is not None:
                            first_sent = True
                            latency_hooks["_tts_first_pcm_logged"] = True
                            t0 = latency_hooks.get("t0")
                            tid = latency_hooks.get("turn_id", "?")
                            if isinstance(t0, (int, float)):
                                ms = (time.perf_counter() - float(t0)) * 1000.0
                                logger.info(
                                    "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                                    tid, "07_first_pcm_to_client", ms, len(audio_data),
                                )

                        async with ws_lock:
                            await websocket.send_bytes(audio_data)

            return True

        except Exception as e:
            logger.warning(f"[VolcengineTTS] Synthesis error: {e}")
            self._ready = False
            return False

    async def close(self) -> None:
        """Close the WebSocket connection."""
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None
            self._ready = False


class TTSPool:
    """
    Volcengine TTS connection pool for low-latency streaming.

    Maintains a pool of pre-warmed connections to eliminate connection overhead
    during synthesis requests.
    """

    def __init__(self, config: dict, pool_size: int = 3):
        self._config = config
        self._pool_size = pool_size
        self._connections: asyncio.Queue = asyncio.Queue()
        self._lock = asyncio.Lock()

    async def warm_up(self) -> None:
        """Initialize all connections in the pool."""
        logger.info(f"[VolcengineTTS] Warming up {self._pool_size} connections...")
        for i in range(self._pool_size):
            conn = _WarmTTSConnection(self._config)
            if await conn.connect():
                await self._connections.put(conn)
                logger.info(f"[VolcengineTTS] Connection {i + 1} ready")
            else:
                logger.warning(f"[VolcengineTTS] Connection {i + 1} failed, will retry on demand")

    async def get_connection(self) -> Optional[_WarmTTSConnection]:
        """Get a connection from the pool."""
        if self._connections.empty():
            conn = _WarmTTSConnection(self._config)
            if await conn.connect():
                return conn
            return None

        try:
            conn = self._connections.get_nowait()
            if not conn.ready:
                if await conn.connect():
                    return conn
                return None
            return conn
        except asyncio.QueueEmpty:
            conn = _WarmTTSConnection(self._config)
            if await conn.connect():
                return conn
            return None

    async def return_connection(self, conn: _WarmTTSConnection) -> None:
        """Return a connection to the pool."""
        if conn and conn.ready:
            try:
                await self._connections.put(conn)
            except Exception:
                pass

    async def close_all(self) -> None:
        """Close all connections in the pool."""
        while not self._connections.empty():
            try:
                conn = self._connections.get_nowait()
                await conn.close()
            except asyncio.QueueEmpty:
                break


class VolcengineTTSProvider(BaseTTSProvider):
    """
    Volcengine TTS Provider.

    Implements streaming TTS synthesis using ByteDance Volcengine TTS service.
    Uses TTSPool for connection pooling to achieve low-latency synthesis.

    Design principles:
    - TTSPool maintains pre-warmed connections for zero-connection-delay synthesis
    - synthesize_single: point-to-point single sentence synthesis
    - synthesize_queue: queue consumer for streaming LLM output
    """

    def __init__(self):
        self._pool: Optional[TTSPool] = None

    async def warm_up(self) -> None:
        """
        Warm up the Volcengine TTS provider.

        Initializes the TTSPool with pre-warmed connections.
        """
        from core.config import CONFIG
        self._pool = TTSPool(CONFIG, pool_size=3)
        await self._pool.warm_up()
        logger.info("[VolcengineTTS] Provider warmed up successfully")

    async def close(self) -> None:
        """
        Close the Volcengine TTS provider.

        Closes all connections in the pool.
        """
        if self._pool:
            await self._pool.close_all()
            self._pool = None
            logger.info("[VolcengineTTS] Provider closed")

    async def synthesize_single(
        self,
        text: str,
        websocket: WebSocket,
        ws_lock: asyncio.Lock,
        config: dict,
        latency_hooks: Optional[dict] = None,
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
            config: Configuration dictionary (uses global CONFIG)
            latency_hooks: Optional latency tracking dict (t0, turn_id, _tts_first_pcm_logged)
        """
        from core.config import CONFIG
        effective_config = config if config else CONFIG

        conn = None
        try:
            if self._pool:
                conn = await self._pool.get_connection()

            if not conn:
                conn = _WarmTTSConnection(effective_config)
                if not await conn.connect():
                    logger.error("[VolcengineTTS] Failed to establish connection for single synthesis")
                    return

            clean_text = _sanitize_tts_text(text)
            if not clean_text:
                return

            await conn.synthesize(clean_text, websocket, ws_lock, latency_hooks)

        except Exception as e:
            logger.warning(f"[VolcengineTTS] Single synthesis error: {e}")
        finally:
            if conn and self._pool:
                await self._pool.return_connection(conn)

    async def synthesize_queue(
        self,
        websocket: WebSocket,
        ws_lock: asyncio.Lock,
        config: dict,
        segment_queue: asyncio.Queue,
        latency_hooks: Optional[dict] = None,
    ) -> None:
        """
        Consume text segments from queue and synthesize sequentially.

        This is the queue consumer entry point used for streaming LLM output.
        Maintains a connection across all segments for low-latency synthesis.
        The tts_finished signal is sent by the caller in their finally block.

        Args:
            websocket: FastAPI WebSocket connection
            ws_lock: asyncio.Lock for thread-safe operations
            config: Configuration dictionary (uses global CONFIG)
            segment_queue: asyncio.Queue containing text segments (None = end signal)
            latency_hooks: Optional latency tracking dict
        """
        from core.config import CONFIG
        effective_config = config if config else CONFIG

        conn = None
        try:
            if self._pool:
                conn = await self._pool.get_connection()

            if not conn:
                conn = _WarmTTSConnection(effective_config)
                if not await conn.connect():
                    logger.error("[VolcengineTTS] Failed to establish connection for queue synthesis")
                    return

            while True:
                text_chunk = await segment_queue.get()
                if text_chunk is None:
                    segment_queue.task_done()
                    break

                clean_text = _sanitize_tts_text(text_chunk)
                if not clean_text:
                    segment_queue.task_done()
                    continue

                logger.info(f"[VolcengineTTS] Synthesizing: {clean_text[:40]}")
                await conn.synthesize(clean_text, websocket, ws_lock, latency_hooks)
                segment_queue.task_done()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"[VolcengineTTS] Queue synthesis error: {e}")
        finally:
            if conn and self._pool:
                await self._pool.return_connection(conn)
