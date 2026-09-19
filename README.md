<div align="center">

# xjtu-siyuanxuetang-grab

**思源学堂 2.0 个人课程资料下载与整理工具**

用于将当前账号**有权访问的课程资料**批量下载到本地，并按照课件、作业、录像等类型进行整理。

支持 AI Skill、Prompt 与独立 Python 脚本三种使用方式，**推荐装成 Skill**——
装好后一句自然语言即可跑完整个流程。

[![CI](https://github.com/XLJFZ/xjtu-siyuanxuetang-grab/actions/workflows/ci.yml/badge.svg)](https://github.com/XLJFZ/xjtu-siyuanxuetang-grab/actions/workflows/ci.yml)
![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

</div>

---

## 使用范围与说明

本项目主要用于个人学习过程中对课程资料进行备份、归档和离线整理。

使用本项目时，请遵守学校相关规定、课程资源版权要求以及教学平台的正常使用规则。

本项目的设计原则包括：

- 仅访问当前登录账号正常拥有访问权限的课程和资源；
- 不绕过统一身份认证、平台权限控制或其他访问限制；
- 不提供越权访问、权限提升或批量探测无权限课程的功能；
- 不进行高并发请求，并对 401、403 等权限类错误停止重试；
- 不包含任何课程课件、课堂录像、用户 Cookie、账号密码或其他登录凭据；
- 下载得到的课程资料仅应在获得授权的范围内使用，不应通过本项目重新公开传播课程资源。

本项目是一个客户端自动化与资料整理工具，并不会改变服务器对课程、附件和录像资源的权限判断。

> 使用者应自行确认其对相关课程资源具有合法、合规的访问和使用权限。

---

## 这是什么

思源学堂 2.0 的部分课程附件由网页前端通过接口动态加载，直接保存网页并不能完整保存课程中的课件、作业和其他附件。

这个项目将用户在浏览器中已经能够正常访问的课程资源进行统一整理，并提供：

- 课程附件清单生成
- 课件与作业批量下载
- 课堂录像与课程回放下载
- 按章节自动整理
- 断点续传
- 下载失败自动重试
- 文件大小与 SHA-256 校验
- 增量更新
- `--dry-run` 下载前预览

整个流程仍然使用用户本人正常登录后获得的会话状态。

**装成 Skill 后**，你只要说一句：

> 帮我把这门课的资料都下载下来，课程链接：`https://lms.xjtu.edu.cn/course/<课程ID>/index`

AI 就会自动走完「登录 → 干跑列清单 → 确认 → 下载 → 归类」的完整流程。安装方式见
下方[「装成 Skill」](#装成-skill推荐用法)一节。

不装 Skill 也能用——把 [`prompt.md`](prompt.md) 里的那段话贴进对话，
或直接用 `scripts/` 下的脚本命令行跑。

## 工作流程

基本流程为：

```text
统一身份认证
      ↓
保存本地登录状态
      ↓
获取当前账号可访问的课程活动信息
      ↓
生成文件清单
      ↓
用户确认
      ↓
下载并整理课程资料
```

项目不会尝试修改平台权限，也不会绕过服务端对课程资源的访问控制。

**装成 Skill 后，上面的流程由助手驱动**——你只需要在「用户确认」这一步看一眼清单。
命令行方式则每一步都由你自己触发。

## 附件来源的实际情况

思源学堂的附件不在 DOM 里，是前端通过接口动态渲染的，直接抓页面扫不到。而且附件分几类来源，只看一类会漏掉一部分：

| 来源 | 位置 | 典型内容 |
|---|---|---|
| ① 活动数据里的 `uploads` 数组 | `/api/courses/<ID>/activities` | 作业附件、**课堂录像** |
| ② `type=page` 活动正文 `data.content` 内嵌的 `/api/uploads/<id>` | 需逐个取活动详情再抽取 | **课件 PDF（几乎全在这）** |
| ③ `type=lecture_live` 活动数据里的回放地址 | 指向校外录播系统 | **教室直播回放** |

第二类藏在正文富文本的附件区块里，`uploads` 字段是 `null`——很容易误判成「这门课没有课件文件」。

**录像有两类，都已支持**，落到独立的 `录像/` 与 `回放/` 目录：

```bash
python scripts/lms_fetch.py --course <ID> --out ./课程资料              # 含录像
python scripts/lms_fetch.py --course <ID> --out ./课程资料 --no-video    # 只要讲义
```

- **`online_video`（课堂录像）** 就是普通附件，和课件共用下载端点，
  断点续传 / etag 校验全部适用
- **`lecture_live`（直播回放）** 走独立录播域名，由 `scripts/lms_live.py` 处理；
  默认只下「屏幕录制」机位，`--all-cameras` 可连教师机位一起下

两类录像的实现差异与注意事项见 [`SKILL.md`](SKILL.md)。

## 装成 Skill（推荐用法）

**推荐把它装成 Skill。** 装好之后不需要记任何命令，直接对 AI 说一句话就能跑完整流程：

> 帮我把这门课的资料都下载下来，课程链接：`https://lms.xjtu.edu.cn/course/<课程ID>/index`

AI 会自动完成「登录 → 干跑列清单 → 等你确认 → 下载 → 按章归类」，并在此过程中遵守
本项目的使用边界与负载控制策略。

### 安装：把文件夹放进助手的 skills 目录

`SKILL.md` 是通用的 Skill 格式，主流 AI 编程助手都认。把整个
`xjtu-siyuanxuetang-grab` 文件夹放进对应目录即可：

| 工具 | 放入目录 |
|---|---|
| WorkBuddy | `~/.workbuddy/skills/xjtu-siyuanxuetang-grab/` |
| Claude Code | `~/.claude/skills/xjtu-siyuanxuetang-grab/` |
| Codex | `~/.codex/skills/xjtu-siyuanxuetang-grab/` |
| 其他 | 该助手约定的 skills / prompts 目录 |

```
Windows:       C:\Users\<你>\.<助手目录>\skills\xjtu-siyuanxuetang-grab\
macOS / Linux: ~/.<助手目录>/skills/xjtu-siyuanxuetang-grab/
```

装好后无需重启会话之外的其他配置，也不需要把命令抄进对话——
助手匹配到「下载课程资料」这类场景时会自动读取 `SKILL.md` 并按其中的流程执行。

> Skill 是一层很薄的约定：目录下放一份 `SKILL.md`（YAML frontmatter + 正文），
> 助手匹配到场景时自动读取并执行。任何支持这套约定的工具都能用。
> `SKILL.md` 里已经写明使用边界（仅访问当前账号有权访问的课程、不并发、不重试权限错误），
> 所以装成 Skill 后**不会绕过这些约束**。

### 不想装 Skill 的两种替代

| 方式 | 做法 | 适合 |
|---|---|---|
| **Prompt** | 把 [`prompt.md`](prompt.md) 贴进任意对话式 AI，改掉两处 `【】` | 助手不支持 Skill 机制，或只想临时用一次 |
| **命令行** | 直接 `python scripts/lms_fetch.py ...`（见下方「快速开始」） | 自己完全控制每一步，或做脚本化/定时任务 |

两种方式都不需要 `SKILL.md` 被助手识别。此时 `SKILL.md` 相当于一份完整的操作手册，
`prompt.md` 则是可复制的通用 Prompt。

## 快速开始

```bash
# 1. 装依赖（只有登录脚本需要 playwright）
pip install playwright

# 2. 登录一次（弹浏览器，手动过统一身份认证，登录态落盘）
python scripts/lms_login.py --course <课程ID>

# 3. 干跑，先看清单再决定
python scripts/lms_fetch.py --course <课程ID> --out "./课程资料" --dry-run

# 4. 正式下载（幂等，可反复跑做增量补件）
python scripts/lms_fetch.py --course <课程ID> --out "./课程资料"
```

**想要开箱就是分好章的结构？** 加个 `--organize`：

```bash
python scripts/lms_fetch.py --course <课程ID> --out "./课程资料" --organize --dry-run   # 先看归类对不对
python scripts/lms_fetch.py --course <课程ID> --out "./课程资料" --organize
```

**有些课带录像**，两类都自动落到独立目录，不跟讲义混在一起：

```bash
python scripts/lms_fetch.py --course <课程ID> --out "./课程资料" --dry-run    # 输出里会写「其中课堂录像: N」
python scripts/lms_fetch.py --course <课程ID> --out "./课程资料" --no-video   # 只要讲义，跳过录像
```

| 类型 | 落到 | 说明 |
|---|---|---|
| `online_video` 课堂录像 | `录像/<活动标题>/` | 就是普通附件，原样复用下载逻辑 |
| `lecture_live` 直播回放 | `回放/<活动标题>/` | 走独立录播域名，默认只下屏幕录制机位 |

录像往往几百 MB（一节课 90 分钟的回放可达数百 MB），**下之前先看干跑的体积**。
录像与回放都不参与 `--organize`——标题认不出章号，硬套只会全堆进 `其他/`。

```
课程资料/
├── 课件/
├── 作业/
├── 录像/                                    ← online_video
│   └── 第1讲 课程介绍/
│       └── 第1讲课程介绍.mp4
└── 回放/                                    ← lecture_live
    └── <日期>-<课程名>/
        └── <日期>-<课程名>-<时间戳>-encoder.mp4
```

> 回放文件名里的时间戳不是装饰：同一天的多个活动 **`title` 可能完全相同**，
> 只有开课时间字段不同。只用标题命名会让多节课互相覆盖、只剩最后一节。

产物从 `课件/<活动标题>/` 变成：

```
课程资料/
├── 课件/
│   ├── 第01章 绪论/
│   ├── 第11章 卷积的应用--Harris, GFTT,SIFT特征/
│   └── 其他/                    ← 认不出章号的
└── 作业/
    ├── 第07章作业 Canny边缘检测/
    └── 其他/                    ← 实验1、大实验作业这类
```

章节号从活动标题和文件名两处抽，优先采信活动标题。已覆盖 `第1章 / 第一章 / 第1讲`、
`Lec11 / Lecture 3 / Chapter 2 / Unit 5`、纯数字开头 `01-绪论` 等写法。

**会主动避开这些「假章号」**——它们前面是实验/作业/项目编号，不是章次：

| 输入 | 结果 | 原因 |
|---|---|---|
| `实验1-数据库管理系统配置` + `1.1使用docker安装部署openGauss.pdf` | `其他/` | `1.1` 是小节号 |
| `Project 1 Dolly Zoom` + `Project1_Dollyzoom.zip` | `其他/` | 项目编号 |
| `大实验作业` + `实验报告模板.docx` | `其他/` | 实验编号 |
| `【第一章】作业` + `第1章作业题目.png` | `第01章作业/` | 这是真章号，该按章归 |

用系统自带的 Edge / Chrome 时**不需要**跑 `playwright install` 下载浏览器内核，脚本会自动探测。

## 目录内容

| 文件 | 用途 |
|---|---|
| `SKILL.md` | Skill 正文：流程、资源类型、注意事项 |
| `prompt.md` | 可直接贴进对话的 Prompt，改掉两处 `【】` 即可 |
| `scripts/lms_common.py` | 公共配置——**唯一**含机器相关逻辑的文件 |
| `scripts/lms_login.py` | 登录：Playwright 持久化 profile，登录态落盘 JSON |
| `scripts/lms_fetch.py` | 下载主体：列清单 / 下载 / 归类，支持续传、重试、干跑与增量 |
| `scripts/lms_organize.py` | 章节解析：中文数字转换、多写法匹配、假章号排除 |
| `scripts/lms_live.py` | 直播回放下载：串行请求、按响应长度校正偏移 |
| `tests/test_organize.py` | 章节解析离线自测，22 个用例 |
| `tests/test_fetch.py` | 下载逻辑离线自测，64 个用例（用假 opener 模拟服务端） |
| `ci.yml.txt` | CI 配置，用 `enable-ci.bat` 启用 |
| `release.yml.txt` | 打 tag 自动发版的 Actions 配置 |
| `pack.py.txt` | Release 工作流用的打包脚本 |
| `release.py` | 维护者用的一键发布脚本 |

## 参数与环境变量

| 参数 | 作用 |
|---|---|
| `--organize` | 自动按章整理：`课件/第01章 xxx/`，认不出放 `其他/` |
| `--list-only <文件>` | 只列清单不下载，导出 `.json` 或 `.csv` |
| `--manifest <文件>` | 下载后把清单+sha256 写入 `.json` 或 `.csv` |
| `--retries N` | 单文件重试次数，默认 3（指数退避） |
| `--no-verify` | 跳过下载前的登录态探测 |
| `--exclude "2020\|2021\|2022"` | 文件名正则，命中跳过（清理旧版作业） |
| `--no-video` | 跳过录像与回放，只要讲义时用 |
| `--all-cameras` | 直播回放默认只下「屏幕录制」机位，加此项连「教师机位」一起下 |
| `--split-projects` | 项目压缩包单独进 `项目/` |
| `--layout flat` | 平铺，不按活动建子文件夹 |
| `--dry-run` | 只打清单不下载；配 `-v` 会显示归类后的目标目录 |
| `-q` / `--quiet` | 不显示下载进度 |
| `--activities <json>` | 用本地清单，省一次请求 |
| `--base <url>` | 换平台地址（**别的 TronClass 学校可直接复用**） |

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `LMS_BASE` | `https://lms.xjtu.edu.cn` | 平台地址 |
| `LMS_CACHE` | `~/.lms-grab` | 登录态 / profile 目录 |
| `LMS_BROWSER` | 自动探测 | 强制指定浏览器 exe 绝对路径 |

浏览器探测顺序：`LMS_BROWSER` → 系统 Edge → 系统 Chrome → playwright 自带 chromium（Windows / macOS / Linux 三分支都已覆盖）。

## 安全与负载控制

项目有意避免可能给教学平台造成额外压力的行为。

网络请求采用保守策略：

- 默认串行处理较大的录像资源；
- 遇到 401 / 403 / 404 等响应不会进行无意义的高频重试；
- 网络异常采用有限次数的退避重试；
- 支持断点续传，避免重复下载已经完成的数据；
- 建议先使用 `--dry-run` 检查资源数量与体积。

请勿修改程序用于高并发扫描、批量枚举课程或其他超出正常个人学习用途的操作。

## 下载的可靠性设计

下载一门课动辄几十个文件、几百 MB，中途出问题很常见。这几层是刻意加的：

**断点续传。** 平台支持分片请求，所以中断后能带 `Range` 头从断点继续。
残片保留在 `<文件名>.part`，续传时会把残片计入 sha256 —— 否则哈希校验就是空的。

服务端若忽略 `Range`（返回 200 全量），脚本会丢掉残片重下，不会把新旧数据拼成脏文件。

**失败重试。** 超时、连接中断、5xx 会退避重试（默认 3 次）。但 **403 / 404 不重试**——
那是平台侧的限制（无权限或已删除），重试只是浪费时间。

**登录态探测。** 下载前先探一次接口。登录过期会明确报「登录态已失效，跑 `lms_login.py`」，
而不是让每个文件都显示成 `N/A` 让人误以为平台没给权限。退出码 `3` 专门表示这个情况。

**完整性校验。** 平台不提供官方哈希响应头，可用的信号是 `etag`。
脚本会用它和元信息大小交叉验证，并把本地算出的 sha256 写进 `--manifest`，供事后核对。

**文件名安全。** 长文件名截断时**保住扩展名**（超长才补 6 位短哈希防重名），
非法字符替换，路径穿越（`../../etc/passwd`）会被拍平。

## 开发者：跑测试

抓取逻辑最容易出回归，所以有**纯离线**测试，不需要网络和登录态：

```bash
python tests/test_organize.py     # 22 个用例
python tests/test_fetch.py        # 64 个用例
```

| 文件 | 覆盖 |
|---|---|
| `test_organize.py` | 中文数字转换、括号剥离、六种章节写法、假章号排除、目录名去重、目录名不带扩展名 |
| `test_fetch.py` | 下载成功/重试/403 不重试/5xx 重试/空响应/HTML 响应/sha256 不符/etag 不符、断点续传四场景、文件名安全、归类路径、清单导出、多来源计数、回放短读判定 |

`test_fetch.py` 用假 opener 脚本化服务端行为，所以能测「第一次超时第二次成功」这类
真实环境里很难复现的路径。

CI 在 push / PR 时自动跑三平台 × 三个 Python 版本，外加一步隐私自检——
确认仓库里没有误提交登录态、脚本里没有残留本机绝对路径。

### 启用 CI（可选）

**先说结论：不启用也完全能用。** 发布走本地方案（见下）就够了，
`release.py` 在打 zip 后已经强制跑过全部离线测试，测试不过就 `SystemExit`，不会发出坏包。

CI 的额外价值只有一个：**跨平台兼容性验证**（ubuntu / windows / macos × py3.8/3.10/3.12）。
如果你是 Windows 单平台使用，这个价值有限。

想启用的话，配置已经随包提供，只是放在 `.txt` 里。原因：GitHub 对
`.github/workflows/` 下的文件有**额外权限要求**（token 需带 `Workflows: write`），
通过 Contents API 推送会被 403 拒掉，所以只能在你本地还原后再用真 git 推上去。

```
双击 enable-ci.bat          # Windows
```

它会生成 `ci.yml`（测试 + 隐私自检）、`release.yml`（打 tag 自动发版）
和 `.github/scripts/pack.py`，然后 `git add / commit / push` 就生效了。
手动方式也一样简单：

```bash
mkdir -p .github/workflows .github/scripts
cp ci.yml.txt     .github/workflows/ci.yml
cp release.yml.txt .github/workflows/release.yml
cp pack.py.txt    .github/scripts/pack.py
```

> **前提：你的 git push 得是通的。** 如果 git 协议被代理拦（症状是
> `schannel: server closed abruptly` 或 `CONNECT tunnel failed, response 502`），
> 最后那步 `git push` 会失败 —— 而且 `release.yml` 本身也依赖「推 tag」触发，
> 同样用不上。这种情况下保持默认就好，用下面的方式 A 发布。

## 维护者：怎么发一个版本

### 方式 A：本地一键（推荐，本机 git 协议不通时唯一可行）

```bash
set GH_TOKEN=github_pat_xxx

python release.py --version 1.1.0 --title "下载可靠性" --dry-run   # 先看打包内容
python release.py --version 1.1.0 --title "下载可靠性" --yes       # 正式发
```

它会依次做：**版本号自检 → 打包 → 包体校验 → 打 tag → 建 Release → 传附件**。

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只打包+列清单，不碰 GitHub |
| `--push-code` | 发布前先把源码推到 main（注意：依赖旁边的 `gh_push_dir.py`） |
| `--notes notes.md` | 用文件里的内容当 Release 说明 |
| `--resume` | 上次发到一半中断了，只补缺的部分 |
| `--update-notes` | 只更新已发布 Release 的说明，不重新打包 |
| `--yes` | 跳过发布前确认。**脚本化/自动化调用必须加**，否则 `input()` 会 `EOFError` |

> **`--title` 只写版本号后面那部分。** 脚本自己会拼成 `v1.1.0 —— <title>`，
> 你写 `--title "v1.1.0 - 下载可靠性"` 会得到 `v1.1.0 —— v1.1.0 - 下载可靠性`。
> （新版已自动剥掉重复前缀，但仍建议只写后半段。）

> **改代码要在打包之前。** `release.py` 自己也在发布内容里（`INCLUDE` 含它），
> 如果打包后才改它，就会出现「zip 里是旧版、仓库里是新版」的不一致。
> 维护者工作区里 `_release/release.py` 和 `_release/xjtu-lms-grab/release.py`
> 必须同步 —— 后者才是会被打进包的那份。

`release.py` 放在仓库内外都能跑：它会自动判断自己在维护者工作区
（源目录是旁边的 `xjtu-lms-grab/`）还是在仓库内（源目录就是自己所在目录）。

**三层保护：**

1. **版本号自检** —— tag 已存在就报错退出。已发布的版本内容不可变，要改就发新版本号。
   （这条是踩过坑换来的：v1.0.0 曾被原地覆盖过。）
2. **包体校验** —— 上传前解压到临时目录，确认关键文件齐全、没混进登录态、
   离线测试能过（两个测试文件，共 86 个用例；用例数由测试自己报出，并与
   静态扫描的 `def test_` 数量交叉核对，对不上就告警）。校验不过就不发。
3. **打包白名单** —— 用 `INCLUDE` 显式列出该打进去的东西，新文件必须手动加；
   另有体积上限兜底，防止课程资料误入。
   **这份清单和 CI 用的 `pack.py.txt` 必须保持一致** —— 否则本地发的包和 CI 发的包内容不同。

## 文档站

线上地址：**https://xljfz.github.io/xjtu-siyuanxuetang-grab/**

站点是 `docs/index.html` 一个单页（纯静态、零依赖、无构建），
由 GitHub Pages 从 `main` 分支的 `/docs` 目录发布。改完推上去即可：

```bash
python push_docs.py
```

页面改动相关的完整说明（发布源为什么这么选、设置页的两个坑、响应式布局注意事项、
怎么验证部署结果）见 **[docs/MAINTAINING.md](docs/MAINTAINING.md)**。

### 方式 B：GitHub Actions 自动发

启用 workflow 后（见上），打一个 tag 就自动发布：

```bash
git tag v1.1.0 && git push origin v1.1.0
```

或在仓库 Actions 页面手动触发 `Release`，输入版本号。

> 前提同样是 **git push 通**。不通就只能走方式 A。

## 使用中的注意事项

1. **附件来源不止一处。** 课件 PDF 藏在活动正文里，只看附件数组会误判成「这门课没有课件文件」。
2. **文件名要从平台接口的元信息取，别从正文链接文字猜。** 正文显示名与实际文件名常不一致。
3. **403 / 404 是平台侧限制，不是下载失败。** 个别附件元信息取不到（无权限 / 已删除），脚本会标 `N/A` 跳过，别反复重试。

另外：启动 URL **不要带 `#/`**——带 hash 会让页面路由异常，渲染进程假死。

## 隐私与凭据

本项目不会要求用户将账号密码写入代码。

登录由浏览器和学校统一身份认证系统完成，随后只在用户本机保存必要的会话状态。

任何与登录凭据、Cookie、浏览器 profile 相关的文件都已通过 `.gitignore` 排除，
不应提交到 Git 仓库。提交代码前仍建议运行 `git status` 确认。

## 关于课程资源

本仓库仅包含下载与整理工具本身。

**仓库中不应提交：** 课程课件、教师 PPT、作业答案、课堂录像、课程回放、学生名单、
平台登录状态或其他未经授权公开的教学资源。

如果课程教师或学校对某些资源另有使用限制，应以对应要求为准。

## 移植性

仓库内**不含任何本机专属路径**。机器相关逻辑全部收敛在 `scripts/lms_common.py` 一个文件里（域名 / 缓存目录 / 浏览器探测），换台电脑直接可用。

登录态默认落在 `~/.lms-grab/`，按课程 ID 分开存，不污染项目目录。

## 更新日志

| 版本 | 变更 |
|---|---|
| v1.2.1 | 修 `collect()` 的来源②计数（原先用总数相减，把直播回放误算成「正文内嵌」）；`release.py` 包体校验改为上报测试实际执行的用例数并与静态扫描交叉核对；抽取 `download()` 中重复四次的失败处理块；回放下载新增短读判定（对比 `Content-Length`，超阈值告警并写入清单）；README 用例数与文件清单同步 |
| v1.2.0 | 直播回放下载（`lms_live.py`）；修同一天多个 `lecture_live` 活动 title 相同导致回放互相覆盖的丢数据 bug（文件名加入本地时间戳与机位）；离线测试扩到 79 个 |
| v1.1.0 | 下载可靠性：断点续传、自动重试、sha256 校验、etag 交叉验证、登录态探测、进度条、`--list-only` / `--manifest`、语义化退出码；修长文件名丢扩展名；测试扩到 60 个 |
| v1.0.4 | `release.py` 纳入发布内容并支持两种存放位置；新增 `--update-notes` |
| v1.0.3 | 新增本地一键发布脚本 `release.py`：版本号自检 + 包体校验 + 打包白名单 |
| v1.0.2 | 新增 `--organize` 按章整理；新增离线测试与 CI；`.gitignore` 补漏（profile 目录、压缩包、缓存） |
| v1.0.1 | 安装说明泛化到 WorkBuddy / Claude Code / Codex，强调不装 Skill 也能用；仓库改名 xjtu-siyuanxuetang-grab |
| v1.0.0 | 首个版本：登录、两类附件来源合并、增量下载、干跑 |

## License

本项目代码按照仓库中的开源许可证发布。

许可证仅适用于本项目自身代码，并不意味着通过本工具访问或下载的第三方课程内容同时获得相同授权。
