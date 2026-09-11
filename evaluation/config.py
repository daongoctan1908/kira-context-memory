"""Explicit eval settings; importing this module never loads the application's .env."""

import json
import os
from collections.abc import Mapping
from hashlib import sha256
from pathlib import Path
from typing import Annotated, Literal

from dotenv import dotenv_values
from pydantic import AnyHttpUrl, Field, SecretStr, StringConstraints, field_validator

from evaluation.models import EvalModel, Profile, Suite

ModelId = Annotated[str, StringConstraints(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_./:-]{0,199}$")]


class ProviderConfig(EvalModel):
    base_url: AnyHttpUrl | None = None
    model: ModelId | None = None
    api_key: SecretStr | None = Field(default=None, exclude=True, repr=False)
    auth_required: bool = False

    @field_validator("base_url")
    @classmethod
    def clean_endpoint(cls, value: AnyHttpUrl | None) -> AnyHttpUrl | None:
        if value and (value.username or value.password or value.query or value.fragment):
            raise ValueError("endpoint must not contain credentials, query or fragment")
        return value

    @field_validator("model")
    @classmethod
    def model_is_not_a_key(cls, value: str | None) -> str | None:
        if value and value.startswith("sk-"):
            raise ValueError("expected a model identifier")
        return value

    @property
    def configured(self) -> bool:
        return bool(
            self.base_url
            and self.model
            and (
                not self.auth_required or (self.api_key and self.api_key.get_secret_value().strip())
            )
        )


class EvalConfig(EvalModel):
    profile: Profile = Profile.MOCK
    suites: tuple[Suite, ...] = Field(default=(Suite.FORMATION,), min_length=1)
    formation_mode: Literal["write_free", "persistent"] = "write_free"
    extraction: ProviderConfig = Field(default_factory=ProviderConfig)
    rewrite: ProviderConfig = Field(default_factory=ProviderConfig)
    embedding: ProviderConfig = Field(default_factory=ProviderConfig)
    embedding_dimensions: int | None = Field(default=None, ge=1, le=65536, strict=True)
    extraction_json_mode: Literal["json_object", "prompt_only"] = "json_object"
    connect_timeout_seconds: float = Field(default=2.0, gt=0, le=60, allow_inf_nan=False)
    read_timeout_seconds: float = Field(default=8.0, gt=0, le=120, allow_inf_nan=False)
    total_timeout_seconds: float = Field(default=15.0, gt=0, le=180, allow_inf_nan=False)
    max_response_bytes: int = Field(default=1_048_576, ge=1024, le=4_194_304, strict=True)
    extraction_max_tokens: int = Field(default=1000, ge=1, le=4096, strict=True)
    rewrite_max_tokens: int = Field(default=256, ge=1, le=4096, strict=True)
    temperature: float = Field(default=0.0, ge=0, le=2, allow_inf_nan=False)
    retries: Literal[0] = 0
    database_url: SecretStr | None = Field(default=None, exclude=True, repr=False)
    memory_database_url: SecretStr | None = Field(default=None, exclude=True, repr=False)
    memory_schema: str = Field(default="memory", pattern=r"^[a-z_][a-z0-9_]{0,62}$")
    memory_collection: str = Field(default="memories", pattern=r"^[a-z_][a-z0-9_]{0,43}$")
    gateway_url: AnyHttpUrl | None = None
    worker_url: AnyHttpUrl | None = None
    kira_mock_url: AnyHttpUrl | None = None

    _clean_service_urls = field_validator("gateway_url", "worker_url", "kira_mock_url")(
        ProviderConfig.clean_endpoint.__func__
    )

    @field_validator("database_url", "memory_database_url")
    @classmethod
    def postgres_only(cls, value: SecretStr | None) -> SecretStr | None:
        if value and not value.get_secret_value().startswith(
            ("postgresql://", "postgres://", "postgresql+asyncpg://")
        ):
            raise ValueError("expected PostgreSQL connection URI")
        return value

    def fingerprint(self) -> str:
        """Hash only serializable non-secret config; credentials and DB URIs are excluded."""
        canonical = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        return sha256(canonical.encode()).hexdigest()


def load_config(
    *,
    profile: Profile,
    suites: tuple[Suite, ...],
    env_file: Path | None = None,
    environment: Mapping[str, str] | None = None,
    formation_mode: Literal["write_free", "persistent"] = "write_free",
) -> EvalConfig:
    """Process env wins over the explicit file. No dotenv interpolation or ambient app config."""
    if profile == Profile.MOCK:
        provider = ProviderConfig(base_url="https://week5.invalid/v1", model="mock-model")
        return EvalConfig(
            profile=profile,
            suites=suites,
            formation_mode=formation_mode,
            extraction=provider,
            rewrite=provider,
            embedding=provider,
            database_url=SecretStr("postgresql://mock.invalid/eval"),
            memory_database_url=SecretStr("postgresql://mock.invalid/eval"),
            gateway_url="https://week5.invalid",
            worker_url="https://week5.invalid",
            kira_mock_url="https://week5.invalid",
        )
    values: dict[str, str | None] = {}
    if env_file is not None:
        if not env_file.is_file():
            raise ValueError("explicit evaluation env file is missing")
        values.update(dotenv_values(env_file, interpolate=False, encoding="utf-8"))
    values.update(os.environ if environment is None else environment)

    def get(name: str) -> str | None:
        value = values.get(name)
        return value.strip() if value and value.strip() else None

    def provider(kind: str, shared_model: str) -> ProviderConfig:
        # Internal runs never silently inherit OpenAI credentials or endpoints.
        external = profile == Profile.EXTERNAL_SYNTHETIC
        base = get(f"WEEK5_{kind}_BASE_URL")
        model = get(f"WEEK5_{kind}_MODEL")
        key = get(f"WEEK5_{kind}_API_KEY")
        # Shared credentials are used only with the shared endpoint, never a custom override.
        if external and not base:
            base = get("WEEK5_OPENAI_BASE_URL")
            model = model or get(shared_model)
            key = key or get("OPENAI_API_KEY")
        return ProviderConfig(
            base_url=base,
            model=model,
            api_key=SecretStr(key) if key else None,
            auth_required=external,
        )

    numeric: dict[str, int | float] = {}
    for suffix in ("CONNECT_TIMEOUT_SECONDS", "READ_TIMEOUT_SECONDS", "TOTAL_TIMEOUT_SECONDS"):
        if value := get(f"WEEK5_{suffix}"):
            numeric[suffix.lower()] = float(value)
    if value := get("WEEK5_EMBEDDING_DIMENSIONS"):
        numeric["embedding_dimensions"] = int(value)
    secrets = {
        name: SecretStr(value) if (value := get(f"WEEK5_{name.upper()}")) else None
        for name in ("database_url", "memory_database_url")
    }
    return EvalConfig(
        profile=profile,
        suites=suites,
        formation_mode=formation_mode,
        extraction=provider("EXTRACTION", "WEEK5_OPENAI_CHAT_MODEL"),
        rewrite=provider("REWRITE", "WEEK5_OPENAI_CHAT_MODEL"),
        embedding=provider("EMBEDDING", "WEEK5_OPENAI_EMBEDDING_MODEL"),
        extraction_json_mode=get("WEEK5_EXTRACTION_JSON_MODE") or "json_object",
        gateway_url=get("WEEK5_GATEWAY_URL"),
        worker_url=get("WEEK5_WORKER_URL"),
        kira_mock_url=get("WEEK5_KIRA_MOCK_URL"),
        memory_schema=get("WEEK5_MEMORY_SCHEMA") or "memory",
        memory_collection=get("WEEK5_MEMORY_COLLECTION") or "memories",
        **secrets,
        **numeric,
    )
