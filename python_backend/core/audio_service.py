"""
core.audio_service — 聚合导出模块。

所有 ASR / TTS 功能已迁移至 core/audio/ 子模块。
本文件保留以维持向后兼容导入路径。
"""
from core.audio.protocol import (
    MIN_AUDIO_BYTES,
    ASR_STREAM_START_BYTES,
    ASR_PCM_CHUNK_BYTES,
    MAX_PAYLOAD_LEN,
    MAX_DECOMPRESS_LEN,
    MAX_ID_LEN,
    MAX_TTS_PAYLOAD_LEN,
    MAX_TTS_CONTROL_PAYLOAD_LEN,
    generate_asr_header,
    pack_tts_request,
    parse_tts_server_frame,
)
from core.audio.asr import (
    run_volcengine_wss_asr,
    run_volc_streaming_asr_worker,
)
from core.audio.tts import (
    TTSPool,
    get_tts_pool,
    init_tts_pool,
    warm_tts_pool,
    run_tts_to_ws,
    run_tts_turn_reused_from_queue,
    iter_tts_pcm_chunks_pooled,
    iter_tts_pcm_chunks,
    _TTS_POOL_SIZE,
)
