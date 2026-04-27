#!/usr/bin/env python3
"""
Mock WebSocket Client — English Coach Interactive Tester
=====================================================
极简 WebSocket 纯文本客户端，用于调试微场景流转和诱导逻辑。

功能：
  - 连接到 FastAPI WebSocket 端点（/ws/coach）
  - 支持 test_text_input（文本对话）和 switch_topic（切换话题）action
  - 监听并美化打印所有服务器返回事件
  - 自动检测 scenario_transition / phase 流转

前置依赖：
    pip install websockets

启动方式：
    # 1. 先确保后端服务运行在 127.0.0.1:8000
    python server.py

    # 2. 另开终端，运行本客户端
    python -m scripts.mock_ws_client [--host HOST] [--port PORT] [--user-id USER_ID] [--topic-id TOPIC_ID]

    # 示例：连接星巴克话题（topic_id 需要从 DB 查询）
    python -m scripts.mock_ws_client --topic-id 4

    # 示例：连接本地默认话题
    python -m scripts.mock_ws_client

    # 退出：直接按 Ctrl+C 或输入 /quit

WebSocket URL 格式：
    ws://{host}:{port}/ws/coach?user_id={user_id}

服务器支持的事件契约（服务端 → 客户端）：
    test_ai_reply        : AI 完整回复文本
    ai_text_stream       : AI 流式输出片段（打字机效果）
    scenario_transition  : 微场景流转信号（Phase 3）
    topic_changed        : 话题切换完成
    topic_mastery_reached: 进度更新
    teaching_data        : Director 副 LLM 辅导数据（翻译/提示/纠错）
    session_report       : 学习报告（preliminary / final）
    tts_finished         : TTS 播放完成
    asr_partial         : ASR 中间结果（仅供参考）
    role_swapped         : 角色翻转完成
    warmup_success       : ping 响应

客户端发送的 action 契约（客户端 → 服务端）：
    test_text_input : 发送文本给教练（模拟用户语音输入）
    switch_topic    : 切换到指定话题（中途切换）
    ping            : 心跳/初始化连接
    /quit           : 退出客户端
    /help           : 打印帮助
    /topic <id>    : 切换话题（快捷命令）
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import os
import socket
import websockets

# 添加 python_backend 到 path（支持 python -m scripts.mock_ws_client 直接运行）
_backend_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _backend_dir not in sys.path:
    sys.path.insert(0, _backend_dir)


# ── 颜色美化 ────────────────────────────────────────────────────────────────

class _Colors:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"

    # 事件类型颜色
    COURIER_AI = "\033[96m"    # 青色 — AI 打字机流
    COURIER_AI_FINAL = "\033[92m"  # 绿色 — AI 完整回复
    TEACHING = "\033[94m"       # 蓝色 — 教学辅导数据
    TRANSITION = "\033[93m"      # 黄色 — 场景流转
    PHASE = "\033[95m"           # 紫色 — 阶段变化
    PROGRESS = "\033[92m"        # 绿色 — 进度条
    TOPIC = "\033[93m"           # 黄色 — 话题切换
    ERROR = "\033[91m"           # 红色 — 错误
    TTS = "\033[90m"             # 灰色 — TTS 事件
    SYSTEM = "\033[90m"          # 灰色 — 系统消息
    USER_INPUT = "\033[96m"     # 青色 — 用户输入

    # 进度条
    BAR_FULL = "█"
    BAR_EMPTY = "░"


def _c(text: str, color: str) -> str:
    """为文本加上 ANSI 颜色前缀和 RESET 后缀"""
    return f"{color}{text}{_Colors.RESET}"


def _print_progress_bar(progress: float, width: int = 30) -> str:
    """打印一个进度条字符串"""
    filled = int(width * max(0.0, min(100.0, progress)) / 100.0)
    bar = _Colors.BAR_FULL * filled + _Colors.BAR_EMPTY * (width - filled)
    return f"[{bar}] {progress:.1f}%"


# ── 事件美化打印 ────────────────────────────────────────────────────────────

def _print_ai_stream(text: str) -> None:
    """打印 AI 流式打字机输出（青色）"""
    print(_c(f"  🤖 [AI] {text}", _Colors.COURIER_AI), end="", flush=True)


def _print_ai_reply(text: str, phase: str = "") -> None:
    """打印 AI 完整回复（绿色）"""
    phase_tag = _c(f"[{phase}]", _Colors.PHASE) if phase else ""
    print()
    print(_c(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", _Colors.COURIER_AI_FINAL))
    print(_c(f"  🤖 教练 AI {phase_tag}:", _Colors.COURIER_AI_FINAL))
    for line in text.strip().split("\n"):
        print(_c(f"     {line}", _Colors.COURIER_AI_FINAL))
    print(_c(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", _Colors.COURIER_AI_FINAL))


def _print_scenario_transition(data: dict) -> None:
    """打印微场景流转信号（黄色大字）"""
    prev = data.get("previous_scenario_code", "?")
    new_code = data.get("new_scenario_code", "?")
    new_name = data.get("new_scenario_name", "?")
    new_intent = data.get("new_intent", "")
    delta = data.get("progress_delta", 0)
    print()
    print(_c("╔══════════════════════════════════════════════════════╗", _Colors.TRANSITION))
    print(_c("║           🎯 微场景流转！                                ║", _Colors.TRANSITION))
    print(_c("╠══════════════════════════════════════════════════════╣", _Colors.TRANSITION))
    print(_c(f"║   {prev:54s}║", _Colors.TRANSITION))
    print(_c(f"║   → {new_code:52s}║", _Colors.TRANSITION))
    print(_c(f"║   场景名: {new_name:45s}║", _Colors.TRANSITION))
    if new_intent:
        intent_short = new_intent[:46] + "..." if len(new_intent) > 46 else new_intent
        print(_c(f"║   意图: {intent_short:47s}║", _Colors.TRANSITION))
    if delta:
        print(_c(f"║   奖励分: +{delta:.0f}                                     ║", _Colors.TRANSITION))
    print(_c("╚══════════════════════════════════════════════════════╝", _Colors.TRANSITION))
    print()


def _print_topic_changed(data: dict) -> None:
    """打印话题切换"""
    title = data.get("topic_title", "?")
    title_zh = data.get("topic_title_zh", "")
    role = data.get("role_name", "?")
    tier = data.get("depth_tier", "?")
    print()
    print(_c(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", _Colors.TOPIC))
    print(_c(f"  📋 话题切换完成", _Colors.TOPIC))
    print(_c(f"     标题: {title} {('(' + title_zh + ')') if title_zh else ''}", _Colors.TOPIC))
    print(_c(f"     角色: {role}", _Colors.TOPIC))
    print(_c(f"     Depth Tier: {tier}", _Colors.TOPIC))
    print(_c(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", _Colors.TOPIC))
    print()


def _print_progress(data: dict) -> None:
    """打印进度条"""
    progress = data.get("progress", 0.0)
    level = data.get("level", 1)
    bar = _print_progress_bar(progress)
    turn = data.get("turn_in_scenario", 0)
    print()
    print(_c(f"  📊 进度: {bar}  Lv.{level}" + (f"  场景轮数: {turn}" if turn else ""), _Colors.PROGRESS))


def _print_teaching_data(data: dict) -> None:
    """打印 Director 副 LLM 的辅导数据"""
    # coaching_data 可能在 data 子对象里（coach_ws 发送格式），也可能直接在根级别
    inner = data.get("data", data)

    translation = inner.get("ai_translation_cn", "") or data.get("ai_translation_cn", "")
    hints = inner.get("suggested_hints_en", []) or data.get("suggested_hints_en", [])
    correction = inner.get("coach_correction_cn", "") or data.get("coach_correction_cn", "")

    # Phase 3 双轨信号
    intent_achieved = inner.get("intent_achieved") or data.get("intent_achieved")
    constraints_hit = inner.get("constraints_hit") or data.get("constraints_hit")
    scenario_completed = inner.get("scenario_completed") or data.get("scenario_completed")

    # 检查是否有任何有意义的内容
    has_content = bool(
        translation or hints or correction
        or intent_achieved is not None
        or constraints_hit is not None
        or scenario_completed is not None
    )

    print()
    print(_c("  ── 教练辅导数据 ──────────────────────────", _Colors.TEACHING))
    if translation:
        print(_c(f"  🌐 翻译: {translation}", _Colors.TEACHING))
    if hints:
        hint_str = " | ".join(hints)
        print(_c(f"  💡 提示: {hint_str}", _Colors.TEACHING))
    if correction:
        print(_c(f"  ✏️  纠错: {correction}", _Colors.TEACHING))

    # 双轨信号（阶段二/三）始终显示
    ia = "✅" if intent_achieved else ("❌" if intent_achieved is False else "⏳")
    ch = "✅" if constraints_hit else ("❌" if constraints_hit is False else "⏳")
    sc = "🎯" if scenario_completed else ""
    if intent_achieved is not None or constraints_hit is not None or scenario_completed is not None:
        print(_c(f"  双轨 [意图 {ia} | 约束 {ch}] {sc}", _Colors.TEACHING))

    if not has_content:
        print(_c("  (无辅导数据，等待下一轮...)", _Colors.DIM))

    print(_c("  ─────────────────────────────────────────", _Colors.TEACHING))


def _print_session_report(data: dict) -> None:
    """打印学习报告"""
    stage = data.get("stage", "?")
    topic = data.get("topic_title", "?")
    print()
    print(_c(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", _Colors.TOPIC))
    print(_c(f"  📝 学习报告 [{stage.upper()}] — {topic}", _Colors.TOPIC))

    mastery = data.get("mastery_score", 0.0)
    nodes_hit = data.get("nodes_hit", 0)
    nodes_total = data.get("nodes_total", 0)
    bar = _print_progress_bar(mastery)
    print(_c(f"     掌握度: {bar}", _Colors.TOPIC))
    print(_c(f"     命中: {nodes_hit}/{nodes_total} 个词汇", _Colors.TOPIC))
    print(_c(f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", _Colors.TOPIC))
    print()


def _print_error(data: dict) -> None:
    """打印错误"""
    code = data.get("code", "?")
    msg = data.get("message", str(data))
    print()
    print(_c(f"  ❌ 错误 [{code}]: {msg}", _Colors.ERROR))


def _print_raw(data: dict) -> None:
    """兜底：打印未识别的事件（灰色）"""
    event = data.get("event", "?")
    # 过滤掉大字段
    display = {k: v for k, v in data.items() if k not in ("ai_text", "transcript") and not isinstance(v, (list, dict) and len(str(v)) > 200)}
    print(_c(f"  [未识别事件: {event}] {display}", _Colors.SYSTEM))


# ── 主客户端 ────────────────────────────────────────────────────────────────

async def _event_loop(ws: websockets.WebSocketClientProtocol, user_id: str) -> None:
    """
    事件监听协程：持续监听服务器消息并美化打印。
    在独立协程中运行，不阻塞主线程的输入循环。
    """
    try:
        async for raw in ws:
            try:
                data = json.loads(raw)
            except json.JSONDecodeError:
                print(_c(f"  [原始] {raw[:120]}", _Colors.SYSTEM))
                continue

            event = data.get("event", "")

            if event == "ai_text_stream":
                _print_ai_stream(data.get("text", ""))
            elif event == "test_ai_reply":
                _print_ai_reply(data.get("text", ""), data.get("phase", ""))
            elif event == "scenario_transition":
                _print_scenario_transition(data)
            elif event == "topic_changed":
                _print_topic_changed(data)
            elif event == "topic_mastery_reached":
                _print_progress(data)
            elif event == "teaching_data":
                _print_teaching_data(data)
            elif event == "session_report":
                _print_session_report(data)
            elif event == "error":
                _print_error(data)
            elif event in ("warmup_success", "role_swapped", "tts_finished", "asr_partial"):
                # 不打印，只静默跳过
                pass
            else:
                _print_raw(data)

    except websockets.ConnectionClosed:
        print(_c("\n[连接已断开]", _Colors.ERROR))
    except Exception as e:
        print(_c(f"\n[监听异常] {e}", _Colors.ERROR))


async def _input_loop(ws: websockets.WebSocketClientProtocol, user_id: str, default_topic_id: int | None) -> None:
    """
    文本输入协程：读取终端输入，构造 action 并发送。
    支持快捷命令：
      /quit      — 退出
      /help      — 帮助
      /topic <id> — 切换话题
    """
    print()
    print(_c("  ╔══════════════════════════════════════════════════════╗", _Colors.TRANSITION))
    print(_c("  ║  English Coach — Mock WebSocket Client                   ║", _Colors.TRANSITION))
    print(_c("  ╠══════════════════════════════════════════════════════╣", _Colors.TRANSITION))
    print(_c("  ║  输入你的英文句子，按回车发送                            ║", _Colors.TRANSITION))
    print(_c("  ║  /topic <id> — 切换话题                                   ║", _Colors.TRANSITION))
    print(_c("  ║  /quit       — 退出                                        ║", _Colors.TRANSITION))
    print(_c("  ╚══════════════════════════════════════════════════════╝", _Colors.TRANSITION))
    print()

    try:
        while True:
            try:
                raw = await asyncio.get_event_loop().run_in_executor(
                    None, lambda: input(_c("\nYou: ", _Colors.USER_INPUT))
                )
            except (EOFError, OSError):
                break

            text = (raw or "").strip()
            if not text:
                continue

            # ── 快捷命令处理 ──────────────────────────────────────────────
            if text.lower() == "/quit":
                print(_c("\n正在断开连接...", _Colors.SYSTEM))
                await ws.close()
                break

            if text.lower() == "/help":
                print()
                print(_c("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", _Colors.TOPIC))
                print(_c("  快捷命令：", _Colors.TOPIC))
                print(_c("    /topic <id>   — 切换到指定话题 ID", _Colors.TOPIC))
                print(_c("    /quit          — 退出客户端", _Colors.TOPIC))
                print(_c("━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━", _Colors.TOPIC))
                continue

            if text.lower().startswith("/topic "):
                parts = text.split()
                if len(parts) >= 2:
                    try:
                        tid = int(parts[1])
                        payload = {
                            "action": "switch_topic",
                            "topic_id": tid,
                            "user_id": user_id,
                        }
                        await ws.send(json.dumps(payload))
                        print(_c(f"  → 正在切换到 topic_id={tid} ...", _Colors.TOPIC))
                    except ValueError:
                        print(_c(f"  → 无效的 topic ID: {parts[1]}", _Colors.ERROR))
                else:
                    print(_c("  → 用法: /topic <id>", _Colors.ERROR))
                continue

            # ── 正常对话 ─────────────────────────────────────────────────
            payload = {
                "action": "test_text_input",
                "text": text,
                "user_id": user_id,
            }
            try:
                await ws.send(json.dumps(payload))
            except websockets.ConnectionClosed:
                print(_c("\n[连接已断开，无法发送]", _Colors.ERROR))
                break

    except EOFError:
        pass


async def run_client(
    host: str,
    port: int,
    user_id: str,
    topic_id: int | None,
) -> None:
    """建立 WebSocket 连接，启动输入和监听两个协程"""

    uri = f"ws://{host}:{port}/ws/coach?user_id={user_id}"
    print(_c(f"\n正在连接到 {uri} ...", _Colors.SYSTEM))

    try:
        async with websockets.connect(uri, ping_interval=None) as ws:
            print(_c(f"✅ 连接成功！\n", _Colors.PROGRESS))

            # 发送 ping 初始化
            await ws.send(json.dumps({"action": "ping", "user_id": user_id}))

            # 若指定了 topic_id，切换话题
            if topic_id is not None:
                print(_c(f"→ 正在切换到 topic_id={topic_id} ...", _Colors.TOPIC))
                await ws.send(json.dumps({
                    "action": "switch_topic",
                    "topic_id": topic_id,
                    "user_id": user_id,
                }))

            # 并发运行：事件监听 + 文本输入
            await asyncio.gather(
                _event_loop(ws, user_id),
                _input_loop(ws, user_id, topic_id),
            )

    except websockets.InvalidURI:
        print(_c(f"[错误] 无效的 WebSocket URI: {uri}", _Colors.ERROR))
    except websockets.exceptions.ConnectionClosed as e:
        print(_c(f"\n[连接已断开: {e}]", _Colors.ERROR))
    except OSError as e:
        print(_c(f"\n[网络错误: {e}]", _Colors.ERROR))
    except Exception as e:
        print(_c(f"\n[错误] {type(e).__name__}: {e}", _Colors.ERROR))


def _get_default_host() -> str:
    """尝试自动获取本机 IP（局域网内其他设备可访问）"""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return "127.0.0.1"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="English Coach — Mock WebSocket Terminal Client",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  python -m scripts.mock_ws_client                              # 连接本地（默认话题）
  python -m scripts.mock_ws_client --host 127.0.0.1 --port 8000  # 指定地址
  python -m scripts.mock_ws_client --topic-id 4                  # 自动切换到星巴克话题

提示: topic_id 需要从 DB 查询。可运行:
  python -c "from database import SessionLocal, Topic; \\
    db = SessionLocal(); \\
    [print(f'  id={t.id}: {t.title}') for t in db.query(Topic).all()]; \\
    db.close()"
        """,
    )
    parser.add_argument(
        "--host",
        default=_get_default_host(),
        help=f"后端服务地址（默认: 自动检测本机 IP）",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="后端服务端口（默认: 8000）",
    )
    parser.add_argument(
        "--user-id",
        default="mock_client_user",
        help="用户 ID（默认: mock_client_user）",
    )
    parser.add_argument(
        "--topic-id",
        type=int,
        default=None,
        help="连接后立即切换到指定话题 ID",
    )

    args = parser.parse_args()
    print(f"\n🚀 English Coach Mock Client")
    print(f"   Host:   {args.host}:{args.port}")
    print(f"   User:   {args.user_id}")
    print(f"   Topic:  {args.topic_id if args.topic_id else '(默认话题)'}")
    print()

    try:
        asyncio.run(run_client(
            host=args.host,
            port=args.port,
            user_id=args.user_id,
            topic_id=args.topic_id,
        ))
    except KeyboardInterrupt:
        print(_c("\n\n客户端已退出。", _Colors.SYSTEM))


if __name__ == "__main__":
    main()
