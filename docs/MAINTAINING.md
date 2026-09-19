# 维护者指南

面向修改这个仓库的人。使用者请看 [README](../README.md)。

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

用 API 查构建记录时，会看到这种输出：

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

> 附注一：查 Pages 配置的 `GET /repos/<owner>/<repo>/pages` 有时会返回 **404**，
> 哪怕站点实际在正常运行。这是 token 权限范围的问题（该端点需要 `Pages` 读权限），
> 不是"站点被删了"。**以线上实测为准，不要以这个端点为准。**
>
> 附注二：验证时优先看响应头的 `X-Cache: MISS` + `Age: 0`——这表示命中源站新内容，
> 而非 CDN 缓存的旧版本。如果拿到 `X-Cache: HIT` 且 `Age` 较大，就是缓存，
> 过一会再请求。

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

**不需要** `Workflows`（CI 配置是以 `.txt` 形式推的），
**不需要** `Administration`（改默认分支才用，可忽略）。

---

## 相关文件

| 文件 | 用途 |
|---|---|
| `docs/index.html` | 文档站页面，改它就够了 |
| `docs/MAINTAINING.md` | 本文件，站点的维护说明 |
| `push_docs.py` | 推 `docs/` 到 GitHub（在维护者工作区里） |
| `release.py` | 发版本：版本号自检 → 打包 → 校验 → tag → Release → 附件 |
| `ci.yml.txt` / `release.yml.txt` | CI 配置，用 `enable-ci.bat` 还原启用 |
