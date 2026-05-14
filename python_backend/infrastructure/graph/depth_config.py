"""
depth_config.py — 图谱难度层级配置

统一管理 DEPTH_DEFINITIONS 和 DEPTH_META，避免代码重复。

版本: v1.0 (2026-05-13)
"""

# ============================================================
# DEPTH 定义常量（v1.2 已定稿）
# 核心约束：Depth 仅控制"词汇/句法复杂度"，不控制对话轮数或故事情节
# 同一 Topic 的不同 Depth 必须保持相同的 step_order
# ============================================================

DEPTH_DEFINITIONS = """
## Difficulty Level Definitions (Strictly Follow)

IMPORTANT: All depths within the same topic must maintain the SAME step_order. Only expression complexity increases, NOT the basic storyline!

NOTE: The examples below illustrate structural complexity levels (word → phrase → sentence), NOT role-specific expressions. Generate expressions from the learner's perspective based on the Learner Role in Scene Context.

### Depth 1 - Foundation (Core Survival Phrases)
- Goal: Complete the core communicative function with simplest vocabulary.
- Vocabulary: High-frequency basic words, phrases preferred (e.g., single nouns, basic verbs).
- Constraints: Generate only 2-3 most essential nouns or basic verb phrases.
- Example Constraints: ["basic noun phrase", "simple verb phrase", "question word"]

### Depth 2 - Intermediate (Politeness & Detail Modifiers)
- Goal: Add detail modifiers, variation options, and basic polite expressions on top of basic function.
- Vocabulary: Advanced compound words, complete simple sentences (e.g., polite requests, conditional phrases).
- Constraints: Generate 3-4 constraints, MUST include at least one polite expression or modifier.
- Example Constraints: ["polite request phrase", "modifier phrase", "conditional sentence", "please/politeness marker"]

### Depth 3 - Advanced (Native Expressions & Complex Sentences)
- Goal: Use native idioms, indirect requests, or complex clauses commonly used by native speakers.
- Vocabulary: Advanced vocabulary, subjunctive mood, complex sentences (e.g., embedded sentences, indirect requests).
- Constraints: Generate 4-5 constraints, MUST include advanced communicative patterns.
- Example Constraints: ["indirect request sentence", "embedded sentence", "subjunctive sentence", "idiomatic sentence"]
"""

DEPTH_META = {
    1: {
        "desc": "Foundation",
        "keyword": "core survival phrases",
        "constraint_range": (2, 3),
        "scenario_count": "3-5",
    },
    2: {
        "desc": "Intermediate",
        "keyword": "politeness and detail modifiers",
        "constraint_range": (3, 4),
        "scenario_count": "2-4",
    },
    3: {
        "desc": "Advanced",
        "keyword": "native expressions and complex sentences",
        "constraint_range": (4, 5),
        "scenario_count": "1-2",
    },
}
