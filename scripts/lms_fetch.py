# -*- coding: utf-8 -*-
"""
思源学堂 2.0 (TronClass) —— 批量下载课程附件（课件 + 作业 + 项目压缩包）

附件有两类来源，只看 uploads 字段会漏掉一大半:
  ① 活动 JSON 的 uploads 数组
  ② page 类型活动正文 data.content 里内嵌的 /api/uploads/<id>   ← 课件 PDF 全在这里

下载端点: GET /api/uploads/<id>/blob     元信息: GET /api/uploads/<id>

用法:
    python lms_fetch.py --course 33593 --out "./CV" --dry-run
    python lms_fetch.py --course 33593 --out "./CV"
    python lms_fetch.py --course 33593 --out ./CV --exclude "2020|2021|2022"
    python lms_fetch.py --course 33593 --out ./CV --organize
    python lms_fetch.py --course 33593 --out ./CV --layout flat

目录结构（默认 activity 布局）:
    <out>/{课件,作业}/<活动标题>/<文件名>
加 --organize 后自动按章归并:
    <out>/课件/第01章 绪论/<文件名>
    <out>/课件/其他/<文件名>          ← 认不出章号的
加 --split-projects 后 zip 项目包单独走:
    <out>/项目/<活动标题>/<文件名>

环境要求:
    只需要标准库 + 已跑过 lms_login.py 生成的登录态 JSON，不依赖浏览器。
"""
import argparse
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.request
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lms_common import BASE, HOST, state_path
from lms_organize import parse_chapter, chapter_dir

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

# 常见的项目压缩包 -> 归到 项目/ 而不是 作业/
PROJ_HINT = re.compile(r"project|proj[_\-\s]?\d|zip", re.I)


def safe(s):
    return re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", (s or "").strip())[:80] or "untitled"


def opener(state):
    st = json.load(open(state, encoding="utf-8"))
    cj = http.cookiejar.CookieJar()
    for c in st["cookies"]:
        cj.set_cookie(http.cookiejar.Cookie(
            0, c["name"], c["value"], None, False,
            c.get("domain", HOST), True, True,
            c.get("path", "/"), True, False, None, False, None, None, {}))
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Referer", BASE + "/course/index")]
    return op


def get_json(op, url, timeout=60):
    return json.loads(op.open(url, timeout=timeout).read())


def collect(op, course, activities=None):
    """返回 [(kind, 活动标题, upload_id)]，已去重"""
    if activities is None:
        activities = get_json(op, "%s/api/courses/%s/activities?sub_course_id=0"
                              % (BASE, course))["activities"]
    plan = {}

    def add(kind, act, uid):
        plan.setdefault((kind, safe(act), int(uid)), None)

    # 来源 ①
    for a in activities:
        for u in (a.get("uploads") or []):
            add("作业" if a.get("type") == "homework" else "课件", a.get("title"), u["id"])
    n1 = len(plan)

    # 来源 ②
    for a in activities:
        if a.get("type") != "page":
            continue
        try:
            d = get_json(op, "%s/api/activities/%s?sub_course_id=0" % (BASE, a["id"]))
        except Exception as e:
            print("  详情失败 %s: %s" % (a["id"], str(e)[:50]))
            continue
        blob = json.dumps(d.get("data") or {}, ensure_ascii=False)
        for uid in set(re.findall(r"/api/uploads/(\d+)", blob)):
            add("课件", a.get("title"), uid)
    return sorted(plan.keys()), n1


def meta(op, uid):
    try:
        return get_json(op, "%s/api/uploads/%s" % (BASE, uid))
    except Exception:
        return {}


def main():
    global BASE, HOST
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", required=True)
    ap.add_argument("--out", required=True, help="输出根目录")
    ap.add_argument("--state", default=None, help="登录态 JSON (默认 ~/.lms-grab/state_<ID>.json)")
    ap.add_argument("--base", default=None, help="平台地址, 默认 https://lms.xjtu.edu.cn")
    ap.add_argument("--activities", default=None, help="本地活动清单 JSON, 省一次请求")
    ap.add_argument("--exclude", default=None, help="文件名正则, 命中则跳过")
    ap.add_argument("--layout", choices=["activity", "flat"], default="activity",
                    help="activity=按活动分文件夹(默认)  flat=平铺")
    ap.add_argument("--organize", action="store_true",
                    help="自动按章整理: 课件/第01章 xxx/ ；识别不到章号的放 其他/")
    ap.add_argument("--split-projects", action="store_true",
                    help="把项目压缩包单独放进 项目/ 目录")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="干跑时打印归类后的目标目录")
    args = ap.parse_args()

    if args.base:
        BASE = args.base.rstrip("/")
        HOST = urlparse(BASE).hostname or HOST
    print("平台: %s" % BASE)

    state = args.state or state_path(args.course)
    if not os.path.isfile(state):
        print("!! 没有登录态 %s，先跑 lms_login.py --course %s" % (state, args.course),
              file=sys.stderr)
        return 2
    op = opener(state)

    acts = None
    if args.activities and os.path.isfile(args.activities):
        acts = json.load(open(args.activities, encoding="utf-8")).get("activities")

    print("扫描课程 %s ..." % args.course)
    keys, n1 = collect(op, args.course, acts)
    print("  来源① uploads 字段: %d" % n1)
    print("  来源② 正文内嵌:     %d" % (len(keys) - n1))
    print("  去重后共 %d 个附件" % len(keys))

    excl = re.compile(args.exclude) if args.exclude else None
    ok = fail = skip = 0
    for i, (kind, act, uid) in enumerate(keys, 1):
        m = meta(op, uid)
        if not m:
            # 403 无权限 / 404 资源不存在，服务端限制，不是下载失败
            print("[%2d] N/A   upload %s (平台侧无权限或已删除, 跳过)" % (i, uid))
            skip += 1
            continue
        name = safe(m.get("name") or ("upload_%s" % uid))
        size = m.get("size") or 0
        if excl and excl.search(name):
            print("[%2d] EXCLUDE %s" % (i, name))
            skip += 1
            continue

        if args.split_projects and PROJ_HINT.search(name) and name.lower().endswith(".zip"):
            dest = os.path.join(args.out, "项目", act)
        elif args.organize:
            ch = parse_chapter(act, name)
            if ch is None:
                dest = os.path.join(args.out, kind, "其他")
            else:
                dest = os.path.join(args.out, kind, chapter_dir(ch, [act, name]))
        elif args.layout == "flat":
            dest = os.path.join(args.out, kind)
        else:
            dest = os.path.join(args.out, kind, act)
        path = os.path.join(dest, name)

        if os.path.exists(path) and os.path.getsize(path) > 1024:
            skip += 1
            continue
        if args.dry_run:
            if args.verbose:
                print("[%2d] PLAN  %-4s %-30s -> %s" % (i, kind, name[:28],
                                                        os.path.relpath(dest, args.out)))
            else:
                print("[%2d] PLAN  %-9s %-28s %-44s %8.1fKB"
                      % (i, kind, act[:26], name[:42], size / 1024))
            continue
        os.makedirs(dest, exist_ok=True)
        try:
            r = op.open("%s/api/uploads/%s/blob" % (BASE, uid), timeout=300)
            data = r.read()
            ct = r.headers.get("Content-Type", "")
            if len(data) < 512 or "text/html" in ct:
                print("[%2d] BAD  %s ct=%s %dB" % (i, name[:40], ct[:20], len(data)))
                fail += 1
                continue
            with open(path, "wb") as f:
                f.write(data)
            exp = size
            mark = "" if (not exp or abs(exp - len(data)) < 16) else "  !!SIZE exp=%d" % exp
            print("[%2d] OK   %8.1fKB  %s/%s%s" % (i, len(data) / 1024, act[:18], name[:40], mark))
            ok += 1
        except Exception as e:
            print("[%2d] ERR  %s :: %s" % (i, name[:40], str(e)[:60]))
            fail += 1

    tag = " (dry-run)" if args.dry_run else ""
    print("=== done%s ok=%d fail=%d skip=%d ===" % (tag, ok, fail, skip))
    return 0


if __name__ == "__main__":
    sys.exit(main())
