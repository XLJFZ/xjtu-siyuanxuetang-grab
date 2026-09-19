<div align="center">

# xjtu-siyuanxuetang-grab

**思源学堂 2.0 课程资料一键抓取 —— AI Skill + Prompt + 脚本**

把西安交大 [思源学堂 2.0](https://lms.xjtu.edu.cn)（TronClass 平台）任意课程的课件、作业、项目压缩包一次性拉到本地。

实测：单门课 **58 个文件 / 530 MB / 0 失败**。

</div>

---

## 这是什么

一个给 AI 用的 Skill。装好以后，你只要说一句：

> 帮我把这门课的资料都下载下来，课程链接：`https://lms.xjtu.edu.cn/course/<课程ID>/index`

AI 就会自动走完「登录 → 干跑列清单 → 确认 → 下载 → 归类」的完整流程。

不装 Skill 也能用——把 [`prompt.md`](prompt.md) 里的那段话贴进对话，配合 `scripts/` 下的脚本手动跑。

## 为什么需要它

思源学堂的附件不在 DOM 里，是 AngularJS 调 API 渲染的，**直接爬页面永远扫不到**。而且附件分两类来源，只看一类会漏掉一半以上：

| 来源 | 位置 | 典型内容 |
|---|---|---|
| ① 活动 JSON 的 `uploads` 数组 | `/api/courses/<ID>/activities` | 作业附件 |
| ② `type=page` 活动正文 `data.content` 内嵌的 `/api/uploads/<id>` | 需逐个拉 `/api/activities/<id>` 再正则抽取 | **课件 PDF（几乎全在这）** |

第二类藏在正文富文本的 `div.ccbb-attachments` 里，`uploads` 字段是 `null`——很容易误判成「这门课没有课件文件」。

## 安装

`SKILL.md` 是通用的 Skill 格式，主流 AI 编程助手都认。把 `xjtu-siyuanxuetang-grab`
文件夹放进对应助手的 skills 目录即可：

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

> Skill 是一层很薄的约定：目录下放一份 `SKILL.md`（YAML frontmatter + 正文），
> 助手匹配到场景时自动读取并执行。任何支持这套约定的工具都能用。

**不装 Skill 也能用。** 脚本是独立的命令行工具，不依赖任何助手——把仓库 clone 或解压到
任意位置直接跑即可（用法见下方「快速开始」）。此时 `SKILL.md` 相当于一份操作手册；
`prompt.md` 则是通用 Prompt，贴进任意对话式 AI 就能让它照着做，不需要它支持 Skill 机制。

## 快速开始

```bash
# 1. 装依赖（只有登录脚本需要 playwright）
pip install playwright

# 2. 登录一次（弹浏览器，手动过统一身份认证，登录态落盘）
python scripts/lms_login.py --course <课程ID>

# 3. 干跑，先看清单再决定
python scripts/lms_fetch.py --course <课程ID> --out "./CV" --dry-run

# 4. 正式下载（幂等，可反复跑做增量补件）
python scripts/lms_fetch.py --course <课程ID> --out "./CV"
```

用系统自带的 Edge / Chrome 时**不需要**跑 `playwright install` 下载浏览器内核，脚本会自动探测。

## 目录内容

| 文件 | 用途 |
|---|---|
| `SKILL.md` | Skill 正文：流程、接口速查、三个必踩的坑 |
| `prompt.md` | 可直接贴进对话的 Prompt，改掉两处 `【】` 即可 |
| `scripts/lms_common.py` | 公共配置——**唯一**含机器相关逻辑的文件 |
| `scripts/lms_login.py` | 登录：Playwright 持久化 profile，登录态落盘 JSON |
| `scripts/lms_fetch.py` | 抓取主体：列清单 / 下载 / 归类，支持干跑与增量 |

## 参数与环境变量

| 参数 | 作用 |
|---|---|
| `--exclude "2020\|2021\|2022"` | 文件名正则，命中跳过（清理旧版作业） |
| `--split-projects` | 项目压缩包单独进 `项目/` |
| `--layout flat` | 平铺，不按活动建子文件夹 |
| `--activities <json>` | 用本地清单，省一次请求 |
| `--base <url>` | 换平台地址（**别的 TronClass 学校可直接复用**） |

| 环境变量 | 默认值 | 作用 |
|---|---|---|
| `LMS_BASE` | `https://lms.xjtu.edu.cn` | 平台地址 |
| `LMS_CACHE` | `~/.lms-grab` | 登录态 / profile 目录 |
| `LMS_BROWSER` | 自动探测 | 强制指定浏览器 exe 绝对路径 |

浏览器探测顺序：`LMS_BROWSER` → 系统 Edge → 系统 Chrome → playwright 自带 chromium（Windows / macOS / Linux 三分支都已覆盖）。

## 三个必踩的坑

1. **`uploads` 为空 ≠ 没有附件。** 课件 PDF 藏在正文 `data.content` 里，必须两个来源都扫。
2. **文件名要从 `/api/uploads/<id>` 的 `name` 取，别从正文链接文字猜。** 正文写 `SIFT特征.pdf`，实际文件可能叫 `Lec11-卷积的应用--Harris, GFTT,SIFT特征.pdf`。
3. **403 / 404 是平台侧限制，不是下载失败。** 个别附件元信息取不到（无权限 / 已删除），脚本会标 `N/A` 跳过，别反复重试。下载端点用 `/blob`，`/download` 是 404。

另外：启动 URL **不要带 `#/`**——带 hash 会让 SPA 卡成 `index#/#%2F`，渲染进程假死。

## 移植性

仓库内**不含任何本机专属路径**。机器相关逻辑全部收敛在 `scripts/lms_common.py` 一个文件里（域名 / 缓存目录 / 浏览器探测），换台电脑直接可用。

登录态默认落在 `~/.lms-grab/`，按课程 ID 分开存，不污染项目目录。

## 出处

2026-09-19 从《计算机视觉与模式识别》（课程 33593）的一次实战抓取中提炼。当天实测下载 58 个文件 / 530.5 MB / 0 失败。

## License

MIT
