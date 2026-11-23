import asyncio
import base64
import os
import random
import string
import time
import uuid
import requests
from typing import Dict, Optional
from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

app = FastAPI(title="AWS Quick Automated Alias Register Engine")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

QUICKWORK_API = "http://quickwork2api:8080"

FIRST_NAMES = ["Alex", "David", "Emma", "Sophia", "Daniel", "Olivia", "Michael", "Grace", "Lucas", "Chloe", "Ethan", "Hannah"]
LAST_NAMES = ["Miller", "Taylor", "Brooks", "Wilson", "Clark", "Lewis", "Walker", "Hall", "Young", "Allen", "King", "Wright"]

STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
delete Object.getPrototypeOf(navigator).webdriver;
window.chrome = window.chrome || { runtime: {}, loadTimes: function(){}, csi: function(){}, app: {} };
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });
Object.defineProperty(navigator, 'plugins', { get: () => [1, 2, 3, 4, 5] });
Object.defineProperty(navigator, 'hardwareConcurrency', { get: () => 8 });
Object.defineProperty(navigator, 'deviceMemory', { get: () => 16 });
"""


def random_name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def random_password() -> str:
    chars = string.ascii_letters + string.digits
    body = "".join(random.choices(chars, k=10))
    return f"AwsQ#{body}9!"


class JobState:
    def __init__(self, job_id: str, email: str, name: str, password: str, admin_key: str, proxy: str = ""):
        self.job_id = job_id
        self.email = email
        self.name = name
        self.password = password
        self.admin_key = admin_key
        self.proxy = proxy
        self.status = "starting"
        self.message = "正在启动真指纹浏览器并初始化 AWS Quick 登录会话..."
        self.error = ""
        self.session_id = ""
        self.discovery_url = ""
        self.authorize_url = ""
        self.account_id = 0
        self.screenshot_b64 = ""
        self.created_at = time.time()
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.captured_callback: Optional[str] = None
        self.last_api_error: str = ""
        self.lock = asyncio.Lock()


jobs: Dict[str, JobState] = {}
playwright_instance = None
browser_instance: Optional[Browser] = None


async def get_browser() -> Browser:
    global playwright_instance, browser_instance
    if browser_instance is None or not browser_instance.is_connected():
        playwright_instance = await async_playwright().start()
        use_headed = bool(os.environ.get("DISPLAY"))
        browser_instance = await playwright_instance.chromium.launch(
            headless=not use_headed,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
                "--disable-infobars",
                "--window-size=1280,800",
            ],
        )
    return browser_instance


async def snap(job: JobState):
    if job.page:
        try:
            png = await job.page.screenshot(type="jpeg", quality=65)
            job.screenshot_b64 = "data:image/jpeg;base64," + base64.b64encode(png).decode()
        except Exception:
            pass


async def cleanup_job(job: JobState):
    if job.context:
        try:
            await job.context.close()
        except Exception:
            pass
        job.context = None
        job.page = None


def prune_old_jobs():
    now = time.time()
    stale = [jid for jid, j in jobs.items() if now - j.created_at > 1800]
    for jid in stale:
        j = jobs.pop(jid, None)
        if j and j.context:
            asyncio.create_task(cleanup_job(j))


async def detect_block_or_error(page: Page) -> Optional[str]:
    try:
        body = (await page.inner_text("body")).lower()
    except Exception:
        return None
    if "captcha" in body or "puzzle" in body or "verify you are human" in body or "human verification" in body:
        return "AWS 页面触发了人机验证/CAPTCHA，为遵守安全策略已停止自动化流程，请改用浏览器手动上号或切换代理/稍后再试。"
    for sel in ['[role="alert"]', '.awsui-alert-error', '[data-testid*="error"]']:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=300):
                txt = (await loc.inner_text()).strip()
                if txt and len(txt) > 3 and "required" not in txt.lower():
                    return f"AWS 页面提示错误: {txt[:180]}"
        except Exception:
            continue
    return None


class StartRequest(BaseModel):
    email: str
    name: Optional[str] = ""
    password: Optional[str] = ""
    proxy: Optional[str] = ""
    admin_key: str


class VerifyRequest(BaseModel):
    job_id: str
    code: str
    admin_key: str


@app.get("/quick-autoreg/healthz")
async def healthz():
    return {
        "status": "ok",
        "service": "quick-autoreg",
        "display": os.environ.get("DISPLAY", ""),
        "active_jobs": len(jobs),
    }


@app.post("/quick-autoreg/start")
async def start_register(req: StartRequest):
    prune_old_jobs()
    email = req.email.strip()
    if "@" not in email:
        raise HTTPException(status_code=400, detail="请输入合法的邮箱地址")

    try:
        r = requests.post(
            f"{QUICKWORK_API}/quick-admin/api/login/start",
            headers={"X-Admin-Key": req.admin_key, "Content-Type": "application/json"},
            json={"count": 1},
            timeout=10,
        )
        if r.status_code != 200:
            raise HTTPException(status_code=r.status_code, detail=f"鉴权失败或 quickwork2api 异常: {r.text}")
        sess_data = r.json()["sessions"][0]
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"连接 quickwork2api 失败: {e}")

    job_id = uuid.uuid4().hex[:12]
    name = (req.name or "").strip() or random_name()
    password = (req.password or "").strip() or random_password()
    proxy = (req.proxy or "").strip()

    job = JobState(job_id, email, name, password, req.admin_key, proxy=proxy)
    job.session_id = sess_data["id"]
    job.discovery_url = sess_data["discovery_url"]
    jobs[job_id] = job

    asyncio.create_task(drive_step1_send_code(job))
    return {
        "job_id": job_id,
        "email": email,
        "name": name,
        "password": password,
        "status": job.status,
        "message": job.message,
    }


async def human_click(page: Page, selectors, timeout=5000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout):
                box = await loc.bounding_box()
                if box:
                    await page.mouse.move(
                        box["x"] + box["width"] * random.uniform(0.3, 0.7),
                        box["y"] + box["height"] * random.uniform(0.3, 0.7),
                        steps=random.randint(6, 14),
                    )
                    await asyncio.sleep(random.uniform(0.1, 0.3))
                await loc.click()
                return True
        except Exception:
            continue
    return False


async def human_type_any(page: Page, selectors, text: str, timeout=8000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if await loc.is_visible(timeout=timeout):
                box = await loc.bounding_box()
                if box:
                    await page.mouse.move(
                        box["x"] + box["width"] * random.uniform(0.3, 0.7),
                        box["y"] + box["height"] * random.uniform(0.3, 0.7),
                        steps=random.randint(5, 12),
                    )
                await loc.click()
                await asyncio.sleep(random.uniform(0.15, 0.35))
                await loc.fill("")
                await loc.press_sequentially(text, delay=random.randint(45, 95))
                return True
        except Exception:
            continue
    return False


async def drive_step1_send_code(job: JobState):
    async with job.lock:
        try:
            browser = await get_browser()
            ctx_kwargs = {
                "viewport": {"width": 1280, "height": 800},
                "locale": "en-US",
                "timezone_id": "America/Los_Angeles",
                "user_agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            }
            if job.proxy:
                ctx_kwargs["proxy"] = {"server": job.proxy}

            context = await browser.new_context(**ctx_kwargs)
            await context.add_init_script(STEALTH_JS)
            page = await context.new_page()
            job.context = context
            job.page = page

            async def intercept_callback(route):
                job.captured_callback = route.request.url
                await route.fulfill(status=200, content_type="text/html", body="<html><body>Callback Captured</body></html>")

            await page.route("http://localhost:18080/**", intercept_callback)
            await page.route("http://127.0.0.1:18080/**", intercept_callback)

            async def log_sso_resp(resp):
                if ("sso.signin.aws" in resp.url or "profile.aws.amazon.com/api/send-otp" in resp.url) and resp.status >= 400:
                    try:
                        txt = await resp.text()
                        print(f"[autoreg {job.job_id}] {resp.status} {resp.url} -> {txt[:300]}", flush=True)
                        if "BLOCKED" in txt and "TES" in txt:
                            job.last_api_error = "AWS TES 拦截了当前服务器机房 IP (Request was blocked by TES)。请在上方填入住宅代理，或在本机运行 python scripts/quick_local_autoreg.py！"
                    except Exception:
                        pass

            page.on("response", log_sso_resp)

            job.status = "sending_code"
            job.message = f"正在打开 AWS Quick 登录页并填入邮箱 {job.email}..."
            await page.goto(job.discovery_url, wait_until="domcontentloaded", timeout=35000)

            try:
                await page.wait_for_selector('input[placeholder*="username@example.com"], input[type="email"]', timeout=15000)
            except Exception:
                pass

            await asyncio.sleep(random.uniform(1.0, 2.0))
            await human_click(page, ["button#awsccc-cb-btn-accept", "button:has-text('Accept')"], timeout=1200)

            filled = await human_type_any(
                page,
                [
                    'input[placeholder*="username@example.com"]',
                    'input[type="email"]',
                    'input[name="email"]',
                    'input[type="text"]',
                ],
                job.email,
                timeout=10000,
            )
            await snap(job)
            if not filled:
                job.status = "error"
                job.error = "未找到邮箱输入框，请查看右侧实时截图"
                job.message = job.error
                return

            await asyncio.sleep(random.uniform(0.8, 1.5))
            await human_click(
                page,
                [
                    '[data-testid="test-primary-button"]',
                    'button[type="submit"]',
                    'button:has-text("Continue")',
                    'button:has-text("继续")',
                ],
                timeout=5000,
            )

            job.message = "已提交邮箱，正在等待跳转至姓名/验证码页面..."
            await asyncio.sleep(4.0)
            await snap(job)

            if err_msg := await detect_block_or_error(page):
                job.status = "error"
                job.error = err_msg
                job.message = err_msg
                return

            for step_try in range(4):
                await human_click(page, ["button#awsccc-cb-btn-accept", "button:has-text('Accept')"], timeout=800)

                body_text = (await page.inner_text("body")).lower()
                if "verification code" in body_text or "verify your email" in body_text or "6-digit" in body_text or "验证码" in body_text:
                    break

                pwd_loc = page.locator('input[type="password"]:visible')
                if await pwd_loc.count() > 0:
                    job.status = "error"
                    job.error = f"邮箱 {job.email} 已经注册过 AWS Builder ID，请换一个新别名序号！"
                    job.message = job.error
                    return

                inputs = page.locator('input[type="text"]:visible')
                if await inputs.count() > 0:
                    inp = inputs.first
                    val = await inp.input_value()
                    if val.strip() == "" or val.strip() == job.email or "sorry" in body_text:
                        await inp.click()
                        await asyncio.sleep(0.3)
                        await inp.fill("")
                        await inp.press_sequentially(job.name, delay=random.randint(55, 110))
                        await snap(job)
                        await asyncio.sleep(random.uniform(1.2, 2.2))
                        await human_click(
                            page,
                            [
                                '[data-testid="test-primary-button"]',
                                'button[type="submit"]',
                                'button:has-text("Continue")',
                                'button:has-text("继续")',
                            ],
                            timeout=5000,
                        )
                        await asyncio.sleep(4.5)
                        await snap(job)
                        if job.last_api_error:
                            job.status = "error"
                            job.error = job.last_api_error
                            job.message = job.last_api_error
                            return
                        if err_msg := await detect_block_or_error(page):
                            if step_try == 0 and "ERR-837" in err_msg:
                                print(f"[autoreg {job.job_id}] Got ERR-837 on name submit, retrying after pause...", flush=True)
                                await asyncio.sleep(3)
                                continue
                            job.status = "error"
                            job.error = err_msg
                            job.message = err_msg
                            return
                else:
                    await asyncio.sleep(1.5)

            body_text = (await page.inner_text("body")).lower()
            if "verification code" not in body_text and "verify your email" not in body_text and "6-digit" not in body_text:
                job.status = "error"
                job.error = "未能到达验证码输入页面，请查看右侧实时截图"
                job.message = job.error
                return

            job.status = "waiting_code"
            job.message = f"📩 验证码已发送至你的主 Gmail 收件箱（目标别名: {job.email}）！请输入 6 位验证码："
            await snap(job)

        except Exception as e:
            job.status = "error"
            job.error = f"发送验证码阶段异常: {e}"
            job.message = job.error
            await snap(job)


@app.post("/quick-autoreg/verify")
async def verify_code(req: VerifyRequest):
    job = jobs.get(req.job_id)
    if not job:
        raise HTTPException(status_code=404, detail="找不到该注册任务或已过期")
    if req.admin_key != job.admin_key:
        raise HTTPException(status_code=401, detail="管理口令不匹配")
    code = req.code.strip()
    if not code:
        raise HTTPException(status_code=400, detail="请输入 6 位验证码")

    asyncio.create_task(drive_step2_verify_and_complete(job, code))
    return {"job_id": job.job_id, "status": "verifying", "message": "正在填入验证码并自动完成后续密码设置、租户开通与两步 OAuth 回调..."}


async def drive_step2_verify_and_complete(job: JobState, code: str):
    async with job.lock:
        page = job.page
        if not page:
            job.status = "error"
            job.error = "浏览器会话已关闭"
            job.message = job.error
            return

        try:
            job.status = "verifying"
            job.message = f"正在填入验证码 {code}..."

            filled = await human_type_any(
                page,
                [
                    'input[placeholder*="digit"]',
                    'input[type="text"]:visible',
                    'input[type="tel"]:visible',
                    'input[type="number"]:visible',
                ],
                code,
                timeout=8000,
            )
            await snap(job)
            if not filled:
                job.status = "error"
                job.error = "未找到验证码输入框，请查看截图"
                job.message = job.error
                return

            await asyncio.sleep(0.8)
            await human_click(
                page,
                [
                    'button:has-text("Verify")',
                    'button:has-text("Continue")',
                    '[data-testid="test-primary-button"]',
                    'button[type="submit"]',
                ],
                timeout=5000,
            )

            job.message = f"验证码已提交，正在自动设置强密码 ({job.password})..."
            await asyncio.sleep(3.5)
            await snap(job)

            if err_msg := await detect_block_or_error(page):
                job.status = "error"
                job.error = err_msg
                job.message = err_msg
                return

            pwd_boxes = page.locator('input[type="password"]:visible')
            for _ in range(8):
                if await pwd_boxes.count() >= 1:
                    break
                await asyncio.sleep(1.5)

            cnt = await pwd_boxes.count()
            if cnt >= 1:
                await pwd_boxes.nth(0).click()
                await pwd_boxes.nth(0).press_sequentially(job.password, delay=random.randint(35, 70))
                await asyncio.sleep(0.4)
                if cnt >= 2:
                    await pwd_boxes.nth(1).click()
                    await pwd_boxes.nth(1).press_sequentially(job.password, delay=random.randint(35, 70))
                await snap(job)
                await asyncio.sleep(0.8)
                await human_click(
                    page,
                    [
                        'button:has-text("Create AWS Builder ID")',
                        'button:has-text("Continue")',
                        '[data-testid="test-primary-button"]',
                        'button[type="submit"]',
                    ],
                    timeout=6000,
                )

            job.status = "authorizing"
            job.message = "密码设置成功！正在等待 AWS Quick 首次开通工作区与第一步 Discovery 回调..."

            for _ in range(30):
                if job.captured_callback and "nonce=" in job.captured_callback:
                    break
                await snap(job)
                if err_msg := await detect_block_or_error(page):
                    job.status = "error"
                    job.error = err_msg
                    job.message = err_msg
                    return
                await human_click(
                    page,
                    [
                        'button:has-text("Allow")',
                        'button:has-text("Allow access")',
                        'button:has-text("Continue")',
                        'button:has-text("Get started")',
                        'button:has-text("Create")',
                        '[data-testid="test-primary-button"]',
                        'button[type="submit"]',
                    ],
                    timeout=1000,
                )
                await asyncio.sleep(2)

            if not job.captured_callback or "nonce=" not in job.captured_callback:
                await snap(job)
                job.status = "error"
                job.error = f"等待第一步 Discovery 回调超时 (当前 URL: {page.url})"
                job.message = job.error
                return

            cb1 = job.captured_callback
            job.captured_callback = None
            job.message = "已捕获第一步回调！正在自动换取第二步 OIDC 最终授权链接..."

            r1 = requests.post(
                f"{QUICKWORK_API}/quick-admin/api/login/callback",
                headers={"X-Admin-Key": job.admin_key, "Content-Type": "application/json"},
                json={"session_id": job.session_id, "callback_url": cb1},
                timeout=15,
            )
            if r1.status_code != 200:
                job.status = "error"
                job.error = f"提交第一步回调失败: {r1.text}"
                job.message = job.error
                return

            auth_url = r1.json()["session"]["authorize_url"]
            job.authorize_url = auth_url
            job.message = "正在自动完成第二步 OIDC 授权确认..."

            await page.goto(auth_url, wait_until="domcontentloaded", timeout=30000)
            for _ in range(20):
                if job.captured_callback and "code=" in job.captured_callback:
                    break
                await snap(job)
                await human_click(
                    page,
                    [
                        'button:has-text("Allow")',
                        'button:has-text("Allow access")',
                        'button:has-text("Authorize")',
                        'button:has-text("Continue")',
                        '#cli_login_button',
                        '[data-testid="allow-access-button"]',
                        'button[type="submit"]',
                    ],
                    timeout=1000,
                )
                await asyncio.sleep(1.5)

            if not job.captured_callback or "code=" not in job.captured_callback:
                await snap(job)
                job.status = "error"
                job.error = f"等待第二步 Code 回调超时 (当前 URL: {page.url})"
                job.message = job.error
                return

            cb2 = job.captured_callback
            r2 = requests.post(
                f"{QUICKWORK_API}/quick-admin/api/login/callback",
                headers={"X-Admin-Key": job.admin_key, "Content-Type": "application/json"},
                json={"session_id": job.session_id, "callback_url": cb2},
                timeout=15,
            )
            if r2.status_code != 200:
                job.status = "error"
                job.error = f"提交第二步回调失败: {r2.text}"
                job.message = job.error
                return

            sess_final = r2.json()["session"]
            job.account_id = sess_final.get("account_id", 0)
            job.status = "done"
            job.message = f"🎉 全自动注册并入库成功！账号 #{job.account_id} ({job.email} / 密码: {job.password}) 已加入轮询池！"
            await cleanup_job(job)

        except Exception as e:
            job.status = "error"
            job.error = f"验证及入库阶段异常: {e}"
            job.message = job.error
            await snap(job)


@app.get("/quick-autoreg/status/{job_id}")
async def get_job_status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "job_id": job.job_id,
        "email": job.email,
        "name": job.name,
        "password": job.password,
        "status": job.status,
        "message": job.message,
        "error": job.error,
        "account_id": job.account_id,
        "screenshot": job.screenshot_b64,
    }

from muse_login import router as muse_router
app.include_router(muse_router)
