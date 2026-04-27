import os
import re
import time
import asyncio
import subprocess
import sys
import logging
import socket
import ast  # 引入 AST 模块进行代码语法检测
import shutil      # 用于复制文件保存快照
import datetime    # 用于生成时间戳后缀
import argparse    # 命令行参数解析
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

# ⚠️ 注意：必须在设置完环境变量后，再导入 google.generativeai
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
# ⚙️ 进化引擎配置 (Agent Config)
# ==========================================
TARGET_SCORE = 90                # 满意度：达到此分数则判定为该轮“合格”
REQUIRED_STABLE_ROUNDS = 3       # 满意轮数：需要连续 N 轮达标才停止优化
REPORT_CONTEXT_TRUNCATE_CHARS = 3000  # 注入 LLM 的报告最大字符数
SNAPSHOT_ARCHIVE_MIN_SCORE = 75  # 快照存档最低分数门槛
TEST_SUBPROCESS_TIMEOUT_SEC = 180  # 测试脚本 subprocess 超时（秒），防止永久阻塞
# ==========================================

ENGINE_FILE = "core/dialogue_engine.py"
SERVER_SCRIPT = "server.py"
TEST_SCRIPT = "auto_test_suite.py"

# 🌟 新增：历史高分快照保存目录
ARCHIVE_DIR = "archive_prompts"
os.makedirs(ARCHIVE_DIR, exist_ok=True)

# 🌟 场景级优化专用路径（已迁移至 Topic DB，保留常量用于日志提示）

# 1. 验证 Gemini 配置
gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    logger.error("❌ 找不到 GEMINI_API_KEY，请确保在 config.env 中已配置！")
    exit(1)

genai.configure(api_key=gemini_api_key)
# 🌟 修复 404 报错：指定 latest 版本后缀
model = genai.GenerativeModel('gemini-2.5-pro')


def parse_retry_delay_seconds(error: Exception, attempt: int) -> int:
    """
    从 Gemini 错误中提取服务端建议的重试秒数。
    若无法提取，则回退到指数递增等待策略。
    """
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

def read_engine_code():
    if not os.path.exists(ENGINE_FILE):
        return ""
    with open(ENGINE_FILE, "r", encoding="utf-8") as f:
        return f.read()

def write_engine_code(new_code):
    dir_path = os.path.dirname(ENGINE_FILE)
    os.makedirs(dir_path, exist_ok=True)
    tmp_path = os.path.join(dir_path, f".engine_{os.getpid()}.tmp")
    try:
        with open(tmp_path, "w", encoding="utf-8") as f:
            f.write(new_code)
        os.replace(tmp_path, ENGINE_FILE)
    except Exception:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
        raise

def extract_score(output_text):
    match = re.search(r'🏆 最终得分：\s*(\d+)', output_text)
    if match:
        return int(match.group(1))
    return 0

def clean_code(text):
    if "```python" in text:
        text = text.split("```python")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    return text.strip() + "\n"

def extract_target_func(full_code, target_func="build_dynamic_prompt"):
    """简易 AST 解析：从全量代码中精准提取目标函数的代码"""
    lines = full_code.split('\n')
    start_idx = -1
    for i, line in enumerate(lines):
        if line.startswith(f"def {target_func}"):
            start_idx = i
            break
            
    if start_idx == -1:
        return None, -1, -1
        
    end_idx = len(lines)
    for i in range(start_idx + 1, len(lines)):
        # 只要遇到非空行且没有缩进，说明上一个函数结束了
        if lines[i].strip() and not lines[i].startswith(" ") and not lines[i].startswith("\t"):
            end_idx = i
            break
            
    func_code = '\n'.join(lines[start_idx:end_idx])
    return func_code, start_idx, end_idx

def sanitize_report_for_llm(report: str) -> str:
    """清洗测试报告，移除可能干扰 LLM 的特殊标记，防止 Prompt 注入。"""
    # 移除常见的 Prompt 注入标记
    markers_to_remove = [
        "[TASK:", "[EVOLUTION", "[CRITICAL", "[INJECT", "[SYSTEM",
        "```system", "```instruction", "```config",
    ]
    result = report
    for marker in markers_to_remove:
        result = result.replace(marker, "[BLOCKED]")
    # 限制连续特殊字符数量（防止 ASCII art 或混淆）
    result = re.sub(r'[^\S\n]{30,}', ' ', result)
    return result


# ── 场景级优化工具函数（已迁移至 Topic DB）────────────────────────────────────


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

    logger.info(f"🧠 [场景进化] 正在优化话题 {topic_id} '{scene_desc}' 的 scene_specific_rules...")

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
|[TEST REPORT (20-Round Adversarial Analysis)]:
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

            # 尝试解析 JSON 数组
            if raw.startswith("["):
                try:
                    # 直接解析（可能有尾随逗号/换行）
                    import re as _re
                    cleaned = _re.sub(r",\s*]", "]", _re.sub(r",\s*}", "}", raw))
                    new_rules = json.loads(cleaned)
                    if isinstance(new_rules, list) and all(isinstance(r, str) for r in new_rules):
                        logger.info(f"✅ 话题 {topic_id} 规则优化成功，得到 {len(new_rules)} 条规则")
                        return new_rules
                except Exception:
                    pass

            # 回退：纯文本解析
            lines = raw.split("\n")
            new_rules = []
            for line in lines:
                line = line.strip().lstrip("-*0123456789. )")
                if len(line) > 10:
                    new_rules.append(line)
            if new_rules:
                logger.info(f"✅ 话题 {topic_id} 规则优化成功（文本回退），得到 {len(new_rules)} 条规则")
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
    logger.info(f"🧬 话题级进化引擎启动！目标话题: {topic_id} — {topic_title}")
    logger.info(f"🎯 目标满意度: >= {TARGET_SCORE} | 🛡️ 连续稳定轮数: {REQUIRED_STABLE_ROUNDS}")
    logger.info("=" * 60)

    win_env = os.environ.copy()
    win_env["PYTHONIOENCODING"] = "utf-8"

    stable_count = 0
    generation = 1

    current_rules = get_topic_rules(topic_id)
    if current_rules:
        logger.info(f"📋 初始规则: {current_rules}")
    else:
        logger.warning(f"⚠️ 话题 {topic_id} 暂无 scene_specific_rules，将从空白开始生成")

    while True:
        logger.info("-" * 40)
        logger.info(f"▶️ 第 {generation} 代测试 [话题: {topic_id}](当前连续达标: {stable_count}/{REQUIRED_STABLE_ROUNDS})")

        # 1. 启动后端服务器
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

            # 2. 运行话题对抗测试
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

        # 3. 提取得分
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
                logger.info(f"✅ 话题 {topic_id} '{topic_title}' 优化完毕！连续 {REQUIRED_STABLE_ROUNDS} 轮达到目标满意度 ({TARGET_SCORE}分)")
                logger.info(f"🎖️ 最终规则:")
                for r in final_rules:
                    logger.info(f"   - {r}")
                logger.info("🌟" * 20)
                break
            else:
                logger.info("👌 表现达标，正在进行连续稳定性验证...")
                await asyncio.sleep(2)
        else:
            stable_count = 0
            logger.warning("❌ 分数未达标，触发话题规则自动重构...")

            report_idx = test_stdout.find("📊 裁判最终报告：")
            report_content = test_stdout[report_idx:] if report_idx != -1 else test_stdout[-1500:]

            new_rules = await call_gemini_to_fix_scene_rules(
                topic_id, current_rules, report_content
            )

            if new_rules and len(new_rules) > 0:
                set_topic_rules(topic_id, new_rules)
                current_rules = new_rules
                logger.info(f"💾 AI 已完成话题规则优化（第 {generation} 代），准备下一轮验证。")
            else:
                logger.warning("⚠️ 规则生成失败，直接进入下一轮演化。")

            await asyncio.sleep(3)

        generation += 1


async def call_gemini_to_fix(report, current_full_code):
    logger.info("🧠 [AI Agent]: 正在提取核心函数并进行 Prompt 重构 (保留20轮测试报告)...")

    # 1. 精准提取目标函数，抛弃其他无关代码
    func_code, start_idx, end_idx = extract_target_func(current_full_code)
    if not func_code:
        logger.error("❌ 无法从文件中定位到 build_dynamic_prompt 函数！")
        return None

    short_report = report[-REPORT_CONTEXT_TRUNCATE_CHARS:] if len(report) > REPORT_CONTEXT_TRUNCATE_CHARS else report
    short_report = sanitize_report_for_llm(short_report)

    prompt = f"""
[TASK: SYSTEM EVOLUTION FOR ENGLISH COACH]
You are the lead Python architect and Prompt Engineer for 'EnglishCoach'.
Your task is to optimize the `build_dynamic_prompt` function based on the test report.

[TEST REPORT (20-Round Analysis)]:
{short_report}

[EVOLUTION GUIDELINES]:
1. AVOID OVERFITTING: DO NOT hardcode specific scenes (like "McDonald's" or "ordering food") into the prompts. Always rely on the dynamic variables (scene_name, role_name).
2. LIMIT RULES: Keep the "UNIVERSAL COACHING RULES" to a maximum of 5 distinct, concise rules. Do not bloat the prompt.
3. FIX FIREWALLS: Address the specific phase bleeding or logic issues pointed out in the Test Report. Ensure the boundaries between ICE_BREAKING, CORE_TASK, EVENT_EXTENSION, and WRAP_UP are absolute.

[CRITICAL SYNTAX WARNING 🛑]:
- You MUST use the "List Array Append" approach instead of multi-line f-strings to avoid syntax errors.
- Define a list like `prompt_blocks = []`, then use `prompt_blocks.append(...)` for each line or block.
- Finally return `"\\n".join(prompt_blocks)`.
- DO NOT use multi-line strings (`\"\"\"`).

[CURRENT FUNCTION CODE]:
```python
{func_code}
```
[STRICT OUTPUT FORMAT]:
You MUST return ONLY the completely rewritten build_dynamic_prompt Python function.
DO NOT include imports, other classes, or any other functions.
Return ONLY valid Python code inside a markdown code block. No extra chat.
"""

    current_prompt = prompt
    max_retries = 3
    
    for attempt in range(max_retries):
        try:
            response = await asyncio.wait_for(
                model.generate_content_async(current_prompt),
                timeout=120.0
            )
            new_func_code = clean_code(response.text)

            # 3. 将 AI 返回的新函数无缝拼接回原始的完整文件代码中
            lines = current_full_code.split('\n')
            new_full_code = '\n'.join(lines[:start_idx]) + "\n" + new_func_code + "\n" + '\n'.join(lines[end_idx:])
            
            # 🌟 内部语法自愈校验机制
            try:
                ast.parse(new_full_code)
                return new_full_code # 语法完美通过，直接返回给主程序
                
            except SyntaxError as err:
                logger.warning(f"⚠️ [内部自愈 {attempt+1}/{max_retries}]: AI 生成的代码存在语法错误 ({err})")
                
                if attempt < max_retries - 1:
                    logger.info("🔧 正在要求 AI 原地修复语法错误 (保留本次优化思路，无需重新跑测试)...")
                    current_prompt = f"""
[CRITICAL ERROR FEEDBACK]
Your previous attempt resulted in a Python SyntaxError: {err}

Here is your broken code:
```python
{new_func_code}
```

HOW TO FIX IT:
1. You likely messed up quotation marks (`"`, `'`) or string concatenation.
2. DO NOT use multi-line f-strings (`\"\"\"`). Use a Python list (e.g., `prompt_blocks = []`) and `.append()`.
3. Ensure all strings inside `.append()` are properly closed.

Please FIX the syntax error and return ONLY the valid `build_dynamic_prompt` function. Do not change your prompt strategy, just fix the Python syntax!
"""
                    continue
                else:
                    logger.error("❌ 连续 3 次语法修复均失败，彻底放弃本次重构。")
                    return None

        except asyncio.TimeoutError:
            logger.error("❌ Gemini API 请求超时！(请检查网络代理)")
            return None
        except asyncio.CancelledError:
            logger.warning("⚠️ 收到中断信号，终止本次 Gemini 重构请求。")
            raise
        except Exception as e:
            error_str = str(e)
            if "429" in error_str or "Quota" in error_str or "ResourceExhausted" in error_str:
                wait_seconds = parse_retry_delay_seconds(e, attempt)
                logger.warning(
                    f"⚠️ 触发 Gemini Pro 频率/配额限制，等待 {wait_seconds} 秒后重试 "
                    f"(第 {attempt + 1}/{max_retries} 次)..."
                )
                await asyncio.sleep(wait_seconds)
            else:
                logger.error(f"❌ Gemini API 调用失败: {e}")
                return None

    logger.error("❌ 连续重试均失败，放弃本次重构。")
    return None


async def run_optimization_loop():
    logger.info("=" * 60)
    logger.info("🧬 英语教练 AI - 真·全自动 AI 进化引擎启动！")
    logger.info(f"🎯 目标满意度: >= {TARGET_SCORE} | 🛡️ 连续稳定轮数: {REQUIRED_STABLE_ROUNDS}")
    logger.info("=" * 60)

    win_env = os.environ.copy()
    win_env["PYTHONIOENCODING"] = "utf-8"

    stable_count = 0
    generation = 1
    best_score = 0  # 🌟 记录挂机期间的历史最高分

    while True:
        logger.info("-" * 40)
        logger.info(f"▶️ 第 {generation} 代测试开始 (当前连续达标: {stable_count}/{REQUIRED_STABLE_ROUNDS})")

        logger.info("🟢 正在尝试唤醒后端服务器...")
        with open(SERVER_LOG_FILE, "a", encoding="utf-8") as server_log_fd:
            server_process = subprocess.Popen(
                [sys.executable, SERVER_SCRIPT],
                env=win_env,
                stdout=server_log_fd,
                stderr=subprocess.STDOUT
            )
            ready = await wait_for_server_ready()
            if not ready:
                logger.warning("⚠️ 后端在预期时间内未就绪，测试可能失败。")

            logger.info("⚔️ 混沌对抗测试进行中 (约需1-2分钟)...")
            test_process = subprocess.Popen(
                [sys.executable, TEST_SCRIPT],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                env=win_env
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

        # 快照存档逻辑 (只保存 >= SNAPSHOT_ARCHIVE_MIN_SCORE 分，且打破当前记录的代码)
        if score >= SNAPSHOT_ARCHIVE_MIN_SCORE and score > best_score:
            best_score = score
            logger.info(f"🏆 发现新的历史最高分: {best_score} 分！正在生成快照备份...")
            timestamp = datetime.datetime.now().strftime("%m%d_%H%M%S")
            backup_filename = f"dialogue_engine_{score}pts_{timestamp}.py"
            backup_path = os.path.join(ARCHIVE_DIR, backup_filename)
            shutil.copyfile(ENGINE_FILE, backup_path)
            logger.info(f"💾 高分快照已安全保存至: {backup_path}")

        if score >= TARGET_SCORE:
            stable_count += 1
            if stable_count >= REQUIRED_STABLE_ROUNDS:
                logger.info("🌟" * 20)
                logger.info(f"✅ 优化完毕！系统已连续 {REQUIRED_STABLE_ROUNDS} 轮达到目标满意度 ({TARGET_SCORE}分)。")
                logger.info("🎉 当前 Prompt 已经达到防过拟合的生产级稳定性，停止演化！")
                logger.info("🌟" * 20)
                break
            else:
                logger.info("👌 表现达标，正在进行连续稳定性验证...")
                await asyncio.sleep(2)
        else:
            stable_count = 0
            logger.warning("❌ 分数未达标，触发自动重构流程...")

            report_idx = test_stdout.find("📊 裁判最终报告：")
            report_content = test_stdout[report_idx:] if report_idx != -1 else test_stdout[-1500:]

            current_code = read_engine_code()
            new_code = await call_gemini_to_fix(report_content, current_code)

            if new_code and len(new_code) > 2000:
                # 既然从 call_gemini_to_fix 活着出来，必定没语法错误，直接写入
                write_engine_code(new_code)
                logger.info("💾 AI 已完成代码重构 (内部语法检测通过)，覆盖原文件，准备下一轮验证。")
            else:
                logger.warning("⚠️ 代码生成或内部修复失败，直接进入下一轮演化。")

            await asyncio.sleep(3)

        generation += 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="英语教练 AI - 全自动进化引擎",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
使用示例:
  python auto_optimizer.py                                # 全局引擎优化（优化 build_dynamic_prompt）
  python auto_optimizer.py --topic-id 1                # 优化话题 ID=1 的 scene_specific_rules
  python auto_optimizer.py --topic-id 2                # 优化话题 ID=2 的 scene_specific_rules
  python auto_optimizer.py --all-topics                 # 依次优化 Topic DB 中的所有话题
        """,
    )
    parser.add_argument(
        "--topic-id", type=int, default=None,
        help="要优化的话题 ID（整数，从 Topic DB 查询）"
    )
    parser.add_argument(
        "--all-topics", action="store_true",
        help="依次优化 Topic DB 中的所有话题"
    )
    args = parser.parse_args()

    try:
        if args.topic_id:
            asyncio.run(run_topic_optimization_loop(args.topic_id))
        elif args.all_topics:
            topics = list_all_topics()
            logger.info(f"🔄 将依次优化 {len(topics)} 个话题: {[t['title'] for t in topics]}")
            for t in topics:
                asyncio.run(run_topic_optimization_loop(t["id"]))
        else:
            asyncio.run(run_optimization_loop())
    except KeyboardInterrupt:
        logger.warning("🛑 用户中断：已停止自动优化循环。")