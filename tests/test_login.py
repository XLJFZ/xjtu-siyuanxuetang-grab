# -*- coding: utf-8 -*-
"""lms_login 的离线测试 —— 不需要 playwright、不需要网络、不开浏览器。

覆盖三块收紧后的语义：

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


class FakePage:
    """只实现 url 属性的假标签页。"""

    def __init__(self, url):
        self.url = url


class FakePagesCtx:
    """只实现 pages 属性的假上下文。"""

    def __init__(self, urls):
        self.pages = [FakePage(u) for u in urls]


class AnyLoggedPageCase(unittest.TestCase):
    """★ 登录检测必须遍历**所有**标签页，而不是只看 ctx.pages[0]。

    回归背景：认证跳转发生在**新标签页**时（身份提供方 window.open，
    或用户自己开新页去认证），被盯着的那个 tab URL 永远不变 ——
    明明 cookie 已写全，脚本却报「登录超时、未保存登录态」，逼用户
    重复认证一次。实测踩过一次（课程 32102）。
    """

    def test_first_tab_hit(self):
        ctx = FakePagesCtx(["%s/course/1/index" % C.BASE,
                            "https://auth.example.edu.cn/cas/login"])
        p, u = L.any_logged_page(ctx)
        self.assertEqual(u, "%s/course/1/index" % C.BASE)
        self.assertIs(p, ctx.pages[0])

    def test_hit_on_later_tab(self):
        """★ 核心回归：第一个 tab 停在 CAS 登录页，第二个 tab 才是 LMS。"""
        ctx = FakePagesCtx([
            "https://authserver.example.edu.cn/cas/login?service=" + C.BASE,
            "%s/course/1/index" % C.BASE,
        ])
        p, u = L.any_logged_page(ctx)
        self.assertEqual(u, "%s/course/1/index" % C.BASE)
        self.assertIs(p, ctx.pages[1],
                      "必须返回命中的那个 page —— 后续抓活动清单要用它")

    def test_no_hit_returns_none(self):
        ctx = FakePagesCtx(["https://authserver.example.edu.cn/cas/login"])
        p, u = L.any_logged_page(ctx)
        self.assertIsNone(p)
        self.assertIsNone(u)

    def test_empty_pages(self):
        p, u = L.any_logged_page(FakePagesCtx([]))
        self.assertIsNone(p)
        self.assertIsNone(u)

    def test_closed_page_does_not_crash(self):
        """标签页被关掉后读 .url 会抛异常 —— 不能把整轮登录拖崩。"""
        class Boom:
            @property
            def url(self):
                raise RuntimeError("page closed")

        ctx = FakePagesCtx([])
        ctx.pages = [Boom(), FakePage("%s/course/2/index" % C.BASE)]
        p, u = L.any_logged_page(ctx)
        self.assertEqual(u, "%s/course/2/index" % C.BASE)
        self.assertIs(p, ctx.pages[1])


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


class CookiesForHostCase(unittest.TestCase):
    """三处调用方（落盘门禁 / 运行中探针 / --check）共用同一个过滤口径。"""

    def test_keeps_host_and_subdomain_and_parent(self):
        host = C.HOST
        parent = host.split(".", 1)[1] if "." in host else host
        ck = [_cookie(host), _cookie("." + host, name="a"),
              _cookie(parent, name="b"), _cookie("other.example", name="c")]
        names = [c["name"] for c in L.cookies_for_host(ck)]
        self.assertIn("session", names)
        self.assertIn("a", names)
        self.assertIn("b", names)
        self.assertNotIn("c", names)

    def test_empty_and_none(self):
        self.assertEqual(L.cookies_for_host([]), [])
        self.assertEqual(L.cookies_for_host(None), [])

    def test_domainless_cookie_kept(self):
        """没写 domain 的按当前 host 处理（与 lms_fetch.opener 一致）。"""
        self.assertEqual(len(L.cookies_for_host([{"name": "n", "value": "v"}])), 1)


class CookieSigCase(unittest.TestCase):
    """指纹只用来判「集合变没变」，绝不能因为顺序不同而误判成变化。"""

    def test_order_independent(self):
        a = [_cookie(name="x"), _cookie(name="y")]
        b = [_cookie(name="y"), _cookie(name="x")]
        self.assertEqual(L.cookie_sig(a), L.cookie_sig(b))

    def test_value_change_detected(self):
        a = [{"name": "s", "value": "1", "domain": C.HOST}]
        b = [{"name": "s", "value": "2", "domain": C.HOST}]
        self.assertNotEqual(L.cookie_sig(a), L.cookie_sig(b))

    def test_new_cookie_detected(self):
        a = [_cookie(name="x")]
        b = [_cookie(name="x"), _cookie(name="session")]
        self.assertNotEqual(L.cookie_sig(a), L.cookie_sig(b))

    def test_empty(self):
        self.assertEqual(L.cookie_sig([]), L.cookie_sig(None))


class ShouldProbeCase(unittest.TestCase):
    """探针频率控制 —— 写错了要么打爆接口，要么认证完成也发现不了。"""

    CK = [_cookie()]

    def test_no_cookies_never_probe(self):
        self.assertFalse(L.should_probe([], None, 0.0, 100.0))

    def test_too_soon_never_probe(self):
        """★哪怕 cookie 已经变了：距上次不足 PROBE_MIN_GAP 也不许发。"""
        sig = L.cookie_sig(self.CK)
        self.assertFalse(L.should_probe(self.CK, "different", 100.0,
                                        100.0 + L.PROBE_MIN_GAP / 2))

    def test_sig_changed_after_gap_probes(self):
        now = 100.0 + L.PROBE_MIN_GAP
        self.assertTrue(L.should_probe(self.CK, "different", 100.0, now))

    def test_unchanged_before_heartbeat_skips(self):
        sig = L.cookie_sig(self.CK)
        now = 100.0 + L.PROBE_HEARTBEAT / 2
        if L.PROBE_HEARTBEAT / 2 < L.PROBE_MIN_GAP:
            self.skipTest("心跳小于最小间隔，本用例无意义")
        self.assertFalse(L.should_probe(self.CK, sig, 100.0, now))

    def test_unchanged_after_heartbeat_probes(self):
        sig = L.cookie_sig(self.CK)
        self.assertTrue(L.should_probe(self.CK, sig, 100.0,
                                       100.0 + L.PROBE_HEARTBEAT))


class StateAgeCase(unittest.TestCase):
    """「几天前创建」只是给用户的判断依据，不能因为格式异常就崩。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="age_")
        self.path = os.path.join(self.tmp, "state_1.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_tz_offset_format(self):
        """★ 落盘用的是 %z（'+0800'），strptime 只读前 19 位，别被偏移噎住。"""
        self._write({"created_at": "2026-10-20T10:00:00+0800"})
        age = L.state_age_days(self.path)
        self.assertIsNotNone(age)
        self.assertGreater(age, 0)

    def test_garbage_returns_none(self):
        self._write({"created_at": "not a date"})
        self.assertIsNone(L.state_age_days(self.path))

    def test_missing_file_returns_none(self):
        self.assertIsNone(L.state_age_days(os.path.join(self.tmp, "nope.json")))


class ApiStatusOfCase(unittest.TestCase):
    """探针的三态必须原样透传，不能把「无法判定」说成「已失效」。"""

    def setUp(self):
        self._real = L.F.api_status

    def tearDown(self):
        L.F.api_status = self._real

    def _stub(self, status, why):
        L.F.api_status = lambda op: (status, why)

    def test_no_host_cookie_short_circuits_to_invalid(self):
        status, _ = L.api_status_of([_cookie("other.example")])
        self.assertEqual(status, L.F.API_INVALID)

    def test_valid_passthrough(self):
        self._stub(L.F.API_VALID, "ok")
        self.assertEqual(L.api_status_of([_cookie()])[0], L.F.API_VALID)

    def test_invalid_passthrough(self):
        self._stub(L.F.API_INVALID, "过期")
        self.assertEqual(L.api_status_of([_cookie()])[0], L.F.API_INVALID)

    def test_unknown_passthrough(self):
        self._stub(L.F.API_UNKNOWN, "网关拦页")
        self.assertEqual(L.api_status_of([_cookie()])[0], L.F.API_UNKNOWN)

    def test_nameless_cookie_is_invalid_not_a_crash(self):
        """（{domain} 但没有 name 的）坏 cookie 没法发出去 —— opener 会跳过它，
        于是请求等于匿名，服务端回登录页 → invalid。
        「没有可发送的凭据」判成 invalid 是诚实的结论（该去重新登录了），
        但**绝不能抛异常**把整个登录流程打断。
        """
        status, why = L.api_status_of([{"domain": C.HOST}])
        self.assertEqual(status, L.F.API_INVALID)
        self.assertIsInstance(why, str)


class WritePayloadCase(unittest.TestCase):
    """探针命中那条写盘路径 —— 与 save_state 必须一字不差。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="payload_")
        self.path = os.path.join(self.tmp, "state_1.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_writes_same_shape_as_save_state(self):
        L._write_payload(self.path, "9", [_cookie()])
        with open(self.path, encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["version"], 1)
        self.assertEqual(st["course"], "9")
        self.assertEqual(st["host"], C.HOST)
        self.assertEqual(len(st["cookies"]), 1)
        self.assertFalse(os.path.exists(self.path + ".tmp"))

    def test_empty_refuses(self):
        with self.assertRaises(L.StateNotSaved):
            L._write_payload(self.path, "9", [])
        self.assertFalse(os.path.exists(self.path))

    @unittest.skipUnless(os.name == "posix", "Windows 靠 ACL，不测 POSIX mode")
    def test_mode_0600(self):
        L._write_payload(self.path, "9", [_cookie()])
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)


if __name__ == "__main__":
    unittest.main(verbosity=2)
