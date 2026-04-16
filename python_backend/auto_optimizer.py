import os
import re
import time
import asyncio
import subprocess
import sys
import logging
import google.generativeai as genai
from dotenv import load_dotenv

# ==========================================
# 📝 日志配置 (不输出到控制台，全部写入文件)
# ==========================================
LOG_FILE = "evolution_engine.log"
SERVER_LOG_FILE = "server_output.log"

# 清理可能存在的默认 handler
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    encoding='utf-8'
)
logger = logging.getLogger(__name__)

# ==========================================
# ⚙️ 进化引擎配置 (Agent Config)
# ==========================================
TARGET_SCORE = 90                # 满意度：达到此分数则判定为该轮“合格”
REQUIRED_STABLE_ROUNDS = 3       # 满意轮数：需要连续 N 轮达标才停止优化
# ==========================================

# 路径配置
ENGINE_FILE = "core/dialogue_engine.py"
SERVER_SCRIPT = "server.py"
TEST_SCRIPT = "auto_test_suite.py"

load_dotenv("config.env")

# 1. 验证 Gemini 配置
gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    logger.error("❌ 找不到 GEMINI_API_KEY，请确保在 config.env 中已配置！")
    exit(1)

genai.configure(api_key=gemini_api_key)

# 🌟 核心升级：指定使用最强大的 gemini-1.5-pro 模型
model = genai.GenerativeModel('gemini-1.5-pro')

def read_engine_code():
    if not os.path.exists(ENGINE_FILE):
        return ""
    with open(ENGINE_FILE, "r", encoding="utf-8") as f:
        return f.read()

def write_engine_code(new_code):
    os.makedirs(os.path.dirname(ENGINE_FILE), exist_ok=True)
    with open(ENGINE_FILE, "w", encoding="utf-8") as f:
        f.write(new_code)

def extract_score(output_text):
    """从测试输出中提取裁判给出的百分制分数"""
    match = re.search(r'🏆 最终得分：\s*(\d+)', output_text)
    if match:
        return int(match.group(1))
    return 0

def clean_code(text):
    """清理大模型返回的 markdown 代码块包裹，只保留纯 Python 代码"""
    if "```python" in text:
        text = text.split("```python")[1].split("```")[0]
    elif "```" in text:
        text = text.split("```")[1].split("```")[0]
    return text.strip() + "\n"

async def call_gemini_to_fix(report, current_code):
    """自动将测试报告和当前代码发送给 Gemini Pro 进行重构"""
    logger.info("🧠 [Gemini Pro Agent]: 正在进行架构反思与 Prompt 重构...")
    
    prompt = f"""
[TASK: SYSTEM EVOLUTION FOR ENGLISH COACH]
You are the lead Python architect and Prompt Engineer for 'EnglishCoach'.
Your task is to optimize the `build_dynamic_prompt` function in the provided codebase based on the test report.

[TEST REPORT]:
{report}

[EVOLUTION GUIDELINES]:
1. AVOID OVERFITTING: DO NOT hardcode specific scenes (like "McDonald's" or "ordering food") into the prompts. Always rely on the dynamic variables (scene_name, role_name) injected from ACTIVE_SCENE.
2. LIMIT RULES: Keep the "UNIVERSAL COACHING RULES" to a maximum of 5 distinct, concise rules. Do not bloat the prompt.
3. FIX FIREWALLS: Address the specific phase bleeding or logic issues pointed out in the Test Report. Ensure the boundaries between ICE_BREAKING, CORE_TASK, EVENT_EXTENSION, and WRAP_UP are absolute.

[CURRENT CODE]:
```python
{current_code}
```

[STRICT OUTPUT FORMAT]:
You MUST return the ENTIRE, fully functional, modified dialogue_engine.py Python code.
DO NOT truncate any functions, imports, or logic outside of build_dynamic_prompt. I will write your exact output to the file.
Return ONLY valid Python code inside a markdown code block. No extra chat.
"""
    try:
        response = await model.generate_content_async(prompt)
        return clean_code(response.text)
    except Exception as e:
        logger.error(f"❌ Gemini API 调用失败: {e}")
        return None


async def run_optimization_loop():
    logger.info("=" * 60)
    logger.info("🧬 英语教练 AI - 真·全自动 Gemini Pro 进化引擎启动！")
    logger.info(f"🎯 目标满意度: >= {TARGET_SCORE} | 🛡️ 连续稳定轮数: {REQUIRED_STABLE_ROUNDS}")
    logger.info("=" * 60)

    # 🌟 修复 Windows 管道编码崩溃的关键：注入强制 UTF-8 环境变量
    win_env = os.environ.copy()
    win_env["PYTHONIOENCODING"] = "utf-8"

    stable_count = 0
    generation = 1

    while True:
        logger.info("-" * 40)
        logger.info(f"▶️ 第 {generation} 代测试开始 (当前连续达标: {stable_count}/{REQUIRED_STABLE_ROUNDS})")

        # 1. 启动后端 Server
        logger.info("🟢 正在尝试唤醒后端服务器...")

        # 将 server 的输出重定向到文件，彻底告别控制台打印
        server_log_fd = open(SERVER_LOG_FILE, "a", encoding="utf-8")
        server_process = subprocess.Popen(
            [sys.executable, SERVER_SCRIPT],  # 🌟 强制使用当前虚拟环境的 Python
            env=win_env,
            stdout=server_log_fd,
            stderr=subprocess.STDOUT
        )
        time.sleep(3)  # 等待服务器完全启动

        # 2. 运行自动化对抗测试
        logger.info("⚔️ 混沌对抗测试进行中 (约需1-2分钟)...")
        test_process = subprocess.Popen(
            [sys.executable, TEST_SCRIPT],  # 🌟 同样确保测试脚本也使用正确的 Python
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # 捕获错误输出
            text=True,
            encoding='utf-8',
            env=win_env  # 🌟 注入环境
        )

        test_stdout, test_stderr = test_process.communicate()

        # 稳妥地关闭服务器
        server_process.terminate()
        try:
            server_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            server_process.kill()

        # 释放日志文件句柄
        server_log_fd.close()

        # 3. 分析分数
        score = extract_score(test_stdout)

        # 🌟 增强版崩溃拦截
        if score == 0:
            logger.warning("⚠️ 警告：瞬间 0 分！测试脚本可能已崩溃。")
            if test_stderr:
                logger.error(f"❌ 详细崩溃日志 (Stderr):\n{test_stderr.strip()}")
            else:
                logger.error(f"❌ 异常输出 (Stdout):\n{test_stdout[-500:]}")

            logger.info("⏳ 休眠 5 秒后重试...")
            time.sleep(5)
            generation += 1
            continue

        logger.info(f"📊 本轮裁决得分: {score}")

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
                time.sleep(2)
        else:
            stable_count = 0
            logger.warning("❌ 分数未达标，触发自动重构流程...")

            report_idx = test_stdout.find("📊 裁判最终报告：")
            report_content = test_stdout[report_idx:] if report_idx != -1 else test_stdout[-1500:]

            current_code = read_engine_code()
            new_code = await call_gemini_to_fix(report_content, current_code)

            if new_code and len(new_code) > 2000:
                write_engine_code(new_code)
                logger.info("💾 Gemini Pro 已完成代码重构并覆盖原文件，准备下一轮验证。")
            else:
                logger.warning("⚠️ 代码生成异常或疑似截断，放弃本次修改，直接进行下一轮重试。")

            time.sleep(3)

        generation += 1


if __name__ == "__main__":
    asyncio.run(run_optimization_loop())