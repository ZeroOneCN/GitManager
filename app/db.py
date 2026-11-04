"""SQLite 存储层。

职责：仓库配置与操作审计的持久化；统一封装连接、建表与查询。
约束：仓库移除只删配置行，不删除审计历史；所有写操作串行化，避免并发写冲突。
"""

from __future__ import annotations

import os
import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

# 建表语句：幂等执行
_SCHEMA = """
CREATE TABLE IF NOT EXISTS repositories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    repo_id INTEGER,
    repo_path TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL,
    command TEXT NOT NULL,
    exit_code INTEGER NOT NULL,
    ok INTEGER NOT NULL,
    duration_ms INTEGER NOT NULL,
    forced INTEGER NOT NULL DEFAULT 0,
    output_summary TEXT NOT NULL DEFAULT '',
    client_ip TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts);
CREATE INDEX IF NOT EXISTS idx_audit_repo ON audit_logs(repo_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);

CREATE TABLE IF NOT EXISTS allowed_roots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    path TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ssh_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    key_path TEXT NOT NULL,
    host_alias TEXT NOT NULL DEFAULT '',
    is_default INTEGER NOT NULL DEFAULT 0,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_ssh_unique
    ON ssh_identities(key_path, host_alias);

CREATE TABLE IF NOT EXISTS repo_ssh_bindings (
    repo_id INTEGER PRIMARY KEY,
    identity_id INTEGER NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ignored_paths (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    repo_id INTEGER NOT NULL,
    pattern TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ignored_repo ON ignored_paths(repo_id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_ignored_unique ON ignored_paths(repo_id, pattern);
"""


def now_iso() -> str:
    """返回带本地时区偏移的当前时间字符串（ISO-8601，精确到秒）。

    参数：
        无。
    返回值：
        例如 "2026-11-04T19:00:00+08:00"，用于统一存储与展示。
    """
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Database:
    """SQLite 访问封装。

    参数：
        path: 数据库文件绝对路径。
    """

    def __init__(self, path: Path) -> None:
        """初始化数据库对象（不立即建表）。

        参数：
            path: 数据库文件路径。
        返回值：
            无。
        """
        self.path = Path(path)
        self._write_lock = threading.Lock()

    def _connect(self) -> sqlite3.Connection:
        """创建新的数据库连接。

        参数：
            无。
        返回值：
            配置好 Row 工厂与忙等待超时的连接对象。
        """
        conn = sqlite3.connect(str(self.path), timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout = 15000")
        return conn

    def init(self, retention_days: int, default_roots: tuple[Path, ...] = ()) -> int:
        """初始化数据库：建目录、建表、迁移审计列、种子白名单，并清理超期审计。

        参数：
            retention_days: 审计日志保留天数。
            default_roots: 首次初始化时从 .env 导入的默认白名单根目录。
        返回值：
            本次清理删除的审计行数。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
            # 兼容第一期数据库：审计表补充身份名称列（仅记录身份名，不记录密钥内容）
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(audit_logs)")}
            if "identity_name" not in columns:
                conn.execute("ALTER TABLE audit_logs ADD COLUMN identity_name TEXT")
            # 兼容旧数据库：补充局域网来源 IP 列（仅非回环请求写入）
            if "client_ip" not in columns:
                conn.execute(
                    "ALTER TABLE audit_logs ADD COLUMN client_ip TEXT NOT NULL DEFAULT ''"
                )
        if default_roots and not self.list_allowed_roots():
            for root in default_roots:
                try:
                    self.insert_allowed_root(str(root))
                except sqlite3.IntegrityError:
                    continue
        return self.purge_audit(retention_days)

    def list_repos(self) -> list[dict[str, Any]]:
        """列出全部已配置仓库。

        参数：
            无。
        返回值：
            按添加顺序排列的仓库字典列表（id/name/path/created_at）。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, name, path, created_at FROM repositories ORDER BY id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_repo(self, repo_id: int) -> dict[str, Any] | None:
        """按 id 查询仓库。

        参数：
            repo_id: 仓库主键。
        返回值：
            仓库字典；不存在时返回 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, path, created_at FROM repositories WHERE id = ?",
                (repo_id,),
            ).fetchone()
        return dict(row) if row else None

    def find_repo_by_path(self, path: str) -> dict[str, Any] | None:
        """按路径查找仓库（Windows 下忽略大小写）。

        参数：
            path: 仓库绝对路径。
        返回值：
            已存在的仓库字典；未找到返回 None。
        """
        target = path.lower()
        for repo in self.list_repos():
            if repo["path"].lower() == target:
                return repo
        return None

    def insert_repo(self, path: str, name: str) -> int:
        """新增仓库配置。

        参数：
            path: 仓库根目录绝对路径（唯一）。
            name: 展示名称（目录名）。
        返回值：
            新记录的 id。
        异常：
            sqlite3.IntegrityError: 路径已存在时抛出（由业务层先行判重）。
        """
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO repositories(path, name, created_at) VALUES (?, ?, ?)",
                (path, name, now_iso()),
            )
            return int(cursor.lastrowid)

    def delete_repo(self, repo_id: int) -> dict[str, Any] | None:
        """删除仓库配置记录（不触碰磁盘目录）。

        参数：
            repo_id: 仓库主键。
        返回值：
            被删除的仓库字典；不存在时返回 None。
        """
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, name, path, created_at FROM repositories WHERE id = ?",
                (repo_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM repositories WHERE id = ?", (repo_id,))
            conn.execute("DELETE FROM repo_ssh_bindings WHERE repo_id = ?", (repo_id,))
            conn.execute("DELETE FROM ignored_paths WHERE repo_id = ?", (repo_id,))
        return dict(row)

    # ------------------------------------------------------------------
    # 本地静音规则（ignored_paths）
    # ------------------------------------------------------------------

    def list_mutes(self, repo_id: int) -> list[dict[str, Any]]:
        """列出某仓库的本地静音规则。

        参数：
            repo_id: 仓库主键。
        返回值：
            [{id, repo_id, pattern, note, created_at}]，按创建顺序排列。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, repo_id, pattern, note, created_at
                FROM ignored_paths WHERE repo_id = ? ORDER BY id ASC
                """,
                (repo_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def insert_mute(self, repo_id: int, pattern: str, note: str = "") -> int:
        """写入一条本地静音规则（不修改仓库文件）。

        参数：
            repo_id: 仓库主键。
            pattern: 路径或通配规则。
            note: 可选备注。
        返回值：
            新记录 id。
        异常：
            sqlite3.IntegrityError: 同一仓库下规则重复时抛出。
        """
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO ignored_paths(repo_id, pattern, note, created_at) VALUES (?, ?, ?, ?)",
                (repo_id, pattern, note, now_iso()),
            )
            return int(cursor.lastrowid)

    def delete_mute(self, repo_id: int, mute_id: int) -> dict[str, Any] | None:
        """删除一条本地静音规则。

        参数：
            repo_id: 仓库主键（防止跨仓库删除）。
            mute_id: 规则主键。
        返回值：
            被删除的记录；不存在时返回 None。
        """
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, repo_id, pattern, note, created_at FROM ignored_paths WHERE id = ? AND repo_id = ?",
                (mute_id, repo_id),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM ignored_paths WHERE id = ?", (mute_id,))
        return dict(row)

    # ------------------------------------------------------------------
    # 白名单根目录
    # ------------------------------------------------------------------

    def list_allowed_roots(self) -> list[dict[str, Any]]:
        """列出已持久化的白名单根目录。

        参数：
            无。
        返回值：
            按添加顺序排列的 {id, path, created_at} 列表。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, path, created_at FROM allowed_roots ORDER BY id ASC"
            ).fetchall()
        return [dict(row) for row in rows]

    def get_allowed_root(self, root_id: int) -> dict[str, Any] | None:
        """按 id 查询白名单根目录。

        参数：
            root_id: 根目录主键。
        返回值：
            {id, path, created_at}；不存在时返回 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT id, path, created_at FROM allowed_roots WHERE id = ?",
                (root_id,),
            ).fetchone()
        return dict(row) if row else None

    def insert_allowed_root(self, path: str) -> int:
        """新增白名单根目录。

        参数：
            path: 规范化后的绝对路径（唯一）。
        返回值：
            新记录 id。
        异常：
            sqlite3.IntegrityError: 路径已存在时抛出。
        """
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                "INSERT INTO allowed_roots(path, created_at) VALUES (?, ?)",
                (path, now_iso()),
            )
            return int(cursor.lastrowid)

    def delete_allowed_root(self, root_id: int) -> dict[str, Any] | None:
        """删除白名单根目录记录（不触碰磁盘，也不删除受管仓库）。

        参数：
            root_id: 根目录主键。
        返回值：
            被删除的记录；不存在时返回 None。
        """
        with self._write_lock, self._connect() as conn:
            row = conn.execute(
                "SELECT id, path, created_at FROM allowed_roots WHERE id = ?",
                (root_id,),
            ).fetchone()
            if row is None:
                return None
            conn.execute("DELETE FROM allowed_roots WHERE id = ?", (root_id,))
        return dict(row)

    # ------------------------------------------------------------------
    # SSH 身份与仓库绑定
    # ------------------------------------------------------------------

    def list_identities(self) -> list[dict[str, Any]]:
        """列出全部 SSH 身份配置。

        参数：
            无。
        返回值：
            身份字典列表（含 enabled / is_default 布尔化字段）。
        """
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT id, name, key_path, host_alias, is_default, enabled,
                       created_at, updated_at
                FROM ssh_identities ORDER BY is_default DESC, id ASC
                """
            ).fetchall()
        return [self._identity_row(row) for row in rows]

    def get_identity(self, identity_id: int) -> dict[str, Any] | None:
        """按 id 查询 SSH 身份。

        参数：
            identity_id: 身份主键。
        返回值：
            身份字典；不存在时返回 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, name, key_path, host_alias, is_default, enabled,
                       created_at, updated_at
                FROM ssh_identities WHERE id = ?
                """,
                (identity_id,),
            ).fetchone()
        return self._identity_row(row) if row else None

    def find_identity(self, key_path: str, host_alias: str) -> dict[str, Any] | None:
        """按私钥路径与 Host 别名查找身份（大小写不敏感）。

        参数：
            key_path: 私钥绝对路径。
            host_alias: Host 别名，可为空字符串。
        返回值：
            已存在的身份字典；未找到返回 None。
        """
        target_key = os.path.normcase(key_path)
        for identity in self.list_identities():
            if os.path.normcase(identity["key_path"]) == target_key and identity["host_alias"] == host_alias:
                return identity
        return None

    def insert_identity(self, record: dict[str, Any]) -> int:
        """新增 SSH 身份配置。

        参数：
            record: 含 name/key_path/host_alias/enabled/is_default 的字典。
        返回值：
            新记录 id。
        异常：
            sqlite3.IntegrityError: 同一私钥与 Host 别名组合重复时抛出。
        """
        stamp = now_iso()
        with self._write_lock, self._connect() as conn:
            if record.get("is_default"):
                conn.execute("UPDATE ssh_identities SET is_default = 0")
            cursor = conn.execute(
                """
                INSERT INTO ssh_identities
                    (name, key_path, host_alias, is_default, enabled, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["name"],
                    record["key_path"],
                    record.get("host_alias", ""),
                    1 if record.get("is_default") else 0,
                    1 if record.get("enabled", True) else 0,
                    stamp,
                    stamp,
                ),
            )
            return int(cursor.lastrowid)

    def update_identity(self, identity_id: int, fields: dict[str, Any]) -> dict[str, Any] | None:
        """更新 SSH 身份配置（只更新传入字段）。

        参数：
            identity_id: 身份主键。
            fields: 可包含 name/key_path/host_alias/enabled/is_default。
        返回值：
            更新后的身份字典；不存在时返回 None。
        """
        current = self.get_identity(identity_id)
        if current is None:
            return None
        assignments: list[str] = []
        params: list[Any] = []
        for key in ("name", "key_path", "host_alias"):
            if key in fields and fields[key] is not None:
                assignments.append(f"{key} = ?")
                params.append(str(fields[key]))
        for key in ("enabled", "is_default"):
            if key in fields and fields[key] is not None:
                assignments.append(f"{key} = ?")
                params.append(1 if fields[key] else 0)
        assignments.append("updated_at = ?")
        params.append(now_iso())
        with self._write_lock, self._connect() as conn:
            if fields.get("is_default"):
                conn.execute("UPDATE ssh_identities SET is_default = 0")
            conn.execute(
                f"UPDATE ssh_identities SET {', '.join(assignments)} WHERE id = ?",
                [*params, identity_id],
            )
        return self.get_identity(identity_id)

    def delete_identity(self, identity_id: int) -> dict[str, Any] | None:
        """删除 SSH 身份配置，并解除相关仓库绑定（不触碰磁盘密钥文件）。

        参数：
            identity_id: 身份主键。
        返回值：
            被删除的身份字典；不存在时返回 None。
        """
        identity = self.get_identity(identity_id)
        if identity is None:
            return None
        with self._write_lock, self._connect() as conn:
            conn.execute("DELETE FROM ssh_identities WHERE id = ?", (identity_id,))
            conn.execute("DELETE FROM repo_ssh_bindings WHERE identity_id = ?", (identity_id,))
        return identity

    def set_default_identity(self, identity_id: int) -> dict[str, Any] | None:
        """把指定身份设为全局默认（同时清除其他默认标记）。

        参数：
            identity_id: 身份主键。
        返回值：
            更新后的身份字典；不存在时返回 None。
        """
        identity = self.get_identity(identity_id)
        if identity is None:
            return None
        with self._write_lock, self._connect() as conn:
            conn.execute("UPDATE ssh_identities SET is_default = 0")
            conn.execute(
                "UPDATE ssh_identities SET is_default = 1, updated_at = ? WHERE id = ?",
                (now_iso(), identity_id),
            )
        return self.get_identity(identity_id)

    def count_identity_bindings(self, identity_id: int) -> int:
        """统计绑定了指定身份的仓库数量。

        参数：
            identity_id: 身份主键。
        返回值：
            绑定该身份的仓库数。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS c FROM repo_ssh_bindings WHERE identity_id = ?",
                (identity_id,),
            ).fetchone()
        return int(row["c"]) if row else 0

    def get_repo_binding(self, repo_id: int) -> int | None:
        """读取仓库绑定的 SSH 身份 id。

        参数：
            repo_id: 仓库主键。
        返回值：
            身份 id；未绑定返回 None。
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT identity_id FROM repo_ssh_bindings WHERE repo_id = ?",
                (repo_id,),
            ).fetchone()
        return int(row["identity_id"]) if row else None

    def set_repo_binding(self, repo_id: int, identity_id: int | None) -> None:
        """设置或清除仓库级 SSH 身份绑定。

        参数：
            repo_id: 仓库主键。
            identity_id: 身份 id；None 表示清除绑定。
        返回值：
            无。
        """
        with self._write_lock, self._connect() as conn:
            if identity_id is None:
                conn.execute("DELETE FROM repo_ssh_bindings WHERE repo_id = ?", (repo_id,))
            else:
                conn.execute(
                    """
                    INSERT INTO repo_ssh_bindings(repo_id, identity_id, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(repo_id) DO UPDATE
                    SET identity_id = excluded.identity_id, updated_at = excluded.updated_at
                    """,
                    (repo_id, identity_id, now_iso()),
                )

    @staticmethod
    def _identity_row(row: sqlite3.Row) -> dict[str, Any]:
        """把 SQLite 行转换为带布尔字段的身份字典。

        参数：
            row: ssh_identities 查询结果行。
        返回值：
            含布尔化 enabled / is_default 的字典。
        """
        data = dict(row)
        data["enabled"] = bool(data.get("enabled"))
        data["is_default"] = bool(data.get("is_default"))
        return data

    def insert_audit(self, record: dict[str, Any]) -> int:
        """写入一条审计记录。

        参数：
            record: 包含 ts/repo_id/repo_path/action/command/exit_code/ok/
                    duration_ms/forced/output_summary/identity_name/client_ip 的字典。
        返回值：
            新审计记录的 id。
        """
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_logs
                    (ts, repo_id, repo_path, action, command, exit_code, ok,
                     duration_ms, forced, output_summary, identity_name, client_ip)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record["ts"],
                    record.get("repo_id"),
                    record.get("repo_path", ""),
                    record["action"],
                    record["command"],
                    int(record["exit_code"]),
                    1 if record["ok"] else 0,
                    int(record["duration_ms"]),
                    1 if record.get("forced") else 0,
                    record.get("output_summary", ""),
                    record.get("identity_name"),
                    record.get("client_ip", ""),
                ),
            )
            return int(cursor.lastrowid)

    def query_audit(
        self,
        limit: int = 50,
        offset: int = 0,
        repo_id: int | None = None,
        action: str | None = None,
        only_failed: bool = False,
    ) -> tuple[int, list[dict[str, Any]]]:
        """分页查询审计日志。

        参数：
            limit: 每页条数（1-200）。
            offset: 偏移量。
            repo_id: 可选，按仓库过滤。
            action: 可选，按动作名精确过滤。
            only_failed: 为 True 时只返回失败记录。
        返回值：
            (符合条件的总条数, 当前页记录列表（按时间倒序）)。
        """
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))

        conditions: list[str] = []
        params: list[Any] = []
        if repo_id is not None:
            conditions.append("repo_id = ?")
            params.append(repo_id)
        if action:
            conditions.append("action = ?")
            params.append(action)
        if only_failed:
            conditions.append("ok = 0")
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""

        with self._connect() as conn:
            total = int(
                conn.execute(
                    f"SELECT COUNT(*) AS c FROM audit_logs {where}", params
                ).fetchone()["c"]
            )
            rows = conn.execute(
                f"""
                SELECT id, ts, repo_id, repo_path, action, command, exit_code,
                       ok, duration_ms, forced, output_summary, identity_name, client_ip
                FROM audit_logs {where}
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return total, [dict(row) for row in rows]

    def last_push_failure(self, repo_id: int) -> dict[str, Any] | None:
        """读取某仓库最近一次失败的推送审计记录。

        参数：
            repo_id: 仓库主键。
        返回值：
            {ts, action, command, output_summary}；没有失败记录时返回 None。
            用于“仅在存在 403 历史时提示凭据问题”，不发起网络请求。
        """
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT ts, action, command, output_summary FROM audit_logs
                WHERE repo_id = ? AND ok = 0 AND action IN ('push', 'push_set_upstream', 'push_force')
                ORDER BY id DESC LIMIT 1
                """,
                (repo_id,),
            ).fetchone()
        return dict(row) if row else None

    def list_audit_actions(self) -> list[str]:
        """列出审计表中出现过的动作名（用于前端筛选下拉）。

        参数：
            无。
        返回值：
            去重并按字母排序的动作名列表。
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT DISTINCT action FROM audit_logs ORDER BY action ASC"
            ).fetchall()
        return [str(row["action"]) for row in rows]

    def purge_audit(self, retention_days: int) -> int:
        """清理超过保留期的审计日志。

        参数：
            retention_days: 保留天数（>=1）。
        返回值：
            删除的行数。
        """
        cutoff = (
            datetime.now().astimezone() - timedelta(days=max(1, retention_days))
        ).isoformat(timespec="seconds")
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute("DELETE FROM audit_logs WHERE ts < ?", (cutoff,))
            return int(cursor.rowcount or 0)
