import asyncio
import websockets
import json
import os
import random
import socket
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv("config.env")
client = AsyncOpenAI(api_key=os.getenv("DEEPSEEK_KEY"), base_url=os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com"))

TEST_USER_ID = "auto_tester_007"

# ================= ⚙️ 测试配置中心 =================
TARGET_TEST_ROUNDS = 20
# =================================================

PERSONAS = [
    {"name": "完美通关者", 
     "prompt": "You are a customer at McDonald's. Start with small talk, then order food. When a random event happens, solve it quickly. Finally, say you want to pay and say goodbye. Speak under 15 words."},
    {"name": "暴躁抬杠者",
     "prompt": "You are a very impatient and rude customer. Complain about the menu. Change your mind frequently. Speak under 15 words."},
    {"name": "跑题大师",
     "prompt": "You are a distracted customer. You try to order food, but keep asking the cashier personal questions or talking about the weather. Speak under 15 words."}
]

def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"

async def run_combat_and_judge():
    current_ip = get_local_ip()
    uri = f"ws://{current_ip}:8000/ws/coach?user_id={TEST_USER_ID}"
    persona = random.choice(PERSONAS)

    print("\n" + "🔥" * 25)
    print(f"🚀 AI 混沌对抗测试启动！")
    print(f"🎭 注入混沌人格: 【{persona['name']}】")
    print(f"⏱️ 设定有效对话长度: {TARGET_TEST_ROUNDS} 回合")
    print("🔥" * 25 + "\n")

    full_transcript = []
    current_phase_tracker = ""

    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"action": "ping", "user_id": TEST_USER_ID}))

            tester_history = [{"role": "system", "content": persona["prompt"]}]
            start_msg = "Hi there."
            print(f"👤 [测试员 (扮演 {persona['name']})]: {start_msg}")
            full_transcript.append(f"User: {start_msg}")

            await ws.send(json.dumps({"action": "test_text_input", "text": start_msg, "user_id": TEST_USER_ID}))
            tester_history.append({"role": "assistant", "content": start_msg})

            valid_turns = 0  

            while valid_turns < TARGET_TEST_ROUNDS:
                response = await ws.recv()
                if isinstance(response, str):
                    data = json.loads(response)
                    
                    if data.get("event") != "test_ai_reply":
                        continue

                    valid_turns += 1
                    ai_text = data.get("text")
                    phase = data.get("phase")

                    if phase != current_phase_tracker:
                        # 🌟 修复死循环：如果从收尾回到了破冰，说明是新的一局，清空测试员记忆！
                        if current_phase_tracker == "WRAP_UP" and phase == "ICE_BREAKING":
                            print("\n🔄 [测试员重置] 开启新的一局，测试员记忆已清空...")
                            tester_history = [{"role": "system", "content": persona["prompt"]}]

                        if current_phase_tracker != "":
                            print(f"\n✨ ---> 引擎阶段流转: {current_phase_tracker} ➡️ {phase} <--- ✨\n")
                        current_phase_tracker = phase

                    print(f"🏰 [教练 AI ({phase})]: {ai_text}")
                    full_transcript.append(f"Coach ({phase}): {ai_text}")
                    tester_history.append({"role": "user", "content": ai_text})

                    if valid_turns >= TARGET_TEST_ROUNDS:
                        print(f"\n✅ 达到设定的 {TARGET_TEST_ROUNDS} 次有效对话上限，对抗结束。")
                        break

                    print("⏳ 测试员思考中...")
                    completion = await client.chat.completions.create(
                        model="deepseek-chat",
                        messages=tester_history,
                        max_tokens=40
                    )
                    user_reply = completion.choices[0].message.content

                    print(f"👤 [测试员]: {user_reply}")
                    full_transcript.append(f"User: {user_reply}")
                    tester_history.append({"role": "assistant", "content": user_reply})

                    await asyncio.sleep(0.5)
                    await ws.send(
                        json.dumps({"action": "test_text_input", "text": user_reply, "user_id": TEST_USER_ID}))

            print("\n" + "=" * 50)
            print("⚖️ 正在呼叫 DeepSeek 裁判进行深度裁决...")
            print("=" * 50)

            transcript_str = "\n".join(full_transcript)
            judge_prompt = f"""
            以下是一段用户（模拟【{persona['name']}】人格）与英语教练AI之间的对话记录。
            教练AI的底层被设计为拥有 4 个阶段：ICE_BREAKING(破冰闲聊) -> CORE_TASK(主线考核) -> EVENT_EXTENSION(突发事件) -> WRAP_UP(付款收尾)。

            【对话记录】:
            {transcript_str}

            请你作为高级质量检测员，对教练AI的表现进行极其严格的评估。
            你需要考察以下三个维度（每个维度满分 30 分）：
            1. 四阶段流转与角色保持 (Phase & Roleplay): 教练AI是否平滑且自然地推进了四大阶段（观察括号里的阶段标签）？在过渡时是否生硬？是否始终保持了对应的身份？
            2. 教学与引导性 (Guiding & Open-ended): 教练AI是否尽量使用了开放式提问引导用户开口？在 CORE_TASK 阶段是否自然地将话题引向了核心任务？
            3. 控场与应对突发 (Event Handling & Pull-back): 面对偏题、暴躁或突发剧情（EVENT_EXTENSION）时，教练AI是否能妥善应对，并在解决问题后自然地把话题拉回下一阶段？
            另设 10 分的综合印象分。

            【输出要求（必须遵守）】:
            1. 必须全程使用【简体中文】输出。
            2. 在报告的最开头，使用极大的字号（Markdown 一级标题）输出最终的【综合得分（百分制）】，例如：# 🏆 最终得分：85/100
            3. 详细列出各个维度的扣分项和加分项。
            4. 最后给出一句犀利的总结点评。
            """

            judge_resp = await client.chat.completions.create(
                model="deepseek-chat",
                messages=[{"role": "user", "content": judge_prompt}]
            )

            print("\n📊 裁判最终报告：\n")
            print(judge_resp.choices[0].message.content)
            print("\n" + "=" * 50)

    except Exception as e:
        print(f"❌ 测试脚本异常: {e}")

if __name__ == "__main__":
    asyncio.run(run_combat_and_judge())