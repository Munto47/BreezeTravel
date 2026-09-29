"""Provider-specific wire options shared by all generative model clients."""
from __future__ import annotations

from urllib.parse import urlparse

from openai import AsyncOpenAI as OpenAIClient


def is_kimi_code(base_url: object) -> bool:
    url = urlparse(str(base_url))
    return url.hostname in {"api.kimi.com", "api.kimi.ai"} and url.path.startswith("/coding")


def kimi_options(options: dict) -> dict:
    extra = dict(options.get("extra_body") or {})
    extra.pop("enable_thinking", None)
    extra["thinking"] = {"type": "disabled"}
    return {**options, "temperature": 0.6, "extra_body": extra}


def AsyncOpenAI(**kwargs):
    client = OpenAIClient(**kwargs)
    if is_kimi_code(kwargs.get("base_url", "")):
        create = client.chat.completions.create

        async def create_kimi(**options):
            return await create(**kimi_options(options))

        client.chat.completions.create = create_kimi
    return client


def chat_model(**kwargs):
    from langchain_openai import ChatOpenAI
    from app.config import get_settings

    settings = get_settings()
    if settings.kimi_for_code:
        kwargs.update(model=settings.kimi_model, api_key=settings.kimi_for_code,
                      base_url=settings.kimi_api_url)
        kwargs = kimi_options(kwargs)
    return ChatOpenAI(**kwargs)
