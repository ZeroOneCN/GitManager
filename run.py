"""GitManager 启动脚本。

用法：D:\\CodeDevelop\\Env\\Python313\\python.exe run.py
监听地址与端口来自项目根目录 .env（默认仅 127.0.0.1；改为 0.0.0.0 可局域网访问）。
"""

from __future__ import annotations

import uvicorn

from app.config import build_access_urls, firewall_hint, load_settings


def main() -> None:
    """读取配置并启动 uvicorn 服务。

    参数：
        无。
    返回值：
        无（阻塞至服务退出）。
    """
    settings = load_settings()

    print("=" * 62)
    print(f"GitManager 监听 {settings.host}:{settings.port}")
    print("可访问地址：")
    for url in build_access_urls(settings.host, settings.port):
        print(f"  {url}")
    if settings.lan_enabled:
        print(f"令牌鉴权：{'已启用' if settings.auth_required else '未启用（缺少令牌）'}")
        print(f"允许的 Host：{'、'.join(settings.allowed_hosts)}")
        print("如需从其他设备访问，请手工执行（服务不会自动修改防火墙）：")
        print(f"  {firewall_hint(settings.port)}")
    print("=" * 62)

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
