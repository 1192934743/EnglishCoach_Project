import os
import re
import time
import asyncio
import subprocess
import sys
import logging
import socket
import argparse
import json
from dotenv import load_dotenv

# 必须在设置完环境变量后，再导入 google.generativeai
load_dotenv("config.env")

# 强制注入网络代理，防止国内直连 Google API 导致无尽卡死
proxy_url = os.getenv("HTTP_PROXY")
if not proxy_url:
    raise RuntimeError("HTTP_PROXY environment variable is not set. Please configure it in config.env.")
os.environ['http_proxy'] = proxy_url
os.environ['https_proxy'] = proxy_url
os.environ['HTTP_PROXY'] = proxy_url
os.environ['HTTPS_PROXY'] = proxy_url

import google.generativeai as genai

# ── 场景级优化：数据库操作 ──────────────────────────────────────────────────
from database import SessionLocal, Topic

def get_topic_rules(topic_id: int) -> list:
    """从 Topic DB 获取指定话题的 scene_specific_rules"""
    db = SessionLocal()
    try:
        topic = db.query(Topic).filter(Topic.id == topic_id).first()
        if not topic:
            logger.error(f"❌ 话题 ID {topic_id} 不存在于数据库")
            return []
        return topic.scene_specific_rules or []
    finally:
        db.close()

def set_topic_rules(topic_id: int, rules: list) -> bool:
    """将 scene_specific_rules 写回 Topic DB"""
    db = SessionLocal()
    try:
        topic = db.query(Topic).filter(Topic.id == topic_id).first()
        if not topic:
            logger.error(f"❌ 话题 ID {topic_id} 不存在于数据库")
            return False
        topic.scene_specific_rules = rules
        db.commit()
        return True
    finally:
        db.close()

def list_all_topics() -> list:
    """列出 Topic DB 中所有话题的 (id, title)"""
    db = SessionLocal()
    try:
        topics = db.query(Topic.id, Topic.title, Topic.title_zh).all()
        return [{"id": t.id, "title": t.title, "title_zh": t.title_zh} for t in topics]
    finally:
        db.close()

# ==========================================
# 📝 日志配置 (同时输出到文件和控制台)
# ==========================================
LOG_FILE = "evolution_engine.log"
SERVER_LOG_FILE = "server_output.log"

for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)
logger.info(f"🌐 系统网络代理已自动配置为: {proxy_url}")

# ==========================================
# ⚙️ 洗护器配置 (Agent Config)
# ==========================================
TARGET_SCORE = 90                # 满意度：达到此分数则判定为该轮“合格”
REQUIRED_STABLE_ROUNDS = 3       # 满意轮数：需要连续 N 轮达标才停止优化
REPORT_CONTEXT_TRUNCATE_CHARS = 3000  # 注入 LLM 的报告最大字符数
TEST_SUBPROCESS_TIMEOUT_SEC = 180  # 测试脚本 subprocess 超时（秒），防止永久阻塞

SERVER_SCRIPT = "server.py"
TEST_SCRIPT = "auto_test_suite.py"

# 1. 验证 Gemini 配置
gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    logger.error("❌ 找不到 GEMINI_API_KEY，请确保在 config.env 中已配置！")
    exit(1)

genai.configure(api_key=gemini_api_key)
model = genai.GenerativeModel('gemini-2.5-pro')

def parse_retry_delay_seconds(error: Exception, attempt: int) -> int:
    """从 Gemini 错误中提取服务端建议的重试秒数。"""
    default_wait = 30 * (attempt + 1)
    error_str = str(error)

    retry_match = re.search(r"Please retry in\s+(\d+(?:\.\d+)?)s", error_str, re.IGNORECASE)
    if retry_match:
        return max(1, int(float(retry_match.group(1))) + 1)

    retry_delay = getattr(error, "retry_delay", None)
    if retry_delay:
        seconds = int(getattr(retry_delay, "seconds", 0) or 0)
        if seconds > 0:
            return seconds + 1

    return default_wait

async def wait_for_server_ready(host: str = "127.0.0.1", port: int = 8000, timeout: float = 12.0) -> bool:
    """等待 server.py 监听端口，避免测试脚本抢跑。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.8):
                return True
        except OSError:
            await asyncio.sleep(0.3)
    return False

def extract_score(output_text):
    match = re.search(r'🏆 最终得分：\s*(\d+)', output_text)
    if match:
        return int(match.group(1))
    return 0

def sanitize_report_for_llm(report: str) -> str:
    """清洗测试报告，移除可能干扰 LLM 的特殊标记，防止 Prompt 注入。"""
    markers_to_remove = [
        "[TASK:", "[EVOLUTION", "[CRITICAL", "[INJECT", "[SYSTEM",
        "```system", "```instruction", "```config",
    ]
    result = report
    for marker in markers_to_remove:
        result = result.replace(marker, "[BLOCKED]")
    result = re.sub(r'[^\S\n]{30,}', ' ', result)
    return result

# ── 场景级优化工具函数 ────────────────────────────────────

async def call_gemini_to_fix_scene_rules(
    topic_id: int,
    current_rules: list,
    test_report: str,
) -> list:
    """
    调用 Gemini 优化指定话题的 scene_specific_rules。
    Returns: 优化后的规则列表，失败时返回 None。
    """
    db = SessionLocal()
    try:
        topic = db.query(Topic).filter(Topic.id == topic_id).first()
        if not topic:
            logger.error(f"❌ 话题 ID {topic_id} 不存在")
            return None
        scene_desc = topic.title
        role_name = topic.role_name or "Assistant"
        level = topic.learner_level or "Intermediate"
    finally:
        db.close()

    logger.info(f"🧠 [场景进化] 正在分析话题 {topic_id} '{scene_desc}' 的局限并生成护栏...")

    short_report = (
        test_report[-REPORT_CONTEXT_TRUNCATE_CHARS:]
        if len(test_report) > REPORT_CONTEXT_TRUNCATE_CHARS
        else test_report
    )
    short_report = sanitize_report_for_llm(short_report)

    rules_str = "\n".join(f"- {r}" for r in current_rules) if current_rules else "(无规则，当前为空)"

    prompt = f"""
|[TASK: SCENE-SPECIFIC RULES EVOLUTION FOR ENGLISH COACH]
|You are the scene rules optimizer for 'EnglishCoach'.
|Currently optimizing the topic: **{topic_id}** — "{scene_desc}"
|
|[SCENE METADATA]:
|- Role: {role_name}
|- Level: {level}
|
|[CURRENT scene_specific_rules]:
|{rules_str}
|
|[TEST REPORT (Adversarial Analysis)]:
|{short_report}
|
|[EVOLUTION GUIDELINES]:
|1. NUM_RULES: Generate exactly 2-4 rules. More is NOT better — quality over quantity.
|2. RULE QUALITY: Each rule should be a concrete "IF-THEN" guardrail. Describe the trigger condition and the exact response behavior.
|3. SCENE FOCUS: Rules must be specific to this scene's context (the role and scenario), NOT generic.
|4. NO OVERLAP: Rules should not contradict each other or duplicate universal rules already in the global prompt.
|5. LEVEL_AWARE: The rules should match the learner level ({level}) — Beginner rules should be simpler, Professional rules more demanding.
|
|[OUTPUT FORMAT]:
|Return a JSON array of rule strings (no extra text, no markdown code fences).
|Example:
|["If the user says they are not hungry, suggest a drink instead.", "Always confirm the full order before payment."]
"""

    for attempt in range(3):
        try:
            response = await asyncio.wait_for(
                model.generate_content_async(prompt),
                timeout=120.0,
            )
            raw = response.text.strip()

            if raw.startswith("["):
                try:
                    import re as _re
                    cleaned = _re.sub(r",\s*]", "]", _re.sub(r",\s*}", "}", raw))
                    new_rules = json.loads(cleaned)
                    if isinstance(new_rules, list) and all(isinstance(r, str) for r in new_rules):
                        logger.info(f"✅ 话题 {topic_id} 规则生成成功，得到 {len(new_rules)} 条专属规则")
                        return new_rules
                except Exception:
                    pass

            # 回退解析
            lines = raw.split("\n")
            new_rules = []
            for line in lines:
                line = line.strip().lstrip("-*0123456789. )")
                if len(line) > 10:
                    new_rules.append(line)
            if new_rules:
                logger.info(f"✅ 话题 {topic_id} 规则生成成功（文本回退），得到 {len(new_rules)} 条专属规则")
                return new_rules[:4]

        except asyncio.TimeoutError:
            logger.warning(f"⚠️ Gemini 请求超时（第 {attempt+1}/3）")
        except Exception as e:
            if "429" in str(e) or "Quota" in str(e) or "ResourceExhausted" in str(e):
                wait_s = parse_retry_delay_seconds(e, attempt)
                logger.warning(f"⚠️ Gemini 配额限制，等待 {wait_s}s（第 {attempt+1}/3）")
                await asyncio.sleep(wait_s)
            else:
                logger.error(f"❌ Gemini 调用失败: {e}")

    logger.error(f"❌ 话题 {topic_id} 规则优化失败（3次重试均失败）")
    return None

async def run_topic_optimization_loop(topic_id: int):
    """
    对单个话题运行对抗优化循环，直到 scene_specific_rules 达到目标分数。
    """
    db = SessionLocal()
    try:
        topic = db.query(Topic).filter(Topic.id == topic_id).first()
        topic_title = topic.title if topic else str(topic_id)
    finally:
        db.close()

    logger.info("=" * 60)
    logger.info(f"🧬 场景规则洗护器启动！目标话题: {topic_id} — {topic_title}")
    logger.info(f"🎯 目标满意度: >= {TARGET_SCORE} | 🛡️ 连续稳定轮数: {REQUIRED_STABLE_ROUNDS}")
    logger.info("=" * 60)

    win_env = os.environ.copy()
    win_env["PYTHONIOENCODING"] = "utf-8"

    stable_count = 0
    generation = 1

    current_rules = get_topic_rules(topic_id)
    if current_rules:
        logger.info(f"📋 初始专属规则: {current_rules}")
    else:
        logger.warning(f"⚠️ 话题 {topic_id} 暂无 scene_specific_rules，将从空白开始生成")

    while True:
        logger.info("-" * 40)
        logger.info(f"▶️ 第 {generation} 代验证 [话题: {topic_id}](当前连续达标: {stable_count}/{REQUIRED_STABLE_ROUNDS})")

        logger.info("🟢 正在唤醒后端服务器...")
        with open(SERVER_LOG_FILE, "a", encoding="utf-8") as server_log_fd:
            server_process = subprocess.Popen(
                [sys.executable, SERVER_SCRIPT],
                env=win_env,
                stdout=server_log_fd,
                stderr=subprocess.STDOUT,
            )
            ready = await wait_for_server_ready()
            if not ready:
                logger.warning("⚠️ 后端在预期时间内未就绪，测试可能失败。")

            logger.info(f"⚔️ 话题对抗测试进行中（约需1-2分钟）...")
            test_process = subprocess.Popen(
                [sys.executable, TEST_SCRIPT, "--topic-id", str(topic_id)],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                env=win_env,
            )

            try:
                test_stdout, test_stderr = test_process.communicate(timeout=TEST_SUBPROCESS_TIMEOUT_SEC)
            except subprocess.TimeoutExpired:
                test_process.kill()
                test_stdout, test_stderr = test_process.communicate()
                logger.error(f"⚠️ 测试脚本执行超时（>{TEST_SUBPROCESS_TIMEOUT_SEC}s），已被强制终止。")

            server_process.terminate()
            try:
                server_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server_process.kill()
                server_process.wait(timeout=3)

        score = extract_score(test_stdout)

        if score == 0:
            logger.warning("⚠️ 警告：瞬间 0 分！测试脚本可能已崩溃。")
            if test_stderr:
                logger.error(f"❌ 详细崩溃日志 (Stderr):\n{test_stderr.strip()}")
            else:
                logger.error(f"❌ 异常输出 (Stdout):\n{test_stdout[-500:]}")

            logger.info("⏳ 休眠 5 秒后重试...")
            await asyncio.sleep(5)
            generation += 1
            continue

        logger.info(f"📊 本轮裁决得分: {score}")

        if score >= TARGET_SCORE:
            stable_count += 1
            if stable_count >= REQUIRED_STABLE_ROUNDS:
                final_rules = get_topic_rules(topic_id)
                logger.info("🌟" * 20)
                logger.info(f"✅ 话题 {topic_id} '{topic_title}' 规则洗护完毕！连续 {REQUIRED_STABLE_ROUNDS} 轮达到目标满意度 ({TARGET_SCORE}分)")
                logger.info(f"🎖️ 最终落库规则:")
                for r in final_rules:
                    logger.info(f"   - {r}")
                logger.info("🌟" * 20)
                break
            else:
                logger.info("👌 表现达标，正在进行连续稳定性验证...")
                await asyncio.sleep(2)
        else:
            stable_count = 0
            logger.warning("❌ 分数未达标，触发 AI 护栏规则生成...")

            report_idx = test_stdout.find("📊 裁判最终报告：")
            report_content = test_stdout[report_idx:] if report_idx != -1 else test_stdout[-1500:]

            new_rules = await call_gemini_to_fix_scene_rules(
                topic_id, current_rules, report_content
            )

            if new_rules and len(new_rules) > 0:
                set_topic_rules(topic_id, new_rules)
                current_rules = new_rules
                logger.info(f"💾 AI 已完成护栏更新（第 {generation} 代）并写入数据库，准备下一轮验证。")
            else:
                logger.warning("⚠️ 规则生成失败，使用原规则进入下一轮。")

            await asyncio.sleep(3)

        generation += 1

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="英语教练 AI - 场景规则自动洗护器 (Data Scrubber)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python auto_optimizer.py --topic-id 1                # 自动洗护话题 ID=1 的专属规则 (scene_specific_rules)
  python auto_optimizer.py --all-topics                 # 批量洗护 Topic DB 中的所有话题
        """,
    )
    
    # 将参数设为互斥且必填，避免之前不小心运行全局替换
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument(
        "--topic-id", type=int, default=None,
        help="要洗护的话题 ID（整数，从 Topic DB 查询）"
    )
    group.add_argument(
        "--all-topics", action="store_true",
        help="依次洗护 Topic DB 中的所有话题"
    )
    args = parser.parse_args()

    try:
        if args.topic_id:
            asyncio.run(run_topic_optimization_loop(args.topic_id))
        elif args.all_topics:
            topics = list_all_topics()
            logger.info(f"🔄 准备依次洗护 {len(topics)} 个话题: {[t['title'] for t in topics]}")
            for t in topics:
                asyncio.run(run_topic_optimization_loop(t["id"]))
    except KeyboardInterrupt:
        logger.warning("🛑 用户中断：已停止规则洗护循环。")