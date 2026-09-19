---
name: xjtu-lms-grab
description: 抓取西安交大思源学堂 2.0（lms.xjtu.edu.cn，TronClass）任意课程的课件、作业、项目压缩包。当用户给出 lms.xjtu.edu.cn/course/<id> 之类链接，或说「下载这门课的课件/作业」「把课上资料拉下来」时使用。覆盖：Playwright 持久化 profile 登录一次并落盘登录态、两类附件来源（uploads 字段 + 正文内嵌）、下载端点 /api/uploads/<id>/blob、按章/按活动归类、增量补件。
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

## 关键参数

| 参数 | 作用 |
|---|---|
| `--exclude "2020\|2021\|2022"` | 文件名正则，命中就跳过（清旧版作业很好用） |
| `--split-projects` | 项目压缩包单独进 `项目/`，不混在作业里 |
| `--layout flat` | 平铺，不按活动建子文件夹 |
| `--activities <json>` | 用本地清单，省一次请求 |
| `--base <url>` | 换平台地址（别的学校用同一套 TronClass 时） |

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

## 归类建议

下载产物默认 `<目录>/{课件,作业}/<活动标题>/<文件名>`。若要按章整理（如 `课件/第01章 绪论/`），
需要人工把活动标题映射到章节号——不同课程映射不同，别硬编码。

## 环境备注

- 浏览器自动探测，别手写绝对路径。顺序：系统 Edge → Chrome → playwright 自带 chromium。
- 别手写裸 CDP：Edge 会冻结后台标签页（冻结的渲染进程不执行 JS，`Runtime.evaluate` 必超时），
  且事件回调里再发命令会造成 WebSocket 重入死锁。Playwright 已把这些都处理掉。
- 长命令的 stdout 在某些机器上会丢，跑长任务建议重定向到日志文件。
- `scripts/lms_common.py` 是所有「机器相关」配置的唯一出处（域名 / 缓存目录 / 浏览器探测），
  移植到新环境只需看这一个文件。
