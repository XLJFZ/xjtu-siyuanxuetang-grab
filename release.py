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
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile

OWNER = "XLJFZ"
REPO = "xjtu-siyuanxuetang-grab"
API = "https://api.github.com"
PROXY = os.environ.get("HTTPS_PROXY") or "http://127.0.0.1:4774"

HERE = os.path.dirname(os.path.abspath(__file__))


def find_src():
    """定位发布源目录。

    这个脚本有两种存放位置：
      A. 仓库同级（维护者工作区）：`_release/release.py`，源在 `_release/xjtu-lms-grab/`
      B. 仓库内部（随包分发）：  `<repo>/release.py`，源就是脚本自己所在的目录
    """
    for cand in (os.path.join(HERE, "xjtu-lms-grab"), HERE):
        if os.path.isfile(os.path.join(cand, "SKILL.md")):
            return cand
    raise SystemExit("找不到发布源目录（应在其中看到 SKILL.md），当前脚本位于 %s" % HERE)


SRC = find_src()
ZIP_STEM = "xjtu-siyuanxuetang-grab"                # 压缩包与顶层文件夹名

# 打进 Release 包的内容（白名单，不是黑名单 —— 新文件要显式加进来）
INCLUDE = [
    ".gitignore",
    "LICENSE",
    "README.md",
    "SKILL.md",
    "prompt.md",
    "ci.yml.txt",
    "release.yml.txt",
    "pack.py.txt",
    "enable-ci.bat",
    "release.py",
    "scripts",
    "tests",
]

# 就算在 INCLUDE 目录里也绝不打包
EXCLUDE_NAMES = {
    "__pycache__", ".git", ".github", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", ".idea", ".vscode",
}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".pyd", ".log", ".swp")
# 登录态绝不能进包
EXCLUDE_STATE = re.compile(r"(^|/)(state_.*\.json|.*\.state\.json|storage_state\.json|cookies.*\.json|.*\.har)$", re.I)

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
    pass


def curl_json(args, expect=None):
    """调 curl 并解析 JSON。expect 是允许的 HTTP 状态码集合。"""
    cmd = ["curl", "-s", "--max-time", "180",
           "-w", "\n%{http_code}",
           "-H", "Authorization: Bearer " + token(),
           "-H", "Accept: application/vnd.github+json"] + args
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
        raise ApiError("HTTP %d %s" % (code, msg[:200] or str(data)[:200]))
    return code, data


def token():
    t = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not t:
        err("没有 GH_TOKEN 环境变量。先执行：set GH_TOKEN=github_pat_xxx")
        sys.exit(2)
    return t


# ---------------------------------------------------------------- 打包

def collect_files(src):
    """按 INCLUDE 白名单收集文件，返回 [(绝对路径, 包内相对路径)]。"""
    out = []
    for entry in INCLUDE:
        full = os.path.join(src, entry)
        if not os.path.exists(full):
            warn("INCLUDE 里的 %s 不存在，跳过" % entry)
            continue
        if os.path.isfile(full):
            out.append((full, entry))
            continue
        for root, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_NAMES]
            for fn in sorted(files):
                p = os.path.join(root, fn)
                rel = os.path.relpath(p, src).replace(os.sep, "/")
                if fn in EXCLUDE_NAMES or fn.endswith(EXCLUDE_SUFFIX):
                    continue
                if EXCLUDE_STATE.search(rel):
                    warn("跳过疑似登录态：%s" % rel)
                    continue
                out.append((p, rel))
    return sorted(out, key=lambda x: x[1])


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


def verify_zip(zip_path):
    """解包到临时目录跑一遍测试，确认包是能用的。"""
    tmp = tempfile.mkdtemp(prefix="relverify_")
    try:
        with zipfile.ZipFile(zip_path) as z:
            z.extractall(tmp)
        root = os.path.join(tmp, ZIP_STEM)

        # 关键文件必须在
        for must in ("README.md", "SKILL.md", "scripts/lms_fetch.py",
                     "scripts/lms_organize.py", "tests/test_organize.py"):
            if not os.path.isfile(os.path.join(root, must)):
                raise SystemExit("校验失败：包里缺 %s" % must)

        # 登录态绝不能有
        for r, _d, fs in os.walk(root):
            for fn in fs:
                if EXCLUDE_STATE.search(fn):
                    raise SystemExit("校验失败：包里混进了登录态 %s" % fn)

        # 跑离线测试
        r = subprocess.run([sys.executable, os.path.join(root, "tests", "test_organize.py")],
                           capture_output=True, text=True)
        tail = (r.stderr or r.stdout).strip().splitlines()
        if r.returncode != 0:
            print("\n".join(tail[-8:]))
            raise SystemExit("校验失败：离线测试没通过")
        ran = [l for l in tail if l.startswith("Ran ")]
        ok("包内自测通过（%s）" % (ran[0] if ran else "OK"))
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


def push_code(src, branch="main"):
    """调 gh_push_dir.py 把源目录推上去（复用已有脚本，不重写）。"""
    pusher = os.path.join(HERE, "..", "_工具", "gh_push_dir.py")
    pusher = os.path.abspath(pusher)
    if not os.path.isfile(pusher):
        warn("找不到 %s，跳过代码推送" % pusher)
        return
    r = subprocess.run([sys.executable, pusher, src, "%s/%s" % (OWNER, REPO), branch],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    for line in (r.stdout or "").splitlines()[-14:]:
        info(line)
    if r.returncode != 0:
        err("代码推送失败")
        print((r.stdout or "") + (r.stderr or ""))
        raise SystemExit(1)


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


def upload_asset(release_id, zip_path):
    """上传附件。必须走 curl —— urllib 经代理对 uploads.github.com 会 502。"""
    zsize = os.path.getsize(zip_path)
    for attempt in (1, 2):
        code, d = curl_json([
            "-X", "POST",
            "-H", "Content-Type: application/zip",
            "-H", "Content-Length: %d" % zsize,
            "--data-binary", "@" + zip_path,
            "https://uploads.github.com/repos/%s/%s/releases/%d/assets?name=%s"
            % (OWNER, REPO, release_id, os.path.basename(zip_path)),
        ], expect=(201,))
        if code == 201:
            return d
        warn("第 %d 次上传返回 %d，重试" % (attempt, code))
    raise ApiError("附件上传失败")


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
    name = ("%s —— %s" % (tag, args.title)) if args.title else tag

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

    total_steps = 4 if args.dry_run else (6 if args.push_code else 5)
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

    # ---- 4. 推代码（可选）----
    if args.push_code:
        n += 1
        step(n, total_steps, "推送代码到 main")
        push_code(SRC)
        ok("代码已推送")

    # ---- 5. 确认 ----
    if not args.yes:
        print("\n即将发布 %s，附件 %s (%.1f KB)"
              % (tag, os.path.basename(zip_path), os.path.getsize(zip_path) / 1024))
        ans = input("确认？[y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            info("已取消。压缩包留在 %s" % zip_path)
            return 0

    # ---- 6. 打 tag + 建 Release + 传附件 ----
    n += 1
    step(n, total_steps, "打 tag / 建 Release / 传附件")

    existing = release_by_tag(tag) if args.resume else None
    if existing:
        ok("Release 已存在，跳过创建：%s" % existing["html_url"])
        rel = existing
    else:
        sha = get_main_sha()
        info("目标 commit: %s" % sha[:12])
        if not tag_exists(tag):
            create_tag(tag, sha)
            ok("tag %s 已创建" % tag)
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

    up = upload_asset(rel["id"], zip_path)
    ok("附件 %s (%.1f KB)" % (up.get("name"), up.get("size", 0) / 1024))

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
