package oauth

import (
	"context"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync"
	"time"

	"quickwork-openai/internal/store"
)

// WebSession represents one active browser login session in the Web UI.
type WebSession struct {
	ID           string    `json:"id"`
	Provider     string    `json:"provider"`
	Stage        string    `json:"stage"`
	Step         int       `json:"step"` // 1 = waiting discovery callback, 2 = waiting code callback, 3 = done
	DiscoveryURL string    `json:"discovery_url"`
	AuthorizeURL string    `json:"authorize_url,omitempty"`
	AccountEmail string    `json:"account_email,omitempty"`
	AccountID    int64     `json:"account_id,omitempty"`
	CreatedAt    time.Time `json:"created_at"`

	flow *loginFlow
}

// WebManager manages concurrent web login sessions and account operations.
type WebManager struct {
	mu         sync.Mutex
	sessions   map[string]*WebSession
	store      *store.Store
	appVersion string
	port       int
}

func NewWebManager(st *store.Store, appVersion string, port int) *WebManager {
	if appVersion == "" {
		appVersion = "1.2.4660"
	}
	if port <= 0 {
		port = 18080
	}
	return &WebManager{
		sessions:   make(map[string]*WebSession),
		store:      st,
		appVersion: appVersion,
		port:       port,
	}
}

func (m *WebManager) StartSession(provider, stage, region string) (*WebSession, error) {
	m.mu.Lock()
	defer m.mu.Unlock()

	// Clean up sessions older than 1 hour
	now := time.Now()
	for k, s := range m.sessions {
		if now.Sub(s.CreatedAt) > time.Hour {
			delete(m.sessions, k)
		}
	}

	opts := normalizeLoginOptions(LoginOptions{
		Provider:   provider,
		Stage:      stage,
		Region:     region,
		Port:       m.port,
		AppVersion: m.appVersion,
	})
	f, err := newLoginFlow(opts, m.store, opts.Port)
	if err != nil {
		return nil, err
	}
	discURL := discoveryURL(f.provider, f.stage, f.region, f.nonce, opts.Port, opts.AppVersion)
	id, _ := randomToken()
	sess := &WebSession{
		ID:           id,
		Provider:     f.provider,
		Stage:        f.stage,
		Step:         1,
		DiscoveryURL: discURL,
		CreatedAt:    now,
		flow:         f,
	}
	m.sessions[id] = sess
	return sess, nil
}

func (m *WebManager) SubmitCallback(ctx context.Context, sessionID, rawURL string) (*WebSession, string, error) {
	m.mu.Lock()
	sess := m.sessions[sessionID]
	m.mu.Unlock()
	if sess == nil {
		return nil, "", fmt.Errorf("登录会话不存在或已过期，请点击「新建登录任务」")
	}

	q, err := parseCallbackURL(rawURL)
	if err != nil {
		return sess, "", fmt.Errorf("无法解析粘贴的 URL: %v", err)
	}

	switch classifyCallback(q) {
	case callbackDiscovery, callbackDiscoveryError:
		authURL, err := sess.flow.processDiscovery(ctx, q)
		if err != nil {
			return sess, "", fmt.Errorf("第一步 Discovery 校验失败: %v", err)
		}
		sess.AuthorizeURL = authURL
		sess.Step = 2
		return sess, "第一步成功！请点击打开下方【第二步最终授权链接】，完成后粘贴 127.0.0.1:18080/?code=... 链接", nil

	case callbackCode, callbackCodeError:
		acct, err := sess.flow.completeLogin(ctx, q)
		if err != nil {
			return sess, "", fmt.Errorf("第二步 Token 交换失败: %v", err)
		}
		sess.Step = 3
		sess.AccountID = acct.ID
		sess.AccountEmail = acct.Email
		if sess.AccountEmail == "" {
			sess.AccountEmail = acct.DisplayName
		}
		return sess, fmt.Sprintf("🎉 入库成功！账号 #%d (%s) 已加入轮询池", acct.ID, sess.AccountEmail), nil

	default:
		return sess, "", fmt.Errorf("未识别的回调链接，请粘贴浏览器跳转到 localhost:18080/callback?nonce=... 或 127.0.0.1:18080/?code=... 的完整地址")
	}
}

// LocalBridgeListener runs on 127.0.0.1:18080 inside the server so if the user
// connects via SSH tunnel (-L 18080:127.0.0.1:18080), callbacks complete 100%
// automatically without even copying URLs!
func (m *WebManager) HandleBridgeCallback(w http.ResponseWriter, r *http.Request) {
	q := r.URL.Query()
	m.mu.Lock()
	var target *WebSession
	nonce := q.Get("nonce")
	state := q.Get("state")
	for _, s := range m.sessions {
		if nonce != "" && s.flow.nonce == nonce {
			target = s
			break
		}
		if state != "" && s.flow.state == state {
			target = s
			break
		}
	}
	m.mu.Unlock()

	if target == nil {
		http.Error(w, "No matching active login session found. Please create a login session in the Web UI first.", http.StatusBadRequest)
		return
	}

	if r.URL.Path == "/callback" || nonce != "" {
		authURL, err := target.flow.processDiscovery(r.Context(), q)
		if err != nil {
			http.Error(w, "Discovery failed: "+err.Error(), http.StatusBadRequest)
			return
		}
		target.AuthorizeURL = authURL
		target.Step = 2
		http.Redirect(w, r, authURL, http.StatusFound)
		return
	}

	acct, err := target.flow.completeLogin(r.Context(), q)
	if err != nil {
		http.Error(w, "Login failed: "+err.Error(), http.StatusBadRequest)
		return
	}
	target.Step = 3
	target.AccountID = acct.ID
	target.AccountEmail = acct.Email
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	_, _ = fmt.Fprintf(w, `<!doctype html><html><body style="font-family:system-ui;background:#0f172a;color:#f8fafc;display:flex;align-items:center;justify-content:center;height:100vh;margin:0"><div style="background:#1e293b;padding:32px;border-radius:12px;text-align:center;box-shadow:0 10px 25px rgba(0,0,0,0.4)"><h2 style="color:#4ade80;margin-top:0">✅ Google 账号入库成功！</h2><p>账号: <b>%s</b> (#%d)</p><p style="color:#94a3b8;font-size:14px">你可以直接关闭此无痕窗口，继续上号。</p></div></body></html>`, acct.Email, acct.ID)
}

func writeWebJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(v)
}

func checkWebAuth(r *http.Request, validKeys []string) bool {
	if len(validKeys) == 0 {
		return true
	}
	tok := strings.TrimSpace(r.Header.Get("X-Admin-Key"))
	if tok == "" {
		tok = strings.TrimSpace(r.URL.Query().Get("key"))
	}
	for _, k := range validKeys {
		if k != "" && tok == k {
			return true
		}
	}
	return false
}

// CompleteSession is used by the automated register engine after it drives
// the browser through email/code/password and captures the final code callback.
func (m *WebManager) CompleteSession(ctx context.Context, sessionID string) (*store.Account, error) {
	m.mu.Lock()
	sess := m.sessions[sessionID]
	m.mu.Unlock()
	if sess == nil {
		return nil, fmt.Errorf("session not found")
	}
	return nil, nil
}
