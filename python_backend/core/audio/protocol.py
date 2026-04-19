"""
Volcengine ASR / TTS 二进制协议解析与打包模块。

与火山引擎 sauc bigmodel_async (ASR) 和 bidirectional TTS WebSocket 协议对应。
所有协议常量、打包函数、解析函数集中于此，供 asr.py / tts.py 调用。
"""
from __future__ import annotations

import struct
import json
import gzip
import logging
from typing import Optional, Tuple

logger = logging.getLogger("EnglishCoach")

# ================= ASR 协议常量 =================

MIN_AUDIO_BYTES: int = 8000          # 音频过短抛弃：约 250ms (16kHz 16-bit mono)
ASR_STREAM_START_BYTES: int = 4800  # 缓冲达到此字节数即启动流式 ASR（约 150ms）
ASR_RECEIVE_TIMEOUT: float = 5.0    # 发送完毕后，等待识别终稿的最长超时 (秒)
ASR_PCM_CHUNK_BYTES: int = 6400     # 200ms @ 16kHz mono int16 (16000 * 2 * 0.2)

MAX_PAYLOAD_LEN: int = 10 * 1024 * 1024   # ASR 单帧最大长度 (压缩或未压缩)
MAX_DECOMPRESS_LEN: int = 50 * 1024 * 1024  # ASR 解压后业务体最大长度 (防 Zip Bomb)


# ================= TTS 协议常量 =================

MAX_ID_LEN: int = 1024                      # TTS 防止畸形包分配巨量内存
MAX_TTS_PAYLOAD_LEN: int = 5 * 1024 * 1024  # TTS Event 352 音频单帧 payload 上限 (5MB)
MAX_TTS_CONTROL_PAYLOAD_LEN: int = 512 * 1024  # TTS 非音频帧 payload 上限 (512KB)


# ================= ASR 协议 =================

def generate_asr_header(message_type: int, flags: int, serialization: int, compression: int) -> bytes:
    """
    生成 ASR 请求头 4 字节。

    :param message_type: 1=建立连接, 2=发送音频, 15=服务端错误
    :param flags: 控制标志
    :param serialization: 序列化方式 (1=JSON)
    :param compression: 压缩方式 (1=GZIP)
    """
    header = bytearray(4)
    header[0] = 0x11
    header[1] = (message_type << 4) | flags
    header[2] = (serialization << 4) | compression
    header[3] = 0x00
    return bytes(header)


# ================= TTS 协议 =================

def pack_tts_request(event_type: int, session_id: str = "", payload_dict: Optional[dict] = None) -> bytes:
    """
    火山双向 TTS 二进制封包。

    事件号说明：
      1  : 建立连接请求 (Client -> Server)
      2  : 客户端发送完毕，请求结束会话 (Client -> Server)
      50 : 服务端建连就绪，请求配置 (Server -> Client)
      51 : 服务端拒绝连接 (Server -> Client)
      100: 发送具体业务参数，如音色、采样率 (Client -> Server)
      150: 鉴权/配置通过，会话正式启动 (Server -> Client)
      200: 发送待合成文本 (Client -> Server)
      102: 标记当前文本流发送完成 (Client -> Server)
      352: 下发合成好的音频流 (Server -> Client)
      350: 句子开始 (Server -> Client)
      351: 句子结束 (Server -> Client)
      152: 服务端告知当前文本已全部合成完毕 (Server -> Client)
      52 : 服务端确认会话关闭 (Server -> Client)
    """
    if payload_dict is None:
        payload_dict = {}
    payload_bytes = json.dumps(payload_dict).encode('utf-8')
    header = bytearray(4)
    header[0], header[1], header[2], header[3] = 0x11, 0x14, 0x10, 0x00

    event_bytes = struct.pack('>i', event_type)
    payload_len_bytes = struct.pack('>I', len(payload_bytes))

    if event_type in (1, 2):
        return bytes(header) + event_bytes + payload_len_bytes + payload_bytes
    else:
        id_bytes = session_id.encode('utf-8')
        id_len_bytes = struct.pack('>I', len(id_bytes))
        return bytes(header) + event_bytes + id_len_bytes + id_bytes + payload_len_bytes + payload_bytes


# ================= TTS 下行帧解析（ASR / TTS 共用） =================

def parse_tts_server_frame(message: bytes) -> Optional[Tuple[int, bytes]]:
    """
    解析双向 TTS 下行帧，返回 (event_type, payload) 或 None（跳过/损坏）。

    火山引擎帧格式：
      header[0] = 0x11
      header[1] = (msg_type << 4) | flags
      header[2] = serialization << 4 | compression
      header[3] = 0x00
      msg_type: 9=业务帧, 11=控制帧, 15=错误帧
    """
    if isinstance(message, str) or len(message) < 4:
        return None

    header_size = (message[0] & 0x0F) * 4
    msg_type = (message[1] >> 4) & 0x0F
    flags = message[1] & 0x0F

    # 错误帧解析（msg_type=15）或未知 msg_type
    # 注意：错误帧也可能包含 ID 字段（flags & 0x04），需要正确跳过
    if msg_type == 15 or (msg_type not in (9, 11) and len(message) > 12):
        offset = header_size
        err_info = ""
        
        # 如果 flags 包含 ID 标志（0x04），需要跳过 ID_len + ID_data
        if flags & 0x04:
            if offset + 4 <= len(message):
                id_len = struct.unpack(">I", message[offset: offset + 4])[0]
                offset += 4
                if 0 < id_len <= MAX_ID_LEN and offset + id_len <= len(message):
                    offset += id_len
                    err_info = f"(session_id present)"
        
        err_raw = message[offset:]
        
        # 错误帧可能包含 payload_len 前缀
        if len(err_raw) >= 4:
            payload_len = struct.unpack(">I", err_raw[:4])[0]
            if 0 < payload_len <= MAX_TTS_CONTROL_PAYLOAD_LEN and len(err_raw) >= 4 + payload_len:
                err_raw = err_raw[4: 4 + payload_len]
        
        try:
            err_str = err_raw.decode("utf-8", errors="replace").strip('\x00')
            if '{' in err_str:
                try:
                    err_json = json.loads(err_str)
                    err_str = err_json.get("message", err_json.get("error", err_str))
                except:
                    pass
        except Exception:
            err_str = f"RawHex: {err_raw.hex()[:100]}"
        
        logger.error(f"TTS 服务端返回错误 (msg_type={msg_type}){err_info}: {err_str}")
        return None

    if msg_type not in (9, 11):
        logger.debug(f"TTS 收到非标准帧: msg_type={msg_type}, flags={flags}, size={len(message)}")
        return None

    offset = header_size
    event_type: Optional[int] = None

    # flags 的 bit 2 (0x04) 表示帧中存在 ID 字段
    # flags=1,3,4,5,6,7 都可能包含 ID 字段，关键是检查 bit 2
    if flags & 0x04:
        if offset + 4 > len(message):
            return None
        event_type = struct.unpack(">i", message[offset: offset + 4])[0]
        offset += 4

        if offset + 4 > len(message):
            return None
        id_len = struct.unpack(">I", message[offset: offset + 4])[0]

        if id_len > MAX_ID_LEN or offset + 4 + id_len > len(message):
            return None
        offset += 4 + id_len
    else:
        event_type = None

    if offset + 4 > len(message):
        return None
    payload_size = struct.unpack(">I", message[offset: offset + 4])[0]
    offset += 4

    pl_limit = MAX_TTS_PAYLOAD_LEN if event_type == 352 else MAX_TTS_CONTROL_PAYLOAD_LEN
    if payload_size > pl_limit or offset + payload_size > len(message):
        logger.warning(f"TTS 抛弃异常帧: event={event_type} payload_size={payload_size} limit={pl_limit}")
        return None

    payload = message[offset: offset + payload_size]
    if event_type is None:
        return None
    return (event_type, payload)


# ================= ASR 下行帧解析 =================

def parse_asr_server_frame(message: bytes) -> Optional[Tuple[int, dict]]:
    """
    解析 ASR 下行帧，返回 (msg_type_high, parsed_result_dict) 或 None。

    msg_type_high:
      9 = 正常业务帧
      15 = 服务端错误帧
    """
    if isinstance(message, str) or len(message) < 4:
        return None

    msg_type_high = (message[1] >> 4) & 0x0F
    serialization = (message[2] >> 4) & 0x0F
    compression = message[2] & 0x0F

    if msg_type_high == 15:
        header_size = (message[0] & 0x0F) * 4
        err_raw = message[header_size:]
        try:
            err_str = err_raw.decode("utf-8", errors="ignore")
            if '{' in err_str:
                err_str = json.loads(err_str).get("message", err_str)
        except Exception:
            err_str = f"RawHex: {err_raw.hex()[:50]}"
        logger.error(f"ASR 服务端返回错误: {err_str}")
        return (15, {})

    if msg_type_high != 9:
        return None

    payload_offset = ((message[0] & 0x0F) * 4) + (
        4 if (message[1] & 0x0F) in (1, 3) else 0
    ) + 4
    if payload_offset > len(message):
        return None

    payload_len = struct.unpack(">I", message[payload_offset - 4: payload_offset])[0]
    if payload_len > MAX_PAYLOAD_LEN or payload_offset + payload_len > len(message):
        logger.warning(f"ASR 抛弃异常大包: payload_len={payload_len}")
        return None

    payload_bytes = message[payload_offset: payload_offset + payload_len]

    if compression == 1:
        try:
            payload_bytes = gzip.decompress(payload_bytes)
        except Exception as e:
            logger.warning(f"ASR Gzip 解压失败: {e}")
            return None

    if len(payload_bytes) > MAX_DECOMPRESS_LEN:
        logger.warning("ASR 抛弃超大业务体 (超过 MAX_DECOMPRESS_LEN)")
        return None

    if serialization != 1:
        return None

    try:
        result = json.loads(payload_bytes.decode("utf-8"))
        return (9, result)
    except Exception as e:
        logger.warning(f"ASR JSON 解析失败: {e}")
        return None
