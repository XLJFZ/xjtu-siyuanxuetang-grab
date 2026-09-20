# -*- coding: utf-8 -*-
"""xjtu-siyuanxuetang-grab 一键发布 —— 打包 + 校验 + 推送 + Release + 附件

为什么要工程化：以前每次发版都要复制上一版脚本、改 4 个常量（TAG / NAME /
BODY / ZIP 路径），v1.0.0 → v1.0.1 → v1.0.2 三次全靠手改，第 4 次必然出错。
现在收敛成一条命令。

用法
----
    set GH_TOKEN=github_pat_xxx

    # 只打包，看看会装进去哪些文件（不联网，可反复跑）
    python release.py --version 1.0.3 --dry-run

    # 正式发布（自动打 tag、建 Release、传附件）
    python release.py --version 1.0.3 --title "文档修正"

    # 说明文字太长就写文件
    python release.py --version 1.0.3 --title "文档修正" --notes notes.md

    # 代码有改动、要先推代码再发版
    python release.py --version 1.0.3 --title "文档修正" --push-code

    # 中途失败后重跑（跳过已建好的 tag / Release，只补缺的部分）
    python release.py --version 1.0.3 --title "文档修正" --resume

设计要点
--------
1. **版本号自检**：发布前查远端 tag 是否已存在，存在就直接报错停下 ——
   防止重演「原地覆盖已发布 Release」那次问题。已发布的版本一律不可变。
2. **上传附件走 curl**：Python urllib 经本机代理访问 uploads.github.com 会 502，
   curl 能通。所以整个脚本的 HTTP 层统一用 curl，避免两套行为。
3. **打包排除规则显式声明**：不靠 .gitignore 猜，用 EXCLUDE 列表 + 体积上限双重兜底，
   绝不把 __pycache__ / .git / 登录态打进 Release 包。
4. **幂等**：--resume 让中断后可以重跑，不会重复建 tag / Release。
5. **tag 必须绑定到明确的源码 commit**：优先级为
   「已存在 tag 指向的 commit」>「本次原子推送产生的 commit」>「远端 main HEAD」。
   并且校验的是**实际要上传的 zip**（不是工作区）与那个 commit 的 tree
   逐文件一致，不一致就停下 —— 不允许出现「tag 指向 A、asset 来自 B」。
   校验通过后用 sha256 锁定这份 zip，上传前再核对一次。
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

OWNER = "XLJFZ"
REPO = "xjtu-siyuanxuetang-grab"
API = "https://api.github.com"
PROXY = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:4774"

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 打包白名单与推送白名单必须是同一份定义，否则「包里有什么」和
# 「仓库里有什么」会各自漂移 —— 详见 tools/release_common.py。
from tools import release_common as RC  # noqa: E402


def find_src():
    """定位发布源目录。

    这个脚本有两种存放位置：
      A. 工作区根（当前布局）：`D:\\xjtu-siyuanxuetang-grab\\release.py`，
         源就是脚本自己所在的目录
      B. 仓库同级（历史布局）：`<工作区>/_release/release.py`，
         源在 `<工作区>/_release/xjtu-lms-grab/`
    """
    for cand in (os.path.join(HERE, "xjtu-lms-grab"), HERE):
        if os.path.isfile(os.path.join(cand, "SKILL.md")):
            return cand
    raise SystemExit("找不到发布源目录（应在其中看到 SKILL.md），当前脚本位于 %s" % HERE)


SRC = find_src()
ZIP_STEM = "xjtu-siyuanxuetang-grab"                # 压缩包与顶层文件夹名

# 打进 Release 包 / 推上仓库的内容 —— 唯一定义在 tools/release_common.py
INCLUDE = RC.INCLUDE
# 排除规则同样只有一份定义（下面几处沿用旧名字，避免改动扩散）
EXCLUDE_NAMES = RC.EXCLUDE_NAMES
EXCLUDE_SUFFIX = RC.EXCLUDE_SUFFIX
EXCLUDE_STATE = RC.EXCLUDE_STATE
EXCLUDE_PROFILE = RC.EXCLUDE_PROFILE

# 单个文件体积上限，超过就报警（防止误把课程资料打进去）
MAX_FILE_MB = 5
MAX_ZIP_MB = 20


# ---------------------------------------------------------------- 输出

class C:
    OK = "\033[92m" if os.name != "nt" or os.environ.get("WT_SESSION") else ""
    WARN = "\033[93m" if OK else ""
    ERR = "\033[91m" if OK else ""
    DIM = "\033[90m" if OK else ""
    END = "\033[0m" if OK else ""


def info(msg):
    print("  " + msg, flush=True)


def ok(msg):
    print("  %s✓%s %s" % (C.OK, C.END, msg), flush=True)


def warn(msg):
    print("  %s!%s %s" % (C.WARN, C.END, msg), flush=True)


def err(msg):
    # 走 stderr 但强制 flush，否则跟 stdout 混排时顺序会乱
    print("  %s✗%s %s" % (C.ERR, C.END, msg), file=sys.stderr, flush=True)


def step(n, total, title):
    print("\n%s[%d/%d]%s %s" % (C.DIM, n, total, C.END, title), flush=True)


# ---------------------------------------------------------------- HTTP（走 curl）

class ApiError(Exception):
    """GitHub 接口错误。code 为 HTTP 状态码，网络层失败时为 None。"""

    def __init__(self, msg, code=None):
        Exception.__init__(self, msg)
        self.code = code


def _curl_config():
    """把凭据写进临时 curl 配置文件，避免 token 出现在 argv。

    argv 在同一台机器上的其他进程可见（ps / 任务管理器 / WMI 查命令行），
    token 放 `-H` 等于把凭据公开给本机所有进程。curl 配置文件的
    `header = "..."` 与 -H 等价，但内容只存在于文件里。
    文件用 mkstemp 创建（POSIX 下即 0600），用完立刻删除。
    """
    tok = token()
    fd, path = tempfile.mkstemp(prefix="ghcurl_", suffix=".cfg")
    try:
        os.write(fd, ('header = "Authorization: Bearer %s"\n'
                      'header = "Accept: application/vnd.github+json"\n'
                      'header = "X-GitHub-Api-Version: 2022-11-28"\n'
                      % tok).encode("utf-8"))
    finally:
        os.close(fd)
    return path


def curl_json(args, expect=None):
    """调 curl 并解析 JSON。expect 是允许的 HTTP 状态码集合。"""
    cfg = _curl_config()
    cmd = ["curl", "-s", "--max-time", "180",
           "-w", "\n%{http_code}", "-K", cfg] + args
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        if r.returncode != 0:
            raise ApiError("curl 退出码 %d: %s" % (r.returncode, r.stderr[:200]))
        out = r.stdout
        if "\n" not in out:
            raise ApiError("curl 无输出: %s" % r.stderr[:200])
        body, _, code_s = out.rpartition("\n")
        try:
            code = int(code_s.strip())
        except ValueError:
            raise ApiError("无法解析 HTTP 状态: %r" % code_s[:40])
        try:
            data = json.loads(body) if body.strip() else None
        except json.JSONDecodeError:
            data = {"__raw__": body[:400]}
        if expect is not None and code not in expect:
            msg = ""
            if isinstance(data, dict):
                msg = data.get("message", "")
            raise ApiError("HTTP %d %s" % (code, msg[:200] or str(data)[:200]),
                           code)
        return code, data
    finally:
        try:
            os.remove(cfg)
        except OSError:
            pass


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not t:
        err("没有 GH_TOKEN 环境变量。先执行：set GH_TOKEN=github_pat_xxx")
        sys.exit(2)
    return t


# ---------------------------------------------------------------- 打包

def collect_files(src):
    """按 INCLUDE 白名单收集文件，返回 [(绝对路径, 包内相对路径)]。

    收集规则只有一份实现（tools/release_common.collect）——
    本地打包、原子推送、CI 打包三处共用，否则「包里有什么」和
    「仓库里有什么」会各自漂移，而 tag 只能绑定到其中一个。
    """
    files = RC.collect(src)
    present = {rel.split("/")[0] for _p, rel in files}
    for entry in INCLUDE:
        if entry not in present:
            warn("INCLUDE 里的 %s 不存在或没有可打包内容，跳过" % entry)
    return files


def build_zip(src, version, dry_run=False):
    files = collect_files(src)
    if not files:
        raise SystemExit("没有可打包的文件，检查 INCLUDE 列表")

    total = sum(os.path.getsize(p) for p, _ in files)
    print("  源目录: %s" % src)
    print("  文件数: %d，原始体积 %.1f KB" % (len(files), total / 1024))

    # 体积体检 —— 课程资料误入时在这里拦下
    big = [(rel, os.path.getsize(p)) for p, rel in files
           if os.path.getsize(p) > MAX_FILE_MB * 1024 * 1024]
    for rel, sz in big:
        warn("大文件 %.1f MB：%s" % (sz / 1024 / 1024, rel))

    if dry_run:
        for p, rel in files:
            print("      %-34s %7.1f KB" % (rel, os.path.getsize(p) / 1024))
        return None, files

    zip_path = os.path.join(HERE, "%s-v%s.zip" % (ZIP_STEM, version))
    if os.path.exists(zip_path):
        os.remove(zip_path)

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in files:
            # 顶层套一层文件夹，解压不会散落一地
            z.write(p, "%s/%s" % (ZIP_STEM, rel))

    size = os.path.getsize(zip_path)
    if size > MAX_ZIP_MB * 1024 * 1024:
        warn("压缩包 %.1f MB 超过预期上限 %d MB，确认一下是否打进了不该打的东西"
             % (size / 1024 / 1024, MAX_ZIP_MB))
    print("  压缩包: %s (%.1f KB)" % (os.path.basename(zip_path), size / 1024))
    return zip_path, files


def count_cases(root):
    """静态数一数 tests/ 下有多少个 test_ 方法，用来和实跑结果交叉核对。"""
    n = 0
    tdir = os.path.join(root, "tests")
    for fn in os.listdir(tdir):
        if not (fn.startswith("test_") and fn.endswith(".py")):
            continue
        with open(os.path.join(tdir, fn), encoding="utf-8") as f:
            for line in f:
                if re.match(r"\s+def test_", line):
                    n += 1
    return n


def _parse_ran(lines):
    """从 unittest 输出里取实际执行的用例数，取不到返回 None。"""
    for l in lines:
        m = re.match(r"Ran (\d+) tests?", l.strip())
        if m:
            return int(m.group(1))
    return None


def verify_zip(zip_path):
    """解包到临时目录跑一遍测试，确认包是能用的。"""
    tmp = tempfile.mkdtemp(prefix="relverify_")
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        root = os.path.join(tmp, ZIP_STEM)

        # 关键文件必须在
        for must in ("README.md", "SKILL.md", "docs/MAINTAINING.md",
                     "scripts/lms_fetch.py",
                     "scripts/lms_organize.py", "scripts/lms_selfcheck.py",
                     "tests/test_organize.py", "tests/test_fetch.py",
                     "tests/test_selfcheck.py", "tests/test_push.py",
                     "tools/gh_push_dir.py", "tools/release_common.py",
                     ".github/workflows/ci.yml", ".github/workflows/release.yml",
                     ".github/scripts/pack.py"):
            if not os.path.isfile(os.path.join(root, must)):
                raise SystemExit("校验失败：包里缺 %s" % must)

        # 登录态绝不能有
        for r, _d, fs in os.walk(root):
            for fn in fs:
                if EXCLUDE_STATE.search(fn):
                    raise SystemExit("校验失败：包里混进了登录态 %s" % fn)

        # 跑离线测试（每个文件都要跑，别只跑一个）
        n_files = 0
        total_ran = 0
        for t in ("test_organize.py", "test_fetch.py", "test_selfcheck.py",
                  "test_push.py"):
            r = subprocess.run([sys.executable,
                                os.path.join(root, "tests", t)],
                               capture_output=True, text=True)
            tail = (r.stderr or r.stdout).strip().splitlines()
            if r.returncode != 0:
                print("\n".join(tail[-8:]))
                raise SystemExit("校验失败：%s 没通过" % t)
            # 实际执行的用例数由 unittest 自己报，别拿文件数顶替
            ran = _parse_ran(tail)
            if ran is None:
                raise SystemExit("校验失败：%s 没有输出用例数，测试可能没跑起来" % t)
            total_ran += ran
            n_files += 1

        # 交叉核对：静态数出来的用例数和实跑数应当一致，
        # 不一致说明有 test_ 方法没被收集到（拼错、被跳过、类名不对）。
        static_n = count_cases(root)
        msg = "包内自测通过（%d 个测试文件，共 %d 个用例）" % (n_files, total_ran)
        if static_n != total_ran:
            warn("%s —— 但静态扫到 %d 个 test_ 方法，差 %d 个，查一下是否有用例没被收集"
                 % (msg, static_n, static_n - total_ran))
        else:
            ok(msg)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# ---------------------------------------------------------------- GitHub 操作

def get_main_sha():
    _, d = curl_json(["https://api.github.com/repos/%s/%s/git/ref/heads/main" % (OWNER, REPO)],
                     expect=(200,))
    return d["object"]["sha"]


def tag_exists(tag):
    code, _ = curl_json(["https://api.github.com/repos/%s/%s/git/ref/tags/%s"
                         % (OWNER, REPO, tag)], expect=(200, 404, 409))
    return code == 200


def release_by_tag(tag):
    code, d = curl_json(["https://api.github.com/repos/%s/%s/releases/tags/%s"
                         % (OWNER, REPO, tag)], expect=(200, 404))
    return d if code == 200 else None


def remote_tree(sha):
    """取某个 commit 的完整文件清单 -> {path: (blob_sha, mode)}。

    这是「tag SHA 与本地内容是否一致」的判据来源：Git 的 blob sha 由内容决定，
    所以本地文件与远端同一路径的 blob sha 相同，即证明是同一份内容。
    """
    _code, c = curl_json(["https://api.github.com/repos/%s/%s/git/commits/%s"
                          % (OWNER, REPO, sha)], expect=(200,))
    _code, t = curl_json(["https://api.github.com/repos/%s/%s/git/trees/%s"
                          "?recursive=1" % (OWNER, REPO, c["tree"]["sha"])],
                         expect=(200,))
    if t.get("truncated"):
        raise SystemExit("远端 tree 被截断，无法完成一致性校验（仓库过大）")
    out = {}
    for it in t.get("tree", []):
        if it.get("type") == "blob":
            out[it["path"]] = (it.get("sha"), it.get("mode") or "100644")
    return out


def tag_commit_sha(tag):
    """tag 指向的 commit SHA。annotated tag 需要再解一层。"""
    _code, d = curl_json(["https://api.github.com/repos/%s/%s/git/ref/tags/%s"
                          % (OWNER, REPO, tag)], expect=(200,))
    obj = d["object"]
    if obj.get("type") == "tag":
        _code, t = curl_json(["https://api.github.com/repos/%s/%s/git/tags/%s"
                              % (OWNER, REPO, obj["sha"])], expect=(200,))
        obj = t["object"]
    return obj["sha"]


def sha256_file(path):
    """分块算文件 sha256 —— zip 可能有上百 MB，不能整个读进内存。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest()


def archive_blobs(zip_path):
    """解压 Release zip，返回 {包内相对路径: git blob sha}。

    ★ 比对对象是「实际要上传的那个 zip」，不是当前工作区：
      打包和推送之间存在 TOCTOU —— 若工作区在打包之后又被改过，
      「工作区 == 远端」的校验会通过，但发出去的仍是旧内容。
      只有 zip 本身才算数。

    zip 里有一层顶层目录 ZIP_STEM/，比对前剥掉，否则每个路径都多一段前缀。
    范围仍由 tools/release_common.py 的 INCLUDE / 排除规则决定，
    不另立一份清单。
    """
    tmp = tempfile.mkdtemp(prefix="archblob_")
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        root = os.path.join(tmp, ZIP_STEM)
        if not os.path.isdir(root):
            raise SystemExit("压缩包顶层不是 %s/，结构不对" % ZIP_STEM)
        return RC.local_blobs(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def verify_archive_matches_remote(zip_path, sha):
    """★ 最终不变量：实际 zip 的内容 == source_sha 对应 commit 的源码。

    三个方向都要查，缺一不可：
      missing —— zip 里有、远端 commit 没有（打包进了未提交的东西）
      changed —— 两边都有但内容不同（最典型的 TOCTOU 症状）
      absent  —— 远端有、zip 里没有（推送了却没打进包，发布不完整）

    返回 {ok, missing, changed, absent, n_archive}。
    """
    archive = archive_blobs(zip_path)
    remote = remote_tree(sha)
    missing = sorted(p for p in archive if p not in remote)
    changed = sorted(p for p in archive
                     if p in remote and remote[p][0] != archive[p])
    absent = sorted(p for p in remote
                    if RC.in_scope(p) and p not in archive)
    return {"ok": not (missing or changed or absent),
            "missing": missing, "changed": changed, "absent": absent,
            "n_archive": len(archive)}


def report_archive_mismatch(sha, rep):
    for p in rep["missing"]:
        err("包里有、远端 %s 没有：%s" % (sha[:12], p))
    for p in rep["changed"]:
        err("内容不一致（包 vs commit）：%s" % p)
    for p in rep["absent"]:
        err("远端有、包里没有：%s" % p)


def assert_archive_unchanged(zip_path, expected_sha256):
    """上传前再算一次 zip 的 sha256，防止「校验通过后被替换」。"""
    actual = sha256_file(zip_path)
    if actual != expected_sha256:
        err("压缩包在校验之后被改动过（sha256 %s != %s），停止上传"
            % (actual[:12], expected_sha256[:12]))
        raise SystemExit(1)


def push_code(src, version, branch="main"):
    """用仓库内的原子推送器同步源码，返回本次产生的 commit SHA。

    ★ 为什么必须由它返回 SHA 而不是回头去读 main：
      `push` 完成后再 `get_main_sha()`，读到的是「此刻的 main」——
      如果这中间又有人推了一次，拿到的就是别人的 commit，
      tag 会打在错误的位置上。推送器自己知道它创建了哪个 commit，
      由它返回才是可靠的。

    ★ 为什么不再容忍「部分成功」：旧脚本遇到无权限文件会跳过但退出码仍为 0，
      于是「包是完整的、仓库是残缺的」也能一路走到发版。
      现在的推送器要么整体成功、要么整体失败。
    """
    pusher = os.path.abspath(os.path.join(HERE, "tools", "gh_push_dir.py"))
    if not os.path.isfile(pusher):
        err("找不到仓库内的推送脚本 %s" % pusher)
        err("它随仓库一起分发；如果是旧版本解压出来的包，请重新下载完整包")
        raise SystemExit(1)

    fd, result_path = tempfile.mkstemp(prefix="pushres_", suffix=".json")
    os.close(fd)
    cmd = [sys.executable, pusher,
           "--src", src, "--repo", "%s/%s" % (OWNER, REPO),
           "--branch", branch,
           "--message", "release: prepare v%s" % version,
           "--result-json", result_path]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding="utf-8", errors="replace")
        for line in (r.stdout or "").splitlines()[-16:]:
            info(line)
        if r.returncode != 0:
            err("代码推送失败（退出码 %d）" % r.returncode)
            err("推送器在有文件没推成功时不会返回 0，不要绕过这一步")
            print((r.stdout or "") + (r.stderr or ""))
            raise SystemExit(1)
        try:
            with open(result_path, encoding="utf-8") as f:
                rep = json.load(f)
        except (OSError, ValueError) as e:
            err("读不到推送结果：%s" % e)
            raise SystemExit(1)
    finally:
        try:
            os.remove(result_path)
        except OSError:
            pass

    sha = rep.get("sha")
    if not sha:
        err("推送结果里没有 commit SHA，无法把 tag 绑定到明确的源码")
        raise SystemExit(1)
    info("变更 +%d / ~%d / -%d，未变 %d"
         % (len(rep.get("added") or []), len(rep.get("modified") or []),
            len(rep.get("deleted") or []), rep.get("unchanged", 0)))
    return sha


def resolve_source_sha(args, src, tag):
    """确定本次发布要打 tag 的 commit SHA。

    优先级：
      ① tag 已存在      -> 用它已经指向的 commit（已发布内容不可变，
                            --resume 只能补缺，不能换内容）
      ② --push-code     -> 原子推送返回的那个 commit
      ③ 其余            -> 远端 main HEAD

    注意：这里**不做内容校验**。源码内容是否与发布包一致，
    由之后的 verify_archive_matches_remote() 用「实际 zip」去证明 ——
    工作区比较挡不住打包与推送之间的改动（TOCTOU）。
    """
    if tag_exists(tag):
        sha = tag_commit_sha(tag)
        info("tag %s 已存在，内容以它指向的 %s 为准" % (tag, sha[:12]))
    elif args.push_code:
        sha = push_code(src, args.version)
        ok("原子推送完成，一个 commit：%s" % sha[:12])
    else:
        sha = get_main_sha()
        info("未推送代码，以远端 main HEAD %s 为源码 commit" % sha[:12])
    return sha


def create_tag(tag, sha):
    _, d = curl_json(["-X", "POST",
                      "-H", "Content-Type: application/json",
                      "-d", json.dumps({"ref": "refs/tags/" + tag, "sha": sha}),
                      "https://api.github.com/repos/%s/%s/git/refs" % (OWNER, REPO)],
                     expect=(201,))
    return d


def create_release(tag, name, body):
    payload = json.dumps({
        "tag_name": tag, "target_commitish": "main",
        "name": name, "body": body,
        "draft": False, "prerelease": False,
    }, ensure_ascii=False).encode("utf-8")
    # body 里有中文和换行，走临时文件传而不是命令行参数
    with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as f:
        f.write(payload)
        tmp = f.name
    try:
        _, d = curl_json(["-X", "POST",
                          "-H", "Content-Type: application/json",
                          "--data-binary", "@" + tmp,
                          "https://api.github.com/repos/%s/%s/releases" % (OWNER, REPO)],
                         expect=(201,))
    finally:
        os.remove(tmp)
    return d


def update_notes(tag, name, body):
    """更新已发布 Release 的标题和说明。

    注意：**没有 `PATCH /releases/tags/<tag>` 这个端点**（会 404），
    必须先用 tag 查出数字 id，再 `PATCH /releases/<id>`。
    """
    _, rel = curl_json(["https://api.github.com/repos/%s/%s/releases/tags/%s"
                        % (OWNER, REPO, tag)], expect=(200,))
    payload = json.dumps({"name": name, "body": body}, ensure_ascii=False).encode("utf-8")
    with tempfile.NamedTemporaryFile("wb", suffix=".json", delete=False) as f:
        f.write(payload)
        tmp = f.name
    try:
        _, d = curl_json(["-X", "PATCH",
                          "-H", "Content-Type: application/json",
                          "--data-binary", "@" + tmp,
                          "https://api.github.com/repos/%s/%s/releases/%d"
                          % (OWNER, REPO, rel["id"])], expect=(200,))
    finally:
        os.remove(tmp)
    return d


# 值得重试的上传失败：限流与服务端临时故障。
# 其余 4xx（400 参数错、401 凭据错、404 release 不存在、422 校验失败）
# 重试多少次都是同一个结果，盲目重试只会拖长失败时间。
RETRY_ASSET_STATUS = (408, 425, 429, 500, 502, 503, 504)


def upload_asset(release_id, zip_path, attempts=3):
    """上传附件。必须走 curl —— urllib 经代理对 uploads.github.com 会 502。

    ★ 旧写法是 `code, d = curl_json(..., expect=(201,))` 之后判断 code：
      curl_json 在返回非 201 时**已经抛了 ApiError**，那句 `warn(重试)`
      永远执行不到，第二次 attempt 也就永远不存在。现在改成在
      except 里决定要不要重试，并按状态码区分「临时故障」与「永久错误」。
    """
    zsize = os.path.getsize(zip_path)
    last = None
    for attempt in range(1, attempts + 1):
        try:
            _code, d = curl_json([
                "-X", "POST",
                "-H", "Content-Type: application/zip",
                "-H", "Content-Length: %d" % zsize,
                "--data-binary", "@" + zip_path,
                "https://uploads.github.com/repos/%s/%s/releases/%d/assets?name=%s"
                % (OWNER, REPO, release_id, os.path.basename(zip_path)),
            ], expect=(201,))
            return d
        except ApiError as e:
            last = e
            if e.code is not None and e.code not in RETRY_ASSET_STATUS:
                raise                       # 4xx 是永久错误，重试没有意义
            if attempt == attempts:
                raise
            warn("第 %d 次上传失败（%s），%d 秒后重试" % (attempt, e, 3 * attempt))
            time.sleep(3 * attempt)
    raise last


# ---------------------------------------------------------------- 主流程

def main():
    ap = argparse.ArgumentParser(
        description="一键发布 xjtu-siyuanxuetang-grab",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--version", required=True, help="版本号，不带 v，如 1.0.3")
    ap.add_argument("--title", default="", help="Release 标题里版本号后面的部分")
    ap.add_argument("--notes", default=None, help="说明文字文件（md），不传则自动生成")
    ap.add_argument("--push-code", action="store_true", help="发布前先把源目录推到 main")
    ap.add_argument("--dry-run", action="store_true", help="只打包+校验，不碰 GitHub")
    ap.add_argument("--resume", action="store_true", help="跳过已存在的 tag/Release，只补缺")
    ap.add_argument("--update-notes", action="store_true",
                    help="只更新已发布 Release 的说明文字，不重新打包上传")
    ap.add_argument("--yes", action="store_true", help="跳过发布前确认")
    args = ap.parse_args()

    ver = args.version.lstrip("vV")
    tag = "v" + ver

    # --title 只写「版本号后面的那部分」。人很容易顺手写成 "v1.1.0 - xxx"，
    # 那样拼出来就成了 "v1.1.0 —— v1.1.0 - xxx"。这里自动剥掉重复的版本号前缀。
    title = args.title.strip()
    for dup in (tag, ver):
        if title.lower().startswith(dup.lower()):
            title = title[len(dup):].lstrip(" \t-—–:：")
            break
    name = ("%s —— %s" % (tag, title)) if title else tag

    # 参数预检 —— 放在最前面，别等 tag 都建好了才发现文件不存在
    if args.notes and not os.path.isfile(args.notes):
        err("--notes 指定的文件不存在：%s" % args.notes)
        info("路径相对当前工作目录：%s" % os.getcwd())
        return 2

    # ---- 特殊模式：只改说明 ----
    if args.update_notes:
        print("=" * 62)
        print("  更新 %s 的 Release 说明" % tag)
        print("=" * 62)
        body = ""
        if args.notes:
            with open(args.notes, encoding="utf-8") as f:
                body = f.read()
        else:
            err("--update-notes 需要同时指定 --notes <文件>")
            return 2
        d = update_notes(tag, name, body)
        ok("已更新：%s" % d["html_url"])
        info("标题 %s，正文 %d 字" % (d["name"], len(d["body"])))
        return 0

    print("=" * 62)
    print("  发布 %s/%s  →  %s" % (OWNER, REPO, tag))
    print("=" * 62)

    total_steps = 4 if args.dry_run else 7
    n = 0

    # ---- 1. 版本号自检 ----
    n += 1
    step(n, total_steps, "版本号自检")
    if args.dry_run:
        info("干跑模式，跳过远端检查")
    else:
        exists = tag_exists(tag)
        rel_now = release_by_tag(tag) if exists else None
        if not exists:
            ok("tag %s 未被占用" % tag)
        elif not args.resume:
            err("tag %s 已存在！" % tag)
            info("已发布的版本内容不可变，请换一个版本号。")
            info("如果上次发到一半中断了，加 --resume 只补缺漏的部分。")
            return 1
        else:
            # resume 模式：只有真缺东西才继续，否则拒绝
            missing = []
            if not rel_now:
                missing.append("Release")
            elif not rel_now.get("assets"):
                missing.append("Release 附件")
            if not missing:
                err("tag %s 已存在，且 Release 与附件都齐全，没有可补的东西。" % tag)
                info("确实要重发，请换版本号（已发布内容不可变）。")
                return 1
            warn("tag %s 已存在，缺 %s，按 --resume 继续补" % (tag, "、".join(missing)))

    # ---- 2. 打包 ----
    n += 1
    step(n, total_steps, "打包")
    zip_path, files = build_zip(SRC, ver, dry_run=args.dry_run)

    if args.dry_run:
        print("\n%s干跑结束。以上是会被打包的内容。%s" % (C.DIM, C.END))
        return 0

    # ---- 3. 包体校验 ----
    n += 1
    step(n, total_steps, "包体校验")
    verify_zip(zip_path)
    ok("压缩包结构正确、无登录态、离线测试通过")

    # ---- 4. 确定源码 commit（可选推代码）----
    n += 1
    step(n, total_steps, "确定源码 commit")
    source_sha = resolve_source_sha(args, SRC, tag)

    # ---- 5. 实际发布包 vs 源码 commit 逐文件校验 ----
    # ★ 校验对象是 zip 本身，不是工作区。打包之后工作区又被动过的场景
    #   （TOCTOU）在这里暴露：zip 还是旧内容，而 push 的已是新内容，
    #   若拿工作区比会「验证通过」，发出去的却是旧包。
    n += 1
    step(n, total_steps, "校验实际发布包与源码 commit 一致")
    arep = verify_archive_matches_remote(zip_path, source_sha)
    if not arep["ok"]:
        err("压缩包内容与远端 %s 的源码不一致，停止发布" % source_sha[:12])
        report_archive_mismatch(source_sha, arep)
        err("多半是打包之后工作区又变了 —— 重新跑一次发布即可")
        raise SystemExit(1)
    ok("zip 内 %d 个文件与源码 commit %s 的 tree 逐文件一致"
       % (arep["n_archive"], source_sha[:12]))

    # 锁定这一个 zip：之后无论谁改它，上传前都会被 sha256 拦下
    archive_sha256 = sha256_file(zip_path)
    info("asset sha256: %s" % archive_sha256)

    # ---- 6. 确认 ----
    if not args.yes:
        print("\n即将发布 %s，附件 %s (%.1f KB)"
              % (tag, os.path.basename(zip_path), os.path.getsize(zip_path) / 1024))
        print("源码 commit: %s" % source_sha)
        print("asset sha256: %s" % archive_sha256)
        ans = input("确认？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            info("已取消。压缩包留在 %s" % zip_path)
            return 0

    # ---- 7. 打 tag + 建 Release + 传附件 ----
    n += 1
    step(n, total_steps, "打 tag / 建 Release / 传附件")
    assert_archive_unchanged(zip_path, archive_sha256)

    existing = release_by_tag(tag) if args.resume else None
    if existing:
        ok("Release 已存在，跳过创建：%s" % existing["html_url"])
        info("内容仍绑定原 commit %s（--resume 只补缺，不改内容）" % source_sha[:12])
        rel = existing
    else:
        if not tag_exists(tag):
            create_tag(tag, source_sha)
            ok("tag %s 已创建 -> %s" % (tag, source_sha[:12]))
        body = ""
        if args.notes:
            with open(args.notes, encoding="utf-8") as f:
                body = f.read()
        else:
            body = auto_notes(ver, files)
        rel = create_release(tag, name, body)
        ok("Release: %s" % rel["html_url"])

    # 附件去重：同名已存在就先删，保证重跑不会留下两个包
    for a in rel.get("assets", []):
        if a["name"] == os.path.basename(zip_path):
            curl_json(["-X", "DELETE",
                       "https://api.github.com/repos/%s/%s/releases/assets/%d"
                       % (OWNER, REPO, a["id"])], expect=(204, 200))
            warn("删掉同名旧附件 %s" % a["name"])

    # 上传前最后一次确认：就是校验过的那个文件，一个字节都不能变
    assert_archive_unchanged(zip_path, archive_sha256)
    up = upload_asset(rel["id"], zip_path)
    ok("附件 %s (%.1f KB，sha256 %s…)"
       % (up.get("name"), up.get("size", 0) / 1024, archive_sha256[:12]))

    print("\n" + "=" * 62)
    print("  发布完成")
    print("  %s" % rel["html_url"])
    print("  直链: %s" % up.get("browser_download_url"))
    print("=" * 62)
    return 0


def auto_notes(ver, files):
    """没给 --notes 时按打包内容自动生成一份说明骨架。"""
    groups = {}
    for _p, rel in files:
        top = rel.split("/")[0]
        groups[top] = groups.get(top, 0) + 1
    lines = ["## v%s" % ver, "", "### 包内容", ""]
    for k in sorted(groups):
        lines.append("- `%s`（%d 个文件）" % (k, groups[k]))
    lines += ["", "### 安装", "",
              "下载下方 zip 解压即用，不需要 git。", "",
              "```bash",
              "pip install playwright          # 只有登录脚本需要",
              "python scripts/lms_login.py --course <课程ID>",
              "python scripts/lms_fetch.py  --course <课程ID> --out ./课程资料 --organize",
              "```", ""]
    return "\n".join(lines)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ApiError as e:
        err(str(e))
        raise SystemExit(1)
    except KeyboardInterrupt:
        print("\n中断。")
        raise SystemExit(130)
