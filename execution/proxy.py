"""Inherit an existing proxy only for explicitly selected model CLI children."""

import os
import re
import sys
from collections.abc import Mapping
from urllib.parse import unquote, urlsplit


PROXY_KEYS = {"HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"}
TRANSPORT_KEYS = PROXY_KEYS - {"NO_PROXY"}


def _read_windows_proxy() -> dict[str, object]:
    """Read manual user proxy settings; never fetch or execute PAC configuration."""
    if sys.platform != "win32":
        return {}
    import winreg
    settings: dict[str, object] = {}
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Software\Microsoft\Windows\CurrentVersion\Internet Settings") as key:
            for name in ("ProxyEnable", "ProxyServer", "ProxyOverride"):
                try:
                    settings[name] = winreg.QueryValueEx(key, name)[0]
                except FileNotFoundError:
                    continue
    except OSError:
        return {}
    return settings


def _proxy_url(value: str, default_scheme: str = "http") -> str:
    if not isinstance(value, str) or any(ord(character) < 32 or character.isspace() for character in value):
        raise ValueError("Invalid configured CLI proxy address")
    if not value:
        return ""
    normalized = value if "://" in value else f"{default_scheme}://{value}"
    try:
        parsed = urlsplit(normalized)
        port = parsed.port
        valid = (parsed.scheme in {"http", "https", "socks", "socks5", "socks5h"}
                 and parsed.hostname and (port is None or port > 0)
                 and parsed.path in {"", "/"} and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("Invalid configured CLI proxy address")
    return normalized


def get_cli_proxy_env(environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Explicit environment wins; otherwise inherit enabled Windows manual proxy."""
    source = os.environ if environ is None else environ
    explicit = {key.upper(): value for key, value in source.items() if key.upper() in PROXY_KEYS}
    if any(key in explicit for key in TRANSPORT_KEYS):
        return {key: _proxy_url(value) if key in TRANSPORT_KEYS else value for key, value in explicit.items()}
    settings = _read_windows_proxy()
    if settings.get("ProxyEnable") != 1 or not settings.get("ProxyServer"):
        return explicit
    server = settings["ProxyServer"]
    if not isinstance(server, str):
        raise ValueError("Invalid configured CLI proxy address")
    inherited: dict[str, str] = {}
    if "=" in server:
        for mapping in server.split(";"):
            if not mapping.strip():
                continue
            protocol, separator, address = mapping.strip().partition("=")
            env_key = {"http": "HTTP_PROXY", "https": "HTTPS_PROXY", "socks": "ALL_PROXY"}.get(protocol.lower())
            if not separator or not address.strip():
                raise ValueError("Invalid configured CLI proxy mapping")
            if env_key:
                inherited[env_key] = _proxy_url(address.strip(), "socks5" if protocol.lower() == "socks" else "http")
    else:
        inherited = dict.fromkeys(("HTTP_PROXY", "HTTPS_PROXY"), _proxy_url(server.strip()))
    bypass = settings.get("ProxyOverride")
    if isinstance(bypass, str):
        # <local> has no portable equivalent; retain only explicit host entries.
        hosts = [host.strip() for host in bypass.split(";") if host.strip() and host.strip().lower() != "<local>"]
        if hosts:
            inherited["NO_PROXY"] = ",".join(hosts)
    inherited.update(explicit)
    return inherited


def redact_cli_proxy_credentials(text: str, proxy_env: Mapping[str, str]) -> str:
    """Redact URL user information and separately echoed proxy passwords."""
    for key, value in proxy_env.items():
        if key.upper() not in TRANSPORT_KEYS or not value:
            continue
        try:
            password = urlsplit(value).password
        except ValueError:
            password = None
        if password:
            # URL renderers normalize percent-hex casing; password letters remain case-sensitive.
            parts = re.split(r"(%[0-9A-Fa-f]{2})", password)
            pattern = "".join(f"(?i:{part})" if index % 2 else re.escape(part)
                              for index, part in enumerate(parts))
            text = re.sub(pattern, "[redacted]", text).replace(unquote(password), "[redacted]")
    return re.sub(r"([a-zA-Z][a-zA-Z0-9+.-]*://)[^/\s@]+@", r"\1[redacted]@", text)
