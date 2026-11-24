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
    # The private brands file: brand profiles (shown or hidden), their Drive folders, the TSV
    # brands they belong to, tool texts and the system prompt. Default: ___brands.json in the
    # folder above SCRIPTS_CONFIG_DIR (the admin configs folder). Never in git; start from
    # app/brands.example.json. See app/brands_file.py.
    brands_file: Path | None = None

    # New-niche research (app/research): how many runs may have a browser open at once
    # (more wait their turn), and whether the browsers stay hidden.
    research_max_browsers: int = 4
    research_headless: bool = True

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
