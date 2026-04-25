# EnglishCoach

Flutter client (`flutter_frontend`) and Python FastAPI backend (`python_backend`) for an English AI coaching app.

## Quick start

**Backend**

```bash
cd python_backend
cp config.env.example config.env
# Edit config.env with your API keys, then:
pip install -r requirements.txt
python server.py
```

**Frontend**

```bash
cd flutter_frontend
flutter pub get
flutter run
```

Point the app WebSocket URL at your running backend (see `websocket_client.dart` / settings as applicable).

## Docker 部署

使用自动化脚本一键部署后端到任意分支。

### 前置条件

- Linux/macOS（带有 apt、yum 或 brew）
- Git

### 完整部署步骤

```bash
# 1. 克隆项目
git clone https://github.com/1192934743/EnglishCoach_Project.git
cd EnglishCoach_Project/python_backend

# 2. 复制并编辑环境变量文件
cp config.env.example config.env
# 编辑 .env 填入你的 API Keys 和数据库配置

# 3. 运行部署脚本（默认部署 main 分支）
chmod +x deploy.sh
./deploy.sh

# 4. 指定分支部署（如 azure）
./deploy.sh azure
```

### 参数说明

| 参数 | 说明 | 默认值 |
|------|------|--------|
| `branch_name` | Git 分支名 | `main` |

### 常用命令

```bash
# 查看容器状态
docker compose ps

# 查看实时日志
docker compose logs -f backend
sudo docker compose logs --tail=50 backend

# 启动容器
sudo docker compose up -d backend

# 停止容器
docker compose down

sudo docker compose build --no-cache backend

# 重新部署
./deploy.sh <branch>
```

### 工作原理

脚本会依次完成：
1. 检查并安装 Docker / Docker Compose
2. 创建必要的目录（`./data`, `./logs`）
3. 切换到指定分支并拉取最新代码
4. 使用 `--no-cache` 重新构建镜像
5. 启动容器并验证状态
