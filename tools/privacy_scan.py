# -*- coding: utf-8 -*-
"""发布树隐私扫描 —— 「什么内容不许出现在公开包里」的唯一定义。

为什么需要它
------------
公开仓库和 Release 附件是任何人可下载的，一旦混进本机绝对路径、真实凭据
或私有主机名就收不回来（Git 历史可追，已发布的附件不可变）。发布前的最后
一道闸门就是这里。

为什么不能只写一条 grep
-----------------------
路径的写法不止一种：盘符后面可能跟一个分隔符，也可能跟两个（源码字符串
字面量里的转义形式），而反斜杠在 shell / YAML / Python 里还会被再转义一层。
只钉住一种写法必然漏掉另一种 —— 真实踩过：发布工具的文档字符串里写着
转义形式的盘符路径，而检查只匹配单分隔符，于是发布前放行了。

分类，而不是一票否决
--------------------
同一条规则命中的东西不都是泄漏。命中项落进四类之一，各自有据可查：

    block        真实泄漏 —— 让 gate 失败
    placeholder  占位写法（尖括号、xxx、%VAR% 之类），进包也无意义
    generic_os   通用系统目录（Windows 安装目录等），与个人身份无关
    allowed      逐行豁免表命中的检测器定义 / 合成夹具

后三类只记录、不失败。

豁免必须精确到行，且凭据类规则禁止豁免
--------------------------------------
ALLOWLIST 的每条都要写明「哪个文件的哪一行、为什么」。文件用相对路径，
另配一条必须命中该行的正则 —— 因此同一个文件里新出现的真实路径照样会被
拦下。**豁免不等于关掉规则**。

此外有一条不可协商的边界：**凭据类规则（secret-token / preview-token）
不接受任何行级豁免**。v1.5.1 踩过的坑：`rule="*"` 的通配豁免把一条真实
PAT 前缀当「合成夹具」放行了。教训是 —— 路径写错顶多是难看，凭据放出去
就是事故；豁免机制只对「形态本身无害」的规则（路径 / 主机名）开放。

跑法
----
    python tools/privacy_scan.py --root .               # 人读格式
    python tools/privacy_scan.py --root . --format github   # 给 CI 用
    python tools/privacy_scan.py --root . --list-noted  # 连非阻塞项一起看
"""
import argparse
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from tools import release_common as RC  # noqa: E402

# ---------------------------------------------------------------- 分类常量

BLOCK = "block"
PLACEHOLDER = "placeholder"
GENERIC_OS = "generic_os"
ALLOWED = "allowed"
NAME_MENTION = "name_mention"

NOTED = (PLACEHOLDER, GENERIC_OS, ALLOWED, NAME_MENTION)   # 记录但不失败


# ---------------------------------------------------------------- 规则

# 盘符路径：一个字母 + 冒号 + 一或多个分隔符（反斜杠、正斜杠都算，因此
# 「一个分隔符」和「两个分隔符（源码里的转义写法）」两种形态同时覆盖）。
#
# 前置否定环视排掉两类误报：
#   * `%s://` 这类格式化占位 —— 冒号前是 `%`
#   * `https://` 这类 URI scheme —— 冒号前还有别的字母
#
# 末尾允许空格连接的后续段：否则带空格的通用系统目录会断在第一个空格上，
# 分不出「通用目录」和「真实路径」。此类段用前缀匹配识别（见
# _is_generic_os_segment），括号截断也认得出来。
_WIN_ABS_PATH = re.compile(
    r"(?<![A-Za-z0-9_%])[A-Za-z]:[\\/]+[^\s\"'`)\]]*"
    r"(?:[ \t][^\s\"'`)\]]+)*")

# POSIX 用户目录
_POSIX_USER_PATH = re.compile(r"/(?:Users|home)/[A-Za-z0-9._-]+")

# 真实凭据：GitHub PAT / OAuth token 的值（占位写法 github_pat_xxx 不达标）
_SECRET_TOKEN = re.compile(r"(?:github_pat_[A-Za-z0-9_]{20,}|gh[pousr]_[A-Za-z0-9]{20,})")

# 回放一次性凭据：查询串里带够长的值才算
_PREVIEW_TOKEN = re.compile(
    r"previewToken[\"'\]\)\s]*[:=]\s*[\"']?[A-Za-z0-9_.-]{16,}")

# 私有主机名
_PRIVATE_HOST = re.compile(r"rms-v5\.xjtu\.edu\.cn")

# 本机登录态目录。单独出现属于文档说明（README/SKILL 里就写着默认缓存目录），
# 只记录；真泄漏必然带着盘符路径，由 win-absolute-path 拦下。
_STATE_DIR = re.compile(r"\.lms-grab[\\/]")

# 凭据类文件名（只记录）：这些都是**规则名**，出现在 .gitignore / 白名单
# 定义 / 文档说明里是正常的；会随包发布的是「文件本身」，那由白名单排除规则
# 与包体校验（EXCLUDE_STATE 走查）拦，不在这里重复判死。
_CRED_FILE_NAME = re.compile(
    r"state_[A-Za-z0-9*_-]+\.json|storage_state\.json"
    r"|cookies[A-Za-z0-9*_.-]*\.json|activities[A-Za-z0-9*_.-]*\.json|\*\.har")


# 分类：盘符路径里的通用系统目录段（与个人身份无关）
_GENERIC_OS_SEGMENTS = {
    "program files", "program files (x86)", "programdata", "windows",
    "system32", "syswow64", "temp", "tmp", "runner",
}

# 分类：占位段。尖括号、星号、xxx、%VAR%、$VAR、~、$ {VAR}
_PLACEHOLDER_SEGMENT = re.compile(
    r"^(?:<[^>]*>|\*+|x{2,}|xxx+|%[^%]+%|\$[A-Za-z_{][^$]*|\$\{[^}]*\}|~|\.\.\.)$",
    re.I)


def _split_segments(path):
    """把盘符路径切成段（去掉盘符本身）。"""
    segs = [s for s in re.split(r"[\\/]+", path) if s]
    if segs and re.match(r"^[A-Za-z]:$", segs[0]):
        segs = segs[1:]
    return segs


def _is_generic_os_segment(seg):
    """前缀匹配而不是全等匹配：`Program Files (x86` 这种被括号截断的段也要认出来。"""
    low = seg.lower()
    return any(low.startswith(name + " ") or low == name
               for name in _GENERIC_OS_SEGMENTS)


def _classify_drive_path(text):
    """盘符路径是通用系统目录 / 占位写法 / 真实泄漏，取三者之一。"""
    segs = _split_segments(text)
    if not segs:
        return BLOCK
    for s in segs:
        if _PLACEHOLDER_SEGMENT.match(s):
            return PLACEHOLDER
    if _is_generic_os_segment(segs[0]):
        return GENERIC_OS
    return BLOCK


def _classify_posix_user(text):
    segs = [s for s in text.split("/") if s]
    # segs = ["Users"|"home", "<name>"]
    if len(segs) >= 2 and _PLACEHOLDER_SEGMENT.match(segs[1]):
        return PLACEHOLDER
    if len(segs) >= 2 and segs[1].lower() in _GENERIC_OS_SEGMENTS:
        return GENERIC_OS
    return BLOCK


class Rule(object):
    def __init__(self, name, regex, severity, classify=None, note=""):
        self.name = name
        self.regex = regex
        self.severity = severity       # 未命中 classify 时的默认分类
        self.classify = classify
        self.note = note
        # 凭据类规则不接受行级豁免（见模块 docstring 的边界说明）。
        self.exemptable = name not in _NON_EXEMPTABLE_RULES


# 凭据一旦放出去就是事故，不存在「这行是夹具所以没事」—— 夹具必须运行时
# 构造，让凭据字形根本不出现在源码字面量里，而不是靠豁免放行。
_NON_EXEMPTABLE_RULES = frozenset(("secret-token", "preview-token"))


RULES = [
    Rule("win-absolute-path", _WIN_ABS_PATH, BLOCK, _classify_drive_path,
         "本机盘符绝对路径"),
    Rule("posix-user-path", _POSIX_USER_PATH, BLOCK, _classify_posix_user,
         "POSIX 用户目录绝对路径"),
    Rule("secret-token", _SECRET_TOKEN, BLOCK, note="真实凭据值"),
    Rule("preview-token", _PREVIEW_TOKEN, BLOCK, note="一次性回放凭据"),
    Rule("private-host", _PRIVATE_HOST, BLOCK, note="私有主机名"),
    Rule("state-dir-path", _STATE_DIR, NAME_MENTION, note="本机登录态目录"),
    # 规则名出现在 .gitignore / 排除正则 / 文档说明里是正常的；真正的
    # 凭据文件由白名单排除规则 + 包体校验拦，这里只记录不判死。
    Rule("credential-file-name", _CRED_FILE_NAME, NAME_MENTION,
         note="凭据类文件名"),
]


# ---------------------------------------------------------------- 逐行豁免

class Allow(object):
    """一行豁免：`path` 下的某一行，命中 `line` 正则才放行。

    `rule` 填规则名，或 `"*"` 表示任意**可豁免**规则 —— 凭据类规则
    （secret-token / preview-token）即使写了 `"*"` 也不会被覆盖。
    """

    def __init__(self, rule, path, line, reason):
        self.rule = rule                 # 规则名，或 "*"（仅覆盖可豁免规则）
        self.path = path                 # 发布树内相对路径（精确匹配）
        self.line = re.compile(line)
        self.reason = reason


# 每条都必须精确到「哪个文件、哪一行、为什么」。
# 新增豁免要同时在这里写清理由 —— 否则就是变相关掉规则。
# 凭据类规则在这里写也没用：_allow_reason 对它们一律返回 None。
ALLOWLIST = [
    # 扫描器自身只在这些行上放行合成夹具（盘符路径 / 私有主机名 /
    # POSIX 用户目录各有一处）。豁免精确到行：标记缺失的行照样会被拦下。
    # 注意：本条**不含**凭据类 —— 凭据夹具必须运行时构造，不得靠豁免放行。
    Allow("*", "tests/test_push.py",
          r"# privacy-scan: fixture$",
          "测试合成夹具：本行用于验证扫描器能命中该类形态，不是真实泄漏；"
          "豁免精确到行，同文件里未标记的行仍会被拦下"),
]


def _allow_reason(rule_name, rel, line):
    # 双保险：即使将来有人误把凭据规则写进 ALLOWLIST，这里也直接拒绝。
    if rule_name in _NON_EXEMPTABLE_RULES:
        return None
    for a in ALLOWLIST:
        if a.rule not in (rule_name, "*"):
            continue
        if a.path != rel:
            continue
        if a.line.search(line):
            return a.reason
    return None


# ---------------------------------------------------------------- 扫描

class Finding(object):
    def __init__(self, rule, kind, path, line_no, line, match, reason=""):
        self.rule = rule
        self.kind = kind
        self.path = path
        self.line_no = line_no
        self.line = line
        self.match = match
        self.reason = reason

    @property
    def blocking(self):
        return self.kind == BLOCK

    def __repr__(self):
        return "<Finding %s %s %s:%d>" % (self.rule, self.kind,
                                          self.path, self.line_no)


def scan_text(rel, text):
    """扫一段文本，返回全部命中（含非阻塞项）。"""
    out = []
    for i, raw in enumerate(text.splitlines(), 1):
        for rule in RULES:
            for m in rule.regex.finditer(raw):
                if rule.classify:
                    kind = rule.classify(m.group(0))
                else:
                    kind = rule.severity
                reason = ""
                if kind == BLOCK and rule.exemptable:
                    reason = _allow_reason(rule.name, rel, raw) or ""
                    if reason:
                        kind = ALLOWED
                out.append(Finding(rule.name, kind, rel, i, raw.strip(),
                                   m.group(0), reason))
    return out


# 二进制 / 生成物不进普通文本扫描
_BINARY_SUFFIX = (".zip", ".pyc", ".pyo", ".pyd", ".png", ".jpg", ".jpeg",
                  ".gif", ".ico", ".webp", ".pdf", ".woff", ".woff2", ".ttf",
                  ".gz", ".whl", ".so", ".dll", ".exe")


def _looks_binary(path):
    if path.lower().endswith(_BINARY_SUFFIX):
        return True
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(4096)
    except OSError:
        return True


def scan_tree(root, include=None):
    """扫一棵树的**发布白名单文件集**，返回 (findings, 扫过的文件数)。

    文件集复用 tools/release_common.collect —— 不另立一份清单，
    否则「发布白名单」和「隐私扫描范围」迟早各走各的（这正是漏检成因之一）。
    """
    findings, n = [], 0
    for path, rel in RC.collect(root, include):
        if _looks_binary(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            continue
        n += 1
        findings.extend(scan_text(rel, text))
    return findings, n


def scan_files(root, rels):
    """扫指定的一组相对路径（调试 / 定点复核用）。"""
    findings, n = [], 0
    for rel in rels:
        path = os.path.join(root, rel)
        if not os.path.isfile(path) or _looks_binary(path):
            continue
        with open(path, encoding="utf-8", errors="replace") as f:
            text = f.read()
        n += 1
        findings.extend(scan_text(rel, text))
    return findings, n


def blocking(findings):
    return [f for f in findings if f.blocking]


def noted(findings):
    return [f for f in findings if not f.blocking]


def assert_clean(root, include=None):
    """发布前 gate：有任何阻塞项就抛 SystemExit。返回 (扫描文件数, 记录数)。"""
    findings, n = scan_tree(root, include)
    bad = blocking(findings)
    if bad:
        lines = [format_findings(bad)]
        raise SystemExit("隐私扫描未通过（%d 处）：\n%s" % (len(bad), lines[0]))
    return n, len(noted(findings))


# ---------------------------------------------------------------- 输出

def format_findings(findings, fmt="text"):
    if fmt == "github":
        out = []
        for f in findings:
            level = "error" if f.blocking else "notice"
            out.append("::%s file=%s,line=%d::[%s] %s | %s"
                       % (level, f.path, f.line_no, f.rule, f.match,
                          f.reason or f.kind))
        return "\n".join(out)
    if fmt == "json":
        import json
        return json.dumps([{
            "rule": f.rule, "kind": f.kind, "path": f.path,
            "line": f.line_no, "match": f.match, "text": f.line,
            "reason": f.reason} for f in findings], ensure_ascii=False, indent=2)
    width = max([len(f.path) for f in findings] or [0])
    out = []
    for f in findings:
        out.append("%-9s %-20s %-*s:%-4d %s"
                   % (f.kind, f.rule, width, f.path, f.line_no, f.match))
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="发布树隐私扫描")
    ap.add_argument("--root", default=PROJECT_ROOT,
                    help="要扫描的树根（默认仓库/包根）")
    ap.add_argument("--include", default=None,
                    help="逗号分隔的白名单覆盖（默认用 release_common.INCLUDE）")
    ap.add_argument("--format", default="text",
                    choices=("text", "github", "json"))
    ap.add_argument("--list-noted", action="store_true",
                    help="连非阻塞项（占位 / 通用系统目录 / 已豁免）一起列出")
    args = ap.parse_args(argv)

    include = [s for s in args.include.split(",") if s] if args.include else None
    findings, n = scan_tree(args.root, include)
    bad, rest = blocking(findings), noted(findings)

    if bad:
        print(format_findings(bad, args.format))
    if args.list_noted and rest:
        print(format_findings(rest, args.format))
    print("扫描 %d 个文本文件：阻塞 %d，记录 %d" % (n, len(bad), len(rest)))
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
