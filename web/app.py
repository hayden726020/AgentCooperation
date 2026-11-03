"""Web Console application factory and server entrypoint."""

import argparse
import ipaddress
import sys
from pathlib import Path
from urllib.parse import urlsplit
from flask import Flask, render_template, jsonify, request

from config.settings import settings
from web.api import api_bp


def _origin_tuple(value):
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            return None
        return parsed.scheme, parsed.hostname.lower(), parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError:
        return None


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

    # REMOTE_ADDR is the direct peer, not an authentication mechanism. Never
    # expose this app through an unauthenticated local reverse proxy: such a
    # proxy makes remote clients appear to originate on loopback.
    @app.before_request
    def enforce_web_boundary():
        current = _origin_tuple(request.host_url)
        if current is None or current[1] not in {"localhost", "127.0.0.1", "::1"}:
            return jsonify({"error": "Untrusted host"}), 403
        try:
            peer = ipaddress.ip_address(request.remote_addr or "")
            if isinstance(peer, ipaddress.IPv6Address) and peer.ipv4_mapped:
                peer = peer.ipv4_mapped
            loopback = peer.is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            return jsonify({"error": "Remote access is not permitted"}), 403
        origin = request.headers.get("Origin")
        if request.method not in {"GET", "HEAD", "OPTIONS"} and origin is not None:
            if _origin_tuple(origin) != current or _origin_tuple(origin) is None:
                return jsonify({"error": "Cross-origin request is not allowed"}), 403

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
