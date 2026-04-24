import copy
import json
import asyncio
import gzip
import inspect
import struct
import uuid
import logging
import time
import os
import re
import threading
import websockets
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("EnglishCoach")

# ================= 协议常量与防爆配置 =================

MIN_AUDIO_BYTES = 8000           # 音频过短抛弃：约 250ms (16kHz 16-bit mono 采样率)
# 缓冲达到此字节数即启动流式 ASR（约 150ms），早于 MIN_AUDIO_BYTES，便于用户一开口就建连上传
ASR_STREAM_START_BYTES = 4800
ASR_RECEIVE_TIMEOUT = 5.0        # 发送完毕后，等待识别终稿的最长超时 (秒)
# 火山 sauc bigmodel_async 文档：单包约 200ms PCM 性能最优；100~200ms/包，间隔 100~200ms（实时流）。
# 整段缓冲回放时不宜每包 sleep 200ms（会成倍拉长总耗时），用 200ms 分包 + 仅 yield 让接收协程跑满。
ASR_PCM_CHUNK_BYTES = 6400       # 200ms @ 16kHz mono int16 (16000 * 2 * 0.2)

MAX_ID_LEN = 1024                # TTS: 防止畸形包分配巨量内存的上限保护
MAX_PAYLOAD_LEN = 10 * 1024 * 1024  # ASR: 单帧在链路上的最大长度 (压缩或未压缩)
MAX_TTS_PAYLOAD_LEN = 5 * 1024 * 1024  # TTS: Event 352 音频单帧 payload 上限 (5MB)
MAX_TTS_CONTROL_PAYLOAD_LEN = 512 * 1024  # TTS: 非音频帧 payload 上限 (512KB)
# ASR: 解压后或未压缩时、进入 JSON 解析前的业务体最大长度 (防 Zip Bomb / 异常大 JSON)
MAX_DECOMPRESS_LEN = 50 * 1024 * 1024

# ================= 协议打包工具 =================

def generate_asr_header(message_type, flags, serialization, compression):
    header = bytearray(4)
    header[0] = 0x11
    header[1] = (message_type << 4) | flags
    header[2] = (serialization << 4) | compression
    header[3] = 0x00
    return bytes(header)

def pack_tts_request(event_type, session_id="", payload_dict=None):
    """
    火山双向 TTS 二进制封包与事件号说明：
    - Event 1: 建立连接请求 (Client -> Server)
    - Event 2: 客户端发送完毕，请求结束会话 (Client -> Server)
    - Event 50: 服务端建连就绪，请求配置 (Server -> Server)
    - Event 100: 发送具体业务参数，如音色、采样率 (Client -> Server)
    - Event 150: 鉴权/配置通过，会话正式启动 (Server -> Client)
    - Event 200: 发送待合成文本 (Client -> Server)
    - Event 102: 标记当前文本流发送完成 (Client -> Server)
    - Event 352: 下发合成好的音频流 (Server -> Client)
    - Event 152: 服务端告知当前文本已全部合成完毕 (Server -> Client)
    - Event 52: 服务端确认会话关闭 (Server -> Client)
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

# ================= 1. 语音转文字 (ASR) =================

async def run_volcengine_wss_asr(pcm_bytes: bytearray, config: dict, max_retries=3):
    WSS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    final_text, detected_emotion = "", "neutral"

    if len(pcm_bytes) < MIN_AUDIO_BYTES:
        return final_text, detected_emotion

    volc_api_key = config.get('VOLC_API_KEY')
    volc_resource_id = config.get('VOLC_RESOURCE_ID_ASR')
    asr_language = config.get("ASR_LANGUAGE", "en-US")

    if not volc_api_key or not volc_resource_id:
        logger.error("🚨 ASR 启动失败: 缺失 VOLC_API_KEY 或 VOLC_RESOURCE_ID_ASR")
        return final_text, detected_emotion

    for attempt in range(max_retries):
        try:
            headers = {
                "Authorization": f"Bearer {volc_api_key}",
                "x-api-key": volc_api_key,
                "X-Api-Resource-Id": volc_resource_id,
                "X-Api-Connect-Id": str(uuid.uuid4())
            }
            async with websockets.connect(WSS_URL, additional_headers=headers, open_timeout=10) as ws:
                req_params = {
                    "user": {"uid": "english_ai"},
                    "audio": {
                        "format": "pcm", "codec": "raw", "rate": 16000,
                        "bits": 16, "channel": 1, "language": asr_language
                    },
                    "request": {
                        "model_name": "bigmodel",
                        "enable_punc": True,
                        "enable_itn": True,
                        "enable_ddc": True,
                        "show_utterances": True,
                        "enable_emotion_detection": True,
                        "enable_nonstream": False,
                    }
                }
                payload = gzip.compress(json.dumps(req_params).encode('utf-8'))
                await ws.send(generate_asr_header(1, 0, 1, 1) + struct.pack('>I', len(payload)) + payload)

                async def receive_responses():
                    nonlocal final_text, detected_emotion
                    async for message in ws:
                        if isinstance(message, str) or len(message) < 4: continue
                        serialization, compression = (message[2] >> 4) & 0x0f, message[2] & 0x0f

                        # 解析正常结果帧
                        if (message[1] >> 4) & 0x0f == 9:
                            payload_offset = ((message[0] & 0x0f) * 4) + (4 if (message[1] & 0x0f) in [1, 3] else 0) + 4
                            if payload_offset > len(message): continue

                            payload_len = struct.unpack('>I', message[payload_offset - 4:payload_offset])[0]
                            # ASR 大包截断与越界保护
                            if payload_len > MAX_PAYLOAD_LEN or payload_offset + payload_len > len(message):
                                logger.warning(f"ASR 抛弃异常大包: payload_len={payload_len}")
                                continue

                            payload_bytes = message[payload_offset:payload_offset + payload_len]

                            if compression == 1:
                                try:
                                    payload_bytes = gzip.decompress(payload_bytes)
                                except Exception as e:
                                    logger.warning(f"ASR Gzip 解压失败: {e}")
                                    continue

                            # 解压后或未压缩原始体统一上限，再进入 JSON（防 Zip Bomb 与异常大 JSON）
                            if len(payload_bytes) > MAX_DECOMPRESS_LEN:
                                logger.warning("ASR 抛弃超大业务体 (超过 MAX_DECOMPRESS_LEN)")
                                continue

                            if serialization == 1:
                                try:
                                    result = json.loads(payload_bytes.decode('utf-8')).get('result', {})
                                    utterances = result.get('utterances', [])
                                    if utterances and utterances[-1].get('definite'):
                                        final_text = utterances[-1].get('text', '')
                                        detected_emotion = utterances[-1].get('additions', {}).get('emotion', 'neutral')
                                        return
                                except Exception as e:
                                    logger.warning(f"ASR JSON 解析失败: {e}")
                                    continue

                        # 解析服务端报错帧
                        elif (message[1] >> 4) & 0x0f == 15:
                            header_size = (message[0] & 0x0f) * 4
                            err_raw = message[header_size:]
                            try:
                                err_str = err_raw.decode('utf-8', errors='ignore')
                                if '{' in err_str:
                                    err_str = json.loads(err_str).get('message', err_str)
                            except Exception:
                                err_str = f"RawHex: {err_raw.hex()[:50]}"
                            logger.error(f"ASR 服务端返回错误: {err_str}")
                            break

                async def send_audio():
                    for i in range(0, len(pcm_bytes), ASR_PCM_CHUNK_BYTES):
                        chunk = pcm_bytes[i : i + ASR_PCM_CHUNK_BYTES]
                        payload = gzip.compress(chunk)
                        await ws.send(
                            generate_asr_header(2, 0, 0, 1)
                            + struct.pack(">I", len(payload))
                            + payload
                        )
                        await asyncio.sleep(0)
                    empty = gzip.compress(b'')
                    await ws.send(generate_asr_header(2, 2, 0, 1) + struct.pack('>I', len(empty)) + empty)

                receive_task = asyncio.create_task(receive_responses())
                send_task = asyncio.create_task(send_audio())

                done, pending = await asyncio.wait(
                    [receive_task, send_task],
                    return_when=asyncio.FIRST_COMPLETED
                )

                # 兼容性加强：防 CancelledError 与 InvalidStateError 穿透
                for task in done:
                    if not task.cancelled():
                        try:
                            exc = task.exception()
                            if exc:
                                logger.warning(f"ASR 并发任务内部结束/异常: {exc}")
                        except asyncio.InvalidStateError:
                            pass

                if send_task in done and receive_task in pending:
                    try:
                        await asyncio.wait_for(receive_task, timeout=ASR_RECEIVE_TIMEOUT)
                    except asyncio.TimeoutError:
                        logger.warning("⚠️ ASR 接收终稿超时。")

                for task in pending:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                return final_text, detected_emotion

        except Exception as e:
            logger.warning(f"⚠️ ASR 协议异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
            else:
                logger.error("❌ ASR 多次尝试后彻底失败")

    return final_text, detected_emotion


async def _asr_handle_server_message(
    message: bytes,
    holder: dict,
    on_partial,
) -> bool:
    """解析单帧 ASR 下行；返回 True 表示已拿到 definite 终稿。"""
    if isinstance(message, str) or len(message) < 4:
        return False
    serialization = (message[2] >> 4) & 0x0F
    compression = message[2] & 0x0F
    msg_type_high = (message[1] >> 4) & 0x0F

    if msg_type_high == 9:
        payload_offset = ((message[0] & 0x0F) * 4) + (
            4 if (message[1] & 0x0F) in [1, 3] else 0
        ) + 4
        if payload_offset > len(message):
            return False
        payload_len = struct.unpack(">I", message[payload_offset - 4 : payload_offset])[0]
        if payload_len > MAX_PAYLOAD_LEN or payload_offset + payload_len > len(message):
            logger.warning("ASR(stream) 抛弃异常大包: payload_len=%s", payload_len)
            return False
        payload_bytes = message[payload_offset : payload_offset + payload_len]
        if compression == 1:
            try:
                payload_bytes = gzip.decompress(payload_bytes)
            except Exception as e:
                logger.warning("ASR(stream) Gzip 解压失败: %s", e)
                return False
        if len(payload_bytes) > MAX_DECOMPRESS_LEN:
            logger.warning("ASR(stream) 抛弃超大业务体")
            return False
        if serialization != 1:
            return False
        try:
            result = json.loads(payload_bytes.decode("utf-8")).get("result", {})
        except Exception as e:
            logger.warning("ASR(stream) JSON 解析失败: %s", e)
            return False
        utterances = result.get("utterances") or []
        if not utterances:
            return False
        u = utterances[-1]
        text = (u.get("text") or "").strip()
        definite = bool(u.get("definite"))
        emotion = u.get("additions", {}).get("emotion", "neutral")
        if text and not definite and on_partial is not None:
            prev = holder.get("_partial_sig")
            if text != prev:
                holder["_partial_sig"] = text
                pr = on_partial(text)
                if inspect.isawaitable(pr):
                    await pr
        if definite and text:
            holder["text"] = text
            holder["emotion"] = emotion
            return True
        return False

    if msg_type_high == 15:
        header_size = (message[0] & 0x0F) * 4
        err_raw = message[header_size:]
        try:
            err_str = err_raw.decode("utf-8", errors="ignore")
            if "{" in err_str:
                err_str = json.loads(err_str).get("message", err_str)
        except Exception:
            err_str = f"RawHex: {err_raw.hex()[:50]}"
        logger.error("ASR(stream) 服务端错误: %s", err_str)
    return False


async def run_volc_streaming_asr_worker(
    pcm_queue: asyncio.Queue,
    config: dict,
    *,
    on_partial=None,
    max_retries: int = 3,
):
    """
    真流式 ASR：与 Volc bigmodel_async 单连接，从 pcm_queue 取 PCM 块（bytes）；
    收到 None 表示本句结束，发负包/空包收尾并等待 definite 终稿。
    """
    holder: dict = {"text": "", "emotion": "neutral", "_partial_sig": ""}
    volc_api_key = config.get("VOLC_API_KEY")
    volc_resource_id = config.get("VOLC_RESOURCE_ID_ASR")
    asr_language = config.get("ASR_LANGUAGE", "en-US")
    if not volc_api_key or not volc_resource_id:
        logger.error("🚨 ASR 流式：缺失 VOLC_API_KEY 或 VOLC_RESOURCE_ID_ASR")
        return "", "neutral"

    WSS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    headers = {
        "Authorization": f"Bearer {volc_api_key}",
        "x-api-key": volc_api_key,
        "X-Api-Resource-Id": volc_resource_id,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }
    req_params = {
        "user": {"uid": "english_ai"},
        "audio": {
            "format": "pcm",
            "codec": "raw",
            "rate": 16000,
            "bits": 16,
            "channel": 1,
            "language": asr_language,
        },
        "request": {
            "model_name": "bigmodel",
            "enable_punc": True,
            "enable_itn": True,
            "enable_ddc": True,
            "show_utterances": True,
            "enable_emotion_detection": True,
            "enable_nonstream": False,
        },
    }
    init_payload = gzip.compress(json.dumps(req_params).encode("utf-8"))

    for attempt in range(max_retries):
        holder["text"] = ""
        holder["emotion"] = "neutral"
        holder["_partial_sig"] = ""
        try:
            async with websockets.connect(
                WSS_URL, additional_headers=headers, open_timeout=10
            ) as ws:
                await ws.send(
                    generate_asr_header(1, 0, 1, 1)
                    + struct.pack(">I", len(init_payload))
                    + init_payload
                )

                recv_done = asyncio.Event()

                async def recv_loop():
                    try:
                        async for message in ws:
                            if await _asr_handle_server_message(
                                message, holder, on_partial
                            ):
                                recv_done.set()
                                return
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:
                        logger.warning("ASR(stream) recv_loop 异常: %s", e)

                recv_task = asyncio.create_task(recv_loop())

                async def send_pcm_chunk(raw: bytes):
                    for i in range(0, len(raw), ASR_PCM_CHUNK_BYTES):
                        if recv_done.is_set():
                            return
                        sub = raw[i : i + ASR_PCM_CHUNK_BYTES]
                        pl = gzip.compress(sub)
                        await ws.send(
                            generate_asr_header(2, 0, 0, 1)
                            + struct.pack(">I", len(pl))
                            + pl
                        )
                        await asyncio.sleep(0)

                try:
                    while not recv_done.is_set():
                        item = await pcm_queue.get()
                        if item is None:
                            if not recv_done.is_set():
                                empty = gzip.compress(b"")
                                await ws.send(
                                    generate_asr_header(2, 2, 0, 1)
                                    + struct.pack(">I", len(empty))
                                    + empty
                                )
                            break
                        if item:
                            await send_pcm_chunk(item)
                except asyncio.CancelledError:
                    recv_task.cancel()
                    raise
                finally:
                    if not recv_task.done():
                        try:
                            await asyncio.wait_for(
                                recv_task, timeout=ASR_RECEIVE_TIMEOUT
                            )
                        except asyncio.TimeoutError:
                            logger.warning("⚠️ ASR 流式：等待 recv 结束超时")
                            recv_task.cancel()
                            try:
                                await recv_task
                            except asyncio.CancelledError:
                                pass
                        except Exception:
                            recv_task.cancel()
                            try:
                                await recv_task
                            except asyncio.CancelledError:
                                pass
                    else:
                        try:
                            await recv_task
                        except Exception:
                            pass

                return holder.get("text") or "", holder.get("emotion") or "neutral"

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(
                "⚠️ ASR 流式协议异常 (尝试 %s/%s): %s",
                attempt + 1,
                max_retries,
                e,
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)

    return holder.get("text") or "", holder.get("emotion") or "neutral"


# ================= 2. 文本转流式语音 (TTS) — Azure Neural TTS =================
"""
Azure Neural TTS 流式合成模块。

设计原则：
- 使用 synthesizing 事件 + asyncio.Queue 实现真正线程安全流式推流，
  彻底消除 PullAudioOutputStream + asyncio.to_thread 的线程漏
- run_tts_to_ws：点读单句入口，audio_config=None，由 server.py 在 finally 中发送 tts_finished。
- run_tts_turn_reused_from_queue：队列消费入口，引擎初始化在循环外，
  整个 turn 复用同一个 SpeechSynthesizer，由 server.py 在 finally 中发送 tts_finished。
- 不需要外部连接池（Azure SDK 内部维护 WebSocket 复用）。
"""

import azure.cognitiveservices.speech as speechsdk


def _sanitize_tts_text(text: str) -> str:
    """去除不可用于 TTS 合成的控制字符，保留标点与文字。"""
    if not text:
        return ""
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text).strip()


# ── 点读入口（server.py request_tts 调用）────────────────────────────────────

async def run_tts_to_ws(
    text: str,
    client_ws: WebSocket,
    ws_lock: asyncio.Lock,
    config: dict,
    latency_hooks: Optional[dict] = None,
):
    """
    点读单句 TTS：调用 Azure Neural TTS，将 PCM 流实时推送到前端。
    使用 synthesizing 事件 + asyncio.Queue 实现真正的线程安全流式推流，
    彻底消除 PullAudioOutputStream + asyncio.to_thread 的线程泄漏。
    tts_finished 信号由 server.py 调用方在 finally 块中统一发送，此函数不发送结束信号。
    """
    azure_key = config.get("AZURE_SPEECH_KEY", "").strip()
    azure_region = config.get("AZURE_SPEECH_REGION", "").strip()
    voice_name = config.get("VOICE", "en-US-AriaNeural").strip()

    if not azure_key:
        logger.error("[AzureTTS] 缺失 AZURE_SPEECH_KEY，请检查 config.env")
        return

    try:
        speech_config = speechsdk.SpeechConfig(
            subscription=azure_key, region=azure_region
        )
        speech_config.set_speech_synthesis_output_format(
            speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm
        )
        speech_config.speech_synthesis_voice_name = voice_name

        # audio_config=None：不推送到扬声器，只触发 synthesizing 回调
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
                    await client_ws.send_bytes(chunk)

            except asyncio.TimeoutError:
                if synthesis_done.is_set() and audio_queue.empty():
                    break

    except Exception as e:
        logger.warning("[AzureTTS] 点读下发中断 (前端可能断开): %s", e)


# ── 队列消费入口（server.py tts_consumer 调用）────────────────────────────────

async def run_tts_turn_reused_from_queue(
    client_ws: WebSocket,
    ws_lock: asyncio.Lock,
    config: dict,
    segment_queue: asyncio.Queue,
    latency_hooks: Optional[dict] = None,
):
    """
    一轮对话 TTS：从 segment_queue 逐句取出文本，调用 Azure Neural TTS 合成，
    将 PCM 流实时推送到前端。

    引擎初始化在循环外——整个 turn 复用同一个 SpeechSynthesizer 实例，
    每句只调用 start_speaking_text_async，彻底消灭每句重握手的 5s 延迟。
    使用 synthesizing 事件 + asyncio.Queue 实现真正的线程安全流式推流，
    彻底消除 PullAudioOutputStream + asyncio.to_thread 的线程泄漏。
    tts_finished 信号由 server.py tts_consumer 调用方在 finally 块中统一发送，
    此函数不发送结束信号。
    """
    azure_key = config.get("AZURE_SPEECH_KEY", "").strip()
    azure_region = config.get("AZURE_SPEECH_REGION", "").strip()
    voice_name = config.get("VOICE", "en-US-AriaNeural").strip()

    if not azure_key:
        logger.error("[AzureTTS] 缺失 AZURE_SPEECH_KEY，请检查 config.env")
        return

    try:
        # ── 引擎初始化移到循环外：整个 turn 只握手一次 ──
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

            logger.info(f"[TRACK_TTS] 开始请求 Azure 合成单句 text={clean_text[:40]}")
            synthesis_done.clear()
            synthesizer.start_speaking_text_async(clean_text)

            last_chunk_time = time.perf_counter()
            chunk_seq = 0
            first_yielded = False
            while True:
                try:
                    chunk = await asyncio.wait_for(audio_queue.get(), timeout=1.0)
                    if chunk is None:
                        logger.info(f"[TRACK_TTS] 句合成结束，总包数 chunk_seq={chunk_seq}")
                        break

                    # 句内卡顿检测
                    now = time.perf_counter()
                    interval_ms = (now - last_chunk_time) * 1000.0
                    last_chunk_time = now
                    chunk_seq += 1
                    if interval_ms > 200:
                        logger.warning(f"[TRACK_TTS] 句内音频流生成卡顿! interval={interval_ms:.1f}ms chunk_seq={chunk_seq} len={len(chunk)}")

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
                        await client_ws.send_bytes(chunk)

                except asyncio.TimeoutError:
                    if synthesis_done.is_set() and audio_queue.empty():
                        break

            segment_queue.task_done()

    except Exception as e:
        logger.warning("[AzureTTS] 对话轮次 TTS 异常: %s", e)


# ── 导出接口（兼容旧 import，server.py 无需大幅修改）───────────────────────────

def init_tts_pool(config: dict):
    """Azure TTS 无需外部连接池，保留函数签名兼容。"""
    pass


async def warm_tts_pool():
    """Azure TTS 无需预热，保留函数签名兼容。"""
    pass


def get_tts_pool():
    """Azure TTS 无需外部连接池，保留函数签名兼容。"""
    return None
