"""
EnglishCoach 端到端测试 - 翻译与纠错质量测试

对应端到端测试计划 Q4.x 测试用例。
"""
import pytest

from tests.quality_rules import TranslationQualityRules, CorrectionDetail
from tests.fixtures.test_data import (
    LLM_DEFECT_RESPONSES,
    GRAMMAR_ERRORS,
    SPELLING_ERRORS,
)


class TestTranslationQuality:
    """翻译质量测试"""

    # ========== Q4.1 翻译不自然 ==========

    @pytest.mark.unit
    def test_translation_natural(self):
        """自然翻译应通过"""
        translation = "好的，请问需要大杯、中杯还是小杯？"
        original = "What size would you like: small, medium, or large?"
        passed, error = TranslationQualityRules.check_translation_naturalness(
            translation, original
        )
        assert passed, f"自然翻译应通过: {error}"

    @pytest.mark.unit
    def test_translation_literal(self):
        """直译应被检测"""
        translation = "什么大小？"
        original = "What size?"
        passed, error = TranslationQualityRules.check_translation_naturalness(
            translation, original
        )
        assert not passed, "直译应被检测"

    @pytest.mark.unit
    def test_translation_empty(self):
        """空翻译应被检测"""
        translation = ""
        original = "Hello!"
        passed, error = TranslationQualityRules.check_translation_naturalness(
            translation, original
        )
        assert not passed, "空翻译应被检测"

    @pytest.mark.unit
    def test_translation_length_abnormal(self):
        """长度异常的翻译应被检测"""
        # 翻译过短
        translation = "好"
        original = "What size would you like: small, medium, or large?"
        passed, error = TranslationQualityRules.check_translation_naturalness(
            translation, original
        )
        assert not passed, "翻译过短应被检测"

    # ========== Q4.2-Q4.5 纠错质量 ==========

    @pytest.mark.unit
    def test_correction_triggered_with_expected_error(self):
        """有错误时应触发纠错"""
        user_input = "I want large coffee"
        correction = "建议添加冠词 'a'"
        expected_error = "缺少冠词"

        passed, error = TranslationQualityRules.check_correction_triggered(
            user_input, correction, expected_error
        )
        assert passed, f"应有纠错: {error}"

    @pytest.mark.unit
    def test_correction_missing(self):
        """有错误但未纠错应被检测"""
        user_input = "I want large coffee"
        correction = ""  # 未纠错
        expected_error = "缺少冠词"

        passed, error = TranslationQualityRules.check_correction_triggered(
            user_input, correction, expected_error
        )
        assert not passed, "漏判错误应被检测"

    @pytest.mark.unit
    def test_correction_not_triggered_correct_input(self):
        """正确表达不应触发纠错"""
        user_input = "I'd like a coffee, please."
        correction = ""  # 未纠错，正确

        passed, error = TranslationQualityRules.check_correction_triggered(
            user_input, correction, None
        )
        assert passed, f"正确表达不应纠错: {error}"

    @pytest.mark.unit
    def test_false_positive(self):
        """误判应被检测"""
        user_input = "I'd like a coffee, please."
        correction = "建议用 'want' 更自然"  # 误判：正确表达被纠错
        is_correct = True

        passed, error = TranslationQualityRules.check_no_false_positive(
            user_input, correction, is_correct
        )
        assert not passed, "误判应被检测"

    @pytest.mark.unit
    def test_correction_specific(self):
        """具体纠错应通过"""
        correction = "建议用 'a large coffee' 代替 'large coffee'"
        passed, error = TranslationQualityRules.check_correction_specificity(correction)
        assert passed, f"具体纠错应通过: {error}"

    @pytest.mark.unit
    def test_correction_vague(self):
        """模糊纠错应被检测"""
        correction = "有问题"  # 过于模糊
        passed, error = TranslationQualityRules.check_correction_specificity(correction)
        assert not passed, "模糊纠错应被检测"

    @pytest.mark.unit
    def test_correction_empty(self):
        """空纠错应通过"""
        correction = ""
        passed, error = TranslationQualityRules.check_correction_specificity(correction)
        assert passed, "空纠错应通过"

    # ========== 综合纠错准确性验证 ==========

    @pytest.mark.unit
    def test_validate_correction_accuracy_correct_input(self):
        """正确输入验证"""
        user_input = "I'd like a coffee, please."
        correction = ""  # 正确表达，无纠错
        ground_truth_errors = []

        passed, error, uncovered = TranslationQualityRules.validate_correction_accuracy(
            user_input, correction, ground_truth_errors
        )
        assert passed, f"正确输入验证应通过: {error}"

    @pytest.mark.unit
    def test_validate_correction_accuracy_missing(self):
        """漏判验证"""
        user_input = "I want large coffee"
        correction = ""  # 漏判
        ground_truth_errors = [
            CorrectionDetail("缺少冠词", "large coffee", "a large coffee")
        ]

        passed, error, uncovered = TranslationQualityRules.validate_correction_accuracy(
            user_input, correction, ground_truth_errors
        )
        assert not passed, "漏判应被检测"
        assert "缺少冠词" in uncovered, "应标记未覆盖的错误"

    @pytest.mark.unit
    def test_validate_correction_accuracy_partial(self):
        """部分纠错验证"""
        user_input = "I want large coffee"
        correction = "缺少冠词"  # 纠错但不具体
        ground_truth_errors = [
            CorrectionDetail("缺少冠词", "large coffee", "a large coffee")
        ]

        passed, error, uncovered = TranslationQualityRules.validate_correction_accuracy(
            user_input, correction, ground_truth_errors
        )
        # 有纠错但可能不完整或不够具体


# ================= Mock 缺陷测试 =================

class TestTranslationWithMockDefects:
    """使用 Mock 缺陷数据测试翻译与纠错"""

    @pytest.mark.unit
    def test_defect_translation_literal(self):
        """直译缺陷应被检测"""
        defect_data = LLM_DEFECT_RESPONSES["translation_literal"]
        translation = defect_data["ai_translation_cn"]
        original = "What size would you like?"

        passed, error = TranslationQualityRules.check_translation_naturalness(
            translation, original
        )
        assert not passed, "直译应被检测"

    @pytest.mark.unit
    def test_defect_translation_empty(self):
        """空翻译缺陷应被检测"""
        defect_data = LLM_DEFECT_RESPONSES["translation_empty"]
        translation = defect_data["ai_translation_cn"]
        original = "Hello!"

        passed, error = TranslationQualityRules.check_translation_naturalness(
            translation, original
        )
        assert not passed, "空翻译应被检测"

    @pytest.mark.unit
    def test_defect_correction_false_positive(self):
        """误判缺陷应被检测"""
        defect_data = LLM_DEFECT_RESPONSES["correction_false_positive"]
        user_input = "I'd like a coffee, please."
        correction = defect_data["coach_correction_cn"]

        passed, error = TranslationQualityRules.check_no_false_positive(
            user_input, correction, is_correct=True
        )
        assert not passed, "误判应被检测"

    @pytest.mark.unit
    def test_defect_correction_missing(self):
        """漏判缺陷应被检测"""
        defect_data = LLM_DEFECT_RESPONSES["correction_missing"]
        user_input = "I want large coffee"  # 有错误
        correction = defect_data["coach_correction_cn"]  # 空

        passed, error = TranslationQualityRules.check_correction_triggered(
            user_input, correction, expected_error="缺少冠词"
        )
        assert not passed, "漏判应被检测"

    @pytest.mark.unit
    def test_defect_correction_vague(self):
        """模糊纠错缺陷应被检测"""
        defect_data = LLM_DEFECT_RESPONSES["correction_vague"]
        correction = defect_data["coach_correction_cn"]

        passed, error = TranslationQualityRules.check_correction_specificity(correction)
        assert not passed, "模糊纠错应被检测"


# ================= 语法错误测试 =================

class TestGrammarErrors:
    """语法错误测试"""

    @pytest.mark.unit
    @pytest.mark.parametrize("case", GRAMMAR_ERRORS, ids=lambda c: c.wrong)
    def test_grammar_error_correction(self, case: GrammarErrorCase):
        """语法错误应有纠错"""
        # 模拟教练对语法错误的反应
        correction = f"建议: {case.correct}"

        passed, error = TranslationQualityRules.check_correction_triggered(
            case.wrong, correction, case.error_type
        )
        assert passed, f"语法错误应有纠错: {case.wrong} - {error}"

    @pytest.mark.unit
    def test_grammar_error_missing_article(self):
        """缺少冠词"""
        user_input = "I want large coffee"
        correction = "建议添加冠词 'a'"
        expected_error = "缺少冠词"

        passed, error = TranslationQualityRules.check_correction_triggered(
            user_input, correction, expected_error
        )
        assert passed, f"缺少冠词应有纠错: {error}"

    @pytest.mark.unit
    def test_grammar_error_wrong_tense(self):
        """进行时态错误"""
        user_input = "I am wanting coffee"
        correction = "'want' 不用进行时"
        expected_error = "进行时态错误"

        passed, error = TranslationQualityRules.check_correction_triggered(
            user_input, correction, expected_error
        )
        assert passed, f"进行时态错误应有纠错: {error}"

    @pytest.mark.unit
    def test_grammar_error_subject_verb(self):
        """主谓一致错误"""
        user_input = "He go to school"
        correction = "第三人称单数用 'goes'"
        expected_error = "主谓一致"

        passed, error = TranslationQualityRules.check_correction_triggered(
            user_input, correction, expected_error
        )
        assert passed, f"主谓一致错误应有纠错: {error}"


# ================= 拼写错误测试 =================

class TestSpellingErrors:
    """拼写错误测试"""

    @pytest.mark.unit
    @pytest.mark.parametrize("case", SPELLING_ERRORS, ids=lambda c: c.wrong)
    def test_spelling_error_correction(self, case):
        """拼写错误应有纠错"""
        correction = case.expected_correction

        passed, error = TranslationQualityRules.check_correction_triggered(
            case.wrong, correction, "拼写错误"
        )
        assert passed, f"拼写错误应有纠错: {case.wrong} - {error}"
