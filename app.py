# -*- coding: utf-8 -*-
"""
CNKI 批量下载器 —— 本地桌面应用后端（标准库 HTTP 服务 + Playwright 引擎）
"""
import os
import re
import sys
import json
import time
import threading
import subprocess
from pathlib import Path
from urllib.parse import urlparse, parse_qs, unquote
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler

BASE = Path(__file__).resolve().parent
WEB = BASE / "web"
DATA = BASE / "data"
DATA.mkdir(exist_ok=True)
CONFIG_FILE = DATA / "config.json"
LOG_FILE = DATA / "history.log"
PROFILE = DATA / "browser-profile"

PORT = int(os.environ.get("CNKI_PORT", "8765"))

DEFAULT_CONFIG = {
    "outdir": str(Path.home() / "Downloads" / "CNKI"),
    "order": "篇名",
    "delay": 3,
    "use_proxy": False,
}

_lock = threading.Lock()
STATE = {
    "items": [],
    "running": False,
    "paused": False,
    "phase": "idle",
    "message": "",
    "log": [],
    "browser_open": False,
    "outdir": DEFAULT_CONFIG["outdir"],
    "order": DEFAULT_CONFIG["order"],
    "delay": DEFAULT_CONFIG["delay"],
}
ENGINE = None


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CONFIG_FILE.exists():
            cfg.update(json.loads(CONFIG_FILE.read_text("utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        CONFIG_FILE.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), "utf-8")
    except Exception:
        pass


def add_log(msg, level="info"):
    rec = {"t": time.strftime("%H:%M:%S"), "m": str(msg), "lv": level}
    with _lock:
        STATE["log"].append(rec)
        if len(STATE["log"]) > 800:
            del STATE["log"][:200]
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}][{level}] {msg}\n")
    except Exception:
        pass


def state_cb(**kw):
    with _lock:
        STATE.update(kw)


# ---------------- 文本解析 ----------------
URL_RE = re.compile(r"https?://[^\s\)\]\"'<>，。；]+")


def _kw_from_url(u):
    try:
        q = parse_qs(urlparse(u).query)
        for k in ("kw", "q"):
            if k in q and q[k]:
                return unquote(q[k][0])
    except Exception:
        pass
    return ""


def parse_lines(text):
    items = []
    seq = 0
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        line = re.sub(r"^[\-\*\u2022]\s*", "", line)
        m_md = re.match(r"^\[([^\]]{2,200})\]\((https?://[^\)\s]+)\)\s*$", line)
        if m_md:
            line = f"{m_md.group(1)} | {m_md.group(2)}"
        m_url = URL_RE.search(line)
        url = ""
        title = ""
        if m_url:
            url = m_url.group(0).rstrip(".,;")
            title = (line[: m_url.start()] + " " + line[m_url.end():]).strip(" \t-|:：,，")
        else:
            title = line
        title = re.sub(r"^\d{1,3}[\.、\)）]\s*", "", title).strip()
        title = re.sub(r"^\[\s*\]\s*", "", title).strip()
        if not title and url:
            title = _kw_from_url(url)
        if not title and not url:
            continue
        seq += 1
        items.append(
            {
                "seq": seq,
                "title": title,
                "url": url,
                "status": "pending",
                "msg": "",
                "file": "",
                "size": 0,
            }
        )
    return items


def parse_file(path):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(path)
    raw = p.read_text("utf-8", errors="ignore")
    items = []
    # markdown / html 链接
    for m in re.finditer(r"\[([^\]]{4,200})\]\((https?://[^\)\s]+)\)", raw):
        title, url = m.group(1).strip(), m.group(2).strip()
        if "cnki.net" in url:
            items.append({"title": title, "url": url})
    # 纯链接
    for u in URL_RE.findall(raw):
        if "cnki.net" in u and not any(i["url"] == u for i in items):
            items.append({"title": _kw_from_url(u), "url": u})
    if not items:
        # 退化为按行解析
        txt = re.sub(r"<[^>]+>", "\n", raw) if p.suffix.lower() in (".html", ".htm") else raw
        items = [{"title": i["title"], "url": i["url"]} for i in parse_lines(txt)]
    out = []
    for n, it in enumerate(items, 1):
        out.append(
            {
                "seq": n,
                "title": it["title"],
                "url": it["url"],
                "status": "pending",
                "msg": "",
                "file": "",
                "size": 0,
            }
        )
    return out


# ---------------- 工作线程 ----------------
def worker(items, outdir, order, delay):
    global ENGINE
    os.makedirs(outdir, exist_ok=True)
    from cnki_engine import Engine

    # Playwright 的 sync 驱动与创建它的线程绑定，worker 每次跑在新线程里，
    # 复用上一次的 ENGINE 会抛 "Playwright connection closed"。这里强制重建。
    if ENGINE is not None:
        try:
            ENGINE.stop_flag = True
            ENGINE.close_browser()
        except Exception:
            pass
        ENGINE = None

    if ENGINE is None:
        ENGINE = Engine(PROFILE, log_cb=add_log, state_cb=state_cb)
    ENGINE.stop_flag = False
    ENGINE.skip_flag = False
    ENGINE.pause_flag = False

    add_log(f"任务开始：{len(items)} 篇 → {outdir}", "ok")
    try:
        ENGINE.ensure_browser()
        state_cb(browser_open=True)
    except Exception as e:
        add_log(f"浏览器启动失败：{e}", "err")
        state_cb(running=False, phase="idle")
        return

    verify_fails = 0
    for it in items:
        if ENGINE.stop_flag:
            add_log("已停止", "warn")
            break
        while ENGINE.pause_flag and not ENGINE.stop_flag:
            time.sleep(0.5)

        it["status"] = "running"
        it["msg"] = "处理中"
        ENGINE.skip_flag = False
        add_log(f"[{it['seq']}] {it['title'][:60]}")
        try:
            ok, msg = ENGINE.download_item(
                {**it, "order": order}, outdir, delay=delay
            )
        except Exception as e:
            ok, msg = False, f"异常：{str(e)[:120]}"
            add_log(f"  ✖ {msg}", "err")
        if ok:
            verify_fails = 0
            fp = Path(outdir)
            cand = sorted(fp.glob(f"{it['seq']:02d}_*.pdf"))
            it["status"] = "done"
            it["msg"] = "已下载"
            if cand:
                it["file"] = str(cand[0])
                it["size"] = cand[0].stat().st_size
        else:
            it["status"] = "fail"
            it["msg"] = msg
            if "验证" in str(msg):
                verify_fails += 1
            else:
                verify_fails = 0
        state_cb(items=items)
        if verify_fails >= 2:
            add_log(
                "连续两篇都卡在安全验证，任务已中止 —— 请先在浏览器窗口里完成登录/验证，再重新点「开始下载」",
                "err",
            )
            state_cb(message="请先在浏览器里完成知网登录与安全验证，然后重新开始")
            break
        if delay and it is not items[-1]:
            time.sleep(max(0, delay))

    done = sum(1 for i in items if i["status"] == "done")
    add_log(f"任务结束：成功 {done} / {len(items)}", "ok" if done else "warn")
    state_cb(running=False, phase="idle", items=items)


# ---------------- HTTP ----------------
def pick_folder():
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.FolderBrowserDialog;"
        "$d.Description='选择 PDF 保存目录';"
        "if($d.ShowDialog() -eq 'OK'){Write-Output $d.SelectedPath}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=180,
        )
        return r.stdout.strip()
    except Exception:
        return ""


def pick_file():
    ps = (
        "Add-Type -AssemblyName System.Windows.Forms;"
        "$d=New-Object System.Windows.Forms.OpenFileDialog;"
        "$d.Filter='清单文件|*.md;*.txt;*.html;*.htm;*.json|所有文件|*.*';"
        "if($d.ShowDialog() -eq 'OK'){Write-Output $d.FileName}"
    )
    try:
        r = subprocess.run(
            ["powershell", "-NoProfile", "-STA", "-Command", ps],
            capture_output=True, text=True, timeout=180,
        )
        return r.stdout.strip()
    except Exception:
        return ""


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=str(WEB), **kw)

    def log_message(self, fmt, *args):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def do_GET(self):
        if self.path.startswith("/api/state"):
            with _lock:
                snap = json.loads(json.dumps(STATE, ensure_ascii=False))
            snap["browser_open"] = bool(ENGINE and ENGINE.ctx is not None)
            return self._json(snap)
        if self.path.startswith("/api/config"):
            return self._json(load_config())
        return super().do_GET()

    def do_POST(self):
        global ENGINE
        p = self.path
        data = self._body()

        if p.startswith("/api/parse"):
            return self._json({"items": parse_lines(data.get("text", ""))})

        if p.startswith("/api/import"):
            try:
                path = data.get("path") or pick_file()
                if not path:
                    return self._json({"items": [], "error": "未选择文件"})
                return self._json({"items": parse_file(path), "path": path})
            except Exception as e:
                return self._json({"items": [], "error": str(e)})

        if p.startswith("/api/pick/folder"):
            return self._json({"path": pick_folder()})

        if p.startswith("/api/pick/file"):
            return self._json({"path": pick_file()})

        if p.startswith("/api/start"):
            if STATE["running"]:
                return self._json({"error": "任务进行中"}, 400)
            items = data.get("items") or []
            if not items:
                return self._json({"error": "没有可下载的条目"}, 400)
            outdir = data.get("outdir") or DEFAULT_CONFIG["outdir"]
            order = data.get("order") or "篇名"
            delay = float(data.get("delay") or 3)
            cfg = load_config()
            cfg.update({"outdir": outdir, "order": order, "delay": delay})
            save_config(cfg)
            STATE.update(
                items=items, running=True, paused=False, phase="running",
                message="", log=[], outdir=outdir, order=order, delay=delay,
            )
            threading.Thread(
                target=worker, args=(items, outdir, order, delay), daemon=True
            ).start()
            return self._json({"ok": True})

        if p.startswith("/api/stop"):
            if ENGINE:
                ENGINE.stop_flag = True
                ENGINE.pause_flag = False
            return self._json({"ok": True})

        if p.startswith("/api/pause"):
            if ENGINE:
                ENGINE.pause_flag = True
            state_cb(paused=True, message="已暂停（当前这篇下载完后停住）")
            return self._json({"ok": True})

        if p.startswith("/api/resume"):
            if ENGINE:
                ENGINE.pause_flag = False
            state_cb(paused=False, message="")
            return self._json({"ok": True})

        if p.startswith("/api/skip"):
            if ENGINE:
                ENGINE.skip_flag = True
            return self._json({"ok": True})

        if p.startswith("/api/browser/open"):
            try:
                if ENGINE is None:
                    from cnki_engine import Engine
                    ENGINE = Engine(PROFILE, log_cb=add_log, state_cb=state_cb)
                ENGINE.open_login(data.get("url") or "https://kns.cnki.net/")
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p.startswith("/api/browser/close"):
            if ENGINE:
                ENGINE.close_browser()
            return self._json({"ok": True})

        if p.startswith("/api/quit"):
            def _bye():
                time.sleep(0.4)
                if ENGINE:
                    ENGINE.stop_flag = True
                    ENGINE.close_browser()
                os._exit(0)
            threading.Thread(target=_bye, daemon=True).start()
            return self._json({"ok": True})

        if p.startswith("/api/open-folder"):
            path = data.get("path") or STATE.get("outdir")
            try:
                os.makedirs(path, exist_ok=True)
                os.startfile(path)
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        return self._json({"error": "not found"}, 404)


def main():
    cfg = load_config()
    STATE["outdir"] = cfg["outdir"]
    add_log("CNKI 批量下载器已启动")
    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    print(f"CNKI Downloader running at http://127.0.0.1:{PORT}")
    srv.serve_forever()


if __name__ == "__main__":
    main()
