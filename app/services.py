"""业务服务层：仓库管理、体检聚合、pull / push / commit 编排。

设计约束：
- 所有 Git 操作一律通过 GitExecutor（自动审计），本层不直接调用 subprocess。
- 仓库路径只来自数据库记录或经过白名单校验的输入，不接受请求中的任意 cwd。
- 破坏性能力（删除目录、reset --hard、批量强推）不提供。
"""

from __future__ import annotations

import asyncio
import fnmatch
import os
import re
import shutil
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from .config import Settings, path_is_under
from .db import Database, now_iso
from .domain import (
    classify_remote_error,
    detect_remote_protocol,
    https_to_ssh,
    map_git_error,
    parse_ahead_behind,
    parse_cmdkey_output,
    parse_status_porcelain_z,
)
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
    # 远端类失败附加结构化诊断；原始输出仍完整保留在 output 字段
    diagnosis = classify_remote_error(result.stderr, result.stdout)
    if diagnosis:
        payload["diagnosis"] = diagnosis
        diag_code = diagnosis["code"]
        if diag_code.startswith("http_403"):
            status_code = 403
        elif diag_code.startswith("http_401"):
            status_code = 401
        elif diag_code.startswith("ssh_") or diag_code == "network_error":
            status_code = 502
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


def _rel_to_repo(repo_path: str, raw: str) -> str:
    """把任意路径规范化为仓库内相对路径（统一正斜杠、去掉仓库根前缀）。

    参数：
        repo_path: 仓库根目录。
        raw: 原始路径（可能为绝对路径或 git 输出的相对路径）。
    返回值：
        相对仓库根的正斜杠路径（不带前导斜杠）。
    """
    text = (raw or "").replace("\\", "/").strip()
    root = (repo_path or "").replace("\\", "/").rstrip("/")
    if root and text.lower().startswith(root.lower() + "/"):
        text = text[len(root) + 1:]
    return text.lstrip("/")


def _mute_matches(pattern: str, path: str) -> bool:
    """判断路径是否命中本地静音规则（支持精确路径、目录前缀与通配符）。

    参数：
        pattern: 数据库中保存的模式（例如 "dist/"、"*.log"、"docs/a.md"）。
        path: 仓库内相对路径。
    返回值：
        命中返回 True。比较时忽略大小写，以适配 Windows。
    """
    rule = (pattern or "").replace("\\", "/").strip()
    target = (path or "").replace("\\", "/").strip()
    if not rule or not target:
        return False
    if rule.startswith("/"):
        rule = rule[1:]
    lowered_rule = rule.lower()
    lowered_target = target.lower()
    if rule.endswith("/"):
        base = lowered_rule.rstrip("/")
        return lowered_target == base or lowered_target.startswith(base + "/")
    if any(ch in rule for ch in "*?["):
        return fnmatch.fnmatchcase(lowered_target, lowered_rule)
    return lowered_target == lowered_rule


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

    # ------------------------------------------------------------------
    # 忽略规则诊断与本地静音
    # ------------------------------------------------------------------

    def _read_tracked_paths(self, repo: dict[str, Any]) -> set[str]:
        """读取仓库索引中已跟踪的路径集合（小写、正斜杠，便于比较）。

        参数：
            repo: 仓库字典。
        返回值：
            已跟踪路径集合；读取失败时返回空集合。
        """
        result = self.executor.run(
            repo["id"],
            repo["path"],
            "ls_files",
            [*QUOTEPATH_ARGS, "ls-files", "-z"],
            record_audit=False,
        )
        if not result.ok:
            return set()
        return {
            _rel_to_repo(repo["path"], token).lower()
            for token in result.stdout.split("\x00")
            if token
        }

    def _read_ignore_hits(
        self, repo: dict[str, Any], paths: list[str]
    ) -> dict[str, dict[str, Any]]:
        """批量查询忽略规则命中情况（含规则来源与行号）。

        参数：
            repo: 仓库字典。
            paths: 仓库内相对路径列表（已去重）。
        返回值：
            {小写路径: {source_file, line, pattern, source}}；
            source 形如 ".gitignore:12:*.log"，原样供前端展示。
        """
        hits: dict[str, dict[str, Any]] = {}
        unique = [item for item in dict.fromkeys(paths) if item]
        for start in range(0, len(unique), 200):
            chunk = unique[start:start + 200]
            result = self.executor.run(
                repo["id"],
                repo["path"],
                "check_ignore",
                [
                    *QUOTEPATH_ARGS,
                    "check-ignore",
                    "--no-index",
                    "-v",
                    "-z",
                    "--stdin",
                ],
                record_audit=False,
                input_text="\x00".join(chunk) + "\x00",
            )
            # -z 输出为四个字段：source、行号、规则、路径，均以 NUL 分隔
            tokens = result.stdout.split("\x00")
            index = 0
            while index + 3 < len(tokens):
                source_file, line_text, pattern, target = (
                    tokens[index],
                    tokens[index + 1],
                    tokens[index + 2],
                    tokens[index + 3],
                )
                index += 4
                if not source_file or not target:
                    continue
                try:
                    line = int(line_text)
                except ValueError:
                    line = 0
                key = _rel_to_repo(repo["path"], target).lower()
                hits[key] = {
                    "source_file": source_file,
                    "line": line,
                    "pattern": pattern,
                    "source": f"{source_file}:{line}:{pattern}",
                }
        return hits

    def _diagnose_dirty(
        self, repo: dict[str, Any], parsed: dict[str, Any]
    ) -> dict[str, Any]:
        """为全部脏文件生成忽略归因与静音标记。

        参数：
            repo: 仓库字典。
            parsed: parse_status_porcelain_z 的结构。
        返回值：
            {
              files: [{path, code, groups, tracked, ignored, ignore_source,
                       diagnosis, muted, mute_id}],
              counts: {total, pending, muted, tracked_ignored, ignorable, rule_matched},
              mutes: [{id, pattern, note, created_at}],
              gitignore_exists: bool
            }。
        """
        ordered: list[dict[str, Any]] = []
        merged: dict[str, dict[str, Any]] = {}
        for group in ("staged", "modified", "deleted", "untracked", "conflicts"):
            for item in parsed.get(group, []):
                key = _rel_to_repo(repo["path"], item["path"]).lower()
                entry = merged.get(key)
                if entry is None:
                    entry = {
                        "path": item["path"],
                        "code": item["code"],
                        "groups": [],
                    }
                    merged[key] = entry
                    ordered.append(entry)
                if group not in entry["groups"]:
                    entry["groups"].append(group)
                if group == "conflicts":
                    entry["code"] = item["code"]

        mutes = self.database.list_mutes(int(repo["id"]))
        tracked = self._read_tracked_paths(repo)
        # 目录型未跟踪条目（形如 "dir/"）传给 git 时去掉尾部分隔符
        query_paths = [_rel_to_repo(repo["path"], entry["path"]).rstrip("/") for entry in ordered]
        hits = self._read_ignore_hits(repo, query_paths)

        files: list[dict[str, Any]] = []
        counts = {
            "total": len(ordered),
            "pending": 0,
            "muted": 0,
            "tracked_ignored": 0,
            "ignorable": 0,
            "rule_matched": 0,
        }
        for entry in ordered:
            rel = _rel_to_repo(repo["path"], entry["path"])
            key = rel.lower()
            is_tracked = key in tracked
            hit = hits.get(key)
            is_ignored = hit is not None
            mute = next(
                (item for item in mutes if _mute_matches(item["pattern"], rel)),
                None,
            )

            if is_tracked and is_ignored:
                diagnosis = "tracked_ignored"
            elif is_ignored:
                diagnosis = "rule_matched"
            elif not is_tracked:
                diagnosis = "untracked_ignorable"
            else:
                diagnosis = "dirty_tracked"

            record = {
                "path": entry["path"],
                "code": entry["code"],
                "groups": entry["groups"],
                "tracked": is_tracked,
                "ignored": is_ignored,
                "ignore_source": hit["source"] if hit else None,
                "ignore_pattern": hit["pattern"] if hit else None,
                "diagnosis": diagnosis,
                "muted": mute is not None,
                "mute_id": mute["id"] if mute else None,
            }
            files.append(record)

            if mute is None:
                counts["pending"] += 1
            else:
                counts["muted"] += 1
            if diagnosis == "tracked_ignored":
                counts["tracked_ignored"] += 1
            elif diagnosis == "untracked_ignorable":
                counts["ignorable"] += 1
            elif diagnosis == "rule_matched":
                counts["rule_matched"] += 1

        gitignore = Path(repo["path"]) / ".gitignore"
        return {
            "files": files,
            "counts": counts,
            "mutes": mutes,
            "gitignore_exists": gitignore.is_file(),
        }

    def _decorate_files(
        self, parsed: dict[str, Any], diagnosis: dict[str, Any]
    ) -> dict[str, list[dict[str, Any]]]:
        """把忽略归因/静音标记合并回四类文件列表。

        参数：
            parsed: 状态解析结果。
            diagnosis: _diagnose_dirty 的返回值。
        返回值：
            与 parsed 同结构的文件列表（每项增加 muted / diagnosis / ignore_source）。
        """
        info = {
            _rel_to_repo("", item["path"]).lower(): item
            for item in diagnosis.get("files", [])
        }
        decorated: dict[str, list[dict[str, Any]]] = {}
        for group in ("staged", "modified", "deleted", "untracked", "conflicts"):
            decorated[group] = []
            for item in parsed.get(group, []):
                key = _rel_to_repo("", item["path"]).lower()
                extra = info.get(key, {})
                decorated[group].append(
                    {
                        **item,
                        "muted": bool(extra.get("muted")),
                        "mute_id": extra.get("mute_id"),
                        "diagnosis": extra.get("diagnosis"),
                        "ignore_source": extra.get("ignore_source"),
                    }
                )
        return decorated

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
                "pending": 0,
                "muted": 0,
                "tracked_ignored": 0,
                "ignorable": 0,
                "rule_matched": 0,
            },
            "files": {
                "staged": [],
                "modified": [],
                "deleted": [],
                "untracked": [],
                "conflicts": [],
            },
            "remotes": [],
            "remote_info": None,
            "ignore": {"counts": {}, "mutes": [], "gitignore_exists": False},
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
        if parsed["counts"]["total"]:
            diagnosis = self._diagnose_dirty(repo, parsed)
            decorated = self._decorate_files(parsed, diagnosis)
        else:
            diagnosis = {
                "files": [],
                "counts": {"total": 0, "pending": 0, "muted": 0, "tracked_ignored": 0, "ignorable": 0, "rule_matched": 0},
                "mutes": self.database.list_mutes(int(repo["id"])),
                "gitignore_exists": (Path(repo["path"]) / ".gitignore").is_file(),
            }
            decorated = {group: [] for group in ("staged", "modified", "deleted", "untracked", "conflicts")}
        base["files"] = {
            "staged": decorated["staged"],
            "modified": decorated["modified"],
            "deleted": decorated["deleted"],
            "untracked": decorated["untracked"],
            "conflicts": decorated["conflicts"],
        }
        counts = diagnosis["counts"]
        base["dirty"] = {
            "total": parsed["counts"]["total"],
            "staged": parsed["counts"]["staged"],
            "modified": parsed["counts"]["modified"],
            "deleted": parsed["counts"]["deleted"],
            "untracked": parsed["counts"]["untracked"],
            "conflicts": parsed["counts"]["conflicts"],
            "pending": counts.get("pending", 0),
            "muted": counts.get("muted", 0),
            "tracked_ignored": counts.get("tracked_ignored", 0),
            "ignorable": counts.get("ignorable", 0),
            "rule_matched": counts.get("rule_matched", 0),
        }
        base["ignore"] = {
            "counts": counts,
            "mutes": diagnosis["mutes"],
            "gitignore_exists": diagnosis["gitignore_exists"],
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
        base["remote_info"] = self._remote_info(
            repo, base["remotes"], base["upstream"], base["ssh_identity"]
        )
        return base

    def _remote_info(
        self,
        repo: dict[str, Any],
        remotes: list[dict[str, str]],
        upstream: str | None,
        identity: dict[str, Any] | None,
    ) -> dict[str, Any]:
        """汇总远端协议与 SSH 身份生效情况（不发起网络请求）。

        参数：
            repo: 仓库字典。
            remotes: _read_remotes 的返回值。
            upstream: 当前分支 upstream（如 origin/main）。
            identity: 当前生效的 SSH 身份。
        返回值：
            {name, protocol, url, ssh_identity_effective, https_with_ssh_identity,
             ssh_convertible, ssh_url, credential_warning}。
        """
        selected_name = upstream.split("/", 1)[0] if upstream else ""
        selected = next(
            (item for item in remotes if item["name"] == selected_name), None
        )
        if selected is None and remotes:
            selected = remotes[0]
        if selected is None:
            return {
                "name": None,
                "protocol": "unknown",
                "url": "",
                "ssh_identity_effective": False,
                "https_with_ssh_identity": bool(identity),
                "ssh_convertible": False,
                "ssh_url": None,
                "credential_warning": None,
            }

        protocol = selected.get("protocol") or "unknown"
        url = selected.get("push_url") or selected.get("fetch_url") or ""
        conversion = https_to_ssh(url)
        info: dict[str, Any] = {
            "name": selected["name"],
            "protocol": protocol,
            "url": url,
            "ssh_identity_effective": bool(identity) and protocol == "ssh",
            "https_with_ssh_identity": bool(identity) and protocol == "https",
            "ssh_convertible": bool(conversion.get("ok")),
            "ssh_url": conversion.get("ssh_url") if conversion.get("ok") else None,
            "credential_warning": None,
        }
        # 仅在审计中存在 403 历史时给出凭据提示，避免每次预检都误报
        failure = self.database.last_push_failure(int(repo["id"]))
        if protocol == "https" and failure:
            history = f"{failure.get('output_summary') or ''}\n{failure.get('command') or ''}"
            match = re.search(r"permission to\s+(.+?)\s+denied to\s+([^\s]+)", history, re.I)
            if match or "returned error: 403" in history.lower():
                owner = match.group(1).strip().rstrip(".") if match else ""
                account = match.group(2).strip().rstrip(".,;:。") if match else ""
                info["credential_warning"] = {
                    "owner": owner,
                    "account": account,
                    "ts": failure.get("ts"),
                    "message": (
                        f"历史推送曾因凭据账号 {account} 对 {owner} 无写权限而失败，"
                        "建议改用 SSH 地址或更换 HTTPS 凭据。"
                        if match
                        else "历史推送曾返回 403，建议检查当前 HTTPS 凭据权限。"
                    ),
                }
        return info

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
            [{name, fetch_url, push_url, protocol}]；失败时返回空列表。
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
                name,
                {"name": name, "fetch_url": "", "push_url": "", "protocol": "unknown"},
            )
            if kind == "(fetch)":
                entry["fetch_url"] = url
            elif kind == "(push)":
                entry["push_url"] = url
            elif not entry["fetch_url"]:
                entry["fetch_url"] = url
        for entry in items.values():
            entry["protocol"] = detect_remote_protocol(
                entry.get("push_url") or entry.get("fetch_url") or ""
            )
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
        diagnosis = self._diagnose_dirty(repo, parsed)
        decorated = self._decorate_files(parsed, diagnosis)
        files: list[dict[str, Any]] = []
        for group in ("staged", "modified", "deleted", "untracked", "conflicts"):
            for item in decorated[group]:
                files.append(
                    {
                        "path": item["path"],
                        "code": item["code"],
                        "group": group,
                        "old_path": item.get("old_path"),
                        "renamed": bool(item.get("renamed")),
                        "muted": bool(item.get("muted")),
                        "mute_id": item.get("mute_id"),
                        "diagnosis": item.get("diagnosis"),
                        "ignore_source": item.get("ignore_source"),
                    }
                )
        return {
            "files": files,
            "conflicts": parsed["conflicts"],
            # 保留四类分组计数（兼容既有看板与测试），并补充归因/静音计数
            "counts": {**diagnosis["counts"], **parsed["counts"]},
            "mutes": diagnosis["mutes"],
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

    # ------------------------------------------------------------------
    # 忽略规则报告、写入 .gitignore、本地静音、取消跟踪
    # ------------------------------------------------------------------

    def _write_manual_audit(
        self, repo: dict[str, Any], action: str, command: str, summary: str
    ) -> None:
        """写入不经过 git 子进程的动作审计（如追加 .gitignore、静音、凭据操作）。

        参数：
            repo: 仓库字典。
            action: 动作类型（ignore_append / mute / untrack 等）。
            command: 展示用命令或操作描述。
            summary: 结果摘要。
        返回值：
            无。
        """
        self.database.insert_audit(
            {
                "ts": now_iso(),
                "repo_id": repo.get("id"),
                "repo_path": repo.get("path", ""),
                "action": action,
                "command": command,
                "exit_code": 0,
                "ok": True,
                "duration_ms": 0,
                "forced": False,
                "output_summary": summary,
                "identity_name": None,
            }
        )

    def ignore_report(self, repo_id: int) -> dict[str, Any]:
        """生成某个仓库的忽略归因报告（三类文件与静音规则）。

        参数：
            repo_id: 仓库主键。
        返回值：
            {repo, files, counts, mutes, gitignore}；files 每项含
            tracked / ignored / ignore_source / diagnosis / muted。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        parsed = self._collect_dirty(repo)
        diagnosis = self._diagnose_dirty(repo, parsed)
        gitignore_path = Path(repo["path"]) / ".gitignore"
        return {
            "repo": {"id": repo["id"], "name": repo["name"], "path": repo["path"]},
            "files": diagnosis["files"],
            "counts": diagnosis["counts"],
            "mutes": diagnosis["mutes"],
            "gitignore": {
                "path": str(gitignore_path),
                "exists": diagnosis["gitignore_exists"],
            },
        }

    def _rule_for_path(self, repo_path: str, raw: str) -> str | None:
        """为单个路径生成 .gitignore 规则建议。

        参数：
            repo_path: 仓库根目录。
            raw: 用户/前端提交的路径。
        返回值：
            文件返回 "/相对路径"，目录返回 "相对路径/"；
            路径非法（为空或越出仓库）时返回 None。
        """
        rel = _rel_to_repo(repo_path, raw).strip()
        if not rel or rel.startswith("../") or rel == "..":
            return None
        is_dir = rel.endswith("/") or (Path(repo_path) / rel).is_dir()
        rel = rel.strip("/")
        if not rel:
            return None
        return f"{rel}/" if is_dir else f"/{rel}"

    def _read_gitignore(self, target: Path) -> tuple[bytes, str, str]:
        """读取目标 .gitignore 的原始字节与文本（保留 BOM 与换行风格）。

        参数：
            target: .gitignore 文件路径。
        返回值：
            (原始字节, 去掉 BOM 的文本, 换行风格 "\\r\\n" 或 "\\n")。
            BOM 通过文本内容保留在原始字节中，追加时不破坏。
        """
        if not target.is_file():
            return b"", "", "\n"
        raw = target.read_bytes()
        bom = raw.startswith(b"\xef\xbb\xbf")
        body = raw[3:] if bom else raw
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError:
            text = body.decode("utf-8", errors="replace")
        newline = "\r\n" if "\r\n" in text else "\n"
        return raw, text, newline

    def preview_ignore(self, repo_id: int, paths: list[str]) -> dict[str, Any]:
        """生成 .gitignore 追加预览（不写入任何文件）。

        参数：
            repo_id: 仓库主键。
            paths: 待忽略的路径列表。
        返回值：
            {target, exists, newline, rules, skipped, append, original, preview}。
            skipped 记录因等价规则已存在而不再追加的项。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        target = Path(repo["path"]) / ".gitignore"
        raw, text, newline = self._read_gitignore(target)
        existing = {
            line.strip().lstrip("/").rstrip("/")
            for line in text.splitlines()
            if line.strip() and not line.strip().startswith("#")
        }
        rules: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        seen: set[str] = set()
        for raw_path in paths[:500]:
            rule = self._rule_for_path(repo["path"], raw_path)
            if rule is None:
                continue
            key = rule.lstrip("/").rstrip("/").lower()
            if key in existing or key in seen:
                skipped.append(
                    {
                        "path": raw_path,
                        "rule": rule,
                        "reason": "等价规则已存在，不重复追加。",
                    }
                )
                continue
            seen.add(key)
            rules.append({"path": raw_path, "rule": rule})

        append_block = "".join(f"{item['rule']}{newline}" for item in rules)
        original_display = text
        if original_display and not original_display.endswith(("\n", "\r")):
            original_display += newline
        preview = original_display + append_block
        return {
            "target": str(target),
            "exists": target.is_file(),
            "newline": "crlf" if newline == "\r\n" else "lf",
            "rules": rules,
            "skipped": skipped,
            "append": append_block,
            "original": text[-8000:],
            "original_truncated": len(text) > 8000,
            "preview": preview[-9000:],
            "preview_truncated": len(preview) > 9000,
            "had_trailing_newline": bool(raw) and raw.endswith(b"\n"),
        }

    def append_ignore(self, repo_id: int, paths: list[str], note: str = "") -> dict[str, Any]:
        """把选中的规则追加到仓库根 .gitignore，并立即刷新状态验证。

        参数：
            repo_id: 仓库主键。
            paths: 待忽略的路径列表。
            note: 可选备注（写入审计摘要）。
        返回值：
            {ok, target, appended, skipped, message, status}。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        preview = self.preview_ignore(repo_id, paths)
        rules = [item["rule"] for item in preview["rules"]]
        if not rules:
            return {
                "ok": True,
                "target": preview["target"],
                "appended": [],
                "skipped": preview["skipped"],
                "message": "所有规则均已存在，未修改 .gitignore。",
                "status": self.collect_status(repo),
            }
        target = Path(preview["target"])
        raw, _text, newline = self._read_gitignore(target)
        block = "".join(f"{rule}{newline}" for rule in rules).encode("utf-8")
        if raw and not raw.endswith(b"\n"):
            block = newline.encode("utf-8") + block
        with target.open("ab") as handle:
            handle.write(block)
        self._write_manual_audit(
            repo,
            "ignore_append",
            f"append .gitignore: {', '.join(rules)}",
            f"追加 {len(rules)} 条规则" + (f"；备注：{note}" if note else ""),
        )
        return {
            "ok": True,
            "target": str(target),
            "appended": rules,
            "skipped": preview["skipped"],
            "message": f"已向 .gitignore 追加 {len(rules)} 条规则。",
            "status": self.collect_status(repo),
        }

    def add_mutes(
        self, repo_id: int, patterns: list[str], note: str = ""
    ) -> dict[str, Any]:
        """把路径/规则写入本地静音表（只影响看板，不修改仓库）。

        参数：
            repo_id: 仓库主键。
            patterns: 路径或通配规则列表。
            note: 可选备注。
        返回值：
            {ok, added, skipped, mutes, message, status}。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        added: list[str] = []
        skipped: list[dict[str, str]] = []
        existing = {
            _rel_to_repo("", item["pattern"]).lstrip("/").lower()
            for item in self.database.list_mutes(repo_id)
        }
        for raw_pattern in patterns[:200]:
            pattern = _rel_to_repo(repo["path"], raw_pattern).strip().strip("/")
            if not pattern:
                continue
            key = pattern.lower()
            if key in existing:
                skipped.append({"pattern": pattern, "reason": "该静音规则已存在。"})
                continue
            try:
                self.database.insert_mute(repo_id, pattern, note)
            except sqlite3.IntegrityError:
                skipped.append({"pattern": pattern, "reason": "该静音规则已存在。"})
                continue
            existing.add(key)
            added.append(pattern)
        if added:
            self._write_manual_audit(
                repo,
                "mute",
                f"mute: {', '.join(added)}",
                f"静音 {len(added)} 项" + (f"；备注：{note}" if note else ""),
            )
        return {
            "ok": True,
            "added": added,
            "skipped": skipped,
            "mutes": self.database.list_mutes(repo_id),
            "message": f"已静音 {len(added)} 项（仅本地生效）。" if added else "没有新增静音项。",
            "status": self.collect_status(repo),
        }

    def remove_mute(self, repo_id: int, mute_id: int) -> dict[str, Any]:
        """删除一条本地静音规则（不影响磁盘文件与已跟踪状态）。

        参数：
            repo_id: 仓库主键。
            mute_id: 静音规则主键。
        返回值：
            {ok, removed, mutes, status}。
        异常：
            ApiError: 规则不存在。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        removed = self.database.delete_mute(repo_id, mute_id)
        if removed is None:
            raise ApiError("mute_not_found", "静音规则不存在或已被移除。", 404)
        self._write_manual_audit(
            repo, "mute_remove", f"unmute: {removed['pattern']}", "取消静音 1 项"
        )
        return {
            "ok": True,
            "removed": removed,
            "mutes": self.database.list_mutes(repo_id),
            "message": "已取消静音。",
            "status": self.collect_status(repo),
        }

    def untrack_paths(
        self, repo_id: int, paths: list[str], confirm: bool
    ) -> dict[str, Any]:
        """执行 `git rm --cached`：从版本库移除但保留磁盘文件。

        参数：
            repo_id: 仓库主键。
            paths: 需要取消跟踪的路径。
            confirm: 必须为 True（危险操作需显式确认）。
        返回值：
            {ok, removed, needs_commit, message, status}。
        异常：
            ApiError: 未确认、无有效路径或命令失败。
        """
        if not confirm:
            raise ApiError(
                "confirm_required",
                "取消跟踪属于危险操作，请先确认“会从版本库移除但保留磁盘文件”。",
                400,
            )
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        tracked = self._read_tracked_paths(repo)
        selected: list[str] = []
        skipped: list[dict[str, str]] = []
        for raw_path in paths[:200]:
            rel = _rel_to_repo(repo["path"], raw_path).strip().strip("/")
            if not rel:
                continue
            if rel.lower() not in tracked:
                skipped.append({"path": rel, "reason": "该路径未被跟踪，无需取消跟踪。"})
                continue
            if rel not in selected:
                selected.append(rel)
        if not selected:
            raise ApiError(
                "nothing_to_untrack",
                "没有可取消跟踪的文件（选中项均未被 git 跟踪）。",
                400,
                {"skipped": skipped},
            )
        result = self.executor.run(
            repo["id"],
            repo["path"],
            "untrack",
            [*QUOTEPATH_ARGS, "rm", "--cached", "--quiet", "--", *selected],
            forced=True,
        )
        if not result.ok:
            raise _error_from_result(
                "untrack", result, extra={"paths": selected, "skipped": skipped}
            )
        return {
            "ok": True,
            "removed": selected,
            "skipped": skipped,
            "needs_commit": True,
            "message": "已从版本库移除并保留磁盘文件；还需额外提交一次删除变更（未自动提交）。",
            "status": self.collect_status(repo),
        }

    # ------------------------------------------------------------------
    # 远端协议转换与 HTTPS 凭据
    # ------------------------------------------------------------------

    def preview_remote_ssh(self, repo_id: int, remote_name: str) -> dict[str, Any]:
        """生成远端地址 HTTPS→SSH 转换预览（不修改配置）。

        参数：
            repo_id: 仓库主键。
            remote_name: 远端名称（为空时使用当前分支远端或第一个）。
        返回值：
            {remote, fetch_url, push_url, ssh_fetch_url, ssh_push_url,
             fetch_convertible, push_convertible, reason}。
        """
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        remotes = self._read_remotes(repo)
        selected = next(
            (item for item in remotes if item["name"] == remote_name), None
        )
        if selected is None and remotes:
            selected = remotes[0]
        if selected is None:
            raise ApiError("remote_missing", "该仓库未配置远端。", 404)
        fetch = https_to_ssh(selected.get("fetch_url") or "")
        push = https_to_ssh(selected.get("push_url") or selected.get("fetch_url") or "")
        reasons = [
            item.get("reason")
            for item in (fetch, push)
            if not item.get("ok")
        ]
        return {
            "remote": selected["name"],
            "fetch_url": selected.get("fetch_url") or "",
            "push_url": selected.get("push_url") or "",
            "ssh_fetch_url": fetch.get("ssh_url"),
            "ssh_push_url": push.get("ssh_url"),
            "fetch_convertible": bool(fetch.get("ok")),
            "push_convertible": bool(push.get("ok")),
            "reason": reasons[0] if reasons else None,
        }

    def convert_remote_ssh(
        self, repo_id: int, remote_name: str, mode: str, confirm: bool
    ) -> dict[str, Any]:
        """把远端地址切换为 SSH（默认同时修改 fetch 与 push）。

        参数：
            repo_id: 仓库主键。
            remote_name: 远端名称。
            mode: both / push / fetch。
            confirm: 必须为 True。
        返回值：
            {ok, remote, mode, fetch_url, push_url, message, status}。
        """
        if not confirm:
            raise ApiError("confirm_required", "修改远端地址需要显式确认。", 400)
        if mode not in ("both", "push", "fetch"):
            raise ApiError("invalid_mode", "模式只能是 both / push / fetch。", 400)
        repo = self.get_repo(repo_id)
        self._ensure_repo_dir(repo)
        preview = self.preview_remote_ssh(repo_id, remote_name)
        name = preview["remote"]
        need_fetch = mode in ("both", "fetch")
        need_push = mode in ("both", "push")
        if need_fetch and not preview["fetch_convertible"]:
            raise ApiError(
                "remote_not_convertible",
                preview.get("reason") or "该远端地址不支持转换为 SSH。",
                400,
            )
        if need_push and not preview["push_convertible"]:
            raise ApiError(
                "remote_not_convertible",
                preview.get("reason") or "该远端地址不支持转换为 SSH。",
                400,
            )
        commands: list[list[str]] = []
        if need_fetch:
            commands.append(["remote", "set-url", name, preview["ssh_fetch_url"]])
        if need_push:
            commands.append(
                ["remote", "set-url", "--push", name, preview["ssh_push_url"]]
            )
        for command in commands:
            result = self.executor.run(
                repo["id"],
                repo["path"],
                "remote_set_url",
                [*QUOTEPATH_ARGS, *command],
            )
            if not result.ok:
                raise _error_from_result("remote_set_url", result)
        return {
            "ok": True,
            "remote": name,
            "mode": mode,
            "fetch_url": preview["ssh_fetch_url"] if need_fetch else preview["fetch_url"],
            "push_url": preview["ssh_push_url"] if need_push else preview["push_url"],
            "message": "远端地址已切换为 SSH。",
            "status": self.collect_status(repo),
        }

    def list_https_credentials(self) -> dict[str, Any]:
        """读取 Windows 凭据管理器中的 git HTTPS 条目（不展示密码）。

        参数：
            无。
        返回值：
            {ok, items:[{target, host, url}], raw, message}。
            仅在 Windows 可用；其他平台返回空列表并说明。
        """
        if os.name != "nt":
            return {
                "ok": False,
                "items": [],
                "raw": "",
                "message": "当前系统不是 Windows，无法读取 cmdkey 凭据。",
            }
        try:
            completed = subprocess.run(
                ["cmdkey", "/list"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ok": False,
                "items": [],
                "raw": "",
                "message": f"读取凭据列表失败：{exc}",
            }
        text = completed.stdout.decode("utf-8", errors="replace")
        if not text.strip():
            text = completed.stdout.decode("mbcs", errors="replace")
        items = parse_cmdkey_output(text)
        return {
            "ok": True,
            "items": items,
            "raw": text[-3000:],
            "message": f"共找到 {len(items)} 条 git HTTPS 凭据。" if items else "未发现 git HTTPS 凭据。",
        }

    def delete_https_credential(self, target: str) -> dict[str, Any]:
        """清除指定 host 的 git HTTPS 凭据（需显式确认后由调用方触发）。

        参数：
            target: cmdkey 目标，如 git:https://github.com。
        返回值：
            {ok, target, message}。
        异常：
            ApiError: 目标格式非法或删除失败。
        """
        text = (target or "").strip()
        if not re.match(r"^git:(https?://[^\s]+)$", text, flags=re.IGNORECASE):
            raise ApiError(
                "invalid_credential_target",
                "只允许清除形如 git:https://host 的 git HTTPS 凭据。",
                400,
            )
        if os.name != "nt":
            raise ApiError("unsupported_platform", "当前系统不支持 cmdkey。", 400)
        try:
            completed = subprocess.run(
                ["cmdkey", f"/delete:{text}"],
                capture_output=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApiError("credential_delete_failed", f"清除凭据失败：{exc}", 500)
        output = (completed.stdout + completed.stderr).decode("utf-8", errors="replace")
        if completed.returncode != 0:
            raise ApiError(
                "credential_delete_failed",
                f"清除凭据失败（退出码 {completed.returncode}）：{output.strip()[-500:]}",
                409,
                {"command": f"cmdkey /delete:{text}", "exit_code": completed.returncode},
            )
        self._write_manual_audit(
            {"id": None, "path": ""},
            "credential_delete",
            f"cmdkey /delete:{text}",
            "已清除 git HTTPS 凭据",
        )
        return {
            "ok": True,
            "target": text,
            "message": "已清除该 host 的凭据；下次推送会要求重新登录。",
        }

    def login_https_credential(self, confirm: bool) -> dict[str, Any]:
        """在独立控制台窗口启动 Git Credential Manager 登录（可交互）。

        参数：
            confirm: 必须为 True（会打开外部窗口并可能要求输入凭据）。
        返回值：
            {ok, installed, launched, message, install_url}。
            未安装 GCM 时明确返回 installed=False，不做假成功。
        """
        if not confirm:
            raise ApiError(
                "confirm_required", "打开交互式凭据登录需要显式确认。", 400
            )
        executable = shutil.which("git-credential-manager")
        if executable is None:
            return {
                "ok": False,
                "installed": False,
                "launched": False,
                "message": "未检测到 Git Credential Manager（git-credential-manager）。"
                "请安装后再使用交互式登录。",
                "install_url": "https://github.com/git-ecosystem/git-credential-manager/releases",
            }
        if os.name != "nt":
            return {
                "ok": False,
                "installed": True,
                "launched": False,
                "message": "当前系统不支持独立控制台窗口登录，请在终端执行 git-credential-manager login。",
                "install_url": "",
            }
        try:
            subprocess.Popen(
                [
                    "cmd.exe",
                    "/c",
                    "start",
                    "",
                    "cmd.exe",
                    "/k",
                    "git-credential-manager login",
                ],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                close_fds=True,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            return {
                "ok": False,
                "installed": True,
                "launched": False,
                "message": f"启动凭据登录窗口失败：{exc}",
                "install_url": "",
            }
        self._write_manual_audit(
            {"id": None, "path": ""},
            "credential_login",
            "git-credential-manager login",
            "已在独立控制台窗口启动交互式凭据登录",
        )
        return {
            "ok": True,
            "installed": True,
            "launched": True,
            "message": "已在独立控制台窗口启动凭据登录，请按窗口提示完成。",
            "install_url": "",
        }
