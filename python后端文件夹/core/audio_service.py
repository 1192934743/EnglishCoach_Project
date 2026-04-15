import json
import asyncio
import gzip
import struct
import uuid
import logging
import websockets
from fastapi import WebSocket

logger = logging.getLogger("EnglishCoach")


def generate_asr_header(message_type, flags, serialization, compression):
    header = bytearray(4)
    header[0] = 0x11
    header[1] = (message_type << 4) | flags
    header[2] = (serialization << 4) | compression
    header[3] = 0x00
    return bytes(header)


def pack_tts_request(event_type, session_id="", payload_dict=None):
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


async def run_volcengine_wss_asr(pcm_bytes: bytearray, config: dict, max_retries=3):
    WSS_URL = "wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async"
    final_text, detected_emotion = "", "neutral"
    if len(pcm_bytes) < 8000:
        return final_text, detected_emotion

    for attempt in range(max_retries):
        try:
            headers = {
                "Authorization": f"Bearer {config['VOLC_API_KEY']}",
                "x-api-key": config['VOLC_API_KEY'],
                "X-Api-Resource-Id": config['VOLC_RESOURCE_ID_ASR'],
                "X-Api-Connect-Id": str(uuid.uuid4())
            }
            async with websockets.connect(WSS_URL, additional_headers=headers, open_timeout=10) as ws:
                req_params = {
                    "user": {"uid": "english_ai"},
                    "audio": {"format": "pcm", "codec": "raw", "rate": 16000, "bits": 16, "channel": 1,
                              "language": "zh-CN"},
                    "request": {"model_name": "bigmodel", "enable_punc": True, "show_utterances": True,
                                "enable_emotion_detection": True}
                }
                payload = gzip.compress(json.dumps(req_params).encode('utf-8'))
                await ws.send(generate_asr_header(1, 0, 1, 1) + struct.pack('>I', len(payload)) + payload)

                async def receive_responses():
                    nonlocal final_text, detected_emotion
                    async for message in ws:
                        if isinstance(message, str) or len(message) < 4: continue
                        serialization, compression = (message[2] >> 4) & 0x0f, message[2] & 0x0f
                        if (message[1] >> 4) & 0x0f == 9:
                            payload_offset = ((message[0] & 0x0f) * 4) + (4 if (message[1] & 0x0f) in [1, 3] else 0) + 4
                            payload_bytes = message[payload_offset:payload_offset + struct.unpack('>I', message[
                                payload_offset - 4:payload_offset])[0]]
                            if compression == 1:
                                payload_bytes = gzip.decompress(payload_bytes)
                            if serialization == 1:
                                result = json.loads(payload_bytes.decode('utf-8')).get('result', {})
                                utterances = result.get('utterances', [])
                                if utterances and utterances[-1].get('definite'):
                                    final_text = utterances[-1].get('text', '')
                                    detected_emotion = utterances[-1].get('additions', {}).get('emotion', 'neutral')
                                    return

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
                done, pending = await asyncio.wait([receive_task, send_task], return_when=asyncio.FIRST_COMPLETED)

                if send_task in done:
                    try:
                        await asyncio.wait_for(receive_task, timeout=2.0)
                    except asyncio.TimeoutError:
                        pass
                else:
                    send_task.cancel()

                return final_text, detected_emotion

        except Exception as e:
            logger.warning(f"⚠️ ASR 网络波动 (尝试 {attempt + 1}/{max_retries}): {e}")
            if attempt < max_retries - 1:
                await asyncio.sleep(0.5)
            else:
                logger.error(f"❌ ASR 彻底失败")

    return final_text, detected_emotion


async def run_tts_to_ws(text: str, client_ws: WebSocket, ws_lock: asyncio.Lock, config: dict):
    WSS_URL = "wss://openspeech.bytedance.com/api/v3/tts/bidirection"
    headers = {
        "Authorization": f"Bearer {config['VOLC_API_KEY']}",
        "x-api-key": config['VOLC_API_KEY'],
        "X-Api-Resource-Id": config['VOLC_RESOURCE_ID_TTS'],
        "X-Api-Connect-Id": str(uuid.uuid4())
    }

    try:
        async with websockets.connect(WSS_URL, additional_headers=headers) as ws:
            await ws.send(pack_tts_request(1))
            session_id = str(uuid.uuid4())
            is_session_started = False

            async def receiver():
                nonlocal is_session_started
                async for message in ws:
                    if isinstance(message, str) or len(message) < 4: continue
                    header_size = (message[0] & 0x0f) * 4
                    msg_type = (message[1] >> 4) & 0x0f
                    flags = message[1] & 0x0f

                    if msg_type == 15: break
                    if msg_type in [9, 11]:
                        offset = header_size
                        if flags == 4:
                            event_type = struct.unpack('>i', message[offset:offset + 4])[0]
                            offset += 4
                            id_len = struct.unpack('>I', message[offset:offset + 4])[0]
                            offset += 4 + id_len
                        else:
                            event_type = None

                        payload_size = struct.unpack('>I', message[offset:offset + 4])[0]
                        offset += 4
                        payload = message[offset: offset + payload_size]

                        if event_type == 50:
                            req = {
                                "user": {"uid": "english_coach"},
                                "namespace": "BidirectionalTTS",
                                "req_params": {"speaker": config["VOICE"],
                                               "audio_params": {"format": "pcm", "sample_rate": 24000}}
                            }
                            await ws.send(pack_tts_request(100, session_id, req))
                        elif event_type == 150:
                            is_session_started = True
                        elif event_type == 352:
                            async with ws_lock:
                                await client_ws.send_bytes(payload)
                        elif event_type == 152:
                            await ws.send(pack_tts_request(2))
                        elif event_type == 52:
                            break

            recv_task = asyncio.create_task(receiver())
            while not is_session_started:
                await asyncio.sleep(0.01)

            await ws.send(pack_tts_request(200, session_id, {"req_params": {"text": text}}))
            await ws.send(pack_tts_request(102, session_id))
            await recv_task

    except Exception as e:
        logger.error(f"❌ TTS 异常: {e}")