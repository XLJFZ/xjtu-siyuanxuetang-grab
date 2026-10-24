# -*- coding: utf-8 -*-
"""
思源学堂 2.0 —— 登录一次，把登录态落盘。
后续所有下载都不再需要浏览器，直接 lms_fetch.py 跑。

只处理当前账号有权访问的课程资源。

用法:
    python lms_login.py --course <课程ID>
    python lms_login.py --course <课程ID> --state D:/xxx/state.json
    python lms_login.py --course <课程ID> --wait 400
    python lms_login.py --course <课程ID> --check     # 只探测，绝不开浏览器
    python lms_login.py --course <课程ID> --force     # 无视已有登录态，强制重登

环境要求:
    pip install playwright
    浏览器自动探测 Edge -> Chrome -> playwright 自带 chromium，
    也可以用 LMS_BROWSER="D:/xxx/msedge.exe" 强制指定。

登录判定的两条通道（v1.7.0）
---------------------------
① 页面 URL 落在 LMS 域 —— 免费、快，但**只在 Playwright 看得见那个标签页时有效**；
② 浏览器 cookie 罐里已有可用凭据，且拿它打一次接口被服务端认 —— 慢一次 HTTP，
   但**与「认证发生在哪个窗口」完全无关**。

② 是唯一可信的判据。真实故障（2026-10-24）：统一身份认证在别处完成后，
   被盯着的标签页 URL 一直停在 CAS 登录页，脚本傻等到 420s 上限报「登录超时」，
   而 profile 里的 cookie 其实早就写全 —— 用户白等 7 分钟，还得再登录一次。
   加上 ② 之后，认证一落地最多 3s 就被认出来。
"""
import argparse
import json
import os
import sys
import time
from urllib.parse import urlparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lms_common import (BASE, HOST, describe_state, find_browser, profile_path,
                        require_playwright, state_path)

# 复用 lms_fetch 的 cookie→opener 与接口探测，避免两套「登录态还活着吗」的判据。
# 登录判断与下载器预检必须用同一份实现，否则两者的结论会飘。
import lms_fetch as F

# 轮询节奏（秒）。URL 检查是白捡的，每次都做；cookie 探针要发 HTTP，
# 只在「cookie 集合变了」或「距上次探测满心跳」时才打，避免空转刷接口。
URL_POLL = 0.5
PROBE_MIN_GAP = 3.0
PROBE_HEARTBEAT = 10.0


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


def _live_url(page):
    """读一个 page 的当前 URL；页面已关闭 / 上下文已销毁时返回空串。"""
    try:
        return page.url or ""
    except Exception:
        return ""


def any_logged_page(ctx):
    """遍历**所有**标签页，返回 (page, url) —— 第一个已落在 LMS 域的。

    ★ 为什么不能只看 ctx.pages[0]：认证跳转可能发生在**新标签页**里
    （身份提供方 window.open，或用户自己开了新页去认证），而被盯着的
    那个 tab 会一直停在登录页。表现是「等满 --wait 仍报超时」，但
    profile 里的 cookie 其实早已写全 —— 实测踩过：用户明明认证成功，
    脚本却报「登录超时、未保存登录态」，逼着人再认证一次。

    命中时把 page 一起返回：后续抓活动清单必须用**登录成功的那个标签页**，
    否则会在停在 CAS 页的 tab 上 inner_text，什么也抓不到。
    """
    try:
        pages = list(ctx.pages)
    except Exception:
        return None, None
    for p in pages:
        u = _live_url(p)
        if is_lms_url(u):
            return p, u
    return None, None


def cookies_for_host(cookies):
    """只留适用于当前 HOST 的 cookie（domain 精确 / 子域匹配）。

    独立成函数是因为它有三处调用方：落盘前的硬门禁、运行中的探针、
    以及 --check。三处口径必须一模一样，否则会出现
    「判断说有 cookie、落盘时又没有」的自相矛盾。
    """
    keep = []
    for c in cookies or []:
        dom = str(c.get("domain") or "").lstrip(".").lower()
        if not dom or HOST.lower() == dom or HOST.lower().endswith("." + dom):
            keep.append(c)
    return keep


def cookie_sig(cookies):
    """cookie 集合的指纹，用来判断「认证动作是否已经发生」。

    只比名字 + 值 + 域，不参与任何输出 —— 指纹不是凭据，但也别打印。
    """
    parts = sorted("%s=%s@%s" % (c.get("name") or "", c.get("value") or "",
                                 c.get("domain") or "") for c in cookies or [])
    return "|".join(parts)


def should_probe(cookies, last_sig, last_probe, now):
    """这一轮要不要发探针请求（频率控制的唯一判据）。

    ★ 三条同时成立才发：
      ① 有适用于本 HOST 的 cookie —— 没有就问也没用；
      ② 距上次至少 PROBE_MIN_GAP —— URL 轮询很密，不设下限会把接口打成高频；
      ③ cookie 集合变了（认证动作的迹象），或距上次已满 PROBE_HEARTBEAT。
    去掉 ② 会在登录页每 0.5s 打一次接口；去掉 ① 会在还没登录时空转；
    去掉 ③ 的「变了」那一半，认证完成要等满一个心跳才被发现。
    """
    if not cookies:
        return False
    gap = now - last_probe
    if gap < PROBE_MIN_GAP:
        return False
    return cookie_sig(cookies) != last_sig or gap >= PROBE_HEARTBEAT


def state_age_days(path):
    """登录态文件是几天前写的；读不出来返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        # 形如 "2026-10-17T08:54:00+0800"：取前 19 位即可，时区偏移忽略
        t = time.mktime(time.strptime(str(st.get("created_at") or "")[:19],
                                      "%Y-%m-%dT%H:%M:%S"))
        return (time.time() - t) / 86400.0
    except Exception:
        return None


def api_status_of(cookies):
    """用一批 cookie 打一次轻量接口，返回 lms_fetch 的三态 (status, why)。

    valid / invalid / unknown —— 与 selfcheck --online、fetch 预检同一套语义。
    """
    if not cookies_for_host(cookies):
        return F.API_INVALID, "还没有属于 %s 的 cookie" % HOST
    try:
        return F.api_status(F.opener_from_cookies(cookies))
    except Exception as e:                      # 本地错误（cookie 结构异常等）
        return F.API_UNKNOWN, "探测失败: %s" % str(e)[:60]


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

    真正的写盘实现只有一份（_write_payload），探针命中与 URL 命中两条路径
    产出完全相同的文件结构与安全语义（POSIX 上 .tmp 创建即 0600、
    任何失败路径都删掉半成品）。
    """
    keep = cookies_for_host(ctx.cookies([BASE]))

    if not keep:
        raise StateNotSaved(
            "没有获取到任何属于 %s 的 cookie —— 登录未真正完成，"
            "未写入 %s（已有登录态保持不动）" % (HOST, state))

    return _write_payload(state, course, keep)


def save_activities(state, course):
    """用已落盘的 cookie 直接把活动清单抓下来（不再让浏览器导航一次）。

    旧实现是 `page.goto(api_url)` 再从 inner_text 取正文 —— 多一次页面
    渲染（约 1~3s），而且要求那个标签页还活着。换成普通 HTTP 请求后
    与浏览器无关，失败也只影响这份缓存（fetch 阶段会自己重新拉）。
    """
    try:
        op = F.opener(state)
        data = F.get_json(op, "%s/api/courses/%s/activities?sub_course_id=0"
                          % (BASE, course), retries=2)
        acts_path = os.path.join(os.path.dirname(os.path.abspath(state)),
                                 "activities_%s.json" % course)
        with open(acts_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print("活动清单已保存: %s (%d 条)"
              % (acts_path, len(data.get("activities") or [])))
    except Exception as e:
        print("活动清单抓取失败(不影响登录态): %s" % str(e)[:100])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--course", required=True, help="课程 ID（从课程链接里的 /course/<ID>/ 取）")
    ap.add_argument("--state", default=None, help="登录态输出路径 (默认 ~/.lms-grab/state_<ID>.json)")
    ap.add_argument("--profile", default=None, help="Playwright 持久化 profile 目录")
    ap.add_argument("--wait", type=int, default=300, help="等待手动登录的秒数")
    ap.add_argument("--check", action="store_true",
                    help="只探测既有登录态是否还有效，不开浏览器")
    ap.add_argument("--force", action="store_true",
                    help="即使既有登录态仍然有效，也强制打开浏览器重新登录")
    args = ap.parse_args()

    state = args.state or state_path(args.course)
    profile = args.profile or profile_path(args.course)
    os.makedirs(os.path.dirname(os.path.abspath(state)), exist_ok=True)

    # ---------------- 快路径：既有登录态还有效就根本不开浏览器 ----------------
    # 这是最省时的一步：省掉浏览器启动 + 导航 + 检测 ≈ 15~25s，换成一次 HTTP 探测。
    # 语义上不改变任何结果 —— 能下载就继续用旧登录态，不能下载才去重新认证。
    t_edge = time.time()
    st, why, _n = describe_state(state)

    if args.check:
        # --check 只回答「还能用吗」，绝不替用户开浏览器
        if st != "ok":
            print("✗ 登录态不可用: %s —— %s" % (st, why), file=sys.stderr)
            return 2
        status, reason = api_status_of(_load_cookies(state))
        if status == F.API_VALID:
            print("✓ %s" % reason)
            return 0
        print("✗ %s" % reason, file=sys.stderr)
        return 3

    if st == "ok" and not args.force:
        age = state_age_days(state)
        status, reason = api_status_of(_load_cookies(state))
        if status == F.API_VALID:
            print("登录态仍有效%s：无需打开浏览器（判定耗时 %.2fs）"
                  % ("（%d 天前创建）" % int(age) if age is not None else "",
                     time.time() - t_edge))
            print("  %s" % reason)
            save_activities(state, args.course)
            print("=== login ok (cached, %.2fs) ===" % (time.time() - t_edge))
            return 0
        if status == F.API_INVALID:
            print("登录态已失效（%s），开始重新登录" % reason, flush=True)
        else:
            print("登录态无法判定（%s），按需重新登录" % reason, flush=True)

    t_start = time.time()
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

        # ★ 登录成功有两道判据，**先免费后付费**：
        #   ① 任意标签页的 host 回到 LMS 域（is_lms_url 精确/子域匹配）—— 0 成本；
        #   ② 拿浏览器 cookie 罐里的凭据打一次接口，被服务端认 —— 一次 HTTP。
        #   只靠 ① 会漏：认证若发生在别处，URL 永远不变（2026-10-24 实测等满 420s）。
        #   只靠 ② 会慢：每轮都要发请求。两个一起用，命中即退出。
        hit_page, logged_url = any_logged_page(ctx)
        if hit_page is not None:
            page = hit_page
        if logged_url:
            print("profile 里已有登录态, 跳过手动登录")
        else:
            print(">>> 请在弹出的浏览器窗口里完成统一身份认证 <<<")
        t0, last = time.time(), -1
        last_sig, last_probe, last_status = None, 0.0, "等待认证"
        while (not logged_url) and time.time() - t0 < args.wait:
            el = int(time.time() - t0)
            # 窗口被关掉 = 用户放弃，别傻等到上限
            try:
                open_pages = list(ctx.pages)
            except Exception:
                open_pages = []
            if not open_pages:
                print("!! 浏览器窗口已关闭，放弃等待", file=sys.stderr)
                ctx.close()
                return 1

            hit_page, hit_url = any_logged_page(ctx)
            if hit_url:
                page, logged_url = hit_page, hit_url
                break

            # cookie 探针：认证一落地就能认出来，与认证发生在哪个窗口无关
            ck = cookies_for_host(ctx.cookies([BASE]))
            now = time.time()
            if should_probe(ck, last_sig, last_probe, now):
                last_sig, last_probe = cookie_sig(ck), now
                status, reason = api_status_of(ck)
                last_status = reason
                if status == F.API_VALID:
                    print("检测到有效登录态（%s）" % reason, flush=True)
                    # 直接把这批 cookie 落盘，不等 URL 跳转
                    try:
                        keep = cookies_for_host(ck)
                        _write_payload(state, args.course, keep)
                    except StateNotSaved as e:
                        print("!! %s" % e, file=sys.stderr)
                        ctx.close()
                        return 1
                    print("登录态已保存: %s (%d cookies)" % (state, len(keep)))
                    print("  注意: 该文件等同于你的登录凭据, 不要分享或提交到 Git")
                    save_activities(state, args.course)
                    print("=== login ok (%.1fs) ===" % (time.time() - t_start))
                    try:
                        ctx.close()
                    except Exception:
                        pass
                    return 0

            if el // 10 != last // 10:
                last = el
                print("  等待 %3ds  url=%s%s" % (
                    el, _live_url(page)[:80],
                    ("" if not ck else "  [已取到 %d 个 cookie，%s]" % (len(ck), last_status))),
                      flush=True)
            time.sleep(URL_POLL)

        if not logged_url:
            print("!! 登录超时, 未保存登录态", file=sys.stderr)
            print("   已尝试的标签页数: %d" % len(list(ctx.pages)), file=sys.stderr)
            if last_sig:
                print("   接口探测结果: %s" % last_status, file=sys.stderr)
                print("   cookie 已写进 profile 但服务端仍不认 —— 多半是认证没走完；"
                      "重跑本命令会命中 profile 登录态", file=sys.stderr)
            ctx.close()
            return 1
        print("登录成功:", logged_url[:100])

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
        try:
            ctx.close()
        except Exception:
            pass

    # 活动清单用普通 HTTP 抓，不依赖浏览器（浏览器已经关了）
    save_activities(state, args.course)
    print("=== login ok (%.1fs) ===" % (time.time() - t_start))
    return 0


def _load_cookies(state):
    """从登录态文件取 cookie 数组（读不出来就当空）。"""
    try:
        with open(state, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, list):
            return st
        return st.get("cookies") or []
    except Exception:
        return []


def _write_payload(state, course, cookies):
    """把一批现成 cookie 按落盘格式写出（与 save_state 共用同一套安全语义）。

    两条写盘路径（URL 命中 / 探针命中）都必须落到这个函数上，否则会出现
    「一条路径写了 0600、另一条写了默认 umask」这种只有攻防测试才会发现的差异。

    临时文件安全（fail-closed）：
      POSIX 上 .tmp 从**创建那一刻**就是 0600（os.open(mode=0o600)），
      不做「先普通 open 写完再 chmod」—— 那样默认 umask 宽的机器上，
      带 cookie 的 .tmp 会以 0644 短暂存在；写入中途异常退出时残留在盘上。
      任何一步失败都会删掉 .tmp 再抛出，绝不留下半成品。
      Windows 不伪造 POSIX mode 语义（它靠 ACL，chmod 没有实际意义）。
    """
    if not cookies:
        raise StateNotSaved("没有可写盘的 cookie（%s 保持不动）" % state)
    payload = {
        "version": 1,
        "base": BASE,
        "host": HOST,
        "course": str(course),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "cookies": cookies,
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
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if os.name == "posix":
        try:
            os.chmod(state, 0o600)
        except OSError:
            pass
    return cookies


if __name__ == "__main__":
    sys.exit(main())
