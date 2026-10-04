# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Hermes directory-plugin entry point. Registers CLI; does not spawn a server."""

if __package__:
    from .hermes_bridge.main import register
else:
    # Test collectors may import a directory plugin as a flat __init__ module.
    from hermes_bridge.main import register

__all__ = ["register"]
