# EnglishCoach 后端测试说明

## 分层（测试金字塔）

| 层级 | 目录 / 标记 | 目的 |
|------|----------------|------|
| 单元 | `test_server_pure.py` | 不拉起外网：裁剪历史、`trace_id` 归一等纯函数 |
| 契约 / API | `test_api_contracts.py` | `TestClient` 访问 REST，校验 JSON 形态 |
| 集成 | `@pytest.mark.integration` | WebSocket `ping` 握手；依赖本地 `english_coach.db` 与占位/真实密钥 |

## 运行

在 `python_backend` 目录下：

```bash
# 仅快速单测（CI 默认）
pytest tests -m "not integration" -q

# 含 WebSocket 握手（需能通过 import 校验的密钥；外网预热可能失败但不影响 ping 结构）
pytest tests -m integration -q

# 全部 + 覆盖率
pytest tests --cov=. --cov-report=term-missing -q
```

若未配置 `config.env`，`tests/conftest.py` 会在导入 `server` 前注入占位环境变量，避免 `RuntimeError: Missing essential API keys`。

## 与「混沌脚本」的关系

`auto_test_suite.py` 仍适合对真实服务做长会话压测；本目录的 pytest 用于**回归与重构护栏**，可在无真机环境下在 CI 中跑大部分用例。
