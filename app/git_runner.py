"""Git 子进程执行器（全项目唯一的 subprocess 出口）。

职责：以参数数组安全调用系统 git，采集输出与耗时，并在每次调用后写入审计表。
约束：禁用 shell；固定非交互环境变量；超时即终止；异常同样必须落审计。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .db import Database, now_iso


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


class GitExecutor:
    """Git 命令执行器。

    参数：
        git_executable: git 可执行文件绝对路径。
        timeout_seconds: 单次命令超时秒数。
        database: 审计写入使用的 Database 实例。
        summary_limit: 审计输出摘要最大字符数。
    """

    def __init__(
        self,
        git_executable: str,
        timeout_seconds: int,
        database: Database,
        summary_limit: int = 2000,
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

    def _build_env(self) -> dict[str, str]:
        """构造 git 子进程环境变量。

        参数：
            无。
        返回值：
            在继承当前环境的基础上禁用交互提示、固定英文输出的环境字典。
        """
        env = os.environ.copy()
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = "echo"
        env["GCM_INTERACTIVE"] = "never"
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
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
    ) -> GitResult:
        """执行一次 git 命令并自动记录审计。

        参数：
            repo_id: 仓库主键（无仓库上下文时可为 None）。
            repo_path: 命令工作目录（仓库绝对路径）。
            action: 动作标识，如 status / pull / push。
            args: 传给 git 的参数数组（不包含 git 本身）。
            forced: 是否属于强制类操作，用于审计标记。
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
            completed = subprocess.run(
                [self.git_executable, *args],
                cwd=path_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=self.timeout_seconds,
                shell=False,
                stdin=subprocess.DEVNULL,
                env=self._build_env(),
            )
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
