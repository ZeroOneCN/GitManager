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

### 1.4 里程碑与时间计划（2026-11-04）

| 里程碑 | 内容 | 计划完成 |
| --- | --- | --- |
| M1 | 文档体系建立（README / ALIGNMENT / CONSENSUS / DESIGN / TASK） | 2026-11-04 18:45 |
| M2 | 后端基础层（配置、SQLite、Git 执行器 + 审计） | 2026-11-04 19:10 |
| M3 | 业务 API（仓库、体检、pull、push、commit、审计） | 2026-11-04 19:40 |
| M4 | 中文前端页面（看板 / 提交面板 / 审计） | 2026-11-04 20:10 |
| M5 | 真实仓库端到端自测（带 upstream） | 2026-11-04 20:35 |
| M6 | 验收文档回写 + 本地 git 提交（**严禁 push**） | 2026-11-04 20:55 |

---

## 二、实施策略

### 2.1 技术选型（既定，不替换）

- 后端：Python 3.13 + FastAPI + uvicorn；所有 Git 操作通过 `subprocess` 调用**系统 git CLI**，不引入 GitPython 等第三方库。
- 存储：SQLite 单文件（`data/gitmanager.db`），存仓库配置与操作审计。
- 前端：无构建链静态页面（`app/templates/index.html` + `app/static/*`），由 FastAPI 直接托管。

### 2.2 关键安全策略（最保守实现，已固化）

1. **监听地址**：启动时校验 `APP_HOST` 必须为 `127.0.0.1` / `localhost`，否则拒绝启动。
2. **仓库白名单**：`.env` 中 `GITMANAGER_ALLOWED_ROOTS`（分号分隔的绝对目录）；添加仓库时对目标路径做 `Path.resolve()` 后必须位于某一根目录下，且必须是 Git 仓库（`git rev-parse --show-toplevel` 校验）。默认仅 `E:\Code`。
3. **操作面收敛**：后端只暴露固定动作（status/fetch/pull/push/commit/stash 内部使用），不存在通用命令执行接口；所有命令以参数数组传入，`shell=False`。
4. **pull 自动 stash**：默认关闭（`PULL_AUTO_STASH_DEFAULT=false`），每次请求必须显式 `auto_stash=true` 才会执行；stash 后 pull 失败则**保留 stash** 并提示，绝不静默丢弃。
5. **push**：仅当前分支、仅已配置 upstream；无 upstream 需显式 `set_upstream=true` 并给出 remote/branch。force 仅在 `force=true` **且** `confirm_force=true` 时以 `--force-with-lease` 执行，并在审计表标记 `forced=1`。
6. **无破坏性能力**：不实现删除仓库目录、`reset --hard`、批量推送。
7. **凭据安全**：`GIT_TERMINAL_PROMPT=0` 防止子进程挂起等待输入；`.env` 不存放任何密钥，`.gitignore` 排除 `.env` 与数据库。

### 2.3 操作流程

- 遵循 ALIGN → CONSENSUS → DESIGN → TASK → 实现 → 验收 顺序推进。
- 每个任务完成后回写 `docs/GitManagerWeb/TASK_*.md` 状态与 `ACCEPTANCE_*.md` 结论。
- GitManager 自身代码只做**本地 git commit**，严禁 push 到任何远程。

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
