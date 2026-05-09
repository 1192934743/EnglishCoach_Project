---
name: Topic生成与图谱打通改造方案
overview: 在 database.py、topic_generator.py、seed_topics.py 三处做联动改造：给 Topic 加 domain 字段、废弃 TargetNode 写入、打通 vocab_tags 下游传递链路。
todos:
  - id: db-domain
    content: database.py — Topic 模型加 domain 字段 + ensure_schema_upgrades() 追加
    status: completed
  - id: topic-gen
    content: topic_generator.py — schema 加 domain、移除 TargetNode 写入、加 domain 参数
    status: completed
  - id: seed-topics
    content: seed_topics.py — SEED_DESCRIPTIONS 改为字典结构、传递 domain
    status: completed
  - id: graph-gen
    content: graph_generator.py — 注入 Topic.vocab_tags 上游上下文到 Prompt
    status: completed
  - id: migrate-script
    content: scripts/migrate_add_topic_domain.py — 新建一次性迁移脚本
    status: pending
isProject: false
---

# Topic 生成与图谱打通 — 完整修改方案

## 现状分析

```
seed_topics.py (字符串列表)
        ↓  desc
topic_generator.py (DeepSeek) → Topic + TargetNode  ← 两套词汇系统互不相通
        ↓  topic.id
graph_generator.py (Gemini)  → MicroScenario + ScenarioConstraint  ← 盲生成
```

**三个核心问题：**
1. `Topic` 没有 `domain` 字段 → Migration Lock 无法工作
2. `topic_generator.py` 仍在写 `TargetNode` → 历史包袱未清理
3. Topic 的 `vocab_tags` 不参与 Scenario 生成 → 上下脱节

---

## 修改计划（5 步）

### Step 1: `database.py` — 加 `domain` 字段

在 `Topic` 模型声明里加一行：

```python:python_backend/database.py
class Topic(Base):
    __tablename__ = 'topics'
    ...
    # 话题领域分类，支撑 Migration Lock（防横向沉迷）
    domain = Column(String, nullable=True)  # e.g. "餐饮", "出行", "购物"
```

在 `ensure_schema_upgrades()` 的 ALTER TABLE 块中追加：

```python:python_backend/database.py
if "domain" not in colnames:  # topics 表
    conn.execute(text("ALTER TABLE topics ADD COLUMN domain VARCHAR(64)"))
    print("[Schema] Added topics.domain column")
```

---

### Step 2: `topic_generator.py` — 传 domain 参数落地，移除 TargetNode 写入

**2a. `get_or_generate_topic()` 加 domain 参数：**

```python:python_backend/infrastructure/topic_generator.py
async def get_or_generate_topic(
    description: str,
    openai_client,
    db: Optional[Session] = None,
    domain: Optional[str] = None,  # ← 直接传入，不让 LLM 生成
) -> Topic:
```

**2b. Prompt 里只把 domain 作为上下文提示（不要求 JSON 输出）：**

```python:python_backend/infrastructure/topic_generator.py
prompt = f"""...
TARGET DOMAIN: "{domain}"  ← 仅作上下文参考，不要求 LLM 输出 domain 字段
..."""
```

**2c. `_save_to_db()` 直接用参数落库：**

```python:python_backend/infrastructure/topic_generator.py
topic = Topic(
    ...
    domain=domain,  # ← 直接写入，不走 LLM 输出
)
```

**2d. 移除 TargetNode 写入（模型保留，只停写入）：**

删除 `_save_to_db()` 中的这段：
```python:python_backend/infrastructure/topic_generator.py
#     for n in raw_nodes:
#         db.add(TargetNode(...))
#     db.commit()
```

**2e. 移除 `_build_reference_template()` 中对 TargetNode 的查询：**

```python:python_backend/infrastructure/topic_generator.py
# nodes 字段改为空列表（不再从 TargetNode 读取）
"nodes": [],
```

**2f. 移除顶层 import 中的 TargetNode：**

```python:python_backend/infrastructure/topic_generator.py
from database import SessionLocal, Topic, TargetNode, topic_title_zh_fallback
# 改为
from database import SessionLocal, Topic, topic_title_zh_fallback
```

---

### Step 3: `seed_topics.py` — 改为带 domain 的字典结构

```python:python_backend/scripts/seed_topics.py
SEED_DESCRIPTIONS = [
    {"domain": "餐饮", "desc": "Ordering drinks and food at a coffee shop or café"},
    {"domain": "餐饮", "desc": "Sitting down at a restaurant, ordering from the menu, and paying the bill"},
    {"domain": "出行", "desc": "Checking in luggage and going through security at an airport"},
    {"domain": "出行", "desc": "Taking a taxi or rideshare like Uber — giving directions and making small talk"},
    {"domain": "出行", "desc": "Checking in at a hotel, asking for amenities, and checking out"},
    {"domain": "购物", "desc": "Shopping for clothes — asking for sizes, colors, and trying items on"},
    {"domain": "购物", "desc": "Grocery shopping and asking store staff where to find items"},
    {"domain": "医疗", "desc": "Visiting a doctor, describing symptoms, and understanding a prescription"},
    {"domain": "职业", "desc": "Calling customer service to report a problem or request a refund"},
    {"domain": "职业", "desc": "A business meeting — presenting ideas, giving feedback, and discussing next steps"},
    {"domain": "社交", "desc": "Making small talk at a party — introducing yourself and discussing hobbies"},
    {"domain": "社交", "desc": "Renting an apartment — asking the landlord about rules, utilities, and repairs"},
]
```

调用处传 domain：

```python:python_backend/scripts/seed_topics.py
for item in SEED_DESCRIPTIONS:
    topic = await get_or_generate_topic(item["desc"], client, db, domain=item["domain"])
```

---

### Step 4: `graph_generator.py` — 注入 Topic.vocab_tags 上游上下文

在 `generate_micro_scenarios()` 的 prompt 里追加 Topic 词汇，引导 constraint 生成：

```python:python_backend/scripts/graph_generator.py
## Topic Vocabulary Reference (for constraint generation guidance)
- Vocab Tags: {', '.join(topic.vocab_tags or [])}
- Sentence Patterns: {', '.join(topic.sentence_patterns or [])}

## Generation Guidance
- Constraints should align with and expand upon the topic's vocab_tags
- Prioritize vocabulary from the topic's sentence_patterns where semantically appropriate
```

---

## 改动文件清单

- `python_backend/database.py` — Topic 模型加 domain 字段 + `ensure_schema_upgrades()` 追加 ALTER
- `python_backend/infrastructure/topic_generator.py` — domain 参数传入 + 移除 TargetNode 写入
- `python_backend/scripts/seed_topics.py` — SEED_DESCRIPTIONS 改为字典结构 + 传递 domain
- `python_backend/scripts/graph_generator.py` — 注入 Topic.vocab_tags 上游上下文到 Prompt