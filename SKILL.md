---
name: xjtu-lms-grab
description: 抓取西安交大思源学堂 2.0（lms.xjtu.edu.cn，TronClass）任意课程的课件、作业、项目压缩包。当用户给出 lms.xjtu.edu.cn/course/<id> 之类链接，或说「下载这门课的课件/作业」「把课上资料拉下来」时使用。覆盖：Playwright 持久化 profile 登录一次并落盘登录态、两类附件来源（uploads 字段 + 正文内嵌）、下载端点 /api/uploads/<id>/blob、按章/按活动归类、增量补件、断点续传 + 自动重试 + sha256 校验、`--list-only` 只导清单不下文件。
agent_created: true
---

# 思源学堂 2.0 课程资料抓取

适用：用户给一个 `lms.xjtu.edu.cn/course/<课程ID>` 链接，要课件 / 作业 / 项目文件。

脚本在 `scripts/`，**不含任何本机专属路径**，换台机器直接用。

### 环境要求

- Python 3.8+。系统自带的就行，不需要特定环境。
- `lms_login.py` 需要 playwright：`pip install playwright`
  （用系统 Edge / Chrome 的话**不必**跑 `playwright install`，脚本会自动探测浏览器）
- `lms_fetch.py` 只用标准库，不依赖浏览器。

### 默认落盘位置

登录态和浏览器 profile 默认放 **`~/.lms-grab/`**（`C:\Users\<你>\.lms-grab` 或 `/home/<你>/.lms-grab`），
按课程 ID 分开存。想改位置用 `--state` / `--profile`，或设环境变量 `LMS_CACHE`。

## 流程

### 1. 先问清课程 ID 和输出目录
ID 从 URL 里取：`/course/33593/` → `33593`。
输出目录默认就是该课程的工作区根（如 `D:\计算机视觉与模式识别`）。

### 2. 登录（只需一次）
```bash
python scripts/lms_login.py --course <ID>
```
- 会弹出浏览器窗口，用户手动完成统一身份认证。
- 登录态落到 `~/.lms-grab/state_<ID>.json`，顺带存活动清单。
- **profile 目录也保留登录态**，第二次跑会直接跳过登录。
- 浏览器自动探测顺序：环境变量 `LMS_BROWSER` → Edge → Chrome → playwright 自带 chromium
  （Windows / macOS / Linux 三条分支都写了）。

> [!warning] 启动 URL 不要带 `#/`
> 带 hash 会让 SPA 卡成 `index#/#%2F`，渲染进程假死。脚本里用的是 `/course/<ID>/index`。

### 3. 先干跑看清单
```bash
python scripts/lms_fetch.py --course <ID> --out "<目录>" --dry-run
```
把清单给用户过一眼再真下，避免下了一堆不需要的旧版本。

### 4. 正式下载
```bash
python scripts/lms_fetch.py --course <ID> --out "<目录>"
```
幂等：已存在且 >1KB 的文件自动跳过，可以反复跑做增量补件。

## 下载的可靠性设计

长任务最怕「跑了十分钟断在中途、还得从头再来」。这几条都是为它加的：

| 能力 | 说明 |
|---|---|
| **断点续传** | 服务端支持 `accept-ranges: bytes`。中断后残片留在 `<文件>.part`，重跑时带 `Range` 头接着下，返回 206 才认。服务端若忽略 Range（返回 200）则丢弃残片重下，不会拼出错文件。 |
| **自动重试** | 默认 3 次，指数退避。只重试「可能自己好」的错误（超时 / 5xx / 连接重置），401/403/404 直接放弃不浪费时间。`--retries N` 可调。 |
| **sha256 校验** | 边下边算，结果写进 `--manifest`。续传的部分也会计入哈希，最终值与完整下载逐字节一致。 |
| **etag 交叉验证** | 思源学堂的 etag 形如 `"698e9012-cb2ec"`，后半段是十六进制文件大小（`0xcb2ec` = 832236）。下载完拿它跟实际字节数对一遍。 |
| **登录态探测** | 开跑前先打一个轻量 API，401 就明确告诉你「登录态过期了，重跑 lms_login.py」，而不是等到下 20 个文件全 401 才发现。`--no-verify` 可跳过。 |
| **进度显示** | 单文件百分比 + 当前/总数。`-q` 关掉。 |

> [!note] 服务端不提供 sha256 头
> 实测响应头只有 `etag` / `last-modified` / `content-length`，没有官方哈希。
> 所以完整性是这样做的：本地 sha256 存进 manifest 供事后复核，再用 etag 里的字节数交叉验证。

## 关键参数

| 参数 | 作用 |
|---|---|
| `--list-only FILE` | **只导清单不下载**，写入 `.json` 或 `.csv`。用来先看清有什么、算总体积 |
| `--manifest FILE` | 下载完导出清单 + 每个文件的 sha256 与大小 |
| `--retries N` | 单文件重试次数，默认 3 |
| `--no-verify` | 跳过开跑前的登录态探测 |
| `--exclude "2020\|2021\|2022"` | 文件名正则，命中就跳过（清旧版作业很好用） |
| `--no-video` | 跳过课堂录像与直播回放。录像动辄几百 MB，只要讲义时用得上 |
| `--all-cameras` | 直播回放默认只下「屏幕录制」机位，加此项连「教师机位」一起下 |
| `--split-projects` | 项目压缩包单独进 `项目/`，不混在作业里 |
| `--layout flat` | 平铺，不按活动建子文件夹 |
| `--activities <json>` | 用本地清单，省一次请求 |
| `--base <url>` | 换平台地址（别的学校用同一套 TronClass 时） |
| `-q` / `-v` | `-q` 不显示进度；`-v` 干跑时打印归类后的目标目录 |

### 退出码

脚本的退出码有语义，`&&` 串联或 CI 里判断很有用：

| 码 | 含义 |
|---|---|
| `0` | 全部成功 |
| `2` | 找不到登录态，需要先跑 `lms_login.py` |
| `3` | 登录态已过期 |
| `4` | 部分文件下载失败（清单里标了是哪些） |

### 5. 环境变量（不想改代码就用这些）

| 变量 | 作用 |
|---|---|
| `LMS_BASE` | 平台地址，默认 `https://lms.xjtu.edu.cn` |
| `LMS_CACHE` | 登录态 / profile 目录，默认 `~/.lms-grab` |
| `LMS_BROWSER` | 强制指定浏览器 exe 的绝对路径 |

### 6. 用 `--exclude` 清旧版前先确认「有没有唯一版本」
不同年份的作业 PDF 常同时存在（课件区挂 2021 版，作业区挂 2025 版）。按年份批量排除时，
**先确认该章节在作业区是否有新版**，否则可能把唯一版本一起排除掉。
（实例：计算机视觉课的第六章作业只有课件区的 2021 版，作业区根本没有新版。）

## 接口速查

| 用途 | 端点 |
|---|---|
| 全部活动（含 uploads 字段） | `GET /api/courses/<ID>/activities?sub_course_id=0` |
| 单个活动详情 | `GET /api/activities/<活动id>?sub_course_id=0` |
| 课件分页列表 | `GET /api/course/<ID>/coursewares?conditions=...` |
| 附件元信息（**取文件名用它**） | `GET /api/uploads/<上传id>` |
| **下载** | `GET /api/uploads/<上传id>/blob` |
| 直播回放地址 | 活动详情 `data.external_live_detail.replay_videos[].url`（指向 rms-v5，非本域名） |

页面路由：课件 `/course/<ID>/courseware#/` · 作业 `/course/<ID>/homework#/` · 章节 `/course/<ID>/content#/`
· 活动详情 `/course/<ID>/learning-activity#/<id>`

## 三个必踩的坑

> [!important] ① 附件有两类来源，只看 `uploads` 会漏掉一大半
> - **① 活动 JSON 的 `uploads` 数组** —— 作业附件在这。
> - **② `type=page` 活动正文富文本 `data.content` 里的 `/api/uploads/<id>`** —— **课件 PDF 几乎全在这**，
>   藏在 `div.ccbb-attachments` 里。`uploads` 字段是 `null`，很容易误判成「这门课没有课件文件」。
> 判断「有没有附件」时，**`uploads` 为空 ≠ 没附件**，必须再查 `data.content`。

> [!warning] ② 文件名要从 `/api/uploads/<id>` 的 `name` 取，不要从正文硬猜
> 正文显示的链接名和真实文件名常不一致（正文写 `SIFT特征.pdf`，实际是
> `Lec11-卷积的应用--Harris, GFTT,SIFT特征.pdf`）。

> [!note] ③ 403 / 404 是平台侧限制，不是下载失败
> 个别附件元信息就取不到（403 无权限、404 已删除），脚本会标 `N/A` 跳过，不用反复重试。
> `/api/uploads/<id>/download` 是 404，正确端点是 `/blob`。

## 课堂录像

思源学堂的录像有**两种完全不同的承载方式**，脚本对两类都支持，但实现路径不同。

| 类型 | 目录 | 端点 | 校验 |
|---|---|---|---|
| `online_video` | `录像/` | `/api/uploads/<id>/blob`（同课件） | etag 大小 + sha256 |
| `lecture_live` | `回放/` | `rms-v5.xjtu.edu.cn/.../preview?previewToken=` | 只有 Content-Length |

```bash
python scripts/lms_fetch.py --course <ID> --out ./课程资料              # 全都要
python scripts/lms_fetch.py --course <ID> --out ./课程资料 --no-video    # 只要讲义
```

### 类型 A：`online_video` —— 就是普通附件

录像躺在活动的 `uploads[]` 里，和课件走同一个端点：

```
activities[i].type == "online_video"
  .uploads[0] = {id: 76161, name: "xxx_标清.mp4", size: 5618914, ...}
GET /api/uploads/<id>/blob
```

实测（2026-09，20 个此类活动，含 `.mp4` / `.flv`）：`Content-Type: video/mp4`、
带 `Content-Length`、`accept-ranges: bytes`，**断点续传与 etag 校验全部可用**，
下载字节数与 API 声明的 `size` 逐字节一致。

> [!tip] 哪些课有
> 各课差异极大——建筑设计类课程常有（某门 CAD 课 17 个），理论课往往一个没有。
> 先 `--dry-run` 看输出里的「其中课堂录像: N」一行。

### 类型 B：`lecture_live` —— 直播回放，另一套域名

教室录播走的是**完全独立**的一套系统，不在 `uploads` 里：

```
activities[i].type == "lecture_live"
  .data.external_live_detail.replay_videos[] = [
      {camera_id: 960821, camera_type: "instructor", url: "..."},   # 教师机位
      {camera_id: 960824, camera_type: "encoder",    url: "..."},   # 屏幕录制
  ]
```

URL 指向 `rms-v5.xjtu.edu.cn`（不是 `lms.xjtu.edu.cn`），由 `scripts/lms_live.py` 处理。
默认只下 `encoder`（屏幕录制 = 正课画面），`--all-cameras` 连 `instructor` 一起下。

#### 下载契约（实测）

```
https://rms-v5.xjtu.edu.cn/api/base/orgs/xjtu/captures/<capture_id>/videos/<camera_id>/preview?previewToken=<hex>
```

响应头（`GET`，不带 Range）：

```
Content-Length: 438175558        ← 完整大小，可作进度与校验依据
Accept-Ranges:  bytes            ← 支持断点续传
Etag:          "d4a1f1b58094be3d0b03424470c3c342"
```

带 `Range` 返回 `206`，`Content-Range: bytes 100000000-102097151/438175558`。

> [!warning] 三个必须注意的实测行为
> **① `previewToken` 会限流，别并发。** 同一 token 同时发多个请求（GET + HEAD + Range 混着来）
> 会稳定返回 403，看起来像 token 过期。**串行发就完全正常**——实测单连接连续读 438 MB /
> 4.6 分钟无一次中断，token 跨多轮请求也不失效。排查时如果看到 403，先怀疑并发而不是时效。
>
> **② 服务端会把 Range 对齐到 2 MB 边界。** 请求 `bytes=100000000-100199999`（20 万字节）
> 实际返回 `bytes 100000000-102097151`（2 MB）。**必须按响应里的 `Content-Range` 用实际长度**，
> 不能假设服务端照办请求值，否则续传时会错位。
>
> **③ 读取速度会衰减。** 前 30 秒能到 8 MB/s（吃服务端缓存），之后稳定到 ~1.6 MB/s
> （实时转码速度）。一节课（438 MB / 90 分钟）约需 5 分钟下完。进度条必须显示，别让人以为卡死。

> [!important] 文件名必须带时间戳，否则会丢数据
> 同一天的多个 `lecture_live` 活动 **`title` 完全相同**。实测一门课 4 节课都叫
> 「2026-09-19-计算机视觉与模式识别」，只有 `start_time` 不同（06:30Z / 07:30Z / 08:40Z / 09:40Z）。
> 只用标题命名会让 4 节课**互相覆盖、最终只剩最后一节**。
> 现在文件名形如 `<标题>-<YYYYMMDD-HHMM 本地时间>-<机位>.mp4`，
> `start_time` 在活动详情的**顶层**，不在 `data` 里。

> [!warning] 别把 `lecture_live` 和 `online_video` 搞混
> 名字里都带「视频」，但一个是附件、一个是流媒体。判断方式很简单：看 `type` 字段。
> 两类**现在都能下**，不用再挑。区别只在体量与耗时：`online_video` 是普通附件，
> 一个几十 MB，下载很快；`lecture_live` 回放单节 350–440 MB，且服务端是实时转码
> （~1.6 MB/s），一节 90 分钟的课要约 5 分钟。
>
> 数量上别按课的类型想当然。全量扫过 58 门课，`online_video` 共 20 个、`lecture_live`
> 出现在少数几门课里，且分布很偏：
>
> | 课程 | `online_video` | `lecture_live` |
> |---|---|---|
> | 计算机辅助建筑设计【04 | 17 | 0 |
> | 传统木构与营造做法 | 2 | 8 |
> | 计算机视觉与模式识别 | 1 | 4 |
>
> 建筑设计类课程能攒到 17 个录像——**「录像很少」是错觉**，开扫前别预设。

## 归类建议

下载产物默认 `<目录>/{课件,作业,录像,回放}/<活动标题>/<文件名>`。

**录像与回放不参与 `--organize`**：标题基本认不出章号，硬套只会让几十个视频全堆进
`录像/其他/`。所以这两类始终按活动标题分目录，文档部分照常按章归并。

**优先试 `--organize`**：脚本会从活动标题和文件名里自动抽chapter号，归到
`课件/第01章 绪论/` 这样的结构，认不出章号的统一进 `其他/`。
```bash
python scripts/lms_fetch.py --course <ID> --out ./课程资料 --organize --dry-run
```
先用 `--dry-run` 看一遍归类结果再正式跑——**解析规则是启发式的，不同课的花样不同**。

规则在 `scripts/lms_organize.py`，已覆盖这些写法：`第1章 / 第一章 / 第1讲`、
`Lec11 / Lecture 3 / Chapter 2 / Unit 5`、纯数字开头 `01-绪论`。
并且会主动**排除**这些不是章号的数字：`实验1`、`作业2`、`项目3`、`1.1使用docker…`（小节号）、
`第七章作业（2021版）`（会被识别为第7章，这是期望行为）。

只有一类情况需要人工干预：活动标题本身就是章节名、文件名又是别的东西——
此时优先采信活动标题。

## 环境备注

- 浏览器自动探测，别手写绝对路径。顺序：系统 Edge → Chrome → playwright 自带 chromium。
- 别手写裸 CDP：Edge 会冻结后台标签页（冻结的渲染进程不执行 JS，`Runtime.evaluate` 必超时），
  且事件回调里再发命令会造成 WebSocket 重入死锁。Playwright 已把这些都处理掉。
- 长命令的 stdout 在某些机器上会丢，跑长任务建议重定向到日志文件。
- `scripts/lms_common.py` 是所有「机器相关」配置的唯一出处（域名 / 缓存目录 / 浏览器探测），
  移植到新环境只需看这一个文件。
