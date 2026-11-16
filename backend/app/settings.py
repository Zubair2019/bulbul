from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(BACKEND_DIR.parent / ".env", BACKEND_DIR / ".env"),
        extra="ignore",
    )

    # Empty DATABASE_URL -> in-memory storage (handy for quick local runs and tests).
    database_url: str = ""
    app_version: str = "0.1.0"  # also the Langfuse release/version on traces
    dev_user_id: str = "dev-user"
    cors_origins: str = "http://localhost:5173"

    models_config: Path = BACKEND_DIR / "config" / "models.yaml"
    agents_dir: Path = BACKEND_DIR / "app" / "agents" / "defs"
    persona_file: Path = BACKEND_DIR / "config" / "persona.md"  # Bulbul's character and voice (editable)

    max_supervisor_steps: int = 12

    # Where the browser reaches Bulbul (OAuth redirects come back here): 5173 dev, 8080 Docker.
    public_url: str = "http://localhost:5173"
    # Fernet key that encrypts integration tokens at rest. Generate:
    #   uv run python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
    bulbul_secret_key: str = ""
    # Swiggy MCP base URL. Empty = Swiggy off. Local stub: http://127.0.0.1:8765 (uv run python -m stubs.swiggy_instamart)
    swiggy_base_url: str = ""
    # Live voice (voicecat, mounted at /api/voice). Voice sessions call this API over loopback so
    # replies stream; empty = http://127.0.0.1:$PORT (this server).
    voice_enabled: bool = True
    voice_bulbul_url: str = ""

    langfuse_public_key: str = ""
    langfuse_secret_key: str = ""
    langfuse_base_url: str = ""  # empty -> SDK default (EU cloud); e.g. https://us.cloud.langfuse.com
    langfuse_tracing_environment: str = "development"  # development | docker | production ...


@lru_cache
def get_settings() -> Settings:
    return Settings()
