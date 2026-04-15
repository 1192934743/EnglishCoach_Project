import asyncio
import websockets
import json
import os
import random
from openai import AsyncOpenAI
from dotenv import load_dotenv

load_dotenv("config.env")
client = AsyncOpenAI(api_key=os.getenv("DEEPSEEK_KEY"), base_url=os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com"))

TEST_USER_ID = "auto_tester_007"

# ================= ⚙️ 测试配置中心 =================
# 🌟 在这里设置你想要对抗的“总回合数”（一问一答算1个回合）
# 建议值：快速回归测试填 6，深度抗压测试填 15-20
TARGET_TEST_ROUNDS = 20
# =================================================

# 1. 混沌对抗人格池 (Chaos Fuzzing)
PERSONAS = [
    {"name": "乖巧学员", "prompt": "You are a polite customer ordering food. Speak naturally, under 15 words."},
    {"name": "暴躁抬杠者",
     "prompt": "You are a very impatient and rude customer. Complain about the menu. Change your mind frequently. Speak under 15 words."},
    {"name": "跑题大师",
     "prompt": "You are a distracted customer. You try to order food, but keep asking the cashier personal questions or talking about the weather. Speak under 15 words."}
]


async def run_combat_and_judge():
    uri = f"ws://localhost:8000/ws/coach?user_id={TEST_USER_ID}"
    persona = random.choice(PERSONAS)

    print("\n" + "🔥" * 25)
    print(f"🚀 AI 混沌对抗测试启动！")
    print(f"🎭 注入混沌人格: 【{persona['name']}】")
    print(f"⏱️ 设定对抗长度: {TARGET_TEST_ROUNDS} 回合")
    print("🔥" * 25 + "\n")

    full_transcript = []

    try:
        async with websockets.connect(uri) as ws:
            await ws.send(json.dumps({"action": "ping", "user_id": TEST_USER_ID}))

            tester_history = [{"role": "system", "content": persona["prompt"]}]
            start_msg = "Hi there."
            print(f"👤 [测试员 (扮演 {persona['name']})]: {start_msg}")
            full_transcript.append(f"User: {start_msg}")

            await ws.send(json.dumps({"action": "test_text_input", "text": start_msg, "user_id": TEST_USER_ID}))
            tester_history.append({"role": "assistant", "content": start_msg})

            for turn in range(TARGET_TEST_ROUNDS):
                response = await ws.recv()
                if isinstance(response, str):
                    data = json.loads(response)
                    if data.get("event") == "test_ai_reply":
                        ai_text = data.get("text")
                        phase = data.get("phase")

                        print(f"🏰 [教练 AI ({phase})]: {ai_text}")
                        full_transcript.append(f"Coach ({phase}): {ai_text}")
                        tester_history.append({"role": "user", "content": ai_text})

                        # 到了最后一轮，测试员就不再回复了，准备进入打分环节
                        if turn == TARGET_TEST_ROUNDS - 1:
                            print(f"\n✅ 达到设定的 {TARGET_TEST_ROUNDS} 回合上限，对抗结束。")
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

            # 2. LLM-as-a-Judge 自动裁决 (强制中文输出 + 量化得分)
            transcript_str = "\n".join(full_transcript)
            judge_prompt = f"""
            以下是一段用户（模拟【{persona['name']}】人格）与扮演麦当劳收银员的教练AI之间的对话记录。

            【对话记录】:
            {transcript_str}

            请你作为高级质量检测员，对教练AI的表现进行极其严格的评估。
            你需要考察以下三个维度（每个维度满分 30 分）：
            1. 角色保持度 (Roleplay Integrity): 教练AI是否始终保持收银员的身份？应对刁难时是否得体？
            2. 教学引导性 (Open-ended Questions): 教练AI是否尽量使用了开放式提问（而非简单的Yes/No），并且提问是否符合当前场景常识？
            3. 控场能力 (Pull-back Ability): 面对偏题或暴躁的用户，教练AI是否能自然地把话题拉回点餐主线？
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