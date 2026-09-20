#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI 用的打包脚本 —— 把仓库内容打成 dist/xjtu-siyuanxuetang-grab-v<ver>.zip

和本地 release.py 的 build_zip 保持同一套排除规则，但这里是**仓库视角**：
源目录就是仓库根目录，不像本地那样指到工作区根。

为什么要单独一份：release.py 里有本机路径和直传逻辑，
在 GitHub Runner 上跑不通。CI 里只做「打包」这一件事，
「发 Release」交给 softprops/action-gh-release 这个成熟 action。

注意：INCLUDE 与 release.py 必须是同一份定义 —— 现在两者都引用
tools/release_common.py，不再各存一份（踩过：这里漏了 release.py，
而本地那份有，于是本地包和 CI 包内容不一致）。
"""
import argparse
import os
import sys
import zipfile

# 仓库根目录（本文件位于 .github/scripts/ 下，往上两级）
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import release_common as RC  # noqa: E402

PKG_NAME = "xjtu-siyuanxuetang-grab"

# 白名单与排除规则共用，避免 CI 包 / 本地包 / 远端仓库三者漂移


def collect():
    return RC.collect(ROOT)


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
