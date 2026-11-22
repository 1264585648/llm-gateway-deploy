# AI API 中转站部署工程（OVH 赠金起步 / 云无关极速迁移版）

本仓库为 **New API + MySQL 8 + Redis 7 + Nginx + Uptime Kuma + AWS Quick (`quickwork2api`) 无头多号池 + 跨机房多 IP 出口代理 (`Gost v3`)** 的生产级标准化部署方案，专门针对**“前期用 OVH / GCP / Azure / AWS 等各路赠金号接力运营”**的场景设计，核心理念：**配置全代码化 + 账本定时加密异地备 + 10 分钟无痛跨云迁移**。

---

## 核心文档与脚本导航（下次复刻先看这里）

- **[实施步骤与踩坑复刻指南.md](./实施步骤与踩坑复刻指南.md)**：**【强烈推荐首读】** 完整复盘从 0 到 1 上线全流程（7 大阶段 SOP）以及实战中踩过的 **17 个核心暗坑与一键修复命令**（含 Ubuntu 24.04 `ssh.socket` 22222 端口改造、Certbot 免停机续签、AWS Quick 无头容器化、跨 A/B/C 段多 IP `gost` 出口防盗刷等）。
- **[OVH-US-赠金与可开通资源指南.md](./OVH-US-赠金与可开通资源指南.md)**：OVHcloud 美区 `$200` 赠金适用范围、美东/美西配额对比、全机型价格测算与防反扣信用卡清单。
- **核心自动化脚本与模板**：
  - `scripts/init_server.sh`：主站新机一键初始化（2GB Swap、TCP BBR、`ulimit 65535`、Docker、UFW `22/22222/80/443`、Fail2ban）。
  - `scripts/deploy_quickwork2api.sh`：一键使用 `golang:1.26-alpine` 容器免环境编译 `aws-quickwork-2api`，并自动向 New API 注入 `AWS-Quick-Pool` 渠道。
  - `scripts/init_egress_node.sh`：跨机房轻量出口节点一键初始化（开启 `22222` 端口、BBR、`gost v3` 自适应 HTTP/SOCKS5 代理、UFW 锁死仅主站 IP 可访问）。
  - `scripts/backup.sh` & `scripts/restore.sh`：MySQL 在线热备 + `quickwork.db` 号池备份 + OpenSSL AES-256 加密 + Rclone R2 同步与一键跨云还原。
  - `nginx/conf.d/gateway-unified.conf.template`：单域名（同时承载控制台与 `/v1/*` SSE 流式接口）生产级 Nginx 配置模板。

---

## 一、 OVH 赠金号开鸡与避坑指南

### 1. 机房地域选择优先级
1. **首选：新加坡（`SGP1` / `AP-SOUTH-SGP`）**
   - 离国内最近，且 IP 极纯净，直连 OpenAI / Anthropic / Gemini / AWS Bedrock 等海外上游基本零风控。
   - **必检项**：在 OVH Public Cloud 控制台建机前，确认一下亚太区出站流量（Egress）在你的赠金计划下的单价（AI 文本 API 流量极小，百万 Token 仅数 MB；但若大量走多模态图片/Base64 需关注流量账单）。
2. **次选：美国西海岸（`US-WEST-OR` Hillsboro）**
   - 欧美区出站流量成本极低，且离 OpenAI / Anthropic 服务器物理距离极近（上游延迟 < 30ms），适合重度多模态或欧美用户。
3. **规格推荐**：
   - 选 **Ubuntu 24.04 LTS** 或 **Debian 12**。
   - 算力选 **2C4G（如 `d2-4`）** 或 **4C8G（如 `b3-8` / `b2-7`）** 足够起步。
   - **务必在 OVH 控制台开启 Billing Alert（预算/赠金消耗告警）**，防止赠金用超自动扣绑定的卡！

### 2. 开机后第一步：网络与上游连通性摸底
SSH 登录 OVH 服务器后，先跑下面三条命令验收网络质量：

```bash
# 测试到 OpenAI / Anthropic / Google Gemini 的 TCP+TLS 建连速度与连通性
for url in https://api.openai.com https://api.anthropic.com https://generativelanguage.googleapis.com; do
  echo "=== $url ==="
  curl -o /dev/null -s -w "DNS:%{time_namelookup}s  TCP:%{time_connect}s  TLS:%{time_appconnect}s  HTTP:%{http_code}\n" "$url"
done
```
- **理想标准**：`TLS` 耗时 `< 0.35s`，且不返回 `403`（OpenAI/Anthropic 根路径返回 `404` 或 `421` 属于正常连通，没有被地区封锁）。

### 3. 如果国内晚高峰直连 OVH 卡顿怎么办？
不要把数据库和核心服务搬走！采用 **“边缘接入 + OVH 云源站”** 轻量解法：
- 后期花 ¥15~30/月买一台香港/日本三网优化小鸡（或利用国内云厂商赠金机），仅跑一层 Nginx 反代（关闭缓冲）转发到 OVH，把 `api.yourdomain.com` 解析到优化小鸡即可；OVH 继续免费用赠金扛算力、数据库、Anti-DDoS 和纯净海外出口。

---

## 二、 首次部署步骤（15 分钟上线）

### Step 1. 准备域名 DNS（推荐托管在 Cloudflare）
添加三条 A 记录指向你的 OVH 公网 IP，**将 TTL 设为 60s~120s**（方便以后换赠金机秒切）：
- `api.yourdomain.com` → OVH IP（**灰色云朵 DNS Only，切勿开免费版 Proxy 防 100s 超时斩断思考模型**）
- `panel.yourdomain.com` → OVH IP（可开橙色云朵 WAF，证书签发期间先关）
- `status.yourdomain.com` → OVH IP（可开橙色云朵）

### Step 2. 上传本项目并初始化服务器
```bash
# 在 OVH 服务器上创建部署目录
sudo mkdir -p /opt/llm-gateway && sudo chown $USER:$USER /opt/llm-gateway
# 将本目录文件上传（scp / rsync / git clone）到 /opt/llm-gateway 后执行：
cd /opt/llm-gateway
# 若在 Windows 编辑过脚本，先统一转为 LF 换行符防止执行报错
sed -i 's/\r$//' scripts/*.sh
sudo bash scripts/init_server.sh
```

### Step 3. 生成随机密钥并配置 `.env`
```bash
cp .env.example .env
chmod 600 .env

# 生成两把 64 位强随机密钥，分别填入 .env 的 SESSION_SECRET 与 CRYPTO_SECRET
openssl rand -hex 32
openssl rand -hex 32
```
填完 `.env` 后，**立刻在本地电脑安全位置（如密码管理器）备份一份 `.env`！**

### Step 4. 启动核心容器
```bash
docker compose up -d
# 查看所有容器是否进入 healthy 状态
docker compose ps
```

### Step 5. 配置 Nginx 反代与自动 HTTPS 证书
```bash
sudo cp nginx/conf.d/00-gateway-common.conf /etc/nginx/conf.d/

# 复制模板并替换为你的真实域名（把 yourdomain.com 换成你的域名）
DOMAIN="yourdomain.com"
for name in api panel status; do
  sed "s/yourdomain.com/${DOMAIN}/g" "nginx/conf.d/${name}.conf.template" | sudo tee "/etc/nginx/conf.d/${name}.conf" > /dev/null
done

# 首次签发证书（若 Nginx 还没证书会报错，先用 certbot --nginx 或 standalone 签发）
sudo systemctl stop nginx
sudo certbot certonly --standalone -d "api.${DOMAIN}" -d "panel.${DOMAIN}" -d "status.${DOMAIN}"
sudo nginx -t && sudo systemctl start nginx
```

### Step 6. 初始化 New API 后台与分组隔离
1. 浏览器访问 `https://panel.yourdomain.com`，默认管理员账号 `root`，密码 `123456`（**登录第一件事：立刻修改 root 密码！**）。
2. 在后台按三层流量建好分组（Group）：
   - `free_fans`：绑定你自己的闲置额度/赠金渠道，强限流，用兑换码发给粉丝/朋友。
   - `default`：绑定性价比低价批发上游（多渠道权重轮询 + 自动容灾），按成本 × 1.3~1.8 设定倍率。
   - `vip_official`：绑定官方/云厂商（Bedrock/Vertex/Azure）高稳渠道，卖稳定性与服务溢价。
3. 访问 `https://status.yourdomain.com` 初始化 Uptime Kuma，配置各上游模型探活并开启公开状态页。

---

## 三、 配置免费异地灾备（Cloudflare R2 + Rclone）

**赠金机随时可能到期或触发风控，异地备份是底线！**
1. 在 Cloudflare 开通 **R2**（每月免费 10GB 存储 + 0 出口流量费），创建一个私有 Bucket（如 `llm-gateway-backups`）并生成 API Token。
2. 在服务器上执行 `rclone config`，新建名为 `r2` 的 S3 类型配置（Provider 选 `Cloudflare`）。
3. 手动测试一次备份：
   ```bash
   bash /opt/llm-gateway/scripts/backup.sh
   ```
4. 写入定时任务（每 6 小时备份一次）：
   ```bash
   (crontab -l 2>/dev/null; echo "0 */6 * * * /opt/llm-gateway/scripts/backup.sh >> /var/log/llm-backup.log 2>&1") | crontab -
   ```

---

## 四、 赠金到期 / 封号：10 分钟平滑迁移 SOP

当 OVH 赠金快用完、准备切到下一家（GCP / Azure / AWS / DigitalOcean / 付费 VPS）时：

1. **新机初始化**：新机执行 `sudo bash scripts/init_server.sh`。
2. **还原工程与密钥**：把代码目录 + 本地保管的 `.env` 放到新机 `/opt/llm-gateway/`。
3. **拉起空库**：先执行 `docker compose up -d mysql redis`，等待健康检查通过。
4. **拉取最新 R2 账本备份并还原**：
   ```bash
   rclone copy r2:llm-gateway-backups/db/ ./backups/
   bash scripts/restore.sh ./backups/<最新的备份文件名>
   ```
5. **配置新机 Nginx & 证书**：执行 Step 5 签发证书并验证 `curl https://api.yourdomain.com/api/status` 正常。
6. **切流量**：在 Cloudflare 把 `api` / `panel` / `status` 的 A 记录改指向新机 IP（TTL 60s 下约 1 分钟全球生效）。
7. 观察 24 小时无旧机请求后，在 OVH 销毁旧实例，**零丢单、零余额丢失完成平滑交接**。
