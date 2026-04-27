import asyncio
import websockets
import json
import os
import random
import socket
import argparse
from openai import AsyncOpenAI
from dotenv import load_dotenv
from database import SessionLocal, Topic

load_dotenv("config.env")
client = AsyncOpenAI(api_key=os.getenv("DEEPSEEK_KEY"), base_url=os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com"))

TEST_USER_ID = "auto_tester_007"

# ================= ⚙️ 测试配置中心 =================
TARGET_TEST_ROUNDS = 20
# =================================================

# ── 场景人格映射 ─────────────────────────────────────────────────────────────
# 每个场景对应一组对抗人格（正面、刁难、跑题），用于场景级优化
SCENE_PERSONAS = {
    "mcdonalds": [
        {"name": "McD-完美顾客",
         "prompt": "You are a customer at McDonald's. Start with small talk, then order food. When a random event happens, solve it quickly. Finally, say you want to pay and say goodbye. Speak under 15 words."},
        {"name": "McD-暴躁顾客",
         "prompt": "You are a very impatient and rude McDonald's customer. Complain about the menu items. Change your mind frequently about what you want. Speak under 15 words."},
        {"name": "McD-跑题大师",
         "prompt": "You are a distracted customer at McDonald's. Try to order food but keep asking the cashier about personal topics or talking about unrelated things. Speak under 15 words."},
    ],
    "job_interview": [
        {"name": "JI-专业应聘者",
         "prompt": "You are a calm and professional job candidate. Answer questions concisely and logically. If asked a technical question, give structured answers. Speak under 15 words."},
        {"name": "JI-紧张小白",
         "prompt": "You are a nervous job candidate who gives very short one-word answers. You are easily confused by technical questions and often say 'I don't know'. Speak under 15 words."},
        {"name": "JI-话痨自嗨",
         "prompt": "You are an over-enthusiastic job candidate. You talk too much and go off-topic. You mention your pet projects unnecessarily. Speak under 15 words."},
    ],
    "custom_imitation": [
        {"name": "CI-认真学习者",
         "prompt": "You are a language learner having a casual conversation. Respond naturally to the topics raised. Occasionally make small grammatical errors. Speak under 15 words."},
        {"name": "CI-沉默寡言",
         "prompt": "You are a shy language learner who responds with one or two words only. You rarely initiate topics. Speak under 10 words."},
        {"name": "CI-话匣子",
         "prompt": "You are an overly talkative language learner. You constantly switch topics and talk about random things. Speak under 15 words."},
    ],
}

# ── 兜底通用人格（用于未指定话题 ID 时的默认模式）───────────────────────────
FALLBACK_PERSONAS = [
    {"name": "通用-正常用户",
     "prompt": "You are a normal user having a conversation. Respond naturally and helpfully. Speak under 15 words."},
    {"name": "通用-刁难用户",
     "prompt": "You are a difficult user who complains, changes topic frequently, and tests the assistant's patience. Speak under 15 words."},
    {"name": "通用-沉默用户",
     "prompt": "You are a quiet user who gives very short answers and rarely elaborates. Speak under 10 words."},
]


def get_scene_personas(scene_id: str) -> list:
    return SCENE_PERSONAS.get(scene_id, FALLBACK_PERSONAS)


def get_local_ip():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def load_topic_meta(topic_id: int) -> dict:
    """从 Topic DB 读取话题的裁判用元信息"""
    db = SessionLocal()
    try:
        t = db.query(Topic).filter(Topic.id == topic_id).first()
        if not t:
            return {}
        return {
            "title": t.title or "",
            "title_zh": t.title_zh or "",
            "role_name": t.role_name or "Assistant",
            "learner_level": t.learner_level or "Intermediate",
            "scene_specific_rules": t.scene_specific_rules or [],
        }
    finally:
        db.close()

def switch_scene_in_scenes_json(scene_id: str, file_path: str = "scenes.json"):
    """[已废弃] scenes.json 已迁移至 Topic DB，该函数不再使用。场景切换通过 WebSocket switch_topic action 完成。"""
    return False


async def run_combat_and_judge(topic_id: int = None, topic_meta: dict = None):
    """
    运行对抗测试。
    - topic_id: 要测试的话题 ID（整数，从 Topic DB）
    - topic_meta: 话题元信息（包含 title/role_name/scene_specific_rules 等）
    """
    topic_meta = topic_meta or {}
    current_ip = get_local_ip()
    uri = f"ws://{current_ip}:8000/ws/coach?user_id={TEST_USER_ID}"

    # 场景感知人格选择（支持旧字符串参数兼容，同时支持整数 topic_id）
    scene_id = str(topic_id) if topic_id is not None else None
    if scene_id:
        personas_pool = get_scene_personas(scene_id)
        print(f"\n🎯 测试话题 ID: {topic_id}")
    else:
        personas_pool = FALLBACK_PERSONAS
        print(f"\n🚀 AI 混沌对抗测试启动（通用模式）")

    persona = random.choice(personas_pool)

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

            # 连接建立后，若指定了 topic_id，通过 switch_topic action 切换话题
            if topic_id is not None:
                await ws.send(json.dumps({"action": "switch_topic", "topic_id": topic_id, "user_id": TEST_USER_ID}))
                # 等待话题切换完成
                while True:
                    resp = await ws.recv()
                    resp_data = json.loads(resp)
                    if resp_data.get("event") == "topic_changed":
                        print(f"✅ 已切换到话题: {resp_data.get('topic_title')} (ID={resp_data.get('topic_id')})")
                        break
                await asyncio.sleep(0.3)

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
                        print(f"\n✅ 达到设定的 {TARGET_TEST_ROUNDS} 次有效对话上限，对抗结束。即将断开连接。")
                        break # 跳出 while，随后将退出 async with 块并切断连接

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

        # 🌟 测试走完让 ws 断开，在这里慢慢调 DeepSeek 裁判接口
        print("\n" + "=" * 50)
        print("⚖️ 正在呼叫 DeepSeek 裁判进行深度裁决...")
        print("=" * 50)

        transcript_str = "\n".join(full_transcript)

        # 生成场景上下文
        if topic_meta:
            rules = topic_meta.get("scene_specific_rules", [])
            rules_text = "\n".join(f"- {r}" for r in rules) if rules else "(暂无专属规则)"
            scene_context = f"""【场景】{topic_meta.get('title', '')} ({topic_meta.get('title_zh', '')})
【扮演角色】{topic_meta.get('role_name', 'Assistant')}
【目标水平】{topic_meta.get('learner_level', 'Intermediate')}
【场景专属规则】{rules_text}"""
            rules_guidance = f"""
【场景专属护栏评估（若有）】：
{chr(10).join(f"- {r}" for r in rules) if rules else '(无)'}
若教练AI违反上述任意规则，每条扣 5-10 分。"""
        else:
            scene_context = "【场景背景】: 通用对话测试。"
            rules_guidance = ""

        judge_prompt = f"""
        {scene_context}
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
        {rules_guidance}
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
    parser = argparse.ArgumentParser(description="AI 混沌对抗测试套件")
    parser.add_argument(
        "--topic-id", type=int, default=None,
        help="要测试的话题 ID（整数，从 Topic DB 查询）"
    )
    args = parser.parse_args()

    topic_meta = load_topic_meta(args.topic_id) if args.topic_id else {}
    asyncio.run(run_combat_and_judge(topic_id=args.topic_id, topic_meta=topic_meta))