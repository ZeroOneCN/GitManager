"""运行配置模块。

职责：从项目根目录的 .env 读取全部运行参数，并在启动阶段做安全校验。
约束：端口、git 路径、白名单、保留天数等不得在代码中硬编码。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

# 项目根目录（app/config.py 的上一级）
PROJECT_ROOT = Path(__file__).resolve().parent.parent

# 允许的服务监听地址：仅本机回环
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


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


@dataclass(frozen=True)
class Settings:
    """应用运行配置（不可变）。"""

    host: str
    port: int
    git_executable: str
    db_path: Path
    allowed_roots: tuple[Path, ...]
    log_retention_days: int
    pull_auto_stash_default: bool
    git_timeout_seconds: int
    output_summary_limit: int = 2000

    def is_path_allowed(self, path: Path) -> bool:
        """判断给定绝对路径是否位于白名单根目录内。

        参数：
            path: 已 resolve 的绝对路径。
        返回值：
            位于任一白名单根目录（含根目录本身）时返回 True。
        """
        target = os.path.normcase(str(path))
        for root in self.allowed_roots:
            root_text = os.path.normcase(str(root))
            if target == root_text or target.startswith(root_text + os.sep):
                return True
        return False


def load_settings() -> Settings:
    """加载并校验运行配置。

    参数：
        无（从项目根目录 .env 与进程环境变量读取）。
    返回值：
        校验通过的 Settings 实例。
    异常：
        ConfigError: 监听地址非回环、git 可执行文件缺失、白名单为空等不安全配置。
    """
    load_dotenv(PROJECT_ROOT / ".env", override=False)

    host = (os.getenv("APP_HOST") or "127.0.0.1").strip()
    if host not in ALLOWED_HOSTS:
        raise ConfigError(
            f"APP_HOST={host} 不被允许：服务只允许监听 127.0.0.1 或 localhost"
        )

    port = _env_int("APP_PORT", 8765, minimum=1)

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

    return Settings(
        host=host,
        port=port,
        git_executable=str(Path(git_executable).resolve()),
        db_path=db_path.resolve(),
        allowed_roots=tuple(roots),
        log_retention_days=_env_int("GITMANAGER_LOG_RETENTION_DAYS", 90, minimum=1),
        pull_auto_stash_default=_env_bool("PULL_AUTO_STASH_DEFAULT", False),
        git_timeout_seconds=_env_int("GIT_OPERATION_TIMEOUT_SECONDS", 120, minimum=5),
    )
