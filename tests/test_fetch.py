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


class TestLiveShortRead(Base):
    """回放下载的短读判定 —— lms_live 没有官方哈希可用，只能靠声明总长比对。

    回归背景：服务端声明的 Content-Length 与自然 EOF 实得字节数之间存在小幅差异，
    旧实现静默接受，调用方无法分辨「正常的自然短读」和「真的没下完」。
    """

    class _Op:
        """假的 opener：直接给出 _open_range 的返回值形状。"""

        def __init__(self, body, content_range=None, headers=None):
            self.body = body
            self.content_range = content_range
            self.headers = headers or {}

        def open(self, req, timeout=None):
            h = {"Content-Type": "video/mp4"}
            h.update(self.headers)
            if self.content_range:
                h["Content-Range"] = self.content_range
            return FakeResponse(self.body, headers=h, status=206)

    def _dl(self, body_len, declared):
        import lms_live
        op = self._Op(b"x" * body_len,
                      content_range="bytes 0-%d/%d" % (body_len - 1, declared))
        path = os.path.join(self.tmp, "r.mp4")
        return lms_live.download(op, "http://r/x", path, retries=1, quiet=True)

    def test_normal_short_read_is_tolerated(self):
        """实测的自然短读（约 5.6%）落在阈值内：算成功，但不给 note。"""
        res = self._dl(944000, 1000000)          # 少 5.6%
        self.assertTrue(res["ok"])
        self.assertEqual(res["size"], 944000)
        self.assertEqual(res["declared"], 1000000)
        self.assertAlmostEqual(res["shortfall"], 0.056, places=3)
        self.assertIsNone(res["note"])

    def test_excessive_short_read_fails(self):
        """少得太多（超过阈值）必须判失败。

        回归背景：以前无论少多少都 return ok=True，还先 os.replace 落盘，
        于是截断的视频被当成完整文件写进最终路径，下次运行因为「文件已存在」
        直接跳过 —— 用户永远拿不到完整视频，日志里却是一片 OK。
        """
        res = self._dl(500000, 1000000)          # 少 50%
        self.assertFalse(res["ok"])
        self.assertIn("500000", res["err"])
        self.assertIn("1000000", res["err"])
        self.assertEqual(res["declared"], 1000000)
        self.assertAlmostEqual(res["shortfall"], 0.5, places=3)
        self.assertTrue(res.get("partial"))

    def test_excessive_short_read_keeps_part(self):
        """失败时不能落盘成最终文件，且 .part 要保留供下次续传。"""
        import lms_live
        op = self._Op(b"x" * 500000,
                      content_range="bytes 0-499999/1000000")
        path = os.path.join(self.tmp, "trunc.mp4")
        res = lms_live.download(op, "http://r/x", path, retries=1, quiet=True)
        self.assertFalse(res["ok"])
        self.assertFalse(os.path.exists(path), "残缺文件不许落盘成最终文件")
        self.assertTrue(os.path.exists(path + ".part"), ".part 应保留以便续传")

    def test_excessive_short_read_retries_then_fails(self):
        """重试次数用尽后仍是残缺 → 失败。"""
        import lms_live
        calls = []

        class _Op:
            def open(self, req, timeout=None):
                calls.append(1)
                return FakeResponse(b"x" * 500000, status=206, headers={
                    "Content-Type": "video/mp4",
                    "Content-Range": "bytes 0-499999/1000000"})

        path = os.path.join(self.tmp, "r2.mp4")
        res = lms_live.download(_Op(), "http://r/x", path, retries=3, quiet=True)
        self.assertFalse(res["ok"])
        self.assertEqual(len(calls), 3, "三次重试都要用上")

    def test_exact_length_has_no_shortfall(self):
        res = self._dl(1000000, 1000000)
        self.assertTrue(res["ok"])
        self.assertEqual(res["shortfall"], 0.0)
        self.assertIsNone(res["note"])

    def test_no_declared_length_stays_silent(self):
        """拿不到声明总长时不该瞎判，shortfall / note 都留空。"""
        import lms_live
        op = self._Op(b"x" * 4096)               # 没有 Content-Range
        path = os.path.join(self.tmp, "n.mp4")
        res = lms_live.download(op, "http://r/x", path, retries=1, quiet=True)
        self.assertTrue(res["ok"])
        self.assertIsNone(res["declared"])
        self.assertIsNone(res["shortfall"])
        self.assertIsNone(res["note"])

    def test_tolerance_constant_reasonable(self):
        """阈值本身要大于实测的 5.6%，否则正常回放天天告警。"""
        import lms_live
        self.assertGreater(lms_live.SHORT_TOLERANCE, 0.056)


class TestLiveRangeAlignment(Base):
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
        # 第二次请求不应再带 Range（残片已作废）
        self.assertEqual(op.calls[-1][0], 0, "缺口后应重下全量")

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


class TestCompleteTolerance(Base):
    """下载判据与增量判据必须一致 —— 回放允许 8% 自然短读。

    回归背景：`lms_live.download()` 认定 944000/1000000 是成功，
    但下一轮 `already_complete()` 要求严格相等，又把它判成要重下 ——
    同一个文件在两套判据下反复横跳。
    """

    def _file(self, n):
        p = os.path.join(self.tmp, "f.bin")
        with open(p, "wb") as f:
            f.write(b"x" * n)
        return p

    def test_attachment_needs_exact_size(self):
        """普通附件仍然是严格判等 —— 不能给 PDF 放 8% 容差。"""
        p = self._file(944)
        ok, local, exp = F.already_complete(p, 1000)
        self.assertFalse(ok, "普通附件 944/1000 必须判为不完整")
        self.assertEqual(local, 944)
        self.assertEqual(exp, 1000)

    def test_replay_tolerates_short_read(self):
        """回放 944/1000 少 5.6%，落在 SHORT_TOLERANCE 内 → 算完整。"""
        import lms_live
        p = self._file(944)
        ok, _local, _exp = F.already_complete(
            p, 1000, tolerance=lms_live.SHORT_TOLERANCE)
        self.assertTrue(ok)

    def test_replay_beyond_tolerance_is_incomplete(self):
        import lms_live
        p = self._file(800)
        ok, _local, _exp = F.already_complete(
            p, 1000, tolerance=lms_live.SHORT_TOLERANCE)
        self.assertFalse(ok, "少 20% 超出容差，必须重下")

    def test_local_larger_than_expected_is_incomplete(self):
        """比声明还大 → 不完整，无论容差多少。"""
        import lms_live
        p = self._file(1200)
        for tol in (0.0, lms_live.SHORT_TOLERANCE, 5.0):
            ok, _l, _e = F.already_complete(p, 1000, tolerance=tol)
            self.assertFalse(ok, "tolerance=%s 时不该判为完整" % tol)

    def test_tolerance_boundary_value(self):
        """正好等于容差 → 完整（与 lms_live 的 `<` 判定保持方向一致）。"""
        import lms_live
        boundary = int(1000 * (1 - lms_live.SHORT_TOLERANCE))
        p = self._file(boundary)
        ok, _l, _e = F.already_complete(
            p, 1000, tolerance=lms_live.SHORT_TOLERANCE)
        self.assertTrue(ok)
        ok2, _l2, _e2 = F.already_complete(
            self._file(boundary - 1), 1000,
            tolerance=lms_live.SHORT_TOLERANCE)
        self.assertFalse(ok2)

    def test_complete_tolerance_per_kind(self):
        """每个 kind 的容差取值：回放跟 lms_live 一致，其余为 0。"""
        import lms_live
        self.assertEqual(F.complete_tolerance("回放"),
                         lms_live.SHORT_TOLERANCE)
        for kind in ("课件", "作业", "录像"):
            self.assertEqual(F.complete_tolerance(kind), 0.0)

    def test_bad_tolerance_falls_back_to_zero(self):
        p = self._file(999)
        ok, _l, _e = F.already_complete(p, 1000, tolerance="junk")
        self.assertFalse(ok)

    def test_getsize_oserror_under_replay_tolerance(self):
        """回放分支（带容差）同样不能被 OSError 带着走向「已完成」。"""
        import unittest.mock as mock
        import lms_live
        p = self._file(999)
        with mock.patch("os.path.getsize", side_effect=OSError(121, "semaphore")):
            ok, local, exp = F.already_complete(
                p, 1000, tolerance=lms_live.SHORT_TOLERANCE)
        self.assertFalse(ok)
        self.assertEqual(local, 0)
        self.assertEqual(exp, 1000)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
