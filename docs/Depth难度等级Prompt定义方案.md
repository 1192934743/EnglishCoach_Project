# Depth 难度等级 Prompt 定义方案

> 版本：v1.2（已追加架构师预备指令）
> 日期：2026-05-09
> 状态：Phase 1/2 待交付，引擎层规则已确立

---

## 一、问题背景

### 1.1 当前问题

在 `graph_generator.py` 中，depth 信息只是简单传入数字：

```python
prompt = f"""...
难度等级：{depth}   # 只传递 "1"、"2"、"3"
...
"""
```

LLM 无法理解 Depth 1/2/3 的实际含义，可能导致生成结果不符合预期。

### 1.2 设计目标

- 让 LLM 明确理解每个难度等级的特征
- 生成的场景在不同 depth 间有明显梯度差异
- **仅聚焦"词汇与句法复杂度"，不涉及对话轮数或故事情节**

### 1.3 架构约束（已确立）

> **重要澄清（架构师评审）**：
> 1. Depth 的输出结果仅为 JSON（意图、描述、Constraints 约束词），**不包含对话剧本**
> 2. 对话轮数和容错度由运行时的 `DialogueEngine` 和 `MasteryScorer` 决定，LLM 无法控制
> 3. **Depth = 静态难度（词汇/句型升级），Twists = 动态突发事件（运行时注入）**
> 4. 同一 Topic 的不同 Depth 必须保持相同的业务流程（step_order），**绝不能改变故事情节**

---

## 二、Depth 难度等级定义

### 2.1 核心原则

| 原则 | 说明 |
|------|------|
| **业务流程不变** | 同一 Topic 的 Depth 1/2/3 共享相同的 step_order |
| **仅词汇升级** | 区别只在于表达方式的复杂度 |
| **无轮数约束** | 对话轮数由运行时引擎决定，Prompt 不涉及 |

### 2.2 DEPTH_DEFINITIONS（已定稿）

```python
DEPTH_DEFINITIONS = """
## 难度等级定义（请严格遵循）
注意：同一 Topic 的不同 Depth 必须保持相同的业务流程（step_order），仅仅是表达的复杂度升级！绝不要改变基本故事情节！

### Depth 1 - 入门级（Foundation - 核心生存句型）
- 目标：用最简单的词汇完成该步骤的核心交际功能。
- 词汇要求：高频基础词汇，短语为主（如 I want, a coffee, how much）。
- 约束生成（Constraints）：只生成 2-3 个最核心的名词或极其基础的动词短语。
- 示例：（点单 Step）Constraints: ["I want", "coffee", "large"]

### Depth 2 - 进阶级（Intermediate - 礼貌与细节修饰）
- 目标：在完成基础功能上，增加细节修饰、选择变体和基础的礼貌用语。
- 词汇要求：进阶复合词汇，完整的简单句（如 I would like, instead of, with oat milk）。
- 约束生成（Constraints）：生成 3-4 个约束，必须包含至少一个礼貌句型或修饰语。
- 示例：（点单 Step）Constraints: ["I would like", "a latte", "with oat milk", "please"]

### Depth 3 - 精通级（Advanced - 地道表达与长难句）
- 目标：使用母语者常用的地道习语、委婉语或复杂从句来处理该步骤。
- 词汇要求：高级词汇、虚拟语气、长难句（如 I was wondering if, would it be possible to, I'd appreciate it if）。
- 约束生成（Constraints）：生成 4-5 个约束，必须包含高级交际句型。
- 示例：（点单 Step）Constraints: ["I was wondering if", "could possibly make it", "decaf", "extra shot"]
"""
```

### 2.3 Depth 梯度对比表

| 维度 | Depth 1 | Depth 2 | Depth 3 |
|------|---------|---------|---------|
| **词汇类型** | 基础短语 | 进阶复合词 | 高级词汇/习语 |
| **句型复杂度** | 单词/简单短语 | 完整简单句 | 虚拟语气/长难句 |
| **约束数量** | 2-3 个 | 3-4 个 | 4-5 个 |
| **礼貌程度** | 无修饰 | 基础礼貌 | 委婉地道 |
| **示例** | "I want coffee" | "I would like a latte, please" | "I was wondering if you could possibly make it decaf" |

---

## 三、DEPTH_META 配置（已定稿）

```python
DEPTH_META = {
    1: {
        "desc": "入门级（Foundation）",
        "keyword": "核心生存句型",
        "constraint_count_range": (2, 3),
        "scenario_count": "3-5",
        "example": '["I want", "coffee", "large"]',
    },
    2: {
        "desc": "进阶级（Intermediate）",
        "keyword": "礼貌与细节修饰",
        "constraint_count_range": (3, 4),
        "scenario_count": "2-4",
        "example": '["I would like", "a latte", "with oat milk", "please"]',
    },
    3: {
        "desc": "精通级（Advanced）",
        "keyword": "地道表达与长难句",
        "constraint_count_range": (4, 5),
        "scenario_count": "1-2",
        "example": '["I was wondering if", "could possibly make it", "decaf", "extra shot"]',
    },
}
```

---

## 四、Prompt 模板（已定稿）

```python
PROMPT_TEMPLATE = """[任务：为 {topic_title} 生成功能迁移微场景]

## 话题信息
- 话题名称：{topic_title}
- 话题中文名：{topic_title_zh}
- 用户角色：{role_name}

## 难度等级定义
{DEPTH_DEFINITIONS}

## 本次生成目标
**本次生成：Depth {depth}（{depth_desc}）**
关键词：{depth_keyword}

## 已有场景参考（用于避免重复）
{existing_scenarios}

## 生成要求
{instruction_str}

## 输出格式（仅输出 JSON，不要包含其他文字）
{{
    "scenarios": [
        {{
            "scenario_code": "UNIQUE_CODE",
            "scenario_name": "场景名称（{depth_desc}）",
            "intent_desc": "教学意图描述",
            "scene_desc": "情境描述",
            "step_order": 1-3,
            "flow_explanation": "该场景在整体流程中的位置（必须与同 Topic 其他 Depth 的 step_order 一致）",
            "is_entry_point": true/false,
            "constraints": [
                {{
                    "constraint_text": "目标词汇/短语（{depth_keyword}）",
                    "constraint_type": "word" or "phrase" or "sentence",
                    "weight": 1.0,
                    "hint_cn": "中文提示"
                }}
            ]
        }}
    ]
}}

## 质量检查清单
- [ ] 场景是否明显符合 Depth {depth} 的定义？
- [ ] 词汇复杂度是否与 depth 匹配？
- [ ] step_order 是否与其他 Depth 一致（仅表达方式不同）？
- [ ] 是否避免了与已有场景重复？
- [ ] 约束词数量是否在 {constraint_count_range} 范围内？
"""
```

---

## 五、实现方案

### 5.1 代码修改点

在 `graph_generator.py` 中新增：

```python
# ============================================================
# DEPTH 定义常量（已定稿）
# ============================================================

DEPTH_DEFINITIONS = """
## 难度等级定义（请严格遵循）
注意：同一 Topic 的不同 Depth 必须保持相同的业务流程（step_order），仅仅是表达的复杂度升级！绝不要改变基本故事情节！

### Depth 1 - 入门级（Foundation - 核心生存句型）
- 目标：用最简单的词汇完成该步骤的核心交际功能。
- 词汇要求：高频基础词汇，短语为主（如 I want, a coffee, how much）。
- 约束生成（Constraints）：只生成 2-3 个最核心的名词或极其基础的动词短语。

### Depth 2 - 进阶级（Intermediate - 礼貌与细节修饰）
- 目标：在完成基础功能上，增加细节修饰、选择变体和基础的礼貌用语。
- 词汇要求：进阶复合词汇，完整的简单句（如 I would like, instead of, with oat milk）。
- 约束生成（Constraints）：生成 3-4 个约束，必须包含至少一个礼貌句型或修饰语。

### Depth 3 - 精通级（Advanced - 地道表达与长难句）
- 目标：使用母语者常用的地道习语、委婉语或复杂从句来处理该步骤。
- 词汇要求：高级词汇、虚拟语气、长难句（如 I was wondering if, would it be possible to）。
- 约束生成（Constraints）：生成 4-5 个约束，必须包含高级交际句型。
"""

DEPTH_META = {
    1: {"desc": "入门级（Foundation）", "keyword": "核心生存句型", "constraint_range": (2, 3)},
    2: {"desc": "进阶级（Intermediate）", "keyword": "礼貌与细节修饰", "constraint_range": (3, 4)},
    3: {"desc": "精通级（Advanced）", "keyword": "地道表达与长难句", "constraint_range": (4, 5)},
}
```

修改 `generate_micro_scenarios()` 函数：

```python
async def generate_micro_scenarios(topic: Topic, depth: int, db: Session) -> dict:
    # ... 现有代码 ...

    # 构建 Prompt（使用 DEPTH_DEFINITIONS）
    meta = DEPTH_META[depth]
    prompt = f"""[任务：为 {topic.title} 生成功能迁移微场景]
话题：{topic.title} ({getattr(topic, 'title_zh', '')})

{DEPTH_DEFINITIONS}

**本次生成：Depth {depth}（{meta['desc']}）**
关键词：{meta['keyword']}

{context_str}

{instruction_str}

{JSON_OUTPUT_TEMPLATE}
"""
```

---

## 六、评审要点（已解答）

| 问题 | 解答 |
|------|------|
| ✅ 删除"对话轮数" | 已删除，轮数由运行时引擎控制 |
| ✅ 删除"容错性" | 已删除，容错由 MasteryScorer 处理 |
| ✅ 区分 Depth vs Twists | 已明确：Depth=静态词汇升级，Twists=动态意外注入 |
| ✅ 删除"故事情节突变" | 已删除，Depth 只改变表达方式，不改变流程 |
| ✅ 聚焦交际句法复杂度 | 已重构，核心聚焦词汇/句型难度 |

---

## 七、后续工作

| 优先级 | 任务 | 说明 |
|--------|------|------|
| P0 | 修改 graph_generator.py | 集成已定稿的 DEPTH_DEFINITIONS |
| P1 | 测试 Depth 1 场景 | 验证基础词汇生成效果 |
| P1 | 测试 Depth 2 场景 | 验证礼貌用语生成效果 |
| P1 | 测试 Depth 3 场景 | 验证高级句型生成效果 |
| P2 | 验证梯度差异 | 确保同一 Topic 不同 Depth 的词汇有明显升级 |

---

## 八、引擎层架构规则（Phase 3 & 4 预备）

> **重要**：以下两条规则需深刻记忆，在编写 `mastery_scorer.py` 和 `session_planner.py` 时必须严格遵守。

### 8.1 评分引擎的"向下兼容豁免权" (Downward Compatibility Exemption)

**业务痛点**：当图谱目标是基础表达（如 Depth 1 的 "I want"），而用户脱口而出更高级的表达（如 Depth 2 的 "I would like"）时，机械的匹配会误判用户打错。

**开发约束**：在编写 `mastery_scorer.py` 的 LLM 评估 Prompt 时，必须显式注入一条豁免规则：

> **"当检测目标约束词时，如果用户使用了语义相同且更高级、更礼貌的表达方式，必须判定为 100% 掌握，严禁死板的字符串匹配降分。"**

**示例**：

| 图谱目标 | 用户实际表达 | 正确判定 |
|---------|-------------|---------|
| "I want" (Depth 1) | "I would like" (Depth 2) | ✅ 100% 掌握 |
| "coffee" (Depth 1) | "a latte, please" (Depth 2) | ✅ 100% 掌握 |
| "how much" (Depth 1) | "what's the price of" (Depth 2) | ✅ 100% 掌握 |

### 8.2 调度引擎的"全局能力跳级" (Global Placement)

**业务痛点**：用户在"麦当劳"已经练到了 Depth 2，去"星巴克"时如果再从 Depth 1 开始会觉得枯燥；但如果直接跳过星巴克的"点单"环节去"结账"又违背常理。

**开发约束**：在编写 `session_planner.py` 时，必须遵守以下规则：

| 维度 | 约束 |
|------|------|
| **业务时间线** | `step_order` 绝对不可跳跃 |
| **难度动态发牌** | `depth` 必须依据用户全局领域能力等级动态决定 |

**具体逻辑**：

```
用户进入新话题（如"星巴克"）时：
1. 拉取 step_order = 1 的节点开局（时间线起点）
2. 但拉取哪个 Depth，不能无脑选 Depth 1
3. 必须查询用户的 Domain Competence Level（全局领域能力等级）
4. 直接拉取该等级对应的节点（如 Step 1 - Depth 2）

示例：
- 用户在"麦当劳"已掌握 Depth 2 的点单技能
- 进入"星巴克"时 → step_order=1, depth=2（直接跳级）
- 进入"机场"时 → step_order=1（新领域），depth=1（从头开始）
```

---

## 九、分层架构总结

```
┌─────────────────────────────────────────────────────────────────┐
│                        线下图谱构建                               │
│  graph_generator.py                                              │
│  ├── 职责：铺设"词汇地图"（Constraints）                          │
│  ├── 输出：JSON（意图、描述、约束词）                              │
│  └── Depth = 词汇复杂度由简入深（1→2→3）                          │
├─────────────────────────────────────────────────────────────────┤
│                        线上对话引擎                               │
│  DialogueEngine + MasteryScorer                                  │
│  ├── 职责：驾驶"汽车"在地图上行驶                                  │
│  ├── 决定：对话轮数、容错度、情节跌宕                              │
│  ├── Twist = 运行时动态注入意外                                   │
│  └── 豁免规则：更高级表达 = 100% 掌握                             │
├─────────────────────────────────────────────────────────────────┤
│                        调度引擎                                   │
│  session_planner.py                                              │
│  ├── 职责：决定下一站去哪里、难度几颗星                            │
│  ├── 规则：step_order 不可跳，depth 动态发牌                      │
│  └── 依据：Domain Competence Level（全局能力等级）                 │
└─────────────────────────────────────────────────────────────────┘
```
