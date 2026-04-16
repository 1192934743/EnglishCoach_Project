import os
import sys
from dotenv import load_dotenv

# ==========================================
# 1. 环境与代理配置 
# ==========================================
load_dotenv("config.env")
proxy_url = os.getenv("HTTP_PROXY", "http://127.0.0.1:7890")
os.environ['http_proxy'] = proxy_url
os.environ['https_proxy'] = proxy_url
os.environ['HTTP_PROXY'] = proxy_url
os.environ['HTTPS_PROXY'] = proxy_url

print(f"🌐 当前使用的代理: {proxy_url}")

import google.generativeai as genai

# ==========================================
# 2. 初始化 API
# ==========================================
gemini_api_key = os.getenv("GEMINI_API_KEY")
if not gemini_api_key:
    print("❌ 找不到 GEMINI_API_KEY，请检查 config.env 文件！")
    sys.exit(1)

genai.configure(api_key=gemini_api_key)

def test_gemini():
    print("\n🔍 正在查询当前 API Key 支持的模型列表...")
    try:
        available_models = []
        # 拉取当前 Key 支持的所有模型
        for m in genai.list_models():
            if 'generateContent' in m.supported_generation_methods:
                available_models.append(m.name)
                print(f" - 支持的模型: {m.name}")

        if not available_models:
            print("❌ 您的 API Key 没有找到支持文本生成的模型。")
            return

        print("\n✅ 模型列表拉取成功！")
        
        # 随便挑列表里的第一个能用的模型进行一次真实的对话测试
        target_model_name = available_models[0]
        print(f"\n🚀 选定测试模型: {target_model_name}")
        print("⏳ 正在发送测试请求 (Hello World)...")

        # SDK 要求传入时通常不带 'models/' 前缀
        model_id = target_model_name.replace("models/", "")
        model = genai.GenerativeModel(model_id)

        response = model.generate_content("Please reply exactly: 'Hello World! API is working.'")
        print(f"\n✅ 测试成功！Gemini 返回内容: \n{response.text.strip()}")

    except Exception as e:
        print(f"\n❌ 测试失败，错误信息: {e}")

if __name__ == "__main__":
    test_gemini()