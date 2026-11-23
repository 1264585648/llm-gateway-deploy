import asyncio
import base64
import os
import time
import urllib.parse
import uuid
from typing import Any, Dict, Optional

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from playwright.async_api import BrowserContext, Page

MUSE2API_URL = os.environ.get("MUSE2API_URL", "http://muse2api:18610")
DEFAULT_EGRESS_PROXY = os.environ.get("MUSE_DEFAULT_PROXY", "").strip()
router = APIRouter()


class MuseJobState:
    def __init__(self, job_id: str, email: str, label: str, proxy_url: str, admin_key: str):
        self.job_id = job_id
        self.email = email
        self.label = label or email
        self.proxy_url = (proxy_url or "").strip()
        self.admin_key = admin_key
        # starting | sending_code | waiting_code | waiting_input | verifying | blocked | done | error
        self.status = "starting"
        self.message = "正在启动无头浏览器并打开 muse.ai 登录页..."
        self.error = ""
        self.hint = ""
        self.account_id = ""
        self.expires_at = 0
        self.cookie_count = 0
        self.screenshot_b64 = ""
        self.created_at = time.time()
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.lock = asyncio.Lock()


muse_jobs: Dict[str, MuseJobState] = {}


def _parse_playwright_proxy(proxy_url: str) -> Optional[Dict[str, str]]:
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    u = urllib.parse.urlsplit(raw)
    if not u.scheme or not u.hostname:
        raise ValueError("代理格式无效，示例: socks5://egress-ip:18443")
    scheme = "socks5" if u.scheme.lower() == "socks5h" else u.scheme.lower()
    port = f":{u.port}" if u.port else ""
    cfg: Dict[str, str] = {"server": f"{scheme}://{u.hostname}{port}"}
    if u.username:
        cfg["username"] = urllib.parse.unquote(u.username)
    if u.password:
        cfg["password"] = urllib.parse.unquote(u.password)
    return cfg


def _muse_proxy_payload(proxy_url: str) -> Optional[Dict[str, Any]]:
    raw = (proxy_url or "").strip()
    if not raw:
        return None
    return {"server": raw}


async def _snap(job: MuseJobState):
    if job.page:
        try:
            png = await job.page.screenshot(type="jpeg", quality=68)
            job.screenshot_b64 = "data:image/jpeg;base64," + base64.b64encode(png).decode()
        except Exception:
            pass


async def _cleanup_job(job: MuseJobState):
    if job.context:
        try:
            await job.context.close()
        except Exception:
            pass
        job.context = None
        job.page = None


def _prune_old_jobs():
    now = time.time()
    stale = [jid for jid, j in muse_jobs.items() if now - j.created_at > 1800]
    for jid in stale:
        j = muse_jobs.pop(jid, None)
        if j and j.context:
            asyncio.create_task(_cleanup_job(j))


def _verify_muse_admin_key(admin_key: str):
    key = (admin_key or "").strip()
    if not key:
        raise HTTPException(status_code=401, detail="请填写 Muse2API 管理密钥 (MUSE2API_KEY)")
    try:
        r = requests.get(
            f"{MUSE2API_URL}/admin/status",
            headers={"Authorization": f"Bearer {key}"},
            timeout=10,
        )
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"连接 muse2api 失败: {exc}")
    if r.status_code == 401:
        raise HTTPException(status_code=401, detail="Muse2API 管理密钥无效 (401)")
    if not r.ok:
        raise HTTPException(status_code=r.status_code, detail=f"muse2api 响应异常: {r.text[:200]}")


async def _detect_block(page: Page) -> Optional[str]:
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        return None
    captcha_kw = (
        "captcha", "verify you are human", "human verification",
        "complete the security check", "arkose", "turnstile",
        "security puzzle", "not a robot", "人机验证", "安全验证"
    )
    if any(k in body for k in captcha_kw):
        return "Muse/Meta 页面触发了人机验证（CAPTCHA）。遵循安全规范已停止自动化，请改用本地浏览器登录后使用 Chrome 扩展一键导入。"
    if "not available in your country" in body or "not available in your region" in body or "地区不可用" in body:
        return "当前出口地区受限，请切换至美区直连或自定义美区 SOCKS5/HTTP 出口代理后重试。"
    if "credit card" in body or "add a payment method" in body or "billing" in body and "card number" in body:
        return "页面要求绑定支付方式/信用卡验证，已停止自动化，请查看右侧截图并在本地浏览器完成。"
    return None


async def _detect_error_banner(page: Page) -> Optional[str]:
    for sel in ('[role="alert"]', '[aria-live="assertive"]', '[data-testid*="error" i]'):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=250):
                txt = (await loc.inner_text()).strip()
                if txt and len(txt) > 3 and "required" not in txt.lower():
                    return txt[:200]
        except Exception:
            continue
    return None


async def _click_any(page: Page, selectors, timeout=3500) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout):
                await loc.click()
                return True
        except Exception:
            continue
    return False


async def _fill_any(page: Page, selectors, text: str, timeout=5000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout):
                await loc.click()
                await loc.fill(text)
                return True
        except Exception:
            continue
    return False


async def _extract_and_import_cookies(job: MuseJobState) -> bool:
    if not job.context:
        return False
    all_cookies = await job.context.cookies(["https://muse.ai", "https://www.muse.ai"])
    cookies: Dict[str, str] = {}
    expires: Dict[str, int] = {}
    for c in all_cookies:
        domain = (c.get("domain") or "").lstrip(".").lower()
        if not (domain == "muse.ai" or domain.endswith(".muse.ai")):
            continue
        name = c.get("name") or ""
        val = c.get("value") or ""
        if name and val:
            cookies[name] = val
            exp = c.get("expires")
            if isinstance(exp, (int, float)) and exp > 0:
                expires[name] = int(exp)

    if "hatch_sess" not in cookies:
        return False

    payload: Dict[str, Any] = {
        "label": job.label,
        "cookies": cookies,
        "expires": expires,
    }
    proxy_obj = _muse_proxy_payload(job.proxy_url)
    if proxy_obj:
        payload["proxy"] = proxy_obj

    r = requests.post(
        f"{MUSE2API_URL}/admin/accounts",
        headers={
            "Authorization": f"Bearer {job.admin_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=15,
    )
    if not r.ok:
        raise RuntimeError(f"写入 muse2api 号池失败 (HTTP {r.status_code}): {r.text[:240]}")

    data = r.json()
    added = (data.get("added") or [{}])[0]
    job.account_id = str(added.get("id") or "")
    job.cookie_count = int(added.get("cookie_count") or len(cookies))
    job.expires_at = int(added.get("expires_at") or 0)
    job.status = "done"
    job.error = ""
    job.message = (
        f"🎉 登录并入池成功！账号 {job.label}（ID: {job.account_id}，提取 {job.cookie_count} 条 Cookie，"
        f"含核心 HttpOnly hatch_sess）已加入 Meta Muse 号池！"
    )
    await _snap(job)
    await _cleanup_job(job)
    return True


async def _get_shared_browser():
    # Reuse the headed/Xvfb Playwright browser instance from server.py
    import server as srv
    return await srv.get_browser()


async def _drive_step1(job: MuseJobState):
    async with job.lock:
        try:
            browser = await _get_shared_browser()
            ctx_kwargs: Dict[str, Any] = {
                "viewport": {"width": 1280, "height": 800},
                "locale": "en-US",
                "timezone_id": "America/Los_Angeles",
                "user_agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
                ),
            }
            pw_proxy = _parse_playwright_proxy(job.proxy_url)
            if pw_proxy:
                ctx_kwargs["proxy"] = pw_proxy

            context = await browser.new_context(**ctx_kwargs)
            page = await context.new_page()
            job.context = context
            job.page = page

            job.status = "sending_code"
            job.message = f"正在打开 https://muse.ai/ 并填入邮箱 {job.email}..."
            await page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=35000)
            await asyncio.sleep(3.0)
            await _snap(job)

            if blk := await _detect_block(page):
                job.status = "blocked"
                job.error = blk
                job.message = blk
                return

            # Some screens hide the form behind a "Log in" button
            email_selectors = [
                'input[placeholder*="Mobile number or email" i]',
                'input[placeholder*="email" i]',
                'input[type="email"]',
                'input[name="email"]',
                'input[type="text"]',
            ]
            filled = await _fill_any(page, email_selectors, job.email, timeout=6000)
            if not filled:
                await _click_any(page, ['button:has-text("Log in")', 'a:has-text("Log in")'], timeout=3000)
                await asyncio.sleep(2.0)
                filled = await _fill_any(page, email_selectors, job.email, timeout=6000)

            await _snap(job)
            if not filled:
                job.status = "error"
                job.error = "未找到 muse.ai 邮箱输入框，请查看右侧实时截图。"
                job.message = job.error
                return

            await asyncio.sleep(0.4)
            await _click_any(
                page,
                [
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                    'button:has-text("Next")',
                    'button:has-text("继续")',
                ],
                timeout=5000,
            )

            job.message = "已提交邮箱，正在等待跳转至验证码/登录验证页面..."
            await asyncio.sleep(4.5)
            await _snap(job)

            if await _extract_and_import_cookies(job):
                return

            if blk := await _detect_block(page):
                job.status = "blocked"
                job.error = blk
                job.message = blk
                return

            if err_b := await _detect_error_banner(page):
                job.status = "waiting_input"
                job.hint = f"页面提示: {err_b}"
                job.message = f"页面出现提示，请查看右侧截图并根据页面要求输入内容（如验证码/密码）"
                return

            body = (await page.inner_text("body")).lower()
            pwd_count = await page.locator('input[type="password"]:visible').count()
            if pwd_count > 0 and "code" not in body and "verification" not in body:
                job.status = "waiting_code"
                job.hint = "password"
                job.message = f"页面要求输入密码或登录凭证（{job.email}），请在下方填写并提交。"
            else:
                job.status = "waiting_code"
                job.hint = "otp"
                job.message = f"已向 {job.email} 触发登录流程！请查看右侧实时画面，并在下方填入收到的邮箱验证码（或页面要求的字段）。"
        except Exception as exc:
            job.status = "error"
            job.error = f"启动登录异常: {exc}"
            job.message = job.error
            await _snap(job)


async def _fill_code_or_text(page: Page, value: str) -> bool:
    val = value.strip()
    # 1. Check split OTP boxes (4-8 single-char inputs)
    split_boxes = page.locator('input[maxlength="1"]:visible')
    if 4 <= await split_boxes.count() <= 8 and val.isdigit():
        await split_boxes.first.click()
        await page.keyboard.type(val, delay=70)
        return True

    # 2. Check password field if visible
    pwd = page.locator('input[type="password"]:visible')
    if await pwd.count() > 0:
        await pwd.first.click()
        await pwd.first.fill(val)
        return True

    # 3. Single OTP / text input
    return await _fill_any(
        page,
        [
            'input[autocomplete="one-time-code"]',
            'input[inputmode="numeric"]',
            'input[name*="code" i]',
            'input[placeholder*="code" i]',
            'input[placeholder*="otp" i]',
            'input[placeholder*="name" i]',
            'input[type="tel"]',
            'input[type="number"]',
            'input[type="text"]',
        ],
        val,
        timeout=5000,
    )


async def _drive_verify(job: MuseJobState, code: str, extra_name: str = ""):
    async with job.lock:
        if not job.page or not job.context:
            job.status = "error"
            job.error = "浏览器会话已关闭，请重新发起登录。"
            job.message = job.error
            return

        page = job.page
        try:
            job.status = "verifying"
            job.error = ""
            job.message = "正在提交验证码/登录凭证..."

            if code.strip():
                ok = await _fill_code_or_text(page, code)
                await _snap(job)
                if not ok:
                    job.status = "waiting_input"
                    job.message = "当前页面未找到输入框，请查看右侧截图（若已进入工作区可点「立即提取 Cookie 入池」）。"
                    return
                await asyncio.sleep(0.4)
                clicked = await _click_any(
                    page,
                    [
                        'button[type="submit"]',
                        'button:has-text("Continue")',
                        'button:has-text("Verify")',
                        'button:has-text("Next")',
                        'button:has-text("Log in")',
                        'button:has-text("Submit")',
                        'button:has-text("确认")',
                        'button:has-text("继续")',
                    ],
                    timeout=3500,
                )
                if not clicked:
                    await page.keyboard.press("Enter")

            # Poll for hatch_sess or next step (up to ~24s)
            for _ in range(12):
                await asyncio.sleep(2.0)
                await _snap(job)

                if await _extract_and_import_cookies(job):
                    return

                if blk := await _detect_block(page):
                    job.status = "blocked"
                    job.error = blk
                    job.message = blk
                    return

                # If user provided an optional display name and Muse asks for name
                if extra_name.strip():
                    name_inp = page.locator('input[placeholder*="name" i]:visible, input[name*="name" i]:visible').first
                    try:
                        if await name_inp.is_visible(timeout=200) and not (await name_inp.input_value()).strip():
                            await name_inp.fill(extra_name.strip())
                            await _click_any(page, ['button[type="submit"]', 'button:has-text("Continue")', 'button:has-text("Next")'], timeout=1500)
                            continue
                    except Exception:
                        pass

                # Auto-click harmless onboarding continue/agree buttons only when no empty text/password inputs remain
                visible_inputs = page.locator('input[type="text"]:visible, input[type="password"]:visible, input[type="email"]:visible, input[type="tel"]:visible, input[inputmode="numeric"]:visible')
                if await visible_inputs.count() == 0:
                    await _click_any(
                        page,
                        [
                            'button:has-text("I agree")',
                            'button:has-text("Agree")',
                            'button:has-text("Accept")',
                            'button:has-text("Continue")',
                            'button:has-text("Get started")',
                            'button:has-text("Skip")',
                        ],
                        timeout=600,
                    )

            # Still not logged in after 24s -> pause cleanly and show live screenshot for next user input
            err_b = await _detect_error_banner(page)
            job.status = "waiting_input"
            if err_b:
                job.message = f"页面提示：{err_b}。请根据右侧最新截图继续输入或重试。"
            else:
                job.message = "尚未检测到登录完成 Cookie（hatch_sess），请查看右侧最新截图：若页面需要填写姓名/生日/密码/新验证码，可在下方继续提交；若已进入首页可点「立即提取 Cookie」。"
        except Exception as exc:
            job.status = "error"
            job.error = f"提交验证异常: {exc}"
            job.message = job.error
            await _snap(job)


class MuseStartReq(BaseModel):
    email: str
    label: Optional[str] = ""
    proxy: Optional[str] = ""
    admin_key: str


class MuseVerifyReq(BaseModel):
    job_id: str
    code: Optional[str] = ""
    extra_name: Optional[str] = ""
    admin_key: str


class MuseActionReq(BaseModel):
    job_id: str
    admin_key: str


@router.post("/muse-autoreg/start")
async def muse_start(req: MuseStartReq):
    _prune_old_jobs()
    _verify_muse_admin_key(req.admin_key)
    email = (req.email or "").strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="请输入合法的邮箱地址")
    if req.proxy:
        try:
            _parse_playwright_proxy(req.proxy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    job_id = uuid.uuid4().hex[:12]
    label = (req.label or "").strip() or email
    job = MuseJobState(job_id, email, label, req.proxy or "", req.admin_key)
    muse_jobs[job_id] = job
    asyncio.create_task(_drive_step1(job))
    return _job_dict(job)


@router.post("/muse-autoreg/verify")
async def muse_verify(req: MuseVerifyReq):
    _verify_muse_admin_key(req.admin_key)
    job = muse_jobs.get(req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="登录会话不存在或已过期")
    asyncio.create_task(_drive_verify(job, req.code or "", req.extra_name or ""))
    return _job_dict(job)


@router.post("/muse-autoreg/extract")
async def muse_extract(req: MuseActionReq):
    _verify_muse_admin_key(req.admin_key)
    job = muse_jobs.get(req.job_id)
    if not job or not job.context:
        raise HTTPException(status_code=404, detail="登录会话不存在或已关闭")
    async with job.lock:
        try:
            await _snap(job)
            ok = await _extract_and_import_cookies(job)
            if not ok:
                return {"ok": False, "message": "当前浏览器尚未获取到核心登录 Cookie (hatch_sess)，请先完成登录步骤。", "job": _job_dict(job)}
            return {"ok": True, "message": job.message, "job": _job_dict(job)}
        except Exception as exc:
            raise HTTPException(status_code=500, detail=str(exc))


@router.post("/muse-autoreg/snap")
async def muse_snap(req: MuseActionReq):
    _verify_muse_admin_key(req.admin_key)
    job = muse_jobs.get(req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    await _snap(job)
    return _job_dict(job)


@router.delete("/muse-autoreg/job/{job_id}")
async def muse_close_job(job_id: str):
    job = muse_jobs.pop(job_id, None)
    if job:
        await _cleanup_job(job)
    return {"ok": True}


@router.get("/muse-autoreg/status/{job_id}")
async def muse_status(job_id: str):
    job = muse_jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="登录会话不存在")
    return _job_dict(job)


def _job_dict(job: MuseJobState) -> Dict[str, Any]:
    return {
        "job_id": job.job_id,
        "email": job.email,
        "label": job.label,
        "proxy": job.proxy_url,
        "status": job.status,
        "hint": job.hint,
        "message": job.message,
        "error": job.error,
        "account_id": job.account_id,
        "cookie_count": job.cookie_count,
        "expires_at": job.expires_at,
        "screenshot": job.screenshot_b64,
    }


@router.get("/muse-login", response_class=HTMLResponse)
@router.get("/muse-login/", response_class=HTMLResponse)
async def muse_login_page():
    extra_opt = (
        f'<option value="{DEFAULT_EGRESS_PROXY}">独立出口代理 ({DEFAULT_EGRESS_PROXY})</option>'
        if DEFAULT_EGRESS_PROXY
        else ""
    )
    html = MUSE_LOGIN_HTML.replace("__EXTRA_PROXY_OPTION__", extra_opt)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


MUSE_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Meta Muse 极速上号器 & 号池管理</title>
<style>
  :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc; --muted: #94a3b8; --primary: #3b82f6; --success: #22c55e; --danger: #ef4444; --warn: #f59e0b; --purple: #8b5cf6; --cyan: #06b6d4; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }
  .container { max-width: 1200px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
  .title { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .badge { font-size: 12px; padding: 4px 10px; border-radius: 999px; background: rgba(6,182,212,0.18); color: #22d3ee; font-weight: 600; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  input, select, textarea { background: #0b1120; border: 1px solid var(--border); color: var(--text); padding: 9px 12px; border-radius: 8px; font-size: 14px; outline: none; }
  input:focus, select:focus, textarea:focus { border-color: var(--cyan); }
  button, a.btn { background: var(--primary); color: #fff; border: none; padding: 9px 16px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; text-decoration: none; display: inline-flex; align-items: center; gap: 6px; transition: opacity 0.15s; }
  button:hover, a.btn:hover { opacity: 0.9; }
  button.secondary, a.btn.secondary { background: #334155; }
  button.success { background: var(--success); }
  button.purple { background: var(--purple); }
  button.cyan { background: var(--cyan); color: #042f2e; }
  button.danger { background: var(--danger); padding: 6px 10px; font-size: 12px; }
  button.sm { padding: 6px 10px; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; margin-top: 14px; font-size: 14px; }
  th, td { text-align: left; padding: 12px 10px; border-bottom: 1px solid var(--border); }
  th { color: var(--muted); font-weight: 600; font-size: 13px; }
  .status-pill { padding: 3px 9px; border-radius: 999px; font-size: 12px; font-weight: 600; }
  .status-ok { background: rgba(34,197,94,0.18); color: #4ade80; }
  .status-warn { background: rgba(245,158,11,0.18); color: #fbbf24; }
  .status-bad { background: rgba(239,68,68,0.18); color: #f87171; }
  .task-item { background: #0b1120; border: 1px solid var(--border); border-radius: 10px; padding: 16px; margin-top: 14px; }
  .task-done { border-color: var(--success); background: rgba(34,197,94,0.06); }
  .task-err { border-color: var(--danger); background: rgba(239,68,68,0.06); }
  .tip { color: var(--muted); font-size: 13px; line-height: 1.65; }
  .auto-grid { display: grid; grid-template-columns: 1fr 340px; gap: 16px; align-items: start; }
  .shot { width: 100%; border-radius: 8px; border: 1px solid var(--border); cursor: zoom-in; background: #020617; }
  @media (max-width: 860px) { .auto-grid { grid-template-columns: 1fr; } }
</style>
</head>
<body>
<div class="container">
  <div class="header">
    <div class="title">
      🎨 Meta Muse 极速上号器 & 多号池管理
      <span class="badge" id="poolCount">加载中...</span>
    </div>
    <div class="row">
      <a class="btn secondary" href="/admin" target="_blank">🖥️ 完整运维台 & 媒体库</a>
      <a class="btn secondary" href="/admin/extension" download>🧩 下载 Chrome 导号插件</a>
      <input type="password" id="adminKey" placeholder="输入 MUSE2API_KEY" style="width:230px">
      <button onclick="saveKeyAndLoad()">验证 / 刷新</button>
    </div>
  </div>

  <!-- 方式一：服务端 Playwright 邮箱验证码直登上号 -->
  <div class="card" style="border-color:#06b6d4">
    <h3 style="margin:0 0 6px 0;color:#67e8f9">🤖 方式一：服务端邮箱验证码直登上号（免开代理 / 自动提取 HttpOnly Cookie 入池）</h3>
    <div class="tip" style="margin-bottom:14px">
      服务器内置 <b>Playwright 真实渲染引擎（美西/美东双美区出口可选）</b>：输入邮箱点击启动后，服务器自动打开 <code>muse.ai</code> 触发登录验证码并回传实时画面；你只需把收到的 <b>邮箱验证码</b>（或密码/姓名）填进来点击提交，系统自动抓齐 <code>HttpOnly hatch_sess</code> 并直接写入 <code>Meta-Muse-Pool</code> 号池！（若遇人机验证将自动停止并提示。）
    </div>

    <div class="row" style="background:#0b1120;padding:14px;border-radius:10px;border:1px solid var(--border)">
      <label style="font-size:13px;color:var(--muted)">邮箱:</label>
      <input type="text" id="baseEmail" placeholder="输入你的邮箱 (如 name@gmail.com)" style="width:230px" oninput="updatePreview()">
      <select id="aliasMode" onchange="updatePreview()">
        <option value="raw" selected>原样使用邮箱</option>
        <option value="plus">Gmail +号别名 (+m01)</option>
        <option value="dot">Gmail 点号变体</option>
      </select>
      <input type="number" id="aliasIdx" value="1" min="1" style="width:66px" onchange="updatePreview()" title="别名序号">
      <label style="font-size:13px;color:var(--muted)">出口 IP:</label>
      <select id="proxySel" onchange="toggleCustomProxy()">
        <option value="">主节点直连 (默认出口)</option>
        __EXTRA_PROXY_OPTION__
        <option value="custom">自定义 SOCKS5/HTTP 代理...</option>
      </select>
      <input type="text" id="customProxy" placeholder="socks5://user:pass@ip:port" style="display:none;width:220px">
      <button class="cyan" onclick="startMuseLogin()" style="margin-left:auto">🚀 启动登录 & 发送验证码</button>
    </div>
    <div style="margin-top:8px;font-size:13px;color:#38bdf8;font-family:monospace" id="previewEmail"></div>

    <div id="jobList"></div>
  </div>

  <!-- 方式二：快速批量粘贴 Cookie 上号 -->
  <div class="card">
    <h3 style="margin:0 0 8px 0">📋 方式二：一键批量粘贴 Cookie 上号（配合 Chrome 扩展或 F12 导出的 Cookie）</h3>
    <div class="row" style="align-items:stretch">
      <input type="text" id="cookieLabel" placeholder="账号标签（可选，如 muse-01）" style="width:220px">
      <input type="text" id="cookieProxy" placeholder="绑定代理（可选，如 socks5://egress-ip:18443）" style="width:320px">
      <button class="purple" onclick="importCookieHeader()">📥 一键导入 Cookie</button>
    </div>
    <textarea id="cookieText" rows="2" style="width:100%;margin-top:10px;font-family:monospace;font-size:12px" placeholder="粘贴原始 Cookie 请求头（例如 hatch_sess=xxx; hatch_vml=yyy）或 EditThisCookie JSON..."></textarea>
  </div>

  <!-- 号池监控表格 -->
  <div class="card">
    <div style="display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:10px">
      <h3 style="margin:0">📦 当前 Meta Muse 多模态号池（文本 <code>muse-spark</code> + 原图 <code>muse-image</code> + 视频 <code>muse-video</code>）</h3>
      <div class="row">
        <button class="secondary sm" onclick="keepaliveAll()">💓 一键全池保活 & 唤醒 VM</button>
        <button class="secondary sm" onclick="loadAccounts()">🔄 刷新列表</button>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th>标签 / ID</th>
          <th>状态</th>
          <th>绑定出口代理</th>
          <th>本周剩余额度</th>
          <th>Cookie 有效期</th>
          <th>备注 / 诊断</th>
          <th>操作</th>
        </tr>
      </thead>
      <tbody id="accountTable">
        <tr><td colspan="7" style="text-align:center;color:var(--muted)">请输入右上角 MUSE2API_KEY 加载号池</td></tr>
      </tbody>
    </table>
  </div>
</div>

<script>
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.get('key')) {
  localStorage.setItem('muse_admin_key', urlParams.get('key'));
}
document.getElementById('adminKey').value = localStorage.getItem('muse_admin_key') || '';
if (localStorage.getItem('muse_base_email')) {
  document.getElementById('baseEmail').value = localStorage.getItem('muse_base_email');
}
if (localStorage.getItem('muse_alias_idx')) {
  document.getElementById('aliasIdx').value = localStorage.getItem('muse_alias_idx');
}

function getKey() {
  return document.getElementById('adminKey').value.trim();
}
function saveKeyAndLoad() {
  localStorage.setItem('muse_admin_key', getKey());
  loadAccounts();
}
function toggleCustomProxy() {
  const v = document.getElementById('proxySel').value;
  document.getElementById('customProxy').style.display = v === 'custom' ? 'inline-block' : 'none';
}
function getSelectedProxy() {
  const v = document.getElementById('proxySel').value;
  return v === 'custom' ? document.getElementById('customProxy').value.trim() : v;
}
function buildEmail(base, mode, idx) {
  base = (base || '').trim();
  if (!base.includes('@') || mode === 'raw') return base;
  const [rawLocal, domain] = base.split('@');
  const local = rawLocal.split('+')[0];
  if (mode === 'dot' && local.length > 2) {
    const clean = local.replace(/\\./g, '');
    let out = '';
    for (let i = 0; i < clean.length; i++) {
      out += clean[i];
      if (i < clean.length - 1 && ((idx >> i) & 1)) out += '.';
    }
    if (out === clean) out = clean[0] + '.' + clean.slice(1);
    return out + '@' + domain;
  }
  return local + '+m' + String(idx).padStart(2, '0') + '@' + domain;
}
function updatePreview() {
  const base = document.getElementById('baseEmail').value.trim();
  const mode = document.getElementById('aliasMode').value;
  const idx = parseInt(document.getElementById('aliasIdx').value || '1', 10);
  const em = buildEmail(base, mode, idx);
  document.getElementById('previewEmail').textContent = em ? ('本次登录邮箱预览：' + em) : '';
}

const activeJobs = {};

async function startMuseLogin() {
  const key = getKey();
  if (!key) return alert('请先在右上角填入 MUSE2API_KEY');
  const base = document.getElementById('baseEmail').value.trim();
  if (!base || !base.includes('@')) return alert('请输入合法邮箱');
  const mode = document.getElementById('aliasMode').value;
  const idx = parseInt(document.getElementById('aliasIdx').value || '1', 10);
  const email = buildEmail(base, mode, idx);
  const proxy = getSelectedProxy();

  localStorage.setItem('muse_admin_key', key);
  localStorage.setItem('muse_base_email', base);
  if (mode !== 'raw') {
    document.getElementById('aliasIdx').value = String(idx + 1);
    localStorage.setItem('muse_alias_idx', String(idx + 1));
    updatePreview();
  }

  try {
    const r = await fetch('/muse-autoreg/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, label: email, proxy, admin_key: key })
    });
    const d = await r.json();
    if (!r.ok) return alert(d.detail || '启动失败');
    activeJobs[d.job_id] = d;
    renderJobs();
    pollJob(d.job_id);
  } catch (e) {
    alert('请求异常: ' + e);
  }
}

async function pollJob(jobId) {
  const timer = setInterval(async () => {
    if (!activeJobs[jobId]) { clearInterval(timer); return; }
    try {
      const r = await fetch('/muse-autoreg/status/' + jobId);
      if (!r.ok) { clearInterval(timer); return; }
      const d = await r.json();
      const prevStatus = activeJobs[jobId].status;
      activeJobs[jobId] = d;
      renderJobs();
      if (d.status === 'done') {
        clearInterval(timer);
        if (prevStatus !== 'done') loadAccounts();
      } else if (d.status === 'error' || d.status === 'blocked') {
        clearInterval(timer);
      }
    } catch (e) {}
  }, 2000);
}

async function submitVerify(jobId) {
  const key = getKey();
  const codeEl = document.getElementById('code_' + jobId);
  const nameEl = document.getElementById('name_' + jobId);
  const code = codeEl ? codeEl.value.trim() : '';
  const extra_name = nameEl ? nameEl.value.trim() : '';
  const r = await fetch('/muse-autoreg/verify', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_id: jobId, code, extra_name, admin_key: key })
  });
  const d = await r.json();
  if (!r.ok) return alert(d.detail || '提交失败');
  activeJobs[jobId] = d;
  renderJobs();
  pollJob(jobId);
}

async function extractNow(jobId) {
  const r = await fetch('/muse-autoreg/extract', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_id: jobId, admin_key: getKey() })
  });
  const d = await r.json();
  if (d.job) activeJobs[jobId] = d.job;
  renderJobs();
  if (d.ok) loadAccounts();
  else alert(d.message || d.detail || '尚未检测到登录 Cookie');
}

async function refreshSnap(jobId) {
  const r = await fetch('/muse-autoreg/snap', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_id: jobId, admin_key: getKey() })
  });
  if (r.ok) {
    activeJobs[jobId] = await r.json();
    renderJobs();
  }
}

async function closeJob(jobId) {
  await fetch('/muse-autoreg/job/' + jobId, { method: 'DELETE' });
  delete activeJobs[jobId];
  renderJobs();
}

function renderJobs() {
  const box = document.getElementById('jobList');
  const list = Object.values(activeJobs).reverse();
  if (!list.length) { box.innerHTML = ''; return; }
  box.innerHTML = list.map(j => {
    const cls = j.status === 'done' ? 'task-item task-done' : (j.status === 'error' || j.status === 'blocked' ? 'task-item task-err' : 'task-item');
    const showInput = ['waiting_code', 'waiting_input'].includes(j.status);
    const oldCode = (document.getElementById('code_' + j.job_id) || {}).value || '';
    const oldName = (document.getElementById('name_' + j.job_id) || {}).value || '';
    return `
      <div class="${cls}">
        <div class="auto-grid">
          <div>
            <div class="row" style="justify-content:space-between;margin-bottom:8px">
              <strong>📧 ${j.email} <span style="font-size:12px;color:var(--muted)">(${j.proxy || '美西直连'})</span></strong>
              <span class="status-pill ${j.status==='done'?'status-ok':(j.status==='error'||j.status==='blocked'?'status-bad':'status-warn')}">${j.status}</span>
            </div>
            <div class="tip" style="color:${j.error?'#fca5a5':'#cbd5e1'};margin-bottom:10px">${j.message}</div>
            ${showInput ? `
              <div class="row" style="margin-bottom:8px">
                <input type="text" id="code_${j.job_id}" value="${oldCode.replace(/"/g,'&quot;')}" placeholder="${j.hint==='password'?'输入账号密码':'输入邮箱收到的验证码（或页面要求字段）'}" style="width:260px">
                <input type="text" id="name_${j.job_id}" value="${oldName.replace(/"/g,'&quot;')}" placeholder="可选：新号昵称" style="width:150px">
                <button class="success" onclick="submitVerify('${j.job_id}')">✅ 提交并自动入池</button>
              </div>
            ` : ''}
            <div class="row">
              ${j.status !== 'done' ? `<button class="secondary sm" onclick="refreshSnap('${j.job_id}')">🔄 刷新画面</button>` : ''}
              ${j.status !== 'done' ? `<button class="purple sm" onclick="extractNow('${j.job_id}')">🍪 立即提取 Cookie 入池</button>` : ''}
              <button class="danger" onclick="closeJob('${j.job_id}')">关闭会话</button>
            </div>
          </div>
          <div>
            ${j.screenshot ? `<img class="shot" src="${j.screenshot}" onclick="window.open(this.src)" title="点击放大实时画面">` : '<div class="tip" style="text-align:center;padding:40px 0;border:1px dashed var(--border);border-radius:8px">等待浏览器画面...</div>'}
          </div>
        </div>
      </div>
    `;
  }).join('');
}

async function importCookieHeader() {
  const key = getKey();
  const txt = document.getElementById('cookieText').value.trim();
  const label = document.getElementById('cookieLabel').value.trim();
  const proxy = document.getElementById('cookieProxy').value.trim();
  if (!key) return alert('请先填写 MUSE2API_KEY');
  if (!txt) return alert('请粘贴 Cookie 内容');
  let body = { label };
  if (proxy) body.proxy = { server: proxy };
  if (txt.startsWith('[') || txt.startsWith('{')) {
    try {
      const parsed = JSON.parse(txt);
      if (Array.isArray(parsed)) {
        const c = {}, e = {};
        parsed.forEach(item => { if (item.name) { c[item.name] = item.value; if (item.expirationDate) e[item.name] = Math.floor(item.expirationDate); } });
        body.cookies = c; body.expires = e;
      } else {
        body.cookies = parsed;
      }
    } catch { body.cookie_header = txt; }
  } else {
    body.cookie_header = txt;
  }
  const r = await fetch('/admin/accounts', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key },
    body: JSON.stringify(body)
  });
  const d = await r.json();
  if (!r.ok) return alert(d.detail || JSON.stringify(d));
  document.getElementById('cookieText').value = '';
  alert('导入成功！共导入 ' + d.count + ' 个账号' + (d.warning ? ('\\n注意：' + d.warning) : ''));
  loadAccounts();
}

async function loadAccounts() {
  const key = getKey();
  if (!key) return;
  try {
    const r = await fetch('/admin/accounts', { headers: { 'Authorization': 'Bearer ' + key } });
    if (r.status === 401) {
      document.getElementById('accountTable').innerHTML = '<tr><td colspan="7" style="text-align:center;color:#f87171">MUSE2API_KEY 无效 (401)</td></tr>';
      return;
    }
    const d = await r.json();
    const list = d.accounts || [];
    const st = d.stats || {};
    document.getElementById('poolCount').textContent = `总号数: ${list.length} | 健康: ${st.healthy || 0}`;
    if (!list.length) {
      document.getElementById('accountTable').innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted)">号池暂无账号，请用上方上号器登录或用 Chrome 扩展一键导入</td></tr>';
      return;
    }
    document.getElementById('accountTable').innerHTML = list.map(a => {
      const okBadge = a.ok === true ? '<span class="status-pill status-ok">可用</span>' : (a.ok === false ? '<span class="status-pill status-bad">异常</span>' : '<span class="status-pill status-warn">未检测</span>');
      const exp = a.expires_at ? new Date(a.expires_at * 1000).toLocaleString() : '未知';
      const proxyStr = (a.proxy && a.proxy.server) ? a.proxy.server : '美西直连';
      const q = a.quota ? `${a.quota.plan || ''} (${a.quota.remaining_pct ?? a.quota.used_pct ?? '?'}%)` : '未查';
      return `
        <tr>
          <td><strong>${a.label || a.id}</strong><div style="font-size:11px;color:var(--muted)">${a.id}</div></td>
          <td>${okBadge} ${a.enabled === false ? '<span class="status-pill status-bad">已禁用</span>' : ''}</td>
          <td style="font-family:monospace;font-size:12px">${proxyStr}</td>
          <td>${q}</td>
          <td style="font-size:12px">${exp}</td>
          <td style="font-size:12px;color:var(--muted);max-width:220px;overflow:hidden;text-overflow:ellipsis">${a.note || '-'}</td>
          <td class="row">
            <button class="secondary sm" onclick="testAcc('${a.id}')">测试</button>
            <button class="secondary sm" onclick="quotaAcc('${a.id}')">额度</button>
            <button class="danger" onclick="delAcc('${a.id}')">删</button>
          </td>
        </tr>
      `;
    }).join('');
  } catch (e) {}
}

async function testAcc(id) {
  const r = await fetch(`/admin/accounts/${id}/test`, { method: 'POST', headers: { 'Authorization': 'Bearer ' + getKey() } });
  const d = await r.json();
  alert(d.ok ? '✅ 账号会话测试通过！' : ('❌ 测试未通过: ' + (d.error || JSON.stringify(d))));
  loadAccounts();
}
async function quotaAcc(id) {
  const r = await fetch(`/admin/accounts/${id}/quota`, { method: 'POST', headers: { 'Authorization': 'Bearer ' + getKey() } });
  const d = await r.json();
  alert('额度查询结果: ' + JSON.stringify(d.quota || d, null, 2));
  loadAccounts();
}
async function delAcc(id) {
  if (!confirm('确认删除此 Muse 账号？')) return;
  await fetch(`/admin/accounts/${id}`, { method: 'DELETE', headers: { 'Authorization': 'Bearer ' + getKey() } });
  loadAccounts();
}
async function keepaliveAll() {
  const r = await fetch('/admin/accounts/keepalive?force=true', { method: 'POST', headers: { 'Authorization': 'Bearer ' + getKey() } });
  const d = await r.json();
  alert('全池保活完成: ' + JSON.stringify(d));
  loadAccounts();
}

updatePreview();
if (getKey()) loadAccounts();
</script>
</body>
</html>
"""
