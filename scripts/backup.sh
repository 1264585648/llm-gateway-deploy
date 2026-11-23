#!/usr/bin/env bash
# =========================================================
# 自动账本备份脚本（MySQL 热备 + 号池凭证打包 + 可选加密 + Rclone 上传至 Cloudflare R2）
# 赠金机建站保命核心：建议配置 crontab 每 6 小时自动执行一次
# 例：0 */6 * * * /opt/llm-gateway/scripts/backup.sh >> /opt/llm-gateway/backups/backup.log 2>&1
# =========================================================
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
ENV_FILE="${PROJECT_DIR}/.env"

if [[ ! -f "$ENV_FILE" ]]; then
  echo "[ERROR] 找不到 ${ENV_FILE}"
  exit 1
fi

# 加载环境变量
set -a
# shellcheck disable=SC1090
source "$ENV_FILE"
set +a

BACKUP_DIR="${PROJECT_DIR}/backups"
mkdir -p "$BACKUP_DIR"

TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
SQL_GZ="${BACKUP_DIR}/${MYSQL_DATABASE}_${TIMESTAMP}.sql.gz"
TARGET_FILE="$SQL_GZ"

upload_r2() {
  local file="$1"
  if [[ -n "${RCLONE_REMOTE:-}" ]] && command -v rclone >/dev/null 2>&1; then
    if rclone listremotes | grep -q "^${RCLONE_REMOTE%%:*}:"; then
      rclone copy "$file" "${RCLONE_REMOTE}/db/" --transfers 2
      echo "[$(date '+%F %T')] 已同步至异地存储: ${RCLONE_REMOTE}/db/$(basename "$file")"
    else
      echo "[WARN] rclone 尚未配置 remote '${RCLONE_REMOTE%%:*}'，仅保留本地备份"
    fi
  fi
}

encrypt_file_if_configured() {
  local src="$1"
  if [[ -n "${BACKUP_ENCRYPT_PASSPHRASE:-}" && "${BACKUP_ENCRYPT_PASSPHRASE}" != "REPLACE_WITH_BACKUP_ENCRYPT_PASS" ]]; then
    local enc="${src}.enc"
    openssl enc -aes-256-cbc -salt -pbkdf2 \
      -pass "pass:${BACKUP_ENCRYPT_PASSPHRASE}" \
      -in "$src" \
      -out "$enc"
    rm -f "$src"
    echo "$enc"
  else
    echo "$src"
  fi
}

echo "[$(date '+%F %T')] 开始备份数据库 ${MYSQL_DATABASE}..."

# 使用 --single-transaction 实现 InnoDB 在线热备，不锁表不影响用户调用 API
docker exec new-api-mysql mysqldump \
  -u root \
  -p"${MYSQL_ROOT_PASSWORD}" \
  --single-transaction \
  --quick \
  --routines \
  --triggers \
  --default-character-set=utf8mb4 \
  "${MYSQL_DATABASE}" | gzip > "$SQL_GZ"

TARGET_FILE="$(encrypt_file_if_configured "$SQL_GZ")"
echo "[$(date '+%F %T')] 数据库备份就绪: $(basename "$TARGET_FILE")"
upload_r2 "$TARGET_FILE"

# 同步备份各 2API 号池凭证与核心账本（若存在）
POOLS_TAR="${BACKUP_DIR}/pools_${TIMESTAMP}.tar.gz"
POOL_PATHS=()
[[ -f "${PROJECT_DIR}/aws-quickwork-2api/data/quickwork.db" ]] && POOL_PATHS+=("aws-quickwork-2api/data/quickwork.db" "aws-quickwork-2api/config.json")
[[ -d "${PROJECT_DIR}/workbuddy2api/auths" ]] && POOL_PATHS+=("workbuddy2api/auths" "workbuddy2api/config.json")
[[ -f "${PROJECT_DIR}/workbuddy2api/data/state.json" ]] && POOL_PATHS+=("workbuddy2api/data/state.json")
[[ -f "${PROJECT_DIR}/workbuddy2api/data/api_keys.json" ]] && POOL_PATHS+=("workbuddy2api/data/api_keys.json")
[[ -f "${PROJECT_DIR}/workbuddy2api/data/providers.json" ]] && POOL_PATHS+=("workbuddy2api/data/providers.json")
[[ -f "${PROJECT_DIR}/workbuddy2api/data/proxies.json" ]] && POOL_PATHS+=("workbuddy2api/data/proxies.json")
[[ -f "${PROJECT_DIR}/muse2api/data/accounts.json" ]] && POOL_PATHS+=("muse2api/data/accounts.json")

if [[ ${#POOL_PATHS[@]} -gt 0 ]]; then
  tar -czf "$POOLS_TAR" -C "$PROJECT_DIR" "${POOL_PATHS[@]}"
  POOLS_FINAL="$(encrypt_file_if_configured "$POOLS_TAR")"
  echo "[$(date '+%F %T')] 号池凭证备份就绪: $(basename "$POOLS_FINAL")"
  upload_r2 "$POOLS_FINAL"
fi

# 清理过期的本地备份
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
find "$BACKUP_DIR" -type f \( -name "*.sql.gz" -o -name "*.sql.gz.enc" -o -name "*.db.gz" -o -name "*.tar.gz" -o -name "*.tar.gz.enc" \) -mtime "+${RETENTION_DAYS}" -delete
echo "[$(date '+%F %T')] 备份流程结束（本地保留最近 ${RETENTION_DAYS} 天）。"
