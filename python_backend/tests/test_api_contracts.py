"""REST 契约：不标记 integration 时仍可能触发 lifespan（含 DeepSeek 预热尝试）。"""
from __future__ import annotations

import importlib

from fastapi.testclient import TestClient

_server = importlib.import_module("server")


def test_get_topics_returns_shape():
    with TestClient(_server.app) as client:
        r = client.get("/api/topics")
        assert r.status_code == 200
        body = r.json()
        assert "topics" in body
        assert isinstance(body["topics"], list)
        if body["topics"]:
            t0 = body["topics"][0]
            for key in ("id", "title", "category", "total_nodes", "avg_mastery"):
                assert key in t0


def test_get_stats_requires_user_id():
    with TestClient(_server.app) as client:
        r = client.get("/api/stats")
        assert r.status_code == 422
