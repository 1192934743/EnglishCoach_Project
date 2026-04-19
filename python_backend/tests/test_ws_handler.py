"""
测试 api/websocket/handler.py — WsSessionHandler 及重构后的模块

运行方式：pytest tests/test_ws_handler.py -v
"""
import pytest
from unittest.mock import MagicMock, AsyncMock
import asyncio
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))


class TestWsSessionHandler:
    """WsSessionHandler 单元测试"""

    def test_handler_init(self):
        """Handler 实例初始化后状态正确"""
        from api.websocket.handler import WsSessionHandler

        mock_ws = MagicMock()
        config = {"DEEPSEEK_KEY": "test", "VOLC_API_KEY": "test"}

        handler = WsSessionHandler(mock_ws, "user_123", config)

        assert handler._user_id == "user_123"
        assert handler._config == config
        assert handler._is_flipped is False
        assert handler._session_hits == set()
        assert handler._session_ctx["phase"] == "ICE_BREAKING"
        assert handler._current_task_packet is None
        assert handler._chat_history == []

    def test_latency_log_no_crash(self):
        """_latency_log 在各种边界条件下不抛异常"""
        from api.websocket.handler import _latency_log

        _latency_log({}, "test_stage")           # 无 t0
        _latency_log({"t0": None}, "test_stage")  # t0 为 None
        _latency_log({"t0": "invalid"}, "test_stage")  # t0 类型错误

    def test_coerce_turn_trace_id_valid(self):
        """有效 trace_id 直接返回（小写化）"""
        from api.websocket.handler import _coerce_turn_trace_id

        assert _coerce_turn_trace_id("abcd1234") == "abcd1234"
        assert _coerce_turn_trace_id("ABC12345") == "abc12345"

    def test_coerce_turn_trace_id_invalid(self):
        """无效 trace_id 生成 8 位短 ID"""
        from api.websocket.handler import _coerce_turn_trace_id

        # 空字符串
        result = _coerce_turn_trace_id("")
        assert len(result) == 8

        # None
        result = _coerce_turn_trace_id(None)
        assert len(result) == 8

        # 太短
        result = _coerce_turn_trace_id("ab")
        assert len(result) == 8

        # 含短横线（去除后恰好 4 字符，仍有效）→ 直接返回小写化结果
        assert _coerce_turn_trace_id("a-b-c-d") == "abcd"
        assert _coerce_turn_trace_id("ab-cd-ef") == "abcdef"

        # 完全无效：去除后长度不足 → 生成 8 位 ID
        result = _coerce_turn_trace_id("a-b")
        assert len(result) == 8

    def test_handler_constants(self):
        """业务常量定义正确"""
        from api.websocket.handler import (
            LLM_MAX_TOKENS, DEFAULT_TOPIC_ID,
            MAX_AUDIO_BYTES, MAX_BUFFER_CHARS,
            FIRST_TTS_EARLY_FLUSH_CHARS, MAX_CONTEXT_TOKENS,
        )

        assert LLM_MAX_TOKENS == 80
        assert DEFAULT_TOPIC_ID == 999
        assert MAX_AUDIO_BYTES == 5 * 1024 * 1024
        assert MAX_BUFFER_CHARS == 38
        assert FIRST_TTS_EARLY_FLUSH_CHARS == 22
        assert MAX_CONTEXT_TOKENS == 2000


class TestAudioProtocol:
    """core/audio/protocol.py 单元测试"""

    def test_generate_asr_header(self):
        """ASR 请求头 4 字节格式正确"""
        from core.audio.protocol import generate_asr_header

        # message_type=1, flags=0, serialization=1, compression=1
        header = generate_asr_header(1, 0, 1, 1)
        assert len(header) == 4
        assert header[0] == 0x11
        assert header[1] == 0x10  # 1 << 4 | 0
        assert header[2] == 0x11  # 1 << 4 | 1
        assert header[3] == 0x00

    def test_generate_asr_header_with_flags(self):
        """带结束标志的音频帧"""
        from core.audio.protocol import generate_asr_header

        header = generate_asr_header(2, 2, 0, 1)
        assert len(header) == 4
        assert header[1] == 0x22  # 2 << 4 | 2

    def test_pack_tts_request_connection(self):
        """TTS Event 1/2 不含 session_id"""
        from core.audio.protocol import pack_tts_request

        data = pack_tts_request(1, session_id="", payload_dict={"test": "value"})
        assert len(data) > 8

    def test_pack_tts_request_with_session(self):
        """TTS Event 100+ 含 session_id"""
        from core.audio.protocol import pack_tts_request

        data = pack_tts_request(
            100, session_id="abc123",
            payload_dict={"speaker": "BV001"}
        )
        assert len(data) > 8

    def test_parse_tts_server_frame_short(self):
        """过短消息返回 None"""
        from core.audio.protocol import parse_tts_server_frame

        assert parse_tts_server_frame(b"") is None
        assert parse_tts_server_frame(b"x") is None

    def test_parse_tts_server_frame_error_frame(self):
        """错误帧（msg_type=15）返回 None"""
        from core.audio.protocol import parse_tts_server_frame

        # msg_type=15: header[1] = 0xF0, body 至少 4 字节
        raw = bytes([0x10, 0xF0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00])
        result = parse_tts_server_frame(raw)
        assert result is None  # 错误由 logger 记录

    def test_parse_asr_server_frame_short(self):
        """ASR 短消息返回 None"""
        from core.audio.protocol import parse_asr_server_frame

        assert parse_asr_server_frame(b"") is None
        # 有效头但无 payload
        assert parse_asr_server_frame(bytes([0x11, 0x10, 0x10, 0x00])) is None


class TestBackwardCompatAudioService:
    """向后兼容导入：core.audio_service 导出与原接口一致"""

    def test_backward_compat(self):
        """旧路径导入仍然有效"""
        from core.audio_service import (
            MIN_AUDIO_BYTES,
            ASR_STREAM_START_BYTES,
            run_volcengine_wss_asr,
            run_volc_streaming_asr_worker,
            TTSPool,
            init_tts_pool,
            run_tts_to_ws,
            run_tts_turn_reused_from_queue,
        )

        assert MIN_AUDIO_BYTES == 8000
        assert ASR_STREAM_START_BYTES == 4800
        assert TTSPool is not None
        assert callable(run_tts_to_ws)
        assert callable(run_tts_turn_reused_from_queue)

    def test_tts_pool_size_exported(self):
        """_TTS_POOL_SIZE 从 core.audio_service 导出（server.py startup 使用）"""
        from core.audio_service import _TTS_POOL_SIZE

        assert isinstance(_TTS_POOL_SIZE, int)
        assert _TTS_POOL_SIZE >= 1


class TestApiHttp:
    """api/http 路由测试"""

    def test_router_has_endpoints(self):
        """HTTP 路由包含 topics 和 stats 端点"""
        from api.http import router

        paths = [r.path for r in router.routes]
        assert "/topics" in paths
        assert "/stats" in paths


class TestLLMClient:
    """DeepSeek LLM Client 测试"""

    def test_client_init(self):
        """LLM Client 初始化"""
        from infrastructure.llm.deepseek_client import DeepSeekLLMClient

        client = DeepSeekLLMClient(
            api_key="test_key",
            base_url="https://api.deepseek.com",
        )
        assert client._api_key == "test_key"
        assert client._base_url == "https://api.deepseek.com"
        assert client._client is not None

    def test_warm_up_no_crash(self):
        """warm_up 在无网络时优雅处理"""
        import asyncio
        from infrastructure.llm.deepseek_client import DeepSeekLLMClient

        client = DeepSeekLLMClient(api_key="invalid_key")
        # 不抛异常
        asyncio.get_event_loop().run_until_complete(client.warm_up(reason="test"))


class TestServerTopLevel:
    """server.py 顶层导入测试"""

    def test_server_loads(self):
        """server.py 顶层加载成功"""
        import server

        assert server.app is not None
        assert "DEEPSEEK_KEY" in server.CONFIG
        assert "VOLC_API_KEY" in server.CONFIG
        assert "/topics" in [r.path for r in server.app.routes]
        assert "/stats" in [r.path for r in server.app.routes]
