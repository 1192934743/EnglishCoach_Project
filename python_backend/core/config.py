import os
import logging
from dotenv import load_dotenv

# 定位到 python_backend 根目录
_BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# 初始化日志配置
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
logger = logging.getLogger("EnglishCoach")
try:
    _log_path = os.path.join(_BACKEND_DIR, "server_output.log")
    _fh = logging.FileHandler(_log_path, encoding="utf-8")
    _fh.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")
    )
    if not any(isinstance(h, logging.FileHandler) and getattr(h, "baseFilename", "") == os.path.abspath(_log_path) for h in logger.handlers):
        logger.addHandler(_fh)
    logger.info("EnglishCoach file log: %s", os.path.abspath(_log_path))
except OSError as _e:
    logger.warning("Could not open server_output.log: %s", _e)

# 加载环境变量
load_dotenv(os.path.join(_BACKEND_DIR, "config.env"))

CONFIG = {
    "DEEPSEEK_KEY": os.getenv("DEEPSEEK_KEY"),
    "DEEPSEEK_BASE": os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com"),
    "VOLC_API_KEY": os.getenv("VOLC_API_KEY"),
    "VOLC_RESOURCE_ID_ASR": os.getenv("VOLC_RESOURCE_ID"),
    "VOLC_RESOURCE_ID_TTS": os.getenv("VOLC_RESOURCE_ID_TTS"),
    "AZURE_SPEECH_KEY": os.getenv("AZURE_SPEECH_KEY"),
    "AZURE_SPEECH_REGION": os.getenv("AZURE_SPEECH_REGION", "eastasia"),
    "ASR_LANGUAGE": os.getenv("ASR_LANGUAGE", "en-US"),
    # TTS Configuration
    "DEFAULT_TTS_ENGINE": os.getenv("DEFAULT_TTS_ENGINE", "azure"),
    "VOICE": os.getenv("VOICE", "en-US-AriaNeural"),
}

if not CONFIG["DEEPSEEK_KEY"] or not CONFIG["VOLC_API_KEY"]:
    logger.error("🚨 致命错误: 缺少必要的环境变量 (DEEPSEEK_KEY 或 VOLC_API_KEY)。请检查 config.env 文件。")
    raise RuntimeError("Missing essential API keys in config.")

if not CONFIG.get("VOLC_RESOURCE_ID_ASR"):
    logger.warning("⚠️ 未设置 VOLC_RESOURCE_ID（ASR）：语音转写将在调用时失败，请检查 config.env。")
if not CONFIG.get("AZURE_SPEECH_KEY") or not CONFIG.get("AZURE_SPEECH_REGION"):
    logger.warning("⚠️ 未设置 AZURE_SPEECH_KEY/REGION：合成语音将在调用时失败，请检查 config.env。")

TEACHING_CONFIG = {"enable_correction": False, "enable_translation": True, "enable_hints": True}

# 业务全局常量
LLM_MAX_TOKENS = 500
DEFAULT_TOPIC_ID = 999
MAX_AUDIO_BYTES = 5 * 1024 * 1024
MAX_BUFFER_CHARS = 65
FIRST_TTS_EARLY_FLUSH_CHARS = 22
MAX_CONTEXT_TOKENS = 2000