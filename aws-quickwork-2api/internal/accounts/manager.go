// Package accounts selects accounts for rotation and keeps their tokens fresh.
package accounts

import (
	"context"
	"errors"
	"fmt"
	"hash/fnv"
	"strings"
	"sync"
	"sync/atomic"
	"time"

	"quickwork-openai/internal/config"
	"quickwork-openai/internal/oauth"
	"quickwork-openai/internal/store"
)

// ErrNoAccounts means the database has no enabled account.
var ErrNoAccounts = errors.New("no accounts configured; run `quickproxy login`")

// Manager rotates over enabled accounts, tracks in-flight concurrency per
// account, supports user/IP sticky affinity, and refreshes tokens on demand.
type Manager struct {
	store *store.Store
	cfg   *config.Config

	next atomic.Uint64

	mu       sync.Mutex
	locks    map[int64]*sync.Mutex
	inflight map[int64]*atomic.Int64
}

func New(st *store.Store, cfg *config.Config) *Manager {
	return &Manager{
		store:    st,
		cfg:      cfg,
		locks:    make(map[int64]*sync.Mutex),
		inflight: make(map[int64]*atomic.Int64),
	}
}

func (m *Manager) lockFor(id int64) *sync.Mutex {
	m.mu.Lock()
	defer m.mu.Unlock()
	l := m.locks[id]
	if l == nil {
		l = &sync.Mutex{}
		m.locks[id] = l
	}
	return l
}

func (m *Manager) counterFor(id int64) *atomic.Int64 {
	m.mu.Lock()
	defer m.mu.Unlock()
	c := m.inflight[id]
	if c == nil {
		c = &atomic.Int64{}
		m.inflight[id] = c
	}
	return c
}

// Acquire increments the in-flight request counter for an account and returns
// a release function that must be called when the upstream call finishes.
func (m *Manager) Acquire(id int64) (inFlight int64, release func()) {
	c := m.counterFor(id)
	cur := c.Add(1)
	var once sync.Once
	return cur, func() {
		once.Do(func() {
			c.Add(-1)
		})
	}
}

// InFlight returns the current number of active requests on an account.
func (m *Manager) InFlight(id int64) int64 {
	return m.counterFor(id).Load()
}

// Next returns the next usable account in round-robin order (fallback).
func (m *Manager) Next(ctx context.Context) (*store.Account, error) {
	return m.NextForAffinity(ctx, "", nil)
}

// NextForAffinity selects an account using User/IP sticky affinity combined
// with Least-In-Flight concurrency protection:
//  1. If affinityKey is non-empty, it hashes affinityKey to a primary account.
//     If that account is currently idle (inFlight == 0) and not in exclude, it
//     is chosen immediately to maximize prompt cache hits for that user/IP.
//  2. If the primary sticky account is busy (inFlight > 0) or no affinityKey
//     is present, it scans accounts starting from the sticky/round-robin index
//     and picks the usable account with the lowest in-flight concurrency,
//     preventing concurrent requests from piling onto a single account.
func (m *Manager) NextForAffinity(ctx context.Context, affinityKey string, exclude map[int64]bool) (*store.Account, error) {
	list, err := m.store.Enabled(ctx)
	if err != nil {
		return nil, err
	}
	if len(list) == 0 {
		return nil, ErrNoAccounts
	}

	var start int
	if k := strings.TrimSpace(affinityKey); k != "" {
		h := fnv.New32a()
		_, _ = h.Write([]byte(k))
		start = int(h.Sum32() % uint32(len(list)))
	} else {
		start = int(m.next.Add(1)-1) % len(list)
	}

	// Build candidate order sorted by (excluded, inFlight, offset from start)
	type candidate struct {
		idx      int
		inFlight int64
	}
	cands := make([]candidate, 0, len(list))
	for i := range list {
		idx := (start + i) % len(list)
		acctID := list[idx].ID
		if exclude != nil && exclude[acctID] && len(list) > len(exclude) {
			continue
		}
		cands = append(cands, candidate{
			idx:      idx,
			inFlight: m.InFlight(acctID),
		})
	}
	// Stable insertion sort by inFlight ascending so idle accounts always win,
	// while preserving sticky/round-robin priority among equally idle accounts.
	for i := 1; i < len(cands); i++ {
		cur := cands[i]
		j := i - 1
		for j >= 0 && cands[j].inFlight > cur.inFlight {
			cands[j+1] = cands[j]
			j--
		}
		cands[j+1] = cur
	}

	var errs []string
	for _, cand := range cands {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		a := &list[cand.idx]
		usable, err := m.ensureFresh(ctx, a)
		if err != nil {
			_ = m.store.SetError(ctx, a.ID, err.Error())
			errs = append(errs, fmt.Sprintf("account %d (%s): %v", a.ID, a.Email, err))
			continue
		}
		if err := m.store.MarkUsed(ctx, usable.ID); err != nil {
			return nil, err
		}
		return usable, nil
	}
	return nil, fmt.Errorf("no usable account: %s", strings.Join(errs, "; "))
}

// NextPinned returns one specific account, refreshing it when needed. It is
// used to keep a conversation on the account that owns its upstream session.
func (m *Manager) NextPinned(ctx context.Context, id int64) (*store.Account, error) {
	a, err := m.store.Get(ctx, id)
	if err != nil {
		return nil, err
	}
	if a.Disabled {
		return nil, fmt.Errorf("account %d is disabled", id)
	}
	usable, err := m.ensureFresh(ctx, a)
	if err != nil {
		_ = m.store.SetError(ctx, a.ID, err.Error())
		return nil, err
	}
	if err := m.store.MarkUsed(ctx, usable.ID); err != nil {
		return nil, err
	}
	return usable, nil
}

func (m *Manager) ensureFresh(ctx context.Context, a *store.Account) (*store.Account, error) {
	if !a.Expiring(time.Now(), m.cfg.RefreshBefore.Duration()) {
		if a.JWT() == "" {
			return nil, errors.New("account has no access token")
		}
		return a, nil
	}
	lock := m.lockFor(a.ID)
	lock.Lock()
	defer lock.Unlock()

	// Another request may have refreshed it while we waited.
	latest, err := m.store.Get(ctx, a.ID)
	if err != nil {
		return nil, err
	}
	if !latest.Expiring(time.Now(), m.cfg.RefreshBefore.Duration()) {
		if latest.JWT() == "" {
			return nil, errors.New("account has no access token")
		}
		return latest, nil
	}
	return m.refreshLocked(ctx, latest)
}

// refreshLocked refreshes and persists one account. The caller holds its lock.
func (m *Manager) refreshLocked(ctx context.Context, a *store.Account) (*store.Account, error) {
	refreshed, err := oauth.Refresh(ctx, a)
	if err != nil {
		return nil, err
	}
	if err := m.store.Update(ctx, refreshed); err != nil {
		return nil, err
	}
	return refreshed, nil
}

// RefreshOne forces a refresh of one account.
func (m *Manager) RefreshOne(ctx context.Context, id int64) (*store.Account, error) {
	a, err := m.store.Get(ctx, id)
	if err != nil {
		return nil, err
	}
	lock := m.lockFor(id)
	lock.Lock()
	defer lock.Unlock()
	if a, err = m.store.Get(ctx, id); err != nil {
		return nil, err
	}
	refreshed, err := m.refreshLocked(ctx, a)
	if err != nil {
		_ = m.store.SetError(ctx, id, err.Error())
		return nil, err
	}
	return refreshed, nil
}

// RefreshResult is one account's outcome from RefreshAll.
type RefreshResult struct {
	Account *store.Account
	Err     error
}

// RefreshAll refreshes every stored account that has a refresh token.
func (m *Manager) RefreshAll(ctx context.Context) ([]RefreshResult, error) {
	list, err := m.store.List(ctx)
	if err != nil {
		return nil, err
	}
	results := make([]RefreshResult, 0, len(list))
	for i := range list {
		if err := ctx.Err(); err != nil {
			return results, err
		}
		a := &list[i]
		if strings.TrimSpace(a.RefreshToken) == "" {
			results = append(results, RefreshResult{Account: a, Err: oauth.ErrNoRefreshToken})
			_ = m.store.SetError(ctx, a.ID, oauth.ErrNoRefreshToken.Error())
			continue
		}
		refreshed, err := m.RefreshOne(ctx, a.ID)
		if refreshed == nil {
			refreshed = a
		}
		results = append(results, RefreshResult{Account: refreshed, Err: err})
	}
	return results, nil
}

// ReportFailure records an upstream failure for an account.
func (m *Manager) ReportFailure(ctx context.Context, id int64, err error) {
	if err == nil {
		return
	}
	_ = m.store.SetError(ctx, id, err.Error())
}

// EnabledCount returns the number of accounts in rotation.
func (m *Manager) EnabledCount(ctx context.Context) (int, error) {
	list, err := m.store.Enabled(ctx)
	if err != nil {
		return 0, err
	}
	return len(list), nil
}

// Store returns the underlying SQLite account store.
func (m *Manager) Store() *store.Store { return m.store }
