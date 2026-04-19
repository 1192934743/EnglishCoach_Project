"""
ASR 模块 —— 语音转文字。

支持两种模式：
1. 整段 ASR：缓冲完整音频后发送，适合短句
2. 流式 ASR：边录边传，降低首字延迟
"""
from __future__ import annotations

import json
import gzip
import struct
import uuid
import logging
import asyncio
import inspect
from typing import Optional, Tuple

from core.audio.protocol import (
    generate_asr_header,
    parse_asr_server_frame,
    MIN_AUDIO_BYTES,
    ASR_STREAM_START_BYTES,
    ASR_PCM_CHUNK_BYTES,
    MAX_PAYLOAD_LEN,
    MAX_DECOMPRESS_LEN,
)

logger = logging.getLogger("EnglishCoach")

# ================= 整段 ASR =================

async def run_volcengine_wss_asr(
    pcm_bytes: bytearray,
    config: dict,
    max_retries: int = 3,
) -> Tuple[str, str]:
    """
    整段 ASR：缓冲完整音频后一次性发送，返回 (final_text, detected_emotion)。

    :param pcm_bytes: 完整 PCM 音频数据
    :param config: 包含 VOLC_API_KEY, VOLC_RESOURCE_ID_ASR, ASR_LANGUAGE 的字典
    :param max_retries: 最大重试次数
    :return: (识别文本, 情绪标签)
    """
    WSS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    final_text, detected_emotion = "", "neutral"

    if len(pcm_bytes) < MIN_AUDIO_BYTES:
        return final_text, detected_emotion

    volc_api_key = config.get('VOLC_API_KEY')
    volc_resource_id = config.get('VOLC_RESOURCE_ID_ASR')
    asr_language = config.get("ASR_LANGUAGE", "en-US")

    if not volc_api_key or not volc_resource_id:
        logger.error("ASR 启动失败: 缺失 VOLC_API_KEY 或 VOLC_RESOURCE_ID_ASR")
        return final_text, detected_emotion

    for attempt in range(max_retries):
        try:
            headers = {
                "Authorization": f"Bearer {volc_api_key}",
                "x-api-key": volc_api_key,
                "X-Api-Resource-Id": volc_resource_id,
                "X-Api-Connect-Id": str(uuid.uuid4())
            }
            async with __import__('websockets').connect(WSS_URL, additional_headers=headers, open_timeout=10) as ws:
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
                        parsed = parse_asr_server_frame(message)
                        if not parsed:
                            continue
                        msg_type, result = parsed
                        if msg_type == 15:
                            break
                        utterances = result.get('result', {}).get('utterances', [])
                        if utterances and utterances[-1].get('definite'):
                            final_text = utterances[-1].get('text', '')
                            detected_emotion = utterances[-1].get('additions', {}).get('emotion', 'neutral')
                            return

                async def send_audio():
                    for i in range(0, len(pcm_bytes), ASR_PCM_CHUNK_BYTES):
                        chunk = pcm_bytes[i: i + ASR_PCM_CHUNK_BYTES]
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

                for task in done:
                    if not task.cancelled():
                        try:
                            exc = task.exception()
                            if exc:
                                logger.warning(f"ASR 并发任务内部异常: {exc}")
                        except asyncio.InvalidStateError:
                            pass

                if send_task in done and receive_task in pending:
                    try:
                        await asyncio.wait_for(receive_task, timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning("ASR 接收终稿超时。")

                for task in pending:
                    if not task.done():
                        task.cancel()
                        try:
                            await task
                        except asyncio.CancelledError:
                            pass

                return final_text, detected_emotion

        except Exception as e:
            logger.warning(f"ASR 协议异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
            else:
                logger.error("ASR 多次尝试后彻底失败")

    return final_text, detected_emotion


# ================= 流式 ASR =================

async def _asr_stream_handle_message(
    message: bytes,
    holder: dict,
    on_partial: Optional[callable] = None,
) -> bool:
    """
    解析单帧流式 ASR 下行；返回 True 表示已拿到 definite 终稿。

    :param message: 原始字节帧
    :param holder: dict，用来累积文本和情绪状态，包含 keys: text, emotion, _partial_sig
    :param on_partial: 可选回调，接收 (partial_text) 参数
    :return: 是否已拿到 definite 结果
    """
    parsed = parse_asr_server_frame(message)
    if not parsed:
        return False

    msg_type, result = parsed
    if msg_type == 15:
        return False

    utterances = result.get("result", {}).get("utterances") or []
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


async def run_volc_streaming_asr_worker(
    pcm_queue: asyncio.Queue,
    config: dict,
    *,
    on_partial: Optional[callable] = None,
    max_retries: int = 3,
) -> Tuple[str, str]:
    """
    真流式 ASR：与 Volc bigmodel_async 单连接，从 pcm_queue 取 PCM 块（bytes）；
    收到 None 表示本句结束，发负包/空包收尾并等待 definite 终稿。

    :param pcm_queue: asyncio.Queue，从中获取 PCM bytes 块，None 表示发送完毕
    :param config: 包含 VOLC_API_KEY, VOLC_RESOURCE_ID_ASR, ASR_LANGUAGE 的字典
    :param on_partial: 可选回调，接收 (partial_text)
    :param max_retries: 最大重试次数
    :return: (final_text, detected_emotion)
    """
    holder: dict = {"text": "", "emotion": "neutral", "_partial_sig": ""}
    volc_api_key = config.get("VOLC_API_KEY")
    volc_resource_id = config.get("VOLC_RESOURCE_ID_ASR")
    asr_language = config.get("ASR_LANGUAGE", "en-US")

    if not volc_api_key or not volc_resource_id:
        logger.error("ASR 流式：缺失 VOLC_API_KEY 或 VOLC_RESOURCE_ID_ASR")
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
            "format": "pcm", "codec": "raw", "rate": 16000,
            "bits": 16, "channel": 1, "language": asr_language,
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
            async with __import__('websockets').connect(
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
                            if await _asr_stream_handle_message(message, holder, on_partial):
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
                        sub = raw[i: i + ASR_PCM_CHUNK_BYTES]
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
                            await asyncio.wait_for(recv_task, timeout=5.0)
                        except asyncio.TimeoutError:
                            logger.warning("ASR 流式：等待 recv 结束超时")
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
                "ASR 流式协议异常 (尝试 %s/%s): %s",
                attempt + 1, max_retries, e
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)

    return holder.get("text") or "", holder.get("emotion") or "neutral"
