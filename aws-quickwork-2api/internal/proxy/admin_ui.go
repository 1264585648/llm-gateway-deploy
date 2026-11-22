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
  :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc; --muted: #94a3b8; --primary: #3b82f6; --success: #22c55e; --danger: #ef4444; --warn: #f59e0b; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }
  .container { max-width: 1120px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; flex-wrap: wrap; gap: 12px; }
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
  button.danger { background: var(--danger); padding: 6px 10px; font-size: 12px; }
  button.sm { padding: 6px 10px; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 14px; }
  th, td { text-align: left; padding: 12px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 13px; }
  .status-pill { padding: 3px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .status-ready { background: rgba(34,197,94,0.18); color: #4ade80; }
  .status-expired { background: rgba(245,158,11,0.18); color: #fbbf24; }
  .status-disabled { background: rgba(239,68,68,0.18); color: #f87171; }
  .task-item { background: #0b1120; border: 1px solid var(--border); border-radius: 10px; padding: 14px; margin-top: 12px; }
  .task-done { border-color: var(--success); background: rgba(34,197,94,0.06); }
  .url-box { font-family: monospace; font-size: 12px; background: #1e293b; padding: 8px 10px; border-radius: 6px; word-break: break-all; margin: 8px 0; color: #93c5fd; }
  .msg { font-size: 13px; margin-top: 6px; }
  .tip { color: var(--muted); font-size: 13px; line-height: 1.6; }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title">
      ⚡ AWS Quick 批量上号器 & 号池管理
      <span class="badge" id="poolCount">加载中...</span>
    </div>
    <div class="row">
      <input type="password" id="adminKey" placeholder="输入管理口令 (Admin Key)" style="width:240px">
      <button onclick="saveKeyAndLoad()">验证 / 刷新</button>
    </div>
  </div>

  <!-- 批量上号工作台 -->
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <div>
        <h3 style="margin:0 0 4px 0">🚀 批量生成 Google 上号链接</h3>
        <div class="tip">支持一次生成多个独立登录槽位。点击「打开链接」在无痕窗口登录 Google，把跳转后的 <code>localhost:18080/...</code> 地址粘贴到对应输入框即可自动推进！（共 2 步回调）</div>
      </div>
      <div class="row">
        <select id="batchCount">
          <option value="1">生成 1 个槽位</option>
          <option value="3" selected>批量生成 3 个槽位</option>
          <option value="5">批量生成 5 个槽位</option>
          <option value="10">批量生成 10 个槽位</option>
        </select>
        <button class="success" onclick="createLoginTasks()">+ 批量生成上号链接</button>
      </div>
    </div>
    <div id="taskList"></div>
  </div>

  <!-- 号池列表 -->
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <h3 style="margin:0">📦 当前 Google 账号池（支持 Sticky 粘性 + Least-In-Flight 并发防爆）</h3>
      <div class="row">
        <button class="secondary sm" onclick="refreshAllTokens()">🔄 一键刷新全部 Token</button>
        <button class="secondary sm" onclick="loadAccounts()">刷新列表</button>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th>ID</th>
          <th>Google 邮箱</th>
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

function getKey() {
  return document.getElementById('adminKey').value.trim();
}

function saveKeyAndLoad() {
  localStorage.setItem('quick_admin_key', getKey());
  loadAccounts();
}

async function api(path, opts = {}) {
  const headers = Object.assign({'Content-Type': 'application/json', 'X-Admin-Key': getKey()}, opts.headers || {});
  const res = await fetch(path, Object.assign({}, opts, {headers}));
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
  return data;
}

async function loadAccounts() {
  try {
    const data = await api('/quick-admin/api/accounts');
    const list = data.accounts || [];
    const readyCount = list.filter(a => a.status_text === 'ready').length;
    document.getElementById('poolCount').textContent = '可用 ' + readyCount + ' / 共 ' + list.length + ' 号';
    const tbody = document.getElementById('accountTable');
    if (list.length === 0) {
      tbody.innerHTML = '<tr><td colspan="9" style="text-align:center;color:var(--muted)">暂无账号，请在上方生成链接上号</td></tr>';
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

async function createLoginTasks() {
  const count = parseInt(document.getElementById('batchCount').value, 10) || 1;
  try {
    const data = await api('/quick-admin/api/login/start', {
      method: 'POST',
      body: JSON.stringify({count})
    });
    const container = document.getElementById('taskList');
    (data.sessions || []).forEach((s, idx) => {
      const div = document.createElement('div');
      div.className = 'task-item';
      div.id = 'sess-' + s.id;
      div.innerHTML = renderTaskHTML(s, idx + 1);
      container.prepend(div);
    });
  } catch (e) {
    alert('创建失败: ' + e.message);
  }
}

function renderTaskHTML(s, labelIdx) {
  if (s.step === 3) {
    return '<div style="color:#4ade80;font-weight:700">✅ 账号 #' + s.account_id + ' (' + s.account_email + ') 已成功入库！</div>';
  }
  const currentURL = s.step === 1 ? s.discovery_url : s.authorize_url;
  const stepTitle = s.step === 1
    ? '第一步：点击打开或复制链接到【无痕窗口】登录 Google，然后粘贴跳转后的 localhost:18080/callback?nonce=... 地址'
    : '第二步（最后一步）：点击打开下方【最终授权链接】点 Allow，然后粘贴跳转后的 127.0.0.1:18080/?code=... 地址';
  const stepColor = s.step === 1 ? '#60a5fa' : '#fbbf24';

  return '<div style="display:flex;justify-content:space-between;align-items:center">' +
      '<b style="color:' + stepColor + '">' + stepTitle + '</b>' +
      '<div class="row">' +
        '<button class="sm" onclick="copyText(\'' + currentURL + '\')">📋 复制链接</button>' +
        '<a href="' + currentURL + '" target="_blank" rel="noreferrer"><button class="sm success">↗ 直接打开链接</button></a>' +
      '</div>' +
    '</div>' +
    '<div class="url-box">' + currentURL + '</div>' +
    '<div class="row" style="margin-top:8px">' +
      '<input type="text" id="input-' + s.id + '" placeholder="在此粘贴浏览器跳转后的 http://localhost:18080/... 或 http://127.0.0.1:18080/... 完整网址" style="flex:1" onkeydown="if(event.key===\'Enter\')submitCallback(\'' + s.id + '\')">' +
      '<button onclick="submitCallback(\'' + s.id + '\')">提交回调 URL</button>' +
    '</div>' +
    '<div class="msg" id="msg-' + s.id + '"></div>';
}

async function submitCallback(id) {
  const input = document.getElementById('input-' + id);
  const msgEl = document.getElementById('msg-' + id);
  const val = input.value.trim();
  if (!val) return;
  msgEl.style.color = '#94a3b8';
  msgEl.textContent = '正在向 AWS 校验并交换 Token...';
  try {
    const data = await api('/quick-admin/api/login/callback', {
      method: 'POST',
      body: JSON.stringify({session_id: id, callback_url: val})
    });
    const box = document.getElementById('sess-' + id);
    if (data.session.step === 3) {
      box.className = 'task-item task-done';
    }
    box.innerHTML = renderTaskHTML(data.session);
    loadAccounts();
  } catch (e) {
    msgEl.style.color = '#f87171';
    msgEl.textContent = '❌ ' + e.message;
  }
}

function copyText(txt) {
  navigator.clipboard.writeText(txt);
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
