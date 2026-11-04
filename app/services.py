"""业务服务层：仓库管理、体检聚合、pull / push / commit 编排。

设计约束：
- 所有 Git 操作一律通过 GitExecutor（自动审计），本层不直接调用 subprocess。
- 仓库路径只来自数据库记录或经过白名单校验的输入，不接受请求中的任意 cwd。
- 破坏性能力（删除目录、reset --hard、批量强推）不提供。
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database, now_iso
from .domain import map_git_error, parse_ahead_behind, parse_status_porcelain_z
from .git_runner import GitExecutor, GitResult


class ApiError(Exception):
    """业务错误：携带错误码、中文原因、HTTP 状态码与附加字段。"""

    def __init__(
        self,
        code: str,
        message: str,
        status_code: int = 409,
        extra: dict[str, Any] | None = None,
    ) -> None:
        """构造业务错误。

        参数：
            code: 机器可读错误码。
            message: 面向用户的中文原因。
            status_code: 建议的 HTTP 状态码。
            extra: 附加到响应体的字段（如 stash 信息、needs_upstream）。
        返回值：
            无。
        """
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.extra = extra or {}


def _error_from_result(
    action: str,
    result: GitResult,
    status_code: int = 409,
    extra: dict[str, Any] | None = None,
) -> ApiError:
    """根据失败结果构造 ApiError。

    参数：
        action: 动作名，用于错误映射与提示。
        result: 失败的 git 结果。
        status_code: 默认 HTTP 状态码（网络错误会自动改为 502）。
        extra: 附加响应字段。
    返回值：
        构造好的 ApiError 实例。
    """
    code, message = map_git_error(
        action, result.stderr, result.exit_code, result.timed_out
    )
    if code == "network_error":
        status_code = 502
    payload = {"output": (result.stderr or result.stdout or "").strip()[-1000:]}
    if extra:
        payload.update(extra)
    return ApiError(code, message, status_code, payload)


def _iso_from_mtime(path: Path) -> str | None:
    """把文件修改时间转换为带时区的 ISO 字符串。

    参数：
        path: 文件路径。
    返回值：
        ISO-8601 字符串；文件不存在或不可读时返回 None。
    """
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp).astimezone().isoformat(timespec="seconds")


class GitManagerService:
    """GitManager 业务服务。

    参数：
        settings: 运行配置。
        database: SQLite 访问对象。
        executor: Git 执行器（自动审计）。
    """

    def __init__(
        self, settings: Settings, database: Database, executor: GitExecutor
    ) -> None:
        """初始化服务。

        参数：
            settings: 运行配置。
            database: 数据库对象。
            executor: Git 执行器。
        返回值：
            无。
        """
        self.settings = settings
        self.database = database
        self.executor = executor

    # ------------------------------------------------------------------
    # 仓库管理
    # ------------------------------------------------------------------

    def list_repositories(self) -> list[dict[str, Any]]:
        """列出已配置仓库。

        参数：
            无。
        返回值：
            仓库字典列表。
        """
        return self.database.list_repos()

    def get_repo(self, repo_id: int) -> dict[str, Any]:
        """按 id 获取仓库，不存在时抛出 404。

        参数：
            repo_id: 仓库主键。
        返回值：
            仓库字典。
        """
        repo = self.database.get_repo(repo_id)
        if repo is None:
            raise ApiError("repo_not_found", "仓库不存在或已被移除。", 404)
        return repo

    def _ensure_repo_dir(self, repo: dict[str, Any]) -> Path:
        """确认仓库目录仍然存在。

        参数：
            repo: 仓库字典。
        返回值：
            仓库目录 Path。
        异常：
            ApiError: 目录不存在时抛出 repo_missing。
        """
        path = Path(repo["path"])
        if not path.is_dir():
            raise ApiError(
                "repo_missing",
                f"仓库目录不存在：{repo['path']}（可能已被移动或删除）。",
                404,
                {"repo": repo},
            )
        return path

    def add_repositories(self, raw_paths: list[str]) -> dict[str, Any]:
        """批量添加仓库（逐条校验，允许部分成功）。

        参数：
            raw_paths: 用户提交的路径字符串列表（最多 50 条）。
        返回值：
            {"results": [{path, ok, repo_id?, name?, error?, duplicate?}], "added": n}。
        """
        results: list[dict[str, Any]] = []
        added = 0
        for raw in raw_paths[:50]:
            text = (raw or "").strip().strip('"')
            if not text:
                results.append(
                    {"path": raw, "ok": False, "error": "路径为空，已跳过。"}
                )
                continue

            candidate = Path(text).expanduser()
            if not candidate.is_absolute():
                results.append(
                    {
                        "path": text,
                        "ok": False,
                        "error": "请填写绝对路径（例如 E:\\Code\\MyRepo）。",
                    }
                )
                continue
            try:
                resolved = candidate.resolve()
            except OSError as exc:
                results.append(
                    {"path": text, "ok": False, "error": f"路径无法解析：{exc}"}
                )
                continue
            if not resolved.is_dir():
                results.append(
                    {"path": text, "ok": False, "error": "目录不存在或不是目录。"}
                )
                continue
            if not self.settings.is_path_allowed(resolved):
                roots = "、".join(str(root) for root in self.settings.allowed_roots)
                results.append(
                    {
                        "path": str(resolved),
                        "ok": False,
                        "error": f"路径不在白名单根目录内（允许：{roots}）。",
                    }
                )
                continue

            detect = self.executor.run(
                None, resolved, "detect_repo", ["rev-parse", "--show-toplevel"]
            )
            if not detect.ok:
                results.append(
                    {
                        "path": str(resolved),
                        "ok": False,
                        "error": "不是 Git 仓库（未找到 .git 或无法解析仓库根目录）。",
                    }
                )
                continue

            top_level = Path(detect.stdout.strip()).resolve()
            if not self.settings.is_path_allowed(top_level):
                results.append(
                    {
                        "path": str(resolved),
                        "ok": False,
                        "error": "仓库根目录位于白名单之外，已拒绝添加。",
                    }
                )
                continue
            if not top_level.is_dir():
                results.append(
                    {"path": str(resolved), "ok": False, "error": "仓库根目录无效。"}
                )
                continue

            bare = self.executor.run(
                None, top_level, "detect_bare", ["rev-parse", "--is-bare-repository"]
            )
            if bare.ok and bare.stdout.strip().lower() == "true":
                results.append(
                    {
                        "path": str(top_level),
                        "ok": False,
                        "error": "暂不支持裸仓库（bare repository）。",
                    }
                )
                continue

            existing = self.database.find_repo_by_path(str(top_level))
            if existing:
                results.append(
                    {
                        "path": str(top_level),
                        "ok": True,
                        "duplicate": True,
                        "repo_id": existing["id"],
                        "name": existing["name"],
                        "message": "该仓库已在列表中。",
                    }
                )
                continue

            try:
                repo_id = self.database.insert_repo(str(top_level), top_level.name)
            except sqlite3.IntegrityError:
                results.append(
                    {
                        "path": str(top_level),
                        "ok": False,
                        "error": "该路径已存在（并发重复添加）。",
                    }
                )
                continue

            added += 1
            results.append(
                {
                    "path": str(top_level),
                    "ok": True,
                    "repo_id": repo_id,
                    "name": top_level.name,
                    "message": "添加成功。",
                }
            )
        return {"results": results, "added": added}

    def remove_repository(self, repo_id: int) -> dict[str, Any]:
        """移除仓库配置（绝不删除磁盘目录）。

        参数：
            repo_id: 仓库主键。
        返回值：
            {"ok": True, "removed": {id, name, path}}。
        """
        removed = self.database.delete_repo(repo_id)
        if removed is None:
            raise ApiError("repo_not_found", "仓库不存在或已被移除。", 404)
        return {
            "ok": True,
            "removed": {
                "id": removed["id"],
                "name": removed["name"],
                "path": removed["path"],
            },
            "message": "已从列表移除（磁盘目录未做任何改动）。",
        }

    # ------------------------------------------------------------------
    # 目录浏览（白名单内的多选支持）
    # ------------------------------------------------------------------

    def browse_roots(self) -> list[dict[str, Any]]:
        """列出白名单根目录。

        参数：
            无。
        返回值：
            [{path, name, is_repo}]。
        """
        return [
            {"path": str(root), "name": root.name or str(root), "is_repo": self._is_git_dir(root)}
            for root in self.settings.allowed_roots
        ]

    def _is_git_dir(self, path: Path) -> bool:
        """判断目录是否直接包含 .git（普通仓库）。

        参数：
            path: 目录路径。
        返回值：
            存在 .git 条目时返回 True。
        """
        try:
            return (path / ".git").exists()
        except OSError:
            return False

    def browse_dirs(self, raw_path: str | None) -> dict[str, Any]:
        """浏览白名单根目录内的子目录（只返回目录，用于前端多选）。

        参数：
            raw_path: 目标目录；为空时返回根目录列表。
        返回值：
            {
              "roots": [{path,name,is_repo}]（仅空路径时）,
              "path": 当前目录或 None,
              "parent": 上级目录或 None,
              "is_repo": 当前目录是否仓库,
              "entries": [{name,path,is_repo,is_dir}]
            }。
        """
        roots = self.browse_roots()
        text = (raw_path or "").strip()
        if not text:
            return {
                "roots": roots,
                "path": None,
                "parent": None,
                "is_repo": False,
                "entries": [],
            }

        resolved = Path(text).expanduser().resolve()
        if not self.settings.is_path_allowed(resolved):
            raise ApiError(
                "path_not_allowed",
                "该路径不在白名单根目录内，无法浏览。",
                400,
            )
        if not resolved.is_dir():
            raise ApiError("path_not_found", "目录不存在。", 400)

        parent: str | None = None
        if not any(
            os.path.normcase(str(resolved)) == os.path.normcase(str(root))
            for root in self.settings.allowed_roots
        ):
            candidate_parent = resolved.parent
            if self.settings.is_path_allowed(candidate_parent):
                parent = str(candidate_parent)

        entries: list[dict[str, Any]] = []
        try:
            children = sorted(
                (child for child in resolved.iterdir() if child.is_dir()),
                key=lambda item: item.name.lower(),
            )
        except OSError as exc:
            raise ApiError("path_unreadable", f"目录无法读取：{exc}", 400)

        for child in children[:500]:
            if child.name.startswith("."):
                continue
            entries.append(
                {
                    "name": child.name,
                    "path": str(child),
                    "is_dir": True,
                    "is_repo": self._is_git_dir(child),
                }
            )

        return {
            "roots": roots,
            "path": str(resolved),
            "parent": parent,
            "is_repo": self._is_git_dir(resolved),
            "entries": entries,
        }

    # ------------------------------------------------------------------
    # 体检状态
    # ------------------------------------------------------------------

    def _run_status(self, repo: dict[str, Any]) -> GitResult:
        """执行 git status（porcelain -z）。

        参数：
            repo: 仓库字典。
        返回值：
            GitResult（stdout 为 NUL 分隔的状态文本）。
        """
        return self.executor.run(
            repo["id"],
            repo["path"],
            "status",
            ["status", "--porcelain=v1", "-z", "--untracked-files=normal"],
        )

    def _collect_dirty(self, repo: dict[str, Any]) -> dict[str, Any]:
        """仅采集脏文件信息。

        参数：
            repo: 仓库字典。
        返回值：
            parse_status_porcelain_z 的结构。
        异常：
            ApiError: git status 失败时抛出。
        """
        result = self._run_status(repo)
        if not result.ok:
            raise _error_from_result("status", result)
        return parse_status_porcelain_z(result.stdout)

    def _read_branch(self, repo: dict[str, Any]) -> tuple[str | None, bool, str | None]:
        """读取当前分支、是否 detached、HEAD 短哈希。

        参数：
            repo: 仓库字典。
        返回值：
            (branch, detached, head_sha)；无提交或读取失败时相应字段为 None。
        """
        branch_res = self.executor.run(
            repo["id"], repo["path"], "branch", ["branch", "--show-current"]
        )
        branch = branch_res.stdout.strip() if branch_res.ok else ""
        sha_res = self.executor.run(
            repo["id"], repo["path"], "head", ["rev-parse", "--short", "HEAD"]
        )
        head_sha = sha_res.stdout.strip() if sha_res.ok else None
        branch_value = branch or None
        detached = branch_value is None and head_sha is not None
        return branch_value, detached, head_sha

    def _read_upstream(self, repo: dict[str, Any]) -> tuple[str | None, dict[str, int] | None]:
        """读取当前分支 upstream 与 ahead/behind。

        参数：
            repo: 仓库字典。
        返回值：
            (upstream 名称或 None, {"ahead","behind"} 或 None)。
        """
        upstream_res = self.executor.run(
            repo["id"],
            repo["path"],
            "upstream",
            ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        )
        upstream = upstream_res.stdout.strip() if upstream_res.ok else ""
        if not upstream:
            return None, None
        ab_res = self.executor.run(
            repo["id"],
            repo["path"],
            "ahead_behind",
            ["rev-list", "--left-right", "--count", "HEAD...@{u}"],
        )
        if not ab_res.ok:
            return upstream, None
        return upstream, parse_ahead_behind(ab_res.stdout)

    def _read_last_commit(self, repo: dict[str, Any]) -> dict[str, str] | None:
        """读取最近一次提交信息。

        参数：
            repo: 仓库字典。
        返回值：
            {sha, short, author, date, subject}；无提交时返回 None。
        """
        result = self.executor.run(
            repo["id"],
            repo["path"],
            "last_commit",
            ["log", "-1", "--format=%H%x1f%an%x1f%ad%x1f%s", "--date=iso-strict"],
        )
        if not result.ok:
            return None
        parts = result.stdout.strip().split("\x1f")
        if len(parts) < 4:
            return None
        sha, author, date, subject = parts[0], parts[1], parts[2], "\x1f".join(parts[3:])
        return {
            "sha": sha,
            "short": sha[:7],
            "author": author,
            "date": date,
            "subject": subject,
        }

    def _read_last_fetch(self, repo: dict[str, Any]) -> str | None:
        """读取最后一次 fetch 时间（FETCH_HEAD 文件修改时间）。

        参数：
            repo: 仓库字典。
        返回值：
            带时区 ISO 字符串；从未 fetch 或读取失败时返回 None。
        """
        git_dir_res = self.executor.run(
            repo["id"], repo["path"], "git_dir", ["rev-parse", "--git-dir"]
        )
        if not git_dir_res.ok:
            return None
        git_dir = Path(git_dir_res.stdout.strip())
        if not git_dir.is_absolute():
            git_dir = Path(repo["path"]) / git_dir
        return _iso_from_mtime(git_dir / "FETCH_HEAD")

    def collect_status(self, repo: dict[str, Any]) -> dict[str, Any]:
        """采集单个仓库的完整体检状态。

        参数：
            repo: 仓库字典。
        返回值：
            符合 DESIGN 5.4 的状态字典；仓库不可用时 error 字段非空。
        """
        base: dict[str, Any] = {
            "repo": {"id": repo["id"], "name": repo["name"], "path": repo["path"]},
            "branch": None,
            "detached": False,
            "head_sha": None,
            "upstream": None,
            "upstream_status": None,
            "dirty": {"total": 0, "staged": 0, "modified": 0, "untracked": 0, "conflicts": 0},
            "files": {"staged": [], "modified": [], "untracked": [], "conflicts": []},
            "last_commit": None,
            "last_fetch_at": None,
            "error": None,
            "checked_at": now_iso(),
        }
        repo_path = Path(repo["path"])
        if not repo_path.is_dir():
            base["error"] = {
                "code": "repo_missing",
                "message": "仓库目录不存在（可能已被移动或删除）。",
            }
            return base

        status_result = self._run_status(repo)
        if not status_result.ok:
            code, message = map_git_error(
                "status", status_result.stderr, status_result.exit_code, status_result.timed_out
            )
            base["error"] = {"code": code, "message": message}
            return base

        parsed = parse_status_porcelain_z(status_result.stdout)
        base["files"] = {
            "staged": parsed["staged"],
            "modified": parsed["modified"],
            "untracked": parsed["untracked"],
            "conflicts": parsed["conflicts"],
        }
        base["dirty"] = {
            "total": parsed["counts"]["total"],
            "staged": parsed["counts"]["staged"],
            "modified": parsed["counts"]["modified"],
            "untracked": parsed["counts"]["untracked"],
            "conflicts": parsed["counts"]["conflicts"],
        }

        branch, detached, head_sha = self._read_branch(repo)
        base["branch"] = branch
        base["detached"] = detached
        base["head_sha"] = head_sha

        upstream, ahead_behind = self._read_upstream(repo)
        base["upstream"] = upstream
        base["upstream_status"] = ahead_behind
        base["last_commit"] = self._read_last_commit(repo)
        base["last_fetch_at"] = self._read_last_fetch(repo)
        return base

    def collect_status_by_id(self, repo_id: int) -> dict[str, Any]:
        """按仓库 id 采集体检状态。

        参数：
            repo_id: 仓库主键。
        返回值：
            状态字典。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        return self.collect_status(repo)

    async def collect_all_status(self) -> list[dict[str, Any]]:
        """并发采集全部仓库状态（全局刷新）。

        参数：
            无。
        返回值：
            状态字典列表（单仓失败不影响其他）。
        """
        repos = self.list_repositories()
        if not repos:
            return []
        tasks = [asyncio.to_thread(self._safe_status, repo) for repo in repos]
        return list(await asyncio.gather(*tasks))

    def _safe_status(self, repo: dict[str, Any]) -> dict[str, Any]:
        """采集单个仓库状态并吞掉异常（供并发调用）。

        参数：
            repo: 仓库字典。
        返回值：
            状态字典；异常时返回带 error 的基础结构。
        """
        try:
            return self.collect_status(repo)
        except Exception as exc:  # 保证全局刷新不因单仓异常而中断
            return {
                "repo": {"id": repo["id"], "name": repo["name"], "path": repo["path"]},
                "error": {"code": "internal_error", "message": f"状态采集失败：{exc}"},
                "checked_at": now_iso(),
            }

    def fetch_repository(self, repo_id: int) -> dict[str, Any]:
        """执行 git fetch --prune 并返回最新状态。

        参数：
            repo_id: 仓库主键。
        返回值：
            {"ok": True, "message": ..., "status": 状态字典}。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        result = self.executor.run(
            repo["id"], repo["path"], "fetch", ["fetch", "--prune"]
        )
        if not result.ok:
            raise _error_from_result("fetch", result, status_code=502)
        return {
            "ok": True,
            "message": "已获取远端最新信息。",
            "status": self.collect_status(repo),
            "output": (result.stderr or result.stdout).strip()[-1000:],
        }

    # ------------------------------------------------------------------
    # pull
    # ------------------------------------------------------------------

    def pull_repository(self, repo_id: int, auto_stash: bool) -> dict[str, Any]:
        """执行 ff-only 拉取，可选自动 stash 并在成功后恢复。

        参数：
            repo_id: 仓库主键。
            auto_stash: 是否在脏工作区时自动 stash（必须由前端显式确认）。
        返回值：
            {"ok": True, "message": ..., "stash": {...}, "status": 状态字典}。
        异常：
            ApiError: 脏且未选 stash / 无 upstream / 无法快进 / stash 失败等。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)

        dirty = self._collect_dirty(repo)
        dirty_total = dirty["counts"]["total"]
        stash_used = False
        stash_message = ""

        if dirty_total > 0 and not auto_stash:
            raise ApiError(
                "dirty_worktree",
                f"工作区存在 {dirty_total} 个未提交改动，已停止 pull。"
                "请先提交，或勾选“pull 前自动 stash”后重试。",
                409,
                {"dirty": dirty["counts"]},
            )

        if dirty_total > 0 and auto_stash:
            stash_message = f"GitManager auto stash {now_iso()}"
            stash_res = self.executor.run(
                repo["id"],
                repo["path"],
                "stash_push",
                ["stash", "push", "-u", "-m", stash_message],
            )
            if not stash_res.ok:
                raise _error_from_result("stash", stash_res, extra={"dirty": dirty["counts"]})
            stash_used = True

        pull_res = self.executor.run(
            repo["id"], repo["path"], "pull", ["pull", "--ff-only"]
        )
        stash_state = {
            "used": stash_used,
            "popped": False,
            "kept": stash_used,
            "message": stash_message,
        }

        if not pull_res.ok:
            error = _error_from_result(
                "pull",
                pull_res,
                extra={"stash": stash_state, "dirty": dirty["counts"]},
            )
            if stash_used:
                error.message += (
                    "；自动 stash 已保留（未恢复），请确认后手动执行 git stash list / pop。"
                )
            raise error

        if stash_used:
            pop_res = self.executor.run(
                repo["id"], repo["path"], "stash_pop", ["stash", "pop"]
            )
            if not pop_res.ok:
                raise ApiError(
                    "stash_pop_conflict",
                    "pull 已成功，但恢复自动 stash 时发生冲突。"
                    "stash 已保留，请在终端手动处理后再继续。",
                    409,
                    {
                        "stash": {
                            "used": True,
                            "popped": False,
                            "kept": True,
                            "message": stash_message,
                        },
                        "output": (pop_res.stderr or pop_res.stdout).strip()[-1000:],
                    },
                )
            stash_state["popped"] = True
            stash_state["kept"] = False

        return {
            "ok": True,
            "message": "pull 成功（ff-only 快进）。",
            "stash": stash_state,
            "status": self.collect_status(repo),
            "output": (pull_res.stderr or pull_res.stdout).strip()[-1000:],
        }

    # ------------------------------------------------------------------
    # push
    # ------------------------------------------------------------------

    def _list_remotes(self, repo: dict[str, Any]) -> list[str]:
        """读取仓库已配置的 remote 名称。

        参数：
            repo: 仓库字典。
        返回值：
            remote 名称列表；失败时返回空列表。
        """
        result = self.executor.run(repo["id"], repo["path"], "remote_list", ["remote"])
        if not result.ok:
            return []
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def get_remotes(self, repo_id: int) -> dict[str, Any]:
        """查询仓库已有的 remote 列表（用于推送面板设置上游）。

        参数：
            repo_id: 仓库主键。
        返回值：
            {"items": ["origin", ...]}。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        return {"items": self._list_remotes(repo)}

    def _record_blocked_force(self, repo: dict[str, Any], reason: str) -> None:
        """记录被拒绝的强制推送请求（未执行任何 git 命令）。

        参数：
            repo: 仓库字典。
            reason: 拒绝原因。
        返回值：
            无。
        """
        try:
            self.database.insert_audit(
                {
                    "ts": now_iso(),
                    "repo_id": repo["id"],
                    "repo_path": repo["path"],
                    "action": "push_force_blocked",
                    "command": "git push --force-with-lease（请求被拒绝）",
                    "exit_code": -1,
                    "ok": False,
                    "duration_ms": 0,
                    "forced": True,
                    "output_summary": reason,
                }
            )
        except Exception:
            pass

    def push_repository(
        self,
        repo_id: int,
        set_upstream: bool,
        remote: str | None,
        force: bool,
        confirm_force: bool,
    ) -> dict[str, Any]:
        """推送当前分支到 upstream（可选设置 upstream；强推需二次确认）。

        参数：
            repo_id: 仓库主键。
            set_upstream: 无 upstream 时是否设置上游。
            remote: 设置 upstream 时使用的 remote 名称。
            force: 是否请求强制推送。
            confirm_force: 强制推送的二次确认标记。
        返回值：
            {"ok": True, "message", "forced", "status", "output"}。
        异常：
            ApiError: 无 upstream / 强制未确认 / remote 无效 / 推送失败。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)

        branch, detached, _ = self._read_branch(repo)
        if detached or not branch:
            raise ApiError(
                "detached_head",
                "当前处于 detached HEAD 状态，无法确定要推送的分支。",
                409,
            )

        upstream, _ = self._read_upstream(repo)
        remotes = self._list_remotes(repo)

        if not upstream:
            if force:
                raise ApiError(
                    "no_upstream",
                    "当前分支没有 upstream，无法执行强制推送。请先设置上游分支。",
                    409,
                    {"branch": branch, "remotes": remotes},
                )
            if not set_upstream:
                raise ApiError(
                    "needs_upstream",
                    f"当前分支 {branch} 未设置 upstream，无法推送。"
                    "请选择目标 remote 后设置上游分支。",
                    409,
                    {"branch": branch, "remotes": remotes},
                )
            remote_name = (remote or "").strip()
            if not remote_name or remote_name not in remotes:
                raise ApiError(
                    "invalid_remote",
                    "请从仓库已有的 remote 中选择有效的目标远端。",
                    400,
                    {"branch": branch, "remotes": remotes},
                )
            push_res = self.executor.run(
                repo["id"],
                repo["path"],
                "push_set_upstream",
                ["push", "--set-upstream", remote_name, branch],
            )
            action_text = f"已设置 upstream 并推送 {branch} → {remote_name}/{branch}"
            forced = False
        else:
            if force and not confirm_force:
                self._record_blocked_force(repo, "强制推送缺少二次确认，请求被拒绝。")
                raise ApiError(
                    "force_not_confirmed",
                    "强制推送需要二次确认（confirm_force=true）。本次请求未执行。",
                    400,
                )
            if force:
                push_res = self.executor.run(
                    repo["id"],
                    repo["path"],
                    "push_force",
                    ["push", "--force-with-lease"],
                    forced=True,
                )
                action_text = f"已强制推送（--force-with-lease）{branch} → {upstream}"
                forced = True
            else:
                push_res = self.executor.run(
                    repo["id"], repo["path"], "push", ["push"]
                )
                action_text = f"已推送 {branch} → {upstream}"
                forced = False

        if not push_res.ok:
            raise _error_from_result(
                "push",
                push_res,
                extra={"branch": branch, "upstream": upstream, "remotes": remotes},
            )

        return {
            "ok": True,
            "message": f"推送成功：{action_text}。",
            "forced": forced,
            "branch": branch,
            "upstream": upstream,
            "status": self.collect_status(repo),
            "output": (push_res.stderr or push_res.stdout).strip()[-1000:],
        }

    # ------------------------------------------------------------------
    # 提交
    # ------------------------------------------------------------------

    def list_changes(self, repo_id: int) -> dict[str, Any]:
        """列出工作区变更（用于提交面板）。

        参数：
            repo_id: 仓库主键。
        返回值：
            {
              "files": [{path, code, group}],
              "conflicts": [{path, code}],
              "counts": {...}
            }。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        parsed = self._collect_dirty(repo)
        files: list[dict[str, str]] = []
        for group in ("staged", "modified", "untracked", "conflicts"):
            for item in parsed[group]:
                files.append({"path": item["path"], "code": item["code"], "group": group})
        return {
            "files": files,
            "conflicts": parsed["conflicts"],
            "counts": parsed["counts"],
        }

    def commit_repository(
        self, repo_id: int, files: list[str], message: str
    ) -> dict[str, Any]:
        """仅提交用户勾选的文件。

        参数：
            repo_id: 仓库主键。
            files: 勾选的文件路径（必须属于当前脏文件集合）。
            message: 提交信息（去除空白后不能为空）。
        返回值：
            {"ok": True, "commit": {sha, short, subject}, "output"}。
        异常：
            ApiError: 无文件 / 有冲突 / 文件不在变更集 / git 失败。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)

        clean_message = (message or "").strip()
        if not clean_message:
            raise ApiError("empty_message", "提交信息不能为空。", 400)
        if len(clean_message) > 5000:
            raise ApiError("message_too_long", "提交信息过长（上限 5000 字）。", 400)
        if not files:
            raise ApiError("no_files", "请至少勾选一个文件。", 400)

        parsed = self._collect_dirty(repo)
        if parsed["conflicts"]:
            raise ApiError(
                "has_conflicts",
                "工作区存在未解决的冲突文件，请先手动解决后再提交。",
                409,
                {"conflicts": parsed["conflicts"]},
            )

        # 建立脏文件路径映射（Windows 下忽略大小写），防止路径注入
        allowed: dict[str, str] = {}
        for group in ("staged", "modified", "untracked"):
            for item in parsed[group]:
                allowed[os.path.normcase(item["path"])] = item["path"]

        selected: list[str] = []
        unknown: list[str] = []
        for raw in files:
            key = os.path.normcase((raw or "").replace("/", "\\"))
            real = allowed.get(key)
            if real is None:
                unknown.append(raw)
            elif real not in selected:
                selected.append(real)
        if unknown:
            raise ApiError(
                "file_not_dirty",
                f"以下文件不在当前变更集合中，已拒绝提交：{'、'.join(unknown[:10])}",
                400,
                {"unknown": unknown},
            )
        if not selected:
            raise ApiError("no_files", "请至少勾选一个有效变更文件。", 400)

        add_res = self.executor.run(
            repo["id"], repo["path"], "stage", ["add", "--", *selected]
        )
        if not add_res.ok:
            raise _error_from_result("stage", add_res)

        commit_res = self.executor.run(
            repo["id"],
            repo["path"],
            "commit",
            ["commit", "-m", clean_message, "--", *selected],
        )
        if not commit_res.ok:
            lowered = (commit_res.stderr or "").lower()
            if "nothing to commit" in lowered or "no changes added" in lowered:
                raise ApiError("no_changes", "所选文件没有可提交的变更。", 409)
            raise _error_from_result("commit", commit_res)

        last = self._read_last_commit(repo)
        if last is None:
            raise ApiError(
                "commit_unreadable",
                "提交已执行，但读取新提交信息失败，请到终端确认。",
                500,
            )
        return {
            "ok": True,
            "message": "提交成功。",
            "commit": {
                "sha": last["sha"],
                "short": last["short"],
                "subject": last["subject"],
            },
            "output": (commit_res.stderr or commit_res.stdout).strip()[-1000:],
        }

    # ------------------------------------------------------------------
    # 审计查询
    # ------------------------------------------------------------------

    def query_audit(
        self,
        limit: int,
        offset: int,
        repo_id: int | None,
        action: str | None,
        only_failed: bool,
    ) -> dict[str, Any]:
        """查询审计日志。

        参数：
            limit: 每页条数。
            offset: 偏移量。
            repo_id: 可选仓库过滤。
            action: 可选动作过滤。
            only_failed: 是否只看失败。
        返回值：
            {"total": int, "items": [...]}。
        """
        total, items = self.database.query_audit(
            limit=limit,
            offset=offset,
            repo_id=repo_id,
            action=action,
            only_failed=only_failed,
        )
        return {"total": total, "items": items}
