# -*- coding: utf-8 -*-
"""
思源学堂 2.0 —— 批量下载课程附件（课件 + 作业 + 项目压缩包 + 录像）

只处理当前账号有权访问的课程资源。

附件有三类来源，只看附件字段会漏掉一大半:
  ① 活动数据里的附件数组（含 online_video 类型的课堂录像）
  ② page 类型活动正文里内嵌的附件链接          ← 课件 PDF 全在这里
  ③ lecture_live 类型活动的回放（走校外录播系统，见下）

用法:
    python lms_fetch.py --course <课程ID> --out "./课程资料" --dry-run
    python lms_fetch.py --course <课程ID> --out "./课程资料"
    python lms_fetch.py --course <课程ID> --out "./课程资料" --organize
    python lms_fetch.py --course <课程ID> --out "./课程资料" --list-only manifest.json
    python lms_fetch.py --course <课程ID> --out "./课程资料" --exclude "2020|2021|2022"
    python lms_fetch.py --course <课程ID> --out "./课程资料" --no-video   # 只要文档，不要录像

目录结构（默认 activity 布局）:
    <out>/{课件,作业,录像}/<活动标题>/<文件名>
加 --organize 后文档部分自动按章归并（录像不受影响，仍按活动分目录）:
    <out>/课件/第01章 绪论/<文件名>
    <out>/课件/其他/<文件名>          ← 认不出章号的
加 --split-projects 后 zip 项目包单独走:
    <out>/项目/<活动标题>/<文件名>

环境要求:
    只需要标准库 + 已跑过 lms_login.py 生成的登录态 JSON，不依赖浏览器。
"""
import argparse
import hashlib
import http.cookiejar
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse, quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lms_common import BASE, HOST, state_path
from lms_organize import parse_chapter, chapter_dir
import lms_live

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/153.0.0.0 Safari/537.36 Edg/153.0.0.0")

# 常见大作业 / 项目压缩包的关键词。
# ★ 注意：`zip` **不能**单独作为判据 —— 那样等于「所有压缩包都是项目包」，
#   实测会把 `资料.zip`、`课件.zip` 一起归进 项目/。真正的项目判断来自
#   project / proj1 / Project 2 / 项目 / 大作业 这类关键词，
#   而 .zip 只是「允许被判为项目包」的必要条件之一（见 is_project_pkg）。
PROJ_HINT = re.compile(r"project|proj[_\-\s]?\d|项目|大作业|课程设计|大程",
                       re.I)

# 文件名主干上限。Windows 全路径上限约 260 字符，留足目录深度后给文件名 120 比较稳
MAX_STEM = 120
MAX_EXT = 12                      # 正常扩展名不会超过这个长度

# 退出码 —— 让调用方（AI 或脚本）能区分失败原因
RC_OK = 0
RC_NO_STATE = 2                   # 没有登录态文件
RC_EXPIRED = 3                    # 登录态过期
RC_PARTIAL = 4                    # 有文件下载失败

# 取元信息失败时的三类错误。**必须分开**，否则一次接口抖动会被
# 伪装成「这些文件平台没给权限」而静默跳过，用户还会看到 fail=0。
ERR_UNAVAILABLE = "unavailable"   # 403 / 404，确实拿不到，可跳过
ERR_AUTH = "authentication"       # 401，登录态问题，明确失败
ERR_TRANSIENT = "transient"       # 超时 / 连接错误 / 5xx / JSON 解析失败，算失败


# ---------------------------------------------------------------- 工具

def split_ext(name):
    """切出 (主干, 扩展名)。只认「像扩展名」的尾巴。

    扩展名的判定要严一点，否则这些会被误切：
        第1.2节讲义      -> 不该把 '.2节讲义' 当扩展名
        3.5 英寸软盘     -> 同上
        报告 v1.0       -> 不该把 '.0' 当扩展名

    规则：2~12 字符、以字母开头、只含字母数字（可带 + - _）、不是纯数字。
    """
    stem, dot, ext = name.rpartition(".")
    if not dot or not (1 < len(ext) <= MAX_EXT):
        return name, ""
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9+\-_]*", ext):
        return name, ""
    return stem, "." + ext


# Windows 保留设备名。这些名字**带扩展名也照样不能当文件名**
# （`CON.pdf`、`NUL.txt` 在 Windows 上同样无法创建），
# 在 Linux/macOS 上却完全合法 —— 为了跨平台一致，统一加下划线前缀。
_WIN_RESERVED = frozenset(
    ["CON", "PRN", "AUX", "NUL"]
    + ["COM%d" % i for i in range(1, 10)]
    + ["LPT%d" % i for i in range(1, 10)]
)


def _dedupe_reserved(stem):
    """主干撞上 Windows 保留名就加下划线前缀，保持跨平台一致。"""
    if not stem:
        return stem
    # Windows 对 `CON` 和 `con` 一视同仁，且 `CON.txt` 也非法
    probe = stem.split(".")[0].rstrip(" .")
    if probe.upper() in _WIN_RESERVED:
        return "_" + stem
    return stem


def safe(s, max_stem=MAX_STEM):
    """把任意字符串变成安全的文件名，**保证不切掉扩展名**。

    以前是简单 `[:80]`，实测超过 80 字符的长文件名会被切成 `.p` 甚至完全没扩展名，
    落盘后双击打不开。现在：先切出扩展名，只截主干，并在截断处补 6 位短哈希防重名。

        '很长的标题...（60字）.pdf' -> '很长的标题...（约110字）~a1b2c3.pdf'

    另外处理 Windows 保留设备名：`CON.pdf` / `NUL.txt` 这类在 Windows 上
    无法创建（Linux/macOS 合法），统一转成 `_CON.pdf` / `_NUL.txt`，
    保证三个平台落盘结果一致。
    """
    s = (s or "").strip()
    stem, ext = split_ext(s)
    stem = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "_", stem).strip(" .")

    if not stem:
        return "untitled" + ext

    if len(stem) <= max_stem:
        return _dedupe_reserved(stem) + ext

    # 超长：截断 + 短哈希。哈希取自原始主干，保证同一长名字每次都得到同一个结果
    digest = hashlib.sha1(stem.encode("utf-8")).hexdigest()[:6]
    keep = max_stem - len(digest) - 1
    cut = stem[:keep].rstrip(" .")
    return _dedupe_reserved(cut) + "~" + digest + ext


def human_size(n):
    if n is None:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return "%.1f%s" % (n, unit) if unit != "B" else "%dB" % n
        n /= 1024.0


def is_tty():
    try:
        return sys.stdout.isatty()
    except Exception:
        return False


# ---------------------------------------------------------------- 网络

def load_state(path):
    """读登录态，同时兼容两种格式。

    ① Playwright 的原生 storage_state：{"cookies": [...], "origins": [...]}
    ② 本项目自己的精简结构：{"version":1, "base":..., "host":..., "cookies":[...]}

    两种都只取 cookies 数组，其余字段忽略。旧文件不要突然不能用。
    另外允许裸 cookies 数组（极简格式）。
    """
    with open(path, encoding="utf-8") as f:
        st = json.load(f)
    if isinstance(st, list):
        return {"cookies": st}
    if not isinstance(st, dict):
        raise ValueError("登录态文件结构不对")
    cookies = st.get("cookies")
    if not isinstance(cookies, list):
        raise ValueError("登录态文件缺少 cookies 数组")
    return st


def state_host(path):
    """登录态是为哪个 host 建的。旧格式没有这个字段，返回 None。"""
    try:
        st = load_state(path)
    except Exception:
        return None
    h = st.get("host")
    return str(h) if h else None


def opener(state):
    """按登录态建一个 opener。

    ★ Cookie 重建必须保留安全语义：domain / path / secure / expires。
    以前这些位置被硬编码（secure=True, expires=None），会导致
      - 给目标 host 之外的域名带上 cookie；
      - 把 HTTPS-only 的 cookie 发到 HTTP 上（安全属性被抹掉）。
    现在：domain 与 expires 按原样还原，secure 也按原样还原；
    只装载适用于当前目标 host 的 cookie。
    """
    st = load_state(state)
    target = HOST.lower()

    def applies(c):
        dom = str(c.get("domain") or "").lstrip(".").lower()
        if not dom:
            # 没写 domain 的按当前 host 处理
            return True
        # 精确匹配或子域匹配；目标 host 不能越出 cookie 的域
        return target == dom or target.endswith("." + dom)

    cj = http.cookiejar.CookieJar()
    for c in st.get("cookies") or []:
        if not c.get("name"):
            continue
        if not applies(c):
            continue                    # 不属于当前 host 的，不装载
        dom = c.get("domain") or HOST
        expires = c.get("expires")
        try:
            expires = int(expires) if expires not in (None, "", -1) else None
        except (TypeError, ValueError):
            expires = None
        cj.set_cookie(http.cookiejar.Cookie(
            version=0,
            name=c["name"],
            value=c.get("value") or "",
            port=None,
            port_specified=False,
            domain=dom,
            domain_specified=bool(dom),
            domain_initial_dot=str(dom).startswith("."),
            path=c.get("path") or "/",
            path_specified=True,
            secure=bool(c.get("secure", False)),
            expires=expires,
            discard=(expires is None),
            comment=None,
            comment_url=None,
            rest={},
            rfc2109=False,
        ))
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
    op.addheaders = [("User-Agent", UA), ("Referer", BASE + "/course/index")]
    return op


def get_json(op, url, timeout=60, retries=3):
    """GET 并解析 JSON，带重试（只重试网络类错误，4xx 立即返回）"""
    last = None
    for i in range(retries):
        try:
            return json.loads(op.open(url, timeout=timeout).read())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                raise                       # 不该重试
            last = e
        except Exception as e:
            last = e
        if i < retries - 1:
            time.sleep(1.5 * (i + 1))
    raise last


def api_ok(op):
    """探测登录态是否仍然有效。返回 (是否有效, 说明)"""
    try:
        r = op.open("%s/api/my-courses?sub_course_id=0" % BASE, timeout=30)
        body = r.read()
        if r.status == 200 and b'"courses"' in body:
            return True, "登录态有效"
        return True, "接口可达"
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            return False, "服务端返回 %d，登录态已失效" % e.code
        return True, "接口返回 %d，按可达处理" % e.code
    except Exception as e:
        return True, "探测失败（%s），跳过" % str(e)[:40]


# ---------------------------------------------------------------- 收集

def collect(op, course, activities=None, want_video=True):
    """返回 ([(kind, 活动标题, upload_id)], 来源①数量, 来源②数量)

    kind 取值:
        课件   —— 讲义 / PDF / 附件
        作业   —— homework 类型活动带的文件
        录像   —— online_video 类型活动的课堂录像
        回放   —— lecture_live 类型活动的直播回放（走校外录播系统，另一套端点）

    前三类走相同的附件下载机制，平台支持分片请求，
    断点续传 / etag 校验原样可用。第四类要单独实现，详见 lms_live.py。

    第四类的 upload_id 位放的是「活动 id」，不是 upload id —— 它根本不是附件。
    下载时按 kind 分流，不会走到附件端点上去。

    两个计数都是「进 plan 的去重增量」，来源③（回放）不计入其中，
    想拿回放条数请按 kind == "回放" 数。用 len(keys) 相减推来源②会把回放算进去。
    """
    if activities is None:
        activities = get_json(op, "%s/api/courses/%s/activities?sub_course_id=0"
                              % (BASE, course))["activities"]
    plan = {}

    def add(kind, act, uid):
        plan.setdefault((kind, safe(act), int(uid)), None)

    def kind_of(a):
        """按活动类型决定归属目录。online_video / lecture_live 各自单开一类。"""
        t = a.get("type")
        if t == "homework":
            return "作业"
        if t == "online_video":
            return "录像" if want_video else None
        if t == "lecture_live":
            return "回放" if want_video else None
        return "课件"

    # 来源 ①
    for a in activities:
        k = kind_of(a)
        if k is None:
            continue
        for u in (a.get("uploads") or []):
            add(k, a.get("title"), u["id"])
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
    # 三个来源的去重增量要分开记：来源②若与来源①撞了同一 (kind, 活动, uid)，
    # 就不会进 plan，用减法会把它算错。回放条目也在这里被排除。
    n2 = len(plan) - n1

    # 来源 ③：lecture_live 回放。replay_videos 只在活动详情里有，列表接口拿不到。
    if want_video:
        lives = [x for x in activities if x.get("type") == "lecture_live"]
        if lives:
            print("  (回放: 扫了 %d 个 lecture_live 活动)" % len(lives))
        for a in lives:
            aid = a.get("id")
            if not aid:
                continue                        # 残缺数据，跳过而不是崩
            try:
                d = get_json(op, "%s/api/activities/%s?sub_course_id=0" % (BASE, aid))
            except Exception as e:
                print("  回放详情失败 %s: %s" % (aid, str(e)[:50]))
                continue
            reps = lms_live.parse_replay(d)
            if not reps:
                continue
            # uid 用负数存活动 id —— 它不是 upload，走不了 uploads 端点
            add("回放", a.get("title"), -(int(aid)))
    return sorted(plan.keys()), n1, n2


def meta(op, uid):
    """取一个 upload 的元信息。

    返回 (info, err)。err 为 None 表示成功。

    ★ 为什么不在这里吞异常：以前是 `except Exception: return {}`，
    调用方拿到空 dict 只能一律标成「平台侧无权限或已删除」，于是
    403 / 404 / 500 / 超时 / DNS 失败 / JSON 解不开 全被归成同一种「跳过」。
    一次接口抖动就可能让整门课显示 fail=0、退出码 0，用户以为全下完了。
    现在把错误原样交给调用方，由 expand_items 按类型分流。
    """
    try:
        return get_json(op, "%s/api/uploads/%s" % (BASE, uid)), None
    except urllib.error.HTTPError as e:
        return None, classify_http(e.code)
    except Exception as e:
        return None, {"kind": ERR_TRANSIENT,
                      "msg": "%s: %s" % (type(e).__name__, str(e)[:60])}


def classify_http(code):
    """把 HTTP 状态码映射成错误类别。

    403 / 404 —— 资源确实不可达（无权限 / 已删除），跳过合理；
    401       —— 登录态问题，必须明确失败并提示重新登录；
    其余(5xx 等)—— 服务端临时故障，算获取失败，不能当「没有这个文件」。
    """
    if code in (403, 404):
        return {"kind": ERR_UNAVAILABLE, "code": code,
                "msg": "平台侧无权限或已删除 (HTTP %d)" % code}
    if code == 401:
        return {"kind": ERR_AUTH, "code": code,
                "msg": "登录态失效 (HTTP 401)，请重新登录"}
    return {"kind": ERR_TRANSIENT, "code": code, "msg": "HTTP %d" % code}


def has_expected_size(size):
    """有没有可靠的服务端声明大小。0 / None 都视为不可靠。

    回放这类资源常常拿不到声明总长，此时无法做一致性判断，
    只能退回「文件存在且非空即视为已下载」的老策略。
    """
    try:
        return int(size) > 0
    except (TypeError, ValueError):
        return False


def already_complete(path, size):
    """判断某个目标文件是否真的已经下载完。

    ★ 以前是 `exists and getsize > 1024` —— 服务器上 100MB 的文件，
    本地只有 20MB（上次下到一半被杀）也会被当成完整文件永远跳过。
    现在：服务端有明确 size 时必须**大小相等**才算完整，不等就重下。

    拿不到声明大小时（size 为 0 / None）退回「存在且非空」的宽松策略。
    返回 (是否完整, 本地大小, 期望大小)。
    """
    if not os.path.exists(path):
        return False, 0, None
    local = os.path.getsize(path)
    if has_expected_size(size):
        exp = int(size)
        return local == exp, local, exp
    return local > 1024, local, None


def is_project_pkg(name):
    """这个文件名像不像「项目 / 大作业」压缩包。

    .zip / .7z / .rar 是**必要条件**（项目包一定是压缩包），
    但压缩包不一定是项目包 —— 还要名字里有 project / 项目 / 大作业 这类词。
    以前 `PROJ_HINT` 里直接带了 `zip`，结果所有压缩包都被归进 项目/。
    """
    low = (name or "").lower()
    if not low.endswith((".zip", ".7z", ".rar")):
        return False
    return bool(PROJ_HINT.search(name))


def dest_for(kind, act, name, args):
    """决定落盘目录"""
    if args.split_projects and is_project_pkg(name):
        return os.path.join(args.out, "项目", act)
    # 录像与回放不参与按章归并：标题基本认不出章号，套下来只会全进「其他」
    if args.organize and kind not in ("录像", "回放"):
        ch = parse_chapter(act, name)
        if ch is None:
            return os.path.join(args.out, kind, "其他")
        return os.path.join(args.out, kind, chapter_dir(ch, [act, name]))
    if args.layout == "flat":
        return os.path.join(args.out, kind)
    return os.path.join(args.out, kind, act)


# ---------------------------------------------------------------- 下载

def _etag_size(etag):
    """从 etag 里提取文件大小。

    平台返回的 etag 形如 `"<hex>-<hex>"`，后半段是文件大小的十六进制。
    可用作「文件是否变了」的判据。解析不出来返回 None。
    """
    if not etag:
        return None
    m = re.search(r'"([0-9a-fA-F]+)-([0-9a-fA-F]+)"', etag)
    if not m:
        return None
    try:
        return int(m.group(2), 16)
    except ValueError:
        return None


def download(op, uid, path, expect_size=None, retries=3, quiet=False, resume=True):
    """流式下载 + 重试 + 断点续传 + 进度 + sha256。

    断点续传：平台返回 `accept-ranges: bytes`，所以中断后可以带 Range 头
    从 .part 的断点继续，不用从头再来。.part 文件会保留到成功为止。

    返回 dict: {ok, size, sha256, exp_sha, err, retried, resumed}
    """
    tmp = path + ".part"
    last_err = None
    used_resume = False

    def drop_part():
        """删掉残片。删不掉也无所谓 —— 下次开下会按 offset 重新判断。"""
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass

    for attempt in range(retries):
        # 有残片就尝试续传
        offset = 0
        if resume and os.path.exists(tmp):
            offset = os.path.getsize(tmp)
            if expect_size and offset >= expect_size:
                offset = 0                      # 残片比目标还大，不靠谱，重来
                drop_part()

        # 重试判定只写一遍：还有机会就退避后重来，没机会就把 err 定格返回。
        # retry_or_give_up 的返回值非 None 时就该立刻 return 出去。
        def retry_or_give_up(err, fatal=False):
            nonlocal last_err
            last_err = err
            if attempt < retries - 1:
                time.sleep(1.5 * (attempt + 1))
                return None
            d = {"ok": False, "err": err}
            if fatal:
                d["fatal"] = True
            return d

        try:
            req = urllib.request.Request("%s/api/uploads/%s/blob" % (BASE, uid))
            if offset:
                req.add_header("Range", "bytes=%d-" % offset)
            r = op.open(req, timeout=300)

            ct = r.headers.get("Content-Type", "")
            if "text/html" in ct:
                return {"ok": False, "err": "返回 HTML（%s），非文件" % ct[:30]}

            # 服务端不支持 Range 时会返回 200 + 全量，此时要把已下的部分丢掉
            status = getattr(r, "status", 200)
            partial = (offset > 0 and status == 206)
            if offset and not partial:
                offset = 0
                drop_part()

            total = None
            try:
                total = int(r.headers.get("Content-Length") or 0) or None
            except (TypeError, ValueError):
                total = None
            if total and partial:
                total += offset              # Content-Length 是剩余部分
            exp_sha = (r.headers.get("Lms-Content-Sha256")
                       or r.headers.get("X-Checksum-Sha256"))
            etag_size = _etag_size(r.headers.get("etag"))

            h = hashlib.sha256()
            got = offset
            if partial:
                with open(tmp, "rb") as f:      # 残片也要计入哈希
                    while True:
                        blk = f.read(1048576)
                        if not blk:
                            break
                        h.update(blk)
                used_resume = True

            t0 = time.time()
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
            with open(tmp, "ab" if partial else "wb") as f:
                while True:
                    chunk = r.read(262144)
                    if not chunk:
                        break
                    f.write(chunk)
                    h.update(chunk)
                    got += len(chunk)
                    if not quiet and is_tty():
                        elapsed = max(time.time() - t0, 0.001)
                        speed = (got - offset) / elapsed / 1024 / 1024
                        if total:
                            pct = got * 100.0 / total
                            bar = "█" * int(pct / 4) + "·" * (25 - int(pct / 4))
                            tag = "续传 " if partial else ""
                            sys.stdout.write("\r      [%s] %5.1f%%  %s / %s  %.1fMB/s %s"
                                             % (bar, pct, human_size(got),
                                                human_size(total), speed, tag))
                        else:
                            sys.stdout.write("\r      %s  %.1fMB/s"
                                             % (human_size(got), speed))
                        sys.stdout.flush()
            if not quiet and is_tty():
                sys.stdout.write("\r" + " " * 78 + "\r")
                sys.stdout.flush()

            if got < 512:
                drop_part()
                d = retry_or_give_up("只有 %d 字节，疑似空响应" % got)
                if d:
                    return d
                continue

            # 完整性判据：优先服务端 sha256，其次 etag 里的大小，最后元信息大小
            if exp_sha and exp_sha.lower() != h.hexdigest().lower():
                drop_part()
                d = retry_or_give_up("sha256 不匹配（期望 %s… 实得 %s…）"
                                     % (exp_sha[:12], h.hexdigest()[:12]))
                if d:
                    return d
                continue

            if etag_size and got != etag_size:
                d = retry_or_give_up("大小与 etag 不符（etag=%d 实得=%d）"
                                     % (etag_size, got))
                if d:
                    return d
                continue

            if expect_size and abs(expect_size - got) > 16:
                d = retry_or_give_up("大小与元信息不符（元信息=%d 实得=%d）"
                                     % (expect_size, got))
                if d:
                    return d
                continue

            os.replace(tmp, path)
            return {"ok": True, "size": got, "sha256": h.hexdigest(),
                    "exp_sha": exp_sha, "retried": attempt,
                    "resumed": used_resume}

        except urllib.error.HTTPError as e:
            if e.code in (401, 403, 404):
                return {"ok": False, "err": "HTTP %d（平台侧限制，不重试）" % e.code,
                        "fatal": True}
            last_err = "HTTP %d" % e.code
        except Exception as e:
            last_err = str(e)[:70]

        if attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))

    # 全部失败：保留 .part 以便下次续传（只在能续传时保留）
    if not resume:
        drop_part()
    return {"ok": False, "err": last_err or "未知错误", "kept_part": resume}


# ---------------------------------------------------------------- 主流程

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
    ap.add_argument("--no-video", action="store_true",
                    help="跳过课堂录像与直播回放（录像往往几百 MB）")
    ap.add_argument("--all-cameras", action="store_true",
                    help="直播回放默认只下「屏幕录制」机位，加此项连「教师机位」一起下")
    ap.add_argument("--list-only", default=None, metavar="FILE",
                    help="只列清单不下载，结果写入 FILE（.json 或 .csv）")
    ap.add_argument("--retries", type=int, default=3, help="单个文件重试次数，默认 3")
    ap.add_argument("--manifest", default=None, metavar="FILE",
                    help="下载完成后把清单+sha256 写入 FILE（.json 或 .csv）")
    ap.add_argument("--no-verify", action="store_true",
                    help="跳过下载前的登录态探测")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="干跑时打印归类后的目标目录")
    ap.add_argument("-q", "--quiet", action="store_true", help="不显示进度")
    args = ap.parse_args()

    if args.base:
        BASE = args.base.rstrip("/")
        HOST = urlparse(BASE).hostname or HOST
    print("平台: %s" % BASE)

    state = args.state or state_path(args.course)
    if not os.path.isfile(state):
        print("!! 没有登录态 %s" % state, file=sys.stderr)
        print("   先跑: python lms_login.py --course %s" % args.course, file=sys.stderr)
        return RC_NO_STATE
    op = opener(state)

    # 登录态探测 —— 别让「过期」伪装成「平台没权限」
    if not args.no_verify and not args.dry_run:
        valid, why = api_ok(op)
        if not valid:
            print("!! %s" % why, file=sys.stderr)
            print("   重新登录: python lms_login.py --course %s" % args.course,
                  file=sys.stderr)
            return RC_EXPIRED
        print("   %s" % why)

    acts = None
    if args.activities and os.path.isfile(args.activities):
        acts = json.load(open(args.activities, encoding="utf-8")).get("activities")

    print("扫描课程 %s ..." % args.course)
    keys, n1, n2 = collect(op, args.course, acts, want_video=not args.no_video)
    print("  来源① uploads 字段: %d" % n1)
    print("  来源② 正文内嵌:     %d" % n2)
    n_vid = sum(1 for k in keys if k[0] == "录像")
    n_live = sum(1 for k in keys if k[0] == "回放")
    if n_vid:
        print("  其中课堂录像:       %d" % n_vid)
    if n_live:
        print("  其中直播回放:       %d 个活动" % n_live)
    print("  去重后共 %d 个附件" % len(keys))

    excl = re.compile(args.exclude) if args.exclude else None

    # ---- 把回放条目展开成实际文件条目 ----
    # 回放的一个活动有 2 路机位，要展开成 2 个下载项；
    # 其余 kind 的 uid 就是 upload id，直接用。
    items, live_err = expand_items(op, keys, args)

    # ---- 消解目标路径冲突 ----
    # 必须在列清单和下载之前做，否则第二个同名文件会被静默跳过、悄悄丢文件。
    items, n_collided = resolve_collisions(items, args)
    if n_collided and not args.quiet:
        print("  检测到 %d 个目标路径冲突，已改用确定性后缀避免覆盖" % n_collided)

    # ---- 只列清单 ----
    if args.list_only:
        rows = build_rows(op, items, excl, args, quiet=args.quiet)
        write_manifest(args.list_only, rows)
        print("清单已写入 %s（%d 条）" % (args.list_only, len(rows)))
        return RC_OK

    ok = fail = skip = 0
    auth_fail = 0
    rows = []
    for i, it in enumerate(items, 1):
        kind = it["kind"]
        act = it["activity"]
        name = it["name"]
        size = it.get("size") or 0

        if it.get("error"):
            # ★ 关键分流：只有 403 / 404（确实没权限或已删除）才算「跳过」。
            #   登录态失效、超时、5xx、JSON 解析失败都是**获取失败**，
            #   必须计入 fail，否则接口抖动会被伪装成「平台没给这个文件」，
            #   用户看到 fail=0、退出码 0，以为全下完了。
            if it.get("unavailable"):
                print("[%2d] N/A   %s (%s)" % (i, it.get("uid"),
                                               it.get("err_msg") or "不可达，跳过"))
                skip += 1
                rows.append({"i": i, "kind": kind, "activity": act,
                             "uid": it.get("uid"), "status": "na",
                             "error": it.get("err_msg")})
                continue
            msg = it.get("err_msg") or "元信息获取失败"
            print("[%2d] FAIL  %s :: %s" % (i, str(it.get("uid"))[:40], msg))
            fail += 1
            rows.append({"i": i, "kind": kind, "activity": act,
                         "uid": it.get("uid"), "status": "fail",
                         "error": msg})
            if it.get("err_kind") == ERR_AUTH:
                auth_fail += 1
            continue

        if excl and excl.search(name):
            print("[%2d] EXCLUDE %s" % (i, name))
            skip += 1
            rows.append({"i": i, "kind": kind, "activity": act, "name": name,
                         "status": "excluded"})
            continue

        dest = dest_for(kind, act, name, args)
        path = os.path.join(dest, name)
        rel = os.path.relpath(dest, args.out)

        complete, local_size, exp = already_complete(path, size)
        if complete:
            if not args.quiet:
                print("[%2d] SKIP  %-4s %-30s -> %s" % (i, kind, name[:28], rel))
            skip += 1
            rows.append({"i": i, "kind": kind, "activity": act, "name": name,
                         "size": local_size, "dir": rel, "status": "exists"})
            continue
        if local_size > 0:
            # 有文件但大小对不上 —— 多半是上次中断留下的，重下（.part 会走续传）
            print("[%2d] REDO  %-4s %-30s 本地 %s / 期望 %s"
                  % (i, kind, name[:28], human_size(local_size),
                     human_size(exp) if exp else "未知"))

        if args.dry_run:
            if args.verbose:
                print("[%2d] PLAN  %-4s %-30s -> %s" % (i, kind, name[:28], rel))
            else:
                print("[%2d] PLAN  %-9s %-28s %-40s %10s"
                      % (i, kind, act[:26], name[:38], human_size(size)))
            rows.append({"i": i, "kind": kind, "activity": act, "name": name,
                         "size": size, "dir": rel, "status": "plan"})
            continue

        if not args.quiet:
            print("[%2d] GET   %-4s %-30s %10s" % (i, kind, name[:28], human_size(size)))

        if kind == "回放":
            res = lms_live.download(op, it["url"], path, expect=size,
                                    retries=args.retries, quiet=args.quiet)
        else:
            res = download(op, it["uid"], path, size,
                           retries=args.retries, quiet=args.quiet)

        if res["ok"]:
            mark = "  (重试%d次)" % res["retried"] if res.get("retried") else ""
            if res.get("exp_sha"):
                mark += "  sha256✓"
            if kind == "回放":
                mark += "  (无官方哈希)"
            print("[%2d] OK    %10s  %s%s"
                  % (i, human_size(res["size"]), name[:40], mark))
            # 回放拿不到官方哈希，短读只能靠声明总长比对暴露出来
            if res.get("note"):
                print("       !! %s（超出 %.0f%% 阈值，建议复核）"
                      % (res["note"], lms_live.SHORT_TOLERANCE * 100))
            ok += 1
            rows.append({"i": i, "kind": kind, "activity": act, "name": name,
                         "size": res["size"], "dir": rel, "status": "ok",
                         "sha256": res["sha256"],
                         "server_sha256": res.get("exp_sha"),
                         "retried": res.get("retried", 0),
                         "declared": res.get("declared"),
                         "note": res.get("note")})
        else:
            print("[%2d] FAIL  %s :: %s" % (i, name[:40], res["err"]))
            fail += 1
            rows.append({"i": i, "kind": kind, "activity": act, "name": name,
                         "dir": rel, "status": "fail", "error": res["err"]})

    if args.manifest:
        write_manifest(args.manifest, rows)
        print("清单已写入 %s" % args.manifest)

    tag = " (dry-run)" if args.dry_run else ""
    print("=== done%s ok=%d fail=%d skip=%d ===" % (tag, ok, fail, skip))
    if auth_fail:
        print("!! 有 %d 项因登录态问题失败，先重新登录再跑: "
              "python lms_login.py --course %s" % (auth_fail, args.course),
              file=sys.stderr)
    return RC_PARTIAL if fail else RC_OK


def expand_items(op, keys, args):
    """把 collect() 的 (kind, act, uid) 展开成可下载条目。

    - 普通附件：uid 就是 upload id，取一次元信息拿文件名和大小
    - 回放（uid 是负数）：每个负数对应一个 lecture_live 活动，展开成多路机位

    返回 (items, errors)。每个 item 形如
        {kind, activity, name, uid, size, url?, error?, err_kind?}

    error 字段：None 正常；ERR_UNAVAILABLE 的资源标 unavailable=True（跳过）；
    其余错误（ERR_AUTH / ERR_TRANSIENT）标 error=True，调用方必须计入失败。
    """
    items = []
    for kind, act, uid in keys:
        if kind != "回放":
            m, err = meta(op, uid)
            if err:
                kind_err = err.get("kind")
                items.append({"kind": kind, "activity": act, "uid": uid,
                              "name": "upload_%s" % uid, "error": True,
                              "err_kind": kind_err, "err_msg": err.get("msg"),
                              "unavailable": kind_err == ERR_UNAVAILABLE})
                continue
            items.append({
                "kind": kind, "activity": act, "uid": uid,
                "name": safe(m.get("name") or ("upload_%s" % uid)),
                "size": m.get("size") or 0,
            })
            continue

        # 回放：uid 存的是活动 id 的负数
        act_id = -uid
        try:
            d = get_json(op, "%s/api/activities/%s?sub_course_id=0" % (BASE, act_id))
        except urllib.error.HTTPError as e:
            err = classify_http(e.code)
            items.append({"kind": kind, "activity": act, "uid": act_id,
                          "name": "%s-回放" % act, "error": True,
                          "err_kind": err["kind"], "err_msg": err["msg"],
                          "unavailable": err["kind"] == ERR_UNAVAILABLE})
            continue
        except Exception as e:
            print("  回放详情失败 %s: %s" % (act_id, str(e)[:50]))
            items.append({"kind": kind, "activity": act, "uid": act_id,
                          "name": "%s-回放" % act, "error": True,
                          "err_kind": ERR_TRANSIENT,
                          "err_msg": "%s: %s" % (type(e).__name__, str(e)[:50]),
                          "unavailable": False})
            continue

        reps = lms_live.parse_replay(d)
        if not reps:
            continue
        if not args.all_cameras:
            reps = [r for r in reps if r["camera_type"] == "encoder"] or reps[:1]

        # ★ 同一天的多个 lecture_live 活动 title 完全相同（实际遇到过标题
        # 一致的多个活动），只靠 title 命名会互相覆盖。start_time 是唯一能
        # 区分它们的字段（在活动详情顶层），必须进文件名。
        stamp = lms_live.start_stamp(d)

        for r in reps:
            p = lms_live.probe(op, r["url"])
            name = lms_live.safe_name(act, r["camera_type"], stamp=stamp)
            items.append({
                "kind": kind, "activity": act, "uid": act_id,
                "name": name, "size": p.get("size") or 0,
                "url": r["url"], "camera": r["camera_type"],
                "error": None if p.get("ok") else True,
            })
    return items, None


def resolve_collisions(items, args):
    """下载计划阶段消解目标路径冲突。

    ★ 为什么必须做：不同活动下的同名附件（例如每章都有一份 `讲义.pdf`，
    或 `作业1.zip` 在多处出现）经 safe() / dest_for() 后可能落到同一个路径。
    第二个条目会因为「目标已存在」被静默跳过 —— 文件悄悄丢了，日志里只多
    一行 SKIP，用户根本看不出来。

    处理方式：多个不同 uid 撞到同一路径时，从第二个开始改名为
    `stem~<uid>.ext`。**确定性**的，重复运行得到同一路径 —— 不用随机数，
    也不加时间戳，否则每次跑都是新文件，磁盘会被灌满。

    仅处理带 error 之外的真实条目；条目会增加 `name` / `_collided` 字段。
    返回 (items, n_fixed)。
    """
    seen = {}                       # (dest, name) -> 已经用过的条目
    fixed = 0
    for it in items:
        if it.get("error") or it.get("unavailable"):
            continue                # 这些条目根本不会下载，不参与占位
        name = it.get("name")
        if not name:
            continue
        dest = dest_for(it["kind"], it["activity"], name, args)
        key = (dest, name)
        if key not in seen:
            seen[key] = it
            continue
        # 撞了：换一个带 uid 的确定性后缀
        prev = seen[key]
        if prev.get("_collided"):
            # 前一个已经被改过名，说明新的这个还是撞，直接给它后缀
            pass
        stem, ext = split_ext(name)
        uid = it.get("uid")
        suffix = "~%s" % uid if uid is not None else "~dup"
        new_name = safe("%s%s%s" % (stem, suffix, ext))
        # 极少数情况下带上 uid 还是撞（同名不同 kind 落到同一目录底下的
        # `其他/`），继续加计数把确定性保持住
        n = 2
        while (dest, new_name) in seen:
            new_name = safe("%s%s-%d%s" % (stem, suffix, n, ext))
            n += 1
        it["name"] = new_name
        it["_collided"] = True
        fixed += 1
        seen[(dest, new_name)] = it
    return items, fixed


def build_rows(op, items, excl, args, quiet=False):
    """--list-only 用：把条目整理成行"""
    rows = []
    for i, it in enumerate(items, 1):
        kind = it["kind"]
        act = it["activity"]
        name = it["name"]
        size = it.get("size") or 0
        if it.get("error"):
            rows.append({"i": i, "kind": kind, "activity": act, "uid": it.get("uid"),
                         "status": "na"})
            if not quiet:
                print("[%2d] N/A   %s" % (i, it.get("uid")))
            continue
        dest = dest_for(kind, act, name, args)
        row = {"i": i, "kind": kind, "activity": act, "uid": it.get("uid"),
               "name": name, "size": size,
               "dir": os.path.relpath(dest, args.out),
               "ext": split_ext(name)[1].lstrip(".")}
        if excl and excl.search(name):
            row["status"] = "excluded"
        elif os.path.exists(os.path.join(dest, name)):
            row["status"] = "exists"
        else:
            row["status"] = "plan"
        rows.append(row)
        if not quiet:
            print("[%2d] %-7s %-4s %-30s %10s"
                  % (i, row["status"], kind, name[:28], human_size(size)))
    return rows


# 会被 Excel / LibreOffice 当公式解释的开头字符
_CSV_FORMULA_PREFIX = ("=", "+", "-", "@", "\t", "\r")


def csv_guard(v):
    """CSV 单元格防公式注入。

    LMS 的文件名 / 活动标题是**外部输入**，若以 = + - @ 开头，
    用 Excel 打开清单时会当成公式执行（CSV Formula Injection）。
    处理方式是在前面加一个单引号，让表格软件按文本处理。

    ★ 只作用于写进 CSV 的文本，**不改实际落盘的文件名**，JSON 清单也不改。
    非字符串（数字 / None / bool）原样返回，避免把 size 变成字符串。
    """
    if not isinstance(v, str):
        return v
    if v.startswith(_CSV_FORMULA_PREFIX):
        return "'" + v
    return v


def write_manifest(path, rows):
    """按扩展名决定写 JSON 还是 CSV"""
    ext = os.path.splitext(path)[1].lower()
    if ext == ".csv":
        import csv
        cols = ["i", "status", "kind", "activity", "name", "size", "dir",
                "ext", "sha256", "server_sha256", "uid", "error"]
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            w.writeheader()
            for r in sorted(rows, key=lambda x: x.get("i", 0)):
                w.writerow({k: csv_guard(v) for k, v in r.items()})
    else:
        with open(path, "w", encoding="utf-8") as f:
            json.dump({"count": len(rows), "files": rows}, f,
                      ensure_ascii=False, indent=2)


if __name__ == "__main__":
    sys.exit(main())
