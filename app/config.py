from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    app_name: str = "new-niche-overall"
    debug: bool = False
    port: int = 1023

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
