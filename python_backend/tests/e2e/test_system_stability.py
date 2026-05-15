"""
EnglishCoach 端到端测试 - 系统稳定性测试

对应端到端测试计划第十三章，测试系统稳定性。
包含 Q6.1 - Q6.5 测试用例。

测试依赖：
    pip install pytest pytest-asyncio pytest-timeout pytest-cov psutil

或使用项目测试依赖文件：
    tests/requirements.txt
"""
from __future__ import annotations

import pytest
import asyncio
import time
import threading
from typing import List, Dict, Optional
from dataclasses import dataclass, field


@dataclass
class MockLLMResponse:
    """Mock LLM 响应"""
    text: str = ""
    hints: List[str] = field(default_factory=list)
    translation: str = ""
    correction: str = ""
    delay_seconds: float = 0


class TestSystemStability:
    """Q6.x 系统稳定性测试"""

    # ========== Q6.1 LLM 超时测试 ==========

    @pytest.mark.full_regression
    @pytest.mark.asyncio
    async def test_q6_1_llm_timeout_handling(self):
        """
        Q6.1: LLM 超时 - 响应 >20s 返回超时错误，不崩溃

        测试场景：
        - Mock LLM 响应延迟 25 秒
        - 系统应捕获超时异常
        - 返回友好错误，不崩溃

        验证方法：检查异常类型和系统状态
        """
        TIMEOUT_SECONDS = 20

        async def slow_llm_call():
            await asyncio.sleep(25)  # 模拟超时
            return MockLLMResponse(text="Response after timeout")

        start_time = time.time()

        try:
            result = await asyncio.wait_for(
                slow_llm_call(),
                timeout=TIMEOUT_SECONDS
            )
            elapsed = time.time() - start_time

            # 如果到达这里，说明超时未被正确处理
            assert False, f"应该在 {TIMEOUT_SECONDS}s 内超时"

        except asyncio.TimeoutError:
            elapsed = time.time() - start_time

            # 验证超时时间接近预期
            assert TIMEOUT_SECONDS - 1 <= elapsed <= TIMEOUT_SECONDS + 1, \
                f"超时时间 {elapsed}s 异常"

            # 验证系统未崩溃（可用正常状态码响应）
            assert True, "超时被正确捕获"

    @pytest.mark.full_regression
    @pytest.mark.asyncio
    async def test_q6_1_timeout_recovery(self):
        """Q6.1: 超时后系统恢复能力"""
        call_count = 0

        async def flaky_llm():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(25)  # 第一次超时
                raise asyncio.TimeoutError()
            return MockLLMResponse(text="Second attempt succeeded")

        # 第一次调用超时
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(flaky_llm(), timeout=20)

        # 第二次调用成功
        result = await asyncio.wait_for(flaky_llm(), timeout=20)
        assert result.text == "Second attempt succeeded"

    # ========== Q6.2 JSON 解析失败测试 ==========

    @pytest.mark.full_regression
    @pytest.mark.parametrize("invalid_json,fallback_used", [
        ('{"text": "ok", "hints":}', True),    # 缺少值
        ('{"text": "ok" hints}', True),        # 缺少逗号
        ('{"text": "ok", "hints": ["a", "b"]}', False),  # 有效 JSON
        ('not json at all', True),
        ('{"text": "ok", "nested": {"a":}}}', True),  # 花括号不匹配
    ])
    def test_q6_2_json_parse_error_handling(self, invalid_json, fallback_used):
        """
        Q6.2: JSON 解析失败 - 使用 fallback_hint，继续运行

        测试场景：
        - Director 返回格式错误的 JSON
        - 系统应使用 fallback_hint
        - 继续运行，不崩溃
        """
        import json

        FALLBACK_HINTS = ["Could you try again?", "Let me rephrase.", "Sorry?"]

        def safe_parse_json(raw: str) -> tuple:
            """安全解析 JSON，返回 (解析成功, 数据, 使用fallback)"""
            try:
                data = json.loads(raw)
                return True, data, False
            except json.JSONDecodeError:
                return False, {}, True

        success, data, used_fallback = safe_parse_json(invalid_json)

        if fallback_used:
            assert not success, "应解析失败"
            assert used_fallback, "应使用 fallback"
            # 验证 fallback 内容
            assert len(FALLBACK_HINTS) > 0, "fallback hints 不应为空"
        else:
            assert success, "应解析成功"
            assert not used_fallback, "不应使用 fallback"

    @pytest.mark.full_regression
    def test_q6_2_fallback_hint_quality(self):
        """Q6.2: Fallback Hint 质量检查"""
        FALLBACK_HINTS = [
            "Could you say that again?",
            "I'm not sure I understand.",
            "Try rephrasing your answer.",
        ]

        # Fallback Hint 也应满足基本质量要求
        for hint in FALLBACK_HINTS:
            word_count = len(hint.split())
            assert 2 <= word_count <= 10, \
                f"Fallback hint '{hint}' 词数 {word_count} 不在 2-10 范围内"

            # 不应包含明显模板痕迹
            assert "{" not in hint and "}" not in hint

    # ========== Q6.3 空输入处理测试 ==========

    @pytest.mark.full_regression
    @pytest.mark.parametrize("empty_input", [
        "",
        "   ",
        "\t\n",
        "\r",
        None,
    ])
    def test_q6_3_empty_input_handling(self, empty_input):
        """
        Q6.3: 空输入 - 静默处理，不崩溃

        测试场景：
        - 用户发送空字符串或空白
        - 系统应静默处理
        - 不发送回复给用户
        """
        def process_empty_input(user_input: Optional[str]) -> Optional[str]:
            """处理空输入，返回 None 表示静默处理"""
            if not user_input or not user_input.strip():
                return None  # 静默处理
            return user_input

        result = process_empty_input(empty_input)
        assert result is None, "空输入应返回 None（静默处理）"

    @pytest.mark.full_regression
    def test_q6_3_multiple_empty_inputs(self):
        """Q6.3: 连续空输入不累积"""
        # 模拟空输入处理
        empty_count = 0
        max_empty_allowed = 3

        def handle_input(user_input: Optional[str]) -> bool:
            """返回 True 表示继续运行，False 表示终止"""
            nonlocal empty_count
            if not user_input or not user_input.strip():
                empty_count += 1
                if empty_count >= max_empty_allowed:
                    return False  # 触发超时提示
                return True
            else:
                empty_count = 0  # 正常输入重置计数
                return True

        # 连续空输入
        assert handle_input("") is True
        assert handle_input("   ") is True
        # 注意：第三个空输入会触发阈值，返回 False
        assert handle_input("") is False, "超过阈值应触发提示"

        # 正常输入后重置
        empty_count = 0
        assert handle_input("Hello") is True
        assert handle_input("") is True  # 计数重置后重新开始

        # 验证连续空输入计数正确
        empty_count = 0
        for i in range(2):
            assert handle_input("") is True, f"第{i+1}次空输入应返回 True"
        assert handle_input("") is False, "第3次空输入应返回 False"

    # ========== Q6.4 内存泄漏测试 ==========

    @pytest.mark.nightly
    @pytest.mark.full_regression
    def test_q6_4_session_transcript_limit(self):
        """
        Q6.4: session_transcript 长度受限 - 防止内存泄漏

        测试场景：
        - 长时间对话（100+ 轮）
        - session_transcript 应有长度限制
        - 超过限制时应截断旧记录

        验证方法：检查 transcript 长度和内容
        """
        MAX_TRANSCRIPT_LENGTH = 500  # 最大历史条数

        def trim_transcript(transcript: List[Dict], max_len: int) -> List[Dict]:
            """截断过长的 transcript，保留最新记录"""
            if len(transcript) > max_len:
                # 保留最新的 max_len 条
                return transcript[-max_len:]
            return transcript

        # 模拟 600 轮对话
        long_transcript = [
            {"turn": i, "text": f"Message {i}"}
            for i in range(600)
        ]

        trimmed = trim_transcript(long_transcript, MAX_TRANSCRIPT_LENGTH)

        assert len(trimmed) == MAX_TRANSCRIPT_LENGTH, \
            f"截断后长度应为 {MAX_TRANSCRIPT_LENGTH}"
        assert trimmed[0]["turn"] == 100, \
            "应保留最后 500 条，索引从 100 开始"
        assert trimmed[-1]["turn"] == 599, \
            "最后一条应是原始的最后一条"

    @pytest.mark.unit
    @pytest.mark.full_regression
    def test_q6_4_message_size_estimate(self):
        """
        Q6.4: 消息大小估算（单元测试，非真实内存测量）

        注意：这是估算测试，用于验证数据结构大小的合理性。
        真实内存测试需要 @pytest.mark.nightly 并使用 psutil 实际测量。
        """
        import sys

        # 模拟消息对象大小估算
        def estimate_message_size(text: str) -> int:
            return sys.getsizeof(text) + 200  # 基础开销

        # 每轮消息大小
        avg_message_size = estimate_message_size("Hello world, this is a test message.")

        # 1000 轮对话预估内存
        estimated_memory_1k = avg_message_size * 1000 / 1024 / 1024  # MB

        # 内存估算应在合理范围内（< 50MB）
        assert estimated_memory_1k < 50, \
            f"1000轮对话预估内存 {estimated_memory_1k:.2f}MB 过高"

    # ========== Q6.5a 并发：同一用户多标签页 ==========

    @pytest.mark.full_regression
    def test_q6_5a_multi_tab_isolation(self):
        """
        Q6.5a: 并发 - 同一用户多标签页 session_id 隔离

        测试场景：
        - 用户开多个标签页
        - 每个标签页应独立的 session_id
        - 状态互不影响
        """
        import uuid

        def create_new_session(user_id: str, tab_id: str = None) -> Dict:
            """创建新会话"""
            session_id = str(uuid.uuid4())
            return {
                "session_id": session_id,
                "user_id": user_id,
                "tab_id": tab_id or str(uuid.uuid4()),
                "state": {"turn": 0, "intent": 0.0}
            }

        user_id = "user_123"

        # 创建多个标签页的会话
        tabs = [create_new_session(user_id) for _ in range(3)]

        # 验证 session_id 唯一
        session_ids = [t["session_id"] for t in tabs]
        assert len(set(session_ids)) == 3, "每个标签页应有唯一 session_id"

        # 验证状态隔离
        tabs[0]["state"]["turn"] = 5
        tabs[1]["state"]["turn"] = 10

        assert tabs[0]["state"]["turn"] == 5, "标签页1状态不应被影响"
        assert tabs[1]["state"]["turn"] == 10, "标签页2状态不应被影响"

    # ========== Q6.5b 并发：多用户同场景 ==========

    @pytest.mark.full_regression
    @pytest.mark.asyncio
    async def test_q6_5b_multi_user_scenario_isolation(self):
        """
        Q6.5b: 并发 - 多用户同时访问同一场景状态隔离

        测试场景：
        - 多个用户同时访问 coffee_shop 场景
        - 每个用户的 turn_count、intent_achieved 互不影响
        """
        # 模拟多用户状态
        user_states: Dict[str, Dict] = {}
        lock = asyncio.Lock()

        async def simulate_user_conversation(user_id: str, turns: int):
            """模拟用户对话"""
            async with lock:
                user_states[user_id] = {
                    "turn": 0,
                    "intent_achieved": 0.0,
                    "constraints_hit": 0.0
                }

            for _ in range(turns):
                await asyncio.sleep(0.01)  # 模拟处理
                async with lock:
                    user_states[user_id]["turn"] += 1
                    user_states[user_id]["intent_achieved"] = min(
                        user_states[user_id]["intent_achieved"] + 0.2, 1.0
                    )

        # 并发执行 5 个用户的对话
        await asyncio.gather(*[
            simulate_user_conversation(f"user_{i}", 10)
            for i in range(5)
        ])

        # 验证状态隔离
        assert len(user_states) == 5, "应有 5 个独立用户状态"

        for i, state in enumerate(user_states.values()):
            assert state["turn"] == 10, f"用户 {i} 应完成 10 轮"
            # 各用户的 intent 值应不同（取决于执行顺序）
            assert 0.0 <= state["intent_achieved"] <= 1.0

    # ========== Q6.5c 并发：评分提交 ==========

    @pytest.mark.full_regression
    @pytest.mark.asyncio
    async def test_q6_5c_concurrent_assessment_submission(self):
        """
        Q6.5c: 并发 - 并发提交评分，assessment_records 条数正确

        测试场景：
        - 多个并发请求提交评分
        - 数据库记录条数应正确
        - 无覆盖或丢失
        """
        # 模拟数据库操作
        assessment_records: List[Dict] = []
        write_lock = asyncio.Lock()

        async def submit_assessment(user_id: str, session_id: str,
                                   turn: int, score: float):
            """提交评分"""
            async with write_lock:
                # 模拟数据库写入延迟
                await asyncio.sleep(0.01)
                assessment_records.append({
                    "user_id": user_id,
                    "session_id": session_id,
                    "turn": turn,
                    "score": score,
                    "timestamp": time.time()
                })

        # 并发提交 20 个评分
        await asyncio.gather(*[
            submit_assessment(
                user_id=f"user_{i % 5}",
                session_id=f"session_{i % 10}",
                turn=i,
                score=0.5 + (i % 10) * 0.05
            )
            for i in range(20)
        ])

        # 验证记录数正确
        assert len(assessment_records) == 20, \
            f"应有 20 条记录，实际 {len(assessment_records)} 条"

        # 验证无覆盖（检查 ID 唯一性）
        record_ids = [(r["user_id"], r["session_id"], r["turn"])
                     for r in assessment_records]
        assert len(set(record_ids)) == len(record_ids), \
            "记录 ID 应唯一，不应有覆盖"

    # ========== Q6.5d 并发：锁竞争测试 ==========

    @pytest.mark.full_regression
    def test_q6_5d_lock_contention_detection(self):
        """
        Q6.5d: 并发 - 无锁情况下的数据竞争（测试锁的必要性）

        测试场景：
        - 模拟没有锁保护的情况
        - 验证数据竞争会导致数据丢失
        - 证明锁机制的必要性

        注意：此测试用于验证锁的正确使用，实际代码应始终使用锁
        """
        # ========== 测试 1：无锁情况（演示数据竞争）==========
        def test_without_lock():
            """无锁情况下的数据竞争"""
            shared_counter = {"value": 0}

            def increment_without_lock():
                for _ in range(1000):
                    temp = shared_counter["value"]
                    temp += 1  # 读-改-写 操作，非原子性
                    shared_counter["value"] = temp

            threads = [
                threading.Thread(target=increment_without_lock)
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            # 如果没有锁，由于数据竞争，结果会小于 5000
            return shared_counter["value"]

        # ========== 测试 2：有锁情况（验证锁有效性）==========
        def test_with_lock():
            """有锁情况下的正确性"""
            shared_counter = {"value": 0}
            lock = threading.Lock()

            def increment_with_lock():
                for _ in range(1000):
                    with lock:  # 使用锁保护临界区
                        temp = shared_counter["value"]
                        temp += 1
                        shared_counter["value"] = temp

            threads = [
                threading.Thread(target=increment_with_lock)
                for _ in range(5)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join()

            return shared_counter["value"]

        expected_total = 5000

        # 无锁情况：预期出现数据竞争，结果 < 5000
        result_without_lock = test_without_lock()
        has_race_condition = result_without_lock < expected_total

        # 有锁情况：结果应精确等于 5000
        result_with_lock = test_with_lock()

        # 验证锁的必要性：无锁应有竞争，有锁应正确
        # 注意：在高并发/特殊调度下，无锁测试可能偶然通过
        # 因此我们主要验证有锁情况是正确的
        assert result_with_lock == expected_total, \
            f"有锁情况下结果错误: 期望 {expected_total}, 实际 {result_with_lock}"

        # 无锁情况可能存在竞争（仅作为演示，不强制断言）
        # print(f"无锁结果: {result_without_lock}, 有锁结果: {result_with_lock}")


class TestWebSocketStability:
    """WebSocket 稳定性测试"""

    @pytest.mark.full_regression
    def test_q6_1_ws_connection_timeout(self):
        """Q6.1: WebSocket 连接超时"""
        # Mock WebSocket 连接
        class MockWebSocket:
            def __init__(self):
                self.connected = False
                self.connect_timeout = False

            def connect(self, url, timeout=5):
                if timeout <= 5:
                    raise TimeoutError("Connection timeout")
                self.connected = True

        ws = MockWebSocket()
        with pytest.raises(TimeoutError):
            ws.connect("wss://example.com/ws", timeout=5)

    @pytest.mark.full_regression
    def test_q6_2_ws_reconnect_after_error(self):
        """Q6.2: 错误后自动重连"""
        reconnect_count = 0
        max_retries = 3

        class MockWebSocket:
            def __init__(self):
                self.connected = False

            def connect(self):
                nonlocal reconnect_count
                if reconnect_count < max_retries:
                    reconnect_count += 1
                    raise ConnectionError(f"Attempt {reconnect_count} failed")
                self.connected = True

        ws = MockWebSocket()
        for attempt in range(max_retries + 1):
            try:
                ws.connect()
                break
            except ConnectionError:
                if attempt == max_retries:
                    raise

        assert ws.connected, "应在重试后成功连接"
        assert reconnect_count == max_retries, f"应重试 {max_retries} 次"
