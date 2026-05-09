import os
import re
import sys
import time
import asyncio
import subprocess
import argparse
import datetime
import logging
import socket

# ================= 配置区 =================
SERVER_SCRIPT = "server.py"
TEST_SCRIPT = "auto_test_suite.py"
SERVER_LOG_FILE = "server_output_batch.log"
BATCH_REPORT_DIR = "batch_reports"
TEST_SUBPROCESS_TIMEOUT_SEC = 240  # 单次测试超时时间

os.makedirs(BATCH_REPORT_DIR, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger("BatchEvaluator")

def extract_score(output_text):
    """从测试输出中提取最终得分"""
    match = re.search(r'🏆 最终得分：\s*(\d+)', output_text)
    if match:
        return int(match.group(1))
    return 0

async def wait_for_server_ready(host: str = "127.0.0.1", port: int = 8000, timeout: float = 15.0) -> bool:
    """等待 FastAPI 服务器就绪"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.8):
                return True
        except OSError:
            await asyncio.sleep(0.5)
    return False

async def run_batch_tests(runs: int, topic_id: int = None):
    logger.info("=" * 60)
    logger.info(f"🚀 批量对话测试引擎启动！计划运行 {runs} 轮")
    if topic_id:
        logger.info(f"🎯 锁定测试话题 ID: {topic_id}")
    logger.info("=" * 60)

    win_env = os.environ.copy()
    win_env["PYTHONIOENCODING"] = "utf-8"

    scores = []
    failed_runs = 0
    run_details = []

    for i in range(1, runs + 1):
        logger.info("-" * 40)
        logger.info(f"▶️ 正在执行第 {i}/{runs} 轮测试...")

        # 1. 启动后端服务器
        with open(SERVER_LOG_FILE, "a", encoding="utf-8") as server_log_fd:
            server_process = subprocess.Popen(
                [sys.executable, SERVER_SCRIPT],
                env=win_env,
                stdout=server_log_fd,
                stderr=subprocess.STDOUT,
            )
            
            ready = await wait_for_server_ready()
            if not ready:
                logger.warning("⚠️ 后端服务未在预期时间内就绪，强制继续。")

            # 2. 运行对抗测试脚本
            test_cmd = [sys.executable, TEST_SCRIPT]
            if topic_id is not None:
                test_cmd.extend(["--topic-id", str(topic_id)])

            test_process = subprocess.Popen(
                test_cmd,
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
                logger.error(f"❌ 第 {i} 轮测试执行超时！")
                failed_runs += 1
                score = 0
            else:
                score = extract_score(test_stdout)
                if score == 0:
                    logger.warning("⚠️ 本轮得分为 0，可能是脚本崩溃或裁判拒绝打分。")
                    failed_runs += 1
                else:
                    logger.info(f"✅ 第 {i} 轮测试完成，得分: {score}/100")
                    scores.append(score)

                # 提取深度评价用于生成报告
                report_idx = test_stdout.find("📊 裁判最终报告：")
                judge_report = test_stdout[report_idx:] if report_idx != -1 else "未找到裁判报告"
                
                run_details.append({
                    "run_idx": i,
                    "score": score,
                    "report": judge_report.strip()
                })

            # 3. 关闭服务器，为下一轮做准备
            server_process.terminate()
            try:
                server_process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                server_process.kill()

    # ================= 生成评估报告 =================
    timestamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    report_path = os.path.join(BATCH_REPORT_DIR, f"evaluation_{timestamp}.md")
    
    avg_score = sum(scores) / len(scores) if scores else 0
    max_score = max(scores) if scores else 0
    min_score = min(scores) if scores else 0

    with open(report_path, "w", encoding="utf-8") as f:
        f.write(f"# 批量测试评估报告\n\n")
        f.write(f"**生成时间**: {datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"**总测试轮数**: {runs} (成功: {len(scores)}, 失败/异常: {failed_runs})\n")
        f.write(f"**测试话题 ID**: {topic_id if topic_id else '随机通用'}\n\n")
        f.write(f"## 📊 成绩统计\n")
        f.write(f"- **平均分**: {avg_score:.1f}/100\n")
        f.write(f"- **最高分**: {max_score}/100\n")
        f.write(f"- **最低分**: {min_score}/100\n\n")
        f.write(f"## 📝 详细记录\n\n")
        
        for detail in run_details:
            f.write(f"### 第 {detail['run_idx']} 轮 - 得分: {detail['score']}\n")
            f.write("```text\n")
            f.write(f"{detail['report']}\n")
            f.write("```\n\n")

    logger.info("=" * 60)
    logger.info(f"🎉 批量测试完成！")
    logger.info(f"📈 平均分: {avg_score:.1f} | 最高分: {max_score} | 最低分: {min_score}")
    logger.info(f"📄 详细评估报告已生成: {report_path}")
    logger.info("=" * 60)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="英语教练 AI - 批量自动化测试评估脚本")
    parser.add_argument("--runs", type=int, default=5, help="要执行的测试轮数 (默认: 5)")
    parser.add_argument("--topic-id", type=int, default=None, help="锁定测试特定话题 ID")
    args = parser.parse_args()

    try:
        asyncio.run(run_batch_tests(args.runs, args.topic_id))
    except KeyboardInterrupt:
        logger.warning("\n🛑 用户手动中断测试。")