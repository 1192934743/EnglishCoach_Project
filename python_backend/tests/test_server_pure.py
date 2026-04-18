"""不访问外网、不依赖 WebSocket 的纯逻辑用例。"""
from __future__ import annotations

import importlib

import pytest

# conftest 已设置环境变量后再导入
_server = importlib.import_module("server")


@pytest.mark.parametrize(
    "raw,expect_len",
    [
        (None, 8),
        ("", 8),
        ("ab", 8),
        ("ABCD-ef12", 8),
        ("abcdEF12", 8),
        ("a" * 32, 32),
    ],
)
def test_coerce_turn_trace_id_invalid_or_short_uses_hex8(raw, expect_len):
    tid = _server._coerce_turn_trace_id(raw)
    assert len(tid) == expect_len
    assert all(c in "0123456789abcdef" for c in tid)


def test_coerce_turn_trace_id_valid_lowercases():
    assert _server._coerce_turn_trace_id("631C8E71-AD25-4A6D-AF85-66379148E123") == (
        "631c8e71ad254a6daf8566379148e123"
    )


def test_trim_chat_history_keeps_system_and_pair_boundary():
    trim = _server.trim_chat_history
    long = "x" * 400
    history = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": long},
        {"role": "assistant", "content": long},
        {"role": "user", "content": "last-q"},
        {"role": "assistant", "content": "last-a"},
    ]
    out = trim(history, max_tokens=200)
    assert out[0]["content"] == "SYS"
    assert out[-2]["content"] == "last-q"
    assert out[-1]["content"] == "last-a"


def test_trim_chat_history_single_turn_unchanged():
    trim = _server.trim_chat_history
    h = [{"role": "system", "content": "S"}, {"role": "user", "content": "hi"}]
    assert trim(h, max_tokens=10) == h
