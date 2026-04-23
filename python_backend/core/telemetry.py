import time
import uuid
import logging

logger = logging.getLogger("EnglishCoach")

def _latency_log(lat: dict, stage: str, **kwargs) -> None:
    t0 = lat.get("t0")
    if not isinstance(t0, (int, float)):
        return
    tid = lat.get("turn_id", "?")
    ms = (time.perf_counter() - float(t0)) * 1000.0
    extra = (" " + " ".join(f"{k}={v!r}" for k, v in kwargs.items())) if kwargs else ""
    logger.info("[LATENCY] turn=%s stage=%-28s cum=%8.1fms%s", tid, stage, ms, extra)

def _coerce_turn_trace_id(raw) -> str:
    if not isinstance(raw, str):
        return uuid.uuid4().hex[:8]
    s = raw.strip().replace("-", "")
    if 4 <= len(s) <= 32 and s.isalnum():
        return s.lower()
    return uuid.uuid4().hex[:8]

def _e2e_log_client_report(data: dict) -> None:
    tid_raw = data.get("trace_id")
    tid = tid_raw.strip()[:32].lower() if isinstance(tid_raw, str) and tid_raw.strip() else "?"
    logger.info(
        "[E2E] turn=%s client_submit_to_first_pcm_ms=%r client_submit_to_tts_finished_ms=%r "
        "had_first_pcm=%r client_report_wall_ms=%r",
        tid,
        data.get("submit_to_first_pcm_ms"),
        data.get("submit_to_tts_finished_ms"),
        data.get("had_first_pcm"),
        data.get("client_report_wall_ms"),
    )