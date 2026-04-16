import ast
import asyncio
import logging
import os
import sys

from dotenv import load_dotenv
import google.generativeai as genai


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


def configure_environment() -> genai.GenerativeModel:
    """Load env vars, apply proxy settings, and create a Gemini model."""
    load_dotenv("config.env")

    proxy_url = os.getenv("HTTP_PROXY", "http://127.0.0.1:7890")
    for key in ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"):
        os.environ[key] = proxy_url

    gemini_api_key = os.getenv("GEMINI_API_KEY")
    if not gemini_api_key:
        logger.error("找不到 GEMINI_API_KEY，请确保已在 config.env 中配置。")
        sys.exit(1)

    genai.configure(api_key=gemini_api_key)
    return genai.GenerativeModel("gemini-2.5-pro")


def clean_code(text: str) -> str:
    """Extract Python code from a markdown code block if present."""
    if "```python" in text:
        text = text.split("```python", maxsplit=1)[1].split("```", maxsplit=1)[0]
    elif "```" in text:
        text = text.split("```", maxsplit=1)[1].split("```", maxsplit=1)[0]
    return text.strip() + "\n"


def build_initial_prompt(fake_report: str, broken_code: str) -> str:
    return f"""
[TASK]
You are a Python expert. Your task is to rewrite the `build_dynamic_prompt` function based on this fake report.

[FAKE TEST REPORT]
{fake_report}

[CRITICAL INSTRUCTION]
You MUST use the "List Array Append" approach instead of multi-line f-strings to avoid syntax errors.

[CURRENT BROKEN CODE]
```python
{broken_code}
```

[STRICT OUTPUT FORMAT]
Return ONLY the completely rewritten `build_dynamic_prompt` Python function inside a markdown code block. No extra text.
""".strip()


def build_repair_prompt(error: SyntaxError, new_func_code: str) -> str:
    return f"""
[CRITICAL ERROR FEEDBACK]
Your previous attempt resulted in a Python SyntaxError: {error}

[BROKEN CODE]
```python
{new_func_code}
```

[HOW TO FIX IT]
You likely messed up quotation marks or string concatenation.
Do NOT use multi-line f-strings.
Use a Python list such as `prompt_blocks = []` and `.append()`.
Ensure all strings inside `.append()` are properly closed.

[STRICT OUTPUT FORMAT]
Please FIX the syntax error and return ONLY the valid `build_dynamic_prompt` function inside a markdown code block.
""".strip()


async def test_call_gemini_with_healing(
    fake_report: str,
    broken_code: str,
    model: genai.GenerativeModel,
    max_retries: int = 3,
) -> str | None:
    """Ask Gemini to rewrite code and retry if AST validation fails."""
    logger.info("正在进行重构测试，验证 AST 自愈能力...")

    current_prompt = build_initial_prompt(fake_report, broken_code)

    for attempt in range(max_retries):
        try:
            logger.info("正在向 Gemini 发送请求 (尝试 %s/%s)...", attempt + 1, max_retries)
            response = await asyncio.wait_for(
                model.generate_content_async(current_prompt),
                timeout=60.0,
            )
            new_func_code = clean_code(response.text)

            logger.info("收到代码，正在进行 AST 语法校验...")
            try:
                ast.parse(new_func_code)
                logger.info("AST 校验通过，生成代码语法有效。")
                return new_func_code
            except SyntaxError as err:
                logger.warning(
                    "内部自愈 %s/%s：AI 生成的代码存在语法错误 (%s)",
                    attempt + 1,
                    max_retries,
                    err,
                )
                if attempt < max_retries - 1:
                    logger.info("正在动态修改 Prompt，要求 AI 修复语法错误...")
                    current_prompt = build_repair_prompt(err, new_func_code)
                    continue

                logger.error("连续 %s 次语法修复均失败。", max_retries)
                return None
        except Exception as exc:
            logger.error("请求失败: %s", exc)
            return None

    return None


async def main() -> None:
    model = configure_environment()

    fake_report = (
        "The AI is too polite. Make it slightly more direct and aggressive "
        "in the [UNIVERSAL RULES]."
    )

    broken_code = """
def build_dynamic_prompt(user, is_flipped, session_ctx):
    role_name = "Assistant"
    phase = session_ctx.get("phase", "ICE_BREAKING")

    prompt = f"Role: {role_name}\\n"
    prompt += "We need to fix this string
    return prompt
""".strip()

    logger.info("启动独立验证脚本...")
    final_code = await test_call_gemini_with_healing(fake_report, broken_code, model)

    if final_code:
        logger.info("\n最终生成的安全代码如下:\n%s\n%s", "=" * 40, final_code + "=" * 40)
    else:
        logger.error("测试失败，未能生成有效代码。")


if __name__ == "__main__":
    asyncio.run(main())