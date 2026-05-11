"""Bypass kimi-cli ACP _check_auth so MOONSHOT_API_KEY satisfies auth.
Injected via PYTHONPATH when spawning `kimi acp`."""
try:
    import kimi_cli.acp.server as _s
    for name in ("AcpServer", "ACPServer"):
        cls = getattr(_s, name, None)
        if cls and hasattr(cls, "_check_auth"):
            cls._check_auth = lambda self: None
            break
except Exception:
    pass
