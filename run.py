"""GitManager 启动脚本。

用法：D:\\CodeDevelop\\Env\\Python313\\python.exe run.py
监听地址与端口来自项目根目录 .env（仅允许 127.0.0.1 / localhost）。
"""

from __future__ import annotations

import uvicorn

from app.config import load_settings


def main() -> None:
    """读取配置并启动 uvicorn 服务。

    参数：
        无。
    返回值：
        无（阻塞至服务退出）。
    """
    settings = load_settings()

    from app.main import app  # 延迟导入，确保配置校验先执行

    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level="info",
        access_log=True,
    )


if __name__ == "__main__":
    main()
