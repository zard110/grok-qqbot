"""SenseNova (商汤日日新) poster / slide-deck maker used by the ``ppt_poster`` plugin.

CLI (official-bot/sensenova_make.py, same contract as the old tools/ script):

  sensenova_make.py poster --prompt "..." [--ref img.png] [--out poster.png] [--no-image]
  sensenova_make.py ppt --topic "..." [--slides 6] [--images 2] [--name 标题] [--out DIR]
  sensenova_make.py rerender DIR [--name 标题]      (no API calls)
Deliverables are named after a business title: <title>.pptx / <title>.pdf / <title>-长图.jpg, <title>.png.

Pipeline: sensenova-6.8-flash writes HTML (+ image prompts) -> sensenova-u1.5-fast makes the
illustrations (/v1/images/generations, or /v1/images/edits when --ref is given) -> headless
Chrome renders PNGs (Noto Sans CJK SC, auto-shrink if content overflows) -> ppt also gets
.pptx (full-slide images), a long JPG and a PDF.

stdout: one JSON object {"ok": true, ...paths...} or {"ok": false, "error": ..., "kind": ...}.
Exit 0 ok, 2 generic failure, 3 quota/rate limit (429), 4 auth/key problem.
The API key is read at runtime from official-bot/sensenova-key (or SENSENOVA_API_KEY) and is
never printed or logged.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import html as htmlmod
import json
import mimetypes
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

try:  # imported as core.sensenova (bot / tests) or loaded by the CLI wrapper
    from .settings import DATA_DIR as BOT_DIR
except ImportError:  # pragma: no cover
    from settings import DATA_DIR as BOT_DIR
BASE = "https://token.sensenova.cn/v1"
TEXT_MODEL = "sensenova-6.8-flash"
IMAGE_MODEL = "sensenova-u1.5-fast"
DEFAULT_OUT = BOT_DIR / "media" / "generated"
# Two text blocks "overlap" when their intersection exceeds this share of the smaller block
# (0.08 flagged tight multi-line headings; 0.25 keeps real collisions).
OVERLAP_RATIO = 0.25
SEND_LIMIT = 5 * 1024 * 1024 - 64 * 1024  # send_group.py local media limit (5MB)
USAGE = {"chat_calls": 0, "chat_tokens": 0, "image_calls": 0, "image_tokens": 0}


class MakeError(Exception):
    def __init__(self, msg: str, kind: str = "error", code: int = 2):
        super().__init__(msg)
        self.kind, self.code = kind, code


def _key() -> str:
    v = os.environ.get("SENSENOVA_API_KEY", "").strip()
    if v:
        return v
    try:
        v = (BOT_DIR / "sensenova-key").read_text(encoding="utf-8").strip()
    except OSError:
        v = ""
    if not v:
        raise MakeError("SenseNova key missing (official-bot/sensenova-key)", "auth", 4)
    return v


def _post(path: str, payload: dict, timeout: int) -> dict:
    """POST with one retry on timeout / connection error / 5xx. Never echoes the key."""
    headers = {"Authorization": "Bearer " + _key(), "Content-Type": "application/json"}
    last = ""
    for attempt in range(2):
        try:
            r = requests.post(BASE + path, json=payload, headers=headers, timeout=(15, timeout))
        except (requests.Timeout, requests.ConnectionError) as exc:
            last = f"{type(exc).__name__} on {path}"
            time.sleep(3)
            continue
        body = r.text[:400]
        if r.status_code == 200:
            try:
                return r.json()
            except ValueError:
                last = f"non-JSON 200 from {path}"
                continue
        low = body.lower()
        if r.status_code == 429 or any(s in low for s in ("quota", "insufficient", "rate limit", "积分", "额度")):
            raise MakeError(f"SenseNova 额度/限流（HTTP {r.status_code}）：5 小时窗口 12 万、每周 120 万积分，"
                            f"稍后再试或换小一点的任务。detail={body[:200]}", "quota", 3)
        if r.status_code in (401, 403):
            raise MakeError(f"SenseNova 鉴权失败（HTTP {r.status_code}），检查 sensenova-key", "auth", 4)
        if r.status_code >= 500:
            last = f"HTTP {r.status_code} on {path}: {body[:200]}"
            time.sleep(3)
            continue
        raise MakeError(f"HTTP {r.status_code} on {path}: {body[:300]}", "api", 2)
    raise MakeError(f"SenseNova 请求失败（已重试一次）：{last}", "network", 2)


def chat(system: str, user: str, max_tokens: int = 24000, effort: str = "low") -> str:
    data = _post("/chat/completions", {
        "model": TEXT_MODEL,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "max_tokens": max_tokens, "reasoning_effort": effort,
    }, timeout=420)
    USAGE["chat_calls"] += 1
    USAGE["chat_tokens"] += int((data.get("usage") or {}).get("total_tokens") or 0)
    ch = (data.get("choices") or [{}])[0]
    content = ((ch.get("message") or {}).get("content") or "").strip()
    if ch.get("finish_reason") == "content_filter":
        raise MakeError("内容被 SenseNova 审核拦截，换个说法再试", "content_filter", 2)
    if not content:
        raise MakeError(f"模型没返回正文（finish_reason={ch.get('finish_reason')}）", "empty", 2)
    return content


def gen_image(prompt: str, size: str, out: Path, ref: Path | None = None) -> Path:
    payload = {"model": IMAGE_MODEL, "prompt": prompt[:1500], "n": 1, "size": size,
               "output_format": "jpeg", "response_format": "b64_json", "watermark": False}
    path = "/images/generations"
    if ref:
        mime = mimetypes.guess_type(str(ref))[0] or "image/png"
        if mime == "image/jpg":
            mime = "image/jpeg"
        payload["images"] = [{"image_url": f"data:{mime};base64," + base64.b64encode(ref.read_bytes()).decode()}]
        path = "/images/edits"
    data = _post(path, payload, timeout=240)
    USAGE["image_calls"] += 1
    USAGE["image_tokens"] += int((data.get("usage") or {}).get("total_tokens") or 0)
    item = (data.get("data") or [{}])[0]
    if item.get("b64_json"):
        out.write_bytes(base64.b64decode(item["b64_json"]))
    elif item.get("url"):
        r = requests.get(item["url"], timeout=60)
        r.raise_for_status()
        out.write_bytes(r.content)
    else:
        raise MakeError("图片接口没返回图片", "empty", 2)
    return out


# ---------------------------------------------------------------- HTML helpers
FONT_CSS = """
<meta http-equiv="Content-Security-Policy" content="default-src 'self' file: data: 'unsafe-inline'">
<style id="sn-base">
html,body{margin:0;padding:0;overflow:hidden;background:#111;}
body{font-family:"Noto Sans CJK SC","Noto Color Emoji",sans-serif;-webkit-font-smoothing:antialiased;}
*{box-sizing:border-box;}
</style>
"""

FIT_JS = """
<script id="sn-fit">
(function(){
  // Measure where TEXT ends (decorations bleeding off-canvas are ignored). If text spills out of the
  // fixed canvas, enlarge the layout box by 1/z and zoom it back by z so text reflows into a bigger
  // box and the final frame is still exactly W x H. Then report overlapping text blocks.
  var W=%(w)d,H=%(h)d,root=document.querySelector('%(sel)s');
  function texts(){
    return [].slice.call(root.querySelectorAll('*')).filter(function(e){
      var cs=getComputedStyle(e); if(cs.display==='none'||cs.visibility==='hidden'||+cs.opacity===0) return false;
      for(var k=0;k<e.childNodes.length;k++){var c=e.childNodes[k]; if(c.nodeType===3&&c.textContent.trim()) return true;} return false;});
  }
  function extent(els){
    var R=root.getBoundingClientRect(), o=1;
    els.forEach(function(e){
      var r=document.createRange(); r.selectNodeContents(e); var b=r.getBoundingClientRect();
      if(!b.width||!b.height) return;
      o=Math.max(o,(b.bottom-R.top)/R.height,(b.right-R.left)/R.width,(R.top-b.top)/R.height+1,(R.left-b.left)/R.width+1);
    });
    return o;
  }
  function fit(){
    if(!root){ document.title='FIT nosel'; return; }
    var z=1,i,o,raw=extent(texts());
    for(i=0;i<6;i++){
      o=extent(texts());
      if(o<=1.005) break;
      z=Math.max(0.6, z/Math.min(o,1.5)*0.99);
      root.style.setProperty('width',(W/z)+'px','important');
      root.style.setProperty('height',(H/z)+'px','important');
      root.style.setProperty('zoom',String(z),'important');
    }
    var els=texts(), rs=els.map(function(e){var r=document.createRange(); r.selectNodeContents(e); return r.getBoundingClientRect();}), hits=[];
    for(var a=0;a<els.length;a++) for(var b=a+1;b<els.length;b++){
      if(els[a].contains(els[b])||els[b].contains(els[a])) continue;
      var A=rs[a],B=rs[b],w=Math.min(A.right,B.right)-Math.max(A.left,B.left),h=Math.min(A.bottom,B.bottom)-Math.max(A.top,B.top);
      if(w>4&&h>4){var m=Math.min(A.width*A.height,B.width*B.height); if(m>0&&w*h/m>%(overlap)s) hits.push(els[a].textContent.trim().slice(0,14)+'~'+els[b].textContent.trim().slice(0,14));}
    }
    document.title='FIT z='+z.toFixed(3)+' raw_overflow='+raw.toFixed(2)+' left='+extent(els).toFixed(2)+' overlap='+hits.length+(hits.length?' | '+hits.slice(0,6).join(' ; '):'');
  }
  function go(){ setTimeout(fit, 50); }
  if(document.fonts&&document.fonts.ready){document.fonts.ready.then(go);} else {window.addEventListener('load',go);}
})();
</script>
"""


def extract_html(text: str) -> str:
    m = re.search(r"```(?:html)?\s*(<!DOCTYPE.*?|<html.*?)```", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"(<!DOCTYPE html.*</html>|<html.*</html>)", text, re.S | re.I)
    if m:
        return m.group(1).strip()
    raise MakeError("模型输出里没找到 HTML", "parse", 2)


def extract_prompts(text: str) -> list[str]:
    m = re.search(r"IMAGE_PROMPTS\s*[:：]\s*(\[.*?\])", text, re.S)
    if not m:
        m1 = re.search(r"IMAGE_PROMPT\s*[:：]\s*(.+)", text)
        return [m1.group(1).strip()] if m1 else []
    try:
        arr = json.loads(m.group(1))
        return [str(x).strip() for x in arr if str(x).strip()]
    except ValueError:
        return []


def prepare(html: str, w: int, h: int, sel: str, images: dict[str, str]) -> str:
    for ph, src in images.items():
        html = html.replace("{{" + ph + "}}", src)
    html = re.sub(r"\{\{IMG\d+\}\}", "", html)  # unused placeholders -> empty
    html = re.sub(r"<link[^>]+(fonts\.googleapis|fonts\.gstatic)[^>]*>", "", html, flags=re.I)
    html = re.sub(r"@import\s+url\([^)]*\)\s*;?", "", html)
    head = FONT_CSS + (f"<style>html,body{{width:{w}px;height:{h}px;}}</style>")
    if re.search(r"<head[^>]*>", html, re.I):
        html = re.sub(r"(<head[^>]*>)", lambda m: m.group(1) + '<meta charset="utf-8">' + head, html, count=1, flags=re.I)
    else:
        html = '<meta charset="utf-8">' + head + html
    # neutralise "viewer" CSS (body padding/flex/gap/centering) so the canvas sits at 0,0
    override = (f"<style id=\"sn-override\">html,body{{margin:0!important;padding:0!important;display:block!important;"
                f"width:{w}px!important;height:{h}px!important;overflow:hidden!important;}}"
                f"{sel}{{margin:0!important;transform:none!important;}}</style>")
    if re.search(r"</head>", html, re.I):
        html = re.sub(r"</head>", lambda m: override + "</head>", html, count=1, flags=re.I)
    else:
        html = override + html
    fit = FIT_JS % {"w": w, "h": h, "sel": sel, "overlap": OVERLAP_RATIO}
    if re.search(r"</body>", html, re.I):
        html = re.sub(r"</body>", lambda m: fit + "</body>", html, count=1, flags=re.I)
    else:
        html += fit
    return html


VENV_PY = "/opt/fetchurl-venv/bin/python"  # has playwright (same as fetch_url.py)
RENDER_SCRIPT = r"""
import json, sys
from playwright.sync_api import sync_playwright
jobs = json.loads(sys.argv[1]); out = []
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True, args=["--no-sandbox", "--allow-file-access-from-files"])
    for j in jobs:
        pg = b.new_page(viewport={"width": j["w"], "height": j["h"]}, device_scale_factor=1)
        pg.goto(j["url"], wait_until="load", timeout=45000)
        try:
            pg.wait_for_function("document.title.startsWith('FIT')", timeout=15000)
        except Exception:
            pass
        pg.wait_for_timeout(300)
        pg.screenshot(path=j["png"], clip={"x": 0, "y": 0, "width": j["w"], "height": j["h"]})
        out.append(pg.title()); pg.close()
    b.close()
print(json.dumps(out))
"""


def render_many(jobs: list[tuple[Path, Path, int, int]]) -> list[str]:
    """Render HTML files to PNG with Playwright headless Chrome; returns FIT reports per page."""
    spec = [{"url": h.resolve().as_uri(), "png": str(png), "w": w, "h": hh} for h, png, w, hh in jobs]
    py = VENV_PY if os.path.exists(VENV_PY) else sys.executable
    try:
        r = subprocess.run([py, "-c", RENDER_SCRIPT, json.dumps(spec)], capture_output=True, text=True,
                           timeout=60 + 30 * len(jobs))
    except subprocess.TimeoutExpired:
        raise MakeError("Chrome 渲染超时", "render", 2)
    if r.returncode != 0:
        raise MakeError("Chrome 渲染失败：" + (r.stderr or r.stdout)[-300:], "render", 2)
    for _, png, _, _ in jobs:
        if not png.exists() or png.stat().st_size < 1000:
            raise MakeError(f"Chrome 渲染失败：{png.name}", "render", 2)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return [""] * len(jobs)


def sendable(png: Path) -> Path:
    """send_group.py accepts <=5MB; fall back to a JPEG copy when the PNG is too big."""
    if png.stat().st_size <= SEND_LIMIT:
        return png
    from PIL import Image
    jpg = png.with_suffix(".jpg")
    for q in (90, 82, 72):
        Image.open(png).convert("RGB").save(jpg, "JPEG", quality=q, optimize=True)
        if jpg.stat().st_size <= SEND_LIMIT:
            break
    return jpg


def out_dir(arg: str | None, kind: str) -> Path:
    if arg:
        p = Path(arg).expanduser().resolve()
    else:
        p = DEFAULT_OUT / f"{time.strftime('%Y%m%d-%H%M%S')}-{kind}"
    p.mkdir(parents=True, exist_ok=True)
    return p


REPAIR_SYS = """你是前端排版修复专家。下面的 HTML 在固定画布里渲染后有文字块互相重叠或内容溢出。
请在不改变文案和整体风格的前提下修复：去掉文字元素的绝对定位，改用 flex/grid 正常流，必要时略缩字号或间距。
保留所有 {{IMGn}} 占位符或图片文件名不变。只输出一个 ```html 代码块，不要解释。"""


def needs_fix(fit: str) -> bool:
    m = re.search(r"overlap=(\d+)", fit or "")
    left = re.search(r"left=([\d.]+)", fit or "")
    return bool((m and int(m.group(1)) > 0) or (left and float(left.group(1)) > 1.02))


def repair_html(html: str, fit: str) -> str:
    reply = chat(REPAIR_SYS, f"渲染检测结果：{fit}\n\nHTML：\n```html\n{html}\n```", max_tokens=24000)
    return extract_html(reply)


# ---------------------------------------------------------------- file titles
GENERIC = re.compile(r"(?i)\b(deck|ppt|pptx|slides?|poster|pdf)\b|演示文稿|幻灯片|课件|海报|宣传图|文档|模板|无标题|未命名")


def clean_title(t: str | None, limit: int = 30) -> str:
    """Sanitize a title into a safe file name; '' if it is empty/generic."""
    t = htmlmod.unescape(re.sub(r"<[^>]+>", " ", t or ""))
    t = re.sub(r'[/\\:*?"<>|\x00-\x1f]', " ", t)
    t = re.sub(r"\s+", " ", t).strip(" .·•-_—|")
    t = t[:limit].rstrip(" .·•-_—")
    if len(GENERIC.sub("", t).strip(" .·•-_—")) < 2:
        return ""
    return t


def extract_title(text: str) -> str:
    m = re.search(r"(?:FILE_TITLE|TITLE)\s*[:：]\s*(.+)", text)
    return clean_title(m.group(1).strip().strip("「」\"'《》")) if m else ""


def heading_title(html: str) -> str:
    """Fallback: main heading of the cover (first slide / poster)."""
    first = re.search(r"<section[^>]*class=[\"'][^\"']*\bslide\b.*?</section>", html, re.S | re.I)
    scope = first.group(0) if first else html
    for pat in (r"<h1[^>]*>(.*?)</h1>", r"<[^>]+class=[\"'][^\"']*\b(?:h1|h2|title|main-title|cover-title|headline)\b[^\"']*[\"'][^>]*>(.*?)</",
                r"<h2[^>]*>(.*?)</h2>"):
        m = re.search(pat, scope, re.S | re.I)
        if m:
            t = clean_title(re.sub(r"<br\s*/?>", "", m.group(m.lastindex), flags=re.I).replace("\n", ""))
            t = re.sub(r"\s+(?=[\u4e00-\u9fff])|(?<=[\u4e00-\u9fff])\s+", "", t)
            if t:
                return t
    return ""


def pick_title(name: str | None, reply_title: str, html: str, topic: str, kind: str) -> str:
    return (clean_title(name) or reply_title or heading_title(html) or clean_title(topic)
            or time.strftime(f"{'海报' if kind == 'poster' else '演示'}-%Y%m%d-%H%M"))


def read_meta(od: Path) -> dict:
    try:
        return json.loads((od / "meta.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_meta(od: Path, **kw) -> None:
    meta = read_meta(od)
    meta.update(kw)
    (od / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=1), encoding="utf-8")


def drop_old(od: Path, keep: list[Path]) -> None:
    """Remove previously named deliverables (and legacy deck.*/poster.png) that are not in keep."""
    keep_s = {p.resolve() for p in keep}
    old = [od / f for f in read_meta(od).get("files", [])]
    old += [od / f for f in ("deck.pptx", "deck.pdf", "deck-long.jpg", "poster.png", "poster.jpg")]
    for f in old:
        if f.exists() and f.resolve() not in keep_s:
            f.unlink()


# ---------------------------------------------------------------- poster
POSTER_SYS = """你是顶级中文海报设计师兼前端工程师。根据需求设计一张竖版海报，输出三部分，不要别的解释：
0) 第一行：FILE_TITLE: <用作文件名的中文标题，≤20 字，要体现具体业务/活动，如「Zwift练功房周六夜骑团骑海报」，不要只写"海报"等泛词>
1) 第二行：IMAGE_PROMPT: <给文生图模型的中文画面描述，只描述主视觉/背景插画，不要求画面里出现任何文字，写明构图留白位置、风格、配色>
2) 一个 ```html 代码块：完整单文件 HTML。
HTML 硬性要求：
- 画布固定 1080x1440 像素：最外层 <div class="poster"> 宽 1080px 高 1440px，position:relative，overflow:hidden；所有内容都在里面，绝不超出。
- 主视觉图用占位符 {{IMG1}}，例如 background-image:url('{{IMG1}}') 或 <img src="{{IMG1}}">，用 object-fit:cover / background-size:cover，并加渐变蒙层保证文字可读。
- 字体只用 font-family:"Noto Sans CJK SC"（标题可 font-weight:900），不要引用任何外部字体、外链 CSS、JS 或网络图片。
- 中文文案精炼：主标题 ≤14 字（字号 96–140px），副标题一行，信息区 3–5 条（时间/地点/亮点等，可用 emoji 图标），底部一行号召语。缺的信息用合理占位如「时间待定」，不编造具体人名电话。
- 版式要有设计感：层次分明、留白、对比色强调；所有文字块设置明确宽度并允许换行，避免溢出。
- 布局防重叠：只有背景图、蒙层和纯装饰元素可以 position:absolute；所有文字放进一个 <div class="content">（position:relative;z-index:2;height:100%;display:flex;flex-direction:column;），用 flex/gap/margin 排列，底部号召语用 margin-top:auto 推到底部，保证文字块之间绝不重叠。
- 不要用 vh/vw，全部用 px。"""


def cmd_poster(a) -> dict:
    explicit = Path(a.out).expanduser().resolve() if a.out and a.out.lower().endswith(".png") else None
    od = explicit.parent if explicit else out_dir(a.out, "poster")
    od.mkdir(parents=True, exist_ok=True)
    ref = Path(a.ref).expanduser().resolve() if a.ref else None
    if ref and not ref.is_file():
        raise MakeError(f"参考图不存在：{ref}", "input", 2)
    reply = chat(POSTER_SYS, "海报需求：" + a.prompt + ("\n（用户提供了参考图，主视觉会以它为基础改绘。）" if ref else ""))
    html = extract_html(reply)
    title = pick_title(a.name, extract_title(reply), html, a.prompt, "poster")
    final = explicit or od / f"{title}.png"
    prompts = extract_prompts(reply) or [a.prompt]
    images = {}
    if not a.no_image:
        img = gen_image(prompts[0] + "。竖版构图，画面中不要出现任何文字。", "1536x2048", od / "img1.jpg", ref)
        images["IMG1"] = img.name
    page = od / "poster.html"
    (od / "poster.raw.html").write_text(html, encoding="utf-8")
    page.write_text(prepare(html, 1080, 1440, ".poster", images), encoding="utf-8")
    fit = render_many([(page, final, 1080, 1440)])[0]
    repaired = False
    if needs_fix(fit) and not a.no_repair:
        try:
            fixed = repair_html(html, fit)
            tmp_png = od / "poster.repaired.png"
            page2 = od / "poster.repaired.html"
            page2.write_text(prepare(fixed, 1080, 1440, ".poster", images), encoding="utf-8")
            fit2 = render_many([(page2, tmp_png, 1080, 1440)])[0]
            if not needs_fix(fit2) or fit2.count("~") < fit.count("~"):
                page2.replace(page); tmp_png.replace(final); fit, repaired = fit2, True
                (od / "poster.raw.html").write_text(fixed, encoding="utf-8")
        except MakeError as exc:
            if exc.code in (3, 4):
                raise
    send = sendable(final)
    write_meta(od, kind="poster", title=title, files=sorted({final.name, send.name}))
    return {"ok": True, "kind": "poster", "title": title, "png": str(final), "send_path": str(send),
            "html": str(page), "image_prompts": prompts[:1], "fit": fit, "repaired": repaired,
            "dir": str(od), "usage": USAGE}


# ---------------------------------------------------------------- ppt
PPT_SYS = """你是顶级中文演示文稿设计师兼前端工程师。根据主题做一套 16:9 幻灯片，输出三部分，不要别的解释：
0) 第一行：FILE_TITLE: <用作文件名的中文标题，≤20 字，要体现具体业务和用途，如「乐扣低代码平台产品介绍」，不要只写 deck/PPT/演示文稿 等泛词>
1) 第二行：IMAGE_PROMPTS: ["...", "..."]  —— JSON 数组，恰好 %(nimg)d 条，给文生图模型的中文画面描述（只描述插画/背景，画面不出现文字）。第 1 条用于封面。
2) 一个 ```html 代码块：完整单文件 HTML，含一个 <style> 和恰好 %(n)d 个 <section class="slide">（不要嵌套 section）。
HTML 硬性要求：
- 每个 .slide 固定 width:1280px;height:720px;position:relative;overflow:hidden; padding 自己控制；所有内容不得超出。
- 插画用占位符 {{IMG1}}..{{IMG%(nimg)d}}（{{IMG1}} 放封面），用 <img> + object-fit:cover 或 background-size:cover。没有占位符的页用 CSS 渐变/色块/几何装饰/emoji 图标做视觉，不要用外链图片。
- 字体只用 font-family:"Noto Sans CJK SC"；不要引用外部字体、外链 CSS 或 JS；全部用 px，不用 vh/vw。
- 统一配色和母版风格（页眉/页码/装饰条），封面页 + 内容页 + 最后一页总结/行动号召。
- 布局防重叠：只有背景图、蒙层、页码和纯装饰元素可以 position:absolute；标题和正文用 flex/grid 正常流排版，文字块之间绝不重叠；图片区域要给定固定宽高。
- 内容页每页标题 ≤18 字，要点 3–5 条，每条 ≤40 字，正文字号 22–30px，标题 40–56px；可用卡片、两栏、编号、对比、时间线等版式，别全是同一种列表。
- 内容要准确、具体、有信息量；不确定的事实不要编数字。"""


def split_slides(html: str) -> tuple[str, list[str]]:
    styles = "\n".join(re.findall(r"<style[^>]*>.*?</style>", html, re.S | re.I))
    slides = re.findall(r"<section[^>]*class=[\"'][^\"']*\bslide\b[^\"']*[\"'][^>]*>.*?</section>", html, re.S | re.I)
    if not slides:
        raise MakeError("模型输出里没有 <section class=\"slide\">", "parse", 2)
    return styles, slides


def deck_outputs(od: Path, pngs: list[Path], title: str) -> tuple[Path, Path, Path]:
    """<title>.pptx (full-slide pictures), <title>.pdf and <title>-长图.jpg from the slide PNGs."""
    from PIL import Image
    from pptx import Presentation
    from pptx.util import Emu
    prs = Presentation()
    prs.slide_width, prs.slide_height = Emu(12192000), Emu(6858000)
    for p in pngs:
        s = prs.slides.add_slide(prs.slide_layouts[6])
        s.shapes.add_picture(str(p), 0, 0, prs.slide_width, prs.slide_height)
    prs.core_properties.title = title
    prs.core_properties.subject = title
    pptx_path = od / f"{title}.pptx"
    prs.save(pptx_path)
    ims = [Image.open(p).convert("RGB") for p in pngs]
    pdf_path = od / f"{title}.pdf"
    ims[0].save(pdf_path, "PDF", save_all=True, append_images=ims[1:], resolution=96, title=title)
    gap = 12
    long_img = Image.new("RGB", (1280, len(ims) * 720 + (len(ims) - 1) * gap), (24, 24, 24))
    for i, im in enumerate(ims):
        long_img.paste(im, (0, i * (720 + gap)))
    long_path = od / f"{title}-长图.jpg"
    long_img.save(long_path, "JPEG", quality=88, optimize=True)
    long_path = sendable(long_path)
    drop_old(od, [pptx_path, pdf_path, long_path])
    write_meta(od, kind="ppt", title=title, files=[pptx_path.name, pdf_path.name, long_path.name])
    return pptx_path, pdf_path, long_path


def cmd_rerender(a) -> dict:
    """Re-render an existing output dir from its saved raw HTML (no API calls)."""
    od = Path(a.dir).expanduser().resolve()
    images = {f"IMG{i}": f"img{i}.jpg" for i in range(1, 6) if (od / f"img{i}.jpg").exists()}
    meta = read_meta(od)
    if (od / "deck.raw.html").exists():
        raw = (od / "deck.raw.html").read_text(encoding="utf-8")
        title = clean_title(a.name) or meta.get("title") or heading_title(raw) or clean_title(od.name) or "演示"
        styles, slides = split_slides(raw)
        jobs = []
        for i, sec in enumerate(slides, 1):
            page = od / f"slide{i:02d}.html"
            fixed = od / f"slide{i:02d}.fixed.html"  # accepted repair from the original run
            doc = (fixed.read_text(encoding="utf-8") if fixed.exists()
                   else f"<!DOCTYPE html><html><head>{styles}</head><body>{sec}</body></html>")
            page.write_text(prepare(doc, 1280, 720, ".slide", images), encoding="utf-8")
            jobs.append((page, od / f"slide{i:02d}.png", 1280, 720))
        fits = render_many(jobs)
        pngs = [j[1] for j in jobs]
        pptx_path, pdf_path, long_path = deck_outputs(od, pngs, title)
        return {"ok": True, "kind": "ppt", "title": title, "slides": [str(p) for p in pngs],
                "send_paths": [str(sendable(p)) for p in pngs], "pptx": str(pptx_path), "pdf": str(pdf_path),
                "long_image": str(long_path), "fit": fits, "dir": str(od)}
    if (od / "poster.raw.html").exists():
        raw = (od / "poster.raw.html").read_text(encoding="utf-8")
        title = clean_title(a.name) or meta.get("title") or heading_title(raw) or clean_title(od.name) or "海报"
        page, png = od / "poster.html", od / f"{title}.png"
        page.write_text(prepare(raw, 1080, 1440, ".poster", images), encoding="utf-8")
        fit = render_many([(page, png, 1080, 1440)])[0]
        send = sendable(png)
        drop_old(od, [png, send])
        write_meta(od, kind="poster", title=title, files=sorted({png.name, send.name}))
        return {"ok": True, "kind": "poster", "title": title, "png": str(png), "send_path": str(send),
                "fit": fit, "dir": str(od)}
    raise MakeError("目录里没有 deck.raw.html / poster.raw.html", "input", 2)


def cmd_ppt(a) -> dict:
    n = max(1, min(int(a.slides), 15))
    nimg = max(0, min(int(a.images), n, 4))
    od = out_dir(a.out, "ppt")
    sys_prompt = PPT_SYS % {"n": n, "nimg": max(nimg, 1)}
    reply = chat(sys_prompt, f"主题：{a.topic}\n页数：{n}" + (f"\n补充要求：{a.extra}" if a.extra else ""), max_tokens=40000)
    html = extract_html(reply)
    title = pick_title(a.name, extract_title(reply), html, a.topic, "ppt")
    prompts = extract_prompts(reply)[:nimg]
    (od / "deck.raw.html").write_text(html, encoding="utf-8")
    styles, slides = split_slides(html)
    images: dict[str, str] = {}
    if prompts:
        with cf.ThreadPoolExecutor(max_workers=len(prompts)) as ex:
            futs = {ex.submit(gen_image, p + "。横版 16:9 构图，画面中不要出现任何文字。", "1664x928",
                              od / f"img{i + 1}.jpg"): i for i, p in enumerate(prompts)}
            for f in cf.as_completed(futs):
                i = futs[f]
                try:
                    images[f"IMG{i + 1}"] = f.result().name
                except MakeError as exc:
                    if exc.code in (3, 4):
                        raise
                    # one failed illustration should not kill the deck
    jobs = []
    for i, sec in enumerate(slides, 1):
        page = od / f"slide{i:02d}.html"
        doc = f"<!DOCTYPE html><html><head>{styles}</head><body>{sec}</body></html>"
        page.write_text(prepare(doc, 1280, 720, ".slide", images), encoding="utf-8")
        jobs.append((page, od / f"slide{i:02d}.png", 1280, 720))
    fits = render_many(jobs)
    pngs = [j[1] for j in jobs]
    repaired = []
    bad = [i for i, f in enumerate(fits) if needs_fix(f)][:3] if not a.no_repair else []
    for i in bad:  # at most 3 repair calls per deck
        try:
            doc = f"<!DOCTYPE html><html><head>{styles}</head><body>{slides[i]}</body></html>"
            fixed = repair_html(doc, fits[i])
            page2 = od / f"slide{i + 1:02d}.repaired.html"
            png2 = od / f"slide{i + 1:02d}.repaired.png"
            page2.write_text(prepare(fixed, 1280, 720, ".slide", images), encoding="utf-8")
            f2 = render_many([(page2, png2, 1280, 720)])[0]
            if not needs_fix(f2) or f2.count("~") < fits[i].count("~"):
                page2.replace(jobs[i][0]); png2.replace(pngs[i]); fits[i] = f2; repaired.append(i + 1)
                (od / f"slide{i + 1:02d}.fixed.html").write_text(fixed, encoding="utf-8")
        except MakeError as exc:
            if exc.code in (3, 4):
                raise
    pptx_path, pdf_path, long_path = deck_outputs(od, pngs, title)
    return {"ok": True, "kind": "ppt", "title": title, "slides": [str(p) for p in pngs],
            "send_paths": [str(sendable(p)) for p in pngs], "pptx": str(pptx_path), "pdf": str(pdf_path),
            "long_image": str(long_path), "slide_count": len(pngs), "requested": n,
            "images_used": sorted(images), "fit": fits, "repaired_slides": repaired, "dir": str(od), "usage": USAGE}


def write_result(od: Path | None, res: dict) -> None:
    """Background jobs (plugin actions) poll <dir>/result.json; written atomically."""
    if od is None:
        return
    try:
        od.mkdir(parents=True, exist_ok=True)
        tmp = od / "result.json.tmp"
        tmp.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        tmp.replace(od / "result.json")
    except OSError:
        pass


def _job_dir(a) -> Path | None:
    raw = getattr(a, "dir", None) or getattr(a, "out", None)
    if not raw:
        return None
    p = Path(raw).expanduser().resolve()
    return p.parent if p.suffix.lower() == ".png" else p


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("poster", help="single 1080x1440 poster PNG")
    p.add_argument("--prompt", required=True)
    p.add_argument("--ref", help="reference image (png/jpg/webp) for the main visual")
    p.add_argument("--out", help="output .png path or directory (default media/generated/<ts>-poster/)")
    p.add_argument("--no-image", action="store_true", help="skip u1.5 illustration (cheaper)")
    p.add_argument("--name", help="file title override (default: model FILE_TITLE -> main heading -> prompt)")
    p.add_argument("--no-repair", action="store_true", help="skip the automatic overlap/overflow repair call")
    q = sub.add_parser("ppt", help="16:9 deck -> PNGs + pptx + pdf + long jpg")
    q.add_argument("--topic", required=True)
    q.add_argument("--slides", type=int, default=6)
    q.add_argument("--images", type=int, default=2, help="u1.5 illustrations to generate (0-4, default 2)")
    q.add_argument("--extra", default="", help="extra requirements (audience, style, key points)")
    q.add_argument("--name", help="file title override, e.g. 乐扣低代码平台产品介绍 (default: model FILE_TITLE -> cover heading -> topic)")
    q.add_argument("--out", help="output directory (default media/generated/<ts>-ppt/)")
    q.add_argument("--no-repair", action="store_true", help="skip automatic overlap/overflow repair calls")
    r = sub.add_parser("rerender", help="re-render an output dir from saved raw HTML (no API calls)")
    r.add_argument("dir")
    r.add_argument("--name", help="rename deliverables to this title (default: keep meta.json title / cover heading)")
    a = ap.parse_args(argv)
    t0 = time.time()
    try:
        res = {"poster": cmd_poster, "ppt": cmd_ppt, "rerender": cmd_rerender}[a.cmd](a)
        res["seconds"] = round(time.time() - t0, 1)
        code = 0
    except MakeError as exc:
        res = {"ok": False, "kind": exc.kind, "error": str(exc), "usage": USAGE,
               "seconds": round(time.time() - t0, 1)}
        code = exc.code
    except Exception as exc:  # noqa: BLE001
        res = {"ok": False, "kind": "crash", "error": f"{type(exc).__name__}: {str(exc)[:300]}", "usage": USAGE}
        code = 2
    res["exit_code"] = code
    print(json.dumps(res, ensure_ascii=False))
    write_result(Path(res["dir"]) if res.get("dir") else _job_dir(a), res)
    return code


# ---------------------------------------------------------------- plugin jobs
CLI = Path(__file__).resolve().parent.parent / "sensenova_make.py"


def job_dir(kind: str, event_key: str) -> Path:
    """Deterministic per-event folder so a redelivered event does not start a second job."""
    import hashlib
    digest = hashlib.sha256(event_key.encode("utf-8")).hexdigest()[:10] if event_key else str(int(time.time()))
    return DEFAULT_OUT / f"{time.strftime('%Y%m%d')}-{kind}-{digest}"


def start_job(kind: str, args: dict, event_key: str = "") -> dict:
    """Launch the CLI detached for a plugin action and return where the result will appear.

    The gateway must not block for minutes; the routine polls ``result_file``
    (same JSON as CLI stdout, plus ``exit_code``) and then sends the files itself.
    """
    if kind not in ("poster", "ppt"):
        raise ValueError("Unknown SenseNova job kind")
    od = job_dir(kind, event_key)
    result = od / "result.json"
    if od.exists():
        state = "done" if result.exists() else "running"
        return {"status": state, "dir": str(od), "result_file": str(result), "duplicate": True}
    od.mkdir(parents=True)
    argv = [sys.executable, str(CLI), kind, "--out", str(od)]
    if kind == "poster":
        argv += ["--prompt", str(args.get("prompt") or "")[:1000]]
    else:
        argv += ["--topic", str(args.get("topic") or "")[:500],
                 "--slides", str(max(1, min(int(args.get("slides") or 6), 15)))]
        if args.get("extra"):
            argv += ["--extra", str(args["extra"])[:1000]]
    if args.get("name"):
        argv += ["--name", str(args["name"])[:60]]
    log = open(od / "job.out", "ab")  # noqa: SIM115 - handed to the child
    try:
        proc = subprocess.Popen(argv, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                cwd=str(CLI.parent), start_new_session=True)
    finally:
        log.close()
    return {"status": "started", "pid": proc.pid, "dir": str(od), "result_file": str(result), "duplicate": False}


if __name__ == "__main__":
    sys.exit(main())
