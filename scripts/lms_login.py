# -*- coding: utf-8 -*-
"""
思源学堂 2.0 —— 登录一次，把登录态落盘。
后续所有下载都不再需要浏览器，直接 lms_fetch.py 跑。

只处理当前账号有权访问的课程资源。

用法:
    python lms_login.py --course <课程ID>
    python lms_login.py --course <课程ID> --state D:/xxx/state.json
    python lms_login.py --course <课程ID> --wait 400

环境要求:
    pip install playwright
    浏览器自动探测 Edge -> Chrome -> playwright 自带 chromium，
    也可以用 LMS_BROWSER="D:/xxx/msedge.exe" 强制指定。
"""
import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lms_common import BASE, HOST, find_browser, profile_path, require_playwright, state_path


class StateNotSaved(Exception):
    """登录态没有落盘（没有可用 cookie / 写入失败）—— 调用方按失败退出。"""


def is_lms_url(url):
    """页面 URL 是否真的落在 LMS 站内。

    ★ 必须用 urlparse 取 hostname 做精确 / 子域匹配，禁止字符串包含式判断
    （`HOST in url` 会被 `?service=https://lms.xjtu.edu.cn/...` 这类**出现在
    查询参数里的 HOST** 骗过 —— CAS 登录页 URL 恰恰都带这个参数，
    旧实现因此把「没登录」误判成「已有登录态」）。
    """
    try:
        h = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    if not h:
        return False
    t = HOST.lower()
    return h == t or h.endswith("." + t)


def save_state(ctx, state, course):
    """只保存下载真正需要的登录态，并收紧文件权限。

    ★ 为什么不直接用 ctx.storage_state()：它会把整个浏览器存储快照都写下来
    （localStorage / sessionStorage / 所有域名的 cookie），
    而下载器只需要 LMS 自己域名的 cookie。落盘的东西越少越安全。

    写出的是项目自定义结构（与 Playwright 的原生 storage_state 不同，
    lms_fetch.load_state 两种都能读）：

        {"version": 1, "base": ..., "host": ..., "course": ...,
         "created_at": ..., "cookies": [...]}

    落盘前有硬门禁：**一个适用于当前 HOST 的 cookie 都没有就拒绝写**。
    「URL 看起来对」不等于「登录成功了」—— SSO 中间页 / 部分跳转都可能
    骗过 URL 判断，此时落盘只会产生一份 describe_state 判为 empty/partial
    的废文件，还可能覆盖掉原本正常的历史登录态。

    临时文件安全（fail-closed）：
      POSIX 上 .tmp 从**创建那一刻**就是 0600（os.open(mode=0o600)），
      不做「先普通 open 写完再 chmod」—— 那样默认 umask 宽的机器上，
      带 cookie 的 .tmp 会以 0644 短暂存在；写入中途异常退出时残留在盘上。
      任何一步失败都会删掉 .tmp 再抛出，绝不留下半成品。
      Windows 不伪造 POSIX mode 语义（它靠 ACL，chmod 没有实际意义）。
    """
    cookies = ctx.cookies([BASE])
    # 只留当前 host 的 cookie（ctx.cookies(urls) 已按 URL 过滤，这里再兜一层）
    keep = []
    for c in cookies or []:
        dom = str(c.get("domain") or "").lstrip(".").lower()
        if not dom or HOST.lower() == dom or HOST.lower().endswith("." + dom):
            keep.append(c)

    if not keep:
        raise StateNotSaved(
            "没有获取到任何属于 %s 的 cookie —— 登录未真正完成，"
            "未写入 %s（已有登录态保持不动）" % (HOST, state))

    payload = {
        "version": 1,
        "base": BASE,
        "host": HOST,
        "course": str(course),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cookies": keep,
    }
    tmp = state + ".tmp"
    try:
        if os.name == "posix":
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            f = os.fdopen(fd, "w", encoding="utf-8")
        else:
            f = open(tmp, "w", encoding="utf-8")
        with f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, state)
    except BaseException:
        # 半成品里是 cookie —— 任何失败路径都不许把它留在盘上
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name == "posix":
        try:
            # os.replace 保留了 .tmp 的 0600；这里兜底，防御 umask 之外的意外
            os.chmod(state, 0o600)
        except OSError:
            pass
    return keep


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", required=True, help="课程 ID（从课程链接里的 /course/<ID>/ 取）")
    ap.add_argument("--state", default=None, help="登录态输出路径 (默认 ~/.lms-grab/state_<ID>.json)")
    ap.add_argument("--profile", default=None, help="Playwright 持久化 profile 目录")
    ap.add_argument("--wait", type=int, default=300, help="等待手动登录的秒数")
    args = ap.parse_args()

    state = args.state or state_path(args.course)
    profile = args.profile or profile_path(args.course)
    os.makedirs(os.path.dirname(os.path.abspath(state)), exist_ok=True)

    sync_playwright = require_playwright()

    edge = find_browser()
    if edge:
        print("浏览器: %s" % edge)
    else:
        print("没找到系统浏览器, 用 playwright 自带 chromium"
              "（若报缺少内核, 跑一次: playwright install chromium）")

    url = "%s/course/%s/index" % (BASE, args.course)
    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            user_data_dir=profile, executable_path=edge, headless=False,
            accept_downloads=True, viewport=None,
            args=["--no-first-run", "--no-default-browser-check"],
        )
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.goto(url, timeout=90000, wait_until="domcontentloaded")
        print("已打开:", page.url)

        # ★ 登录成功 = 页面 host 真的回到 LMS 域（is_lms_url 做 hostname
        #   精确 / 子域匹配）。落盘前 save_state 还会再验一次「有适用的
        #   cookie」，两道门都过才算数 —— 单看 URL 会被 SSO 中间页骗过。
        logged = is_lms_url(page.url)
        if logged:
            print("profile 里已有登录态, 跳过手动登录")
        else:
            print(">>> 请在弹出的浏览器窗口里完成统一身份认证 <<<")
        t0, last = time.time(), -1
        while (not logged) and time.time() - t0 < args.wait:
            u = page.url or ""
            el = int(time.time() - t0)
            if el // 20 != last // 20:
                last = el
                print("  等待 %3ds  url=%s" % (el, u[:90]), flush=True)
            if is_lms_url(u):
                logged = True
            else:
                time.sleep(2)
        if not logged:
            print("!! 登录超时, 未保存登录态", file=sys.stderr)
            ctx.close()
            return 1
        print("登录成功:", page.url[:100])

        try:
            cookies = save_state(ctx, state, args.course)
        except StateNotSaved as e:
            # URL 在 LMS 域内但一个适用 cookie 都没有：视为登录未完成。
            # 不写盘、不覆盖旧登录态，按失败退出（exit 1）。
            print("!! %s" % e, file=sys.stderr)
            print("   请在这个窗口里完成登录后重跑本命令", file=sys.stderr)
            try:
                ctx.close()
            except Exception:
                pass
            return 1
        print("登录态已保存: %s (%d cookies)" % (state, len(cookies)))
        print("  注意: 该文件等同于你的登录凭据, 不要分享或提交到 Git")

        # 顺手把活动清单拉下来, 省得 fetch 阶段再问一次
        try:
            page.goto("%s/api/courses/%s/activities?sub_course_id=0"
                      % (BASE, args.course), timeout=60000)
            txt = page.inner_text("body")
            acts_path = os.path.join(os.path.dirname(os.path.abspath(state)),
                                     "activities_%s.json" % args.course)
            with open(acts_path, "w", encoding="utf-8") as f:
                f.write(txt)
            j = json.loads(txt)
            print("活动清单已保存: %s (%d 条)" % (acts_path, len(j.get("activities", []))))
        except Exception as e:
            print("活动清单抓取失败(不影响登录态): %s" % str(e)[:100])
        try:
            ctx.close()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
