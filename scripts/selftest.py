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
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from app.config import load_settings  # noqa: E402
from app.domain import (  # noqa: E402
    classify_remote_error,
    detect_remote_protocol,
    https_to_ssh,
    parse_cmdkey_output,
)
from app.git_runner import build_ssh_command  # noqa: E402

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


def http_probe(
    method: str,
    base: str,
    path: str,
    payload=None,
    headers: dict[str, str] | None = None,
    timeout: int = 15,
):
    """调用接口并返回状态码、响应头与解析后的 JSON（用于鉴权测试）。

    参数：
        method: HTTP 方法。
        base: 服务根地址。
        path: 接口路径。
        payload: 可选 JSON 请求体。
        headers: 可选附加请求头（如 Cookie）。
        timeout: 超时秒数。
    返回值：
        {status, headers, json, body}，不会抛出 HTTP 错误。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(
        base + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8", **(headers or {})},
    )

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        """禁止自动跟随重定向，便于断言登录页跳转。"""

        def redirect_request(self, req, fp, code, msg, hdrs, newurl):  # noqa: D102
            return None

    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            status = response.status
            body = response.read().decode("utf-8", errors="replace")
            resp_headers = dict(response.headers.items())
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read().decode("utf-8", errors="replace")
        resp_headers = dict(exc.headers.items()) if exc.headers else {}
    resp_headers = {str(key).lower(): value for key, value in resp_headers.items()}
    parsed = None
    if body:
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = {"raw": body[:500]}
    return {"status": status, "headers": resp_headers, "json": parsed, "body": body}


def raw_http_request(port: int, path: str, host_header: str, timeout: float = 5.0) -> str:
    """用原始 socket 发起一个最小 HTTP 请求以自定义 Host 头。

    参数：
        port: 本机服务端口。
        path: 请求路径。
        host_header: 希望发送的 Host 头值。
        timeout: 读超时秒数。
    返回值：
        响应首行文本（如 “HTTP/1.1 400 Bad Request”）。
    """
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        payload = (
            f"GET {path} HTTP/1.1\r\nHost: {host_header}\r\n"
            "Connection: close\r\n\r\n"
        )
        sock.sendall(payload.encode("utf-8"))
        chunk = sock.recv(1024).decode("utf-8", errors="replace")
    return chunk.splitlines()[0] if chunk else ""


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


def run_tool(executable: str, args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    """执行非 git 的本地工具（例如 ssh-keygen），仅用于自测准备。

    参数：
        executable: 可执行文件路径。
        args: 参数数组。
        timeout: 超时秒数。
    返回值：
        subprocess.CompletedProcess（调用方自行判断 returncode）。
    """
    return subprocess.run(
        [executable, *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        shell=False,
    )


def force_rmtree(path: Path) -> None:
    """尽力删除目录：遇到只读文件（git 松散对象）时清除属性后重试。

    参数：
        path: 要删除的目录。
    返回值：
        无。
    """

    def handle_error(func, target, _exc):
        """删除失败时清除只读属性并重试一次。

        参数：
            func: shutil 内部使用的删除函数。
            target: 出错路径。
            _exc: 异常对象（未使用）。
        返回值：
            无。
        """
        try:
            os.chmod(target, stat.S_IWRITE)
            func(target)
        except OSError:
            pass

    if not path.exists():
        return
    shutil.rmtree(path, onexc=handle_error)


def wait_scan(base: str, job_id: str, timeout: float = 30.0) -> dict:
    """轮询扫描任务直到结束。

    参数：
        base: 服务根地址。
        job_id: 扫描任务 id。
        timeout: 最长等待秒数。
    返回值：
        任务快照；超时返回最后一次拿到的快照（可能为空字典）。
    """
    deadline = time.time() + timeout
    snapshot: dict = {}
    while time.time() < deadline:
        status, data = http_call("GET", base, f"/api/scan/{job_id}", timeout=10)
        if status == 200 and data:
            snapshot = data
            if data.get("status") in {"done", "error", "cancelled"}:
                return snapshot
        time.sleep(0.15)
    return snapshot


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


def wait_for_auth_server(base: str, timeout: float = 30.0) -> bool:
    """等待启用了令牌鉴权的服务就绪（401 也视为已就绪）。

    参数：
        base: 服务根地址。
        timeout: 最长等待秒数。
    返回值：
        服务可响应（200 或 401）时返回 True。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            probe = http_probe("GET", base, "/api/health", timeout=2)
            if probe["status"] in (200, 401):
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
    scan_root = test_root / "scanroot"
    ssh_repo_dir = test_root / "ssh-repo"
    mixed_dir = test_root / "mixed"
    ssh_dir = test_root / "ssh"
    extra_root: Path | None = None
    selftest_db = PROJECT_ROOT / "data" / "selftest.db"
    lan_db = PROJECT_ROOT / "data" / "selftest_lan.db"
    server_log = PROJECT_ROOT / "data" / "selftest_server.log"
    lan_log = PROJECT_ROOT / "data" / "selftest_lan.log"
    lan_port = port + 2
    lan_token = "selftest-lan-token-9f2c"
    server: subprocess.Popen | None = None
    lan_server: subprocess.Popen | None = None
    log_handle = None
    lan_log_handle = None

    def cleanup() -> None:
        """停止服务并清理临时文件。

        参数：
            无。
        返回值：
            无。
        """
        nonlocal server, log_handle, lan_server, lan_log_handle
        for proc in (server, lan_server):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        for handle in (log_handle, lan_log_handle):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        log_handle = None
        lan_log_handle = None
        if extra_root is not None and extra_root.exists():
            force_rmtree(extra_root)
        if keep:
            print(f"已保留临时数据：{test_root}；数据库：{selftest_db}")
            return
        # 目录联接需先单独移除，避免 rmtree 在 Windows 上留下残留
        for link_dir in (test_root / "scanroot" / "loop", test_root / "work-link"):
            if link_dir.exists():
                try:
                    os.rmdir(link_dir)
                except OSError:
                    pass
        # Windows 上服务退出后可能存在短暂文件句柄残留，多次重试删除
        time.sleep(0.5)
        force_rmtree(test_root)
        for _attempt in range(10):
            if not test_root.exists():
                break
            time.sleep(1.0)
            force_rmtree(test_root)
        try:
            if test_base.exists() and not any(test_base.iterdir()):
                test_base.rmdir()
        except OSError:
            pass
        if test_root.exists():
            print(f"警告：临时目录未能完全删除，请手动清理：{test_root}")
        for suffix in ("", "-wal", "-shm"):
            for db_path in (selftest_db, lan_db):
                target = Path(str(db_path) + suffix)
                if target.exists():
                    try:
                        target.unlink()
                    except OSError:
                        pass
        for log_path in (server_log, lan_log):
            if log_path.exists():
                try:
                    log_path.unlink()
                except OSError:
                    pass

    def make_repo(path: Path) -> None:
        """创建一个带初始提交的真实 Git 仓库（扫描与绑定测试使用）。

        参数：
            path: 仓库目录。
        返回值：
            无。
        """
        path.mkdir(parents=True, exist_ok=True)
        run_git(git_exe, ["init", "-b", "main", str(path)])
        configure_identity(git_exe, path)
        write_file(path / "README.md", f"# {path.name}\n")
        run_git(git_exe, ["add", "."], cwd=path)
        run_git(git_exe, ["commit", "-m", "chore: scan fixture"], cwd=path)

    try:
        # ---------- 1. 准备真实仓库 ----------
        if test_root.exists():
            force_rmtree(test_root)
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

        # ---------- 1.1 准备 SSH 测试目录（使用 ssh-keygen 生成临时密钥） ----------
        ssh_dir.mkdir(parents=True, exist_ok=True)
        key_a = ssh_dir / "id_ed25519"
        key_b = ssh_dir / "custom_work_key"
        for key_path, comment in ((key_a, "selftest@gitmanager"), (key_b, "work@selftest")):
            run_tool(
                settings.ssh_keygen_executable,
                ["-q", "-t", "ed25519", "-N", "", "-C", comment, "-f", str(key_path)],
            )
        write_file(
            ssh_dir / "config",
            "\n".join(
                [
                    "Host selftest-a",
                    "    HostName 127.0.0.1",
                    "    User git",
                    f'    IdentityFile "{str(key_a).replace(os.sep, "/")}"',
                    "",
                    "Host selftest-b",
                    "    HostName 127.0.0.1",
                    "    User git",
                    f'    IdentityFile "{str(key_b).replace(os.sep, "/")}"',
                    "",
                ]
            ),
        )
        check(
            "准备 SSH 临时密钥与 config（仅用于身份扫描）",
            key_a.is_file() and key_a.with_name(key_a.name + ".pub").is_file()
            and key_b.is_file() and (ssh_dir / "config").is_file(),
            str(ssh_dir),
        )

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
        # 指向自测专用 SSH 目录，避免读取开发者真实 ~/.ssh
        env["SSH_DIR"] = str(ssh_dir)
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
        check(
            "默认回环监听免令牌访问",
            bool((settings_data or {}).get("auth_required")) is False
            and bool((settings_data or {}).get("lan_enabled")) is False,
            f"auth_required={(settings_data or {}).get('auth_required')} "
            f"lan_enabled={(settings_data or {}).get('lan_enabled')}",
        )

        # ---------- 2.1 局域网模式：令牌鉴权与 Host 白名单 ----------
        lan_base = f"http://127.0.0.1:{lan_port}"
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.5)
            lan_port_in_use = probe.connect_ex(("127.0.0.1", lan_port)) == 0
        if lan_port_in_use:
            check("局域网测试端口可用", False, f"127.0.0.1:{lan_port} 已被占用")
            return 1
        check("局域网测试端口可用", True, f"127.0.0.1:{lan_port}")

        lan_env = os.environ.copy()
        lan_env["GITMANAGER_HOST"] = "0.0.0.0"
        lan_env["GITMANAGER_PORT"] = str(lan_port)
        lan_env["GITMANAGER_ACCESS_TOKEN"] = lan_token
        lan_env["GITMANAGER_ALLOWED_HOSTS"] = "127.0.0.1;localhost"
        lan_env["GITMANAGER_DB"] = str(lan_db)
        lan_env["SSH_DIR"] = str(ssh_dir)
        lan_log_handle = open(lan_log, "w", encoding="utf-8")
        lan_server = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "app.main:app",
                "--host",
                "0.0.0.0",
                "--port",
                str(lan_port),
                "--log-level",
                "info",
            ],
            cwd=str(PROJECT_ROOT),
            env=lan_env,
            stdout=lan_log_handle,
            stderr=subprocess.STDOUT,
        )
        if not wait_for_auth_server(lan_base):
            tail = ""
            if lan_log.exists():
                tail = "\n".join(lan_log.read_text(encoding="utf-8", errors="replace").splitlines()[-20:])
            check("局域网模式服务启动", False, tail or "鉴权服务健康检查超时")
            return 1
        check("局域网模式服务启动（监听 0.0.0.0）", True, lan_base)

        unauth = http_probe("GET", lan_base, "/api/health")
        check(
            "未登录访问业务接口返回 401",
            unauth["status"] == 401 and (unauth["json"] or {}).get("code") == "unauthorized",
            f"HTTP {unauth['status']}",
        )
        redirect = http_probe("GET", lan_base, "/")
        location = (redirect["headers"] or {}).get("location", "")
        check(
            "未登录访问首页跳转到登录页",
            redirect["status"] in (302, 303, 307) and location.endswith("/login"),
            f"HTTP {redirect['status']} location={location}",
        )
        login_page = http_probe("GET", lan_base, "/login")
        check(
            "登录页可匿名访问且提示令牌来源",
            login_page["status"] == 200 and "GITMANAGER_ACCESS_TOKEN" in login_page["body"],
            f"HTTP {login_page['status']}",
        )
        bad_login = http_probe("POST", lan_base, "/api/auth/login", {"token": "wrong-token"})
        check(
            "错误令牌登录返回 401",
            bad_login["status"] == 401,
            f"HTTP {bad_login['status']} {(bad_login['json'] or {}).get('message')}",
        )
        good_login = http_probe("POST", lan_base, "/api/auth/login", {"token": lan_token})
        set_cookie = (good_login["headers"] or {}).get("set-cookie", "")
        lower_cookie = set_cookie.lower()
        check(
            "正确令牌登录成功并写入 HttpOnly SameSite=Lax Cookie",
            good_login["status"] == 200
            and "httponly" in lower_cookie
            and "samesite=lax" in lower_cookie,
            set_cookie.replace("\n", " "),
        )
        cookie_pair = set_cookie.split(";", 1)[0].strip()
        authed = http_probe("GET", lan_base, "/api/repos", headers={"Cookie": cookie_pair})
        check(
            "携带鉴权 Cookie 后可访问业务接口",
            authed["status"] == 200 and "items" in (authed["json"] or {}),
            f"HTTP {authed['status']}",
        )
        lan_settings = http_probe(
            "GET", lan_base, "/api/settings", headers={"Cookie": cookie_pair}
        )
        lan_settings_data = lan_settings["json"] or {}
        check(
            "局域网模式服务设置标明鉴权已启用且不泄露令牌",
            lan_settings["status"] == 200
            and lan_settings_data.get("auth_required") is True
            and lan_settings_data.get("token_configured") is True
            and lan_token not in lan_settings["body"],
            str({key: lan_settings_data.get(key) for key in (
                "host", "lan_enabled", "auth_required", "token_configured", "allowed_hosts"
            )}),
        )
        evil_line = raw_http_request(lan_port, "/api/health", "evil.example.com")
        check(
            "Host 不在白名单时返回 400",
            "400" in evil_line,
            evil_line,
        )

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
        check(
            "重复添加单条路径返回 409 already_exists",
            status == 409
            and (dup or {}).get("code") == "already_exists"
            and int((dup or {}).get("repo_id") or 0) == work_id
            and bool((dup or {}).get("name")),
            json.dumps(dup, ensure_ascii=False),
        )

        # 同一仓库的多种书写方式（大小写、尾斜杠、含 .. 的相对段）都应命中同一条记录
        variants = [
            ("大小写差异", str(work_dir).upper()),
            ("尾部分隔符", str(work_dir) + os.sep),
            ("含 .. 的路径段", str(work_dir / ".." / work_dir.name)),
        ]
        variant_status: list[tuple[str, int, str]] = []
        for label, variant_path in variants:
            status, dup_variant = http_call(
                "POST", base, "/api/repos", {"paths": [variant_path]}
            )
            variant_status.append(
                (label, status, str((dup_variant or {}).get("code") or ""))
            )
        check(
            "大小写/尾斜杠/.. 写法均判定为同一仓库并返回 409",
            all(
                status == 409 and code == "already_exists"
                for _, status, code in variant_status
            ),
            json.dumps(variant_status, ensure_ascii=False),
        )
        status, repos_after_variants = http_call("GET", base, "/api/repos")
        work_key = os.path.normcase(os.path.realpath(str(work_dir)))
        work_records = [
            item
            for item in (repos_after_variants or {}).get("items", [])
            if os.path.normcase(os.path.realpath(item.get("path", ""))) == work_key
        ]
        check(
            "不同写法重复添加后仅保留一条记录",
            status == 200 and len(work_records) == 1,
            f"matched={len(work_records)}",
        )

        # 目录联接（junction）指向同一仓库时也应判定为同一条
        work_link = test_root / "work-link"
        link_result = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(work_link), str(work_dir)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        if link_result.returncode == 0 and work_link.exists():
            status, link_dup = http_call(
                "POST", base, "/api/repos", {"paths": [str(work_link)]}
            )
            check(
                "目录联接指向同一仓库时判定为同一条（409）",
                status == 409
                and (link_dup or {}).get("code") == "already_exists"
                and int((link_dup or {}).get("repo_id") or 0) == work_id,
                json.dumps(link_dup, ensure_ascii=False),
            )
        else:
            check(
                "目录联接指向同一仓库时判定为同一条（409）",
                True,
                f"环境不支持目录联接，已跳过：{(link_result.stderr or '').strip()[:80]}",
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

        # ---------- 12.5 提交缺陷回归：删除/修改/新增/中文文件名 ----------
        make_repo(mixed_dir)
        write_file(mixed_dir / "删除文件.txt", "待删除内容\n")
        write_file(mixed_dir / "修改文件.txt", "版本 1\n")
        write_file(mixed_dir / "中文文件名.txt", "初始内容\n")
        run_git(git_exe, ["add", "."], cwd=mixed_dir)
        run_git(git_exe, ["commit", "-m", "chore: mixed fixture"], cwd=mixed_dir)
        (mixed_dir / "删除文件.txt").unlink()
        write_file(mixed_dir / "修改文件.txt", "版本 2\n")
        write_file(mixed_dir / "新增文件.txt", "未跟踪内容\n")

        status, mixed_added = http_call("POST", base, "/api/repos", {"paths": [str(mixed_dir)]})
        mixed_id = int(((((mixed_added or {}).get("results") or [{}])[0]).get("repo_id")) or 0)
        check("添加混合变更测试仓库", status == 200 and mixed_id > 0, f"repo_id={mixed_id}")

        status, mixed_changes = http_call("GET", base, f"/api/repos/{mixed_id}/changes")
        mixed_files = (mixed_changes or {}).get("files") or []
        mixed_counts = (mixed_changes or {}).get("counts") or {}
        deleted_paths = [item["path"] for item in mixed_files if item.get("group") == "deleted"]
        check(
            "状态模型包含已删除类别",
            status == 200 and mixed_counts.get("deleted") == 1 and "删除文件.txt" in deleted_paths,
            str(mixed_counts),
        )
        check(
            "中文路径原样输出（无八进制转义）",
            all("\\344" not in item.get("path", "") for item in mixed_files)
            and "修改文件.txt" in [item["path"] for item in mixed_files],
            str([item.get("path") for item in mixed_files]),
        )
        check(
            "四类变更同时可见（已修改/已删除/未跟踪）",
            mixed_counts.get("modified") == 1
            and mixed_counts.get("deleted") == 1
            and mixed_counts.get("untracked") == 1,
            str(mixed_counts),
        )

        status, del_commit = http_call(
            "POST",
            base,
            f"/api/repos/{mixed_id}/commit",
            {"files": ["删除文件.txt"], "message": "fix: 删除废弃文档"},
        )
        check("仅勾选已删除文件可提交成功", status == 200 and (del_commit or {}).get("ok") is True, str((del_commit or {}).get("commit")))
        mixed_status = run_git(
            git_exe, ["-c", "core.quotepath=false", "status", "--porcelain"], cwd=mixed_dir
        ).stdout
        check(
            "删除提交后不再出现已删除条目",
            "删除文件.txt" not in mixed_status and "修改文件.txt" in mixed_status
            and "新增文件.txt" in mixed_status,
            mixed_status.replace("\n", " | "),
        )

        status, mod_commit = http_call(
            "POST",
            base,
            f"/api/repos/{mixed_id}/commit",
            {"files": ["修改文件.txt"], "message": "fix: 更新修改文件"},
        )
        check("仅勾选修改文件可提交成功", status == 200 and (mod_commit or {}).get("ok") is True, str((mod_commit or {}).get("commit")))
        mixed_status2 = run_git(
            git_exe, ["-c", "core.quotepath=false", "status", "--porcelain"], cwd=mixed_dir
        ).stdout
        check(
            "仅提交修改项不影响未跟踪文件",
            mixed_status2.strip() == "?? 新增文件.txt",
            mixed_status2.replace("\n", " | "),
        )

        status, cn_commit = http_call(
            "POST",
            base,
            f"/api/repos/{mixed_id}/commit",
            {"files": ["新增文件.txt"], "message": "feat: 新增中文名文件"},
        )
        check("中文文件名可正常提交", status == 200 and (cn_commit or {}).get("ok") is True, str((cn_commit or {}).get("commit")))
        mixed_status3 = run_git(
            git_exe, ["-c", "core.quotepath=false", "status", "--porcelain"], cwd=mixed_dir
        ).stdout
        check("混合场景全部提交后工作区干净", mixed_status3.strip() == "", mixed_status3)

        status, empty_commit = http_call(
            "POST",
            base,
            f"/api/repos/{mixed_id}/commit",
            {"files": [], "message": "chore: empty"},
        )
        check("空勾选提交被后端拒绝", status == 400, str((empty_commit or {}).get("code")))

        status, stage_audit = http_call(
            "GET", base, f"/api/audit?repo_id={mixed_id}&action=stage&page=1&page_size=10"
        )
        stage_rows = (stage_audit or {}).get("items") or []
        check(
            "暂存命令使用 quotepath=false 与 add -A",
            bool(stage_rows)
            and all("core.quotepath=false" in row.get("command", "") and "add -A" in row.get("command", "") for row in stage_rows),
            str([row.get("command") for row in stage_rows][:1]),
        )

        # ---------- 12.6 审计服务端分页 ----------
        status, page_one = http_call("GET", base, "/api/audit?page=1&page_size=10")
        page_one = page_one or {}
        check(
            "审计第 1 页返回页码元信息",
            status == 200 and page_one.get("page") == 1 and page_one.get("page_size") == 10
            and page_one.get("from") == 1 and page_one.get("to") == min(10, page_one.get("total", 0)),
            f"page={page_one.get('page')} range={page_one.get('from')}-{page_one.get('to')}",
        )
        check(
            "审计每页固定 10 条且 total_pages 正确",
            len(page_one.get("items") or []) == min(10, page_one.get("total", 0))
            and page_one.get("total_pages") == (page_one.get("total", 0) + 9) // 10,
            f"items={len(page_one.get('items') or [])} total_pages={page_one.get('total_pages')}",
        )
        status, page_two = http_call("GET", base, "/api/audit?page=2&page_size=10")
        page_two = page_two or {}
        page_two_ids = [row.get("id") for row in page_two.get("items") or []]
        page_one_ids = [row.get("id") for row in page_one.get("items") or []]
        check(
            "审计第 2 页数据不与第 1 页重复",
            status == 200 and page_two.get("page") == 2
            and not set(page_two_ids) & set(page_one_ids),
            f"page2_items={len(page_two_ids)}",
        )
        status, page_last = http_call("GET", base, f"/api/audit?page={page_one.get('total_pages')}&page_size=10")
        page_last = page_last or {}
        check(
            "审计末页页码与区间正确",
            status == 200 and page_last.get("page") == page_one.get("total_pages")
            and page_last.get("to") == page_last.get("total"),
            f"page={page_last.get('page')} to={page_last.get('to')} total={page_last.get('total')}",
        )
        status, page_over = http_call("GET", base, f"/api/audit?page={(page_one.get('total_pages') or 1) + 5}&page_size=10")
        page_over = page_over or {}
        check(
            "审计页码越界自动收敛到末页",
            status == 200 and page_over.get("page") == page_one.get("total_pages"),
            f"page={page_over.get('page')}",
        )
        status, page_zero = http_call("GET", base, "/api/audit?page=1&page_size=10&action=__no_such_action__")
        page_zero = page_zero or {}
        check(
            "审计无结果时 total_pages=0 且返回空列表",
            status == 200 and page_zero.get("total_pages") == 0
            and page_zero.get("page") == 1 and page_zero.get("items") == []
            and page_zero.get("from") == 0 and page_zero.get("to") == 0,
            f"total={page_zero.get('total')} pages={page_zero.get('total_pages')}",
        )
        status, filtered_page = http_call(
            "GET", base, f"/api/audit?page=1&page_size=10&action=commit&repo_id={mixed_id}&only_failed=false"
        )
        filtered_page = filtered_page or {}
        check(
            "审计筛选条件在分页查询中保持",
            status == 200 and filtered_page.get("page") == 1
            and all(row.get("action") == "commit" and row.get("repo_id") == mixed_id
                    for row in filtered_page.get("items") or []),
            f"items={len(filtered_page.get('items') or [])}",
        )
        status, mixed_remotes = http_call("GET", base, f"/api/repos/{mixed_id}/status")
        remotes = (mixed_remotes or {}).get("remotes") or []
        check(
            "状态接口返回远端完整地址字段",
            status == 200 and isinstance(remotes, list)
            and all("fetch_url" in item for item in remotes),
            str(remotes),
        )

        # ---------- 12.7 忽略归因、.gitignore 追加、静音与取消跟踪 ----------
        ignore_dir = test_root / "ignore-repo"
        make_repo(ignore_dir)
        write_file(ignore_dir / "tracked.tmp", "tracked\n")
        # 先显式跟踪，再写入 *.tmp 规则：否则 git add . 会因忽略规则跳过该文件
        run_git(git_exe, ["add", "tracked.tmp"], cwd=ignore_dir)
        write_file(ignore_dir / ".gitignore", "*.tmp\n")
        run_git(git_exe, ["add", "."], cwd=ignore_dir)
        run_git(git_exe, ["commit", "-m", "chore: tracked tmp"], cwd=ignore_dir)
        # 修改已跟踪文件，使其出现在看板并命中 *.tmp 规则（tracked_ignored）
        write_file(ignore_dir / "tracked.tmp", "tracked changed\n")
        write_file(ignore_dir / "notes.txt", "notes\n")
        write_file(ignore_dir / "dist" / "bundle.js", "console.log(1)\n")
        status, added_ignore = http_call(
            "POST", base, "/api/repos", {"paths": [str(ignore_dir)]}
        )
        ignore_id = ((added_ignore or {}).get("results") or [{}])[0].get("repo_id")
        check(
            "注册忽略归因测试仓库",
            status == 200 and isinstance(ignore_id, int),
            str(ignore_id),
        )
        status, report = http_call("GET", base, f"/api/repos/{ignore_id}/ignore")
        report = report or {}
        report_files = {item["path"]: item for item in report.get("files") or []}
        tracked_tmp = report_files.get("tracked.tmp") or {}
        notes = report_files.get("notes.txt") or {}
        counts = report.get("counts") or {}
        check(
            "已跟踪且命中忽略规则的文件给出正确归因与规则来源",
            status == 200 and tracked_tmp.get("tracked") is True
            and tracked_tmp.get("ignored") is True
            and tracked_tmp.get("diagnosis") == "tracked_ignored"
            and ".gitignore:1:" in (tracked_tmp.get("ignore_source") or ""),
            f"{tracked_tmp.get('diagnosis')} source={tracked_tmp.get('ignore_source')}",
        )
        check(
            "未被规则匹配的未跟踪文件归因为可忽略",
            notes.get("diagnosis") == "untracked_ignorable"
            and notes.get("tracked") is False and notes.get("ignored") is False,
            f"{notes.get('diagnosis')}",
        )
        check(
            "忽略报告的三类计数分别统计",
            counts.get("tracked_ignored", 0) >= 1 and counts.get("ignorable", 0) >= 1
            and counts.get("pending") == counts.get("total"),
            f"counts={counts}",
        )

        status, preview = http_call(
            "POST", base, f"/api/repos/{ignore_id}/ignore/preview",
            {"paths": ["notes.txt", "dist"]},
        )
        preview = preview or {}
        rules = [item.get("rule") for item in preview.get("rules") or []]
        check(
            "规则建议：文件带前导斜杠、目录以斜杠结尾",
            status == 200 and "/notes.txt" in rules and "dist/" in rules,
            str(rules),
        )
        check(
            "预览给出目标 .gitignore 与追加内容",
            preview.get("target", "").endswith(".gitignore")
            and preview.get("exists") is True
            and "/notes.txt" in (preview.get("append") or ""),
            f"target={preview.get('target')}",
        )
        original_gitignore = (ignore_dir / ".gitignore").read_text(encoding="utf-8")
        status, appended = http_call(
            "POST", base, f"/api/repos/{ignore_id}/ignore",
            {"paths": ["notes.txt", "dist"]},
        )
        appended = appended or {}
        content_after = (ignore_dir / ".gitignore").read_text(encoding="utf-8")
        check(
            "追加 .gitignore 保留原内容并写入新规则",
            status == 200 and content_after.startswith(original_gitignore)
            and "/notes.txt" in content_after and "dist/" in content_after,
            f"appended={appended.get('appended')}",
        )
        status, status_after = http_call("GET", base, f"/api/repos/{ignore_id}/status")
        untracked_paths = [
            item.get("path") for item in ((status_after or {}).get("files") or {}).get("untracked") or []
        ]
        check(
            "追加后未跟踪项立刻从看板列表消失",
            status == 200 and "notes.txt" not in untracked_paths
            and not any(path.startswith("dist/") for path in untracked_paths),
            str(untracked_paths),
        )
        content_before_duplicate = (ignore_dir / ".gitignore").read_text(encoding="utf-8")
        status, duplicate = http_call(
            "POST", base, f"/api/repos/{ignore_id}/ignore",
            {"paths": ["notes.txt", "dist"]},
        )
        duplicate = duplicate or {}
        content_after_duplicate = (ignore_dir / ".gitignore").read_text(encoding="utf-8")
        check(
            "等价规则不重复追加且文件内容不变",
            status == 200 and duplicate.get("appended") == []
            and len(duplicate.get("skipped") or []) == 2
            and content_after_duplicate == content_before_duplicate,
            f"appended={duplicate.get('appended')} skipped={len(duplicate.get('skipped') or [])}",
        )

        newline_dir = test_root / "newline-repo"
        make_repo(newline_dir)
        write_file(newline_dir / ".gitignore", "*.log")
        write_file(newline_dir / "latest.txt", "latest\n")
        status, added_newline = http_call(
            "POST", base, "/api/repos", {"paths": [str(newline_dir)]}
        )
        newline_id = ((added_newline or {}).get("results") or [{}])[0].get("repo_id")
        status, newline_result = http_call(
            "POST", base, f"/api/repos/{newline_id}/ignore",
            {"paths": ["latest.txt"]},
        )
        newline_content = (newline_dir / ".gitignore").read_text(encoding="utf-8")
        check(
            "原 .gitignore 无末尾换行时自动补一个再追加",
            status == 200 and newline_content == "*.log\n/latest.txt\n",
            repr(newline_content),
        )

        mute_dir = test_root / "mute-repo"
        make_repo(mute_dir)
        write_file(mute_dir / "only.tmp", "only\n")
        status, added_mute = http_call(
            "POST", base, "/api/repos", {"paths": [str(mute_dir)]}
        )
        mute_id = ((added_mute or {}).get("results") or [{}])[0].get("repo_id")
        status, muted_result = http_call(
            "POST", base, f"/api/repos/{mute_id}/mutes", {"patterns": ["only.tmp"]}
        )
        muted_result = muted_result or {}
        muted_status = muted_result.get("status") or {}
        muted_dirty = muted_status.get("dirty") or {}
        muted_files = (muted_status.get("files") or {}).get("untracked") or []
        check(
            "静音后计数分离且文件标记 muted=true",
            status == 200 and muted_dirty.get("total", 0) > 0
            and muted_dirty.get("pending") == 0 and muted_dirty.get("muted") == 1
            and any(item.get("path") == "only.tmp" and item.get("muted") is True for item in muted_files),
            f"dirty={muted_dirty}",
        )
        check(
            "静音不会把仓库误判为干净（total 与静音数保留）",
            muted_dirty.get("total", 0) >= 1 and muted_dirty.get("muted") == 1
            and muted_status.get("ignore", {}).get("counts", {}).get("muted") == 1,
            f"total={muted_dirty.get('total')} muted={muted_dirty.get('muted')}",
        )
        status, changes_muted = http_call("GET", base, f"/api/repos/{mute_id}/changes")
        muted_change = next(
            (item for item in (changes_muted or {}).get("files") or [] if item.get("path") == "only.tmp"),
            {},
        )
        check(
            "提交面板仍能看到静音文件（不影响提交正确性）",
            status == 200 and muted_change.get("muted") is True
            and muted_change.get("group") == "untracked"
            and ((changes_muted or {}).get("counts") or {}).get("untracked", 0) >= 1,
            f"group={muted_change.get('group')}",
        )
        status, unmuted = http_call(
            "DELETE", base, f"/api/repos/{mute_id}/mutes/{muted_result.get('mutes', [{}])[0].get('id')}"
        )
        unmuted_dirty = ((unmuted or {}).get("status") or {}).get("dirty") or {}
        check(
            "取消静音后待处理计数恢复",
            status == 200 and unmuted_dirty.get("muted") == 0
            and unmuted_dirty.get("pending") == 1,
            f"dirty={unmuted_dirty}",
        )

        status, untrack_reject = http_call(
            "POST", base, f"/api/repos/{ignore_id}/untrack",
            {"paths": ["tracked.tmp"], "confirm": False},
        )
        check(
            "取消跟踪未确认时被拒绝",
            status == 400 and (untrack_reject or {}).get("code") == "confirm_required",
            str((untrack_reject or {}).get("message")),
        )
        status, untracked_result = http_call(
            "POST", base, f"/api/repos/{ignore_id}/untrack",
            {"paths": ["tracked.tmp"], "confirm": True},
        )
        untracked_result = untracked_result or {}
        disk_exists = (ignore_dir / "tracked.tmp").is_file()
        staged_paths = [
            item.get("path") for item in ((untracked_result.get("status") or {}).get("files") or {}).get("staged") or []
        ]
        check(
            "git rm --cached 后文件仍在磁盘且变为已删除待提交",
            status == 200 and disk_exists and "tracked.tmp" in staged_paths
            and untracked_result.get("needs_commit") is True,
            f"exists={disk_exists} staged={staged_paths}",
        )
        status, untrack_audit = http_call(
            "GET", base, f"/api/audit?page=1&page_size=10&action=untrack&repo_id={ignore_id}"
        )
        check(
            "取消跟踪写入审计（action=untrack）",
            status == 200 and ((untrack_audit or {}).get("items") or []) != []
            and ((untrack_audit or {}).get("items") or [{}])[0].get("action") == "untrack",
            f"items={len((untrack_audit or {}).get('items') or [])}",
        )

        # ---------- 12.8 远端错误分类器与地址转换 ----------
        forbidden = classify_remote_error(
            "remote: Permission to lemomo-ai/lemo-opuscar.git denied to ZeroOneCN.\n"
            "fatal: unable to access 'https://github.com/lemomo-ai/lemo-opuscar.git/': "
            "The requested URL returned error: 403"
        ) or {}
        check(
            "403 归因解析出 owner/repo 与被拒账号",
            forbidden.get("code") == "http_403_forbidden"
            and "lemomo-ai/lemo-opuscar.git" in (forbidden.get("detail") or "")
            and "ZeroOneCN" in (forbidden.get("detail") or "")
            and any(item.get("key") == "use_ssh" for item in forbidden.get("suggestions") or []),
            str(forbidden.get("title")),
        )
        check(
            "401 认证失败归类为凭据无效",
            (classify_remote_error("fatal: Authentication failed for 'https://example.com/repo.git/'") or {}).get("code")
            == "http_401_auth",
            "",
        )
        check(
            "SSH 主机密钥问题归类为 ssh_host_key",
            (classify_remote_error("Host key verification failed.\nfatal: Could not read from remote repository.") or {}).get("code")
            == "ssh_host_key",
            "",
        )
        check(
            "SSH 公钥被拒归类为 ssh_publickey",
            (classify_remote_error("git@github.com: Permission denied (publickey).") or {}).get("code")
            == "ssh_publickey",
            "",
        )
        check(
            "non-fast-forward 归类为推送被拒",
            (classify_remote_error("! [rejected] main -> main (non-fast-forward)") or {}).get("code")
            == "push_rejected",
            "",
        )
        check(
            "网络连接失败归类为 network_error",
            (classify_remote_error("fatal: unable to access 'https://x/': Failed to connect to x port 443") or {}).get("code")
            == "network_error",
            "",
        )
        ssh_github = https_to_ssh("https://github.com/lemomo-ai/lemo-opuscar.git")
        ssh_gitee = https_to_ssh("https://gitee.com/owner/repo")
        ssh_gitlab = https_to_ssh("https://gitlab.com/group/sub/repo.git")
        ssh_same = https_to_ssh("git@github.com:owner/repo.git")
        ssh_other = https_to_ssh("https://git.example.com/owner/repo.git")
        check(
            "HTTPS→SSH 转换覆盖 github/gitee/gitlab 且保留 .git 后缀",
            ssh_github.get("ssh_url") == "git@github.com:lemomo-ai/lemo-opuscar.git"
            and ssh_gitee.get("ssh_url") == "git@gitee.com:owner/repo"
            and ssh_gitlab.get("ssh_url") == "git@gitlab.com:group/sub/repo.git",
            f"github={ssh_github.get('ssh_url')} gitee={ssh_gitee.get('ssh_url')}",
        )
        check(
            "已是 SSH 或非主流域名时拒绝转换",
            ssh_same.get("ok") is False and ssh_other.get("ok") is False,
            f"same={ssh_same.get('reason')} other={ssh_other.get('reason')}",
        )
        check(
            "远端协议识别 https / ssh / local",
            detect_remote_protocol("https://github.com/a/b.git") == "https"
            and detect_remote_protocol("git@github.com:a/b.git") == "ssh"
            and detect_remote_protocol(r"E:\Code\repo") == "local",
            "",
        )
        parsed_credentials = parse_cmdkey_output(
            "目标: git:https://github.com\nTarget: LegacyGeneric:target=git:https://gitee.com\n"
            "目标: git:https://github.com\n"
        )
        check(
            "cmdkey 输出解析出 git HTTPS 条目并去重",
            [item.get("target") for item in parsed_credentials]
            == ["git:https://github.com", "git:https://gitee.com"],
            str(parsed_credentials),
        )

        err_dir = test_root / "error-repo"
        make_repo(err_dir)
        run_git(git_exe, ["remote", "add", "origin", "https://127.0.0.1:1/nope.git"], cwd=err_dir)
        head_sha = run_git(git_exe, ["rev-parse", "HEAD"], cwd=err_dir).stdout.strip()
        run_git(git_exe, ["update-ref", "refs/remotes/origin/main", head_sha], cwd=err_dir)
        run_git(git_exe, ["config", "branch.main.remote", "origin"], cwd=err_dir)
        run_git(git_exe, ["config", "branch.main.merge", "refs/heads/main"], cwd=err_dir)
        status, added_error = http_call(
            "POST", base, "/api/repos", {"paths": [str(err_dir)]}
        )
        err_id = ((added_error or {}).get("results") or [{}])[0].get("repo_id")
        status, push_failed = http_call("POST", base, f"/api/repos/{err_id}/push", {})
        push_failed = push_failed or {}
        payload = push_failed
        check(
            "推送网络失败返回结构化诊断且保留原始输出",
            status == 502 and isinstance(payload.get("diagnosis"), dict)
            and payload["diagnosis"].get("code") == "network_error"
            and (payload.get("output") or "").strip() != ""
            and "push" in (payload.get("command") or ""),
            f"code={(payload.get('diagnosis') or {}).get('code')}",
        )

        status, credentials = http_call("GET", base, "/api/credentials")
        before_targets = [item.get("target") for item in (credentials or {}).get("items") or []]
        check(
            "HTTPS 凭据面板仅返回 host 与目标（不返回密码）",
            status == 200 and isinstance((credentials or {}).get("items"), list)
            and all("target" in item and "host" in item and "password" not in item
                    for item in (credentials or {}).get("items") or []),
            f"items={len(before_targets)}",
        )
        status, invalid_delete = http_call(
            "POST", base, "/api/credentials/delete",
            {"target": "https://github.com", "confirm": True},
        )
        check(
            "非 git: 前缀的凭据目标被拒绝",
            status == 400 and (invalid_delete or {}).get("code") == "invalid_credential_target",
            str((invalid_delete or {}).get("message")),
        )
        status, test_delete = http_call(
            "POST", base, "/api/credentials/delete",
            {"target": "git:https://gitmanager-selftest.invalid", "confirm": True},
        )
        status, credentials_after = http_call("GET", base, "/api/credentials")
        after_targets = [item.get("target") for item in (credentials_after or {}).get("items") or []]
        check(
            "清除测试 target 不影响任何真实凭据",
            before_targets == after_targets,
            f"before={len(before_targets)} after={len(after_targets)} delete_status={status}",
        )
        status, login_reject = http_call(
            "POST", base, "/api/credentials/login", {"confirm": False}
        )
        check(
            "交互式登录未确认时被拒绝（不会启动外部窗口）",
            status == 400 and (login_reject or {}).get("code") == "confirm_required",
            str((login_reject or {}).get("message")),
        )

        convert_dir = test_root / "convert-repo"
        make_repo(convert_dir)
        run_git(
            git_exe,
            ["remote", "add", "origin", "https://github.com/lemomo-ai/lemo-opuscar.git"],
            cwd=convert_dir,
        )
        status, added_convert = http_call(
            "POST", base, "/api/repos", {"paths": [str(convert_dir)]}
        )
        convert_id = ((added_convert or {}).get("results") or [{}])[0].get("repo_id")
        status, ssh_preview = http_call(
            "POST", base, f"/api/repos/{convert_id}/remote/ssh/preview",
            {"remote": "origin"},
        )
        ssh_preview = ssh_preview or {}
        check(
            "远端 SSH 转换预览给出目标地址",
            status == 200 and ssh_preview.get("fetch_convertible") is True
            and ssh_preview.get("ssh_fetch_url") == "git@github.com:lemomo-ai/lemo-opuscar.git",
            str(ssh_preview.get("ssh_fetch_url")),
        )
        status, converted = http_call(
            "POST", base, f"/api/repos/{convert_id}/remote/ssh",
            {"remote": "origin", "mode": "both", "confirm": True},
        )
        status, converted_remotes = http_call("GET", base, f"/api/repos/{convert_id}/remotes")
        remote_items = (converted_remotes or {}).get("remotes") or []
        check(
            "切换后 fetch/push 地址变为 SSH 且状态协议为 ssh",
            status == 200 and remote_items and remote_items[0].get("fetch_url", "").startswith("git@github.com:")
            and remote_items[0].get("push_url", "").startswith("git@github.com:")
            and ((converted or {}).get("status") or {}).get("remote_info", {}).get("protocol") == "ssh",
            str(remote_items),
        )
        status, ssh_preview_again = http_call(
            "POST", base, f"/api/repos/{convert_id}/remote/ssh/preview",
            {"remote": "origin"},
        )
        check(
            "已是 SSH 地址时不再重复转换",
            status == 200 and (ssh_preview_again or {}).get("fetch_convertible") is False
            and "已经是 SSH" in ((ssh_preview_again or {}).get("reason") or ""),
            str((ssh_preview_again or {}).get("reason")),
        )

        # ---------- 13. 仓库扫描（只读、跳过噪音、深度与结果上限） ----------
        make_repo(scan_root / "repo-a")
        make_repo(scan_root / "group" / "repo-b")
        make_repo(scan_root / "group" / "repo-c")
        make_repo(scan_root / "node_modules" / "noise-repo")
        make_repo(scan_root / ".venv" / "noise-repo2")
        make_repo(scan_root / "deep" / "a" / "b" / "repo-deep")
        junction = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(scan_root / "loop"), str(scan_root)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=False,
        )
        check(
            "准备扫描夹具（噪音目录、超深目录与目录联接）",
            True,
            "目录联接已创建"
            if junction.returncode == 0
            else f"环境不支持目录联接，循环测试降级：{(junction.stderr or '').strip()[:80]}",
        )

        status, scan_job = http_call(
            "POST",
            base,
            "/api/scan/start",
            {"roots": [str(scan_root)], "max_depth": 3, "limit": 50},
        )
        scan_job_id = (scan_job or {}).get("job_id") or ""
        check(
            "扫描任务启动并返回 job_id",
            status == 200 and bool(scan_job_id),
            str((scan_job or {}).get("status")),
        )
        scan_snapshot = wait_scan(base, scan_job_id)
        scan_items = scan_snapshot.get("items") or []
        scanned_paths = {os.path.normcase(item.get("path", "")) for item in scan_items}
        repo_a_path = os.path.normcase(str(scan_root / "repo-a"))
        repo_b_path = os.path.normcase(str(scan_root / "group" / "repo-b"))
        noise_node = os.path.normcase(str(scan_root / "node_modules" / "noise-repo"))
        noise_venv = os.path.normcase(str(scan_root / ".venv" / "noise-repo2"))
        deep_repo = os.path.normcase(str(scan_root / "deep" / "a" / "b" / "repo-deep"))
        check("扫描任务完成", scan_snapshot.get("status") == "done", str(scan_snapshot.get("status")))
        check(
            "扫描识别真实仓库（含嵌套目录）",
            repo_a_path in scanned_paths and repo_b_path in scanned_paths,
            f"found={len(scan_items)} paths={[item.get('path') for item in scan_items]}",
        )
        check(
            "扫描跳过 node_modules / .venv 噪音目录",
            noise_node not in scanned_paths and noise_venv not in scanned_paths,
            f"found={len(scan_items)}",
        )
        check("扫描遵守最大深度限制", deep_repo not in scanned_paths, f"found={len(scan_items)}")
        check(
            "扫描进度包含已访问目录数",
            int(scan_snapshot.get("scanned_dirs") or 0) >= 4,
            f"scanned_dirs={scan_snapshot.get('scanned_dirs')}",
        )
        check(
            "扫描未因目录联接陷入循环",
            scan_snapshot.get("status") == "done" and int(scan_snapshot.get("scanned_dirs") or 0) < 200,
            f"scanned_dirs={scan_snapshot.get('scanned_dirs')}",
        )
        repo_a_item = next(
            (item for item in scan_items if os.path.normcase(item.get("path", "")) == repo_a_path),
            {},
        )
        check(
            "扫描结果包含当前分支且标记未管理",
            repo_a_item.get("branch") == "main"
            and repo_a_item.get("detached") is False
            and repo_a_item.get("already_managed") is False,
            str(repo_a_item),
        )

        status, limited_job = http_call(
            "POST",
            base,
            "/api/scan/start",
            {"roots": [str(scan_root)], "max_depth": 3, "limit": 2},
        )
        limited_snapshot = wait_scan(base, (limited_job or {}).get("job_id") or "")
        check(
            "扫描结果上限生效并标记截断",
            limited_snapshot.get("status") == "done"
            and limited_snapshot.get("truncated") is True
            and limited_snapshot.get("found") == 2,
            f"found={limited_snapshot.get('found')} truncated={limited_snapshot.get('truncated')}",
        )

        status, bad_scan = http_call("POST", base, "/api/scan/start", {"roots": ["C:\\Windows"]})
        check(
            "白名单外扫描根目录被拒绝",
            status == 400 and (bad_scan or {}).get("code") == "path_not_allowed",
            str((bad_scan or {}).get("code")),
        )
        status, rel_scan = http_call("POST", base, "/api/scan/start", {"roots": ["relative-dir"]})
        check(
            "相对扫描根目录被拒绝",
            status == 400 and (rel_scan or {}).get("code") == "invalid_scan_root",
            str((rel_scan or {}).get("code")),
        )
        status, missing_scan = http_call("GET", base, "/api/scan/not-a-job")
        check(
            "未知扫描任务返回 404",
            status == 404 and (missing_scan or {}).get("code") == "scan_not_found",
            str((missing_scan or {}).get("code")),
        )

        # ---------- 14. 扫描结果批量添加 ----------
        status, batch = http_call(
            "POST",
            base,
            "/api/repos",
            {"paths": [str(scan_root / "repo-a"), str(scan_root / "group" / "repo-b")]},
        )
        batch_results = (batch or {}).get("results") or []
        repo_a_id = int((batch_results[0] or {}).get("repo_id") or 0) if batch_results else 0
        repo_b_id = int((batch_results[1] or {}).get("repo_id") or 0) if len(batch_results) > 1 else 0
        check(
            "扫描结果可一键批量加入管理列表",
            status == 200
            and len(batch_results) == 2
            and all(item.get("ok") for item in batch_results)
            and repo_a_id > 0
            and repo_b_id > 0,
            json.dumps(batch_results, ensure_ascii=False)[:200],
        )
        status, rescan_job = http_call(
            "POST",
            base,
            "/api/scan/start",
            {"roots": [str(scan_root)], "max_depth": 3, "limit": 50},
        )
        rescan_snapshot = wait_scan(base, (rescan_job or {}).get("job_id") or "")
        check(
            "再次扫描把已加入仓库标记为已管理",
            rescan_snapshot.get("status") == "done"
            and int(rescan_snapshot.get("managed_count") or 0) >= 2,
            f"managed_count={rescan_snapshot.get('managed_count')}",
        )
        rescan_items = rescan_snapshot.get("items") or []
        rescan_repo_a = next(
            (item for item in rescan_items if os.path.normcase(item.get("path", "")) == repo_a_path),
            {},
        )
        check(
            "扫描结果每项都带 already_managed 标记（已管理项为 true）",
            bool(rescan_items)
            and all("already_managed" in item for item in rescan_items)
            and rescan_repo_a.get("already_managed") is True,
            f"items={len(rescan_items)} repo_a={rescan_repo_a.get('already_managed')}",
        )
        status, batch_skip = http_call(
            "POST",
            base,
            "/api/repos",
            {"paths": [str(scan_root / "group" / "repo-c"), str(scan_root / "repo-a")]},
        )
        batch_skip = batch_skip or {}
        skip_items = batch_skip.get("skipped") or []
        check(
            "批量添加跳过已存在并给出正确计数",
            status == 200
            and batch_skip.get("added") == 1
            and len(skip_items) == 1
            and skip_items[0].get("reason") == "already_exists"
            and int(skip_items[0].get("repo_id") or 0) == repo_a_id,
            json.dumps(
                {"added": batch_skip.get("added"), "skipped": skip_items},
                ensure_ascii=False,
            )[:300],
        )

        # ---------- 15. 多根白名单管理 ----------
        status, roots_before = http_call("GET", base, "/api/allowed-roots")
        check(
            "白名单列表包含 .env 初始根目录",
            status == 200 and len((roots_before or {}).get("items") or []) >= 1,
            f"count={len((roots_before or {}).get('items') or [])}",
        )
        status, root_added = http_call(
            "POST", base, "/api/allowed-roots", {"path": str(scan_root) + os.sep}
        )
        added_item = (root_added or {}).get("item") or {}
        scan_root_id = int(added_item.get("id") or 0)
        check(
            "新增白名单根目录并规范化尾部分隔符",
            status == 200
            and scan_root_id > 0
            and os.path.normcase(added_item.get("path", "")) == os.path.normcase(str(scan_root)),
            str(added_item.get("path")),
        )
        status, dup_root = http_call(
            "POST", base, "/api/allowed-roots", {"path": str(scan_root).upper()}
        )
        check(
            "重复根目录（大小写不敏感）被拒绝",
            status == 409 and (dup_root or {}).get("code") == "root_duplicate",
            str((dup_root or {}).get("code")),
        )
        status, missing_root = http_call(
            "POST", base, "/api/allowed-roots", {"path": str(test_root / "missing-root")}
        )
        check(
            "不存在的根目录被拒绝",
            status == 400 and (missing_root or {}).get("code") == "root_not_found",
            str((missing_root or {}).get("code")),
        )
        status, relative_root = http_call(
            "POST", base, "/api/allowed-roots", {"path": "_gitmanager_selftest"}
        )
        check(
            "相对路径根目录被拒绝",
            status == 400 and (relative_root or {}).get("code") == "invalid_root",
            str((relative_root or {}).get("code")),
        )
        status, root_in_use = http_call("DELETE", base, f"/api/allowed-roots/{scan_root_id}")
        check(
            "仍有受管仓库时移除白名单被拒绝",
            status == 409
            and (root_in_use or {}).get("code") == "root_in_use"
            and int((root_in_use or {}).get("count") or 0) >= 2,
            f"count={(root_in_use or {}).get('count')}",
        )
        status, root_forced = http_call(
            "DELETE", base, f"/api/allowed-roots/{scan_root_id}?force=true"
        )
        check(
            "强制移除白名单根目录",
            status == 200 and (root_forced or {}).get("ok") is True,
            f"affected={(root_forced or {}).get('affected_repos')}",
        )
        status, restored_root = http_call(
            "POST", base, "/api/allowed-roots", {"path": str(scan_root)}
        )
        check("重新加入白名单", status == 200, str(((restored_root or {}).get("item") or {}).get("path")))
        status, restored_status = http_call("GET", base, f"/api/repos/{repo_a_id}/status")
        check(
            "重新加入后恢复仓库操作",
            status == 200 and bool((restored_status or {}).get("repo")),
            f"HTTP {status}",
        )

        # 使用工作区之外的独立根目录，验证“移除白名单后拒绝操作”
        extra_root = Path(tempfile.mkdtemp(prefix="gitmanager_selftest_extra_"))
        make_repo(extra_root / "repo-x")
        status, extra_added = http_call(
            "POST", base, "/api/allowed-roots", {"path": str(extra_root)}
        )
        extra_root_id = int(((extra_added or {}).get("item") or {}).get("id") or 0)
        check(
            "新增工作区之外的临时白名单根目录",
            status == 200 and extra_root_id > 0,
            str(extra_root),
        )
        status, extra_repo_added = http_call(
            "POST", base, "/api/repos", {"paths": [str(extra_root / "repo-x")]}
        )
        extra_repo_id = int(((((extra_repo_added or {}).get("results") or [{}])[0]).get("repo_id")) or 0)
        check(
            "添加临时根目录下的仓库",
            status == 200 and extra_repo_id > 0,
            f"repo_id={extra_repo_id}",
        )
        status, extra_removed = http_call(
            "DELETE", base, f"/api/allowed-roots/{extra_root_id}?force=true"
        )
        check(
            "移除临时根目录",
            status == 200 and (extra_removed or {}).get("ok") is True,
            str((extra_removed or {}).get("affected_repos")),
        )
        status, blocked = http_call("GET", base, f"/api/repos/{extra_repo_id}/status")
        check(
            "白名单外仓库操作被拒绝",
            status == 403 and (blocked or {}).get("code") == "path_not_allowed",
            str((blocked or {}).get("code")),
        )

        # ---------- 16. SSH 私钥扫描 ----------
        status, ssh_scan = http_call("GET", base, "/api/ssh/scan")
        ssh_scan = ssh_scan or {}
        key_items = {item.get("name"): item for item in ssh_scan.get("keys") or []}
        info_a = key_items.get("id_ed25519") or {}
        check(
            "SSH 扫描发现私钥并读取公钥指纹",
            status == 200
            and bool(info_a.get("public_key_path"))
            and str(info_a.get("fingerprint") or "").startswith("SHA256:"),
            str(info_a.get("fingerprint")),
        )
        check(
            "SSH config Host 别名被解析并关联到私钥",
            "selftest-a" in (ssh_scan.get("hosts") or [])
            and (info_a.get("host_aliases") or []) == ["selftest-a"],
            f"hosts={ssh_scan.get('hosts')} aliases={info_a.get('host_aliases')}",
        )
        check(
            "SSH 扫描结果不包含私钥正文",
            "BEGIN OPENSSH PRIVATE KEY" not in json.dumps(ssh_scan, ensure_ascii=False),
            "已确认无私钥内容",
        )
        check(
            "SSH 目录与 config 状态可读",
            ssh_scan.get("ssh_dir_exists") is True and ssh_scan.get("config_exists") is True,
            str(ssh_scan.get("ssh_dir")),
        )

        # ---------- 17. SSH 身份 CRUD、默认与仓库绑定 ----------
        status, created_a = http_call(
            "POST",
            base,
            "/api/ssh/identities",
            {"name": "自测默认身份", "key_path": str(key_a), "host_alias": "selftest-a"},
        )
        identity_a_id = int(((created_a or {}).get("item") or {}).get("id") or 0)
        check("新增 SSH 身份", status == 200 and identity_a_id > 0, str((created_a or {}).get("message")))
        status, dup_identity = http_call(
            "POST",
            base,
            "/api/ssh/identities",
            {"name": "重复身份", "key_path": str(key_a), "host_alias": "selftest-a"},
        )
        check(
            "重复私钥+别名身份被拒绝",
            status == 400 and (dup_identity or {}).get("code") == "invalid_ssh_identity",
            str((dup_identity or {}).get("code")),
        )
        status, bad_alias = http_call(
            "POST",
            base,
            "/api/ssh/identities",
            {"name": "非法别名", "key_path": str(key_a), "host_alias": "bad host!"},
        )
        check("非法 Host 别名被拒绝", status == 400, str((bad_alias or {}).get("code")))
        status, bad_key = http_call(
            "POST",
            base,
            "/api/ssh/identities",
            {"name": "非法私钥", "key_path": str(work_dir / "README.md"), "host_alias": ""},
        )
        check("非私钥文件被拒绝", status == 400, str((bad_key or {}).get("code")))
        status, created_b = http_call(
            "POST",
            base,
            "/api/ssh/identities",
            {"name": "自测工作密钥", "key_path": str(key_b), "host_alias": "selftest-b"},
        )
        identity_b_id = int(((created_b or {}).get("item") or {}).get("id") or 0)
        check("新增第二个 SSH 身份", status == 200 and identity_b_id > 0, str(identity_b_id))

        identity_a_name = "自测默认身份（改名）"
        status, renamed = http_call(
            "PATCH", base, f"/api/ssh/identities/{identity_a_id}", {"name": identity_a_name}
        )
        check(
            "编辑身份显示名",
            status == 200 and ((renamed or {}).get("item") or {}).get("name") == identity_a_name,
            str(((renamed or {}).get("item") or {}).get("name")),
        )
        status, default_set = http_call("POST", base, f"/api/ssh/identities/{identity_a_id}/default")
        status, settings_after = http_call("GET", base, "/api/settings")
        check(
            "设置全局默认身份并写入运行配置",
            status == 200
            and ((default_set or {}).get("item") or {}).get("is_default") is True
            and (((settings_after or {}).get("default_ssh_identity") or {}).get("name") == identity_a_name),
            str((settings_after or {}).get("default_ssh_identity")),
        )
        status, disabled = http_call(
            "PATCH", base, f"/api/ssh/identities/{identity_b_id}", {"enabled": False}
        )
        check(
            "禁用 SSH 身份",
            status == 200 and ((disabled or {}).get("item") or {}).get("enabled") is False,
            str(((disabled or {}).get("item") or {}).get("enabled")),
        )
        status, bind_disabled = http_call(
            "PUT", base, f"/api/repos/{repo_a_id}/ssh-identity", {"identity_id": identity_b_id}
        )
        check(
            "已禁用身份不能被仓库绑定",
            status == 400 and "禁用" in ((bind_disabled or {}).get("message") or ""),
            str((bind_disabled or {}).get("message")),
        )
        status, bound = http_call(
            "PUT", base, f"/api/repos/{repo_a_id}/ssh-identity", {"identity_id": identity_a_id}
        )
        check(
            "仓库级绑定优先于全局默认",
            status == 200 and ((bound or {}).get("effective") or {}).get("source") == "repo",
            str((bound or {}).get("effective")),
        )
        status, status_a = http_call("GET", base, f"/api/repos/{repo_a_id}/status")
        check(
            "体检状态显示生效 SSH 身份",
            status == 200
            and (((status_a or {}).get("ssh_identity") or {}).get("name") == identity_a_name),
            str((status_a or {}).get("ssh_identity")),
        )

        # ---------- 18. SSH 连接测试与推送身份注入 ----------
        status, ssh_test = http_call(
            "POST", base, f"/api/ssh/identities/{identity_a_id}/test", {"host": "127.0.0.1"}
        )
        check(
            "SSH 连接测试返回可读结果（未实测真实认证）",
            status == 200
            and (ssh_test or {}).get("status") in {"auth_failed", "host_unreachable", "error", "timeout"}
            and bool((ssh_test or {}).get("output")),
            f"status={(ssh_test or {}).get('status')} output={((ssh_test or {}).get('output') or '')[:80]}",
        )

        expected_ssh_command = build_ssh_command(settings.ssh_executable, str(key_a))
        check(
            "GIT_SSH_COMMAND 含 IdentitiesOnly 与 -i 且不拼进命令行",
            "IdentitiesOnly=yes" in expected_ssh_command and f'-i "{str(key_a).replace(chr(92), "/")}"' in expected_ssh_command,
            expected_ssh_command,
        )

        make_repo(ssh_repo_dir)
        run_git(
            git_exe,
            ["remote", "add", "origin", "ssh://127.0.0.1:1/selftest.git"],
            cwd=ssh_repo_dir,
        )
        run_git(git_exe, ["config", "branch.main.remote", "origin"], cwd=ssh_repo_dir)
        run_git(git_exe, ["config", "branch.main.merge", "refs/heads/main"], cwd=ssh_repo_dir)
        status, ssh_added = http_call("POST", base, "/api/repos", {"paths": [str(ssh_repo_dir)]})
        ssh_repo_id = int(((((ssh_added or {}).get("results") or [{}])[0]).get("repo_id")) or 0)
        check("添加 SSH 远端测试仓库", status == 200 and ssh_repo_id > 0, f"repo_id={ssh_repo_id}")
        status, ssh_bind = http_call(
            "PUT", base, f"/api/repos/{ssh_repo_id}/ssh-identity", {"identity_id": identity_a_id}
        )
        check("绑定身份到 SSH 远端仓库", status == 200, str((ssh_bind or {}).get("effective")))
        status, ssh_push = http_call(
            "POST",
            base,
            f"/api/repos/{ssh_repo_id}/push",
            {"set_upstream": True, "remote": "origin"},
        )
        check(
            "SSH 远端推送失败但错误可读",
            status >= 400
            and bool((ssh_push or {}).get("output") or (ssh_push or {}).get("message")),
            f"HTTP {status} code={(ssh_push or {}).get('code')}",
        )
        status, push_audit = http_call(
            "GET", base, f"/api/audit?action=push_set_upstream&repo_id={ssh_repo_id}&limit=5"
        )
        push_rows = (push_audit or {}).get("items") or []
        check(
            "推送审计记录 SSH 身份名",
            status == 200 and bool(push_rows) and push_rows[0].get("identity_name") == identity_a_name,
            str(push_rows[0].get("identity_name")) if push_rows else "no rows",
        )
        check(
            "审计命令文本不包含私钥路径",
            bool(push_rows) and "id_ed25519" not in (push_rows[0].get("command") or ""),
            str(push_rows[0].get("command")) if push_rows else "",
        )
        status, test_audit = http_call("GET", base, "/api/audit?action=ssh_test&limit=5")
        test_rows = (test_audit or {}).get("items") or []
        check(
            "SSH 连接测试审计记录身份名",
            status == 200 and bool(test_rows) and test_rows[0].get("identity_name") == identity_a_name,
            str(test_rows[0].get("identity_name")) if test_rows else "no rows",
        )

        # ---------- 19. 删除身份配置（不删除磁盘密钥） ----------
        del_status, deleted_b = http_call("DELETE", base, f"/api/ssh/identities/{identity_b_id}")
        status, identities_now = http_call("GET", base, "/api/ssh/identities")
        check(
            "删除身份配置但保留磁盘密钥文件",
            del_status == 200
            and (deleted_b or {}).get("ok") is True
            and key_b.is_file()
            and all(item.get("id") != identity_b_id for item in (identities_now or {}).get("items") or []),
            f"key_exists={key_b.is_file()}",
        )
        status, unbound = http_call(
            "PUT", base, f"/api/repos/{repo_a_id}/ssh-identity", {"identity_id": None}
        )
        check(
            "清除仓库绑定后回落全局默认",
            status == 200 and ((unbound or {}).get("effective") or {}).get("source") == "default",
            str((unbound or {}).get("effective")),
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
