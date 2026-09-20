# 维护者指南

面向修改这个仓库的人。使用者请看 [README](../README.md)。

发版流程、CI 配置、测试矩阵、完整更新日志都在这份文档里，README 只留使用者需要的内容。

## README 与本文档的分工（2026-09-20 精简后）

README 面向「拿到仓库想用它下课件的人」，只保留：Skill 定位、快速开始、参数、
可靠性设计、注意事项、隐私、目录清单、跑测试的命令、最近两版更新日志。

以下内容**只在本文档维护**，README 里只留一行链接：

| 内容 | 在哪 |
|---|---|
| 发版流程（方式 A / 方式 B）、四层保护、`release.py` 参数 | 本文「发版流程」 |
| CI / 手动备用发版的配置细节、workflows 权限 | 本文「CI 配置」+「GitHub Actions 的实测坑」 |
| 文档站的发布与排错 | 本文「文档站」 |
| 测试覆盖矩阵、时区用例、`FakeOpener` 范式 | 本文「测试矩阵与覆盖明细」 |
| v1.0.0 ~ v1.2.1 的历史更新日志 | 本文「完整更新日志」 |

改 README 时注意：顶部引用块里的锚点 `#装成-skill推荐用法` 指向
「装成 Skill（推荐用法）」一节，**该标题不能改名**，否则链接失效。

---

## 文档站（GitHub Pages）

**线上地址**：https://xljfz.github.io/xjtu-siyuanxuetang-grab/

站点是仓库里 `docs/` 目录的一个单页，纯静态 HTML + CSS，**零依赖、无构建步骤**。
Pages 直接读 `main` 分支的 `/docs` 目录发布，没有打包环节。

### 改内容

改 `docs/index.html`，然后推上去：

```bash
set GH_TOKEN=<你的 token>
python push_docs.py
```

`push_docs.py` 只推 `docs/` 下的文件，**不碰仓库里其他任何东西**。
推完 Pages 会自动重建，约 1 分钟生效，不需要再去网页操作。

推之前建议先 `--dry-run` 确认清单：

```bash
python push_docs.py --dry-run
```

### 为什么另外写 `push_docs.py`，而不用 `tools/gh_push_dir.py`

`tools/gh_push_dir.py` 推的是**发布白名单**（`tools/release_common.py` 的
`INCLUDE`），范围是整个项目源码。文档站只想更新 `docs/` 一个目录，
没必要让一次文档改动带着源码一起进 diff。

`push_docs.py` 把顶层目录写死成 `docs`，职责单一。
（顺带说明：新版 `gh_push_dir.py` 已经只对变化文件建 blob、且无变化时不建
commit，所以用它推也不会再产生一堆空 commit —— 分开的原因是范围，不是效率。）

### 为什么发布源是 `main/docs` 而不是 `gh-pages` 分支

新建 `gh-pages` 分支必须先 `git push`。**在本机 git 的写操作是被网络策略拦的**
（详见 README「维护者」一节），推不上去。

而 `main` 分支的 `/docs` 目录**不需要建任何新分支**，且推送走的是 Contents API——
和推 `scripts/*.py` 是同一个端点，没有任何额外要求。

| 发布源 | 要建新分支 | 可用性 |
|---|---|---|
| `main` 分支根目录 | 否 | ⚠️ 会把仓库根当网站，源码全变成可下载文件 |
| **`main` 分支 `/docs`** | **否** | ✅ 当前方案 |
| `gh-pages` 分支 | 是 | ❌ 卡在 `git push` |

### 首次开启 Pages 只能手动做（API 会 403）

```bash
POST /repos/<owner>/<repo>/pages
# → 403 Resource not accessible by personal access token
```

Pages 需要**独立的 `Pages: write` 权限**，只有 `Contents: write` 不够。
所以这一步必须去网页点，之后更新内容才全自动。

**操作路径**（`/settings/pages`）：

1. 「来源」下拉框 → **从分支部署**（Deploy from a branch）
2. 分支 → `main`
3. 文件夹 → **`/docs`**
4. 保存

### ⚠️ 设置页的两个坑

**坑一：「来源」现在默认是 `GitHub Actions`。**

如果默认显示 Actions，下面会给 Jekyll / Static HTML 两个模板卡片。
**别点那两个模板的「设置」**——它们会往 `.github/workflows/` 推 workflow 文件，
而这需要 `Workflows: write` 权限，用 Contents 权限的 token 会 403 失败。
（仓库里已有自己的 workflow，别让 Pages 模板再写一份进去。）

必须先手动把「来源」改成「从分支部署」，之后才会出现「分支 + 文件夹」两个下拉框。

**坑二：文件夹默认是 `/ (根目录)`，必须手动改成 `/docs`。**

忘了改的后果很隐蔽：Pages 去根目录找 `index.html` → 找不到 →
**站点返回 404，但设置页照常显示「已发布」，不报错**。
很容易误判成「部署成功了但 CDN 没生效」。

**排查顺序：先看设置页的文件夹选项，再怀疑别的。**

### 验证部署结果

改完推上去，别只看「页面能打开」——**逐字节比对才算验证**：

```python
import hashlib, urllib.request
URL = "https://xljfz.github.io/xjtu-siyuanxuetang-grab/"
local = open("docs/index.html", "rb").read()
req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
with urllib.request.urlopen(req, timeout=30) as r:
    body = r.read()
    print(r.status, len(body), hashlib.sha256(body).hexdigest()[:16])
    print("与本地一致:", body == local)
```

第一次请求可能拿到 CDN 缓存的**旧版本**，重试一两次即可。

想确认部署时间，看响应头比看内容更直接：

```bash
curl -sI https://xljfz.github.io/xjtu-siyuanxuetang-grab/
# Last-Modified 应晚于你最后一次 commit 的时间
```

#### ⚠️ 构建历史里的 `errored` 多半是假警报

用 API 查构建记录（`GET /pages/builds`）时，会看到这种输出：

```
92f6a1b2  built      created=13:42:00   err=None          ← 最终生效的就是它
d3f665da  errored    created=13:41:57   err=Page build failed.
a1bdaf10  built      created=13:39:27   err=None
c8ca2765  errored    created=13:39:22   err=Page build failed.
e2d1677e  errored    created=13:39:18   err=Page build failed.
5b592aca  built      created=13:34:56   err=None
```

**连推多个 commit 时，间隔几秒的那几条必然是 `errored`——这是正常现象，不是部署失败。**

原因：Pages 同时只跑一个构建。新 commit 一进来，正在跑的那次会被中止，
记录上就落成 `errored`，`error.message` 统一写成 `Page build failed.`。
**这是中止时的通用文案，不是真实错误原因**，不要照着这句话去排查内容。

看上面那段就能发现规律：**每次连续推送，最后一条必然是 `built`，被挤掉的必然 `errored`**
（`5b592aca → e2d1677e/c8ca2765 → a1bdaf10`，以及 `a1bdaf10 → d3f665da → 92f6a1b2`，
两轮完全同构）。GitHub 不会标注"被后来的取代了"，所以看起来像失败。

判断真实状态看这三条，不用纠结历史记录：

| 看什么 | 期望值 |
|---|---|
| 最后一条构建记录 | `built`（`err=None`） |
| 响应头 `Last-Modified` | 晚于最后一次 commit 时间 |
| 线上文件 sha256 | 与本地逐字节一致 |

**别被 `errored` 牵着去排查内容**——先比对 sha256，一致就说明已经生效了。

> **同一件事在 Actions 页面显示为红叉 `0/3`**，判读方法见下一节。

> 附注一：查 Pages 配置的 `GET /repos/<owner>/<repo>/pages` 有时会返回 **404**，
> 哪怕站点实际在正常运行。这是 token 权限范围的问题（该端点需要 `Pages` 读权限），
> 不是"站点被删了"。**以线上实测为准，不要以这个端点为准。**
>
> 附注二：验证时优先看响应头的 `X-Cache: MISS` + `Age: 0`——这表示命中源站新内容，
> 而非 CDN 缓存的旧版本。如果拿到 `X-Cache: HIT` 且 `Age` 较大，就是缓存，
> 过一会再请求。

#### ⚠️ 仓库首页/Commits 页上的红叉 `0/3`，同样是假警报

打开仓库首页的提交列表，或点进 Commits 页，会看到这种景象：

```
update: docs/index.html          ✓ 3/3      ← 绿勾
update: docs/MAINTAINING.md      ✗ 0/3      ← 红叉
update: docs/index.html          ✓ 3/3
update: docs/MAINTAINING.md      ✗ 0/3
add:    docs/MAINTAINING.md      ✗ 0/3
```

**这些红叉跟 `errored` 是同一件事的两个观测面**，不是两个问题：

| 界面 | 记录位置 | 状态名 |
|---|---|---|
| Actions 页 / 提交列表 | `GET /actions/runs` | `cancelled`（红叉，`0/3`） |
| Pages 设置页 | `GET /pages/builds` | `errored`（`Page build failed.`） |

查 `/actions/runs` 能看到真相：

```
#9  68b4eb12  success       ← 最终生效
#8  72099a52  cancelled     ← 被 #9 取代
#7  92f6a1b2  success
#6  d3f665da  cancelled     ← 被 #7 取代
#5  a1bdaf10  success
#4  c8ca2765  cancelled     ← 被 #5 取代
#3  e2d1677e  cancelled     ← 被 #5 取代
#2  5b592aca  success
#1  c9fd021c  success
```

**9 条记录里没有一条是 `failure`——全是 `success` 或 `cancelled`。**

那 3 个 job 是 `build` / `report-build-status` / `deploy`。
被取消时它们根本不会执行，所以显示 `0/3`。**GitHub 用红色叉号表示 `cancelled`，
视觉上跟 `failure` 完全一样**，这是最容易误判的地方。

#### 为什么会产生红叉：推送脚本两个文件两个 commit

`push_docs.py` 是**一个文件一个 commit**：

```
[1/2] update docs/MAINTAINING.md    ← 触发一次构建
[2/2] update docs/index.html        ← 触发第二次，把上一次掐掉
```

两个 commit 只隔 2～3 秒。第一个刚启动，第二个就来了，
Pages 的并发策略是**保留最新、取消旧的**，于是"第一个文件"那次提交必然留个红叉。

**因此：红叉只跟推送节奏有关，跟文件内容好坏无关。**

#### 要不要消除这些红叉

**不需要。** 仓库状态是健康的，线上内容已逐字节校验过。红叉只在推送瞬间出现，
最终态都是绿的；这个仓库只有自己人在推，不影响任何人。

真想消除，可以让推送脚本改用 **Git Trees API 一次提交多个文件**，
就不会有中间态了（代价是脚本复杂度上升）。

#### 三秒判断法

看到红叉时按顺序做两件事就够，不用读日志：

1. **看最后一条运行**是否 `success`——是就没事
2. **比对线上 sha256** 与本地是否一致——一致就说明已生效

只要这两条成立，中间有多少红叉都可以忽略。

> 补充：这些 workflow 是 **GitHub 为 Pages 自动生成的**
> （名字叫 `pages build and deployment`），**不是你的仓库文件**——
> 它们不受 `.github/workflows/` 里的配置控制，也无法在仓库里"修"。
> 仓库自己的 `ci.yml` / `release.yml` 与 Pages 的这两条互不相干。

### 页面布局的注意事项

站点是**响应式**的，改版式时注意这几条（都踩过）：

**容器用 `max-width` + `padding`，不要用 `width: min(...)`。**

```css
.wrap { max-width: 1180px; margin: 0 auto; padding: 0 28px; }
```

用 `width: min(100% - 56px, 1180px)` 在窄视口下配合内部 padding 容易算错，导致溢出。

**单列那一档必须显式写 `column-gap: 0`。**

宽屏是多栏布局（`.body-grid` 用 CSS Grid）。如果写成 `gap: 0 clamp(40px, 5vw, 96px)`
而不带媒体查询，**单列时 column-gap 依然生效**，会把列宽撑出视口——
实测 390px 视口下内容被撑到 525px，**横向溢出 134px**。

**分栏后要检查两栏高度差。**

分栏容易一栏长一栏短，底部露出一大片空洞。用这个量：

```javascript
const l = document.querySelector('.col-l').getBoundingClientRect().height;
const r = document.querySelector('.col-r').getBoundingClientRect().height;
console.log(l / r);   // 目标 0.85 ~ 1.15
```

**改完必须跑多档宽度验证**，中间断点最容易出问题，只测 1920 和手机是不够的：

```
2560 / 1920 / 1440 / 1100 / 900 / 600 / 390 / 320
```

用 Playwright 无头 Edge 一次跑完（`channel="msedge"`，不需要 `playwright install`）：

```python
pg = b.new_page(viewport={"width": w, "height": h})
m = pg.evaluate("""() => ({
  w: document.querySelector('.wrap').getBoundingClientRect().width,
  cols: getComputedStyle(document.querySelector('.body-grid'))
          .gridTemplateColumns.split(' ').filter(Boolean).length,
  docW: document.documentElement.scrollWidth,
  win: window.innerWidth
})""")
```

**`docW > win` 就是有横向滚动条，必须为零。**

排查越界元素可以用这段，比肉眼看快：

```javascript
document.querySelectorAll('*').forEach(el => {
  const r = el.getBoundingClientRect();
  if (r.right > window.innerWidth + 1) console.log(el.tagName, el.className, r.width);
});
```

> 注意：`pre > code` 宽于视口是**正常的**（`white-space: pre` + 父级 `overflow-x: auto`
> 允许横向滚动）。要看的是**父级 `pre` 有没有被撑破**。

### 页面写什么、不写什么

**落地页只放「是什么 / 怎么用 / 去哪下载」。**

这个页面曾一度把 README 全部内容搬进来（11 个 section：踩坑、可靠性设计、
全参数表、环境变量表、更新日志），膨胀到 24.9 KB，反而没人看得完。

原则：**细节信息的归宿是 README，不是落地页。**
页面上留一个「完整文档」的链接指回仓库就够了。

---

## 本机环境约束

这些不是仓库的问题，是**这台开发机**的网络和进程特性，但会影响维护方式。

### git 只读通、写不通

| 通道 | 结果 |
|---|---|
| `api.github.com` | ✅ 200 |
| `github.com` 主站 | ✅ 200 |
| `xljfz.github.io` | ✅ 200（直连和走代理都通） |
| **`git push`** | ❌ `schannel: server closed abruptly` / `CONNECT tunnel failed, response 502` |

**`github.com` 能打开 ≠ `git push` 能通。** 首页是只读 GET 走简单 HTTPS；
`push` 是要写入的 POST + 分块传输，代理只放行前者。特征是**「只读通、写不通」**。

所以本仓库的推送一律走 **Contents API**，不能用 git。
`release.py`、`push_docs.py` 都是这个路子。

### token 不要落盘持久化

环境变量在本机**每轮对话结束就消失**（进程被回收），所以「上次 set 过」是不成立的，
每次维护都要现场提供 token。

**不要为了省事把 token 写进 `.env` / `.txt`。** 理由：要写出这个文件，
token 就必须先经过 AI 的上下文；而且明文文件对同机器任何进程可读。
用一次、撤一次是最稳的。

**需要的权限只有一项**：

| 配置 | 值 |
|---|---|
| 仓库访问 | 勾选 `xjtu-siyuanxuetang-grab` |
| 存储库权限 → 内容 | 读取和写入 |

**`.github/workflows/` 下的文件例外**（2026-09-19 起 workflow 已直接放进仓库）：
推它们需要 **`Workflows: write`** 权限，只有 `Contents: write` 时会被 403。

⚠️ **行为在 v1.4.2 变了**：旧推送器遇到无权限文件会**跳过并仍以 0 退出**，
于是「仓库少几个文件」这件事被静默吞掉，一路走到发版。
现在的原子推送器要么整体成功、要么整体失败（非零退出），
`release.py` 读到非零就停下。所以缺 workflows 权限时，
**要么换带该权限的 token，要么在网页端手动上传这几个文件**，
没有「先跳过、以后再说」这个选项。日常改代码不需要每次都推 workflows 文件。

> **细粒度 token 编辑权限后，token 字符串不变。** 实测（2026-09-20）：
> 给已有 token 在网页上补勾「Workflows → 读取和写入」并保存后，
> **原 token 直接就能推 `.github/workflows/`**，不需要重新生成、不需要换字符串。
> 2026-09-20 用这个方式把 `ci.yml` / `release.yml` 推上了远端。
> （注意「选择账户权限」页面全是账户级权限，与推代码无关，别在那里勾。）

> **`Administration` 不需要，但也「改不了」。** 实测（2026-09-19）：
> 用只有 `Contents: write` 的细粒度 token 调仓库元数据端点，
> `PUT /repos/<o>/<r>/topics` 与 `PATCH /repos/<o>/<r>`（改 description / homepage）
> 都返回 **403 `Resource not accessible by personal access token`**。
> 读这些端点是通的（GET 返回 200），只有写被拒。
>
> 推论：**About 描述、Topics、Homepage 这三项只能在网页上手动改**，
> 不要试图用脚本推——推代码的 token 天然没有这个权限。
> 想「让 Skill 定位更显眼」时，唯一可脚本化的位置是 **README 第一屏**
> （GitHub 会把 README 渲染在文件列表上方，效果接近 About 区域）。

---

## GitHub Actions 的实测坑（2026-09-20 首跑后整理）

CI 于 2026-09-20 首次真实运行，当前状态：**全绿 10/10**（3 平台 × 3 Python + 隐私自检）。
以下是首跑踩出来的，改 workflow 或脚本输出前先读：

### 坑一：Windows runner 的 stdout 是 cp1252，中文输出会崩

GitHub 的 `windows-latest` runner 终端编码是 **cp1252**，脚本 `--help` 输出中文
（argparse 的 help 文本就是中文）会抛 `UnicodeEncodeError`，表现为
「Check CLI help works」一步挂掉——且只有 Windows 挂，mac / ubuntu 全绿，
三个 Python 版本全挂，非常有迷惑性。本地复现不了是因为开发终端是 GBK 且
调试时常设 `PYTHONIOENCODING=utf-8`。

修复在两处，**都别删**：

1. `scripts/lms_common.py` 顶层对 `stdout` / `stderr` 做
   `reconfigure(errors="replace")`——编码错误降级为替换符而不是崩溃，
   不改编码本身（本地 GBK 终端显示不受影响）。
2. `ci.yml` 的 test job 与 `release.yml` 的 verify job 设
   `env: PYTHONIOENCODING: utf-8` + `PYTHONUTF8: "1"`。

新脚本只要 import 了 `lms_common` 就自动受保护；**不经过 lms_common 的脚本**
（如 CI 用的 `.github/scripts/pack.py`）要么避免中文输出，要么自己加同样的防护。

### 坑二：删除只在白名单范围内发生（v1.4.2 起已支持删除）

v1.4.2 之前的推送器对每个文件做 GET → PUT，**根本不会删除远端文件**，
本地删掉的（如 v1.4.0 删掉的几个模板）会一直残留在远端 main 上。

现在走 tree API，本地没有、远端有的文件会带 `sha: null` 进 tree 条目，
即真实删除。但有两个边界必须知道：

- **只删白名单（`INCLUDE`）范围内的路径。** 仓库里白名单之外的东西
  （遗留脚本、历史产物）不会被这次推送删掉 —— 避免一次同步把
  不属于本项目的文件悄悄清掉。
- 想手动删某个文件仍可用 `DELETE /repos/<o>/<r>/contents/<path>`，
  body 带 `{"sha": <该文件当前sha>, "branch": "main"}`。

### 坑三：CI 触发是按 commit 的（v1.4.2 起已大幅缓解）

`on: push: branches: [main]` 对**每个 commit** 都触发。旧推送器逐文件提交，
推一批文件会产生 N 个 commit、N 个 CI run，免费 runner 互相挤队列，
一次发布要等十几分钟。

v1.4.2 起一次源码同步 = **一个 commit**，CI 只触发一次。
另外 `release.yml` 不监听任何 push 事件（只保留手动触发、且只发布已存在的 tag），
发版走 `release.py` 本地方案（方式 A）。

---

## 发版流程

### 唯一 publisher：`release.py`

**本仓库只有一个默认 publisher —— 本地的 `release.py`。**
`.github/workflows/release.yml` **不再监听 `push: tags:`**（v1.4.2 收口），
`release.py` 打 tag 后不会再在 Actions 里触发第二次发布。
以前两边同时发：同名附件竞争、标题/正文互相覆盖、
「本地成功但 workflow 失败」还是「反过来」说不清 —— 现在只有一条路。

`release.yml` 保留为**手动备用**：网页 Actions 页输入版本号触发，
**只发布「已经存在的 tag」对应的冻结提交**（语义见下文「方式 B」）。

---

### 方式 A：本地一键（默认，本机 git 协议不通时唯一可行）

```bash
set GH_TOKEN=github_pat_xxx

python release.py --version 1.1.0 --title "下载可靠性" --dry-run   # 先看打包内容
python release.py --version 1.1.0 --title "下载可靠性" --yes       # 正式发
```

完整流程与顺序：

```text
1  版本号自检（tag 未被占用 / resume 只在缺东西时继续）
2  打包（从当前工作区生成 zip）
3  包体校验（解压跑离线测试 + 隐私检查）
4  确定源码 commit（--push-code 则先原子推送，tag 优先级见下）
5  校验「实际 zip」== 源码 commit 的 tree   ← 对象是 zip，不是工作区
6  sha256 锁定这份 zip + 人工确认
7  tag(source_sha) → Release → 再核 sha256 → 上传同一个文件
```

关键是 **第 5 步验证通过的那份 zip，就是第 7 步上传的那份**：
校验后任何改动（哪怕一个字节）都会被上传前的 sha256 复核拦下。

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只打包 + 列清单，不碰 GitHub |
| `--push-code` | 发布前用仓库内的 `tools/gh_push_dir.py` 原子推送源码，并把 tag 绑定到它返回的 commit |
| `--notes notes.md` | 用文件里的内容当 Release 说明 |
| `--resume` | 见下方「`--resume` 的真实语义」—— 不是「恢复任意旧版本」 |
| `--update-notes` | 只更新已发布 Release 的说明，不重新打包 |
| `--yes` | 跳过发布前确认。**脚本化/自动化调用必须加**，否则 `input()` 会 `EOFError` |

> **`--title` 只写版本号后面那部分。** 脚本自己会拼成 `v1.1.0 —— <title>`，
> 你写 `--title "v1.1.0 - 下载可靠性"` 会得到 `v1.1.0 —— v1.1.0 - 下载可靠性`。
> （新版已自动剥掉重复前缀，但仍建议只写后半段。）

> **改代码要在打包之前。** `release.py` 自己也在发布内容里（`INCLUDE` 含它），
> 如果打包后才改它，第 5 步的 archive 校验会直接拦下 —— 这是设计行为，不是误报。

`release.py` 放在仓库内外都能跑：它会自动判断自己在维护者工作区
（源目录是旁边的 `xjtu-lms-grab/`）还是在仓库内（源目录就是自己所在目录）。

**四层保护：**

1. **版本号自检** —— tag 已存在就报错退出。已发布的版本内容不可变，要改就发新版本号。
   （这条是踩过坑换来的：v1.0.0 曾被原地覆盖过。）
2. **包体校验** —— 上传前解压到临时目录，确认关键文件齐全、没混进登录态、
   离线测试能过（四个测试文件，共 384 个用例；用例数由测试自己报出，并与
   静态扫描的 `def test_` 数量交叉核对，对不上就告警）。校验不过就不发。
3. **artifact identity 校验**（v1.4.2 起）—— `verify_archive_matches_remote()`
   把**实际 zip** 的每个文件算成 Git blob sha，与源码 commit 的 tree 逐条比对，
   外加 sha256 锁。见下一节。
4. **打包白名单** —— 用 `INCLUDE` 显式列出该打进去的东西，新文件必须手动加；
   另有体积上限兜底，防止课程资料误入。
   白名单的唯一定义在 **`tools/release_common.py`**，`release.py`、
   `tools/gh_push_dir.py`、`.github/scripts/pack.py` 三处都引用它，
   不存在「各存一份然后漂移」的可能。

### tag 绑定到哪个 commit（v1.4.2 起）

以前是「推完代码 → 再读一次远端 main HEAD → 拿它打 tag」。
这两个动作之间如果又有人推了一次（或推送只成功了一部分），
tag 就会打在别人的 commit 上，而 zip 却来自本地工作区 ——
**asset 与 tag 指向的源码不一致**，这是最难事后发现的一类发布事故。

现在的取值顺序是：

| 情形 | 源码 commit | 说明 |
|---|---|---|
| tag 已存在 | tag 当前指向的 commit | `--resume` 只补缺，不能换内容 |
| `--push-code` | 原子推送**返回**的那个 commit | 推送器自己知道建了谁，不靠回读 main |
| 其余 | 远端 main HEAD | 由第 5 步证明 zip 与它一致 |

### artifact identity：比对对象是 zip，不是工作区

早期版本校验的是「工作区 vs 远端 tree」，这挡不住 TOCTOU：

```text
t1  build_zip() 从工作区 A 打出包
t2  工作区某文件被改成 B
t3  push B → 远端 main = B
t4  「工作区 vs 远端」= B vs B → 验证通过
    但发出去的 asset 仍是 A，tag 却指向 B
```

所以最终不变量必须是：

```text
zip payload 的 blob 集合  ==  source_sha tree 的 blob 集合（按发布范围）
校验时的 zip sha256       ==  上传前的 zip sha256
```

`verify_archive_matches_remote()` 做三件事，任一不满足即停止发布：

| 检查 | 含义 |
|---|---|
| `missing` | 包里有、commit 没有 → 打进了未提交的东西 |
| `changed` | 两边都有但内容不同 → 典型 TOCTOU 症状 |
| `absent`  | commit 有（发布范围内）、包里没有 → 推送了却没打进包 |

范围仍由 `tools/release_common.py` 的 `INCLUDE` / 排除规则决定，
不另立一份清单；zip 顶层的 `xjtu-siyuanxuetang-grab/` 目录在比对前剥掉。

配套的两条硬约束：

- **绝不强制（force）更新 main。** ref 更新带 `force: false`，
  这不是严格意义的 compare-and-swap，而是**基于 parent SHA 的乐观并发保护 /
  非强制 fast-forward 更新**：新 commit 的 parent 是 base=A，
  若 main 仍是 A 则 A→B 是 fast-forward、允许；若 main 已被推到 C，
  C→B 不是 fast-forward，GitHub 直接拒绝（409/422），本次推送整体放弃并报错。
  安全性足够，但不要把它描述成 CAS。
- **`--resume` 不能改内容。** 见下。

### `--resume` 的真实语义

它**只补缺失的 Release / 附件**，不会也不能「把旧版本重新构建出来」：

```text
tag=A，当前工作区=B
--resume
  → source_sha = A（tag 已指向的 commit，不重新推送、不取新代码）
  → 校验「实际 zip」是否与 A 的 tree 一致
  → 不一致（工作区已经是 B，本地无法证明能重建 A 的内容）→ 停止发布
```

也就是说：**如果工作区已经变化且无法重建原 tag 内容，`--resume` 会停下，
不会拿新代码补旧版本。** 想发新代码，请换一个新版本号。

### 方式 B：GitHub Actions 手动备用发布

`release.yml` **只保留 `workflow_dispatch`**，不监听任何 push 事件（含 tag push）。
语义是「**发布一个已经冻结的 tag**」，不是「把当前 main 发布为新版本」：

```text
workflow_dispatch(version=1.4.2)
↓
source job：tag v1.4.2 必须已存在（不存在 → 失败，绝不自动拿 main 创建）
↓
git rev-list -n 1 v1.4.2 → 最终 commit SHA（annotated tag 剥掉 tag object）
→ checkout 该 SHA 并核对
↓
verify job：checkout 精确 SHA → compileall + 离线测试 + 隐私自检
↓
release job：再核 SHA → 版本占用检查（Release/附件已存在 → 失败，不覆盖）
→ pack（从该 SHA 打包）→ 发布 Release + 附件
```

> 这条路径是给 `release.py` 所在网络环境不可用时的人工兜底。
> 为什么不再发布「点击时的 main」：main 是可移动引用，网页点击与 runner
> 真正 checkout 之间可能被别人推进新 commit，拿到的是 B 而不是你以为的 A；
> tag 是不可变发布身份，天然抗这个 race。
> 「补发已有 tag 缺的附件」仍用 `release.py --resume`
> （绑定 tag 已指向的 commit，不会拿新代码顶替旧版本）。二者不要混。

---

## Workflow 按 SHA 分工（source identity，v1.4.2 起）

核心原则：**CI 验证「正在开发的提交」，Release workflow 只验证并发布「已经冻结的发布提交」。**
两者按 source identity 分工，不互相替代：

```text
       开发阶段（ci.yml 验证）              发布阶段（release.yml 验证并发布）

  PR 提交 / main·master push              release.py --push-code
        │                                            │
        ▼                                            ▼
  PR HEAD SHA / 集成分支 HEAD SHA        原子推送 → tag vX.Y.Z 冻结 commit
        │                                            │
        ▼                                            ▼
 ┌───────────────────────────┐      tag → git rev-list -n 1 剥出最终
 │ ci.yml（3 OS × 3 Python） │         commit SHA（source job 锁定）
 │ checkout 精确 source SHA  │                  │
 │ （≠ GitHub 合成的 merge）  │                  ▼
 │ compileall + 384 用例     │      ┌────────────────────────────┐
 │ 隐私自检（contents: read）│      │ release.yml（仅手动触发）   │
 └───────────────────────────┘      │ checkout 精确 commit SHA    │
                                    │ verify（384 用例）→ pack    │
   同一 PR/分支出新 HEAD             │ → Release + 附件            │
   → 取消旧 run（concurrency）      └────────────────────────────┘
```

| 问题 | ci.yml | release.yml |
|---|---|---|
| pull_request 事件的 source | **PR HEAD SHA**（显式 checkout `github.event.pull_request.head.sha`，不用 `refs/pull/N/merge` 合成提交） | — |
| branch push 事件的 source | **`github.sha`**（仅 main/master。**push 不监听 `**`**：PR 分支的每次 push 若同时触发 branch push CI + pull_request CI，3 OS × 3 Python 矩阵直接翻倍——feature 分支的验证统一走 PR 事件） | — |
| tag push 是否触发 | **否**（只监听 branch push / PR / 手动） | 否（不监听任何 push 事件，只保留 `workflow_dispatch`） |
| Release workflow 的 source | — | **tag 剥壳后的最终 commit SHA**（`git rev-list -n 1 vX.Y.Z`，annotated tag 的 tag object 会被剥掉） |
| Release workflow 是否读取 main | — | **否**。main 是可移动引用，点击触发与 runner 实际 checkout 之间可能被推进新 commit；tag 是不可变发布身份 |
| Release workflow 是否要求 tag 已存在 | — | **是**。tag 不存在直接失败，绝不自动拿 main 创建 |
| 同源再验证 | test / lint 两个 job 各自 `git rev-parse HEAD` 与预期 SHA 核对 | source → verify → release 三个 job 逐级传递同一 SHA，verify 与 pack 前各核一次：**verified == packed == tag 指向的 commit** |
| 并发控制 | `concurrency` 组按 PR / 分支分组，新 HEAD 取消旧 run | 不取消（发布是低频终态操作，不与 CI 抢队列） |

为什么 PR 事件必须显式 checkout PR HEAD：`actions/checkout` 默认在 PR 事件下
拿到的是 GitHub 临时合成的 `refs/pull/<N>/merge`（merge commit），
它不是任何人在本地见过的提交——验证它等于验证一个从未存在的状态。
显式指定 `head.sha` 才是「验证 PR 作者写出的代码」。

方式 A（`release.py` 本地发布）不经过这两个 workflow：它在本机完成
原子推送 + archive 校验 + tag + Release + 附件全链路，`release.yml`
对它创建的 tag **不会**自动触发二次发布（没有 `push: tags` 监听）。

---

## 回放完成判据：稳定窗口（v1.4.2 起）

回放（`lms_live.py`）**没有官方哈希**，而唯一能拿到的服务端长度信号 ——
响应头里的 `Content-Length` / `Content-Range` 总长 —— **不是完成真值**：
回放还在转码时这个数会随时间增长，一次 GET 读到 EOF 与紧随其后的一次探测
完全可能看到同一个旧长度，10 秒后对象才变成 N+Δ。

v1.4.1 曾用「实得字节 vs 声明总长，差 8% 以内算自然短读」的比例容差绕过这个
问题，结果是留了一条「尺寸差不多就相信」的旁路：转码中途的快照可能被当成
最终文件永久留在磁盘上（下次运行还会因「文件已存在」跳过）。

**v1.4.2 起唯一的完成判据：**

> 本次实得字节数 == 经稳定窗口确认的远端最终 size

```text
download EOF（got 字节）
      ↓
probe remote（Range: bytes=0-0 → Content-Range 的 total）
      ↓
┌ remote == got 且 (size, ETag/Last-Modified) 持续稳定 60s → ACCEPT
│      （accept = rename 成最终 mp4 + 把确认 size 写进索引）
├ remote > got        → resume 到 remote → EOF 后重新 probe/稳定窗口
│      （轮数上限 TAIL_MAX_ROUNDS，防「转码永远追不上」）
├ size 相同但校验器变了 → 稳定计时重置（对象被替换）
├ remote < got        → FAIL（远端换对象 / 收缩，不截断本地后接受）
└ probe 404 / 异常 / 300s 仍不稳定 → FAIL，保留 .part
```

**注意 `got < declared` 不再先去耗光基于旧 `declared` 的重试**：第一次 EOF
就探远端更合理 —— 旧声明没有信息增益，而稳定探针能直接证明「当前最终对象
到底多大」。`declared` 只作为传输提示写进 `note` 与清单。

### 不变量（改这段代码前先读）

1. **只有通过稳定窗口校验的字节才能 rename 成最终文件**；任何失败出口都保留
   `.part`，且不更新 `.download-index.json`。
2. 稳定计时是**时间戳差**（`time.monotonic`），不是「连续 N 次探测相同」——
   后者会让判据随探测间隔漂移。测试注入虚拟时钟（`download(clock=, sleep=)`）。
3. 探测走 `Range: bytes=0-0`（与实际下载同一条路径），只取响应头就断开；
   不用 HEAD，也不读 body（读 body 会触发限流）。
4. 判据两侧都不再有容差：`SHORT_TOLERANCE` 与 `already_complete(tolerance=...)`
   已删除，增量判据统一为「可信 size 精确相等」。
5. 回放的**可信 size** 只来自稳定窗口确认（索引里的 `size` 字段由下载侧写入）；
   老的 / 守卫分流产生的条目只有 `path` + `name` → 视为无可信 size，
   退回「本轮远端探测值精确比对」，绝不拿猜测值当真。
6. `.part` 不再因为「不小于声明总长」被删掉（声明值会变）；本地比远端长
   由 HTTP 416 与 `remote_smaller` 分支显式判失败。

### 参数

| 常量 | 值 | 含义 |
|---|---|---|
| `TAIL_STABLE_SECONDS` | 60 | 远端对象必须连续这么久不变才算「最终」 |
| `TAIL_PROBE_INTERVAL` | 20 | 两次探测间隔（秒） |
| `TAIL_MAX_WAIT` | 300 | 单次窗口校验最长等待；超时判失败、保留 `.part` |
| `TAIL_MAX_ROUNDS` | 5 | 「远端更大 → 续传追平」的最大轮数 |
| `PROBE_TIMEOUT` | 45 | 单次探测超时 |

测试里 `LiveWindowBase` 注入虚拟时钟（窗口语义可断言、不真等 60 秒），
`LiveFastBase` 把窗口压成 0 秒（只验续传与落盘正确性）。

---

## 回放地址 JIT 与刷新语义（v1.5.0 起）

回放地址的查询串里带**一次性时效凭据**，所以它被当作运行时数据而不是配置：

- **不落盘。** 条目、`.download-index.json`、清单、日志里都没有 URL 与凭据；
  索引只存 `name` / `path` / 经稳定窗口确认的 `size`。
- **下载前现取（just-in-time）。** `lms_live.resolve_replay_url(op, act_id,
  camera_id=, camera_type=, detail=, base=)` 的选择规则是确定性的：
  ① 给了 `camera_id` 就精确命中；② 否则 `camera_type` 必须**唯一**命中；
  ③ 都不给则取 encoder 优先的第一路。失败抛 `LiveResolveError`，
  **消息只含活动 id / 机位，绝不含 URL**。
- **401 / 403 重新解析后继续。** `download(url_provider=..., base_identity=...)`
  在进循环前与每次 401/403 各解析一次。刷新**不消耗 `attempt`**（独立预算），
  但受 `MAX_URL_REFRESHES` 封顶。`.part` 原样保留，下一轮从它的长度续传。
- **刷新后必须复核身份。** 用 `probe_remote` 比对 `(size, etag)`：`size` 不符，
  或两侧 `etag` 都存在且不同 → `identity_conflict`，**fail-closed**：
  保留 `.part`、绝不拼接、不落最终文件、不更新索引。
- **异常文本先脱敏。** urllib 的 `InvalidURL` /
  `ValueError("unknown url type: ...")` 消息**自带完整 URL**。
  `redact_url()` 只削查询串；`redact_text()` 用于自由文本，
  接在 `probe_remote` 的 `err`、`download` 的 provider 失败 / 刷新失败 /
  通用 `except Exception` 四处。

### 不变量（改这段代码前先读）

1. **任何**返回路径都必须带 `refreshes` 字段（测试逐条钉住）。
2. 刷新**不改变** canonical identity 与目标路径，只换「用哪个地址去取」。
3. 身份冲突是 fail-closed，不是重试 —— 重试不会把两份内容变成一份。
4. 不要为了「少一次请求」把 URL 缓存回条目：那正是凭据过期与泄漏的来源。

## `--exclude` 的优先级（v1.5.0 起）

`--exclude` 必须在**任何远端探测 / 地址解析 / 稳定窗口**之前生效。

- `expand_items()` 里**先命名再判排除**：文件名只依赖活动元数据（标题 + 起始时间 +
  机位），所以不探测就能判断。命中即标 `excluded=True` 并 `continue` ——
  **不 probe、不碰回放地址**。
- 下载循环与 `build_rows()` 把 `excluded` 判定**放在 `error` 之前**，
  状态记 `STATUS_EXCLUDED`（计入 `skip`），**永不计 `fail`、永不影响退出码**。
- 匹配语义未变：同一条正则、同一个最终文件名、同样 `re.search`。
- **可达边界（如实记录，不修）**：条目的文件名来自平台元数据，所以「是否被排除」
  必然发生在元数据**之后**。能保证的是「元数据落定后零媒体请求」。
  元数据本身取不到的活动（`scan_error_items` 的 `<title>-回放` 占位名）**不**标
  `excluded`，仍按 fail 报出来 ——「可能被排除」不等于「被排除」，
  不能让接口抖动借排除之名消失。
- 回归里必须有一条**会让它失败**的用例：被排除条目的地址会 502 / 403 / 超时，
  整轮仍 `rc=0`；并带一条不排除的对照组（同样 502 → `rc=4`）。

---

## CI 配置

workflow 配置已直接放在仓库里（`.github/workflows/ci.yml`、`.github/workflows/release.yml`、
`.github/scripts/pack.py`），推上 GitHub 就生效，不需要任何还原步骤。

两点说明：

1. **不启用也完全能用。** 发布走本地方案（方式 A）就够了，`release.py` 在打 zip 后
   已经强制跑过全部离线测试，测试不过就 `SystemExit`，不会发出坏包。
   CI 的额外价值只有**跨平台兼容性验证**（ubuntu / windows / macos × py3.8/3.10/3.12）。
2. **推送 workflows 文件需要 `Workflows: write` 权限。** 用只有 `Contents: write`
   的细粒度 token 推 `.github/workflows/` 下的文件会 403，
   此时在网页端 *Add file → Upload files* 手动上传这几个文件即可；
   或换用带 workflows 权限的 token / 真 git 推送。
   （v1.4.2 起推送器不再「跳过并继续」，缺权限就是整体失败。）

### 「Release verify」和「完整 CI 矩阵」是两件事

v1.4.2 起一次同步只产生一个 commit，CI 也只触发一次，所以不用再等九宫格。
但要把两者的结论区分开：

| | 范围 | 谁触发 | 用途 |
|---|---|---|---|
| **`release.yml` 的 verify job** | 单平台（ubuntu / py3.12）：compileall + 4 个测试文件 + 隐私自检 | 手动（existing tag only，对象是 tag 指向的 commit） | **发布门禁** —— 它挂了 Release 就不会建 |
| **完整 CI 矩阵**（`ci.yml`） | 3 平台 × 3 Python 版本 | main/master push 与 PR（对象是开发中的 HEAD） | **兼容性验证** —— 跨平台 / 老版本 Python 的行为 |

发布时**只需要看 verify 的结论**（约 1 分钟出结果）；
九宫格让它在后台自己跑完即可，不必阻塞发布流程。
如果某个平台单独挂了（例如 Windows 上的编码问题），
再用 `ci.yml` 的结论去定位，而不是回滚已经发出的 Release。

权限设计：workflow 顶层是 `contents: read`；`release.yml` 里只有发布 job 才是
`contents: write`，校验 job 只读。第三方 action 固定到具体 commit SHA
（`softprops/action-gh-release@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65 # v2.6.2`）。

---

## 测试矩阵与覆盖明细

全部离线，不需要网络与登录态，共 **384 个用例**：

```bash
python tests/test_organize.py     # 22 个用例
python tests/test_fetch.py        # 267 个用例
python tests/test_selfcheck.py    # 20 个用例
python tests/test_push.py         # 74 个用例
```

| 文件 | 覆盖 |
|---|---|
| `test_organize.py` | 中文数字转换、括号剥离、六种章节写法、假章号排除、目录名去重、目录名不带扩展名 |
| `test_fetch.py` | 下载成功/重试/403 不重试/5xx 重试/空响应/HTML 响应/sha256 不符/etag 不符、断点续传与 Range 对齐四情形（正常/忽略/向前扩大/缺口，含最后一次缺口保留 `.part`）、**回放稳定窗口**（等长也要过窗口、稳定语义按时间差而不是探测次数、短读不再被容差放行、声明值小于/大于远端的两向情形、ETag 变化重置计时、远端更小 / 404 / 探测异常 / 无 size 头 → 失败且保留 `.part`、远端增长驱动续传后接受、增长轮数受上限约束、416 → 不截断本地；探测走 `Range: bytes=0-0`，416 带 `Content-Range: bytes */N` 时按 RFC 7233 回填长度；`.part` 等于声明总长时不再被删掉重下）、元信息错误分类（403/404→N/A，401→登录态，5xx/超时/坏 JSON→FAIL）、**扫描阶段失败记账**（page / lecture_live 详情的 500/超时/坏 JSON/403/404/401）、`item_status()` 统一状态语义、`--list-only` 退出码（真跑 `main()`）、已有文件精确比对（**无任何容差**：`tolerance` 参数与 `SHORT_TOLERANCE` 已删除并有结构断言钉住、回放 944/1000 同样算不完整、索引可信 size 精确命中、清单视图与主流程同判据）、`.7z` 冲突改名保扩展名与项目包判定、新旧登录态格式兼容、Cookie 安全属性还原、文件名安全（含 Windows 保留名）、路径冲突消解、项目包判定、CSV 公式注入防护、时间戳固定 UTC+8（跨时区一致）、清单导出（`stage` / `err_kind` 入 CSV）、多来源计数、**resource identity**（**持久身份索引** `.download-index.json`：等大小瞬时错误不冒领 plain 名、报错条目经索引占位、新增同身份资源拿独立后缀、同 uid 内容更新原地覆盖路径不漂移、布局切换不移动已分配身份、索引无凭据且损坏可重建、回放机位并入身份键、dry-run 两次运行分配逐字节一致、**等大小三轮收敛封板回归**、course namespace（多课程共用 --out 不串身份）、camera_id 优先的同活动多机位键、同活动多路无 camera_id 同类型显式 identity ambiguous、索引损坏 fail-closed（坏文件改名保留现场 + RC_BAD_INDEX 拒绝下载）、索引路径越界 / 绝对路径条目丢弃；min-uid 冲突规则与 `identity_conflict_target()` 覆盖守卫作为新身份分配与存量迁移保护层；**内容损坏 vs 格式不认识分立**（拒绝路径字节原样保留、不建 `.corrupt-`；结构信封 `entries` → `items` 迁移放行且仅限键已带 course namespace；无 course 旧键的身份语义迁移一律拒绝））、**Round 13 —— `--exclude` 优先级**（被排除条目零媒体请求、带 error 的排除项仍判 `excluded`、匹配语义不变、同一 lecture_live 详情只取一次、被排除条目 502 / 403 / 超时整轮仍 `rc=0` 且有不排除的对照组判 `rc=4`）、**Round 13 —— 回放地址 JIT**（条目不带 `url`、伪造过期凭据 → 真 403 → 重解析 → 成功且 `refreshes==1`、`.part` 续传起点等于残片长度、连续 403 到上限 `url_refresh_exhausted` 且保留 `.part`、刷新后 size 变 / `etag` 变均 `identity_conflict` fail-closed 不拼接、身份一致时不误判冲突、解析器按 camera_id / camera_type 选择且失败消息不含 URL、不经 HEAD、异常文本与返回值 / 日志不含 token） |
| `test_selfcheck.py` | 登录态五种状态判定、检查级别（警告 vs 失败）、退出码、默认不联网、自检清单与 `scripts/` 实际文件一致 |
| `test_push.py` | 发布链路（全部 mock / subprocess，不联网）：N 个文件变化只产生 1 个 commit / 1 次 ref 更新、一次 tree POST 装下全部变化、未变文件不重传 blob、删除用 `sha: null`、**绝不 force**（`force: false` + 冲突时放弃且不动 main）、ref 更新后复核、无变化不建空 commit、删除只在白名单范围内、blob sha 算法（空文件常量）、登录态/profile/缓存被过滤、`release.py` 用的是仓库内推送器且带版本化 commit message、`push_code()` 返回 commit SHA、**tag 绑定推送返回的 SHA 而不是回读 main**、`--resume` 绑定已存在 tag 的 SHA 且不再推代码、附件上传 502 重试而 400 不重试、**artifact identity**（zip==commit 通过 / 工作区漂移不影响 / 旧 zip 拦下 / 缺文件与多文件均失败 / 顶层目录不对拒绝）、**sha256 锁**（校验后被替换必须停上传）、**双 publisher 已消除**（release.yml 无 `push: tags`、手动入口与占用检查保留）、**CLI smoke**（清空 PYTHONPATH 后真实跑 `tools/gh_push_dir.py --help` 与 `pack.py`）、**workflow source identity**（ci.yml 只验证 PR HEAD / 分支 HEAD 且不触发 tag、checkout 精确 source SHA、concurrency 取消旧 run；release.yml 是 existing-tag-only：tag 缺失即失败、绝不建/挪 tag、绝不 checkout main、verify 与 pack 复核同一 SHA、`rev-list -n 1` 剥 annotated tag）、**tag 剥壳实测**（真实 git 验证 lightweight / annotated 都解析到 commit） |

`test_push.py` 用 **`FakeApi`**（模拟 Git Data API 的 ref / commit / tree / blob
四个端点）与假 `subprocess.run`（模拟推送器写出结果 JSON），
所以「并发下 main 被别人更新」「推送返回 B 但 main 已是 C」这类
真实环境里很难构造的场景都能离线验证。

`test_fetch.py` 用 **假 opener**（`FakeOpener` / `RangeFakeOpener` / `FakeResponse` /
`http_err`）脚本化服务端行为，所以能测「第一次超时第二次成功」「服务端把 Range 起点
从 5MB 挪到 4MB」这类真实环境里很难复现的路径。新增用例优先用这个范式，
不要引入 `responses` / `vcr` 之类的新依赖——`lms_fetch.py` 只依赖标准库。

时区用例（`test_stamp_stable_across_machine_timezones`）会遍历
UTC / Asia-Tokyo / America-New_York / Asia-Shanghai，改 `TZ` 环境变量 + `time.tzset()`
验证，因此**不依赖跑测试机器所在的时区**。

改测试断言逻辑时要保持用例总数，`release.py` 的 `verify_zip` 会交叉核对
（静态扫 `def test_` vs 实跑 `Ran N tests`）。新增测试文件要**同时**改四处：
`release.py` 的关键文件清单 + 测试循环、`ci.yml`、`release.yml` 的 verify job、
README / 本文档的用例数。`pack.py` 不用改 —— 它按 `tests/` 整个目录打包。

---

## 运行时副本同步（发布前的正式 gate，v1.5.0 起）

开发工作区之外还有一份**运行时副本** `~/.workbuddy/skills/xjtu-lms-grab/`（agent 实际加载
的那一份）。它靠**手动同步**，所以历史上反复出现「源码路径 / 发布包 / 运行时副本」三套状态
漂移。从 v1.5.0 起把它当**独立交付物**再验一次，作为发布前的正式 gate：
**「仓库测试绿」不等于「副本绿」**。

### 白名单（`_tmp/r15_sync_runtime.py` 是唯一实现）

| 位置 | 内容 |
|---|---|
| 顶层 | `README.md` `SKILL.md` `prompt.md` `LICENSE` `release.py` |
| `scripts/` | 全部 `*.py` |
| `tests/` | 全部 `*.py`（**四个文件都要带**） |
| `tools/` | `release_common.py` `gh_push_dir.py` `privacy_scan.py` |
| `.github/` | `scripts/pack.py`、`workflows/ci.yml`、`workflows/release.yml` |

`tools/` + `release.py` + `.github/` 是 v1.5.0 起才纳入的 —— 它们是 `tests/test_push.py`
的三个真实路径依赖：`import release as R`、`from tools import …`、
exec `.github/scripts/pack.py`、读 workflow 的 `on:` 块。不带上这些，副本里的
`test_push` 必然 `ModuleNotFoundError` —— 同一个 `tests/`，在仓库 / 发布包跑 **384**，
在副本只能跑 **309**。

`docs/` **不需要**：`test_tools_and_docs_are_in_release_package` 只断言
`release_common.INCLUDE` 里含 `"docs"` 字符串，不读真实目录。

### 同步的硬要求

- **显式白名单**，不做整目录递归：绝不把 `_tmp/`、登录态、下载产物、身份索引
  （`.download-index.json`）、`*.part`、浏览器 profile、`__pycache__` 带进副本。
- 同步前后**逐文件 sha256** 比对；落盘前跑共享扫描器
  `tools/privacy_scan.py`（见下节），**不要**另写一套正则 —— 两套规则必然漂移，
  v1.5.0 的漏检就出在「发布前扫描的正则」和「真实字节形态」不一致。
- 同步后复核「副本里没有白名单外的额外文件」。

### 副本侧必须重跑的验证

1. 模块来源断言：`lms_live.__file__` / `lms_fetch.__file__` 必须落在副本目录里；
2. `compileall` 副本里的全部脚本与测试；
3. **四个测试文件全跑 → 384**（与仓库、发布包同一个数字）；
4. 不下载媒体的真实 smoke：`--list-only`；目标 replay 的 index / skip（索引里存着经稳定
   窗口确认的 `size`，所以第二轮必然是 `SKIP`，媒体字节数不变）；JIT 现解析 +
   `probe_remote` 复核 size；
5. 上述输出与 manifest / 索引里都不含 URL 与时效凭据。

本仓库的实现是 `_tmp/r15_runtime_verify.py`（39 项断言，全过即副本可独立自证）。
`_tmp/r13_smoke/` 那份已完整下载的目标文件 + 身份索引就是 index/skip 用例的种子。

### 顺带：发布包本地校验要绕过 `--dry-run`

`release.py --dry-run` 在**第 2 步（打包）就 return**，**不跑 `verify_zip`**。
要本地校验包体（结构 / 无登录态 / 四个测试文件 / 静态-实跑用例数交叉核对），
直接调它自己的两个函数即可，全程不碰 GitHub：

```python
zip_path, files = release.build_zip(release.SRC, "1.5.0")   # 只写本地 zip
release.verify_zip(zip_path)                                # 校验 + 跑 384 用例
```

---

## 发布前隐私 gate（v1.5.1 起，`tools/privacy_scan.py`）

公开仓库与 Release 附件一旦发布就收不回来（Git 历史可追、附件不可变），所以发布前
必须有最后一道内容闸门。**规则只有一份定义**，本地打包、运行时副本同步、CI、
Release 验证四处共用同一个模块。

### 为什么必须有它

v1.5.0 的发布工具文档字符串里写着本机工作区路径，而当时的检查把盘符路径**锚定在具体
目录名**上、且分隔符只允许一个 —— 源码字符串字面量在盘上的真实字节是**两个反斜杠**，
于是这条路径随包发布了。同时 CI 的隐私自检只 grep `scripts/`，
`release.py` / `tests/` / `tools/` 根本不在扫描范围。两处失守叠在一起，gate 就形同虚设。

（顺带一个容易搞错的点：只把分隔符从「一个」改成「一或多个」**本身并不足以**修好它 ——
尾部字符类是贪婪的，两种写法都能匹配上。真正致命的是「锚定具体目录名」。
所以新版规则同时做两件事：不锚名字 + 允许重复分隔符，并各有一条回归用例钉住。）

### 规则与分类

| 规则 | 覆盖 |
|---|---|
| `win-absolute-path` | 盘符 + 冒号 + **一或多个**分隔符：单分隔符与「源码字面量里的双分隔符」两种形态都覆盖 |
| `posix-user-path` | `/Users/<name>/…`、`/home/<name>/…` |
| `secret-token` | `github_pat_…` / `ghp_…` 这类**真实**凭据值（占位写法不达标） |
| `preview-token` | 一次性回放凭据的值 |
| `private-host` | 私有主机名 |
| `state-dir-path` / `credential-file-name` | 登录态目录、凭据类文件名（只记录） |

命中项分四类，**只有第一类失败**：

| 分类 | 含义 |
|---|---|
| `block` | 真实泄漏 —— gate 失败 |
| `placeholder` | 占位写法（`<你>`、`xxx`、`%VAR%`） |
| `generic_os` | 通用系统目录（Windows 安装目录等） |
| `allowed` | 逐行豁免表命中（检测器定义 / 测试合成夹具） |

豁免**精确到行**：每条都要写明「哪个文件的哪一行、为什么」，且必须命中行内正则 ——
所以同一个文件里新出现的真实路径照样会被拦下。**豁免不等于关掉规则。**

### 扫描范围 = 发布范围

扫描的文件集直接调 `release_common.collect()`，不另立清单。白名单新增一个公开文件，
它自动进入扫描；白名单外的东西不看（它本来也不会进包）。二进制与生成 zip 由
`_looks_binary()` 挡在文本扫描之外。

CI（`ci.yml`）与 Release 验证（`release.yml`）都执行：

```bash
python tools/privacy_scan.py --root . --format github --list-noted
```

`release.py` 的 `verify_zip()` 在**解包之后、缺文件检查之前**先跑一遍：本机路径比
「包里缺文件」严重得多，也该更早报出来。

---

## 完整更新日志

README 只留最近两版，历史在这里：

| 版本 | 变更 |
|---|---|
| v1.5.1 | **发布前隐私 gate 硬化。** 移除发布工具文档里的本机工作区路径；新增 `tools/privacy_scan.py`，路径检测改为覆盖**任意**盘符路径（不再锚定具体目录名），且分隔符允许一或多个 —— 旧检查只允许一个分隔符、且锚在本项目目录名上，源码字面量里的双反斜杠形态因此匹配不到，命中项分四类：真实泄漏 / 占位写法 / 通用系统目录 / 逐行豁免，后三类只记录不失败；扫描范围复用发布白名单（`release_common.INCLUDE`）而非手写目录列表，白名单新增的公开文件自动进入扫描；CI 与 Release 两个 workflow 共用同一份扫描器（不再只扫 `scripts/`）；`release.py` 的 `verify_zip()` 解包后先跑隐私扫描，命中即停止发布。离线测试扩到 384 个 |
| v1.5.0 | **回放地址 JIT + `--exclude` 优先级**（见上文两节）。`--exclude` 提前到任何远端探测 / 地址解析 / 稳定窗口之前：命中即标 `excluded` 并跳过，零媒体请求，网络状态无法把它升级成 `FAIL`、也不影响退出码，清单里新增独立的 `excluded` 状态；回放地址改为下载前现取，401 / 403 时重新解析后从 `.part` 续传（刷新次数封顶、不消耗重试预算），刷新前后用 `(size, etag)` 复核身份，冲突即 fail-closed；新增 `redact_url()` / `redact_text()` 封住「urllib 异常自带完整 URL」这条真实泄漏通道；登录态探测按响应体内容区分「登录页」与「网关拦截」，不再把登录态过期误报成接口可达；修掉索引 `size` 在每轮加载时被抹掉的回归（`load_download_index` 只保留 path/name，使可信 size 路径成为死代码）。离线测试扩到 370 个 |
| v1.4.2 | **发布链路专项**：推送改为 Git Data API 原子提交（blob → tree → commit → ref），N 个文件变化 = 1 个 commit + 1 次 ref 更新，CI 只触发一次；`tools/gh_push_dir.py` 纳入仓库，`--push-code` 不再依赖仓库外脚本，且不再「跳过无权限文件仍返回 0」；绝不强制（force）更新 main（`force: false` 的非强制 fast-forward 保护，冲突即放弃）；支持删除（仅限白名单范围）；无变化不建空 commit、未变文件不重传 blob；`--push-code` 返回 commit SHA，tag 直接绑定它；**消除双 publisher**（`release.yml` 不再监听 `v*` tag push，只保留手动备用入口 + 版本占用检查）；**artifact identity 改为校验实际 zip**（`verify_archive_matches_remote()`：zip 内容 vs 源码 commit 的 tree，工作区漂移不再影响结论），并用 sha256 锁定校验过的 zip、上传前复核；`--resume` 语义写清：只补缺，不能拿新代码补旧版本；token 不再进 curl argv（临时配置文件，用完删除）；附件上传重试真正生效（5xx/网络重试，4xx 立即失败）；白名单抽到 `tools/release_common.py` 三处共用，`docs/` 纳入发布包；新增 CLI smoke 测试（清空 PYTHONPATH 跑真实入口）；**workflow 按 SHA 分工**（ci.yml 只验证 PR HEAD / main·master push HEAD——push 不监听 `**`，feature 分支走 PR 避免 push+PR 双跑；显式 checkout 精确 source SHA + concurrency 取消旧 run；release.yml 改为 existing-tag-only：`git rev-list -n 1` 剥壳锁定 commit SHA、逐 job 复核 verified == packed == tag 指向、绝不读 main、绝不建/挪 tag；**resource identity 封板**（`.download-index.json` 持久身份索引：`identity_key → canonical_path` 成为严格函数——其他资源增删、meta 瞬时失败、排序变化、同 uid 内容更新、进程重启都不改变已分配路径；等大小资源不再可能互相冒领字节；新身份按 dest_for + 最小 uid 分配，`identity_conflict_target()` 守卫保留为无索引存量文件的迁移保护层；索引只含资源 ID / 文件名 / 相对路径，不含任何凭据；**fail-closed**：索引解析失败 → 坏文件改名 `.corrupt-<时间戳>` 保留现场 + 语义化退出码 5 拒绝下载，绝不静默失忆；加载时逐条验证路径（相对 / 无 `..` 逃逸 / 非绝对 / normalize 后必须在 out 内），**单条 path 越界同样整份 fail-closed**（改名保留现场 + 拒绝下载，绝不「丢单条继续」静默遗忘身份）；索引不是任意路径写入入口；顶层信封 `{version, identity_schema, items}` 双版本号分立（结构版本 / 身份键语义版本），未知 version 或 identity_schema 一律拒绝；身份键 = `course:<course_id>:upload:<uid>` / `course:<course_id>:live:<act_id>:camera:<camera_id>`（camera_id 缺失降级 type:，同活动多路无 camera_id 同类型 → 显式 identity ambiguous 不硬合并）、索引 fail-closed 统一到单条 path：越界即整份拒绝、identity_schema 必填且未知即拒绝；**内容损坏与格式不认识分立**（JSON 解析失败 / 顶层结构坏 → `.corrupt-<时间戳>` 隔离保留现场；version 或 identity_schema 不认识 → 原文件字节不动、不改名、不迁移、不修复，直接拒绝退出码 5）；**migration 收紧为结构性的**（`entries` 旧信封 / 裸 map → `items` 且仅当键已带 course namespace；loader 绝不承担任何改变身份含义的迁移，无 course 的旧键拒绝而非猜测归属）；**回放完成判据重写：稳定窗口取代比例容差**（详见「回放完成判据：稳定窗口」一节）。唯一的完成判据 = 「本次实得字节数 == 经稳定窗口确认的远端最终 size」：EOF 后周期探测远端 `(size, ETag/Last-Modified)`，连续 60 秒不变且恰等于本地实得字节数才 rename；远端更大 → 续传追平后重新确认（`TAIL_MAX_ROUNDS` 封顶）；远端更小 / 404 / 超时 → 失败且保留 `.part`，不 rename、不更新索引。探测走 `Range: bytes=0-0` 取 `Content-Range` 总长（同下载路径，比 HEAD 可靠，不读 body 免触发限流）；稳定计时用 `time.monotonic` 时间戳差而非「连续 N 次相同」，测试注入虚拟时钟。`declared` 降级为传输提示（只进 note / 清单）。**删除 `SHORT_TOLERANCE` 与 `already_complete(tolerance=...)`**（含结构断言钉住），增量判据统一为可信 size 精确相等：回放读索引里经确认的 `size`，无索引存量文件用本轮远端探测值精确比对；`.part` 不再因「不小于声明总长」被删，本地比远端长由 416 / `remote_smaller` 显式失败而非截断本地。修正推送器在未传 `--include` 时的崩溃（`release.py` 从不传该参数，真实发布首次执行才暴露），并加回归用例钉住该默认值；离线测试扩到 330 个 |
| v1.4.1 | 收尾几处 silent failure 与一致性问题：`collect()` 返回 `scan_errors` 并转成清单条目（扫描失败进 FAIL / manifest / 退出码）；抽出 `item_status()` 统一 `--list-only` / `--dry-run` / 正式下载的错误语义，`--list-only` 不再固定返回 0；`already_complete()` 增加 `tolerance`，回放与下载侧共用 `SHORT_TOLERANCE`；回放续传缺口在最后一次重试时保留有效 `.part`；`split_ext()` 放行 `.7z` 等数字开头扩展名；`already_complete()` 对 `os.path.getsize` 的 OSError 做保守兜底；清单增加 `stage` / `err_kind`；离线测试扩到 212 个 |
| v1.2.1 | 修 `collect()` 的来源②计数（原先用总数相减，把直播回放误算成「正文内嵌」）；`release.py` 包体校验改为上报测试实际执行的用例数并与静态扫描交叉核对；抽取 `download()` 中重复四次的失败处理块；回放下载新增短读判定（对比 `Content-Length`，超阈值告警并写入清单）；README 用例数与文件清单同步 |
| v1.2.0 | 直播回放下载（`lms_live.py`）；修同一天多个 `lecture_live` 活动 title 相同导致回放互相覆盖的丢数据 bug（文件名加入本地时间戳与机位）；离线测试扩到 79 个 |
| v1.1.0 | 下载可靠性：断点续传、自动重试、sha256 校验、etag 交叉验证、登录态探测、进度条、`--list-only` / `--manifest`、语义化退出码；修长文件名丢扩展名；测试扩到 60 个 |
| v1.0.4 | `release.py` 纳入发布内容并支持两种存放位置；新增 `--update-notes` |
| v1.0.3 | 新增本地一键发布脚本 `release.py`：版本号自检 + 包体校验 + 打包白名单 |
| v1.0.2 | 新增 `--organize` 按章整理；新增离线测试与 CI；`.gitignore` 补漏（profile 目录、压缩包、缓存） |
| v1.0.1 | 安装说明泛化到 WorkBuddy / Claude Code / Codex，强调不装 Skill 也能用；仓库改名 xjtu-siyuanxuetang-grab |
| v1.0.0 | 首个版本：登录、两类附件来源合并、增量下载、干跑 |

---

## 相关文件

| 文件 | 用途 |
|---|---|
| `docs/index.html` | 文档站页面，改它就够了 |
| `docs/MAINTAINING.md` | 本文件，站点的维护说明 |
| `push_docs.py` | 推 `docs/` 到 GitHub（在维护者工作区里） |
| `release.py` | 发版本：版本号自检 → 打包 → 校验 → 确定源码 commit → tag → Release → 附件 |
| `tools/gh_push_dir.py` | 原子推送（Git Data API），随仓库分发，`release.py --push-code` 调它 |
| `tools/release_common.py` | **发布白名单的唯一定义**（INCLUDE / EXCLUDE / blob sha / 差异比对），三处共用 |
| `.github/workflows/ci.yml` / `release.yml` | CI（验证开发中的 HEAD）与**手动备用**发版 workflow（release.yml 只发布已存在的 tag，不监听任何 push 事件），随仓库分发（`INCLUDE` 含整个 `.github/`） |
| `.github/scripts/pack.py` | CI 发版用的打包脚本，白名单直接引用 `tools/release_common.py` |
| `scripts/lms_selfcheck.py` | 安装后自检；改动它要同步 `tests/test_selfcheck.py` 的 `CORE_SCRIPTS` 断言 |

### 页面内容与两栏配平的联动

页面是左右两栏网格，**两栏高度比要落在 `0.85 ~ 1.15`**（见上文「页面布局的注意事项」）。
左栏偏长是常态——它放的是使用路径（安装、命令行、录像类型、范围、FAQ），
右栏只放特点与说明。往左栏加内容时留意这个比值：

- 加内容前先量一次（无头 Edge，见上文那条多档宽度检查）。
- 比值超出上限说明左栏过长，**优先把能独立成节的说明型内容挪到右栏**，
  而不是从左栏删信息。挪动比删减安全。
- 「装完先自检」这节就是这样从左上挪到右栏的：内容是说明性质而非操作步骤，
  放右栏既配平了两栏，也更符合「右栏讲为什么、左栏讲怎么做」的分工。
