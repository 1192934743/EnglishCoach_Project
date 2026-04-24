# EnglishCoach Backend Docker Image
FROM python:3.10-slim

# 设置时区
ENV TZ=Asia/Shanghai
RUN ln -snf /usr/share/zoneinfo/$TZ /etc/localtime && echo $TZ > /etc/timezone

# 安装系统依赖：ffmpeg (音频处理) 和 build-essential (编译依赖)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# 设置工作目录
WORKDIR /app

# 复制依赖文件
COPY requirements.txt .

# 安装 Python 依赖
RUN pip install --no-cache-dir -r requirements.txt

# 复制应用代码
COPY . .

# 创建数据目录（用于 SQLite 和日志）
RUN mkdir -p /app/data /app/logs

# 暴露端口
EXPOSE 8000

# 启动命令：使用 uvicorn 运行 FastAPI
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000"]
