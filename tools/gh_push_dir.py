#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把本地源码目录**原子地**推到 GitHub：一次源码同步 = 一个 commit。

为什么要重写
------------
旧实现是「每个文件一次 `PUT /repos/<r>/contents/<path>`」。
18 个文件就是 18 个 commit —— CI 触发 N 次、Pages 建 N 次中间态、
免费 runner 互相挤队列，一次发布要等十几分钟。
现在改走 Git Data API 的 blob / tree / commit / ref 四步：

    GET   /repos/<r>/git/ref/heads/<branch>         取 base commit（并发基线）
    GET   /repos/<r>/git/commits/<base>             取 base tree
    GET   /repos/<r>/git/trees/<tree>?recursive=1   远端清单
    POST  /repos/<r>/git/blobs                      只为变化文件建 blob
    POST  /repos/<r>/git/trees                      base_tree + 变化项（一次）
    POST  /repos/<r>/git/commits                    一个 commit
    PATCH /repos/<r>/git/refs/heads/<branch>        force=false（CAS）
    GET   /repos/<r>/git/ref/heads/<branch>         复核

关键约束
--------
1. **绝不使用 force。** ref 更新一律走 CAS（force=false）：
   base 不是当前 HEAD 就 409/422，此时停下让调用方重跑，
   不允许覆盖别人的提交。
2. **只推送白名单内的文件**，白名单与打包共用
   `tools/release_common.py` 的那份定义 —— 打包什么就推什么。
3. **删除只发生在白名单范围内。** 仓库里白名单之外的文件（遗留脚本等）不碰。
4. **没有变化就不建 commit。** 空 commit 会白白触发一次 CI。
5. **token 只在本进程内使用**（urllib 请求头），不进 argv、不落盘、不打印。

用法
----
    set GH_TOKEN=github_pat_xxx
    python tools/gh_push_dir.py --src . --repo OWNER/REPO --branch main \
        --message "release: prepare v1.4.2" --result-json /tmp/push.json

退出码
------
    0  成功（含「无变化」）
    1  推送失败（权限 / 冲突 / 网络）
    2  用法或环境错误（缺 token / 源目录不存在）
"""
import argparse
import base64
import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import release_common as RC  # noqa: E402

API = "https://api.github.com"

# 已经被服务端明确拒绝、重试没有意义的状态
_NO_RETRY = (401, 403, 404)
# 并发基线失效：ref 更新不是 fast-forward（409 冲突，422 非 fast-forward）
_CONFLICT = (409, 422)
# 值得退避重试的：限流与服务端临时故障
_RETRYABLE = (429, 500, 502, 503, 504)


class PushError(Exception):
    pass


class AuthError(PushError):
    pass


class ConflictError(PushError):
    pass


class TransientError(PushError):
    pass


class Api:
    """GitHub REST 的最小封装 —— 只用标准库 urllib。

    token 只作为请求头出现，不进 argv、不写文件、不进异常文本。
    """

    def __init__(self, token, owner, repo, retries=3, timeout=60,
                 sleep=time.sleep):
        self.token = token
        self.owner = owner
        self.repo = repo
        self.retries = max(1, int(retries))
        self.timeout = timeout
        self.sleep = sleep
        self.calls = []          # [(method, path)] —— 供测试断言调用形状

    def req(self, method, path, payload=None, expect=(200,), allow=()):
        url = path if path.startswith("http") else API + path
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        last = None
        for attempt in range(self.retries):
            self.calls.append((method, path))
            req = urllib.request.Request(url, data=data, method=method)
            req.add_header("Authorization", "Bearer " + self.token)
            req.add_header("Accept", "application/vnd.github+json")
            req.add_header("X-GitHub-Api-Version", "2022-11-28")
            if data:
                req.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as r:
                    body = r.read().decode("utf-8", "replace")
                    code = r.getcode()
                    d = json.loads(body) if body.strip() else {}
                    if code in allow or code in expect:
                        return code, d
                    last = "HTTP %d" % code
            except urllib.error.HTTPError as e:
                code = e.code
                # 顺序有意义：409/422 是「并发基线失效」，比「权限不足」更具体
                if code in _CONFLICT and code not in allow:
                    raise ConflictError("HTTP %d（并发基线失效或非 fast-forward）"
                                        % code)
                if code in _NO_RETRY and code not in allow:
                    raise AuthError("HTTP %d（权限不足或路径不存在）" % code)
                if code in _RETRYABLE:
                    last = "HTTP %d" % code
                else:
                    raise TransientError("HTTP %d" % code)
            except (urllib.error.URLError, socket.timeout, OSError) as e:
                last = "%s: %s" % (type(e).__name__, str(e)[:60])
            if attempt < self.retries - 1:
                self.sleep(2 * (attempt + 1))
        raise TransientError(last or "未知网络错误")


def remote_tree(api, tree_sha):
    """远端 tree -> {path: (blob_sha, mode)}。只收 blob，目录不参与比对。"""
    _code, d = api.req("GET", "/repos/%s/%s/git/trees/%s?recursive=1"
                       % (api.owner, api.repo, tree_sha))
    if d.get("truncated"):
        raise TransientError("远端 tree 被截断（仓库过大），无法完成完整比对")
    out = {}
    for it in d.get("tree", []):
        if it.get("type") != "blob":
            continue
        out[it["path"]] = (it.get("sha"), it.get("mode") or "100644")
    return out


def build_tree_entries(api, src, local, remote, diff):
    """为变化文件建 blob，返回 tree API 需要的条目列表。

    只给 added / modified 建 blob —— 内容没变的文件复用远端已有的 blob，
    不必重复上传（大仓库里这一步最省时间）。
    """
    entries = []
    for rel in diff["added"] + diff["modified"]:
        with open(os.path.join(src, rel), "rb") as f:
            raw = f.read()
        want = local[rel]
        _code, d = api.req("POST", "/repos/%s/%s/git/blobs" % (api.owner, api.repo),
                           {"content": base64.b64encode(raw).decode("ascii"),
                            "encoding": "base64"}, expect=(201,))
        got = d.get("sha")
        if got and got != want:
            # 服务端算出来的和本地不一致：要么内容在读取过程中变了，
            # 要么编码有问题。静默接受会让「远端」和「本地」悄悄分叉。
            raise TransientError("%s 的 blob sha 不一致（本地 %s 远端 %s）"
                                 % (rel, want[:12], got[:12]))
        # 已存在的文件沿用远端 mode，避免把可执行文件位改掉
        mode = (remote[rel][1] if rel in remote else None) or "100644"
        entries.append({"path": rel, "mode": mode, "type": "blob",
                        "sha": got or want})
    for rel in diff["deleted"]:
        # sha 为 null 即「删除该路径」
        entries.append({"path": rel, "mode": "100644", "type": "blob",
                        "sha": None})
    return entries


def push(api, src, branch="main", message=None, include=None, dry_run=False):
    """原子推送。返回一份可序列化的报告（含本次产生的 commit sha）。"""
    if not os.path.isdir(src):
        raise PushError("源目录不存在: %s" % src)

    _code, ref = api.req("GET", "/repos/%s/%s/git/ref/heads/%s"
                         % (api.owner, api.repo, branch))
    base_sha = ref["object"]["sha"]
    _code, commit = api.req("GET", "/repos/%s/%s/git/commits/%s"
                            % (api.owner, api.repo, base_sha))
    base_tree = commit["tree"]["sha"]

    remote = remote_tree(api, base_tree)
    local = RC.local_blobs(src, include)
    diff = RC.compare(local, remote, include)

    report = {
        "branch": branch,
        "base": base_sha,
        "sha": base_sha,               # 无变化时以 base 作为源码 commit
        "message": message,
        "added": diff["added"],
        "modified": diff["modified"],
        "deleted": diff["deleted"],
        "unchanged": diff["unchanged"],
        "changed": bool(diff["added"] or diff["modified"] or diff["deleted"]),
        "dry_run": bool(dry_run),
    }

    if not report["changed"]:
        return report
    if dry_run:
        report["sha"] = None
        return report

    entries = build_tree_entries(api, src, local, remote, diff)
    _code, tree = api.req("POST", "/repos/%s/%s/git/trees" % (api.owner, api.repo),
                          {"base_tree": base_tree, "tree": entries},
                          expect=(201,))
    msg = message or ("chore: sync source (%d files)" % len(local))
    _code, c = api.req("POST", "/repos/%s/%s/git/commits" % (api.owner, api.repo),
                       {"message": msg, "tree": tree["sha"],
                        "parents": [base_sha]}, expect=(201,))
    new_sha = c["sha"]

    # ★ CAS，不是 force。base 已经不是当前 HEAD 时 GitHub 会拒绝，
    #   此时必须停下——覆盖别人的提交比发布晚几分钟严重得多。
    try:
        api.req("PATCH", "/repos/%s/%s/git/refs/heads/%s"
                % (api.owner, api.repo, branch),
                {"sha": new_sha, "force": False}, expect=(200,),
                allow=_CONFLICT)
    except ConflictError:
        try:
            _c2, cur = api.req("GET", "/repos/%s/%s/git/ref/heads/%s"
                               % (api.owner, api.repo, branch))
            now = cur["object"]["sha"]
        except PushError:
            now = "未知"
        raise ConflictError(
            "%s 在推送过程中被更新过，本次推送已放弃（未使用 force）。\n"
            "  推送基线 base : %s\n"
            "  本次新 commit : %s\n"
            "  当前 %s      : %s\n"
            "请重新拉取后重试。" % (branch, base_sha, new_sha, branch, now))

    # 复核：ref 更新成功不等于落到了我们期望的那个 commit 上
    _code, cur = api.req("GET", "/repos/%s/%s/git/ref/heads/%s"
                         % (api.owner, api.repo, branch))
    if cur["object"]["sha"] != new_sha:
        raise TransientError("ref 更新后复核失败：期望 %s，实际 %s"
                             % (new_sha, cur["object"]["sha"]))

    report["sha"] = new_sha
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="原子地把本地源码目录推到 GitHub（一次同步 = 一个 commit）")
    ap.add_argument("--src", required=True, help="本地源目录（仓库根）")
    ap.add_argument("--repo", required=True, help="owner/repo")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--message", default=None, help="commit message")
    ap.add_argument("--include", default=None,
                    help="白名单，逗号分隔；默认与打包白名单一致")
    ap.add_argument("--result-json", default=None,
                    help="把推送报告写成 JSON，供调用方（release.py）读取")
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true", help="只算差异，不改动远端")
    args = ap.parse_args(argv)

    src = os.path.abspath(args.src)
    if not os.path.isdir(src):
        sys.stderr.write("源目录不存在: %s\n" % src)
        return 2
    token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.stderr.write("没有 GH_TOKEN / GITHUB_TOKEN 环境变量\n")
        return 2
    try:
        owner, repo = args.repo.split("/", 1)
    except ValueError:
        sys.stderr.write("--repo 需要写成 owner/repo\n")
        return 2

    # 不传 --include 时回落到 `release_common.INCLUDE`（打包白名单的唯一定义）。
    # ⚠️ 不能让 args.include.split() 裸奔：release.py 调用本脚本时**只**传
    #    --src/--repo/--branch/--message/--result-json，不带 --include。
    include = None
    if args.include:
        include = [x.strip() for x in args.include.split(",") if x.strip()] or None
    api = Api(token, owner, repo, retries=args.retries)
    try:
        report = push(api, src, branch=args.branch, message=args.message,
                      include=include, dry_run=args.dry_run)
    except PushError as e:
        sys.stderr.write("推送失败: %s\n" % e)
        return 1

    if not report["changed"]:
        print("没有变化，未创建 commit（base %s）" % report["base"][:12])
    else:
        print("变更: +%d 新增 / %d 修改 / -%d 删除 / %d 未变"
              % (len(report["added"]), len(report["modified"]),
                 len(report["deleted"]), report["unchanged"]))
        for rel in report["added"]:
            print("   + %s" % rel)
        for rel in report["modified"]:
            print("   ~ %s" % rel)
        for rel in report["deleted"]:
            print("   - %s" % rel)
        if args.dry_run:
            print("干跑：未创建 commit")
        else:
            print("一个 commit: %s -> %s" % (report["base"][:12],
                                             report["sha"][:12]))

    if args.result_json:
        with open(args.result_json, "w", encoding="utf-8") as f:
            json.dump(report, f, ensure_ascii=False, indent=2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
