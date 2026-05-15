"""
EnglishCoach 端到端测试 - Hint 质量测试

对应端到端测试计划 Q3.x 测试用例。
"""
import pytest

from tests.quality_rules import HintQualityRules, HintType
from tests.fixtures.test_data import (
    LLM_DEFECT_RESPONSES,
    HINTS_THREE_TYPES,
    HINTS_ANSWER_SIZE,
)


class TestHintQuality:
    """Hint 质量测试"""

    # ========== Q3.1 Hint 数量固定 ==========

    @pytest.mark.critical_path
    @pytest.mark.unit
    def test_hint_count_correct(self):
        """正确的 3 个 Hint 应通过"""
        hints = ["Got it.", "Large, please.", "Can I pay by card?"]
        passed, error = HintQualityRules.check_count(hints)
        assert passed, f"3个Hint应通过: {error}"

    @pytest.mark.unit
    def test_hint_count_too_few(self):
        """只有 1 个 Hint 应被检测"""
        hints = ["Large, please."]
        passed, error = HintQualityRules.check_count(hints)
        assert not passed, "Hint数量不足应被检测"

    @pytest.mark.unit
    def test_hint_count_too_many(self):
        """超过 3 个 Hint 应被检测"""
        hints = ["Large, please.", "Medium.", "Small.", "I'll take the large."]
        passed, error = HintQualityRules.check_count(hints)
        assert not passed, "Hint数量过多应被检测"

    @pytest.mark.unit
    def test_hint_count_empty(self):
        """空 Hint 列表应被检测"""
        hints = []
        passed, error = HintQualityRules.check_count(hints)
        assert not passed, "空Hint列表应被检测"

    # ========== Q3.2-Q3.4 Hint 长度 ==========

    @pytest.mark.critical_path
    @pytest.mark.unit
    def test_hint_length_beginner_correct(self):
        """Beginner 级别正确长度应通过"""
        hints = ["Large, please.", "Medium, thanks.", "Small, thanks."]  # 2词
        passed, error = HintQualityRules.check_length(hints, "Beginner")
        assert passed, f"Beginner Hint长度应正确: {error}"

    @pytest.mark.critical_path
    @pytest.mark.unit
    def test_hint_length_beginner_exceed(self):
        """Beginner 级别超长 Hint 应被检测"""
        hints = [
            "Can I get a cup of coffee with milk please",
            "Large, please",
            "For here"
        ]  # 第一条6词，超出2-4词限制
        passed, error = HintQualityRules.check_length(hints, "Beginner")
        assert not passed, "Beginner Hint超长应被检测"

    @pytest.mark.unit
    def test_hint_length_intermediate_correct(self):
        """Intermediate 级别正确长度应通过"""
        hints = ["Large, please, thank you.", "I would like a coffee.", "Can I pay by card?"]  # 4-6词
        passed, error = HintQualityRules.check_length(hints, "Intermediate")
        assert passed, f"Intermediate Hint长度应正确: {error}"

    @pytest.mark.unit
    def test_hint_length_advanced_correct(self):
        """Advanced 级别正确长度应通过"""
        hints = ["I would like to order a large cappuccino, please."]
        passed, error = HintQualityRules.check_length([h for h in hints], "Advanced")
        # Advanced 允许更长，但单条提示应在限制内

    # ========== Q3.5 Hint 类型多样性 ==========

    @pytest.mark.critical_path
    @pytest.mark.unit
    def test_hint_type_three_diverse(self):
        """3 种不同类型的 Hint 应通过"""
        hints = ["Got it.", "Large, please.", "Can I pay by card?"]
        target_words = ["receipt", "card", "pay"]
        passed, error = HintQualityRules.check_type_diversity(hints, target_words)
        assert passed, f"3种类型应通过: {error}"

    @pytest.mark.unit
    def test_hint_type_monotone_acknowledge(self):
        """全是 Acknowledge 类型应被检测"""
        hints = ["Okay.", "Sure.", "Got it."]
        passed, error = HintQualityRules.check_type_diversity(hints)
        assert not passed, "类型单一应被检测"

    @pytest.mark.unit
    def test_hint_type_two_diverse(self):
        """2 种不同类型应通过（次优情况）"""
        hints = ["Okay.", "Large, please.", "Medium."]
        passed, error = HintQualityRules.check_type_diversity(hints)
        assert passed, f"2种类型应通过: {error}"

    @pytest.mark.unit
    def test_hint_classify_acknowledge(self):
        """Acknowledge 类型应正确分类"""
        acknowledge_hints = ["I see.", "Okay.", "Got it.", "Fair enough.", "Sure."]
        for hint in acknowledge_hints:
            hint_type = HintQualityRules.classify_hint_type(hint)
            assert hint_type == HintType.ACKNOWLEDGE, f"'{hint}' 应分类为 Acknowledge"

    @pytest.mark.unit
    def test_hint_classify_respond(self):
        """Respond 类型应正确分类"""
        respond_hints = ["Large, please.", "Medium.", "For here, thanks."]
        for hint in respond_hints:
            hint_type = HintQualityRules.classify_hint_type(hint)
            assert hint_type == HintType.RESPOND, f"'{hint}' 应分类为 Respond"

    @pytest.mark.unit
    def test_hint_classify_use_target(self):
        """Use Target 类型应正确分类"""
        hint = "Can I get a receipt?"
        target_words = ["receipt"]
        hint_type = HintQualityRules.classify_hint_type(hint, target_words)
        assert hint_type == HintType.USE_TARGET, f"'{hint}' 应分类为 Use Target"

    # ========== Q3.6 Hint 不与历史重复 ==========

    @pytest.mark.unit
    def test_hint_no_repetition_no_history(self):
        """无历史记录时应通过"""
        hints = ["Large, please."]
        passed, error = HintQualityRules.check_no_repetition(hints, [])
        assert passed, f"无历史应通过: {error}"

    @pytest.mark.unit
    def test_hint_no_repetition_different(self):
        """与历史不同应通过"""
        hints = ["Large, please."]
        history = ["Medium.", "Small."]
        passed, error = HintQualityRules.check_no_repetition(hints, history)
        assert passed, f"不同应通过: {error}"

    @pytest.mark.unit
    def test_hint_no_repetition_same(self):
        """与最近历史重复应被检测"""
        hints = ["Large, please."]
        history = ["Got it.", "Large, please.", "For here."]  # 最近1条重复
        passed, error = HintQualityRules.check_no_repetition(hints, history)
        assert not passed, "与最近历史重复应被检测"

    @pytest.mark.unit
    def test_hint_no_repetition_window(self):
        """检查历史窗口"""
        hints = ["Extra large, please."]
        # 最近3轮内无重复
        history = ["Large, please.", "Medium.", "For here.", "That's all."]
        passed, error = HintQualityRules.check_no_repetition(hints, history, history_window=3)
        assert passed, f"最近3轮无重复应通过: {error}"

    # ========== Q3.7 Hint 回答教练问题 ==========

    @pytest.mark.unit
    def test_hint_answers_question_size(self):
        """尺寸问题应有 Hint 回答"""
        hints = ["Large, please.", "Medium.", "Small."]
        coach_question = "What size would you like: small, medium, or large?"
        passed, error = HintQualityRules.check_answers_question(hints, coach_question)
        assert passed, f"应有Hint回答尺寸问题: {error}"

    @pytest.mark.unit
    def test_hint_not_answering_question(self):
        """无 Hint 回答问题应被检测"""
        hints = ["I see.", "Got it.", "Okay."]
        coach_question = "What size would you like?"
        passed, error = HintQualityRules.check_answers_question(hints, coach_question)
        assert not passed, "无Hint回答问题应被检测"

    @pytest.mark.unit
    def test_hint_not_a_question(self):
        """非问句不应检查"""
        hints = ["Large, please."]
        coach_question = "Here's our menu."
        passed, error = HintQualityRules.check_answers_question(hints, coach_question)
        assert passed, f"非问句应跳过检查: {error}"

    # ========== Q3.8 Hint 类型2举例错误 ==========

    @pytest.mark.unit
    def test_hint_respond_to_go_question(self):
        """堂食/外带问题应有 Hint 回答"""
        hints = ["For here, thanks.", "To go, please.", "Got it."]
        coach_question = "For here or to go?"
        passed, error = HintQualityRules.check_answers_question(
            hints, coach_question,
            question_keywords=["here", "go", "take"]
        )
        assert passed, f"应有Hint回答堂食/外带问题: {error}"


# ================= Mock 缺陷测试 =================

class TestHintWithMockDefects:
    """使用 Mock 缺陷数据测试 Hint 质量"""

    @pytest.mark.unit
    def test_defect_hint_count_wrong(self):
        """Hint 数量错误缺陷应被检测"""
        defect_data = LLM_DEFECT_RESPONSES["hint_count_wrong"]
        hints = defect_data["suggested_hints_en"]

        passed, error = HintQualityRules.check_count(hints)
        assert not passed, "Hint数量错误应被检测"

    @pytest.mark.unit
    def test_defect_hint_count_too_many(self):
        """Hint 数量过多缺陷应被检测"""
        defect_data = LLM_DEFECT_RESPONSES["hint_count_too_many"]
        hints = defect_data["suggested_hints_en"]

        passed, error = HintQualityRules.check_count(hints)
        assert not passed, "Hint数量过多应被检测"

    @pytest.mark.unit
    def test_defect_hint_length_exceed(self):
        """Hint 长度超标缺陷应被检测"""
        defect_data = LLM_DEFECT_RESPONSES["hint_length_exceed"]
        hints = defect_data["suggested_hints_en"]

        passed, error = HintQualityRules.check_length(hints, "Beginner")
        assert not passed, "Hint长度超标应被检测"

    @pytest.mark.unit
    def test_defect_hint_type_monotone(self):
        """Hint 类型单一缺陷应被检测"""
        defect_data = LLM_DEFECT_RESPONSES["hint_type_monotone"]
        hints = defect_data["suggested_hints_en"]

        passed, error = HintQualityRules.check_type_diversity(hints)
        assert not passed, "Hint类型单一应被检测"


# ================= 集成测试 =================

class TestHintQualityIntegration:
    """Hint 质量集成测试"""

    @pytest.mark.e2e
    def test_complete_hint_validation(self):
        """完整 Hint 验证流程"""
        # 2种类型（次优情况，但应通过）
        hints = ["Got it.", "Large, please.", "Medium, thanks."]
        level = "Beginner"
        target_words = []  # 不检查 Use Target
        coach_question = "What size would you like?"
        history = ["Small, thanks.", "Medium, please."]

        # 1. 检查数量
        passed, _ = HintQualityRules.check_count(hints)
        assert passed, f"Hint数量检查失败"

        # 2. 检查长度
        passed, _ = HintQualityRules.check_length(hints, level)
        assert passed, f"Hint长度检查失败: {passed}"

        # 3. 检查类型多样性（2种Acknowledge+Respond，应通过）
        passed, _ = HintQualityRules.check_type_diversity(hints, target_words)
        assert passed, f"Hint类型多样性检查失败: {passed}"

        # 4. 检查不与历史重复
        passed, _ = HintQualityRules.check_no_repetition(hints, history)
        assert passed, f"Hint历史重复检查失败: {passed}"

        # 5. 检查回答问题
        passed, _ = HintQualityRules.check_answers_question(hints, coach_question)
        assert passed, f"Hint回答问题检查失败: {passed}"
