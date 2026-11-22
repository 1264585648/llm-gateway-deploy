#!/usr/bin/env bash
# =========================================================
# 跨 C 段 / 跨机房轻量出口代理节点一键初始化脚本 (Gost v3 + BBR + UFW 白名单)
# 适用场景：在 OVH 第二机房（如 US-EAST-VA-1 d2-2）开轻量节点，为主站提供独立出口 IP
# 用法（可直接通过主站跳板远程执行）：
#   sudo bash init_egress_node.sh <主站公网IPv4> [代理端口，默认 18443]
# =========================================================
set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "[ERROR] 请使用 root 或 sudo 运行此脚本"
  exit 1
fi

MAIN_GATEWAY_IP="${1:-}"
PROXY_PORT="${2:-18443}"

if [[ -z "$MAIN_GATEWAY_IP" ]]; then
  echo "用法: sudo bash $0 <主站公网IPv4> [代理端口，默认 18443]"
  echo "示例: sudo bash $0 51.81.200.61 18443"
  exit 1
fi

echo "==> [1/4] 修复 Ubuntu 24.04 ssh.socket 接管问题，同时开启 22 与 22222 端口..."
cat > /etc/ssh/sshd_config.d/99-custom-ports.conf <<'EOF'
Port 22
Port 22222
PasswordAuthentication no
EOF
systemctl disable --now ssh.socket || true
systemctl enable --now ssh.service
systemctl restart ssh.service

echo "==> [2/4] 开启 TCP BBR 拥塞控制..."
cat > /etc/sysctl.d/99-bbr.conf <<'EOF'
net.core.default_qdisc=fq
net.ipv4.tcp_congestion_control=bbr
EOF
sysctl --system >/dev/null 2>&1 || true

echo "==> [3/4] 安装 Gost v3 并配置自适应 HTTP/SOCKS5 出口代理服务 (端口 ${PROXY_PORT})..."
if [[ ! -f /usr/local/bin/gost ]]; then
  curl -fsSL https://github.com/go-gost/gost/releases/download/v3.0.0-rc10/gost_3.0.0-rc10_linux_amd64.tar.gz -o /tmp/gost.tar.gz
  tar -xzf /tmp/gost.tar.gz -C /tmp
  mv /tmp/gost /usr/local/bin/gost
  chmod +x /usr/local/bin/gost
  rm -f /tmp/gost.tar.gz
fi

cat > /etc/systemd/system/llm-egress-proxy.service <<EOF
[Unit]
Description=LLM Gateway Egress Proxy (HTTP/SOCKS5 Auto)
After=network.target

[Service]
Type=simple
ExecStart=/usr/local/bin/gost -L auto://:${PROXY_PORT}
Restart=always
RestartSec=2
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable --now llm-egress-proxy.service

echo "==> [4/4] 配置 UFW 防火墙（${PROXY_PORT} 端口仅限主站 ${MAIN_GATEWAY_IP} 访问，防止外网盗刷）..."
apt-get update -qq && apt-get install -y -qq ufw >/dev/null
ufw --force reset >/dev/null
ufw default deny incoming
ufw default allow outgoing
ufw allow 22/tcp comment 'SSH Default'
ufw allow 22222/tcp comment 'SSH Alt Port'
ufw allow from "${MAIN_GATEWAY_IP}" to any port "${PROXY_PORT}" proto tcp comment 'Allow Main LLM Gateway Only'
ufw --force enable

echo "========================================================="
echo "[DONE] 出口代理节点初始化完成！"
echo "  - 代理服务状态: $(systemctl is-active llm-egress-proxy.service)"
echo "  - BBR 拥塞控制: $(sysctl -n net.ipv4.tcp_congestion_control)"
echo "  - New API 渠道代理填写:"
echo "      HTTP 代理:   http://$(curl -s -m 5 https://api.ipify.org || echo '<本机公网IP>'):${PROXY_PORT}"
echo "      SOCKS5 代理: socks5://$(curl -s -m 5 https://api.ipify.org || echo '<本机公网IP>'):${PROXY_PORT}"
echo "========================================================="
