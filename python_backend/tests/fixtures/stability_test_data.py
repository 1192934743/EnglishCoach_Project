"""
EnglishCoach 端到端测试 - 系统稳定性测试数据

对应端到端测试计划第十三章的 Mock 测试数据。
"""
from __future__ import annotations


STABILITY_TEST_CASES = {
    # ========== 超时场景 ==========
    "timeout_scenarios": [
        {"delay": 25, "timeout": 20, "expect_timeout": True},
        {"delay": 5, "timeout": 20, "expect_timeout": False},
        {"delay": 20, "timeout": 20, "expect_timeout": True},
    ],

    # ========== JSON 解析失败场景 ==========
    "json_parse_failures": [
        {"raw": '{"text": "ok"}', "valid": True},
        {"raw": '{"text": "ok"', "valid": False},
        {"raw": "not json", "valid": False},
        {"raw": '{"text": }', "valid": False},
        {"raw": '{"text": "ok", }', "valid": False},
    ],

    # ========== 空输入场景 ==========
    "empty_inputs": [
        {"input": "", "expect_silent": True},
        {"input": "   ", "expect_silent": True},
        {"input": "\t\n", "expect_silent": True},
        {"input": "Hello", "expect_silent": False},
    ],

    # ========== 并发场景 ==========
    "concurrent_scenarios": [
        {"users": 10, "requests_per_user": 5, "total_expected": 50},
        {"users": 5, "requests_per_user": 10, "total_expected": 50},
        {"users": 100, "requests_per_user": 1, "total_expected": 100},
    ],
}


STABILITY_FALLBACK_RESPONSES = {
    "timeout": {
        "message": "The AI coach is taking a moment. Please try again.",
        "hints": [
            "Could you try again?",
            "I'm still here, take your time.",
        ]
    },
    "json_error": {
        "message": "Let me rephrase that.",
        "hints": [
            "Could you say that again?",
            "Try rephrasing your answer.",
        ]
    },
    "empty_input": {
        "message": "I didn't catch that. Could you say something?",
        "hints": [
            "Say something to continue.",
            "Try starting with a greeting.",
        ]
    },
}


# 性能基准配置
PERFORMANCE_BASELINES = {
    "memory_mb": 200,           # 启动内存上限
    "response_time_ms": 500,    # 单次响应时间上限
    "throughput_rps": 100,      # 并发吞吐量下限
    "db_query_ms": 100,         # 数据库查询时间上限
}


# 并发测试参数
CONCURRENT_TEST_PARAMS = {
    "thread_count": 5,
    "operations_per_thread": 1000,
    "expected_total": 5000,
}
