"""Configuración del entorno (.env) y carga validada de council.yaml y sus prompts."""

import os
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field, SecretStr

from council.schemas import CouncilConfig

ROOT = Path(__file__).resolve().parent.parent
PROMPTS_DIR = ROOT / "prompts"


class Settings(BaseModel):
    """Variables de entorno del servidor; todas tienen default salvo los secretos."""

    openrouter_api_key: SecretStr | None = None
    openrouter_base_url: str = "https://openrouter.ai/api/v1"
    council_token: SecretStr | None = None
    council_host: str = "127.0.0.1"
    council_port: int = 8765
    council_config_path: Path = ROOT / "council.yaml"
    council_db_path: Path = ROOT / "data" / "council.db"
    council_max_concurrent: int = 4
    council_daily_budget_usd: float = Field(default=1.0, gt=0)
    council_max_sessions: int = 200
    council_session_ttl_days: int = 30
    council_member_timeout_s: float = 180
    council_model_timeout_s: float = 120
    council_summary_trigger_tokens: int = 6000
    council_max_idea_chars: int = 8000
    council_max_context_chars: int = 30000


def _read_env_file(path: Path) -> dict[str, str]:
    """Lee pares CLAVE=valor de un .env, ignorando comentarios y líneas vacías."""
    if not path.is_file():
        return {}
    values = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip("'\"")
    return values


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración leída una sola vez; el entorno real tiene prioridad sobre .env."""
    # Se vuelca .env al entorno para que lo vean también las librerías (p. ej. LANGSMITH_*).
    for key, value in _read_env_file(ROOT / ".env").items():
        os.environ.setdefault(key, value)
    fields = Settings.model_fields
    return Settings.model_validate({k.lower(): v for k, v in os.environ.items() if k.lower() in fields})


def read_prompt(relative_path: str) -> str:
    """Lee un prompt .md comprobando que esté dentro de prompts/."""
    path = (ROOT / relative_path).resolve()
    if not path.is_relative_to(PROMPTS_DIR) or path.suffix != ".md":
        raise ValueError(f"El prompt debe ser un .md dentro de prompts/: {relative_path}")
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        raise ValueError(f"El prompt está vacío: {relative_path}")
    return text


def load_council_config(path: Path) -> CouncilConfig:
    """Carga council.yaml, lo valida y comprueba que todos sus prompts existan."""
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = CouncilConfig.model_validate(data)
    for prompt in [config.member_base_prompt, config.supervisor.prompt, *(m.prompt for m in config.members)]:
        read_prompt(prompt)
    return config
