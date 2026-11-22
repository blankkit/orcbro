"""Request language and deferred messages shared by HTTP, background jobs and pushes."""
from __future__ import annotations

import re
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any

from aiohttp import web

from .i18n_de import DE

LANGUAGES = ("en", "de")
_language: ContextVar[str] = ContextVar("bridge_language", default="en")


def current_language() -> str:
    return _language.get()


def accept_language(header: str | None) -> str:
    """Pick the highest-priority supported language, defaulting to English."""
    choices = []
    for index, part in enumerate((header or "").split(",")):
        pieces = [p.strip() for p in part.split(";")]
        tag = pieces[0].lower().split("-", 1)[0]
        quality = 1.0
        try:
            for parameter in pieces[1:]:
                if parameter.startswith("q="):
                    quality = float(parameter[2:])
            if not 0 < quality <= 1:
                continue
        except ValueError:
            continue
        if tag in (*LANGUAGES, "*"):
            choices.append((quality, -index, "en" if tag == "*" else tag))
    return max(choices)[2] if choices else "en"


@contextmanager
def language_context(language: str):
    token = _language.set(language if language in LANGUAGES else "en")
    try:
        yield
    finally:
        _language.reset(token)


@web.middleware
async def language_middleware(request: web.Request, handler):
    with language_context(accept_language(request.headers.get("Accept-Language"))):
        response = await handler(request)
        response.headers["Content-Language"] = current_language()
        return response


class Message(str):
    """An English message ID and parameters; string operations and logs remain English.

    Render only at the serving boundary. Persist with `pack` to retain parameters across restarts.
    """
    def __new__(cls, english: str, **params):
        params = {key: message_of(value) if isinstance(value, Exception) else value for key, value in params.items()}
        params = {key: str(value) if isinstance(value, Path) else value for key, value in params.items()}
        value = english.format(**params) if params else english
        obj = super().__new__(cls, value)
        obj.english, obj.params = english, params
        return obj

    def __reduce__(self):
        return (_restore_message, (self.english, self.params))


def _restore_message(english: str, params: dict) -> Message:
    return Message(english, **params)


m = Message


def message_of(exc: Exception) -> str:
    """Keep a deferred exception message instead of flattening it with `str`."""
    return next((arg for arg in reversed(exc.args) if isinstance(arg, Message)), m(str(exc)))


class LocalizedText(str):
    """Language variants loaded from the printer vendor's external text tables."""
    def __new__(cls, english: str | None, german: str | None):
        obj = super().__new__(cls, english or "")
        obj.english, obj.german = english, german or english
        return obj

    def __bool__(self):
        return bool(self.english or self.german)

    def __reduce__(self):
        return (LocalizedText, (self.english, self.german))


class Excerpt(str):
    """A vendor message shortened only after its language has been selected."""
    def __new__(cls, source: str, limit: int, fallback: str | None = None):
        obj = super().__new__(cls, cls.shorten(str(source), limit) or str(fallback or ""))
        obj.source, obj.limit, obj.fallback = source, limit, fallback
        return obj

    def __bool__(self):
        return bool(self.source or self.fallback)

    @staticmethod
    def shorten(text: str, limit: int) -> str:
        sentence = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
        return sentence if len(sentence) <= limit else sentence[:limit - 1].rstrip() + "…"

    def __reduce__(self):
        return (Excerpt, (self.source, self.limit, self.fallback))


def render(value: Any, language: str | None = None) -> Any:
    language = language or current_language()
    if isinstance(value, Excerpt):
        return Excerpt.shorten(render(value.source, language) or "", value.limit) or render(value.fallback, language)
    if isinstance(value, Message):
        template = DE.get(value.english, value.english) if language == "de" else value.english
        params = {key: render(item, language) for key, item in value.params.items()}
        return template.format(**params) if params else template
    if isinstance(value, LocalizedText):
        return value.german if language == "de" else value.english
    if isinstance(value, dict):
        return {key: render(item, language) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [render(item, language) for item in value]
    return value


def t(english: str, **params) -> str:
    return render(m(english, **params))


def join_messages(separator: str, values) -> Message:
    params = {f"p{i}": value for i, value in enumerate(values)}
    return m(separator.join("{" + key + "}" for key in params), **params)


def pack(value: Any) -> Any:
    """Encode deferred messages for private job metadata, never for the JSON API."""
    if isinstance(value, Message):
        return {"$message": value.english, "params": pack(value.params)}
    if isinstance(value, dict):
        return {key: pack(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [pack(item) for item in value]
    return value


def unpack(value: Any) -> Any:
    if isinstance(value, dict):
        if set(value) == {"$message", "params"}:
            return m(value["$message"], **unpack(value["params"]))
        return {key: unpack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [unpack(item) for item in value]
    return value
