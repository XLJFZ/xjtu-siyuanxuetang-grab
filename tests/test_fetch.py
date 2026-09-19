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
    """etag 里藏着文件大小 —— 这是思源学堂唯一可用的服务端完整性信号。"""

    def test_parse(self):
        # 实测：etag "698e9012-cb2ec" 对应 832236 字节
        self.assertEqual(F._etag_size('"698e9012-cb2ec"'), 832236)

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

    def _run(self, acts, details, want_video=True):
        """跑一次真实的 collect()，并把它的进度输出静音。"""
        import contextlib
        op = self._Op(acts, details)
        with contextlib.redirect_stdout(io.StringIO()):
            keys, _ = F.collect(op, "1", acts, want_video=want_video)
        return keys

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


class TestReplayNaming(Base):
    """回放文件名 —— 踩过的坑：同一天多个活动 title 完全相同。"""

    def test_stamp_disambiguates(self):
        """没有时间戳时，同一天 4 节课会生成同一个文件名、互相覆盖。

        实测某课程 4 个 lecture_live 活动 title 都是
        「2026-09-19-计算机视觉与模式识别」，只有 start_time 不同。
        """
        import lms_live
        t = "2026-09-19-计算机视觉与模式识别"
        a = lms_live.safe_name(t, "encoder", stamp="20260919-1430")
        b = lms_live.safe_name(t, "encoder", stamp="20260919-1530")
        self.assertNotEqual(a, b)
        self.assertIn("20260919-1430", a)
        self.assertIn("20260919-1530", b)

    def test_camera_disambiguates(self):
        """同一场次的两路机位也不能撞名。"""
        import lms_live
        t = "2026-09-19-计算机视觉与模式识别"
        a = lms_live.safe_name(t, "encoder", stamp="20260919-1430")
        b = lms_live.safe_name(t, "instructor", stamp="20260919-1430")
        self.assertNotEqual(a, b)

    def test_stamp_from_iso(self):
        """UTC 的 ISO 时间要转成本地时间，否则上午的课会显示成凌晨。"""
        import lms_live
        s = lms_live.start_stamp({"start_time": "2026-09-19T06:30:00Z"})
        self.assertTrue(s and s.startswith("20260919-"), s)
        # UTC+8 下 06:30Z == 14:30 本地
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
