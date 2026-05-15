"""
EnglishCoach 端到端测试 - 验证规则库

包含所有质量验证规则的实现，对应端到端测试计划第五章。
"""
from __future__ import annotations

import re
from enum import Enum
from typing import List, Tuple, Optional, Set, Dict, Any


class HintType(Enum):
    """Hint 类型枚举"""
    ACKNOWLEDGE = "acknowledge"   # 类型1：确认/接受
    RESPOND = "respond"            # 类型2：回答问题/选择
    USE_TARGET = "use_target"       # 类型3：使用目标表达


# ================= ReplyQualityRules =================

class ReplyQualityRules:
    """主 LLM 回复质量验证规则"""

    @staticmethod
    def check_not_robotic(reply: str) -> Tuple[bool, str]:
        """检查不像机器人"""
        patterns = [
            r"\[SYSTEM\]",
            r"\[PHASE \d+\]",
            r"\{.*:.*\}",
            r"<\|.*\|>",
        ]
        for pattern in patterns:
            if re.search(pattern, reply, re.IGNORECASE):
                return False, f"包含模板痕迹: {pattern}"
        return True, ""

    @staticmethod
    def check_length(reply: str, level: str) -> Tuple[bool, str]:
        """检查回复长度"""
        word_count = len(reply.split())
        limits = {"Beginner": 10, "Intermediate": 15, "Advanced": 25}
        limit = limits.get(level, 15)
        if word_count > limit:
            return False, f"词数 {word_count} 超过限制 {limit}"
        return True, ""

    @staticmethod
    def check_single_question(reply: str) -> Tuple[bool, str]:
        """检查单一问题规则"""
        question_marks = reply.count("?")
        if question_marks > 1:
            return False, f"包含 {question_marks} 个问号，超过 1 个"
        return True, ""

    @staticmethod
    def check_residual_greeting_handling(reply: str) -> Tuple[bool, str]:
        """检查残留问候处理"""
        goodbye_patterns = ["goodbye", "see you", "take care", "have a nice day"]
        reply_lower = reply.lower()
        for pattern in goodbye_patterns:
            if pattern in reply_lower:
                return False, f"残留问候后继续道别: {pattern}"
        return True, ""

    @staticmethod
    def check_no_spoiler(reply: str, constraints: List[str]) -> Tuple[bool, str]:
        """检查没有泄露目标词汇"""
        reply_lower = reply.lower()
        for constraint in constraints:
            if constraint.lower() in reply_lower:
                return False, f"泄露目标词汇: {constraint}"
        return True, ""

    @staticmethod
    def check_no_repetition(reply: str, previous_replies: List[str]) -> Tuple[bool, str]:
        """检查无连续重复"""
        if previous_replies and previous_replies[-1] == reply:
            return False, f"回复与上一轮完全重复"
        return True, ""

    @staticmethod
    def check_difficulty_level(
        reply: str,
        level: str,
        advanced_vocabulary: List[str] = None
    ) -> Tuple[bool, str]:
        """
        检查词汇难度是否合适。

        Args:
            reply: 教练回复
            level: 学习者级别
            advanced_vocabulary: 高级词汇黑名单（Beginner/Intermediate 不应出现）
        """
        if level in ("Beginner", "Elementary"):
            if not advanced_vocabulary:
                return True, ""

            reply_lower = reply.lower()
            for word in advanced_vocabulary:
                if word.lower() in reply_lower:
                    return False, f"Beginner 级别使用了高级词汇: {word}"
        return True, ""


# ================= HintQualityRules =================

class HintQualityRules:
    """副 LLM Hint 生成质量验证规则"""

    @staticmethod
    def classify_hint_type(hint: str, target_words: List[str] = None) -> HintType:
        """
        根据 Hint 内容分类到对应类型。

        分类逻辑：
        - Acknowledge: 通用确认词，不针对具体问题
        - Use Target: 包含目标词汇的表达（需外部传入）
        - Respond: 针对教练问题的回答
        """
        hint_lower = hint.lower()

        # Acknowledge 模式：通用确认，不针对问题
        acknowledge_patterns = [
            r"^(i )?see($|\.|,)",
            r"^(okay|ok|got it|fair enough|sure|alright)(,?|$|\.)",
            r"^no problem",
            r"^no, thanks?",
            r"^that's (all|fine|okay)",
            r"^sounds good",
        ]

        for pattern in acknowledge_patterns:
            if re.match(pattern, hint_lower):
                return HintType.ACKNOWLEDGE

        # Use Target 模式：包含目标词汇（需外部传入 target_words）
        if target_words:
            if any(word.lower() in hint_lower for word in target_words):
                return HintType.USE_TARGET

        # Respond：其他大部分是对问题的回答
        return HintType.RESPOND

    @staticmethod
    def check_count(hints: List[str]) -> Tuple[bool, str]:
        """检查 Hint 数量"""
        if len(hints) != 3:
            return False, f"Hint 数量 {len(hints)}，应为 3"
        return True, ""

    @staticmethod
    def check_length(hints: List[str], level: str) -> Tuple[bool, str]:
        """检查 Hint 长度"""
        limits = {
            "Beginner": (2, 4),
            "Elementary": (2, 5),
            "Intermediate": (4, 6),
            "Advanced": (6, 8),
        }
        min_words, max_words = limits.get(level, (2, 8))

        issues = []
        for i, hint in enumerate(hints):
            word_count = len(hint.split())
            if not (min_words <= word_count <= max_words):
                issues.append(
                    f"Hint[{i}] '{hint}' 词数 {word_count}，应为 {min_words}-{max_words}"
                )
        if issues:
            return False, "; ".join(issues)
        return True, ""

    @staticmethod
    def check_type_diversity(
        hints: List[str],
        target_words: List[str] = None,
        coach_question: str = ""
    ) -> Tuple[bool, str]:
        """
        检查 Hint 类型多样性。

        规则：3 个 Hint 必须覆盖至少 2 种不同类型。
        理想情况：3 个 Hint 分别对应 Acknowledge / Respond / Use Target。
        """
        types = [
            HintQualityRules.classify_hint_type(h, target_words)
            for h in hints
        ]
        unique_types = set(types)

        if len(unique_types) < 2:
            type_names = [t.value for t in types]
            return False, f"3个Hint类型单一: {type_names}"

        # 理想情况：恰好3种类型各1个
        if len(unique_types) == 3:
            return True, ""

        # 次优情况：2种类型（可能缺某一类）
        type_counts = {t: types.count(t) for t in unique_types}
        return True, f"2种类型分布: {type_counts}"

    @staticmethod
    def check_no_repetition(
        hints: List[str],
        history: List[str],
        history_window: int = 3
    ) -> Tuple[bool, str]:
        """检查 Hint 不与历史重复"""
        recent_history = history[-history_window:] if history else []
        for hint in hints:
            if hint in recent_history:
                return False, f"Hint '{hint}' 与最近 {history_window} 轮历史重复"
        return True, ""

    @staticmethod
    def check_answers_question(
        hints: List[str],
        coach_question: str,
        question_keywords: List[str] = None
    ) -> Tuple[bool, str]:
        """
        检查是否有 Hint 回答教练问题。

        Args:
            hints: 提示列表
            coach_question: 教练问题
            question_keywords: 问题关键词（自动从问句提取或手动指定）
        """
        if not coach_question or "?" not in coach_question:
            return True, ""  # 不是问句，无需检查

        # 自动提取问题关键词
        if question_keywords is None:
            question_lower = coach_question.lower()
            # 常见问题模式
            if "size" in question_lower:
                question_keywords = ["small", "medium", "large", "big"]
            elif "here or to go" in question_lower or "to go" in question_lower:
                question_keywords = ["here", "go", "take"]
            elif "would you like" in question_lower or "can i get" in question_lower:
                question_keywords = ["yes", "sure", "please", "no"]
            else:
                question_keywords = []

        if not question_keywords:
            return True, ""

        # 检查是否有 Hint 回答了问题
        answered = False
        for hint in hints:
            hint_lower = hint.lower()
            for keyword in question_keywords:
                if keyword in hint_lower:
                    answered = True
                    break
            if answered:
                break

        if not answered:
            return False, f"无 Hint 回答问题: 问题关键词 {question_keywords}"
        return True, ""


# ================= TranslationQualityRules =================

class CorrectionDetail:
    """纠错详情"""
    def __init__(self, error_type: str, original: str, corrected: str, note: str = ""):
        self.error_type = error_type
        self.original = original
        self.corrected = corrected
        self.note = note


class TranslationQualityRules:
    """翻译与纠错质量验证规则"""

    @staticmethod
    def check_correction_triggered(
        user_input: str,
        correction: str,
        expected_error: str = None
    ) -> Tuple[bool, str]:
        """
        检查纠错是否正确触发。

        Args:
            user_input: 用户原始输入
            correction: 教练给出的纠错（中文描述）
            expected_error: 期望触发的错误类型（如 "缺少冠词"）

        Returns:
            (是否通过, 错误信息)
        """
        # 有明确期望错误但未触发
        if expected_error and not correction:
            return False, f"漏判错误: {expected_error} (输入: {user_input})"

        # 期望无错误但触发了
        if expected_error is None and correction:
            return False, f"误判: 输入正确但触发了纠错 '{correction}'"

        return True, ""

    @staticmethod
    def check_no_false_positive(
        user_input: str,
        correction: str,
        is_correct: bool
    ) -> Tuple[bool, str]:
        """
        检查无误判（正确表达不应触发纠错）。
        """
        if is_correct and correction:
            return False, f"误判: 正确表达 '{user_input}' 被错误纠错"
        return True, ""

    @staticmethod
    def check_correction_specificity(
        correction: str,
        min_length: int = 5
    ) -> Tuple[bool, str]:
        """
        检查纠错描述是否具体（非模糊）。

        规则：纠错描述应有实质内容，不是泛泛而谈。
        """
        if not correction:
            return True, ""

        if len(correction) < min_length:
            return False, f"纠错描述过于模糊: '{correction}' (长度 {len(correction)} < {min_length})"

        # 检查是否包含具体指导
        vague_patterns = ["有问题", "不对", "错误", "不太好", "不太对", "no"]
        is_vague = sum(1 for p in vague_patterns if p in correction.lower()) >= 2 and len(correction) < 15

        if is_vague:
            return False, f"纠错描述模糊，应给出具体说明: '{correction}'"

        return True, ""

    @staticmethod
    def check_translation_naturalness(
        translation: str,
        original: str
    ) -> Tuple[bool, str]:
        """
        检查翻译是否自然（非直译）。

        规则：
        1. 翻译长度应与原文成比例
        2. 不应有明显直译痕迹
        """
        if not translation:
            return False, "翻译为空"

        # 长度检查：中文字数应为英文词数的 1-2.5 倍
        cn_chars = len(translation)
        en_words = len(original.split())
        expected_min = en_words
        expected_max = en_words * 2.5

        if not (expected_min <= cn_chars <= expected_max * 1.5):  # 放宽到1.5倍
            return False, (
                f"翻译长度异常: {cn_chars} 中文字 vs {en_words} 英文词 "
                f"(期望 {expected_min}-{expected_max})"
            )

        # 检查直译模式
        literal_patterns = [
            (r"^什么", "直译 'what'"),
            (r"^哪个", "直译 'which'"),
            (r"是大的", "直译 'large'"),
        ]

        for pattern, reason in literal_patterns:
            if re.match(pattern, translation):
                return False, f"直译不自然: {reason}，翻译为 '{translation}'"

        return True, ""

    @staticmethod
    def validate_correction_accuracy(
        user_input: str,
        correction: str,
        ground_truth_errors: List[CorrectionDetail]
    ) -> Tuple[bool, str, List[str]]:
        """
        综合验证纠错准确性。

        Returns:
            (是否通过, 错误信息, 未覆盖的错误列表)
        """
        issues = []
        uncovered = []

        if not ground_truth_errors:
            # 无预设错误：正确输入不应纠错
            if correction:
                issues.append(f"误判: '{user_input}' 被错误纠错")
            return (len(issues) == 0, "; ".join(issues), [])

        # 有预设错误：应该触发纠错
        if not correction:
            issues.append(f"漏判: 应纠错 '{user_input}'")
            uncovered = [e.error_type for e in ground_truth_errors]
        else:
            # 检查是否覆盖了关键错误
            correction_lower = correction.lower()
            covered_errors = []

            for error in ground_truth_errors:
                if (error.original.lower() in user_input.lower() or
                    error.error_type in correction_lower):
                    covered_errors.append(error.error_type)

            if not covered_errors:
                issues.append(f"纠错未针对关键错误: {correction}")

            uncovered = [e.error_type for e in ground_truth_errors
                        if e.error_type not in covered_errors]

        return (len(issues) == 0, "; ".join(issues), uncovered)


# ================= ScenarioTransitionRules =================

class ScenarioTransitionRules:
    """微场景流转逻辑验证规则"""

    @staticmethod
    def check_no_cycle(visited: List[int], next_id: int) -> Tuple[bool, str]:
        """检查无循环流转"""
        if next_id in visited:
            return False, f"检测到循环流转: scenario_id={next_id} 已访问"
        return True, ""

    @staticmethod
    def check_isolation(scenario: dict, transitions: List[dict]) -> Tuple[bool, str]:
        """
        检查孤立节点。

        孤立节点定义：is_exit_point=False 且无 transitions
        合法出口：is_exit_point=True 且无 transitions（正常结束）
        """
        is_exit = scenario.get("is_exit_point", False)
        has_transitions = len(transitions) > 0

        if not has_transitions and not is_exit:
            return False, f"孤立节点: {scenario.get('scenario_code', 'unknown')} 无transitions且非出口"
        return True, ""

    @staticmethod
    def check_transition_conditions(
        intent_achieved: bool,
        constraints_hit: bool,
        coach_ready: bool
    ) -> Tuple[bool, str]:
        """
        检查流转条件（三轨全部满足才流转）。

        边界值说明：
        - 三轨全部 True → 流转
        - 两轨 True（如 intent + constraints，但 coach=False）→ 不流转
        - 一轨 True → 不流转
        - 零轨 True → 不流转
        """
        if not (intent_achieved and constraints_hit and coach_ready):
            return False, (
                f"三轨未全部满足，不应流转: "
                f"intent={intent_achieved}, constraints={constraints_hit}, coach={coach_ready}"
            )
        return True, ""

    @staticmethod
    def check_force_transition(
        turn_count: int,
        max_turns: int,
        should_transition: bool
    ) -> Tuple[bool, str]:
        """
        检查超限强制通关。

        当 turn_count >= max_turns 时，应强制流转。
        """
        if turn_count >= max_turns and not should_transition:
            return False, f"轮数超限（{turn_count}>={max_turns}），应强制流转但未流转"
        return True, ""


# ================= WebSocket 状态规则 =================

class WebSocketStateRules:
    """WebSocket 连接状态验证规则"""

    @staticmethod
    def check_session_context_preserved(
        before: dict,
        after: dict,
        preserved_keys: List[str] = None
    ) -> Tuple[bool, str]:
        """
        检查重连后会话上下文是否保留。

        Args:
            before: 断连前上下文
            after: 重连后上下文
            preserved_keys: 必须保留的键列表
        """
        if preserved_keys is None:
            preserved_keys = [
                "user_id", "topic_id", "session_id",
                "turn_count_in_scenario", "task_score", "chat_score",
                "phase", "_constraint_hits"
            ]

        missing_keys = []
        for key in preserved_keys:
            if key not in after:
                missing_keys.append(key)
            elif before.get(key) != after.get(key):
                missing_keys.append(f"{key} (值改变: {before.get(key)} -> {after.get(key)})")

        if missing_keys:
            return False, f"上下文未保留: {missing_keys}"
        return True, ""

    @staticmethod
    def check_progress_integrity(
        before: dict,
        after: dict
    ) -> Tuple[bool, str]:
        """
        检查断连前后进度一致性。

        验证项：turn_count、intent_achieved、constraints_hit 等
        """
        # turn_count 应该保持或略有增加（取决于实现）
        before_turn = before.get("turn_count_in_scenario", 0)
        after_turn = after.get("turn_count_in_scenario", 0)

        if after_turn < before_turn:
            return False, f"turn_count 减少: {before_turn} -> {after_turn}"

        # 分数应该保持
        before_score = before.get("task_score", 0)
        after_score = after.get("task_score", 0)

        if after_score < before_score:
            return False, f"task_score 减少: {before_score} -> {after_score}"

        return True, ""
