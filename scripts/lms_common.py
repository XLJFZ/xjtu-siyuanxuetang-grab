# -*- coding: utf-8 -*-
"""
xjtu-lms-grab —— 公共配置

把「本机专属」的东西全部集中到这里，并改成自动探测 + 环境变量可覆盖，
这样换一台电脑不需要改代码。

可用环境变量:
    LMS_BASE     平台地址, 默认 https://lms.xjtu.edu.cn
                 (别的学校用同一套 TronClass 的话改这个就行, 如 https://lms.xxxx.edu.cn)
    LMS_CACHE    登录态 / profile 存放目录, 默认 ~/.lms-grab
    LMS_BROWSER  强制指定浏览器可执行文件绝对路径
"""
import json
import os
import shutil
import sys
from urllib.parse import urlparse

# 平台地址。注意: 不带末尾斜杠
BASE = os.environ.get("LMS_BASE", "https://lms.xjtu.edu.cn").rstrip("/")

# 从这个域名读 cookie
HOST = urlparse(BASE).hostname or "lms.xjtu.edu.cn"


def cache_dir():
    """登录态 / 浏览器 profile 的默认存放位置: ~/.lms-grab"""
    d = os.environ.get("LMS_CACHE") or os.path.join(os.path.expanduser("~"), ".lms-grab")
    os.makedirs(d, exist_ok=True)
    return d


def state_path(course):
    return os.path.join(cache_dir(), "state_%s.json" % course)


def profile_path(course):
    return os.path.join(cache_dir(), "profile_%s" % course)


def describe_state(path):
    """检查一份登录态文件，返回 (状态, 说明, cookie数)。

    状态取值:
        "missing"  文件不存在
        "bad"      存在但读不出来 / 结构不对            —— 需要重新登录
        "empty"    cookies 为空（登录流程没走完）
        "partial"  cookies 里没有属于目标域名的
        "ok"       可用

    两种格式都认（都只看 cookies 数组）:
        ① Playwright 原生 storage_state ：{"cookies": [...], "origins": [...]}
        ② 项目自定义精简结构            ：{"version":1, "host":..., "cookies":[...]}
    ② 是从 lms_login 起使用的新格式，落盘的敏感信息更少；旧文件继续可用。

    单独抽出来是因为「文件在但内容是空的」和「文件压根不在」从报错上看不出区别，
    而这两种情况的处理方式完全不同。
    """
    if not path or not os.path.isfile(path):
        return "missing", "没有 %s" % (path or ""), 0
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
    except (ValueError, OSError) as e:
        return "bad", "解析失败: %s" % str(e)[:60], 0
    if isinstance(st, list):
        st = {"cookies": st}              # 裸数组也算一种极简格式
    if not isinstance(st, dict) or not isinstance(st.get("cookies"), list):
        return "bad", "结构不对（缺 cookies 数组）", 0
    cookies = st["cookies"]
    if not cookies:
        return "empty", "cookies 为空", 0
    fmt = "新格式" if st.get("version") else "storage_state"
    n_host = sum(1 for c in cookies if _cookie_host_ok(c.get("domain")))
    if not n_host:
        return "partial", "%d 个 cookie，但都不属于 %s" % (len(cookies), HOST), len(cookies)
    return "ok", "%d 个 cookie（%d 个属于 %s）[%s]" % (
        len(cookies), n_host, HOST, fmt), len(cookies)


def _cookie_host_ok(domain):
    """cookie 的 domain 是否覆盖当前目标 host（含子域匹配）"""
    dom = str(domain or "").lstrip(".").lower()
    if not dom:
        return True
    t = HOST.lower()
    return t == dom or t.endswith("." + dom)


def find_browser():
    """
    自动找一台机器上能用的 Chromium 系浏览器。
    顺序: 环境变量 -> Edge -> Chrome -> Playwright 自带 Chromium(返回 None 由调用方兜底)
    找不到时返回 None，调用方不传 executable_path 就会用 playwright 自带的 chromium。
    """
    forced = os.environ.get("LMS_BROWSER")
    if forced and os.path.isfile(forced):
        return forced

    cands = []
    if sys.platform == "win32":
        pf = os.environ.get("ProgramFiles", r"C:\Program Files")
        pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
        lad = os.environ.get("LOCALAPPDATA", "")
        cands = [
            os.path.join(pf86, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(lad, r"Microsoft\Edge\Application\msedge.exe"),
            os.path.join(pf, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(pf86, r"Google\Chrome\Application\chrome.exe"),
            os.path.join(lad, r"Google\Chrome\Application\chrome.exe"),
        ]
    elif sys.platform == "darwin":
        cands = [
            "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
            "/Applications/Google Chrome.app/Contents/Google Chrome",
        ]
    else:
        for exe in ("microsoft-edge", "google-chrome", "chromium", "chromium-browser"):
            p = shutil.which(exe)
            if p:
                cands.append(p)

    for p in cands:
        if p and os.path.isfile(p):
            return p
    return None


def require_playwright():
    """导入 playwright，没装就给一句能直接复制的安装命令"""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        print(
            "缺少 playwright。装一下:\n"
            "    pip install playwright\n"
            "（用系统自带的 Edge/Chrome 就够了，不必跑 playwright install 下载浏览器内核）",
            file=sys.stderr,
        )
        sys.exit(3)
