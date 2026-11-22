#!/usr/bin/env bash
# =========================================================
# 跨云迁机 / 灾备恢复脚本（从 .sql.gz 或 .sql.gz.enc 一键还原账本）
# 用法：bash scripts/restore.sh /path/to/newapi_YYYYMMDD_HHMMSS.sql.gz[.enc]
# =========================================================
set -euo pipefail

if [[ $# -lt 1 ]]; then
  echo "用法: bash $0 <备份文件路径 (.sql.gz 或 .sql.gz.enc)>"
  exit 1
fi

BACKUP_FILE="$1"
if [[ ! -f "$BACKUP_FILE" ]]; then
  echo "[ERROR] 备份文件不存在: ${BACKUP_FILE}"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${PROJECT_DIR}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[ERROR] 找不到 ${ENV_FILE}，请先将原服务器的 .env 恢复到项目根目录！"
  exit 1
fi

set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

echo "[WARN] 即将把 ${BACKUP_FILE} 覆盖还原到数据库 ${MYSQL_DATABASE}！"
read -r -p "确认继续？输入 YES 继续: " CONFIRM
if [[ "$CONFIRM" != "YES" ]]; then
  echo "已取消。"
  exit 0
fi

echo "==> 暂停 new-api 容器防止恢复期间写入冲突..."
cd "$PROJECT_DIR"
docker compose stop new-api || true

TMP_GZ=""
CLEANUP_TMP=false

if [[ "$BACKUP_FILE" == *.enc ]]; then
  if [[ -z "${BACKUP_ENCRYPT_PASSPHRASE:-}" ]]; then
    echo "[ERROR] .env 中未设置 BACKUP_ENCRYPT_PASSPHRASE，无法解密"
    exit 1
  fi
  TMP_GZ="$(mktemp /tmp/restore_XXXXXX.sql.gz)"
  CLEANUP_TMP=true
  echo "==> 解密备份文件..."
  openssl enc -d -aes-256-cbc -pbkdf2 \
    -pass "pass:${BACKUP_ENCRYPT_PASSPHRASE}" \
    -in "$BACKUP_FILE" \
    -out "$TMP_GZ"
else
  TMP_GZ="$BACKUP_FILE"
fi

echo "==> 正在导入 MySQL..."
gunzip -c "$TMP_GZ" | docker exec -i new-api-mysql \
  mysql -u root -p"${MYSQL_ROOT_PASSWORD}" "${MYSQL_DATABASE}"

if [[ "$CLEANUP_TMP" == true ]]; then
  rm -f "$TMP_GZ"
fi

echo "==> 重新启动 new-api 容器..."
docker compose up -d new-api
echo "[DONE] 账本还原完成！请登录后台核对用户余额、API Key 与渠道可用性。"
