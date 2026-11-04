# GitManager Web

一个在本机浏览器里管理多个 Git 仓库的轻量 Web 工具：扫描磁盘批量登记仓库、一屏查看全部仓库状态、按需 pull / push / 提交，并把每一次 git 操作写进审计日志。

> 定位：面向个人开发者的「本机 Git 控制台」。默认只监听 `127.0.0.1`，不做代码托管、不代替 Git 客户端，也不提供任何删除目录、`reset --hard`、强制推送之类的破坏性入口。

---

## 功能特性

- **仓库管理**：登记本机任意路径下的仓库，支持重命名、移除（仅从面板移除，不删除磁盘文件）。
- **扫描批量添加**：指定目录递归扫描 Git 仓库，可取消、可限制深度与结果数量，自动跳过 `node_modules`、`.venv` 等噪音目录。
- **体检看板**：汇总每个仓库的分支、领先/落后提交数、脏文件、冲突、远端协议与 SSH 身份生效情况；支持全部仓库同时刷新。
- **一键 pull / push**：pull 支持显式勾选自动 stash；push 支持设置上游分支与显式强制（需二次确认）。所有失败都会给出结构化诊断与原始输出。
- **提交面板**：按仓库查看变更，勾选文件、填写提交信息后提交；支持只提交已暂存内容。
- **忽略规则**：区分「未跟踪可忽略」「已跟踪但命中 ignore」「被反向/嵌套规则影响」三类成因，可写入 `.gitignore`、仅本地静音、或 `git rm --cached` 取消跟踪（保留磁盘文件）。
- **审计日志**：记录操作类型、命令、退出码、耗时、生效的 SSH 身份；局域网来源的请求额外记录客户端 IP。
- **SSH 身份**：扫描 `~/.ssh` 下的密钥，登记、改名、设为默认、按仓库绑定；执行 git 时通过 `GIT_SSH_COMMAND` + `IdentitiesOnly=yes` 注入，密钥路径不会拼进命令行。页面内可发起 SSH 连接测试。
- **HTTPS 凭据诊断**：读取 Windows 凭据管理器中 `git:https://host` 条目（不显示密码），可清除指定 host 的缓存凭据；可交互式发起一次凭据登录流程。
- **远端错误归因**：把 403 / 401 / Host key / 网络 / non-fast-forward 等原始输出归类为可执行建议，并保留完整原文供复制。
- **局域网访问**：可选开启，开启后强制令牌鉴权 + Host 白名单，带独立登录页。

---

## 界面说明

页面采用 Apple 浅色风格（浅灰背景、白色卡片、圆角与轻阴影），主要区域：

- **顶栏**：品牌标识、当前服务地址、全局刷新、扫描入口、SSH 身份、HTTPS 凭据、服务设置。
- **汇总条**：全部仓库的待处理变更数、已静音数、已跟踪但被 ignore 数，以及最近一次刷新时间。
- **仓库卡片**：仓库名、当前分支、远端协议徽标（仅非默认协议显示，如 `SSH`、`本地`）、领先/落后、脏文件数；底部为固定单行的操作按钮（刷新、拉取、推送、提交、忽略规则等），溢出时在按钮栏内部横向滚动，不换行。
- **提交面板**：文件勾选列表、提交信息输入、提交按钮；同时展示「已静音」的提示，避免误判工作区干净。
- **忽略规则面板**：按三类成因分组，展示规则来源（`文件:行号:规则`），提供写 `.gitignore`、静音、取消跟踪三类动作。
- **弹窗**：全部使用自研弹窗（无浏览器原生 `confirm` / `alert`），危险操作需二次确认并列出受影响路径。
- **登录页**：仅在开启局域网访问时出现，输入访问令牌后写入 HttpOnly Cookie。

---

## 快速开始

环境要求：**Windows 10/11 + Python 3.13**（开发与验证使用 Python 3.13.9）。需要本机已安装 Git，并在 `.env` 中写明 `git.exe` 路径。

```powershell
# 1. 创建并激活虚拟环境
python -m venv .venv
.venv\Scripts\Activate.ps1

# 2. 安装锁定版本的依赖
pip install -r requirements.txt

# 3. 准备配置
Copy-Item .env.example .env
# 按需修改 .env 中的 GIT_EXECUTABLE、GITMANAGER_ALLOWED_ROOTS

# 4. 启动
python run.py
```

启动后访问 <http://127.0.0.1:8765>。默认仅本机可访问，且**不需要登录**。

> 若使用 UI 自动化校验，另需 `pip install -r requirements-dev.txt` 并执行 `python -m playwright install chromium`。

---

## 配置项

所有配置位于项目根目录 `.env`，修改后需重启服务。

| 变量 | 默认值 | 作用 | 风险提示 |
| --- | --- | --- | --- |
| `GITMANAGER_HOST` | `127.0.0.1` | 监听地址；改为 `0.0.0.0` 开启局域网访问 | 改为非回环地址即暴露到网络，必须同时配置令牌与 Host 白名单 |
| `GITMANAGER_PORT` | `8765` | 监听端口 | 端口冲突时启动失败，需换端口 |
| `GITMANAGER_ACCESS_TOKEN` | 空 | 局域网访问令牌；留空时非回环监听首次启动自动生成并写回 `.env` | 令牌等同管理员权限，泄露后同网段设备可操作你的仓库 |
| `GITMANAGER_ALLOWED_HOSTS` | `127.0.0.1;localhost` | 允许的 Host 头（分号分隔），防 DNS rebinding | 局域网访问必须显式加入本机 IP/主机名，否则一律 400 |
| `GIT_EXECUTABLE` | 示例路径 | git 可执行文件绝对路径 | 路径错误时所有操作失败 |
| `GITMANAGER_DB` | `data/gitmanager.db` | SQLite 数据库路径 | 删除会丢失仓库登记、静音规则与审计 |
| `GITMANAGER_ALLOWED_ROOTS` | `E:\Code` | 仓库白名单根目录（分号分隔） | 添加的仓库必须位于这些目录内，写小会限制可用范围 |
| `GITMANAGER_LOG_RETENTION_DAYS` | `90` | 审计日志保留天数 | 过小会丢失排查依据 |
| `PULL_AUTO_STASH_DEFAULT` | `false` | pull 时是否默认自动 stash | 开启后本地改动会被暂存，需手动恢复 |
| `GIT_OPERATION_TIMEOUT_SECONDS` | `120` | 单次 git 子进程超时（秒） | 大仓库操作可能超时 |
| `GITMANAGER_SCAN_MAX_DEPTH` | `6` | 扫描最大深度 | 过大扫描变慢 |
| `GITMANAGER_SCAN_RESULT_LIMIT` | `200` | 扫描结果上限 | 过小可能漏仓库 |
| `GITMANAGER_SCAN_NOISE_DIRS` | 内置默认 | 扫描跳过的噪音目录（分号分隔） | 自定义会覆盖内置列表 |
| `SSH_DIR` | `%USERPROFILE%\.ssh` | SSH 密钥扫描目录 | 指向错误目录会导致扫描不到密钥 |
| `SSH_EXECUTABLE` | 示例路径 | `ssh.exe` 路径 | 缺失时 SSH 连接测试不可用 |
| `SSH_KEYGEN_EXECUTABLE` | 示例路径 | `ssh-keygen.exe` 路径 | 缺失时无法在页面生成密钥 |
| `SSH_TEST_TIMEOUT_SECONDS` | `10` | SSH 连接测试超时 | 过短会误报不可达 |

---

## 局域网访问

默认推荐保持 `GITMANAGER_HOST=127.0.0.1`。确需从同网段其他设备访问时：

1. 修改 `.env`：

```dotenv
GITMANAGER_HOST=0.0.0.0
GITMANAGER_PORT=8765
# 留空则首次启动自动生成并写回 .env，控制台也会打印
GITMANAGER_ACCESS_TOKEN=
# 必须显式加入本机局域网 IP（示例：192.168.1.20），可再加主机名
GITMANAGER_ALLOWED_HOSTS=127.0.0.1;localhost;192.168.1.20
```

2. 启动 `python run.py`。控制台会打印：
   - 本机与探测到的局域网 IPv4 访问地址；
   - 自动生成的访问令牌（若原本为空）；
   - Windows 防火墙放行命令（**服务不会自动修改防火墙**）。

3. 如需放行端口，手动执行（管理员 PowerShell）：

```powershell
netsh advfirewall firewall set rule name="GitManager Web 8765" new enable=yes
# 若规则不存在则创建：
netsh advfirewall firewall add rule name="GitManager Web 8765" dir=in action=allow protocol=TCP localport=8765 profile=private
```

4. 其他设备打开 `http://<本机IP>:8765`，会跳转到登录页，输入 `.env` 中的令牌；登录成功后令牌写入 HttpOnly + SameSite=Lax Cookie。

安全机制：

- **令牌鉴权**：非回环监听时，所有 `/api/**` 均校验 Cookie；静态资源与登录页可匿名访问。未通过的接口返回 401。
- **Host 白名单**：Host 头不匹配返回 400，防止 DNS rebinding 把恶意站点指向本机服务。
- **审计增强**：来自非本机来源的关键 git 操作会额外记录客户端 IP。
- **只读「服务设置」**：页面可查看监听地址、鉴权状态、允许的 Host、可复制的访问链接，但**不显示完整令牌**。

> 安全警告：开启局域网访问后，同一网段内任何拿到令牌的设备都可以 pull / push / commit 你的仓库。请只在可信网络（家庭/内网）临时开启，用完改回 `127.0.0.1`。

---

## SSH 身份配置

1. 在 `.env` 中确认 `SSH_DIR`、`SSH_EXECUTABLE`、`SSH_KEYGEN_EXECUTABLE` 指向正确位置。
2. 顶栏「SSH 身份」面板可扫描密钥、登记身份、设为全局默认，或为单个仓库绑定身份。
3. 执行 git 时程序仅把**密钥路径**注入环境变量：

```text
GIT_SSH_COMMAND=<ssh.exe> -o IdentitiesOnly=yes -i "<私钥路径>"
```

命令行与审计中不出现私钥内容；`IdentitiesOnly=yes` 避免 SSH agent 用错密钥。
4. 面板内可发起连接测试（`ssh -T`），结果只说明认证是否通过，不会修改远端。

---

## 常见问题

**push 返回 403 / `Permission to <owner>/<repo>.git denied to <account>`**
HTTPS 远端使用的是 Windows 凭据管理器缓存的账号，而该账号对目标仓库没有写权限；你配置的 SSH 身份对 HTTPS 远端不生效。可在仓库卡片「改用 SSH 地址」把 GitHub/Gitee/GitLab 的 HTTPS 地址转换为 SSH，或在「HTTPS 凭据」面板清除该 host 缓存后重新登录正确账号。

**文件一直显示「待处理」，但已写进 `.gitignore`**
ignore 规则只对未跟踪文件生效。已跟踪文件即使命中规则也不会被忽略：在「忽略规则」面板中会标记为「已被跟踪，忽略规则不生效」，可用「取消跟踪（保留文件）」执行 `git rm --cached`，然后额外提交一次删除变更（工具不会自动提交）。

**切换标签时页面有轻微抖动**
这是布局重排造成的视觉现象，与仓库数据无关，不影响任何 git 操作。

**pull 被拒绝**
常见原因：工作区有未提交改动、远端与本地历史分叉（非 fast-forward）、或当前分支没有上游。可先提交/暂存本地改动，再显式勾选自动 stash 重试；若历史分叉，需要手工 merge/rebase（本工具不提供强推入口）。

**局域网设备打不开页面**
依次检查：`.env` 是否改为 `0.0.0.0`、`GITMANAGER_ALLOWED_HOSTS` 是否加入该设备访问时使用的 IP/主机名、Windows 防火墙是否放行端口。返回 400 通常是 Host 白名单未匹配；返回 401 是尚未登录。

---

## 目录结构

```text
GitManager/
├─ app/
│  ├─ main.py            # FastAPI 入口、路由、访问控制中间件
│  ├─ config.py          # .env 读取、监听/令牌/Host 白名单、路径白名单
│  ├─ db.py              # SQLite（WAL）建表、迁移、审计与静音存储
│  ├─ git_runner.py      # 唯一 git 子进程出口 + 审计 + SSH 环境注入
│  ├─ services.py        # 业务编排（状态、扫描、提交、忽略、凭据、诊断）
│  ├─ remote_diagnosis.py# 远端错误分类器
│  ├─ static/            # 原生 CSS/JS（无构建链）
│  └─ templates/         # index.html / login.html
├─ scripts/selftest.py   # 端到端自测（独立端口与临时仓库）
├─ tests/ui_phase3_check.py # Playwright UI 校验
├─ docs/GitManagerWeb/   # 设计与验收文档
├─ data/                 # 运行时 SQLite 与日志（不入库）
├─ .env.example
├─ requirements.txt / requirements-dev.txt
└─ run.py
```

---

## 测试

```powershell
# 端到端自测：会在 E:\Code\_gitmanager_selftest 下创建临时仓库，结束后自动清理
python scripts\selftest.py

# UI 校验：需先启动 8765，并安装 Playwright 与 Chromium
python tests\ui_phase3_check.py
```

当前基线：`selftest` 171/171 通过；UI 校验 52/52 通过（含 1280 / 1440 / 1920 三档视口、按钮单行、登录流、服务设置面板、无原生弹窗）。

---

## 安全边界

- 默认只监听 `127.0.0.1`；只有显式改为 `0.0.0.0` 才允许局域网访问，且此时强制令牌鉴权与 Host 白名单。
- `.env`、`data/`（SQLite 与日志）已在 `.gitignore` 中排除，不进入版本库。
- 不提供删除仓库目录、`git reset --hard`、强制推送的常规入口；强制推送仅在 push 面板显式勾选并二次确认后执行。
- 修改远端 URL、清除 HTTPS 凭据、取消跟踪等操作均需弹窗确认并写入审计。
- 仅保存 SSH 私钥的**路径**，不读取、不复制、不上传私钥内容。

---

## 许可证与免责声明

本项目为个人自用工具，未附带开源许可证；未经作者许可请勿再分发。使用本工具即表示你理解：所有 git 操作都在真实仓库上执行，pull / push / commit / 取消跟踪等行为可能改变工作区与版本库状态。操作前请确认仓库状态，重要仓库建议先自行备份。作者不对因使用本工具造成的数据丢失或远端变更承担责任。

---

## 更新日志

- **第一期**：仓库登记、状态看板、pull / push、提交面板、基础审计，Apple 浅色界面。
- **第二期**：目录扫描批量添加、白名单根目录、SSH 身份管理与仓库绑定、连接测试。
- **第三期**：忽略规则面板、审计查询与筛选、单行操作区与最窄卡片适配。
- **第四/五期**：忽略成因归因（可忽略 / 已跟踪命中规则 / 规则来源）、本地静音、`git rm --cached` 取消跟踪；远端错误结构化诊断、HTTPS/SSH 协议徽标与地址转换、Windows HTTPS 凭据查看与清除。
- **发布准备（本版）**：局域网监听 + 令牌鉴权 + Host 白名单 + 登录页 + 服务设置面板；依赖精确锁定与开发依赖拆分；仓库清理与 `.gitignore` 完善；README 改为对外发布版；补齐自测与 UI 校验。
