"""
MasteryScorer - SM-2 简化版掌握度评分引擎

设计参考 SuperMemo SM-2 算法，做了两处简化：
1. 不追踪 Easiness Factor（EF），用统一的增长率代替
2. 时间衰减用指数函数代替 SM-2 的区间调度

核心公式：
  - 正确使用: new = current + (100 - current) * GROWTH_RATE * quality
  - 错误使用: new = current * (1 - DECAY_ON_ERROR)
  - 时间衰减: effective = current * exp(-ln(2) * days / HALF_LIFE_DAYS)

掌握度阈值（与 session_planner.py 中的 MASTERY_THRESHOLD_FOR_TIER_UP 对齐）：
  0  ~ 30: 未入门
  30 ~ 60: 学习中
  60 ~ 80: 基本掌握
  80 ~100: 熟练（可晋级）
"""

from __future__ import annotations

import datetime
import math

# ── 常量（均可在此统一调整） ─────────────────────────────────────────────────

# 掌握度半衰期（天）：练习后 N 天不复习，mastery 衰减到一半
HALF_LIFE_DAYS: float = 7.0

# 正确使用时的增长率：决定"每次正确使用能涨多少分"
# 0.30 意味着离满分的距离每次缩短 30%（收益递减曲线）
GROWTH_RATE: float = 0.30

# 错误/未命中时的衰减率
DECAY_ON_ERROR: float = 0.12

# 每次正确命中的最小收益（防止高分时几乎涨不了分）
MIN_GAIN_ON_CORRECT: float = 3.0

# L1 命中质量（精准正则匹配）
L1_EXACT_QUALITY: float = 1.0
# L1 命中质量（词干匹配，表示用了变形词）
L1_STEM_QUALITY: float = 0.80


# ═══════════════════════════════════════════════════════════════════════════
# 核心公式
# ═══════════════════════════════════════════════════════════════════════════

def update_mastery(current: float, was_correct: bool, quality: float = 1.0) -> float:
    """
    SM-2 简化版掌握度更新。

    Args:
        current:     当前掌握度 (0~100)
        was_correct: 是否正确/自然地使用了该表达
        quality:     使用质量 0~1（L1精准=1.0, L1词干=0.8, L2上下文评估给出）

    Returns:
        更新后的掌握度 (0~100)
    """
    if was_correct:
        gain = (100.0 - current) * GROWTH_RATE * quality
        gain = max(gain, MIN_GAIN_ON_CORRECT)
        return min(100.0, current + gain)
    else:
        return max(0.0, current * (1.0 - DECAY_ON_ERROR))


def compute_decayed_mastery(mastery: float, last_practiced_at: datetime.datetime | None) -> float:
    """
    计算经时间衰减后的「有效掌握度」，用于 session_planner 的深度晋级判断。

    注意：不会写回数据库，仅用于只读查询时的实时修正。
    写回时机：每次练习命中时通过 update_mastery 更新。
    """
    if last_practiced_at is None or mastery <= 0:
        return mastery

    elapsed = datetime.datetime.utcnow() - last_practiced_at
    days = elapsed.total_seconds() / 86400.0

    if days < 0.5:           # 同一天内，不衰减
        return mastery

    decay_factor = math.exp(-math.log(2) * days / HALF_LIFE_DAYS)
    return mastery * decay_factor


def effective_mastery(mastery: float, last_practiced_at: datetime.datetime | None) -> float:
    """compute_decayed_mastery 的别名，语义更清晰的调用方式"""
    return compute_decayed_mastery(mastery, last_practiced_at)


# ═══════════════════════════════════════════════════════════════════════════
# L1 文本规范化工具（无需 spaCy，零额外依赖）
# ═══════════════════════════════════════════════════════════════════════════

# 常见英语缩写展开表（影响最多的短语匹配场景）
CONTRACTIONS: dict[str, str] = {
    "i'd like to": "i would like to",
    "i'd":         "i would",
    "i'm":         "i am",
    "i've":        "i have",
    "i'll":        "i will",
    "i'd've":      "i would have",
    "they're":     "they are",
    "we're":       "we are",
    "you're":      "you are",
    "it's":        "it is",
    "that's":      "that is",
    "there's":     "there is",
    "don't":       "do not",
    "doesn't":     "does not",
    "didn't":      "did not",
    "can't":       "cannot",
    "couldn't":    "could not",
    "won't":       "will not",
    "wouldn't":    "would not",
    "isn't":       "is not",
    "aren't":      "are not",
    "let's":       "let us",
    "gonna":       "going to",
    "wanna":       "want to",
    "gotta":       "got to",
}

# 简单后缀规则（poor man's stemmer，覆盖最常见变形）
# 格式：(suffix_to_strip, min_stem_len)
# 注意：longer suffix rules must come first to prevent partial overlap
# e.g. "ied" must precede "ed" so "tried"→"try" not "tried"→"tri"
_SUFFIX_RULES: list[tuple[str, int]] = [
    ("ying",  2),   # studying→study, crying→cry
    ("ied",   2),   # tried→try, studied→study
    ("ies",   2),   # tries→try, studies→study
    ("ing",   3),   # ordering→order, running→run
    ("ed",    3),   # ordered→order, walked→walk
    ("er",    3),   # bigger→big  (risk: "burger"→"burg"; acceptable trade-off)
    ("est",   3),   # biggest→big
    ("es",    3),   # matches→match, wishes→wish
    ("s",     3),   # burgers→burger, orders→order
]


def normalize_text(text: str) -> str:
    """
    对用户输入做轻量规范化：小写 + 缩写展开。
    用于 L1 节点命中匹配的预处理。
    """
    text = text.lower().strip()
    for contraction, expansion in CONTRACTIONS.items():
        text = text.replace(contraction, expansion)
    return text


def simple_stem(word: str) -> str:
    """
    简单英语词干提取（无需 NLTK/spaCy）。
    只处理最常见的后缀，错误率可接受。
    """
    for suffix, min_len in _SUFFIX_RULES:
        if word.endswith(suffix):
            stem = word[: -len(suffix)]
            if len(stem) >= min_len:
                # -ied / -ying / -ies → 还原 -y 结尾（tried→try, studying→study）
                if suffix in ("ied", "ying", "ies"):
                    return stem + "y"
                return stem
    return word
