"""仓库扫描器。

职责：在主线程之外递归扫描给定根目录，识别 Git 工作仓库并汇报进度。
约束：扫描全程只读（不执行任何 git 命令，仅读取 .git 标记与 HEAD 文件）；
      跳过依赖目录、构建目录、点目录、符号链接与 .git 内部目录，避免噪音与循环。
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from .config import Settings, normalize_repo_path, repo_path_key
from .db import Database, now_iso


class ScanManager:
    """管理扫描任务的生命周期与进度快照。

    参数：
        settings: 运行配置（深度、结果上限、噪音目录）。
        database: 数据库实例（用于判断仓库是否已在管理列表）。
    """

    def __init__(self, settings: Settings, database: Database) -> None:
        """初始化扫描管理器。

        参数：
            settings: 运行配置。
            database: 数据库实例。
        返回值：
            无。
        """
        self.settings = settings
        self.database = database
        self._jobs: dict[str, dict[str, Any]] = {}
        self._lock = threading.Lock()
        self._max_jobs = 12

    def start(self, roots: list[Path], max_depth: int, limit: int) -> dict[str, Any]:
        """创建并启动一个后台扫描任务。

        参数：
            roots: 已通过白名单校验的根目录列表。
            max_depth: 最大扫描深度（根目录为 0）。
            limit: 结果条数上限。
        返回值：
            任务快照（含 job_id 与初始状态）。
        """
        job_id = uuid.uuid4().hex[:12]
        managed = {repo_path_key(repo["path"]) for repo in self.database.list_repos()}
        job: dict[str, Any] = {
            "job_id": job_id,
            "status": "running",
            "roots": [str(root) for root in roots],
            "max_depth": max_depth,
            "limit": limit,
            "scanned_dirs": 0,
            "found": 0,
            "truncated": False,
            "error_samples": [],
            "items": [],
            "managed_count": 0,
            "started_at": now_iso(),
            "finished_at": None,
            "duration_ms": 0,
            "_cancel": False,
            "_managed": managed,
        }
        with self._lock:
            self._jobs[job_id] = job
            self._prune_jobs()

        thread = threading.Thread(
            target=self._run_job,
            args=(job, roots),
            name=f"scan-{job_id}",
            daemon=True,
        )
        thread.start()
        return self.snapshot(job_id) or {}

    def get(self, job_id: str) -> dict[str, Any] | None:
        """读取任务快照。

        参数：
            job_id: 任务 id。
        返回值：
            任务快照；不存在时返回 None。
        """
        return self.snapshot(job_id)

    def cancel(self, job_id: str) -> dict[str, Any] | None:
        """请求取消扫描（线程会在下一个目录处退出）。

        参数：
            job_id: 任务 id。
        返回值：
            取消后的任务快照；不存在时返回 None。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            if job["status"] == "running":
                job["_cancel"] = True
        return self.snapshot(job_id)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        """生成对外可见的任务快照（去除内部字段）。

        参数：
            job_id: 任务 id。
        返回值：
            含进度、结果与耗时的字典；不存在时返回 None。
        """
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            data = {key: value for key, value in job.items() if not key.startswith("_")}
            data["items"] = list(job["items"])
            data["error_samples"] = list(job["error_samples"])
            data["elapsed_ms"] = int((time.time() - job["_started_perf"]) * 1000) if "_started_perf" in job else job["duration_ms"]
            return data

    def _prune_jobs(self) -> None:
        """清理超出保留数量的旧任务（调用方需持有锁）。

        参数：
            无。
        返回值：
            无。
        """
        if len(self._jobs) <= self._max_jobs:
            return
        finished = [jid for jid, job in self._jobs.items() if job["status"] != "running"]
        for jid in finished[: len(self._jobs) - self._max_jobs]:
            self._jobs.pop(jid, None)

    def _run_job(self, job: dict[str, Any], roots: list[Path]) -> None:
        """在后台线程中执行扫描并持续更新进度。

        参数：
            job: 任务字典（可写）。
            roots: 根目录列表。
        返回值：
            无。
        """
        started = time.perf_counter()
        job["_started_perf"] = started
        noise = {name.lower() for name in self.settings.scan_noise_dirs}
        visited: set[str] = set()
        stack: list[tuple[Path, int]] = [(root, 0) for root in reversed(roots)]
        managed: set[str] = job["_managed"]
        limit = int(job["limit"])
        max_depth = int(job["max_depth"])

        try:
            while stack:
                if job["_cancel"]:
                    job["status"] = "cancelled"
                    break
                current, depth = stack.pop()
                real = os.path.normcase(os.path.realpath(str(current)))
                if real in visited:
                    continue
                visited.add(real)
                job["scanned_dirs"] += 1

                if _is_git_worktree(current):
                    branch, detached = _read_branch(current)
                    item_path = normalize_repo_path(current)
                    item = {
                        "path": item_path,
                        "name": current.name or str(current),
                        "branch": branch,
                        "detached": detached,
                        "already_managed": repo_path_key(item_path) in managed,
                    }
                    job["items"].append(item)
                    job["found"] += 1
                    job["managed_count"] = sum(
                        1 for entry in job["items"] if entry["already_managed"]
                    )
                    if job["found"] >= limit:
                        job["truncated"] = True
                        job["status"] = "done"
                        break

                if depth >= max_depth:
                    continue
                try:
                    with os.scandir(current) as iterator:
                        entries = list(iterator)
                except (PermissionError, OSError) as exc:
                    if len(job["error_samples"]) < 5:
                        job["error_samples"].append(f"{current}: {exc}")
                    continue
                for entry in entries:
                    try:
                        if entry.is_symlink():
                            continue
                        if not entry.is_dir(follow_symlinks=False):
                            continue
                    except OSError:
                        continue
                    if entry.name.lower() in noise:
                        continue
                    stack.append((Path(entry.path), depth + 1))
            else:
                job["status"] = "done"
        except Exception as exc:  # 兜底：避免线程静默退出
            job["status"] = "error"
            job["error_samples"].append(f"扫描异常：{exc}")
        finally:
            job["finished_at"] = now_iso()
            job["duration_ms"] = int((time.perf_counter() - started) * 1000)
            job.pop("_managed", None)


def _is_git_worktree(path: Path) -> bool:
    """判断目录是否为 Git 工作仓库（支持 .git 目录与 .git 文件）。

    参数：
        path: 待判断目录。
    返回值：
        存在 .git 目录或 .git 文件时返回 True。
    """
    marker = path / ".git"
    return marker.is_dir() or marker.is_file()


def _read_branch(repo_path: Path) -> tuple[str | None, bool]:
    """只读解析当前分支（不调用 git 命令）。

    参数：
        repo_path: 仓库工作目录。
    返回值：
        (分支名或短提交号, 是否为 detached)；无法解析时返回 (None, False)。
    """
    marker = repo_path / ".git"
    git_dir: Path | None = None
    try:
        if marker.is_dir():
            git_dir = marker
        elif marker.is_file():
            content = marker.read_text(encoding="utf-8", errors="replace").strip()
            if content.lower().startswith("gitdir:"):
                target = content.split(":", 1)[1].strip()
                git_dir = Path(target)
                if not git_dir.is_absolute():
                    git_dir = (repo_path / git_dir).resolve()
    except (OSError, ValueError):
        return None, False
    if git_dir is None:
        return None, False
    try:
        head = (git_dir / "HEAD").read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None, False
    if head.startswith("ref:"):
        ref = head.split(":", 1)[1].strip()
        if ref.startswith("refs/heads/"):
            return ref[len("refs/heads/"):], False
        return ref.rsplit("/", 1)[-1], False
    if len(head) >= 7 and all(ch in "0123456789abcdefABCDEF" for ch in head):
        return head[:7], True
    return None, False
