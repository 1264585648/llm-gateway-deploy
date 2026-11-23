#!/usr/bin/env bash
# =========================================================
# Meta Muse (muse.ai) 多模态 2API 一键拉取与双模增强补丁脚本
# 功能：
#   1. 拉取 czg86389-hub/muse2api 仓库（v1.6.12+，支持无水印原图与视频生成）
#   2. 增强 engine.py：支持 muse-spark 在 /v1/chat/completions 中自动上传多模态图片/附件实现识图对话
#   3. 增强 app.py：
#      - 新增 /v1/video/generations 单数路由别名（兼容 New API 视频中继）
#      - 新增 meta-muse-* / aws-muse-* 模型别名
#      - /v1/chat/completions 双模直出：选择 muse-image / muse-video 时自动调用生图/生视频引擎，
#        流式输出 SSE 进度与心跳防断流，完成后以 Markdown 直接渲染高清原图与 MP4 视频链接
#      - New API 渠道测速快速探活：对 "Hi"/"test"/"ping" 毫秒级返回 OK，不消耗生图/视频额度
# =========================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MUSE_DIR="${ROOT_DIR}/muse2api"

cd "$ROOT_DIR"
if [ ! -d "$MUSE_DIR" ]; then
  echo "[1/3] 克隆 czg86389-hub/muse2api ..."
  git clone https://github.com/czg86389-hub/muse2api.git "$MUSE_DIR"
else
  echo "[1/3] 目录 $MUSE_DIR 已存在，跳过克隆。"
fi

cd "$MUSE_DIR"
echo "[2/3] 应用中转站双模多模态增强补丁（engine.py & app.py）..."

python3 - << 'PYEOF'
# 1. Patch engine.py to support reference_images in chat_stream
with open("engine.py", "r", encoding="utf-8") as f:
    eng = f.read()

target_eng = """    def chat_stream(self, cookies: dict, prompt: str, expires: dict | None = None,
                    timeout: int | None = None, account_id: str | None = None,
                    stop_event=None):
        \"\"\"发一条消息，流式 yield 增量文本。\"\"\"
        timeout = int(timeout or getattr(self.cfg, "chat_timeout", 300))
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread(for_chat=True)
        base_agent, base_text, _ = self._poll_chat()
        self._send(prompt)"""

replace_eng = """    def chat_stream(self, cookies: dict, prompt: str, expires: dict | None = None,
                    timeout: int | None = None, account_id: str | None = None,
                    stop_event=None, reference_images: list[str] | None = None):
        \"\"\"发一条消息，流式 yield 增量文本（支持多模态识图/参考素材附件上传）。\"\"\"
        timeout = int(timeout or getattr(self.cfg, "chat_timeout", 300))
        self.ensure_page(cookies, expires, account_id=account_id)
        self.reset_thread(for_chat=True)
        refs = list(reference_images or [])
        upload = self._attach_images(refs) if refs else None
        if not refs:
            self._clear_attachments()
        try:
            yield from self._chat_stream_inner(prompt, timeout, stop_event)
        finally:
            if upload:
                shutil.rmtree(upload["dir"], ignore_errors=True)

    def _chat_stream_inner(self, prompt: str, timeout: int, stop_event=None):
        base_agent, base_text, _ = self._poll_chat()
        self._send(prompt)"""

if "_chat_stream_inner" not in eng:
    assert target_eng in eng, "target_eng not found in engine.py"
    eng = eng.replace(target_eng, replace_eng, 1)
    with open("engine.py", "w", encoding="utf-8") as f:
        f.write(eng)
    print("  - engine.py patched!")

# 2. Patch app.py
with open("app.py", "r", encoding="utf-8") as f:
    app_code = f.read()

if "meta-muse-spark" not in app_code:
    target_alias = '"muse-text": "muse-spark",'
    replace_alias = (
        '"meta-muse-spark": "muse-spark", "aws-muse-spark": "muse-spark", '
        '"meta-muse-image": "muse-image", "aws-muse-image": "muse-image", '
        '"meta-muse-video": "muse-video", "aws-muse-video": "muse-video", '
        '"muse-text": "muse-spark",'
    )
    assert target_alias in app_code, "target_alias not found"
    app_code = app_code.replace(target_alias, replace_alias, 1)

if '@app.post("/v1/video/generations")' not in app_code:
    app_code = app_code.replace(
        '@app.post("/v1/videos/generations")',
        '@app.post("/v1/videos/generations")\n@app.post("/v1/video/generations")',
        1
    )
    app_code = app_code.replace(
        '@app.get("/v1/videos/generations/{task_id}")',
        '@app.get("/v1/videos/generations/{task_id}")\n@app.get("/v1/video/generations/{task_id}")',
        1
    )

target_safe = """def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None):"""
replace_safe = """def safe_chat_stream(cookies: dict, prompt: str, expires: dict | None,
                     timeout: int, account_id: str | None,
                     reference_images: list[str] | None = None):"""
if "reference_images: list[str] | None = None):" not in app_code:
    assert target_safe in app_code, "target_safe not found"
    app_code = app_code.replace(target_safe, replace_safe, 1)
    app_code = app_code.replace(
        """                    for chunk in engine.chat_stream(
                        cur_cookies, prompt, cur_exp, timeout,
                        account_id=cur_id, stop_event=stop_event
                    ):""",
        """                    for chunk in engine.chat_stream(
                        cur_cookies, prompt, cur_exp, timeout,
                        account_id=cur_id, stop_event=stop_event,
                        reference_images=reference_images
                    ):""",
        1
    )

target_chat_start = """@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):"""

chat_enhancement = """def _extract_chat_references(messages: list[ChatMessage]) -> list[str]:
    \"\"\"从 OpenAI 多模态 messages 中提取所有附带的图片/素材 URL 或 Data URL。\"\"\"
    refs: list[str] = []
    for m in messages or []:
        c = m.content
        if isinstance(c, list):
            for item in c:
                if not isinstance(item, dict):
                    continue
                t = item.get("type")
                if t in ("image_url", "input_image", "image", "video_url", "audio_url", "file"):
                    val = item.get("image_url") or item.get("video_url") or item.get("audio_url") or item.get("file") or item.get("url")
                    if isinstance(val, dict):
                        val = val.get("url") or val.get("data")
                    if isinstance(val, str) and val.strip():
                        refs.append(val.strip())
    return refs


def _last_user_text(messages: list[ChatMessage]) -> str:
    for m in reversed(messages or []):
        if (m.role or "").strip().lower() == "user":
            txt = _content_text(m.content).replace("[图片]", "").strip()
            if txt:
                return txt
    return ""


@app.post("/v1/chat/completions")
async def chat_completions(req: ChatRequest, _=Depends(auth)):"""

if "def _extract_chat_references" not in app_code:
    assert target_chat_start in app_code, "target_chat_start not found"
    app_code = app_code.replace(target_chat_start, chat_enhancement, 1)

target_chat_body = """    model = resolve_model(req.model, default="muse-spark")
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")"""

replace_chat_body = """    model = resolve_model(req.model, default="muse-spark")
    raw_refs = _extract_chat_references(req.messages)
    last_user_prompt = _last_user_text(req.messages) or prompt.strip()
    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())

    # 1. 快速探活通道：New API 渠道测速发送 "Hi"/"test"/"ping"（或尚未导入 Cookie 时测速），
    #    对生图/生视频模型毫秒级返回 OK，既不白白消耗周额度，也避免渠道测速超时。
    is_health_probe = (
        not raw_refs
        and last_user_prompt.strip().lower() in ("hi", "hello", "ping", "test", "ok", "回复 ok 两个字母即可")
    )
    has_live_acc = store.pick_account(rotate=False) is not None
    if is_health_probe and (model in ("muse-image", "muse-video") or not has_live_acc):
        probe_reply = "OK"
        if req.stream:
            def _probe_stream():
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))
                yield _sse(_chat_chunk(cid, created, model, {"content": probe_reply}))
                yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                if _want_usage(req.stream_options):
                    yield _sse({"id": cid, "object": "chat.completion.chunk",
                                "created": created, "model": model, "choices": [],
                                "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}})
                yield "data: [DONE]\\n\\n"
            return StreamingResponse(_probe_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)
        return {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": probe_reply}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }

    # 2. 对话流式直出图片与视频兼容模式（Cherry Studio / NextChat / LobeChat / Playground 直接调用 muse-image / muse-video）
    if model in ("muse-image", "muse-video"):
        expect_kind = "video" if model == "muse-video" else "image"
        prepared_refs = _prepare_reference_images(raw_refs) if raw_refs else []
        ref_kinds = _reference_kinds(prepared_refs)
        clean_prompt = last_user_prompt or "生成高质量作品"
        if expect_kind == "image":
            gen_prompt = build_image_prompt(ImageRequest(prompt=clean_prompt, images=raw_refs or None), ref_kinds)
            gen_timeout = int(req.timeout or CFG.image_timeout)
        else:
            gen_prompt = build_video_prompt(VideoRequest(prompt=clean_prompt, images=raw_refs or None), ref_kinds)
            gen_timeout = int(req.timeout or CFG.video_timeout)

        def _format_media_markdown(res: dict) -> str:
            url = media_url(res["filename"])
            if expect_kind == "image":
                wh = f"{res.get('w')}x{res.get('h')}" if res.get("w") and res.get("h") else "高清原图"
                return f"![{clean_prompt[:40]}]({url})\\n\\n✅ **Muse Image 生成完成**（规格：`{wh}`）  \\n🔗 [点击打开 / 下载无水印原图]({url})"
            thumb_url = f"{url}/thumbnail"
            return (
                f"[![Muse Video Preview]({thumb_url})]({url})\\n\\n"
                f"✅ **Muse Video 视频生成完成！**  \\n"
                f"🎬 [▶️ 点击在线播放 / 下载高清 MP4 视频]({url})"
            )

        if req.stream:
            def _media_chat_stream():
                import queue as _q
                ev_q = _q.Queue()
                last_pct = [0]

                def _on_prog(p):
                    try:
                        pct = int(p or 0)
                        if pct >= last_pct[0] + 15:
                            last_pct[0] = pct
                            ev_q.put(("progress", pct))
                    except Exception:
                        pass

                def _bg_worker():
                    try:
                        res, _ = _run_generation(
                            gen_prompt, expect_kind, gen_timeout,
                            on_progress=_on_prog, reference_images=prepared_refs
                        )
                        ev_q.put(("ok", res))
                    except Exception as exc:
                        ev_q.put(("err", exc))

                threading.Thread(target=_bg_worker, daemon=True).start()
                yield _sse(_chat_chunk(cid, created, model, {"role": "assistant"}))
                banner = (
                    "> 🎨 **Muse Image** 正在为您绘制无水印高清原图，请稍候（约 15~45 秒）...\\n\\n"
                    if expect_kind == "image"
                    else "> 🎬 **Muse Video** 正在唤醒云端工作区并渲染视频，请稍候（约 60~180 秒）...\\n\\n"
                )
                yield _sse(_chat_chunk(cid, created, model, {"content": banner}))

                while True:
                    try:
                        kind_ev, payload = ev_q.get(timeout=8.0)
                    except _q.Empty:
                        yield ": keepalive\\n\\n"
                        continue
                    if kind_ev == "progress":
                        yield _sse(_chat_chunk(cid, created, model, {"content": f"> ⏳ 渲染进度：`{payload}%`...\\n\\n"}))
                    elif kind_ev == "ok":
                        md = _format_media_markdown(payload)
                        yield _sse(_chat_chunk(cid, created, model, {"content": md}))
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                        if _want_usage(req.stream_options):
                            yield _sse({"id": cid, "object": "chat.completion.chunk",
                                        "created": created, "model": model, "choices": [],
                                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}})
                        yield "data: [DONE]\\n\\n"
                        return
                    elif kind_ev == "err":
                        err_msg = f"\\n❌ **生成失败**：{payload}"
                        yield _sse(_chat_chunk(cid, created, model, {"content": err_msg}))
                        yield _sse(_chat_chunk(cid, created, model, {}, finish="stop"))
                        yield "data: [DONE]\\n\\n"
                        return
            return StreamingResponse(_media_chat_stream(), media_type="text/event-stream", headers=_SSE_HEADERS)

        try:
            res, _ = await asyncio.to_thread(
                _run_generation, gen_prompt, expect_kind, gen_timeout,
                None, None, None, prepared_refs
            )
        except MuseAuthError as exc:
            raise HTTPException(401, str(exc))
        except MuseGenerationError as exc:
            raise HTTPException(502, str(exc))
        return {
            "id": cid, "object": "chat.completion", "created": created, "model": model,
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": _format_media_markdown(res)}}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
        }

    # 3. 常规 Muse Spark 文本/代码/多模态识图对话
    prepared_chat_refs = _prepare_reference_images(raw_refs) if raw_refs else None
    timeout = int(req.timeout or CFG.chat_timeout)
    acc = store.pick_account(rotate=True)
    if not acc:
        raise HTTPException(400, "没有可用账号，请先在管理页面导入 cookie")"""

if "is_health_probe" not in app_code:
    assert target_chat_body in app_code, "target_chat_body not found"
    app_code = app_code.replace(target_chat_body, replace_chat_body, 1)
    app_code = app_code.replace(
        """    cid = "chatcmpl-" + uuid.uuid4().hex[:24]
    created = int(time.time())
    acc_id = acc["id"]""",
        """    acc_id = acc["id"]""",
        1
    )
    app_code = app_code.replace(
        "stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id)",
        "stream_gen = safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id, reference_images=prepared_chat_refs)",
        1
    )
    app_code = app_code.replace(
        'return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id))',
        'return "".join(safe_chat_stream(cookies, prompt, expires, timeout, account_id=acc_id, reference_images=prepared_chat_refs))',
        1
    )

target_media = """@app.get("/v1/media/{name}")
def get_media(name: str):
    if "/" in name or "\\\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    return FileResponse(p)"""

replace_media = """_MEDIA_MIME = {
    ".webp": "image/webp", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".gif": "image/gif", ".mp4": "video/mp4",
    ".webm": "video/webm", ".mov": "video/quicktime",
}


@app.api_route("/v1/media/{name}", methods=["GET", "HEAD"])
def get_media(name: str):
    if "/" in name or "\\\\" in name or ".." in name:
        raise HTTPException(400, "非法文件名")
    p = os.path.join(CFG.media_dir, name)
    if not os.path.isfile(p):
        raise HTTPException(404, "文件不存在")
    ext = os.path.splitext(name)[1].lower()
    return FileResponse(p, media_type=_MEDIA_MIME.get(ext))"""

if "_MEDIA_MIME" not in app_code and target_media in app_code:
    app_code = app_code.replace(target_media, replace_media, 1)

    print("  - app.py patched!")

with open("app.py", "w", encoding="utf-8") as f:
    f.write(app_code)
PYEOF

python3 -m py_compile engine.py app.py
echo "[3/3] 语法校验通过！现在可回到项目根目录执行: docker compose up -d --build muse2api"
