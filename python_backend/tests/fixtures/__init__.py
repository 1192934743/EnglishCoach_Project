"""测试数据 fixtures"""
from tests.fixtures.test_data import (
    # 正面样本
    NORMAL_REPLIES,
    RESIDUAL_GREETINGS,
    CLEAR_INTENT_INPUTS,
    HINTS_THREE_TYPES,
    HINTS_ANSWER_SIZE,
    # 负面样本
    SPELLING_ERRORS,
    COLLOQUIAL_EXPRESSIONS,
    CODE_SWITCHING_INPUTS,
    GRAMMAR_ERRORS,
    EDGE_CASES,
    # Mock 缺陷
    LLM_DEFECT_RESPONSES,
    # 测试场景
    TEST_SCENARIOS,
    # 回归套件
    CRITICAL_PATH_TEST_CASES,
    FULL_REGRESSION_TEST_CASES,
    get_nightly_test_cases,
)

__all__ = [
    # 正面样本
    "NORMAL_REPLIES",
    "RESIDUAL_GREETINGS",
    "CLEAR_INTENT_INPUTS",
    "HINTS_THREE_TYPES",
    "HINTS_ANSWER_SIZE",
    # 负面样本
    "SPELLING_ERRORS",
    "COLLOQUIAL_EXPRESSIONS",
    "CODE_SWITCHING_INPUTS",
    "GRAMMAR_ERRORS",
    "EDGE_CASES",
    # Mock 缺陷
    "LLM_DEFECT_RESPONSES",
    # 测试场景
    "TEST_SCENARIOS",
    # 回归套件
    "CRITICAL_PATH_TEST_CASES",
    "FULL_REGRESSION_TEST_CASES",
    "get_nightly_test_cases",
]
