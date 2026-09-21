# -*- coding: utf-8 -*-
"""发布链路的离线测试 —— 不访问真实 GitHub、不需要 token。

覆盖两件事：
  ① tools/gh_push_dir.py 的 Git Data API 流程真的是「一次同步 = 一个 commit」，
     并且 ref 更新是 CAS 而不是 force；
  ② release.py 的 tag 绑定到确定的源码 commit，不再靠「推送完再读一次 main」。

GitHub 行为全部用假对象模拟（FakeApi / 假 subprocess），
不在测试里发起任何网络请求。

跑法：
    python tests/test_push.py
"""
import argparse
import base64
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import zipfile
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from tools import release_common as RC   # noqa: E402
from tools import gh_push_dir as P       # noqa: E402
from tools import privacy_scan as PS     # noqa: E402
import release as R                      # noqa: E402


# ---------------------------------------------------------------- 假 GitHub

class FakeApi:
    """假的 Git Data API。

    行为与真实接口对齐：blob sha 由内容算出、ref 更新走 CAS（冲突抛
    ConflictError）、transient / auth 失败按脚本注入。路径里的 owner/repo
    与 P.push() 拼出来的一致（O/R）。
    """

    def __init__(self, remote=None, main=None, base_tree="T" * 40):
        self.remote = dict(remote or {})     # path -> (sha, mode)
        self.main = main or "A" * 40
        self.base_tree = base_tree
        self.owner, self.repo = "O", "R"
        self.calls = []
        self.blobs = 0
        self.commits = 0
        self.commit_payloads = []
        self.ref_updates = 0
        self.ref_payloads = []
        self.tree_payloads = []
        self.fail_first = {}                 # (method, path) -> 还需失败几次
        self.auth_fail = set()               # (method, path) 命中即 403
        self.conflict_ref = False

    def _p(self, tail):
        return "/repos/O/R/" + tail

    def req(self, method, path, payload=None, expect=(200,), allow=()):
        key = (method, path.split("?")[0])
        self.calls.append(key)
        if key in self.auth_fail:
            raise P.AuthError("HTTP 403（权限不足）")
        if self.fail_first.get(key):
            self.fail_first[key] -= 1
            raise P.TransientError("HTTP 502（模拟服务端临时故障）")

        if key == ("GET", self._p("git/ref/heads/main")):
            return 200, {"object": {"sha": self.main}}
        if method == "GET" and path.startswith(self._p("git/commits/")):
            return 200, {"sha": path.rsplit("/", 1)[-1],
                         "tree": {"sha": self.base_tree}}
        if method == "GET" and path.startswith(self._p("git/trees/")):
            return 200, {"truncated": False,
                         "tree": [{"path": p, "type": "blob",
                                   "sha": s, "mode": m}
                                  for p, (s, m) in sorted(self.remote.items())]}
        if key == ("POST", self._p("git/blobs")):
            self.blobs += 1
            raw = base64.b64decode(payload["content"])
            return 201, {"sha": RC.git_blob_sha(raw)}
        if key == ("POST", self._p("git/trees")):
            self.tree_payloads.append(payload)
            return 201, {"sha": "NEWTREE%d" % len(self.tree_payloads)}
        if key == ("POST", self._p("git/commits")):
            self.commits += 1
            self.commit_payloads.append(payload)
            return 201, {"sha": "COMMIT%d" % self.commits}
        if key == ("PATCH", self._p("git/refs/heads/main")):
            self.ref_updates += 1
            self.ref_payloads.append(payload)
            if self.conflict_ref:
                raise P.ConflictError("HTTP 409（not a fast forward）")
            self.main = payload["sha"]
            return 200, {"object": {"sha": self.main}}
        raise AssertionError("未预期的请求: %s %s" % (method, path))


class FakeProc:
    def __init__(self, rc, out="", err=""):
        self.returncode = rc
        self.stdout = out
        self.stderr = err


def _fake_pusher(report=None, returncode=0):
    """替掉 release.py 里的 subprocess.run —— 模拟推送器写出结果 JSON。"""

    def run(cmd, **_kw):
        if "--result-json" in cmd:
            with open(cmd[cmd.index("--result-json") + 1], "w",
                      encoding="utf-8") as f:
                json.dump(report if report is not None
                          else {"sha": "B" * 40, "unchanged": 3}, f)
        return FakeProc(returncode, "pushed\n")

    return run


# ---------------------------------------------------------------- 基础用例

class PushCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pushtest_")
        self.src = os.path.join(self.tmp, "src")
        os.makedirs(self.src)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write(self, rel, data=b"x"):
        p = os.path.join(self.src, rel)
        d = os.path.dirname(p)
        if d:
            os.makedirs(d, exist_ok=True)
        with open(p, "wb") as f:
            f.write(data)
        return p

    def blob(self, data):
        return RC.git_blob_sha(data)

    def remote_of(self, mapping):
        """{rel: bytes} -> 远端 tree 的 {path: (sha, mode)}"""
        return {rel: (self.blob(data), "100644") for rel, data in mapping.items()}


class TestReleaseCommon(PushCase):

    def test_git_blob_sha_matches_git_object_id(self):
        """空文件的 blob sha 是 git 的已知常量 —— 算法不能自己发明。"""
        self.assertEqual(RC.git_blob_sha(b""),
                         "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")

    def test_blob_sha_varies_with_length_prefix(self):
        """长度前缀是算法的一部分：b"blob 3\\0abc" 不等于 sha1(b"abc")。"""
        import hashlib
        self.assertNotEqual(RC.git_blob_sha(b"abc"),
                            hashlib.sha1(b"abc").hexdigest())

    def test_state_and_profile_files_are_filtered(self):
        """登录态 / 活动清单 / profile / 缓存绝不进白名单。"""
        self.write("scripts/a.py", b"ok")
        for bad in ("scripts/state_1.json", "scripts/activities_1.json",
                    "scripts/storage_state.json",
                    "scripts/profile_x/cookies.json",
                    "scripts/__pycache__/a.pyc", "scripts/a.pyc"):
            self.write(bad, b"secret")
        keys = set(RC.local_blobs(self.src))
        self.assertEqual(keys, {"scripts/a.py"}, keys)

    def test_include_is_shared_by_release_and_pack(self):
        """三处白名单必须是同一份定义 —— 各自一份会漂移。"""
        self.assertIs(R.INCLUDE, RC.INCLUDE)
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "_pack_under_test",
            os.path.join(ROOT, ".github", "scripts", "pack.py"))
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertIs(mod.RC.INCLUDE, RC.INCLUDE)

    def test_tools_and_docs_are_in_release_package(self):
        """README 里有 docs/ 的相对链接，包里就必须有 docs/。"""
        for entry in ("tools", "docs"):
            self.assertIn(entry, RC.INCLUDE)


class TestPrivacyScan(PushCase):
    """发布树隐私扫描 —— 「什么不许进公开包」的唯一定义。

    回归背景：v1.5.0 的发布工具文档字符串里写着**转义形式**的本机盘符路径，
    而当时的检查只匹配单分隔符、CI 又只扫 scripts/，于是这条本机路径随包
    发布了。下面把这些失败模式分别钉住：

      1) 单分隔符形态命中
      2) 源码字面量（双反斜杠）形态命中 —— 当年漏掉的就是这个
      3) URI scheme / 格式化占位不误报
      4) 白名单新增公开文件后自动进入扫描（扫描范围 = 发布范围）
      5) 发布校验必须因此失败
      6) 豁免是逐行的，不是关掉规则
      7) 当前发布树零阻塞
    """

    def _zip_with(self, rel, data):
        """造一个解包后长成 `<ZIP_STEM>/<rel>` 的最小包。"""
        zpath = os.path.join(self.tmp, "planted.zip")
        with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("%s/%s" % (R.ZIP_STEM, rel), data)
        return zpath

    # ---------------------------------------------------------- 命中

    def test_single_separator_drive_path_blocks(self):
        text = 'ROOT = "D:\\repo\\file.py"'                                # privacy-scan: fixture
        self.assertTrue(PS.blocking(PS.scan_text("x/sample.py", text)),
                        "单分隔符的盘符路径必须命中")

    def test_escaped_literal_drive_path_blocks(self):
        """★ 源码字面量在盘上的真实字节是双反斜杠 —— 当年漏掉的就是这一形态。"""
        text = 'ROOT = "D:\\\\repo\\\\file.py"'                            # privacy-scan: fixture
        self.assertTrue(PS.blocking(PS.scan_text("x/sample.py", text)),
                        "双反斜杠形态必须命中，否则文档字符串里的路径会从 gate 下溜走")

    def test_drive_path_rule_is_generic_not_name_anchored(self):
        """★ v1.5.0 漏检的真正成因：旧检查把盘符路径**锚定在具体目录名**上，
        且分隔符只允许一个 —— 于是源码字面量（双反斜杠）形态匹配不到。
        这里的规则必须是「任意盘符路径」而不是「本项目目录名的路径」。"""
        for text in ('ROOT = "E:\\\\some-other-repo\\\\note.md"',                # privacy-scan: fixture
                     'ROOT = "D:\\\\xjtu-siyuanxuetang-grab\\\\release.py"'):    # privacy-scan: fixture
            self.assertTrue(PS.blocking(PS.scan_text("x/sample.py", text)), text)
        # 分隔符必须是「一或多个」：这一条直接钉住正则形态，
        # 因为仅靠命中/不命中区分不出 `[\\/]` 与 `[\\/]+`（尾部类是贪婪的）。
        self.assertIn("[\\\\/]+", PS._WIN_ABS_PATH.pattern,
                      "盘符后的分隔符必须允许重复，否则转义形态会漏")

    def test_windows_user_and_posix_home_block(self):
        for text in ("C:\\Users\\alice\\.lms-grab\\state_common.json",     # privacy-scan: fixture
                     "/Users/carol/proj/x.py",                             # privacy-scan: fixture
                     "/home/dave/.lms-grab/state_common.json"):            # privacy-scan: fixture
            self.assertTrue(PS.blocking(PS.scan_text("x/sample.py", text)), text)

    def test_credential_shapes_block(self):
        # 凭据夹具一律**运行时构造** —— 绝不把真实凭据（或其前缀）写进源码。
        # v1.5.1 的教训：从真实 token 截前缀当夹具，等于亲手把泄漏写进发布包。
        fake_pat = "github_pat_" + "A" * 40
        fake_oauth = "ghp_" + "AbCdEfGh" * 5
        fake_preview = "?previewToken=" + "9f8e" * 6
        for text in (fake_pat,
                     fake_oauth,
                     fake_preview,
                     "https://rms-v5.xjtu.edu.cn/live/a.m3u8"):   # privacy-scan: fixture
            self.assertTrue(PS.blocking(PS.scan_text("x/sample.py", text)), text)

    def test_credential_rules_reject_line_exemption(self):
        """凭据类规则拒绝任何行级豁免 —— 带夹具标记的凭据字形照样拦截。"""
        line = ("github_pat_" + "A" * 40) + "  # privacy-scan: fixture"
        self.assertTrue(PS.blocking(PS.scan_text("tests/test_push.py", line)),
                        "凭据规则必须拒绝行级豁免（豁免不是安全边界）")
        self.assertIsNone(PS._allow_reason("secret-token", "tests/test_push.py", line))
        self.assertIsNone(PS._allow_reason("preview-token", "tests/test_push.py", line))

    def test_only_path_rules_are_exemptable(self):
        """规则层面钉死：凭据类 exemptable=False，路径/主机类为 True。"""
        for rule in PS.RULES:
            if rule.name in ("secret-token", "preview-token"):
                self.assertFalse(rule.exemptable, rule.name)
            else:
                self.assertTrue(rule.exemptable, rule.name)

    def test_no_literal_credential_in_test_source(self):
        """本测试源码自身不得含凭据字面量 —— 夹具必须运行时构造（v1.5.2 教训）。"""
        with open(os.path.join(HERE, "test_push.py"), encoding="utf-8") as f:
            src = f.read()
        hits = [f for f in PS.scan_text("tests/test_push.py", src)
                if f.rule in ("secret-token", "preview-token")]
        self.assertEqual(hits, [],
                         "测试源码里不得出现凭据字面量，夹具一律运行时构造")

    # ---------------------------------------------------------- 不误报

    def test_uri_schemes_are_not_drive_paths(self):
        for text in ("https://example.com/path/to/thing",
                     "http://127.0.0.1:4774",
                     'return "%s://%s%s" % (p.scheme, p.netloc, p.path)',
                     "s3://bucket/key"):
            self.assertFalse(PS.blocking(PS.scan_text("x/sample.py", text)),
                             "URI / 格式化占位不该被当成盘符路径：%s" % text)

    def test_placeholder_and_generic_os_paths_are_noted_not_blocked(self):
        """占位写法与通用系统目录要**记录**，但不属于泄漏。"""
        for text in ("C:\\Users\\<你>\\.lms-grab",                          # privacy-scan: fixture
                     "D:/xxx/state.json",
                     'r"C:\\Program Files (x86)"'):                         # privacy-scan: fixture
            findings = PS.scan_text("x/sample.py", text)
            self.assertFalse(PS.blocking(findings), text)
            self.assertTrue(findings, "不是泄漏也要留下记录，不能静默吞掉：%s" % text)

    # ---------------------------------------------------------- 扫描范围

    def test_new_whitelisted_file_is_scanned_automatically(self):
        """白名单里新增一个公开 .py，扫描必须自动覆盖它 —— 不用改任何清单。"""
        self.write("scripts/brand_new.py",
                   br'ROOT = "D:\repo\brand.py"')                # privacy-scan: fixture
        findings, _n = PS.scan_tree(self.src)
        bad = PS.blocking(findings)
        self.assertEqual([f.path for f in bad], ["scripts/brand_new.py"],
                         "新文件里的本机路径必须被发现")
        self.assertTrue(RC.collect(self.src), "新文件确实落在发布白名单内")

    def test_scan_scope_equals_release_whitelist(self):
        """扫描范围 == 发布范围：白名单外的文件不看，白名单内的一个不漏。"""
        self.write("docs/note.md", br"see D:\repo\note.md")             # privacy-scan: fixture
        self.write("junk/outside.py", br"see D:\repo\outside.py")       # privacy-scan: fixture
        findings, _n = PS.scan_tree(self.src)
        paths = {f.path for f in PS.blocking(findings)}
        self.assertIn("docs/note.md", paths,
                      "白名单内的 docs/ 必须被扫到")
        self.assertNotIn("junk/outside.py", paths,
                         "白名单外的东西不在扫描范围（它也不会进包）")

    def test_scan_tree_uses_the_shared_whitelist(self):
        """扫描器内部必须复用 RC.collect —— 不许再立一份待扫清单。"""
        import inspect
        self.assertIn("RC.collect", inspect.getsource(PS.scan_tree))

    # ---------------------------------------------------------- gate 生效

    def test_release_verification_fails_on_planted_machine_path(self):
        """★ 发布校验是真闸门：包里带本机路径就必须停下来。"""
        zpath = self._zip_with("release.py",
                               br'ROOT = "D:\repo\file.py"' + b"\n")    # privacy-scan: fixture
        with self.assertRaises(SystemExit) as cm:
            R.verify_zip(zpath)
        self.assertIn("泄漏", str(cm.exception),
                      "失败原因要写明是隐私泄漏，别让人以为是缺文件")

    def test_allowlist_is_per_line_not_a_rule_switch(self):
        """豁免精确到行：同一文件里没有标记的行照样被拦下。"""
        literal = 'ROOT = "D:\\repo\\file.py"'                              # privacy-scan: fixture
        self.assertFalse(
            PS.blocking(PS.scan_text("tests/test_push.py",
                                     literal + "  # privacy-scan: fixture")),
            "带豁免标记的合成夹具不该阻塞")
        self.assertTrue(
            PS.blocking(PS.scan_text("tests/test_push.py", literal)),
            "同一文件里没标记的行必须仍被拦下 —— 豁免不等于关掉规则")
        for a in PS.ALLOWLIST:
            self.assertTrue(a.reason.strip(), "每条豁免都要写明理由：%s" % a.path)

    def test_published_tree_has_no_blocking_finding(self):
        """当前发布树：零阻塞。新增任何真实泄漏都会被这条拦下。"""
        findings, n = PS.scan_tree(ROOT)
        bad = PS.blocking(findings)
        self.assertGreater(n, 10, "得真的扫到了相当数量的文件")
        self.assertEqual([], bad, "发布树里不该有阻塞项：\n%s"
                         % PS.format_findings(bad))

    def test_ci_and_release_workflows_run_the_shared_scanner(self):
        """CI / Release 的隐私自检必须跑共享扫描器，不能退化回手写 grep。"""
        for name in ("ci.yml", "release.yml"):
            path = os.path.join(ROOT, ".github", "workflows", name)
            with open(path, encoding="utf-8") as f:
                body = f.read()
            self.assertIn("tools/privacy_scan.py", body,
                          "%s 的隐私自检要复用共享扫描器" % name)
            self.assertNotRegex(
                body, r"grep -rn --include='\*\.py'",
                "%s 不该再手写只扫 scripts/ 的 grep（范围会漏）" % name)


class TestPusherIncludeDefault(PushCase):
    """`release.py` 调用推送器时**不传** `--include`。

    回归用例：`gh_push_dir.main()` 曾在 include 为 None 时直接执行
    `args.include.split(",")` → AttributeError，release.py 走到「确定源码
    commit」这一步就崩。单测全 mock（`run_push` 假 subprocess 里总是显式
    带 `--include`），所以只有真实发布才会暴露 —— 这条把 CLI 默认值钉住。
    """

    def test_cli_without_include_falls_back_to_shared_whitelist(self):
        self.write("README.md", b"readme")
        self.write("scripts/a.py", b"code")
        seen = {}

        def fake_push(api, src, branch="main", message=None,
                      include=None, dry_run=False):
            seen["include"] = include
            return {"changed": False, "base": "A" * 40, "added": [],
                    "modified": [], "deleted": [], "unchanged": 0}

        old_api, old_push = P.Api, P.push
        P.Api = lambda *a, **k: object()
        P.push = fake_push
        try:
            with mock.patch.dict(os.environ, {"GH_TOKEN": "x"}):
                rc = P.main(["--src", self.src, "--repo", "O/R", "--dry-run"])
        finally:
            P.Api, P.push = old_api, old_push

        self.assertEqual(rc, 0, "不传 --include 不应崩溃")
        self.assertIsNone(seen.get("include"),
                          "不传 --include 时必须回落共用白名单（None → INCLUDE）")
        # None 的回落语义真的生效：拿到的是白名单里的文件，而不是空集
        self.assertEqual(sorted(rel for _, rel in RC.collect(self.src, seen["include"])),
                         ["README.md", "scripts/a.py"])


class TestAtomicPush(PushCase):

    def _setup(self):
        # 本地：4 个已改 + 1 个新增；远端还有一个本地没有的旧文件
        self.write("scripts/a.py", b"new-a")
        self.write("scripts/b.py", b"new-b")
        self.write("scripts/c.py", b"new-c")
        self.write("README.md", b"new-readme")
        self.write("docs/d.md", b"new-doc")
        self.write("LICENSE", b"same-license")
        remote = self.remote_of({
            "scripts/a.py": b"old-a",
            "scripts/b.py": b"old-b",
            "scripts/c.py": b"old-c",
            "README.md": b"old-readme",
            "LICENSE": b"same-license",
            "scripts/gone.py": b"old-gone",
        })
        return FakeApi(remote)

    def test_one_commit_for_all_changes(self):
        """★ 核心：N 个文件变化只产生 1 个 commit、1 次 ref 更新。"""
        api = self._setup()
        rep = P.push(api, self.src, message="release: prepare v1.4.2")
        self.assertEqual(api.commits, 1, "commit 数必须是 1")
        self.assertEqual(api.ref_updates, 1, "ref 更新次数必须是 1")
        self.assertEqual(rep["sha"], "COMMIT1")
        self.assertEqual(sorted(rep["modified"]),
                         ["README.md", "scripts/a.py", "scripts/b.py",
                          "scripts/c.py"])
        self.assertEqual(rep["added"], ["docs/d.md"])
        self.assertEqual(rep["deleted"], ["scripts/gone.py"])
        self.assertEqual(rep["unchanged"], 1)          # LICENSE 没变

    def test_unchanged_files_are_not_reuploaded(self):
        """内容没变的文件复用远端已有 blob，不重复上传。"""
        api = self._setup()
        rep = P.push(api, self.src)
        self.assertEqual(api.blobs, 5, "5 个变化文件，未变的 LICENSE 不该上传")

    def test_deletion_uses_null_sha(self):
        api = self._setup()
        P.push(api, self.src)
        entries = api.tree_payloads[0]["tree"]
        gone = [e for e in entries if e["path"] == "scripts/gone.py"]
        self.assertEqual(len(gone), 1)
        self.assertIsNone(gone[0]["sha"], "删除靠 sha=null 表达")

    def test_tree_built_on_base_tree_with_single_message(self):
        api = self._setup()
        base = api.main                      # push 之后 api.main 会被更新
        P.push(api, self.src, message="release: prepare v1.4.2")
        self.assertEqual(api.tree_payloads[0]["base_tree"], api.base_tree)
        self.assertEqual(api.commit_payloads[0]["message"],
                         "release: prepare v1.4.2")
        self.assertEqual(api.commit_payloads[0]["parents"], [base])

    def test_single_tree_request_carries_all_changes(self):
        """一次 tree POST 装下全部变化 —— 不是每个文件一次请求。"""
        api = self._setup()
        P.push(api, self.src)
        self.assertEqual(len(api.tree_payloads), 1)
        self.assertEqual(len(api.tree_payloads[0]["tree"]), 6)

    def test_no_empty_commit_when_nothing_changed(self):
        """没有变化就一个 commit 都不建，避免空转触发 CI。"""
        self.write("scripts/a.py", b"same")
        api = FakeApi(self.remote_of({"scripts/a.py": b"same"}))
        rep = P.push(api, self.src)
        self.assertFalse(rep["changed"])
        self.assertEqual(rep["sha"], api.main, "无变化时应返回 base 作为源码 commit")
        self.assertEqual(api.commits, 0)
        self.assertEqual(api.ref_updates, 0)

    def test_dry_run_does_not_mutate(self):
        self.write("scripts/a.py", b"new")
        api = FakeApi(self.remote_of({"scripts/a.py": b"old"}))
        rep = P.push(api, self.src, dry_run=True)
        self.assertTrue(rep["changed"])
        self.assertIsNone(rep["sha"])
        self.assertEqual(api.commits, 0)
        self.assertEqual(api.ref_updates, 0)

    def test_mode_is_preserved_for_existing_files(self):
        """已存在文件的 mode 沿用远端值，别把可执行位改掉。"""
        self.write("scripts/run.sh", b"new")
        api = FakeApi({"scripts/run.sh": (self.blob(b"old"), "100755")})
        P.push(api, self.src)
        e = api.tree_payloads[0]["tree"][0]
        self.assertEqual(e["mode"], "100755")


class TestRefSafety(PushCase):

    def _setup(self):
        self.write("scripts/a.py", b"new")
        return FakeApi(self.remote_of({"scripts/a.py": b"old"}))

    def test_never_force_update(self):
        """★ 绝不使用 force —— 每个 ref 更新都必须带 force=False。"""
        api = self._setup()
        P.push(api, self.src)
        self.assertEqual(api.ref_updates, 1)
        for pl in api.ref_payloads:
            self.assertIn("force", pl)
            self.assertFalse(pl["force"])

    def test_conflict_stops_and_keeps_main(self):
        """main 被别人更新过 -> 放弃本次推送，不改 ref。"""
        api = self._setup()
        api.conflict_ref = True
        before = api.main
        with self.assertRaises(P.ConflictError) as cm:
            P.push(api, self.src)
        self.assertEqual(api.main, before, "冲突时不能改动 main")
        self.assertIn("未使用 force", str(cm.exception))
        self.assertIn("请重新拉取", str(cm.exception))

    def test_verify_after_ref_update(self):
        """ref 更新后会再读一次复核 —— 更新成功 ≠ 落到了期望的 commit。"""
        api = self._setup()
        rep = P.push(api, self.src)
        self.assertEqual(api.main, rep["sha"])
        self.assertEqual(api.calls[-1],
                         ("GET", "/repos/O/R/git/ref/heads/main"))


class TestDeletionScope(PushCase):

    def test_only_whitelisted_paths_are_deleted(self):
        """白名单外的远端文件（遗留脚本等）不在这次推送里被删。"""
        self.write("scripts/a.py", b"new")
        remote = self.remote_of({"scripts/a.py": b"old", "scripts/old.py": b"x"})
        remote["legacy_root.py"] = (self.blob(b"x"), "100644")   # 白名单外
        api = FakeApi(remote)
        rep = P.push(api, self.src)
        self.assertEqual(rep["deleted"], ["scripts/old.py"])
        paths = [e["path"] for e in api.tree_payloads[0]["tree"]]
        self.assertNotIn("legacy_root.py", paths)


class TestApiRetry(unittest.TestCase):
    """真实 Api 的重试策略 —— 只重试临时故障，4xx 立即失败。"""

    class Resp:
        def __init__(self, code, body=b"{}"):
            self._code, self._body = code, body

        def read(self):
            return self._body

        def getcode(self):
            return self._code

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    def _http(self, code):
        return urllib.error.HTTPError("http://x", code, "err", {},
                                      io.BytesIO(b"{}"))

    def _api(self, script, sleeps=None):
        seq = list(script)
        state = {"n": 0}

        def fake_urlopen(req, timeout=None):
            item = seq[state["n"]] if state["n"] < len(seq) else seq[-1]
            state["n"] += 1
            if isinstance(item, Exception):
                raise item
            return self.Resp(item, b'{"sha":"abc"}')

        old = P.urllib.request.urlopen
        P.urllib.request.urlopen = fake_urlopen
        api = P.Api("t", "O", "R", sleep=lambda _s: None)
        try:
            out = api.req("GET", "/repos/O/R/git/commits/x", expect=(200, 201))
        finally:
            P.urllib.request.urlopen = old
        return out, state["n"]

    def test_502_then_201_succeeds(self):
        _out, n = self._api([self._http(502), 201])
        self.assertEqual(n, 2, "502 应该退避后重试一次")

    def test_403_is_not_retried(self):
        with self.assertRaises(P.AuthError):
            self._api([self._http(403)])
        # 抛异常前只发过一次请求（计数在异常路径里拿不到，断言类型即可）

    def test_403_attempt_count(self):
        """权限错误重试没有意义 —— 只能发一次请求。"""
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise self._http(403)

        old = P.urllib.request.urlopen
        P.urllib.request.urlopen = fake_urlopen
        try:
            api = P.Api("t", "O", "R", retries=3, sleep=lambda _s: None)
            with self.assertRaises(P.AuthError):
                api.req("GET", "/repos/O/R/git/commits/x")
        finally:
            P.urllib.request.urlopen = old
        self.assertEqual(len(calls), 1, "403 不该重试")

    def test_409_is_conflict(self):
        with self.assertRaises(P.ConflictError):
            self._api([self._http(409)])

    def test_422_is_conflict(self):
        """★ 真实 urllib → HTTPError(422) → ConflictError。

        回归背景：ref PATCH 曾错误地传 allow=_CONFLICT，把 409/422 当成
        「正常响应」吞掉，ConflictError 翻译逻辑永远走不到，异常最后退化成
        TransientError 被盲目重试 —— FakeApi 手工抛 ConflictError 掩盖了
        这条真实协议路径。这里必须用 mock urlopen 抛 HTTPError 来测。
        """
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise self._http(422)

        old = P.urllib.request.urlopen
        P.urllib.request.urlopen = fake_urlopen
        try:
            api = P.Api("t", "O", "R", retries=3, sleep=lambda _s: None)
            with self.assertRaises(P.ConflictError):
                api.req("PATCH", "/repos/O/R/git/refs/heads/main")
        finally:
            P.urllib.request.urlopen = old
        self.assertEqual(len(calls), 1, "冲突不该重试")

    def test_network_error_retries_then_gives_up(self):
        calls = []

        def fake_urlopen(req, timeout=None):
            calls.append(1)
            raise urllib.error.URLError("connection reset")

        old = P.urllib.request.urlopen
        P.urllib.request.urlopen = fake_urlopen
        try:
            api = P.Api("t", "O", "R", retries=3, sleep=lambda _s: None)
            with self.assertRaises(P.TransientError):
                api.req("GET", "/repos/O/R/git/commits/x")
        finally:
            P.urllib.request.urlopen = old
        self.assertEqual(len(calls), 3, "网络错误应按 retries 重试")


# ---------------------------------------------------------------- release.py

class _Resp:
    """urlopen 成功响应的最小模拟（带上下文管理器协议）。"""

    def __init__(self, code, body):
        self._code, self._body = code, body

    def read(self):
        return self._body

    def getcode(self):
        return self._code

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _scripted_urlopen(entries):
    """按 (method, URL 后缀, action) 依次回应真实 urllib 请求。

    action:
        dict        -> 200 + JSON body
        (code, dict)-> 指定状态码 + JSON body（blobs/trees/commits 真实
                       服务端回 201，Api.req 的 expect 也按 201 校验，
                       回 200 会被当作异常响应进入重试）
        int         -> HTTPError(code)
        Exception   -> 原样抛出
        callable    -> f(payload_dict) -> dict / (code, dict) / int / Exception
    命中即弹出；重复请求或脚本外的请求直接 AssertionError —— 不静默。
    """
    state = {"entries": list(entries)}

    def fake(req, timeout=None):
        method = req.get_method()
        url = req.full_url
        payload = None
        if req.data:
            try:
                payload = json.loads(req.data.decode("utf-8"))
            except ValueError:
                payload = None
        for i, (m, suffix, action) in enumerate(state["entries"]):
            if m != method or not url.endswith(suffix):
                continue
            del state["entries"][i]
            if callable(action):
                action = action(payload)
            if isinstance(action, Exception):
                raise action
            if isinstance(action, int):
                raise urllib.error.HTTPError(url, action, "err", {},
                                             io.BytesIO(b"{}"))
            code = 200
            if isinstance(action, tuple):
                code, action = action
            return _Resp(code, json.dumps(action).encode("utf-8"))
        raise AssertionError("脚本外的请求: %s %s" % (method, url))

    return fake, state


class TestRealApiRefConflict(PushCase):
    """★ 全流程走真实 Api.req()：urllib → HTTPError(409) → ConflictError。

    FakeApi.conflict_ref 是「手工抛异常」，绕开了 Api.req 的协议翻译层；
    这条用例 mock urlopen，让 PATCH /git/refs/heads/main 真的收到
    HTTPError 409 / 422，验证 push() 的冲突报告语义（fail-closed、
    不用 force、输出 base / 新 commit / 当前远端 SHA）。
    """

    BASE_SHA = "B" * 40
    NEW_SHA = "N" * 40
    OTHER_SHA = "C" * 40

    def _run_conflict(self, patch_code):
        self.write("scripts/a.py", b"new")
        seen_patch = {}

        def patch_payload(pl):
            seen_patch.update(pl or {})
            return patch_code

        entries = [
            ("GET", "/git/ref/heads/main",
             {"object": {"sha": self.BASE_SHA}}),
            ("GET", "/git/commits/" + self.BASE_SHA,
             {"sha": self.BASE_SHA, "tree": {"sha": "TREE1"}}),
            ("GET", "/git/trees/TREE1?recursive=1",
             {"truncated": False, "tree": []}),
            ("POST", "/git/blobs",
             lambda pl: (201, {"sha": RC.git_blob_sha(
                 base64.b64decode(pl["content"]))})),
            ("POST", "/git/trees", (201, {"sha": "T2"})),
            ("POST", "/git/commits", (201, {"sha": self.NEW_SHA})),
            ("PATCH", "/git/refs/heads/main", patch_payload),
            # push() 报告冲突时会回读当前远端 ref
            ("GET", "/git/ref/heads/main",
             {"object": {"sha": self.OTHER_SHA}}),
        ]
        fake, state = _scripted_urlopen(entries)

        old = P.urllib.request.urlopen
        P.urllib.request.urlopen = fake
        try:
            api = P.Api("t", "O", "R", sleep=lambda _s: None)
            cm = self.assertRaises(P.ConflictError)
            with cm:
                P.push(api, self.src, branch="main", message="m")
        finally:
            P.urllib.request.urlopen = old
        self.assertEqual(state["entries"], [],
                         "脚本里的每个请求都必须被真实流程消费掉")
        return str(cm.exception), seen_patch

    def test_409_conflict_reports_shas(self):
        msg, patch = self._run_conflict(409)
        self.assertIn("未使用 force", msg)
        self.assertIn("重新", msg)
        self.assertIn(self.BASE_SHA, msg, "报告要给出推送基线 base SHA")
        self.assertIn(self.NEW_SHA, msg, "报告要给出本次新 commit SHA")
        self.assertIn(self.OTHER_SHA, msg, "报告要给出当前远端 branch SHA")
        self.assertFalse(patch.get("force", True), "ref 更新必须 force=False")

    def test_422_conflict_reports_shas(self):
        """GitHub 对非 fast-forward 也可能回 422 —— 语义必须与 409 一致。"""
        msg, _patch = self._run_conflict(422)
        self.assertIn("未使用 force", msg)
        self.assertIn(self.BASE_SHA, msg)
        self.assertIn(self.NEW_SHA, msg)
        self.assertIn(self.OTHER_SHA, msg)


class TestWorkflowShellCompatibility(unittest.TestCase):
    """★ Windows CI 全红的回归：windows-latest 的默认 run shell 是
    PowerShell，`ACTUAL=$(git rev-parse HEAD)` 这类 Bash 语法在它下面
    直接跑不通 —— 三个 Windows job 曾全红在 Source identity，Python
    测试一次都没跑。

    契约（不用 YAML 解析器，按缩进切 step）：
      ① ci.yml 里任何含 Bash 专属语法的 step 都必须显式 `shell: bash`；
      ② 两个 Source identity step 必须各自显式声明；
      ③ release.yml 所有 job 只跑 ubuntu（那是它敢裸写 Bash 的前提，
        将来有人加 Windows runner 时这条会提醒他补 shell）。
    """

    CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")
    REL = os.path.join(ROOT, ".github", "workflows", "release.yml")
    BASH_ONLY_MARKERS = ("$(", "if [ ", "[ \"$", "; then", "|| {", "set -e")

    def _steps(self, path):
        """把 workflow 拆成 [(step_name, step_text)]，按缩进切，不引依赖。"""
        steps = []
        cur_name, cur = None, []
        with open(path, encoding="utf-8") as f:
            for raw in f:
                line = raw.rstrip("\n")
                if re.match(r"^      - ", line):          # 新 step（缩进 6）
                    if cur_name is not None:
                        steps.append((cur_name, "\n".join(cur)))
                    m = re.match(r"^      - name:\s*(.+)$", line)
                    cur_name = m.group(1).strip() if m else "(unnamed)"
                    cur = [line]
                elif cur_name is not None and line.startswith("        "):
                    cur.append(line)
        if cur_name is not None:
            steps.append((cur_name, "\n".join(cur)))
        return steps

    def _uses_bash_syntax(self, text):
        return any(mk in text for mk in self.BASH_ONLY_MARKERS)

    def test_every_bash_step_declares_shell_bash(self):
        offenders = [name for name, text in self._steps(self.CI)
                     if self._uses_bash_syntax(text) and "shell: bash" not in text]
        self.assertEqual(offenders, [],
                         "这些 step 用了 Bash 语法却没声明 shell: bash，"
                         "在 windows runner 上会跑不通：%s" % offenders)

    def test_both_source_identity_steps_declare_bash(self):
        declared = [text for name, text in self._steps(self.CI)
                    if name == "Source identity"]
        self.assertEqual(len(declared), 2,
                         "test 与 lint 两个 job 都要有 Source identity")
        for text in declared:
            self.assertIn("shell: bash", text,
                          "Source identity 必须显式 shell: bash —— 删掉它"
                          " Windows CI 会全红")

    def test_release_workflow_is_ubuntu_only(self):
        """release.yml 裸写 Bash 的前提是「只在 ubuntu 跑」，钉住这个前提。"""
        with open(self.REL, encoding="utf-8") as f:
            body = f.read()
        runs_on = re.findall(r"runs-on:\s*(\S+)", body)
        self.assertTrue(runs_on, "release.yml 里必须有 job")
        self.assertEqual(set(runs_on), {"ubuntu-latest"},
                         "release.yml 出现非 ubuntu runner 时，含 Bash 语法的"
                         " step 必须补 shell: bash")


class TestCredentialGateConsistency(unittest.TestCase):
    """登录态凭据排除必须四处同频：release_common（打包/推送）、
    .gitignore、ci.yml、release.yml。任何一处漏掉 .tmp 形态，
    用户 --state 指到仓库内路径时，写入中途崩溃留下的
    `state_xxx.json.tmp` 半成品就可能被 Git 跟踪 / 进发布包。
    """

    TMP_NAMES = ("state_123.json.tmp", "x.state.json.tmp",
                 "storage_state.json.tmp", "cookies_x.json.tmp")

    def test_release_common_covers_tmp(self):
        for rel in self.TMP_NAMES:
            self.assertTrue(RC.rel_excluded("scripts/" + rel),
                            "EXCLUDE_STATE 必须拦下 %s" % rel)
        # 命中不能以牺牲原有排除为代价
        for rel in ("scripts/state_1.json", "scripts/storage_state.json",
                    "scripts/a.state.json", "scripts/cookies_x.json",
                    "scripts/activities_1.json", "scripts/dump.har"):
            self.assertTrue(RC.rel_excluded(rel), rel)
        # 不许矫枉过正：正常的 .tmp 之外内容不该被误伤
        self.assertFalse(RC.rel_excluded("scripts/a.py"))

    def test_gitignore_covers_tmp(self):
        with open(os.path.join(ROOT, ".gitignore"), encoding="utf-8") as f:
            gi = f.read()
        for pat in ("state_*.json.tmp", "*.state.json.tmp",
                    "storage_state.json.tmp", "cookies*.json.tmp"):
            self.assertIn(pat, gi, ".gitignore 缺 %s" % pat)

    def test_workflows_cover_tmp(self):
        for name in ("ci.yml", "release.yml"):
            path = os.path.join(ROOT, ".github", "workflows", name)
            with open(path, encoding="utf-8") as f:
                body = f.read()
            self.assertIn(r"(\.tmp)?", body,
                          "%s 的登录态文件名检查必须覆盖 .tmp 形态" % name)
            self.assertIn(r".*\.state\.json", body,
                          "%s 的登录态检查漏了 *.state.json 家族" % name)


# ---------------------------------------------------------------- release.py（续）

class TestPushCodeReturnsSha(PushCase):

    def test_returns_commit_sha(self):
        old = R.subprocess.run
        R.subprocess.run = _fake_pusher({"sha": "B" * 40, "unchanged": 3,
                                         "added": [], "modified": [],
                                         "deleted": []})
        try:
            sha = R.push_code(self.src, "1.4.2")
        finally:
            R.subprocess.run = old
        self.assertEqual(sha, "B" * 40)

    def test_uses_in_repo_pusher_and_versioned_message(self):
        seen = {}

        def run(cmd, **_kw):
            seen["cmd"] = cmd
            return _fake_pusher({"sha": "C" * 40})(cmd)

        old = R.subprocess.run
        R.subprocess.run = run
        try:
            R.push_code(self.src, "1.4.2")
        finally:
            R.subprocess.run = old
        self.assertTrue(seen["cmd"][1].endswith(
            os.path.join("tools", "gh_push_dir.py")), seen["cmd"][:3])
        self.assertIn("release: prepare v1.4.2", seen["cmd"])

    def test_nonzero_exit_stops_release(self):
        old = R.subprocess.run
        R.subprocess.run = _fake_pusher(returncode=1)
        try:
            with self.assertRaises(SystemExit):
                R.push_code(self.src, "1.4.2")
        finally:
            R.subprocess.run = old

    def test_missing_result_sha_stops(self):
        old = R.subprocess.run
        R.subprocess.run = _fake_pusher({"added": []})     # 没有 sha
        try:
            with self.assertRaises(SystemExit):
                R.push_code(self.src, "1.4.2")
        finally:
            R.subprocess.run = old


class TestSourceShaBinding(PushCase):

    def _args(self, push_code):
        return argparse.Namespace(push_code=push_code, version="1.4.2")

    def _patch_push(self, sha):
        self.used = {"push": 0}

        def fake(src, version):
            self.used["push"] += 1
            return sha
        return fake

    def test_tag_binds_push_sha_not_remote_head(self):
        """★ 推送返回 B、远端 main 变成 C 时，tag 必须打在 B 上。"""
        old_push, old_main, old_tag = R.push_code, R.get_main_sha, R.tag_exists
        main_called = []

        def _main_sha():
            main_called.append(1)
            return "C" * 40
        R.push_code = self._patch_push("B" * 40)
        R.get_main_sha = _main_sha
        R.tag_exists = lambda _t: False
        try:
            sha = R.resolve_source_sha(self._args(True), self.src, "v1.4.2")
        finally:
            R.push_code, R.get_main_sha, R.tag_exists = old_push, old_main, old_tag
        self.assertEqual(sha, "B" * 40, "不能拿后来的 main 当源码 commit")
        self.assertEqual(main_called, [], "tag 的来源不能是回读 main")

    def test_resume_binds_existing_tag_sha(self):
        """★ tag 已存在时，--push-code 也不能把内容换成新推的 commit。"""
        old_push, old_tag, old_tagsha = (R.push_code, R.tag_exists,
                                         R.tag_commit_sha)
        R.push_code = self._patch_push("B" * 40)
        R.tag_exists = lambda _t: True
        R.tag_commit_sha = lambda _t: "A" * 40
        try:
            sha = R.resolve_source_sha(self._args(True), self.src, "v1.4.2")
        finally:
            R.push_code, R.tag_exists = old_push, old_tag
            R.tag_commit_sha = old_tagsha
        self.assertEqual(sha, "A" * 40)
        self.assertEqual(self.used["push"], 0, "resume 不该再推一次代码")


class TestArchiveIdentity(PushCase):
    """★ 核心不变量：实际 zip 的内容 == source_sha 对应 commit 的源码。

    比对对象是 zip 本身，不是工作区 —— 打包之后工作区又被动过的场景
    （TOCTOU）只能靠这个抓出来。
    """

    def make_zip(self, mapping, with_stem=True):
        zpath = os.path.join(self.tmp, "pkg.zip")
        with zipfile.ZipFile(zpath, "w") as z:
            for rel, data in mapping.items():
                name = ("%s/%s" % (R.ZIP_STEM, rel)) if with_stem else rel
                z.writestr(name, data)
        return zpath

    def _patch_remote(self, mapping):
        old = R.remote_tree
        R.remote_tree = lambda _sha: {p: (self.blob(d), "100644")
                                      for p, d in mapping.items()}
        return old

    def test_zip_matches_remote_passes(self):
        zp = self.make_zip({"scripts/a.py": b"A"})
        old = self._patch_remote({"scripts/a.py": b"A"})
        try:
            rep = R.verify_archive_matches_remote(zp, "A" * 40)
        finally:
            R.remote_tree = old
        self.assertTrue(rep["ok"])
        self.assertEqual(rep["n_archive"], 1)

    def test_workspace_drift_does_not_block_release(self):
        """zip 与远端一致即可，工作区改成什么都不影响结论。"""
        self.write("scripts/a.py", b"B")               # 工作区已经变成 B
        zp = self.make_zip({"scripts/a.py": b"A"})     # 包里还是打包时的 A
        old = self._patch_remote({"scripts/a.py": b"A"})
        try:
            rep = R.verify_archive_matches_remote(zp, "A" * 40)
        finally:
            R.remote_tree = old
        self.assertTrue(rep["ok"],
                        "要证明的是 asset == tag，不是 workspace == tag")

    def test_stale_zip_fails(self):
        """★ TOCTOU 回归：打包后源码被改并推送，旧 zip 必须被拦下。"""
        zp = self.make_zip({"scripts/a.py": b"A"})     # 包是旧的
        old = self._patch_remote({"scripts/a.py": b"B"})   # 远端已是新的
        try:
            rep = R.verify_archive_matches_remote(zp, "B" * 40)
        finally:
            R.remote_tree = old
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["changed"], ["scripts/a.py"])

    def test_zip_missing_scope_file_fails(self):
        """推送了却没打进包（远端有、包里没有）→ 发布不完整。"""
        zp = self.make_zip({"scripts/a.py": b"A"})
        old = self._patch_remote({"scripts/a.py": b"A", "scripts/b.py": b"B"})
        try:
            rep = R.verify_archive_matches_remote(zp, "A" * 40)
        finally:
            R.remote_tree = old
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["absent"], ["scripts/b.py"])

    def test_zip_extra_in_scope_file_fails(self):
        """包里混进了 commit 里没有的源码 → 不可信，必须失败。"""
        zp = self.make_zip({"scripts/a.py": b"A", "scripts/extra.py": b"X"})
        old = self._patch_remote({"scripts/a.py": b"A"})
        try:
            rep = R.verify_archive_matches_remote(zp, "A" * 40)
        finally:
            R.remote_tree = old
        self.assertFalse(rep["ok"])
        self.assertEqual(rep["missing"], ["scripts/extra.py"])

    def test_out_of_scope_extra_file_is_ignored(self):
        """范围外的文件（维护侧遗留）不在比对范围内，不该误报。"""
        zp = self.make_zip({"scripts/a.py": b"A", "maintainer-note.txt": b"n"})
        old = self._patch_remote({"scripts/a.py": b"A"})
        try:
            rep = R.verify_archive_matches_remote(zp, "A" * 40)
        finally:
            R.remote_tree = old
        self.assertTrue(rep["ok"])

    def test_zip_without_stem_topdir_is_rejected(self):
        """顶层目录不对（解压会散一地）→ 结构错误，直接停。"""
        zp = self.make_zip({"scripts/a.py": b"A"}, with_stem=False)
        with self.assertRaises(SystemExit):
            R.verify_archive_matches_remote(zp, "A" * 40)


class TestArchiveShaLock(PushCase):
    """sha256 锁：校验通过之后 zip 又被替换 → 上传前必须拦下。"""

    def test_upload_blocked_when_zip_replaced(self):
        zpath = os.path.join(self.tmp, "p.zip")
        with open(zpath, "wb") as f:
            f.write(b"verified-content")
        sha = R.sha256_file(zpath)
        with open(zpath, "ab") as f:                 # 模拟校验后被改动
            f.write(b"tampered")
        with self.assertRaises(SystemExit):
            R.assert_archive_unchanged(zpath, sha)

    def test_upload_allowed_when_unchanged(self):
        zpath = os.path.join(self.tmp, "p.zip")
        with open(zpath, "wb") as f:
            f.write(b"verified-content")
        sha = R.sha256_file(zpath)
        R.assert_archive_unchanged(zpath, sha)       # 不抛即通过


def _on_block(path):
    """读 workflow 顶层 `on:` 触发块（到下一个顶层 key 为止）。"""
    with open(path, encoding="utf-8") as f:
        lines = f.read().splitlines()
    block, inside = [], False
    for ln in lines:
        if re.match(r"^on:", ln):
            inside = True
            block.append(ln)
            continue
        if inside:
            if ln and not ln[0].isspace():           # 下一个顶层 key
                break
            block.append(ln)
    return "\n".join(block)


class TestSinglePublisher(unittest.TestCase):
    """双 publisher 必须消除：release.yml 不能再因 v* tag push 自动发布。"""

    REL = os.path.join(ROOT, ".github", "workflows", "release.yml")

    def _on_block(self):
        return _on_block(self.REL)

    def test_no_tag_push_trigger(self):
        block = self._on_block()
        self.assertIn("workflow_dispatch", block, "手动备用入口要保留")
        self.assertNotIn("tags:", block,
                         "release.py 打 tag 后不能再触发第二次发布")
        self.assertNotRegex(block, r"^\s*push:", "不能监听任何 push 事件")

    def test_manual_publish_path_kept(self):
        with open(self.REL, encoding="utf-8") as f:
            body = f.read()
        self.assertIn("action-gh-release", body, "手动备用发布路径要保留")
        self.assertIn("版本占用检查", body, "手动触发必须先检查版本是否已存在")

    def test_default_publisher_is_release_py(self):
        p = os.path.join(R.HERE, "release.py")
        with open(p, encoding="utf-8") as f:
            body = f.read()
        for fn in ("create_tag", "create_release", "upload_asset"):
            self.assertIn("def %s(" % fn, body,
                          "release.py 必须保留完整发布能力")


class TestWorkflowSourceIdentity(unittest.TestCase):
    """CI 与 Release workflow 的 source identity 必须完全明确，不许互相猜。

    CI   → 验证「正在开发的提交」：PR HEAD / branch HEAD
    Release → 只验证并发布「已经冻结的 tag」的最终 commit SHA
    """

    CI = os.path.join(ROOT, ".github", "workflows", "ci.yml")
    REL = os.path.join(ROOT, ".github", "workflows", "release.yml")
    PR_SHA_EXPR = ("github.event_name == 'pull_request' && "
                   "github.event.pull_request.head.sha || github.sha")

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    # ---------------------------------------------------------- CI

    def test_ci_triggers_on_pr_and_branch_push(self):
        block = _on_block(self.CI)
        self.assertIn("pull_request", block)
        self.assertIn("branches:", block, "push 触发要限定在分支上")
        self.assertIn("main", block, "集成分支的 push 要跑 CI")
        self.assertIn("master", block, "集成分支的 push 要跑 CI")
        # 防双跑：push 若监听 "**"，PR 分支的每次 push 会同时触发
        # branch push CI + pull_request CI，3 OS × 3 Python 矩阵翻倍。
        self.assertNotIn("**", block,
                         "push 不能监听 '**'——feature 分支交给 PR 事件，避免双跑")

    def test_ci_never_triggers_on_tag_push(self):
        block = _on_block(self.CI)
        self.assertNotIn("tags:", block, "tag push 不能触发开发期 CI")

    def test_ci_checks_out_pr_head_not_merge_commit(self):
        """★ PR 事件必须 checkout PR HEAD —— 默认可能拿到合成的 merge commit。"""
        body = self._read(self.CI)
        self.assertIn("Checkout exact source HEAD", body)
        self.assertIn("github.event.pull_request.head.sha", body)

    def test_ci_branch_push_uses_github_sha(self):
        body = self._read(self.CI)
        self.assertIn(self.PR_SHA_EXPR, body,
                      "表达式必须覆盖 PR / branch 两种事件")

    def test_ci_records_and_verifies_source_sha(self):
        body = self._read(self.CI)
        self.assertIn("git rev-parse HEAD", body, "要记录 actual checkout SHA")
        self.assertIn("expected:", body, "要同时打印 expected，别只靠 UI 推断")
        self.assertIn("::error::", body, "actual != expected 必须失败")

    def test_ci_concurrency_cancels_same_ref(self):
        body = self._read(self.CI)
        self.assertIn("concurrency:", body)
        self.assertIn("cancel-in-progress: true", body,
                      "同一 PR/分支的新 HEAD 应取消旧 run")
        self.assertIn("github.event.pull_request.number || github.ref", body,
                      "分组要按 PR / 分支区分，不同 PR 互不取消")

    # ------------------------------------------------------ Release

    def test_release_source_is_existing_tag_not_main(self):
        """★ tag=A、main=B 时，release workflow 的 source 必须是 A。"""
        body = self._read(self.REL)
        self.assertIn('git rev-list -n 1 "$TAG"', body,
                      "源必须从 tag 解析，annotated 也要剥到 commit")
        self.assertIn('ref: "v${{ inputs.version }}"', body,
                      "checkout 的是 tag 本身")
        self.assertNotIn("github.sha", body,
                         "release 绝不能从分支 HEAD 取源码")
        self.assertNotRegex(body, r"ref:\s*(main|master)\b",
                            "绝不能 checkout main 当发布源")

    def test_release_fails_when_tag_missing(self):
        body = self._read(self.REL)
        self.assertIn("不存在", body)
        self.assertIn("只发布已冻结的 tag", body,
                      "tag 不存在要明确失败，而不是自动创建")

    def test_release_never_creates_or_moves_tag(self):
        body = self._read(self.REL)
        self.assertNotIn("git/refs", body,
                         "本 workflow 不创建 / 不更新任何 ref（tag 由 release.py 冻结）")

    def test_release_verify_and_pack_on_same_sha(self):
        """verify / pack 都必须发生在 tag 指向的那个 commit 上。"""
        body = self._read(self.REL)
        self.assertEqual(body.count("ref: ${{ needs.source.outputs.sha }}"), 2,
                         "verify 与 release 两个 job 都要 checkout 解析出的 SHA")
        self.assertGreaterEqual(body.count("git rev-parse HEAD"), 2,
                                "每个 job 都要核对 checkout 的是不是那个 SHA")
        self.assertIn("needs: [source, verify]", body,
                      "pack 必须等 verify 通过")

    def test_release_peels_annotated_tag(self):
        body = self._read(self.REL)
        self.assertIn("rev-list -n 1", body,
                      "剥层语义必须显式可见，不能假设 ref/tags 的 object 就是 commit")
        self.assertIn("annotated", body, "要写明为什么不能直接用 object.sha")


GIT_EXE = os.environ.get("GIT_EXE") or shutil.which("git")


@unittest.skipIf(GIT_EXE is None, "本机没有 git，跳过剥层语义验证（CI 上会跑）")
class TestGitTagPeel(unittest.TestCase):
    """release.yml 用 `git rev-list -n 1 <tag>` 取最终 commit。

    这里用真实 git 仓库验证该命令对两种 tag 都返回 commit SHA ——
    workflow 不能依赖 release.py 碰巧打的是 lightweight tag。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="tagpeel_")
        self.env = dict(os.environ,
                        GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
                        GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")

        def run(*args):
            subprocess.run([GIT_EXE] + list(args), cwd=self.tmp, env=self.env,
                           capture_output=True, text=True, check=True)
        self._run = run
        run("init", "-q")
        with open(os.path.join(self.tmp, "a.txt"), "w", encoding="utf-8") as f:
            f.write("1\n")
        run("add", ".")
        run("commit", "-m", "c1")
        self.commit = subprocess.run(
            [GIT_EXE, "rev-parse", "HEAD"], cwd=self.tmp, env=self.env,
            capture_output=True, text=True, check=True).stdout.strip()
        run("tag", "light")                          # lightweight
        run("tag", "-a", "anno", "-m", "annotated")  # annotated

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rev_list(self, tag):
        return subprocess.run(
            [GIT_EXE, "rev-list", "-n", "1", tag], cwd=self.tmp, env=self.env,
            capture_output=True, text=True, check=True).stdout.strip()

    def test_lightweight_tag_resolves_to_commit(self):
        self.assertEqual(self._rev_list("light"), self.commit)

    def test_annotated_tag_peels_to_commit(self):
        self.assertEqual(self._rev_list("anno"), self.commit)

    def test_annotated_tag_object_is_not_the_commit(self):
        """证明「直接拿 ref 的 object.sha」确实会错 —— 剥层是必要的。"""
        obj = subprocess.run(
            [GIT_EXE, "rev-parse", "anno"], cwd=self.tmp, env=self.env,
            capture_output=True, text=True, check=True).stdout.strip()
        self.assertNotEqual(obj, self.commit,
                            "annotated tag 的 object 是 tag object，不是 commit")


class TestCliSmoke(unittest.TestCase):
    """真实 CLI 入口 —— 单元测试 import 成功不代表命令行能跑。"""

    def _run(self, args, out_dir=None):
        env = dict(os.environ)
        env.pop("PYTHONPATH", None)                  # 入口不能依赖外部 PYTHONPATH
        return subprocess.run([sys.executable] + args, capture_output=True,
                              text=True, cwd=ROOT, env=env, timeout=180)

    def test_pusher_help(self):
        r = self._run([os.path.join("tools", "gh_push_dir.py"), "--help"])
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("ModuleNotFoundError", r.stderr)
        self.assertIn("--src", r.stdout)

    def test_pack_creates_zip_in_temp_dir(self):
        out = tempfile.mkdtemp(prefix="packsmoke_")
        try:
            r = self._run([os.path.join(".github", "scripts", "pack.py"),
                           "--version", "0.0.0-test", "--out",
                           os.path.join(out, "dist")])
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            self.assertNotIn("ModuleNotFoundError", r.stderr)
            produced = os.listdir(os.path.join(out, "dist"))
            self.assertEqual(produced,
                             ["xjtu-siyuanxuetang-grab-v0.0.0-test.zip"],
                             produced)
        finally:
            shutil.rmtree(out, ignore_errors=True)   # 不污染工作区


class TestUploadAssetRetry(unittest.TestCase):
    """附件上传：5xx 要重试，4xx 不要盲目重试。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="asset_")
        self.zip = os.path.join(self.tmp, "p.zip")
        with open(self.zip, "wb") as f:
            f.write(b"x" * 128)
        self._old_sleep = R.time.sleep
        R.time.sleep = lambda _s: None

    def tearDown(self):
        R.time.sleep = self._old_sleep
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run(self, script):
        seq = list(script)

        def fake_curl(args, expect=None):
            item = seq.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return fake_curl

    def test_502_then_201(self):
        old = R.curl_json
        R.curl_json = self._run([R.ApiError("HTTP 502", 502),
                                 (201, {"name": "p.zip", "size": 128})])
        try:
            d = R.upload_asset(1, self.zip)
        finally:
            R.curl_json = old
        self.assertEqual(d["name"], "p.zip")

    def test_400_is_not_retried(self):
        calls = []

        def fake_curl(args, expect=None):
            calls.append(1)
            raise R.ApiError("HTTP 400", 400)
        old = R.curl_json
        R.curl_json = fake_curl
        try:
            with self.assertRaises(R.ApiError):
                R.upload_asset(1, self.zip)
        finally:
            R.curl_json = old
        self.assertEqual(len(calls), 1, "400 是永久错误，不该重试")

    def test_network_error_retries_then_raises(self):
        calls = []

        def fake_curl(args, expect=None):
            calls.append(1)
            raise R.ApiError("connection reset")
        old = R.curl_json
        R.curl_json = fake_curl
        try:
            with self.assertRaises(R.ApiError):
                R.upload_asset(1, self.zip, attempts=3)
        finally:
            R.curl_json = old
        self.assertEqual(len(calls), 3, "网络错误应该重试到上限")


class TestNoLegacyPusherDependency(unittest.TestCase):
    """release.py 不能依赖仓库外的脚本 —— 推送逻辑必须在仓库里。"""

    def test_pusher_path_is_inside_repo(self):
        p = os.path.join(R.HERE, "tools", "gh_push_dir.py")
        self.assertTrue(os.path.isfile(p), "仓库内缺少 %s" % p)


if __name__ == "__main__":
    unittest.main(verbosity=2)
