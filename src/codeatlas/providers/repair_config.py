"""Strict configuration for the structured AI repair provider.

Repair configuration is intentionally separate from review configuration.  A
review may be enabled independently, while repair generation additionally
needs an explicit online mode, bounded attempts, and a deterministic fallback
policy.  Credentials are represented only by the *name* of an environment
variable and are resolved immediately before a transport request.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .errors import ProviderConfigError


RepairMode = Literal["offline", "online"]
SUPPORTED_REPAIR_PROVIDERS = frozenset({"offline", "openai_compatible", "live"})
REPAIR_CONFIG_KEYS = frozenset({
    "provider_name",
    "model_name",
    "api_base_url",
    "api_key_env_var",
    "mode",
    "enabled",
    "timeout_seconds",
    "max_response_bytes",
    "max_repair_attempts",
    "max_retries",
    "token_budget",
    "cost_token_budget",
    "deterministic_fallback",
})
_CREDENTIAL_KEYS = frozenset({
    "api_key", "apikey", "api-key", "api_key_value", "secret", "token",
    "access_token", "auth_header", "authorization", "credentials", "password",
})


class RepairProviderConfig(BaseModel):
    """Validated, serializable repair-provider settings without credentials."""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider_name: str = "openai_compatible"
    model_name: str = Field(default="gpt-4o-mini", min_length=1, max_length=128)
    api_base_url: str | None = None
    api_key_env_var: str = Field(default="CODEATLAS_API_KEY", min_length=1, max_length=128)
    mode: RepairMode = "offline"
    enabled: bool = False
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    max_response_bytes: int = Field(default=256_000, ge=1_024, le=2_000_000)
    max_repair_attempts: int = Field(default=2, ge=1, le=5)
    max_retries: int = Field(default=1, ge=0, le=3)
    token_budget: int | None = Field(default=None, ge=1, le=2_000_000)
    cost_token_budget: float | None = Field(default=None, gt=0, le=100_000)
    deterministic_fallback: bool = True

    @property
    def max_attempts(self) -> int:
        """Short integration alias for the bounded repair-attempt limit."""
        return self.max_repair_attempts

    @property
    def maximum_response_bytes(self) -> int:
        """Descriptive alias used by configuration UIs."""
        return self.max_response_bytes

    def validate_config(self) -> None:
        """Validate semantic constraints without resolving a credential."""
        if self.provider_name not in SUPPORTED_REPAIR_PROVIDERS:
            raise ProviderConfigError("unsupported repair provider name")
        if self.mode == "online" and self.provider_name == "offline":
            raise ProviderConfigError("offline provider cannot run in online mode")
        if not self.model_name or any(char.isspace() for char in self.model_name):
            raise ProviderConfigError("unsupported repair model")
        if not self.api_key_env_var or any(char.isspace() for char in self.api_key_env_var):
            raise ProviderConfigError("invalid repair credential environment variable")
        if self.api_base_url is not None and not (
            self.api_base_url.startswith("https://") or self.api_base_url.startswith("http://localhost")
        ):
            raise ProviderConfigError("repair provider endpoint must use HTTPS (localhost is allowed for tests)")
        endpoint = self.api_base_url.lower() if self.api_base_url else ""
        if any(marker in endpoint for marker in ("api_key=", "token=", "authorization=")):
            raise ProviderConfigError("provider endpoint must not contain credentials")
        if self.mode == "online" and not self.enabled:
            raise ProviderConfigError("online repair provider is disabled")

    def resolve_api_key(self) -> str:
        """Resolve a key from the environment, never from a repository file."""
        if self.mode != "online":
            raise ProviderConfigError("repair provider is offline")
        value = os.environ.get(self.api_key_env_var, "").strip()
        if not value:
            raise ProviderConfigError("repair provider credential is unavailable")
        return value

    def to_safe_dict(self) -> dict[str, Any]:
        """Return configuration suitable for manifests and diagnostics."""
        return self.model_dump(mode="json", exclude_none=True)


def _check_credentials(value: Any, source: str, path: str = "") -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            normalized = str(key).strip().lower().replace("-", "_")
            if normalized in _CREDENTIAL_KEYS:
                raise ProviderConfigError(
                    f"credential field '{key}' is not allowed in repair configuration ({source})"
                )
            _check_credentials(item, source, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _check_credentials(item, source, f"{path}[{index}]")
    elif isinstance(value, str):
        # Reject common credential material even when hidden in a value rather
        # than a field.  Environment-variable *names* do not match these forms.
        lowered = value.lower()
        if (
            value.startswith(("sk-", "ghp_", "github_pat_", "AKIA"))
            or "-----begin " in lowered
            or any(marker in lowered for marker in ("api_key=", "token=", "authorization:"))
        ):
            raise ProviderConfigError(f"credential material is not allowed in repair configuration ({source})")


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]

        parsed = yaml.safe_load(text)
    except ImportError:
        parsed = None
    if parsed is None and text.lstrip().startswith("{"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as error:
            raise ProviderConfigError("repair configuration is not valid JSON") from error
    if not isinstance(parsed, dict):
        raise ProviderConfigError("repair configuration must contain an object")
    return parsed


def load_repair_provider_config(
    config_path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    enabled: bool | None = None,
    mode: RepairMode | None = None,
) -> RepairProviderConfig:
    """Load safe configuration from a file plus explicit non-secret overrides."""
    values: dict[str, Any] = {}
    if config_path is not None:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise ProviderConfigError("repair configuration file not found")
        values = _load_mapping(path)
        _check_credentials(values, str(path))
        unknown = set(values) - REPAIR_CONFIG_KEYS
        if unknown:
            raise ProviderConfigError("unknown repair configuration keys")
    if overrides:
        _check_credentials(overrides, "repair overrides")
        unknown = set(overrides) - REPAIR_CONFIG_KEYS
        if unknown:
            raise ProviderConfigError("unknown repair override keys")
        values.update({key: value for key, value in overrides.items() if value is not None})
    if enabled is not None:
        values["enabled"] = enabled
    if mode is not None:
        values["mode"] = mode
    try:
        result = RepairProviderConfig(**values)
        result.validate_config()
        return result
    except ProviderConfigError:
        raise
    except Exception as error:
        raise ProviderConfigError("invalid repair provider configuration") from error


__all__ = [
    "REPAIR_CONFIG_KEYS",
    "SUPPORTED_REPAIR_PROVIDERS",
    "RepairMode",
    "RepairProviderConfig",
    "load_repair_provider_config",
]
