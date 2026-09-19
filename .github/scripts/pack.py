#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 用的打包脚本 —— 把仓库内容打成 dist/xjtu-siyuanxuetang-grab-v<ver>.zip

和本地 release.py 的 build_zip 保持同一套排除规则，但这里是**仓库视角**：
源目录就是仓库根目录，不像本地那样指到工作区根。

为什么要单独一份：release.py 里有本机路径和直传逻辑，
在 GitHub Runner 上跑不通。CI 里只做「打包」这一件事，
「发 Release」交给 softprops/action-gh-release 这个成熟 action。

注意：INCLUDE 必须与 release.py 里的那份保持同步，否则本地发的包和
CI 发的包内容会不一致（踩过：这里漏了 release.py，而本地那份有）。
"""
import argparse
import os
import re
import sys
import zipfile

# 仓库根目录（本文件位于 .github/scripts/ 下，往上两级）
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))

PKG_NAME = "xjtu-siyuanxuetang-grab"

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
]

EXCLUDE_DIRS = {
    "__pycache__", ".git", ".pytest_cache", ".mypy_cache",
    ".ruff_cache", ".venv", "venv", ".idea", ".vscode", "dist",
}
EXCLUDE_SUFFIX = (".pyc", ".pyo", ".pyd", ".log", ".swp")
# 登录态 / 活动清单 / 浏览器 profile 绝不能进包（与 .gitignore 保持同步）
EXCLUDE_STATE = re.compile(
    r"(^|/)(state_.*\.json|.*\.state\.json|storage_state\.json|cookies.*\.json"
    r"|activities_.*\.json|.*\.har)$",
    re.I)
EXCLUDE_PROFILE = re.compile(r"(^|/)profile_[^/]+(/|$)")


def collect():
    found = []
    for entry in INCLUDE:
        full = os.path.join(ROOT, entry)
        if not os.path.exists(full):
            print("跳过不存在的 %s" % entry)
            continue
        if os.path.isfile(full):
            found.append((full, entry))
            continue
        for r, dirs, files in os.walk(full):
            dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
            for fn in sorted(files):
                p = os.path.join(r, fn)
                rel = os.path.relpath(p, ROOT).replace(os.sep, "/")
                if fn in EXCLUDE_DIRS or fn.endswith(EXCLUDE_SUFFIX):
                    continue
                if EXCLUDE_STATE.search(rel) or EXCLUDE_PROFILE.search(rel):
                    print("跳过疑似凭据/个人数据: %s" % rel)
                    continue
                found.append((p, rel))
    return sorted(found, key=lambda x: x[1])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", required=True)
    ap.add_argument("--out", default="dist")
    args = ap.parse_args()

    files = collect()
    if not files:
        sys.exit("没有可打包的文件，检查 INCLUDE")

    os.makedirs(os.path.join(ROOT, args.out), exist_ok=True)
    zip_path = os.path.join(ROOT, args.out, "%s-v%s.zip" % (PKG_NAME, args.version))

    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for p, rel in files:
            z.write(p, "%s/%s" % (PKG_NAME, rel))

    print("打包 %d 个文件 -> %s (%.1f KB)"
          % (len(files), zip_path, os.path.getsize(zip_path) / 1024))
    for _p, rel in files:
        print("   ", rel)


if __name__ == "__main__":
    main()
