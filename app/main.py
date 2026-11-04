"""FastAPI 接入层：HTTP 路由、参数模型、静态页面托管与异常映射。

职责：只做参数校验与 HTTP 语义转换，业务逻辑全部位于 services 层。
"""

from __future__ import annotations

import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import (
    Settings,
    build_access_urls,
    firewall_hint,
    is_loopback_host,
    load_settings,
)
from .db import Database
from .git_runner import CLIENT_IP, GitExecutor
from .services import ApiError, GitManagerService

APP_VERSION = "1.4.0"
APP_DIR = Path(__file__).resolve().parent

settings: Settings = load_settings()
database = Database(settings.db_path)
executor = GitExecutor(
    settings.git_executable,
    settings.git_timeout_seconds,
    database,
    settings.output_summary_limit,
    settings.ssh_executable,
    settings.ssh_keygen_executable,
)
service = GitManagerService(settings, database, executor)

logger = logging.getLogger("gitmanager")

# 鉴权 Cookie 名与有效期（HttpOnly + SameSite=Lax，避免脚本读取与跨站提交）
AUTH_COOKIE_NAME = "gitmanager_token"
AUTH_COOKIE_MAX_AGE = 7 * 24 * 3600

# 匿名可访问的路径：登录页、登录接口与静态资源
ANONYMOUS_PATHS = {
    "/login",
    "/api/auth/login",
    "/api/auth/logout",
    "/api/auth/state",
}


def _is_authenticated(request: Request) -> bool:
    """判断当前请求是否已通过访问令牌鉴权。

    参数：
        request: 当前请求。
    返回值：
        已配置令牌且 Cookie 与令牌一致时返回 True。
    """
    if not settings.access_token:
        return False
    cookie = request.cookies.get(AUTH_COOKIE_NAME, "")
    if not cookie:
        return False
    return hmac.compare_digest(cookie, settings.access_token)


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动时初始化数据库、清理超期审计并打印访问入口。

    参数：
        _: FastAPI 实例（未使用）。
    返回值：
        None（作为异步上下文管理器）。
    """
    purged = database.init(settings.log_retention_days, settings.allowed_roots)
    roots = service.allowed_root_paths()
    logger.info(
        "GitManager 启动：监听 %s:%s，白名单根目录 %s，清理超期审计 %s 条",
        settings.host,
        settings.port,
        "、".join(str(root) for root in roots),
        purged,
    )
    urls = build_access_urls(settings.host, settings.port)
    logger.info("可访问地址：%s", "  ".join(urls))
    if settings.lan_enabled:
        logger.warning(
            "局域网模式已开启：令牌鉴权=%s，允许的 Host=%s",
            "启用" if settings.auth_required else "未启用（缺少令牌）",
            "、".join(settings.allowed_hosts),
        )
        logger.info("如无法从其他设备访问，请手工放行防火墙：%s", firewall_hint(settings.port))
    yield


app = FastAPI(
    title="GitManager Web",
    version=APP_VERSION,
    description="本机 Git 仓库体检与安全操作面板",
    lifespan=lifespan,
)


@app.middleware("http")
async def enforce_access_control(request: Request, call_next):
    """统一访问控制：Host 白名单校验、局域网令牌鉴权、来源 IP 注入。

    参数：
        request: 当前请求。
        call_next: 后续处理链。
    返回值：
        正常响应；Host 非法返回 400，未鉴权的接口返回 401，页面请求重定向到登录页。
    """
    host_header = request.headers.get("host", "")
    host_only = host_header.split(":", 1)[0].strip().lower()
    if not host_only or host_only not in settings.allowed_hosts:
        return JSONResponse(
            status_code=400,
            content={
                "code": "forbidden_host",
                "message": (
                    "Host 不在允许列表内（防 DNS rebinding）。"
                    "局域网访问请在 .env 的 GITMANAGER_ALLOWED_HOSTS 加入本机 IP/主机名。"
                ),
                "allowed_hosts": list(settings.allowed_hosts),
            },
        )

    path = request.url.path
    authenticated = _is_authenticated(request)
    if settings.auth_required and not authenticated:
        if path.startswith("/api/") and path not in ANONYMOUS_PATHS:
            return JSONResponse(
                status_code=401,
                content={"code": "unauthorized", "message": "需要访问令牌，请先登录。"},
            )
        if path not in ANONYMOUS_PATHS and not path.startswith("/static/"):
            return RedirectResponse("/login", status_code=303)

    client_host = request.client.host if request.client else ""
    # 只有非本机来源才记录客户端 IP，避免本机操作产生无效审计字段
    context_token = CLIENT_IP.set("" if is_loopback_host(client_host) else client_host)
    try:
        return await call_next(request)
    finally:
        CLIENT_IP.reset(context_token)


@app.exception_handler(ApiError)
async def handle_api_error(_: Request, exc: ApiError) -> JSONResponse:
    """把业务错误转换为统一 JSON 响应。

    参数：
        _: 请求对象（未使用）。
        exc: 业务错误。
    返回值：
        含 code / message / 附加字段的 JSONResponse。
    """
    payload: dict[str, Any] = {"code": exc.code, "message": exc.message}
    payload.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(RequestValidationError)
async def handle_validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
    """把参数校验错误转换为统一 JSON 响应。

    参数：
        _: 请求对象（未使用）。
        exc: 校验错误。
    返回值：
        HTTP 400 的 JSONResponse。
    """
    return JSONResponse(
        status_code=400,
        content={
            "code": "invalid_request",
            "message": "请求参数不合法，请检查后重试。",
            "detail": str(exc.errors())[:500],
        },
    )


@app.exception_handler(StarletteHTTPException)
async def handle_http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
    """把框架层 HTTP 错误转换为统一 JSON 响应。

    参数：
        _: 请求对象（未使用）。
        exc: HTTP 异常。
    返回值：
        JSONResponse。
    """
    return JSONResponse(
        status_code=exc.status_code,
        content={"code": "http_error", "message": str(exc.detail)},
    )


@app.exception_handler(Exception)
async def handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
    """兜底异常处理：记录日志并返回通用错误。

    参数：
        _: 请求对象（未使用）。
        exc: 未预期异常。
    返回值：
        HTTP 500 的 JSONResponse。
    """
    logger.exception("未预期错误: %s", exc)
    return JSONResponse(
        status_code=500,
        content={"code": "internal_error", "message": f"服务内部错误：{exc}"},
    )


# ----------------------------------------------------------------------
# 请求模型
# ----------------------------------------------------------------------


class AddReposRequest(BaseModel):
    """批量添加仓库请求。"""

    paths: list[str] = Field(..., min_length=1, max_length=500, description="仓库绝对路径列表")


class LoginRequest(BaseModel):
    """访问令牌登录请求。"""

    token: str = Field(..., min_length=1, max_length=200, description="GITMANAGER_ACCESS_TOKEN")


class PullRequest(BaseModel):
    """pull 请求。"""

    auto_stash: bool = Field(False, description="是否在脏工作区时自动 stash（需用户显式确认）")


class PushRequest(BaseModel):
    """push 请求。"""

    set_upstream: bool = Field(False, description="无 upstream 时是否设置上游")
    remote: str | None = Field(None, description="设置 upstream 使用的 remote 名称")
    force: bool = Field(False, description="是否请求强制推送")
    confirm_force: bool = Field(False, description="强制推送的二次确认标记")


class CommitRequest(BaseModel):
    """提交请求。"""

    files: list[str] = Field(..., min_length=1, max_length=1000, description="勾选的文件路径")
    message: str = Field(..., min_length=1, max_length=5000, description="提交信息")


class IgnoreRequest(BaseModel):
    """写入 .gitignore 预览/追加请求。"""

    paths: list[str] = Field(..., min_length=1, max_length=500, description="待忽略的路径")
    note: str = Field("", max_length=200, description="可选备注（写入审计摘要）")


class MuteRequest(BaseModel):
    """本地静音请求。"""

    patterns: list[str] = Field(..., min_length=1, max_length=200, description="路径或通配规则")
    note: str = Field("", max_length=200, description="可选备注")


class UntrackRequest(BaseModel):
    """取消跟踪（git rm --cached）请求。"""

    paths: list[str] = Field(..., min_length=1, max_length=200, description="已跟踪的路径")
    confirm: bool = Field(False, description="危险操作二次确认标记")


class RemoteSshPreviewRequest(BaseModel):
    """远端 HTTPS→SSH 预览请求。"""

    remote: str = Field("", max_length=100, description="远端名称；空表示默认远端")


class RemoteSshRequest(BaseModel):
    """远端地址切换为 SSH 请求。"""

    remote: str = Field("", max_length=100, description="远端名称")
    mode: str = Field("both", description="both / push / fetch")
    confirm: bool = Field(False, description="修改配置的确认标记")


class CredentialDeleteRequest(BaseModel):
    """清除 HTTPS 凭据请求。"""

    target: str = Field(..., min_length=1, max_length=500, description="cmdkey 目标，如 git:https://github.com")
    confirm: bool = Field(False, description="清除凭据的确认标记")


class CredentialLoginRequest(BaseModel):
    """交互式凭据登录请求。"""

    confirm: bool = Field(False, description="打开外部窗口的确认标记")


class AllowedRootRequest(BaseModel):
    """新增白名单根目录请求。"""

    path: str = Field(..., min_length=1, max_length=1000, description="根目录绝对路径")


class ScanStartRequest(BaseModel):
    """启动仓库扫描请求。"""

    roots: list[str] = Field(default_factory=list, max_length=20, description="扫描根目录；空表示全部白名单")
    max_depth: int | None = Field(None, ge=1, le=12, description="最大扫描深度")
    limit: int | None = Field(None, ge=1, le=500, description="结果条数上限")


class SshIdentityCreateRequest(BaseModel):
    """新增 SSH 身份请求。"""

    name: str = Field(..., min_length=1, max_length=60, description="显示名")
    key_path: str = Field(..., min_length=1, max_length=1000, description="私钥绝对路径")
    host_alias: str = Field("", max_length=255, description="可选 Host 别名")
    enabled: bool = Field(True, description="是否启用")
    is_default: bool = Field(False, description="是否设为全局默认")


class SshIdentityUpdateRequest(BaseModel):
    """更新 SSH 身份请求（字段均可选）。"""

    name: str | None = Field(None, min_length=1, max_length=60)
    key_path: str | None = Field(None, min_length=1, max_length=1000)
    host_alias: str | None = Field(None, max_length=255)
    enabled: bool | None = None
    is_default: bool | None = None


class RepoSshBindRequest(BaseModel):
    """仓库 SSH 身份绑定请求。"""

    identity_id: int | None = Field(None, ge=1, description="身份 id；null 表示清除绑定")


class SshTestRequest(BaseModel):
    """SSH 连接测试请求。"""

    host: str | None = Field(None, max_length=255, description="可选，覆盖身份中的 Host 别名")


# ----------------------------------------------------------------------
# 鉴权（仅局域网监听时强制生效）
# ----------------------------------------------------------------------


@app.get("/api/auth/state")
def auth_state(request: Request) -> dict[str, Any]:
    """返回当前鉴权状态（匿名可访问，供登录页判断）。

    参数：
        request: 当前请求。
    返回值：
        {auth_required, authenticated, lan_enabled}。
    """
    return {
        "auth_required": settings.auth_required,
        "authenticated": _is_authenticated(request),
        "lan_enabled": settings.lan_enabled,
    }


@app.post("/api/auth/login")
def auth_login(payload: LoginRequest) -> JSONResponse:
    """校验访问令牌并写入 HttpOnly Cookie。

    参数：
        payload: 令牌文本。
    返回值：
        成功时 200 并设置 Cookie；失败时 401。
    """
    if not settings.access_token or not hmac.compare_digest(
        payload.token.strip(), settings.access_token
    ):
        return JSONResponse(
            status_code=401,
            content={"code": "invalid_token", "message": "访问令牌不正确，请检查 .env 配置。"},
        )
    response = JSONResponse(content={"ok": True, "message": "登录成功。"})
    response.set_cookie(
        AUTH_COOKIE_NAME,
        settings.access_token,
        max_age=AUTH_COOKIE_MAX_AGE,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return response


@app.post("/api/auth/logout")
def auth_logout() -> JSONResponse:
    """清除登录 Cookie（不修改 .env 中的令牌）。

    参数：
        无。
    返回值：
        成功 JSON，并删除 Cookie。
    """
    response = JSONResponse(content={"ok": True, "message": "已退出登录。"})
    response.delete_cookie(AUTH_COOKIE_NAME, path="/")
    return response


# ----------------------------------------------------------------------
# 元信息
# ----------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    """健康检查。

    参数：
        无。
    返回值：
        {"ok": True, "version": 版本号}。
    """
    return {"ok": True, "version": APP_VERSION}


@app.get("/api/settings")
def get_settings() -> dict[str, Any]:
    """返回只读运行配置（不含敏感信息）。

    参数：
        无。
    返回值：
        监听地址与访问入口、鉴权状态、允许的 Host、git 路径、动态白名单根目录、
        扫描参数、日志保留天数与 pull stash 默认值；不包含令牌本身。
    """
    roots = service.allowed_root_paths()
    default_identity = next(
        (item for item in service.list_ssh_identities()["items"] if item["is_default"]),
        None,
    )
    access_urls = build_access_urls(settings.host, settings.port)
    return {
        "host": settings.host,
        "port": settings.port,
        "service_url": access_urls[0],
        "access_urls": access_urls,
        "lan_enabled": settings.lan_enabled,
        "auth_required": settings.auth_required,
        "token_configured": bool(settings.access_token),
        "allowed_hosts": list(settings.allowed_hosts),
        "firewall_hint": firewall_hint(settings.port),
        "git_executable": settings.git_executable,
        "allowed_roots": [str(root) for root in roots],
        "log_retention_days": settings.log_retention_days,
        "pull_auto_stash_default": settings.pull_auto_stash_default,
        "scan_max_depth": settings.scan_max_depth,
        "scan_result_limit": settings.scan_result_limit,
        "ssh_dir": str(settings.ssh_dir),
        "default_ssh_identity": (
            {"id": default_identity["id"], "name": default_identity["name"]}
            if default_identity
            else None
        ),
        "version": APP_VERSION,
    }


# ----------------------------------------------------------------------
# 仓库管理 / 目录浏览
# ----------------------------------------------------------------------


@app.get("/api/repos")
def list_repos() -> dict[str, Any]:
    """列出已配置仓库。

    参数：
        无。
    返回值：
        {"items": [...]}。
    """
    return {"items": service.list_repositories()}


@app.post("/api/repos")
def add_repos(payload: AddReposRequest) -> dict[str, Any]:
    """批量添加本地仓库。

    参数：
        payload: 路径列表。
    返回值：
        逐条添加结果与新增数量。
    """
    return service.add_repositories(payload.paths)


@app.delete("/api/repos/{repo_id}")
def remove_repo(repo_id: int) -> dict[str, Any]:
    """移除仓库配置（不删除磁盘目录）。

    参数：
        repo_id: 仓库主键。
    返回值：
        移除结果。
    """
    return service.remove_repository(repo_id)


@app.get("/api/fs/roots")
def fs_roots() -> dict[str, Any]:
    """列出白名单根目录。

    参数：
        无。
    返回值：
        {"roots": [{path,name,is_repo}]}。
    """
    return {"roots": service.browse_roots()}


@app.get("/api/fs/list")
def fs_list(path: str | None = Query(None, description="要浏览的目录；为空时返回根目录")) -> dict[str, Any]:
    """浏览白名单内的目录（用于前端一次多选）。

    参数：
        path: 目标目录，可为空。
    返回值：
        目录列表与导航信息。
    """
    return service.browse_dirs(path)


# ----------------------------------------------------------------------
# 白名单根目录管理
# ----------------------------------------------------------------------


@app.get("/api/allowed-roots")
def list_allowed_roots() -> dict[str, Any]:
    """列出白名单根目录及受管仓库数量。

    参数：
        无。
    返回值：
        {"items": [{id, path, name, managed_repos}]}。
    """
    return service.list_allowed_roots()


@app.post("/api/allowed-roots")
def add_allowed_root(payload: AllowedRootRequest) -> dict[str, Any]:
    """新增白名单根目录（规范化并持久化到 SQLite）。

    参数：
        payload: 根目录路径。
    返回值：
        新增的根目录信息。
    """
    return service.add_allowed_root(payload.path)


@app.delete("/api/allowed-roots/{root_id}")
def remove_allowed_root(
    root_id: int,
    force: bool = Query(False, description="存在受管仓库时是否强制移除"),
) -> dict[str, Any]:
    """移除白名单根目录（默认在仍有受管仓库时拒绝）。

    参数：
        root_id: 根目录主键。
        force: 是否强制移除。
    返回值：
        移除结果与受影响仓库数量。
    """
    return service.remove_allowed_root(root_id, force)


# ----------------------------------------------------------------------
# 仓库扫描（只读）
# ----------------------------------------------------------------------


@app.post("/api/scan/start")
def start_scan(payload: ScanStartRequest) -> dict[str, Any]:
    """启动只读仓库扫描任务。

    参数：
        payload: 扫描根目录、深度与结果上限。
    返回值：
        任务快照（含 job_id，供前端轮询进度）。
    """
    return service.start_scan(payload.roots, payload.max_depth, payload.limit)


@app.get("/api/scan/{job_id}")
def get_scan(job_id: str) -> dict[str, Any]:
    """查询扫描任务进度与结果。

    参数：
        job_id: 任务 id。
    返回值：
        任务快照。
    """
    return service.get_scan(job_id)


@app.post("/api/scan/{job_id}/cancel")
def cancel_scan(job_id: str) -> dict[str, Any]:
    """取消扫描任务。

    参数：
        job_id: 任务 id。
    返回值：
        任务快照。
    """
    return service.cancel_scan(job_id)


# ----------------------------------------------------------------------
# 体检
# ----------------------------------------------------------------------


@app.get("/api/repos/{repo_id}/status")
def repo_status(repo_id: int) -> dict[str, Any]:
    """读取单个仓库的体检状态（本地只读，不访问网络）。

    参数：
        repo_id: 仓库主键。
    返回值：
        状态对象。
    """
    return service.collect_status_by_id(repo_id)


@app.get("/api/status/all")
async def all_status() -> dict[str, Any]:
    """全局刷新：并发采集全部仓库状态。

    参数：
        无。
    返回值：
        {"items": [状态对象...]}。
    """
    return {"items": await service.collect_all_status()}


@app.post("/api/repos/{repo_id}/fetch")
def fetch_repo(repo_id: int) -> dict[str, Any]:
    """执行 git fetch --prune 并返回最新状态。

    参数：
        repo_id: 仓库主键。
    返回值：
        fetch 结果与最新状态。
    """
    return service.fetch_repository(repo_id)


# ----------------------------------------------------------------------
# pull / push / commit
# ----------------------------------------------------------------------


@app.post("/api/repos/{repo_id}/pull")
def pull_repo(repo_id: int, payload: PullRequest) -> dict[str, Any]:
    """执行 ff-only 拉取（可选自动 stash）。

    参数：
        repo_id: 仓库主键。
        payload: auto_stash 标记。
    返回值：
        拉取结果、stash 状态与最新体检状态。
    """
    return service.pull_repository(repo_id, payload.auto_stash)


@app.post("/api/repos/{repo_id}/push")
def push_repo(repo_id: int, payload: PushRequest) -> dict[str, Any]:
    """推送当前分支（可选设置 upstream；强推需二次确认）。

    参数：
        repo_id: 仓库主键。
        payload: 推送选项。
    返回值：
        推送结果与最新体检状态。
    """
    return service.push_repository(
        repo_id,
        payload.set_upstream,
        payload.remote,
        payload.force,
        payload.confirm_force,
    )


@app.get("/api/repos/{repo_id}/changes")
def repo_changes(repo_id: int) -> dict[str, Any]:
    """列出工作区变更（提交面板数据源）。

    参数：
        repo_id: 仓库主键。
    返回值：
        变更文件列表与冲突信息。
    """
    return service.list_changes(repo_id)


@app.get("/api/repos/{repo_id}/remotes")
def repo_remotes(repo_id: int) -> dict[str, Any]:
    """列出仓库已有的 remote（推送面板设置上游时使用）。

    参数：
        repo_id: 仓库主键。
    返回值：
        {"items": ["origin", ...]}。
    """
    return service.get_remotes(repo_id)


@app.post("/api/repos/{repo_id}/commit")
def commit_repo(repo_id: int, payload: CommitRequest) -> dict[str, Any]:
    """提交勾选的文件。

    参数：
        repo_id: 仓库主键。
        payload: 文件列表与提交信息。
    返回值：
        提交结果与新提交信息。
    """
    return service.commit_repository(repo_id, payload.files, payload.message)


# ----------------------------------------------------------------------
# 忽略归因 / 本地静音 / 取消跟踪 / 远端协议 / HTTPS 凭据
# ----------------------------------------------------------------------


@app.get("/api/repos/{repo_id}/ignore")
def repo_ignore_report(repo_id: int) -> dict[str, Any]:
    """返回忽略归因报告：可忽略、已跟踪但命中规则、规则来源与静音项。

    参数：
        repo_id: 仓库主键。
    返回值：
        {repo, files, counts, mutes, gitignore}。
    """
    return service.ignore_report(repo_id)


@app.post("/api/repos/{repo_id}/ignore/preview")
def repo_ignore_preview(repo_id: int, payload: IgnoreRequest) -> dict[str, Any]:
    """生成 .gitignore 追加预览（不写入文件）。

    参数：
        repo_id: 仓库主键。
        payload: 待忽略路径与备注。
    返回值：
        规则建议、目标文件、追加内容与完整预览。
    """
    return service.preview_ignore(repo_id, payload.paths)


@app.post("/api/repos/{repo_id}/ignore")
def repo_ignore_append(repo_id: int, payload: IgnoreRequest) -> dict[str, Any]:
    """把规则追加到仓库根 .gitignore（重复规则跳过），并返回刷新后的状态。

    参数：
        repo_id: 仓库主键。
        payload: 待忽略路径与备注。
    返回值：
        {ok, target, appended, skipped, message, status}。
    """
    return service.append_ignore(repo_id, payload.paths, payload.note)


@app.get("/api/repos/{repo_id}/mutes")
def repo_mutes(repo_id: int) -> dict[str, Any]:
    """列出本地静音规则与当前静音计数。

    参数：
        repo_id: 仓库主键。
    返回值：
        {mutes, counts}。
    """
    report = service.ignore_report(repo_id)
    return {"mutes": report["mutes"], "counts": report["counts"]}


@app.post("/api/repos/{repo_id}/mutes")
def repo_add_mutes(repo_id: int, payload: MuteRequest) -> dict[str, Any]:
    """新增本地静音规则（只影响看板，不修改仓库文件）。

    参数：
        repo_id: 仓库主键。
        payload: 路径/规则与备注。
    返回值：
        {ok, added, skipped, mutes, message, status}。
    """
    return service.add_mutes(repo_id, payload.patterns, payload.note)


@app.delete("/api/repos/{repo_id}/mutes/{mute_id}")
def repo_remove_mute(repo_id: int, mute_id: int) -> dict[str, Any]:
    """取消一条本地静音规则。

    参数：
        repo_id: 仓库主键。
        mute_id: 静音规则主键。
    返回值：
        {ok, removed, mutes, message, status}。
    """
    return service.remove_mute(repo_id, mute_id)


@app.post("/api/repos/{repo_id}/untrack")
def repo_untrack(repo_id: int, payload: UntrackRequest) -> dict[str, Any]:
    """取消跟踪（git rm --cached），保留磁盘文件，不自动提交。

    参数：
        repo_id: 仓库主键。
        payload: 路径列表与二次确认标记。
    返回值：
        {ok, removed, needs_commit, message, status}。
    """
    return service.untrack_paths(repo_id, payload.paths, payload.confirm)


@app.post("/api/repos/{repo_id}/remote/ssh/preview")
def repo_remote_ssh_preview(
    repo_id: int, payload: RemoteSshPreviewRequest
) -> dict[str, Any]:
    """预览远端地址的 HTTPS→SSH 转换结果（不修改配置）。

    参数：
        repo_id: 仓库主键。
        payload: 远端名称。
    返回值：
        当前 fetch/push 地址与目标 SSH 地址。
    """
    return service.preview_remote_ssh(repo_id, payload.remote)


@app.post("/api/repos/{repo_id}/remote/ssh")
def repo_remote_ssh(repo_id: int, payload: RemoteSshRequest) -> dict[str, Any]:
    """把远端地址切换为 SSH（需确认，执行 git remote set-url 并审计）。

    参数：
        repo_id: 仓库主键。
        payload: 远端名称、模式（both/push/fetch）与确认标记。
    返回值：
        {ok, remote, mode, fetch_url, push_url, message, status}。
    """
    return service.convert_remote_ssh(
        repo_id, payload.remote, payload.mode, payload.confirm
    )


@app.get("/api/credentials")
def list_credentials() -> dict[str, Any]:
    """列出 Windows 凭据管理器中的 git HTTPS 条目（不展示密码）。

    参数：
        无。
    返回值：
        {ok, items, raw, message}。
    """
    return service.list_https_credentials()


@app.post("/api/credentials/delete")
def delete_credential(payload: CredentialDeleteRequest) -> dict[str, Any]:
    """清除指定 host 的 git HTTPS 凭据（需显式确认）。

    参数：
        payload: cmdkey 目标与确认标记。
    返回值：
        {ok, target, message}。
    """
    if not payload.confirm:
        raise ApiError(
            "confirm_required", "清除凭据需要显式确认（下次推送会要求重新登录）。", 400
        )
    return service.delete_https_credential(payload.target)


@app.post("/api/credentials/login")
def login_credential(payload: CredentialLoginRequest) -> dict[str, Any]:
    """在独立控制台窗口启动 Git Credential Manager 交互式登录。

    参数：
        payload: 确认标记。
    返回值：
        {ok, installed, launched, message, install_url}。
    """
    return service.login_https_credential(payload.confirm)


# ----------------------------------------------------------------------
# SSH 身份
# ----------------------------------------------------------------------


@app.get("/api/ssh/scan")
def scan_ssh() -> dict[str, Any]:
    """扫描本机 SSH 私钥与 Host 别名（只读，不读取私钥正文）。

    参数：
        无。
    返回值：
        {"ssh_dir", "hosts", "keys": [...]}。
    """
    return service.scan_ssh()


@app.get("/api/ssh/identities")
def list_ssh_identities() -> dict[str, Any]:
    """列出已配置的 SSH 身份与全局默认。

    参数：
        无。
    返回值：
        {"items": [...], "default": {...} | None}。
    """
    return service.list_ssh_identities()


@app.post("/api/ssh/identities")
def create_ssh_identity(payload: SshIdentityCreateRequest) -> dict[str, Any]:
    """新增 SSH 身份配置。

    参数：
        payload: 身份字段。
    返回值：
        新增的身份。
    """
    return service.create_ssh_identity(payload.model_dump())


@app.patch("/api/ssh/identities/{identity_id}")
def update_ssh_identity(identity_id: int, payload: SshIdentityUpdateRequest) -> dict[str, Any]:
    """更新 SSH 身份配置（仅更新传入字段）。

    参数：
        identity_id: 身份主键。
        payload: 可选字段。
    返回值：
        更新后的身份。
    """
    return service.update_ssh_identity(
        identity_id, payload.model_dump(exclude_unset=True)
    )


@app.delete("/api/ssh/identities/{identity_id}")
def delete_ssh_identity(identity_id: int) -> dict[str, Any]:
    """删除 SSH 身份配置（不删除磁盘密钥文件）。

    参数：
        identity_id: 身份主键。
    返回值：
        删除结果。
    """
    return service.delete_ssh_identity(identity_id)


@app.post("/api/ssh/identities/{identity_id}/default")
def set_default_ssh_identity(identity_id: int) -> dict[str, Any]:
    """设置全局默认 SSH 身份。

    参数：
        identity_id: 身份主键。
    返回值：
        更新后的身份。
    """
    return service.set_default_ssh_identity(identity_id)


@app.post("/api/ssh/identities/{identity_id}/test")
def test_ssh_identity(identity_id: int, payload: SshTestRequest) -> dict[str, Any]:
    """测试 SSH 身份连接（BatchMode，超时短）。

    参数：
        identity_id: 身份主键。
        payload: 可选 Host 覆盖。
    返回值：
        连接测试结果。
    """
    return service.test_ssh_identity(identity_id, payload.host)


@app.put("/api/repos/{repo_id}/ssh-identity")
def bind_repo_ssh_identity(repo_id: int, payload: RepoSshBindRequest) -> dict[str, Any]:
    """设置或清除仓库级 SSH 身份绑定。

    参数：
        repo_id: 仓库主键。
        payload: 身份 id；null 表示清除。
    返回值：
        生效身份。
    """
    return service.bind_repo_ssh_identity(repo_id, payload.identity_id)


# ----------------------------------------------------------------------
# 审计
# ----------------------------------------------------------------------


@app.get("/api/audit")
def query_audit(
    page: int | None = Query(None, ge=1, description="页码（从 1 开始；与 limit/offset 二选一）"),
    page_size: int = Query(10, ge=1, le=50, description="每页条数（默认 10）"),
    limit: int | None = Query(None, ge=1, le=200, description="兼容旧版：每页条数"),
    offset: int = Query(0, ge=0, description="兼容旧版：偏移量"),
    repo_id: int | None = Query(None, ge=1),
    action: str | None = Query(None, max_length=64),
    only_failed: bool = Query(False),
) -> dict[str, Any]:
    """分页查询操作审计（服务端分页）。

    参数：
        page: 页码（1 开始，越界自动收敛到最后页）。
        page_size: 每页条数，默认 10。
        limit: 旧版每页条数（未传 page 时生效）。
        offset: 旧版偏移量（未传 page 时生效）。
        repo_id: 可选仓库过滤。
        action: 可选动作过滤。
        only_failed: 是否只看失败记录。
    返回值：
        页码模式返回 total/total_pages/page/page_size/from/to/items；
        旧版模式返回 total/items。
    """
    if page is None and (limit is not None or offset > 0):
        return service.query_audit_legacy(
            limit or 50, offset, repo_id, action, only_failed
        )
    return service.query_audit(
        page or 1, page_size, repo_id, action, only_failed
    )


@app.get("/api/audit/actions")
def audit_actions() -> dict[str, Any]:
    """返回审计表中出现过的动作名。

    参数：
        无。
    返回值：
        {"items": ["branch", "commit", ...]}。
    """
    return {"items": database.list_audit_actions()}


# ----------------------------------------------------------------------
# 前端页面
# ----------------------------------------------------------------------


@app.get("/login")
def login_page() -> FileResponse:
    """返回局域网访问登录页（匿名可访问）。

    参数：
        无。
    返回值：
        login.html（禁用缓存）。
    """
    return FileResponse(
        APP_DIR / "templates" / "login.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


@app.get("/")
def index_page() -> FileResponse:
    """返回前端入口页面。

    参数：
        无。
    返回值：
        index.html（禁用缓存，确保代码更新即时生效）。
    """
    return FileResponse(
        APP_DIR / "templates" / "index.html",
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )


app.mount("/static", StaticFiles(directory=str(APP_DIR / "static")), name="static")
