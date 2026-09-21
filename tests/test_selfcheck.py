# -*- coding: utf-8 -*-
"""安装后自检的离线测试 —— 不需要网络、不需要登录态、不需要浏览器。

覆盖重点是「状态判定」而不是「打印好看」：
登录态的四种坏形状（缺失 / 解析失败 / cookies 为空 / cookie 域不匹配）
各自该报出什么级别，以及 playwright 缺失算警告不算失败
（它只影响登录那一步，不该阻断整条链路的自检结论）。

跑法：
    python tests/test_selfcheck.py
"""
import io
import json
import os
import shutil
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import lms_common as C   # noqa: E402
import lms_selfcheck as S  # noqa: E402
import lms_fetch as LF   # noqa: E402


class ReportCase(unittest.TestCase):
    """Report 的行为是自检结论的基础，先把它钉住。"""

    def setUp(self):
        # render() 会往 stdout 打印结果表，测试里不该污染输出
        self._buf = io.StringIO()
        self._old = sys.stdout
        sys.stdout = self._buf

    def tearDown(self):
        sys.stdout = self._old

    def test_counts_by_level(self):
        rep = S.Report()
        rep.passed("a")
        rep.warned("b")
        rep.failed("c")
        self.assertEqual(rep.n_warn, 1)
        self.assertEqual(rep.n_fail, 1)

    def test_exit_code_fail_wins(self):
        rep = S.Report()
        rep.warned("a")
        self.assertEqual(rep.render(), 0)     # 只有警告 -> 放行

        rep2 = S.Report()
        rep2.failed("a")
        self.assertEqual(rep2.render(), 1)    # 有失败 -> 拦住

    def test_fixes_only_listed_for_non_pass(self):
        """通过项不进「需要处理」清单，否则清单会被正常项淹掉。"""
        rep = S.Report()
        rep.passed("ok-1", fix="不该出现")
        rep.warned("warn-1", fix="该出现")
        out = "\n".join(r["fix"] for r in rep.rows if r["fix"] and r["status"] != "PASS")
        self.assertIn("该出现", out)
        self.assertNotIn("不该出现", out)


class DescribeStateCase(unittest.TestCase):
    """describe_state 是「登录态能不能用」的唯一判据，四种坏形状都要认出来。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="selfcheck_")
        self.path = os.path.join(self.tmp, "state_1.json")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, obj):
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump(obj, f)

    def test_missing(self):
        st, _why, n = C.describe_state(self.path)
        self.assertEqual(st, "missing")
        self.assertEqual(n, 0)

    def test_none_path(self):
        self.assertEqual(C.describe_state(None)[0], "missing")

    def test_bad_json(self):
        with open(self.path, "w", encoding="utf-8") as f:
            f.write("{ this is not json")
        self.assertEqual(C.describe_state(self.path)[0], "bad")

    def test_missing_cookies_key(self):
        self._write({"origins": []})
        self.assertEqual(C.describe_state(self.path)[0], "bad")

    def test_empty_cookies(self):
        """文件在但 cookies 为空 —— 最常见的「登录没走完」。"""
        self._write({"cookies": [], "origins": []})
        self.assertEqual(C.describe_state(self.path)[0], "empty")

    def test_only_foreign_cookies(self):
        """cookie 全是别的域名的，说明登录时没真的进到课程页。"""
        self._write({"cookies": [{"name": "a", "value": "1", "domain": "other.example"}]})
        st, _why, n = C.describe_state(self.path)
        self.assertEqual(st, "partial")
        self.assertEqual(n, 1)

    def test_ok(self):
        self._write({"cookies": [
            {"name": "session", "value": "x", "domain": C.HOST},
            {"name": "other", "value": "y", "domain": "passport.example"},
        ]})
        st, why, n = C.describe_state(self.path)
        self.assertEqual(st, "ok")
        self.assertEqual(n, 2)
        self.assertIn(C.HOST, why)

    def test_subdomain_matches(self):
        """cookie 域是带点的子域写法也要算命中，否则会误报 partial。"""
        self._write({"cookies": [{"name": "s", "value": "1",
                                  "domain": "." + C.HOST}]})
        self.assertEqual(C.describe_state(self.path)[0], "ok")


class SelfcheckStateCase(unittest.TestCase):
    """自检里对四种坏形状给出的**级别**不能弄错：
    坏形状分「能救」和「不能用」，级别错了会误导用户。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="selfcheck_")
        self._old_cache = os.environ.get("LMS_CACHE")
        os.environ["LMS_CACHE"] = self.tmp

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("LMS_CACHE", None)
        else:
            os.environ["LMS_CACHE"] = self._old_cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check(self, course="1"):
        rep = S.Report()
        path = S.check_state(rep, course)
        lv = [r["status"] for r in rep.rows]
        return rep, path, lv

    def test_no_state_is_warning_not_failure(self):
        """还没登录是正常状态，不该让自检报失败。"""
        _rep, path, lv = self._check()
        self.assertIn(S._WARN, lv)
        self.assertNotIn(S._FAIL, lv)
        self.assertIsNone(path)

    def test_broken_file_is_failure(self):
        with open(S.state_path("1"), "w", encoding="utf-8") as f:
            f.write("not json")
        _rep, _path, lv = self._check()
        self.assertIn(S._FAIL, lv)

    def test_empty_cookies_is_failure(self):
        with open(S.state_path("1"), "w", encoding="utf-8") as f:
            json.dump({"cookies": []}, f)
        _rep, path, lv = self._check()
        self.assertIn(S._FAIL, lv)
        self.assertIsNotNone(path)          # 有文件路径，便于修复提示指到具体文件

    def test_foreign_cookies_is_warning(self):
        with open(S.state_path("1"), "w", encoding="utf-8") as f:
            json.dump({"cookies": [{"name": "a", "value": "1", "domain": "x.example"}]}, f)
        _rep, _path, lv = self._check()
        self.assertIn(S._WARN, lv)
        self.assertNotIn(S._FAIL, lv)

    def test_valid_state_passes(self):
        with open(S.state_path("1"), "w", encoding="utf-8") as f:
            json.dump({"cookies": [{"name": "s", "value": "1", "domain": C.HOST}]}, f)
        _rep, path, lv = self._check()
        self.assertIn(S._PASS, lv)
        self.assertNotIn(S._FAIL, lv)
        self.assertIsNotNone(path)


class OfflineSafetyCase(unittest.TestCase):
    """自检默认**不许联网**。用户装完就自检，不该在不知情时打服务端。"""

    def test_no_network_without_flag(self):
        import inspect
        src = inspect.getsource(S.main)
        # 在线探测必须被 --online 包着
        self.assertIn("if args.online", src)
        idx = src.index("check_online")
        head = src[:idx]
        self.assertIn("if args.online", head)

    def test_online_is_opt_in_flag(self):
        import inspect
        src = inspect.getsource(S.main)
        self.assertIn('"--online"', src)
        self.assertIn("action=\"store_true\"", src)


class OnlineProbeCase(unittest.TestCase):
    """--online 三态：只有服务端明确拒绝才 FAIL；无法判定必须 WARN。

    ★ 旧实现把 api_ok 的布尔值直接映射 PASS/FAIL —— 网络异常会被报成
    「登录态有效」（PASS），网关拦页会被报成「重新登录」（FAIL）。
    探测通道本身故障和登录态失效是两回事，级别必须分开。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="selfcheck_online_")
        self._old_cache = os.environ.get("LMS_CACHE")
        os.environ["LMS_CACHE"] = self.tmp
        with open(S.state_path("1"), "w", encoding="utf-8") as f:
            json.dump({"cookies": [{"name": "s", "value": "1",
                                    "domain": C.HOST}]}, f)

    def tearDown(self):
        if self._old_cache is None:
            os.environ.pop("LMS_CACHE", None)
        else:
            os.environ["LMS_CACHE"] = self._old_cache
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _check(self, status):
        rep = S.Report()
        with mock.patch.object(LF, "api_status",
                               return_value=(status, "probe-why")):
            S.check_online(rep, S.state_path("1"))
        return [r["status"] for r in rep.rows]

    def test_valid_passes(self):
        self.assertIn(S._PASS, self._check("valid"))

    def test_invalid_fails_with_relogin_hint(self):
        lv = self._check("invalid")
        self.assertIn(S._FAIL, lv)
        self.assertNotIn(S._PASS, lv)

    def test_unknown_warns_never_passes_or_fails(self):
        """★ 网络异常 / 网关故障必须 WARN —— 不许伪装成 PASS，
        也不许抖一次就把用户赶去重新登录。"""
        lv = self._check("unknown")
        self.assertIn(S._WARN, lv)
        self.assertNotIn(S._PASS, lv)
        self.assertNotIn(S._FAIL, lv)


class LayoutCase(unittest.TestCase):
    def test_core_scripts_actually_exist(self):
        """自检列表里的脚本必须真在 scripts/ 下，否则自检自己就是错的。"""
        sdir = os.path.join(HERE, "..", "scripts")
        for fn, _desc in S.CORE_SCRIPTS:
            self.assertTrue(os.path.isfile(os.path.join(sdir, fn)), fn)

    def test_min_py_matches_docs(self):
        """README / SKILL.md 写的是 Python 3.8+，自检不能比它更松。"""
        self.assertGreaterEqual(S.MIN_PY, (3, 8))


if __name__ == "__main__":
    unittest.main(verbosity=2)
