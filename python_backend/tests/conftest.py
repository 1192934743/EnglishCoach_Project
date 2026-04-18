"""
pytest 入口：在收集用例、导入 server 之前补齐密钥，否则 server 模块级会 raise。
"""
from __future__ import annotations

import os
from pathlib import Path

_PLACEHOLDER = "pytest-placeholder-not-for-real-api"
os.environ.setdefault("DEEPSEEK_KEY", _PLACEHOLDER)
os.environ.setdefault("VOLC_API_KEY", _PLACEHOLDER)
os.environ.setdefault("VOLC_RESOURCE_ID", "pytest-asr-resource")
os.environ.setdefault("VOLC_RESOURCE_ID_TTS", "pytest-tts-resource")

# python_backend 为 cwd 时 load_dotenv("config.env") 才能命中
_backend_root = Path(__file__).resolve().parents[1]
os.chdir(_backend_root)
