import asyncio
import copy
import gzip
import json
import logging
import os
import struct
import threading
import time
import uuid
import websockets
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

MAX_PAYLOAD_LEN = 10 * 1024 * 1024  # ASR: 单帧在链路上的最大长度 (压缩或未压缩)
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


# ================= 语音转文字 (ASR) =================

async def run_volcengine_wss_asr(pcm_bytes: bytearray, config: dict, max_retries=3):
    """
    一次性 ASR：传入完整 PCM 音频，返回 (recognized_text, detected_emotion)。

    优先使用 ASR 连接池获取预热连接，回退新建。
    """
    final_text, detected_emotion = "", "neutral"
    _trace_id = str(uuid.uuid4())[:8]
    logger.info(f"[ASROneShot][{_trace_id}] 开始处理 pcm_bytes={len(pcm_bytes)}")

    if len(pcm_bytes) < MIN_AUDIO_BYTES:
        logger.info(f"[ASROneShot][{_trace_id}] 音频太短，跳过 ASR len={len(pcm_bytes)} < MIN={MIN_AUDIO_BYTES}")
        return final_text, detected_emotion

    volc_api_key = config.get('VOLC_API_KEY')
    volc_resource_id = config.get('VOLC_RESOURCE_ID_ASR')
    asr_language = config.get("ASR_LANGUAGE", "en-US")

    if not volc_api_key or not volc_resource_id:
        logger.error("🚨 ASR 启动失败: 缺失 VOLC_API_KEY 或 VOLC_RESOURCE_ID_ASR")
        return final_text, detected_emotion

    pool = get_asr_pool()
    conn = None
    ws = None
    _used_pool = False

    for attempt in range(max_retries):
        logger.info(f"[ASROneShot][{_trace_id}] 尝试 {attempt + 1}/{max_retries}")
        try:
            # ========== Step 1: 获取连接 ==========
            if ws is None:
                if pool:
                    try:
                        conn = await pool.acquire()
                        ws = conn.ws
                        _used_pool = True
                        logger.info(f"[ASROneShot][{_trace_id}] 使用池化纯净连接 conn_id={conn.connect_id[:8]}")
                    except Exception as e:
                        logger.warning(f"[ASROneShot][{_trace_id}] 池化连接获取失败，回退新建: {e}")
                        pool = None

                if ws is None:
                    WSS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
                    headers = {
                        "Authorization": f"Bearer {volc_api_key}",
                        "x-api-key": volc_api_key,
                        "X-Api-Resource-Id": volc_resource_id,
                        "X-Api-Connect-Id": str(uuid.uuid4())
                    }
                    logger.info(f"[ASROneShot][{_trace_id}] 正在新建 ASR 连接...")
                    ws = await asyncio.wait_for(
                        websockets.connect(WSS_URL, additional_headers=headers, open_timeout=10),
                        timeout=12.0
                    )
                    logger.info(f"[ASROneShot][{_trace_id}] 新建 ASR 连接成功")

            # ========== Step 2: 发送业务初始化包 ==========
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
            logger.info(f"[ASROneShot][{_trace_id}] 发送初始化包 payload_len={len(payload)}")
            await ws.send(generate_asr_header(1, 0, 1, 1) + struct.pack('>I', len(payload)) + payload)

            # ========== Step 3: 音频收发 ==========
            result_ready = asyncio.Event()
            _chunks_sent = 0
            _chunks_total = (len(pcm_bytes) + ASR_PCM_CHUNK_BYTES - 1) // ASR_PCM_CHUNK_BYTES

            async def receive_responses():
                nonlocal final_text, detected_emotion
                _msg_count = 0
                async for message in ws:
                    _msg_count += 1
                    if isinstance(message, str) or len(message) < 4:
                        continue
                    serialization, compression = (message[2] >> 4) & 0x0f, message[2] & 0x0f

                    # 解析正常结果帧
                    if (message[1] >> 4) & 0x0f == 9:
                        payload_offset = ((message[0] & 0x0f) * 4) + (4 if (message[1] & 0x0f) in [1, 3] else 0) + 4
                        if payload_offset > len(message):
                            continue

                        payload_len = struct.unpack('>I', message[payload_offset - 4:payload_offset])[0]
                        if payload_len > MAX_PAYLOAD_LEN or payload_offset + payload_len > len(message):
                            logger.warning(f"[ASROneShot][{_trace_id}] 收到异常大包: payload_len={payload_len}")
                            continue

                        payload_bytes = message[payload_offset:payload_offset + payload_len]

                        if compression == 1:
                            try:
                                payload_bytes = gzip.decompress(payload_bytes)
                            except Exception as e:
                                logger.warning(f"ASR Gzip 解压失败: {e}")
                                continue

                        if len(payload_bytes) > MAX_DECOMPRESS_LEN:
                            logger.warning(f"[ASROneShot][{_trace_id}] 抛弃超大业务体 (超过 MAX_DECOMPRESS_LEN)")
                            continue

                        if serialization == 1:
                            try:
                                result = json.loads(payload_bytes.decode('utf-8')).get('result', {})
                                utterances = result.get('utterances', [])
                                logger.info(f"[ASROneShot][{_trace_id}] 收到响应 utterances={len(utterances)} msg_count={_msg_count}")
                                if utterances and utterances[-1].get('definite'):
                                    final_text = utterances[-1].get('text', '')
                                    detected_emotion = utterances[-1].get('additions', {}).get('emotion', 'neutral')
                                    logger.info(f"[ASROneShot][{_trace_id}] ✓ 识别成功 text={final_text[:50]}")
                                    result_ready.set()
                                    return
                            except Exception as e:
                                logger.warning(f"[ASROneShot][{_trace_id}] JSON 解析失败: {e}")
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
                        logger.error(f"[ASROneShot][{_trace_id}] ASR 服务端返回错误: {err_str}")
                        break

            async def send_audio():
                nonlocal _chunks_sent
                for i in range(0, len(pcm_bytes), ASR_PCM_CHUNK_BYTES):
                    chunk = pcm_bytes[i : i + ASR_PCM_CHUNK_BYTES]
                    chunk_payload = gzip.compress(chunk)
                    await ws.send(
                        generate_asr_header(2, 0, 0, 1)
                        + struct.pack(">I", len(chunk_payload))
                        + chunk_payload
                    )
                    _chunks_sent += 1
                    await asyncio.sleep(0)
                empty = gzip.compress(b'')
                logger.info(f"[ASROneShot][{_trace_id}] 发送完毕 _chunks_sent={_chunks_sent}/{_chunks_total}")
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
                            logger.warning(f"ASR 并发任务内部结束/异常: {exc}")
                    except asyncio.InvalidStateError:
                        pass

            if send_task in done and receive_task in pending:
                try:
                    await asyncio.wait_for(receive_task, timeout=ASR_RECEIVE_TIMEOUT)
                    if result_ready.is_set():
                        # 成功获取结果
                        for task in pending:
                            task.cancel()
                            try:
                                await task
                            except asyncio.CancelledError:
                                pass
                        break
                except asyncio.TimeoutError:
                    logger.warning("⚠️ ASR 接收终稿超时。")

            for task in pending:
                if not task.done():
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass

            if final_text:
                break

            # 无结果，连接可能已失效，下次循环新建
            if pool and conn:
                await pool.release(conn, was_successful=False)
                conn = None
                ws = None
            elif ws:
                try:
                    await ws.close()
                except Exception:
                    pass
                ws = None

        except Exception as e:
            logger.warning(f"⚠️ ASR 协议异常 (尝试 {attempt + 1}/{max_retries}): {e}")
            if pool and conn:
                await pool.release(conn, was_successful=False)
                conn = None
                ws = None
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
            else:
                logger.error("❌ ASR 多次尝试后彻底失败")

    # ========== Step 4: 释放连接 ==========
    if pool and conn:
        await pool.release(conn, was_successful=True)
    elif ws:
        try:
            await ws.close()
        except Exception:
            pass

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

    # DEBUG: 记录收到的消息类型
    if len(message) >= 4:
        msg_type_low = message[1] & 0x0F
        logger.debug(f"ASR recv: msg_type={msg_type_high}.{msg_type_low} len={len(message)} serial={serialization} comp={compression}")

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
                if asyncio.iscoroutine(pr):
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


# ================= ASR 连接预热池 (弹匣模式) =================
"""
火山引擎 ASR (sauc/bigmodel_async) 协议特点：
- 每个 WebSocket 只能完成一次 ASR 会话
- 发送结束空包后，连接即失效，无法复用

因此 ASRPool 采用 "弹匣消耗与补充模式"：
1. 预热：仅建立 TCP/TLS 连接，不发送业务初始化包
2. 取用：pop 纯净连接交给 worker 使用
3. 释放：worker 用完后关闭连接，触发后台异步补充新连接
"""

_ASR_POOL_SIZE = int(os.getenv("ASR_WARM_POOL_SIZE", "3"))
_ASR_WARM_TIMEOUT_SEC = float(os.getenv("ASR_WARM_TIMEOUT_SEC", "30"))
_ASR_CONNECT_TIMEOUT = 12.0


class _WarmASRConnection:
    """单条预热好的 ASR 底层连接。仅持有已建立的 WebSocket，状态为"等待业务初始化" """
    __slots__ = ("ws", "connect_id", "created_at")

    def __init__(self, ws, connect_id: str):
        self.ws = ws
        self.connect_id = connect_id
        self.created_at = time.monotonic()


class ASRPool:
    """
    ASR 连接池 - 弹匣消耗与补充模式

    核心区别于 TTSPool：
    - ASR 每个连接只能用一次
    - 用完后不能放回队列，必须丢弃 + 异步补充
    - 预热时只建立 TCP 连接，不发业务包
    """

    def __init__(self, config: dict):
        self._config = config
        self._idle: asyncio.Queue = asyncio.Queue()
        self._in_use: int = 0
        self._total_created: int = 0
        self._total_consumed: int = 0
        self._closed: bool = False
        self._lock: asyncio.Lock = asyncio.Lock()
        self._WSS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"

    def _build_headers(self) -> dict:
        api_key = self._config.get("VOLC_API_KEY")
        resource_id = self._config.get("VOLC_RESOURCE_ID_ASR")
        if not api_key or not resource_id:
            raise ValueError("ASR 配置缺失: VOLC_API_KEY 或 VOLC_RESOURCE_ID_ASR")
        return {
            "Authorization": f"Bearer {api_key}",
            "x-api-key": api_key,
            "X-Api-Resource-Id": resource_id,
            "X-Api-Connect-Id": str(uuid.uuid4()),
        }

    async def warm_up(self) -> None:
        """服务启动时预热连接（仅建立 TCP，不发业务包）"""
        if self._closed:
            return
        logger.info(f"[ASRPool] 开始预热，预设 {_ASR_POOL_SIZE} 条连接...")

        tasks = [self._build_one_connection() for _ in range(_ASR_POOL_SIZE)]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        success_count = 0
        for r in results:
            if isinstance(r, _WarmASRConnection):
                await self._idle.put(r)
                success_count += 1
        logger.info(f"[ASRPool] warm_up 完成，成功 {success_count}/{_ASR_POOL_SIZE} 条")

    async def _build_one_connection(self) -> Optional[_WarmASRConnection]:
        """创建一条预热连接：仅完成 TCP/TLS 建连，返回纯净连接"""
        connect_id = str(uuid.uuid4())
        headers = self._build_headers()
        headers["X-Api-Connect-Id"] = connect_id

        try:
            ws = await asyncio.wait_for(
                websockets.connect(
                    self._WSS_URL,
                    additional_headers=headers,
                    open_timeout=_ASR_CONNECT_TIMEOUT,
                ),
                timeout=_ASR_CONNECT_TIMEOUT + 1.0,
            )

            conn = _WarmASRConnection(ws, connect_id)
            async with self._lock:
                self._total_created += 1

            logger.debug(f"[ASRPool] 纯净连接就绪: {connect_id[:8]}")
            return conn

        except asyncio.TimeoutError:
            logger.warning(f"[ASRPool] 建连超时: {connect_id[:8]}")
            return None
        except Exception as e:
            logger.warning(f"[ASRPool] 建连异常: {e}")
            return None

    async def _replenish_one(self) -> None:
        """后台异步补充一条新连接（补充弹匣）"""
        if self._closed:
            return
        try:
            conn = await self._build_one_connection()
            if conn is not None:
                await self._idle.put(conn)
                logger.debug(f"[ASRPool] 补充连接成功: {conn.connect_id[:8]}")
            else:
                logger.warning("[ASRPool] 补充连接失败，将在下次取用时重试")
        except Exception as e:
            logger.warning(f"[ASRPool] 补充连接异常: {e}")

    async def acquire(self) -> _WarmASRConnection:
        """获取一条就绪连接"""
        if self._closed:
            raise RuntimeError("ASRPool 已关闭")

        _wait_start = time.monotonic()
        logger.info(f"[ASRPool] acquire() start _idle={self._idle.qsize()} _in_use={self._in_use} _total_created={self._total_created} _total_consumed={self._total_consumed}")

        while True:
            try:
                conn = self._idle.get_nowait()
                age = time.monotonic() - conn.created_at

                if age > _ASR_WARM_TIMEOUT_SEC:
                    logger.warning(f"[ASRPool] 连接过期({age:.1f}s)，丢弃并补充: {conn.connect_id[:8]}")
                    await self._close_one(conn)
                    asyncio.create_task(self._replenish_one())
                    continue

                async with self._lock:
                    self._in_use += 1
                logger.info(f"[ASRPool] acquire 成功 conn_id={conn.connect_id[:8]} in_use={self._in_use}")
                return conn

            except asyncio.QueueEmpty:
                pass

            async with self._lock:
                if self._total_created - self._total_consumed < _ASR_POOL_SIZE:
                    needed = min(1, _ASR_POOL_SIZE - (self._total_created - self._total_consumed))
                else:
                    needed = 0

            if needed > 0:
                logger.info(f"[ASRPool] 池为空，正在创建新连接 (created={self._total_created} consumed={self._total_consumed})")
                conn = await self._build_one_connection()
                if conn is not None:
                    async with self._lock:
                        self._in_use += 1
                    logger.info(f"[ASRPool] 新建连接成功 conn_id={conn.connect_id[:8]} in_use={self._in_use}")
                    return conn
                else:
                    logger.warning("[ASRPool] 新建连接失败，将重试")

            # 超时保护：等待超过 10 秒则抛出异常
            elapsed = time.monotonic() - _wait_start
            if elapsed > 10.0:
                raise TimeoutError(f"[ASRPool] 获取连接超时 ({elapsed:.1f}s)")

            await asyncio.sleep(0.1)

    async def release(self, conn: _WarmASRConnection, was_successful: bool = True) -> None:
        """释放连接（弹匣消耗模式）"""
        async with self._lock:
            self._in_use -= 1
            self._total_consumed += 1
        logger.info(f"[ASRPool] release conn_id={conn.connect_id[:8]} was_successful={was_successful} in_use={self._in_use}")

        if self._closed:
            await self._close_one(conn)
            return

        await self._close_one(conn)

        if was_successful:
            logger.info(f"[ASRPool] 连接消耗完成: {conn.connect_id[:8]}")
        else:
            logger.warning(f"[ASRPool] 连接异常中断: {conn.connect_id[:8]}")

        asyncio.create_task(self._replenish_one())

    async def _close_one(self, conn: _WarmASRConnection) -> None:
        """关闭单个连接"""
        try:
            await conn.ws.close(code=1000, reason="ASR session ended")
        except Exception:
            pass

    async def close(self) -> None:
        """关闭池中所有连接"""
        async with self._lock:
            self._closed = True

        # 关闭所有空闲连接
        idle_count = 0
        while True:
            try:
                conn = self._idle.get_nowait()
                await self._close_one(conn)
                idle_count += 1
            except asyncio.QueueEmpty:
                break

        logger.info(
            f"[ASRPool] 已关闭，消耗连接 {self._total_consumed}/{self._total_created} 条，"
            f"剩余空闲 {idle_count} 条，in_use={self._in_use}"
        )


# 模块级连接池单例
_asr_pool: Optional[ASRPool] = None


def get_asr_pool() -> Optional[ASRPool]:
    return _asr_pool


def init_asr_pool(config: dict) -> ASRPool:
    global _asr_pool
    if _asr_pool is None:
        _asr_pool = ASRPool(config)
    return _asr_pool


async def warm_asr_pool() -> None:
    """供 server.py startup 调用"""
    pool = get_asr_pool()
    if pool:
        await pool.warm_up()


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

    优先使用 ASR 连接池获取预热连接，回退新建。
    """
    pool = get_asr_pool()
    conn = None
    ws = None
    _trace_id = str(uuid.uuid4())[:8]
    _used_pool = False
    _idle_size = pool._idle.qsize() if pool else -1
    logger.info(f"[ASRStream][{_trace_id}] 流式 ASR worker 启动 pool={'存在' if pool else 'None'} idle={_idle_size}")

    try:
        # ========== Step 1: 从池中获取纯净连接 ==========
        if pool:
            try:
                logger.info(f"[ASRStream][{_trace_id}] 正在从池获取连接...")
                conn = await pool.acquire()
                ws = conn.ws
                _used_pool = True
                logger.info(f"[ASRStream][{_trace_id}] 使用池化纯净连接 conn_id={conn.connect_id[:8]}")
            except Exception as e:
                logger.warning(f"[ASRStream][{_trace_id}] 池化连接获取失败，回退新建: {e}")
                pool = None

        # ========== Step 2: 如需新建连接 ==========
        if ws is None:
            WSS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
            volc_api_key = config.get("VOLC_API_KEY")
            volc_resource_id = config.get("VOLC_RESOURCE_ID_ASR")
            if not volc_api_key or not volc_resource_id:
                logger.error(f"🚨 [ASRStream][{_trace_id}] ASR 流式：缺失 VOLC_API_KEY 或 VOLC_RESOURCE_ID_ASR")
                return "", "neutral"
            headers = {
                "Authorization": f"Bearer {volc_api_key}",
                "x-api-key": volc_api_key,
                "X-Api-Resource-Id": volc_resource_id,
                "X-Api-Connect-Id": str(uuid.uuid4()),
            }
            logger.info(f"[ASRStream][{_trace_id}] 正在新建 ASR 连接...")
            ws = await asyncio.wait_for(
                websockets.connect(WSS_URL, additional_headers=headers, open_timeout=10),
                timeout=12.0,
            )
            logger.info(f"[ASRStream][{_trace_id}] 新建 ASR 连接成功")

        # ========== Step 3: 发送业务初始化包（连接已就绪） ==========
        asr_language = config.get("ASR_LANGUAGE", "en-US")
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
        logger.info(f"[ASRStream][{_trace_id}] 发送初始化包 payload_len={len(init_payload)}")
        await ws.send(
            generate_asr_header(1, 0, 1, 1) + struct.pack(">I", len(init_payload)) + init_payload
        )

        # ========== Step 4: 音频收发循环 ==========
        holder: dict = {"text": "", "emotion": "neutral", "_partial_sig": ""}
        recv_done = asyncio.Event()
        _chunks_received = 0
        _chunks_from_queue = 0

        async def recv_loop():
            nonlocal _chunks_received
            _msg_count = 0
            try:
                async for message in ws:
                    _msg_count += 1
                    if await _asr_handle_server_message(message, holder, on_partial):
                        logger.info(f"[ASRStream][{_trace_id}] ✓ 识别成功 text={holder.get('text', '')[:50]} msg_count={_msg_count}")
                        recv_done.set()
                        return
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"[ASRStream][{_trace_id}] recv_loop 异常: {e}")

        recv_task = asyncio.create_task(recv_loop())

        async def send_pcm_chunk(raw: bytes):
            nonlocal _chunks_from_queue
            for i in range(0, len(raw), ASR_PCM_CHUNK_BYTES):
                if recv_done.is_set():
                    return
                sub = raw[i : i + ASR_PCM_CHUNK_BYTES]
                pl = gzip.compress(sub)
                await ws.send(
                    generate_asr_header(2, 0, 0, 1) + struct.pack(">I", len(pl)) + pl
                )
                _chunks_from_queue += 1
                await asyncio.sleep(0)

        try:
            while not recv_done.is_set():
                item = await pcm_queue.get()
                if item is None:
                    logger.info(f"[ASRStream][{_trace_id}] 收到队列结束信号 _chunks_from_queue={_chunks_from_queue}")
                    if not recv_done.is_set():
                        empty = gzip.compress(b"")
                        await ws.send(
                            generate_asr_header(2, 2, 0, 1) + struct.pack(">I", len(empty)) + empty
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
                    await asyncio.wait_for(recv_task, timeout=ASR_RECEIVE_TIMEOUT)
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

        result_text = holder.get("text") or ""
        result_emotion = holder.get("emotion") or "neutral"
        logger.info(f"[ASRStream][{_trace_id}] 完成 result_text={repr(result_text[:30])} used_pool={_used_pool}")

        # ========== Step 5: 释放连接 ==========
        if pool and conn:
            await pool.release(conn, was_successful=True)
        elif ws:
            try:
                await ws.close()
            except Exception:
                pass

        return result_text, result_emotion

    except asyncio.CancelledError:
        logger.warning(f"[ASRStream][{_trace_id}] 被取消")
        raise
    except Exception as e:
        logger.warning(f"⚠️ [ASRStream][{_trace_id}] ASR 流式协议异常: {e}")
        if pool and conn:
            await pool.release(conn, was_successful=False)
        elif ws:
            try:
                await ws.close()
            except Exception:
                pass
        return "", "neutral"


# ================= 2. 文本转流式语音 (TTS) =================

_TTS_POOL_SIZE = int(os.getenv("TTS_WARM_POOL_SIZE", "2"))
_TTS_WARM_TIMEOUT_SEC = float(os.getenv("TTS_WARM_TIMEOUT_SEC", "300"))

# TTS 协议常量
MAX_ID_LEN = 1024
MAX_TTS_PAYLOAD_LEN = 5 * 1024 * 1024
MAX_TTS_CONTROL_PAYLOAD_LEN = 512 * 1024


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

    async def _build_one_connection(self) -> Optional[_WarmTTSConnection]:
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
        """获取一条就绪连接（已在 ConnectionStarted 状态）。若池为空则新建一条（同步等待）。"""
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
_tts_pool: Optional[TTSPool] = None


def get_tts_pool() -> Optional[TTSPool]:
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


# ================= TTS 核心异步迭代器 =================

_PCM_STREAM_END = object()


def _try_parse_tts_server_frame(message: bytes) -> Optional[tuple]:
    """解析双向 TTS 下行帧，返回 (event_type, payload) 或 None（跳过/损坏）。"""
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
            err_str = f"RawHex: {err_raw.hex()}"
        logger.error(f"TTS 服务端返回错误(msg_type=15): {err_str}")
        return None

    if msg_type not in [9, 11]:
        logger.warning(f"[VolcEngineTTS] 收到未知 msg_type={msg_type} flags={flags} len={len(message)} raw_hex={message.hex()[:80]}")
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

    pl_limit = MAX_TTS_PAYLOAD_LEN if event_type == 352 else MAX_TTS_CONTROL_PAYLOAD_LEN
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
):
    """单句、单 WebSocket：按包 yield PCM（event 352）。"""
    import copy as copy_module
    WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"

    volc_api_key = config.get("VOLC_API_KEY")
    volc_resource_id = config.get("VOLC_RESOURCE_ID_TTS")
    voice_type = config.get("VOLC_VOICE", "BV001_streaming")

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
                        continue
                    offset += 4 + id_len
                else:
                    event_type = None

                if offset + 4 > len(message):
                    continue
                payload_size = struct.unpack(">I", message[offset : offset + 4])[0]
                offset += 4

                pl_limit = MAX_TTS_PAYLOAD_LEN if event_type == 352 else MAX_TTS_CONTROL_PAYLOAD_LEN
                if payload_size > pl_limit or offset + payload_size > len(message):
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

                elif event_type == 352:
                    if latency_hooks is not None and not latency_hooks.get("_tts_first_pcm_logged"):
                        latency_hooks["_tts_first_pcm_logged"] = True
                        t0 = latency_hooks.get("t0")
                        tid = latency_hooks.get("turn_id", "?")
                        if isinstance(t0, (int, float)):
                            ms = (time.perf_counter() - float(t0)) * 1000.0
                            logger.info(
                                "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                                tid, "07_first_pcm_to_client", ms, len(payload),
                            )
                    await pcm_queue.put(payload)

                elif event_type == 152:
                    break

                elif event_type == 52:
                    break
        finally:
            await mark_stream_end()

    try:
        async with websockets.connect(WSS_URL, additional_headers=headers, open_timeout=10) as ws:
            await ws.send(pack_tts_request(1))
            session_id = str(uuid.uuid4())

            recv_task = asyncio.create_task(receiver())

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

            for char in text:
                synthesis_req = copy_module.deepcopy(base_req)
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
    """一轮对话 TTS：优先从连接池取预热连接，省去建连 + ConnectionStarted 开销。"""
    import copy as copy_module
    pool = get_tts_pool()
    use_pool = pool is not None
    voice_type = config.get("VOLC_VOICE", "BV001_streaming")
    resource_id = config.get("VOLC_RESOURCE_ID_TTS", "")

    conn: Optional[_WarmTTSConnection] = None
    ws = None

    logger.info(f"[VolcEngineTTS] run_tts_turn 开始 | voice={voice_type} resource={resource_id} use_pool={use_pool}")

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
        req_dump = json.dumps(copy_module.deepcopy(base_req), ensure_ascii=False)
        logger.info(f"[VolcEngineTTS] 发送 StartSession(100) session={session_id} req={req_dump[:200]}")
        await wss.send(pack_tts_request(100, session_id, copy_module.deepcopy(base_req)))
        deadline = time.monotonic() + 8.0
        saw_150 = False
        while time.monotonic() < deadline and not saw_150:
            raw = await _recv_frame_deadline(wss, deadline)
            parsed = _try_parse_tts_server_frame(raw)
            if not parsed:
                # 记录原始错误帧
                if isinstance(raw, bytes) and len(raw) > 4:
                    header_size = (raw[0] & 0x0F) * 4
                    msg_type = (raw[1] >> 4) & 0x0F
                    if msg_type == 15:
                        err_raw = raw[header_size:]
                        logger.error(f"[VolcEngineTTS] 收到错误响应帧 msg_type=15 raw_hex={raw.hex()[:80]}")
                continue
            et, pb = parsed
            logger.debug(f"[VolcEngineTTS] 收到事件 et={et} len={len(pb)}")
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
                continue
            if et == 351:
                continue
            if et == 52:
                continue
        if not saw_150:
            raise TimeoutError("火山引擎 TTS 等待 SessionStarted(150) 超时")

    async def _send_and_wait_152(wss, session_id: str, chunk_text: str) -> None:
        """发 TaskRequest(逐字) + FinishSession(102)，等 SessionFinished(152)；同时透传 352"""
        logger.info(f"[VolcEngineTTS] 发送 TaskRequest(200)+FinishSession(102) session={session_id} text={chunk_text[:50]}")
        for char in chunk_text:
            synthesis_req = copy_module.deepcopy(base_req)
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
                # 记录未能解析的帧，可能是错误或需特殊处理
                if isinstance(raw, bytes) and len(raw) >= 4:
                    logger.debug(f"[VolcEngineTTS] 跳过未能解析的帧 len={len(raw)} hex={raw.hex()[:60]}")
                continue
            et, pb = parsed
            logger.debug(f"[VolcEngineTTS] _send_and_wait_152 收到事件 et={et} len={len(pb)}")
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
                continue
            if et == 351:
                continue
            if et == 152:
                break
            if et == 52:
                continue

    # 主循环
    try:
        while True:
            text_chunk = await segment_queue.get()
            if text_chunk is None:
                segment_queue.task_done()
                break

            session_id = _make_session_id()

            if ws is None:
                if use_pool:
                    try:
                        conn = await pool.acquire()
                        ws = conn.ws
                        logger.info("[VolcEngineTTS] 使用池化连接")
                    except Exception as e:
                        logger.warning(f"[VolcEngineTTS] 池化连接获取失败，回退新建: {e}")
                        use_pool = False
                        conn = None

                if not use_pool or ws is None:
                    WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
                    volc_api_key = config.get("VOLC_API_KEY")
                    volc_resource_id = config.get("VOLC_RESOURCE_ID_TTS")
                    if not volc_api_key or not volc_resource_id:
                        logger.error("TTS 启动失败: 缺失配置信息")
                        return
                    headers = {
                        "Authorization": f"Bearer {volc_api_key}",
                        "x-api-key": volc_api_key,
                        "X-Api-Resource-Id": volc_resource_id,
                        "X-Api-Connect-Id": str(uuid.uuid4()),
                    }
                    logger.info(f"[VolcEngineTTS] 建立新连接 url={WSS_URL} resource_id={volc_resource_id} api_key前缀={volc_api_key[:10]}...")
                    ws = await websockets.connect(
                        WSS_URL, additional_headers=headers, open_timeout=10
                    )
                    logger.info(f"[VolcEngineTTS] WebSocket 连接已建立，等待 ServerReady(50)")
                    await ws.send(pack_tts_request(1))
                    deadline = time.monotonic() + 8.0
                    saw_50 = False
                    while time.monotonic() < deadline and not saw_50:
                        raw = await _recv_frame_deadline(ws, deadline)
                        parsed = _try_parse_tts_server_frame(raw)
                        if not parsed:
                            # 记录原始错误帧
                            if isinstance(raw, bytes) and len(raw) > 4:
                                header_size = (raw[0] & 0x0F) * 4
                                msg_type = (raw[1] >> 4) & 0x0F
                                if msg_type == 15:
                                    err_raw = raw[header_size:]
                                    logger.error(f"[VolcEngineTTS] 建连阶段收到错误响应 msg_type=15 raw_hex={raw.hex()[:80]}")
                            continue
                        et, _pb = parsed
                        if et == 52:
                            continue
                        if et == 50:
                            saw_50 = True
                            logger.info("[VolcEngineTTS] 收到 ServerReady(50)，连接就绪")
                            break
                    if not saw_50:
                        raise TimeoutError("火山引擎 TTS 建连超时(等待事件 50)")

            logger.info(f"[VolcEngineTTS] 开始处理文本块 session={session_id} text={text_chunk[:30]}...")
            await _send_and_wait_150(ws, session_id)
            logger.info(f"[VolcEngineTTS] TaskRequest 完成 session={session_id}")
            await _send_and_wait_152(ws, session_id, text_chunk)
            logger.info(f"[VolcEngineTTS] 文本块处理完成 session={session_id}")
            segment_queue.task_done()

    except asyncio.CancelledError:
        raise
    except websockets.exceptions.ConnectionClosed:
        logger.warning("TTS WebSocket 意外断开")
    except Exception as e:
        logger.error(f"TTS 全局异常: {e}")
    finally:
        if use_pool and conn is not None:
            await pool.release(conn)
        elif ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
