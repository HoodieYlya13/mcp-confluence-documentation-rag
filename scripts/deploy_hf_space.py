import io
import logging
import os
import sys

from huggingface_hub import HfApi

logging.basicConfig(level=logging.INFO, format="%(levelname)s  %(message)s")
logger = logging.getLogger("deploy_hf_space")

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SPACE_NAME = "mcp-confluence-documentation-rag"

SECRET_KEYS = [
    "CONFLUENCE_URL",
    "CONFLUENCE_EMAIL",
    "CONFLUENCE_API_TOKEN",
    "GEMINI_API_KEY",
    "AUTH_TOKENS",
]

SPACE_VARIABLES = {
    "DOCUMENT_SOURCE": "confluence",
    "CONFLUENCE_SPACE_KEY": "ATSOPS",
    "RETRIEVER_BACKEND": "semantic",
    "LLM_BACKEND": "gemini",
    "MCP_TRANSPORT": "streamable-http",
    "HTTP_PORT": "7860",
}

UPLOAD_PATTERNS = [
    "Dockerfile",
    "requirements.txt",
    "requirements-semantic.txt",
    "src/**",
    "mock_cern_confluence/**",
]

SPACE_README_TEMPLATE = """---
title: MCP Confluence Documentation RAG
emoji: \U0001F300
colorFrom: blue
colorTo: indigo
sdk: docker
app_port: 7860
pinned: false
---

# MCP Accelerator Operations Substrate (Live)

A secure, RBAC-enforced Model Context Protocol (MCP) server exposing a
retrieval-augmented knowledge base built from a live Atlassian Confluence
space. Built as a CERN BE-CSS Applied AI portfolio project.

- **Transport:** MCP streamable HTTP at `/mcp`
- **Auth:** Bearer token (server-side token-to-role mapping, mirrors an
  OIDC identity claim flow)
- **Retrieval:** LlamaIndex + ChromaDB with ACL filters pushed into the
  vector query; local sentence-transformers embeddings (no embedding API)
- **Health:** `GET /health` (public)

Source, architecture documentation and evaluation harness:
https://github.com/{github_user}/mcp-confluence-documentation-rag
"""


def load_env_file(path: str) -> None:
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def main() -> int:
    load_env_file(os.path.join(BASE_DIR, ".env"))

    hf_token = os.environ.get("HF_TOKEN", "")
    if not hf_token:
        logger.error("HF_TOKEN must be set.")
        return 1

    missing = [key for key in SECRET_KEYS if not os.environ.get(key)]
    if missing:
        logger.error(f"Missing required secrets in environment: {missing}")
        return 1

    api = HfApi(token=hf_token)
    username = api.whoami()["name"]
    repo_id = f"{username}/{SPACE_NAME}"

    api.create_repo(
        repo_id=repo_id,
        repo_type="space",
        space_sdk="docker",
        private=False,
        exist_ok=True,
    )
    logger.info(f"Space ready: https://huggingface.co/spaces/{repo_id}")

    for key in SECRET_KEYS:
        api.add_space_secret(repo_id=repo_id, key=key, value=os.environ[key])
        logger.info(f"Secret set: {key}")

    for key, value in SPACE_VARIABLES.items():
        api.add_space_variable(repo_id=repo_id, key=key, value=value)
        logger.info(f"Variable set: {key}={value}")

    readme = SPACE_README_TEMPLATE.format(github_user="HoodieYlya13")
    api.upload_file(
        path_or_fileobj=io.BytesIO(readme.encode("utf-8")),
        path_in_repo="README.md",
        repo_id=repo_id,
        repo_type="space",
        commit_message="Update Space README",
    )

    api.upload_folder(
        folder_path=BASE_DIR,
        repo_id=repo_id,
        repo_type="space",
        allow_patterns=UPLOAD_PATTERNS,
        commit_message="Deploy MCP server",
    )
    logger.info("Code uploaded. Space build will start automatically.")
    logger.info(f"Health URL (after build): https://{username}-{SPACE_NAME.replace('_','-')}.hf.space/health")
    return 0


if __name__ == "__main__":
    sys.exit(main())
