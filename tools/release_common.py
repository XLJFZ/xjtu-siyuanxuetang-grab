# -*- coding: utf-8 -*-
"""发布白名单 —— 「哪些文件属于这个项目」的唯一定义。

三处共用同一份定义，避免各写一份导致本地包 / CI 包 / 远端仓库三者漂移：

    release.py                本地打包 + 推送白名单 + 一致性校验
    tools/gh_push_dir.py      原子推送时的本地清单
    .github/scripts/pack.py   CI 打包

为什么必须是白名单而不是黑名单：源目录里散落的打包产物
（`xjtu-siyuanxuetang-grab-v*.zip`）用黑名单挡不住，
曾经就是这样混进仓库根目录的。
"""
import hashlib
import os
import re

# 属于本项目的顶层条目。可以是文件，也可以是目录（整个目录纳入）。
INCLUDE = [
    ".gitignore",
    ".github",
    "LICENSE",
    "README.md",
    "SKILL.md",
    "prompt.md",
    "release.py",
    "scripts",
    "tests",
    "tools",
    "docs",
]

# 就算在 INCLUDE 目录里也绝不打包 / 绝不推送
EXCLUDE_NAMES = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", ".idea", ".vscode", "dist",
}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".pyd", ".log", ".swp")

# 登录态 / 活动清单 / 浏览器 profile 绝不能进包或进仓库（与 .gitignore 同步）
# (\.tmp)? —— lms_login 落盘走「写 <state>.tmp 再原子替换」，进程中途死掉
# 会留下 state_xxx.json.tmp 半成品；它同样带着 cookie，必须和正主一样拦。
EXCLUDE_STATE = re.compile(
    r"(^|/)(state_.*\.json|.*\.state\.json|storage_state\.json|cookies.*\.json"
    r"|activities_.*\.json)(\.tmp)?$|(^|/).*\.har$", re.I)
EXCLUDE_PROFILE = re.compile(r"(^|/)profile_[^/]+(/|$)")


def rel_excluded(rel):
    """包内相对路径是否命中排除规则（目录段 / 扩展名 / 凭据三类）。"""
    parts = rel.split("/")
    if any(p in EXCLUDE_NAMES for p in parts):
        return True
    if parts and parts[-1].endswith(EXCLUDE_SUFFIX):
        return True
    return bool(EXCLUDE_STATE.search(rel) or EXCLUDE_PROFILE.search(rel))


def collect(root, include=None):
    """按白名单收集文件，返回 [(绝对路径, 包内相对路径)]，按相对路径排序。"""
    out = []
    for entry in include or INCLUDE:
        full = os.path.join(root, entry)
        if not os.path.exists(full):
            continue
        if os.path.isfile(full):
            if not rel_excluded(entry):
                out.append((full, entry))
            continue
        for r, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_NAMES]
            for fn in sorted(files):
                p = os.path.join(r, fn)
                rel = os.path.relpath(p, root).replace(os.sep, "/")
                if rel_excluded(rel):
                    continue
                out.append((p, rel))
    return sorted(out, key=lambda x: x[1])


def git_blob_sha(data):
    """Git blob 对象名 —— 与 `git hash-object` 同一算法。

    用它而不是内容哈希，是为了能直接和 GitHub 的 tree API 返回的 sha 比对：
    「本地文件」与「远端 commit 里的同一个文件」是否同一份内容，
    一次 sha 比对就能定论。
    """
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def git_blob_sha_file(path):
    with open(path, "rb") as f:
        return git_blob_sha(f.read())


def local_blobs(root, include=None):
    """本地白名单文件 -> {相对路径: blob sha}。"""
    return {rel: git_blob_sha_file(p) for p, rel in collect(root, include)}


def in_scope(rel, include=None):
    """某个远端路径是否落在白名单管辖范围内。

    只用来决定「远端有、本地没有的文件要不要删」——
    白名单外的东西（例如仓库里遗留的旧脚本）一律不碰。
    """
    for entry in include or INCLUDE:
        if rel == entry or rel.startswith(entry.rstrip("/") + "/"):
            return True
    return False


def compare(local, remote, include=None):
    """本地清单 vs 远端清单 -> 变更集合。

    local  : {rel: blob_sha}
    remote : {rel: (blob_sha, mode)}

    返回 {added, modified, deleted, unchanged}（前三者的元素均为相对路径，
    unchanged 为数量）。deleted 只包含**白名单范围内**的远端文件，
    范围外的远端文件不会被这次推送删除。
    """
    added = sorted(p for p in local if p not in remote)
    modified = sorted(p for p in local
                      if p in remote and remote[p][0] != local[p])
    deleted = sorted(p for p in remote
                     if p not in local
                     and in_scope(p, include)
                     and not rel_excluded(p))
    unchanged = len(local) - len(added) - len(modified)
    return {"added": added, "modified": modified, "deleted": deleted,
            "unchanged": unchanged}
