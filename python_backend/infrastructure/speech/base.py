"""
ASRClient / TTSClient — 语音服务抽象（Phase 1 预留）

当前实现：volcengine_client（即 core/audio_service.py）。
迁移时将 run_volcengine_wss_asr / run_tts_to_ws 包装进 VolcengineASRClient。
"""
from abc import ABC, abstractmethod
from typing import Tuple


class ASRClient(ABC):
    @abstractmethod
    async def transcribe(self, pcm_bytes: bytearray, config: dict) -> Tuple[str, str]:
        """返回 (text, emotion)"""


class TTSClient(ABC):
    @abstractmethod
    async def synthesize_to_ws(self, text: str, websocket, ws_lock, config: dict) -> None:
        """将合成的 PCM 流写入 WebSocket"""
