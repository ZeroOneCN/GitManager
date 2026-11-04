"""SSH 身份服务。

职责：扫描本机 ~/.ssh 下的私钥与 Host 别名，维护身份配置（仅路径与别名），
      解析仓库生效身份，并提供连接测试。
约束：绝不读取或保存私钥正文；扫描时仅检查文件首行的 PEM 头用于识别私钥；
      密钥文件只读，删除身份配置不删除磁盘文件。
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from .config import Settings
from .db import Database
from .git_runner import GitExecutor

_HOST_RE = re.compile(r"^[A-Za-z0-9._@:-]+$")


class SshService:
    """SSH 身份扫描、配置与测试服务。

    参数：
        settings: 运行配置（SSH 目录、ssh/ssh-keygen 路径、超时）。
        database: 数据库实例（身份与绑定持久化）。
        executor: 命令执行器（指纹读取与连接测试）。
    """

    def __init__(self, settings: Settings, database: Database, executor: GitExecutor) -> None:
        """初始化 SSH 身份服务。

        参数：
            settings: 运行配置。
            database: 数据库实例。
            executor: 命令执行器。
        返回值：
            无。
        """
        self.settings = settings
        self.database = database
        self.executor = executor

    # ------------------------------------------------------------------
    # 扫描
    # ------------------------------------------------------------------

    def scan(self) -> dict[str, Any]:
        """扫描 SSH 目录中的私钥与 config 中的 Host 别名。

        参数：
            无。
        返回值：
            含 ssh_dir/config_path/hosts/keys 的字典；keys 仅含路径、指纹与备注。
        """
        ssh_dir = self.settings.ssh_dir
        config_path = ssh_dir / "config"
        hosts, key_aliases = _parse_ssh_config(config_path)
        keys: list[dict[str, Any]] = []
        if ssh_dir.is_dir():
            for entry in sorted(ssh_dir.iterdir(), key=lambda item: item.name.lower()):
                if not entry.is_file():
                    continue
                name_lower = entry.name.lower()
                if name_lower.endswith(".pub") or name_lower in {
                    "config",
                    "known_hosts",
                    "known_hosts.old",
                    "authorized_keys",
                }:
                    continue
                if not _looks_like_private_key(entry):
                    continue
                key_path = str(entry.resolve())
                public_path = entry.with_name(entry.name + ".pub")
                fingerprint_info = None
                comment = ""
                if public_path.is_file():
                    fingerprint_info = self.executor.key_fingerprint(str(public_path))
                    comment = _public_key_comment(public_path)
                aliases = key_aliases.get(os.path.normcase(key_path), [])
                warnings: list[str] = []
                encrypted = _key_is_encrypted(entry)
                if encrypted:
                    warnings.append("私钥已加密，非交互模式下需要 ssh-agent 托管")
                if not public_path.is_file():
                    warnings.append("未找到同名公钥，无法获取指纹")
                configured = any(
                    os.path.normcase(item["key_path"]) == os.path.normcase(key_path)
                    for item in self.database.list_identities()
                )
                keys.append(
                    {
                        "key_path": key_path,
                        "name": entry.name,
                        "public_key_path": str(public_path) if public_path.is_file() else None,
                        "fingerprint": (fingerprint_info or {}).get("fingerprint"),
                        "bits": (fingerprint_info or {}).get("bits"),
                        "comment": comment or (fingerprint_info or {}).get("comment", ""),
                        "host_aliases": aliases,
                        "suggested_alias": aliases[0] if aliases else "",
                        "encrypted": encrypted,
                        "readable": os.access(entry, os.R_OK),
                        "warnings": warnings,
                        "configured": configured,
                    }
                )
        return {
            "ssh_dir": str(ssh_dir),
            "ssh_dir_exists": ssh_dir.is_dir(),
            "config_path": str(config_path),
            "config_exists": config_path.is_file(),
            "hosts": hosts,
            "keys": keys,
        }

    # ------------------------------------------------------------------
    # 身份 CRUD
    # ------------------------------------------------------------------

    def list_identities(self) -> list[dict[str, Any]]:
        """列出已配置身份，并附加公钥指纹、绑定数量与文件状态。

        参数：
            无。
        返回值：
            身份字典列表。
        """
        scanned = self.scan()
        fingerprints = {
            os.path.normcase(item["key_path"]): item for item in scanned["keys"]
        }
        items: list[dict[str, Any]] = []
        for identity in self.database.list_identities():
            info = fingerprints.get(os.path.normcase(identity["key_path"]), {})
            key_file = Path(identity["key_path"])
            item = dict(identity)
            item.update(
                {
                    "fingerprint": info.get("fingerprint"),
                    "public_key_path": info.get("public_key_path"),
                    "warnings": info.get("warnings", []),
                    "key_exists": key_file.is_file(),
                    "bound_repos": self.database.count_identity_bindings(identity["id"]),
                }
            )
            items.append(item)
        return items

    def create_identity(self, payload: dict[str, Any]) -> dict[str, Any]:
        """创建身份配置。

        参数：
            payload: name/key_path/host_alias/enabled/is_default。
        返回值：
            新身份字典。
        异常：
            ValueError: 字段校验失败或私钥/别名重复。
        """
        record = self._validate_payload(payload, identity_id=None)
        if self.database.find_identity(record["key_path"], record["host_alias"]):
            raise ValueError("同一私钥与 Host 别名的身份已存在")
        identity_id = self.database.insert_identity(record)
        return self.database.get_identity(identity_id) or {}

    def update_identity(self, identity_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """更新身份配置。

        参数：
            identity_id: 身份主键。
            payload: 可部分更新的字段。
        返回值：
            更新后的身份字典。
        异常：
            ValueError: 身份不存在或字段校验失败。
        """
        current = self.database.get_identity(identity_id)
        if current is None:
            raise ValueError("身份不存在")
        merged = {**current, **{key: value for key, value in payload.items() if value is not None}}
        record = self._validate_payload(merged, identity_id=identity_id)
        duplicate = self.database.find_identity(record["key_path"], record["host_alias"])
        if duplicate and duplicate["id"] != identity_id:
            raise ValueError("同一私钥与 Host 别名的身份已存在")
        updated = self.database.update_identity(identity_id, record)
        if updated is None:
            raise ValueError("身份不存在")
        return updated

    def delete_identity(self, identity_id: int) -> dict[str, Any]:
        """删除身份配置（不删除磁盘密钥文件）。

        参数：
            identity_id: 身份主键。
        返回值：
            被删除的身份字典。
        异常：
            ValueError: 身份不存在。
        """
        identity = self.database.delete_identity(identity_id)
        if identity is None:
            raise ValueError("身份不存在")
        return identity

    def set_default(self, identity_id: int) -> dict[str, Any]:
        """设置全局默认身份。

        参数：
            identity_id: 身份主键。
        返回值：
            更新后的身份字典。
        异常：
            ValueError: 身份不存在或已被禁用。
        """
        identity = self.database.get_identity(identity_id)
        if identity is None:
            raise ValueError("身份不存在")
        if not identity["enabled"]:
            raise ValueError("已禁用的身份不能设为默认")
        updated = self.database.set_default_identity(identity_id)
        if updated is None:
            raise ValueError("身份不存在")
        return updated

    def resolve_for_repo(self, repo_id: int) -> dict[str, Any] | None:
        """解析仓库当前生效的 SSH 身份（仓库绑定优先于全局默认）。

        参数：
            repo_id: 仓库主键。
        返回值：
            含 id/name/key_path/source 的身份字典；无可用身份时返回 None。
        """
        binding_id = self.database.get_repo_binding(repo_id)
        if binding_id is not None:
            bound = self.database.get_identity(binding_id)
            if bound and bound["enabled"]:
                return self._effective(bound, "repo")
        for identity in self.database.list_identities():
            if identity["enabled"] and identity["is_default"]:
                return self._effective(identity, "default")
        return None

    def bind_repo(self, repo_id: int, identity_id: int | None) -> dict[str, Any] | None:
        """设置或清除仓库级身份绑定。

        参数：
            repo_id: 仓库主键。
            identity_id: 身份 id；None 表示清除绑定。
        返回值：
            绑定后的生效身份；清除后回落到全局默认或 None。
        异常：
            ValueError: 身份不存在或已禁用。
        """
        if identity_id is not None:
            identity = self.database.get_identity(identity_id)
            if identity is None:
                raise ValueError("身份不存在")
            if not identity["enabled"]:
                raise ValueError("已禁用的身份不能被绑定")
        self.database.set_repo_binding(repo_id, identity_id)
        return self.resolve_for_repo(repo_id)

    def test_connection(self, identity_id: int, host_override: str | None = None) -> dict[str, Any]:
        """测试身份到 Host 的连接。

        参数：
            identity_id: 身份主键。
            host_override: 可选，覆盖身份中的 Host 别名。
        返回值：
            含 status/output/permission_issue 的测试结果，并附加身份名。
        异常：
            ValueError: 身份不存在、无 Host 且未提供覆盖值。
        """
        identity = self.database.get_identity(identity_id)
        if identity is None:
            raise ValueError("身份不存在")
        host = (host_override or identity["host_alias"] or "").strip()
        if not host:
            raise ValueError("该身份未配置 Host 别名，请先填写或临时指定")
        if not _HOST_RE.match(host):
            raise ValueError("Host 别名包含非法字符")
        result = self.executor.test_ssh(identity, host, self.settings.ssh_test_timeout_seconds)
        result["identity_name"] = identity["name"]
        result["host"] = host
        return result

    def _validate_payload(self, payload: dict[str, Any], identity_id: int | None) -> dict[str, Any]:
        """校验并规范化身份字段。

        参数：
            payload: 待校验字段。
            identity_id: 更新时的身份 id（新建为 None）。
        返回值：
            规范化后的 name/key_path/host_alias/enabled/is_default 字典。
        异常：
            ValueError: 字段非法。
        """
        name = str(payload.get("name") or "").strip()
        if not name or len(name) > 60:
            raise ValueError("显示名必填且不超过 60 个字符")
        raw_key = str(payload.get("key_path") or "").strip()
        if not raw_key:
            raise ValueError("私钥路径必填")
        key_path = Path(os.path.expandvars(raw_key)).expanduser()
        if not key_path.is_absolute():
            raise ValueError("私钥路径必须是绝对路径")
        if any(ch in raw_key for ch in ("\n", "\r", "\x00")):
            raise ValueError("私钥路径包含非法控制字符")
        if not key_path.is_file():
            raise ValueError("私钥文件不存在")
        if not _looks_like_private_key(key_path):
            raise ValueError("目标文件不是 OpenSSH/PEM 私钥")
        host_alias = str(payload.get("host_alias") or "").strip()
        if host_alias and not _HOST_RE.match(host_alias):
            raise ValueError("Host 别名只能包含字母、数字、点、下划线、@、: 与连字符")
        return {
            "name": name,
            "key_path": str(key_path.resolve()),
            "host_alias": host_alias,
            "enabled": bool(payload.get("enabled", True)),
            "is_default": bool(payload.get("is_default", False)),
        }

    @staticmethod
    def _effective(identity: dict[str, Any], source: str) -> dict[str, Any]:
        """构造生效身份的对外视图。

        参数：
            identity: 身份记录。
            source: 来源（repo 或 default）。
        返回值：
            仅含展示所需字段的字典。
        """
        return {
            "id": identity["id"],
            "name": identity["name"],
            "key_path": identity["key_path"],
            "host_alias": identity["host_alias"],
            "source": source,
        }


def _parse_ssh_config(path: Path) -> tuple[list[str], dict[str, list[str]]]:
    """解析 ssh config 中的 Host 别名与 IdentityFile 映射。

    参数：
        path: config 文件路径。
    返回值：
        (可用 Host 别名列表, {规范化私钥路径: 别名列表})。
    """
    hosts: list[str] = []
    aliases: dict[str, list[str]] = {}
    if not path.is_file():
        return hosts, aliases
    current: list[str] = []
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return hosts, aliases
    for raw in lines:
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            continue
        keyword, value = parts[0].lower(), parts[1].strip()
        if keyword == "host":
            current = [
                item
                for item in value.split()
                if "*" not in item and "?" not in item and not item.startswith("!")
            ]
            for item in current:
                if item not in hosts:
                    hosts.append(item)
        elif keyword == "identityfile":
            raw_path = value.strip().strip('"')
            if any(token in raw_path for token in ("%d", "%h", "%r", "%u", "%l")):
                continue
            key_path = Path(os.path.expandvars(raw_path)).expanduser()
            if not key_path.is_absolute():
                key_path = Path.home() / ".ssh" / key_path
            normalized = os.path.normcase(str(key_path.resolve()))
            if current:
                aliases[normalized] = _dedupe(aliases.get(normalized, []) + current)
    return hosts, aliases


def _dedupe(items: list[str]) -> list[str]:
    """保持顺序去重。

    参数：
        items: 字符串列表。
    返回值：
        去重后的列表。
    """
    seen: set[str] = set()
    result: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _looks_like_private_key(path: Path) -> bool:
    """通过文件首部判断是否为私钥（不读取、不保存正文）。

    参数：
        path: 候选文件。
    返回值：
        首部包含 PRIVATE KEY 标记时返回 True。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return False
    return b"PRIVATE KEY" in head


def _key_is_encrypted(path: Path) -> bool:
    """判断私钥是否带加密标记（仅检查首部）。

    参数：
        path: 私钥文件。
    返回值：
        带 ENCRYPTED 标记时返回 True。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(1024)
    except OSError:
        return False
    return b"ENCRYPTED" in head


def _public_key_comment(public_key_path: Path) -> str:
    """读取公钥注释（降级展示用，不涉及私钥内容）。

    参数：
        public_key_path: 公钥文件路径。
    返回值：
        公钥行最后一列（通常是 user@host）；读取失败返回空字符串。
    """
    try:
        line = public_key_path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return ""
    parts = line.split()
    return parts[-1] if len(parts) >= 3 else ""
