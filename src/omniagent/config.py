from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _get_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "on"}


def _split_csv(raw: str | None) -> list[str]:
    if not raw:
        return []
    return [item.strip() for item in raw.split(",") if item.strip()]


def _parse_path_intents(raw: str | None) -> dict[str, str]:
    entries: dict[str, str] = {}
    for item in _split_csv(raw):
        if "=" not in item:
            continue
        key, value = item.split("=", 1)
        normalized_key = key.strip()
        normalized_value = value.strip()
        if not normalized_key or not normalized_value:
            continue
        entries[normalized_key] = normalized_value
    return entries


def _load_config_file(project_root: Path) -> dict[str, Any]:
    """Load omniagent.toml if it exists. Returns empty dict if not found."""
    for name in ("omniagent.toml", ".omniagent.toml"):
        path = project_root / name
        if path.is_file():
            with open(path, "rb") as f:
                return tomllib.load(f)
    return {}


def _get(env_name: str, file_value: Any, default: Any) -> Any:
    """Resolve config value: env var > config file > default."""
    env_val = os.getenv(env_name)
    if env_val is not None:
        return env_val
    if file_value is not None:
        return file_value
    return default


def _get_bool_merged(env_name: str, file_value: Any, default: bool) -> bool:
    env_val = os.getenv(env_name)
    if env_val is not None:
        return env_val.lower() in {"1", "true", "yes", "on"}
    if file_value is not None:
        return bool(file_value)
    return default


def _get_optional_int(env_name: str, file_value: Any) -> int | None:
    raw = _get(env_name, file_value, None)
    if raw in {None, ""}:
        return None
    return int(raw)


def _get_optional_path(
    env_name: str,
    file_value: Any,
    *,
    base_dir: Path | None = None,
) -> Path | None:
    env_value = os.getenv(env_name)
    raw = env_value if env_value is not None else file_value
    if raw in {None, ""}:
        return None
    path = Path(str(raw)).expanduser()
    if env_value is None and base_dir is not None and not path.is_absolute():
        return base_dir / path
    return path


@dataclass(slots=True)
class LLMConfig:
    provider: str = "openai"
    model: str = ""
    api_key: str | None = None
    api_base: str | None = None
    openrouter_free_only: bool = True
    openrouter_site_url: str = ""
    openrouter_app_title: str = "OmniAgent"
    opencode_free_only: bool = True
    opencode_allow_anonymous: bool = False
    timeout_seconds: int = 120
    max_retries: int = 2
    temperature: float = 0.0
    prompt_dir: Path | None = None
    fallback_provider: str = ""
    enable_runtime_fallback: bool = True
    verify_on_startup: bool = False


@dataclass(slots=True)
class GatewayConfig:
    app_name: str = "omniagent"
    default_mode: str = "semi_auto"
    task_timeout_seconds: int = 30


@dataclass(slots=True)
class SQLiteConfig:
    path: Path


@dataclass(slots=True)
class PlaywrightConfig:
    enabled: bool = False
    browser: str = "chromium"


@dataclass(slots=True)
class EmbeddingConfig:
    enabled: bool = False
    provider: str = "local_hash"
    model: str = ""
    model_path: Path | None = None
    tokenizer_path: Path | None = None
    api_key: str | None = None
    api_base: str | None = None
    dimensions: int | None = None
    pooling: str = "mean"
    normalize: bool = True
    max_tokens: int = 256
    timeout_seconds: int = 30
    max_retries: int = 1


@dataclass(slots=True)
class SandboxConfig:
    enabled: bool = True
    timeout_seconds: int = 30
    allow_network: bool = False
    authorized_paths: list[Path] = field(default_factory=list)
    path_intents: dict[str, str] = field(default_factory=dict)


@dataclass(slots=True)
class LoggingConfig:
    level: str = "INFO"
    json_logs: bool = True
    file_path: Path | None = None


@dataclass(slots=True)
class AppConfig:
    llm: LLMConfig
    gateway: GatewayConfig
    sqlite: SQLiteConfig
    playwright: PlaywrightConfig
    embedding: EmbeddingConfig
    sandbox: SandboxConfig
    logging: LoggingConfig

    @classmethod
    def from_env(cls, project_root: Path) -> "AppConfig":
        file_cfg = _load_config_file(project_root)
        llm_cfg = file_cfg.get("llm", {})
        gw_cfg = file_cfg.get("gateway", {})
        pw_cfg = file_cfg.get("playwright", {})
        emb_cfg = file_cfg.get("embedding", {})
        sb_cfg = file_cfg.get("sandbox", {})
        log_cfg = file_cfg.get("logging", {})

        # -- LLM --
        provider = str(
            _get("OMNIAGENT_LLM_PROVIDER", llm_cfg.get("provider"), "openai")
        ).strip()
        provider_name = provider.lower()
        if provider_name == "openrouter":
            provider_api_key = os.getenv("OPENROUTER_API_KEY")
        elif provider_name == "opencode":
            provider_api_key = os.getenv("OPENCODE_API_KEY")
        elif provider_name == "ark":
            provider_api_key = os.getenv("ARK_API_KEY")
        else:
            provider_api_key = os.getenv("OPENAI_API_KEY") or os.getenv("ARK_API_KEY")
        api_key = (
            os.getenv("OMNIAGENT_LLM_API_KEY")
            or provider_api_key
            or llm_cfg.get("api_key")
            or None
        )
        if api_key == "":
            api_key = None

        if provider_name == "openrouter":
            provider_api_base = os.getenv("OPENROUTER_API_BASE")
        elif provider_name == "opencode":
            provider_api_base = os.getenv("OPENCODE_API_BASE")
        else:
            provider_api_base = os.getenv("ARK_BASE_URL") or os.getenv("ARK_API_BASE")
        api_base = (
            os.getenv("OMNIAGENT_LLM_API_BASE")
            or provider_api_base
            or llm_cfg.get("api_base")
            or (
                "https://openrouter.ai/api/v1"
                if provider_name == "openrouter"
                else (
                    "https://opencode.ai/zen/v1"
                    if provider_name == "opencode"
                    else None
                )
            )
            or None
        )
        if api_base == "":
            api_base = None

        if provider_name == "openrouter":
            default_model = "openrouter/free"
        elif provider_name == "opencode":
            default_model = "deepseek-v4-flash-free"
        else:
            default_model = ""
        model = str(_get("OMNIAGENT_LLM_MODEL", llm_cfg.get("model"), default_model)).strip()
        if not model:
            model = default_model
        openrouter_site_url = str(
            os.getenv("OMNIAGENT_OPENROUTER_SITE_URL")
            or os.getenv("OPENROUTER_SITE_URL")
            or llm_cfg.get("openrouter_site_url")
            or ""
        ).strip()
        openrouter_app_title = str(
            os.getenv("OMNIAGENT_OPENROUTER_APP_TITLE")
            or os.getenv("OPENROUTER_APP_TITLE")
            or llm_cfg.get("openrouter_app_title")
            or "OmniAgent"
        ).strip()

        embedding_api_key = (
            os.getenv("OMNIAGENT_EMBEDDING_API_KEY") or emb_cfg.get("api_key") or None
        )
        if embedding_api_key == "":
            embedding_api_key = None

        embedding_api_base = (
            os.getenv("OMNIAGENT_EMBEDDING_API_BASE") or emb_cfg.get("api_base") or None
        )
        if embedding_api_base == "":
            embedding_api_base = None

        prompt_dir_raw = os.getenv("OMNIAGENT_PROMPT_DIR")
        if prompt_dir_raw:
            prompt_dir = Path(prompt_dir_raw)
        else:
            prompt_dir = Path(__file__).resolve().parent / "prompts"

        # -- SQLite --
        sqlite_path = Path(
            _get("OMNIAGENT_SQLITE_PATH", None, project_root / "data" / "omniagent.sqlite3")
        )

        # -- Logging --
        log_path_raw = os.getenv("OMNIAGENT_LOG_PATH") or log_cfg.get("file_path")
        log_path = (
            Path(log_path_raw) if log_path_raw else project_root / "artifacts" / "omniagent.log"
        )

        # -- Sandbox authorized_paths --
        env_auth_paths = os.getenv("OMNIAGENT_SANDBOX_AUTHORIZED_PATHS")
        if env_auth_paths:
            authorized_paths = [
                Path(item).expanduser().resolve() for item in _split_csv(env_auth_paths)
            ]
        elif sb_cfg.get("authorized_paths"):
            authorized_paths = [
                Path(item).expanduser().resolve() for item in sb_cfg["authorized_paths"]
            ]
        else:
            authorized_paths = []

        # -- Sandbox path_intents --
        env_path_intents = os.getenv("OMNIAGENT_SANDBOX_PATH_INTENTS")
        if env_path_intents:
            path_intents = _parse_path_intents(env_path_intents)
        elif sb_cfg.get("path_intents"):
            path_intents = dict(sb_cfg["path_intents"])
        else:
            path_intents = {}

        return cls(
            llm=LLMConfig(
                provider=provider,
                model=model,
                api_key=api_key,
                api_base=api_base,
                openrouter_free_only=_get_bool_merged(
                    "OMNIAGENT_OPENROUTER_FREE_ONLY",
                    llm_cfg.get("openrouter_free_only"),
                    True,
                ),
                openrouter_site_url=openrouter_site_url,
                openrouter_app_title=openrouter_app_title,
                opencode_free_only=_get_bool_merged(
                    "OMNIAGENT_OPENCODE_FREE_ONLY",
                    llm_cfg.get("opencode_free_only"),
                    True,
                ),
                opencode_allow_anonymous=_get_bool_merged(
                    "OMNIAGENT_OPENCODE_ALLOW_ANONYMOUS",
                    llm_cfg.get("opencode_allow_anonymous"),
                    False,
                ),
                timeout_seconds=int(
                    _get("OMNIAGENT_LLM_TIMEOUT", llm_cfg.get("timeout_seconds"), 120)
                ),
                max_retries=int(_get("OMNIAGENT_LLM_MAX_RETRIES", llm_cfg.get("max_retries"), 2)),
                temperature=float(_get("OMNIAGENT_LLM_TEMPERATURE", llm_cfg.get("temperature"), 0)),
                prompt_dir=prompt_dir,
                fallback_provider=str(
                    _get("OMNIAGENT_LLM_FALLBACK_PROVIDER", llm_cfg.get("fallback_provider"), "")
                ),
                enable_runtime_fallback=_get_bool_merged(
                    "OMNIAGENT_LLM_ENABLE_RUNTIME_FALLBACK",
                    llm_cfg.get("enable_runtime_fallback"),
                    True,
                ),
                verify_on_startup=_get_bool_merged(
                    "OMNIAGENT_LLM_VERIFY_ON_STARTUP",
                    llm_cfg.get("verify_on_startup"),
                    False,
                ),
            ),
            gateway=GatewayConfig(
                app_name=str(_get("OMNIAGENT_APP_NAME", gw_cfg.get("app_name"), "omniagent")),
                default_mode=str(
                    _get("OMNIAGENT_DEFAULT_MODE", gw_cfg.get("default_mode"), "semi_auto")
                ),
                task_timeout_seconds=int(
                    _get("OMNIAGENT_TASK_TIMEOUT", gw_cfg.get("task_timeout_seconds"), 30)
                ),
            ),
            sqlite=SQLiteConfig(path=sqlite_path),
            playwright=PlaywrightConfig(
                enabled=_get_bool_merged(
                    "OMNIAGENT_PLAYWRIGHT_ENABLED", pw_cfg.get("enabled"), False
                ),
                browser=str(
                    _get("OMNIAGENT_PLAYWRIGHT_BROWSER", pw_cfg.get("browser"), "chromium")
                ),
            ),
            embedding=EmbeddingConfig(
                enabled=_get_bool_merged(
                    "OMNIAGENT_EMBEDDING_ENABLED", emb_cfg.get("enabled"), False
                ),
                provider=str(
                    _get(
                        "OMNIAGENT_EMBEDDING_PROVIDER",
                        emb_cfg.get("provider"),
                        "local_hash",
                    )
                ),
                model=str(_get("OMNIAGENT_EMBEDDING_MODEL", emb_cfg.get("model"), "")),
                model_path=_get_optional_path(
                    "OMNIAGENT_EMBEDDING_MODEL_PATH",
                    emb_cfg.get("model_path"),
                    base_dir=project_root,
                ),
                tokenizer_path=_get_optional_path(
                    "OMNIAGENT_EMBEDDING_TOKENIZER_PATH",
                    emb_cfg.get("tokenizer_path"),
                    base_dir=project_root,
                ),
                api_key=embedding_api_key,
                api_base=embedding_api_base,
                dimensions=_get_optional_int(
                    "OMNIAGENT_EMBEDDING_DIMENSIONS",
                    emb_cfg.get("dimensions"),
                ),
                pooling=str(_get("OMNIAGENT_EMBEDDING_POOLING", emb_cfg.get("pooling"), "mean")),
                normalize=_get_bool_merged(
                    "OMNIAGENT_EMBEDDING_NORMALIZE",
                    emb_cfg.get("normalize"),
                    True,
                ),
                max_tokens=int(
                    _get("OMNIAGENT_EMBEDDING_MAX_TOKENS", emb_cfg.get("max_tokens"), 256)
                ),
                timeout_seconds=int(
                    _get("OMNIAGENT_EMBEDDING_TIMEOUT", emb_cfg.get("timeout_seconds"), 30)
                ),
                max_retries=int(
                    _get("OMNIAGENT_EMBEDDING_MAX_RETRIES", emb_cfg.get("max_retries"), 1)
                ),
            ),
            sandbox=SandboxConfig(
                enabled=_get_bool_merged("OMNIAGENT_SANDBOX_ENABLED", sb_cfg.get("enabled"), True),
                timeout_seconds=int(
                    _get("OMNIAGENT_SANDBOX_TIMEOUT", sb_cfg.get("timeout_seconds"), 30)
                ),
                allow_network=_get_bool_merged(
                    "OMNIAGENT_SANDBOX_ALLOW_NETWORK", sb_cfg.get("allow_network"), False
                ),
                authorized_paths=authorized_paths,
                path_intents=path_intents,
            ),
            logging=LoggingConfig(
                level=str(_get("OMNIAGENT_LOG_LEVEL", log_cfg.get("level"), "INFO")),
                json_logs=_get_bool_merged("OMNIAGENT_JSON_LOGS", log_cfg.get("json_logs"), True),
                file_path=log_path,
            ),
        )
