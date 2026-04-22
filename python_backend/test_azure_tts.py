import azure.cognitiveservices.speech as speechsdk
import asyncio
import os
import sys
import time
import threading
import re
from dotenv import load_dotenv

# 1. 加载配置
load_dotenv("config.env") 
AZURE_SPEECH_KEY = os.getenv("AZURE_SPEECH_KEY", "").strip()
AZURE_SPEECH_REGION = os.getenv("AZURE_SPEECH_REGION", "").strip()

if not AZURE_SPEECH_KEY or not AZURE_SPEECH_REGION:
    print("❌ 致命错误：未读取到 KEY 或 REGION，请检查 config.env！")
    sys.exit(1)

async def simulate_llm_producer(text: str, segment_queue: asyncio.Queue):
    """
    【角色 A：大模型与调度总管】
    模拟大模型流式输出，遇到标点符号就切断，丢入 TTS 队列。
    """
    print("\n[大模型] 正在思考并按标点流式输出...")
    
    # 模拟按标点符号切分句子
    chunks = re.split(r'(?<=[.!?。！？])\s+', text)
    
    for i, chunk in enumerate(chunks):
        if chunk.strip():
            print(f"  -> [调度器] 切片 #{i+1} 进入队列: '{chunk}'")
            await segment_queue.put(chunk)
            # 模拟大模型生成文本的延迟
            await asyncio.sleep(0.6) 
            
    print("  -> [调度器] AI 回复已全部生成，推入结束标志 None。")
    await segment_queue.put(None)


async def test_azure_tts_consumer(segment_queue: asyncio.Queue):
    """
    【角色 B：TTS 打工人】
    只负责消费队列中的文本并拉取音频流，【严禁】在这里发送 tts_finished 信号。
    """
    print("[TTS打工人] 启动，准备消费队列...\n")
    
    # 初始化 Azure 引擎 (复用同一个引擎，Azure 底层会自动维持 WebSocket 长连接)
    speech_config = speechsdk.SpeechConfig(subscription=AZURE_SPEECH_KEY, region=AZURE_SPEECH_REGION)
    speech_config.set_speech_synthesis_output_format(speechsdk.SpeechSynthesisOutputFormat.Raw24Khz16BitMonoPcm)
    
    pull_stream = speechsdk.audio.PullAudioOutputStream()
    audio_config = speechsdk.audio.AudioOutputConfig(stream=pull_stream)
    synthesizer = speechsdk.SpeechSynthesizer(speech_config=speech_config, audio_config=audio_config)

    # 🌟 核心机制：跨线程的“完成标志位” (只声明一次)
    synthesis_done = threading.Event()

    def evt_completed(evt):
        synthesis_done.set()  # 本小句合成完毕
        
    def evt_canceled(evt):
        synthesis_done.set()  # 发生异常中断

    # 绑定事件 (在循环外只绑定一次，防止内存泄漏)
    synthesizer.synthesis_completed.connect(evt_completed)
    synthesizer.synthesis_canceled.connect(evt_canceled)

    total_chunks = 0

    try:
        while True:
            # 1. 从队列获取单句片段
            text_chunk = await segment_queue.get()
            
            if text_chunk is None:
                segment_queue.task_done()
                print("\n[TTS打工人] 收到下班通知 (None)，退出消费循环。")
                break
                
            total_chunks += 1
            print(f"\n[TTS打工人] 正在合成切片 #{total_chunks}: '{text_chunk}'")
            
            # 2. 每次合成前，重置跨线程标志位
            synthesis_done.clear()
            
            # 3. 发起本句的异步合成
            synthesizer.start_speaking_text_async(text_chunk)
            
            audio_buffer = bytes(8192)
            chunk_bytes = 0
            
            # 4. 带超时保护的拉取循环
            while True:
                try:
                    # 极短超时，让协程有机会检查标志位
                    filled_size = await asyncio.wait_for(
                        asyncio.to_thread(pull_stream.read, audio_buffer), 
                        timeout=0.1
                    )
                    
                    if filled_size > 0:
                        chunk_bytes += filled_size
                        # 这里在主项目里是：await client_ws.send_bytes(chunk)
                    
                    if filled_size == 0:
                        break # 数据被榨干，主动退出

                except asyncio.TimeoutError:
                    # 如果超时等不到新数据，检查底层 C++ 线程是不是已经宣布这句合成了
                    if synthesis_done.is_set():
                        print(f"  -> [防死锁出口] 本句音频全接收完毕，安全切断拉取！")
                        break
                        
            print(f"  -> 本句处理完成，共拉取 {chunk_bytes} bytes。")
            segment_queue.task_done()

    except Exception as e:
        print(f"TTS 消费循环异常: {e}")


async def main():
    test_text = "Hello, nice to meet you too! Welcome to McDonald's drive-thru. What can I get for you today?"
    
    # 创建通信队列
    segment_queue = asyncio.Queue()
    
    # 1. 启动 TTS 消费者后台任务
    consumer_task = asyncio.create_task(test_azure_tts_consumer(segment_queue))
    
    # 2. 启动大模型生产者任务
    await simulate_llm_producer(test_text, segment_queue)
    
    # 3. 等待消费者把队列里所有音频全部处理完
    await consumer_task
    
    # 🚨 4. 最核心的改动：当大模型说完了，且 TTS 也全播完了，最后发送全局结束信号！
    print("\n🚀 >>> [调度总管向前端发送信号]: {'event': 'tts_finished'} <<< 🚀")
    print("前端播放器此时关闭，完美无截断！")

if __name__ == "__main__":
    asyncio.run(main())