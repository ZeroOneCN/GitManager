"""Git 输出解析与错误映射。

职责：把 git CLI 的原始输出转换为前端可用的结构化数据，并把英文错误
映射为可读中文错误码。
"""

from __future__ import annotations

from typing import Any

# git status 冲突状态对
_CONFLICT_CODES = {"DD", "AU", "UD", "UA", "DU", "AA", "UU"}


def parse_status_porcelain_z(output: str) -> dict[str, Any]:
    """解析 `git status --porcelain=v1 -z` 输出。

    参数：
        output: git 原样输出（NUL 分隔）。
    返回值：
        {
          "staged"/"modified"/"untracked"/"conflicts": [{path, code}],
          "counts": {staged, modified, untracked, conflicts, total}
        }；
        其中 total 为去重后的脏文件总数（同一文件可能同时处于 staged 与 modified）。
    """
    staged: list[dict[str, str]] = []
    modified: list[dict[str, str]] = []
    untracked: list[dict[str, str]] = []
    conflicts: list[dict[str, str]] = []
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
        # 重命名/复制条目后紧跟原路径，跳过
        if x in ("R", "C") and index < len(tokens):
            index += 1
        if not path:
            continue

        item = {"path": path, "code": f"{x}{y}"}
        unique_paths.add(path)

        if x == "?":
            untracked.append(item)
        elif x == "!":
            continue
        elif f"{x}{y}" in _CONFLICT_CODES:
            conflicts.append(item)
        else:
            if x != " ":
                staged.append(item)
            if y != " " and y in {"M", "D", "T"}:
                modified.append(item)

    return {
        "staged": staged,
        "modified": modified,
        "untracked": untracked,
        "conflicts": conflicts,
        "counts": {
            "staged": len(staged),
            "modified": len(modified),
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
    if "could not resolve host" in text or "network is unreachable" in text:
        return "network_error", "无法解析远端主机或网络不可达，请检查网络与远端地址。"
    if (
        "unable to access" in text
        or "failed to connect" in text
        or "connection timed out" in text
        or "could not read from remote repository" in text
    ):
        return "network_error", "连接远端失败，请检查网络、远端地址或访问权限。"
    if "permission denied" in text or "publickey" in text or "authentication failed" in text:
        return "auth_error", "远端认证失败，请检查本机 SSH/凭据配置。"
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
