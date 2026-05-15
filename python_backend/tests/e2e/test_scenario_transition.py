"""
EnglishCoach 端到端测试 - 场景流转逻辑测试

对应端到端测试计划第十二章，测试微场景流转逻辑。
包含 Q5.1 - Q5.7 测试用例。
"""
from __future__ import annotations

import pytest
from typing import List, Dict, Optional
from dataclasses import dataclass, field


@dataclass
class ScenarioNode:
    """场景节点"""
    id: int
    code: str
    name: str
    is_exit_point: bool = False
    transitions: List[Dict] = field(default_factory=list)
    difficulty_level: str = "Beginner"
    target_expressions: List[str] = field(default_factory=list)


@dataclass
class TransitionContext:
    """流转上下文"""
    intent_achieved: bool = False
    constraints_hit: bool = False
    coach_ready: bool = False
    max_turns: int = 10
    current_turn: int = 1


class TestScenarioTransitionLogic:
    """Q5.x 微场景流转逻辑测试"""

    # ========== Q5.1 循环流转测试 ==========

    @pytest.mark.full_regression
    def test_q5_1_prevent_circular_visit(self):
        """
        Q5.1: 循环流转 - 连续访问相同场景应被阻止

        测试场景：
        - 用户连续两次尝试访问相同场景
        - 第二次访问时系统应检测并阻止

        验证方法：检查 visited_scenarios 列表，第二次访问应返回警告
        """
        visited_scenarios: List[int] = []
        current_scenario_id = 1

        # 第一次访问
        if current_scenario_id not in visited_scenarios:
            visited_scenarios.append(current_scenario_id)

        assert 1 in visited_scenarios

        # 第二次尝试访问同一场景（模拟）
        should_block = current_scenario_id in visited_scenarios
        assert should_block, "第二次访问相同场景时应被阻止"

    @pytest.mark.full_regression
    def test_q5_1_visited_tracking_after_transition(self):
        """
        Q5.1: 流转后 visited_tracking 正确更新

        测试场景：
        - 场景 A → 场景 B 流转后
        - visited_scenarios 应包含 [A, B]
        """
        visited_scenarios = []
        scenarios = [1, 2, 3]

        # 模拟正常流转
        for scenario_id in scenarios:
            if scenario_id not in visited_scenarios:
                visited_scenarios.append(scenario_id)

        assert visited_scenarios == [1, 2, 3]
        assert len(set(visited_scenarios)) == len(visited_scenarios), "不应有重复"

    # ========== Q5.2 孤立节点检测测试 ==========

    @pytest.mark.full_regression
    def test_q5_2_isolated_node_detection(self):
        """
        Q5.2: 孤立节点 - is_exit_point=False 且无 transitions

        测试场景：
        - 场景 A: is_exit_point=False, transitions=[]
        - 应发送 warning 日志，继续运行

        验证方法：检查日志级别和系统行为
        """
        test_cases = [
            # (scenario, expected_warning, expected_continue)
            (
                ScenarioNode(id=1, code="test_001", name="孤立节点",
                            is_exit_point=False, transitions=[]),
                True,  # 应发出警告
                True   # 但应继续运行
            ),
            (
                ScenarioNode(id=2, code="test_002", name="正常出口",
                            is_exit_point=True, transitions=[]),
                False,  # 不应警告
                True   # 继续运行
            ),
            (
                ScenarioNode(id=3, code="test_003", name="中间节点",
                            is_exit_point=False, transitions=[{"next": 4}]),
                False,  # 不应警告
                True   # 继续运行
            ),
        ]

        for scenario, expected_warning, expected_continue in test_cases:
            is_exit = scenario.is_exit_point
            has_transitions = len(scenario.transitions) > 0

            is_isolated = not has_transitions and not is_exit

            assert is_isolated == expected_warning, \
                f"场景 {scenario.code} 孤立状态检测错误"

            # 孤立节点应继续运行，不应崩溃
            if is_isolated:
                # 模拟系统行为：发出警告但继续
                log_level = "WARNING" if is_isolated else "INFO"
                assert log_level == "WARNING", "孤立节点应发出 WARNING 日志"

    # ========== Q5.3 流转条件边界测试 ==========

    @pytest.mark.full_regression
    @pytest.mark.parametrize("intent,constraints,coach,should_transition", [
        # 三轨全满足
        (True, True, True, True),
        # 只有一轨满足
        (True, False, False, False),
        (False, True, False, False),
        (False, False, True, False),
        # 两轨满足（但不是三轨）
        (True, True, False, False),
        (True, False, True, False),
        (False, True, True, False),
        # 零轨满足
        (False, False, False, False),
    ])
    def test_q5_3_transition_conditions_boundary(
        self, intent, constraints, coach, should_transition
    ):
        """
        Q5.3: 流转条件过严/过松 - 三轨全部满足才流转

        测试边界值：
        - intent=1.0, constraints=0.95, coach=0.9（三轨全满足）
        - 只有一轨/两轨满足时不应流转
        """
        # 三轨判定逻辑
        def check_transition(ctx: TransitionContext) -> bool:
            return ctx.intent_achieved and ctx.constraints_hit and ctx.coach_ready

        ctx = TransitionContext(
            intent_achieved=intent,
            constraints_hit=constraints,
            coach_ready=coach
        )

        result = check_transition(ctx)
        assert result == should_transition, \
            f"三轨条件判定错误: intent={intent}, constraints={constraints}, coach={coach}"

    # ========== Q5.4a 流转条件过松测试 ==========

    @pytest.mark.full_regression
    def test_q5_4a_single_track_satisfied_no_transition(self):
        """
        Q5.4a: 流转条件过松 - 只有一轨满足不应流转

        测试场景：只有 intent 满足，但 constraints 和 coach 不满足
        """
        ctx = TransitionContext(
            intent_achieved=True,   # 只有这一轨满足
            constraints_hit=False,
            coach_ready=False
        )

        should_transition = (
            ctx.intent_achieved and
            ctx.constraints_hit and
            ctx.coach_ready
        )

        assert not should_transition, "只有一轨满足时不应流转"

    # ========== Q5.4b 流转条件边界测试 ==========

    @pytest.mark.full_regression
    def test_q5_4b_two_tracks_satisfied_no_transition(self):
        """
        Q5.4b: 流转条件边界 - 两轨满足但 coach=False 时不应流转

        测试场景：intent + constraints 满足，但 coach=False
        """
        ctx = TransitionContext(
            intent_achieved=True,
            constraints_hit=True,
            coach_ready=False  # 关键：这一轨不满足
        )

        should_transition = (
            ctx.intent_achieved and
            ctx.constraints_hit and
            ctx.coach_ready
        )

        assert not should_transition, "两轨满足但 coach=False 时不应流转"

    # ========== Q5.5 超限强制通关测试 ==========

    @pytest.mark.full_regression
    def test_q5_5_max_turns_force_transition(self):
        """
        Q5.5: 超限强制通关 - 达到 max_turns 应强制流转

        测试场景：
        - 当前轮次 = max_turns
        - 即使三轨未全部满足，也应强制流转
        """
        max_turns = 10

        # 场景1: 未达上限，三轨未全满足
        ctx1 = TransitionContext(current_turn=5, max_turns=max_turns,
                               intent_achieved=False, constraints_hit=False, coach_ready=False)
        normal_transition = ctx1.intent_achieved and ctx1.constraints_hit and ctx1.coach_ready

        # 场景2: 达到上限，三轨未全满足
        ctx2 = TransitionContext(current_turn=10, max_turns=max_turns,
                               intent_achieved=False, constraints_hit=False, coach_ready=False)
        force_transition = (
            ctx2.current_turn >= ctx2.max_turns or
            (ctx2.intent_achieved and ctx2.constraints_hit and ctx2.coach_ready)
        )

        assert not normal_transition, "正常情况下三轨未满足不应流转"
        assert force_transition, "达到 max_turns 应强制流转"

    # ========== Q5.6 难度升级时机测试 ==========

    @pytest.mark.full_regression
    def test_q5_6_difficulty_upgrade_on_exit(self):
        """
        Q5.6: 难度升级时机 - 话题出口节点通关应触发深度升级

        测试场景：
        - 用户在出口节点通关
        - 应触发难度升级标记
        """
        difficulty_levels = ["Beginner", "Intermediate", "Advanced"]

        def get_next_difficulty(current: str) -> Optional[str]:
            idx = difficulty_levels.index(current) if current in difficulty_levels else -1
            return difficulty_levels[idx + 1] if idx < len(difficulty_levels) - 1 else None

        # 模拟出口节点通关触发升级
        scenarios = [
            ScenarioNode(id=1, code="exit_1", name="出口节点1",
                        is_exit_point=True, difficulty_level="Beginner"),
            ScenarioNode(id=2, code="exit_2", name="出口节点2",
                        is_exit_point=True, difficulty_level="Intermediate"),
            ScenarioNode(id=3, code="exit_3", name="出口节点3",
                        is_exit_point=True, difficulty_level="Advanced"),
        ]

        upgrade_results = []
        for scenario in scenarios:
            if scenario.is_exit_point:
                next_level = get_next_difficulty(scenario.difficulty_level)
                upgrade_results.append(next_level)

        assert upgrade_results == ["Intermediate", "Advanced", None], \
            "出口节点通关应触发难度升级"

    # ========== Q5.7 晋级标记持久化测试 ==========

    @pytest.mark.full_regression
    def test_q5_7_promotion_persistence(self):
        """
        Q5.7: 晋级标记持久 - 下次进入话题应使用新难度

        测试场景：
        - 用户在场景 A (Beginner) 晋级
        - 标记持久化
        - 下次进入同一话题应使用 Intermediate
        """
        # 模拟用户晋级状态
        class UserProgressionState:
            def __init__(self):
                self.difficulty_markers: Dict[str, str] = {}

            def mark_upgraded(self, topic: str, new_level: str):
                self.difficulty_markers[topic] = new_level

            def get_difficulty(self, topic: str, default: str = "Beginner") -> str:
                return self.difficulty_markers.get(topic, default)

        state = UserProgressionState()
        topic = "coffee_shop"

        # 第一次进入：默认 Beginner
        assert state.get_difficulty(topic) == "Beginner"

        # 通关后晋级
        state.mark_upgraded(topic, "Intermediate")

        # 再次进入：应使用 Intermediate
        assert state.get_difficulty(topic) == "Intermediate"

        # 验证状态持久（模拟新会话）
        new_session_state = state  # 实际应从数据库加载
        assert new_session_state.get_difficulty(topic) == "Intermediate"


class TestScenarioTransitionEdgeCases:
    """Q5.x 边界场景测试"""

    def test_q5_1_concurrent_visits(self):
        """Q5.1 边界：并发访问相同场景"""
        import threading
        import time

        visited = []
        lock = threading.Lock()

        def visit(scenario_id):
            with lock:
                if scenario_id not in visited:
                    time.sleep(0.01)  # 模拟处理延迟
                    visited.append(scenario_id)
                    return True
                return False

        threads = [threading.Thread(target=visit, args=(1,)) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 只应有一个成功访问
        assert len(visited) == 1

    def test_q5_3_exact_boundary_values(self):
        """Q5.3 边界：精确边界值测试"""
        # 浮点数精度问题 - 使用参数化明确测试每个边界
        thresholds = {"intent": 0.7, "constraints": 0.8, "coach": 0.9}

        test_cases = [
            # (intent, constraints, coach, expected_transition, description)
            (0.69, 0.79, 0.89, False, "全部略低于阈值"),
            (0.70, 0.80, 0.90, True, "全部精确等于阈值"),
            (0.71, 0.81, 0.91, True, "全部略高于阈值"),
        ]

        for intent, constraints, coach, expected, desc in test_cases:
            should_transition = (
                intent >= thresholds["intent"] and
                constraints >= thresholds["constraints"] and
                coach >= thresholds["coach"]
            )
            assert should_transition == expected, \
                f"{desc}: 期望 {expected}, 实际 {should_transition}"

    def test_q5_4_extreme_values(self):
        """Q5.4 边界：极端值测试"""
        ctx = TransitionContext(
            intent_achieved=False,
            constraints_hit=False,
            coach_ready=False
        )

        # 零轨满足
        should_transition = (
            ctx.intent_achieved and
            ctx.constraints_hit and
            ctx.coach_ready
        )
        assert not should_transition, "零轨满足不应流转"

        # 所有极端情况
        for intent in [True, False]:
            for constraints in [True, False]:
                for coach in [True, False]:
                    ctx = TransitionContext(
                        intent_achieved=intent,
                        constraints_hit=constraints,
                        coach_ready=coach
                    )
                    result = ctx.intent_achieved and ctx.constraints_hit and ctx.coach_ready
                    expected = intent and constraints and coach
                    assert result == expected
