#!/usr/bin/env bash
# =========================================================
# 新云服务器（OVH / AWS / GCP / Azure 等赠金机）一键初始化脚本
# 适用系统：Ubuntu 22.04 / 24.04 LTS, Debian 12
# 执行方式：sudo bash scripts/init_server.sh
# =========================================================
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "[ERROR] 请使用 root 或 sudo 运行此脚本"
  exit 1
fi

echo "==> [1/6] 更新系统包并安装基础工具..."
apt-get update -y
apt-get install -y \
  ca-certificates curl gnupg lsb-release git ufw fail2ban \
  nginx certbot python3-certbot-nginx rclone openssl htop jq

echo "==> [2/6] 配置 2GB Swap（防止小内存赠金机瞬时 OOM 杀掉 MySQL）..."
if [[ ! -f /swapfile ]]; then
  fallocate -l 2G /swapfile || dd if=/dev/zero of=/swapfile bs=1M count=2048
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "[OK] Swap 创建完成"
else
  echo "[SKIP] /swapfile 已存在"
fi

echo "==> [3/6] 开启 TCP BBR 拥塞控制与高并发内核调优..."
cat <<'EOF' > /etc/sysctl.d/99-llm-gateway.conf
# 开启 BBR 加速跨海长连接与 SSE 流式响应
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr

# 提升并发连接与端口复用能力
net.core.somaxconn = 65535
net.core.netdev_max_backlog = 16384
net.ipv4.tcp_max_syn_backlog = 8192
net.ipv4.tcp_tw_reuse = 1
net.ipv4.tcp_fin_timeout = 15
net.ipv4.tcp_keepalive_time = 600
net.ipv4.tcp_keepalive_intvl = 30
net.ipv4.tcp_keepalive_probes = 5
net.ipv4.ip_local_port_range = 10240 65535

# 降低 swap 倾向，尽量优先使用物理内存
vm.swappiness = 10
EOF
sysctl --system

echo "==> [4/6] 调大系统文件句柄上限 (ulimit -n 65535)..."
cat <<'EOF' > /etc/security/limits.d/99-llm-gateway.conf
* soft nofile 65535
* hard nofile 65535
root soft nofile 65535
root hard nofile 65535
EOF

echo "==> [5/6] 安装 Docker 与 Docker Compose 插件..."
if ! command -v docker >/dev/null 2>&1; then
  curl -fsSL https://get.docker.com | sh
  systemctl enable --now docker
else
  echo "[SKIP] Docker 已安装: $(docker --version)"
fi

echo "==> [6/6] 配置 UFW 防火墙（仅开放 22 / 22222 / 80 / 443）与 Fail2ban..."
mkdir -p /var/www/certbot
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH'
ufw allow 22222/tcp comment 'SSH Alt Port'
ufw allow 80/tcp comment 'HTTP ACME'
ufw allow 443/tcp comment 'HTTPS Gateway'
ufw --force enable
systemctl enable --now fail2ban
systemctl enable --now nginx

echo "========================================================="
echo "[DONE] 服务器初始化完成！"
echo "  - BBR 状态: $(sysctl -n net.ipv4.tcp_congestion_control)"
echo "  - Docker:   $(docker --version)"
echo "  - 防火墙:   仅放行 22 / 22222 / 80 / 443"
echo "下一步：进入项目目录，配置 .env 后执行 docker compose up -d"
echo "========================================================="
