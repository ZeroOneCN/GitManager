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

from .config import Settings, path_is_under
from .db import Database, now_iso
from .domain import map_git_error, parse_ahead_behind, parse_status_porcelain_z
from .git_runner import GitExecutor, GitResult
from .scanner import ScanManager
from .ssh_service import SshService

# 状态与提交相关命令统一关闭 quotepath，保证中文路径原样输出、可被前端匹配。
QUOTEPATH_ARGS = ["-c", "core.quotepath=false"]


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
    payload = {
        "output": (result.stderr or result.stdout or "").strip()[-1000:],
        "command": result.command,
        "exit_code": result.exit_code,
    }
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
        self.scanner = ScanManager(settings, database)
        self.ssh = SshService(settings, database, executor)

    # ------------------------------------------------------------------
    # 白名单根目录（SQLite 持久化，.env 仅作首次种子）
    # ------------------------------------------------------------------

    def allowed_root_paths(self) -> tuple[Path, ...]:
        """读取当前生效的白名单根目录。

        参数：
            无。
        返回值：
            已持久化根目录的 Path 元组（按添加顺序）。
        """
        return tuple(Path(item["path"]) for item in self.database.list_allowed_roots())

    def _path_allowed(self, path: Path) -> bool:
        """判断路径是否位于当前白名单内。

        参数：
            path: 已 resolve 的绝对路径。
        返回值：
            位于任一允许根目录时返回 True。
        """
        return path_is_under(path, self.allowed_root_paths())

    def list_allowed_roots(self) -> dict[str, Any]:
        """列出白名单根目录及其下受管仓库数量。

        参数：
            无。
        返回值：
            {"items": [{id, path, name, created_at, managed_repos}]}。
        """
        repos = self.database.list_repos()
        items: list[dict[str, Any]] = []
        for root in self.database.list_allowed_roots():
            root_path = Path(root["path"])
            managed = sum(
                1
                for repo in repos
                if path_is_under(Path(repo["path"]), (root_path,))
            )
            items.append(
                {
                    "id": root["id"],
                    "path": root["path"],
                    "name": root_path.name or root["path"],
                    "created_at": root["created_at"],
                    "managed_repos": managed,
                }
            )
        return {"items": items}

    def add_allowed_root(self, raw_path: str) -> dict[str, Any]:
        """新增白名单根目录（规范化、去尾部分隔符、大小写不敏感去重）。

        参数：
            raw_path: 用户提交的目录路径。
        返回值：
            {"item": {id, path, name, created_at, managed_repos}}。
        异常：
            ApiError: 路径非法、不存在或重复。
        """
        text = (raw_path or "").strip().strip('"')
        if not text:
            raise ApiError("invalid_root", "根目录路径不能为空。", 400)
        candidate = Path(os.path.expandvars(text)).expanduser()
        if not candidate.is_absolute():
            raise ApiError("invalid_root", "请填写绝对路径。", 400)
        try:
            resolved = candidate.resolve()
        except OSError as exc:
            raise ApiError("invalid_root", f"路径无法解析：{exc}", 400)
        if not resolved.is_dir():
            raise ApiError("root_not_found", "目录不存在或不是目录。", 400)
        existing = {
            os.path.normcase(item["path"])
            for item in self.database.list_allowed_roots()
        }
        if os.path.normcase(str(resolved)) in existing:
            raise ApiError("root_duplicate", "该根目录已在白名单中。", 409)
        try:
            root_id = self.database.insert_allowed_root(str(resolved))
        except sqlite3.IntegrityError:
            raise ApiError("root_duplicate", "该根目录已在白名单中。", 409)
        root = self.database.get_allowed_root(root_id) or {}
        return {
            "item": {
                "id": root.get("id", root_id),
                "path": root.get("path", str(resolved)),
                "name": resolved.name or str(resolved),
                "created_at": root.get("created_at"),
                "managed_repos": 0,
            },
            "message": "根目录已加入白名单。",
        }

    def remove_allowed_root(self, root_id: int, force: bool = False) -> dict[str, Any]:
        """移除白名单根目录；仍有受管仓库时需显式强制确认。

        参数：
            root_id: 根目录主键。
            force: 是否在存在受管仓库时仍然移除。
        返回值：
            {"ok": True, "removed": {...}, "affected_repos": n, "message": ...}。
        异常：
            ApiError: 根目录不存在；存在受管仓库且未确认。
        """
        root = self.database.get_allowed_root(root_id)
        if root is None:
            raise ApiError("root_not_found", "白名单根目录不存在。", 404)
        root_path = Path(root["path"])
        affected = [
            repo
            for repo in self.database.list_repos()
            if path_is_under(Path(repo["path"]), (root_path,))
        ]
        if affected and not force:
            raise ApiError(
                "root_in_use",
                f"该根目录下仍有 {len(affected)} 个受管仓库。"
                "移除后这些仓库将超出白名单而不可操作（仓库配置与磁盘数据不会自动删除）。",
                409,
                {
                    "count": len(affected),
                    "repos": [
                        {"id": repo["id"], "name": repo["name"], "path": repo["path"]}
                        for repo in affected[:10]
                    ],
                },
            )
        removed = self.database.delete_allowed_root(root_id)
        return {
            "ok": True,
            "removed": removed,
            "affected_repos": len(affected),
            "message": "根目录已从白名单移除；受管仓库仍保留在列表中但暂不可操作。"
            if affected
            else "根目录已从白名单移除。",
        }

    # ------------------------------------------------------------------
    # 仓库扫描
    # ------------------------------------------------------------------

    def start_scan(
        self, raw_roots: list[str] | None, max_depth: int | None, limit: int | None
    ) -> dict[str, Any]:
        """启动只读仓库扫描任务。

        参数：
            raw_roots: 指定根目录列表；为空时扫描全部白名单根目录。
            max_depth: 最大深度（默认取配置，限制 1-12）。
            limit: 结果上限（默认取配置，限制 1-500）。
        返回值：
            扫描任务快照（含 job_id 与进度）。
        异常：
            ApiError: 根目录为空、不存在或不在白名单内。
        """
        allowed = self.allowed_root_paths()
        if not allowed:
            raise ApiError("no_allowed_roots", "白名单为空，请先添加根目录。", 400)
        roots: list[Path] = []
        for raw in raw_roots or []:
            text = (raw or "").strip().strip('"')
            if not text:
                continue
            candidate = Path(os.path.expandvars(text)).expanduser()
            if not candidate.is_absolute():
                raise ApiError("invalid_scan_root", f"扫描根目录必须是绝对路径：{text}", 400)
            try:
                resolved = candidate.resolve()
            except OSError as exc:
                raise ApiError("invalid_scan_root", f"路径无法解析：{exc}", 400)
            if not resolved.is_dir():
                raise ApiError("scan_root_not_found", f"扫描根目录不存在：{resolved}", 400)
            if not path_is_under(resolved, allowed):
                raise ApiError(
                    "path_not_allowed",
                    f"扫描根目录不在白名单内，已拒绝：{resolved}",
                    400,
                )
            if resolved not in roots:
                roots.append(resolved)
        if not roots:
            roots = list(allowed)
        depth = max_depth if max_depth is not None else self.settings.scan_max_depth
        depth = max(1, min(int(depth), 12))
        cap = limit if limit is not None else self.settings.scan_result_limit
        cap = max(1, min(int(cap), 500))
        return self.scanner.start(roots, depth, cap)

    def get_scan(self, job_id: str) -> dict[str, Any]:
        """读取扫描任务进度。

        参数：
            job_id: 任务 id。
        返回值：
            任务快照。
        异常：
            ApiError: 任务不存在或已过期。
        """
        snapshot = self.scanner.get(job_id)
        if snapshot is None:
            raise ApiError("scan_not_found", "扫描任务不存在或已过期。", 404)
        return snapshot

    def cancel_scan(self, job_id: str) -> dict[str, Any]:
        """取消扫描任务。

        参数：
            job_id: 任务 id。
        返回值：
            任务快照。
        异常：
            ApiError: 任务不存在。
        """
        snapshot = self.scanner.cancel(job_id)
        if snapshot is None:
            raise ApiError("scan_not_found", "扫描任务不存在或已过期。", 404)
        return snapshot

    # ------------------------------------------------------------------
    # SSH 身份
    # ------------------------------------------------------------------

    def scan_ssh(self) -> dict[str, Any]:
        """扫描本机 SSH 私钥与 Host 别名（只读）。

        参数：
            无。
        返回值：
            扫描结果（不含私钥内容）。
        """
        return self.ssh.scan()

    def list_ssh_identities(self) -> dict[str, Any]:
        """列出已配置的 SSH 身份。

        参数：
            无。
        返回值：
            {"items": [...], "default": {...} | None}。
        """
        items = self.ssh.list_identities()
        default = next((item for item in items if item["is_default"]), None)
        return {"items": items, "default": default}

    def create_ssh_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建 SSH 身份配置。

        参数：
            payload: 身份字段。
        返回值：
            {"item": 身份字典}。
        异常：
            ApiError: 字段非法或重复。
        """
        try:
            item = self.ssh.create_identity(payload)
        except ValueError as exc:
            raise ApiError("invalid_ssh_identity", str(exc), 400)
        return {"item": item, "message": "SSH 身份已保存。"}

    def update_ssh_identity(self, identity_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """更新 SSH 身份配置。

        参数：
            identity_id: 身份主键。
            payload: 可部分更新字段。
        返回值：
            {"item": 身份字典}。
        异常：
            ApiError: 身份不存在或字段非法。
        """
        try:
            item = self.ssh.update_identity(identity_id, payload)
        except ValueError as exc:
            raise ApiError("invalid_ssh_identity", str(exc), 400)
        return {"item": item, "message": "SSH 身份已更新。"}

    def delete_ssh_identity(self, identity_id: int) -> dict[str, Any]:
        """删除 SSH 身份配置（不删除磁盘密钥）。

        参数：
            identity_id: 身份主键。
        返回值：
            {"ok": True, "removed": {...}, "message": ...}。
        异常：
            ApiError: 身份不存在。
        """
        try:
            item = self.ssh.delete_identity(identity_id)
        except ValueError as exc:
            raise ApiError("ssh_identity_not_found", str(exc), 404)
        return {
            "ok": True,
            "removed": item,
            "message": "身份配置已删除（磁盘私钥文件未做改动）。",
        }

    def set_default_ssh_identity(self, identity_id: int) -> dict[str, Any]:
        """设置全局默认 SSH 身份。

        参数：
            identity_id: 身份主键。
        返回值：
            {"item": 身份字典}。
        异常：
            ApiError: 身份不存在或已禁用。
        """
        try:
            item = self.ssh.set_default(identity_id)
        except ValueError as exc:
            raise ApiError("invalid_ssh_identity", str(exc), 400)
        return {"item": item, "message": "已设为全局默认身份。"}

    def bind_repo_ssh_identity(self, repo_id: int, identity_id: int | None) -> dict[str, Any]:
        """设置或清除仓库级 SSH 身份绑定。

        参数：
            repo_id: 仓库主键。
            identity_id: 身份 id；None 表示清除绑定。
        返回值：
            {"effective": 生效身份 | None, "message": ...}。
        异常：
            ApiError: 仓库/身份不存在或已禁用。
        """
        repo = self.get_repo(repo_id)
        try:
            effective = self.ssh.bind_repo(repo["id"], identity_id)
        except ValueError as exc:
            raise ApiError("invalid_ssh_identity", str(exc), 400)
        return {
            "effective": effective,
            "message": "仓库已绑定该 SSH 身份。" if identity_id is not None else "已清除仓库绑定，回落到全局默认。",
        }

    def test_ssh_identity(self, identity_id: int, host: str | None) -> dict[str, Any]:
        """测试 SSH 身份连接（BatchMode，不交互）。

        参数：
            identity_id: 身份主键。
            host: 可选覆盖的 Host 别名。
        返回值：
            测试结果。
        异常：
            ApiError: 身份不存在或缺少 Host。
        """
        try:
            return self.ssh.test_connection(identity_id, host)
        except ValueError as exc:
            raise ApiError("invalid_ssh_identity", str(exc), 400)

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
        if not self._path_allowed(path):
            raise ApiError(
                "path_not_allowed",
                f"仓库已不在白名单根目录内，操作已拒绝：{repo['path']}。"
                "请先在“允许的根目录”中添加对应根路径。",
                403,
                {"repo": repo},
            )
        return path

    def _network_identity(self, repo: dict[str, Any]) -> dict[str, Any] | None:
        """解析仓库网络操作应使用的 SSH 身份。

        参数：
            repo: 仓库字典。
        返回值：
            生效身份字典；无可用身份时返回 None（走系统默认 SSH 配置）。
        """
        return self.ssh.resolve_for_repo(int(repo["id"]))

    def add_repositories(self, raw_paths: list[str]) -> dict[str, Any]:
        """批量添加仓库（逐条校验，允许部分成功）。

        参数：
            raw_paths: 用户提交的路径字符串列表（最多 500 条）。
        返回值：
            {"results": [{path, ok, repo_id?, name?, error?, duplicate?}], "added": n}。
        """
        results: list[dict[str, Any]] = []
        added = 0
        allowed_roots = self.allowed_root_paths()
        for raw in raw_paths[:500]:
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
            if not path_is_under(resolved, allowed_roots):
                roots = "、".join(str(root) for root in allowed_roots)
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
            if not path_is_under(top_level, allowed_roots):
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
        items: list[dict[str, Any]] = []
        for root in self.database.list_allowed_roots():
            path = Path(root["path"])
            items.append(
                {
                    "id": root["id"],
                    "path": str(path),
                    "name": path.name or str(path),
                    "is_repo": self._is_git_dir(path),
                }
            )
        return items

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
        if not self._path_allowed(resolved):
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
            for root in self.allowed_root_paths()
        ):
            candidate_parent = resolved.parent
            if self._path_allowed(candidate_parent):
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
            [*QUOTEPATH_ARGS, "status", "--porcelain=v1", "-z", "--untracked-files=normal"],
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
            "dirty": {
                "total": 0,
                "staged": 0,
                "modified": 0,
                "deleted": 0,
                "untracked": 0,
                "conflicts": 0,
            },
            "files": {
                "staged": [],
                "modified": [],
                "deleted": [],
                "untracked": [],
                "conflicts": [],
            },
            "remotes": [],
            "last_commit": None,
            "last_fetch_at": None,
            "ssh_identity": None,
            "error": None,
            "checked_at": now_iso(),
        }
        base["ssh_identity"] = self._network_identity(repo)
        repo_path = Path(repo["path"])
        if not repo_path.is_dir():
            base["error"] = {
                "code": "repo_missing",
                "message": "仓库目录不存在（可能已被移动或删除）。",
            }
            return base
        if not self._path_allowed(repo_path):
            base["error"] = {
                "code": "path_not_allowed",
                "message": "仓库已不在白名单根目录内，状态读取已停止。",
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
            "deleted": parsed["deleted"],
            "untracked": parsed["untracked"],
            "conflicts": parsed["conflicts"],
        }
        base["dirty"] = {
            "total": parsed["counts"]["total"],
            "staged": parsed["counts"]["staged"],
            "modified": parsed["counts"]["modified"],
            "deleted": parsed["counts"]["deleted"],
            "untracked": parsed["counts"]["untracked"],
            "conflicts": parsed["counts"]["conflicts"],
        }

        base["remotes"] = self._read_remotes(repo)

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
            repo["id"],
            repo["path"],
            "fetch",
            ["fetch", "--prune"],
            ssh_identity=self._network_identity(repo),
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
            repo["id"],
            repo["path"],
            "pull",
            ["pull", "--ff-only"],
            ssh_identity=self._network_identity(repo),
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

    def _read_remotes(self, repo: dict[str, Any]) -> list[dict[str, str]]:
        """读取仓库远端名称与完整地址（不做截断，供前端完整展示）。

        参数：
            repo: 仓库字典。
        返回值：
            [{name, fetch_url, push_url}]；失败时返回空列表。
        """
        result = self.executor.run(
            repo["id"],
            repo["path"],
            "remote_list",
            [*QUOTEPATH_ARGS, "remote", "-v"],
        )
        if not result.ok:
            return []
        items: dict[str, dict[str, str]] = {}
        for line in result.stdout.splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            name, url, kind = parts[0], parts[1], parts[-1]
            entry = items.setdefault(
                name, {"name": name, "fetch_url": "", "push_url": ""}
            )
            if kind == "(fetch)":
                entry["fetch_url"] = url
            elif kind == "(push)":
                entry["push_url"] = url
            elif not entry["fetch_url"]:
                entry["fetch_url"] = url
        return list(items.values())

    def _list_remotes(self, repo: dict[str, Any]) -> list[str]:
        """读取仓库已配置的 remote 名称。

        参数：
            repo: 仓库字典。
        返回值：
            remote 名称列表；失败时返回空列表。
        """
        return [item["name"] for item in self._read_remotes(repo)]

    def get_remotes(self, repo_id: int) -> dict[str, Any]:
        """查询仓库已有的 remote 列表（用于推送面板设置上游）。

        参数：
            repo_id: 仓库主键。
        返回值：
            {"items": ["origin", ...], "remotes": [{name, fetch_url, push_url}]}。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        details = self._read_remotes(repo)
        return {
            "items": [item["name"] for item in details],
            "remotes": details,
        }

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
                ssh_identity=self._network_identity(repo),
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
                    ssh_identity=self._network_identity(repo),
                )
                action_text = f"已强制推送（--force-with-lease）{branch} → {upstream}"
                forced = True
            else:
                push_res = self.executor.run(
                    repo["id"],
                    repo["path"],
                    "push",
                    ["push"],
                    ssh_identity=self._network_identity(repo),
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
        files: list[dict[str, Any]] = []
        for group in ("staged", "modified", "deleted", "untracked", "conflicts"):
            for item in parsed[group]:
                files.append(
                    {
                        "path": item["path"],
                        "code": item["code"],
                        "group": group,
                        "old_path": item.get("old_path"),
                        "renamed": bool(item.get("renamed")),
                    }
                )
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
        for group in ("staged", "modified", "deleted", "untracked"):
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

        # 必须使用 git add -A：已删除/重命名文件只有 -A 才能正确进入暂存区
        add_res = self.executor.run(
            repo["id"],
            repo["path"],
            "stage",
            [*QUOTEPATH_ARGS, "add", "-A", "--", *selected],
        )
        if not add_res.ok:
            raise ApiError(
                "stage_failed",
                f"暂存失败（git 退出码 {add_res.exit_code}）：{add_res.command}\n"
                f"{(add_res.stderr or add_res.stdout).strip()}",
                409,
                {
                    "command": add_res.command,
                    "exit_code": add_res.exit_code,
                    "output": (add_res.stderr or add_res.stdout).strip()[-1000:],
                },
            )

        commit_res = self.executor.run(
            repo["id"],
            repo["path"],
            "commit",
            [*QUOTEPATH_ARGS, "commit", "-m", clean_message, "--", *selected],
        )
        if not commit_res.ok:
            raise ApiError(
                "commit_failed",
                f"提交失败（git 退出码 {commit_res.exit_code}）：{commit_res.command}\n"
                f"{(commit_res.stderr or commit_res.stdout).strip()}",
                409,
                {
                    "command": commit_res.command,
                    "exit_code": commit_res.exit_code,
                    "output": (commit_res.stderr or commit_res.stdout).strip()[-1000:],
                },
            )

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
        page: int,
        page_size: int,
        repo_id: int | None,
        action: str | None,
        only_failed: bool,
    ) -> dict[str, Any]:
        """按页查询审计日志（服务端分页，每页条数固定由 page_size 控制）。

        参数：
            page: 页码（从 1 开始；越界时自动收敛到最后页）。
            page_size: 每页条数（1-50）。
            repo_id: 可选仓库过滤。
            action: 可选动作过滤。
            only_failed: 是否只看失败。
        返回值：
            {
              total, total_pages, page, page_size, items,
              "from"/"to" 为当前页 1-based 记录区间（无记录时为 0）
            }。
        """
        page_size = max(1, min(int(page_size), 50))
        total, _ = self.database.query_audit(
            limit=1,
            offset=0,
            repo_id=repo_id,
            action=action,
            only_failed=only_failed,
        )
        total_pages = (total + page_size - 1) // page_size
        if total_pages == 0:
            current = 1
        else:
            current = max(1, min(int(page), total_pages))
        offset = (current - 1) * page_size
        _, items = self.database.query_audit(
            limit=page_size,
            offset=offset,
            repo_id=repo_id,
            action=action,
            only_failed=only_failed,
        )
        return {
            "total": total,
            "total_pages": total_pages,
            "page": current,
            "page_size": page_size,
            "from": offset + 1 if items else 0,
            "to": offset + len(items) if items else 0,
            "items": items,
        }

    def query_audit_legacy(
        self,
        limit: int,
        offset: int,
        repo_id: int | None,
        action: str | None,
        only_failed: bool,
    ) -> dict[str, Any]:
        """兼容旧版 limit/offset 查询（供既有脚本与旧调用方使用）。

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
