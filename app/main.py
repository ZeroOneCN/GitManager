"""FastAPI 接入层：HTTP 路由、参数模型、静态页面托管与异常映射。

职责：只做参数校验与 HTTP 语义转换，业务逻辑全部位于 services 层。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import Settings, load_settings
from .db import Database
from .git_runner import GitExecutor
from .services import ApiError, GitManagerService

APP_VERSION = "1.0.0"
APP_DIR = Path(__file__).resolve().parent

settings: Settings = load_settings()
database = Database(settings.db_path)
executor = GitExecutor(
    settings.git_executable,
    settings.git_timeout_seconds,
    database,
    settings.output_summary_limit,
)
service = GitManagerService(settings, database, executor)

logger = logging.getLogger("gitmanager")

# 允许的 Host 头（防 DNS rebinding；服务本身只绑定回环地址）
_ALLOWED_HOSTS = {"127.0.0.1", "localhost", settings.host.lower()}


@asynccontextmanager
async def lifespan(_: FastAPI):
    """应用生命周期：启动时初始化数据库并清理超期审计。

    参数：
        _: FastAPI 实例（未使用）。
    返回值：
        None（作为异步上下文管理器）。
    """
    purged = database.init(settings.log_retention_days)
    logger.info(
        "GitManager 启动：监听 %s:%s，白名单根目录 %s，清理超期审计 %s 条",
        settings.host,
        settings.port,
        "、".join(str(root) for root in settings.allowed_roots),
        purged,
    )
    yield


app = FastAPI(
    title="GitManager Web",
    version=APP_VERSION,
    description="本机 Git 仓库体检与安全操作面板",
    lifespan=lifespan,
)


@app.middleware("http")
async def enforce_loopback_host(request: Request, call_next):
    """校验 Host 头，拒绝非本机来源的请求。

    参数：
        request: 当前请求。
        call_next: 后续处理链。
    返回值：
        正常响应；Host 非法时返回 403 JSON。
    """
    host_header = request.headers.get("host", "")
    host_only = host_header.split(":", 1)[0].strip().lower()
    if host_only not in _ALLOWED_HOSTS:
        return JSONResponse(
            status_code=403,
            content={
                "code": "forbidden_host",
                "message": "仅允许从本机（127.0.0.1 / localhost）访问。",
            },
        )
    return await call_next(request)


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

    paths: list[str] = Field(..., min_length=1, max_length=50, description="仓库绝对路径列表")


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
        监听地址、git 路径、白名单根目录、日志保留天数与 pull stash 默认值。
    """
    return {
        "host": settings.host,
        "port": settings.port,
        "git_executable": settings.git_executable,
        "allowed_roots": [str(root) for root in settings.allowed_roots],
        "log_retention_days": settings.log_retention_days,
        "pull_auto_stash_default": settings.pull_auto_stash_default,
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
# 审计
# ----------------------------------------------------------------------


@app.get("/api/audit")
def query_audit(
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    repo_id: int | None = Query(None, ge=1),
    action: str | None = Query(None, max_length=64),
    only_failed: bool = Query(False),
) -> dict[str, Any]:
    """分页查询操作审计。

    参数：
        limit: 每页条数。
        offset: 偏移量。
        repo_id: 可选仓库过滤。
        action: 可选动作过滤。
        only_failed: 是否只看失败记录。
    返回值：
        {"total": int, "items": [...]}。
    """
    return service.query_audit(limit, offset, repo_id, action, only_failed)


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
