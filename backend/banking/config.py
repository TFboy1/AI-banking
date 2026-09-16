from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    database_path: Path = ROOT / "data" / "banking.sqlite3"
    llm_base_url: str = ""
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 60
    llm_max_steps: int = 12
    llm_tool_mode: Literal["auto", "native", "structured"] = "auto"
    feishu_enabled: bool = False
    feishu_app_id: str = ""
    feishu_app_secret: str = ""
    feishu_bridge_token: str = ""
    backend_url: str = "http://127.0.0.1:8000"

    @property
    def model_ready(self) -> bool:
        return bool(self.llm_base_url and self.llm_api_key and self.llm_model)

    @property
    def feishu_ready(self) -> bool:
        return bool(
            self.feishu_enabled
            and self.feishu_app_id
            and self.feishu_app_secret
            and len(self.feishu_bridge_token) >= 32
        )
