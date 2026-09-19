# -*- coding: utf-8 -*-
"""章节解析的离线自测 —— 不需要网络、不需要登录态。

跑法：
    python -m pytest tests/ -q
或（不想装 pytest 时）：
    python tests/test_organize.py

用例全部取自真实课程数据：《计算机视觉与模式识别》33593 与《数据库系统》33590。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts"))

from lms_organize import cn_to_int, parse_chapter, chapter_dir, strip_brackets  # noqa: E402


class TestCnToInt(unittest.TestCase):
    def test_arabic(self):
        self.assertEqual(cn_to_int("3"), 3)
        self.assertEqual(cn_to_int("12"), 12)
        self.assertEqual(cn_to_int("0"), 0)

    def test_single_cn(self):
        self.assertEqual(cn_to_int("一"), 1)
        self.assertEqual(cn_to_int("九"), 9)
        self.assertEqual(cn_to_int("零"), 0)

    def test_shi(self):
        self.assertEqual(cn_to_int("十"), 10)
        self.assertEqual(cn_to_int("十二"), 12)
        self.assertEqual(cn_to_int("二十"), 20)
        self.assertEqual(cn_to_int("二十一"), 21)

    def test_digits_run(self):
        # 「一二」这种连写按逐位处理：12
        self.assertEqual(cn_to_int("一二"), 12)

    def test_bai(self):
        self.assertEqual(cn_to_int("一百"), 100)
        self.assertEqual(cn_to_int("一百零五"), 105)

    def test_invalid(self):
        self.assertIsNone(cn_to_int("abc"))
        self.assertIsNone(cn_to_int(""))
        self.assertIsNone(cn_to_int(None))


class TestStripBrackets(unittest.TestCase):
    def test_wrap(self):
        # 整串被一对括号包住 -> 剥
        self.assertEqual(strip_brackets("【Lec11】"), "Lec11")
        self.assertEqual(strip_brackets("[Chapter 3]"), "Chapter 3")
        self.assertEqual(strip_brackets("（绪论）"), "绪论")

    def test_nested_repeat(self):
        self.assertEqual(strip_brackets("【[Lec11]】"), "Lec11")

    def test_partial_kept(self):
        # 只有开头有括号，不算包裹，保持原样
        self.assertEqual(strip_brackets("【第一章】课件"), "【第一章】课件")
        # 中间括号保留
        self.assertEqual(strip_brackets("卷积的应用（上）"), "卷积的应用（上）")


class TestParseChapter(unittest.TestCase):
    """真实数据回归 —— 这些标题/文件名都是抓取时真实遇到的。"""

    def test_real_cv_course(self):
        cases = [
            # (活动标题, 文件名, 期望章号)
            ("Lec1 Introduction", "Lec1.pdf", 1),
            ("第十二章 贝叶斯分类", "第十二章 贝叶斯分类.pdf", 12),
            ("第十三章 卷积神经网络", "第十三章 CNN.pdf", 13),
            ("第十一章 Harris特征点", "Lec11-卷积的应用--Harris, GFTT,SIFT特征.pdf", 11),
            ("Chapter 3 - CNN", "Chapter 3 - CNN.pdf", 3),
            ("第1讲 绪论", "第1讲 绪论.mp4", 1),
        ]
        for act, name, want in cases:
            with self.subTest(act=act, name=name):
                self.assertEqual(parse_chapter(act, name), want)

    def test_real_db_course(self):
        cases = [
            ("【第一章】课件", "第1章-数据库系统概述-米辅.pptx", 1),
            ("【第二章】课件", "第2章-关系模型-米辅.pptx", 2),
            ("【第三章】课件", "第3章-关系数据库语言SQL-米辅.pptx", 3),
            ("【第四章】课件", "第4章-数据库设计_米辅.pptx", 4),
            ("【第五章】课件", "第5章-数据依赖与关系模式规范化-米辅.pptx", 5),
            ("【第六章】课件", "第6章-查询处理与查询优化-米辅.pptx", 6),
            ("课程简介", "第0章-课程简介-米辅.pdf", 0),
        ]
        for act, name, want in cases:
            with self.subTest(act=act, name=name):
                self.assertEqual(parse_chapter(act, name), want)

    def test_not_chapter_by_design(self):
        """这些数字是编号不是章号，必须返回 None，否则会被塞进错误的章目录。"""
        bad = [
            ("实验1-数据库管理系统配置", "1.1使用docker安装部署openGauss.pdf"),
            ("实验1-数据库管理系统配置", "1.4使用gsql远程连接.pdf"),
            ("实验1-数据库管理系统配置", "1.6使用Data Studio远程连接.pdf"),
            ("实验2-数据库语言SQL的使用", "实验2指导书-数据库语言SQL的使用.pdf"),
            ("实验3-存储过程与函数与触发器", "实验3指导书-存储过程、函数与触发器.pdf"),
            ("Project 1 Dolly Zoom", "Project1_Dollyzoom.zip"),
            ("大实验作业", "实验报告模板.docx"),
            ("第七章作业", "第七章作业（2021版）.pdf"),  # 但见下：这条其实能识别
        ]
        for act, name in bad[:6]:
            with self.subTest(act=act, name=name):
                self.assertIsNone(parse_chapter(act, name))

    def test_homework_chapter_kept(self):
        """作业区的「第N章作业」应该识别出章号 —— 作业也该按章归位。

        注意区分：活动标题是「实验1-xxx」时数字是实验编号，不是章号；
        但标题写「【第一章】作业」时，那个一就是章号。
        """
        self.assertEqual(parse_chapter("【第一章】作业", "第1章作业题目.png"), 1)
        self.assertEqual(parse_chapter("第七章作业", "第七章作业（2021版）.pdf"), 7)
        # 实验编号仍然不算章号
        self.assertIsNone(parse_chapter("实验2-数据库语言SQL的使用"))

    def test_act_title_wins(self):
        """活动标题比文件名规范，优先采信标题。"""
        # 标题说第11章，文件名里的 Lec11 也在，一致
        self.assertEqual(parse_chapter("第十一章 Harris", "Lec11-xxx.pdf"), 11)

    def test_empty(self):
        self.assertIsNone(parse_chapter("", None))
        self.assertIsNone(parse_chapter(None))


class TestChapterDir(unittest.TestCase):
    def test_with_title(self):
        self.assertEqual(chapter_dir(1, ["第1章-数据库系统概述-米辅.pptx"]),
                         "第01章 数据库系统概述-米辅.pptx")

    def test_pad(self):
        self.assertEqual(chapter_dir(12, ["第十二章 贝叶斯分类"]), "第12章 贝叶斯分类")

    def test_no_dup_prefix(self):
        """核心回归：不能拼成「第00章 第0章-课程简介」。"""
        d = chapter_dir(0, ["课程简介", "第0章-课程简介-米辅.pdf"])
        self.assertEqual(d, "第00章 课程简介-米辅.pdf")
        # 关键是别出现两次「第0章」
        self.assertEqual(d.count("第0章"), 0)
        self.assertTrue(d.startswith("第00章 课程简介"))

    def test_brackets_stripped(self):
        d = chapter_dir(1, ["【第一章】课件", "第1章-数据库系统概述-米辅.pptx"])
        self.assertEqual(d, "第01章 数据库系统概述-米辅.pptx")

    def test_no_title(self):
        self.assertEqual(chapter_dir(7), "第07章")
        self.assertEqual(chapter_dir(7, ["", None]), "第07章")

    def test_title_truncated(self):
        long_title = "第1章 " + "很长" * 40
        self.assertLessEqual(len(chapter_dir(1, [long_title])), len("第01章 ") + 40)


if __name__ == "__main__":
    unittest.main(verbosity=2)
