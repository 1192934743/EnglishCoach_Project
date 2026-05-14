"""
TopicGenerator — 三层话题生成策略

相似度判断  →  策略

> 0.75    →  直接使用已有话题（零 LLM 成本）
0.40~0.75 →  参考引导生成：把最近似话题的结构作为 few-shot 模板，
              AI 生成"同等质量但内容全新"的话题
< 0.40    →  纯净生成：从零生成，无参考约束

所有 AI 生成的话题都写入 DB + 加入 VectorStore，下次直接命中，越用越好。

相似度计算：Jaccard 词集重叠（无需外部模型）
  对于有 embedding 的话题，同时用 VectorStore cosine 并取最大值
"""

from __future__ import annotations

import json
import logging
import re
import asyncio
from typing import Optional

from sqlalchemy.orm import Session

from database import SessionLocal, Topic, topic_title_zh_fallback
from application.services.mastery_scorer import normalize_text

logger = logging.getLogger("EnglishCoach")

# ── 质量达标配置 ─────────────────────────────────────────────────────────────
MAX_RETRIES = 3           # 最多重试次数
TARGET_GRADE = "A"        # 目标等级
RETRY_GRADES = ["B", "C", "D"]  # 触发重试的等级

# 禁止的角色关键词（质量检查用）
# 注意：使用精确匹配，避免误杀如 "Shop Assistant" 这类合理角色
_FORBIDDEN_ROLE_KEYWORDS = [
    # 通用教学角色
    "english coach",
    "language coach",
    "english tutor",
    "language tutor",
    "english teacher",
    "language teacher",
    "english instructor",
    "language instructor",
    "english trainer",
    "language trainer",
    # 泛化的服务角色
    "english assistant",
    "language assistant",
    "teaching assistant",
    "generic",
    "service provider",
    "customer service",
]

# 重试时的质量反馈提示
_RETRY_PROMPTS = [
    "请生成更丰富、更有深度的内容，确保包含足够的词汇和句型。",
    "请确保包含更多词汇标签和句型模式，增加场景深度。",
    "请优化话题结构，增加 depth_level=3 的节点数量。",
]

# ── 生成参数 ─────────────────────────────────────────────────────────────────
# 注意：使用 Jaccard 相似度（词集重叠），值域与 cosine 不同，通常 < 0.15
# 实测基准（3 话题 DB）：
#   "job interview for software engineer" vs "Technical Job Interview" → ~0.067（实际同一话题）
#   "coffee shop ordering" vs "McDonald's Ordering"                   → ~0.038（同类话题）
#   "airport check-in"    vs any food/job topic                       → ~0.000（完全不同）
SIMILARITY_REUSE_THRESHOLD = 0.055      # 高于此值：直接复用（同一话题）
SIMILARITY_REFERENCE_THRESHOLD = 0.020 # 0.02~0.055：参考生成（同类话题）；低于：纯净生成
GENERATE_MAX_TOKENS = 800
GENERATE_TIMEOUT = 30.0

# ── 输出 JSON Schema（AI 必须遵守的结构）────────────────────────────────────
_OUTPUT_SCHEMA = {
    "title": "string — short English topic name, e.g. 'Coffee Shop Ordering'",
    "title_zh": "string — short Chinese UI label for Chinese-mode users, e.g. '咖啡店点餐' (2–12 chars typical)",
    "category": "string — topic category, e.g. 'Food & Drink', 'Travel', 'Career'",
    "role_name": "string — AI's role in the scene, e.g. 'Barista'",
    "learner_level": "string — 'Beginner' | 'Intermediate' | 'Professional'",
    "scene_prompt": "string — 1-2 sentence immersive scene description for the AI",
    "vocab_tags": ["list of 8-12 key vocabulary words or short phrases"],
    "sentence_patterns": ["list of 4-6 key sentence starters or patterns"],
    "scene_specific_rules": ["list of 2-3 coaching guardrail rules for this scene"],
    "difficulty_tiers": {
        "1": {"rules": ["list — 1 rule describing depth-1 focus"]},
        "2": {"rules": ["list — 1 rule describing depth-2 focus"]},
        "3": {"rules": ["list — 1 rule describing depth-3 focus"]}
    },
    "nodes": [
        {
            "text": "string — the expression to practice",
            "type": "word | phrase | sentence",
            "depth_level": "integer 1, 2, or 3"
        }
    ]
}
_SCHEMA_STR = json.dumps(_OUTPUT_SCHEMA, indent=2, ensure_ascii=False)

# depth_level 节点数量建议（引导 AI 产出平衡的节点）
_NODE_DISTRIBUTION_HINT = (
    "Include: 3-4 nodes at depth_level=1 (survival basics), "
    "2-3 nodes at depth_level=2 (intermediate), "
    "1-2 nodes at depth_level=3 (advanced/situational)."
)


# ═══════════════════════════════════════════════════════════════════════════
# 公开接口
# ═══════════════════════════════════════════════════════════════════════════

async def get_or_generate_topic(
    description: str,
    openai_client,
    db: Optional[Session] = None,
    domain: Optional[str] = None,
    role_hint: Optional[str] = None,
) -> Topic:
    """
    主入口：根据描述返回一个 Topic（已有或新生成）。

    - 调用方通过 TaskPacket 使用返回的 Topic
    - 所有生成的 Topic 均持久化到 DB
    - domain 由调用方直接传入，不经 LLM 生成
    - role_hint 建议角色名称，引导 AI 生成更具体的角色

    此函数绝不抛出异常，失败时返回 fallback Topic。
    """
    should_close = db is None
    db = db or SessionLocal()
    try:
        return await _get_or_generate(description, openai_client, db, domain=domain, role_hint=role_hint)
    except Exception as e:
        logger.error(f"[TopicGenerator] Unexpected error: {e}", exc_info=True)
        return _get_fallback_topic(db)
    finally:
        if should_close:
            db.close()


async def generate_topic_with_quality(
    description: str,
    openai_client,
    db: Optional[Session] = None,
    *,
    domain: Optional[str] = None,
    role_hint: Optional[str] = None,
    max_retries: int = MAX_RETRIES,
    target_grade: str = TARGET_GRADE,
) -> Optional[Topic]:
    """
    生成话题直到质量达标，或达到最大重试次数。

    返回 None 表示生成失败（所有尝试都未达标）。
    """
    should_close = db is None
    db = db or SessionLocal()
    try:
        for attempt in range(max_retries):
            # 1. 生成话题
            topic_data = await _generate_topic_data(
                description, openai_client, domain=domain, role_hint=role_hint
            )

            # 2. 质量评估
            grade, score, issues = _assess_topic_quality(topic_data)

            if grade == target_grade:
                # 达标，入库
                return _save_to_db(topic_data, db, domain=domain, quality_grade=grade, quality_score=score, quality_issues=issues, generation_attempts=attempt + 1)

            # 未达标，记录日志
            logger.warning(
                f"[TopicGenerator] Attempt {attempt + 1}/{max_retries} "
                f"grade={grade} (score={score}), issues={issues}"
            )

            # 3. 如果还有重试机会，带着反馈重新生成
            if attempt < max_retries - 1:
                feedback = _format_quality_feedback(issues)
                topic_data = await _generate_topic_data(
                    description, openai_client, domain=domain, role_hint=role_hint, quality_feedback=feedback
                )

                # 再次评估
                grade, score, issues = _assess_topic_quality(topic_data)
                if grade == target_grade:
                    return _save_to_db(topic_data, db, domain=domain, quality_grade=grade, quality_score=score, quality_issues=issues, generation_attempts=attempt + 1)

        # 所有尝试都失败
        logger.error(f"[TopicGenerator] Failed to generate topic after {max_retries} attempts")
        return None
    finally:
        if should_close:
            db.close()


# ═══════════════════════════════════════════════════════════════════════════
# 内部流程
# ═══════════════════════════════════════════════════════════════════════════

async def _get_or_generate(
    description: str,
    openai_client,
    db: Session,
    *,
    domain: Optional[str] = None,
    role_hint: Optional[str] = None,
) -> Topic:
    all_topics = db.query(Topic).all()

    # ── 1. 相似度匹配 ──────────────────────────────────────────────────────
    best_topic, best_sim = _find_best_match(description, all_topics)
    logger.info(f"[TopicGenerator] '{description}' → best match: "
                f"'{best_topic.title if best_topic else None}' sim={best_sim:.2f}")

    # ── 层 1：直接复用 ──────────────────────────────────────────────────────
    if best_sim >= SIMILARITY_REUSE_THRESHOLD and best_topic is not None:
        # 旧库话题可能没有 title_zh：用内置英文→中文表补写 DB，便于中文界面与 API
        if not (getattr(best_topic, "title_zh", None) or "").strip():
            zh_fb = topic_title_zh_fallback(best_topic.title)
            if zh_fb:
                best_topic.title_zh = zh_fb
                db.commit()
                db.refresh(best_topic)
                logger.info(
                    "[TopicGenerator] Backfilled title_zh for reused topic %r",
                    best_topic.title,
                )
        # 直接复用场景下也写入 domain（新生成时 domain 由调用方保证，存量可能缺失）
        if domain and not getattr(best_topic, "domain", None):
            best_topic.domain = domain
            db.commit()
        logger.info(f"[TopicGenerator] Tier-1 reuse: '{best_topic.title}'")
        return best_topic

    # ── 层 2：参考引导生成 ────────────────────────────────────────────────
    if best_sim >= SIMILARITY_REFERENCE_THRESHOLD and best_topic is not None:
        logger.info(f"[TopicGenerator] Tier-2 reference-guided generation")
        return await _generate_with_reference(description, best_topic, openai_client, db, domain=domain, role_hint=role_hint)

    # ── 层 3：纯净生成 ────────────────────────────────────────────────────
    logger.info(f"[TopicGenerator] Tier-3 pure generation (no good reference)")
    return await _generate_from_scratch(description, openai_client, db, domain=domain, role_hint=role_hint)


# ── 相似度计算 ─────────────────────────────────────────────────────────────

def _find_best_match(
    description: str,
    topics: list[Topic],
) -> tuple[Optional[Topic], float]:
    """
    Jaccard 词集重叠相似度。
    将话题的 title + vocab_tags + sentence_patterns + category 合并为词集。
    """
    if not topics:
        return None, 0.0

    desc_words = _text_to_wordset(description)
    if not desc_words:
        return None, 0.0

    best_topic = None
    best_sim = 0.0

    for topic in topics:
        sim = _topic_similarity(desc_words, topic)
        if sim > best_sim:
            best_sim = sim
            best_topic = topic

    return best_topic, best_sim


def _text_to_wordset(text: str) -> set[str]:
    """Normalize → tokenize → strip stop words"""
    _STOP = {"a", "an", "the", "and", "or", "in", "at", "to", "for",
             "of", "with", "on", "is", "are", "i", "you", "my", "your"}
    words = set(normalize_text(text).split())
    return words - _STOP


def _topic_similarity(desc_words: set[str], topic: Topic) -> float:
    topic_words: set[str] = set()
    topic_words.update(_text_to_wordset(topic.title or ""))
    topic_words.update(_text_to_wordset(topic.category or ""))
    for tag in (topic.vocab_tags or []):
        topic_words.update(_text_to_wordset(tag))
    for pat in (topic.sentence_patterns or []):
        topic_words.update(_text_to_wordset(pat))

    if not topic_words or not desc_words:
        return 0.0

    intersection = len(topic_words & desc_words)
    union = len(topic_words | desc_words)
    return intersection / union if union > 0 else 0.0


# ── 层 2：参考引导生成 ─────────────────────────────────────────────────────

def _build_reference_template(topic: Topic) -> dict:
    """
    从已有 Topic 提取纯内容结构（去掉运行时字段），
    作为 few-shot 模板传给 AI。
    """
    return {
        "title": topic.title,
        "title_zh": getattr(topic, "title_zh", None) or "",
        "category": topic.category,
        "role_name": topic.role_name,
        "learner_level": topic.learner_level,
        "scene_prompt": topic.system_prompt,
        "vocab_tags": topic.vocab_tags or [],
        "sentence_patterns": topic.sentence_patterns or [],
        "scene_specific_rules": topic.scene_specific_rules or [],
        "difficulty_tiers": topic.difficulty_tiers or {},
        "nodes": [],
    }


async def _generate_with_reference(
    description: str,
    reference: Topic,
    openai_client,
    db: Session,
    *,
    domain: Optional[str] = None,
    role_hint: Optional[str] = None,
    quality_feedback: Optional[str] = None,
) -> Topic:
    ref_template = _build_reference_template(reference)
    ref_json = json.dumps(ref_template, indent=2, ensure_ascii=False)

    domain_hint = f'\nTARGET DOMAIN: "{domain}"' if domain else ""
    role_hint_str = f'\nSUGGESTED ROLE: "{role_hint}"' if role_hint else ""
    quality_hint = f"\n\n质量改进要求：{quality_feedback}" if quality_feedback else ""

    prompt = f"""You are designing English language practice topics for a conversation coaching app.

TARGET TOPIC: "{description}"
{domain_hint}
{role_hint_str}
{quality_hint}

REFERENCE EXAMPLE (highest-quality similar topic from our library — use it as a structural template):
{ref_json}

Create a NEW topic for the target description by:
1. Keeping the SAME structural quality (depth of vocab_tags, node distribution, rule style)
2. Replacing ALL content with material appropriate for the new target topic
3. Maintaining the same learner_level unless the new topic clearly suits a different level

{_NODE_DISTRIBUTION_HINT}

Always include title_zh (natural Chinese for the same topic as title).

Return ONLY a JSON object matching this exact schema (no markdown, no explanation):
{_SCHEMA_STR}"""

    return await _call_llm_and_save(prompt, db, openai_client, domain=domain)


# ── 层 3：纯净生成 ─────────────────────────────────────────────────────────

async def _generate_from_scratch(
    description: str,
    openai_client,
    db: Session,
    *,
    domain: Optional[str] = None,
    role_hint: Optional[str] = None,
    quality_feedback: Optional[str] = None,
) -> Topic:
    domain_hint = f'\nTARGET DOMAIN: "{domain}"' if domain else ""
    role_hint_str = f'\nSUGGESTED ROLE: "{role_hint}"' if role_hint else ""
    quality_hint = f"\n\n质量改进要求：{quality_feedback}" if quality_feedback else ""

    prompt = f"""You are designing English language practice topics for a conversation coaching app.

TARGET TOPIC: "{description}"
{domain_hint}
{role_hint_str}
{quality_hint}

Create a complete topic with rich coaching content.
{_NODE_DISTRIBUTION_HINT}

Always include title_zh (natural Chinese for the same topic as title).

Return ONLY a JSON object matching this exact schema (no markdown, no explanation):
{_SCHEMA_STR}"""

    return await _call_llm_and_save(prompt, db, openai_client, domain=domain)


# ── 内部：生成原始数据（不直接入库）──────────────────────────────────────────

async def _generate_topic_data(
    description: str,
    openai_client,
    *,
    domain: Optional[str] = None,
    role_hint: Optional[str] = None,
    quality_feedback: Optional[str] = None,
) -> dict:
    """生成话题数据字典（用于质量达标流程）"""
    domain_hint = f'\nTARGET DOMAIN: "{domain}"' if domain else ""
    role_hint_str = f'\nSUGGESTED ROLE: "{role_hint}"' if role_hint else ""
    quality_hint = f"\n\n质量改进要求：{quality_feedback}" if quality_feedback else ""

    prompt = f"""You are designing English language practice topics for a conversation coaching app.

TARGET TOPIC: "{description}"
{domain_hint}
{role_hint_str}
{quality_hint}

Create a complete topic with rich coaching content.
{_NODE_DISTRIBUTION_HINT}

Always include title_zh (natural Chinese for the same topic as title).

IMPORTANT: Avoid generating generic roles like "English Coach", "Language Tutor", or "Teacher".
Use specific roles like "Barista", "Taxi Driver", "Shop Assistant" etc.

Return ONLY a JSON object matching this exact schema (no markdown, no explanation):
{_SCHEMA_STR}"""

    try:
        resp = await asyncio.wait_for(
            openai_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a language curriculum designer. Output valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=GENERATE_MAX_TOKENS,
                response_format={"type": "json_object"},
            ),
            timeout=GENERATE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"TopicGenerator LLM timeout (>{GENERATE_TIMEOUT}s)")

    raw = resp.choices[0].message.content
    data = _parse_and_validate(raw)

    # 填充缺失的 title_zh
    if not (data.get("title_zh") or "").strip():
        title_en = (data.get("title") or "").strip()
        if title_en:
            z = await llm_fill_title_zh_only(openai_client, title_en)
            if z:
                data["title_zh"] = z

    return data


# ── LLM 调用 + 持久化 ──────────────────────────────────────────────────────

_FILL_TITLE_ZH_TIMEOUT = 18.0


# ═══════════════════════════════════════════════════════════════════════════════
# 质量达标机制
# ═══════════════════════════════════════════════════════════════════════════════

def _assess_topic_quality(data: dict) -> tuple[str, int, list[dict]]:
    """
    评估话题质量。
    返回: (grade, score, issues)
    """
    score = 100
    issues = []

    # ── 1. 角色正确性（严重问题，直接降级）────────────────────────────────
    role_name = data.get("role_name", "")
    if _is_invalid_role(role_name):
        score -= 50
        issues.append({"type": "invalid_role", "severity": "high", "detail": role_name})

    # ── 2. vocab_tags 丰富度 ──────────────────────────────────────────────
    vocab_tags = data.get("vocab_tags", [])
    if not vocab_tags:
        score -= 20
        issues.append({"type": "missing_vocab_tags", "severity": "high"})
    elif len(vocab_tags) < 5:
        score -= 10
        issues.append({"type": "insufficient_vocab_tags", "severity": "medium", "count": len(vocab_tags)})
    elif len(vocab_tags) < 8:
        score -= 5
        issues.append({"type": "low_vocab_tags", "severity": "low", "count": len(vocab_tags)})

    # ── 3. sentence_patterns 丰富度 ───────────────────────────────────────
    patterns = data.get("sentence_patterns", [])
    if not patterns:
        score -= 20
        issues.append({"type": "missing_sentence_patterns", "severity": "high"})
    elif len(patterns) < 3:
        score -= 10
        issues.append({"type": "insufficient_patterns", "severity": "medium", "count": len(patterns)})

    # ── 4. nodes/micro_scenarios 完整性 ──────────────────────────────────
    nodes = data.get("nodes", [])
    if not nodes:
        score -= 20
        issues.append({"type": "missing_nodes", "severity": "high"})
    else:
        if len(nodes) < 5:
            score -= 10
            issues.append({"type": "insufficient_nodes", "severity": "medium", "count": len(nodes)})

        # 检查深度分布
        depth_counts = {1: 0, 2: 0, 3: 0}
        for node in nodes:
            d = node.get("depth_level", 1)
            depth_counts[d] = depth_counts.get(d, 0) + 1

        if depth_counts[1] < 2:
            score -= 5
            issues.append({"type": "insufficient_depth1_nodes", "severity": "low"})
        if depth_counts[3] == 0:
            score -= 5
            issues.append({"type": "missing_depth3_nodes", "severity": "low"})

    # ── 5. title_zh 存在性 ────────────────────────────────────────────────
    if not data.get("title_zh"):
        score -= 10
        issues.append({"type": "missing_title_zh", "severity": "medium"})

    # ── 6. difficulty_tiers 完整性 ────────────────────────────────────────
    tiers = data.get("difficulty_tiers", {})
    if not tiers or len(tiers) < 3:
        score -= 5
        issues.append({"type": "incomplete_difficulty_tiers", "severity": "low"})

    # ── 等级判定 ──────────────────────────────────────────────────────────
    score = max(0, score)  # 确保非负

    if score >= 90:
        grade = "A"
    elif score >= 70:
        grade = "B"
    elif score >= 50:
        grade = "C"
    else:
        grade = "D"

    return grade, score, issues


def _format_quality_feedback(issues: list[dict]) -> str:
    """将质量问题格式化为 LLM 反馈"""
    if not issues:
        return ""

    feedback_lines = ["请改进以下问题："]
    for issue in issues:
        detail = issue.get("detail", "")
        if detail:
            feedback_lines.append(f"- {issue['type']}: {detail}")
        else:
            feedback_lines.append(f"- {issue['type']}")

    return "\n".join(feedback_lines)


def _is_invalid_role(role_name: str) -> bool:
    """检查是否是禁止的通用角色（精确短语匹配）"""
    if not role_name:
        return True

    role_lower = role_name.lower().strip()
    for forbidden in _FORBIDDEN_ROLE_KEYWORDS:
        if role_lower == forbidden:
            return True
        # 也检查是否包含整个禁止短语
        if forbidden in role_lower and role_lower.replace(forbidden, "").strip() == "":
            return True
    return False


def _validate_and_fix_role_name(role_name: str, default: str = "Service Provider") -> str:
    """验证并修正角色名称"""
    if not role_name or _is_invalid_role(role_name):
        return default
    return role_name


async def llm_fill_title_zh_only(openai_client, title_en: str) -> Optional[str]:
    """
    仅根据英文 canonical 标题生成短中文 UI 名（2–12 字典型）。
    供批量补全脚本与主生成流程复用；失败返回 None。
    """
    title_en = (title_en or "").strip()
    if not title_en:
        return None
    try:
        resp = await asyncio.wait_for(
            openai_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {
                        "role": "system",
                        "content": "Output a single JSON object only. Keys: title_zh (string).",
                    },
                    {
                        "role": "user",
                        "content": (
                            f'English practice topic title: "{title_en}"\n'
                            "Reply with JSON only: "
                            '{"title_zh":"<short natural Chinese UI name, 2-12 Chinese characters, no English>"}'
                        ),
                    },
                ],
                max_tokens=120,
                response_format={"type": "json_object"},
            ),
            timeout=_FILL_TITLE_ZH_TIMEOUT,
        )
        raw = resp.choices[0].message.content
        extra = json.loads(raw.strip())
        z = (extra.get("title_zh") or "").strip()
        return z or None
    except Exception as e:
        logger.warning("[TopicGenerator] llm_fill_title_zh_only failed for %r: %s", title_en, e)
        return None


async def _fill_title_zh_if_missing(data: dict, openai_client) -> None:
    """
    主 JSON 里若未给出 title_zh（模型偶发漏字段），补一次极短调用，保证新生成话题可中文展示。
    """
    if (data.get("title_zh") or "").strip():
        return
    title_en = (data.get("title") or "").strip()
    if not title_en:
        return
    z = await llm_fill_title_zh_only(openai_client, title_en)
    if z:
        data["title_zh"] = z
        logger.info("[TopicGenerator] Filled missing title_zh via follow-up LLM call")


async def _call_llm_and_save(
    prompt: str,
    db: Session,
    openai_client,
    *,
    domain: Optional[str] = None,
) -> Topic:
    try:
        resp = await asyncio.wait_for(
            openai_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a language curriculum designer. Output valid JSON only."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=GENERATE_MAX_TOKENS,
                response_format={"type": "json_object"},
            ),
            timeout=GENERATE_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise RuntimeError(f"TopicGenerator LLM timeout (>{GENERATE_TIMEOUT}s)")

    raw = resp.choices[0].message.content
    data = _parse_and_validate(raw)
    await _fill_title_zh_if_missing(data, openai_client)
    return _save_to_db(data, db, domain=domain)


def _parse_and_validate(raw: str) -> dict:
    """Parse + minimal validation of LLM output."""
    try:
        data = json.loads(raw.strip())
    except json.JSONDecodeError:
        # Try to extract JSON from markdown
        match = re.search(r'\{[\s\S]*\}', raw)
        if match:
            data = json.loads(match.group())
        else:
            raise ValueError(f"LLM returned unparseable JSON: {raw[:200]}")

    # Ensure required fields exist with defaults
    data.setdefault("title", "Custom Practice Topic")
    data.setdefault("title_zh", "")
    data.setdefault("category", "General")
    data.setdefault("role_name", "English Coach")
    data.setdefault("learner_level", "Intermediate")
    data.setdefault("scene_prompt", data["title"])
    data.setdefault("vocab_tags", [])
    data.setdefault("sentence_patterns", [])
    data.setdefault("scene_specific_rules", [])
    data.setdefault("difficulty_tiers", {
        "1": {"rules": ["Focus on basic vocabulary and fundamental expressions."]},
        "2": {"rules": ["Introduce more complex phrasing and situational variations."]},
        "3": {"rules": ["Handle advanced situations, edge cases, and nuanced language."]},
    })
    data.setdefault("nodes", [])
    return data


def _save_to_db(
    data: dict,
    db: Session,
    *,
    domain: Optional[str] = None,
    quality_grade: Optional[str] = None,
    quality_score: Optional[int] = None,
    quality_issues: Optional[list] = None,
    generation_attempts: int = 1,
) -> Topic:
    """Persist generated Topic, return detached Topic."""
    tzh = (data.get("title_zh") or "").strip()

    # 验证并修正角色名称
    role_name = _validate_and_fix_role_name(data.get("role_name", ""))

    topic = Topic(
        title=data["title"],
        title_zh=tzh or None,
        category=data["category"],
        role_name=role_name,
        learner_level=data["learner_level"],
        system_prompt=data["scene_prompt"],
        vocab_tags=data["vocab_tags"],
        sentence_patterns=data["sentence_patterns"],
        scene_specific_rules=data["scene_specific_rules"],
        difficulty_tiers=data["difficulty_tiers"],
        voice="Stanley",
        domain=domain,
        # 质量字段
        quality_grade=quality_grade,
        quality_score=quality_score,
        quality_issues=quality_issues,
        generation_attempts=generation_attempts,
        # 未达标话题标记为未发布
        is_published=(quality_grade == "A") if quality_grade else True,
    )
    db.add(topic)
    db.commit()
    db.refresh(topic)

    # Save values before expunge (accessing attributes on detached instance raises DetachedInstanceError)
    saved_title = topic.title
    saved_id = topic.id
    db.expunge(topic)
    logger.info(f"[TopicGenerator] Saved new topic: '{saved_title}' "
                f"(id={saved_id}, domain={domain}, grade={quality_grade})")
    return topic


def _get_fallback_topic(db: Session) -> Topic:
    """Last resort: return any existing topic."""
    topic = db.query(Topic).first()
    if topic:
        db.expunge(topic)
        return topic
    # If DB is completely empty, create a bare-bones topic
    topic = Topic(
        title="General English Conversation",
        title_zh="通用英语对话",
        category="Daily Life",
        role_name="English Coach",
        learner_level="Intermediate",
        system_prompt="Have a natural English conversation.",
    )
    db.add(topic)
    db.commit()
    db.refresh(topic)
    db.expunge(topic)
    return topic
