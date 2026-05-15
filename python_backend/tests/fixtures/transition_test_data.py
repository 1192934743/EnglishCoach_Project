"""
EnglishCoach 端到端测试 - 场景流转测试数据

对应端到端测试计划第十二章的 Mock 测试数据。
"""
from __future__ import annotations


TRANSITION_TEST_CASES = {
    # ========== 正常流转路径 ==========
    "normal_path": {
        "scenarios": [
            {"id": 1, "code": "order_drink", "name": "点饮料",
             "is_exit_point": False, "transitions": [{"next": 2}]},
            {"id": 2, "code": "pay", "name": "付款",
             "is_exit_point": True, "transitions": []},
        ],
        "expected_transitions": [(1, 2)],
    },

    # ========== 循环流转（应阻止）==========
    "circular_attack": {
        "scenarios": [
            {"id": 1, "code": "loop_a", "name": "循环A",
             "is_exit_point": False, "transitions": [{"next": 2}]},
            {"id": 2, "code": "loop_b", "name": "循环B",
             "is_exit_point": False, "transitions": [{"next": 1}]},  # 循环
        ],
        "expected_warning": "循环流转检测",
    },

    # ========== 孤立节点（应警告但继续）==========
    "isolated_node": {
        "scenarios": [
            {"id": 1, "code": "orphan", "name": "孤立节点",
             "is_exit_point": False, "transitions": []},  # 孤立
        ],
        "expected_log_level": "WARNING",
    },

    # ========== 多出口场景 ==========
    "multi_exit": {
        "scenarios": [
            {"id": 1, "code": "main", "name": "主场景",
             "is_exit_point": False, "transitions": [{"next": 2}, {"next": 3}]},
            {"id": 2, "code": "exit_a", "name": "出口A",
             "is_exit_point": True, "transitions": []},
            {"id": 3, "code": "exit_b", "name": "出口B",
             "is_exit_point": True, "transitions": []},
        ],
        "possible_exits": [2, 3],
    },
}


TRANSITION_BOUNDARY_CASES = [
    # (intent, constraints, coach, expected_transition)
    (0.99, 0.99, 0.99, True),   # 接近1
    (1.0, 1.0, 1.0, True),      # 精确1
    (0.5, 0.5, 0.5, False),     # 全部低于阈值
    (0.9, 0.5, 0.5, False),     # 只有一轨高
    (0.9, 0.9, 0.5, False),    # 两轨高但第三轨低
]


# 难度升级路径
DIFFICULTY_UPGRADE_PATHS = [
    ("Beginner", "Intermediate"),
    ("Intermediate", "Advanced"),
    ("Advanced", None),  # 最高级别，无更高
]


# 流转阈值配置
TRANSITION_THRESHOLDS = {
    "intent": 0.7,
    "constraints": 0.8,
    "coach": 0.9,
}
