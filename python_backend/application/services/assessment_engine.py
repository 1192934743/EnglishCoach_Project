"""
AssessmentEngine — L1/L2 混合降级评估架构

分层说明（来自架构设计）：
  L1（本地，同步，每轮触发）：
    - depth_level 1~2 的生存级词汇/短语
    - 使用 mastery_scorer 的规范化 + 词干提取做命中判断
    - 命中后立即更新 UserProgress（SM-2 公式）
    - 在 dialogue_engine.evaluate_and_check_progress 中调用

  L2（LLM，异步，WRAP_UP 后触发）：
    - 全部 target/review 节点（不限 depth_level）
    - 使用完整对话记录做上下文感知评估
    - 能识别：缩写等价（"I'd like" = "I would like"）、自然度、语法质量
    - 校正 L1 的假阳性，并为 depth_level 3+ 给出首次评分
    - 结果写回 UserProgress 并更新 LearningSession.session_summary

触发条件（cost 控制，来自 Gemini 建议）：
  - depth_tier >= 2 时才触发 L2（tier=1 的基础词汇 L1 足够）
  - 对话轮次 < 3 时跳过（数据量太少，评估无意义）
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from database import SessionLocal, UserProgress, LearningSession
from domain.entities.task_packet import TaskPacket
from application.services.mastery_scorer import update_mastery, L1_EXACT_QUALITY
from application.services import report_builder

logger = logging.getLogger("EnglishCoach")

# L2 每次调用的 token 上限（节省成本：评估不需要长篇大论）
L2_MAX_TOKENS = 400
# L2 请求超时（秒）
L2_TIMEOUT = 20.0
# 触发 L2 的最低对话轮次（少于此轮次跳过，数据不足）
L2_MIN_TURNS = 3
# 触发 L2 的最低 depth_tier
L2_MIN_DEPTH_TIER = 2


# ═══════════════════════════════════════════════════════════════════════════
# L2 评估入口
# ═══════════════════════════════════════════════════════════════════════════

async def run_l2_assessment(
    transcript: list[dict],
    task_packet: TaskPacket,
    user_id: str,
    session_id: str,
    openai_client,
    websocket=None,
    ws_lock=None,
    db: Optional[Session] = None,
) -> None:
    """
    异步 L2 评估主入口，在 WRAP_UP 后由 server.py 以 create_task 触发。

    transcript:    [{"role": "user"/"assistant", "text": "..."}]
    session_id:    会话唯一标识，用于前端匹配 preliminary/final 报告
    openai_client: 来自 server.py 的 AsyncOpenAI 实例（不重新创建，节省连接）
    websocket:     可选；若提供，L2 完成后推送 final 报告
    ws_lock:       配套的 asyncio.Lock，保证 WebSocket 线程安全

    此函数绝不向调用方抛出异常，所有错误内部吞掉并记录日志。
    """
    # ── 触发条件检查 ────────────────────────────────────────────────────────
    if not task_packet or not task_packet.has_nodes():
        logger.debug("L2 skip: no nodes in task_packet")
        return

    if task_packet.depth_tier < L2_MIN_DEPTH_TIER:
        logger.debug(f"L2 skip: depth_tier={task_packet.depth_tier} < {L2_MIN_DEPTH_TIER}")
        return

    user_turns = [t for t in transcript if t.get("role") == "user"]
    if len(user_turns) < L2_MIN_TURNS:
        logger.debug(f"L2 skip: only {len(user_turns)} user turns (< {L2_MIN_TURNS})")
        return

    should_close = db is None
    db = db or SessionLocal()
    try:
        l2_results = await _run_l2_core(
            transcript, task_packet, user_id, openai_client, db
        )
        # 如果有 WebSocket，发送 final 报告
        if websocket and ws_lock and l2_results:
            final_report = report_builder.build_final_report(
                task_packet, session_id, l2_results, db, user_id
            )
            if final_report:
                try:
                    import json
                    async with ws_lock:
                        await websocket.send_text(json.dumps(final_report))
                    logger.info(f"[L2] Final report sent: avg_quality={final_report.get('avg_quality', 0):.2f}")
                except Exception as ws_err:
                    logger.warning(f"[L2] Final report WebSocket send failed: {ws_err}")
    except Exception as e:
        logger.error(f"L2 assessment unexpected error: {e}", exc_info=True)
    finally:
        if should_close:
            db.close()


# ═══════════════════════════════════════════════════════════════════════════
# L2 核心逻辑
# ═══════════════════════════════════════════════════════════════════════════

async def _run_l2_core(
    transcript: list[dict],
    task_packet: TaskPacket,
    user_id: str,
    openai_client,
    db: Session,
) -> list[dict]:
    """Returns the parsed L2 results list (empty list on failure/skip)."""
    all_nodes = task_packet.target_nodes + task_packet.review_nodes
    if not all_nodes:
        return []

    transcript_text = _format_transcript(transcript)
    nodes_json = json.dumps(
        [{"node_id": n["id"], "expression": n["node_text"]} for n in all_nodes],
        ensure_ascii=False
    )

    prompt = _build_l2_prompt(transcript_text, nodes_json)

    try:
        resp = await asyncio.wait_for(
            openai_client.chat.completions.create(
                model="deepseek-chat",
                messages=[
                    {"role": "system", "content": "You are a precise English language assessment AI. Output JSON only."},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=L2_MAX_TOKENS,
                response_format={"type": "json_object"},
            ),
            timeout=L2_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning(f"L2 assessment timeout (>{L2_TIMEOUT}s), skipping this session.")
        return []
    except Exception as e:
        logger.error(f"L2 LLM call failed: {e}")
        return []

    raw = resp.choices[0].message.content
    results = _parse_l2_response(raw)
    if not results:
        logger.warning("L2: could not parse LLM response, skipping mastery update.")
        return []

    # ── 将 L2 结果写入 UserProgress ───────────────────────────────────────
    node_id_to_result = {r["node_id"]: r for r in results}
    updated_count = 0

    for node in all_nodes:
        nid = node.get("id")
        if nid is None:
            continue
        result = node_id_to_result.get(nid)
        if result is None:
            continue

        attempted: bool = result.get("attempted", False)
        correct: bool = result.get("correct", False)
        quality: float = float(result.get("quality", 0.0))

        if not attempted:
            # 节点未被尝试：不更新 mastery（保留 L1 的评估结果）
            continue

        progress = db.query(UserProgress).filter(
            UserProgress.user_id == user_id,
            UserProgress.node_id == nid,
        ).first()

        if progress is None:
            progress = UserProgress(
                user_id=user_id,
                node_id=nid,
                mastery_score=0.0,
                practice_count=0,
            )
            db.add(progress)

        old_mastery = progress.mastery_score
        new_mastery = update_mastery(old_mastery, was_correct=correct, quality=quality)

        # L2 是权威评估：直接覆盖 mastery，而不是再叠加
        # 为防止 L2 幻觉导致 mastery 大幅下降，限制单次最大降幅为 20 分
        if new_mastery < old_mastery:
            new_mastery = max(new_mastery, old_mastery - 20.0)

        progress.mastery_score = new_mastery
        progress.last_practiced_at = datetime.datetime.utcnow()
        updated_count += 1

        logger.info(
            f"[L2] node='{node['node_text']}' | "
            f"correct={correct} quality={quality:.2f} | "
            f"mastery: {old_mastery:.1f} -> {new_mastery:.1f}"
        )

    db.commit()

    # ── 更新 LearningSession.session_summary ──────────────────────────────
    _write_session_summary(user_id, task_packet, results, db)

    logger.info(f"[L2] Assessment complete: {updated_count}/{len(all_nodes)} nodes updated.")
    return results


# ═══════════════════════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════════════════════

def _format_transcript(transcript: list[dict]) -> str:
    """
    将对话记录格式化为 LLM 可读的文本。
    只保留最近 20 轮，防止 prompt 过长。
    """
    recent = transcript[-40:]  # 每轮2条(user+assistant)，最多20轮对话
    lines = []
    for turn in recent:
        role = turn.get("role", "unknown").capitalize()
        text = turn.get("text", "").strip()
        if text:
            lines.append(f"{role}: {text}")
    return "\n".join(lines)


def _build_l2_prompt(transcript_text: str, nodes_json: str) -> str:
    return f"""Analyze this English practice conversation and evaluate the learner's use of target expressions.

CONVERSATION:
{transcript_text}

TARGET EXPRESSIONS TO EVALUATE:
{nodes_json}

For each expression, determine:
- attempted: did the LEARNER (not AI) try to use this expression or an equivalent?
- correct: was it used correctly in context? (Accept minor grammar errors if meaning is clear. Accept contractions: "I'd like" = "I would like")
- quality: 0.0 = not used, 0.3 = attempted but wrong, 0.6 = correct but unnatural, 1.0 = correct and natural

Output ONLY a JSON object with key "results" containing an array:
{{"results": [{{"node_id": <int>, "attempted": <bool>, "correct": <bool>, "quality": <float>}}]}}"""


def _parse_l2_response(raw: str) -> list[dict]:
    """Parse L2 LLM response, robust to common JSON issues."""
    raw = raw.strip()
    try:
        data = json.loads(raw)
        # Handle both {"results": [...]} and direct array [...]
        if isinstance(data, dict):
            results = data.get("results", [])
        elif isinstance(data, list):
            results = data
        else:
            return []

        # Validate each entry has required fields
        validated = []
        for r in results:
            if isinstance(r, dict) and "node_id" in r:
                validated.append({
                    "node_id": int(r["node_id"]),
                    "attempted": bool(r.get("attempted", False)),
                    "correct": bool(r.get("correct", False)),
                    "quality": max(0.0, min(1.0, float(r.get("quality", 0.0)))),
                })
        return validated

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        logger.warning(f"L2 response parse error: {e} | raw={raw[:200]}")
        # Fallback: try to extract JSON array from text
        match = re.search(r'\[[\s\S]*\]', raw)
        if match:
            try:
                items = json.loads(match.group())
                return _parse_l2_response(json.dumps({"results": items}))
            except Exception:
                pass
        return []


def _write_session_summary(
    user_id: str,
    task_packet: TaskPacket,
    results: list[dict],
    db: Session,
) -> None:
    """Find the most recent LearningSession for this user/topic and write L2 summary."""
    try:
        session = (
            db.query(LearningSession)
            .filter(
                LearningSession.user_id == user_id,
                LearningSession.topic_id == task_packet.topic_id,
            )
            .order_by(LearningSession.start_time.desc())
            .first()
        )
        if session is None:
            return

        attempted = [r for r in results if r.get("attempted")]
        correct = [r for r in attempted if r.get("correct")]
        avg_quality = (
            sum(r["quality"] for r in attempted) / len(attempted)
            if attempted else 0.0
        )

        session.session_summary = {
            "assessed_by": "L2_LLM",
            "assessed_at": datetime.datetime.utcnow().isoformat(),
            "nodes_attempted": len(attempted),
            "nodes_correct": len(correct),
            "avg_quality": round(avg_quality, 3),
            "depth_tier": task_packet.depth_tier,
        }
        db.commit()
    except Exception as e:
        logger.warning(f"L2 session_summary write failed (non-critical): {e}")
