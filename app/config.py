from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

APP_DIR = Path(__file__).resolve().parent


class Settings(BaseSettings):
    app_name: str = "new-niche-overall"
    debug: bool = False
    port: int = 8000

    # Admin panel: every /admin route requires this key (header X-API-Key, or login cookie).
    # Left empty, the admin panel stays locked.
    admin_api_key: str = ""
    # Where uploaded inputs are kept: <admin_dir>/inputs/tsv/__script_prompts.tsv
    admin_dir: Path = APP_DIR / "admin"
    # Where the processed config is written: <scripts_config_dir>/__scripts.prompt.json
    # Set SCRIPTS_CONFIG_DIR to share it (e.g. a network drive); the default stays in the app.
    scripts_config_dir: Path = APP_DIR / "admin" / "configs" / "_scripts"
    # Brand slug -> Google Drive folder URL. Private: kept out of git (see brand_links.example.json).
    brand_links_file: Path = APP_DIR / "brand_links.json"

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
