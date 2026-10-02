"""Typed configuration for reviewer providers with strict credential boundaries.

API keys are never accepted in source files, ``.codeatlas.yml``, provider
config files, packets, manifests, logs, or terminal output.  Credentials are
resolved exclusively from environment variables named by ``api_key_env_var``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from pydantic import BaseModel, ConfigDict, Field

from .errors import ProviderConfigError

ALLOWED_PRIVACY_MODES = ("zero_retention", "no_training", "standard")

# Keys a provider config file may set.  Deliberately excludes any credential
# material: there is no accepted spelling of an inline API key.
ALLOWED_CONFIG_KEYS = frozenset({
    "provider_name",
    "model_name",
    "api_base_url",
    "api_key_env_var",
    "timeout_seconds",
    "max_retries",
    "max_output_tokens",
    "max_response_bytes",
    "max_packet_bytes",
    "temperature",
    "request_budget",
    "privacy_mode",
})

_FORBIDDEN_CREDENTIAL_KEYS = ("api_key", "apikey", "api-key", "api_key_value", "secret", "token", "auth_header")


class ProviderConfig(BaseModel):
    """Configuration for a reviewer provider backend."""

    model_config = ConfigDict(extra="ignore")

    provider_name: str = "live"
    model_name: str = "gpt-4o"
    api_base_url: str | None = None
    api_key_env_var: str = "CODEATLAS_API_KEY"
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    max_retries: int = Field(default=3, ge=0, le=10)
    max_output_tokens: int = Field(default=4096, ge=1, le=200_000)
    max_response_bytes: int = Field(default=2_000_000, ge=1)
    max_packet_bytes: int = Field(default=100_000, ge=1)
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    request_budget: int = Field(default=5, ge=1)
    enabled: bool = False
    privacy_mode: str = "zero_retention"
    dry_run: bool = False
    estimated_cost_per_1k_input: float = Field(default=0.005, ge=0)
    estimated_cost_per_1k_output: float = Field(default=0.015, ge=0)

    def validate_config(self) -> None:
        """Fail closed on invalid configuration before any request is made.

        Raises:
            ProviderConfigError: If any value is outside its safe range or the
                privacy mode is unknown.
        """
        if self.provider_name not in {"live"}:
            raise ProviderConfigError(f"Unknown live provider name: {self.provider_name!r}")
        if not self.model_name or any(ch.isspace() for ch in self.model_name):
            raise ProviderConfigError("model_name must be a non-empty identifier without whitespace")
        if not self.api_key_env_var or any(ch.isspace() for ch in self.api_key_env_var):
            raise ProviderConfigError("api_key_env_var must be a non-empty environment variable name")
        if self.privacy_mode not in ALLOWED_PRIVACY_MODES:
            raise ProviderConfigError(
                f"privacy_mode must be one of {ALLOWED_PRIVACY_MODES}, got {self.privacy_mode!r}"
            )
        if self.api_base_url is not None:
            url = self.api_base_url.strip()
            if not (url.startswith("https://") or url.startswith("http://localhost")):
                raise ProviderConfigError("api_base_url must be an https:// URL (http://localhost allowed for tests)")
        # NOTE: credential presence is intentionally NOT checked here; it is
        # resolved at transport time so fake transports and dry runs never
        # require real credentials.

    def resolve_api_key(self) -> str:
        """Resolve the API key from environment variables without persisting it.

        Raises:
            ProviderConfigError: If the environment variable is unset or empty.
        """
        if self.dry_run:
            return "dry-run-sentinel"

        key = os.environ.get(self.api_key_env_var, "").strip()
        if not key:
            raise ProviderConfigError(
                f"Missing API key environment variable '{self.api_key_env_var}'. "
                "Live provider calls require credentials configured in the environment. "
                "Never store API keys in repository files or configuration."
            )
        return key

    def to_safe_dict(self) -> dict[str, Any]:
        """Return safe configuration dictionary guaranteed free of credentials."""
        return {
            "provider_name": self.provider_name,
            "model_name": self.model_name,
            "api_base_url": self.api_base_url,
            "api_key_env_var": self.api_key_env_var,
            "timeout_seconds": self.timeout_seconds,
            "max_retries": self.max_retries,
            "max_output_tokens": self.max_output_tokens,
            "max_response_bytes": self.max_response_bytes,
            "max_packet_bytes": self.max_packet_bytes,
            "temperature": self.temperature,
            "request_budget": self.request_budget,
            "enabled": self.enabled,
            "privacy_mode": self.privacy_mode,
            "dry_run": self.dry_run,
        }


def _load_mapping(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore[import-untyped]

        parsed = yaml.safe_load(text)
    except ImportError:
        parsed = None
    if parsed is None and not text.lstrip().startswith("{"):
        raise ProviderConfigError(f"Provider config file could not be parsed: {path}")
    if parsed is None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as err:
            raise ProviderConfigError(f"Provider config file is not valid YAML or JSON: {path}: {err}") from err
    if not isinstance(parsed, dict):
        raise ProviderConfigError(f"Provider config file must contain a mapping: {path}")
    return parsed


def _check_no_inline_credentials(data: dict[str, Any], source: str) -> None:
    for key in data:
        normalized = str(key).strip().lower().replace("-", "_")
        if normalized in _FORBIDDEN_CREDENTIAL_KEYS:
            raise ProviderConfigError(
                f"Refusing to load inline credential key '{key}' from {source}. "
                "Store credentials in an environment variable and reference it via api_key_env_var."
            )


def load_provider_config(
    config_path: str | Path | None = None,
    *,
    overrides: dict[str, Any] | None = None,
    enabled: bool = True,
    dry_run: bool = False,
) -> ProviderConfig:
    """Build a ProviderConfig from an optional file plus explicit overrides.

    File keys are restricted to ``ALLOWED_CONFIG_KEYS``; any inline credential
    key fails closed.  Overrides (e.g. CLI flags) win over file values.
    """
    file_values: dict[str, Any] = {}
    if config_path is not None:
        path = Path(config_path).expanduser()
        if not path.is_file():
            raise ProviderConfigError(f"Provider config file not found: {path}")
        file_values = _load_mapping(path)
        _check_no_inline_credentials(file_values, str(path))
        unknown = set(file_values) - ALLOWED_CONFIG_KEYS
        if unknown:
            raise ProviderConfigError(f"Unknown provider config keys in {path}: {sorted(unknown)}")

    merged: dict[str, Any] = dict(file_values)
    if overrides:
        _check_no_inline_credentials(overrides, "provider overrides")
        unknown = set(overrides) - ALLOWED_CONFIG_KEYS
        if unknown:
            raise ProviderConfigError(f"Unknown provider override keys: {sorted(unknown)}")
        merged.update({k: v for k, v in overrides.items() if v is not None})

    merged["enabled"] = enabled
    merged["dry_run"] = dry_run

    try:
        config = ProviderConfig(**merged)
    except Exception as err:  # pydantic validation -> typed config error
        raise ProviderConfigError(f"Invalid provider configuration: {err}") from err
    config.validate_config()
    return config


__all__ = [
    "ProviderConfig",
    "ALLOWED_CONFIG_KEYS",
    "ALLOWED_PRIVACY_MODES",
    "load_provider_config",
]
