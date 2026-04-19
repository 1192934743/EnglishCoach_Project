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
import websockets
from collections.abc import AsyncIterator
from typing import Optional, Tuple

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
    - Event 50: 服务端建连就绪，请求配置 (Server -> Client)
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

# ================= 2. 文本转流式语音 (TTS) =================

# ================= TTS 连接预热池 =================

_TTS_POOL_SIZE = int(os.getenv("TTS_WARM_POOL_SIZE", "2"))
_TTS_WARM_TIMEOUT_SEC = float(os.getenv("TTS_WARM_TIMEOUT_SEC", "300"))


class _WarmTTSConnection:
    """
    单条预热好的 TTS 连接。
    持有已建立的 WebSocket，状态为等待 StartSession 请求。
    """

    __slots__ = ("ws", "connect_id", "created_at")

    def __init__(self, ws, connect_id: str):
        self.ws = ws
        self.connect_id = connect_id
        self.created_at = time.monotonic()


class TTSPool:
    """
    TTS 连接池：预热 N 条连接，调用时直接取用，省去建连 + 握手 + SessionStarted 的 ~200-500ms。

    官方最佳实践（双向流式 TTS）：
      ConnectionStarted → StartSession → TaskRequest → ... → SessionFinished → (新) StartSession
    每次 StartSession 前服务端不再发 50，收到 150 即代表 session 正式开始。

    使用流程：
      conn = await pool.acquire()   # 等待一条已预热的连接（或新建）
      # conn.ws 已就绪，可在上面发 StartSession / TaskRequest
      # 收到 SessionFinished 后立即 startNewSession，连接留在池中
      await pool.release(conn)       # 放回池中（连接未关闭）
    """

    def __init__(self, config: dict):
        self._config = config
        self._idle: asyncio.Queue = asyncio.Queue()
        self._in_use: int = 0
        self._total_created: int = 0
        self._closed: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()

    async def warm_up(self) -> None:
        """服务启动时预热连接（并行创建，不阻塞）"""
        if self._closed:
            return
        async with self._lock:
            if self._total_created >= _TTS_POOL_SIZE:
                return
            remaining = _TTS_POOL_SIZE - self._total_created
        tasks = [self._build_one_connection() for _ in range(remaining)]
        await asyncio.gather(*tasks, return_exceptions=True)
        logger.info(f"[TTSPool] warm_up 完成，预热 {self._total_created}/{_TTS_POOL_SIZE} 条连接")

    async def _build_one_connection(self) -> _WarmTTSConnection | None:
        """创建一条预热连接：完成 StartConnection + 等待 ConnectionStarted"""
        WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
        api_key = self._config.get("VOLC_API_KEY")
        resource_id = self._config.get("VOLC_RESOURCE_ID_TTS")
        if not api_key or not resource_id:
            logger.warning("[TTSPool] 缺少 TTS 配置，跳过建连")
            return None
        connect_id = str(uuid.uuid4())
        headers = {
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Connect-Id": connect_id,
        }
        try:
            ws = await asyncio.wait_for(
                websockets.connect(WSS_URL, additional_headers=headers, open_timeout=10),
                timeout=12.0,
            )
            await ws.send(pack_tts_request(1))
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=min(2.0, deadline - time.monotonic()))
                except asyncio.TimeoutError:
                    continue
                parsed = _try_parse_tts_server_frame(raw)
                if not parsed:
                    continue
                event_type, _ = parsed
                if event_type == 52:
                    continue
                if event_type == 50:
                    conn = _WarmTTSConnection(ws, connect_id)
                    async with self._lock:
                        self._total_created += 1
                    await self._idle.put(conn)
                    logger.debug(f"[TTSPool] 连接就绪: {connect_id[:8]}")
                    return conn
                if event_type == 51:
                    logger.warning(f"[TTSPool] 服务端拒绝建连: {connect_id[:8]}")
                    return None
            logger.warning(f"[TTSPool] 等待 ConnectionStarted 超时: {connect_id[:8]}")
        except asyncio.TimeoutError:
            logger.warning(f"[TTSPool] 建连超时: {connect_id[:8]}")
        except Exception as e:
            logger.warning(f"[TTSPool] 建连异常: {e}")
        return None

    async def acquire(self) -> _WarmTTSConnection:
        """
        获取一条就绪连接（已在 ConnectionStarted 状态）。
        若池为空则新建一条（同步等待）。
        """
        if self._closed:
            raise RuntimeError("TTSPool 已关闭")
        while True:
            try:
                conn = self._idle.get_nowait()
                if time.monotonic() - conn.created_at > _TTS_WARM_TIMEOUT_SEC:
                    logger.debug(f"[TTSPool] 连接过期，丢弃并重建: {conn.connect_id[:8]}")
                    await self._close_one(conn)
                    continue
                self._in_use += 1
                return conn
            except asyncio.QueueEmpty:
                pass
            async with self._lock:
                if self._total_created < _TTS_POOL_SIZE:
                    needed = min(1, _TTS_POOL_SIZE - self._total_created)
                else:
                    needed = 0
            if needed > 0:
                conn = await self._build_one_connection()
                if conn is not None:
                    self._in_use += 1
                    return conn
            await asyncio.sleep(0.05)

    async def release(self, conn: _WarmTTSConnection) -> None:
        """将连接放回空闲池（尝试 ping 保活，失败则丢弃）"""
        self._in_use -= 1
        if self._closed:
            await self._close_one(conn)
            return
        try:
            await asyncio.wait_for(conn.ws.ping(), timeout=3.0)
        except Exception:
            logger.debug(f"[TTSPool] 连接已失效，丢弃: {conn.connect_id[:8]}")
            await self._close_one(conn)
            async with self._lock:
                if self._total_created > 0:
                    self._total_created -= 1
            return
        await self._idle.put(conn)

    async def _close_one(self, conn: _WarmTTSConnection) -> None:
        try:
            await conn.ws.close()
        except Exception:
            pass

    async def close(self) -> None:
        """关闭池中所有连接（服务停止时调用）"""
        self._closed = True
        closed = 0
        while True:
            try:
                conn = self._idle.get_nowait()
                await self._close_one(conn)
                closed += 1
            except asyncio.QueueEmpty:
                break
        logger.info(f"[TTSPool] 已关闭 {closed} 条预热连接")


# 模块级连接池单例（server.py 启动时初始化）
_tts_pool: TTSPool | None = None


def get_tts_pool() -> TTSPool | None:
    return _tts_pool


def init_tts_pool(config: dict) -> TTSPool:
    global _tts_pool
    if _tts_pool is None:
        _tts_pool = TTSPool(config)
    return _tts_pool


async def warm_tts_pool() -> None:
    """供 server.py startup 调用，启动时并行预热连接池"""
    pool = get_tts_pool()
    if pool:
        await pool.warm_up()


async def iter_tts_pcm_chunks_pooled(
    text: str,
    config: dict,
    latency_hooks: Optional[dict] = None,
) -> AsyncIterator[bytes]:
    """
    从连接池取出一条预热好的 TTS 连接，省去 StartConnection + ConnectionStarted 开销。
    内部完成 StartSession → TaskRequest(逐字) → FinishSession → 释放回池。

    官方流程（连接复用）：
      StartSession(100) → SessionStarted(150) → TaskRequest(200) × N → FinishSession(102)
      → 收到 SessionFinished(152) 后，连接回到池中供下一句复用。
    """
    pool = get_tts_pool()
    if pool is None:
        logger.warning("[TTSPool] 池未初始化，回退到非池化模式")
        async for chunk in iter_tts_pcm_chunks(text, config, latency_hooks):
            yield chunk
        return

    voice_type = config.get("VOICE", "BV001_streaming")
    session_id = str(uuid.uuid4())

    conn = None
    try:
        conn = await pool.acquire()
        ws = conn.ws

        base_req = {
            "user": {"uid": "english_coach"},
            "namespace": "BidirectionalTTS",
            "req_params": {
                "speaker": voice_type,
                "audio_params": {
                    "format": "pcm",
                    "sample_rate": 24000,
                    "enable_timestamp": True,
                },
                "additions": json.dumps({
                    "disable_markdown_filter": False,
                }),
            },
        }

        await ws.send(pack_tts_request(100, session_id, base_req))

        deadline = time.monotonic() + 8.0
        saw_150 = False
        while time.monotonic() < deadline and not saw_150:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=min(2.0, max(0.05, deadline - time.monotonic()))
                )
            except asyncio.TimeoutError:
                continue
            parsed = _try_parse_tts_server_frame(raw)
            if not parsed:
                continue
            et, pb = parsed
            if et == 352:
                if latency_hooks is not None and not latency_hooks.get("_tts_first_pcm_logged"):
                    latency_hooks["_tts_first_pcm_logged"] = True
                    t0 = latency_hooks.get("t0")
                    tid = latency_hooks.get("turn_id", "?")
                    if isinstance(t0, (int, float)):
                        ms = (time.perf_counter() - float(t0)) * 1000.0
                        logger.info(
                            "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                            tid,
                            "07_first_pcm_to_client",
                            ms,
                            len(pb),
                        )
                yield pb
                continue
            if et == 150:
                saw_150 = True
                break
            if et == 350:
                logger.debug("TTS 句子开始 (event 350)")
                continue
            if et == 351:
                logger.debug("TTS 句子结束 (event 351)")
                continue
            if et == 52:
                continue
        if not saw_150:
            raise TimeoutError("火山引擎 TTS 池化连接等待 SessionStarted(150) 超时")

        for char in text:
            synthesis_req = copy.deepcopy(base_req)
            synthesis_req["event"] = 200
            synthesis_req["req_params"]["text"] = char
            await ws.send(pack_tts_request(200, session_id, synthesis_req))
            await asyncio.sleep(0.005)

        await ws.send(pack_tts_request(102, session_id))

        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(
                    ws.recv(), timeout=min(2.0, max(0.05, deadline - time.monotonic()))
                )
            except asyncio.TimeoutError:
                continue
            parsed = _try_parse_tts_server_frame(raw)
            if not parsed:
                continue
            et, pb = parsed
            if et == 352:
                if latency_hooks is not None and not latency_hooks.get("_tts_first_pcm_logged"):
                    latency_hooks["_tts_first_pcm_logged"] = True
                    t0 = latency_hooks.get("t0")
                    tid = latency_hooks.get("turn_id", "?")
                    if isinstance(t0, (int, float)):
                        ms = (time.perf_counter() - float(t0)) * 1000.0
                        logger.info(
                            "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                            tid,
                            "07_first_pcm_to_client",
                            ms,
                            len(pb),
                        )
                yield pb
                continue
            if et == 350:
                logger.debug("TTS 句子开始 (event 350)")
                continue
            if et == 351:
                logger.debug("TTS 句子结束 (event 351)")
                continue
            if et == 152:
                break
            if et == 52:
                continue
        # 成功完成，连接回到池中
        await pool.release(conn)
        conn = None

    except asyncio.CancelledError:
        if conn is not None:
            try:
                await pool.release(conn)
            except Exception:
                pass
        raise
    except Exception:
        if conn is not None:
            try:
                await pool.release(conn)
            except Exception:
                pass
        raise


# ================= TTS 核心异步迭代器（支持从连接池取用） =================

_PCM_STREAM_END = object()


def _try_parse_tts_server_frame(message: bytes) -> Optional[Tuple[int, bytes]]:
    """
    解析双向 TTS 下行帧，返回 (event_type, payload) 或 None（跳过/损坏）。
    """
    if isinstance(message, str) or len(message) < 4:
        return None
    header_size = (message[0] & 0x0F) * 4
    msg_type = (message[1] >> 4) & 0x0F
    flags = message[1] & 0x0F

    if msg_type == 15:
        err_raw = message[header_size:]
        try:
            err_str = err_raw.decode("utf-8", errors="ignore")
            if "{" in err_str:
                err_str = json.loads(err_str).get("message", err_str)
        except Exception:
            err_str = f"RawHex: {err_raw.hex()[:50]}"
        logger.error(f"TTS 服务端返回错误: {err_str}")
        return None

    if msg_type not in [9, 11]:
        return None

    offset = header_size
    event_type: Optional[int] = None
    if flags == 4:
        if offset + 4 > len(message):
            return None
        event_type = struct.unpack(">i", message[offset : offset + 4])[0]
        offset += 4

        if offset + 4 > len(message):
            return None
        id_len = struct.unpack(">I", message[offset : offset + 4])[0]

        if id_len > MAX_ID_LEN or offset + 4 + id_len > len(message):
            return None
        offset += 4 + id_len
    else:
        event_type = None

    if offset + 4 > len(message):
        return None
    payload_size = struct.unpack(">I", message[offset : offset + 4])[0]
    offset += 4

    pl_limit = (
        MAX_TTS_PAYLOAD_LEN
        if event_type == 352
        else MAX_TTS_CONTROL_PAYLOAD_LEN
    )
    if payload_size > pl_limit or offset + payload_size > len(message):
        return None

    payload = message[offset : offset + payload_size]
    if event_type is None:
        return None
    return (event_type, payload)


async def iter_tts_pcm_chunks(
    text: str,
    config: dict,
    latency_hooks: Optional[dict] = None,
) -> AsyncIterator[bytes]:
    """
    单句、单 WebSocket：按包 yield PCM（event 352）。
    火山官方推荐单连接复用、在 SessionFinished 后再 startSession；当前实现仍为
    「每句新连接」以规避历史上同连接连发第二句起不回 152 的卡死，与 server 顺序消费一致。
    """
    WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"

    volc_api_key = config.get("VOLC_API_KEY")
    volc_resource_id = config.get("VOLC_RESOURCE_ID_TTS")
    voice_type = config.get("VOICE", "BV001_streaming")

    if not volc_api_key or not volc_resource_id:
        logger.error("TTS 启动失败: 缺失配置信息")
        return

    headers = {
        "Authorization": f"Bearer {volc_api_key}",
        "x-api-key": volc_api_key,
        "X-Api-Resource-Id": volc_resource_id,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }

    pcm_queue: asyncio.Queue = asyncio.Queue()
    stream_ended = False
    session_started_event = asyncio.Event()

    async def mark_stream_end():
        nonlocal stream_ended
        if stream_ended:
            return
        stream_ended = True
        await pcm_queue.put(_PCM_STREAM_END)

    async def receiver():
        try:
            async for message in ws:
                if isinstance(message, str) or len(message) < 4:
                    continue
                header_size = (message[0] & 0x0F) * 4
                msg_type = (message[1] >> 4) & 0x0F
                flags = message[1] & 0x0F

                if msg_type == 15:
                    err_raw = message[header_size:]
                    try:
                        err_str = err_raw.decode("utf-8", errors="ignore")
                        if "{" in err_str:
                            err_str = json.loads(err_str).get("message", err_str)
                    except Exception:
                        err_str = f"RawHex: {err_raw.hex()[:50]}"
                    logger.error(f"TTS 服务端返回错误: {err_str}")
                    break

                if msg_type not in [9, 11]:
                    continue

                offset = header_size
                if flags == 4:
                    if offset + 4 > len(message):
                        continue
                    event_type = struct.unpack(">i", message[offset : offset + 4])[0]
                    offset += 4

                    if offset + 4 > len(message):
                        continue
                    id_len = struct.unpack(">I", message[offset : offset + 4])[0]

                    if id_len > MAX_ID_LEN or offset + 4 + id_len > len(message):
                        logger.warning(f"TTS 抛弃异常包: id_len={id_len}")
                        continue
                    offset += 4 + id_len
                else:
                    event_type = None

                if offset + 4 > len(message):
                    continue
                payload_size = struct.unpack(">I", message[offset : offset + 4])[0]
                offset += 4

                pl_limit = (
                    MAX_TTS_PAYLOAD_LEN
                    if event_type == 352
                    else MAX_TTS_CONTROL_PAYLOAD_LEN
                )
                if payload_size > pl_limit or offset + payload_size > len(message):
                    logger.warning(
                        f"TTS 抛弃异常包: payload_size={payload_size}, limit={pl_limit}"
                    )
                    continue

                payload = message[offset : offset + payload_size]

                if event_type == 50:
                    req = {
                        "user": {"uid": "english_coach"},
                        "namespace": "BidirectionalTTS",
                        "req_params": {
                            "speaker": voice_type,
                            "audio_params": {
                                "format": "pcm",
                                "sample_rate": 24000,
                                "enable_timestamp": True,
                            },
                            "additions": json.dumps({
                                "disable_markdown_filter": False,
                            }),
                        },
                    }
                    await ws.send(pack_tts_request(100, session_id, req))

                elif event_type == 150:
                    session_started_event.set()

                elif event_type == 350:
                    logger.debug("TTS 句子开始 (event 350)")

                elif event_type == 351:
                    logger.debug("TTS 句子结束 (event 351)")

                elif event_type == 352:
                    if latency_hooks is not None and not latency_hooks.get(
                        "_tts_first_pcm_logged"
                    ):
                        latency_hooks["_tts_first_pcm_logged"] = True
                        t0 = latency_hooks.get("t0")
                        tid = latency_hooks.get("turn_id", "?")
                        if isinstance(t0, (int, float)):
                            ms = (time.perf_counter() - float(t0)) * 1000.0
                            logger.info(
                                "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                                tid,
                                "07_first_pcm_to_client",
                                ms,
                                len(payload),
                            )
                    await pcm_queue.put(payload)

                elif event_type == 152:
                    # SessionFinished：服务端通知本 session 合成完毕，session 关闭。
                    # 不发 Event 2（FinishConnection），连接由上下文管理器自动关闭。
                    break

                elif event_type == 52:
                    # ConnectionFinished：服务端确认连接已关闭。收到后 break 退出。
                    break
        finally:
            await mark_stream_end()

    try:
        async with websockets.connect(
            WSS_URL, additional_headers=headers, open_timeout=10
        ) as ws:
            await ws.send(pack_tts_request(1))
            session_id = str(uuid.uuid4())

            recv_task = asyncio.create_task(receiver())

            try:
                try:
                    await asyncio.wait_for(session_started_event.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    raise TimeoutError("火山引擎 TTS 建连超时")

                base_req = {
                    "user": {"uid": "english_coach"},
                    "namespace": "BidirectionalTTS",
                    "req_params": {
                        "speaker": voice_type,
                        "audio_params": {
                            "format": "pcm",
                            "sample_rate": 24000,
                            "enable_timestamp": True,
                        },
                        "additions": json.dumps({
                            "disable_markdown_filter": False,
                        }),
                    },
                }

                # 官方最佳实践：逐字发送文本，5ms 间隔，让服务端尽早开始合成
                for char in text:
                    synthesis_req = copy.deepcopy(base_req)
                    synthesis_req["event"] = 200
                    synthesis_req["req_params"]["text"] = char
                    await ws.send(pack_tts_request(200, session_id, synthesis_req))
                    await asyncio.sleep(0.005)

                await ws.send(pack_tts_request(102, session_id))

                while True:
                    item = await pcm_queue.get()
                    if item is _PCM_STREAM_END:
                        break
                    yield item

                try:
                    await recv_task
                except asyncio.CancelledError:
                    raise
                except websockets.exceptions.ConnectionClosed:
                    pass
                except Exception:
                    logger.debug("TTS recv_task 收尾异常", exc_info=True)

            finally:
                if not recv_task.done():
                    recv_task.cancel()
                    try:
                        await asyncio.wait_for(recv_task, timeout=1.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass
                await mark_stream_end()

    except websockets.exceptions.ConnectionClosed:
        logger.warning("TTS WebSocket 意外断开")
    except Exception as e:
        logger.error(f"TTS 全局异常: {e}")
async def run_tts_to_ws(
    text: str,
    client_ws: WebSocket,
    ws_lock: asyncio.Lock,
    config: dict,
    latency_hooks: Optional[dict] = None,
):
    """点读单句 TTS：优先从连接池取预热连接，池未就绪时回退非池化。"""
    pool = get_tts_pool()
    use_pool = pool is not None

    async def _iter():
        if use_pool:
            async for chunk in iter_tts_pcm_chunks_pooled(text, config, latency_hooks):
                yield chunk
        else:
            async for chunk in iter_tts_pcm_chunks(text, config, latency_hooks):
                yield chunk

    try:
        async for payload in _iter():
            async with ws_lock:
                await client_ws.send_bytes(payload)
    except Exception as e:
        logger.warning(f"TTS 下发中断 (前端可能断开): {e}")


async def run_tts_turn_reused_from_queue(
    client_ws: WebSocket,
    ws_lock: asyncio.Lock,
    config: dict,
    segment_queue: asyncio.Queue,
    latency_hooks: Optional[dict] = None,
):
    """
    一轮对话 TTS：优先从连接池取预热连接，省去建连 + ConnectionStarted 开销。

    官方连接复用流程：
      ConnectionStarted → StartSession(100) → SessionStarted(150) → TaskRequest(200) → ...
      → SessionFinished(152) → (下一句) StartSession(100) → SessionStarted(150) → ...

    池中连接已处于 ConnectionStarted 状态，首句可直接发 StartSession，后续句同理。
    队列结束后发送 FinishSession(102)，连接回到池中供下一轮复用。
    """
    pool = get_tts_pool()
    use_pool = pool is not None
    voice_type = config.get("VOICE", "BV001_streaming")

    conn: Optional[_WarmTTSConnection] = None
    ws = None

    base_req = {
        "user": {"uid": "english_coach"},
        "namespace": "BidirectionalTTS",
        "req_params": {
            "speaker": voice_type,
            "audio_params": {
                "format": "pcm",
                "sample_rate": 24000,
                "enable_timestamp": True,
            },
            "additions": json.dumps({
                "disable_markdown_filter": False,
            }),
        },
    }

    def _make_session_id() -> str:
        return str(uuid.uuid4())

    async def _recv_frame_deadline(wss, deadline: float) -> bytes:
        while time.monotonic() < deadline:
            remain = deadline - time.monotonic()
            if remain <= 0:
                break
            try:
                return await asyncio.wait_for(
                    wss.recv(), timeout=max(0.05, min(2.0, remain))
                )
            except asyncio.TimeoutError:
                continue
        raise TimeoutError("火山引擎 TTS 收包超时")

    async def _send_and_wait_150(wss, session_id: str) -> None:
        """发 StartSession(100)，等 SessionStarted(150)；同时透传 352 音频帧"""
        await wss.send(pack_tts_request(100, session_id, copy.deepcopy(base_req)))
        deadline = time.monotonic() + 8.0
        saw_150 = False
        while time.monotonic() < deadline and not saw_150:
            raw = await _recv_frame_deadline(wss, deadline)
            parsed = _try_parse_tts_server_frame(raw)
            if not parsed:
                continue
            et, pb = parsed
            if et == 352:
                if latency_hooks is not None and not latency_hooks.get("_tts_first_pcm_logged"):
                    latency_hooks["_tts_first_pcm_logged"] = True
                    t0 = latency_hooks.get("t0")
                    tid = latency_hooks.get("turn_id", "?")
                    if isinstance(t0, (int, float)):
                        ms = (time.perf_counter() - float(t0)) * 1000.0
                        logger.info(
                            "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                            tid, "07_first_pcm_to_client", ms, len(pb),
                        )
                async with ws_lock:
                    await client_ws.send_bytes(pb)
                continue
            if et == 150:
                saw_150 = True
                break
            if et == 350:
                logger.debug("TTS 句子开始 (event 350)")
                continue
            if et == 351:
                logger.debug("TTS 句子结束 (event 351)")
                continue
            if et == 52:
                continue
        if not saw_150:
            raise TimeoutError("火山引擎 TTS 等待 SessionStarted(150) 超时")

    async def _send_and_wait_152(wss, session_id: str, chunk_text: str) -> None:
        """发 TaskRequest(逐字) + FinishSession(102)，等 SessionFinished(152)；同时透传 352"""
        for char in chunk_text:
            synthesis_req = copy.deepcopy(base_req)
            synthesis_req["event"] = 200
            synthesis_req["req_params"]["text"] = char
            await wss.send(pack_tts_request(200, session_id, synthesis_req))
            await asyncio.sleep(0.005)
        await wss.send(pack_tts_request(102, session_id))

        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            raw = await _recv_frame_deadline(wss, deadline)
            parsed = _try_parse_tts_server_frame(raw)
            if not parsed:
                continue
            et, pb = parsed
            if et == 352:
                if latency_hooks is not None and not latency_hooks.get("_tts_first_pcm_logged"):
                    latency_hooks["_tts_first_pcm_logged"] = True
                    t0 = latency_hooks.get("t0")
                    tid = latency_hooks.get("turn_id", "?")
                    if isinstance(t0, (int, float)):
                        ms = (time.perf_counter() - float(t0)) * 1000.0
                        logger.info(
                            "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                            tid, "07_first_pcm_to_client", ms, len(pb),
                        )
                async with ws_lock:
                    await client_ws.send_bytes(pb)
                continue
            if et == 350:
                logger.debug("TTS 句子开始 (event 350)")
                continue
            if et == 351:
                logger.debug("TTS 句子结束 (event 351)")
                continue
            if et == 152:
                break
            if et == 52:
                continue

    # ── 主循环 ──────────────────────────────────────────────────────────────
    try:
        while True:
            text_chunk = await segment_queue.get()
            if text_chunk is None:
                segment_queue.task_done()
                break

            session_id = _make_session_id()

            # 首次使用时：从池中取连接（或新建）
            if ws is None:
                if use_pool:
                    try:
                        conn = await pool.acquire()
                        ws = conn.ws
                        logger.debug("[TTS] 使用池化连接")
                    except Exception as e:
                        logger.warning(f"[TTS] 池化连接获取失败，回退新建: {e}")
                        use_pool = False
                        conn = None

                if not use_pool or ws is None:
                    # 非池化回退（首次建连，含 ConnectionStarted）
                    WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
                    volc_api_key = config.get("VOLC_API_KEY")
                    volc_resource_id = config.get("VOLC_RESOURCE_ID_TTS")
                    if not volc_api_key or not volc_resource_id:
                        logger.error("🚨 TTS 启动失败: 缺失配置信息")
                        return
                    headers = {
                        "Authorization": f"Bearer {volc_api_key}",
                        "x-api-key": volc_api_key,
                        "X-Api-Resource-Id": volc_resource_id,
                        "X-Api-Connect-Id": str(uuid.uuid4()),
                    }
                    ws = await websockets.connect(
                        WSS_URL, additional_headers=headers, open_timeout=10
                    )
                    await ws.send(pack_tts_request(1))
                    deadline = time.monotonic() + 8.0
                    saw_50 = False
                    while time.monotonic() < deadline and not saw_50:
                        raw = await _recv_frame_deadline(ws, deadline)
                        parsed = _try_parse_tts_server_frame(raw)
                        if not parsed:
                            continue
                        et, _pb = parsed
                        if et == 52:
                            continue
                        if et == 50:
                            saw_50 = True
                            break
                    if not saw_50:
                        raise TimeoutError("火山引擎 TTS 建连超时(等待事件 50)")

            # StartSession + 逐字发送
            await _send_and_wait_150(ws, session_id)
            await _send_and_wait_152(ws, session_id, text_chunk)
            segment_queue.task_done()

    except asyncio.CancelledError:
        raise
    except websockets.exceptions.ConnectionClosed:
        logger.warning("🌋 TTS WebSocket 意外断开")
    except Exception as e:
        logger.error(f"❌ TTS 全局异常: {e}")
    finally:
        if use_pool and conn is not None:
            await pool.release(conn)
        elif ws is not None:
            try:
                await ws.close()
            except Exception:
                pass