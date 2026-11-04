"""运行配置模块。

职责：从项目根目录的 .env 读取全部运行参数，并在启动阶段做安全校验。
约束：端口、git 路径、白名单、保留天数等不得在代码中硬编码。
"""

from __future__ import annotations

import os
import re
import secrets
import socket
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/config.py 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 回环监听地址集合：命中时视为仅本机访问，可免令牌
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1"}

# 允许的 Host 头默认值（防 DNS rebinding）
DEFAULT_ALLOWED_HOSTS = "127.0.0.1;localhost"

# .env 中令牌行的匹配模式（用于自动生成后写回）
_TOKEN_LINE = re.compile(r"^GITMANAGER_ACCESS_TOKEN=.*$", re.MULTILINE)


class ConfigError(RuntimeError):
    """配置错误：启动阶段抛出，用于拒绝不安全的配置。"""


def _env_bool(name: str, default: bool) -> bool:
    """读取布尔型环境变量。

    参数：
        name: 环境变量名。
        default: 变量缺失或无法解析时的默认值。
    返回值：
        解析后的布尔值（true/1/yes/on 视为真）。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int, minimum: int = 1) -> int:
    """读取整型环境变量。

    参数：
        name: 环境变量名。
        default: 变量缺失时的默认值。
        minimum: 允许的最小值，低于该值时回退到默认值。
    返回值：
        解析后的整数。
    """
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError:
        return default
    return value if value >= minimum else default


def _env_text(name: str, default: str) -> str:
    """读取文本型环境变量（自动展开 %VAR% 与 ~）。

    参数：
        name: 环境变量名。
        default: 变量缺失或为空时的默认值。
    返回值：
        去除首尾空白并展开环境变量后的文本。
    """
    raw = os.getenv(name)
    value = raw if raw is not None and raw.strip() else default
    return os.path.expandvars(value.strip())


def _env_list(name: str, default: str) -> tuple[str, ...]:
    """读取分号分隔的列表型环境变量。

    参数：
        name: 环境变量名。
        default: 变量缺失时的默认分号分隔文本。
    返回值：
        去空、去重后的字符串元组。
    """
    raw = os.getenv(name) or default
    items: list[str] = []
    for part in raw.replace(",", ";").split(";"):
        item = part.strip()
        if item and item not in items:
            items.append(item)
    return tuple(items)


def is_loopback_host(host: str) -> bool:
    """判断监听地址是否属于本机回环。

    参数：
        host: 监听地址文本。
    返回值：
        127.0.0.1 / localhost / ::1 时返回 True。
    """
    return host.strip().lower() in LOOPBACK_HOSTS


def detect_lan_ipv4() -> list[str]:
    """探测本机可用的局域网 IPv4 地址（不发起真实网络请求）。

    参数：
        无。
    返回值：
        去重后的非回环 IPv4 列表；探测失败时返回空列表。
    """
    addresses: list[str] = []

    def add(value: str) -> None:
        """把候选地址加入结果列表（过滤空值与回环）。

        参数：
            value: IPv4 字符串。
        返回值：
            无。
        """
        text = value.strip()
        if text and text not in addresses and not text.startswith("127."):
            addresses.append(text)

    # 方式一：UDP connect 仅由路由表选出出口网卡地址，不会真的发包
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("8.8.8.8", 80))
            add(probe.getsockname()[0])
    except OSError:
        pass
    # 方式二：解析主机名（覆盖多网卡场景）
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            add(info[4][0])
    except OSError:
        pass
    return addresses


def build_access_urls(host: str, port: int) -> list[str]:
    """生成服务访问地址列表（本机 + 探测到的局域网 IPv4）。

    参数：
        host: 监听地址。
        port: 服务端口。
    返回值：
        可直接点击的 http 地址列表；监听回环时只含本机地址。
    """
    urls: list[str] = [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]
    if not is_loopback_host(host):
        for ip in detect_lan_ipv4():
            urls.append(f"http://{ip}:{port}")
    return urls


def firewall_hint(port: int) -> str:
    """生成 Windows 防火墙放行命令提示（不自动执行）。

    参数：
        port: 服务端口。
    返回值：
        供用户手工执行的 netsh 命令文本。
    """
    return (
        f'netsh advfirewall firewall add rule name="GitManager Web {port}" '
        f'dir=in action=allow protocol=TCP localport={port} profile=private'
    )


def _ensure_access_token(env_path: Path, lan_enabled: bool) -> str:
    """确保局域网模式存在访问令牌（缺失时生成并写回 .env）。

    参数：
        env_path: .env 文件路径。
        lan_enabled: 是否为非回环监听（只有此时才强制令牌）。
    返回值：
        当前生效的令牌文本；回环模式且未配置时返回空字符串。
    """
    token = (os.getenv("GITMANAGER_ACCESS_TOKEN") or "").strip()
    if token or not lan_enabled:
        return token

    token = secrets.token_urlsafe(32)
    try:
        original = env_path.read_text(encoding="utf-8") if env_path.exists() else ""
        if _TOKEN_LINE.search(original):
            updated = _TOKEN_LINE.sub(f"GITMANAGER_ACCESS_TOKEN={token}", original)
        else:
            separator = "" if not original or original.endswith("\n") else "\n"
            updated = f"{original}{separator}GITMANAGER_ACCESS_TOKEN={token}\n"
        env_path.write_text(updated, encoding="utf-8")
    except OSError as exc:
        print(f"[警告] 访问令牌写回 .env 失败：{exc}")
    os.environ["GITMANAGER_ACCESS_TOKEN"] = token
    print("=" * 62)
    print("已为局域网访问自动生成访问令牌（已写入 .env）：")
    print(f"  GITMANAGER_ACCESS_TOKEN={token}")
    print("请妥善保管；如需更换，删除该行后重启即可重新生成。")
    print("=" * 62)
    return token


def _strip_trailing_separator(text: str) -> str:
    """去除路径末尾多余的目录分隔符（保留盘符根目录）。

    参数：
        text: 绝对路径文本。
    返回值：
        去掉末尾 "\\" 或 "/" 的路径；"E:\\" 这类盘符根保持不变。
    """
    trimmed = text.rstrip("\\/")
    if not trimmed:
        return text
    if os.name == "nt" and len(trimmed) == 2 and trimmed[1] == ":":
        return trimmed + os.sep
    return trimmed


def normalize_repo_path(path: str | os.PathLike[str]) -> str:
    """规范化仓库路径：解析真实路径并去掉尾部分隔符。

    参数：
        path: 用户输入或数据库中的路径文本（可含引号、环境变量、.. 或符号链接）。
    返回值：
        可入库与比对的真实绝对路径（保留磁盘上的真实大小写）。
    """
    text = os.path.expandvars(str(path).strip().strip('"'))
    try:
        real = os.path.realpath(text)
    except OSError:
        real = os.path.abspath(text)
    return _strip_trailing_separator(real)


def repo_path_key(path: str | os.PathLike[str]) -> str:
    """生成用于去重比对的路径键（Windows 下大小写不敏感）。

    参数：
        path: 任意写法的路径文本。
    返回值：
        规范化后（Windows 下再 casefold）的字符串，供集合与数据库比对使用。
    """
    normalized = normalize_repo_path(path)
    return normalized.casefold() if os.name == "nt" else normalized


def path_is_under(path: Path, roots: tuple[Path, ...] | list[Path]) -> bool:
    """判断路径是否位于任一允许根目录之下（Windows 大小写不敏感）。

    参数：
        path: 已 resolve 的绝对路径。
        roots: 允许根目录集合。
    返回值：
        位于某个根目录（含根目录本身）时返回 True。
    """
    target = os.path.normcase(str(path))
    for root in roots:
        root_text = os.path.normcase(str(root))
        if target == root_text or target.startswith(root_text + os.sep):
            return True
    return False


@dataclass(frozen=True)
class Settings:
    """应用运行配置（不可变）。"""

    host: str
    port: int
    access_token: str
    allowed_hosts: tuple[str, ...]
    git_executable: str
    db_path: Path
    allowed_roots: tuple[Path, ...]
    log_retention_days: int
    pull_auto_stash_default: bool
    git_timeout_seconds: int
    output_summary_limit: int = 2000
    scan_max_depth: int = 6
    scan_result_limit: int = 200
    scan_noise_dirs: tuple[str, ...] = ()
    ssh_dir: Path = Path("")
    ssh_executable: str = "ssh"
    ssh_keygen_executable: str = "ssh-keygen"
    ssh_test_timeout_seconds: int = 10

    @property
    def lan_enabled(self) -> bool:
        """是否为非回环监听（即允许局域网访问）。

        参数：
            无。
        返回值：
            监听地址不是回环地址时返回 True。
        """
        return not is_loopback_host(self.host)

    @property
    def auth_required(self) -> bool:
        """是否需要令牌鉴权（局域网监听时强制开启）。

        参数：
            无。
        返回值：
            非回环监听且已配置令牌时返回 True。
        """
        return self.lan_enabled and bool(self.access_token)

    def is_path_allowed(self, path: Path) -> bool:
        """判断给定绝对路径是否位于 .env 初始白名单根目录内。

        参数：
            path: 已 resolve 的绝对路径。
        返回值：
            位于任一初始根目录（含根目录本身）时返回 True。
        """
        return path_is_under(path, self.allowed_roots)


def load_settings() -> Settings:
    """加载并校验运行配置。

    参数：
        无（从项目根目录 .env 与进程环境变量读取）。
    返回值：
        校验通过的 Settings 实例。
    异常：
        ConfigError: git 可执行文件缺失、白名单为空等不安全配置。
    """
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    host = (os.getenv("GITMANAGER_HOST") or "127.0.0.1").strip()
    if not host:
        host = "127.0.0.1"

    port = _env_int("GITMANAGER_PORT", 8765, minimum=1)
    lan_enabled = not is_loopback_host(host)
    access_token = _ensure_access_token(PROJECT_ROOT / ".env", lan_enabled)

    allowed_hosts = _env_list("GITMANAGER_ALLOWED_HOSTS", DEFAULT_ALLOWED_HOSTS)
    allowed_hosts = tuple(item.lower() for item in allowed_hosts)
    if lan_enabled and all(is_loopback_host(item) for item in allowed_hosts):
        # 局域网监听但白名单只有回环地址：从其它设备访问会被 400 拒绝，需明确提醒
        print(
            "[安全提示] 当前监听非回环地址，但 GITMANAGER_ALLOWED_HOSTS 仅包含回环地址；"
            "请把本机局域网 IP 或主机名显式加入该列表，否则局域网请求会被 400 拒绝。"
        )

    git_executable = (os.getenv("GIT_EXECUTABLE") or "git").strip()
    if not Path(git_executable).is_file():
        raise ConfigError(f"GIT_EXECUTABLE 不存在：{git_executable}")

    db_path = Path(os.getenv("GITMANAGER_DB") or "data/gitmanager.db").expanduser()
    if not db_path.is_absolute():
        db_path = PROJECT_ROOT / db_path

    raw_roots = os.getenv("GITMANAGER_ALLOWED_ROOTS") or ""
    roots: list[Path] = []
    for item in raw_roots.split(";"):
        item = item.strip()
        if not item:
            continue
        candidate = Path(item).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        candidate = candidate.resolve()
        if candidate.is_dir():
            roots.append(candidate)
    if not roots:
        raise ConfigError(
            "GITMANAGER_ALLOWED_ROOTS 未配置有效目录：至少需要一个已存在的白名单根目录"
        )

    git_resolved = Path(git_executable).resolve()
    # Git for Windows 自带 OpenSSH：优先使用其内置 ssh / ssh-keygen，避免依赖 PATH
    git_base = git_resolved.parent.parent
    bundled_ssh = git_base / "usr" / "bin" / "ssh.exe"
    bundled_keygen = git_base / "usr" / "bin" / "ssh-keygen.exe"
    ssh_default = str(bundled_ssh) if bundled_ssh.is_file() else "ssh"
    keygen_default = str(bundled_keygen) if bundled_keygen.is_file() else "ssh-keygen"

    ssh_dir_text = _env_text("SSH_DIR", "%USERPROFILE%\\.ssh")
    ssh_dir = Path(ssh_dir_text).expanduser()

    return Settings(
        host=host,
        port=port,
        access_token=access_token,
        allowed_hosts=allowed_hosts,
        git_executable=str(git_resolved),
        db_path=db_path.resolve(),
        allowed_roots=tuple(roots),
        log_retention_days=_env_int("GITMANAGER_LOG_RETENTION_DAYS", 90, minimum=1),
        pull_auto_stash_default=_env_bool("PULL_AUTO_STASH_DEFAULT", False),
        git_timeout_seconds=_env_int("GIT_OPERATION_TIMEOUT_SECONDS", 120, minimum=5),
        scan_max_depth=_env_int("GITMANAGER_SCAN_MAX_DEPTH", 6, minimum=1),
        scan_result_limit=_env_int("GITMANAGER_SCAN_RESULT_LIMIT", 200, minimum=1),
        scan_noise_dirs=_env_list(
            "GITMANAGER_SCAN_NOISE_DIRS",
            "node_modules;.venv;venv;dist;build;target;bin;obj;__pycache__;"
            ".git;.cache;.idea;.vscode;.pytest_cache;.mypy_cache;.tox",
        ),
        ssh_dir=ssh_dir,
        ssh_executable=_env_text("SSH_EXECUTABLE", ssh_default),
        ssh_keygen_executable=_env_text("SSH_KEYGEN_EXECUTABLE", keygen_default),
        ssh_test_timeout_seconds=_env_int("SSH_TEST_TIMEOUT_SECONDS", 10, minimum=3),
    )
