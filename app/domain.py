"""Git 输出解析与错误映射。

职责：把 git CLI 的原始输出转换为前端可用的结构化数据，并把英文错误
映射为可读中文错误码。
"""

from __future__ import annotations

import re
from typing import Any

# git status 冲突状态对
_CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}

# 允许从 HTTPS 转换为 SSH 的常见托管域名（其他域名不做猜测）
_SSH_HOSTS = {"github.com", "gitee.com", "gitlab.com"}


def parse_status_porcelain_z(output: str) -> dict[str, Any]:
    """解析 `git status --porcelain=v1 -z` 输出。

    参数：
        output: git 原样输出（NUL 分隔，重命名条目紧跟原路径）。
    返回值：
        {
          "staged"/"modified"/"deleted"/"untracked"/"conflicts":
              [{path, code, old_path?, renamed?}],
          "counts": {staged, modified, deleted, untracked, conflicts, total}
        }；
        其中 total 为去重后的脏文件总数（同一文件可能同时处于 staged 与 modified）。
        说明：工作区已删除（Y=D）归入 deleted，暂存区已删除（X=D）归入 staged。
    """
    staged: list[dict[str, Any]] = []
    modified: list[dict[str, Any]] = []
    deleted: list[dict[str, Any]] = []
    untracked: list[dict[str, Any]] = []
    conflicts: list[dict[str, Any]] = []
    unique_paths: set[str] = set()

    tokens = output.split("\x00")
    index = 0
    while index < len(tokens):
        entry = tokens[index]
        index += 1
        if not entry or len(entry) < 4:
            continue
        x, y = entry[0], entry[1]
        path = entry[3:]
        old_path: str | None = None
        # 重命名/复制条目后紧跟原路径，单独记录后跳过
        if x in ("R", "C") and index < len(tokens):
            old_path = tokens[index]
            index += 1
        if not path:
            continue

        item: dict[str, Any] = {"path": path, "code": f"{x}{y}"}
        if old_path:
            item["old_path"] = old_path
            item["renamed"] = True
        unique_paths.add(path)

        if x == "?":
            untracked.append(item)
        elif x == "!":
            continue
        elif f"{x}{y}" in _CONFLICT_CODES:
            conflicts.append(item)
        else:
            # 暂存区变更（含新增、修改、删除、重命名）
            if x != " ":
                staged.append(item)
            # 工作区未暂存变更：删除单独成类，修改/类型变化归入 modified
            if y == "D":
                deleted.append({**item, "code": f"{x}D"})
            elif y in {"M", "T", "R", "C"}:
                modified.append({**item, "code": f"{x}{y}"})

    return {
        "staged": staged,
        "modified": modified,
        "deleted": deleted,
        "untracked": untracked,
        "conflicts": conflicts,
        "counts": {
            "staged": len(staged),
            "modified": len(modified),
            "deleted": len(deleted),
            "untracked": len(untracked),
            "conflicts": len(conflicts),
            "total": len(unique_paths),
        },
    }


def parse_ahead_behind(output: str) -> dict[str, int] | None:
    """解析 `git rev-list --left-right --count HEAD...@{u}` 输出。

    参数：
        output: 形如 "2\\t1\\n" 的文本（左=本地领先，右=远端领先）。
    返回值：
        {"ahead": 2, "behind": 1}；无法解析时返回 None。
    """
    parts = output.strip().split()
    if len(parts) != 2:
        return None
    try:
        return {"ahead": int(parts[0]), "behind": int(parts[1])}
    except ValueError:
        return None


def map_git_error(
    action: str,
    stderr: str,
    exit_code: int,
    timed_out: bool = False,
) -> tuple[str, str]:
    """将 git 失败输出映射为错误码与中文原因。

    参数：
        action: 业务动作名（如 pull / push / commit）。
        stderr: git 标准错误输出。
        exit_code: 退出码。
        timed_out: 是否因超时失败。
    返回值：
        (错误码, 面向用户的中文原因)。
    """
    text = (stderr or "").lower()
    first_line = ""
    for line in (stderr or "").splitlines():
        stripped = line.strip()
        if stripped:
            first_line = stripped
            break

    if timed_out:
        return "timeout", "git 命令执行超时，请检查网络或仓库状态后重试。"

    if "index.lock" in text:
        return (
            "lock_conflict",
            "Git 索引被其他进程占用（存在 index.lock），请稍后重试。",
        )
    # HTTPS 403：凭据账号无写权限（必须在通用 network 判定之前处理）
    forbidden = _match_permission_denied(stderr)
    if forbidden:
        owner, account = forbidden
        return (
            "remote_forbidden",
            f"当前 HTTPS 凭据账号 {account} 对 {owner} 无写权限。"
            "请改用有权限的账号凭据，或把远端切换为 SSH 地址。",
        )
    if "returned error: 403" in text or "returned error: 403" in stderr.lower():
        return (
            "remote_forbidden",
            "远端返回 403（无权限）。请检查当前 HTTPS 凭据账号对该仓库是否有写权限。",
        )
    if "host key verification failed" in text:
        return (
            "ssh_host_key",
            "SSH 主机密钥校验失败（Host key verification failed）：本机 known_hosts 不信任该主机。",
        )
    if "permission denied (publickey)" in text:
        return (
            "ssh_publickey",
            "SSH 公钥认证失败（Permission denied (publickey)）：当前生效的 SSH 身份未被远端接受。",
        )
    if "could not resolve host" in text or "network is unreachable" in text:
        return "network_error", "无法解析远端主机或网络不可达，请检查网络与远端地址。"
    if "authentication failed" in text or "returned error: 401" in text:
        return "auth_error", "远端认证失败：HTTPS 凭据无效或已过期，请重新登录。"
    if "permission denied" in text or "publickey" in text:
        return "auth_error", "远端认证失败，请检查本机 SSH/凭据配置。"
    if (
        "unable to access" in text
        or "failed to connect" in text
        or "connection timed out" in text
        or "could not read from remote repository" in text
    ):
        return "network_error", "连接远端失败，请检查网络、远端地址或访问权限。"
    if (
        "no tracking information" in text
        or "no upstream branch" in text
        or "has no upstream" in text
        or "no upstream configured" in text
    ):
        return "no_upstream", "当前分支未设置 upstream，无法确定拉取/推送目标。"
    if action.startswith("push") and (
        "non-fast-forward" in text
        or "rejected" in text
        or "fetch first" in text
        or "stale info" in text
    ):
        return "push_rejected", "推送被远端拒绝（远端有本地没有的提交），请先 pull 后再推送。"
    if "not possible to fast-forward" in text or "diverging branches" in text:
        return "non_fast_forward", "无法快进合并：本地与远端已分叉，需要手动处理后再 pull。"
    if "non-fast-forward" in text:
        return "non_fast_forward", "无法快进：远端存在本地没有的提交或历史不一致。"
    if (
        "your local changes" in text
        or "would be overwritten" in text
        or "please commit your changes or stash" in text
    ):
        return "dirty_worktree", "工作区存在会被覆盖的未提交改动，请先提交或选择自动 stash。"
    if "couldn't find remote ref" in text:
        return "remote_ref_missing", "远端不存在目标分支，请检查 upstream 配置。"

    detail = first_line or f"退出码 {exit_code}"
    return "git_error", f"git {action} 失败：{detail}"


def _match_permission_denied(text: str) -> tuple[str, str] | None:
    """从 git 输出中解析 403 的仓库与被拒账号。

    参数：
        text: git 原始输出（stdout/stderr 合并文本）。
    返回值：
        (owner/repo, account)；未匹配时返回 None。
        例如 "Permission to lemomo-ai/lemo-opuscar.git denied to ZeroOneCN." →
        ("lemomo-ai/lemo-opuscar.git", "ZeroOneCN")。
    """
    match = re.search(
        r"permission to\s+(.+?)\s+denied to\s+([^\s]+)",
        text or "",
        flags=re.IGNORECASE,
    )
    if not match:
        return None
    owner = match.group(1).strip().rstrip(".")
    account = match.group(2).strip().rstrip(".,;:。")
    return owner, account


def _push_rejected(title: str, detail: str) -> dict[str, Any]:
    """构造推送被拒绝的统一诊断结构（内部辅助）。

    参数：
        title: 简短标题。
        detail: 中文说明。
    返回值：
        诊断字典。
    """
    return {
        "code": "push_rejected",
        "title": title,
        "detail": detail,
        "suggestions": [
            {"key": "pull", "label": "先拉取再推送", "hint": "git pull --ff-only 后重试"},
            {"key": "view_audit", "label": "查看审计原文", "hint": "在审计页查看完整命令与输出"},
        ],
    }


def classify_remote_error(stderr: str, stdout: str = "") -> dict[str, Any] | None:
    """把远端类 git 失败输出归类为结构化诊断。

    参数：
        stderr: git 标准错误输出。
        stdout: git 标准输出（部分错误只出现在 stdout）。
    返回值：
        {code, title, detail, suggestions[]}；无法归类时返回 None。
        suggestions 中的 key 供前端映射到具体动作入口。
    """
    text = "\n".join(part for part in (stdout or "", stderr or "") if part).strip()
    lower = text.lower()
    if not lower:
        return None

    forbidden = _match_permission_denied(text)
    if forbidden:
        owner, account = forbidden
        return {
            "code": "http_403_forbidden",
            "title": "HTTPS 凭据账号无写权限（403）",
            "detail": f"当前 HTTPS 凭据账号 {account} 对 {owner} 无写权限。"
            "SSH 密钥只有在远端为 SSH 地址时才会被使用。",
            "suggestions": [
                {"key": "use_ssh", "label": "改用 SSH 地址", "hint": "把 remote 切换为 git@host:owner/repo.git"},
                {"key": "https_credentials", "label": "管理 HTTPS 凭据", "hint": "清除该 host 凭据后重新登录"},
                {"key": "ssh_test", "label": "测试 SSH 身份", "hint": "确认密钥已被远端接受"},
            ],
        }
    if "returned error: 403" in lower:
        return {
            "code": "http_403",
            "title": "远端拒绝访问（403）",
            "detail": "远端返回 403：当前 HTTPS 凭据对该仓库没有写权限，或该账号无权访问。",
            "suggestions": [
                {"key": "https_credentials", "label": "管理 HTTPS 凭据", "hint": "清除该 host 凭据后重新登录"},
                {"key": "use_ssh", "label": "改用 SSH 地址", "hint": "若该仓库支持 SSH，可避免 HTTPS 凭据问题"},
            ],
        }
    if "returned error: 401" in lower or "authentication failed" in lower:
        return {
            "code": "http_401_auth",
            "title": "HTTPS 凭据无效或已过期（401）",
            "detail": "远端认证失败：本机缓存的口令或令牌不可用，需要重新登录。",
            "suggestions": [
                {"key": "https_credentials", "label": "重新登录 HTTPS 凭据", "hint": "清除旧凭据后在独立窗口登录"},
                {"key": "use_ssh", "label": "改用 SSH 地址", "hint": "已配置 SSH 身份时可直接切换"},
            ],
        }
    if "host key verification failed" in lower:
        return {
            "code": "ssh_host_key",
            "title": "SSH 主机密钥不受信任",
            "detail": "本机 known_hosts 不信任该主机，连接被拒绝（Host key verification failed）。",
            "suggestions": [
                {"key": "ssh_test", "label": "测试 SSH 身份", "hint": "测试时会以 accept-new 方式确认主机密钥"},
                {"key": "view_audit", "label": "查看原始输出", "hint": "核对主机指纹后再决定是否信任"},
            ],
        }
    if "permission denied (publickey)" in lower:
        return {
            "code": "ssh_publickey",
            "title": "SSH 公钥认证失败",
            "detail": "当前生效的 SSH 身份未被远端接受（Permission denied (publickey)）。",
            "suggestions": [
                {"key": "ssh_test", "label": "测试当前 SSH 身份", "hint": "确认私钥、公钥是否已添加到远端账号"},
                {"key": "manage_ssh", "label": "管理 SSH 身份", "hint": "为该仓库绑定正确的密钥"},
            ],
        }
    if "non-fast-forward" in lower or "[rejected]" in lower or "fetch first" in lower:
        return _push_rejected(
            "推送被远端拒绝（non-fast-forward）",
            "远端存在本地没有的提交。请先拉取（ff-only）合并后再推送，或与协作者确认分支策略。",
        )
    if "protected branch" in lower or "pre-receive hook declined" in lower:
        return _push_rejected(
            "远端受保护分支拒绝推送",
            "该分支被远端保护规则拦截（protected branch / hook declined）。请改用合并请求或联系仓库管理员。",
        )
    if (
        "could not resolve host" in lower
        or "network is unreachable" in lower
        or "connection timed out" in lower
        or "failed to connect" in lower
        or "proxy" in lower
        or "unable to access" in lower
    ):
        return {
            "code": "network_error",
            "title": "网络或代理连接失败",
            "detail": "无法解析主机或连接被中断，请检查网络、代理与远端地址。",
            "suggestions": [
                {"key": "retry", "label": "重试", "hint": "网络恢复后重新执行"},
                {"key": "view_audit", "label": "查看原始输出", "hint": "确认失败发生在解析、连接还是代理环节"},
            ],
        }
    return None


def detect_remote_protocol(url: str) -> str:
    """识别远端 URL 的协议类型。

    参数：
        url: remote 的 fetch/push 地址。
    返回值：
        "https" | "ssh" | "local" | "unknown"。
    """
    text = (url or "").strip()
    if not text:
        return "unknown"
    lower = text.lower()
    if lower.startswith("https://") or lower.startswith("http://"):
        return "https"
    if lower.startswith("ssh://") or re.match(r"^[^\s@]+@[^\s:]+:", text):
        return "ssh"
    if lower.startswith("file://") or re.match(r"^[a-z]:[\\/]", lower) or text.startswith("\\\\"):
        return "local"
    return "unknown"


def https_to_ssh(url: str) -> dict[str, Any]:
    """把常见托管平台的 HTTPS 地址转换为 SSH 地址（不做任何猜测）。

    参数：
        url: 原始 remote 地址。
    返回值：
        {ok: True, ssh_url, host, owner_repo} 或
        {ok: False, reason}（已是 SSH、域名不受支持、路径不合法）。
    """
    text = (url or "").strip()
    if not text:
        return {"ok": False, "reason": "远端地址为空。"}
    if detect_remote_protocol(text) == "ssh":
        return {"ok": False, "reason": "该地址已经是 SSH 形式，无需转换。"}
    match = re.match(r"^https?://(?:[^\s@/]+@)?([^\s/:]+)(?::\d+)?/(.+)$", text, re.IGNORECASE)
    if not match:
        return {"ok": False, "reason": "只支持 https://<host>/<owner>/<repo> 形式的地址。"}
    host = match.group(1).lower()
    path = match.group(2).strip().strip("/")
    if host not in _SSH_HOSTS:
        return {
            "ok": False,
            "reason": f"不支持的托管域名：{host}（仅支持 github.com / gitee.com / gitlab.com）。",
        }
    segments = [segment for segment in path.split("/") if segment]
    if len(segments) < 2:
        return {"ok": False, "reason": "地址缺少 owner/repo 路径，无法转换。"}
    owner_repo = "/".join(segments)
    ssh_url = f"git@{host}:{owner_repo}"
    return {"ok": True, "ssh_url": ssh_url, "host": host, "owner_repo": owner_repo}


def parse_cmdkey_output(output: str) -> list[dict[str, Any]]:
    """解析 Windows `cmdkey /list` 输出中的 git HTTPS 凭据条目。

    参数：
        output: cmdkey 原始输出（可能是中文系统，故不依赖固定标签）。
    返回值：
        [{target, host, url}]；target 形如 git:https://github.com。
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for line in (output or "").splitlines():
        match = re.search(r"(git:(https?://[^\s]+))", line, flags=re.IGNORECASE)
        if not match:
            continue
        target = match.group(1).strip().rstrip("\\")
        if target.lower() in seen:
            continue
        seen.add(target.lower())
        url = target.split(":", 1)[1]
        host = re.sub(r"^https?://", "", url, flags=re.IGNORECASE).split("/")[0]
        items.append({"target": target, "host": host, "url": url})
    return items
