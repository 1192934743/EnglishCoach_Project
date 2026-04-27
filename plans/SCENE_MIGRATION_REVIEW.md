# 场景数据迁移评审报告

> 目标：取消 `scenes.json`，场景数据全面迁移到 Topic DB，扩展完整字段。
> 迁移前后评审时间：2026-04-27

---

## 一、现状分析

### 1.1 两条数据路径

```
前端 topic_browser
  └── GET /api/topics              → Topic DB（全量话题列表）
  └── request_topic (WebSocket)    → topic_generator → Topic DB

真实会话（99.9%+ 场景）
  └── coach_ws:session_planner.build_task_packet(user_id)
       └── db.query(Topic).all()              ← 直接查 Topic DB
       └── topic.scene_specific_rules ✅ 已读 DB
       └── topic.difficulty_tiers   ✅ 已读 DB
       └── topic.vocab_tags         ✅ 已读 DB
       └── topic.learner_level / role_name / voice ✅ 已读 DB

Fallback 兜底（极少触发，仅 task_packet=None 时）
  └── build_prompts(task_packet=None)
       └── load_active_scene() → scenes.json ❌
       └── 这是 scenes.json 唯一的运行时消费点
```

### 1.2 关键结论

**话题推荐系统（用户不选话题时自动选）完全不受影响。**

WebSocket 连接建立时，`build_task_packet(user_id)` 被调用：
1. 查询 `db.query(Topic).all()`
2. 对所有话题按四维度打分（相似度 32% + 复习紧迫度 32% + 新鲜度 16% + 掌握缺口 20%）
3. 选最高分话题，构建 TaskPacket

这个流程完全不经过 `scenes.json`。scenes.json 只在 fallback 分支被读，而真实会话永远有 TaskPacket。

### 1.3 scenes.json 的引用点

| 文件 | 引用内容 | 运行时影响 |
|------|---------|-----------|
| `core/dialogue_engine.py` | `SCENES_FILE_PATH`、`load_active_scene()` | 仅 fallback 路径 |
| `auto_optimizer.py` | `SCENES_FILE` 常量及 JSON 读写函数 | 仅优化脚本 |
| `auto_test_suite.py` | `switch_scene_in_scenes_json()` | 仅测试脚本 |
| `archive_prompts/` | 旧归档代码 | 不改动 |

---

## 二、迁移方案

### 2.1 数据库模型扩展（database.py）

Topic 表已有全部字段，只需新增一个活跃标记：

```python
class Topic(Base):
    # ... 现有字段 ...
    # 新增：替代 scenes.json 的 current_active_id
    is_active = Column(Boolean, default=False)
```

种子数据中，将第一个话题的 `is_active = True`。

### 2.2 删除 scenes.json 运行时引用（dialogue_engine.py）

```python
# 删除
SCENES_FILE_PATH = os.path.join(_BACKEND_DIR, "scenes.json")
def load_active_scene(file_path=SCENES_FILE_PATH): ...

# 新增
def load_active_topic(db: Session = None) -> dict:
    """从 Topic DB 加载当前活跃场景（替代 scenes.json）"""
    from database import SessionLocal
    if db is None:
        db = SessionLocal()
    topic = db.query(Topic).filter(Topic.is_active == True).first()
    if not topic:
        topic = db.query(Topic).first()  # 终极兜底
    return {
        "scene": topic.title,
        "role": topic.role_name or "Assistant",
        "level": topic.learner_level or "Intermediate",
        "voice": topic.voice or "Stanley",
        "scene_specific_rules": topic.scene_specific_rules or [],
    }
```

fallback 分支：`active_scene = load_active_topic(db)` 替代 `load_active_scene()`。

### 2.3 删除 scenes.json 文件

迁移完成后，删除 `python_backend/scenes.json`。

### 2.4 新增场景切换 WebSocket Action（coach_ws.py）

```python
if action == "switch_topic":
    topic_id = data.get("topic_id")
    topic = db.query(Topic).filter(Topic.id == topic_id).first()
    if topic:
        db.query(Topic).update({Topic.is_active: False})
        topic.is_active = True
        db.commit()
        current_task_packet = session_planner.build_task_packet_for_topic(user_id, topic.id)
        await safe_send_ws(websocket, ws_lock, {
            "event": "topic_changed",
            "topic_id": topic.id,
            "topic_title": topic.title,
            "topic_title_zh": topic.title_zh or "",
        })
```

### 2.5 更新 auto_optimizer.py（场景级优化）

- 删除：`SCENES_FILE`、`load_scenes_config()`、`set_scene_rules()`、`list_all_scenes()`
- 改为：直接从 Topic DB 读写 `scene_specific_rules`
- `run_scene_optimization_loop()` 改为用 topic_id 查询/更新 Topic DB

### 2.6 更新 auto_test_suite.py（对抗测试）

- 删除：`switch_scene_in_scenes_json()`
- 场景切换改为通过 WebSocket `switch_topic` action 或直接写 DB `is_active`
- `--scene` 参数改为接受 topic_id（整数）

---

## 三、话题推荐系统影响评估

| 问题 | 结论 |
|------|------|
| 用户不选话题时自动选哪个？ | `build_task_packet()` 查 Topic DB 打分选出，完全不依赖 scenes.json |
| 话题评分依赖什么字段？ | 相似度（embedding/vocab_tags）+ 复习紧迫度（UserProgress）+ 新鲜度（历史 session）+ 掌握缺口 |
| 新话题如何加入？ | 通过 `topic_generator.get_or_generate_topic()` 生成并写入 Topic DB |
| `is_active` 字段是做什么的？ | 仅替代 scenes.json 的 `current_active_id`，用于 fallback 兜底路径。真实会话用 TaskPacket，不走 is_active |
| scenes.json 删除后前端有影响吗？ | 无。前端所有 topic 操作已通过 WebSocket `topic_changed` 事件与 DB 同步 |

---

## 四、扩展字段说明

Topic DB 已支持 scenes.json 没有的字段：

| 字段 | 类型 | 用途 | 是否新增 |
|------|------|------|---------|
| `vocab_tags` | JSON list | 核心词汇，向量化和相似度计算 | 否（已有） |
| `sentence_patterns` | JSON list | 核心句型，Jaccard 相似度计算 | 否（已有） |
| `difficulty_tiers` | JSON dict | 按深度分层的规则配置 | 否（已有） |
| `embedding` | JSON list | 向量特征，话题相似度计算 | 否（已有） |
| `scene_specific_rules` | JSON list | 场景专属护栏规则 | 否（已有） |
| `is_active` | Boolean | 替代 scenes.json current_active_id | **是（新增）** |

迁移完成后，所有场景配置统一存于 Topic DB，无 JSON 文件依赖。

---

## 五、风险与注意事项

1. **向后兼容**：已有 `english_coach.db` 的用户，Topic 表缺少 `is_active` 字段。需要 `ensure_schema_upgrades()` 中添加 migration 逻辑。
2. **auto_optimizer 存量数据**：已生成的 scene_specific_rules 存在 scenes.json 中，迁移后需要一次性同步到 Topic DB。
3. **测试脚本**：需要确保 `auto_test_suite.py` 和 `auto_optimizer.py` 在迁移后能正确连接 Topic DB。
4. **种子数据**：`init_db()` 中 `is_active=True` 的设定需与 `DEFAULT_TOPIC_ID` 保持一致。

---

## 六、验收标准

- [ ] 删除 `scenes.json` 后，backend 启动不报错
- [ ] `build_prompts(task_packet=None)` 的 fallback 路径从 Topic DB 读取活跃话题
- [ ] `auto_optimizer.py --scene <topic_id>` 能正确读写 Topic DB 的 `scene_specific_rules`
- [ ] `auto_test_suite.py --scene <topic_id>` 能切换到正确话题进行测试
- [ ] 前端 topic browser 和 chat 流程无影响
- [ ] 用户不选话题时，自动选出的话题来自 Topic DB 评分系统（原有逻辑不变）
