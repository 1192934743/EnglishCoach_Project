"""
AssessmentEngine — L2 异步评估架构

基于 LearningSession.nodes_mastered 追踪掌握度。
"""

from __future__ import annotations

import asyncio
import datetime
import json
import logging
import re
from typing import Optional

from sqlalchemy.orm import Session

from database import SessionLocal, LearningSession
from domain.entities.task_packet import TaskPacket
from application.services import report_builder

logger = logging.getLogger("EnglishCoach")

L2_MAX_TOKENS = 400
L2_TIMEOUT = 20.0
L2_MIN_TURNS = 3
L2_MIN_DEPTH_TIER = 2


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
    """异步 L2 评估主入口"""
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
        if websocket and ws_lock and l2_results:
            final_report = report_builder.build_final_report(
                task_packet, session_id, l2_results, db, user_id
            )
            if final_report:
                try:
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


async def _run_l2_core(
    transcript: list[dict],
    task_packet: TaskPacket,
    user_id: str,
    openai_client,
    db: Session,
) -> list[dict]:
    """L2 核心逻辑"""
    all_constraints = task_packet.constraints + task_packet.review_constraints
    if not all_constraints:
        return []

    transcript_text = _format_transcript(transcript)
    nodes_json = json.dumps(
        [{"node_id": c.constraint_id, "expression": c.constraint_text} for c in all_constraints if c.constraint_id],
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
        logger.warning("L2: could not parse LLM response, skipping.")
        return []

    _write_session_summary(user_id, task_packet, results, db)
    logger.info(f"[L2] Assessment complete: {len(results)} expressions evaluated.")
    return results


def _format_transcript(transcript: list[dict]) -> str:
    """将对话记录格式化为 LLM 可读的文本"""
    recent = transcript[-40:]
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
    """Parse L2 LLM response"""
    raw = raw.strip()
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            results = data.get("results", [])
        elif isinstance(data, list):
            results = data
        else:
            return []

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
        logger.warning(f"L2 response parse error: {e}")
        return []


def _write_session_summary(
    user_id: str,
    task_packet: TaskPacket,
    results: list[dict],
    db: Session,
) -> None:
    """更新 LearningSession.session_summary"""
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
        avg_quality = sum(r["quality"] for r in attempted) / len(attempted) if attempted else 0.0

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
        logger.warning(f"L2 session_summary write failed: {e}")
