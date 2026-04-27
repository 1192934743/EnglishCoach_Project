# 场景数据迁移计划（修订版）

> 根据 Gemini 评审修订：2026-04-27
> 目标：取消 scenes.json，场景数据全面迁移到 Topic DB

---

## Gemini 评审结论（采纳）

### 缺陷一：is_active 不能放 Topic 表
**问题**：`is_active` 是全局元数据表，在 WebSocket 中执行 `db.query(Topic).update({Topic.is_active: False})` 会影响所有用户。用户 A 切话题 → 用户 B 连上 fallback → 被强制切到用户 A 的话题。

**采纳**：删除 `is_active` 字段。不引入任何全局会话状态。

### 缺陷二：dialogue_engine 的 fallback 是死代码
**问题**：coach_ws.py 连接时无论何时都会调用 `build_task_packet(user_id)` → `_build(user_id, db)` → 正常选话题或 `_fallback_task_packet(db)`。TaskPacket 永远是有效实例。dialogue_engine.py 的 `load_active_scene()` 和 fallback 分支永远不执行。

**采纳**：dialogue_engine.py 中的 `load_active_scene()`、`SCENES_FILE_PATH`、以及 `build_prompts` 中的 fallback 分支全部删除，只保留 TaskPacket 路径。

### 缺陷三：switch_topic 不应该写数据库
**问题**：切换话题只需要重新生成当前用户的 TaskPacket，不需要修改任何共享数据。

**采纳**：WebSocket `switch_topic` action 只调用 `session_planner.build_task_packet_for_topic(user_id, topic_id)`，纯内存操作。

---

## 执行计划

### Phase 1：清理 dialogue_engine.py（死代码删除）

**`python_backend/core/dialogue_engine.py`**

1. 删除 `SCENES_FILE_PATH` 常量
2. 删除 `load_active_scene()` 函数
3. 删除 `build_prompts()` 中的 fallback 分支（`if task_packet is None` 及 `load_active_scene()` 调用）
4. 删除 `build_evaluator_prompt()` 中的 `load_active_scene()` 调用

**注意**：`STATIC_SYSTEM_TEMPLATE` 中的 `{% if scene_specific_rules %}` 块**保留不动**。Jinja2 的空列表 `[]` 自动跳过该块，不影响。

### Phase 2：删除 scenes.json 文件

**删除 `python_backend/scenes.json`**

确认代码中无引用后直接删除。

### Phase 3：更新 auto_optimizer.py（场景级优化）

**`python_backend/auto_optimizer.py`**

1. 删除 `SCENES_FILE`、`load_scenes_config()`、`save_scenes_config()`
2. `set_scene_rules(scene_id, rules)` 改为直接更新 Topic DB 的 `scene_specific_rules` 字段
3. `list_all_scenes()` 改为查询 Topic DB
4. `--all-scenes` 遍历 Topic DB 而非 JSON keys

### Phase 4：更新 auto_test_suite.py（对抗测试）

**`python_backend/auto_test_suite.py`**

1. 删除 `switch_scene_in_scenes_json()` 函数
2. 场景切换改为通过 WebSocket 发送 `switch_topic` action（纯内存操作，**绝不碰数据库**）
3. `--scene` 参数改为接受 topic_id（整数）

### Phase 5：coach_ws.py 新增 switch_topic（纯内存）

**`python_backend/api/websocket/coach_ws.py`**

```python
if action == "switch_topic":
    topic_id = data.get("topic_id")
    if topic_id and current_user:
        new_packet = await run_in_threadpool(
            session_planner.build_task_packet_for_topic,
            current_user.id,
            topic_id
        )
        if new_packet:
            # 1. 更新当前任务包与基础会话 ID
            current_task_packet = new_packet
            topic_id_for_progress = current_task_packet.topic_id
            session_id = str(uuid.uuid4())

            # 2. 清理历史状态（session_hits 原为 set，用 clear()；session_transcript 同样）
            session_hits.clear()
            session_transcript.clear()
            session_ctx = SessionContext()

            # 3. 重新获取新话题的掌握度快照
            mastery_snapshot = await run_in_threadpool(
                _take_mastery_snapshot, current_user.id, current_task_packet
            )

            # 4. 重新生成 Prompt
            static_sys, dynamic_turn = build_prompts(
                current_user, is_flipped, session_ctx, current_task_packet, session_hits
            )
            chat_history = [{"role": "system", "content": static_sys}]

            # 5. 通知前端
            await safe_send_ws(websocket, ws_lock, {
                "event": "topic_changed",
                "topic_id": current_task_packet.topic_id,
                "topic_title": current_task_packet.topic_title,
                "topic_title_zh": getattr(current_task_packet, "topic_title_zh", None) or "",
                "role_name": current_task_packet.role_name,
                "depth_tier": current_task_packet.depth_tier,
                "session_id": session_id,
            })
    continue
```

**注意**：`session_hits` 是 `set` 类型，必须用 `.clear()` 而非 `= []`；`session_transcript` 也需同步清理；必须重新调用 `build_prompts()` 生成 `static_sys` 否则 `NameError`；必须重置 `topic_id_for_progress`、`session_id`、`mastery_snapshot`。

### Phase 6：数据库清理

**`python_backend/database.py`**

不新增 `is_active` 字段。不修改 Topic 表结构。

> **关键约束**：`switch_topic` 永不写数据库，所有场景切换均为当前用户的内存操作。

### Phase 7：前端验证

**`flutter_frontend/`**

无代码改动。`topic_changed` 事件处理逻辑保持不变（`topic_id` 字段已由后端提供）。

---

## 改动汇总

```
删除
  python_backend/scenes.json
  python_backend/core/dialogue_engine.py 中的 load_active_scene() 及 fallback 分支
  python_backend/auto_optimizer.py 中的 JSON 文件操作
  python_backend/auto_test_suite.py 中的 switch_scene_in_scenes_json()

新增
  python_backend/api/websocket/coach_ws.py: switch_topic action（纯内存，不写 DB）

修改
  python_backend/auto_optimizer.py: JSON → Topic DB 读写
  python_backend/auto_test_suite.py: 场景切换改为 topic_id 参数

不变
  python_backend/database.py          不新增任何字段
  python_backend/application/services/session_planner.py  不改动
  flutter_frontend/                    不改动
```

---

## 验收标准

- [ ] `scenes.json` 删除后 backend 启动不报错
- [ ] `build_prompts()` 只接收有效 TaskPacket，删除了所有 fallback 分支
- [ ] `auto_optimizer.py --scene <topic_id>` 能正确读写 Topic DB 的 `scene_specific_rules`
- [ ] `auto_test_suite.py --scene <topic_id>` 能切换话题进行测试
- [ ] `switch_topic` action 只重新生成 TaskPacket，不修改共享数据库
- [ ] 前端 topic browser 和 chat 流程无影响
