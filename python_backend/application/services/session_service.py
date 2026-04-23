import logging
from fastapi.concurrency import run_in_threadpool
import application.services.session_planner as session_planner
import application.services.assessment_engine as assessment_engine
from core.config import MAX_CONTEXT_TOKENS

logger = logging.getLogger("EnglishCoach")

# 主 LLM 将不再挂载任何 Tools，全力保障 TTFB。以下工具专供后台副 LLM 使用。
EVALUATOR_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "submit_analysis_and_feedback",
            "description": "Submit analysis of the user's latest speech and the AI coach's response.",
            "parameters": {
                "type": "object",
                "properties": {
                    "ai_translation_cn": {
                        "type": "string",
                        "description": "A natural Chinese translation of the AI Coach's English reply."
                    },
                    "suggested_hints_en": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "2 or 3 short English responses the User could say next."
                    },
                    "coach_correction_cn": {
                        "type": "string",
                        "description": "If the user made a grammar/vocabulary mistake, briefly correct it in Chinese. Otherwise leave empty."
                    },
                    "should_advance_phase": {
                        "type": "boolean",
                        "description": "True ONLY if the current phase goal is fulfilled and the coach is moving to the next phase."
                    }
                },
                "required": ["ai_translation_cn", "suggested_hints_en", "coach_correction_cn", "should_advance_phase"]
            }
        }
    }
]

async def _renew_task_packet(
        user_id: str,
        old_packet,
        nodes_hit: set,
        transcript: list,
        openai_client,
        session_id: str = "",
        websocket=None,
        ws_lock=None,
):
    if not old_packet or not user_id:
        return
    try:
        await run_in_threadpool(
            session_planner.save_learning_session, user_id, old_packet, nodes_hit
        )
    except Exception as e:
        logger.error(f"[WRAP_UP] save_learning_session error: {e}")

    try:
        await assessment_engine.run_l2_assessment(
            transcript, old_packet, user_id, session_id,
            openai_client, websocket, ws_lock,
        )
    except Exception as e:
        logger.error(f"[WRAP_UP] L2 assessment error (non-blocking): {e}")

def trim_chat_history(history: list[dict], max_tokens: int = MAX_CONTEXT_TOKENS) -> list[dict]:
    if len(history) <= 1:
        return history
    system = history[0]
    turns = history[1:]
    while len(turns) > 2:
        total_chars = sum(len(m.get("content", "")) for m in [system] + turns)
        if total_chars // 4 <= max_tokens:
            break
        turns = turns[2:]
    return [system] + turns