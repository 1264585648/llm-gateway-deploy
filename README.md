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

## 四、 AWS Quick 多号池接入与 Web 批量上号器（定制版 `quickwork2api`）

本仓库已内置深度定制的 **`aws-quickwork-2api`** 服务（位于 `aws-quickwork-2api/`，在 `docker-compose.yml` 中作为 `quickwork2api` 容器运行），无需安装官方桌面客户端即可将一批普通 Google 账号转化为高可用 **Claude Opus 5.5 (`us.anthropic.claude-opus-5-5`)** 上游号池。

### 1. 针对中转站高并发与高缓存命中的深度定制特性
- **用户/IP/会话粘性亲和（Sticky Affinity）**：按 `req.User` → `X-Conversation-Id` / `X-Thread-Id` → 客户端真实 IP（`Cf-Connecting-Ip` / `X-Real-Ip` / `X-Forwarded-For`）→ 会话指纹进行 FNV-1a 一致性哈希，保证同一用户或同一多轮会话固定路由至同一 Google 账号，最大化 `cache_read` 缓存命中率（高达 98%）。
- **单号在途并发水位保护（Least-In-Flight Spillover）**：实时追踪每个 Google 账号的在途并发数（`inFlight`）。当某用户的粘性首选账号正在处理请求（`inFlight > 0`）时，新并发请求**自动溢出分配给当前 `inFlight == 0` 的空闲账号**，彻底杜绝并发请求堆死在单个账号上。
- **内置 Web 批量上号器与号池监控面板（`/quick-admin`）**：
  - 访问地址：`https://<你的域名>/quick-admin?key=<config.json中的api_key>`
  - 支持一次批量生成 1~10 个独立 Google OAuth 登录槽位，粘贴两步回调 URL 即可秒级入库并自动加入轮询池。
  - 实时大盘监控每个账号的 `In-Flight` 并发数、累计调用次数、Token 过期时间，并支持一键刷新 Token、启停或剔除账号。
- **对外标准化模型 ID 映射**：
  - `aws-quick-opus5-5` → 映射 `smart`（底层 `us.anthropic.claude-opus-5-5`，1M 上下文）
  - `aws-quick-sonnet5-5` → 映射 `balanced`
  - `aws-quick-haiku5-5` → 映射 `fast`
  - 同时保留 `aws-quick-smart` / `aws-quick-fast` / `aws-quick-balanced` / `aws-quick-auto`

### 2. 首次在新机器编译与启动 `quickwork2api`
```bash
cd /opt/llm-gateway/aws-quickwork-2api
cp config.json.example config.json
# 使用 Go 1.26 容器一键编译静态二进制
docker run --rm -u $(id -u):$(id -g) -e CGO_ENABLED=0 -v "$PWD":/src -w /src golang:1.26-alpine go build -o bin/quickproxy ./cmd/quickproxy
cd /opt/llm-gateway
docker compose up -d quickwork2api
```

---

## 五、 Meta Muse 多模态号池接入（文本 / 无水印原图 / 视频生成：`muse2api`）

本仓库已集成 **`czg86389-hub/muse2api`** 并附带一键拉取与双模增强脚本 [`scripts/setup_muse2api.sh`](scripts/setup_muse2api.sh)（在 `docker-compose.yml` 中作为 `muse2api` 容器运行），通过无头 Chromium (CDP `BrowserContext` 多账号独立沙箱隔离) + 云端 Hatch VM 保活，把 Meta Muse (`muse.ai`) 的免费周额度（约 1 亿 Token/周/号）转化为标准 OpenAI 兼容多模态 API：

### 1. 支持的模型与双模调用能力
- **文本 / 代码 / 多模态识图对话 (`muse-spark` / `meta-muse-spark`)**：
  - 支持 `/v1/chat/completions` 与 `/v1/responses`，并增强了 `engine.py`，支持客户端通过 OpenAI Vision (`image_url`) 上传参考图片进行识图问答。
- **文生图 & 多参考图编辑 (`muse-image` / `meta-muse-image` / `dall-e-3` / `gpt-image-1`)**：
  - **标准生图接口**：完整兼容 `/v1/images/generations`、`/v1/images/edits`（支持单图或多达 10 张参考图混合编辑）、`/v1/images/tasks` 异步轮询，自动打开图片查看器抓取无水印原图（实测 `1344x1792`）。
  - **对话流式直出模式**：在 Cherry Studio / NextChat / LobeChat 等普通聊天客户端直接选 `muse-image` 发文字或发图对话，内部自动调用生图引擎并带 SSE 心跳防断流，生成后以 Markdown 原图直接渲染！
- **文生视频 & 图生视频 (`muse-video` / `meta-muse-video` / `sora`)**：
  - **标准异步视频接口**：完整兼容 `/v1/videos`、`/v1/videos/generations`、`/v1/video/generations`（适配 New API 单数视频中继路由），支持 `5s / 6s / 8s / 10s`、`16:9 / 9:16` 及多图/视频/音频参考素材。
  - **对话流式直出模式**：在普通聊天客户端选 `muse-video` 发起对话，SSE 实时推送渲染进度与 `: keepalive` 保活，完成后返回缩略图封面与可点击播放/下载的 MP4 直链（`/v1/media/*.mp4`）。

### 2. 运维面板、Chrome 一键导号与跨区代理隔离
- **深色运维后台**：`https://<你的域名>/admin`（或 `/muse-admin` 自动跳转），支持查看实时账号池健康状态、各号本周剩余额度百分比、媒体库缩略图预览与在线接口调试。
- **Chrome / Edge 一键导号插件**：直接在 `/admin` 页面下载 `muse2api-extension.zip` 加载至浏览器，登录 `https://muse.ai/` 后点击扩展图标一键提取含 `HttpOnly` 的 `hatch_sess` Cookie 推送至服务器。
- **多号独立 IP 出口隔离**：每个 Muse 账号可在后台单独配置代理（如直连美西主节点 + 美东出口节点 `socks5://<美东IP>:18443`），彻底避免多号同 IP 关联风控。

### 3. 一键部署命令
```bash
bash /opt/llm-gateway/scripts/setup_muse2api.sh
# 在 .env 中填入 MUSE2API_KEY 与 MUSE2API_PUBLIC_BASE=https://<你的域名>
docker compose up -d --build muse2api
```

---

## 六、 赠金到期 / 封号：10 分钟平滑迁移 SOP

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
