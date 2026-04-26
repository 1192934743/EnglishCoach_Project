#!/usr/bin/env python3
"""
LLM API 测试脚本
用法: python scripts/test_llm_api.py --model deepseek-chat
       python scripts/test_llm_api.py --model doubao-pro

前提: 在 python_backend/config.env 中配置对应的 API Key
火山引擎豆包使用 VOLC_API_KEY（已配置）
"""

import os
import sys
import asyncio
import argparse
from pathlib import Path
from dotenv import load_dotenv

# 加载 config.env
_backend_dir = Path(__file__).parent.parent / "python_backend"
load_dotenv(_backend_dir / "config.env")

TEST_MESSAGES = [
    {"role": "system", "content": "You are a helpful assistant."},
    {"role": "user", "content": "Say 'Hello, API test passed!' in exactly those words."}
]

def get_client_config(model_name: str):
    """根据模型返回对应的 API 配置"""
    if "deepseek" in model_name:
        key = os.getenv("DEEPSEEK_KEY")
        base = os.getenv("DEEPSEEK_BASE", "https://api.deepseek.com")
        return key, base, model_name, "chat"
    elif "doubao" in model_name:
        key = os.getenv("VOLC_ARK_API_KEY")
        base = "https://ark.cn-beijing.volces.com/api/v3"
        model = os.getenv("DOUBAO_MODEL", "doubao-pro-32k")
        return key, base, model, "responses"
    elif "gpt" in model_name:
        key = os.getenv("OPENAI_KEY")
        base = os.getenv("OPENAI_BASE", "https://api.openai.com/v1")
        return key, base, model_name, "chat"
    else:
        raise ValueError(f"Unsupported model: {model_name}")

async def test_api(model: str, stream: bool = True):
    from openai import AsyncOpenAI

    key, base, model_id, api_type = get_client_config(model)
    if not key:
        print(f"Error: Missing API key for {model}")
        if "doubao" in model or api_type == "responses":
            print(f"  Please set VOLC_API_KEY in config.env (already configured)")
        elif "deepseek" in model:
            print(f"  Please set DEEPSEEK_KEY in config.env")
        else:
            print(f"  Please set OPENAI_KEY in config.env")
        return False

    client = AsyncOpenAI(api_key=key, base_url=base)
    print(f"Testing model: {model}")
    print(f"  API Base: {base}")
    print(f"  Model ID: {model_id}")

    try:
        if api_type == "responses":
            # 火山方舟 ARK API v3 格式
            response = await client.responses.create(
                model=model_id,
                input=[
                    {"role": msg["role"], "content": msg["content"]}
                    for msg in TEST_MESSAGES
                ],
                stream=stream
            )
            if stream:
                print("Stream response: ", end="", flush=True)
                async for chunk in response:
                    if chunk.choices:
                        for choice in chunk.choices:
                            if choice.delta and choice.delta.content:
                                print(choice.delta.content, end="", flush=True)
                print()
            else:
                print(f"Response: {response.output_text}")
        else:
            # 标准 Chat Completions API
            if stream:
                response = await client.chat.completions.create(
                    model=model_id,
                    messages=TEST_MESSAGES,
                    max_tokens=50,
                    stream=True
                )
                print("Stream response: ", end="", flush=True)
                async for chunk in response:
                    if chunk.choices and chunk.choices[0].delta.content:
                        print(chunk.choices[0].delta.content, end="", flush=True)
                print()
            else:
                response = await client.chat.completions.create(
                    model=model_id,
                    messages=TEST_MESSAGES,
                    max_tokens=50
                )
                print(f"Response: {response.choices[0].message.content}")
        return True
    except Exception as e:
        print(f"Error: {e}")
        return False

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Test LLM API")
    parser.add_argument("--model", default="deepseek-chat",
                        choices=["deepseek-chat", "doubao-pro", "doubao-lite", "gpt-4o-mini"],
                        help="Model to test")
    parser.add_argument("--no-stream", action="store_true", help="Disable streaming")
    args = parser.parse_args()

    success = asyncio.run(test_api(args.model, stream=not args.no_stream))

    if success:
        print("\nAPI test PASSED!")
        sys.exit(0)
    else:
        print("\nAPI test FAILED!")
        sys.exit(1)
