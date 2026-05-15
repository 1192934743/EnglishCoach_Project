"""
EnglishCoach 端到端测试 - 测试数据

包含正面样本、负面样本、Mock LLM 缺陷响应等测试数据。
对应端到端测试计划第四章。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import List, Tuple, Optional


# ================= 正面样本 =================

NORMAL_REPLIES = [
    "Large, please.",
    "For here, thanks.",
    "I'd like a cappuccino.",
    "Can I pay by card?",
    "That's all, thanks.",
    "I see.",
    "Got it.",
]

RESIDUAL_GREETINGS = [
    "Thanks.",
    "Thank you!",
    "Bye.",
    "See you!",
    "You're welcome.",
    "Sounds good.",
    "Sure.",
]

CLEAR_INTENT_INPUTS = [
    "I'd like to book a table for tonight.",
    "Can I have the check, please?",
    "I'd prefer a window seat.",
    "Large, please.",
    "For here, to go.",
]

HINTS_THREE_TYPES = [
    "Got it.",           # Acknowledge
    "Large, please.",    # Respond
    "Can I get a receipt?",  # Use Target
]

HINTS_ANSWER_SIZE = [
    "Okay.",
    "Large, please.",
    "Medium, thanks.",
]

# ================= 负面样本 =================

@dataclass
class SpellingErrorCase:
    """拼写错误测试用例"""
    wrong: str
    correct: str
    expected_correction: str


SPELLING_ERRORS = [
    SpellingErrorCase("larga", "large", "缺少冠词或拼写错误"),
    SpellingErrorCase("cofee", "coffee", "拼写错误"),
    SpellingErrorCase("rastrant", "restaurant", "拼写错误"),
    SpellingErrorCase("resevation", "reservation", "拼写错误"),
]


@dataclass
class ColloquialExpressionCase:
    """口语化表达测试用例"""
    expression: str
    formal: str


COLLOQUIAL_EXPRESSIONS = [
    ColloquialExpressionCase("gimme a coffee", "give me a coffee"),
    ColloquialExpressionCase("gotta go", "have to go"),
    ColloquialExpressionCase("innit", "isn't it"),
    ColloquialExpressionCase("yeah", "yes"),
    ColloquialExpressionCase("nope", "no"),
]


CODE_SWITCHING_INPUTS = [
    "我要 a coffee",
    "这个 menu 有什么推荐的",
    "我要点这个 burger",
]


@dataclass
class GrammarErrorCase:
    """语法错误测试用例"""
    wrong: str
    correct: str
    error_type: str
    expected_correction: str


GRAMMAR_ERRORS = [
    GrammarErrorCase(
        "I want large coffee",
        "I want a large coffee",
        "缺少冠词",
        "建议添加冠词 'a'"
    ),
    GrammarErrorCase(
        "I am wanting coffee",
        "I want coffee",
        "进行时态错误",
        "want 不用进行时"
    ),
    GrammarErrorCase(
        "He go to school",
        "He goes to school",
        "主谓一致",
        "第三人称单数用 goes"
    ),
    GrammarErrorCase(
        "I have went there",
        "I have gone there",
        "过去分词错误",
        "have gone 不是 have went"
    ),
    GrammarErrorCase(
        "more better",
        "better",
        "比较级错误",
        "better 本身就是比较级"
    ),
]


EDGE_CASES = [
    ("", "empty_input"),
    ("   ", "whitespace_only"),
    ("???", "nonsense"),
    ("Hello? Hello?? Are you there?", "repeated_question"),
    ("xxxxxxxxxx", "gibberish"),
]


# ================= Mock LLM 缺陷响应 =================

LLM_DEFECT_RESPONSES: dict = {
    # ========== Hint 相关缺陷 ==========
    "hint_count_wrong": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": ["Large, please."],  # 数量错误：只有1个
        "coach_correction_cn": "",
    },
    "hint_count_too_many": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": ["Large, please.", "Medium.", "Small.", "I'll take the large."],
        "coach_correction_cn": "",
    },
    "hint_length_exceed": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": [
            "Can I get a cup of coffee with milk and sugar please, thank you very much",
            "Large, please",
            "For here, thanks"
        ],  # 第一条超过长度限制
        "coach_correction_cn": "",
    },
    "hint_type_monotone": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": ["Okay.", "Sure.", "Got it."],  # 三条都是 Acknowledge 类型
        "coach_correction_cn": "",
    },

    # ========== 翻译相关缺陷 ==========
    "translation_literal": {
        "ai_translation_cn": "什么大小？",  # 直译，不自然
        "suggested_hints_en": ["Large, please", "Medium", "Small"],
        "coach_correction_cn": "",
    },
    "translation_empty": {
        "ai_translation_cn": "",  # 翻译为空
        "suggested_hints_en": ["Large, please", "Medium", "Small"],
        "coach_correction_cn": "",
    },

    # ========== 纠错相关缺陷 ==========
    "correction_false_positive": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": ["Large, please", "Medium", "Small"],
        "coach_correction_cn": "建议用 'I'd like' 代替 'I want' 更自然",  # 误判：输入正确
    },
    "correction_missing": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": ["Large, please", "Medium", "Small"],
        "coach_correction_cn": "",  # 漏判：输入有错误但未纠错
    },
    "correction_vague": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": ["Large, please", "Medium", "Small"],
        "coach_correction_cn": "有问题",  # 纠错过于模糊
    },

    # ========== 主 LLM 回复相关缺陷 ==========
    "reply_too_long": {
        "ai_text": (
            "Of course! We have a wonderful selection of beverages today. "
            "Let me tell you about our special drinks. We have the classic latte, "
            "the refreshing iced Americano, the creamy cappuccino, the sweet "
            "caramel macchiato, and many more options to choose from. Which "
            "one would you like to have? Or perhaps you'd like to hear about "
            "our seasonal specials? We also have some delicious pastries if "
            "you're feeling hungry."
        ),
        "suggested_hints_en": ["Large latte, please.", "Iced Americano.", "What's recommended?"],
    },
    "reply_multiple_questions": {
        "ai_text": (
            "What can I get for you today? Would you like that hot or iced? "
            "And do you need any food with that?"
        ),
        "suggested_hints_en": ["Large latte, hot.", "Just coffee.", "Iced please."],
    },
    "reply_robotic": {
        "ai_text": "[SYSTEM] Hello user. This is Phase 1: ICE BREAKING. Please respond with your order. Processed: NO_ERROR",
        "suggested_hints_en": ["Large, please.", "Got it.", "Thanks."],
    },
    "reply_spoiler": {
        "ai_text": "Great choice! You can say 'Large, please' when ordering. Or try 'For here, thanks' if you're staying.",
        "suggested_hints_en": ["Thanks for the tip.", "I'll remember that.", "Got it."],
    },
    "reply_repeated": {
        "ai_text": "Large, please. Large, please.",
        "suggested_hints_en": ["Got it.", "Thanks.", "Okay."],
    },

    # ========== 场景流转相关缺陷 ==========
    "transition_false_positive": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": ["Large, please.", "Medium.", "Small."],
        "coach_correction_cn": "",
        "intent_achieved": True,
        "constraints_hit": False,  # 只有一轨满足
        "coach_ready_to_transition": True,
        "scenario_completed": True,  # 但错误地标记为完成
    },
    "transition_missing": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": ["Large, please.", "Medium.", "Small."],
        "coach_correction_cn": "",
        "intent_achieved": True,
        "constraints_hit": True,
        "coach_ready_to_transition": True,
        "scenario_completed": False,  # 应该流转但未标记
    },
    "transition_partial": {
        "ai_translation_cn": "好的。",
        "suggested_hints_en": ["Large, please.", "Medium.", "Small."],
        "coach_correction_cn": "",
        "intent_achieved": True,
        "constraints_hit": True,
        "coach_ready_to_transition": False,  # 两轨满足，coach不满足
        "scenario_completed": False,
    },

    # ========== WebSocket 相关缺陷 ==========
    "ws_disconnect": {
        "event": "ws_disconnect",
        "code": 1000,
        "reason": "Normal closure",
    },
    "ws_timeout": {
        "event": "error",
        "code": "LLM_TIMEOUT",
        "message": "The AI coach is taking too long.",
    },

    # ========== 边界条件缺陷 ==========
    "empty_input_response": {
        "ai_text": "",
        "suggested_hints_en": ["Hello?", "Are you there?"],
    },
    "json_parse_error": {
        "raw_response": "Here is my analysis: {invalid json}...",
        "fallback_used": True,
    },
}


# ================= 测试场景 Fixture =================

@dataclass
class TestScenario:
    """测试场景"""
    name: str
    topic: str
    level: str
    constraints: List[str]
    scene_desc: str
    coach_question: str
    max_turns: int = 8


TEST_SCENARIOS = {
    "coffee_ordering_beginner": TestScenario(
        name="coffee_ordering_beginner",
        topic="点咖啡",
        level="Beginner",
        constraints=["Large, please", "For here, thanks"],
        scene_desc="You are a coffee shop assistant. The user wants to order a drink.",
        coach_question="What size would you like: small, medium, or large?",
        max_turns=8,
    ),
    "restaurant_booking": TestScenario(
        name="restaurant_booking",
        topic="餐厅订位",
        level="Intermediate",
        constraints=["I'd like to book a table", "For tonight"],
        scene_desc="You are a restaurant receptionist. Help the user book a table.",
        coach_question="How many people will be dining?",
        max_turns=10,
    ),
    "payment_method": TestScenario(
        name="payment_method",
        topic="付款方式",
        level="Beginner",
        constraints=["Can I pay by card", "In cash"],
        scene_desc="You are a cashier. The user is ready to pay.",
        coach_question="How would you like to pay?",
        max_turns=5,
    ),
}


# ================= 回归测试套件 =================

# 冒烟测试：核心路径
CRITICAL_PATH_TEST_CASES = [
    "test_hint_count_fixed",
    "test_hint_length_beginner",
    "test_reply_not_robotic",
    "test_translation_not_empty",
    "test_scenario_no_cycle",
]

# 完整回归测试用例列表
FULL_REGRESSION_TEST_CASES = [
    # 主 LLM 回复质量
    "test_reply_not_robotic",
    "test_reply_length_beginner",
    "test_reply_length_intermediate",
    "test_reply_single_question",
    "test_reply_no_spoiler",
    "test_reply_no_repetition",
    # Hint 质量
    "test_hint_count_fixed",
    "test_hint_length_beginner",
    "test_hint_length_intermediate",
    "test_hint_type_diversity",
    "test_hint_no_repetition",
    "test_hint_answers_question",
    # 翻译与纠错
    "test_translation_not_empty",
    "test_translation_natural",
    "test_correction_triggered",
    "test_correction_not_false_positive",
    "test_correction_specific",
    # 场景流转
    "test_scenario_no_cycle",
    "test_scenario_isolation",
    "test_transition_three_track",
    "test_transition_partial_blocked",
    # 系统稳定性
    "test_empty_input_handling",
    "test_json_parse_error_fallback",
    # 渐进诱导
    "test_turn1_natural_start",
    "test_turn2_opportunity_created",
]


def get_nightly_test_cases() -> List[str]:
    """获取夜间测试用例（完整回归 + 长时间运行）"""
    return FULL_REGRESSION_TEST_CASES + [
        "test_memory_leak_session_trim",
        "test_websocket_disconnect_recovery",
        "test_websocket_progress_preserved",
    ]
