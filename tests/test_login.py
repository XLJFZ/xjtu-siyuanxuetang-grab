# -*- coding: utf-8 -*-
"""lms_login 的离线测试 —— 不需要 playwright、不需要网络、不开浏览器。

覆盖两块收紧后的语义：

  ① 登录成功判定：必须 urlparse 取 hostname 做精确 / 子域匹配。
     字符串包含式判断（`HOST in url`）会被 CAS 登录页查询参数里的
     `service=https://lms...` 骗过 —— 这是旧实现的真实漏洞：
     没登录却被判成「profile 里已有登录态」，随后 0 cookie 落盘照样报成功。

  ② 落盘安全（fail-closed）：
       - 没有适用 cookie 就拒绝写（不允许覆盖已有正常登录态）；
       - POSIX 上 .tmp 从创建那一刻就是 0600，不做「写完再 chmod」；
       - 任何失败路径（写入异常 / 替换异常）都不留 .tmp 半成品；
       - Windows 不伪造 POSIX mode 语义。

跑法：
    python tests/test_login.py
"""
import json
import os
import shutil
import stat
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import lms_common as C   # noqa: E402
import lms_login as L    # noqa: E402


class FakeCtx:
    """只实现 save_state 用到的那个方法。"""

    def __init__(self, cookies):
        self._cookies = cookies

    def cookies(self, _urls):
        return self._cookies


def _cookie(domain=None, name="session"):
    return {"name": name, "value": "v", "domain": domain or C.HOST}


class IsLmsUrlCase(unittest.TestCase):
    """登录成功判定的第一道门：URL 必须真的落在 LMS 域内。"""

    def test_exact_host_ok(self):
        self.assertTrue(L.is_lms_url("%s/course/1/index" % C.BASE))

    def test_subdomain_of_host_ok(self):
        """HOST 的子域（www.lms.xxx）算站内；HOST 的兄弟域
        （lms.xxx 父域下的其他主机）不算 —— 那不是 LMS。"""
        self.assertTrue(L.is_lms_url("https://www.%s/x" % C.HOST))
        parent = C.HOST.split(".", 1)[1] if "." in C.HOST else C.HOST
        self.assertFalse(L.is_lms_url("https://other.%s/x" % parent))

    def test_sso_host_is_not_lms_even_when_url_contains_base(self):
        """★ 核心回归：CAS 登录页的查询参数里带着 service=<BASE>，
        字符串包含式判断会在这里被骗过。"""
        url = ("https://authserver.example.edu.cn/cas/login?service="
               + C.BASE + "/course/1/index")
        self.assertFalse(L.is_lms_url(url))

    def test_url_containment_trap(self):
        url = "https://evil.example.com/redirect?to=" + C.BASE
        self.assertFalse(L.is_lms_url(url))

    def test_empty_and_garbage(self):
        self.assertFalse(L.is_lms_url(""))
        self.assertFalse(L.is_lms_url(None))
        self.assertFalse(L.is_lms_url("not a url"))


class SaveStateCase(unittest.TestCase):
    """落盘门禁与临时文件安全。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="logintest_")
        self.path = os.path.join(self.tmp, "state_1.json")
        self.ctx_ok = FakeCtx([_cookie()])
        self.ctx_empty = FakeCtx([])

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _assert_no_tmp(self):
        self.assertFalse(os.path.exists(self.path + ".tmp"),
                         "失败路径不许留下带 cookie 的 .tmp 半成品")

    def test_save_writes_payload_and_cleans_tmp(self):
        keep = L.save_state(self.ctx_ok, self.path, "1")
        self.assertEqual(len(keep), 1)
        self.assertTrue(os.path.isfile(self.path))
        with open(self.path, encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["version"], 1)
        self.assertEqual(st["host"], C.HOST)
        self.assertEqual(len(st["cookies"]), 1)
        self._assert_no_tmp()

    def test_dotted_and_parent_domain_cookies_are_kept(self):
        """Playwright 常见形态：domain 带 GuidingDot（'.lms.xxx'）。
        父域 cookie（'.xjtu.xxx'）也适用于 HOST；外来域一律不留。"""
        parent = C.HOST.split(".", 1)[1] if "." in C.HOST else C.HOST
        ctx = FakeCtx([_cookie("." + C.HOST), _cookie(parent, name="p"),
                       _cookie("other.example", name="x")])
        keep = L.save_state(ctx, self.path, "1")
        self.assertEqual({c["name"] for c in keep}, {"session", "p"})

    def test_zero_cookies_refuses_to_save(self):
        """★ 0 cookie = 登录未完成：拒绝落盘，而不是保存一份废文件。"""
        with self.assertRaises(L.StateNotSaved):
            L.save_state(self.ctx_empty, self.path, "1")
        self.assertFalse(os.path.exists(self.path), "不该产生任何状态文件")
        self._assert_no_tmp()

    def test_foreign_only_cookies_refuse_to_save(self):
        """cookie 全是别的域名的 —— 和 describe_state 的 partial 同一判定。"""
        ctx = FakeCtx([_cookie("passport.example")])
        with self.assertRaises(L.StateNotSaved):
            L.save_state(ctx, self.path, "1")
        self.assertFalse(os.path.exists(self.path))
        self._assert_no_tmp()

    def test_failed_save_never_overwrites_existing_state(self):
        """旧登录态是正常文件时，一次失败的登录绝不能把它覆盖掉。"""
        old = json.dumps({"version": 1, "cookies": [_cookie()]},
                         ensure_ascii=False)
        with open(self.path, "w", encoding="utf-8") as f:
            f.write(old)
        with self.assertRaises(L.StateNotSaved):
            L.save_state(self.ctx_empty, self.path, "1")
        with open(self.path, encoding="utf-8") as f:
            self.assertEqual(f.read(), old, "已有登录态必须原样保留")
        self._assert_no_tmp()

    def test_unserializable_cookie_leaves_no_tmp(self):
        """序列化中途炸掉 —— .tmp 必须被清掉，state 也不该出现。"""
        ctx = FakeCtx([{"name": "s", "value": object(), "domain": C.HOST}])
        with self.assertRaises(Exception):
            L.save_state(ctx, self.path, "1")
        self.assertFalse(os.path.exists(self.path))
        self._assert_no_tmp()

    def test_replace_failure_leaves_no_state(self):
        """原子替换那一步失败 —— 同样清理干净，不留下任何文件。"""
        real_replace = os.replace

        def boom(src, dst):
            raise OSError("simulated crash")
        os.replace = boom
        try:
            with self.assertRaises(OSError):
                L.save_state(self.ctx_ok, self.path, "1")
        finally:
            os.replace = real_replace
        self.assertFalse(os.path.exists(self.path))
        self._assert_no_tmp()


@unittest.skipUnless(os.name == "posix",
                     "POSIX mode 语义 —— Windows 不伪造（CI 的 ubuntu 会跑）")
class PosixModeCase(unittest.TestCase):
    """0600 必须从 .tmp 创建那一刻就成立，而不是写完再 chmod。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="logintest_")
        self.path = os.path.join(self.tmp, "state_1.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_saved_state_is_0600(self):
        L.save_state(FakeCtx([_cookie()]), self.path, "1")
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)

    def test_tmp_is_0600_at_replace_time(self):
        """★ 在 os.replace 里偷看 .tmp 的 mode 再炸掉 —— 权限收紧必须
        发生在创建时。旧实现（先 open 再 chmod）在这里看到的会是
        umask 放开后的 0644。"""
        captured = {}
        real_replace = os.replace

        def spy(src, dst):
            captured["mode"] = stat.S_IMODE(os.stat(src).st_mode)
            raise OSError("probe")
        os.replace = spy
        try:
            with self.assertRaises(OSError):
                L.save_state(FakeCtx([_cookie()]), self.path, "1")
        finally:
            os.replace = real_replace
        self.assertEqual(captured.get("mode"), 0o600,
                         ".tmp 在替换那一刻就必须已经是 0600")


if __name__ == "__main__":
    unittest.main(verbosity=2)
