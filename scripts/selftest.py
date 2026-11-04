"""GitManager 端到端自测脚本。

流程：在项目白名单根目录内创建临时 bare 远端与工作仓库 → 启动真实 uvicorn 服务
（使用独立的 selftest 数据库）→ 通过 HTTP API 验证仓库管理、体检、提交、pull、
push、强制推送防护、审计与白名单 → 清理临时数据。

用法：
    D:\\CodeDevelop\\Env\\Python313\\python.exe scripts\\selftest.py
可选参数：
    --keep   保留临时仓库与数据库，便于人工复查
    --port   指定测试端口（默认 8767）
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_settings  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    """记录一条断言结果并立即打印。

    参数：
        name: 断言名称。
        ok: 是否通过。
        detail: 附加说明（失败原因或关键数据）。
    返回值：
        与 ok 相同的布尔值，便于链式判断。
    """
    RESULTS.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" —— {detail}" if detail else ""))
    return ok


def http_call(method: str, base: str, path: str, payload=None, timeout: int = 30):
    """调用测试服务的 HTTP API。

    参数：
        method: HTTP 方法。
        base: 服务根地址。
        path: API 路径。
        payload: JSON 请求体（可选）。
        timeout: 超时秒数。
    返回值：
        (HTTP 状态码, 解析后的 JSON 或 None)。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
    parsed = None
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body[:500]}
    return status, parsed


def run_git(git_exe: str, args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    """执行本地 git 命令（仅用于自测准备与校验）。

    参数：
        git_exe: git 可执行文件路径。
        args: git 参数数组。
        cwd: 工作目录。
        check: 是否在失败时抛出异常。
    返回值：
        subprocess.CompletedProcess。
    """
    completed = subprocess.run(
        [git_exe, *args],
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        shell=False,
    )
    if check and completed.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} 失败（{cwd}）：{completed.stderr.strip()}"
        )
    return completed


def configure_identity(git_exe: str, repo: Path) -> None:
    """为测试仓库设置本地提交身份。

    参数：
        git_exe: git 可执行文件路径。
        repo: 仓库目录。
    返回值：
        无。
    """
    run_git(git_exe, ["config", "user.name", "GitManager Selftest"], cwd=repo)
    run_git(git_exe, ["config", "user.email", "selftest@local"], cwd=repo)


def write_file(path: Path, content: str) -> None:
    """写入 UTF-8 文本文件。

    参数：
        path: 文件路径。
        content: 文件内容。
    返回值：
        无。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def wait_for_server(base: str, timeout: float = 30.0) -> bool:
    """轮询健康检查直到服务就绪。

    参数：
        base: 服务根地址。
        timeout: 最长等待秒数。
    返回值：
        就绪返回 True，超时返回 False。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            status, _ = http_call("GET", base, "/api/health", timeout=2)
            if status == 200:
                return True
        except (urllib.error.URLError, ConnectionResetError, TimeoutError):
            pass
        time.sleep(0.35)
    return False


def main() -> int:
    """执行完整自测流程。

    参数：
        无（命令行参数由 sys.argv 处理）。
    返回值：
        进程退出码：0 全部通过，1 存在失败。
    """
    settings = load_settings()
    git_exe = settings.git_executable
    keep = "--keep" in sys.argv
    port = 8767
    if "--port" in sys.argv:
        try:
            port = int(sys.argv[sys.argv.index("--port") + 1])
        except (IndexError, ValueError):
            print("--port 参数无效，使用默认 8767")
    base = f"http://{settings.host}:{port}"

    test_base = settings.allowed_roots[0] / "_gitmanager_selftest"
    test_root = test_base / f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    remote_dir = test_root / "remote.git"
    remote2_dir = test_root / "remote2.git"
    work_dir = test_root / "work"
    clone_dir = test_root / "clone"
    noup_dir = test_root / "noup"
    notrepo_dir = test_root / "notrepo"
    selftest_db = PROJECT_ROOT / "data" / "selftest.db"
    server_log = PROJECT_ROOT / "data" / "selftest_server.log"
    server: subprocess.Popen | None = None
    log_handle = None

    def cleanup() -> None:
        """停止服务并清理临时文件。

        参数：
            无。
        返回值：
            无。
        """
        nonlocal server, log_handle
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=8)
            except subprocess.TimeoutExpired:
                server.kill()
                server.wait(timeout=5)
        if log_handle is not None:
            try:
                log_handle.close()
            except OSError:
                pass
            log_handle = None
        if keep:
            print(f"已保留临时数据：{test_root}；数据库：{selftest_db}")
            return
        shutil.rmtree(test_root, ignore_errors=True)
        for _attempt in range(4):
            if not test_root.exists():
                break
            time.sleep(0.6)
            shutil.rmtree(test_root, ignore_errors=True)
        try:
            if test_base.exists() and not any(test_base.iterdir()):
                test_base.rmdir()
        except OSError:
            pass
        if test_root.exists():
            print(f"警告：临时目录未能完全删除，请手动清理：{test_root}")
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(selftest_db) + suffix)
            if target.exists():
                try:
                    target.unlink()
                except OSError:
                    pass
        if server_log.exists():
            try:
                server_log.unlink()
            except OSError:
                pass

    try:
        # ---------- 1. 准备真实仓库 ----------
        if test_root.exists():
            shutil.rmtree(test_root, ignore_errors=True)
        test_root.mkdir(parents=True, exist_ok=True)

        run_git(git_exe, ["init", "--bare", "-b", "main", str(remote_dir)])
        run_git(git_exe, ["init", "-b", "main", str(work_dir)])
        configure_identity(git_exe, work_dir)
        write_file(work_dir / "README.md", "# GitManager Selftest\n")
        run_git(git_exe, ["add", "."], cwd=work_dir)
        run_git(git_exe, ["commit", "-m", "chore: initial commit"], cwd=work_dir)
        run_git(git_exe, ["remote", "add", "origin", str(remote_dir)], cwd=work_dir)
        run_git(git_exe, ["push", "-u", "origin", "main"], cwd=work_dir)
        check("准备带 upstream 的测试仓库", True, str(work_dir))

        # ---------- 2. 启动真实服务 ----------
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            port_in_use = probe.connect_ex((settings.host, port)) == 0
        if port_in_use:
            check("测试端口可用", False, f"{settings.host}:{port} 已被占用，请使用 --port 指定其他端口")
            return 1
        check("测试端口可用", True, f"{settings.host}:{port}")

        env = os.environ.copy()
        env["GITMANAGER_DB"] = str(selftest_db)
        server_log.parent.mkdir(parents=True, exist_ok=True)
        log_handle = open(server_log, "w", encoding="utf-8")
        server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                settings.host,
                "--port",
                str(port),
                "--log-level",
                "info",
            ],
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        if not wait_for_server(base):
            tail = ""
            if server_log.exists():
                tail = "\n".join(server_log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
            check("服务启动", False, tail or "健康检查超时")
            return 1
        check("服务启动（uvicorn 真实监听）", True, base)

        status, health = http_call("GET", base, "/api/health")
        check("健康检查返回 200", status == 200 and bool(health and health.get("ok")), str(health))

        status, settings_data = http_call("GET", base, "/api/settings")
        allowed_roots = (settings_data or {}).get("allowed_roots", [])
        check(
            "服务只暴露回环监听配置",
            status == 200 and (settings_data or {}).get("host") in {"127.0.0.1", "localhost"},
            f"host={((settings_data or {}).get('host'))} roots={allowed_roots}",
        )

        status, _ = http_call("GET", base, "/api/nonexistent")
        check("未知接口返回结构化错误", status == 404)

        request = urllib.request.Request(base + "/")
        try:
            with urllib.request.urlopen(request, timeout=10) as response:
                page = response.read().decode("utf-8", errors="replace")
                page_status = response.status
        except urllib.error.HTTPError as exc:
            page, page_status = exc.read().decode("utf-8", errors="replace"), exc.code
        check(
            "前端入口页面可访问",
            page_status == 200 and "GitManager" in page and "/static/app.js" in page,
            f"HTTP {page_status}",
        )

        # ---------- 3. 仓库管理 ----------
        status, added = http_call("POST", base, "/api/repos", {"paths": [str(work_dir)]})
        results = (added or {}).get("results", [])
        first = results[0] if results else {}
        check(
            "添加白名单内的真实仓库",
            status == 200 and first.get("ok") is True and first.get("repo_id"),
            json.dumps(first, ensure_ascii=False),
        )
        work_id = int(first.get("repo_id") or 0)

        status, repos = http_call("GET", base, "/api/repos")
        items = (repos or {}).get("items", [])
        check(
            "仓库列表已持久化",
            status == 200 and any(item["id"] == work_id for item in items),
            f"count={len(items)}",
        )

        status, dup = http_call("POST", base, "/api/repos", {"paths": [str(work_dir)]})
        dup_result = ((dup or {}).get("results") or [{}])[0]
        check(
            "重复添加给出幂等提示",
            status == 200 and dup_result.get("ok") is True and dup_result.get("duplicate") is True,
            json.dumps(dup_result, ensure_ascii=False),
        )

        status, rejected = http_call("POST", base, "/api/repos", {"paths": ["C:\\Windows"]})
        rejected_result = ((rejected or {}).get("results") or [{}])[0]
        check(
            "白名单外路径被拒绝",
            status == 200 and rejected_result.get("ok") is False and "白名单" in (rejected_result.get("error") or ""),
            (rejected_result.get("error") or ""),
        )

        notrepo_dir.mkdir(parents=True, exist_ok=True)
        status, rejected2 = http_call("POST", base, "/api/repos", {"paths": [str(notrepo_dir)]})
        rejected2_result = ((rejected2 or {}).get("results") or [{}])[0]
        check(
            "非 Git 目录被拒绝",
            status == 200 and rejected2_result.get("ok") is False,
            (rejected2_result.get("error") or ""),
        )

        # ---------- 4. 体检状态 ----------
        status, repo_status = http_call("GET", base, f"/api/repos/{work_id}/status")
        repo_status = repo_status or {}
        check("状态接口可用", status == 200 and bool(repo_status.get("repo")), f"HTTP {status}")
        check("当前分支正确", repo_status.get("branch") == "main", str(repo_status.get("branch")))
        check("upstream 识别正确", repo_status.get("upstream") == "origin/main", str(repo_status.get("upstream")))
        check(
            "ahead/behind 初始为 0/0",
            (repo_status.get("upstream_status") or {}).get("ahead") == 0
            and (repo_status.get("upstream_status") or {}).get("behind") == 0,
            str(repo_status.get("upstream_status")),
        )
        last_commit = repo_status.get("last_commit") or {}
        check(
            "最近提交信息完整",
            last_commit.get("author") == "GitManager Selftest"
            and last_commit.get("subject") == "chore: initial commit"
            and bool(last_commit.get("short")),
            str(last_commit),
        )
        check("初始状态工作区干净", (repo_status.get("dirty") or {}).get("total") == 0, str(repo_status.get("dirty")))

        status, fetched = http_call("POST", base, f"/api/repos/{work_id}/fetch")
        fetch_status = (fetched or {}).get("status") or {}
        check(
            "fetch 接口可用且回写 FETCH_HEAD 时间",
            status == 200 and bool(fetch_status.get("last_fetch_at")),
            str(fetch_status.get("last_fetch_at")),
        )

        status, all_status = http_call("GET", base, "/api/status/all")
        all_items = (all_status or {}).get("items", [])
        check(
            "全局体检返回所有仓库",
            status == 200 and len(all_items) == 1 and all_items[0].get("repo", {}).get("id") == work_id,
            f"count={len(all_items)}",
        )

        # ---------- 5. 提交面板 ----------
        write_file(work_dir / "only.txt", "only\n")
        write_file(work_dir / "keep.txt", "keep\n")
        status, changes = http_call("GET", base, f"/api/repos/{work_id}/changes")
        changes = changes or {}
        check(
            "提交面板列出两个未跟踪文件",
            status == 200 and (changes.get("counts") or {}).get("untracked") == 2,
            str(changes.get("counts")),
        )

        status, committed = http_call(
            "POST",
            base,
            f"/api/repos/{work_id}/commit",
            {"files": ["only.txt"], "message": "feat: add only.txt"},
        )
        check(
            "仅提交勾选文件",
            status == 200 and (committed or {}).get("ok") is True,
            str((committed or {}).get("commit")),
        )
        show = run_git(git_exe, ["show", "--name-only", "--format=%s", "-1"], cwd=work_dir)
        changed_files = [line for line in show.stdout.splitlines()[1:] if line.strip()]
        check(
            "提交内容不包含未勾选文件",
            "only.txt" in changed_files and "keep.txt" not in changed_files,
            f"changed={changed_files}",
        )
        check("未勾选文件仍为未跟踪", (work_dir / "keep.txt").exists())

        # 先把 only.txt 提交推送到远端，保证后续 pull 场景处于可快进状态
        status, first_push = http_call("POST", base, f"/api/repos/{work_id}/push", {})
        check(
            "首次推送提交成功",
            status == 200 and (first_push or {}).get("ok") is True,
            str((first_push or {}).get("message")),
        )

        # ---------- 6. pull：脏工作区保护 + 自动 stash ----------
        run_git(git_exe, ["clone", str(remote_dir), str(clone_dir)], cwd=test_root)
        configure_identity(git_exe, clone_dir)
        write_file(clone_dir / "remote-change.txt", "remote\n")
        run_git(git_exe, ["add", "."], cwd=clone_dir)
        run_git(git_exe, ["commit", "-m", "chore: remote change"], cwd=clone_dir)
        run_git(git_exe, ["push", "origin", "main"], cwd=clone_dir)

        status, pull_fail = http_call("POST", base, f"/api/repos/{work_id}/pull", {"auto_stash": False})
        check(
            "脏工作区 pull 被拒绝并给出原因",
            status == 409 and (pull_fail or {}).get("code") == "dirty_worktree"
            and "未提交" in ((pull_fail or {}).get("message") or ""),
            str((pull_fail or {}).get("code")),
        )
        check("被拒绝后未跟踪文件仍在", (work_dir / "keep.txt").exists())

        status, pull_ok = http_call("POST", base, f"/api/repos/{work_id}/pull", {"auto_stash": True})
        stash_info = (pull_ok or {}).get("stash") or {}
        check(
            "自动 stash 后 pull 成功并自动 pop",
            status == 200 and (pull_ok or {}).get("ok") is True
            and stash_info.get("used") is True and stash_info.get("popped") is True,
            json.dumps(stash_info, ensure_ascii=False),
        )
        stash_list = run_git(git_exe, ["stash", "list"], cwd=work_dir)
        check(
            "stash 已恢复且未残留",
            stash_list.stdout.strip() == "" and (work_dir / "keep.txt").exists(),
            f"stash={'empty' if not stash_list.stdout.strip() else stash_list.stdout.strip()}",
        )
        pulled_status = (pull_ok or {}).get("status") or {}
        check(
            "pull 后 behind 归零且收到远端文件",
            (pulled_status.get("upstream_status") or {}).get("behind") == 0
            and (work_dir / "remote-change.txt").exists(),
            str(pulled_status.get("upstream_status")),
        )

        # ---------- 7. push ----------
        status, commit2 = http_call(
            "POST",
            base,
            f"/api/repos/{work_id}/commit",
            {"files": ["keep.txt"], "message": "feat: keep.txt"},
        )
        check("提交待推送文件", status == 200, str((commit2 or {}).get("commit")))

        status, pushed = http_call("POST", base, f"/api/repos/{work_id}/push", {})
        check(
            "push 当前分支成功",
            status == 200 and (pushed or {}).get("ok") is True and (pushed or {}).get("forced") is False,
            str((pushed or {}).get("message")),
        )
        remote_log = run_git(git_exe, ["--git-dir", str(remote_dir), "log", "-1", "--format=%s", "main"])
        check("远端已收到最新提交", remote_log.stdout.strip() == "feat: keep.txt", remote_log.stdout.strip())

        # ---------- 8. 无 upstream 的推送引导 ----------
        run_git(git_exe, ["init", "--bare", "-b", "main", str(remote2_dir)])
        run_git(git_exe, ["init", "-b", "main", str(noup_dir)])
        configure_identity(git_exe, noup_dir)
        write_file(noup_dir / "noup.txt", "noup\n")
        run_git(git_exe, ["add", "."], cwd=noup_dir)
        run_git(git_exe, ["commit", "-m", "chore: noup initial"], cwd=noup_dir)
        run_git(git_exe, ["remote", "add", "origin", str(remote2_dir)], cwd=noup_dir)

        status, noup_added = http_call("POST", base, "/api/repos", {"paths": [str(noup_dir)]})
        noup_id = int((((noup_added or {}).get("results") or [{}])[0]).get("repo_id") or 0)
        check("添加无 upstream 测试仓库", status == 200 and noup_id > 0, f"repo_id={noup_id}")

        status, needs = http_call("POST", base, f"/api/repos/{noup_id}/push", {})
        check(
            "无 upstream 推送返回引导信息",
            status == 409 and (needs or {}).get("code") == "needs_upstream"
            and "origin" in ((needs or {}).get("remotes") or []),
            json.dumps(needs or {}, ensure_ascii=False),
        )

        status, set_up = http_call(
            "POST",
            base,
            f"/api/repos/{noup_id}/push",
            {"set_upstream": True, "remote": "origin"},
        )
        check(
            "显式设置 upstream 后推送成功",
            status == 200 and (set_up or {}).get("ok") is True,
            str((set_up or {}).get("message")),
        )
        remote2_log = run_git(git_exe, ["--git-dir", str(remote2_dir), "log", "-1", "--format=%s", "main"])
        check("第二个远端收到提交", remote2_log.stdout.strip() == "chore: noup initial", remote2_log.stdout.strip())

        # ---------- 9. 强制推送防护 ----------
        status, force_blocked = http_call(
            "POST",
            base,
            f"/api/repos/{work_id}/push",
            {"force": True, "confirm_force": False},
        )
        check(
            "未二次确认的强制推送被拒绝",
            status == 400 and (force_blocked or {}).get("code") == "force_not_confirmed",
            str((force_blocked or {}).get("code")),
        )
        status, blocked_audit = http_call("GET", base, "/api/audit?action=push_force_blocked&limit=10")
        blocked_items = (blocked_audit or {}).get("items", [])
        check(
            "强制推送请求留下审计记录",
            status == 200 and blocked_items and blocked_items[0].get("forced") == 1
            and blocked_items[0].get("ok") == 0,
            f"count={len(blocked_items)}",
        )

        # ---------- 10. 非快进 pull ----------
        write_file(work_dir / "local.txt", "local\n")
        status, local_commit = http_call(
            "POST",
            base,
            f"/api/repos/{work_id}/commit",
            {"files": ["local.txt"], "message": "feat: local only"},
        )
        check("创建本地领先提交", status == 200, str((local_commit or {}).get("commit")))

        run_git(git_exe, ["pull", "--ff-only"], cwd=clone_dir)
        write_file(clone_dir / "clone2.txt", "clone\n")
        run_git(git_exe, ["add", "."], cwd=clone_dir)
        run_git(git_exe, ["commit", "-m", "chore: clone diverged"], cwd=clone_dir)
        run_git(git_exe, ["push", "origin", "main"], cwd=clone_dir)

        status, fetched_before_pull = http_call("POST", base, f"/api/repos/{work_id}/fetch")
        status, diverged_status = http_call("GET", base, f"/api/repos/{work_id}/status")
        up = (diverged_status or {}).get("upstream_status") or {}
        check("本地与远端分叉被识别", up.get("ahead") == 1 and up.get("behind") == 1, str(up))

        status, pull_diverged = http_call("POST", base, f"/api/repos/{work_id}/pull", {"auto_stash": False})
        check(
            "无法快进的 pull 返回可读错误",
            status == 409 and (pull_diverged or {}).get("code") == "non_fast_forward"
            and "快进" in ((pull_diverged or {}).get("message") or ""),
            f"{((pull_diverged or {}).get('code'))} / {((pull_diverged or {}).get('message'))}",
        )

        # 仅清理自测仓库的分叉状态（直接使用测试仓库，不经过应用）
        run_git(git_exe, ["fetch", "origin"], cwd=work_dir)
        run_git(git_exe, ["reset", "--hard", "origin/main"], cwd=work_dir)
        run_git(git_exe, ["clean", "-fd"], cwd=work_dir)
        status, clean_status = http_call("GET", base, f"/api/repos/{work_id}/status")
        check(
            "自测仓库恢复到同步状态",
            status == 200 and (clean_status or {}).get("dirty", {}).get("total") == 0
            and ((clean_status or {}).get("upstream_status") or {}).get("ahead") == 0,
            str((clean_status or {}).get("upstream_status")),
        )

        # ---------- 11. 审计 ----------
        status, audit = http_call("GET", base, "/api/audit?limit=5")
        audit_items = (audit or {}).get("items", [])
        check(
            "审计日志可查询",
            status == 200 and (audit or {}).get("total", 0) > 0 and bool(audit_items),
            f"total={(audit or {}).get('total')}",
        )
        check(
            "审计记录包含完整命令与退出码",
            bool(audit_items) and audit_items[0].get("command", "").startswith("git ")
            and isinstance(audit_items[0].get("exit_code"), int),
            str(audit_items[0].get("command")) if audit_items else "",
        )

        status, failed_audit = http_call("GET", base, "/api/audit?only_failed=true&limit=5")
        check(
            "失败审计可筛选",
            status == 200 and (failed_audit or {}).get("total", 0) > 0,
            f"failed={(failed_audit or {}).get('total')}",
        )

        status, pull_audit = http_call("GET", base, "/api/audit?action=pull&limit=5")
        check(
            "pull 动作已入审计",
            status == 200 and (pull_audit or {}).get("total", 0) > 0,
            f"pull={(pull_audit or {}).get('total')}",
        )
        status, stash_audit = http_call("GET", base, "/api/audit?action=stash_push&limit=5")
        check(
            "自动 stash 已入审计",
            status == 200 and (stash_audit or {}).get("total", 0) > 0,
            f"stash_push={(stash_audit or {}).get('total')}",
        )

        # ---------- 12. 移除仓库配置 ----------
        status, removed = http_call("DELETE", base, f"/api/repos/{work_id}")
        check(
            "移除仓库配置成功（不删除磁盘目录）",
            status == 200 and (removed or {}).get("ok") is True and work_dir.exists(),
            str((removed or {}).get("message")),
        )
        status, repos_after = http_call("GET", base, "/api/repos")
        check(
            "移除后列表不再包含该仓库",
            status == 200 and all(item["id"] != work_id for item in (repos_after or {}).get("items", [])),
            f"count={len((repos_after or {}).get('items', []))}",
        )
        status, history = http_call("GET", base, f"/api/audit?repo_id={work_id}&limit=5")
        check(
            "移除仓库后审计历史仍保留",
            status == 200 and (history or {}).get("total", 0) > 0,
            f"history={(history or {}).get('total')}",
        )

        return 0 if all(ok for _, ok, _ in RESULTS) else 1
    finally:
        cleanup()
        passed = sum(1 for _, ok, _ in RESULTS if ok)
        total = len(RESULTS)
        print("\n" + "=" * 62)
        print(f"自测汇总：通过 {passed} / {total}")
        if passed != total:
            print("失败项：")
            for name, ok, detail in RESULTS:
                if not ok:
                    print(f"  - {name}：{detail}")
        print("=" * 62)


if __name__ == "__main__":
    sys.exit(main())
