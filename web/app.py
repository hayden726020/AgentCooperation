"""Web Console application factory and server entrypoint."""

import argparse
import sys
from pathlib import Path
from flask import Flask, render_template, jsonify

from config.settings import settings
from web.api import api_bp


def create_app(mock_default: bool = False) -> Flask:
    """Create and configure the Flask web console application."""
    base_dir = Path(__file__).resolve().parent

    app = Flask(
        __name__,
        template_folder=str(base_dir / "templates"),
        static_folder=str(base_dir / "static"),
    )
    app.config["MOCK_DEFAULT"] = mock_default
    app.config["JSON_AS_ASCII"] = False

    # Register API blueprint
    app.register_blueprint(api_bp)

    @app.route("/")
    def index():
        return render_template(
            "index.html",
            mock_default=app.config.get("MOCK_DEFAULT", False),
            primary_agent=settings.load_policy().primary_agent,
        )

    @app.route("/health")
    def health():
        return jsonify({"status": "healthy", "service": "AgentCooperation Web Console"})

    return app


def main():
    parser = argparse.ArgumentParser(description="AgentCooperation 可视化 Web 控制台")
    parser.add_argument("--port", "-p", type=int, default=5000, help="Web 服务器端口 (默认 5000)")
    parser.add_argument("--host", "-H", default="127.0.0.1", help="绑定主机地址 (默认 127.0.0.1)")
    parser.add_argument("--mock", action="store_true", help="默认开启演练模拟账库模式")
    parser.add_argument("--debug", action="store_true", help="开启 Flask 调试模式")

    args = parser.parse_args()

    app = create_app(mock_default=args.mock)
    print(f"🚀 AgentCooperation Web 控制台已启动: http://{args.host}:{args.port}")
    print(f"模式: {'独立演练模拟账库 (Mock)' if args.mock else '真实调用账库 (Live)'}")
    app.run(host=args.host, port=args.port, debug=args.debug)


if __name__ == "__main__":
    main()
