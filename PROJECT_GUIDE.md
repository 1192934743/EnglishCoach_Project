# EnglishCoach 项目指南

> **重要**：本文档是新聊天会话的必读文件。每次开启新对话时，请先阅读此文档了解项目现状。

---

## 一、项目概述

### 1.1 项目定位

EnglishCoach 是一个 AI 英语口语练习应用，通过模拟真实对话场景（咖啡店点单、酒店预订等），帮助用户练习英语口语表达。

### 1.2 技术栈

| 层级 | 技术 |
|------|------|
| 前端 | WebSocket 客户端（浏览器） |
| 后端 | Python FastAPI |
| LLM | DeepSeek / Azure OpenAI |
| TTS | 火山引擎 |
| 数据库 | SQLite |
| 测试 | pytest + pytest-asyncio |

### 1.3 核心模块

```
python_backend/
├── api/
│   ├── http/          # HTTP REST API
│   └── websocket/     # WebSocket 实时通信
├── application/       # 业务逻辑层
│   └── services/      # Session、Assessment、Mastery 等服务
├── core/              # 核心引擎
│   └── dialogue_engine.py  # 对话引擎
├── domain/           # 领域模型
│   └── entities/     # SessionContext、TaskPacket 等
└── infrastructure/   # 基础设施
    ├── llm/          # LLM 客户端
    ├── tts/          # TTS 服务
    └── vector_store/ # 向量存储
```

---

## 二、核心功能

### 2.1 对话流程

```
用户输入 → WebSocket → Coach WS → DialogueEngine
                                      ↓
                              LLM (Actor) - 主回复
                              LLM (Director) - Hint 生成
                              LLM (Evaluator) - 评估纠错
                                      ↓
                              响应用户 ← WebSocket
```

### 2.2 三大 LLM 角色

| 角色 | 职责 | Prompt 模板 |
|------|------|-------------|
| **Actor** | 生成自然对话回复 | `ACTOR_TEMPLATE` |
| **Director** | 生成 3 个 Hints | `DIRECTOR_TEMPLATE` |
| **Evaluator** | 评估用户输入、纠错 | `EVALUATOR_TEMPLATE` |

### 2.3 难度等级

| 等级 | 特点 |
|------|------|
| Beginner | 简单词汇、短句（≤10词） |
| Intermediate | 中等难度（≤15词） |
| Advanced | 复杂表达（≤25词） |

### 2.4 场景系统

- **微场景**：单一对话节点（如"点饮料"）
- **场景图**：多个微场景 + 流转关系
- **流转条件**：三轨判定（intent + constraints + coach）

---

## 三、测试体系

### 3.1 端到端测试概览

测试文件位于 `python_backend/tests/` 目录：

| 文件 | 测试范围 |
|------|----------|
| `e2e/test_actor_reply_quality.py` | Q1.x 主 LLM 回复质量 |
| `e2e/test_director_hint_quality.py` | Q3.x Hint 生成质量 |
| `e2e/test_translation_correction.py` | Q4.x 翻译与纠错质量 |
| `e2e/test_scenario_transition.py` | Q5.x 场景流转逻辑 |
| `e2e/test_system_stability.py` | Q6.x 系统稳定性 |
| `fixtures/test_data.py` | 测试数据（正面/负面样本） |

### 3.2 运行测试

#### 基础命令

```bash
# 进入测试目录
cd python_backend/tests

# 运行所有测试
python -m pytest -v

# 运行特定测试文件
python -m pytest e2e/test_scenario_transition.py -v

# 运行特定测试用例
python -m pytest e2e/test_system_stability.py::TestSystemStability::test_q6_1_llm_timeout_handling -v

# 按标记运行
python -m pytest -m full_regression -v        # 完整回归
python -m pytest -m critical_path -v         # 冒烟测试
python -m pytest -m nightly -v                # 夜间测试
```

#### 输出选项

```bash
# 简洁输出
python -m pytest -q

# 显示详细失败信息
python -m pytest -v --tb=long

# 生成 HTML 报告
python -m pytest --html=report.html

# 生成 JUnit XML（CI 用）
python -m pytest --junitxml=results.xml
```

### 3.3 测试标记

| 标记 | 用途 | 执行频率 |
|------|------|----------|
| `@pytest.mark.critical_path` | 冒烟测试 | 每次提交 |
| `@pytest.mark.full_regression` | 完整回归 | 发布前 |
| `@pytest.mark.nightly` | 夜间测试 | 定时 |
| `@pytest.mark.unit` | 单元测试 | 持续 |
| `@pytest.mark.asyncio` | 异步测试 | 持续 |

### 3.4 测试依赖

```bash
# 安装测试依赖
pip install pytest pytest-asyncio pytest-timeout pytest-cov psutil

# 或使用 requirements.txt
cd python_backend/tests
pip install -r requirements.txt
```

### 3.5 查看测试输出

测试运行后，结果直接显示在**终端**：

```
e2e/test_scenario_transition.py::TestScenarioTransitionLogic::test_q5_1_prevent_circular_visit PASSED
============================= 41 passed in 41.04s =============================
```

---

## 四、Q5.x 和 Q6.x 测试详情

### 4.1 Q5.x 场景流转测试（19 个用例）

| 测试 ID | 内容 |
|---------|------|
| Q5.1 | 循环流转检测 - 防止重复访问同一场景 |
| Q5.2 | 孤立节点检测 - 无出口且无 transitions |
| Q5.3 | 三轨流转条件边界测试（参数化 9 种组合） |
| Q5.4a | 只有一轨满足不应流转 |
| Q5.4b | 两轨满足但 coach=False 不应流转 |
| Q5.5 | 达到 max_turns 强制通关 |
| Q5.6 | 出口节点通关触发难度升级 |
| Q5.7 | 晋级标记持久化 |

### 4.2 Q6.x 系统稳定性测试（22 个用例）

| 测试 ID | 内容 |
|---------|------|
| Q6.1 | LLM 超时处理（20s 超时） |
| Q6.2 | JSON 解析失败使用 fallback_hint |
| Q6.3 | 空输入静默处理 |
| Q6.4 | session_transcript 长度限制 |
| Q6.5a | 多标签页 session_id 隔离 |
| Q6.5b | 多用户同场景状态隔离 |
| Q6.5c | 并发评分提交无覆盖 |
| Q6.5d | 锁竞争检测 |

### 4.3 测试结果

**当前状态**：41 个测试全部通过 ✅

```
============================= 41 passed in 41.04s =============================
```

---

## 五、待完成事项

### 5.1 高优先级

- [ ] Q7.x 渐进诱导效果测试（Turn 1-2 引导逻辑）
- [ ] Q7.6 WebSocket 断连恢复测试
- [ ] 完整场景图测试（多节点流转）

### 5.2 中优先级

- [ ] Mock LLM 集成测试（与真实 LLM 对比）
- [ ] 性能基准测试（psutil 内存测量）
- [ ] CI/CD 流水线配置（GitHub Actions）

### 5.3 低优先级

- [ ] 测试覆盖率报告
- [ ] 性能回归基准
- [ ] 模糊测试（Fuzzing）

---

## 六、关键文件索引

### 6.1 文档

| 文件 | 说明 |
|------|------|
| `docs/端到端测试计划.md` | 完整的端到端测试计划（包含所有测试用例定义） |
| `docs/副导演Hint质量优化方案.md` | Hint 优化方案 |
| `docs/副导演Hint质量优化方案.md` | Hint 优化方案 |

### 6.2 核心代码

| 文件 | 说明 |
|------|------|
| `core/dialogue_engine.py` | 对话引擎核心 |
| `application/services/session_service.py` | 会话服务 |
| `api/websocket/coach_ws.py` | WebSocket 处理 |
| `tests/conftest.py` | pytest 配置入口 |

### 6.3 配置

| 文件 | 说明 |
|------|------|
| `config.env` | 环境配置 |
| `python_backend/tests/pytest.ini` | pytest 配置 |
| `python_backend/tests/requirements.txt` | 测试依赖 |

---

## 七、常见问题

### Q1: 测试失败怎么办？

1. 查看终端输出的详细错误信息
2. 使用 `--tb=long` 查看完整堆栈
3. 单独运行失败的测试：`pytest path/to/test.py::TestClass::test_name -v`

### Q2: 如何添加新测试？

1. 在 `tests/e2e/` 下创建测试文件
2. 参考 `test_scenario_transition.py` 的结构
3. 使用 `@pytest.mark.full_regression` 标记
4. 运行验证

### Q3: Mock LLM 如何工作？

测试使用 Mock 数据模拟 LLM 响应，无需真实 API key。
`conftest.py` 会自动设置占位符密钥。

### Q4: 异步测试报错 `TypeError`？

确保：
1. 安装了 `pytest-asyncio`
2. pytest.ini 中设置了 `asyncio_mode = auto`
3. 异步函数用 `@pytest.mark.asyncio` 装饰

---

## 八、联系方式与资源

- **测试计划文档**：`docs/端到端测试计划.md`
- **测试目录**：`python_backend/tests/`
- **配置文件**：`python_backend/tests/pytest.ini`

---

> **最后更新**：2026-05-15
> **测试状态**：41/41 通过 ✅
