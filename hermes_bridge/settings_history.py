# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Durable private settings versions indexed over native Hermes backups."""

from __future__ import annotations

import hashlib
import importlib
import json
import os
import re
import stat
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .core import BridgeError, Object, bounded, object_json


@dataclass(frozen=True)
class State:
    """Exact live file bytes, distinguishing absent files from empty files."""

    config: bytes | None
    env: bytes | None

    @property
    def revision(self) -> str:
        """Hash both file states with explicit presence markers and length delimiters."""
        digest = hashlib.sha256()
        for data in (self.config, self.env):
            digest.update(b"missing\0" if data is None else b"present\0")
            digest.update(len(data or b"").to_bytes(8, "big"))
            digest.update(data or b"")
        return digest.hexdigest()


def safe_path(path: Path) -> None:
    """Reject links in every existing component before handing paths to native writers."""
    for component in (path, *path.parents):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            msg = "unsafe_settings_path"
            raise BridgeError(msg, "Settings and history paths must not contain symlinks")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return
    if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)):
        msg = "unsafe_settings_path"
        raise BridgeError(msg, "Settings and history paths must be regular files or directories")


def read_bytes(path: Path) -> bytes | None:
    """Read a safe regular file, preserving absence separately from empty contents."""
    safe_path(path)
    try:
        if not stat.S_ISREG(path.lstat().st_mode):
            msg = "unsafe_settings_path"
            raise BridgeError(msg, "Settings must be regular files")
        return path.read_bytes()
    except FileNotFoundError:
        return None


def read_state(home: Path) -> State:
    """Read the exact native configuration and persisted environment file bytes."""
    return State(read_bytes(home / "config.yaml"), read_bytes(home / ".env"))


def private_dir(path: Path) -> None:
    """Prepare a current-user directory with private permissions and no links."""
    safe_path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.stat().st_uid != os.getuid():
        msg = "unsafe_settings_path"
        raise BridgeError(msg, "History directories must be owned by the current user")
    path.chmod(0o700)


def _decode_metadata(raw: bytes) -> Object:
    try:
        metadata = object_json(json.loads(raw))
    except (ValueError, BridgeError) as exc:
        msg = "history_unavailable"
        raise BridgeError(msg, "Configuration history cannot be read") from exc
    return metadata


class SettingsHistory:
    """Private sidecars index Hermes's own config/.env backup files, scoped to one profile."""

    def __init__(self, profile: str, home: Path) -> None:
        """Bind history to a profile name and its absolute native home path."""
        self.profile = profile
        self.home = home
        self.module = importlib.import_module("hermes_cli.config_backups")
        self.utils = importlib.import_module("utils")
        self.root = Path(self.module.backups_dir(home / "config.yaml"))
        self.index = self.root / "bridge-versions"
        self.scope = hashlib.sha256(str(home.absolute()).encode()).hexdigest()

    def prepare(self) -> None:
        """Prepare private history directories and refuse existing linked entries."""
        private_dir(self.root.parent)
        private_dir(self.root)
        for entry in self.root.iterdir():
            safe_path(entry)
        private_dir(self.index)

    def capture(self, source_home: Path, state: State, reason: str) -> str:
        """Finish both private backup files and durable metadata before callers change live files."""
        version_id = uuid.uuid4().hex
        metadata: Object = {
            "id": version_id,
            "profile": self.profile,
            "scope": self.scope,
            "created_at": datetime.now(UTC).isoformat(),
            "reason": reason,
            "revision": state.revision,
        }
        try:
            self.prepare()
            # Native backup_config creates directories using the umask. Tighten them first.
            source_root = Path(self.module.backups_dir(source_home / "config.yaml"))
            private_dir(source_root.parent)
            private_dir(source_root)
            for filename, data, label in (
                ("config.yaml", state.config, "config"),
                (".env", state.env, "env"),
            ):
                source = source_home / filename
                safe_path(source)
                if read_bytes(source) != data:
                    msg = "revision_conflict"
                    raise BridgeError(msg, "Configuration changed while saving its version")
                backup_reason = f"bridge-{version_id}"
                if data:
                    backup: object = self.module.backup_config(source, backup_reason, keep=1)
                    if not isinstance(backup, Path):
                        msg = "history_unavailable"
                        raise BridgeError(msg, "Cannot save a private configuration version")
                    safe_path(backup)
                    backup.chmod(0o600)
                    target = self.root / backup.name
                    safe_path(target)
                    if backup != target:
                        if target.exists():
                            msg = "history_unavailable"
                            raise BridgeError(msg, "Configuration version already exists")
                        backup.replace(target)
                else:
                    target = self.root / f"{filename}.{backup_reason}.empty"
                    safe_path(target)
                    self.utils.atomic_write_bytes(target, data or b"", mode=0o600, fsync_dir=True)
                if read_bytes(target) != (data or b""):
                    msg = "history_unavailable"
                    raise BridgeError(msg, "Cannot verify saved configuration version")
                # Native backup_config uses copy2. Re-publish with its native atomic
                # writer so the backup data and directory entry are fsynced as well.
                self.utils.atomic_write_bytes(target, data or b"", mode=0o600, fsync_dir=True)
                metadata[f"{label}_file"] = target.name
                metadata[f"{label}_exists"] = data is not None
            path = self.index / f"{version_id}.json"
            safe_path(path)
            self.utils.atomic_json_write(path, metadata, mode=0o600, fsync_dir=True)
            self.load(version_id)  # Validate the on-disk pair before authorizing a write.
        except (OSError, ValueError) as exc:
            msg = "history_unavailable"
            raise BridgeError(msg, "Cannot save a private configuration version") from exc
        return version_id

    def _metadata(self, version_id: str) -> Object:
        if not re.fullmatch(r"[a-f0-9]{32}", version_id):
            msg = "invalid_input"
            raise BridgeError(msg, "A configuration version ID from history is required")
        path = self.index / f"{version_id}.json"
        safe_path(path)
        try:
            raw = read_bytes(path)
            if raw is None:
                msg = "version_not_found"
                raise BridgeError(msg, "Configuration version does not exist in this profile")
            for directory in (self.root.parent, self.root, self.index):
                directory_info = directory.stat()
                if (
                    not stat.S_ISDIR(directory_info.st_mode)
                    or directory_info.st_uid != os.getuid()
                    or directory_info.st_mode & 0o077
                ):
                    msg = "history_unavailable"
                    raise BridgeError(msg, "Configuration history directories are not private")
            info = path.stat()
            if info.st_uid != os.getuid() or info.st_mode & 0o077:
                msg = "history_unavailable"
                raise BridgeError(msg, "Configuration history is not private")
            metadata = _decode_metadata(raw)
        except (OSError, ValueError) as exc:
            msg = "history_unavailable"
            raise BridgeError(msg, "Configuration history cannot be read") from exc
        if (
            metadata.get("id") != version_id
            or metadata.get("profile") != self.profile
            or metadata.get("scope") != self.scope
        ):
            msg = "version_not_found"
            raise BridgeError(msg, "Configuration version does not belong to this profile")
        return metadata

    def load(self, version_id: str) -> tuple[State, Object]:
        """Verify ownership, permissions, scope, native indexing, and snapshot integrity."""
        metadata = self._metadata(version_id)
        values: list[bytes | None] = []
        for filename, label in (("config.yaml", "config"), (".env", "env")):
            name = metadata.get(f"{label}_file")
            exists = metadata.get(f"{label}_exists")
            prefix = f"{filename}.bridge-{version_id}."
            if (
                not isinstance(name, str)
                or not name.startswith(prefix)
                or Path(name).name != name
                or not isinstance(exists, bool)
            ):
                msg = "history_unavailable"
                raise BridgeError(msg, "Configuration version metadata is invalid")
            path = self.root / name
            safe_path(path)
            # Require the indexed file to still be a native backup in this profile.
            native_files: object = self.module.list_config_backups(
                self.home / filename, f"bridge-{version_id}"
            )
            if not isinstance(native_files, list) or path not in native_files:
                msg = "history_unavailable"
                raise BridgeError(msg, "Configuration version backup is missing")
            try:
                data = read_bytes(path)
                info = path.stat()
            except OSError as exc:
                msg = "history_unavailable"
                raise BridgeError(msg, "Configuration version backup cannot be read") from exc
            if data is None or info.st_uid != os.getuid() or info.st_mode & 0o077:
                msg = "history_unavailable"
                raise BridgeError(msg, "Configuration version backup is not private")
            if not exists and data:
                msg = "history_unavailable"
                raise BridgeError(msg, "Missing-file version backup is invalid")
            values.append(data if exists else None)
        state = State(values[0], values[1])
        if metadata.get("revision") != state.revision:
            msg = "history_unavailable"
            raise BridgeError(msg, "Configuration version integrity check failed")
        return state, metadata

    def versions(self, limit: int) -> Object:
        """Return newest verified version metadata up to a validated page limit."""
        bounded(limit, 1, 1000)
        safe_path(self.root)
        safe_path(self.index)
        if not self.index.exists():
            return {"profile": self.profile, "versions": [], "count": 0}
        items: list[Object] = []
        for path in self.index.iterdir():
            safe_path(path)
            if not re.fullmatch(r"[a-f0-9]{32}\.json", path.name):
                continue
            _state, metadata = self.load(path.stem)
            items.append({key: metadata[key] for key in ("id", "created_at", "reason", "revision")})
        items.sort(key=lambda item: str(item["created_at"]), reverse=True)
        return object_json(
            {
                "profile": self.profile,
                "versions": items[:limit],
                "count": len(items),
                "has_more": len(items) > limit,
            }
        )
