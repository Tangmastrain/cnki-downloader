# -*- coding: utf-8 -*-
"""
CNKI 批量下载引擎
----------------
设计原则：
1. 使用「有头」浏览器 + 持久化用户目录，登录态/机构权限完全由用户本人完成；
2. 遇到安全验证（滑块/点选）或登录页时自动暂停，把控制权交还用户，绝不尝试破解；
3. 每篇文献的多条下载路径按优先级依次尝试，全部失败则进入「人工接管」，
   用户在浏览器里手动点一下下载，程序依然能捕获并归档到目标文件名。
"""
import os
import re
import time
import shutil
import difflib
from pathlib import Path

from playwright.sync_api import sync_playwright

CNKI_HOME = "https://kns.cnki.net/"
SEARCH_URL = "https://kns.cnki.net/kns8s/defaultresult/index?kw={kw}&korder={order}"

ORDER_MAP = {
    "主题": "SU",
    "篇名": "TI",
    "关键词": "KY",
    "作者": "AU",
    "摘要": "AB",
}

# 详情页 PDF 下载按钮候选选择器（按命中概率排序，跨版本兜底）
PDF_SELECTORS = [
    "#pdfDown",
    "a#pdfDown",
    "#DownLoadParts a[href*='pdf']",
    "a[href*='dflag=pdfdown']",
    "a[href*='pdfdown']",
    ".btn-dlpdf a",
    ".operate-btn:has-text('PDF')",
    "a:has-text('PDF下载')",
    "a:has-text('PDF 下载')",
    "button:has-text('PDF下载')",
    "a:has-text('下载PDF')",
    "li:has-text('PDF下载') a",
]

VERIFY_HINTS = ("安全验证", "验证码", "captcha")


def sanitize(name, maxlen=90):
    name = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "")
    name = re.sub(r"\s+", " ", name).strip(" .")
    return name[:maxlen] or "untitled"


def looks_like_pdf(path):
    try:
        with open(path, "rb") as f:
            return f.read(5) == b"%PDF-"
    except Exception:
        return False


class Engine:
    def __init__(self, profile_dir, log_cb=None, state_cb=None):
        self.profile_dir = str(profile_dir)
        self.log_cb = log_cb or (lambda m, lv="info": None)
        self.state_cb = state_cb or (lambda **kw: None)
        self._pw = None
        self.ctx = None
        self.page = None
        self._downloads = []
        self._pdf_urls = []
        self.skip_flag = False
        self.pause_flag = False
        self.stop_flag = False
        self.headless = False
        self.use_proxy = False

    # ---------------- 基础设施 ----------------
    def log(self, msg, level="info"):
        try:
            self.log_cb(msg, level)
        except Exception:
            pass

    def ensure_browser(self):
        if self.ctx is not None:
            try:
                _ = self.ctx.pages
                return self.ctx
            except Exception:
                self.ctx = None
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        if self._pw is None:
            self._pw = sync_playwright().start()

        args = [
            "--disable-blink-features=AutomationControlled",
            "--start-maximized",
            "--lang=zh-CN",
        ]
        if not self.use_proxy:
            args.append("--no-proxy-server")

        last_err = None
        for _attempt in range(2):
            if self._pw is None:
                self._pw = sync_playwright().start()
            for channel in ("chromium", None):
                try:
                    self.ctx = self._pw.chromium.launch_persistent_context(
                        user_data_dir=self.profile_dir,
                        channel=channel,
                        headless=self.headless,
                        accept_downloads=True,
                        locale="zh-CN",
                        viewport=None,
                        args=args,
                        downloads_path=str(Path(self.profile_dir) / "_tmp_downloads"),
                    )
                    break
                except Exception as e:
                    last_err = e
                    self.ctx = None
            if self.ctx is not None:
                break
            # 常见于上次驱动被回收："Playwright connection closed" —— 重建驱动后重试一次
            self.log(f"浏览器启动重试（{str(last_err)[:80]}）", "warn")
            self._reset_pw()
        if self.ctx is None:
            raise RuntimeError(f"浏览器启动失败：{last_err}")

        self.ctx.set_default_timeout(30000)
        # download 是 Page 级事件，需逐页挂载（含后续新开的标签页）
        self.ctx.on("page", self._attach_page)
        self.ctx.on("response", self._on_response)
        for _p in self.ctx.pages:
            self._attach_page(_p)
        self.page = self.ctx.pages[0] if self.ctx.pages else self.ctx.new_page()
        self.log("浏览器已就绪（持久化配置目录，登录态会保留）", "ok")
        return self.ctx

    def _attach_page(self, page):
        try:
            page.on("download", self._on_download)
        except Exception:
            pass

    def _on_download(self, download):
        self._downloads.append(download)

    def _on_response(self, resp):
        try:
            url = resp.url or ""
            if not re.search(r"cnki\.net", url):
                return
            ct = ""
            try:
                ct = (resp.headers or {}).get("content-type", "") or ""
            except Exception:
                pass
            if "application/pdf" in ct.lower() or re.search(
                r"(pdfdown|/pdf/|\.pdf($|\?)|dflag=pdf|download)", url, re.I
            ):
                self._pdf_urls.append(url)
        except Exception:
            pass

    def open_login(self, url=CNKI_HOME):
        self.ensure_browser()
        page = self.page or self.ctx.new_page()
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as e:
            self.log(f"打开页面超时：{e}", "warn")
        self.log("请在浏览器窗口内完成机构登录 / 安全验证，完成后回到本界面点「开始下载」", "ok")
        return page

    def _reset_pw(self):
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

    def close_browser(self):
        try:
            if self.ctx:
                self.ctx.close()
        except Exception:
            pass
        self.ctx = None
        self.page = None
        self._reset_pw()

    # ---------------- 验证/登录拦截 ----------------
    def _is_blocked(self, page):
        try:
            u = page.url or ""
        except Exception:
            return True
        if "/verify/" in u or "captcha" in u.lower():
            return True
        try:
            t = page.title() or ""
        except Exception:
            t = ""
        return any(h in t for h in VERIFY_HINTS)

    def _wait_user(self, page, reason, timeout_min=15):
        """把控制权交还用户，等待页面恢复正常。"""
        self.state_cb(phase="manual", message=reason)
        self.log(f"⏸ {reason} —— 已暂停，请在浏览器窗口处理（最多等待 {timeout_min} 分钟）", "warn")
        end = time.time() + timeout_min * 60
        while time.time() < end:
            if self.stop_flag or self.skip_flag:
                return False
            try:
                page.wait_for_timeout(800)
            except Exception:
                return False
            if not self._is_blocked(page):
                self.log("✔ 验证已通过，继续", "ok")
                self.state_cb(phase="running", message="")
                return True
        self.log("等待超时", "err")
        return False

    def _ensure_ok(self, page):
        for _ in range(2):
            if not self._is_blocked(page):
                return True
            if not self._wait_user(page, "检测到安全验证 / 登录页，需要你手动完成"):
                return False
        return not self._is_blocked(page)

    # ---------------- 检索 ----------------
    def _goto(self, page, url, tries=2):
        for i in range(tries):
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=45000)
                page.wait_for_timeout(1200)
                return True
            except Exception as e:
                if i == tries - 1:
                    self.log(f"页面加载失败：{str(e)[:120]}", "err")
                    return False
                page.wait_for_timeout(1500)
        return False

    def _collect_rows(self, page):
        """从检索结果页抓取 (标题, 链接) 列表"""
        rows = []
        sels = [
            "table.result-table-list tbody tr",
            ".result-table-list tbody tr",
            "table.result-table-list tr",
        ]
        for sel in sels:
            try:
                loc = page.locator(sel)
                n = min(loc.count(), 60)
            except Exception:
                n = 0
            if not n:
                continue
            for i in range(n):
                tr = loc.nth(i)
                try:
                    a = tr.locator("td.name a, a.fz14, td.name a.fz14").first
                    if a.count() == 0:
                        continue
                    title = (a.inner_text() or "").strip()
                    title = re.sub(r"\s+", " ", title)
                    href = a.get_attribute("href") or ""
                    rows.append({"title": title, "href": href, "row": tr})
                except Exception:
                    continue
            if rows:
                break
        return rows

    def _open_detail_from_search(self, page, target_title, idx=0):
        rows = self._collect_rows(page)
        if not rows:
            self.log("检索结果为空（可能未登录 / 无权限 / 页面结构变化）", "err")
            return None
        pick = None
        if target_title:
            best, best_r = -1, 0.0
            for i, r in enumerate(rows):
                ratio = difflib.SequenceMatcher(None, target_title, r["title"]).ratio()
                if ratio > best_r:
                    best, best_r = i, ratio
            if best_r >= 0.55:
                pick = rows[best]
                self.log(f"命中结果：{pick['title'][:50]}（相似度 {best_r:.2f}）")
            else:
                self.log(f"未找到高相似结果（最高 {best_r:.2f}），改取第 {idx + 1} 条", "warn")
        if pick is None:
            pick = rows[min(idx, len(rows) - 1)]
        before = {id(p) for p in self.ctx.pages}
        try:
            a = pick["row"].locator("td.name a, a.fz14").first
            a.click(timeout=10000)
        except Exception as e:
            self.log(f"点击结果失败：{str(e)[:100]}", "warn")
            return None
        page.wait_for_timeout(2500)
        # 可能新开标签页
        for p in self.ctx.pages:
            if id(p) not in before:
                self.page = p
                return p
        return page

    # ---------------- 下载 ----------------
    def _click_and_grab(self, page, locator, timeout=25):
        self._downloads.clear()
        self._pdf_urls.clear()
        before = {id(p) for p in self.ctx.pages}
        try:
            locator.click(timeout=8000)
        except Exception as e:
            return None, f"click_failed: {str(e)[:80]}"

        end = time.time() + timeout
        while time.time() < end:
            try:
                page.wait_for_timeout(400)
            except Exception:
                pass
            if self._downloads:
                return ("download", self._downloads[0]), None
            for p in self.ctx.pages:
                if id(p) not in before:
                    try:
                        u = p.url or ""
                    except Exception:
                        u = ""
                    if u and (".pdf" in u.lower() or "pdfdown" in u.lower() or "download" in u.lower()):
                        return ("url", u), None
            if self._pdf_urls:
                return ("url", self._pdf_urls[-1]), None
            # 点击后可能又弹验证
            if self._is_blocked(page):
                return None, "blocked"
        return None, "no_download"

    def _save_payload(self, payload, out_path):
        kind, obj = payload
        if kind == "download":
            obj.save_as(out_path)
            if looks_like_pdf(out_path) and os.path.getsize(out_path) > 8000:
                return True, "ok"
            if os.path.exists(out_path):
                os.remove(out_path)
            return False, "下载内容不是有效 PDF（可能无权限/需付费）"
        # url
        try:
            r = self.ctx.request.get(obj, timeout=90000)
            body = r.body()
            if body[:5] == b"%PDF-":
                Path(out_path).write_bytes(body)
                return True, "ok"
            return False, f"直链返回非 PDF（HTTP {r.status}）"
        except Exception as e:
            return False, f"直链获取失败：{str(e)[:80]}"

    def _manual_takeover(self, page, out_path, timeout_min=15):
        self.state_cb(phase="manual", message="自动下载未成功，请在浏览器中手动点击下载")
        self.log("⚠ 未自动触发下载，已切到人工接管：请在浏览器窗口点一下「PDF下载」，程序会自动接管文件", "warn")
        self._downloads.clear()
        self._pdf_urls.clear()
        end = time.time() + timeout_min * 60
        while time.time() < end:
            if self.stop_flag or self.skip_flag:
                return False, "已跳过"
            try:
                page.wait_for_timeout(600)
            except Exception:
                pass
            if self._downloads:
                ok, msg = self._save_payload(("download", self._downloads[0]), out_path)
                self.state_cb(phase="running", message="")
                return ok, msg
            if self._pdf_urls:
                ok, msg = self._save_payload(("url", self._pdf_urls[-1]), out_path)
                if ok:
                    self.state_cb(phase="running", message="")
                    return True, msg
        return False, "人工接管超时"

    def download_item(self, item, outdir, delay=3.0):
        """item: {title, url, order}"""
        ctx = self.ensure_browser()
        page = self.page or ctx.new_page()
        try:
            if page.is_closed():
                page = ctx.new_page()
                self.page = page
        except Exception:
            page = ctx.new_page()
            self.page = page
        title = item.get("title") or ""
        url = (item.get("url") or "").strip()

        # 1. 打开目标页
        if url and re.search(r"cnki\.net", url):
            self.log(f"打开：{url[:90]}")
            if not self._goto(page, url):
                return False, "页面打不开"
        elif title:
            kw = item.get("query") or title
            order = ORDER_MAP.get(item.get("order", "篇名"), "TI")
            search = SEARCH_URL.format(kw=_quote(kw), order=order)
            self.log(f"检索：{kw[:50]}")
            if not self._goto(page, search):
                return False, "检索页打不开"
            if not self._ensure_ok(page):
                return False, "验证未通过"
            page = self._open_detail_from_search(page, title)
            if page is None:
                return False, "未找到匹配文献"
            self.page = page
        else:
            return False, "缺少标题与链接"

        if not self._ensure_ok(page):
            return False, "验证未通过"

        # 2. 文件名
        page_title = ""
        try:
            page_title = (page.title() or "").split("-")[0].strip()
        except Exception:
            pass
        fname = title or page_title or item.get("id", "paper")
        out_path = str(Path(outdir) / f"{item.get('seq', 1):02d}_{sanitize(fname)}.pdf")

        # 3. 依次尝试候选按钮
        for sel in PDF_SELECTORS:
            if self.stop_flag or self.skip_flag:
                return False, "已跳过"
            try:
                loc = page.locator(sel).first
                if loc.count() == 0 or not loc.is_visible():
                    continue
            except Exception:
                continue
            self.log(f"尝试下载按钮：{sel}")
            payload, err = self._click_and_grab(page, loc)
            if payload:
                ok, msg = self._save_payload(payload, out_path)
                if ok:
                    size = os.path.getsize(out_path) / 1024 / 1024
                    self.log(f"✔ 已保存 {Path(out_path).name}（{size:.1f} MB）", "ok")
                    return True, "ok"
                self.log(f"  该路径无效：{msg}", "warn")
            elif err == "blocked":
                if not self._ensure_ok(page):
                    return False, "验证未通过"
            else:
                self.log(f"  未触发下载（{err}）", "warn")

        # 4. 全页面兜底：找任何含 PDF 字样的可点元素
        try:
            cand = page.locator("a:visible, button:visible")
            n = min(cand.count(), 400)
            for i in range(n):
                el = cand.nth(i)
                try:
                    txt = (el.inner_text() or "").strip()
                except Exception:
                    continue
                if "PDF" in txt.upper() and len(txt) < 20:
                    payload, err = self._click_and_grab(page, el, timeout=20)
                    if payload:
                        ok, msg = self._save_payload(payload, out_path)
                        if ok:
                            self.log(f"✔ 已保存 {Path(out_path).name}", "ok")
                            return True, "ok"
        except Exception:
            pass

        # 5. 人工接管
        return self._manual_takeover(page, out_path)


def _quote(s):
    from urllib.parse import quote
    return quote(s)
