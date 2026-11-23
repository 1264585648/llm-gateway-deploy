#!/usr/bin/env python3
"""
AWS Quick 本地全自动别名批量上号器（走本机干净 IP，彻底绕开云机房 IP 被 AWS TES 拦截）
用法：
  python scripts/quick_local_autoreg.py
只需输入基础 Gmail 邮箱 + 6 位邮箱验证码，自动完成注册并将号池 Token 入库到 OVH 网关！
"""

import os
import random
import string
import sys
import time
import requests
from playwright.sync_api import sync_playwright

GATEWAY_BASE = os.environ.get("QUICK_GATEWAY_URL", "https://model.that-is-ai.com").rstrip("/")
ADMIN_KEY = os.environ.get("QUICK_ADMIN_KEY", "")

FIRST_NAMES = ["Alex", "David", "Emma", "Sophia", "Daniel", "Olivia", "Michael", "Grace", "Lucas", "Chloe", "Ethan", "Hannah"]
LAST_NAMES = ["Miller", "Taylor", "Brooks", "Wilson", "Clark", "Lewis", "Walker", "Hall", "Young", "Allen", "King", "Wright"]


def random_name() -> str:
    return f"{random.choice(FIRST_NAMES)} {random.choice(LAST_NAMES)}"


def random_password() -> str:
    chars = string.ascii_letters + string.digits
    body = "".join(random.choices(chars, k=10))
    return f"AwsQ#{body}9!"


def generate_alias(base_email: str, mode: str, idx: int) -> str:
    base_email = base_email.strip()
    local, domain = base_email.split("@", 1)
    local = local.split("+")[0]
    if mode in ("dot", "dot_rand") and len(local) > 2:
        clean = local.replace(".", "")
        mask = random.randint(1, (1 << min(len(clean) - 1, 16)) - 1) if mode == "dot_rand" else idx
        out = []
        for i, ch in enumerate(clean):
            out.append(ch)
            if i < len(clean) - 1 and ((mask >> i) & 1):
                out.append(".")
        res = "".join(out)
        if res == clean:
            res = clean[0] + "." + clean[1:]
        return f"{res}@{domain}"
    elif mode == "plus":
        return f"{local}+q{idx:02d}@{domain}"
    return base_email


def click_any(page, selectors, timeout=4000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                loc.click()
                return True
        except Exception:
            continue
    return False


def human_type_any(page, selectors, text: str, timeout=8000) -> bool:
    for sel in selectors:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=timeout):
                loc.click()
                time.sleep(random.uniform(0.15, 0.35))
                loc.fill("")
                loc.press_sequentially(text, delay=random.randint(45, 90))
                return True
        except Exception:
            continue
    return False


def check_tes_or_error(page) -> str:
    try:
        body = page.inner_text("body").lower()
    except Exception:
        return ""
    if "captcha" in body or "puzzle" in body or "verify you are human" in body:
        return "触发人机验证 (CAPTCHA)，请在弹出的浏览器窗口中手动完成后继续，或稍后重试。"
    for sel in ['[role="alert"]', '.awsui-alert-error', '[data-testid*="error"]']:
        try:
            loc = page.locator(sel).first
            if loc.is_visible(timeout=300):
                txt = loc.inner_text().strip()
                if txt and len(txt) > 3 and "required" not in txt.lower():
                    return txt
        except Exception:
            continue
    return ""


def register_one(playwright, admin_key: str, email: str, proxy_url: str = "") -> bool:
    print(f"\n==========================================")
    print(f"🚀 开始注册并入库账号: {email}")
    print(f"==========================================")

    # 1. 向服务器申请 OAuth 登录槽位
    headers = {"X-Admin-Key": admin_key, "Content-Type": "application/json"}
    r = requests.post(f"{GATEWAY_BASE}/quick-admin/api/login/start", headers=headers, json={"count": 1}, timeout=15)
    if r.status_code != 200:
        print(f"❌ 申请登录槽位失败 ({r.status_code}): {r.text}")
        return False
    sess = r.json()["sessions"][0]
    session_id = sess["id"]
    discovery_url = sess["discovery_url"]

    launch_kwargs = {
        "headless": False,
        "args": ["--disable-blink-features=AutomationControlled"],
    }
    # 优先使用本机 Chrome，不存在则回退 Edge 或 Playwright 内置 Chromium
    for ch in ("chrome", "msedge", None):
        try:
            if ch:
                browser = playwright.chromium.launch(channel=ch, **launch_kwargs)
            else:
                browser = playwright.chromium.launch(**launch_kwargs)
            break
        except Exception:
            continue

    ctx_kwargs = {"viewport": {"width": 1100, "height": 760}, "locale": "en-US"}
    if proxy_url:
        ctx_kwargs["proxy"] = {"server": proxy_url}

    context = browser.new_context(**ctx_kwargs)
    context.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => undefined });")
    page = context.new_page()

    captured = {"url": None, "tes_blocked": False}

    def intercept_cb(route):
        captured["url"] = route.request.url
        route.fulfill(status=200, content_type="text/html", body="<h3>Callback Captured!</h3>")

    page.route("http://localhost:18080/**", intercept_cb)
    page.route("http://127.0.0.1:18080/**", intercept_cb)

    def on_resp(resp):
        if "api/send-otp" in resp.url and resp.status >= 400:
            try:
                txt = resp.text()
                print(f"⚠️ send-otp 返回 {resp.status}: {txt}")
                if "BLOCKED" in txt or "TES" in txt:
                    captured["tes_blocked"] = True
            except Exception:
                pass

    page.on("response", on_resp)

    try:
        name = random_name()
        password = random_password()

        print(f"🌐 正在打开 AWS 登录页 (姓名: {name})...")
        page.goto(discovery_url, wait_until="domcontentloaded", timeout=35000)
        page.wait_for_selector('input[placeholder*="username@example.com"], input[type="email"]', timeout=15000)
        time.sleep(1.0)
        click_any(page, ["button#awsccc-cb-btn-accept", "button:has-text('Accept')"], timeout=1000)

        human_type_any(page, ['input[placeholder*="username@example.com"]', 'input[type="email"]'], email)
        time.sleep(0.8)
        click_any(page, ['[data-testid="test-primary-button"]', 'button:has-text("Continue")', 'button[type="submit"]'])
        time.sleep(3.5)

        # 自动填写姓名并触发验证码发送
        for _ in range(4):
            click_any(page, ["button#awsccc-cb-btn-accept", "button:has-text('Accept')"], timeout=800)
            body_text = page.inner_text("body").lower()
            if "verification code" in body_text or "verify your email" in body_text or "6-digit" in body_text:
                break
            if page.locator('input[type="password"]:visible').count() > 0:
                print(f"⚠️ 邮箱 {email} 已注册过，直接输入已有密码或换下一个别名。")
                break
            inputs = page.locator('input[type="text"]:visible')
            if inputs.count() > 0:
                inp = inputs.first
                val = inp.input_value().strip()
                if val == "" or val == email:
                    inp.click()
                    inp.press_sequentially(name, delay=random.randint(50, 100))
                    time.sleep(1.0)
                    click_any(page, ['[data-testid="test-primary-button"]', 'button:has-text("Continue")', 'button[type="submit"]'])
                    time.sleep(4.0)
                    err = check_tes_or_error(page)
                    if err:
                        print(f"❌ AWS 页面提示错误: {err}")
                        if captured["tes_blocked"]:
                            print("💡 提示：当前出口 IP 被 AWS TES 拦截，请尝试切换代理/节点，或在浏览器内手动点一次试试。")
                        return False
            else:
                time.sleep(1.5)

        body_text = page.inner_text("body").lower()
        if "verification code" not in body_text and "verify your email" not in body_text and "6-digit" not in body_text:
            print("⚠️ 尚未自动到达验证码页，如浏览器弹窗需要人工确认请直接在窗口内操作，确认到达验证码页后继续。")

        code = input(f"\n📩 请输入 {email} 收到的 6 位数字验证码 (输入 skip 跳过): ").strip()
        if not code or code.lower() == "skip":
            return False

        human_type_any(page, ['input[placeholder*="digit"]', 'input[type="text"]:visible', 'input[type="tel"]:visible'], code)
        time.sleep(0.6)
        click_any(page, ['button:has-text("Verify")', 'button:has-text("Continue")', '[data-testid="test-primary-button"]', 'button[type="submit"]'])
        time.sleep(3.5)

        # 自动设置强密码
        pwd_boxes = page.locator('input[type="password"]:visible')
        for _ in range(8):
            if pwd_boxes.count() >= 1:
                break
            time.sleep(1.2)

        if pwd_boxes.count() >= 1:
            print(f"🔑 正在自动设置密码: {password}")
            pwd_boxes.nth(0).click()
            pwd_boxes.nth(0).press_sequentially(password, delay=random.randint(35, 70))
            if pwd_boxes.count() >= 2:
                pwd_boxes.nth(1).click()
                pwd_boxes.nth(1).press_sequentially(password, delay=random.randint(35, 70))
            time.sleep(0.6)
            click_any(page, ['button:has-text("Create AWS Builder ID")', 'button:has-text("Continue")', '[data-testid="test-primary-button"]', 'button[type="submit"]'])

        print("⏳ 正在等待第一步 Discovery 回调（自动点击 Allow / Continue）...")
        for _ in range(35):
            if captured["url"] and "nonce=" in captured["url"]:
                break
            click_any(
                page,
                ['button:has-text("Allow")', 'button:has-text("Allow access")', 'button:has-text("Continue")', 'button:has-text("Get started")', 'button:has-text("Create")', '[data-testid="test-primary-button"]', 'button[type="submit"]'],
                timeout=1000,
            )
            time.sleep(1.5)

        if not captured["url"] or "nonce=" not in captured["url"]:
            print("❌ 等待第一步 Discovery 回调超时")
            return False

        cb1 = captured["url"]
        captured["url"] = None
        r1 = requests.post(
            f"{GATEWAY_BASE}/quick-admin/api/login/callback",
            headers=headers,
            json={"session_id": session_id, "callback_url": cb1},
            timeout=15,
        )
        if r1.status_code != 200:
            print(f"❌ 提交第一步回调失败: {r1.text}")
            return False

        auth_url = r1.json()["session"]["authorize_url"]
        print("🔐 正在自动完成第二步 OIDC 最终授权...")
        page.goto(auth_url, wait_until="domcontentloaded", timeout=30000)

        for _ in range(25):
            if captured["url"] and "code=" in captured["url"]:
                break
            click_any(
                page,
                ['button:has-text("Allow")', 'button:has-text("Allow access")', 'button:has-text("Authorize")', 'button:has-text("Continue")', '#cli_login_button', '[data-testid="allow-access-button"]', 'button[type="submit"]'],
                timeout=1000,
            )
            time.sleep(1.5)

        if not captured["url"] or "code=" not in captured["url"]:
            print("❌ 等待第二步 Code 回调超时")
            return False

        cb2 = captured["url"]
        r2 = requests.post(
            f"{GATEWAY_BASE}/quick-admin/api/login/callback",
            headers=headers,
            json={"session_id": session_id, "callback_url": cb2},
            timeout=15,
        )
        if r2.status_code != 200:
            print(f"❌ 提交第二步回调失败: {r2.text}")
            return False

        sess_final = r2.json()["session"]
        acct_id = sess_final.get("account_id", 0)
        print(f"🎉 入库成功！账号 #{acct_id}: {email} | 密码: {password} 已加入线上号池！")

        with open("quick_aliases.local.txt", "a", encoding="utf-8") as f:
            f.write(f"#{acct_id}\t{email}\t{password}\t{name}\n")
        return True

    finally:
        try:
            context.close()
            browser.close()
        except Exception:
            pass


def main():
    print("=== AWS Quick 本机干净 IP 别名上号器 ===")
    admin_key = ADMIN_KEY or input("请输入管理口令 (Admin Key): ").strip()
    base_email = input("请输入基础 Gmail 邮箱 (例如 rahafsunm7md7@gmail.com): ").strip()
    mode = (input("别名模式 [dot_rand(随机点号,默认) / dot(顺序点号) / plus(+号后缀) / raw]: ").strip() or "dot_rand").lower()
    start_idx = int(input("起始序号 (默认 1): ").strip() or "1")
    count = int(input("连续注册数量 (默认 3): ").strip() or "3")
    raw_proxy = input("代理池(支持逗号分隔多个,每次随机挑1个; 默认本机 Clash http://127.0.0.1:7890, 填 none 则不用代理): ").strip()
    if raw_proxy.lower() == "none":
        proxy_pool = []
    elif raw_proxy == "":
        proxy_pool = ["http://127.0.0.1:7890"]
    else:
        proxy_pool = [p.strip() for p in raw_proxy.replace(";", ",").split(",") if p.strip()]

    with sync_playwright() as p:
        for i in range(count):
            idx = start_idx + i
            email = generate_alias(base_email, mode, idx)
            chosen_proxy = random.choice(proxy_pool) if proxy_pool else ""
            if chosen_proxy:
                print(f"🌐 本轮随机分配代理: {chosen_proxy}")
            ok = register_one(p, admin_key, email, proxy_url=chosen_proxy)
            if not ok:
                cont = input("本轮未完成，是否继续下一个？(y/N): ").strip().lower()
                if cont != "y":
                    break


if __name__ == "__main__":
    main()
