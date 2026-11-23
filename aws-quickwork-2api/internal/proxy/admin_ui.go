package proxy

import (
	"encoding/json"
	"net/http"
	"strconv"
	"strings"
	"time"

	"quickwork-openai/internal/oauth"
)

func (s *Server) registerAdminRoutes(mux *http.ServeMux) {
	if s.webOAuth == nil {
		s.webOAuth = oauth.NewWebManager(s.accounts.Store(), s.cfg.AppVersion, s.cfg.OAuth.CallbackPort)
	}
	mux.HandleFunc("/quick-admin", s.handleAdminPage)
	mux.HandleFunc("/quick-admin/", s.handleAdminPage)
	mux.HandleFunc("/quick-admin/api/accounts", s.handleAdminAccounts)
	mux.HandleFunc("/quick-admin/api/login/start", s.handleAdminLoginStart)
	mux.HandleFunc("/quick-admin/api/login/callback", s.handleAdminLoginCallback)
	mux.HandleFunc("/quick-admin/api/accounts/refresh", s.handleAdminRefresh)
	mux.HandleFunc("/quick-admin/api/accounts/toggle", s.handleAdminToggle)
	mux.HandleFunc("/quick-admin/api/accounts/delete", s.handleAdminDelete)
}

func (s *Server) checkAdminKey(w http.ResponseWriter, r *http.Request) bool {
	if len(s.cfg.APIKeys) == 0 {
		return true
	}
	got := strings.TrimSpace(r.Header.Get("X-Admin-Key"))
	if got == "" {
		got = strings.TrimSpace(r.URL.Query().Get("key"))
	}
	for _, k := range s.cfg.APIKeys {
		if k != "" && got == k {
			return true
		}
	}
	writeJSON(w, http.StatusUnauthorized, map[string]any{"error": "管理口令错误或未填写"})
	return false
}

func (s *Server) handleAdminAccounts(w http.ResponseWriter, r *http.Request) {
	if !s.checkAdminKey(w, r) {
		return
	}
	list, err := s.accounts.Store().List(r.Context())
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	type acctView struct {
		ID         int64  `json:"id"`
		Email      string `json:"email"`
		Provider   string `json:"provider"`
		Region     string `json:"region"`
		ExpiresAt  int64  `json:"expires_at"`
		UseCount   int64  `json:"use_count"`
		InFlight   int64  `json:"in_flight"`
		Disabled   bool   `json:"disabled"`
		LastError  string `json:"last_error"`
		StatusText string `json:"status_text"`
	}
	now := time.Now().Unix()
	out := make([]acctView, 0, len(list))
	for _, a := range list {
		st := "ready"
		if a.Disabled {
			st = "disabled"
		} else if a.ExpiresAt > 0 && a.ExpiresAt <= now {
			st = "expired"
		}
		email := a.Email
		if email == "" {
			email = a.DisplayName
		}
		out = append(out, acctView{
			ID:         a.ID,
			Email:      email,
			Provider:   a.Provider,
			Region:     a.Region,
			ExpiresAt:  a.ExpiresAt,
			UseCount:   a.UseCount,
			InFlight:   s.accounts.InFlight(a.ID),
			Disabled:   a.Disabled,
			LastError:  a.LastError,
			StatusText: st,
		})
	}
	writeJSON(w, http.StatusOK, map[string]any{"accounts": out})
}

func (s *Server) handleAdminLoginStart(w http.ResponseWriter, r *http.Request) {
	if !s.checkAdminKey(w, r) {
		return
	}
	var req struct {
		Count    int    `json:"count"`
		Provider string `json:"provider"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if req.Count <= 0 {
		req.Count = 1
	}
	if req.Count > 20 {
		req.Count = 20
	}
	if req.Provider == "" {
		req.Provider = s.cfg.OAuth.Provider
	}
	sessions := make([]*oauth.WebSession, 0, req.Count)
	for i := 0; i < req.Count; i++ {
		sess, err := s.webOAuth.StartSession(req.Provider, s.cfg.OAuth.BuildStage, s.cfg.OAuth.Region)
		if err != nil {
			writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
			return
		}
		sessions = append(sessions, sess)
	}
	writeJSON(w, http.StatusOK, map[string]any{"sessions": sessions})
}

func (s *Server) handleAdminLoginCallback(w http.ResponseWriter, r *http.Request) {
	if !s.checkAdminKey(w, r) {
		return
	}
	var req struct {
		SessionID   string `json:"session_id"`
		CallbackURL string `json:"callback_url"`
	}
	if err := json.NewDecoder(r.Body).Decode(&req); err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error()})
		return
	}
	sess, msg, err := s.webOAuth.SubmitCallback(r.Context(), req.SessionID, req.CallbackURL)
	if err != nil {
		writeJSON(w, http.StatusBadRequest, map[string]any{"error": err.Error(), "session": sess})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"message": msg, "session": sess})
}

func (s *Server) handleAdminRefresh(w http.ResponseWriter, r *http.Request) {
	if !s.checkAdminKey(w, r) {
		return
	}
	idStr := r.URL.Query().Get("id")
	if idStr != "" {
		id, _ := strconv.ParseInt(idStr, 10, 64)
		a, err := s.accounts.RefreshOne(r.Context(), id)
		if err != nil {
			writeJSON(w, http.StatusBadGateway, map[string]any{"error": err.Error()})
			return
		}
		writeJSON(w, http.StatusOK, map[string]any{"message": "已刷新 " + a.Email})
		return
	}
	res, err := s.accounts.RefreshAll(r.Context())
	if err != nil {
		writeJSON(w, http.StatusBadGateway, map[string]any{"error": err.Error()})
		return
	}
	okCount := 0
	for _, item := range res {
		if item.Err == nil {
			okCount++
		}
	}
	writeJSON(w, http.StatusOK, map[string]any{"message": "已成功刷新 " + strconv.Itoa(okCount) + "/" + strconv.Itoa(len(res)) + " 个账号"})
}

func (s *Server) handleAdminToggle(w http.ResponseWriter, r *http.Request) {
	if !s.checkAdminKey(w, r) {
		return
	}
	var req struct {
		ID       int64 `json:"id"`
		Disabled bool  `json:"disabled"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	if err := s.accounts.Store().SetDisabled(r.Context(), req.ID, req.Disabled); err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleAdminDelete(w http.ResponseWriter, r *http.Request) {
	if !s.checkAdminKey(w, r) {
		return
	}
	var req struct {
		ID int64 `json:"id"`
	}
	_ = json.NewDecoder(r.Body).Decode(&req)
	_, err := s.accounts.Store().Delete(r.Context(), req.ID)
	if err != nil {
		writeJSON(w, http.StatusInternalServerError, map[string]any{"error": err.Error()})
		return
	}
	writeJSON(w, http.StatusOK, map[string]any{"ok": true})
}

func (s *Server) handleAdminPage(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = w.Write([]byte(adminHTMLPage))
}

const adminHTMLPage = `<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>AWS Quick 批量上号器 & 号池监控</title>
<style>
  :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc; --muted: #94a3b8; --primary: #3b82f6; --success: #22c55e; --danger: #ef4444; --warn: #f59e0b; --purple: #8b5cf6; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }
  .container { max-width: 1180px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
  .title { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 10px; }
  .badge { font-size: 12px; padding: 4px 10px; border-radius: 999px; background: rgba(59,130,246,0.2); color: #60a5fa; font-weight: 600; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  input, select { background: #0b1120; border: 1px solid var(--border); color: var(--text); padding: 9px 12px; border-radius: 8px; font-size: 14px; outline: none; }
  input:focus { border-color: var(--primary); }
  button { background: var(--primary); color: #fff; border: none; padding: 9px 16px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; transition: opacity 0.15s; }
  button:hover { opacity: 0.9; }
  button.secondary { background: #334155; }
  button.success { background: var(--success); }
  button.purple { background: var(--purple); }
  button.danger { background: var(--danger); padding: 6px 10px; font-size: 12px; }
  button.sm { padding: 6px 10px; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 14px; }
  th, td { text-align: left; padding: 12px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 13px; }
  .status-pill { padding: 3px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .status-ready { background: rgba(34,197,94,0.18); color: #4ade80; }
  .status-expired { background: rgba(245,158,11,0.18); color: #fbbf24; }
  .status-disabled { background: rgba(239,68,68,0.18); color: #f87171; }
  .task-item { background: #0b1120; border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-top: 12px; }
  .task-done { border-color: var(--success); background: rgba(34,197,94,0.06); }
  .tip { color: var(--muted); font-size: 13px; line-height: 1.6; }
  .auto-grid { display: grid; grid-template-columns: 1fr 260px; gap: 16px; align-items: center; }
  @media (max-width: 768px) { .auto-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title">
      ⚡ AWS Quick 极速全自动别名上号器 & 号池管理
      <span class="badge" id="poolCount">加载中...</span>
    </div>
    <div class="row">
      <input type="password" id="adminKey" placeholder="输入管理口令 (Admin Key)" style="width:240px">
      <button onclick="saveKeyAndLoad()">验证 / 刷新</button>
    </div>
  </div>

  <!-- 全自动：只需输入主 Gmail + 6位验证码 -->
  <div class="card" style="border-color:#8b5cf6">
    <h3 style="margin:0 0 6px 0;color:#c4b5fd">🤖 极简全自动模式：只需填基础 Gmail + 输入 6 位邮箱验证码（免开网页 / 免粘回调 URL）</h3>
    <div class="tip" style="margin-bottom:14px">
      服务器后台内置 <b>Playwright 无头浏览器引擎</b>：点击「一键发验证码」后，服务器会自动帮你生成 Gmail 别名（如 <code>+q01</code>）、自动打开 AWS 填表并触发验证码邮件；你只需把主 Gmail 收到的 <b>6 位数字验证码</b> 填进来点提交，服务器会自动完成设置密码、开通工作区、拦截两步 OAuth 回调并直接写入号池！
    </div>

    <div class="row" style="background:#0b1120;padding:14px;border-radius:10px;border:1px solid var(--border)">
      <label style="font-size:13px;color:var(--muted)">基础 Gmail 邮箱:</label>
      <input type="text" id="baseEmail" placeholder="例如 rahafsunm7md7@gmail.com" style="width:220px" value="rahafsunm7md7@gmail.com">
      <label style="font-size:13px;color:var(--muted)">别名规则:</label>
      <select id="aliasMode" onchange="updatePreviewAlias()">
        <option value="dot_rand" selected>随机点号变体 (每次随机分配新地址)</option>
        <option value="dot">顺序点号变体 (按序号)</option>
        <option value="plus">+号后缀 (user+q01@gmail.com)</option>
        <option value="raw">原样使用输入框邮箱</option>
      </select>
      <label style="font-size:13px;color:var(--muted)">序号:</label>
      <input type="number" id="aliasStartIdx" value="1" min="1" style="width:64px" onchange="updatePreviewAlias()">
      <label style="font-size:13px;color:var(--muted)">代理池(逗号分隔,每次随机挑1个):</label>
      <input type="text" id="proxyUrl" placeholder="如 http://172.18.0.1:17890, http://135.148.234.185:18443" style="width:270px">
      <span style="font-size:13px;color:#a78bfa;font-family:monospace" id="previewAlias"></span>
      <button class="purple" onclick="startAutoReg()" style="margin-left:auto">🚀 一键裂变别名 & 发送验证码</button>
    </div>

    <div id="autoJobList"></div>
  </div>

  <!-- 号池列表 -->
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <h3 style="margin:0">📦 当前 Google / 别名账号池（支持 Sticky 粘性 + Least-In-Flight 并发防爆）</h3>
      <div class="row">
        <button class="secondary sm" onclick="refreshAllTokens()">🔄 一键刷新全部 Token</button>
        <button class="secondary sm" onclick="loadAccounts()">刷新列表</button>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>账号邮箱 (含别名)</th>
          <th>区域</th>
          <th>状态</th>
          <th>实时并发 (In-Flight)</th>
          <th>累计调用</th>
          <th>Token 到期时间</th>
          <th>最近错误</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody id="accountTable">
        <tr><td colspan="9" style="text-align:center;color:var(--muted)">请输入右上角管理口令加载数据</td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.get('key')) {
  localStorage.setItem('quick_admin_key', urlParams.get('key'));
}
document.getElementById('adminKey').value = localStorage.getItem('quick_admin_key') || '';
if (localStorage.getItem('quick_base_email')) {
  document.getElementById('baseEmail').value = localStorage.getItem('quick_base_email');
}
if (localStorage.getItem('quick_alias_idx')) {
  document.getElementById('aliasStartIdx').value = localStorage.getItem('quick_alias_idx');
}
if (localStorage.getItem('quick_proxy_url')) {
  document.getElementById('proxyUrl').value = localStorage.getItem('quick_proxy_url');
}

function getKey() {
  return document.getElementById('adminKey').value.trim();
}

function saveKeyAndLoad() {
  localStorage.setItem('quick_admin_key', getKey());
  loadAccounts();
}

function generateAlias(baseEmail, mode, index) {
  baseEmail = (baseEmail || '').trim();
  if (!baseEmail.includes('@') || mode === 'raw') return baseEmail;
  const parts = baseEmail.split('@');
  const local = parts[0].split('+')[0];
  const domain = parts[1];
  if ((mode === 'dot' || mode === 'dot_rand') && local.length > 2) {
    const clean = local.replace(/\./g, '');
    const mask = mode === 'dot_rand'
      ? Math.max(1, Math.floor(Math.random() * ((1 << Math.min(clean.length - 1, 16)) - 1)) + 1)
      : index;
    let out = '';
    for (let i = 0; i < clean.length; i++) {
      out += clean[i];
      if (i < clean.length - 1 && ((mask >> i) & 1)) {
        out += '.';
      }
    }
    if (out === clean) out = clean[0] + '.' + clean.slice(1);
    return out + '@' + domain;
  }
  const num = String(index).padStart(2, '0');
  return local + '+q' + num + '@' + domain;
}

function updatePreviewAlias() {
  const base = document.getElementById('baseEmail').value;
  const mode = document.getElementById('aliasMode').value;
  const idx = parseInt(document.getElementById('aliasStartIdx').value, 10) || 1;
  document.getElementById('previewAlias').textContent = '👉 下一个将注册: ' + generateAlias(base, mode, idx);
}
document.getElementById('baseEmail').addEventListener('input', updatePreviewAlias);
updatePreviewAlias();

async function api(path, opts = {}) {
  const headers = Object.assign({'Content-Type': 'application/json', 'X-Admin-Key': getKey()}, opts.headers || {});
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || data.detail || ('HTTP ' + res.status));
  return data;
}

const activePolls = {};

async function startAutoReg() {
  const baseEmail = document.getElementById('baseEmail').value.trim();
  const mode = document.getElementById('aliasMode').value;
  let idx = parseInt(document.getElementById('aliasStartIdx').value, 10) || 1;
  const targetEmail = generateAlias(baseEmail, mode, idx);
  if (!targetEmail) return alert('请输入合法的基础 Gmail 邮箱');

  localStorage.setItem('quick_base_email', baseEmail);
  if (mode !== 'raw') {
    idx++;
    document.getElementById('aliasStartIdx').value = idx;
    localStorage.setItem('quick_alias_idx', idx);
    updatePreviewAlias();
  }

  const proxyUrl = (document.getElementById('proxyUrl').value || '').trim();
  localStorage.setItem('quick_proxy_url', proxyUrl);

  try {
    const data = await api('/quick-autoreg/start', {
      method: 'POST',
      body: JSON.stringify({
        email: targetEmail,
        proxy: proxyUrl,
        admin_key: getKey()
      })
    });
    const container = document.getElementById('autoJobList');
    const div = document.createElement('div');
    div.className = 'task-item';
    div.id = 'job-' + data.job_id;
    container.prepend(div);
    renderJobCard(data);
    pollJob(data.job_id);
  } catch (e) {
    alert('启动失败: ' + e.message);
  }
}

function pollJob(jobId) {
  if (activePolls[jobId]) clearInterval(activePolls[jobId]);
  activePolls[jobId] = setInterval(async () => {
    try {
      const data = await api('/quick-autoreg/status/' + jobId);
      renderJobCard(data);
      if (data.status === 'done' || data.status === 'error') {
        clearInterval(activePolls[jobId]);
        delete activePolls[jobId];
        if (data.status === 'done') loadAccounts();
      }
    } catch (e) {
      clearInterval(activePolls[jobId]);
    }
  }, 1800);
}

function renderJobCard(j) {
  const box = document.getElementById('job-' + j.job_id);
  if (!box) return;
  if (j.status === 'done') box.className = 'task-item task-done';

  // If user is currently typing code, don't overwrite the input element!
  const existingCodeInput = document.getElementById('code-' + j.job_id);
  if (existingCodeInput && j.status === 'waiting_code') {
    const msgEl = document.getElementById('jobmsg-' + j.job_id);
    if (msgEl) msgEl.textContent = j.message;
    const imgEl = document.getElementById('jobimg-' + j.job_id);
    if (imgEl && j.screenshot) imgEl.src = j.screenshot;
    return;
  }

  let actionArea = '';
  if (j.status === 'waiting_code') {
    actionArea = '<div class="row" style="margin-top:12px">' +
      '<input type="text" id="code-' + j.job_id + '" maxlength="8" placeholder="输入 Gmail 收到的 6 位数字验证码" style="width:260px;font-size:16px;font-weight:700;letter-spacing:2px;border-color:#22c55e" onkeydown="if(event.key===\'Enter\')submitVerifyCode(\'' + j.job_id + '\')">' +
      '<button class="success" onclick="submitVerifyCode(\'' + j.job_id + '\')">✅ 提交验证码并全自动入库</button>' +
    '</div>';
  }

  const statusColor = j.status === 'done' ? '#4ade80' : (j.status === 'error' ? '#f87171' : (j.status === 'waiting_code' ? '#fbbf24' : '#60a5fa'));
  const imgHTML = j.screenshot ? '<img id="jobimg-' + j.job_id + '" src="' + j.screenshot + '" style="width:100%;border-radius:8px;border:1px solid var(--border)" title="无头浏览器实时画面">' : '';

  box.innerHTML = '<div class="auto-grid">' +
    '<div>' +
      '<div class="row" style="margin-bottom:6px">' +
        '<span class="badge" style="background:rgba(139,92,246,0.2);color:#c4b5fd">📧 ' + j.email + '</span>' +
        '<span style="font-size:12px;color:var(--muted);font-family:monospace">自动密码: ' + j.password + '</span>' +
        (j.proxy ? '<span class="badge" style="background:rgba(34,197,94,0.16);color:#4ade80">🌐 出口: ' + j.proxy + '</span>' : '') +
      '</div>' +
      '<div id="jobmsg-' + j.job_id + '" style="font-weight:600;color:' + statusColor + ';font-size:14px">' + j.message + '</div>' +
      actionArea +
    '</div>' +
    '<div>' + imgHTML + '</div>' +
  '</div>';

  if (j.status === 'waiting_code') {
    const inp = document.getElementById('code-' + j.job_id);
    if (inp) inp.focus();
  }
}

async function submitVerifyCode(jobId) {
  const inp = document.getElementById('code-' + jobId);
  if (!inp) return;
  const code = inp.value.trim();
  if (!code) return alert('请输入 6 位验证码');
  inp.disabled = true;
  try {
    const data = await api('/quick-autoreg/verify', {
      method: 'POST',
      body: JSON.stringify({
        job_id: jobId,
        code: code,
        admin_key: getKey()
      })
    });
    const msgEl = document.getElementById('jobmsg-' + jobId);
    if (msgEl) msgEl.textContent = data.message;
    pollJob(jobId);
  } catch (e) {
    inp.disabled = false;
    alert('提交验证码失败: ' + e.message);
  }
}

async function loadAccounts() {
  try {
    const data = await api('/quick-admin/api/accounts');
    const list = data.accounts || [];
    const readyCount = list.filter(a => a.status_text === 'ready').length;
    document.getElementById('poolCount').textContent = '可用 ' + readyCount + ' / 共 ' + list.length + ' 号';
    const tbody = document.getElementById('accountTable');
    if (list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted)">暂无账号，请在上方一键裂变上号</td></tr>';
      return;
    }
    tbody.innerHTML = list.map(a => {
      const exp = a.expires_at ? new Date(a.expires_at * 1000).toLocaleString() : '-';
      const pillClass = a.status_text === 'ready' ? 'status-ready' : (a.status_text === 'disabled' ? 'status-disabled' : 'status-expired');
      return '<tr>' +
        '<td>#' + a.id + '</td>' +
        '<td><b>' + (a.email || '-') + '</b></td>' +
        '<td>' + (a.region || 'us-east-1') + '</td>' +
        '<td><span class="status-pill ' + pillClass + '">' + a.status_text + '</span></td>' +
        '<td><b style="color:' + (a.in_flight > 0 ? '#fbbf24' : '#4ade80') + '">' + a.in_flight + '</b></td>' +
        '<td>' + a.use_count + '</td>' +
        '<td>' + exp + '</td>' +
        '<td style="color:#f87171;max-width:220px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + (a.last_error || '') + '">' + (a.last_error || '-') + '</td>' +
        '<td class="row">' +
          '<button class="secondary sm" onclick="refreshOne(' + a.id + ')">刷新</button>' +
          '<button class="secondary sm" onclick="toggleAcct(' + a.id + ',' + (!a.disabled) + ')">' + (a.disabled ? '启用' : '禁用') + '</button>' +
          '<button class="danger" onclick="deleteAcct(' + a.id + ')">删除</button>' +
        '</td>' +
      '</tr>';
    }).join('');
  } catch (e) {
    document.getElementById('accountTable').innerHTML = '<tr><td colspan="9" style="text-align:center;color:#f87171">' + e.message + '</td></tr>';
  }
}

async function refreshAllTokens() {
  try {
    const data = await api('/quick-admin/api/accounts/refresh', {method: 'POST'});
    alert(data.message);
    loadAccounts();
  } catch (e) {
    alert('刷新失败: ' + e.message);
  }
}

async function refreshOne(id) {
  try {
    await api('/quick-admin/api/accounts/refresh?id=' + id, {method: 'POST'});
    loadAccounts();
  } catch (e) {
    alert('刷新失败: ' + e.message);
  }
}

async function toggleAcct(id, disabled) {
  await api('/quick-admin/api/accounts/toggle', {method: 'POST', body: JSON.stringify({id, disabled})});
  loadAccounts();
}

async function deleteAcct(id) {
  if (!confirm('确定要从号池删除账号 #' + id + ' 吗？')) return;
  await api('/quick-admin/api/accounts/delete', {method: 'POST', body: JSON.stringify({id})});
  loadAccounts();
}

if (getKey()) loadAccounts();
setInterval(() => { if (getKey()) loadAccounts(); }, 8000);
</script>
</body>
</html>
`
