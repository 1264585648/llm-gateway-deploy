#!/usr/bin/env bash
# =========================================================
# 自动账本备份脚本（MySQL 热备 + 可选加密 + Rclone 上传至 Cloudflare R2）
# 赠金机建站保命核心：建议配置 crontab 每 6 小时自动执行一次
# 例：0 */6 * * * /opt/llm-gateway/scripts/backup.sh >> /var/log/llm-backup.log 2>&1
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

# 若配置了加密口令，则用 AES-256-CBC 加密后再上传对象存储
if [[ -n "${BACKUP_ENCRYPT_PASSPHRASE:-}" && "${BACKUP_ENCRYPT_PASSPHRASE}" != "REPLACE_WITH_BACKUP_ENCRYPT_PASS" ]]; then
  ENC_FILE="${SQL_GZ}.enc"
  openssl enc -aes-256-cbc -salt -pbkdf2 \
    -pass "pass:${BACKUP_ENCRYPT_PASSPHRASE}" \
    -in "$SQL_GZ" \
    -out "$ENC_FILE"
  rm -f "$SQL_GZ"
  TARGET_FILE="$ENC_FILE"
  echo "[$(date '+%F %T')] 备份已加密: $(basename "$TARGET_FILE")"
fi

# 上传至异地对象存储（如 Cloudflare R2）
if [[ -n "${RCLONE_REMOTE:-}" ]] && command -v rclone >/dev/null 2>&1; then
  if rclone listremotes | grep -q "^${RCLONE_REMOTE%%:*}:"; then
    rclone copy "$TARGET_FILE" "${RCLONE_REMOTE}/db/" --transfers 2
    echo "[$(date '+%F %T')] 已同步至异地存储: ${RCLONE_REMOTE}/db/$(basename "$TARGET_FILE")"
  else
    echo "[WARN] rclone 尚未配置 remote '${RCLONE_REMOTE%%:*}'，仅保留本地备份"
  fi
fi

# 同步备份 AWS Quick 多号池 SQLite 账本（如存在）
QUICK_DB="${PROJECT_DIR}/aws-quickwork-2api/data/quickwork.db"
if [[ -f "$QUICK_DB" ]]; then
  QUICK_BAK="${BACKUP_DIR}/quickwork_${TIMESTAMP}.db.gz"
  gzip -c "$QUICK_DB" > "$QUICK_BAK"
  echo "[$(date '+%F %T')] 已备份 AWS Quick 号池数据库: $(basename "$QUICK_BAK")"
  if [[ -n "${RCLONE_REMOTE:-}" ]] && command -v rclone >/dev/null 2>&1 && rclone listremotes | grep -q "^${RCLONE_REMOTE%%:*}:"; then
    rclone copy "$QUICK_BAK" "${RCLONE_REMOTE}/db/" --transfers 2
  fi
fi

# 清理过期的本地备份
RETENTION_DAYS="${BACKUP_RETENTION_DAYS:-7}"
find "$BACKUP_DIR" -type f \( -name "*.sql.gz" -o -name "*.sql.gz.enc" -o -name "*.db.gz" \) -mtime "+${RETENTION_DAYS}" -delete
echo "[$(date '+%F %T')] 备份流程结束（本地保留最近 ${RETENTION_DAYS} 天）。"
