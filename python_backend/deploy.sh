#!/bin/bash
# EnglishCoach Backend 部署脚本
# 用法: ./deploy.sh [可选: git_branch]

set -e

# 配置
APP_NAME="englishcoach"
BACKEND_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
GIT_BRANCH="${1:-main}"  # 默认 main 分支，可传入参数指定

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

# 进入后端目录
cd "$BACKEND_DIR"

log_info "=========================================="
log_info "EnglishCoach Backend 部署脚本"
log_info "=========================================="
log_info "工作目录: $BACKEND_DIR"
log_info "Git 分支: $GIT_BRANCH"

# 1. 检查并安装 Docker
check_docker() {
    if ! command -v docker &> /dev/null; then
        log_warn "Docker 未安装，正在安装..."

        # Ubuntu/Debian 安装脚本
        curl -fsSL https://get.docker.com -o get-docker.sh
        sudo sh get-docker.sh
        sudo usermod -aG docker $USER

        rm -f get-docker.sh
        log_info "Docker 安装完成!"
    else
        log_info "Docker 已安装: $(docker --version)"
    fi

    if ! command -v docker-compose &> /dev/null && ! docker compose version &> /dev/null; then
        log_warn "Docker Compose 未安装，正在安装..."
        sudo apt-get update
        sudo apt-get install -y docker-compose
        log_info "Docker Compose 安装完成!"
    fi
}

# 2. 创建必要目录
setup_dirs() {
    log_info "创建必要目录..."
    mkdir -p data logs
    log_info "目录创建完成: ./data, ./logs"
}

# 3. 拉取最新代码
pull_code() {
    log_info "检查 Git 仓库..."
    if [ -d .git ]; then
        git fetch origin
        log_info "切换到分支: $GIT_BRANCH"
        git checkout "$GIT_BRANCH"
        git pull origin "$GIT_BRANCH"
        log_info "代码更新完成!"
    else
        log_warn "不是 Git 仓库，跳过代码拉取"
    fi
}

# 4. 构建并启动容器
deploy() {
    log_info "停止旧容器（如有）..."
    docker compose down || docker-compose down 2>/dev/null || true

    log_info "构建 Docker 镜像..."
    docker compose build --no-cache backend || docker-compose build --no-cache backend

    log_info "启动容器（后台运行）..."
    docker compose up -d backend || docker-compose up -d backend

    log_info "等待服务启动..."
    sleep 5

    # 检查容器状态
    if docker compose ps | grep -q "englishcoach-backend.*Up"; then
        log_info "容器启动成功!"
    else
        log_error "容器启动可能失败，请检查日志: docker compose logs backend"
    fi
}

# 5. 显示状态
show_status() {
    log_info "=========================================="
    log_info "部署完成！当前状态:"
    echo ""
    docker compose ps || docker-compose ps
    echo ""
    log_info "查看日志: docker compose logs -f backend"
    log_info "=========================================="
}

# 主流程
main() {
    check_docker
    setup_dirs
    pull_code
    deploy
    show_status
}

main "$@"
