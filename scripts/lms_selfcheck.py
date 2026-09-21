# -*- coding: utf-8 -*-
"""
安装后自检 —— 确认这套脚本在本机真的能跑起来。

为什么需要它：这个项目的失败模式几乎都是「环境问题」而不是「逻辑问题」——
python 版本太老、playwright 没装、浏览器探测不到、登录态文件缺失或形状不对。
这些问题如果没有自检，会一直潜伏到正式下载时才以一个含糊的报错冒出来，
用户会误以为是「平台没权限」。

自检只做只读动作。**唯一一次网络请求需要显式加 `--online`**，
因为「装好了没」和「登录态还有效吗」是两件事，不该在安装阶段就默认触碰服务端。

用法:
    python scripts/lms_selfcheck.py --course <课程ID>          # 离线自检
    python scripts/lms_selfcheck.py --course <课程ID> --online # 顺带探测登录态

退出码:
    0  全部通过（可能有警告）
    1  存在失败项
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lms_common import (BASE, cache_dir, describe_state, find_browser,   # noqa: E402
                        profile_path, state_path)

# 依赖标准库的脚本 —— 这几个缺任何一个整条链路都不通
CORE_SCRIPTS = [
    ("lms_common.py", "公共配置（域名 / 缓存目录 / 浏览器探测）"),
    ("lms_fetch.py", "下载主体"),
    ("lms_organize.py", "章节解析"),
    ("lms_live.py", "直播回放下载"),
]

MIN_PY = (3, 8)

_PASS, _WARN, _FAIL = "PASS", "WARN", "FAIL"


class Report:
    """收集检查结果，最后统一输出，避免边检查边刷屏看不清全貌。"""

    def __init__(self):
        self.rows = []

    def add(self, status, name, detail="", fix=""):
        self.rows.append({"status": status, "name": name,
                          "detail": detail, "fix": fix})

    def passed(self, name, detail="", fix=""):
        self.add(_PASS, name, detail, fix)

    def warned(self, name, detail="", fix=""):
        self.add(_WARN, name, detail, fix)

    def failed(self, name, detail="", fix=""):
        self.add(_FAIL, name, detail, fix)

    @property
    def n_fail(self):
        return sum(1 for r in self.rows if r["status"] == _FAIL)

    @property
    def n_warn(self):
        return sum(1 for r in self.rows if r["status"] == _WARN)

    def render(self):
        mark = {_PASS: "✓", _WARN: "!", _FAIL: "✗"}
        print("")
        print("=" * 58)
        print("  自检结果")
        print("=" * 58)
        for r in self.rows:
            print("  %s %-28s %s" % (mark[r["status"]], r["name"], r["detail"]))
        fixes = [r for r in self.rows if r["fix"] and r["status"] != _PASS]
        if fixes:
            print("\n  需要处理:")
            for r in fixes:
                print("    - %s: %s" % (r["name"], r["fix"]))
        n_ok = sum(1 for r in self.rows if r["status"] == _PASS)
        print("\n  %d 项通过 / %d 项警告 / %d 项失败" % (n_ok, self.n_warn, self.n_fail))
        if self.n_fail:
            print("  结论: 环境不可用，先按上面的「需要处理」逐条修。")
        elif self.n_warn:
            print("  结论: 核心可用，警告项按需处理。")
        else:
            print("  结论: 环境就绪。")
        return 1 if self.n_fail else 0


# ---------------------------------------------------------------- 各项检查

def check_python(rep):
    if sys.version_info >= MIN_PY:
        rep.passed("Python 版本", "%d.%d.%d" % sys.version_info[:3])
    else:
        rep.failed("Python 版本", "%d.%d.%d（需要 %d.%d+）" % (
            sys.version_info[0], sys.version_info[1], sys.version_info[2],
            MIN_PY[0], MIN_PY[1]),
            "换 3.8 以上的 Python 再跑")


def check_layout(rep):
    """脚本目录里的文件是否齐全、能否真的 import 进来。

    注意：这里逐个真 import，而不是 os.path.isfile —— 文件在但语法错、
    内部依赖缺失（比如 lms_live 漏了某个 import）都要在自检阶段暴露。
    """
    missing = []
    broken = []
    for fn, desc in CORE_SCRIPTS:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), fn)
        if not os.path.isfile(path):
            missing.append(fn)
            continue
        mod = fn[:-3]
        try:
            __import__(mod)
        except Exception as e:
            broken.append("%s(%s: %s)" % (fn, e.__class__.__name__, str(e)[:50]))

    if missing:
        rep.failed("脚本完整性", "缺 %s" % "、".join(missing),
                   "仓库不完整，重新解压一份完整的包")
    elif broken:
        rep.failed("脚本可导入", "失败 %s" % "；".join(broken),
                   "脚本可能被改坏或 Python 版本不兼容，看完整报错："
                   "python -c \"import lms_fetch\"")
    else:
        rep.passed("脚本完整性", "%d 个核心脚本齐全且可导入" % len(CORE_SCRIPTS))


def check_playwright(rep):
    """playwright 只有登录脚本需要，所以缺了算警告不算失败。"""
    try:
        import playwright  # noqa: F401
    except ImportError:
        rep.warned("playwright", "未安装（仅影响登录这一步）",
                   "pip install playwright")
        return

    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
        rep.passed("playwright", "已安装")
    except Exception as e:
        rep.failed("playwright", "装了但导入失败: %s" % str(e)[:50],
                   "重装一次: pip install --force-reinstall playwright")


def check_browser(rep):
    """浏览器探测。找不到不算致命 —— playwright 自带 chromium 也能兜底。"""
    b = find_browser()
    if b:
        rep.passed("浏览器", os.path.basename(b))
    else:
        rep.warned("浏览器", "没探测到 Edge / Chrome",
                   "装一个系统 Edge / Chrome，或用 LMS_BROWSER 指定 exe 绝对路径；"
                   "也可以跑 playwright install chromium 用自带内核")


def check_state(rep, course):
    """检查登录态文件是否存在、是不是一份形状合法的 storage_state。

    形状检查是必要的：常见的损坏是「文件在但 cookies 为空」
    （登录流程中途被打断），这种拿去做请求会全部 401，
    从报错上看和「登录态过期」一模一样，很难区分。
    """
    p = state_path(course)
    status, why, _n = describe_state(p)

    if status == "ok":
        rep.passed("登录态", why)
        return p
    if status == "missing":
        rep.warned("登录态", "还没有 %s" % os.path.basename(p),
                   "python scripts/lms_login.py --course %s" % course)
        return None
    if status == "bad":
        rep.failed("登录态", why, "删掉 %s 重新登录" % p)
        return None
    if status == "empty":
        rep.failed("登录态", "%s（登录流程没走完）" % why,
                   "重跑 lms_login.py --course %s" % course)
        return p
    # partial
    rep.warned("登录态", why, "多半是登录时没真的进入课程页，重跑一次登录")
    return p


def check_profile(rep, course):
    """持久化 profile 是「第二次不用再登录」的关键，缺了只是多登录一次。"""
    d = profile_path(course)
    if os.path.isdir(d):
        rep.passed("浏览器 profile", os.path.basename(d))
    else:
        rep.warned("浏览器 profile", "还没生成（第一次登录时创建）",
                   "属正常现象，跑一次登录就会有")


def check_cache(rep):
    d = cache_dir()
    if os.path.isdir(d):
        rep.passed("缓存目录", d)
    else:
        rep.warned("缓存目录", "%s 不可用" % d,
                   "设一个可写的 LMS_CACHE 环境变量")


def check_online(rep, state):
    """可选的在线探测：确认登录态在服务端还认。

    ★ 三态语义（对应 lms_fetch.api_status）：
        valid    -> PASS   服务端确认登录态有效
        invalid  -> FAIL   服务端明确拒绝（登录页 / 401/403）——
                            唯一该让用户重新登录的形态
        unknown  -> WARN   无法判定（网关拦页 / 非 401/403 的 HTTP 错误 /
                            网络异常 / 本地脚本问题）

    ★ unknown 绝不能报 PASS：探测通道本身故障不等于登录态失效，
    报 PASS 会让用户带着过期登录态白跑一趟下载；也不报 FAIL ——
    网络抖一次就把用户赶去重新登录，同样是在制造错误结论。
    """
    if not state:
        rep.warned("登录态探测", "跳过（没有登录态文件）")
        return
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from lms_fetch import api_status, opener
        status, why = api_status(opener(state))
    except Exception as e:
        rep.warned("登录态探测", "无法执行: %s" % str(e)[:50],
                   "先确认基础脚本没问题，再单独跑 --online")
        return
    if status == "valid":
        rep.passed("登录态探测", why)
    elif status == "invalid":
        rep.failed("登录态探测", why,
                   "重新登录: python scripts/lms_login.py --course <课程ID>")
    else:
        rep.warned("登录态探测", "%s（无法判定，不作为通过）" % why,
                   "稍后重跑 --online；连续 unknown 再查网络 / 代理")


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(description="xjtu-lms-grab 安装后自检")
    ap.add_argument("--course", required=True, help="课程 ID（登录态按课程分开存）")
    ap.add_argument("--online", action="store_true",
                    help="额外打一次接口，确认登录态在服务端仍然有效")
    ap.add_argument("--quiet", action="store_true", help="只输出结论")
    args = ap.parse_args()

    rep = Report()
    print("自检目标: %s" % BASE)
    print("课程 ID : %s" % args.course)

    check_python(rep)
    check_layout(rep)
    check_playwright(rep)
    check_browser(rep)
    check_cache(rep)
    state = check_state(rep, args.course)
    check_profile(rep, args.course)
    if args.online:
        check_online(rep, state)

    rc = rep.render()

    if not args.online:
        print("\n  提示: 加 --online 可以顺带确认登录态在服务端是否仍然有效。")
    if not os.environ.get("LMS_BROWSER") and find_browser() is None:
        print("  提示: 也可以直接装好浏览器后重跑本自检。")
    return rc

if __name__ == "__main__":
    sys.exit(main())
