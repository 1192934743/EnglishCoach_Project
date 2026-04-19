"""
TTS 模块 —— 文本转流式语音。

支持两种模式：
1. 非池化模式：每句新建 WebSocket 连接
2. 池化模式：从预热连接池获取连接，省去建连 + 握手开销

连接池由 init_tts_pool / warm_tts_pool 管理。

火山双向 TTS 官方协议参考：
  https://www.volcengine.com/docs/6561/1329505
"""
from __future__ import annotations

import copy
import json
import asyncio
import logging
import time
import uuid
import websockets
from collections.abc import AsyncIterator
from typing import Optional

from fastapi import WebSocket

from core.audio.protocol import pack_tts_request, parse_tts_server_frame

logger = logging.getLogger("EnglishCoach")

# ================= TTS 连接预热池 =================

_TTS_POOL_SIZE = int(__import__('os').getenv("TTS_WARM_POOL_SIZE", "2"))
_TTS_WARM_TIMEOUT_SEC = float(__import__('os').getenv("TTS_WARM_TIMEOUT_SEC", "300"))


# ── 火山官方 demo 中的鉴权头字段（V3 API）───────────────────────────────
def _build_tts_headers(api_key: str, resource_id: str) -> dict:
    """按火山 V3 API 格式构建 WebSocket 鉴权请求头"""
    return {
        "Authorization": f"Bearer {api_key}",
        "x-api-key": api_key,
        "X-Api-Resource-Id": resource_id,
        "X-Api-Connect-Id": str(uuid.uuid4()),
    }


def _get_tts_resource_id(voice_type: str) -> str:
    """官方 demo 中的资源 ID 映射逻辑"""
    if voice_type and voice_type.startswith("S_"):
        return "volc.megatts.default"
    return "volc.service_type.10029"


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
    TTS 连接池：预热 N 条连接，调用时直接取用，
    省去建连 + 握手 + SessionStarted 的 ~200-500ms。

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

    async def _build_one_connection(self) -> Optional[_WarmTTSConnection]:
        """创建一条预热连接：完成 StartConnection + 等待 ConnectionStarted"""
        WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
        api_key = self._config.get("VOLC_API_KEY")
        voice_type = self._config.get("VOICE", "BV001_streaming")
        # 优先用 config 中配置的 TTS resource_id，否则按官方 demo 逻辑映射
        resource_id = self._config.get("VOLC_RESOURCE_ID_TTS") or _get_tts_resource_id(voice_type)
        if not api_key:
            logger.warning("[TTSPool] 缺少 TTS 配置，跳过建连")
            return None

        headers = _build_tts_headers(api_key, resource_id)
        connect_id = headers["X-Api-Connect-Id"]

        try:
            ws = await asyncio.wait_for(
                websockets.connect(WSS_URL, additional_headers=headers, max_size=10 * 1024 * 1024, open_timeout=10),
                timeout=12.0,
            )
            # StartConnection event_type=1（无 payload）
            await ws.send(pack_tts_request(1))
            deadline = time.monotonic() + 10.0
            while time.monotonic() < deadline:
                try:
                    raw = await asyncio.wait_for(
                        ws.recv(), timeout=min(2.0, deadline - time.monotonic())
                    )
                except asyncio.TimeoutError:
                    continue
                parsed = parse_tts_server_frame(raw)
                if not parsed:
                    continue
                event_type, _ = parsed
                # 火山服务在建连就绪后会先推 EventType=52（服务端推送配置帧，可忽略）
                if event_type == 52:
                    continue
                # ConnectionStarted = 50
                if event_type == 50:
                    conn = _WarmTTSConnection(ws, connect_id)
                    async with self._lock:
                        self._total_created += 1
                    await self._idle.put(conn)
                    logger.debug(f"[TTSPool] 连接就绪: {connect_id[:8]}")
                    return conn
                # ConnectionRejected = 51
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
        """获取一条就绪连接（已在 ConnectionStarted 状态）。若池为空则新建。"""
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


# ================= 模块级连接池单例 =================

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


# ================= 底层 TTS 迭代器（内部使用） =================

_PCM_STREAM_END = object()


def _base_req_template(voice_type: str, sample_rate: int = 24000) -> dict:
    """
    火山引擎 TTS 请求模板。
    注意：服务端 Go struct 期望 additions 是 string 类型，不是一个对象。
    根据官方文档，startSession 请求只需要必要的参数。
    """
    return {
        "user": {"uid": "english_coach"},
        "namespace": "BidirectionalTTS",
        "req_params": {
            "speaker": voice_type,
            "audio_params": {
                "format": "pcm",
                "sample_rate": sample_rate,
                "enable_timestamp": True,
            },
            # 移除 additions 字段，服务端期望它是 string 而非 object
        },
    }


def _log_first_pcm(latency_hooks: Optional[dict], pcm_bytes: bytes) -> None:
    """记录首包 PCM 的延迟钩子（统一在 iter_tts_pcm_chunks 和 iter_tts_pcm_chunks_pooled 中使用）"""
    if latency_hooks is not None and not latency_hooks.get("_tts_first_pcm_logged"):
        latency_hooks["_tts_first_pcm_logged"] = True
        t0 = latency_hooks.get("t0")
        tid = latency_hooks.get("turn_id", "?")
        if isinstance(t0, (int, float)):
            ms = (time.perf_counter() - float(t0)) * 1000.0
            logger.info(
                "[LATENCY] turn=%s stage=%-32s cum=%8.1fms pcm_bytes=%s",
                tid, "07_first_pcm_to_client", ms, len(pcm_bytes),
            )


async def _recv_deadline(ws, deadline: float) -> bytes:
    while time.monotonic() < deadline:
        remain = deadline - time.monotonic()
        if remain <= 0:
            break
        try:
            return await asyncio.wait_for(
                ws.recv(), timeout=max(0.05, min(2.0, remain))
            )
        except asyncio.TimeoutError:
            continue
    raise TimeoutError("火山引擎 TTS 收包超时")


# ── 火山引擎 event 类型常量（必须是 int32，不是字符串）────────
# 服务端 Go struct 期望 TTSRequest.event 是 int32 类型
_EVENT_SESSION_STARTED = 150     # server -> client
_EVENT_SESSION_FINISHED = 152     # server -> client
_EVENT_CONNECTION_STARTED = 50   # server -> client
_EVENT_TASK_REQUEST = 200         # client -> server
_EVENT_START_SESSION = 100       # client -> server
_EVENT_FINISH_SESSION = 102      # client -> server


async def _send_and_wait_150(
    ws, session_id: str, base_req: dict,
    ws_lock: asyncio.Lock, client_ws: WebSocket,
    latency_hooks: Optional[dict] = None,
) -> None:
    """
    发送 StartSession，等 SessionStarted；边收边透传 352 音频帧。
    按官方 demo：JSON payload["event"] = "StartSession"（字符串）。
    """
    req = copy.deepcopy(base_req)
    req["event"] = _EVENT_START_SESSION  # int32 类型
    await ws.send(pack_tts_request(100, session_id, req))
    deadline = time.monotonic() + 8.0
    saw_150 = False
    while time.monotonic() < deadline and not saw_150:
        raw = await _recv_deadline(ws, deadline)
        parsed = parse_tts_server_frame(raw)
        if not parsed:
            continue
        et, pb = parsed
        if et == 352:
            _log_first_pcm(latency_hooks, pb)
            if client_ws is not None and ws_lock is not None:
                async with ws_lock:
                    await client_ws.send_bytes(pb)
            continue
        if et == 150:
            saw_150 = True
            break
        if et in (350, 351, 52):
            continue
    if not saw_150:
        raise TimeoutError("火山引擎 TTS 等待 SessionStarted(150) 超时")


async def _send_and_wait_152(
    ws, session_id: str, chunk_text: str, base_req: dict,
    ws_lock: asyncio.Lock, client_ws: WebSocket,
    latency_hooks: Optional[dict] = None,
) -> None:
    """
    按官方 demo 逐字发送 + 背景收音频（send_task 异步发，recv 循环边收边透传）。
    TaskRequest 需要包含 event 字段（int32=200）。
    """
    # 背景任务：逐字发送
    # TaskRequest 需要 event=200 和 text 字段
    async def send_chars():
        for char in chunk_text:
            task_req = {"event": _EVENT_TASK_REQUEST, "req_params": {"text": char}}
            await ws.send(pack_tts_request(200, session_id, task_req))
            await asyncio.sleep(0.005)
        await ws.send(pack_tts_request(102, session_id))

    send_task = asyncio.create_task(send_chars())

    deadline = time.monotonic() + 120.0
    while time.monotonic() < deadline:
        raw = await _recv_deadline(ws, deadline)
        parsed = parse_tts_server_frame(raw)
        if not parsed:
            continue
        et, pb = parsed
        if et == 352:
            _log_first_pcm(latency_hooks, pb)
            if client_ws is not None and ws_lock is not None:
                async with ws_lock:
                    await client_ws.send_bytes(pb)
            continue
        if et in (350, 351, 52):
            continue
        if et == 152:
            break

    await send_task


# ================= 对外接口：池化 TTS（连接已就绪，yield PCM） =================

async def iter_tts_pcm_chunks_pooled(
    text: str,
    config: dict,
    latency_hooks: Optional[dict] = None,
) -> AsyncIterator[bytes]:
    """
    从连接池取出一条预热好的 TTS 连接，省去 StartConnection + ConnectionStarted 开销。
    内部完成 StartSession → TaskRequest(逐字) → FinishSession → 释放回池。
    通过 yield 方式返回 PCM 数据（供调用方自行处理）。
    """
    pool = get_tts_pool()
    if pool is None:
        async for chunk in iter_tts_pcm_chunks(text, config, latency_hooks):
            yield chunk
        return

    voice_type = config.get("VOICE", "BV001_streaming")
    sample_rate = int(config.get("TTS_SAMPLE_RATE", 24000))
    session_id = str(uuid.uuid4())
    base_req = _base_req_template(voice_type, sample_rate)

    conn = None
    try:
        conn = await pool.acquire()
        ws = conn.ws

        # 发 StartSession，等 SessionStarted
        req_init = copy.deepcopy(base_req)
        req_init["event"] = _EVENT_START_SESSION
        await ws.send(pack_tts_request(100, session_id, req_init))
        deadline = time.monotonic() + 8.0
        saw_150 = False
        while time.monotonic() < deadline and not saw_150:
            raw = await _recv_deadline(ws, deadline)
            parsed = parse_tts_server_frame(raw)
            if not parsed:
                continue
            et, pb = parsed
            if et == 352:
                _log_first_pcm(latency_hooks, pb)
                yield pb
                continue
            if et == 150:
                saw_150 = True
                break
            if et in (350, 351, 52):
                continue
        if not saw_150:
            logger.warning("[TTS] 池化连接等待 SessionStarted 超时")
            await pool.release(conn)
            return

        # 官方 demo 模式：背景发字符，主循环收音频
        # TaskRequest 只发送 text 字段，不需要完整的 base_req
        async def send_chars():
            for char in text:
                task_req = {
                    "req_params": {"text": char}
                }
                await ws.send(pack_tts_request(200, session_id, task_req))
                await asyncio.sleep(0.005)
            await ws.send(pack_tts_request(102, session_id))

        send_task = asyncio.create_task(send_chars())

        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            raw = await _recv_deadline(ws, deadline)
            parsed = parse_tts_server_frame(raw)
            if not parsed:
                continue
            et, pb = parsed
            if et == 352:
                _log_first_pcm(latency_hooks, pb)
                yield pb
                continue
            if et in (350, 351, 52):
                continue
            if et == 152:
                break

        await send_task
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


# ================= 对外接口：非池化 TTS（每句新建连接） =================

async def iter_tts_pcm_chunks(
    text: str,
    config: dict,
    latency_hooks: Optional[dict] = None,
) -> AsyncIterator[bytes]:
    """
    单句、单 WebSocket：按包 yield PCM（event 352）。

    官方 demo 流程：
    1. StartConnection(1) → 等待 ConnectionStarted(50)
    2. StartSession(100) → 等待 SessionStarted(150)
    3. TaskRequest(200) 逐字发送 → FinishSession(102) → 等待 SessionFinished(152)
    4. 断连（每句新连接模式）

    鉴权头按官方 demo 使用 X-Api-App-Key / X-Api-Access-Key（非 Bearer token）。
    """
    WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
    volc_api_key = config.get("VOLC_API_KEY")
    volc_resource_id = config.get("VOLC_RESOURCE_ID_TTS")
    voice_type = config.get("VOICE", "BV001_streaming")
    sample_rate = int(config.get("TTS_SAMPLE_RATE", 24000))
    # 按官方 demo 逻辑：优先 config 里的 resource_id，否则按音色名映射
    if not volc_resource_id:
        volc_resource_id = _get_tts_resource_id(voice_type)

    if not volc_api_key:
        logger.error("TTS 启动失败: 缺失 VOLC_API_KEY")
        return

    headers = _build_tts_headers(volc_api_key, volc_resource_id)

    pcm_queue: asyncio.Queue = asyncio.Queue()
    session_started_event = asyncio.Event()

    async def mark_stream_end():
        await pcm_queue.put(_PCM_STREAM_END)

    async def receiver(ws):
        try:
            async for message in ws:
                if isinstance(message, str) or len(message) < 4:
                    continue
                parsed = parse_tts_server_frame(message)
                if not parsed:
                    continue
                et, pb = parsed
                if et == 50:
                    # ConnectionStarted: 发 StartSession
                    req = _base_req_template(voice_type, sample_rate)
                    req["event"] = _EVENT_START_SESSION
                    await ws.send(pack_tts_request(100, session_id, req))
                elif et == 150:
                    session_started_event.set()
                elif et == 352:
                    _log_first_pcm(latency_hooks, pb)
                    await pcm_queue.put(pb)
                elif et == 152:
                    break
                elif et == 52:
                    break
        finally:
            await mark_stream_end()

    session_id = str(uuid.uuid4())

    try:
        async with websockets.connect(
            WSS_URL, additional_headers=headers,
            max_size=10 * 1024 * 1024, open_timeout=10
        ) as ws:
            await ws.send(pack_tts_request(1))

            recv_task = asyncio.create_task(receiver(ws))

            try:
                await asyncio.wait_for(session_started_event.wait(), timeout=5.0)
            except asyncio.TimeoutError:
                raise TimeoutError("火山引擎 TTS 建连超时（等待 SessionStarted）")

            base_req = _base_req_template(voice_type, sample_rate)

            # 官方 demo 模式：背景发字符，主循环边收音频边透传
            # TaskRequest 需要 event=200 和 text 字段
            async def send_chars():
                for char in text:
                    task_req = {"event": _EVENT_TASK_REQUEST, "req_params": {"text": char}}
                    await ws.send(pack_tts_request(200, session_id, task_req))
                    await asyncio.sleep(0.005)
                await ws.send(pack_tts_request(102, session_id))

            send_task = asyncio.create_task(send_chars())

            while True:
                item = await pcm_queue.get()
                if item is _PCM_STREAM_END:
                    break
                yield item

            await send_task

            try:
                await recv_task
            except asyncio.CancelledError:
                raise
            except websockets.exceptions.ConnectionClosed:
                pass
            except Exception:
                logger.debug("TTS recv_task 收尾异常", exc_info=True)

    except websockets.exceptions.ConnectionClosed:
        logger.warning("TTS WebSocket 意外断开")
    except Exception as e:
        logger.error(f"TTS 全局异常: {e}")


# ================= 点读 TTS：向 WebSocket 客户端发送音频流 =================

async def run_tts_to_ws(
    text: str,
    client_ws: WebSocket,
    ws_lock: asyncio.Lock,
    config: dict,
    latency_hooks: Optional[dict] = None,
) -> None:
    """
    点读单句 TTS：优先从连接池取预热连接，池未就绪时回退非池化。
    将 PCM 数据直接通过 client_ws.send_bytes() 发回前端。
    """
    pool = get_tts_pool()
    use_pool = False  # TODO: 临时强制非池化模式，方便排查 TTS 无声问题
    voice_type = config.get("VOICE", "BV001_streaming")
    sample_rate = int(config.get("TTS_SAMPLE_RATE", 24000))
    base_req = _base_req_template(voice_type, sample_rate)

    conn: Optional[_WarmTTSConnection] = None

    try:
        if use_pool:
            conn = await pool.acquire()
            ws = conn.ws
            session_id = str(uuid.uuid4())
            try:
                await _send_and_wait_150(ws, session_id, base_req, ws_lock, client_ws, latency_hooks)
                await _send_and_wait_152(ws, session_id, text, base_req, ws_lock, client_ws, latency_hooks)
            finally:
                await pool.release(conn)
                conn = None
        else:
            async for payload in iter_tts_pcm_chunks(text, config, latency_hooks):
                async with ws_lock:
                    await client_ws.send_bytes(payload)

    except Exception as e:
        logger.warning(f"TTS 下发中断 (前端可能断开): {e}")


# ================= 一轮对话 TTS：从队列消费多句，连接复用 =================

async def run_tts_turn_reused_from_queue(
    client_ws: WebSocket,
    ws_lock: asyncio.Lock,
    config: dict,
    segment_queue: asyncio.Queue,
    latency_hooks: Optional[dict] = None,
) -> None:
    """
    一轮对话 TTS：优先从连接池取预热连接，省去建连 + ConnectionStarted 开销。

    官方连接复用流程（单连接多 session）：
      ConnectionStarted → StartSession(100) → SessionStarted(150)
      → TaskRequest(200) × N → FinishSession(102)
      → 收到 SessionFinished(152) 后，重发 StartSession(100) 开启新一轮 session
      → 全部结束后发送 FinishConnection(2) 断连

    注意：同一个 WebSocket 连接下支持多次 session，但不支持同时多个 session。
    """
    pool = get_tts_pool()
    use_pool = False  # TODO: 临时强制非池化模式，方便排查 TTS 无声问题
    voice_type = config.get("VOICE", "BV001_streaming")
    sample_rate = int(config.get("TTS_SAMPLE_RATE", 24000))
    base_req = _base_req_template(voice_type, sample_rate)

    conn: Optional[_WarmTTSConnection] = None
    ws = None

    # ── 本地辅助函数 ─────────────────────────────────────────────────────────
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

    async def _send_and_wait_150_inline(wss, session_id: str) -> None:
        """发 StartSession(100)，等 SessionStarted(150)；同时透传 352"""
        req = copy.deepcopy(base_req)
        req["event"] = _EVENT_START_SESSION
        await wss.send(pack_tts_request(100, session_id, req))
        deadline = time.monotonic() + 8.0
        saw_150 = False
        while time.monotonic() < deadline and not saw_150:
            raw = await _recv_frame_deadline(wss, deadline)
            parsed = parse_tts_server_frame(raw)
            if not parsed:
                continue
            et, pb = parsed
            if et == 352:
                _log_first_pcm(latency_hooks, pb)
                async with ws_lock:
                    await client_ws.send_bytes(pb)
                continue
            if et == 150:
                saw_150 = True
                break
            if et in (350, 351, 52):
                continue
        if not saw_150:
            raise TimeoutError("火山引擎 TTS 等待 SessionStarted(150) 超时")

    async def _send_and_wait_152_inline(wss, session_id: str, chunk_text: str) -> None:
        """按官方 demo：背景发字符，主循环收音频"""
        async def send_chars():
            # TaskRequest 需要 event=200 和 text 字段
            for char in chunk_text:
                task_req = {"event": _EVENT_TASK_REQUEST, "req_params": {"text": char}}
                await wss.send(pack_tts_request(200, session_id, task_req))
                await asyncio.sleep(0.005)
            await wss.send(pack_tts_request(102, session_id))

        send_task = asyncio.create_task(send_chars())

        deadline = time.monotonic() + 120.0
        while time.monotonic() < deadline:
            raw = await _recv_frame_deadline(wss, deadline)
            parsed = parse_tts_server_frame(raw)
            if not parsed:
                continue
            et, pb = parsed
            if et == 352:
                _log_first_pcm(latency_hooks, pb)
                async with ws_lock:
                    await client_ws.send_bytes(pb)
                continue
            if et in (350, 351, 52):
                continue
            if et == 152:
                break

        await send_task

    # ── 主循环 ──────────────────────────────────────────────────────────────
    try:
        while True:
            text_chunk = await segment_queue.get()
            if text_chunk is None:
                segment_queue.task_done()
                break

            session_id = str(uuid.uuid4())

            # 首次使用：从池中取连接（或新建）
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
                    WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
                    volc_api_key = config.get("VOLC_API_KEY")
                    volc_resource_id = config.get("VOLC_RESOURCE_ID_TTS")
                    if not volc_resource_id:
                        volc_resource_id = _get_tts_resource_id(voice_type)
                    if not volc_api_key:
                        logger.error("TTS 启动失败: 缺失配置信息")
                        return
                    headers = _build_tts_headers(volc_api_key, volc_resource_id)
                    ws = await websockets.connect(
                        WSS_URL, additional_headers=headers,
                        max_size=10 * 1024 * 1024, open_timeout=10
                    )
                    await ws.send(pack_tts_request(1))
                    deadline = time.monotonic() + 8.0
                    saw_50 = False
                    while time.monotonic() < deadline and not saw_50:
                        raw = await _recv_frame_deadline(ws, deadline)
                        parsed = parse_tts_server_frame(raw)
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
            await _send_and_wait_150_inline(ws, session_id)
            await _send_and_wait_152_inline(ws, session_id, text_chunk)
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
