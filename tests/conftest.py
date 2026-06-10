import os

os.environ["DOCUMENT_SOURCE"] = "local"
os.environ["RETRIEVER_BACKEND"] = "tfidf"
os.environ["LLM_BACKEND"] = "stub"
os.environ["STDIO_ROLE"] = ""
os.environ["AUTH_TOKENS"] = (
    '{"test-junior-token": "JUNIOR_OP", '
    '"test-lead-token": "ATS_CORE_LEAD", '
    '"bad-role-token": "GOD_MODE"}'
)
