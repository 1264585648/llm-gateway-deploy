import asyncio
import base64
import hashlib
import hmac
import os
import re
import struct
import time
import urllib.parse
import uuid
from typing import Any, Dict, List, Optional

import requests
from fastapi import APIRouter, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from playwright.async_api import BrowserContext, Page

MUSE2API_URL = os.environ.get("MUSE2API_URL", "http://muse2api:18610")
DEFAULT_EGRESS_PROXY = os.environ.get("MUSE_DEFAULT_PROXY", "").strip()
router = APIRouter()


def _normalize_totp_secret(raw: str) -> str:
    """支持裸 Base32 密钥（含空格/横杠）或 otpauth://totp/...?secret=XXX 格式。"""
    s = (raw or "").strip()
    if not s:
        return ""
    if s.lower().startswith("otpauth://"):
        parsed = urllib.parse.urlsplit(s)
        q = urllib.parse.parse_qs(parsed.query)
        s = (q.get("secret") or [""])[0].strip()
    s = re.sub(r"[\s\-]+", "", s).upper()
    return s


def _generate_totp(secret_raw: str, digits: int = 6, interval: int = 30) -> str:
    """标准 RFC 6238 TOTP 生成器（纯标准库实现）；若用户输入的是纯 6~8 位数字则直接原样返回。"""
    s = _normalize_totp_secret(secret_raw)
    if not s:
        raise ValueError("2FA 密钥为空")
    if s.isdigit() and 4 <= len(s) <= 8:
        return s
    # Pad Base32 to multiple of 8
    pad_len = (-len(s)) % 8
    try:
        key = base64.b32decode(s + ("=" * pad_len), casefold=True)
    except Exception as exc:
        raise ValueError(f"2FA 密钥不是合法的 Base32 字符串: {exc}") from exc
    counter = int(time.time()) // interval
    msg = struct.pack(">Q", counter)
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code_int = (struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF) % (10 ** digits)
    return str(code_int).zfill(digits)


async def _fresh_totp(secret_raw: str) -> str:
    """若当前 30 秒 TOTP 窗口剩余不足 3 秒，自动等待进入下一个窗口防验证码途中过期。"""
    s = _normalize_totp_secret(secret_raw)
    if not (s.isdigit() and 4 <= len(s) <= 8):
        rem = 30 - (int(time.time()) % 30)
        if rem <= 3:
            await asyncio.sleep(rem + 0.6)
    return _generate_totp(secret_raw)


class MuseJobState:
    def __init__(
        self,
        job_id: str,
        email: str,
        password: str,
        totp_secret: str,
        label: str,
        proxy_url: str,
        admin_key: str,
    ):
        self.job_id = job_id
        self.email = email
        self.password = password
        self.totp_secret = _normalize_totp_secret(totp_secret)
        self.label = label or email
        self.proxy_url = (proxy_url or "").strip()
        self.admin_key = admin_key
        # queued | starting | logging_in | waiting_code | waiting_input | verifying | blocked | done | error
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
# Limit concurrent headless login contexts to 2 so Chromium RAM stays low & clean
_LOGIN_SEM = asyncio.Semaphore(2)


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
    stale = [jid for jid, j in muse_jobs.items() if now - j.created_at > 3600]
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
        "security puzzle", "not a robot", "confirm you're human",
        "confirm you are human", "人机验证", "安全验证"
    )
    if any(k in body for k in captcha_kw):
        return "Muse/Meta 页面触发了人机验证（CAPTCHA）。遵循安全规范已停止自动化，请查看右侧截图并改用本地浏览器登录后通过 Chrome 扩展导入。"
    if "not available in your country" in body or "not available in your region" in body or "地区不可用" in body:
        return "当前出口地区受限，请切换至美区直连或自定义美区 SOCKS5/HTTP 出口代理后重试。"
    if "credit card" in body or "add a payment method" in body or ("billing" in body and "card number" in body):
        return "页面要求绑定支付方式/信用卡验证，已停止自动化，请查看右侧截图并在本地浏览器完成。"
    if "account has been locked" in body or "account has been disabled" in body or "suspended" in body and "appeal" in body:
        return "该账号在 Meta 侧处于锁定或风控状态，请查看右侧截图。"
    return None


async def _detect_error_banner(page: Page) -> Optional[str]:
    for sel in ('[role="alert"]', '[aria-live="assertive"]', '[data-testid*="error" i]', '.error_box'):
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=250):
                txt = (await loc.inner_text()).strip()
                if txt and len(txt) > 3 and "required" not in txt.lower():
                    return txt[:200]
        except Exception:
            continue
    return None


async def _click_any(page: Page, selectors, timeout=1500) -> bool:
    for sel in selectors:
        try:
            locs = page.locator(sel)
            cnt = await locs.count()
            if cnt == 0:
                continue
            for i in range(min(cnt, 4)):
                loc = locs.nth(i)
                if await loc.is_visible(timeout=min(timeout, 400)):
                    await loc.click()
                    return True
        except Exception:
            continue
    return False


async def _fill_any(page: Page, selectors, text: str, timeout=3000) -> bool:
    for sel in selectors:
        try:
            locs = page.locator(sel)
            cnt = await locs.count()
            if cnt == 0:
                continue
            for i in range(min(cnt, 4)):
                loc = locs.nth(i)
                if await loc.is_visible(timeout=min(timeout, 600)):
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
        f"🎉 自动登录并入池成功！账号 {job.label}（ID: {job.account_id}，提取 {job.cookie_count} 条 Cookie，"
        f"含核心 HttpOnly hatch_sess）已加入 Meta Muse 号池！"
    )
    await _snap(job)
    await _cleanup_job(job)
    return True


async def _get_shared_browser():
    import server as srv
    return await srv.get_browser()


async def _fill_code_or_text(page: Page, value: str, prefer_code: bool = False) -> bool:
    val = value.strip()
    split_boxes = page.locator('input[maxlength="1"]:visible')
    if 4 <= await split_boxes.count() <= 8 and val.isdigit():
        await split_boxes.first.click()
        await page.keyboard.type(val, delay=65)
        return True

    if not prefer_code:
        pwd = page.locator('input[type="password"]:visible')
        if await pwd.count() > 0:
            await pwd.first.click()
            await pwd.first.fill(val)
            return True

    return await _fill_any(
        page,
        [
            'input[autocomplete="one-time-code"]',
            'input[name="approvals_code"]',
            'input[name*="code" i]',
            'input[inputmode="numeric"]',
            'input[placeholder*="code" i]',
            'input[placeholder*="otp" i]',
            'input[aria-label*="code" i]',
            'input[type="tel"]',
            'input[type="number"]',
            'input[type="text"]',
        ],
        val,
        timeout=4000,
    )


async def _submit_form_button(page: Page) -> bool:
    clicked = await _click_any(
        page,
        [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("Next")',
            '[role="button"]:has-text("Next")',
            'button:has-text("Log in")',
            '[role="button"]:has-text("Log in")',
            'button:has-text("Log In")',
            '[role="button"]:has-text("Log In")',
            'button:has-text("Continue")',
            '[role="button"]:has-text("Continue")',
            'button:has-text("Verify")',
            '[role="button"]:has-text("Verify")',
            'button:has-text("Submit")',
            '[role="button"]:has-text("Submit")',
            'button:has-text("Confirm")',
            '[role="button"]:has-text("Confirm")',
            'button:has-text("登录")',
            '[role="button"]:has-text("登录")',
            'button:has-text("继续")',
            '[role="button"]:has-text("继续")',
            'button:has-text("下一步")',
            '[role="button"]:has-text("下一步")',
            'button:has-text("确认")',
            '[role="button"]:has-text("确认")',
        ],
        timeout=1200,
    )
    if not clicked:
        try:
            await page.keyboard.press("Enter")
            return True
        except Exception:
            return False
    return clicked


async def _drive_auto_login(job: MuseJobState):
    async with _LOGIN_SEM:
        async with job.lock:
            try:
                job.status = "starting"
                job.message = f"正在启动浏览器并打开 https://muse.ai/（账号 {job.email}）..."
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

                await page.goto("https://muse.ai/", wait_until="domcontentloaded", timeout=35000)
                await asyncio.sleep(3.0)
                await _snap(job)

                if blk := await _detect_block(page):
                    job.status = "blocked"
                    job.error = blk
                    job.message = blk
                    return

                # Step 1: Fill email / mobile / username on muse.ai landing page
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

                await asyncio.sleep(0.35)
                await _submit_form_button(page)
                job.status = "logging_in"
                job.message = f"已提交账号 {job.email}，正在自动推进密码与 2FA 校验..."
                await asyncio.sleep(4.0)
                await _snap(job)

                # Step 2: Intelligent state machine loop for Password -> 2FA (TOTP) -> Trust Device -> Cookie Extraction
                pwd_attempts = 0
                totp_attempts = 0
                switched_2fa_method = False

                for step_i in range(18):
                    if await _extract_and_import_cookies(job):
                        return

                    if blk := await _detect_block(page):
                        job.status = "blocked"
                        job.error = blk
                        job.message = blk
                        return

                    try:
                        body = (await page.inner_text("body")).lower()
                    except Exception:
                        body = ""

                    # Handle Meta/Facebook cookie consent banners if any
                    await _click_any(
                        page,
                        [
                            'button:has-text("Allow all cookies")',
                            'button:has-text("Accept all")',
                            'button[data-cookiebanner="accept_button"]',
                        ],
                        timeout=400,
                    )

                    # Case A: Password field visible
                    pwd_loc = page.locator('input[type="password"]:visible')
                    if await pwd_loc.count() > 0:
                        if not job.password:
                            job.status = "waiting_code"
                            job.hint = "password"
                            job.message = f"页面要求输入密码（{job.email}），因未预先填写密码，请在下方输入并提交。"
                            await _snap(job)
                            return

                        if pwd_attempts >= 2:
                            err_b = await _detect_error_banner(page)
                            job.status = "error"
                            job.error = f"密码提交后未能通过（{err_b or '请检查账号密码是否正确，查看右侧截图'}）"
                            job.message = job.error
                            await _snap(job)
                            return

                        # If Facebook/Meta login page ALSO shows an empty email box alongside password, fill it too
                        email_box = page.locator('input[name="email"]:visible, input[type="email"]:visible').first
                        try:
                            if await email_box.is_visible(timeout=250) and not (await email_box.input_value()).strip():
                                await email_box.fill(job.email)
                        except Exception:
                            pass

                        job.message = f"检测到密码输入页，正在自动填入密码并登录（第 {pwd_attempts + 1} 次）..."
                        await pwd_loc.first.click()
                        await pwd_loc.first.fill(job.password)
                        pwd_attempts += 1
                        await _snap(job)
                        await asyncio.sleep(0.35)
                        await _submit_form_button(page)
                        await asyncio.sleep(4.0)
                        await _snap(job)
                        continue

                    # Case B: Intermediate choice page asking "Log in with password" / "Continue with Facebook"
                    if job.password and pwd_attempts == 0:
                        clicked_pwd_choice = await _click_any(
                            page,
                            [
                                'button:has-text("Log in with password")',
                                'button:has-text("Use password")',
                                'a:has-text("Log in with password")',
                                'button:has-text("Enter password")',
                                'button:has-text("使用密码登录")',
                            ],
                            timeout=500,
                        )
                        if clicked_pwd_choice:
                            job.message = "已选择「使用密码登录」，正在加载密码框..."
                            await asyncio.sleep(2.5)
                            await _snap(job)
                            continue

                    # Case C0: Meta "Confirm your Meta Account" -> select Third-party Authentication App -> click Next
                    is_meta_method_select = (
                        "confirm your meta account" in body
                        or "please select a method" in body
                        or ("third-party authentication app" in body and "recovery code" in body)
                    )
                    if is_meta_method_select:
                        job.message = "检测到「Confirm your Meta Account」验证方式选择页，自动选择 Authentication App 并点击 Next..."
                        await _click_any(
                            page,
                            [
                                'label:has-text("Third-party Authentication App")',
                                'div[role="radio"]:has-text("Authentication App")',
                                'span:has-text("Third-party Authentication App")',
                                'span:has-text("Authentication App")',
                            ],
                            timeout=1200,
                        )
                        await asyncio.sleep(0.4)
                        await _submit_form_button(page)
                        await asyncio.sleep(3.5)
                        await _snap(job)
                        continue

                    # Case C: Meta 2FA defaulted to "Check your notifications on another device" -> switch to Authenticator App
                    is_other_device_2fa = (
                        job.totp_secret
                        and not switched_2fa_method
                        and (
                            "check your notifications on another device" in body
                            or "waiting for approval" in body
                            or "check your whatsapp" in body
                            or "text message (sms)" in body
                            or "在其他设备上查看通知" in body
                        )
                    )
                    if is_other_device_2fa:
                        job.message = "检测到默认设备通知审批，正在自动切换至「身份验证器 App (2FA)」..."
                        clicked_try = await _click_any(
                            page,
                            [
                                'button:has-text("Try another way")',
                                'a:has-text("Try another way")',
                                'div[role="button"]:has-text("Try another way")',
                                'button:has-text("尝试其他方式")',
                                'button:has-text("Other ways")',
                            ],
                            timeout=1800,
                        )
                        if clicked_try:
                            switched_2fa_method = True
                            await asyncio.sleep(2.0)
                            await _snap(job)
                            await _click_any(
                                page,
                                [
                                    'label:has-text("Authentication app")',
                                    'div[role="radio"]:has-text("Authentication app")',
                                    'span:has-text("Authentication app")',
                                    'span:has-text("Authenticator app")',
                                    'span:has-text("身份验证应用")',
                                ],
                                timeout=2000,
                            )
                            await asyncio.sleep(0.5)
                            await _submit_form_button(page)
                            await asyncio.sleep(3.0)
                            await _snap(job)
                            continue

                    # Case D: 2FA / OTP Code input screen
                    is_code_screen = any(
                        kw in body
                        for kw in (
                            "authentication app", "authenticator", "login code",
                            "two-factor", "6-digit", "verification code",
                            "security code", "enter code", "enter the code",
                            "check your email", "we sent a code",
                            "双重验证", "身份验证", "验证码",
                        )
                    )
                    code_input_present = (
                        await page.locator(
                            'input[maxlength="1"]:visible, input[autocomplete="one-time-code"]:visible, '
                            'input[name="approvals_code"]:visible, input[name*="code" i]:visible, '
                            'input[inputmode="numeric"]:visible, input[placeholder*="code" i]:visible, '
                            'input[type="tel"]:visible, input[type="number"]:visible, input[type="text"]:visible'
                        ).count() > 0
                    )

                    if is_code_screen and code_input_present:
                        # Distinguish between 2FA TOTP app code vs Email OTP when user only supplied email (no password/2fa)
                        is_email_otp = ("check your email" in body or "sent to your email" in body or "邮箱" in body) and not (
                            "authentication app" in body or "authenticator" in body or "two-factor" in body
                        )
                        if job.totp_secret and not is_email_otp:
                            if totp_attempts >= 2:
                                err_b = await _detect_error_banner(page)
                                job.status = "waiting_code"
                                job.hint = "otp"
                                job.message = f"自动填入 2FA 验证码后未通过（{err_b or '请核对 2FA 密钥或查看右侧截图手动输入'}）"
                                await _snap(job)
                                return

                            totp_code = await _fresh_totp(job.totp_secret)
                            job.message = f"检测到 2FA 验证页，已实时计算 6 位 TOTP 验证码 ({totp_code}) 并自动提交..."
                            filled_code = await _fill_code_or_text(page, totp_code, prefer_code=True)
                            totp_attempts += 1
                            await _snap(job)
                            if filled_code:
                                await asyncio.sleep(0.4)
                                await _submit_form_button(page)
                                await asyncio.sleep(4.5)
                                await _snap(job)
                                continue
                        else:
                            job.status = "waiting_code"
                            job.hint = "otp"
                            job.message = f"页面需要输入{'邮箱验证码' if is_email_otp else '2FA 验证码'}（{job.email}），请查看右侧截图并在下方提交。"
                            await _snap(job)
                            return

                    # Case E: Post-2FA "Trust this device" / "Save browser" / "Continue as ..." / "Next" / "I agree"
                    visible_inputs = page.locator(
                        'input[type="text"]:visible, input[type="password"]:visible, '
                        'input[type="email"]:visible, input[type="tel"]:visible, input[inputmode="numeric"]:visible'
                    )
                    if await visible_inputs.count() == 0:
                        clicked_post = await _click_any(
                            page,
                            [
                                'button:has-text("Trust this device")',
                                '[role="button"]:has-text("Trust this device")',
                                'button:has-text("Always confirm")',
                                '[role="button"]:has-text("Always confirm")',
                                'button:has-text("Save browser")',
                                '[role="button"]:has-text("Save browser")',
                                'button:has-text("Remember browser")',
                                '[role="button"]:has-text("Remember browser")',
                                'button:has-text("Continue as")',
                                '[role="button"]:has-text("Continue as")',
                                'button:has-text("Next")',
                                '[role="button"]:has-text("Next")',
                                'button:has-text("Continue")',
                                '[role="button"]:has-text("Continue")',
                                'button:has-text("I agree")',
                                '[role="button"]:has-text("I agree")',
                                'button:has-text("Agree")',
                                '[role="button"]:has-text("Agree")',
                                'button:has-text("Accept")',
                                '[role="button"]:has-text("Accept")',
                                'button:has-text("Get started")',
                                '[role="button"]:has-text("Get started")',
                                'button:has-text("Skip")',
                                '[role="button"]:has-text("Skip")',
                                'button:has-text("信任此设备")',
                                '[role="button"]:has-text("信任此设备")',
                                'button:has-text("继续")',
                                '[role="button"]:has-text("继续")',
                                'button:has-text("下一步")',
                                '[role="button"]:has-text("下一步")',
                            ],
                            timeout=500,
                        )
                        if clicked_post:
                            job.message = "正在确认设备信任与工作区授权跳转..."
                            await asyncio.sleep(3.5)
                            await _snap(job)
                            continue

                    # If we're on a non-muse domain (e.g. facebook.com/meta.com) and already authenticated, navigate back to muse.ai once
                    if step_i in (10, 14) and "muse.ai" not in page.url.lower():
                        await asyncio.sleep(2.0)
                    else:
                        await asyncio.sleep(2.0)
                    await _snap(job)

                # Reached end of auto loop without hatch_sess -> hand over gracefully to interactive mode
                err_b = await _detect_error_banner(page)
                job.status = "waiting_input"
                job.message = (
                    f"自动推进暂停（{err_b or '当前页面需人工确认或额外输入'}）。"
                    "请查看右侧实时画面：可输入页面所需内容点提交，或若已进首页可直接点「🍪 立即提取 Cookie 入池」。"
                )
                await _snap(job)
            except Exception as exc:
                job.status = "error"
                job.error = f"自动登录异常: {exc}"
                job.message = job.error
                await _snap(job)


async def _drive_manual_verify(job: MuseJobState, code: str, extra_name: str = ""):
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
            job.message = "正在提交人工输入的凭证/验证码..."

            if code.strip():
                ok = await _fill_code_or_text(page, code)
                await _snap(job)
                if not ok:
                    job.status = "waiting_input"
                    job.message = "当前页面未找到输入框，请查看右侧截图（若已登录可点「立即提取 Cookie 入池」）。"
                    return
                await asyncio.sleep(0.35)
                await _submit_form_button(page)

            for _ in range(12):
                await asyncio.sleep(2.2)
                await _snap(job)

                if await _extract_and_import_cookies(job):
                    return

                if blk := await _detect_block(page):
                    job.status = "blocked"
                    job.error = blk
                    job.message = blk
                    return

                if extra_name.strip():
                    name_inp = page.locator('input[placeholder*="name" i]:visible, input[name*="name" i]:visible').first
                    try:
                        if await name_inp.is_visible(timeout=200) and not (await name_inp.input_value()).strip():
                            await name_inp.fill(extra_name.strip())
                            await _submit_form_button(page)
                            continue
                    except Exception:
                        pass

                # If a 2FA screen appears AFTER manual password entry and user supplied totp_secret, auto-fill it!
                body = (await page.inner_text("body")).lower()
                if job.totp_secret and any(k in body for k in ("authentication app", "authenticator", "two-factor", "login code", "6-digit")):
                    totp_code = await _fresh_totp(job.totp_secret)
                    if await _fill_code_or_text(page, totp_code, prefer_code=True):
                        job.message = f"检测到 2FA 验证页，已自动填入 TOTP ({totp_code})..."
                        await _submit_form_button(page)
                        await asyncio.sleep(3.0)
                        continue

                visible_inputs = page.locator(
                    'input[type="text"]:visible, input[type="password"]:visible, '
                    'input[type="email"]:visible, input[type="tel"]:visible, input[inputmode="numeric"]:visible'
                )
                if await visible_inputs.count() == 0:
                    await _click_any(
                        page,
                        [
                            'button:has-text("Trust this device")',
                            'button:has-text("Continue as")',
                            'button:has-text("I agree")',
                            'button:has-text("Agree")',
                            'button:has-text("Accept")',
                            'button:has-text("Continue")',
                            'button:has-text("Get started")',
                            'button:has-text("Skip")',
                        ],
                        timeout=600,
                    )

            err_b = await _detect_error_banner(page)
            job.status = "waiting_input"
            job.message = (
                f"页面提示：{err_b}。请根据右侧最新截图继续输入或重试。"
                if err_b
                else "尚未检测到核心 Cookie（hatch_sess），请查看右侧画面继续提交或点「立即提取 Cookie」。"
            )
        except Exception as exc:
            job.status = "error"
            job.error = f"提交验证异常: {exc}"
            job.message = job.error
            await _snap(job)


class MuseStartReq(BaseModel):
    email: str
    password: Optional[str] = ""
    totp_secret: Optional[str] = ""
    label: Optional[str] = ""
    proxy: Optional[str] = ""
    admin_key: str


class MuseBatchItem(BaseModel):
    email: str
    password: Optional[str] = ""
    totp_secret: Optional[str] = ""
    label: Optional[str] = ""
    proxy: Optional[str] = ""


class MuseBatchStartReq(BaseModel):
    items: List[MuseBatchItem]
    admin_key: str


class MuseVerifyReq(BaseModel):
    job_id: str
    code: Optional[str] = ""
    extra_name: Optional[str] = ""
    admin_key: str


class MuseActionReq(BaseModel):
    job_id: str
    admin_key: str


class TotpPreviewReq(BaseModel):
    secret: str


@router.post("/muse-autoreg/totp-preview")
async def muse_totp_preview(req: TotpPreviewReq):
    try:
        code = _generate_totp(req.secret)
        rem = 30 - (int(time.time()) % 30)
        return {"ok": True, "code": code, "remaining": rem}
    except Exception as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.post("/muse-autoreg/start")
async def muse_start(req: MuseStartReq):
    _prune_old_jobs()
    _verify_muse_admin_key(req.admin_key)
    email = (req.email or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="请输入账号/邮箱")
    if req.totp_secret:
        try:
            _generate_totp(req.totp_secret)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
    if req.proxy:
        try:
            _parse_playwright_proxy(req.proxy)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    job_id = uuid.uuid4().hex[:12]
    label = (req.label or "").strip() or email
    job = MuseJobState(
        job_id,
        email,
        (req.password or "").strip(),
        req.totp_secret or "",
        label,
        req.proxy or "",
        req.admin_key,
    )
    muse_jobs[job_id] = job
    asyncio.create_task(_drive_auto_login(job))
    return _job_dict(job)


@router.post("/muse-autoreg/batch-start")
async def muse_batch_start(req: MuseBatchStartReq):
    _prune_old_jobs()
    _verify_muse_admin_key(req.admin_key)
    if not req.items:
        raise HTTPException(status_code=400, detail="批量列表为空")
    if len(req.items) > 30:
        raise HTTPException(status_code=400, detail="单次批量上号最多支持 30 个账号")

    created = []
    for item in req.items:
        email = (item.email or "").strip()
        if not email:
            continue
        if item.totp_secret:
            _generate_totp(item.totp_secret)
        if item.proxy:
            _parse_playwright_proxy(item.proxy)
        job_id = uuid.uuid4().hex[:12]
        job = MuseJobState(
            job_id,
            email,
            (item.password or "").strip(),
            item.totp_secret or "",
            (item.label or "").strip() or email,
            item.proxy or "",
            req.admin_key,
        )
        job.status = "queued"
        job.message = "已加入自动登录队列，等待空闲浏览器槽位..."
        muse_jobs[job_id] = job
        asyncio.create_task(_drive_auto_login(job))
        created.append(_job_dict(job))
    return {"count": len(created), "jobs": created}


@router.post("/muse-autoreg/verify")
async def muse_verify(req: MuseVerifyReq):
    _verify_muse_admin_key(req.admin_key)
    job = muse_jobs.get(req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="登录会话不存在或已过期")
    asyncio.create_task(_drive_manual_verify(job, req.code or "", req.extra_name or ""))
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
        "has_password": bool(job.password),
        "has_2fa": bool(job.totp_secret),
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
    html = MUSE_LOGIN_HTML.replace("__EXTRA_PROXY_OPTION__", extra_opt).replace(
        "__DEFAULT_EGRESS_JS__", DEFAULT_EGRESS_PROXY
    )
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


MUSE_LOGIN_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Meta Muse 账号密码+2FA 全自动上号器 & 号池管理</title>
<style>
  :root { --bg: #0f172a; --card: #1e293b; --border: #334155; --text: #f8fafc; --muted: #94a3b8; --primary: #3b82f6; --success: #22c55e; --danger: #ef4444; --warn: #f59e0b; --purple: #8b5cf6; --cyan: #06b6d4; }
  * { box-sizing: border-box; }
  body { margin: 0; padding: 24px; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background: var(--bg); color: var(--text); }
  .container { max-width: 1220px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }
  .title { font-size: 22px; font-weight: 700; display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
  .badge { font-size: 12px; padding: 4px 10px; border-radius: 999px; background: rgba(6,182,212,0.18); color: #22d3ee; font-weight: 600; }
  .card { background: var(--card); border: 1px solid var(--border); border-radius: 12px; padding: 20px; margin-bottom: 20px; }
  .row { display: flex; gap: 10px; align-items: center; flex-wrap: wrap; }
  .tabs { display: flex; gap: 8px; margin-bottom: 14px; }
  .tab { background: #0b1120; border: 1px solid var(--border); color: var(--muted); padding: 8px 16px; border-radius: 8px; font-size: 14px; font-weight: 600; cursor: pointer; }
  .tab.active { background: rgba(6,182,212,0.16); border-color: var(--cyan); color: #67e8f9; }
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
      🎨 Meta Muse 全自动上号器（账号 + 密码 + 2FA 自动登录）
      <span class="badge" id="poolCount">加载中...</span>
    </div>
    <div class="row">
      <a class="btn secondary" href="/admin" target="_blank">🖥️ 完整运维台 & 媒体库</a>
      <a class="btn secondary" href="/admin/extension" download>🧩 Chrome 导号插件</a>
      <input type="password" id="adminKey" placeholder="输入 MUSE2API_KEY" style="width:230px">
      <button onclick="saveKeyAndLoad()">验证 / 刷新</button>
    </div>
  </div>

  <!-- 方式一：账号密码 + 2FA 全自动登录（支持单号 & 批量多行排队） -->
  <div class="card" style="border-color:#06b6d4">
    <div class="tabs">
      <div class="tab active" id="tabSingle" onclick="switchTab('single')">🔐 单号自动登录（账号 + 密码 + 2FA）</div>
      <div class="tab" id="tabBatch" onclick="switchTab('batch')">📦 批量排队自动上号（多行粘贴 账号----密码----2FA）</div>
    </div>

    <!-- 单号模式 -->
    <div id="paneSingle">
      <div class="tip" style="margin-bottom:12px">
        填入 <b>账号/邮箱 + 密码 + 2FA 密钥（Base32 或 <code>otpauth://</code>）</b> 后点启动，服务器自动完成填邮箱 → 填密码 → 实时计算 6 位 TOTP 2FA 验证码 → 自动信任设备 → 提取 <code>HttpOnly hatch_sess</code> 写入号池！若不填密码/2FA，则自动退回邮箱验证码交互模式。
      </div>
      <div class="row" style="background:#0b1120;padding:14px;border-radius:10px;border:1px solid var(--border)">
        <input type="text" id="singleEmail" placeholder="账号 / 邮箱 (必填)" style="width:220px">
        <input type="password" id="singlePwd" placeholder="密码 (选填，自动填密)" style="width:180px">
        <input type="text" id="singleTotp" placeholder="2FA Base32 密钥 (选填，自动算码)" style="width:240px" oninput="previewTotp()">
        <input type="text" id="singleLabel" placeholder="备注标签 (选填)" style="width:140px">
        <select id="proxySel" onchange="toggleCustomProxy()">
          <option value="">主节点直连 (默认出口)</option>
          __EXTRA_PROXY_OPTION__
          <option value="custom">自定义代理...</option>
        </select>
        <input type="text" id="customProxy" placeholder="socks5://user:pass@ip:port" style="display:none;width:200px">
        <button class="cyan" onclick="startSingleLogin()" style="margin-left:auto">🚀 一键自动登录入池</button>
      </div>
      <div id="totpLive" style="margin-top:8px;font-size:13px;color:#4ade80;font-family:monospace"></div>
    </div>

    <!-- 批量模式 -->
    <div id="paneBatch" style="display:none">
      <div class="tip" style="margin-bottom:10px">
        每行一个账号，支持 <code>----</code>、<code>|</code>、<code>,</code>、<code>Tab</code> 或空格分隔，格式：<code>账号----密码----2FA密钥</code>（也支持第 4 列填自定义备注标签）。系统按 2 并发排队自动跑完并逐个写入号池！
      </div>
      <textarea id="batchLines" rows="5" style="width:100%;font-family:monospace;font-size:13px" placeholder="example1@gmail.com----MyPassword123----JBSWY3DPEHPK3PXP&#10;example2@gmail.com----MyPassword456----NB2W45DFOIZA====----muse02"></textarea>
      <div class="row" style="margin-top:10px">
        <label style="font-size:13px;color:var(--muted)">批量出口分配:</label>
        <select id="batchProxyMode">
          <option value="direct">全部走主节点直连</option>
          <option value="egress">全部走独立出口代理</option>
          <option value="alternate">自动交替分配（美西直连 / 美东出口轮流分流）</option>
        </select>
        <button class="cyan" onclick="startBatchLogin()" style="margin-left:auto">🚀 启动批量排队自动上号</button>
      </div>
    </div>

    <div id="jobList"></div>
  </div>

  <!-- 方式二：快捷批量粘贴 Cookie -->
  <div class="card">
    <h3 style="margin:0 0 8px 0">📋 方式二：直接粘贴 Cookie 上号（兼容 Network Cookie 头 / EditThisCookie JSON）</h3>
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
      <h3 style="margin:0">📦 当前 Meta Muse 多模态号池（<code>muse-spark</code> + <code>muse-image</code> + <code>muse-video</code>）</h3>
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
const DEFAULT_EGRESS = "__DEFAULT_EGRESS_JS__";
const urlParams = new URLSearchParams(window.location.search);
if (urlParams.get('key')) {
  localStorage.setItem('muse_admin_key', urlParams.get('key'));
}
document.getElementById('adminKey').value = localStorage.getItem('muse_admin_key') || '';

function getKey() { return document.getElementById('adminKey').value.trim(); }
function saveKeyAndLoad() { localStorage.setItem('muse_admin_key', getKey()); loadAccounts(); }
function switchTab(t) {
  document.getElementById('tabSingle').classList.toggle('active', t === 'single');
  document.getElementById('tabBatch').classList.toggle('active', t === 'batch');
  document.getElementById('paneSingle').style.display = t === 'single' ? 'block' : 'none';
  document.getElementById('paneBatch').style.display = t === 'batch' ? 'block' : 'none';
}
function toggleCustomProxy() {
  document.getElementById('customProxy').style.display = document.getElementById('proxySel').value === 'custom' ? 'inline-block' : 'none';
}
function getSelectedProxy() {
  const v = document.getElementById('proxySel').value;
  return v === 'custom' ? document.getElementById('customProxy').value.trim() : v;
}

let totpTimer = null;
async function previewTotp() {
  clearTimeout(totpTimer);
  const el = document.getElementById('totpLive');
  const sec = document.getElementById('singleTotp').value.trim();
  if (!sec) { el.textContent = ''; return; }
  totpTimer = setTimeout(async () => {
    try {
      const r = await fetch('/muse-autoreg/totp-preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ secret: sec })
      });
      const d = await r.json();
      if (r.ok) {
        el.style.color = '#4ade80';
        el.textContent = `✅ 2FA 密钥有效 · 当前实时动态码：${d.code}（剩余 ${d.remaining}s）`;
      } else {
        el.style.color = '#f87171';
        el.textContent = '⚠️ ' + (d.detail || '2FA 密钥格式无效');
      }
    } catch (e) {}
  }, 250);
}

const activeJobs = {};

async function startSingleLogin() {
  const key = getKey();
  if (!key) return alert('请先在右上角填入 MUSE2API_KEY');
  const email = document.getElementById('singleEmail').value.trim();
  if (!email) return alert('请输入账号或邮箱');
  const password = document.getElementById('singlePwd').value;
  const totp_secret = document.getElementById('singleTotp').value.trim();
  const label = document.getElementById('singleLabel').value.trim() || email;
  const proxy = getSelectedProxy();
  localStorage.setItem('muse_admin_key', key);

  const r = await fetch('/muse-autoreg/start', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password, totp_secret, label, proxy, admin_key: key })
  });
  const d = await r.json();
  if (!r.ok) return alert(d.detail || '启动失败');
  activeJobs[d.job_id] = d;
  renderJobs();
  pollJob(d.job_id);
}

function parseAccountLine(line) {
  const s = line.trim();
  if (!s || s.startsWith('#')) return null;
  let parts;
  if (s.includes('----')) parts = s.split('----');
  else if (s.includes('|')) parts = s.split('|');
  else if (s.includes('\t')) parts = s.split('\t');
  else if (s.includes(',')) parts = s.split(',');
  else parts = s.split(/\\s+/);
  parts = parts.map(x => x.trim()).filter((_, idx) => idx < 4);
  if (!parts[0]) return null;
  return {
    email: parts[0],
    password: parts[1] || '',
    totp_secret: parts[2] || '',
    label: parts[3] || parts[0],
  };
}

async function startBatchLogin() {
  const key = getKey();
  if (!key) return alert('请先在右上角填入 MUSE2API_KEY');
  const raw = document.getElementById('batchLines').value;
  const mode = document.getElementById('batchProxyMode').value;
  const parsed = raw.split(/\\r?\\n/).map(parseAccountLine).filter(Boolean);
  if (!parsed.length) return alert('未解析到有效账号行，请按「账号----密码----2FA」格式每行粘贴一个');
  parsed.forEach((item, i) => {
    if (mode === 'egress' && DEFAULT_EGRESS) item.proxy = DEFAULT_EGRESS;
    else if (mode === 'alternate' && DEFAULT_EGRESS && i % 2 === 1) item.proxy = DEFAULT_EGRESS;
    else item.proxy = '';
  });

  const r = await fetch('/muse-autoreg/batch-start', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ items: parsed, admin_key: key })
  });
  const d = await r.json();
  if (!r.ok) return alert(d.detail || '批量启动失败');
  (d.jobs || []).forEach(j => {
    activeJobs[j.job_id] = j;
    pollJob(j.job_id);
  });
  renderJobs();
}

async function pollJob(jobId) {
  const timer = setInterval(async () => {
    if (!activeJobs[jobId]) { clearInterval(timer); return; }
    try {
      const r = await fetch('/muse-autoreg/status/' + jobId);
      if (!r.ok) { clearInterval(timer); return; }
      const d = await r.json();
      const prev = activeJobs[jobId].status;
      activeJobs[jobId] = d;
      renderJobs();
      if (d.status === 'done') {
        clearInterval(timer);
        if (prev !== 'done') loadAccounts();
      } else if (d.status === 'error' || d.status === 'blocked') {
        clearInterval(timer);
      }
    } catch (e) {}
  }, 2000);
}

async function submitVerify(jobId) {
  const code = (document.getElementById('code_' + jobId) || {}).value || '';
  const extra_name = (document.getElementById('name_' + jobId) || {}).value || '';
  const r = await fetch('/muse-autoreg/verify', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_id: jobId, code: code.trim(), extra_name: extra_name.trim(), admin_key: getKey() })
  });
  const d = await r.json();
  if (!r.ok) return alert(d.detail || '提交失败');
  activeJobs[jobId] = d;
  renderJobs();
  pollJob(jobId);
}

async function extractNow(jobId) {
  const r = await fetch('/muse-autoreg/extract', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
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
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ job_id: jobId, admin_key: getKey() })
  });
  if (r.ok) { activeJobs[jobId] = await r.json(); renderJobs(); }
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
    const badges = `${j.has_password ? '🔑密码 ' : ''}${j.has_2fa ? '🛡️2FA ' : ''}${j.proxy || '主节点直连'}`;
    return `
      <div class="${cls}">
        <div class="auto-grid">
          <div>
            <div class="row" style="justify-content:space-between;margin-bottom:8px">
              <strong>📧 ${j.email} <span style="font-size:12px;color:var(--muted)">(${badges})</span></strong>
              <span class="status-pill ${j.status==='done'?'status-ok':(j.status==='error'||j.status==='blocked'?'status-bad':'status-warn')}">${j.status}</span>
            </div>
            <div class="tip" style="color:${j.error?'#fca5a5':'#cbd5e1'};margin-bottom:10px">${j.message}</div>
            ${showInput ? `
              <div class="row" style="margin-bottom:8px">
                <input type="text" id="code_${j.job_id}" value="${oldCode.replace(/"/g,'&quot;')}" placeholder="${j.hint==='password'?'输入账号密码':'输入验证码 / 页面要求字段'}" style="width:260px">
                <input type="text" id="name_${j.job_id}" value="${oldName.replace(/"/g,'&quot;')}" placeholder="可选：昵称" style="width:140px">
                <button class="success" onclick="submitVerify('${j.job_id}')">✅ 提交并继续</button>
              </div>
            ` : ''}
            <div class="row">
              ${j.status !== 'done' ? `<button class="secondary sm" onclick="refreshSnap('${j.job_id}')">🔄 刷新画面</button>` : ''}
              ${j.status !== 'done' ? `<button class="purple sm" onclick="extractNow('${j.job_id}')">🍪 立即提取 Cookie</button>` : ''}
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
      } else { body.cookies = parsed; }
    } catch { body.cookie_header = txt; }
  } else { body.cookie_header = txt; }
  const r = await fetch('/admin/accounts', {
    method: 'POST', headers: { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + key },
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
      document.getElementById('accountTable').innerHTML = '<tr><td colspan="7" style="text-align:center;color:var(--muted)">号池暂无账号</td></tr>';
      return;
    }
    document.getElementById('accountTable').innerHTML = list.map(a => {
      const okBadge = a.ok === true ? '<span class="status-pill status-ok">可用</span>' : (a.ok === false ? '<span class="status-pill status-bad">异常</span>' : '<span class="status-pill status-warn">未检测</span>');
      const exp = a.expires_at ? new Date(a.expires_at * 1000).toLocaleString() : '未知';
      const proxyStr = (a.proxy && a.proxy.server) ? a.proxy.server : '主节点直连';
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

if (getKey()) loadAccounts();
</script>
</body>
</html>
"""
