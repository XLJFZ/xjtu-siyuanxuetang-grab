# -*- coding: utf-8 -*-
"""
思源学堂 2.0 (TronClass) —— 登录一次，把登录态落盘。
后续所有抓取都不再需要浏览器，直接 lms_fetch.py 跑。

用法:
    python lms_login.py --course 33593
    python lms_login.py --course 33593 --state D:/xxx/state.json
    python lms_login.py --course 33593 --wait 400

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

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lms_common import BASE, HOST, find_browser, profile_path, require_playwright, state_path


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", required=True, help="课程 ID, 如 33593")
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

        logged = "login" not in (page.url or "").lower()
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
            if HOST in u and "login" not in u.lower():
                logged = True
            else:
                time.sleep(2)
        if not logged:
            print("!! 登录超时, 未保存登录态", file=sys.stderr)
            ctx.close()
            return 1
        print("登录成功:", page.url[:100])

        ctx.storage_state(path=state)
        n = len(json.load(open(state, encoding="utf-8")).get("cookies", []))
        print("登录态已保存: %s (%d cookies)" % (state, n))

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
