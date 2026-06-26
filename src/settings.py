import os
from functools import lru_cache
from typing import Dict, List, Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=os.path.join(BASE_DIR, ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
    )

    document_source: Literal["local", "confluence"] = "local"
    confluence_url: str = ""
    confluence_email: str = ""
    confluence_api_token: str = ""
    confluence_space_key: str = "ATSOPS"
    acl_label_roles: Dict[str, str] = {
        "acl-junior-op": "JUNIOR_OP",
        "acl-ats-core-lead": "ATS_CORE_LEAD",
    }

    retriever_backend: Literal["tfidf", "semantic"] = "tfidf"
    embedding_model: str = "sentence-transformers/all-MiniLM-L6-v2"
    chroma_dir: str = os.path.join(BASE_DIR, ".chroma")
    chunk_max_words: int = 220

    llm_backend: Literal["auto", "gemini", "ollama", "stub"] = "auto"
    gemini_api_key: str = ""
    gemini_models: List[str] = [
        "gemini-3.1-flash-lite",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
        "gemma-4-31b-it",
        "gemini-flash-latest",
    ]
    ollama_url: str = "http://localhost:11434"
    ollama_model: str = "llama3.2"

    mcp_transport: Literal["stdio", "streamable-http"] = "stdio"
    http_host: str = "0.0.0.0"
    http_port: int = 7860
    auth_tokens: Dict[str, str] = {}
    stdio_role: str = ""
    sync_interval_hours: float = 24.0

    sso_issuer: str = ""
    sso_audience: str = ""
    sso_jwks_url: str = ""
    sso_insecure_issuer: str = ""

    @property
    def sso_issuers(self) -> list[str]:
        return parse_list(self.sso_issuer)

    @property
    def sso_audiences(self) -> list[str]:
        return parse_list(self.sso_audience)

    @property
    def sso_jwks_urls(self) -> list[str]:
        return parse_list(self.sso_jwks_url)

    @property
    def sso_insecure_issuers(self) -> list[str]:
        return parse_list(self.sso_insecure_issuer)


def parse_list(val: str) -> list[str]:
    val = str(val).strip()
    if not val:
        return []
    if val.startswith('[') and val.endswith(']'):
        val = val[1:-1]
    return [p.strip().strip('"\'') for p in val.split(',') if p.strip()]


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
