# 维护者指南

面向修改这个仓库的人。使用者请看 [README](../README.md)。

发版流程、CI 配置、测试矩阵、完整更新日志都在这份文档里，README 只留使用者需要的内容。

## README 与本文档的分工（2026-09-20 精简后）

README 面向「拿到仓库想用它下课件的人」，只保留：Skill 定位、快速开始、参数、
可靠性设计、注意事项、隐私、目录清单、跑测试的命令、最近两版更新日志。

以下内容**只在本文档维护**，README 里只留一行链接：

| 内容 | 在哪 |
|---|---|
| 发版流程（方式 A / 方式 B）、三层保护、`release.py` 参数 | 本文「发版流程」 |
| CI / 自动发版的配置细节、workflows 权限 | 本文「CI 配置」+「GitHub Actions 的实测坑」 |
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

### 为什么不用 `gh_push_dir.py`

那个脚本推**整个目录**。如果拿它推这个仓库，会把已有的 12 个文件（`README.md`、
`release.py`、`tests/` 等）全部重推一遍，**每个都产生一条无意义的 commit**。
`push_docs.py` 把顶层目录写死成 `docs`，避免这个问题。

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
推它们需要 **`Workflows: write`** 权限，只有 `Contents: write` 时 Contents API
会 403。`gh_push_dir.py` 遇到这种情况会**跳过这些文件并明确列出剩余清单**（退出码 0），
此时在网页端 *Add file → Upload files* 手动上传，或换带 workflows 权限的 token。
日常改代码不需要每次都推 workflows 文件。

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

### 坑二：`gh_push_dir.py` 只增改、不删除

它对每个文件做 GET → PUT，**不会删除远端已有文件**。本地删掉的文件
（如 v1.4.0 删掉的 `ci.yml.txt` 等四个模板）会一直残留在远端 main 上。
清理方式：`DELETE /repos/<o>/<r>/contents/<path>`，body 带
`{"sha": <该文件当前sha>, "branch": "main"}`。删除后记得复核远端根目录。

### 坑三：CI 触发是按 commit 的

`on: push: branches: [main]` 对**每个 commit** 都触发。`gh_push_dir.py`
逐文件提交，推一批文件会产生多个 commit、多个 CI run——看到一排 run 别慌，
**只看 head sha 对应（最新）那条的结论**，前面的中途 run 可以无视。
另外 `release.yml` 靠「推 `v*` tag」触发，本机 git 推不了 tag，
发版仍走 `release.py` 本地方案（方式 A）。

---

## 发版流程

### 方式 A：本地一键（推荐，本机 git 协议不通时唯一可行）

```bash
set GH_TOKEN=github_pat_xxx

python release.py --version 1.1.0 --title "下载可靠性" --dry-run   # 先看打包内容
python release.py --version 1.1.0 --title "下载可靠性" --yes       # 正式发
```

它会依次做：**版本号自检 → 打包 → 包体校验 → 打 tag → 建 Release → 传附件**。

| 参数 | 作用 |
|---|---|
| `--dry-run` | 只打包 + 列清单，不碰 GitHub |
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

`release.py` 放在仓库内外都能跑：它会自动判断自己在维护者工作区
（源目录是旁边的 `xjtu-lms-grab/`）还是在仓库内（源目录就是自己所在目录）。

**三层保护：**

1. **版本号自检** —— tag 已存在就报错退出。已发布的版本内容不可变，要改就发新版本号。
   （这条是踩过坑换来的：v1.0.0 曾被原地覆盖过。）
2. **包体校验** —— 上传前解压到临时目录，确认关键文件齐全、没混进登录态、
   离线测试能过（三个测试文件，共 212 个用例；用例数由测试自己报出，并与
   静态扫描的 `def test_` 数量交叉核对，对不上就告警）。校验不过就不发。
3. **打包白名单** —— 用 `INCLUDE` 显式列出该打进去的东西，新文件必须手动加；
   另有体积上限兜底，防止课程资料误入。
   **这份清单和 CI 用的 `.github/scripts/pack.py` 必须保持一致** —— 否则本地发的包和 CI 发的包内容不同。

### 方式 B：GitHub Actions 自动发

启用 workflow 后，打一个 tag 就自动发布：

```bash
git tag v1.1.0 && git push origin v1.1.0
```

或在仓库 Actions 页面手动触发 `Release`，输入版本号。

> 前提是 **git push 通**。本机不通（见「本机环境约束」），所以实际仍走方式 A。

---

## CI 配置

workflow 配置已直接放在仓库里（`.github/workflows/ci.yml`、`.github/workflows/release.yml`、
`.github/scripts/pack.py`），推上 GitHub 就生效，不需要任何还原步骤。

两点说明：

1. **不启用也完全能用。** 发布走本地方案（方式 A）就够了，`release.py` 在打 zip 后
   已经强制跑过全部离线测试，测试不过就 `SystemExit`，不会发出坏包。
   CI 的额外价值只有**跨平台兼容性验证**（ubuntu / windows / macos × py3.8/3.10/3.12）。
2. **推送 workflows 文件需要 `Workflows: write` 权限。** 用只有 `Contents: write`
   的细粒度 token 走 Contents API 时，`.github/workflows/` 下的文件会 403
   （推送脚本会跳过它们并明确列出剩余清单），此时在网页端
   *Add file → Upload files* 手动上传这几个文件即可；或换用带 workflows 权限的
   token / 真 git 推送。

权限设计：workflow 顶层是 `contents: read`；`release.yml` 里只有发布 job 才是
`contents: write`，校验 job 只读。第三方 action 固定到具体 commit SHA
（`softprops/action-gh-release@3bb12739c298aeb8a4eeaf626c5b8d85266b0e65 # v2.6.2`）。

---

## 测试矩阵与覆盖明细

全部离线，不需要网络与登录态，共 **212 个用例**：

```bash
python tests/test_organize.py     # 22 个用例
python tests/test_fetch.py        # 170 个用例
python tests/test_selfcheck.py    # 20 个用例
```

| 文件 | 覆盖 |
|---|---|
| `test_organize.py` | 中文数字转换、括号剥离、六种章节写法、假章号排除、目录名去重、目录名不带扩展名 |
| `test_fetch.py` | 下载成功/重试/403 不重试/5xx 重试/空响应/HTML 响应/sha256 不符/etag 不符、断点续传与 Range 对齐四情形（正常/忽略/向前扩大/缺口，含最后一次缺口保留 `.part`）、回放短读判定（轻度过、严重失败、保留 `.part`）、元信息错误分类（403/404→N/A，401→登录态，5xx/超时/坏 JSON→FAIL）、**扫描阶段失败记账**（page / lecture_live 详情的 500/超时/坏 JSON/403/404/401）、`item_status()` 统一状态语义、`--list-only` 退出码（真跑 `main()`）、已有文件大小比对与短读容差（回放 8% vs 普通附件 0%）、`.7z` 冲突改名保扩展名与项目包判定、新旧登录态格式兼容、Cookie 安全属性还原、文件名安全（含 Windows 保留名）、路径冲突消解、项目包判定、CSV 公式注入防护、时间戳固定 UTC+8（跨时区一致）、清单导出（`stage` / `err_kind` 入 CSV）、多来源计数 |
| `test_selfcheck.py` | 登录态五种状态判定、检查级别（警告 vs 失败）、退出码、默认不联网、自检清单与 `scripts/` 实际文件一致 |

`test_fetch.py` 用 **假 opener**（`FakeOpener` / `RangeFakeOpener` / `FakeResponse` /
`http_err`）脚本化服务端行为，所以能测「第一次超时第二次成功」「服务端把 Range 起点
从 5MB 挪到 4MB」这类真实环境里很难复现的路径。新增用例优先用这个范式，
不要引入 `responses` / `vcr` 之类的新依赖——`lms_fetch.py` 只依赖标准库。

时区用例（`test_stamp_stable_across_machine_timezones`）会遍历
UTC / Asia-Tokyo / America-New_York / Asia-Shanghai，改 `TZ` 环境变量 + `time.tzset()`
验证，因此**不依赖跑测试机器所在的时区**。

改测试断言逻辑时要保持用例总数，`release.py` 的 `verify_zip` 会交叉核对
（静态扫 `def test_` vs 实跑 `Ran N tests`）。新增测试文件要**同时**改三处：
`release.py` 的关键文件清单 + 测试循环、`ci.yml`、`pack.py`。

---

## 完整更新日志

README 只留最近两版，历史在这里：

| 版本 | 变更 |
|---|---|
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
| `release.py` | 发版本：版本号自检 → 打包 → 校验 → tag → Release → 附件 |
| `.github/workflows/ci.yml` / `release.yml` | CI 与自动发版 workflow，随仓库分发（`release.py` 的 `INCLUDE` 含整个 `.github/`） |
| `.github/scripts/pack.py` | CI 发版用的打包脚本，`INCLUDE` 必须与 `release.py` 保持同步 |
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
