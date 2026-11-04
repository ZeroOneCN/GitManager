"""Git 子进程执行器（全项目唯一的 subprocess 出口）。

职责：以参数数组安全调用系统 git，采集输出与耗时，并在每次调用后写入审计表。
约束：禁用 shell；固定非交互环境变量；超时即终止；异常同样必须落审计。
"""

from __future__ import annotations

import os
import subprocess
import time
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import Database, now_iso

# 当前请求的客户端 IP（仅非回环来源时由接入层写入，用于局域网审计）
CLIENT_IP: ContextVar[str] = ContextVar("gitmanager_client_ip", default="")


@dataclass
class GitResult:
    """单次 git 调用结果。"""

    ok: bool
    exit_code: int
    stdout: str
    stderr: str
    command: str
    duration_ms: int
    timed_out: bool = False
    error: str = ""
    data: dict[str, Any] = field(default_factory=dict)


def _quote_arg(arg: str) -> str:
    """将单个参数包装为便于展示的命令片段（仅用于审计文本）。

    参数：
        arg: 原始命令行参数。
    返回值：
        含空格或引号时加双引号的展示字符串。
    """
    if arg == "" or any(ch in arg for ch in (' ', '"', "\t")):
        return '"' + arg.replace('"', '\\"') + '"'
    return arg


def build_ssh_command(ssh_executable: str, key_path: str) -> str:
    """构造 GIT_SSH_COMMAND 字符串（不拼接进命令行，仅作为环境变量传入）。

    参数：
        ssh_executable: ssh 客户端可执行文件路径或命令名。
        key_path: 私钥绝对路径。
    返回值：
        含 IdentitiesOnly 与 -i 参数的 ssh 命令文本。
    异常：
        ValueError: 路径包含换行或 NUL 等危险字符时抛出。
    """
    if any(ch in key_path for ch in ("\n", "\r", "\x00")):
        raise ValueError("私钥路径包含非法控制字符")
    # Git for Windows 通过 shell 解析该变量：统一使用正斜杠并加双引号，避免空格与注入
    safe_key = key_path.replace("\\", "/").replace('"', '\\"')
    safe_ssh = ssh_executable.replace("\\", "/").replace('"', '\\"')
    if any(ch in safe_ssh for ch in ("\n", "\r", "\x00")):
        raise ValueError("SSH 可执行文件路径包含非法控制字符")
    ssh_part = f'"{safe_ssh}"' if " " in safe_ssh else safe_ssh
    return f'{ssh_part} -o IdentitiesOnly=yes -i "{safe_key}"'


class GitExecutor:
    """Git 命令执行器。

    参数：
        git_executable: git 可执行文件绝对路径。
        timeout_seconds: 单次命令超时秒数。
        database: 审计写入使用的 Database 实例。
        summary_limit: 审计输出摘要最大字符数。
        ssh_executable: ssh 客户端路径（用于身份注入与连接测试）。
        ssh_keygen_executable: ssh-keygen 路径（用于读取公钥指纹）。
    """

    def __init__(
        self,
        git_executable: str,
        timeout_seconds: int,
        database: Database,
        summary_limit: int = 2000,
        ssh_executable: str = "ssh",
        ssh_keygen_executable: str = "ssh-keygen",
    ) -> None:
        """初始化执行器。

        参数：
            git_executable: git 可执行文件绝对路径。
            timeout_seconds: 子进程超时时间（秒）。
            database: SQLite 访问对象，用于写审计。
            summary_limit: 输出摘要截断长度。
        返回值：
            无。
        """
        self.git_executable = git_executable
        self.timeout_seconds = timeout_seconds
        self.database = database
        self.summary_limit = summary_limit
        self.ssh_executable = ssh_executable
        self.ssh_keygen_executable = ssh_keygen_executable

    def _build_env(self, ssh_identity: dict[str, Any] | None = None) -> dict[str, str]:
        """构造 git 子进程环境变量。

        参数：
            ssh_identity: 可选身份字典（含 name 与 key_path），用于注入 GIT_SSH_COMMAND。
        返回值：
            在继承当前环境的基础上禁用交互提示、固定英文输出，并按需注入 SSH 身份的环境字典。
        异常：
            ValueError: 身份中的私钥路径含危险字符时抛出。
        """
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "echo"
        env["GCM_INTERACTIVE"] = "never"
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        if ssh_identity:
            env["GIT_SSH_COMMAND"] = build_ssh_command(
                self.ssh_executable, str(ssh_identity["key_path"])
            )
        else:
            env.pop("GIT_SSH_COMMAND", None)
        return env

    def _summarize(self, stdout: str, stderr: str) -> str:
        """生成审计用输出摘要。

        参数：
            stdout: 标准输出文本。
            stderr: 标准错误文本。
        返回值：
            合并后的尾部摘要（去除 NUL 并按长度截断）。
        """
        text = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part)
        text = text.replace("\x00", "\n").strip()
        if len(text) > self.summary_limit:
            text = "…" + text[-self.summary_limit:]
        return text

    def _write_audit(
        self,
        repo_id: int | None,
        repo_path: str,
        action: str,
        command: str,
        exit_code: int,
        ok: bool,
        duration_ms: int,
        forced: bool,
        summary: str,
        identity_name: str | None = None,
    ) -> None:
        """写入单条审计记录（失败不影响主流程）。

        参数：
            repo_id: 仓库主键，可为空（如添加仓库时的探测）。
            repo_path: 仓库路径文本。
            action: 业务动作名。
            command: 展示用命令文本。
            exit_code: 退出码（超时或异常为 -1）。
            ok: 是否成功。
            duration_ms: 耗时毫秒。
            forced: 是否为强制操作。
            summary: 输出摘要。
            identity_name: 本次操作使用的 SSH 身份名（不记录私钥内容）。
        返回值：
            无。
        """
        try:
            self.database.insert_audit(
                {
                    "ts": now_iso(),
                    "repo_id": repo_id,
                    "repo_path": repo_path,
                    "action": action,
                    "command": command,
                    "exit_code": exit_code,
                    "ok": ok,
                    "duration_ms": duration_ms,
                    "forced": forced,
                    "output_summary": summary,
                    "identity_name": identity_name,
                    "client_ip": CLIENT_IP.get(),
                }
            )
        except Exception:
            # 审计失败不阻断业务，但通过 stderr 提示便于排查
            import sys

            print(f"[audit] 写入审计失败: {action} @ {repo_path}", file=sys.stderr)

    def run(
        self,
        repo_id: int | None,
        repo_path: str | Path,
        action: str,
        args: list[str],
        forced: bool = False,
        ssh_identity: dict[str, Any] | None = None,
        record_audit: bool = True,
        input_text: str | None = None,
    ) -> GitResult:
        """执行一次 git 命令并自动记录审计。

        参数：
            repo_id: 仓库主键（无仓库上下文时可为 None）。
            repo_path: 命令工作目录（仓库绝对路径）。
            action: 动作标识，如 status / pull / push。
            args: 传给 git 的参数数组（不包含 git 本身）。
            forced: 是否属于强制类操作，用于审计标记。
            ssh_identity: 可选 SSH 身份，仅网络类动作注入 GIT_SSH_COMMAND。
            record_audit: 是否写入审计；只读诊断探测（如 check-ignore）可置 False，
                避免把正常返回码 1 的探测记成失败记录。
            input_text: 需要写入 stdin 的文本（如 check-ignore --stdin 的 NUL 分隔路径）；
                为 None 时使用 DEVNULL，避免任何交互式阻塞。
        返回值：
            GitResult，包含退出码、输出、耗时与展示用命令。
        """
        path_text = str(repo_path)
        command_text = "git " + " ".join(_quote_arg(arg) for arg in args)
        started = time.perf_counter()
        exit_code = -1
        stdout = ""
        stderr = ""
        timed_out = False
        error_text = ""

        try:
            run_kwargs: dict[str, Any] = {
                "cwd": path_text,
                "capture_output": True,
                "text": True,
                "encoding": "utf-8",
                "errors": "replace",
                "timeout": self.timeout_seconds,
                "shell": False,
                "env": self._build_env(ssh_identity),
            }
            if input_text is None:
                run_kwargs["stdin"] = subprocess.DEVNULL
            else:
                run_kwargs["input"] = input_text
            completed = subprocess.run([self.git_executable, *args], **run_kwargs)
            exit_code = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            exit_code = -1
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            error_text = f"命令超时（>{self.timeout_seconds}s）"
        except (FileNotFoundError, NotADirectoryError, PermissionError, OSError) as exc:
            exit_code = -1
            error_text = f"无法在目标目录执行 git：{exc}"
            stderr = error_text

        duration_ms = int((time.perf_counter() - started) * 1000)
        ok = exit_code == 0 and not timed_out
        summary = self._summarize(stdout, stderr)
        if error_text and not summary:
            summary = error_text
        if record_audit:
            self._write_audit(
                repo_id=repo_id,
                repo_path=path_text,
                action=action,
                command=command_text,
                exit_code=exit_code,
                ok=ok,
                duration_ms=duration_ms,
                forced=forced,
                summary=summary,
                identity_name=(ssh_identity or {}).get("name"),
            )

        return GitResult(
            ok=ok,
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            command=command_text,
            duration_ms=duration_ms,
            timed_out=timed_out,
            error=error_text,
        )

    def test_ssh(
        self,
        identity: dict[str, Any],
        host: str,
        timeout_seconds: int,
    ) -> dict[str, Any]:
        """测试 SSH 身份到指定主机的连通性（BatchMode，不产生交互提示）。

        参数：
            identity: 身份字典（含 name 与 key_path）。
            host: Host 别名或主机名。
            timeout_seconds: 超时秒数。
        返回值：
            含 status/exit_code/output/permission_issue/warnings 的字典。
        """
        args = [
            self.ssh_executable,
            "-T",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "IdentitiesOnly=yes",
            "-i",
            str(identity["key_path"]),
            host,
        ]
        display = " ".join(_quote_arg(arg) for arg in args)
        permission_issue = False
        warnings: list[str] = []
        key_path = Path(str(identity["key_path"]))
        if not key_path.is_file():
            permission_issue = True
            warnings.append("私钥文件不存在")
        else:
            try:
                if not os.access(key_path, os.R_OK):
                    permission_issue = True
                    warnings.append("私钥文件不可读")
                mode = key_path.stat().st_mode
                if os.name != "nt" and (mode & 0o077):
                    permission_issue = True
                    warnings.append("私钥权限过宽（建议 chmod 600）")
            except OSError as exc:
                permission_issue = True
                warnings.append(f"无法检查私钥权限：{exc}")

        started = time.perf_counter()
        timed_out = False
        exit_code = -1
        stdout = ""
        stderr = ""
        error_text = ""
        try:
            completed = subprocess.run(
                args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                stdin=subprocess.DEVNULL,
                env=self._build_env(),
            )
            exit_code = int(completed.returncode)
            stdout = completed.stdout or ""
            stderr = completed.stderr or ""
        except subprocess.TimeoutExpired as exc:
            timed_out = True
            stdout = (exc.stdout or "") if isinstance(exc.stdout, str) else ""
            stderr = (exc.stderr or "") if isinstance(exc.stderr, str) else ""
            error_text = f"SSH 连接超时（>{timeout_seconds}s）"
        except (FileNotFoundError, PermissionError, OSError) as exc:
            error_text = f"无法运行 ssh：{exc}"
            stderr = error_text

        output = "\n".join(part for part in (stdout.strip(), stderr.strip()) if part).strip()
        lower = output.lower()
        if timed_out:
            status = "timeout"
        elif "successfully authenticated" in lower:
            # GitHub 等主机认证成功时退出码为 1，需以输出判定
            status = "success"
        elif "permission denied" in lower or "publickey" in lower:
            status = "auth_failed"
        elif any(
            token in lower
            for token in (
                "could not resolve hostname",
                "connection refused",
                "connection timed out",
                "network is unreachable",
            )
        ):
            status = "host_unreachable"
        elif error_text:
            status = "error"
        elif exit_code == 0:
            status = "success"
        else:
            status = "error"

        duration_ms = int((time.perf_counter() - started) * 1000)
        summary = self._summarize(stdout, stderr) or error_text
        self._write_audit(
            repo_id=None,
            repo_path="",
            action="ssh_test",
            command=display,
            exit_code=exit_code,
            ok=status == "success",
            duration_ms=duration_ms,
            forced=False,
            summary=summary,
            identity_name=identity.get("name"),
        )
        return {
            "status": status,
            "exit_code": exit_code,
            "output": output,
            "duration_ms": duration_ms,
            "permission_issue": permission_issue,
            "warnings": warnings,
        }

    def key_fingerprint(self, public_key_path: str, timeout_seconds: int = 5) -> dict[str, Any] | None:
        """读取公钥指纹（失败时返回 None，由调用方降级展示）。

        参数：
            public_key_path: 公钥文件绝对路径。
            timeout_seconds: 超时秒数。
        返回值：
            含 fingerprint/bits/comment 的字典；读取失败返回 None。
        """
        try:
            completed = subprocess.run(
                [self.ssh_keygen_executable, "-lf", public_key_path],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                shell=False,
                stdin=subprocess.DEVNULL,
            )
        except (FileNotFoundError, PermissionError, OSError, subprocess.TimeoutExpired):
            return None
        if completed.returncode != 0:
            return None
        parts = (completed.stdout or "").strip().split()
        if len(parts) < 2:
            return None
        comment = ""
        if "(" in completed.stdout:
            before = completed.stdout.split("(", 1)[0].strip().split()
            comment = " ".join(before[2:]) if len(before) > 2 else ""
        return {"bits": parts[0], "fingerprint": parts[1], "comment": comment}
