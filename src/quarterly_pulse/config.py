"""Environment-driven configuration via pydantic-settings (V2 syntax)."""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Configuration loaded from the process environment and an optional ``.env`` file.

    Only the LLM settings are required at runtime; every connector token is
    optional and drives that connector's fallback chain.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- LLM synthesis -----------------------------------------------------
    llm_model: str = "openai:gpt-4o"
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None

    # --- GitHub ------------------------------------------------------------
    github_token: str | None = None

    # --- LinkedIn fallback chain --------------------------------------------
    linkedin_access_token: str | None = None
    apify_api_token: str | None = None
    apify_linkedin_actor: str = "apimaestro/linkedin-profile-posts"
    apify_linkedin_input: str | None = None
    scrapecreators_api_key: str | None = None
    exa_api_key: str | None = None
