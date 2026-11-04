"""SQLite 存储层。

职责：仓库配置与操作审计的持久化；统一封装连接、建表与查询。
约束：仓库移除只删配置行，不删除审计历史；所有写操作串行化，避免并发写冲突。
"""

from __future__ import annotations

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
    output_summary TEXT NOT NULL DEFAULT ''
);

CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts);
CREATE INDEX IF NOT EXISTS idx_audit_repo ON audit_logs(repo_id);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_logs(action);
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

    def init(self, retention_days: int) -> int:
        """初始化数据库：建目录、建表、启用 WAL，并清理超期审计。

        参数：
            retention_days: 审计日志保留天数。
        返回值：
            本次清理删除的审计行数。
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._write_lock, self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(_SCHEMA)
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
        return dict(row)

    def insert_audit(self, record: dict[str, Any]) -> int:
        """写入一条审计记录。

        参数：
            record: 包含 ts/repo_id/repo_path/action/command/exit_code/ok/
                    duration_ms/forced/output_summary 的字典。
        返回值：
            新审计记录的 id。
        """
        with self._write_lock, self._connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO audit_logs
                    (ts, repo_id, repo_path, action, command, exit_code, ok,
                     duration_ms, forced, output_summary)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                       ok, duration_ms, forced, output_summary
                FROM audit_logs {where}
                ORDER BY id DESC LIMIT ? OFFSET ?
                """,
                [*params, limit, offset],
            ).fetchall()
        return total, [dict(row) for row in rows]

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
