# Copyright (c) 2026 Hermes HTTP MCP contributors
"""Private, versioned native settings with optimistic revision checks."""

from __future__ import annotations

import importlib
import re
import tempfile
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Literal
from urllib.parse import parse_qsl, urlsplit

from .core import BridgeError, Json, Object, json_value, object_json
from .settings_history import SettingsHistory, State, read_bytes, read_state, safe_path

if TYPE_CHECKING:
    from .native import Native

# Convenience edits retain their original small validation contract. Full apply uses
# the native validator and accepts Hermes's extensible configuration tree.
FIELDS: dict[str, str] = {
    "model.default": "string",
    "model.provider": "string",
    "agent.max_turns": "nullable_integer",
    "agent.reasoning_effort": "string",
    "agent.max_tokens": "nullable_integer",
    "memory.enabled": "boolean",
    "memory.provider": "string",
    "memory.memory_char_limit": "integer",
    "memory.user_char_limit": "integer",
    "toolsets": "strings",
}
# This public preserve marker instructs the bridge to retain an existing secret.
PRESERVE: Object = {"$hermes_secret": "preserve"}  # nosec B105
_LOCK = threading.RLock()
_MISSING = object()
_MAX_STRING_LENGTH = 256
_MAX_INTEGER = 1_000_000
_MAX_TOOLSETS = 64
_MAX_TOOLSET_LENGTH = 128
_CREDENTIAL_NAME = re.compile(
    r"(?:^|_)(?:tokens?|secrets?|passwords?|passwd|api_?keys?|auth|authorization|bearer|credentials?|cookies?)(?:_|$)"
)


def _extension_paths(config: Object, defaults: Object, path: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Keep authored extension keys, including nulls that the native stripper treats as defaults."""
    paths: set[tuple[str, ...]] = set()
    for key, value in config.items():
        child = (*path, key)
        if key not in defaults:
            paths.add(child)
        elif isinstance(value, dict):
            if isinstance(defaults[key], dict):
                paths.update(_extension_paths(value, object_json(defaults[key]), child))
            else:
                paths.add(child)
    return paths


def _authored_paths(config: Object, path: tuple[str, ...] = ()) -> set[tuple[str, ...]]:
    """Record explicit leaves and empty maps, which can also pin a native default."""
    paths: set[tuple[str, ...]] = set()
    for key, value in config.items():
        child = (*path, key)
        if isinstance(value, dict) and value:
            paths.update(_authored_paths(value, child))
        else:
            paths.add(child)
    return paths


def _path_value(config: Object, path: tuple[str, ...]) -> object:
    value: object = config
    for key in path:
        if not isinstance(value, dict) or key not in value:
            return _MISSING
        value = value[key]
    return value


def _restore_empty_maps(compact: Object, desired: Object, authored: set[tuple[str, ...]]) -> None:
    """Retain authored containers after their inherited children have been stripped."""
    for path in authored:
        if not isinstance(_path_value(desired, path), dict) or _path_value(compact, path) is not _MISSING:
            continue
        parent = compact
        for key in path[:-1]:
            child = object_json(parent.get(key, {}))
            parent[key] = child
            parent = child
        parent[path[-1]] = {}


class Settings:
    """Read, validate, publish, and restore profile settings through native writers."""

    def __init__(self, native: Native) -> None:
        """Load native persistence APIs while retaining profile and write controls."""
        self.native = native
        self.module = importlib.import_module("hermes_cli.config")
        self.utils = importlib.import_module("utils")
        self.config_lock = self.module._CONFIG_LOCK  # noqa: SLF001 - Native config lock.

    @staticmethod
    def validate(changes: Object) -> Object:
        """Validate the bounded dotted-field convenience update contract."""
        if not changes or len(changes) > len(FIELDS):
            msg = "invalid_input"
            raise BridgeError(msg, "Supply one or more supported settings")
        for key, value in changes.items():
            kind = FIELDS.get(key)
            valid = False
            if kind == "string":
                valid = isinstance(value, str) and 0 < len(value) <= _MAX_STRING_LENGTH and "\n" not in value
            elif kind == "boolean":
                valid = isinstance(value, bool)
            elif kind in ("integer", "nullable_integer"):
                valid = (value is None and kind == "nullable_integer") or (
                    isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= _MAX_INTEGER
                )
            elif kind == "strings":
                valid = (
                    isinstance(value, list)
                    and len(value) <= _MAX_TOOLSETS
                    and all(isinstance(x, str) and 0 < len(x) <= _MAX_TOOLSET_LENGTH for x in value)
                )
            if not valid:
                msg = "invalid_setting"
                raise BridgeError(msg, f"Unsupported field or invalid value: {key}")
        return changes

    def schema(self) -> Object:
        """Describe supported updates, full replacement, secret markers, and history."""
        return {
            "fields": dict(FIELDS),
            "secrets_exported": False,
            "defaults": object_json(self._redact(object_json(self.module.DEFAULT_CONFIG))),
            "full_apply": True,
            "dynamic_keys": True,
            "env_supported": True,
            "secret_preserve_marker": dict(PRESERVE),
            "update": "Get revision, then submit dotted convenience changes and expected_revision.",
            "apply": "Replace config with the complete desired tree. Omitted keys are removed. "
            "Inherited defaults are omitted unless persist_defaults=true; existing explicit values stay. "
            "Supply env to replace persisted environment assignments; null leaves .env unchanged.",
            "versions": "Private configuration and environment versions persist under native backups/config.",
        }

    def _normal(self, config: Object) -> Object:
        config = object_json(config)
        # Native merging accepts the legacy scalar form but consumers need model.default.
        if isinstance(config.get("model"), str):
            config["model"] = {"default": config["model"]}
        try:
            normalized = self.module._canonicalize_config(config)  # noqa: SLF001 - Native canonicalization.
            return object_json(normalized)
        except (TypeError, ValueError) as exc:
            msg = "invalid_setting"
            raise BridgeError(msg, "Native configuration section has an invalid structure") from exc

    def _read_config(self, path: Path) -> Object:
        safe_path(path)
        try:
            return self._normal(object_json(self.module.require_readable_config_before_write(path)))
        except BridgeError:
            raise
        except Exception as exc:
            # Native exceptions can include YAML lines or supplied credential values.
            msg = "invalid_settings"
            raise BridgeError(msg, "Native configuration cannot be read safely") from exc

    def _redact(self, value: Json, *, secret: bool = False) -> Json:
        if (secret and value not in (None, "")) or self._credential_value(value):
            return dict(PRESERVE)
        if isinstance(value, dict):
            return {key: self._redact(item, secret=self._is_secret(key, item)) for key, item in value.items()}
        if isinstance(value, list):
            return [self._redact(item) for item in value]
        return value

    def _resolve(self, value: Json, current: object, *, secret: bool = False) -> Json:
        if value == PRESERVE:
            if current is _MISSING or not (secret or self._credential_value(current)):
                msg = "invalid_setting"
                raise BridgeError(msg, "Secret preserve marker has no existing secret at this location")
            return json_value(current)
        if isinstance(value, dict):
            existing = current if isinstance(current, dict) else {}
            return {
                key: self._resolve(item, existing.get(key, _MISSING), secret=self._is_secret(key, item))
                for key, item in value.items()
            }
        if isinstance(value, list):
            previous = current if isinstance(current, list) else []
            return [
                self._resolve(item, previous[index] if index < len(previous) else _MISSING)
                for index, item in enumerate(value)
            ]
        return value

    def _is_secret(self, key: str, value: Json) -> bool:
        # Native suffix rules omit plural names such as A2A_PEER_TOKENS and
        # credential containers. Keep one classifier for export and round trips.
        if self.module._is_secret_config_key(key):  # noqa: SLF001 - Native secret classifier.
            return True
        for registry in (self.module.OPTIONAL_ENV_VARS, self.module.REQUIRED_ENV_VARS):
            metadata: object = registry.get(key)
            if isinstance(metadata, dict) and metadata.get("password") is True:
                return True
        # Booleans and numeric limits are configuration, not credential strings.
        if value is None or isinstance(value, (bool, int, float)):
            return False
        normalized = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", key).lower().replace("-", "_")
        return bool(_CREDENTIAL_NAME.search(normalized))

    def _credential_argument(self, value: object) -> bool:
        if not isinstance(value, str):
            return False
        if value.startswith("-"):
            name, _separator, argument = value.partition("=")
            if self._is_secret(name.lstrip("-"), value) or self._credential_value(argument):
                return True
        return self._credential_value(value)

    def _credential_value(self, value: object) -> bool:
        if isinstance(value, list):
            # Stdio credentials can be argv values rather than named JSON leaves.
            # Mask the whole list so unchanged exports preserve the exact argv.
            return any(self._credential_argument(item) for item in value)
        if not isinstance(value, str):
            return False
        # Header strings occur in argv as well as named header objects. Reuse
        # the central classifier for custom credential headers, including API keys.
        header = re.match(r"^\s*([!#$%&'*+.^_`|~0-9A-Za-z-]+)\s*:\s*(\S.*)$", value)
        if re.match(r"(?i)^(?:bearer|basic)\s+\S+", value) or (
            header and self._is_secret(header[1], header[2])
        ):
            return True
        if "://" not in value:
            return False
        try:
            url = urlsplit(value)
            return bool(url.username or url.password) or any(
                self._is_secret(key, item) for key, item in parse_qsl(url.query)
            )
        except ValueError:
            # Invalid URL credentials must not escape merely because parsing failed.
            return "@" in value or bool(_CREDENTIAL_NAME.search(value.lower()))

    def _effective(self, config: Object) -> Object:
        merged = self.module._deep_merge(self.module.DEFAULT_CONFIG, config)  # noqa: SLF001 - Native merging.
        # Native saves remove pinned leaves from user files; read their unexpanded
        # administrator values separately so introspection matches the native policy.
        managed = self._normal(object_json(self.module.managed_scope.load_managed_config()))
        merged = self.module._deep_merge(merged, managed)  # noqa: SLF001 - Native managed overlay.
        return object_json(merged)

    def _get(self, profile: str, home: Path) -> Object:
        try:
            state = read_state(home)
            raw = self._read_config(Path(self.module.get_config_path()))
            config = self._effective(raw)
            # Read persisted .env only; native load_env does not mutate process environment.
            env = object_json(self.module.load_env())
            if read_state(home) != state:
                msg = "revision_conflict"
                raise BridgeError(msg, "Configuration changed while reading; read it again")
        except OSError as exc:
            msg = "invalid_settings"
            raise BridgeError(msg, "Native settings files cannot be read") from exc
        settings: Object = {}
        for key in FIELDS:
            value: Json = config
            for part in key.split("."):
                value = value.get(part) if isinstance(value, dict) else None
            settings[key] = value
        return {
            "profile": profile,
            "revision": state.revision,
            "config": self._redact(config),
            "config_overrides": self._redact(raw),
            "env": self._redact(env),
            "settings": self._redact(settings),
            "secrets_exported": False,
            "references_expanded": False,
        }

    def get(self, profile: str, *, view: Literal["full", "editable"] = "full") -> Object:
        """Read a stable, unexpanded settings snapshot with secrets replaced by markers."""
        with _LOCK, self.native.home_scope(profile) as home, self.config_lock:
            result = self._get(profile, home)
            if view == "editable":
                for key in ("config", "config_overrides", "env"):
                    result.pop(key)
            return result

    @staticmethod
    def _require_revision(state: State, expected_revision: str) -> None:
        if not re.fullmatch(r"[a-f0-9]{64}", expected_revision):
            msg = "invalid_input"
            raise BridgeError(msg, "expected_revision from hermes_settings_get is required")
        if state.revision != expected_revision:
            msg = "revision_conflict"
            raise BridgeError(msg, "Configuration or .env changed; read it again before writing")

    def _validate_full(self, config: Object) -> Object:
        try:
            issues: object = self.module.validate_config_structure(config)
        except BridgeError:
            raise
        except Exception as exc:
            msg = "invalid_setting"
            raise BridgeError(msg, "Native configuration validation failed") from exc
        if not isinstance(issues, list):
            msg = "invalid_response"
            raise BridgeError(msg, "Native settings validator returned invalid data")
        errors = sum(getattr(issue, "severity", "") == "error" for issue in issues)
        warnings = sum(getattr(issue, "severity", "") == "warning" for issue in issues)
        report: Object = {"error_count": errors, "warning_count": warnings}
        if errors:
            msg = "invalid_setting"
            raise BridgeError(msg, "Native configuration validation failed", report)
        return report

    def _env_lines(self, env: Object) -> list[str]:
        lines: list[str] = []
        for key, value in env.items():
            if (
                not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or not isinstance(value, str)
                or any(character in value for character in ("\n", "\r", "\0"))
            ):
                msg = "invalid_setting"
                raise BridgeError(msg, "Environment names and single-line string values are required")
            quoted = self.module._quote_env_value(value)  # noqa: SLF001 - Native .env escaping.
            lines.append(f"{key}={quoted}\n")
        return lines

    def _managed(self, previous: Object, desired: Object, old_env: Object, env: Object | None) -> None:
        if self.module.is_managed():
            msg = "access_denied"
            raise BridgeError(msg, "Native settings are managed by the installation")
        managed: object = self.module.managed_scope.managed_config_keys()
        if isinstance(managed, (set, list, tuple)):
            for key in managed:
                if not isinstance(key, str):
                    continue
                old: object = previous
                new: object = desired
                for part in key.split("."):
                    old = old.get(part, _MISSING) if isinstance(old, dict) else _MISSING
                    new = new.get(part, _MISSING) if isinstance(new, dict) else _MISSING
                if old != new:
                    msg = "access_denied"
                    raise BridgeError(msg, "An administrator manages a requested configuration setting")
        if env is not None:
            for key in set(old_env) | set(env):
                if old_env.get(key, _MISSING) != env.get(
                    key, _MISSING
                ) and self.module.managed_scope.is_env_managed(key):
                    msg = "access_denied"
                    raise BridgeError(msg, "An administrator manages a requested environment setting")

    def _publish(self, home: Path, state: State) -> None:
        for filename, data in (("config.yaml", state.config), (".env", state.env)):
            path = home / filename
            safe_path(path)
            if read_bytes(path) == data:
                continue
            if data is None:
                path.unlink(missing_ok=True)
                self.utils.fsync_directory(home)
            else:
                self.utils.atomic_write_bytes(path, data, mode=0o600, fsync_dir=True)
        self._clear_config_cache(home)
        self.module.invalidate_env_cache()

    def _clear_config_cache(self, home: Path) -> None:
        key = str(home / "config.yaml")
        self.module._RAW_CONFIG_CACHE.pop(key, None)  # noqa: SLF001 - Native cache invalidation.
        self.module._LAST_EXPANDED_CONFIG_BY_PATH.pop(key, None)  # noqa: SLF001 - Native cache invalidation.

    def _publish_verified(self, home: Path, state: State) -> None:
        self._publish(home, state)
        if read_state(home) != state:
            msg = "settings file verification failed"
            raise OSError(msg)

    def _publish_guarded(self, home: Path, target: State, previous: State, before_id: str) -> None:
        try:
            self._publish_verified(home, target)
        except (OSError, BridgeError) as exc:
            try:
                self._publish_verified(home, previous)
            except (OSError, BridgeError) as recovery:
                msg = "settings_recovery_required"
                raise BridgeError(
                    msg,
                    "Settings write and automatic recovery failed",
                    {"saved_current_version_id": before_id},
                ) from recovery
            msg = "settings_write_failed"
            raise BridgeError(
                msg,
                "Settings write failed; the previous version was restored",
                {"saved_current_version_id": before_id},
            ) from exc

    def _stage(
        self,
        history: SettingsHistory,
        before: State,
        desired: Object,
        lines: list[str] | None,
        preserve_keys: set[tuple[str, ...]],
    ) -> tuple[State, str]:
        # Both private versions must be durable before either live file changes.
        with tempfile.TemporaryDirectory(prefix=".bridge-stage-", dir=history.root) as temporary:
            stage = Path(temporary)
            if before.config is not None:
                self.utils.atomic_write_bytes(stage / "config.yaml", before.config, mode=0o600)
            token = self.native.constants.set_hermes_home_override(stage)
            try:
                # The staged raw file preserves authored defaults; a get() snapshot must not
                # turn inherited values into overrides (notably the fail-closed dispatch allowlist).
                defaults = self._normal(object_json(self.module.DEFAULT_CONFIG))
                extensions = _extension_paths(desired, defaults)
                leaves = {path for path in preserve_keys if not isinstance(_path_value(desired, path), dict)}
                # Native stripping compares the unnormalized schema; aliases such as scalar
                # model defaults must first be compared in the same canonical form as desired.
                compact = self.module._strip_default_values(  # noqa: SLF001 - Native strip with canonical schema.
                    desired, defaults, preserve_keys=leaves | extensions
                )
                _restore_empty_maps(compact, desired, preserve_keys)
                self.module.save_config(
                    compact,
                    strip_defaults=True,
                    preserve_keys=preserve_keys | extensions,
                    merge_existing=False,
                )
                if lines is not None:
                    writer = self.module._write_env_lines  # noqa: SLF001 - Native full .env writer.
                    writer(stage / ".env", lines, preserve_mode=False)
                elif before.env is not None:
                    self.utils.atomic_write_bytes(stage / ".env", before.env, mode=0o600)
            finally:
                self.native.constants.reset_hermes_home_override(token)
                # Discard native memoized state for the temporary staging home.
                self._clear_config_cache(stage)
                ensured = self.module._HERMES_HOME_ENSURED  # noqa: SLF001 - Native home memoization.
                ensured.discard(str(stage))
            after = read_state(stage)
            return after, history.capture(stage, after, "apply")

    def _apply(
        self,
        profile: str,
        home: Path,
        config: Object,
        env: Object | None,
        expected_revision: str,
        *,
        preserve_keys: set[tuple[str, ...]],
    ) -> Object:
        before = read_state(home)
        self._require_revision(before, expected_revision)
        previous = self._read_config(home / "config.yaml")
        # Aliases in the staged raw file may have different paths from the canonical desired tree.
        preserve_keys |= _authored_paths(previous)
        old_env = object_json(self.module.load_env())
        effective = self._effective(previous)
        desired = self._normal(object_json(self._resolve(object_json(config), effective)))
        desired_env = object_json(self._resolve(object_json(env), old_env)) if env is not None else None
        lines = self._env_lines(desired_env) if desired_env is not None else None
        self._managed(previous, desired, old_env, desired_env)
        validation = self._validate_full(desired)
        history = SettingsHistory(profile, home)
        try:
            history.prepare()
            before_id = history.capture(home, before, "before_apply")
            after, after_id = self._stage(history, before, desired, lines, preserve_keys)
            self._require_revision(read_state(home), expected_revision)
            self._publish_guarded(home, after, before, before_id)
        except OSError as exc:
            msg = "history_unavailable"
            raise BridgeError(msg, "Cannot prepare persistent settings versions") from exc
        result = self._get(profile, home)
        result.update(
            {
                "before_version_id": before_id,
                "version_id": after_id,
                "validation": validation,
                "restart_required": (
                    "New turns load settings; existing agents may retain their configuration."
                ),
            }
        )
        return result

    def apply(
        self,
        profile: str,
        config: Object,
        env: Object | None,
        expected_revision: str,
        *,
        persist_defaults: bool = False,
    ) -> Object:
        """Replace the native tree after durable snapshots and an optimistic revision check."""
        self.native.config.require_writes()
        with _LOCK, self.native.home_scope(profile) as home, self.config_lock:
            preserve_keys = _authored_paths(self._normal(config)) if persist_defaults else set()
            return self._apply(profile, home, config, env, expected_revision, preserve_keys=preserve_keys)

    def update(self, profile: str, changes: Object, expected_revision: str) -> Object:
        """Apply supported dotted changes while retaining unrelated native configuration."""
        self.native.config.require_writes()
        self.validate(changes)
        with _LOCK, self.native.home_scope(profile) as home, self.config_lock:
            desired = self._read_config(home / "config.yaml")
            for key, value in changes.items():
                parts = key.split(".")
                if len(parts) == 1:
                    desired[key] = value
                else:
                    section = desired.setdefault(parts[0], {})
                    if not isinstance(section, dict):
                        msg = "invalid_setting"
                        raise BridgeError(msg, "Conflicting configuration keys")
                    section[parts[1]] = json_value(value)
            return self._apply(
                profile,
                home,
                desired,
                None,
                expected_revision,
                preserve_keys={tuple(key.split(".")) for key in changes},
            )

    def versions(self, profile: str, limit: int = 50) -> Object:
        """List verified private version metadata without revealing saved setting values."""
        with _LOCK, self.native.home_scope(profile) as home, self.config_lock:
            return SettingsHistory(profile, home).versions(limit)

    def restore(self, profile: str, version_id: str, expected_revision: str) -> Object:
        """Restore exact configuration and environment bytes from a verified private version."""
        self.native.config.require_writes()
        with _LOCK, self.native.home_scope(profile) as home, self.config_lock:
            before = read_state(home)
            self._require_revision(before, expected_revision)
            previous = self._read_config(home / "config.yaml")
            history = SettingsHistory(profile, home)
            target, _metadata = history.load(version_id)
            # Parse native backups without rewriting them, preserving exact snapshot bytes.
            saved: object = (
                self.utils.fast_safe_load(target.config.decode("utf-8-sig")) if target.config else {}
            )
            desired = self._normal(object_json(saved or {}))
            secret_scope = importlib.import_module("agent.secret_scope")
            decode_env = secret_scope._decode_env_bytes  # noqa: SLF001 - Native .env byte decoding.
            parse_env = secret_scope._parse_env_text  # noqa: SLF001 - Native .env assignment parsing.
            target_env = object_json(parse_env(decode_env(target.env or b"")))
            self._managed(previous, desired, object_json(self.module.load_env()), target_env)
            validation = self._validate_full(desired)
            before_id = history.capture(home, before, "before_restore")
            self._require_revision(read_state(home), expected_revision)
            self._publish_guarded(home, target, before, before_id)
            result = self._get(profile, home)
            result.update(
                {
                    "saved_current_version_id": before_id,
                    "restored_version_id": version_id,
                    "version_id": version_id,
                    "validation": validation,
                    "restart_required": (
                        "New turns load settings; existing agents may retain their configuration."
                    ),
                }
            )
            return result
