# GitManager Web —— 本机 Git 管理 Web 应用

> 单一管理仓库：本文件是该项目的规划、实施策略与进度跟踪的唯一权威来源。
> 任何计划变更、里程碑调整、任务完成，均须**立即**回写本文件。

- 项目路径：`E:\Code\GitManager`
- 运行环境：Windows 10/11 + Python 3.13（`D:\CodeDevelop\Env\Python313`）+ 系统 Git CLI（`D:\CodeDevelop\Env\Git\cmd\git.exe`）
- 服务地址：`http://127.0.0.1:8765`（仅本机可访问）
- 文档目录：`docs/GitManagerWeb/`

---

## 一、项目规划

### 1.1 项目目标

为开发者提供一个**纯本机、零构建链**的 Git 仓库体检与安全操作面板，覆盖日常最高频的三件事：看状态、拉取、推送；并附带一个可控的提交面板与完整操作审计。

### 1.2 本期范围（In Scope）

| 编号 | 功能 | 说明 |
| --- | --- | --- |
| F1 | 仓库管理 | 手动添加本地仓库（支持一次多选目录）、列表展示、移除配置；持久化到 SQLite |
| F2 | 体检看板 | 分支、脏文件数量与明细（modified/untracked/staged）、ahead/behind、冲突文件、最近提交、最后 fetch 时间；支持全局刷新与单仓刷新 |
| F3 | 一键 pull | 默认 `--ff-only`；可读失败原因；可选 pull 前自动 stash（显式确认，成功后自动 pop） |
| F4 | 一键 push | 仅推送当前分支到已配置 upstream；无 upstream 时引导设置；禁止 force push，强制需二次确认并留审计 |
| F5 | 提交面板 | 勾选文件 + 提交信息 + `git commit`（文件路径精确指定） |
| F6 | 操作审计 | 所有 git 命令（时间/仓库/动作/命令/退出码/输出摘要）落库，页面可查，失败标红 |
| F7 | 安全边界 | 仅监听 127.0.0.1；仓库路径白名单；无删除目录 / `reset --hard` / 批量强推能力 |

### 1.3 明确不做（Out of Scope）

- rebase / cherry-pick / merge / tag / 分支创建删除
- 远程仓库凭据管理、多 remote 选择器（仅使用仓库已有配置）
- 定时任务、后台轮询、多用户、鉴权体系（本机单人使用）
- 打包成 exe / 容器化

### 1.4 第二期范围（2026-11-04 追加，In Scope）

| 编号 | 功能 | 说明 |
| --- | --- | --- |
| F8 | 仓库发现与批量添加 | 给定一个或多个根目录递归扫描本地 Git 仓库，跳过 `node_modules`、`.venv`、`dist`、`build` 等噪音目录；深度与结果上限可配；进度可轮询；结果带复选框一键批量加入；扫描全程只读 |
| F9 | 多根路径白名单 | 白名单由单值扩展为多根路径并持久化到 SQLite（`.env` 的 `GITMANAGER_ALLOWED_ROOTS` 作为首次种子）；页面可增删；移除时若仍有受管仓库给出 409 提示，强制移除后该路径操作被拒绝 |
| F10 | SSH 身份配置 | 扫描 `%USERPROFILE%\.ssh` 私钥与 config Host 别名，只存路径与别名；身份可增删改禁用、设全局默认或绑定单仓；“测试连接”原样展示输出；git 网络操作通过 `GIT_SSH_COMMAND` 注入身份 |
| F11 | Tab 切换滚动稳定性 | 由 `.app-main` 单一滚动容器 + `scrollbar-gutter: stable` 根治滚动条出现/消失造成的宽度抖动，三档视口自动化断言 |
| F12 | Apple 简约视觉体系 | 全站按 Apple 设计规范改造：中性灰阶、单一强调色（#0071e3）、18px 卡片、无投影、仅透明度/颜色过渡 |

### 1.5 里程碑与时间计划（2026-11-04）

| 里程碑 | 内容 | 计划完成 |
| --- | --- | --- |
| M1 | 文档体系建立（README / ALIGNMENT / CONSENSUS / DESIGN / TASK） | 2026-11-04 18:45 |
| M2 | 后端基础层（配置、SQLite、Git 执行器 + 审计） | 2026-11-04 19:10 |
| M3 | 业务 API（仓库、体检、pull、push、commit、审计） | 2026-11-04 19:40 |
| M4 | 中文前端页面（看板 / 提交面板 / 审计） | 2026-11-04 20:10 |
| M5 | 真实仓库端到端自测（带 upstream） | 2026-11-04 20:35 |
| M6 | 验收文档回写 + 本地 git 提交（**严禁 push**） | 2026-11-04 20:55 |
| M7 | 第二期后端：扫描器、多根白名单、SSH 身份服务 | 2026-11-04 19:35 |
| M8 | 第二期接口与前端面板（扫描 / 根目录 / SSH） | 2026-11-04 19:50 |
| M9 | UI 改造：滚动条抖动根治 + Apple 简约风格 | 2026-11-04 20:05 |
| M10 | 第二期端到端自测（扫描 / 白名单 / SSH / 审计身份名） | 2026-11-04 20:15 |
| M11 | 三档视口布局校验 + 文档回写 + 本地提交（**严禁 push**） | 2026-11-04 20:30 |

---

## 二、实施策略

### 2.1 技术选型（既定，不替换）

- 后端：Python 3.13 + FastAPI + uvicorn；所有 Git 操作通过 `subprocess` 调用**系统 git CLI**，不引入 GitPython 等第三方库。
- 存储：SQLite 单文件（`data/gitmanager.db`），存仓库配置与操作审计。
- 前端：无构建链静态页面（`app/templates/index.html` + `app/static/*`），由 FastAPI 直接托管。

### 2.2 关键安全策略（最保守实现，已固化）

1. **监听地址**：启动时校验 `APP_HOST` 必须为 `127.0.0.1` / `localhost`，否则拒绝启动。
2. **仓库白名单**：动态多根路径列表（SQLite `allowed_roots` 表），`.env` 的 `GITMANAGER_ALLOWED_ROOTS`（分号分隔的绝对目录）仅作为首次种子；添加仓库、目录浏览、扫描与所有 git 操作前均做 `Path.resolve()` + `os.path.normcase` 前缀校验，且添加时必须是 Git 仓库（`git rev-parse --show-toplevel` 校验）。默认种子为 `E:\Code`。
3. **操作面收敛**：后端只暴露固定动作（status/fetch/pull/push/commit/stash 内部使用），不存在通用命令执行接口；所有命令以参数数组传入，`shell=False`。
4. **pull 自动 stash**：默认关闭（`PULL_AUTO_STASH_DEFAULT=false`），每次请求必须显式 `auto_stash=true` 才会执行；stash 后 pull 失败则**保留 stash** 并提示，绝不静默丢弃。
5. **push**：仅当前分支、仅已配置 upstream；无 upstream 需显式 `set_upstream=true` 并给出 remote/branch。force 仅在 `force=true` **且** `confirm_force=true` 时以 `--force-with-lease` 执行，并在审计表标记 `forced=1`。
6. **无破坏性能力**：不实现删除仓库目录、`reset --hard`、批量推送。
7. **凭据安全**：`GIT_TERMINAL_PROMPT=0` 防止子进程挂起等待输入；`.env` 不存放任何密钥，`.gitignore` 排除 `.env` 与数据库。
8. **SSH 身份**：只保存私钥**路径**与 Host 别名，任何接口都不返回私钥正文；网络操作以环境变量 `GIT_SSH_COMMAND`（`ssh -o IdentitiesOnly=yes -i "<key>"`）注入，不拼接进命令行；连接测试固定 `BatchMode=yes`、`StrictHostKeyChecking=accept-new` 且短超时。
9. **扫描只读**：扫描器只读取目录结构与 `.git/HEAD`，不执行任何 git 命令，更不写仓库。

### 2.3 操作流程

- 遵循 ALIGN → CONSENSUS → DESIGN → TASK → 实现 → 验收 顺序推进。
- 每个任务完成后回写 `docs/GitManagerWeb/TASK_*.md` 状态与 `ACCEPTANCE_*.md` 结论。
- GitManager 自身代码只做**本地 git commit**，严禁 push 到任何远程。

### 2.4 布局与视觉基线（第二期固化）

- **滚动容器唯一化**：`html, body { height: 100%; overflow: hidden; }`，`body` 为 flex 纵向布局，`.app-header` 不参与滚动，`.app-main` 是唯一纵向滚动容器并固定 `scrollbar-gutter: stable`；表头 `position: sticky`；不再出现「body 与内部容器同时滚动」。
- **禁止的补丁方案**：不使用 `visibility:hidden` 包裹滚动条、`overflow:hidden` + JS 补滚动、`document.body.style.paddingRight = 滚动条宽度` 等会引发重排闪烁的做法。
- **Apple 视觉规范**：字体栈 `-apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI Variable", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif`；背景 `#f5f5f7`、卡片 `#ffffff`、正文 `#1d1d1f`、次级 `#6e6e73`、强调色 `#0071e3`；卡片圆角 18px、按钮胶囊；无投影、无渐变、无位移动效，仅 150–250ms 的透明度/颜色过渡，并尊重 `prefers-reduced-motion`。
- **深浅色**：本期只交付浅色模式（`color-scheme: light`），不做半成品暗色；暗色列为待办。

---

## 三、进度跟踪

> 状态标记：✅ 完成 ｜ 🚧 进行中 ｜ ⬜ 未开始

| 时间 | 里程碑/任务 | 状态 | 结论摘要 |
| --- | --- | --- | --- |
| 2026-11-04 18:27 | 项目初始化检查 | ✅ | 目录为空；Python 3.13.9、git 2.51.2、FastAPI 0.141.1、uvicorn 0.52.3 均可用 |
| 2026-11-04 18:30 | M1 文档体系 | ✅ | 完成 README 与 ALIGNMENT / CONSENSUS / DESIGN / TASK 文档，固化白名单与 pull stash 默认值 |
| 2026-11-04 18:34 | M2 后端基础层 | ✅ | `config.py`（监听地址/白名单/超时校验）、`db.py`（SQLite WAL、仓库与审计表）、`git_runner.py`（唯一 subprocess 出口，逐次审计）完成，`compileall` 通过 |
| 2026-11-04 18:39 | M3 业务 API | ✅ | `domain.py`（porcelain -z 解析与错误映射）、`services.py`、`main.py` 完成：仓库 CRUD、体检/全局刷新、fetch/pull/push/commit、审计查询、目录浏览；补记 `remotes` 接口 |
| 2026-11-04 18:44 | M4 前端页面 | ✅ | `index.html` + `app.css` + `app.js` 完成：看板卡片、健康度结论、提交面板、pull/push 二级确认、目录多选、审计筛选与展开；`node --check` 通过 |
| 2026-11-04 18:48（第 3 轮） | M5 端到端自测 | ✅ | 真实 bare 远端 + 工作仓库 + 真实 uvicorn（8767）跑通 **51/51 PASS**；覆盖状态、commit 仅勾选文件、pull 脏保护、自动 stash、push、无 upstream 引导、force 拒绝审计、non-ff 可读错误、审计与移除配置 |
| 2026-11-04 19:05 | M6 验收与本地提交 | ✅ | 回写 README/ACCEPTANCE/FINAL/TODO；启动 8765 正式服务核对入口页；本地 git 提交（**未 push**） |
| 2026-11-04 19:20 | M7 第二期后端 | ✅ | 新增 `app/scanner.py`（迭代栈扫描、噪音目录、深度/上限/进度）、`app/ssh_service.py`（密钥扫描/指纹/身份 CRUD/绑定/测试）、`allowed_roots` 与 SSH 三张表；`compileall` 与 `import app.main` 通过 |
| 2026-11-04 19:40 | M8 第二期接口与前端 | ✅ | `/api/scan/*`、`/api/allowed-roots`、`/api/ssh/*`、`/api/repos/{id}/ssh-identity` 全部落地；前端扫描/根目录/SSH 三个面板；`node --check` 通过 |
| 2026-11-04 19:55 | M9 UI 改造 | ✅ | `.app-main` 单一滚动容器 + `scrollbar-gutter: stable`；全站 Apple 简约视觉（灰阶/单强调色/18px 卡片/胶囊按钮/无投影） |
| 2026-11-04 20:05 | M10 第二期自测 | ✅ | `scripts/selftest.py` 扩展为 **105/105 PASS**（第一期 51 项 + 第二期 54 项），覆盖扫描、批量添加、白名单增删与拒绝、SSH 扫描/CRUD/绑定/测试、审计身份名 |
| 2026-11-04 20:15 | M11 三档视口校验 | ✅ | `tests/ui_layout_check.py` 在 1280×720 / 1440×900 / 1920×1080 下 **15/15 PASS**，内容区宽度与页头位置切换前后一致；截图存 `docs/GitManagerWeb/ui-shots/` |
