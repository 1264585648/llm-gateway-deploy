#!/usr/bin/env bash
# =========================================================
# AWS Quick (aws-quickwork-2api) 无头多号池一键编译、启动与 New API 渠道自动注册脚本
# 执行位置：在主站 /opt/llm-gateway 目录下运行
#   bash scripts/deploy_quickwork2api.sh
# =========================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

if [[ ! -f ".env" ]]; then
  echo "[ERROR] 找不到 .env 文件，请先完成主站基础配置！"
  exit 1
fi

set -a
# shellcheck disable=SC1091
source .env
set +a

echo "==> [1/4] 拉取 aws-quickwork-2api 源码..."
if [[ ! -d "aws-quickwork-2api" ]]; then
  git clone --depth 1 https://github.com/jasonxu114514/aws-quickwork-2api.git
fi

echo "==> [2/4] 使用 golang:1.26-alpine 容器免环境编译 quickproxy 静态二进制..."
mkdir -p aws-quickwork-2api/bin aws-quickwork-2api/data
docker run --rm \
  -u "$(id -u):$(id -g)" \
  -e CGO_ENABLED=0 \
  -e GOCACHE=/tmp/gocache \
  -e GOPATH=/tmp/gopath \
  -v "${PROJECT_DIR}/aws-quickwork-2api:/src" \
  -w /src \
  golang:1.26-alpine \
  go build -ldflags="-s -w" -o bin/quickproxy ./cmd/quickproxy

echo "==> [3/4] 生成内部鉴权密钥并配置 config.json (监听 0.0.0.0:8080 供 Docker 内网调用)..."
QUICK_KEY="sk-quick-$(openssl rand -hex 16)"
python3 - <<EOF
import json
cfg_path = "aws-quickwork-2api/config.json"
with open(cfg_path, "r") as f:
    cfg = json.load(f)
cfg["addr"] = "0.0.0.0:8080"
cfg["api_keys"] = ["${QUICK_KEY}"]
with open(cfg_path, "w") as f:
    json.dump(cfg, f, indent=2)
EOF
chmod 600 aws-quickwork-2api/config.json

docker compose up -d quickwork2api
sleep 3

# 修复 data 目录权限，方便宿主机普通用户直接执行 ./bin/quickproxy login --provider social -manual
sudo chown -R "$(id -u):$(id -g)" aws-quickwork-2api/data
chmod 777 aws-quickwork-2api/data
chmod 666 aws-quickwork-2api/data/* 2>/dev/null || true

echo "==> [4/4] 自动向 New API 数据库注册 AWS-Quick-Pool 渠道并刷新缓存..."
NOW="$(date +%s)"
MODELS="smart,fast,balanced,auto,claude-sonnet-4-5,claude-sonnet-4-5-20250929,claude-3-7-sonnet-20250219,claude-3-5-sonnet-20241022,claude-3-5-haiku-20241022,claude-opus-4-5"
MAPPING='{"claude-sonnet-4-5":"smart","claude-sonnet-4-5-20250929":"smart","claude-3-7-sonnet-20250219":"smart","claude-3-5-sonnet-20241022":"smart","claude-opus-4-5":"smart","claude-3-5-haiku-20241022":"fast"}'

docker exec new-api-mysql mysql -u root -p"${MYSQL_ROOT_PASSWORD}" "${MYSQL_DATABASE}" -e "
DELETE FROM channels WHERE name='AWS-Quick-Pool';
INSERT INTO channels (type, \`key\`, test_model, status, name, weight, created_time, base_url, models, \`group\`, model_mapping, priority, auto_ban, remark)
VALUES (1, '${QUICK_KEY}', 'smart', 1, 'AWS-Quick-Pool', 10, ${NOW}, 'http://quickwork2api:8080', '${MODELS}', 'default,free_fans', '${MAPPING}', 0, 0, 'AWS Quick Google 多号轮询池');
"

docker compose restart new-api
sleep 3

echo "========================================================="
echo "[DONE] aws-quickwork-2api 部署并接入 New API 完成！"
echo "下一步添加 Google 账号命令："
echo "  cd ${PROJECT_DIR}/aws-quickwork-2api && ./bin/quickproxy login --provider social -manual"
echo "========================================================="
