"""
test_prompt_generation.py
=========================
独立测试脚本：验证 dialogue_engine.py 的 Prompt 生成逻辑。
不依赖任何外部数据库，所有数据均为内存 Mock。

运行方式：
    cd python_backend
    python test_prompt_generation.py

输出：
    1. 主LLM 静态系统 Prompt (System)
    2. 主LLM 动态回合 Prompt (User/Developer)
    3. 副LLM 导演评估 Prompt (System)
"""

import sys
import os

# ── 确保 python_backend 根目录在 Python 路径中 ────────────────────────────────
_BACKEND = os.path.dirname(os.path.abspath(__file__))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)


# ─────────────────────────────────────────────────────────────────────────────
# 1. Mock User（绕过 database.py，避免连接真实 DB）
# ─────────────────────────────────────────────────────────────────────────────

class MockUser:
    """模拟 User ORM 对象，仅保留 build_prompts 需要的字段。"""

    def __init__(self, politeness_level: int = 1, settings: dict = None):
        self.politeness_level = politeness_level
        # settings 格式: {"learner_level": "Intermediate", ...}
        self.settings = settings or {}


# ─────────────────────────────────────────────────────────────────────────────
# 2. Mock TargetNode（模拟数据库中的词汇节点）
# ─────────────────────────────────────────────────────────────────────────────

def make_node(node_id: int, node_text: str, node_type: str = "vocabulary",
              depth_level: int = 1) -> dict:
    """构造一个符合 TaskPacket node 格式的字典。"""
    return {
        "id": node_id,
        "node_text": node_text,
        "node_type": node_type,
        "depth_level": depth_level,
    }


# ─────────────────────────────────────────────────────────────────────────────
# 3. 构造 Mock TaskPacket（手工创建，不调用 session_planner）
# ─────────────────────────────────────────────────────────────────────────────

from domain.entities.task_packet import TaskPacket

MOCK_TASK_PACKET = TaskPacket(
    topic_id=1,
    topic_title="Ordering Coffee at Starbucks",
    topic_title_zh="星巴克点单",
    scene_prompt="A busy Starbucks coffee shop",
    role_name="Barista",
    learner_level="Intermediate",          # 用户指定的级别
    voice="Stanley",
    depth_tier=1,
    max_reply_sentences=3,
    target_nodes=[
        make_node(node_id=101, node_text="decaf",     depth_level=1),
        make_node(node_id=102, node_text="extra shot", depth_level=1),
    ],
    bonus_nodes=[],
    review_nodes=[],
    difficulty_config=None,                # 使用默认配置
    scene_specific_rules=[
        "Always ask for the customer's name for the cup.",
        "Confirm if it's for here or to go.",
    ],
    vocab_tags=["coffee", "size", "milk", "iced"],
    sentence_patterns=[
        "I'd like a ... please.",
        "Is that for here or to go?",
    ],
    session_goal="Practice ordering a coffee and asking for customizations.",
)


# ─────────────────────────────────────────────────────────────────────────────
# 4. 构造 Mock Session Context
# ─────────────────────────────────────────────────────────────────────────────

MOCK_SESSION_CTX = {
    # ── 阶段控制 ────────────────────────────────────────────────────────────
    "phase": "CORE_TASK",          # 测试 CORE_TASK 阶段（词表引导）

    # ── 词汇状态 ────────────────────────────────────────────────────────────
    # new_targets: 本局待命中词汇（模拟未命中）
    "new_targets": [
        make_node(node_id=101, node_text="decaf",      depth_level=1),
        make_node(node_id=102, node_text="extra shot",  depth_level=1),
    ],
    # history_targets: 之前命中过的旧词（模拟已命中）
    "history_targets": [
        make_node(node_id=201, node_text="iced latte",  depth_level=1),
    ],
    # 模拟已命中词表中的 "iced latte"
    # 注意: session_hits 会在调用 build_prompts 时以参数传入

    # ── 当前事件（EVENT_EXTENSION 阶段会使用）───────────────────────────────
    "current_event": (
        "A colleague just texted you asking to add a 'special request' to the order. "
        "Ask the barista what the best option is for this situation."
    ),

    # ── 状态机辅助字段（build_prompts 不直接使用，但存在则不影响）───────────
    "phase_turns": 0,
    "chat_score": 0.0,
    "task_score": 0.0,
}


# ─────────────────────────────────────────────────────────────────────────────
# 5. 主测试逻辑
# ─────────────────────────────────────────────────────────────────────────────

def _separator(title: str, width: int = 80) -> None:
    line = "=" * width
    print()
    print(line)
    print(f"  {title}")
    print(line)


def run_test():
    # ── 导入被测模块（使用绝对导入）─────────────────────────────────────────
    from core.dialogue_engine import build_prompts, build_evaluator_prompt

    # ── 构造 Mock User：politeness_level=1 (Professional and Polite) ────────
    mock_user = MockUser(
        politeness_level=1,
        settings={"learner_level": "Intermediate"}
    )

    # ── 模拟已命中一个词（iced latte）───────────────────────────────────────
    # 这样 unhit_targets 会显示 "decaf", "extra shot"
    # hit_targets   会显示 "iced latte"
    session_hits = {201}  # iced latte 已命中

    # ── 调用 build_prompts ─────────────────────────────────────────────────
    static_prompt, dynamic_prompt = build_prompts(
        user=mock_user,
        is_flipped=False,
        session_ctx=MOCK_SESSION_CTX,
        task_packet=MOCK_TASK_PACKET,
        session_hits=session_hits,
    )

    # ── 调用 build_evaluator_prompt ───────────────────────────────────────
    evaluator_prompt = build_evaluator_prompt(
        session_ctx=MOCK_SESSION_CTX,
        task_packet=MOCK_TASK_PACKET,
    )

    # ── 打印结果 ───────────────────────────────────────────────────────────
    _separator("1. 主LLM 静态系统 Prompt (System)", width=80)
    print(static_prompt)

    _separator("2. 主LLM 动态回合 Prompt (User/Developer)", width=80)
    print(dynamic_prompt)

    _separator("副LLM 导演评估 Prompt (System)", width=80)
    print(evaluator_prompt)

    _separator("测试完成", width=80)
    print()
    print("[OK] 所有 Prompt 生成成功，无外部数据库依赖。")
    print()
    print("-- 关键变量检查：")
    print(f"   user.politeness_level  = {mock_user.politeness_level} (Professional and Polite)")
    print(f"   task_packet.scene_prompt= {MOCK_TASK_PACKET.scene_prompt!r}")
    print(f"   task_packet.role_name   = {MOCK_TASK_PACKET.role_name!r}")
    print(f"   task_packet.learner_level = {MOCK_TASK_PACKET.learner_level!r}")
    print(f"   session_ctx.phase      = {MOCK_SESSION_CTX['phase']!r}")
    print(f"   unhit targets          = [decaf, extra shot]")
    print(f"   hit targets            = [iced latte]")
    print(f"   current_event          = {MOCK_SESSION_CTX['current_event'][:60]}...")


if __name__ == "__main__":
    run_test()
