# FreeProxyPool v0.1.0 + B-01~B-08 优化部署脚本
# 在目标服务器上执行：cd /opt/FreeProxyPool && bash deploy.sh
#
# 前置条件：
#   - 已安装 Docker + Docker Compose
#   - 已有 .env 文件（含 API_KEY / ADMIN_PASSWORD 等）
#   - 新代码已拷贝到 /opt/FreeProxyPool（见下方 copy 步骤）

set -euo pipefail

cd /opt/FreeProxyPool

echo "== 1. 停止旧容器 =="
docker compose down

echo "== 2. 构建新镜像（含 maxminddb 依赖） =="
docker compose build

echo "== 3. 启动全部服务 =="
docker compose up -d

echo "== 4. 等待网关就绪 =="
sleep 5
curl -sf http://127.0.0.1:5020/health || true

echo "== 5. 查看容器状态 =="
docker compose ps

echo ""
echo "部署完成。请检查："
echo "  - 网关健康：curl http://<公网IP>:5020/health"
echo "  - 版本信息：curl http://<公网IP>:5020/version"
echo "  - 代理统计：curl -H 'API-KEY: <你的Key>' http://<公网IP>:5020/count/"
