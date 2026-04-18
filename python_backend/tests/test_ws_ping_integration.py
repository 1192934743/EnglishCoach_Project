"""WebSocket 最小握手：需完整 lifespan。"""
from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

_server = importlib.import_module("server")


@pytest.mark.integration
def test_ws_coach_ping_returns_warmup_success():
    """连接后服务端可能先发 evaluate 相关事件，再响应 ping；轮询直到 warmup_success。"""
    uid = "pytest-ws-user-001"
    with TestClient(_server.app) as client:
        with client.websocket_connect(f"/ws/coach?user_id={uid}") as ws:
            ws.send_text(json.dumps({"action": "ping", "user_id": uid}))
            data = None
            for _ in range(15):
                raw = ws.receive_text()
                data = json.loads(raw)
                if data.get("event") == "warmup_success":
                    break
            assert data is not None
            assert data.get("event") == "warmup_success"


@pytest.mark.integration
def test_ws_client_latency_report_roundtrip_logs():
    """服务端应对 client_latency_report 静默成功（不抛）。"""
    uid = "pytest-ws-user-002"
    with TestClient(_server.app) as client:
        with client.websocket_connect(f"/ws/coach?user_id={uid}") as ws:
            ws.send_text(
                json.dumps(
                    {
                        "action": "client_latency_report",
                        "trace_id": "a" * 16,
                        "submit_to_first_pcm_ms": 100,
                        "submit_to_tts_finished_ms": 500,
                        "had_first_pcm": True,
                        "client_report_wall_ms": 1_700_000_000_000,
                    }
                )
            )
            # 服务端仅打日志，无 JSON 回包 — 不应抛错关闭连接
            ws.close()
