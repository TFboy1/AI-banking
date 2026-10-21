from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", env_file_encoding="utf-8", extra="ignore")

    app_auth_enabled: bool = True
    multi_user_accounts: bool = True
    seed_demo_data: bool = True
    scheduled_worker_enabled: bool = True
    daily_report_worker_enabled: bool = True
    birthday_worker_enabled: bool = True
    speech_model_dir: Path = ROOT / 'data' / 'speech'
    face_model_dir: Path = ROOT / 'data' / 'face'
    face_enabled: bool = True
    database_path: Path = ROOT / "data" / "banking.sqlite3"
    audit_witness_path:Path|None=None
    audit_witness_enabled:bool=True
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
    feishu_public_url: str = ""
    backend_url: str = "http://127.0.0.1:8000"
    travel_url: str = "http://127.0.0.1:8004"
    travel_bridge_key_path: Path = ROOT / "data" / ".travel-bridge-key"
    food_url: str = "http://127.0.0.1:8002"
    food_bridge_key_path: Path = ROOT / "data" / ".food-bridge-key"
    securities_url: str = "http://127.0.0.1:8001"
    securities_bridge_key_path: Path = ROOT / "data" / ".securities-bridge-key"

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
