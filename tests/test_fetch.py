# -*- coding: utf-8 -*-
"""下载逻辑的离线自测 —— 不需要网络、不需要登录态。

用假 opener 模拟各种服务端行为（超时、5xx、403、sha256 不符、断流），
验证重试策略、sha256 校验、 .part 清理是否按预期工作。

跑法：
    python tests/test_fetch.py
"""
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))

import lms_fetch as F  # noqa: E402


# ---------------------------------------------------------------- 假 opener

class FakeResponse:
    def __init__(self, data, headers=None, status=200, chunk=None):
        self._buf = io.BytesIO(data)
        self.headers = headers or {}
        self.status = status
        self._chunk = chunk

    def read(self, n=None):
        if self._chunk:
            n = self._chunk
        return self._buf.read(n)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class RangeFakeOpener:
    """支持 Range 的假 opener：按请求的 Range 头返回对应片段。"""

    def __init__(self, data, chunk=262144):
        self.data = data
        self.chunk = chunk
        self.ranges = []

    def _serve(self, url, timeout=None, data=None):
        rng = None
        if data is not None:
            pass
        # 从 Request 对象里取 Range 头
        headers = {}
        if hasattr(url, "headers"):
            headers = {k.lower(): v for k, v in url.headers.items()}
            rng = headers.get("range")
        start = 0
        if rng:
            start = int(rng.split("=")[1].split("-")[0])
        self.ranges.append(start)
        body = self.data[start:]
        st = 206 if rng else 200
        return FakeResponse(body, {
            "Content-Type": "application/pdf",
            "Content-Length": str(len(body)),
            "Accept-Ranges": "bytes",
        }, status=st)

    def open(self, url, timeout=None, data=None):
        return self._serve(url, timeout, data)


class FakeOpener:
    """按脚本化序列返回响应或抛异常。每次 open() 消耗一条。"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = 0

    def open(self, url, timeout=None, data=None):
        self.calls += 1
        if not self.script:
            raise AssertionError("假 opener 被调用超过脚本长度")
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def http_err(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, io.BytesIO(b""))


def req_url(u):
    """假 opener 收到的可能是 URL 字符串，也可能是 Request 对象。

    `get_json()` / `api_ok()` 现在会带 `Accept: application/json` 发请求，
    走的是 Request 对象。凡是按 URL 分发的假 opener 都必须先归一化 ——
    否则 `"/activities?" in url` 会直接在 Request 上抛 TypeError。
    """
    return getattr(u, "full_url", u)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="lmstest_")
        self.path = os.path.join(self.tmp, "out.bin")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def part_files(self):
        return [f for f in os.listdir(self.tmp) if f.endswith(".part")]


# ---------------------------------------------------------------- 下载

class TestDownload(Base):

    def test_success(self):
        data = b"x" * 4096
        op = FakeOpener([FakeResponse(data, {"Content-Type": "application/pdf"})])
        r = F.download(op, 1, self.path, quiet=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["size"], 4096)
        self.assertEqual(r["sha256"], hashlib.sha256(data).hexdigest())
        self.assertTrue(os.path.isfile(self.path))
        self.assertEqual(self.part_files(), [])

    def test_retry_then_success(self):
        """前两次超时，第三次成功 —— 应该自动重试且最终落盘。"""
        data = b"y" * 2048
        op = FakeOpener([
            TimeoutError("timed out"),
            TimeoutError("timed out"),
            FakeResponse(data, {"Content-Type": "application/pdf"}),
        ])
        # 把退避时间压掉，别让测试等 4.5 秒
        orig = F.time.sleep
        F.time.sleep = lambda _s: None
        try:
            r = F.download(op, 1, self.path, retries=3, quiet=True)
        finally:
            F.time.sleep = orig
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["retried"], 2)
        self.assertTrue(os.path.isfile(self.path))

    def test_403_not_retried(self):
        """403 是平台侧限制，不该反复重试 —— 只调一次 open 就该返回。"""
        op = FakeOpener([http_err(403)])
        r = F.download(op, 1, self.path, retries=3, quiet=True)
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("fatal"))
        self.assertEqual(op.calls, 1, "403 不应该被重试")
        self.assertFalse(os.path.exists(self.path))

    def test_500_retried(self):
        """500 是服务端临时故障，应该重试。"""
        op = FakeOpener([http_err(500), http_err(500), http_err(500)])
        orig = F.time.sleep
        F.time.sleep = lambda _s: None
        try:
            r = F.download(op, 1, self.path, retries=3, quiet=True)
        finally:
            F.time.sleep = orig
        self.assertFalse(r["ok"])
        self.assertEqual(op.calls, 3)

    def test_empty_response_rejected(self):
        """只有几十字节的响应视为空，不能落盘。"""
        op = FakeOpener([FakeResponse(b"tiny", {"Content-Type": "application/pdf"})])
        orig = F.time.sleep
        F.time.sleep = lambda _s: None
        try:
            r = F.download(op, 1, self.path, retries=1, quiet=True)
        finally:
            F.time.sleep = orig
        self.assertFalse(r["ok"])
        self.assertIn("字节", r["err"])
        self.assertFalse(os.path.exists(self.path))

    def test_html_response_rejected(self):
        """返回 HTML 说明登出或被拦，不是文件。"""
        op = FakeOpener([FakeResponse(b"<html>" + b"z" * 2000,
                                      {"Content-Type": "text/html; charset=utf-8"})])
        r = F.download(op, 1, self.path, quiet=True)
        self.assertFalse(r["ok"])
        self.assertIn("HTML", r["err"])

    def test_sha256_mismatch(self):
        """服务端给了 sha256 但对不上 —— 不能落盘。"""
        data = b"z" * 4096
        op = FakeOpener([FakeResponse(data, {
            "Content-Type": "application/pdf",
            "Lms-Content-Sha256": "0" * 64,
        })])
        orig = F.time.sleep
        F.time.sleep = lambda _s: None
        try:
            r = F.download(op, 1, self.path, retries=1, quiet=True)
        finally:
            F.time.sleep = orig
        self.assertFalse(r["ok"])
        self.assertIn("sha256", r["err"])
        self.assertFalse(os.path.exists(self.path))

    def test_sha256_match(self):
        """服务端 sha256 对得上 —— 正常落盘。"""
        data = b"w" * 4096
        digest = hashlib.sha256(data).hexdigest()
        op = FakeOpener([FakeResponse(data, {
            "Content-Type": "application/pdf",
            "Lms-Content-Sha256": digest,
        })])
        r = F.download(op, 1, self.path, quiet=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["exp_sha"], digest)

    def test_part_cleaned_when_no_resume(self):
        """关掉续传时，失败后不能留下 .part 垃圾。"""
        op = FakeOpener([http_err(500), http_err(500), http_err(500)])
        orig = F.time.sleep
        F.time.sleep = lambda _s: None
        try:
            F.download(op, 1, self.path, retries=3, quiet=True, resume=False)
        finally:
            F.time.sleep = orig
        self.assertEqual(self.part_files(), [], "残留了 .part 文件")

    def test_part_kept_when_resume(self):
        """开着续传时，失败要保留残片，下次才能接着下。"""
        op = FakeOpener([http_err(500), http_err(500), http_err(500)])
        orig = F.time.sleep
        F.time.sleep = lambda _s: None
        try:
            r = F.download(op, 1, self.path, retries=3, quiet=True, resume=True)
        finally:
            F.time.sleep = orig
        self.assertFalse(r["ok"])
        self.assertTrue(r.get("kept_part"), "开着续传却没保留残片")

    def test_overwrite_existing(self):
        """目标已存在时应被原子替换。"""
        with open(self.path, "wb") as f:
            f.write(b"old")
        data = b"n" * 4096
        op = FakeOpener([FakeResponse(data, {"Content-Type": "application/pdf"})])
        r = F.download(op, 1, self.path, quiet=True)
        self.assertTrue(r["ok"])
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), data)


class TestResume(Base):
    """断点续传 —— 平台返回 accept-ranges: bytes 才成立。"""

    def test_resume_from_part(self):
        """有残片时应带 Range 头，从断点继续，而不是从头下。"""
        full = bytes(range(256)) * 400          # 102400 字节
        half = len(full) // 2
        with open(self.path + ".part", "wb") as f:
            f.write(full[:half])

        op = RangeFakeOpener(full)
        r = F.download(op, 1, self.path, retries=1, quiet=True)
        self.assertTrue(r["ok"], r)
        self.assertTrue(r["resumed"], "没有走续传路径")
        self.assertEqual(r["size"], len(full))
        # 请求的 Range 起点应该是残片大小
        self.assertEqual(op.ranges[0], half)
        # 整个文件的 sha256 必须正确（说明残片被正确计入）
        self.assertEqual(r["sha256"], hashlib.sha256(full).hexdigest())
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), full)
        self.assertEqual(self.part_files(), [])

    def test_resume_sha_covers_prefix(self):
        """续传时哈希必须覆盖残片部分，否则完整性校验形同虚设。"""
        full = b"A" * 5000 + b"B" * 5000
        with open(self.path + ".part", "wb") as f:
            f.write(full[:5000])
        op = RangeFakeOpener(full)
        r = F.download(op, 1, self.path, retries=1, quiet=True)
        self.assertTrue(r["ok"])
        self.assertEqual(r["sha256"], hashlib.sha256(full).hexdigest())

    def test_server_ignores_range(self):
        """服务端不支持 Range（返回 200 全量）时，要丢掉残片重下，不能拼接成脏文件。"""
        full = b"C" * 8000
        with open(self.path + ".part", "wb") as f:
            f.write(b"WRONG" * 1000)            # 5000 字节残片

        class NoRangeOpener:
            def open(self, url, timeout=None, data=None):
                return FakeResponse(full, {
                    "Content-Type": "application/pdf",
                    "Content-Length": str(len(full)),
                }, status=200)                  # 故意不带 206

        r = F.download(NoRangeOpener(), 1, self.path, retries=1, quiet=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["size"], len(full))
        self.assertEqual(r["sha256"], hashlib.sha256(full).hexdigest())
        with open(self.path, "rb") as f:
            self.assertEqual(f.read(), full)

    def test_part_larger_than_target_discarded(self):
        """残片比目标还大说明不靠谱，应丢弃重下。"""
        full = b"D" * 1000
        with open(self.path + ".part", "wb") as f:
            f.write(b"X" * 5000)
        op = RangeFakeOpener(full)
        r = F.download(op, 1, self.path, expect_size=1000, retries=1, quiet=True)
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["sha256"], hashlib.sha256(full).hexdigest())


class TestEtagSize(Base):
    """etag 里藏着文件大小 —— 这是平台唯一可用的服务端完整性信号。"""

    def test_parse(self):
        # etag 后半段是十六进制文件大小：0xcb2ec = 832236
        self.assertEqual(F._etag_size('"00000000-cb2ec"'), 832236)

    def test_invalid(self):
        self.assertIsNone(F._etag_size(None))
        self.assertIsNone(F._etag_size(""))
        self.assertIsNone(F._etag_size("W/abc"))

    def test_size_mismatch_detected(self):
        """etag 大小与实际不符时应判定失败。"""
        data = b"E" * 4096
        op = FakeOpener([FakeResponse(data, {
            "Content-Type": "application/pdf",
            "etag": '"1-%x"' % 999999,
        })])
        orig = F.time.sleep
        F.time.sleep = lambda _s: None
        try:
            r = F.download(op, 1, self.path, retries=1, quiet=True)
        finally:
            F.time.sleep = orig
        self.assertFalse(r["ok"])
        self.assertIn("etag", r["err"])


# ---------------------------------------------------------------- 文件名

class TestSafe(Base):

    def test_normal(self):
        self.assertEqual(F.safe("讲义.pdf"), "讲义.pdf")
        self.assertEqual(F.safe("a b c.pptx"), "a b c.pptx")

    def test_long_keeps_ext(self):
        """核心回归：以前超 80 字符会把扩展名切掉。"""
        for n in (79, 80, 82, 100, 200, 500):
            name = "测" * n + ".pdf"
            got = F.safe(name)
            self.assertTrue(got.endswith(".pdf"),
                            "%d 字符时扩展名丢了：%r" % (n, got))
            stem = got[:-4]
            self.assertLessEqual(len(stem), F.MAX_STEM,
                                 "%d 字符时主干超长：%d" % (n, len(stem)))

    def test_long_is_stable(self):
        """同一个长名字每次得到同一个结果（短哈希取自内容，不是随机）。"""
        long = "标题" * 100 + ".docx"
        self.assertEqual(F.safe(long), F.safe(long))

    def test_long_names_differ(self):
        """两个不同的长名字不能撞成同一个文件。"""
        a = F.safe("A" * 200 + ".pdf")
        b = F.safe("B" * 200 + ".pdf")
        self.assertNotEqual(a, b)

    def test_illegal_chars(self):
        self.assertEqual(F.safe('a:b*c?d"e<f>g|h.pdf'), "a_b_c_d_e_f_g_h.pdf")

    def test_path_traversal(self):
        got = F.safe("../../../etc/passwd")
        self.assertNotIn("/", got)
        self.assertNotIn("\\", got)

    def test_empty(self):
        self.assertEqual(F.safe(""), "untitled")
        self.assertEqual(F.safe(None), "untitled")

    def test_multidot(self):
        self.assertEqual(F.safe("a.b.c.docx"), "a.b.c.docx")

    def test_no_ext(self):
        self.assertEqual(F.safe("README"), "README")

    def test_extension_like_tail_not_split(self):
        """『第1.2节讲义』这种尾部不该被当成扩展名切走。"""
        stem, ext = F.split_ext("第1.2节讲义")
        self.assertEqual(ext, "")
        self.assertEqual(stem, "第1.2节讲义")

    def test_dotted_short_tail_is_ext(self):
        stem, ext = F.split_ext("报告.pdf")
        self.assertEqual(stem, "报告")
        self.assertEqual(ext, ".pdf")


class TestHumanSize(Base):
    def test_units(self):
        self.assertEqual(F.human_size(0), "0B")
        self.assertEqual(F.human_size(512), "512B")
        self.assertEqual(F.human_size(1024), "1.0KB")
        self.assertEqual(F.human_size(1024 * 1024), "1.0MB")


# ---------------------------------------------------------------- 落盘路径

class FakeArgs:
    def __init__(self, **kw):
        self.out = "OUT"
        self.organize = False
        self.layout = "activity"
        self.split_projects = False
        for k, v in kw.items():
            setattr(self, k, v)


class TestDestFor(Base):
    def test_activity_layout(self):
        d = F.dest_for("课件", "第1章 绪论", "a.pdf", FakeArgs())
        self.assertEqual(d, os.path.join("OUT", "课件", "第1章 绪论"))

    def test_flat_layout(self):
        d = F.dest_for("课件", "第1章 绪论", "a.pdf", FakeArgs(layout="flat"))
        self.assertEqual(d, os.path.join("OUT", "课件"))

    def test_organize(self):
        d = F.dest_for("课件", "第1章 绪论", "第1章-绪论.pdf", FakeArgs(organize=True))
        self.assertTrue(d.endswith(os.path.join("课件", "第01章 绪论")), d)

    def test_organize_other(self):
        d = F.dest_for("作业", "实验1-配置", "1.1步骤.pdf", FakeArgs(organize=True))
        self.assertTrue(d.endswith(os.path.join("作业", "其他")), d)

    def test_split_projects(self):
        d = F.dest_for("作业", "项目一", "Project1.zip",
                       FakeArgs(split_projects=True))
        self.assertEqual(d, os.path.join("OUT", "项目", "项目一"))

    def test_split_projects_only_zip(self):
        """--split-projects 只对 zip 生效，非 zip 走原逻辑。"""
        d = F.dest_for("作业", "项目一", "Project1.pdf",
                       FakeArgs(split_projects=True))
        self.assertEqual(d, os.path.join("OUT", "作业", "项目一"))

    def test_video_dir(self):
        """录像单独一类，不混进课件。"""
        d = F.dest_for("录像", "Lec1 Introduction", "Lec1.mp4", FakeArgs())
        self.assertEqual(d, os.path.join("OUT", "录像", "Lec1 Introduction"))

    def test_video_ignores_organize(self):
        """--organize 不该把录像塞进「其他」——视频标题认不出章号。

        踩过：不加这个例外，加了 --organize 的课程里录像会全部落进
        <out>/录像/其他/，几十个视频平铺在一起没法看。
        """
        d = F.dest_for("录像", "Lec1 Introduction", "Lec1.mp4",
                       FakeArgs(organize=True))
        self.assertEqual(d, os.path.join("OUT", "录像", "Lec1 Introduction"))
        self.assertNotIn("其他", d)

    def test_video_flat(self):
        d = F.dest_for("录像", "Lec1", "Lec1.mp4", FakeArgs(layout="flat"))
        self.assertEqual(d, os.path.join("OUT", "录像"))


# ---------------------------------------------------------------- 活动分类

class TestCollectKinds(Base):
    """collect() 的活动类型分流 —— 纯离线，喂假 activities 列表。

    直接调真的 collect()，用假 opener 挡掉网络。
    """

    class _Op:
        """只回答两个请求：活动列表、单个活动详情。"""

        def __init__(self, acts, details):
            self.acts = acts
            self.details = details
            self.urls = []

        def open(self, url, timeout=None, data=None):
            url = req_url(url)
            self.urls.append(url)
            if "/activities?" in url:
                payload = {"activities": self.acts}
            else:
                aid = url.split("/activities/")[1].split("?")[0]
                payload = self.details.get(int(aid), {})
            raw = json.dumps(payload).encode("utf-8")

            class R:
                def __init__(self, b):
                    self._b = io.BytesIO(b)
                    self.status = 200
                    self.headers = {}

                def read(self, n=None):
                    return self._b.read() if n is None else self._b.read(n)

                def __enter__(self):
                    return self

                def __exit__(self, *a):
                    return False

            return R(raw)

    def _run_full(self, acts, details, want_video=True):
        """跑一次真实的 collect()，返回 (keys, n1, n2, scan_errors)，输出静音。"""
        import contextlib
        op = self._Op(acts, details)
        with contextlib.redirect_stdout(io.StringIO()):
            return F.collect(op, "1", acts, want_video=want_video)

    def _run(self, acts, details, want_video=True):
        return self._run_full(acts, details, want_video)[0]

    def _kinds(self, acts, want_video=True):
        return self._run(acts, {}, want_video)

    def test_online_video_goes_to_video(self):
        acts = [{"type": "online_video", "title": "Lec1 Introduction",
                 "uploads": [{"id": 101}]}]
        self.assertEqual(self._kinds(acts), [("录像", "Lec1 Introduction", 101)])

    def test_no_video_skips(self):
        acts = [{"type": "online_video", "title": "Lec1",
                 "uploads": [{"id": 101}]}]
        self.assertEqual(self._kinds(acts, want_video=False), [])

    def test_homework_still_homework(self):
        acts = [{"type": "homework", "title": "作业1",
                 "uploads": [{"id": 202}]}]
        self.assertEqual(self._kinds(acts), [("作业", "作业1", 202)])

    def test_doc_type_is_courseware(self):
        """lesson / material / 未知识别类型 ── 一律归课件，不能因为加了
        录像分支就把它们漏掉。"""
        acts = [{"type": "lesson", "title": "第1章", "uploads": [{"id": 1}]},
                {"type": "material", "title": "参考", "uploads": [{"id": 2}]},
                {"type": "whatever", "title": "未知", "uploads": [{"id": 3}]}]
        kinds = [k for k, _, _ in self._kinds(acts)]
        self.assertEqual(kinds, ["课件", "课件", "课件"])

    def test_lecture_live_goes_to_replay(self):
        """lecture_live 单开一类，且 uid 用负数存活动 id —— 它不是附件，
        走不了 uploads 端点，必须能一眼区分出来。"""
        acts = [{"id": 777, "type": "lecture_live", "title": "9-19 第一场",
                 "uploads": []}]
        det = {"data": {"external_live_detail": {"replay_videos": [
            {"camera_id": 1, "camera_type": "encoder", "url": "http://r/x"}]}}}
        keys = self._run(acts, {777: det}, want_video=True)
        self.assertEqual(keys, [("回放", "9-19 第一场", -777)])

    def test_lecture_live_skipped_by_no_video(self):
        acts = [{"id": 777, "type": "lecture_live", "title": "L", "uploads": []}]
        det = {"data": {"external_live_detail": {"replay_videos": [
            {"camera_id": 1, "camera_type": "encoder", "url": "http://r/x"}]}}}
        keys = self._run(acts, {777: det}, want_video=False)
        self.assertEqual(keys, [])

    def test_lecture_live_without_replay_skipped(self):
        """live 活动还没生成回放时 replay_videos 为空，不该产生条目。"""
        acts = [{"id": 777, "type": "lecture_live", "title": "L", "uploads": []}]
        det = {"data": {"external_live_detail": {"replay_videos": []}}}
        keys = self._run(acts, {777: det}, want_video=True)
        self.assertEqual(keys, [])

    def test_activity_without_id_survives(self):
        """活动数据缺 id 时不能崩 —— 错误处理分支里再抛异常是最难查的。

        （真实场景：API 偶发返回残缺对象。）
        """
        acts = [{"type": "lecture_live", "title": "残缺", "uploads": []}]
        keys = self._run(acts, {}, want_video=True)
        self.assertEqual(keys, [])

    def test_mixed(self):
        acts = [{"type": "online_video", "title": "V", "uploads": [{"id": 1}]},
                {"type": "homework", "title": "H", "uploads": [{"id": 2}]},
                {"type": "lesson", "title": "L", "uploads": [{"id": 3}]}]
        kinds = [k for k, _, _ in self._kinds(acts)]
        self.assertEqual(sorted(kinds), ["作业", "录像", "课件"])

    def test_source_counts_exclude_replay(self):
        """来源①②的计数只算 uploads 类条目，回放（来源③）不能混进去。

        回归背景：主流程一度用 len(keys) - n1 反推来源②，视频默认开启时
        回放条目会被算成「正文内嵌」，数字虚高且随回放数量漂移。
        """
        acts = [{"id": 777, "type": "lecture_live", "title": "回放", "uploads": []},
                {"type": "lesson", "title": "L", "uploads": [{"id": 3}]}]
        det = {777: {"data": {"external_live_detail": {"replay_videos": [
            {"camera_id": 1, "camera_type": "encoder", "url": "http://r/x"}]}}}}
        keys, n1, n2, _scan = self._run_full(acts, det, want_video=True)

        self.assertEqual(n1, 1)                     # 来源①：只有 L 的那个附件
        self.assertEqual(n2, 0)                     # 正文内嵌：没有
        self.assertEqual(n1 + n2, 1)                # 两个来源合计
        self.assertEqual(len(keys), 2)              # 但总条目含回放，多 1
        self.assertEqual(sum(1 for k, _, _ in keys if k == "回放"), 1)

    def test_source2_counts_embedded_uploads(self):
        """type=page 活动正文里嵌的 uploads 要计入来源②，且与条目数吻合。"""
        acts = [{"id": 5, "type": "page", "title": "第1讲", "uploads": None}]
        det = {5: {"data": {"content":
                            '<img src="/api/uploads/8811"><a href="/api/uploads/8812">'}}}
        keys, n1, n2, _scan = self._run_full(acts, det, want_video=False)

        self.assertEqual(n1, 0)
        self.assertEqual(n2, 2)
        self.assertEqual(sorted(keys), [("课件", "第1讲", 8811), ("课件", "第1讲", 8812)])


class TestReplayNaming(Base):
    """回放文件名 —— 踩过的坑：同一天多个活动 title 完全相同。"""

    def test_stamp_disambiguates(self):
        """没有时间戳时，同一天多节课会生成同一个文件名、互相覆盖。

        实际情形：同一课程的多个 lecture_live 活动 title 完全相同，
        只有 start_time 不同。
        """
        import lms_live
        t = "2026-09-19-示例课程A"
        a = lms_live.safe_name(t, "encoder", stamp="20260919-1430")
        b = lms_live.safe_name(t, "encoder", stamp="20260919-1530")
        self.assertNotEqual(a, b)
        self.assertIn("20260919-1430", a)
        self.assertIn("20260919-1530", b)

    def test_camera_disambiguates(self):
        """同一场次的两路机位也不能撞名。"""
        import lms_live
        t = "2026-09-19-示例课程A"
        a = lms_live.safe_name(t, "encoder", stamp="20260919-1430")
        b = lms_live.safe_name(t, "instructor", stamp="20260919-1430")
        self.assertNotEqual(a, b)

    def test_stamp_utc_to_cst(self):
        """UTC 的 ISO 时间固定换算成 UTC+8，与运行机器的本地时区无关。"""
        import lms_live
        s = lms_live.start_stamp({"start_time": "2026-09-19T06:30:00Z"})
        self.assertEqual(s, "20260919-1430")

    def test_stamp_stable_across_machine_timezones(self):
        """★ 核心回归：同一份数据在任意时区的机器上必须得到同一个时间戳。

        回归背景：旧实现用 dt.astimezone()（转机器本地时区），于是同一门课
        在中国（UTC+8）、日本（UTC+9）、GitHub Actions（UTC）会生成三个
        不同的文件名，同一批资料的落盘路径不稳定。
        这里直接改 TZ 环境变量并重算，验证结果不受影响。
        """
        import lms_live
        import time as _time
        cases = [
            ("2026-09-19T06:30:00Z", "20260919-1430"),
            ("2026-01-01T00:00:00Z", "20260101-0800"),
            ("2026-12-31T16:00:00Z", "20270101-0000"),   # 跨年跨日
            ("2026-09-19T06:30:00+08:00", "20260919-0630"),  # 已带 +08:00
        ]
        for tz in ("UTC", "Asia/Tokyo", "America/New_York", "Asia/Shanghai"):
            old = os.environ.get("TZ")
            os.environ["TZ"] = tz
            try:
                if hasattr(_time, "tzset"):
                    _time.tzset()
                for raw, want in cases:
                    got = lms_live.start_stamp({"start_time": raw})
                    self.assertEqual(got, want,
                                     "TZ=%s 时 %s 得到 %s" % (tz, raw, got))
            finally:
                if old is None:
                    os.environ.pop("TZ", None)
                else:
                    os.environ["TZ"] = old
                if hasattr(_time, "tzset"):
                    _time.tzset()

    def test_stamp_naive_time_untouched(self):
        """没有时区信息的裸时间不做偏移 —— 当作已经是本地（北京时间）语义。"""
        import lms_live
        s = lms_live.start_stamp({"start_time": "2026-09-19T14:30:00"})
        self.assertEqual(s, "20260919-1430")

    def test_stamp_fallback_regex(self):
        """解析失败时退回正则，不能因为格式怪就丢掉时间戳。"""
        import lms_live
        s = lms_live.start_stamp({"start_time": "2026-09-19T14:30:00.123456"})
        self.assertEqual(s, "20260919-1430")

    def test_stamp_missing(self):
        import lms_live
        self.assertIsNone(lms_live.start_stamp({}))
        self.assertIsNone(lms_live.start_stamp(None))

    def test_parse_replay_encoder_first(self):
        """两路机位按「屏幕录制优先」排序 —— 多数人只要正课画面。"""
        import lms_live
        d = {"data": {"external_live_detail": {"replay_videos": [
            {"camera_id": 1, "camera_type": "instructor", "url": "u1"},
            {"camera_id": 2, "camera_type": "encoder", "url": "u2"},
        ]}}}
        reps = lms_live.parse_replay(d)
        self.assertEqual(reps[0]["camera_type"], "encoder")
        self.assertEqual(len(reps), 2)

    def test_parse_replay_skips_empty_url(self):
        import lms_live
        d = {"data": {"external_live_detail": {"replay_videos": [
            {"camera_id": 1, "camera_type": "encoder", "url": ""},
            {"camera_id": 2, "camera_type": "encoder", "url": "ok"},
        ]}}}
        self.assertEqual(len(lms_live.parse_replay(d)), 1)

    def test_parse_replay_no_detail(self):
        import lms_live
        self.assertEqual(lms_live.parse_replay({}), [])
        self.assertEqual(lms_live.parse_replay(None), [])


# ---------------------------------------------------------------- 清单导出

class TestManifest(Base):
    def test_json(self):
        p = os.path.join(self.tmp, "m.json")
        rows = [{"i": 1, "name": "a.pdf", "status": "ok", "size": 123}]
        F.write_manifest(p, rows)
        import json
        d = json.load(open(p, encoding="utf-8"))
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["files"][0]["name"], "a.pdf")

    def test_csv(self):
        p = os.path.join(self.tmp, "m.csv")
        rows = [{"i": 1, "name": "a.pdf", "status": "ok", "size": 123}]
        F.write_manifest(p, rows)
        with open(p, encoding="utf-8-sig") as f:
            head = f.readline().strip()
        self.assertIn("name", head)
        self.assertIn("status", head)


class LiveClock:
    """虚拟时钟 —— 稳定窗口的测试不需要真的等 60 秒。

    verify_tail 用 time.monotonic 的时间戳差判定稳定，这里把「时间推进」
    交给假 sleeper：sleep(n) 就是把虚拟时间往前推 n 秒。
    """

    def __init__(self):
        self.t = 0.0
        self.sleeps = 0

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.sleeps += 1
        self.t += max(0.0, float(seconds))


class LiveServer:
    """回放服务端的可编程假实现。

    区分两类请求（回放服务端在真实环境里也是两条路径）：
      - 下载请求（无 Range，或 Range: bytes=N-）→ 返回下一个 body
      - 探测请求（Range: bytes=0-0）→ 只回响应头，size 取 probe_sizes 序列

    probe_sizes / etags / lasts 耗尽后停在最后一个值（模拟「稳定」）；
    也可以塞 Exception 进去模拟探测炸掉。
    """

    def __init__(self, bodies, probe_sizes=None, etags=None, lasts=None,
                 declared=None, content_type="video/mp4"):
        if isinstance(bodies, bytes):
            bodies = [bodies]
        self.bodies = list(bodies)
        self.probe_sizes = None if probe_sizes is None else list(probe_sizes)
        self.etags = None if etags is None else list(etags)
        self.lasts = None if lasts is None else list(lasts)
        self.declared = declared
        self.content_type = content_type
        self.gets = 0
        self.probes = 0
        self.range_headers = []

    def _next(self, seq, n):
        if seq is None:
            return None
        if not seq:
            return None
        return seq[min(n, len(seq) - 1)]

    def open(self, req, timeout=None):
        headers = {k.lower(): v for k, v in (req.headers or {}).items()}
        rng = headers.get("range")
        self.range_headers.append(rng)
        if rng == "bytes=0-0":
            size = self._next(self.probe_sizes, self.probes)
            self.probes += 1
            if isinstance(size, Exception):
                raise size
            if size is None:
                return FakeResponse(b"", headers={
                    "Content-Type": "video/mp4"}, status=206)
            h = {"Content-Type": self.content_type,
                 "Content-Range": "bytes 0-0/%d" % size}
            tag = self._next(self.etags, self.probes - 1)
            if tag:
                h["ETag"] = tag
            lm = self._next(self.lasts, self.probes - 1)
            if lm:
                h["Last-Modified"] = lm
            return FakeResponse(b"x", headers=h, status=206)

        body = self._next(self.bodies, self.gets)
        self.gets += 1
        if isinstance(body, Exception):
            raise body
        total = self.declared if self.declared is not None else len(body)
        h = {"Content-Type": self.content_type,
             "Content-Range": "bytes 0-%d/%d" % (len(body) - 1, total)}
        return FakeResponse(body, headers=h, status=206)


class LiveWindowBase(Base):
    """注入虚拟时钟的回放测试基类 —— 窗口语义可断言，且不真等。"""

    def setUp(self):
        super().setUp()
        self.clock = LiveClock()

    def dl(self, op, path, **kw):
        import lms_live
        kw.setdefault("retries", 1)
        kw.setdefault("quiet", True)
        return lms_live.download(op, "http://r/x", path,
                                 clock=self.clock.now, sleep=self.clock.sleep,
                                 **kw)


class TestLiveTailVerification(LiveWindowBase):
    """★ 回放完成判据：只有「实得字节 == 经稳定窗口确认的远端 size」才算完成。

    回归背景：旧实现按「实得字节 vs 响应头声明总长」的比例容差判断
    （8% 以内算成功）。声明值在转码期间会变，于是同一次下载在两套判据下
    反复横跳，截断的视频也可能被当成完整文件永久留下。
    """

    def test_exact_body_needs_stable_window(self):
        """等长也要过窗口 —— 只探一次不算数（转码端可能还要长）。"""
        import lms_live
        op = LiveServer(b"x" * 4096, probe_sizes=[4096], etags=["v1"])
        path = os.path.join(self.tmp, "a.mp4")
        res = self.dl(op, path)
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["verified_size"], 4096)
        self.assertEqual(res["identity"], (4096, "v1"))
        self.assertGreaterEqual(res["probes"], 2, "必须探测两次以上")
        self.assertGreaterEqual(self.clock.t, lms_live.TAIL_STABLE_SECONDS,
                                "接受前必须真的等满稳定窗口")
        self.assertEqual(os.path.getsize(path), 4096)
        self.assertEqual(op.gets, 1, "确认通过不该再重下整份")

    def test_transport_failures_still_use_retry_budget(self):
        """稳定窗口的引入不能吃掉 transport 级重试：超时仍按 retries 重来。"""
        op = LiveServer([socket.timeout("t"), socket.timeout("t"),
                         b"x" * 4096], probe_sizes=[4096])
        path = os.path.join(self.tmp, "retry.mp4")
        res = self.dl(op, path, retries=3)
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["retried"], 2, "两次超时都应被如实记账")
        self.assertEqual(op.gets, 3)

    def test_window_is_time_based_not_probe_count(self):
        """稳定语义按**时间差**，不是「连续 N 次一样」。

        探测间隔 70s > 稳定阈值 60s 时，第二次探测就应认定稳定 ——
        如果实现写成「连续 3 次相同」，这个用例会失败。
        """
        import lms_live
        op = LiveServer(b"x" * 100, probe_sizes=[100])
        r = lms_live.verify_tail(op, "http://r/x", 100,
                                 clock=self.clock.now, sleep=self.clock.sleep,
                                 stable_seconds=60, interval=70, max_wait=300)
        self.assertTrue(r["ok"], r.get("err"))
        self.assertEqual(r["probes"], 2, "按时间差判定时两次探测即够")

    def test_short_read_is_not_tolerated_when_remote_is_bigger(self):
        """★ 声明 1000000、实得 944000、远端仍是 1000000 → 必须判失败。

        旧行为：944000/1000000 少 5.6%，落在 8% 容差内 → 判成功落盘。
        新行为：远端最终 size 是 1000000，本地只有 944000 → 不给过。
        """
        op = LiveServer(b"x" * 944000, probe_sizes=[1000000],
                        declared=1000000)
        path = os.path.join(self.tmp, "short.mp4")
        res = self.dl(op, path)
        self.assertFalse(res["ok"], "远端 1000000 > 本地 944000，不能算完成")
        self.assertFalse(os.path.exists(path), "不许落盘成最终文件")
        self.assertTrue(os.path.exists(path + ".part"), ".part 必须保留续传")

    def test_declared_smaller_than_body_is_ok(self):
        """反向情形：响应头声明 1000000，但远端对象真的只有 944000。

        旧行为按声明判「少 5.6%」可能放行或反复重试；新行为直接问远端 ——
        远端就是 944000，与实得一致 → 完成。
        """
        op = LiveServer(b"x" * 944000, probe_sizes=[944000], declared=1000000)
        path = os.path.join(self.tmp, "decl.mp4")
        res = self.dl(op, path)
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["size"], 944000)
        self.assertEqual(res["verified_size"], 944000)
        self.assertEqual(res["declared"], 1000000)
        self.assertIsNotNone(res["note"], "声明与实得的差异要记进 note")
        self.assertAlmostEqual(res["shortfall"], 0.056, places=3)

    def test_no_size_header_times_out(self):
        """远端连长度都不给 → 无法确认完成，超时判失败（不猜）。"""
        import lms_live
        op = LiveServer(b"x" * 5000, probe_sizes=[])
        path = os.path.join(self.tmp, "nosize.mp4")
        res = self.dl(op, path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "timeout")
        self.assertTrue(os.path.exists(path + ".part"))
        self.assertFalse(os.path.exists(path))

    def test_remote_smaller_fails_without_truncating(self):
        """远端对象比本地还小 → 身份异常，失败；绝不截断本地后接受。"""
        op = LiveServer(b"x" * 4096, probe_sizes=[3600])
        path = os.path.join(self.tmp, "smaller.mp4")
        res = self.dl(op, path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "remote_smaller")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(os.path.getsize(path + ".part"), 4096)

    def test_probe_404_is_gone(self):
        op = LiveServer(b"x" * 4096,
                        probe_sizes=[http_err(404)])
        path = os.path.join(self.tmp, "gone.mp4")
        res = self.dl(op, path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "gone")
        self.assertTrue(os.path.exists(path + ".part"))

    def test_probe_error_never_accepts(self):
        """探测一直炸 → 不能「没证据就当完成」，超时失败。"""
        op = LiveServer(b"x" * 4096, probe_sizes=[socket.timeout("x")])
        path = os.path.join(self.tmp, "err.mp4")
        res = self.dl(op, path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "timeout")
        self.assertFalse(os.path.exists(path))

    def test_etag_change_resets_stability(self):
        """长度相同但 ETag 变了（对象被替换）→ 稳定计时必须重来。"""
        import lms_live
        op = LiveServer(b"x" * 1000, probe_sizes=[1000],
                        etags=["a", "a", "b", "b", "b", "b"])
        r = lms_live.verify_tail(op, "http://r/x", 1000,
                                 clock=self.clock.now, sleep=self.clock.sleep)
        self.assertTrue(r["ok"], r.get("err"))
        self.assertEqual(r["identity"], (1000, "b"))
        self.assertEqual(r["probes"], 6, "换 ETag 之前累计的稳定时间不算数")

    def test_identity_falls_back_to_last_modified(self):
        """没有 ETag 时退化成 Last-Modified 当校验器。"""
        import lms_live
        op = LiveServer(b"x" * 100, probe_sizes=[100],
                        lasts=["Wed, 01 Jan 2025 00:00:00 GMT"])
        r = lms_live.verify_tail(op, "http://r/x", 100,
                                 clock=self.clock.now, sleep=self.clock.sleep)
        self.assertTrue(r["ok"], r.get("err"))
        self.assertEqual(r["identity"],
                         (100, "Wed, 01 Jan 2025 00:00:00 GMT"))

    def test_growth_triggers_resume_then_accepts(self):
        """远端比本地大 → 续传追平 → 重新走窗口，最终接受完整文件。"""
        op = LiveServer([b"x" * 4096, b"x" * 4400],
                        probe_sizes=[4096, 4400, 4400, 4400, 4400],
                        etags=["v1"])
        path = os.path.join(self.tmp, "grow.mp4")
        res = self.dl(op, path)
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["size"], 4400)
        self.assertEqual(res["verified_size"], 4400)
        self.assertGreaterEqual(op.gets, 2, "应当续传一次")

    def test_runaway_growth_bounded(self):
        """远端一直比本地大 → 轮数用尽后失败，保留 .part，不无限追。"""
        import lms_live
        op = LiveServer(b"x" * 4096, probe_sizes=[99999999])
        path = os.path.join(self.tmp, "runaway.mp4")
        res = self.dl(op, path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "growing")
        self.assertEqual(op.gets, lms_live.TAIL_MAX_ROUNDS,
                         "追平轮数必须受 TAIL_MAX_ROUNDS 约束")
        self.assertFalse(os.path.exists(path))
        self.assertTrue(res.get("kept_part"))

    def test_416_means_remote_smaller(self):
        """本地 .part 已经超出远端对象长度 → 416 → 判失败且保留本地字节。"""
        op = LiveServer([http_err(416)], probe_sizes=[1600])
        path = os.path.join(self.tmp, "p416.mp4")
        with open(path + ".part", "wb") as f:
            f.write(b"y" * 4096)
        res = self.dl(op, path)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "remote_smaller")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(os.path.getsize(path + ".part"), 4096,
                         "本地有效前缀不能被删掉")

    def test_no_part_left_after_accept(self):
        op = LiveServer(b"x" * 2048, probe_sizes=[2048])
        path = os.path.join(self.tmp, "clean.mp4")
        res = self.dl(op, path)
        self.assertTrue(res["ok"], res.get("err"))
        self.assertFalse(os.path.exists(path + ".part"))

    def test_probe_follows_416_content_range(self):
        """416 带 `Content-Range: bytes */N` 时 N 就是对象长度（RFC 7233）。

        0 长度对象也会回 416 —— 不能把它当「探测失败」，否则明明可下载的
        资源会被标成错误。
        """
        import lms_live
        err = urllib.error.HTTPError("http://r/x", 416, "range", {
            "Content-Range": "bytes */12345",
            "ETag": "e1"}, io.BytesIO(b""))

        class _Op:
            def open(self, req, timeout=None):
                raise err

        p = lms_live.probe_remote(_Op(), "http://r/x")
        self.assertTrue(p["ok"])
        self.assertEqual(p["size"], 12345)
        self.assertEqual(p["etag"], "e1")

    def test_probe_416_without_content_range_is_failure(self):
        """416 但没给 Content-Range → 拿不到长度，当探测失败（保守）。"""
        import lms_live
        err = urllib.error.HTTPError("http://r/x", 416, "range", {},
                                     io.BytesIO(b""))

        class _Op:
            def open(self, req, timeout=None):
                raise err

        p = lms_live.probe_remote(_Op(), "http://r/x")
        self.assertFalse(p["ok"])
        self.assertIsNone(p["size"])

    def test_probe_uses_range_not_head(self):
        """探测必须走 Range: bytes=0-0（与实际下载同一条路径）。"""
        op = LiveServer(b"x" * 4096, probe_sizes=[4096])
        self.dl(op, os.path.join(self.tmp, "range.mp4"))
        self.assertTrue(op.range_headers, "探测请求没发出来")
        self.assertIn("bytes=0-0", op.range_headers)


class LiveFastBase(Base):
    """lms_live 测试基类：把稳定窗口压成 0 秒。

    这些用例验的是断点续传与落盘正确性，没必要真等 60 秒；
    窗口本身的语义（时间差、ETag 重置、超时）由 TestLiveTailVerification
    用虚拟时钟专门覆盖。
    """

    def setUp(self):
        super().setUp()
        import lms_live
        self._live_saved = (lms_live.TAIL_STABLE_SECONDS,
                            lms_live.TAIL_PROBE_INTERVAL)
        lms_live.TAIL_STABLE_SECONDS = 0.0
        lms_live.TAIL_PROBE_INTERVAL = 0.0

    def tearDown(self):
        import lms_live
        (lms_live.TAIL_STABLE_SECONDS,
         lms_live.TAIL_PROBE_INTERVAL) = self._live_saved
        super().tearDown()


class TestLiveRangeAlignment(LiveFastBase):
    """回放断点续传的 Range 对齐 —— lms_live 下载最容易写坏文件的地方。

    回归背景：回放服务端会按自己的策略调整请求的字节区间（实测常按 4MB /
    2MB 边界对齐），返回的 Content-Range 起点**未必等于**请求的 offset。
    旧实现只处理了 start == 0 一种情况，其余一律 append，于是：

        part = 前 5MB，请求 bytes=5MB-，服务端从 4MB 开始返回
        → 4MB~5MB 这段被写入第二遍 → 文件从 4MB 起整体错位，永久损坏

    这组测试逐字节校验最终内容，确保四种情形都对。
    """

    class _Op:
        """假 opener：模拟服务端「把 Range 起点向前对齐到 boundary」的行为。

        serve_from 是服务端实际返回的起点；boundary 用来模拟按块对齐。
        只有第一次请求才对齐 —— 后续请求按实际 offset 正常返回。
        """

        def __init__(self, total, serve_from=None):
            self.total = total
            self.serve_from = serve_from
            self.calls = []

        def open(self, req, timeout=None):
            headers = {k.lower(): v for k, v in (req.headers or {}).items()}
            rng = headers.get("range")
            req_start = 0
            if rng:
                req_start = int(rng.split("=")[1].split("-")[0])
            start = req_start
            # 第一次请求才「扩大」起点
            if req_start and not self.calls and self.serve_from is not None:
                start = self.serve_from
            self.calls.append((req_start, start))
            body = bytes([(i % 251) for i in range(start, self.total)])
            h = {"Content-Type": "video/mp4",
                 "Content-Range": "bytes %d-%d/%d" % (start, self.total - 1,
                                                      self.total)}
            return FakeResponse(body, headers=h, status=206)

    def _part_with_bytes(self, path, n):
        """造一个「前 n 字节正确」的残片"""
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(bytes([(i % 251) for i in range(n)]))
        return tmp

    def test_server_expands_range_backwards(self):
        """★ 核心回归：part=前 5MB，请求 offset=5MB，服务端从 4MB 开始返回。

        最终文件必须逐字节等于原始数据，4MB~5MB 不能被写两遍。
        """
        import lms_live
        MB = 1024 * 1024
        total = 6 * MB
        path = os.path.join(self.tmp, "expand.mp4")
        self._part_with_bytes(path, 5 * MB)

        op = self._Op(total, serve_from=4 * MB)
        res = lms_live.download(op, "http://r/x", path, retries=1, quiet=True)

        self.assertTrue(res["ok"], res.get("err"))
        with open(path, "rb") as f:
            got = f.read()
        want = bytes([(i % 251) for i in range(total)])
        self.assertEqual(len(got), total)
        self.assertEqual(got, want, "向前扩大的重叠段没有被正确丢弃")

    def test_server_restarts_from_zero_discards_part(self):
        """服务端忽略 Range 从头给（start==0）→ 残片作废，文件仍须完整。"""
        import lms_live
        total = 300000
        path = os.path.join(self.tmp, "from0.mp4")
        self._part_with_bytes(path, 100000)

        op = self._Op(total, serve_from=0)
        res = lms_live.download(op, "http://r/x", path, retries=1, quiet=True)

        self.assertTrue(res["ok"], res.get("err"))
        with open(path, "rb") as f:
            got = f.read()
        self.assertEqual(got, bytes([(i % 251) for i in range(total)]))

    def test_server_gap_is_not_appended(self):
        """★ start > offset（出现缺口）时不能 append，否则必然得到坏文件。

        旧实现在这种情形下直接 append，文件会缺一段、长度也不对。
        正确行为：判定这次续传无效 → 丢弃残片 → 重下一次（无 Range 的完整请求），
        最终文件必须逐字节正确。
        """
        import lms_live
        total = 300000
        path = os.path.join(self.tmp, "gap.mp4")
        self._part_with_bytes(path, 100000)

        op = self._Op(total, serve_from=150000)   # 第一次：服务端从 150000 开始
        res = lms_live.download(op, "http://r/x", path, retries=2, quiet=True)

        self.assertTrue(res["ok"], res.get("err"))
        with open(path, "rb") as f:
            got = f.read()
        self.assertEqual(len(got), total, "缺口情形下文件长度不对")
        self.assertEqual(got, bytes([(i % 251) for i in range(total)]))
        # 第二次请求不应再带 Range（残片已作废）—— 注意后面还有探测请求，
        # 所以这里看第 2 次调用而不是最后一次
        self.assertEqual(op.calls[1][0], 0, "缺口后应重下全量")

    def test_gap_keeps_part_when_no_retry_left(self):
        """★ 缺口但已无重试机会时：不落盘、**保留**有效前缀的 .part。

        回归背景：旧实现在这里先 os.remove(tmp) 再 return kept_part=False，
        把已经正确下载的前 100000 字节白白丢掉 —— 用户下次只能从零开始。
        正确行为：这次响应不作数，但原有前缀仍然有效，必须留住。
        """
        import lms_live
        total = 300000
        path = os.path.join(self.tmp, "gap2.mp4")
        self._part_with_bytes(path, 100000)
        before = open(path + ".part", "rb").read()

        op = self._Op(total, serve_from=150000)
        res = lms_live.download(op, "http://r/x", path, retries=1, quiet=True)

        self.assertFalse(res["ok"])
        self.assertFalse(os.path.exists(path), "残缺文件不许落盘")
        self.assertTrue(os.path.exists(path + ".part"),
                        "有效前缀不能因为最后一次失败而被删掉")
        self.assertTrue(res.get("kept_part"), "应如实报告保留了 .part")
        # .part 不能被这次错误响应污染
        with open(path + ".part", "rb") as f:
            self.assertEqual(f.read(), before)
        self.assertEqual(os.path.getsize(path + ".part"), 100000)

    def test_gap_discards_part_when_retry_left(self):
        """还有重试机会时仍然丢掉残片 —— 否则下一轮会继续撞同一个缺口。"""
        import lms_live
        total = 300000
        path = os.path.join(self.tmp, "gap3.mp4")
        self._part_with_bytes(path, 100000)

        op = self._Op(total, serve_from=150000)
        res = lms_live.download(op, "http://r/x", path, retries=2, quiet=True)

        self.assertTrue(res["ok"], res.get("err"))
        with open(path, "rb") as f:
            self.assertEqual(f.read(),
                             bytes([(i % 251) for i in range(total)]))
        self.assertFalse(os.path.exists(path + ".part"), "成功后不该留 .part")

    def test_part_at_declared_size_is_not_deleted(self):
        """★ `.part` 已经等于声明总长时不再被删掉重下 —— 声明值会变。

        旧行为：`if expect and offset >= expect` → os.remove(tmp) 从零重下，
        把已经下对的前缀白白扔掉（回放转码期间声明值随时会涨，这个判断本身
        就是错的）。新行为：照常按 .part 大小续传。
        """
        import lms_live
        total = 300000
        path = os.path.join(self.tmp, "expect.mp4")
        self._part_with_bytes(path, total)

        op = self._Op(total, serve_from=None)
        res = lms_live.download(op, "http://r/x", path, expect=total,
                                retries=1, quiet=True)

        self.assertTrue(res["ok"], res.get("err"))
        self.assertTrue(res["resumed"], "应走续传，而不是从零重下")
        self.assertEqual(op.calls[0][0], total, "请求应带 offset=300000")
        self.assertEqual(os.path.getsize(path), total)

    def test_normal_resume_unchanged(self):
        """start == offset 的正常续传不能被上面的逻辑误伤。"""
        import lms_live
        total = 300000
        path = os.path.join(self.tmp, "ok.mp4")
        self._part_with_bytes(path, 100000)

        op = self._Op(total, serve_from=None)     # 服务端老实返回
        res = lms_live.download(op, "http://r/x", path, retries=1, quiet=True)

        self.assertTrue(res["ok"], res.get("err"))
        self.assertTrue(res["resumed"])
        with open(path, "rb") as f:
            got = f.read()
        self.assertEqual(got, bytes([(i % 251) for i in range(total)]))
        self.assertEqual(op.calls[0][0], 100000, "应按 part 大小请求续传")


class TestProjectPkg(Base):
    """--split-projects 的判定 —— 压缩包 ≠ 项目包。

    回归背景：旧的 PROJ_HINT 里直接写了 `zip`，配合
    `name.lower().endswith(".zip")`，等价于「所有 zip 都算项目包」，
    实测会把 `资料.zip`、`课件.zip` 一起归进 项目/。
    `.zip` 只应是必要条件，真正的判据是名字里的 project / 项目 / 大作业 等词。
    """

    def test_plain_zip_is_not_project(self):
        """资料.zip 是普通压缩包，不是项目包。"""
        self.assertFalse(F.is_project_pkg("资料.zip"))
        d = F.dest_for("课件", "参考资料", "资料.zip",
                       FakeArgs(split_projects=True))
        self.assertEqual(d, os.path.join("OUT", "课件", "参考资料"))

    def test_project1_zip_is_project(self):
        self.assertTrue(F.is_project_pkg("Project1.zip"))

    def test_proj_2_zip_is_project(self):
        self.assertTrue(F.is_project_pkg("proj_2.zip"))
        self.assertTrue(F.is_project_pkg("proj-3.zip"))

    def test_chinese_keywords(self):
        self.assertTrue(F.is_project_pkg("大作业.zip"))
        self.assertTrue(F.is_project_pkg("课程设计.zip"))
        self.assertTrue(F.is_project_pkg("项目二.rar"))
        self.assertTrue(F.is_project_pkg("Project 2.7z"))

    def test_non_archive_never_project(self):
        """非压缩包即使名字带 project 也不进 项目/（只认压缩包）。"""
        self.assertFalse(F.is_project_pkg("Project1.pdf"))


class TestWinReservedNames(Base):
    """Windows 保留设备名 —— CON.pdf 这类在 Windows 上根本创建不了。

    Linux/macOS 完全合法，但为了让三个平台落盘结果一致，统一加下划线前缀。
    """

    def test_bare_reserved(self):
        for name in ("CON", "PRN", "AUX", "NUL"):
            self.assertEqual(F.safe(name), "_" + name)

    def test_reserved_with_ext(self):
        """带扩展名也照样非法 —— CON.pdf / NUL.txt 同样创建不了。"""
        self.assertEqual(F.safe("CON.pdf"), "_CON.pdf")
        self.assertEqual(F.safe("NUL.txt"), "_NUL.txt")
        self.assertEqual(F.safe("aux.MP4"), "_aux.MP4")

    def test_com_lpt(self):
        for i in range(1, 10):
            self.assertEqual(F.safe("COM%d.pdf" % i), "_COM%d.pdf" % i)
            self.assertEqual(F.safe("lpt%d.txt" % i), "_lpt%d.txt" % i)

    def test_case_insensitive(self):
        self.assertEqual(F.safe("con.PDF"), "_con.PDF")
        self.assertEqual(F.safe("CoM1.zip"), "_CoM1.zip")

    def test_normal_name_untouched(self):
        self.assertEqual(F.safe("console.pdf"), "console.pdf")
        self.assertEqual(F.safe("COM10.pdf"), "COM10.pdf")
        self.assertEqual(F.safe("NULl.pdf"), "NULl.pdf")

    def test_consistency_with_truncation(self):
        """超长名字截断后，只有主干**整体**是保留名才算非法。

        `CONxxxxx…` 不是保留名（Windows 只拦整个主干恰好等于 CON 的），
        不该被加前缀 —— 这条用来防止修复过度。
        """
        long = "CON" + "x" * 200 + ".pdf"
        out = F.safe(long)
        self.assertTrue(out.startswith("CON"), out)
        self.assertTrue(out.endswith(".pdf"), out)

    def test_exact_reserved_after_truncation_fixed(self):
        """主干恰好就是保留名时（含截断产生的）要加前缀。"""
        self.assertEqual(F.safe("NUL.pdf"), "_NUL.pdf")


class TestCsvFormulaInjection(Base):
    """CSV 公式注入 —— LMS 的文件名是外部输入，Excel 会当公式执行。

    只对 CSV 文本字段转义，JSON 清单与实际落盘文件名都不能改。
    """

    def test_dangerous_prefixes_are_escaped(self):
        for bad in ("=cmd|'/c calc'!A1", "+1+1", "-2+3", "@SUM(A1)"):
            self.assertEqual(F.csv_guard(bad), "'" + bad)

    def test_normal_text_untouched(self):
        self.assertEqual(F.csv_guard("第1章 讲义.pdf"), "第1章 讲义.pdf")
        self.assertEqual(F.csv_guard("normal.zip"), "normal.zip")

    def test_non_string_untouched(self):
        """size 这类数字不能被加引号变成字符串。"""
        self.assertEqual(F.csv_guard(123), 123)
        self.assertIsNone(F.csv_guard(None))

    def test_csv_escaped_json_not(self):
        """写 CSV 时转义，写 JSON 时不转义，落盘文件名不受影响。"""
        rows = [{"i": 1, "name": "=danger.pdf", "status": "ok", "size": 3}]
        p = os.path.join(self.tmp, "m.csv")
        F.write_manifest(p, rows)
        with open(p, encoding="utf-8-sig") as f:
            body = f.read()
        self.assertIn("'=danger.pdf", body)

        pj = os.path.join(self.tmp, "m.json")
        F.write_manifest(pj, rows)
        with open(pj, encoding="utf-8") as f:
            d = json.load(f)
        self.assertEqual(d["files"][0]["name"], "=danger.pdf")
        self.assertEqual(rows[0]["name"], "=danger.pdf")


class TestExistingFileCompleteness(Base):
    """已存在文件的完整性判断 —— 旧的 `> 1024` 判据太松。

    回归背景：服务器上 100MB 的文件，本地只有 20MB（上次下到一半被杀）
    也会被当成完整文件永远跳过。有明确声明大小时必须**大小相等**才算完整。
    """

    def test_exact_match_is_complete(self):
        p = os.path.join(self.tmp, "a.bin")
        with open(p, "wb") as f:
            f.write(b"x" * 1000)
        ok, local, exp = F.already_complete(p, 1000)
        self.assertTrue(ok)
        self.assertEqual(local, 1000)
        self.assertEqual(exp, 1000)

    def test_truncated_is_not_complete(self):
        """★ 核心回归：本地截断文件不能算完整。"""
        p = os.path.join(self.tmp, "b.bin")
        with open(p, "wb") as f:
            f.write(b"x" * (20 * 1024 * 1024))
        ok, local, exp = F.already_complete(p, 100 * 1024 * 1024)
        self.assertFalse(ok, "本地 20MB / 服务器 100MB 不该被当成已下载完成")
        self.assertEqual(exp, 100 * 1024 * 1024)

    def test_larger_is_not_complete(self):
        p = os.path.join(self.tmp, "c.bin")
        with open(p, "wb") as f:
            f.write(b"x" * 5000)
        ok, _, _ = F.already_complete(p, 4000)
        self.assertFalse(ok)

    def test_no_declared_size_falls_back_to_nonempty(self):
        """拿不到声明大小时退回「存在且非空」，否则回放会被反复重下。"""
        p = os.path.join(self.tmp, "d.bin")
        with open(p, "wb") as f:
            f.write(b"x" * 5000)
        ok, _, exp = F.already_complete(p, 0)
        self.assertTrue(ok)
        self.assertIsNone(exp)

    def test_getsize_oserror_is_treated_incomplete(self):
        """★ os.path.getsize 抛 OSError 时不能被当成已完成。

        失败处理的方向必须保守：

        - **绝不能判为「完整」**：那等于把一个大小未知 / 状态不确定的文件
          当成已下载而永久跳过 —— 这正是本项目一直在修的那类 silent failure。
          保守判 false，最坏结果只是重下一次几十 MB。
        - **也绝不能写成 `except: return True`**：那是把「这次 stat 没成功」
          伪装成「上次已经下好了」，和把 500 当成 404 是同一种错误。
        """
        import unittest.mock as mock
        p = os.path.join(self.tmp, "e.bin")
        with open(p, "wb") as f:
            f.write(b"x" * 1000)
        with mock.patch("os.path.getsize", side_effect=OSError(5, "I/O error")):
            ok, local, exp = F.already_complete(p, 1000)
        self.assertFalse(ok, "stat 失败的文件不能被判为完整")
        self.assertEqual(local, 0, "本地大小未知时应返回 0，不能编一个数")
        self.assertEqual(exp, 1000, "期望大小仍要返回，供上层打日志")

    def test_getsize_oserror_without_declared_size(self):
        """拿不到声明大小时同样保守 —— exp 返回 None 而不是臆造。"""
        import unittest.mock as mock
        p = os.path.join(self.tmp, "f.bin")
        with open(p, "wb") as f:
            f.write(b"x" * 5000)
        with mock.patch("os.path.getsize", side_effect=OSError(13, "denied")):
            ok, local, exp = F.already_complete(p, 0)
        self.assertFalse(ok)
        self.assertEqual(local, 0)
        self.assertIsNone(exp)

    def test_oserror_or_missing_file_same_direction(self):
        """文件不存在与 stat 失败必须是同一个方向，不能一个 false 一个 true。"""
        import unittest.mock as mock
        p = os.path.join(self.tmp, "g.bin")
        with open(p, "wb") as f:
            f.write(b"x" * 1000)
        missing = F.already_complete(os.path.join(self.tmp, "nope.bin"), 1000)
        with mock.patch("os.path.getsize", side_effect=OSError(2, "gone")):
            errored = F.already_complete(p, 1000)
        self.assertFalse(missing[0])
        self.assertFalse(errored[0])
        self.assertEqual(missing[0], errored[0])

    def test_missing_file(self):
        ok, local, _ = F.already_complete(os.path.join(self.tmp, "nope.bin"), 100)
        self.assertFalse(ok)
        self.assertEqual(local, 0)

    def test_has_expected_size(self):
        self.assertTrue(F.has_expected_size(100))
        self.assertFalse(F.has_expected_size(0))
        self.assertFalse(F.has_expected_size(None))
        self.assertFalse(F.has_expected_size("abc"))


class TestStateCompat(Base):
    """登录态格式兼容 —— 新格式（项目自定义）与旧格式（storage_state）都要能读。

    回归背景：lms_login 从 v1.4 起只保存下载真正需要的 cookie（自定义结构 +
    POSIX 0600），但这不能让用户手上的旧 state 突然不能用。
    """

    def test_old_storage_state_format(self):
        p = os.path.join(self.tmp, "old.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"cookies": [{"name": "sid", "value": "1",
                                    "domain": "lms.xjtu.edu.cn", "path": "/"}],
                       "origins": []}, f)
        st = F.load_state(p)
        self.assertEqual(len(st["cookies"]), 1)

    def test_new_custom_format(self):
        p = os.path.join(self.tmp, "new.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "base": "https://lms.xjtu.edu.cn",
                       "host": "lms.xjtu.edu.cn", "course": "123",
                       "created_at": "2026-09-19T22:00:00+0800",
                       "cookies": [{"name": "sid", "value": "1",
                                    "domain": "lms.xjtu.edu.cn"}]}, f)
        st = F.load_state(p)
        self.assertEqual(len(st["cookies"]), 1)
        self.assertEqual(F.state_host(p), "lms.xjtu.edu.cn")

    def test_bare_list_format(self):
        p = os.path.join(self.tmp, "bare.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump([{"name": "sid", "value": "1"}], f)
        st = F.load_state(p)
        self.assertEqual(len(st["cookies"]), 1)

    def test_bad_structure_raises(self):
        p = os.path.join(self.tmp, "bad.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"no_cookies": 1}, f)
        with self.assertRaises(ValueError):
            F.load_state(p)


class TestCookieSecurity(Base):
    """Cookie 重建要保留安全语义 —— 尤其不能把 HTTPS-only 的 cookie 发到 HTTP。

    回归背景：旧 opener() 把 domain/secure/expires 的位置硬编码成
    `None, False` 和 `True, True`，`secure` 被无条件设成 True、
    `expires` 丢失，而且不做 host 过滤。
    """

    def _state(self, cookies, host="lms.xjtu.edu.cn"):
        p = os.path.join(self.tmp, "st.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "host": host, "cookies": cookies}, f)
        return p

    def _jar(self, path):
        op = F.opener(path)
        for h in op.handlers:
            if hasattr(h, "cookiejar"):
                return h.cookiejar
        self.fail("没找到 CookieJar")

    def test_secure_flag_preserved(self):
        p = self._state([{"name": "s", "value": "1",
                          "domain": "lms.xjtu.edu.cn", "path": "/",
                          "secure": True},
                         {"name": "n", "value": "2",
                          "domain": "lms.xjtu.edu.cn", "path": "/",
                          "secure": False}])
        jar = self._jar(p)
        got = {c.name: c.secure for c in jar}
        self.assertTrue(got["s"], "HTTPS-only 属性被抹掉了")
        self.assertFalse(got["n"])

    def test_expires_preserved(self):
        p = self._state([{"name": "s", "value": "1",
                          "domain": "lms.xjtu.edu.cn",
                          "expires": 1900000000}])
        jar = self._jar(p)
        c = list(jar)[0]
        self.assertEqual(c.expires, 1900000000)

    def test_path_preserved(self):
        p = self._state([{"name": "s", "value": "1",
                          "domain": "lms.xjtu.edu.cn", "path": "/api"}])
        jar = self._jar(p)
        self.assertEqual(list(jar)[0].path, "/api")

    def test_foreign_domain_filtered(self):
        """别的域名的 cookie 不应被装进 jar（不盲目全导入）。"""
        p = self._state([{"name": "lms", "value": "1",
                          "domain": "lms.xjtu.edu.cn"},
                         {"name": "evil", "value": "x",
                          "domain": "example.com"}])
        jar = self._jar(p)
        names = {c.name for c in jar}
        self.assertIn("lms", names)
        self.assertNotIn("evil", names)

    def test_subdomain_cookie_accepted(self):
        p = self._state([{"name": "s", "value": "1",
                          "domain": ".lms.xjtu.edu.cn"}])
        jar = self._jar(p)
        self.assertEqual(len(list(jar)), 1)


LOGIN_PAGE = (b"<!DOCTYPE html><html><head>"
              b"<title>Login - \xe8\xa5\xbf\xe5\xae\x89\xe4\xba\xa4\xe9\x80\x9a"
              b"\xe5\xa4\xa7\xe5\xad\xa6\xe7\xbb\x9f\xe4\xb8\x80\xe8\xba\xab"
              b"\xe4\xbb\xbd\xe8\xae\xa4\xe8\xaf\x81\xe7\xbd\x91\xe5\x85\xb3"
              b"</title></head><body>" + b"y" * 800 + b"</body></html>")


class TestAuthDetection(Base):
    """登录态探测必须能真的判出「过期」。

    ★ 回归背景（2026-09-20 实测）：平台对**未认证**的 API 请求回的是
    `HTTP 200 + 统一身份认证登录页 HTML`，**不是 401**。旧 `api_ok()` 写的是
    `if r.status == 200 and b'"courses"' in body: ... ; return True, "接口可达"`，
    于是登录页落进兜底分支被报成「接口可达」—— 预检永远不会失败，
    `get_json()` 接着抛 `Expecting value: line 1 column 1 (char 0)`，
    看起来像接口坏了，其实是没登录（当时扫了 58 门课才发现）。

    这条用例的意义就是**让预检有可能失败**。
    """

    def _api_ok(self, resp):
        return F.api_ok(FakeOpener([resp]))

    def test_login_page_with_200_is_reported_as_expired(self):
        """核心回归：200 + 登录页 HTML ≠ 有效。"""
        valid, why = self._api_ok(FakeResponse(
            LOGIN_PAGE, {"Content-Type": "text/html;charset=UTF-8"}))
        self.assertFalse(valid, "200 的登录页被判成了有效登录态")
        self.assertIn("过期", why)

    def test_login_page_detected_even_without_html_content_type(self):
        """只看 Content-Type 不够 —— 有的网关会漏掉/写错它，得看响应体。"""
        valid, _why = self._api_ok(FakeResponse(LOGIN_PAGE, {}))
        self.assertFalse(valid)

    def test_html_by_doctype_without_login_keyword(self):
        """别的 HTML（比如网关错误页）也要拦住，但**不能说成「登录过期」**。"""
        valid, why = self._api_ok(FakeResponse(
            b"<!doctype html><html><body>502 bad gateway</body></html>",
            {"Content-Type": "text/html"}))
        self.assertFalse(valid, "非 JSON 的 200 不该放行")
        self.assertNotIn("过期", why,
                         "别把网关错误说成登录过期 —— 用户会白做一次重新登录")
        self.assertIn("JSON", why)

    def test_generic_html_is_not_turned_into_401_by_get_json(self):
        """★ 边界：只有**登录页**才是鉴权失败。

        v1.4.1 定的语义是「200 但 body 不是 JSON → ERR_TRANSIENT」，
    不能因为这次修 bug 就顺手把所有 HTML 都升级成 auth。
        """
        op = FakeOpener([FakeResponse(b"<html>oops</html>", {})] * 3)
        m, err = F.meta(op, 1)
        self.assertIsNone(m)
        self.assertEqual(err["kind"], F.ERR_TRANSIENT)

    def test_valid_json_still_passes(self):
        """别修过头：正常 JSON 必须继续判有效。"""
        valid, why = self._api_ok(FakeResponse(
            b'{"courses":[{"id":1}]}', {"Content-Type": "application/json"}))
        self.assertTrue(valid)
        self.assertIn("有效", why)

    def test_401_and_403_are_expired(self):
        for code in (401, 403):
            valid, why = self._api_ok(http_err(code))
            self.assertFalse(valid, "HTTP %d 应判为失效" % code)

    def test_benign_http_error_still_tolerated(self):
        """5xx 只是抖动，不该把用户赶去重新登录（保持原有的宽容语义）。"""
        valid, _why = self._api_ok(http_err(502))
        self.assertTrue(valid)

    def test_network_error_still_tolerated(self):
        valid, _why = self._api_ok(OSError("connection reset"))
        self.assertTrue(valid)

    # ------------------------------------------------ api_status（三态）

    def _api_status(self, resp):
        return F.api_status(FakeOpener([resp]))

    def test_api_status_valid_on_json(self):
        status, _why = self._api_status(FakeResponse(
            b'{"courses":[{"id":1}]}', {"Content-Type": "application/json"}))
        self.assertEqual(status, F.API_VALID)

    def test_api_status_invalid_on_login_page(self):
        status, _why = self._api_status(FakeResponse(LOGIN_PAGE, {}))
        self.assertEqual(status, F.API_INVALID)

    def test_api_status_invalid_on_401_403(self):
        for code in (401, 403):
            status, _why = self._api_status(http_err(code))
            self.assertEqual(status, F.API_INVALID, "HTTP %d 应判 invalid" % code)

    def test_api_status_unknown_never_masks_as_pass_or_fail(self):
        """★ 无法判定的三种形态（网关拦页 / 5xx / 网络异常）都必须是
        unknown —— selfcheck --online 靠它报 WARN，绝不能静默 PASS，
        也不能把网络抖动误报成「登录态失效」。"""
        for resp in (FakeResponse(b"<html>gateway error</html>", {}),
                     http_err(502),
                     OSError("connection reset")):
            status, _why = self._api_status(resp)
            self.assertEqual(status, F.API_UNKNOWN, repr(resp))

    def test_api_ok_sends_json_accept(self):
        """必须显式声明 Accept: application/json —— 声明后服务端会回 401，
        而不是用 HTML 200 打哑谜；这也是让上面这些判断成立的前提。"""
        op = FakeOpener([FakeResponse(b'{"courses":[]}',
                                      {"Content-Type": "application/json"})])
        seen = {}

        def spy(url, timeout=None, data=None):
            req = url
            seen["accept"] = dict(getattr(req, "headers", {}) or {}).get("Accept")
            seen["is_request"] = hasattr(req, "headers")
            return op.open(url, timeout, data)

        class Spy:
            addheaders = [("User-Agent", "t"), ("Referer", "http://r")]

            def open(self, url, timeout=None, data=None):
                return spy(url, timeout, data)

        F.api_ok(Spy())
        self.assertTrue(seen.get("is_request"), "应该用 Request 对象以便加头")
        self.assertIn("application/json", seen.get("accept") or "")

    def test_get_json_turns_login_page_into_401(self):
        """HTML 200 要被转成 401（清晰、且不重试），而不是 JSONDecodeError。"""
        op = FakeOpener([FakeResponse(
            LOGIN_PAGE, {"Content-Type": "text/html;charset=UTF-8"})])
        with self.assertRaises(urllib.error.HTTPError) as cm:
            F.get_json(op, "http://x/api/whatever", retries=3)
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(op.calls, 1, "401 不该重试")

    def test_get_json_still_parses_valid_json(self):
        op = FakeOpener([FakeResponse(b'{"activities":[]}',
                                      {"Content-Type": "application/json"})])
        self.assertEqual(F.get_json(op, "http://x/api/a"), {"activities": []})


class TestPathCollision(Base):
    """目标路径冲突 —— 同名附件撞到同一路径会让第二个被静默跳过。

    回归背景：不同活动下的 `讲义.pdf` 经 safe()/dest_for() 后落到同一路径，
    第二个条目因为「目标已存在」被 SKIP，文件悄悄丢了。
    """

    def _items(self, names, uid_from=100):
        out = []
        for i, n in enumerate(names):
            out.append({"kind": "课件", "activity": "第1章",
                        "name": n, "uid": uid_from + i, "size": 10})
        return out

    def test_no_collision_untouched(self):
        items, n = F.resolve_collisions(
            self._items(["a.pdf", "b.pdf"]), FakeArgs())
        self.assertEqual(n, 0)
        self.assertEqual([it["name"] for it in items], ["a.pdf", "b.pdf"])

    def test_collision_gets_deterministic_suffix(self):
        items, n = F.resolve_collisions(
            self._items(["讲义.pdf", "讲义.pdf"]), FakeArgs())
        self.assertEqual(n, 1)
        self.assertEqual(items[0]["name"], "讲义.pdf")
        self.assertEqual(items[1]["name"], "讲义~101.pdf")

    def test_collision_stable_across_runs(self):
        """重复运行必须得到同样的路径 —— 不能用随机数或时间戳。"""
        a, _ = F.resolve_collisions(self._items(["x.pdf", "x.pdf"]), FakeArgs())
        b, _ = F.resolve_collisions(self._items(["x.pdf", "x.pdf"]), FakeArgs())
        self.assertEqual([i["name"] for i in a], [i["name"] for i in b])

    def test_three_way_collision(self):
        items, n = F.resolve_collisions(
            self._items(["x.pdf", "x.pdf", "x.pdf"]), FakeArgs())
        self.assertEqual(n, 2)
        names = [i["name"] for i in items]
        self.assertEqual(len(set(names)), 3, "三个条目必须有三个不同路径")

    def test_error_items_ignored(self):
        """不参与下载的条目不该占位。"""
        items = self._items(["a.pdf"]) + [
            {"kind": "课件", "activity": "第1章", "name": "a.pdf",
             "uid": 999, "error": True, "unavailable": True}]
        _, n = F.resolve_collisions(items, FakeArgs())
        self.assertEqual(n, 0)

    def test_collision_across_activities(self):
        """不同活动下的同名文件在 flat 布局下也会撞。"""
        items = [
            {"kind": "课件", "activity": "A", "name": "x.pdf", "uid": 1},
            {"kind": "课件", "activity": "B", "name": "x.pdf", "uid": 2},
        ]
        _, n = F.resolve_collisions(items, FakeArgs(layout="flat"))
        self.assertEqual(n, 1)

    # ---- resource identity：plain 名的归属由「最小 uid」决定，不是顺序 ----

    def test_winner_is_min_uid_not_list_order(self):
        """列表顺序在后的最小 uid 保留 plain 名 —— 身份决定，不是位置。"""
        items = [
            {"kind": "课件", "activity": "A", "name": "x.pdf", "uid": 200},
            {"kind": "课件", "activity": "B", "name": "x.pdf", "uid": 100},
        ]
        _, n = F.resolve_collisions(items, FakeArgs(layout="flat"))
        self.assertEqual(n, 1)
        by_uid = {it["uid"]: it["name"] for it in items}
        self.assertEqual(by_uid[100], "x.pdf")
        self.assertEqual(by_uid[200], "x~200.pdf")

    def test_append_higher_uid_keeps_existing_assignment(self):
        """★ 追加稳定性：集合新增更大 uid 时，已有分配一个都不变。"""
        run1 = [
            {"kind": "课件", "activity": "A", "name": "x.pdf", "uid": 100},
            {"kind": "课件", "activity": "B", "name": "x.pdf", "uid": 101},
        ]
        F.resolve_collisions(run1, FakeArgs(layout="flat"))
        run2 = run1 + [
            {"kind": "课件", "activity": "C", "name": "x.pdf", "uid": 102},
        ]
        F.resolve_collisions(run2, FakeArgs(layout="flat"))
        r1 = {it["uid"]: it["name"] for it in run1}
        r2 = {it["uid"]: it["name"] for it in run2}
        self.assertEqual(r2[100], r1[100])
        self.assertEqual(r2[101], r1[101])
        self.assertEqual(r2[102], "x~102.pdf")

    def test_smaller_uid_takes_over_deterministically(self):
        """新出现的更小 uid 会接管 plain 名 —— 行为确定、可预期，
        且旧文件由下载守卫保住（见 TestResourceIdentityGuard）。"""
        items = [
            {"kind": "课件", "activity": "A", "name": "x.pdf", "uid": 100},
            {"kind": "课件", "activity": "B", "name": "x.pdf", "uid": 50},
        ]
        _, n = F.resolve_collisions(items, FakeArgs(layout="flat"))
        self.assertEqual(n, 1)
        by_uid = {it["uid"]: it["name"] for it in items}
        self.assertEqual(by_uid[50], "x.pdf")
        self.assertEqual(by_uid[100], "x~100.pdf")

    def test_renamed_name_dodges_other_groups_plain_name(self):
        """~uid 后缀撞上另一组的原始文件名时要继续加计数，不能覆盖它。"""
        items = [
            {"kind": "课件", "activity": "A", "name": "x.pdf", "uid": 100},
            {"kind": "课件", "activity": "B", "name": "x.pdf", "uid": 200},
            # 平台上真有叫 `x~200.pdf` 的文件 —— flat 布局同目录
            {"kind": "课件", "activity": "B", "name": "x~200.pdf", "uid": 300},
        ]
        _, n = F.resolve_collisions(items, FakeArgs(layout="flat"))
        # 只有 uid=200 需要改名；uid=300 的原始名本来就不冲突
        self.assertEqual(n, 1)
        by_uid = {it["uid"]: it["name"] for it in items}
        self.assertEqual(by_uid[100], "x.pdf")
        self.assertEqual(by_uid[200], "x~200-2.pdf",
                         "~uid 候选撞上 uid300 的原始名，要继续加计数")
        self.assertEqual(by_uid[300], "x~200.pdf", "原始名不能被改名者抢走")


class TestResourceIdentityGuard(Base):
    """resource identity 守卫：目标已有「完整但与本资源不符」的文件时绝不覆盖。

    回归背景：瞬时错误会把已下载条目的名字槽让出来（meta 失败 → 合成名
    不占位），同目录的另一资源顺势拿走 plain 名并原地覆盖 ——
    前一个 uid 的数据被后一个 uid 的字节替换，且下次运行又翻回来。
    守卫的判定只读文件系统，下载 / --dry-run / --list-only 三处共用。
    """

    KIND = "课件"

    def _dest(self):
        d = os.path.join(self.tmp, self.KIND)
        os.makedirs(d, exist_ok=True)
        return d

    def _put(self, name, size):
        p = os.path.join(self._dest(), name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def _guard(self, name, size, uid=100, kind=None):
        """kind 已不再影响守卫（容差被彻底删除），保留参数只为让调用点读起来
        清楚是在测哪一类资源。"""
        return F.identity_conflict_target(
            self._dest(), name, size, uid)

    # ---- 基本分流 ----

    def test_clean_target_uses_plain_name(self):
        action, p = self._guard("讲义.pdf", 10)
        self.assertEqual(action, "use")
        self.assertTrue(p.endswith(os.path.join(self.KIND, "讲义.pdf")))

    def test_matching_file_is_use(self):
        """大小相符 → 原路 use（调用方随后按 exists 跳过）。"""
        self._put("讲义.pdf", 10)
        action, p = self._guard("讲义.pdf", 10)
        self.assertEqual((action, os.path.basename(p)), ("use", "讲义.pdf"))

    def test_part_file_bypasses_guard(self):
        """.part 残片是本资源的断点 → 原路 use，交给续传。"""
        p = self._put("讲义.pdf", 4)
        with open(p + ".part", "wb") as f:
            f.write(b"y" * 6)
        action, got = self._guard("讲义.pdf", 10)
        self.assertEqual((action, got), ("use", p))

    def test_empty_file_treated_as_junk(self):
        """空文件视为垃圾，原地重下覆盖。"""
        self._put("讲义.pdf", 0)
        action, p = self._guard("讲义.pdf", 10)
        self.assertEqual(os.path.basename(p), "讲义.pdf")

    # ---- 核心守卫 ----

    def test_foreign_file_gets_suffix_and_survives(self):
        """★ 别的资源的文件不能被覆盖：另存 ~uid，原文件字节原样保留。"""
        foreign = self._put("讲义.pdf", 999)
        with open(foreign, "rb") as f:
            before = f.read()
        action, p = self._guard("讲义.pdf", 10, uid=200)
        self.assertEqual(action, "use")
        self.assertEqual(os.path.basename(p), "讲义~200.pdf")
        with open(foreign, "rb") as f:
            self.assertEqual(f.read(), before, "原文件被改动了")

    def test_previous_guard_copy_is_skip(self):
        """上次守卫分流下去的那份就是本资源 → skip，不重复下载。"""
        self._put("讲义.pdf", 999)
        self._put("讲义~200.pdf", 10)
        action, p = self._guard("讲义.pdf", 10, uid=200)
        self.assertEqual(action, "skip")
        self.assertEqual(os.path.basename(p), "讲义~200.pdf")

    def test_second_conflict_gets_counter_suffix(self):
        """~uid 候选也被别的文件占了且大小不符 → 继续加计数。"""
        self._put("讲义.pdf", 999)
        self._put("讲义~200.pdf", 888)
        action, p = self._guard("讲义.pdf", 10, uid=200)
        self.assertEqual(action, "use")
        self.assertEqual(os.path.basename(p), "讲义~200-2.pdf")

    # ---- 回放不再有容差：大小差一点也算不相符 ----

    def test_replay_short_file_is_not_the_same_resource(self):
        """★ 回放 95/100 少 5% —— 旧实现落在 8% 容差内会 use（把截断的字节
        当成本资源固化）；现在比对是精确的 → 走守卫另存，原文件不动。
        """
        self._put("直播.mp4", 95)
        action, p = self._guard("直播.mp4", 100, uid=7, kind="回放")
        self.assertEqual(action, "use")
        self.assertEqual(os.path.basename(p), "直播~7.mp4")
        self.assertEqual(os.path.getsize(os.path.join(self._dest(), "直播.mp4")),
                         95, "原文件被动过")

    def test_replay_exact_match_is_use(self):
        """字节数完全一致才算本资源。"""
        self._put("直播.mp4", 100)
        action, p = self._guard("直播.mp4", 100, uid=7, kind="回放")
        self.assertEqual((action, os.path.basename(p)), ("use", "直播.mp4"))

    # ---- 跨运行收敛（瞬时错误 → 恢复） ----

    def test_convergence_after_transient_error(self):
        """★ 三轮仿真：错误释放名字槽 → 守卫另存 → 恢复后零下载收敛。

        run1: uid=100 下载成功（plain 名，10 字节）
        run2: uid=100 meta 瞬时失败（合成名不占位），uid=200 同名同目录
              → 守卫把 200 分流到 ~200，100 的文件原样还在
        run3: 两个都恢复 → 最小 uid 100 保留 plain 名且大小相符，
              200 落在 ~200 且大小相符 → 没有任何字节需要重下
        """
        args = FakeArgs(out=self.tmp, layout="flat")
        dest = self._dest()

        # run1：只有 100
        it100 = {"kind": self.KIND, "activity": "A",
                 "name": "讲义.pdf", "uid": 100, "size": 10}
        F.resolve_collisions([dict(it100)], args)
        p100 = os.path.join(dest, "讲义.pdf")
        with open(p100, "wb") as f:
            f.write(b"a" * 10)

        # run2：100 报错（合成名，不占位），200 要下载
        it200 = {"kind": self.KIND, "activity": "B",
                 "name": "讲义.pdf", "uid": 200, "size": 12}
        action, target = F.identity_conflict_target(
            dest, "讲义.pdf", 12, 200)
        self.assertEqual(action, "use")
        self.assertEqual(os.path.basename(target), "讲义~200.pdf")
        with open(target, "wb") as f:
            f.write(b"b" * 12)

        # run3：都恢复
        items = [dict(it100), dict(it200)]
        F.resolve_collisions(items, args)
        plan = {}
        for it in items:
            d = F.dest_for(it["kind"], it["activity"], it["name"], args)
            action, target = F.identity_conflict_target(
                d, it["name"], it["size"], it["uid"])
            plan[it["uid"]] = (action, target)
        self.assertEqual(plan[100], ("use", p100))
        self.assertEqual(os.path.getsize(p100), 10, "100 的文件被动过")
        self.assertEqual(os.path.basename(plan[200][1]), "讲义~200.pdf")
        self.assertEqual(os.path.getsize(plan[200][1]), 12, "200 的文件被动过")


class TestDownloadIndex(Base):
    """★ resource identity 封板回归：identity_key → canonical_path 必须是严格函数。

    不变量（任一被破坏即回归）：
        其他资源新增 / 删除、其他资源 meta 失败、排序变化、
        同 UID 内容更新、进程重启 —— 都不改变 canonical_path。

    回归背景：min-uid + 覆盖守卫仍留有一个洞 —— size(uid100)==size(uid200)
    时，uid100 瞬时 meta 失败会让 uid200 把 100 的字节当成自己的
    （already_complete 按大小比对，大小相同无法区分）。持久身份索引
    .download-index.json 让报错条目仍占位，洞从根上堵死。
    """

    KIND = "课件"

    def _args(self):
        return FakeArgs(out=self.tmp, layout="flat")

    def _dest(self):
        d = os.path.join(self.tmp, self.KIND)
        os.makedirs(d, exist_ok=True)
        return d

    def _item(self, uid, name="讲义.pdf", size=10, **kw):
        it = {"kind": self.KIND, "activity": "第1章",
              "name": name, "uid": uid, "size": size}
        it.update(kw)
        return it

    COURSE = "1"

    def _assign(self, items):
        """一轮「扫描 → 分配 → 落盘索引」，模拟真实 main() 的索引生命周期。"""
        index = F.load_download_index(self.tmp)
        F.assign_canonical_paths(items, index, self._args(), self.COURSE)
        F.save_download_index(self.tmp, index)
        return index

    def _write(self, path, size, byte=b"a"):
        with open(path, "wb") as f:
            f.write(byte * size)

    def _index_paths(self):
        idx = F.load_download_index(self.tmp)
        return {k: v["path"] for k, v in idx.items()}

    # ---- 封板回归：瞬时错误 + 等大小 ----

    def test_harsh_convergence_equal_sizes(self):
        """★ 用户点名的固定回归：
        run1: uid100 + uid200（同名同大小）
        run2: uid100 meta 失败，且 size100 == size200
        run3: uid100 恢复
        必须证明：两个 uid 的 canonical_path 三轮不漂移；100 的字节从未
        被当成 200；200 的字节从未覆盖 100；run3 无不必要重下。
        """
        # run1：两个都下载成功
        r1 = [self._item(100, size=10), self._item(200, size=10)]
        idx = self._assign(r1)
        self.assertEqual(idx["course:1:upload:100"]["path"], "%s/讲义.pdf" % self.KIND)
        self.assertEqual(idx["course:1:upload:200"]["path"], "%s/讲义~200.pdf" % self.KIND)
        paths_run1 = self._index_paths()
        p100 = os.path.join(self._dest(), "讲义.pdf")
        p200 = os.path.join(self._dest(), "讲义~200.pdf")
        self._write(p100, 10, b"a")
        self._write(p200, 10, b"b")

        # run2：100 meta 失败（合成名 + error），200 可见，大小相同
        r2 = [self._item(100, name="upload_100", size=10, error=True,
                         err_kind=F.ERR_TRANSIENT),
              self._item(200, size=10)]
        idx2 = self._assign(r2)
        self.assertEqual(idx2["course:1:upload:200"]["path"],
                         "%s/讲义~200.pdf" % self.KIND,
                         "等大小也不能让 200 冒领 plain 名 —— 索引占位")
        # 100 报错不可见，但其身份路径仍被占位
        self.assertEqual(idx2["course:1:upload:100"]["path"], "%s/讲义.pdf" % self.KIND)
        with open(p100, "rb") as f:
            self.assertEqual(f.read(), b"a" * 10, "100 的字节被动过")
        with open(p200, "rb") as f:
            self.assertEqual(f.read(), b"b" * 10, "200 的字节被动过")

        # run3：100 恢复 —— 路径不变，文件已完整，无不必要重下
        r3 = [self._item(100, size=10), self._item(200, size=10)]
        idx3 = self._assign(r3)
        self.assertEqual(self._index_paths(), paths_run1,
                         "三轮之后 canonical_path 必须与 run1 完全一致")
        for it in r3:
            self.assertTrue(it.get("_canon"), "恢复后走索引权威，不重新分配")
            d = F.item_dest(it, self._args())
            done, _, _ = F.already_complete(
                os.path.join(d, it["name"]), it["size"])
            self.assertTrue(done, "run3 不应有任何重下")
        with open(p100, "rb") as f:
            self.assertEqual(f.read(), b"a" * 10)
        with open(p200, "rb") as f:
            self.assertEqual(f.read(), b"b" * 10)

    def test_meta_fail_does_not_free_plain_slot(self):
        """等大小场景的单点：报错条目经索引占位，plain 名不被后来的抢走。"""
        self._assign([self._item(100, size=7)])
        self._write(os.path.join(self._dest(), "讲义.pdf"), 7)
        r2 = [self._item(100, name="upload_100", size=7, error=True,
                         err_kind=F.ERR_TRANSIENT),
              self._item(200, size=7)]
        idx = self._assign(r2)
        self.assertEqual(idx["course:1:upload:200"]["path"], "%s/讲义~200.pdf" % self.KIND)

    def test_new_uid_after_index_gets_own_suffix(self):
        """索引时代之后新增的同名资源也拿自己的后缀，plain 名属于先到身份。"""
        self._assign([self._item(100)])
        r2 = [self._item(100), self._item(300)]
        idx = self._assign(r2)
        self.assertEqual(idx["course:1:upload:300"]["path"], "%s/讲义~300.pdf" % self.KIND)

    def test_same_uid_update_keeps_canonical_path(self):
        """同 uid 内容更新（大小变了）：canonical_path 不漂移，原地覆盖。"""
        idx = self._assign([self._item(100, size=10)])
        r2 = [self._item(100, size=15)]
        idx2 = self._assign(r2)
        self.assertEqual(idx2["course:1:upload:100"]["path"], idx["course:1:upload:100"]["path"])
        self.assertTrue(r2[0].get("_canon"))
        self.assertFalse(r2[0].get("_fresh", False), "不走覆盖守卫 —— 是自己的文件")

    def test_canonical_path_ignores_layout_toggle(self):
        """分配过的身份不随 --organize / 布局参数变化而搬家。"""
        self._assign([self._item(100)])
        r2 = [self._item(100)]
        F.assign_canonical_paths(r2, F.load_download_index(self.tmp),
                                 FakeArgs(out=self.tmp, organize=True), "1")
        self.assertEqual(r2[0]["_dest"], self._dest(),
                         "布局切换不能移动已分配的身份")

    # ---- 索引文件本身 ----

    def test_index_file_contains_no_credentials(self):
        """索引是下载数据库，不是凭据：绝不出现 cookie / token 字样。"""
        self._assign([self._item(100), self._item(200)])
        with open(os.path.join(self.tmp, F.INDEX_NAME), encoding="utf-8") as f:
            body = f.read().lower()
        self.assertNotIn("cookie", body)
        self.assertNotIn("token", body)
        self.assertNotIn("authorization", body)

    def test_corrupt_index_fails_closed_then_recovers_deterministically(self):
        """索引损坏 → 抛 IndexCorruptError（不静默当空索引）；清理现场后
        重新分配仍得到确定的 min-uid 布局，覆盖守卫保护存量文件。"""
        self._assign([self._item(100), self._item(200)])
        with open(os.path.join(self.tmp, F.INDEX_NAME), "w", encoding="utf-8") as f:
            f.write("{not json")
        with self.assertRaises(F.IndexCorruptError):
            F.load_download_index(self.tmp)
        leftovers = [f for f in os.listdir(self.tmp)
                     if f.startswith(F.INDEX_NAME + ".corrupt-")]
        self.assertEqual(len(leftovers), 1, "坏文件保留现场")
        # 模拟维护者确认后清理坏文件 → 重新分配，规则确定性
        idx = {}
        r2 = [self._item(100), self._item(200)]
        F.assign_canonical_paths(r2, idx, self._args(), self.COURSE)
        self.assertEqual(idx["course:1:upload:100"]["path"],
                         "%s/讲义.pdf" % self.KIND)
        self.assertEqual(idx["course:1:upload:200"]["path"],
                         "%s/讲义~200.pdf" % self.KIND)

    def test_replay_key_distinguishes_cameras(self):
        """回放一个活动多路机位共用活动 id —— 键必须带机位，否则互抢身份。"""
        k1 = F.identity_key({"kind": "回放", "uid": 456,
                             "camera": "encoder", "camera_id": "c1"}, "1")
        k2 = F.identity_key({"kind": "回放", "uid": 456,
                             "camera": "instructor", "camera_id": "c2"}, "1")
        self.assertNotEqual(k1, k2)

    # ---- main() 集成：--dry-run 也要持久化索引，两次运行分配稳定 ----

    def test_dry_run_persists_index(self):
        acts = [{"id": 1, "type": "lesson", "title": "第1章",
                 "uploads": [{"id": 100}, {"id": 200}]}]
        ups = {100: {"name": "讲义.pdf", "size": 10},
               200: {"name": "讲义.pdf", "size": 10}}
        out = os.path.join(self.tmp, "OUT")
        rc1, _ = run_main(self.tmp, MainOpener(acts, uploads=ups),
                          ["--dry-run", "--layout", "flat"])
        self.assertEqual(rc1, F.RC_OK)
        idx1 = F.load_download_index(out)
        self.assertIn("course:1:upload:100", idx1)
        rc2, _ = run_main(self.tmp, MainOpener(acts, uploads=ups),
                          ["--dry-run", "--layout", "flat"])
        self.assertEqual(rc2, F.RC_OK)
        idx2 = F.load_download_index(out)
        self.assertEqual(idx2, idx1, "两次运行的分配必须逐字节一致")

    # ---- ★ 索引 size 跨轮存活（loader 曾把它抹掉）----

    def _replay_item(self, uid=9, cam="c7"):
        return {"kind": "回放", "activity": "第1章", "uid": uid,
                "camera_id": cam, "name": "a.mp4", "size": 133691967}

    def test_index_size_survives_save_load_round_trip(self):
        """★ 回归：loader 以前把条目规范化成 {"path", "name"} 两项，
        `size` 在**每一次加载**时都被抹掉。

        后果比「少个字段」重得多：indexed_size() 因此在任何真实运行里都
        返回 None —— 「回放优先用索引里经稳定窗口确认的 size」这条路径
        整个成了死代码，增量判定每轮都退回「依赖本轮远端探测」，
        而远端在转码期/抖动时给出的值正是索引要替代的东西。
        """
        key = "course:1:live:9:camera:c7"
        F.save_download_index(self.tmp, {key: {"name": "a.mp4",
                                               "path": "回放/x/a.mp4",
                                               "size": 133691967}})
        back = F.load_download_index(self.tmp)
        self.assertEqual(back[key].get("size"), 133691967,
                         "size 必须原样带出来")
        self.assertEqual(F.indexed_size(self._replay_item(), back, "1"),
                         133691967, "加载后的索引必须能给出可信 size")

    def test_index_size_stable_across_repeated_runs(self):
        """★ 点名场景：下载完成后再来一轮（哪怕整轮全跳过），
        索引里的 verified_size 必须还在，且值不变。

        run1 分配身份 + 模拟下载成功写下 verified_size → 落盘
        run2 重新 load（这里曾丢 size）→ 再分配一次 → 落盘
        run3 再 load → size 必须仍等于 run1 写下的值
        """
        key = "course:1:live:9:camera:c7"

        index = F.load_download_index(self.tmp)
        F.assign_canonical_paths([self._replay_item()], index,
                                 self._args(), "1")
        rec = dict(index[key])
        rec["size"] = 133691967          # 下载成功分支写下的 verified_size
        index[key] = rec
        F.save_download_index(self.tmp, index)

        index2 = F.load_download_index(self.tmp)
        self.assertEqual(index2[key].get("size"), 133691967,
                         "加载后 size 不得丢失")
        F.assign_canonical_paths([self._replay_item()], index2,
                                 self._args(), "1")
        F.save_download_index(self.tmp, index2)

        index3 = F.load_download_index(self.tmp)
        self.assertEqual(index3[key].get("size"), 133691967,
                         "跳过的一轮不能把已验证的 size 冲掉")
        self.assertEqual(F.indexed_size(self._replay_item(), index3, "1"),
                         133691967)

    def test_index_bad_size_dropped_not_fatal(self):
        """坏 size 只丢这个字段，不能让整份索引 fail-closed。

        path 坏了 = 身份漂移（必须拒绝下载）；size 坏了 = 只是「没有可信
        size」，退回本轮远端探测精确比对即可 —— 为它拒绝下载是过度反应。
        """
        key = "course:1:live:9:camera:c7"
        for bad in ("abc", -5, 0, None, True, 3.5, ""):
            F.save_download_index(self.tmp, {key: {"name": "a.mp4",
                                                   "path": "回放/x/a.mp4",
                                                   "size": bad}})
            back = F.load_download_index(self.tmp)      # 不得抛异常
            self.assertNotIn("size", back[key],
                             "坏值 %r 必须被丢弃" % (bad,))
            self.assertIsNone(F.indexed_size(self._replay_item(), back, "1"))

    def test_index_numeric_string_size_accepted(self):
        """索引是允许人工核对的落盘文件：size 写成字符串不该作废整份，
        按数值收下。非数字字符串仍然丢弃（见上一条）。"""
        key = "course:1:live:9:camera:c7"
        F.save_download_index(self.tmp, {key: {"name": "a.mp4",
                                               "path": "回放/x/a.mp4",
                                               "size": "133691967"}})
        back = F.load_download_index(self.tmp)
        self.assertEqual(F.indexed_size(self._replay_item(), back, "1"),
                         133691967)

    def test_merge_index_entry_preserves_size_unless_path_moves(self):
        """★ 索引写入只有 merge_index_entry 一个入口。

        理由：以前是「三处各自赋值整条记录」，要同时都写对才不漏 size ——
        漏一处就等于 index 里那条可信 size 被无关代码路径顺手抹掉。
        语义：原地重新登记（同 path）保住 size；路径真的搬了
        （换名另存 / 重新分配）则旧 size 不再指同一个文件，必须丢弃。
        """
        key = "course:1:live:9:camera:c7"
        idx = {}
        F.merge_index_entry(idx, key, "回放/x/a.mp4", "a.mp4", size=133691967)
        self.assertEqual(idx[key]["size"], 133691967)

        F.merge_index_entry(idx, key, "回放/x/a.mp4", "a.mp4")
        self.assertEqual(idx[key]["size"], 133691967, "同路径不得丢 size")

        F.merge_index_entry(idx, key, "回放/x/a~9.mp4", "a~9.mp4")
        self.assertNotIn("size", idx[key], "换名另存必须丢弃旧 size")
        self.assertEqual(idx[key]["path"], "回放/x/a~9.mp4")


class TestIdentityHardening(Base):
    """resource identity 封板前的最后三条边界：

    1. live 身份用 camera_id（同类型多机位不串身份），缺失才降级 type；
       同活动多路无 camera_id 同类型 → 显式 identity ambiguous，绝不硬合并
    2. identity key 带 course namespace —— 多课程共用 --out 不串身份
    3. 索引 fail-closed：损坏不静默重建；路径越界条目丢弃，索引不是任意写入口
    """

    COURSE = "1"

    # ---- live identity ----

    def test_live_key_uses_camera_id(self):
        """同活动两个同类型机位，camera_id 不同 → 身份不同。"""
        a = F.identity_key({"kind": "回放", "uid": 456,
                            "camera": "encoder", "camera_id": "cam1"}, "1")
        b = F.identity_key({"kind": "回放", "uid": 456,
                            "camera": "encoder", "camera_id": "cam2"}, "1")
        self.assertNotEqual(a, b)
        self.assertIn("camera:cam1", a)

    def test_live_key_falls_back_to_type_without_camera_id(self):
        cid_missing = F.identity_key({"kind": "回放", "uid": 456,
                                      "camera": "instructor"}, "1")
        self.assertEqual(cid_missing, "course:1:live:456:type:instructor")

    def test_ambiguous_replays_fail_loudly(self):
        """同活动多路无 camera_id 且同类型 → 显式 FAIL，绝不硬合并。"""
        detail = {"data": {"external_live_detail": {"replay_videos": [
            {"camera_type": "encoder", "url": "http://x/1.mp4"},
            {"camera_type": "encoder", "url": "http://x/2.mp4"},
        ]}}}
        op = MainOpener([], details={7: detail})
        keys = [("回放", "直播", -7)]   # collect 约定：回放 uid = -活动id
        items, _err = F.expand_items(op, keys, FakeArgs(all_cameras=True))
        self.assertEqual(len(items), 2)
        for it in items:
            self.assertTrue(it.get("error"), "身份歧义必须显式失败")
            self.assertEqual(it.get("err_kind"), F.ERR_IDENTITY)
            self.assertIn("歧义", it.get("err_msg") or "")
            self.assertFalse(it.get("unavailable"))

    def test_course_namespace_separates_same_uid(self):
        """两个课程共用 --out 时，同 uid 不串身份。"""
        a = F.identity_key({"kind": "课件", "uid": 123}, "1")
        b = F.identity_key({"kind": "课件", "uid": 123}, "2")
        self.assertNotEqual(a, b)
        self.assertEqual(a, "course:1:upload:123")

    def test_assign_scopes_index_by_course(self):
        idx = {}
        F.assign_canonical_paths(
            [{"kind": "课件", "activity": "A", "name": "x.pdf", "uid": 9}],
            idx, FakeArgs(out=self.tmp, layout="flat"), "77")
        self.assertIn("course:77:upload:9", idx)

    # ---- 索引 fail-closed ----

    def _corrupt(self):
        p = os.path.join(self.tmp, F.INDEX_NAME)
        with open(p, "w", encoding="utf-8") as f:
            f.write("{not json")
        return p

    def test_corrupt_index_fails_closed_and_preserves_evidence(self):
        self._corrupt()
        with self.assertRaises(F.IndexCorruptError):
            F.load_download_index(self.tmp)
        leftovers = [f for f in os.listdir(self.tmp)
                     if f.startswith(F.INDEX_NAME + ".corrupt-")]
        self.assertEqual(len(leftovers), 1, "坏文件必须改名保留现场")
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, F.INDEX_NAME)), "坏文件不能留在原位")

    def test_main_aborts_on_corrupt_index(self):
        """main() 层面：索引损坏 → 语义化退出码，绝不继续下载。"""
        out = os.path.join(self.tmp, "OUT")
        os.makedirs(out)
        with open(os.path.join(out, F.INDEX_NAME), "w", encoding="utf-8") as f:
            f.write("garbage")
        acts = [{"id": 1, "type": "lesson", "title": "第1章",
                 "uploads": [{"id": 100}]}]
        rc, buf = run_main(self.tmp, MainOpener(acts), ["--dry-run"])
        self.assertEqual(rc, F.RC_BAD_INDEX)
        self.assertIn("拒绝继续", buf)
        leftovers = [f for f in os.listdir(out)
                     if f.startswith(F.INDEX_NAME + ".corrupt-")]
        self.assertEqual(len(leftovers), 1)

    # ---- 索引路径越界防护 ----

    def _load_with(self, path_value):
        p = os.path.join(self.tmp, F.INDEX_NAME)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "identity_schema": 1,
                       "items": {"course:1:upload:1":
                                 {"path": path_value, "name": "x.pdf"}}}, f)
        return F.load_download_index(self.tmp)

    def test_escape_path_fails_closed(self):
        """★ 非法 canonical path 不能「丢单条继续」—— 那等于静默遗忘
        该身份的映射，重新分配又会去抢名字槽。必须整份 fail-closed。"""
        self.assertRaises(F.IndexCorruptError, self._load_with,
                          "../../important.txt")
        leftovers = [f for f in os.listdir(self.tmp)
                     if f.startswith(F.INDEX_NAME + ".corrupt-")]
        self.assertEqual(len(leftovers), 1, "原文件改名保留现场")

    def test_absolute_and_drive_paths_fail_closed(self):
        for bad in ("/etc/passwd", "C:/Windows/x.pdf", "\\\\srv/x.pdf"):
            self.assertRaises(F.IndexCorruptError, self._load_with, bad)

    def test_valid_relative_path_kept(self):
        idx = self._load_with("课件/第1章/讲义.pdf")
        self.assertEqual(idx["course:1:upload:1"]["path"],
                         "课件/第1章/讲义.pdf")

    def test_save_uses_items_envelope(self):
        """顶层必须是 {version, identity_schema, items} —— 结构版本与
        身份键语义版本分两个字段，以后迁移 identity 格式有余地。"""
        F.save_download_index(self.tmp, {"course:1:upload:1":
                                         {"path": "a.pdf", "name": "a.pdf"}})
        with open(os.path.join(self.tmp, F.INDEX_NAME), encoding="utf-8") as f:
            data = json.load(f)
        self.assertEqual(data["version"], F.INDEX_VERSION)
        self.assertEqual(data["identity_schema"], F.IDENTITY_SCHEMA)
        self.assertIsInstance(data["items"], dict)
        self.assertEqual(data["items"]["course:1:upload:1"]["path"], "a.pdf")

    def test_unknown_version_rejected(self):
        p = os.path.join(self.tmp, F.INDEX_NAME)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"version": 2, "identity_schema": 1, "items": {}}, f)
        self.assertRaises(F.IndexCorruptError, F.load_download_index, self.tmp)

    def test_unknown_identity_schema_rejected(self):
        p = os.path.join(self.tmp, F.INDEX_NAME)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "identity_schema": 2, "items": {}}, f)
        self.assertRaises(F.IndexCorruptError, F.load_download_index, self.tmp)

    def test_missing_identity_schema_rejected(self):
        """identity_schema 是身份键语义的声明，缺失 = 未知语义，拒绝。"""
        p = os.path.join(self.tmp, F.INDEX_NAME)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"version": 1, "items": {}}, f)
        self.assertRaises(F.IndexCorruptError, F.load_download_index, self.tmp)

    def test_refuse_leaves_file_byte_identical(self):
        """「格式不认识」类拒绝：原文件字节级原样保留——不改名、不修复。"""
        body = '{"version": 99, "identity_schema": 1, "items": {}}'
        p = os.path.join(self.tmp, F.INDEX_NAME)
        with open(p, "w", encoding="utf-8") as f:
            f.write(body)
        self.assertRaises(F.IndexCorruptError, F.load_download_index, self.tmp)
        with open(p, "rb") as f:
            self.assertEqual(f.read().decode("utf-8"), body,
                             "拒绝路径不得碰原文件")
        self.assertEqual([f for f in os.listdir(self.tmp)
                          if ".corrupt-" in f], [],
                         "格式不认识 ≠ 内容损坏，不做隔离改名")

    def test_structural_envelope_migration_ok(self):
        """结构型迁移（旧信封 entries → items）允许，前提是身份键语义
        已经是当前代（键带 course namespace）。"""
        p = os.path.join(self.tmp, F.INDEX_NAME)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"entries": {"course:1:upload:1":
                                   {"path": "a.pdf", "name": "a.pdf"}}}, f)
        idx = F.load_download_index(self.tmp)
        self.assertEqual(idx["course:1:upload:1"]["path"], "a.pdf")

    def test_identity_semantic_migration_never_happens(self):
        """★ 旧语义键（无 course namespace）→ 直接拒绝。loader 绝不猜
        course 归属去补前缀——那是身份语义推断，不是结构迁移。"""
        p = os.path.join(self.tmp, F.INDEX_NAME)
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"items": {"upload:5": {"path": "a.pdf",
                                              "name": "a.pdf"}}}, f)
        self.assertRaises(F.IndexCorruptError, F.load_download_index, self.tmp)
        self.assertFalse(os.path.exists(
            os.path.join(self.tmp, F.INDEX_NAME + ".part")))

    def test_main_aborts_on_invalid_path(self):
        """main() 层面：单条 path 越界 → RC_BAD_INDEX，下载零进行。"""
        out = os.path.join(self.tmp, "OUT")
        os.makedirs(out)
        with open(os.path.join(out, F.INDEX_NAME), "w", encoding="utf-8") as f:
            json.dump({"version": 1, "identity_schema": 1,
                       "items": {"course:1:upload:100":
                                 {"path": "../../x", "name": "x.pdf"}}}, f)
        acts = [{"id": 1, "type": "lesson", "title": "第1章",
                 "uploads": [{"id": 100}]}]
        rc, buf = run_main(self.tmp, MainOpener(acts), ["--dry-run"])
        self.assertEqual(rc, F.RC_BAD_INDEX)
        self.assertIn("拒绝继续", buf)
        leftovers = [f for f in os.listdir(out)
                     if f.startswith(F.INDEX_NAME + ".corrupt-")]
        self.assertEqual(len(leftovers), 1, "原文件保留现场")


class TestGuardInViews(Base):
    """守卫判定必须反映到清单视图（--list-only / --dry-run 共用逻辑），
    否则清单说 PLAN 某路径、实际下载却落到另一个路径。"""

    def _item(self, **kw):
        base = {"kind": "课件", "activity": "A", "name": "讲义.pdf",
                "uid": 200, "size": 10}
        base.update(kw)
        return base

    def test_row_shows_alt_path_when_foreign_file_present(self):
        d = os.path.join(self.tmp, "课件")
        os.makedirs(d)
        with open(os.path.join(d, "讲义.pdf"), "wb") as f:
            f.write(b"x" * 999)
        args = FakeArgs(out=self.tmp, layout="flat")
        rows = F.build_rows(None, [self._item()], None, args, quiet=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], F.STATUS_PLAN)
        self.assertEqual(rows[0]["name"], "讲义~200.pdf")

    def test_row_shows_exists_on_guard_path(self):
        d = os.path.join(self.tmp, "课件")
        os.makedirs(d)
        with open(os.path.join(d, "讲义.pdf"), "wb") as f:
            f.write(b"x" * 999)
        with open(os.path.join(d, "讲义~200.pdf"), "wb") as f:
            f.write(b"y" * 10)
        args = FakeArgs(out=self.tmp, layout="flat")
        rows = F.build_rows(None, [self._item()], None, args, quiet=True)
        self.assertEqual(rows[0]["status"], F.STATUS_EXISTS)
        self.assertEqual(rows[0]["name"], "讲义~200.pdf")


class TestMetaErrorPropagation(Base):
    """取元信息失败必须按类型分流 —— 不能把网络错误伪装成「无权限」。

    回归背景：旧 meta() 是 `except Exception: return {}`，调用方把空结果
    一律标成「平台侧无权限或已删除」跳过。于是 403/404/500/超时/DNS 失败/
    JSON 解不开 全被当成同一种「跳过」，一次接口抖动就能让整门课显示
    fail=0、退出码 0，用户以为全下完了。
    """

    def _op(self, script):
        return FakeOpener(script)

    def test_403_is_unavailable(self):
        op = self._op([http_err(403)])
        m, err = F.meta(op, 1)
        self.assertIsNone(m)
        self.assertEqual(err["kind"], F.ERR_UNAVAILABLE)

    def test_404_is_unavailable(self):
        op = self._op([http_err(404)])
        m, err = F.meta(op, 1)
        self.assertEqual(err["kind"], F.ERR_UNAVAILABLE)

    def test_401_is_auth(self):
        op = self._op([http_err(401)])
        m, err = F.meta(op, 1)
        self.assertEqual(err["kind"], F.ERR_AUTH)

    def test_500_is_transient(self):
        """5xx 是服务端临时故障，必须算获取失败，不能当「没有这个文件」。"""
        op = self._op([http_err(500)] * 3)
        m, err = F.meta(op, 1)
        self.assertEqual(err["kind"], F.ERR_TRANSIENT)

    def test_timeout_is_transient(self):
        op = self._op([socket.timeout("timed out")] * 3)
        m, err = F.meta(op, 1)
        self.assertEqual(err["kind"], F.ERR_TRANSIENT)

    def test_connection_error_is_transient(self):
        op = self._op([urllib.error.URLError("dns fail")] * 3)
        m, err = F.meta(op, 1)
        self.assertEqual(err["kind"], F.ERR_TRANSIENT)

    def test_invalid_json_is_transient(self):
        """返回 200 但 body 不是 JSON —— 也算获取失败。"""
        op = self._op([FakeResponse(b"<html>oops</html>")] * 3)
        m, err = F.meta(op, 1)
        self.assertEqual(err["kind"], F.ERR_TRANSIENT)

    def test_success_returns_info(self):
        body = json.dumps({"name": "a.pdf", "size": 5}).encode()
        op = self._op([FakeResponse(body)])
        m, err = F.meta(op, 1)
        self.assertIsNone(err)
        self.assertEqual(m["name"], "a.pdf")

    def test_classify_http_mapping(self):
        self.assertEqual(F.classify_http(403)["kind"], F.ERR_UNAVAILABLE)
        self.assertEqual(F.classify_http(404)["kind"], F.ERR_UNAVAILABLE)
        self.assertEqual(F.classify_http(401)["kind"], F.ERR_AUTH)
        for code in (500, 502, 503, 429):
            self.assertEqual(F.classify_http(code)["kind"], F.ERR_TRANSIENT)

    def test_expand_marks_unavailable_vs_error(self):
        """expand_items 要区分「确实拿不到」和「这次获取失败」。"""
        keys = [("课件", "第1章", 1), ("课件", "第1章", 2)]
        op = self._op([http_err(403)] + [http_err(500)] * 3)
        items, _ = F.expand_items(op, keys, FakeArgs())
        self.assertTrue(items[0]["unavailable"], "403 应标 unavailable")
        self.assertTrue(items[1]["error"])
        self.assertFalse(items[1]["unavailable"], "500 不该被当成不可达")
        self.assertEqual(items[1]["err_kind"], F.ERR_TRANSIENT)


# ---------------------------------------------------------------- 扫描阶段

class TestCollectScanFailure(Base):
    """collect() 的扫描失败必须记账 —— 这是最后一个 silent failure。

    回归背景：扫描 page 正文 / lecture_live 详情时是 `except Exception:
    print(…); continue`。一个 page 活动连着 500/500/timeout 时，它正文里的
    5 个 PDF 既不进 plan、也不进 fail、也不在清单里出现，程序照样 exit 0 ——
    「扫描失败」被当成「这门课本来就没有资源」。
    """

    class _Op:
        """按 url 分发：列表请求返回活动，详情请求按 aid 吐结果。

        details 的值可以是 dict（正常详情）、Exception（直接抛）、
        或 bytes（原样当响应体，用来模拟「返回 HTML 而不是 JSON」）。
        """

        def __init__(self, acts, details):
            self.acts = acts
            self.details = details

        def open(self, url, timeout=None, data=None):
            url = req_url(url)
            if "/activities?" in url:
                payload = {"activities": self.acts}
            else:
                aid = int(url.split("/activities/")[1].split("?")[0])
                item = self.details.get(aid, {})
                if isinstance(item, Exception):
                    raise item
                if isinstance(item, bytes):
                    return FakeResponse(item)
                payload = item
            raw = json.dumps(payload).encode("utf-8")
            return FakeResponse(raw)

    def _collect(self, acts, details, want_video=True):
        import contextlib
        op = self._Op(acts, details)
        buf = io.StringIO()
        old_sleep = F.time.sleep
        F.time.sleep = lambda _s: None       # 别让退避把测试拖慢
        try:
            with contextlib.redirect_stdout(buf):
                with contextlib.redirect_stderr(buf):
                    keys, _n1, _n2, errs = F.collect(op, "1", acts,
                                                     want_video=want_video)
        finally:
            F.time.sleep = old_sleep
        return keys, errs

    def _page_acts(self):
        return [{"id": 5, "type": "page", "title": "第1章", "uploads": None}]

    def test_page_detail_500_is_scan_failure(self):
        """★ 核心回归：page 详情反复 500 → 必须记为失败，不能静默跳过。"""
        acts = self._page_acts()
        keys, errs = self._collect(acts, {5: http_err(500)})
        self.assertEqual(keys, [], "详情没取到，正文里的附件自然也没发现")
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["stage"], "page_detail")
        self.assertEqual(errs[0]["activity_id"], 5)
        self.assertEqual(errs[0]["kind"], F.ERR_TRANSIENT)

    def test_page_detail_timeout_is_scan_failure(self):
        acts = self._page_acts()
        keys, errs = self._collect(acts, {5: socket.timeout("timed out")})
        self.assertEqual(keys, [])
        self.assertEqual(errs[0]["kind"], F.ERR_TRANSIENT)
        self.assertIn("timeout", errs[0]["error"].lower())

    def test_page_detail_bad_json_is_scan_failure(self):
        """返回 200 但 body 不是 JSON —— 同样算扫描失败。"""
        acts = self._page_acts()
        keys, errs = self._collect(acts, {5: b"<html>oops</html>"})
        self.assertEqual(keys, [])
        self.assertEqual(errs[0]["kind"], F.ERR_TRANSIENT)

    def test_page_detail_403_is_unavailable(self):
        """403 / 404 是平台确实不给 —— 按既定规则处理，不算失败。"""
        for code in (403, 404):
            acts = self._page_acts()
            keys, errs = self._collect(acts, {5: http_err(code)})
            self.assertEqual(keys, [])
            self.assertEqual(len(errs), 1)
            self.assertEqual(errs[0]["kind"], F.ERR_UNAVAILABLE)

    def test_page_detail_401_is_auth_failure(self):
        acts = self._page_acts()
        _keys, errs = self._collect(acts, {5: http_err(401)})
        self.assertEqual(errs[0]["kind"], F.ERR_AUTH)

    def test_page_detail_ok_has_no_error(self):
        """详情正常时不该凭空产生扫描错误。"""
        acts = self._page_acts()
        detail = {"data": {"content": '<a href="/api/uploads/8811">'}}
        keys, errs = self._collect(acts, {5: detail})
        self.assertEqual(errs, [])
        self.assertEqual(keys, [("课件", "第1章", 8811)])

    def test_live_detail_500_is_scan_failure(self):
        acts = [{"id": 777, "type": "lecture_live", "title": "9-19 第一场",
                 "uploads": []}]
        keys, errs = self._collect(acts, {777: http_err(500)})
        self.assertEqual(keys, [])
        self.assertEqual(len(errs), 1)
        self.assertEqual(errs[0]["stage"], "lecture_live_detail")
        self.assertEqual(errs[0]["activity_id"], 777)
        self.assertEqual(errs[0]["kind"], F.ERR_TRANSIENT)

    def test_live_detail_timeout_is_scan_failure(self):
        acts = [{"id": 777, "type": "lecture_live", "title": "L", "uploads": []}]
        keys, errs = self._collect(acts, {777: socket.timeout("timed out")})
        self.assertEqual(keys, [])
        self.assertEqual(errs[0]["stage"], "lecture_live_detail")
        self.assertEqual(errs[0]["kind"], F.ERR_TRANSIENT)

    def test_live_detail_401_is_auth_failure(self):
        acts = [{"id": 777, "type": "lecture_live", "title": "L", "uploads": []}]
        _keys, errs = self._collect(acts, {777: http_err(401)})
        self.assertEqual(errs[0]["stage"], "lecture_live_detail")
        self.assertEqual(errs[0]["kind"], F.ERR_AUTH)


class TestScanErrorItems(Base):
    """扫描错误转成的清单条目 —— 决定它会不会进 fail / manifest / 退出码。"""

    def _items_for(self, stage, kind_err):
        errs = [{"stage": stage, "activity_id": 123, "activity": "第1章",
                 "kind": kind_err, "error": "HTTP 500"}]
        return F.scan_error_items(errs)

    def test_transient_scan_error_is_fail_item(self):
        items = self._items_for("page_detail", F.ERR_TRANSIENT)
        self.assertEqual(len(items), 1)
        it = items[0]
        self.assertTrue(it["error"])
        self.assertFalse(it["unavailable"])
        self.assertEqual(F.item_status(it), F.STATUS_FAIL)
        row = F.error_row(1, it)
        self.assertEqual(row["status"], "fail")
        self.assertEqual(row["err_kind"], F.ERR_TRANSIENT)
        self.assertIn("扫描失败", row["error"])

    def test_unavailable_scan_error_is_na_item(self):
        items = self._items_for("page_detail", F.ERR_UNAVAILABLE)
        it = items[0]
        self.assertTrue(it["unavailable"])
        self.assertEqual(F.item_status(it), F.STATUS_NA)

    def test_live_stage_maps_to_replay_kind(self):
        items = self._items_for("lecture_live_detail", F.ERR_TRANSIENT)
        self.assertEqual(items[0]["kind"], "回放")

    def test_scan_items_never_take_part_in_collisions(self):
        """扫描失败的条目不占位，否则会挤掉真实的同名文件。"""
        items = self._items_for("page_detail", F.ERR_TRANSIENT)
        scanit = items[0]
        scanit["name"] = "x.pdf"
        plan = [{"kind": "课件", "activity": "第1章", "name": "x.pdf",
                 "uid": 1}] + items
        _, n = F.resolve_collisions(plan, FakeArgs())
        self.assertEqual(n, 0)


# ---------------------------------------------------------------- 状态语义

class TestItemStatus(Base):
    """三种模式共用的错误分类 —— 以前 --list-only 把所有错误拍成 N/A。"""

    def test_unavailable_is_na(self):
        for code in (403, 404):
            it = {"err_kind": F.classify_http(code)["kind"]}
            self.assertEqual(F.item_status(it), "na")

    def test_auth_is_fail(self):
        it = {"err_kind": F.ERR_AUTH}
        self.assertEqual(F.item_status(it), "fail")

    def test_transient_is_fail(self):
        it = {"err_kind": F.ERR_TRANSIENT}
        self.assertEqual(F.item_status(it), "fail")

    def test_missing_kind_is_fail(self):
        """出错条目没有 err_kind 时判 FAIL —— 宁可误报也不静默吞掉。"""
        self.assertEqual(F.item_status({"error": True}), "fail")

    def test_count_fail_ignores_na(self):
        rows = [{"status": "fail"}, {"status": "na"}, {"status": "ok"},
                {"status": "fail"}]
        self.assertEqual(F.count_fail(rows), 2)

    def test_error_row_keeps_diagnostics(self):
        it = {"kind": "课件", "activity": "第1章", "uid": 9,
              "err_kind": F.ERR_TRANSIENT, "err_msg": "HTTP 500"}
        row = F.error_row(3, it)
        self.assertEqual(row["i"], 3)
        self.assertEqual(row["status"], "fail")
        self.assertEqual(row["err_kind"], F.ERR_TRANSIENT)
        self.assertEqual(row["error"], "HTTP 500")


def _make_state(tmp):
    """给 main() 用的一小份登录态。"""
    p = os.path.join(tmp, "st.json")
    with open(p, "w", encoding="utf-8") as f:
        json.dump({"version": 1, "host": "x.edu.cn",
                   "cookies": [{"name": "sid", "value": "1",
                                "domain": "x.edu.cn"}]}, f)
    return p


class MainOpener:
    """给 main() 用的假 opener：活动列表 / 活动详情 / upload 元信息三种请求。

    details / uploads 的值可以是 dict（正常响应）、Exception（直接抛）、
    或 bytes（原样 body，模拟「返回 HTML 而不是 JSON」）。
    """

    def __init__(self, acts, details=None, uploads=None):
        self.acts = acts
        self.details = details or {}
        self.uploads = uploads or {}

    def open(self, url, timeout=None, data=None):
        url = req_url(url)
        if "/activities?" in url:
            return FakeResponse(json.dumps({"activities": self.acts}).encode())
        if "/uploads/" in url:
            uid = int(url.split("/uploads/")[1].split("?")[0])
            item = self.uploads.get(uid, {"name": "u%d.pdf" % uid, "size": 10})
            if isinstance(item, Exception):
                raise item
            return FakeResponse(json.dumps(item).encode())
        aid = int(url.split("/activities/")[1].split("?")[0])
        item = self.details.get(aid, {"data": {}})
        if isinstance(item, Exception):
            raise item
        if isinstance(item, bytes):
            return FakeResponse(item)
        return FakeResponse(json.dumps(item).encode())


def run_main(tmp, op, argv_tail):
    """真跑一次 main()，返回 (退出码, 全部输出)。"""
    import contextlib
    old_open, old_sleep = F.opener, F.time.sleep
    old_base, old_host = F.BASE, F.HOST
    F.opener = lambda _p: op
    F.time.sleep = lambda _s: None
    out = os.path.join(tmp, "OUT")
    sys.argv = ["lms_fetch", "--course", "1", "--out", out,
                "--state", _make_state(tmp), "--base", "https://x.edu.cn",
                "--no-verify"] + argv_tail
    buf = io.StringIO()
    try:
        with contextlib.redirect_stdout(buf):
            with contextlib.redirect_stderr(buf):
                rc = F.main()
    finally:
        F.opener = old_open
        F.time.sleep = old_sleep
        F.BASE, F.HOST = old_base, old_host      # main() 会改这两个全局
    return rc, buf.getvalue()


class TestListOnly(Base):
    """--list-only 的退出码与状态 —— 以前无论接口怎么炸都固定返回 0。"""

    ACTS = [{"id": 5, "type": "page", "title": "第1章", "uploads": None},
            {"id": 6, "type": "lesson", "title": "第2章",
             "uploads": [{"id": 900}]}]

    def _run(self, upload_result):
        out = os.path.join(self.tmp, "m.json")
        rc, buf = run_main(self.tmp, MainOpener(self.ACTS,
                                                uploads={900: upload_result}),
                           ["--list-only", out])
        with open(out, encoding="utf-8") as f:
            rows = json.load(f)["files"]
        return rc, rows, buf

    def test_403_is_na_and_exit_0(self):
        rc, rows, _out = self._run(http_err(403))
        self.assertEqual(rc, F.RC_OK, "403 是平台没给，不算失败")
        self.assertEqual(rows[0]["status"], "na")
        self.assertEqual(rows[0]["err_kind"], F.ERR_UNAVAILABLE)

    def test_404_is_na_and_exit_0(self):
        rc, rows, _out = self._run(http_err(404))
        self.assertEqual(rc, F.RC_OK)
        self.assertEqual(rows[0]["status"], "na")

    def test_500_is_fail_and_exit_partial(self):
        """★ 核心回归：接口 500 时 --list-only 不能报成功。"""
        rc, rows, _out = self._run(http_err(500))
        self.assertEqual(rc, F.RC_PARTIAL, "500 必须让退出码非 0")
        self.assertEqual(rows[0]["status"], "fail")
        self.assertEqual(rows[0]["err_kind"], F.ERR_TRANSIENT)

    def test_timeout_is_fail_and_exit_partial(self):
        rc, rows, _out = self._run(socket.timeout("timed out"))
        self.assertEqual(rc, F.RC_PARTIAL)
        self.assertEqual(rows[0]["status"], "fail")

    def test_401_is_fail_and_exit_partial(self):
        rc, rows, out = self._run(http_err(401))
        self.assertEqual(rc, F.RC_PARTIAL)
        self.assertEqual(rows[0]["status"], "fail")
        self.assertEqual(rows[0]["err_kind"], F.ERR_AUTH)
        self.assertIn("重新登录", out, "401 要给出重新登录的提示")

    def test_ok_is_plan_and_exit_0(self):
        rc, rows, _out = self._run({"name": "a.pdf", "size": 10})
        self.assertEqual(rc, F.RC_OK)
        self.assertEqual(rows[0]["status"], "plan")

    def test_truncated_local_file_is_not_exists(self):
        """★ 清单里的 exists 必须与主流程同一个判据（already_complete）。

        以前 build_rows 只判 os.path.exists，于是本地截断文件在清单里是
        exists、在主流程里却要 REDO —— 两种视图对不上。

        v1.4.2 身份索引落地后的语义：uid900 的 canonical_path 在索引里
        （第一次 --list-only 就已分配），本地截断文件视为「同 uid 的旧内容」
        —— 清单仍是 plan（不是 exists），但路径不漂移，正式下载原地覆盖。
        """
        _, rows, _out = self._run({"name": "第2章讲义.pdf", "size": 100000})
        target = rows[0]
        dest = F.dest_for(target["kind"], target["activity"],
                          target["name"],
                          FakeArgs(out=os.path.join(self.tmp, "OUT")))
        path = os.path.join(dest, target["name"])
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(b"x" * 1000)                # 明显截断

        rc, rows2, _out2 = self._run({"name": "第2章讲义.pdf", "size": 100000})
        self.assertEqual(rc, F.RC_OK)
        row = [r for r in rows2 if r.get("name") == "第2章讲义.pdf"][0]
        self.assertNotEqual(row["status"], "exists",
                            "截断文件不能被清单当成已存在")
        self.assertEqual(row["status"], "plan")
        self.assertEqual(row["name"], "第2章讲义.pdf",
                         "索引权威：同 uid 更新原地覆盖，路径不漂移")
        with open(path, "rb") as f:
            self.assertEqual(len(f.read()), 1000, "--list-only 不改任何文件")


class TestMainScanFailure(Base):
    """★ 最关键的一条：扫描失败不能让程序 exit 0。

    场景：一个 page 活动的详情接口连着失败，正文里其实挂着 5 个 PDF。
    以前的结果是「打印一行详情失败 → continue → 最后 fail=0、exit 0」，
    资源没被发现被当成了「这门课没有资源」。
    """

    ACTS = [{"id": 5, "type": "page", "title": "第1章 绪论", "uploads": None}]

    def _run(self, page_result, tail):
        return run_main(self.tmp, MainOpener(self.ACTS, details={5: page_result}),
                        tail)

    def test_dry_run_transient_scan_error_exits_partial(self):
        rc, out = self._run(http_err(500), ["--dry-run"])
        self.assertEqual(rc, F.RC_PARTIAL,
                         "扫描阶段出错必须让退出码非 0")
        self.assertIn("fail=1", out)
        self.assertIn("扫描阶段", out)

    def test_dry_run_timeout_scan_error_exits_partial(self):
        rc, out = self._run(socket.timeout("timed out"), ["--dry-run"])
        self.assertEqual(rc, F.RC_PARTIAL)
        self.assertIn("fail=1", out)

    def test_dry_run_401_scan_error_exits_partial_with_hint(self):
        rc, out = self._run(http_err(401), ["--dry-run"])
        self.assertEqual(rc, F.RC_PARTIAL)
        self.assertIn("重新登录", out, "401 要给出重新登录的提示")

    def test_dry_run_404_scan_error_is_na(self):
        """404 是平台确实没给 —— 记一笔 N/A，但不算失败。"""
        rc, out = self._run(http_err(404), ["--dry-run"])
        self.assertEqual(rc, F.RC_OK, "404 不该让退出码变非 0")
        self.assertIn("扫描阶段", out)
        self.assertIn("N/A", out)
        self.assertIn("fail=0", out)

    def test_scan_error_lands_in_manifest(self):
        """扫描失败必须写进 manifest，否则事后无从核对。"""
        mf = os.path.join(self.tmp, "m.csv")
        rc, _out = self._run(http_err(500), ["--dry-run", "--manifest", mf])
        self.assertEqual(rc, F.RC_PARTIAL)
        with open(mf, encoding="utf-8-sig") as f:
            body = f.read()
        self.assertIn("fail", body)
        self.assertIn("transient", body, "CSV 必须带上 err_kind")
        self.assertIn("page_detail", body, "CSV 必须带上出错的阶段")

        mj = os.path.join(self.tmp, "m.json")
        rc, _out = self._run(http_err(500),
                             ["--dry-run", "--manifest", mj])
        self.assertEqual(rc, F.RC_PARTIAL)
        with open(mj, encoding="utf-8") as f:
            rows = json.load(f)["files"]
        self.assertEqual(rows[0]["status"], "fail")
        self.assertEqual(rows[0]["err_kind"], F.ERR_TRANSIENT)
        self.assertEqual(rows[0]["stage"], "page_detail")

    def test_scan_ok_still_exits_zero(self):
        """回归保护：详情正常时不能凭空报失败。"""
        detail = {"data": {"content": '<a href="/api/uploads/8811">'}}
        rc, out = self._run(detail, ["--dry-run"])
        self.assertEqual(rc, F.RC_OK, out)
        self.assertNotIn("扫描阶段", out)


class TestNoTolerancePolicy(Base):
    """★ v1.4.2 起全局无容差 —— 完成判据收敛到「可信 size 精确相等」。

    回归背景（两个阶段）：
      - v1.4.1 为了修「上一轮判成功、下一轮判要重下」，给回放加了 8% 短读
        容差 —— 这等于留下一条「尺寸差不多就相信」的旁路。
      - Round 12 删掉它：下载侧的完成真值改为「实得字节 == 经稳定窗口确认的
        远端 size」（lms_live.verify_tail），确认值记进 `.download-index.json`，
        增量判据与它精确比对；没有可信 size 的存量文件用本轮远端探测值精确比对。
    所以 `already_complete` 不再接受 tolerance，`complete_tolerance()` 与
    `lms_live.SHORT_TOLERANCE` 一并删除。
    """

    def _file(self, n):
        p = os.path.join(self.tmp, "f.bin")
        with open(p, "wb") as f:
            f.write(b"x" * n)
        return p

    def test_exact_size_is_complete(self):
        ok, local, exp = F.already_complete(self._file(1000), 1000)
        self.assertTrue(ok)
        self.assertEqual((local, exp), (1000, 1000))

    def test_attachment_needs_exact_size(self):
        ok, local, exp = F.already_complete(self._file(944), 1000)
        self.assertFalse(ok, "944/1000 必须判为不完整")
        self.assertEqual(local, 944)
        self.assertEqual(exp, 1000)

    def test_replay_short_read_is_incomplete_too(self):
        """回放（944000/1000000 少 5.6%）同样不完整 —— 旧容差已删除。"""
        ok, _l, _e = F.already_complete(self._file(944000), 1000000)
        self.assertFalse(ok, "回放不该再有任何短读容差")

    def test_local_larger_than_expected_is_incomplete(self):
        ok, _l, exp = F.already_complete(self._file(1200), 1000)
        self.assertFalse(ok, "比声明还大 → 来源可疑，必须重下")
        self.assertEqual(exp, 1000)

    def test_getsize_oserror_stays_conservative(self):
        import unittest.mock as mock
        p = self._file(999)
        with mock.patch("os.path.getsize", side_effect=OSError(121, "semaphore")):
            ok, local, exp = F.already_complete(p, 1000)
        self.assertFalse(ok, "stat 失败不能走向「已完成」")
        self.assertEqual(local, 0)
        self.assertEqual(exp, 1000)

    def test_tolerance_knobs_are_gone(self):
        """★ 结构保证：容差入口整个消失，不给后人留后门。"""
        import lms_live
        self.assertFalse(hasattr(lms_live, "SHORT_TOLERANCE"))
        self.assertFalse(hasattr(F, "complete_tolerance"))
        self.assertNotIn("tolerance", F.already_complete.__code__.co_varnames)

    # ---- 可信 size（索引确认值）才是增量判据的真值 ----

    def test_indexed_size_read_from_confirmed_entry(self):
        idx = {"course:1:live:-9:camera:1": {"path": "回放/a.mp4",
                                            "name": "a.mp4", "size": 500}}
        it = {"kind": "回放", "activity": "第1章", "name": "a.mp4", "uid": -9,
              "camera_id": 1, "size": 999999}
        self.assertEqual(F.indexed_size(it, idx, 1), 500)

    def test_indexed_size_absent_when_entry_has_no_size(self):
        """老索引 / 守卫分流留下的条目只有 path+name → 无可信 size。"""
        idx = {"course:1:live:-9:camera:1": {"path": "回放/a.mp4",
                                            "name": "a.mp4"}}
        it = {"kind": "回放", "activity": "第1章", "name": "a.mp4", "uid": -9,
              "camera_id": 1, "size": 999}
        self.assertIsNone(F.indexed_size(it, idx, 1))

    def test_indexed_size_absent_without_entry_or_index(self):
        it = {"kind": "回放", "activity": "A", "name": "a.mp4", "uid": -9,
              "camera_id": 1}
        self.assertIsNone(F.indexed_size(it, None, 1))
        self.assertIsNone(F.indexed_size(it, {}, 1))
        self.assertIsNone(F.indexed_size(
            it, {"course:1:live:-9:camera:1": {"size": 0}}, 1),
            "0 不是可信 size")

    def test_manifest_view_prefers_trusted_size(self):
        """清单视图与下载主流程同判据：回放优先用索引确认的 size。

        场景：响应头声明 999999（转码后期才长到那么大），索引里确认值是
        5000，本地正好 5000 字节 —— 必须显示 exists，而不是又判要重下。
        没有可信 size 时（老索引 / 首次）才退回本轮探测值精确比对。
        """
        out = os.path.join(self.tmp, "OUT")
        d = os.path.join(out, "回放")
        os.makedirs(d)
        with open(os.path.join(d, "a.mp4"), "wb") as f:
            f.write(b"x" * 5000)
        args = FakeArgs(out=out, layout="flat", course="1")
        it = {"kind": "回放", "activity": "第1章", "name": "a.mp4", "uid": -9,
              "camera_id": 1, "camera": "encoder", "size": 999999}
        index = {"course:1:live:-9:camera:1": {"path": "回放/a.mp4",
                                              "name": "a.mp4", "size": 5000}}
        rows = F.build_rows(None, [it], None, args, quiet=True, index=index)
        self.assertEqual(rows[0]["status"], F.STATUS_EXISTS)
        self.assertEqual(rows[0]["size"], 5000)
        # 老索引（无可信 size）→ 用声明值精确比对 → 大小对不上，要重下
        rows2 = F.build_rows(None, [it], None, args, quiet=True, index={})
        self.assertEqual(rows2[0]["status"], F.STATUS_PLAN)


class TestSevenZipCollision(Base):
    """`.7z` 撞路径时不能把扩展名改坏。

    回归背景：`split_ext()` 要求扩展名以字母开头，`.7z` 不满足，
    于是 `Project 2.7z` 被整体当成主干，冲突改名得到 `Project 2.7z~123`：
    扩展名坏了、`is_project_pkg()` 认不出项目包、`dest_for()` 从 项目/
    掉回 作业/。
    """

    def test_split_ext_recognizes_7z(self):
        self.assertEqual(F.split_ext("Project 2.7z"), ("Project 2", ".7z"))
        self.assertEqual(F.split_ext("a.rar"), ("a", ".rar"))

    def test_split_ext_still_rejects_version_tails(self):
        """修复不能过度：`第1.2节讲义` / `3.5 英寸` 仍不该被切。"""
        self.assertEqual(F.split_ext("第1.2节讲义"), ("第1.2节讲义", ""))
        self.assertEqual(F.split_ext("报告 v1.0"), ("报告 v1.0", ""))

    def test_two_project_archives_keep_ext_and_dir(self):
        """★ 核心回归：两个同名 `Project 2.7z` 都要保住 .7z 和 项目/。"""
        args = FakeArgs(split_projects=True)
        items = [
            {"kind": "作业", "activity": "大作业", "name": "Project 2.7z",
             "uid": 101, "size": 10},
            {"kind": "作业", "activity": "大作业", "name": "Project 2.7z",
             "uid": 102, "size": 10},
        ]
        got, n = F.resolve_collisions(items, args)
        self.assertEqual(n, 1)
        names = [it["name"] for it in got]
        self.assertEqual(len(set(names)), 2, "两个条目必须是两个不同文件名")
        for nm in names:
            self.assertTrue(nm.endswith(".7z"), "扩展名被改坏了: %r" % nm)
            self.assertTrue(F.is_project_pkg(nm),
                            "改名后认不出是项目包: %r" % nm)
            d = F.dest_for("作业", "大作业", nm, args)
            self.assertEqual(d, os.path.join("OUT", "项目", "大作业"),
                             "改名后落盘目录变了: %r" % d)

    def test_exact_expected_name(self):
        items = [
            {"kind": "作业", "activity": "A", "name": "Project 2.7z", "uid": 1},
            {"kind": "作业", "activity": "A", "name": "Project 2.7z", "uid": 123},
        ]
        got, _n = F.resolve_collisions(items, FakeArgs(split_projects=True))
        self.assertEqual(got[0]["name"], "Project 2.7z")
        self.assertEqual(got[1]["name"], "Project 2~123.7z")

    def test_other_archive_exts_survive(self):
        """顺带确认几种常见扩展名在改名后都还在。"""
        for nm, uid, tail in (("a.zip", 7, ".zip"), ("b.rar", 8, ".rar"),
                              ("c.pdf", 9, ".pdf"), ("d.pptx", 10, ".pptx"),
                              ("e.docx", 11, ".docx"), ("f.mp4", 12, ".mp4")):
            items = [{"kind": "课件", "activity": "A", "name": nm, "uid": 1},
                     {"kind": "课件", "activity": "A", "name": nm, "uid": uid}]
            got, _n = F.resolve_collisions(items, FakeArgs())
            self.assertTrue(got[1]["name"].endswith(tail),
                            "%s 改名后丢了扩展名: %r" % (nm, got[1]["name"]))


# ---------------------------------------------------------------- --exclude

REPLAY_MEDIA = "https://rms.test/captures/c1/videos/1/preview"
REPLAY_NAME = "第1章-20260919-1430-encoder.mp4"


class ReplayMainOpener:
    """能跑通「回放」全流程的假 opener，并**逐条记录媒体请求**。

    用途：验证被 `--exclude` 排除的条目是否真的一次媒体请求都没发。

    - `/api/courses/<id>/activities?` → 活动列表
    - `/api/activities/<id>`          → 活动详情（含 replay_videos）
    - 媒体 URL（rms.test 域）          → 探针回响应头 / 下载回 body

    media_error 非 None 时，任何媒体请求都抛它（模拟 502 / 403 / 超时）。
    """

    MEDIA_HOST = "rms.test"

    def __init__(self, acts, details, media_size=4096, media_error=None):
        self.acts = acts
        self.details = details
        self.media_size = media_size
        self.media_error = media_error
        self.media_requests = []          # ★ 媒体请求逐条记账
        self.detail_requests = []         # 活动详情请求
        self.body = b"x" * media_size

    def open(self, url, timeout=None, data=None):
        u = req_url(url)
        if "/activities?" in u:
            return FakeResponse(json.dumps({"activities": self.acts}).encode())
        if self.MEDIA_HOST in u:
            self.media_requests.append(u)
            if self.media_error is not None:
                raise self.media_error
            hd = {k.lower(): v for k, v in
                  (getattr(url, "headers", None) or {}).items()}
            if hd.get("range") == "bytes=0-0":
                return FakeResponse(b"x", status=206, headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": "bytes 0-0/%d" % self.media_size,
                    "ETag": '"v1"'})
            return FakeResponse(self.body, status=206, headers={
                "Content-Type": "video/mp4",
                "Content-Range": "bytes 0-%d/%d"
                                 % (len(self.body) - 1, self.media_size)})
        self.detail_requests.append(u)
        aid = int(u.split("/activities/")[1].split("?")[0])
        item = self.details.get(aid, {"data": {}})
        if isinstance(item, Exception):
            raise item
        return FakeResponse(json.dumps(item).encode())


def replay_fixture(url=None):
    """一个 lecture_live 活动 + 一路 encoder 机位。

    最终文件名（safe_name）= `第1章-20260919-1430-encoder.mp4`。
    """
    acts = [{"id": 777, "type": "lecture_live", "title": "第1章"}]
    det = {777: {
        "start_time": "2026-09-19T14:30:00+08:00",
        "data": {"external_live_detail": {"replay_videos": [
            {"camera_id": "c1", "camera_type": "encoder",
             "url": url or (REPLAY_MEDIA + "?previewToken=FAKE_TOKEN")}]}},
    }}
    return acts, det


class TestExcludePrecedence(Base):
    """★ `--exclude` 必须在任何远端探测 / URL 解析 / 稳定窗口之前生效。

    回归背景：`expand_items()` 对回放**无条件 probe**，被排除的条目也照探；
    而 `build_rows()` / 下载循环都**先判 error 再判 exclude** —— 于是一个
    用户明确排除的条目，只因为它的 URL 恰好 502，就把整轮判成 fail、
    退出码 4。实测踩到过（58 门课扫一遍时，一个被排除条目 502 触发 exit 4）。
    """

    def _items(self, op, excl, cache=None):
        keys = [("回放", "第1章", -777)]
        args = FakeArgs(all_cameras=False)
        return F.expand_items(op, keys, args, excl=excl,
                              detail_cache=cache)[0]

    def test_excluded_replay_is_never_probed(self):
        """被排除的回放：一个媒体请求都不发，且不被标成 error。"""
        acts, det = replay_fixture()
        op = ReplayMainOpener(acts, det)
        items = self._items(op, re.compile("1430-encoder"))
        self.assertEqual(op.media_requests, [], "被排除条目不得发媒体请求")
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].get("excluded"), items[0])
        self.assertFalse(items[0].get("error"), "不得标成 error")

    def test_not_excluded_still_probes(self):
        """对照组：不排除时照常探测 —— 证明上一条不是因为压根没扫到。"""
        acts, det = replay_fixture()
        op = ReplayMainOpener(acts, det)
        items = self._items(op, None)
        self.assertTrue(op.media_requests, "未排除的条目必须正常探测")
        self.assertFalse(items[0].get("excluded"))
        self.assertEqual(items[0]["size"], 4096)

    def test_exclude_matching_semantics_unchanged(self):
        """匹配语义不动：仍是「对最终文件名做 re.search」。

        用真实文件名 `第1章-20260919-1430-encoder.mp4` 验证：
        能匹配子串的命中，匹配不上的不命中。
        """
        acts, det = replay_fixture()
        hit = self._items(ReplayMainOpener(acts, det), re.compile("1430"))
        self.assertTrue(hit[0].get("excluded"), "子串命中应被排除")

        op2 = ReplayMainOpener(acts, det)
        miss = self._items(op2, re.compile("20260919-1530"))
        self.assertFalse(miss[0].get("excluded"), "匹配不上就不该被排除")
        self.assertTrue(op2.media_requests)

    def test_excluded_flag_beats_error_in_build_rows(self):
        """不变量：带 error 的条目只要被标记 excluded，就绝不能报 fail。

        （当前实现下这条路径已不可达 —— expand 不会再探测被排除条目；
        但清单视图必须自己守住「排除优先于错误」，不能依赖上游顺序。）
        """
        args = FakeArgs(out=self.tmp, layout="flat")
        items = [{"kind": "回放", "activity": "第1章", "uid": 777,
                  "name": REPLAY_NAME, "excluded": True, "error": True,
                  "err_kind": F.ERR_TRANSIENT, "err_msg": "探测失败"}]
        rows = F.build_rows(None, items, re.compile("1430"), args,
                            quiet=True, index={})
        self.assertEqual(rows[0]["status"], F.STATUS_EXCLUDED)
        self.assertEqual(F.count_fail(rows), 0)

    def test_lecture_live_detail_fetched_once(self):
        """lecture_live 详情只取一次：collect() 取到的要复用给 expand_items()。

        以前 collect() 为了枚举机位取一次、expand_items() 又取一次 —— 同一份
        详情两次请求，失败面翻倍，被排除条目也无谓地多一次请求。
        """
        acts, det = replay_fixture()
        op = ReplayMainOpener(acts, det)
        cache = {}
        F.collect(op, "1", activities=acts, detail_cache=cache)
        self.assertEqual(len(op.detail_requests), 1)
        self._items(op, None, cache=cache)
        self.assertEqual(len(op.detail_requests), 1, "详情被取了第二次")

    def test_excluded_replay_with_dead_url_keeps_rc_ok(self):
        """★ 点名场景：被排除条目的 URL 会 502 → 整轮仍 rc=0。

        对照组（同一份 URL、同样 502，但不排除）必须 rc=4 ——
        证明这条断言不是在「反正没事」的空场景里通过的。
        """
        err = urllib.error.HTTPError(REPLAY_MEDIA, 502, "Bad Gateway", {}, None)

        acts, det = replay_fixture()
        op = ReplayMainOpener(acts, det, media_error=err)
        rc, out = run_main(self.tmp, op,
                           ["--exclude", "1430-encoder",
                            "--manifest", os.path.join(self.tmp, "m1.json")])
        self.assertEqual(rc, F.RC_OK, out[-1200:])
        self.assertEqual(op.media_requests, [], "被排除条目不得发媒体请求")
        with open(os.path.join(self.tmp, "m1.json"), encoding="utf-8") as f:
            rows = json.load(f)["files"]
        self.assertEqual([r["status"] for r in rows], [F.STATUS_EXCLUDED])

        acts2, det2 = replay_fixture()
        op2 = ReplayMainOpener(acts2, det2, media_error=err)
        rc2, out2 = run_main(self.tmp, op2, ["--manifest",
                                             os.path.join(self.tmp, "m2.json")])
        self.assertEqual(rc2, F.RC_PARTIAL,
                         "不排除时 502 必须如实计入失败：%s" % out2[-600:])
        self.assertTrue(op2.media_requests)

    def test_excluded_replay_timeout_keeps_rc_ok(self):
        """超时同理：排除项的网络状态不能影响退出码。"""
        acts, det = replay_fixture()
        op = ReplayMainOpener(acts, det, media_error=socket.timeout("t"))
        rc, out = run_main(self.tmp, op, ["--exclude", "1430-encoder"])
        self.assertEqual(rc, F.RC_OK, out[-1200:])
        self.assertEqual(op.media_requests, [])

    def test_excluded_replay_403_keeps_rc_ok(self):
        """403 同理（不能因为「不可达」被记成 N/A 或 fail）。"""
        err = urllib.error.HTTPError(REPLAY_MEDIA, 403, "Forbidden", {}, None)
        acts, det = replay_fixture()
        op = ReplayMainOpener(acts, det, media_error=err)
        rc, out = run_main(self.tmp, op, ["--exclude", "1430-encoder"])
        self.assertEqual(rc, F.RC_OK, out[-1200:])
        self.assertEqual(op.media_requests, [])


# ---------------------------------------------------------------- Round 13 JIT

class JitServer:
    """Round 13 用的假回放服务端：Range 感知 + 按 token 403 + 可切换对象。

    - 请求 URL 里出现 `bad_tokens` 中的任意一个 → 403（模拟时效凭据过期）
    - URL 里出现 `alt_marker` → 返回 `alt_data`（模拟「刷新后拿到另一个对象」）
    - `Range: bytes=0-0` → 只回响应头（size / ETag）
    - 其它 Range → 返回 `data[start:]`，并按真实语义回 `Content-Range`

    逐条记录 `(method, url, range)`，供「零请求 / 不泄漏 / 未用 HEAD」类断言。
    """

    def __init__(self, data, bad_tokens=(), alt_data=None, alt_marker="ALT",
                 etag='"v1"'):
        self.data = data
        self.bad_tokens = list(bad_tokens)
        self.alt_data = alt_data
        self.alt_marker = alt_marker
        self.etag = etag
        self.requests = []

    def open(self, req, timeout=None):
        u = req_url(req)
        method = req.get_method() if hasattr(req, "get_method") else "GET"
        hd = {k.lower(): v for k, v in
              (getattr(req, "headers", None) or {}).items()}
        rng = hd.get("range")
        self.requests.append((method, u, rng))

        for t in self.bad_tokens:
            if t and t in u:
                raise urllib.error.HTTPError(u, 403, "Forbidden", {}, None)

        data = self.data
        if self.alt_data is not None and self.alt_marker in u:
            data = self.alt_data

        if rng == "bytes=0-0":
            return FakeResponse(b"x", status=206, headers={
                "Content-Type": "video/mp4",
                "Content-Range": "bytes 0-0/%d" % len(data),
                "ETag": self.etag,
                "Last-Modified": "Wed, 22 Oct 2025 03:00:12 GMT"})

        start = int(rng.split("=")[1].split("-")[0]) if rng else 0
        body = data[start:]
        h = {"Content-Type": "video/mp4",
             "Content-Range": "bytes %d-%d/%d" % (start, len(data) - 1, len(data)),
             "ETag": self.etag}
        return FakeResponse(body, status=206, headers=h)

    def media_urls(self):
        return [u for _m, u, _r in self.requests]


class TestReplayUrlJit(Base):
    """★ Round 13：replay URL 只在真正要用之前才解析，且从不落盘。

    不变量：
      · 条目 / 索引 / 清单 / 日志都不带 replay URL 与 previewToken
      · 下载前 JIT 解析；.part 续传同样 JIT
      · 401/403 → 重新解析后继续，**保留 .part**
      · 刷新有上限，禁止无限循环
      · 刷新后 stable identity 实质冲突 → fail-closed，绝不拼接
    """

    DATA = b"0123456789" * 512          # 5120 字节，>1024 避开「疑似空响应」

    def setUp(self):
        super().setUp()
        self.clock = LiveClock()

    def dl(self, op, path, url=None, **kw):
        import lms_live
        kw.setdefault("retries", 1)
        kw.setdefault("quiet", True)
        return lms_live.download(op, url, path,
                                 clock=self.clock.now, sleep=self.clock.sleep,
                                 **kw)

    def _part(self, path, prefix):
        with open(path + ".part", "wb") as f:
            f.write(prefix)

    # ---- 1. URL 不落盘 / 不泄漏 ----

    def test_replay_item_carries_no_url(self):
        """回放条目不得携带 URL —— 它是时效凭据，不能进任何落盘路径。"""
        acts, det = replay_fixture()
        op = ReplayMainOpener(acts, det)
        items = F.expand_items(op, [("回放", "第1章", -777)],
                               FakeArgs(all_cameras=False))[0]
        j = json.dumps(items, ensure_ascii=False)
        self.assertNotIn("url", items[0], "条目里不该有 url 字段")
        self.assertNotIn("previewToken", j)
        self.assertNotIn("FAKE_TOKEN", j)

    def test_token_never_in_manifest_or_output(self):
        """清单与输出里不得出现 previewToken。"""
        acts, det = replay_fixture()
        err = urllib.error.HTTPError(REPLAY_MEDIA, 502, "Bad Gateway", {}, None)
        op = ReplayMainOpener(acts, det, media_error=err)
        mf = os.path.join(self.tmp, "m.json")
        _rc, out = run_main(self.tmp, op, ["--manifest", mf])
        self.assertNotIn("previewToken", out)
        self.assertNotIn("FAKE_TOKEN", out)
        with open(mf, encoding="utf-8") as f:
            self.assertNotIn("previewToken", f.read())

    def test_token_never_in_download_result_or_log(self):
        """download() 的返回值与日志同样不得带 token。"""
        import contextlib
        import lms_live
        op = JitServer(self.DATA)
        path = os.path.join(self.tmp, "a.mp4")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            res = self.dl(op, path, url="http://r/x?previewToken=SECRET_T",
                          quiet=False)
        self.assertTrue(res["ok"], res.get("err"))
        blob = json.dumps(res, ensure_ascii=False, default=str)
        self.assertNotIn("SECRET_T", blob)
        self.assertNotIn("SECRET_T", buf.getvalue())

    # ---- 2/4. 403 → 刷新 → 成功；续传起点正确 ----

    def test_first_403_then_refresh_succeeds(self):
        import lms_live
        op = JitServer(self.DATA, bad_tokens=["T_BAD"])
        path = os.path.join(self.tmp, "b.mp4")
        seq = iter(["http://r/x?previewToken=T_BAD",
                    "http://r/x?previewToken=T_GOOD"])
        res = self.dl(op, path, url_provider=lambda: next(seq))
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["refreshes"], 1)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.DATA)
        self.assertFalse(os.path.exists(path + ".part"))

    def test_part_resume_after_refresh_uses_part_offset(self):
        """★ 关键：刷新后必须从 .part 现有大小续传，不得从头再来、不得丢内容。"""
        op = JitServer(self.DATA, bad_tokens=["T_BAD"])
        path = os.path.join(self.tmp, "c.mp4")
        self._part(path, self.DATA[:1024])
        seq = iter(["http://r/x?previewToken=T_BAD",
                    "http://r/x?previewToken=T_GOOD"])
        res = self.dl(op, path, url_provider=lambda: next(seq))
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["refreshes"], 1)
        with open(path, "rb") as f:
            self.assertEqual(f.read(), self.DATA, "续传后内容必须完整正确")
        good = [r for m, u, r in op.requests
                if "T_GOOD" in u and r and r != "bytes=0-0"]
        self.assertTrue(good, "刷新后应发出续传请求")
        self.assertEqual(good[0], "bytes=1024-",
                         "续传起点必须等于 .part 大小")

    # ---- 3. 刷新上限 ----

    def test_refresh_exhausted_fails_clearly_and_keeps_part(self):
        import lms_live
        op = JitServer(self.DATA, bad_tokens=["T_BAD"])
        path = os.path.join(self.tmp, "d.mp4")
        self._part(path, self.DATA[:1024])
        calls = []

        def provider():
            calls.append(1)
            return "http://r/x?previewToken=T_BAD"

        res = self.dl(op, path, url_provider=provider, max_refreshes=2)
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "url_refresh_exhausted")
        self.assertTrue(res["kept_part"], "失败必须保留 .part")
        with open(path + ".part", "rb") as f:
            self.assertEqual(f.read(), self.DATA[:1024], ".part 内容不得被清掉")
        self.assertFalse(os.path.exists(path))
        self.assertEqual(len(calls), 3, "初始 1 次 + 上限 2 次刷新")

    def test_max_refreshes_is_bounded_constant(self):
        import lms_live
        self.assertIsInstance(lms_live.MAX_URL_REFRESHES, int)
        self.assertGreater(lms_live.MAX_URL_REFRESHES, 0)

    # ---- 5. identity 冲突 fail-closed ----

    def test_identity_conflict_fails_closed_without_splicing(self):
        """刷新后对象长度变了 → 必须 fail-closed，绝不把两份字节拼起来。"""
        op = JitServer(self.DATA, bad_tokens=["T_BAD"],
                       alt_data=self.DATA + b"EXTRA")
        path = os.path.join(self.tmp, "e.mp4")
        self._part(path, self.DATA[:1024])
        seq = iter(["http://r/x?previewToken=T_BAD",
                    "http://r/x?previewToken=T_GOOD&ALT=1"])
        res = self.dl(op, path, url_provider=lambda: next(seq),
                      base_identity=(len(self.DATA), '"v1"'))
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "identity_conflict", res)
        self.assertTrue(res["kept_part"])
        with open(path + ".part", "rb") as f:
            self.assertEqual(f.read(), self.DATA[:1024], "不得拼接、不得截断")
        self.assertFalse(os.path.exists(path), "冲突时不得落最终文件")

    def test_identity_etag_change_fails_closed(self):
        """同一长度但 ETag 变了：也是实质冲突（内容换了对象）。"""
        op = JitServer(self.DATA, bad_tokens=["T_BAD"], etag='"v2"')
        path = os.path.join(self.tmp, "f.mp4")
        self._part(path, self.DATA[:1024])
        seq = iter(["http://r/x?previewToken=T_BAD",
                    "http://r/x?previewToken=T_GOOD"])
        res = self.dl(op, path, url_provider=lambda: next(seq),
                      base_identity=(len(self.DATA), '"v1"'))
        self.assertFalse(res["ok"])
        self.assertEqual(res["reason"], "identity_conflict", res)

    def test_same_identity_refresh_is_not_a_conflict(self):
        """对照组：ETag / size 一致时刷新必须正常继续（别过度 fail-closed）。"""
        op = JitServer(self.DATA, bad_tokens=["T_BAD"])
        path = os.path.join(self.tmp, "g.mp4")
        seq = iter(["http://r/x?previewToken=T_BAD",
                    "http://r/x?previewToken=T_GOOD"])
        res = self.dl(op, path, url_provider=lambda: next(seq),
                      base_identity=(len(self.DATA), '"v1"'))
        self.assertTrue(res["ok"], res.get("err"))
        self.assertEqual(res["refreshes"], 1)

    # ---- 解析器 ----

    def test_resolve_picks_camera_by_id(self):
        import lms_live
        acts, det = replay_fixture()
        op = ReplayMainOpener(acts, det)
        u = lms_live.resolve_replay_url(op, 777, camera_id="c1")
        self.assertIn("previewToken=", u)

    def test_resolve_reports_missing_item_without_url(self):
        import lms_live
        acts, det = replay_fixture()
        op = ReplayMainOpener(acts, det)
        with self.assertRaises(lms_live.LiveResolveError) as cm:
            lms_live.resolve_replay_url(op, 777, camera_id="nope")
        self.assertNotIn("previewToken", str(cm.exception))

    def test_no_head_request_in_replay_path(self):
        """HEAD 不是本站可依赖的路径 —— 真实探测必须走 Range: bytes=0-0。"""
        op = JitServer(self.DATA)
        path = os.path.join(self.tmp, "h.mp4")
        self.assertTrue(self.dl(op, path, url="http://r/x?t=1")["ok"])
        self.assertNotIn("HEAD", [m for m, _u, _r in op.requests])

    # ---- 6. 异常文本不得把 URL 带出去 ----

    def test_redact_text_strips_query_anywhere(self):
        """自由文本里的 URL 必须被削掉 query —— token 就住在那里。"""
        import lms_live
        s = lms_live.redact_text(
            "boom https://rms.test/a/b?previewToken=ABC123 tail")
        self.assertNotIn("ABC123", s)
        self.assertNotIn("previewToken", s)
        self.assertIn("https://rms.test/a/b", s)
        self.assertEqual(lms_live.redact_text(None), None)

    def test_transport_error_text_has_no_token(self):
        """★ urllib 的部分异常会把完整 URL 拼进消息。

        ValueError("unknown url type: <url>") 这类文本一旦被原样塞进 err，
        previewToken 就会顺着返回值 / 清单 / 终端输出漏出去。
        """
        import lms_live

        class Boom:
            addheaders = []

            def open(self, req, timeout=None):
                raise ValueError("unknown url type: %s" % req_url(req))

        path = os.path.join(self.tmp, "boom.mp4")
        res = self.dl(Boom(), path,
                      url_provider=lambda: "http://r/x?previewToken=SECRET_Z")
        self.assertFalse(res["ok"])
        blob = json.dumps(res, ensure_ascii=False, default=str)
        self.assertNotIn("SECRET_Z", blob, blob)
        self.assertNotIn("previewToken", blob, blob)

    def test_probe_error_text_has_no_token(self):
        """探测失败的 err 会进条目的 err_msg → 清单，同样不能带 token。"""
        import lms_live

        class Boom:
            addheaders = []

            def open(self, req, timeout=None):
                raise ValueError("bad url %s" % req_url(req))

        p = lms_live.probe_remote(Boom(), "http://r/p?previewToken=SECRET_P")
        self.assertFalse(p["ok"])
        blob = json.dumps(p, ensure_ascii=False, default=str)
        self.assertNotIn("SECRET_P", blob, blob)
        self.assertNotIn("previewToken", blob, blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
