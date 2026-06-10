import logging
from typing import List, Protocol

import httpx

from src.settings import Settings

logger = logging.getLogger("llm")

CONTEXT_BEGIN = "<<<CONTEXT>>>"
CONTEXT_END = "<<<END CONTEXT>>>"

NO_CONTEXT_RESPONSE = (
    "I'm sorry, I couldn't find any relevant operational documents in the database "
    "to answer your question."
)


class LLMError(Exception):
    pass


class LLMClient(Protocol):
    def generate(self, system_prompt: str, query: str) -> str: ...


class StubLLMClient:

    def generate(self, system_prompt: str, query: str) -> str:
        context = ""
        if CONTEXT_BEGIN in system_prompt:
            context = (
                system_prompt.split(CONTEXT_BEGIN, 1)[1].split(CONTEXT_END, 1)[0].strip()
            )
        if not context:
            return NO_CONTEXT_RESPONSE
        return f"Based on the retrieved operational documentation:\n\n{context}"


class GeminiClient:

    def __init__(self, settings: Settings) -> None:
        from google import genai

        if not settings.gemini_api_key:
            raise LLMError("Gemini backend requires GEMINI_API_KEY.")
        self.models: List[str] = list(settings.gemini_models)
        self.logger = logging.getLogger(self.__class__.__name__)
        self._client = genai.Client(api_key=settings.gemini_api_key)

    def generate(self, system_prompt: str, query: str) -> str:
        from google.genai import types

        last_error: Exception | None = None
        for model in self.models:
            try:
                response = self._client.models.generate_content(
                    model=model,
                    contents=query,
                    config=types.GenerateContentConfig(
                        system_instruction=system_prompt,
                        temperature=0.1,
                        max_output_tokens=1024,
                    ),
                )
                text = (response.text or "").strip()
                if not text:
                    raise LLMError(f"Model {model} returned an empty response.")
                self.logger.info("LLM generation succeeded.", extra={"model": model})
                return text
            except Exception as exc:
                last_error = exc
                self.logger.warning(
                    f"Model {model} failed, trying next tier.",
                    extra={"model": model, "error": str(exc)},
                )
        raise LLMError(f"All Gemini model tiers exhausted: {last_error}") from last_error


class OllamaClient:

    def __init__(self, settings: Settings) -> None:
        self.model = settings.ollama_model
        self.logger = logging.getLogger(self.__class__.__name__)
        self._client = httpx.Client(base_url=settings.ollama_url, timeout=120.0)

    def generate(self, system_prompt: str, query: str) -> str:
        try:
            response = self._client.post(
                "/api/chat",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": query},
                    ],
                    "stream": False,
                    "options": {"temperature": 0.1},
                },
            )
            response.raise_for_status()
            text = response.json().get("message", {}).get("content", "").strip()
            if not text:
                raise LLMError(f"Ollama model {self.model} returned an empty response.")
            self.logger.info("LLM generation succeeded.", extra={"model": self.model})
            return text
        except httpx.HTTPError as exc:
            raise LLMError(f"Ollama request failed: {exc}") from exc


def build_llm_client(settings: Settings) -> LLMClient:
    backend = settings.llm_backend
    if backend == "auto":
        backend = "gemini" if settings.gemini_api_key else "stub"

    if backend == "gemini":
        logger.info("LLM backend: Gemini.", extra={"models": settings.gemini_models})
        return GeminiClient(settings)
    if backend == "ollama":
        logger.info(
            "LLM backend: Ollama (air-gapped).",
            extra={"url": settings.ollama_url, "model": settings.ollama_model},
        )
        return OllamaClient(settings)
    logger.info("LLM backend: deterministic stub (tests / offline CI).")
    return StubLLMClient()
