"""Environment-based configuration. Secrets come from the environment / .env only."""
from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str
    test_database_url: str | None = None
    jwt_secret: str
    environment: str = "development"  # development | demo | production

    access_token_minutes: int = 480
    max_failed_logins: int = 5
    lockout_minutes: int = 15
    login_rate_limit_per_minute: int = 20
    min_password_length: int = 10

    default_currency: str = "PKR"
    media_dir: str = "data/media"  # product images; included in backups

    @field_validator("jwt_secret")
    @classmethod
    def _secret_strength(cls, v: str) -> str:
        if len(v) < 32 or v.startswith("CHANGE_ME"):
            raise ValueError("JWT_SECRET must be a random string of at least 32 characters")
        return v

    @property
    def is_production(self) -> bool:
        return self.environment == "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]
