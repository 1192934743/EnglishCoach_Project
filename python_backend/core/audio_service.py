import json
import asyncio
import gzip
import struct
import uuid
import logging
import websockets
from fastapi import WebSocket

logger = logging.getLogger("EnglishCoach")

# ================= 协议常量与防爆配置 =================

MIN_AUDIO_BYTES = 8000           # 音频过短抛弃：约 250ms (16kHz 16-bit mono 采样率)
ASR_RECEIVE_TIMEOUT = 5.0        # 发送完毕后，等待识别终稿的最长超时 (秒)

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
                        "model_name": "bigmodel", "enable_punc": True, 
                        "show_utterances": True, "enable_emotion_detection": True
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
                    chunk_size = 3200
                    for i in range(0, len(pcm_bytes), chunk_size):
                        chunk = pcm_bytes[i:i + chunk_size]
                        payload = gzip.compress(chunk)
                        await ws.send(generate_asr_header(2, 0, 0, 1) + struct.pack('>I', len(payload)) + payload)
                        await asyncio.sleep(0.01)
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

# ================= 2. 文本转流式语音 (TTS) =================

async def run_tts_to_ws(text: str, client_ws: WebSocket, ws_lock: asyncio.Lock, config: dict):
    WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
    
    volc_api_key = config.get('VOLC_API_KEY')
    volc_resource_id = config.get('VOLC_RESOURCE_ID_TTS')
    voice_type = config.get('VOICE', 'BV001_streaming')

    if not volc_api_key or not volc_resource_id:
        logger.error("🚨 TTS 启动失败: 缺失配置信息")
        return

    headers = {
        "Authorization": f"Bearer {volc_api_key}",
        "x-api-key": volc_api_key,
        "X-Api-Resource-Id": volc_resource_id,
        "X-Api-Connect-Id": str(uuid.uuid4())
    }

    try:
        async with websockets.connect(WSS_URL, additional_headers=headers, open_timeout=10) as ws:
            await ws.send(pack_tts_request(1))
            session_id = str(uuid.uuid4())
            is_session_started = False
            client_disconnected = False

            async def receiver():
                nonlocal is_session_started, client_disconnected
                async for message in ws:
                    if client_disconnected: break
                        
                    if isinstance(message, str) or len(message) < 4: continue
                    header_size = (message[0] & 0x0f) * 4
                    msg_type = (message[1] >> 4) & 0x0f
                    flags = message[1] & 0x0f

                    if msg_type == 15:
                        err_raw = message[header_size:]
                        try:
                            err_str = err_raw.decode('utf-8', errors='ignore')
                            if '{' in err_str:
                                err_str = json.loads(err_str).get('message', err_str)
                        except Exception:
                            err_str = f"RawHex: {err_raw.hex()[:50]}"
                        logger.error(f"TTS 服务端返回错误: {err_str}")
                        break
                    
                    if msg_type in [9, 11]:
                        offset = header_size
                        if flags == 4:
                            if offset + 4 > len(message): continue
                            event_type = struct.unpack('>i', message[offset:offset + 4])[0]
                            offset += 4
                            
                            if offset + 4 > len(message): continue
                            id_len = struct.unpack('>I', message[offset:offset + 4])[0]
                            
                            if id_len > MAX_ID_LEN or offset + 4 + id_len > len(message): 
                                logger.warning(f"TTS 抛弃异常包: id_len={id_len}")
                                continue
                            offset += 4 + id_len
                        else:
                            event_type = None

                        if offset + 4 > len(message): continue
                        payload_size = struct.unpack('>I', message[offset:offset + 4])[0]
                        offset += 4

                        pl_limit = MAX_TTS_PAYLOAD_LEN if event_type == 352 else MAX_TTS_CONTROL_PAYLOAD_LEN
                        if payload_size > pl_limit or offset + payload_size > len(message):
                            logger.warning(f"TTS 抛弃异常包: payload_size={payload_size}, limit={pl_limit}")
                            continue
                            
                        payload = message[offset: offset + payload_size]

                        if event_type == 50:
                            req = {
                                "user": {"uid": "english_coach"},
                                "namespace": "BidirectionalTTS",
                                "req_params": {"speaker": voice_type,
                                               "audio_params": {"format": "pcm", "sample_rate": 24000}}
                            }
                            await ws.send(pack_tts_request(100, session_id, req))
                        
                        elif event_type == 150:
                            is_session_started = True
                        
                        elif event_type == 352:
                            try:
                                async with ws_lock:
                                    await client_ws.send_bytes(payload)
                            except Exception as e:
                                logger.warning(f"TTS 下发中断 (前端可能断开): {e}")
                                client_disconnected = True
                                break
                                
                        elif event_type == 152:
                            await ws.send(pack_tts_request(2))
                            
                        elif event_type == 52:
                            break

            recv_task = asyncio.create_task(receiver())
            
            try:
                wait_cycles = 0
                while not is_session_started and wait_cycles < 500:
                    await asyncio.sleep(0.01)
                    wait_cycles += 1
                
                if not is_session_started:
                    raise TimeoutError("火山引擎 TTS 建连超时")

                await ws.send(pack_tts_request(200, session_id, {"req_params": {"text": text}}))
                await ws.send(pack_tts_request(102, session_id)) 
                
                await recv_task
                
                if client_disconnected:
                    try:
                        await ws.send(pack_tts_request(2))
                    except Exception:
                        pass
                     
            finally:
                if not recv_task.done():
                    recv_task.cancel()
                    try:
                        await asyncio.wait_for(recv_task, timeout=1.0)
                    except (asyncio.CancelledError, asyncio.TimeoutError):
                        pass

    except websockets.exceptions.ConnectionClosed:
        logger.warning("🌋 TTS WebSocket 意外断开")
    except Exception as e:
        logger.error(f"❌ TTS 全局异常: {e}")