"""Readable printer messages: `print_error` (display errors) and HMS (component health).

Bambu Lab supplies the texts; ha-bambulab (MIT) collects them in `hms_<language>.json.gz`. The
image downloads pinned English and German tables (Dockerfile). Table structure:

    {"device_error": {"0300400C": {"<text>": ["<model>", …]}}, "device_hms": {"0300010000010001": {…}}}

An empty model list applies to every printer; otherwise a text applies only to listed models.
Select the requested language, falling back to English. Without tables, only codes are available.

Empty text means internal: Bambu Studio and OrcaSlicer hide these print errors
(`HMSQuery::is_internal_error`); ha-bambulab skips internal HMS messages. The bridge marks them
`internal` so the app can hide them."""
from __future__ import annotations

from .i18n import m, current_language, LocalizedText

import gzip
import json
import logging
from pathlib import Path

from .state import HMS_COMMAND_REJECTED

log = logging.getLogger(__name__)

# Bambu's text recommends updating Studio; this command rejection has a different meaning for the bridge.
OWN_TEXTS = {HMS_COMMAND_REJECTED: m('The printer rejected a command because it only accepts signed commands: Developer Mode is off. Enable it on the printer: Settings → WLAN → LAN Only mode → Developer Mode.')}

LANGUAGES = ("de", "en")

# Product name from `get_version` → text-table model abbreviation.
MODELS = {
    "Bambu Lab P1S": "P1S", "Bambu Lab P1P": "P1P", "Bambu Lab P2S": "P2S",
    "Bambu Lab X1 Carbon": "X1C", "Bambu Lab X1E": "X1E",
    "Bambu Lab A1": "A1", "Bambu Lab A1 mini": "A1MINI",
    "Bambu Lab H2D": "H2D", "Bambu Lab H2S": "H2S",
}


class Texts:
    def __init__(self, directory: Path | None):
        self._tables: dict[str, dict] = {}
        for language in LANGUAGES:
            path = directory / f"hms_{language}.json.gz" if directory else None
            if path is None or not path.exists():
                continue
            try:
                with gzip.open(path, "rt", encoding="utf-8") as handle:
                    self._tables[language] = json.load(handle)
            except (OSError, ValueError) as exc:
                log.warning(m('Cannot read error texts %s: %s'), path, exc)

    @property
    def available(self) -> bool:
        return bool(self._tables)

    def error(self, code: str, model: str | None, language: str | None = None) -> str | None:
        """`print_error` text, with or without underscores (`0300_400C`). Empty = internal; None = unknown."""
        return self._lookup("device_error", code, model, language)

    def hms(self, code: str, model: str | None, language: str | None = None) -> str | None:
        """HMS text (`0700_2000_0002_0001`). Empty = internal; None = unknown."""
        return self._lookup("device_hms", code, model, language)

    def _lookup(self, kind: str, code: str, model: str | None, language: str | None = None) -> str | None:
        """Prefer model-specific text over general text, in the requested language with English fallback.
        Specific text beats an empty general entry (0500_C011 is internal except on models such as P1S)."""
        key = code.replace("_", "").upper()
        short = MODELS.get(model or "")
        internal = False
        language = language or current_language()
        for lang in (("de", "en") if language == "de" else ("en",)):
            table = self._tables.get(lang, {})
            entry = table.get(kind, {}).get(key)
            if not isinstance(entry, dict):
                continue
            general = None
            for text, models in entry.items():
                if short and short in (models or []) and text.strip():
                    return _clean(text)
                if not models and general is None:
                    general = text
            if general and general.strip():
                return _clean(general)
            internal = internal or general is not None
        return "" if internal else None

    @staticmethod
    def _variants(lookup, code: str, model: str | None):
        english, german = lookup(code, model, "en"), lookup(code, model, "de")
        return LocalizedText(english, german) if english or german else ("" if english == "" or german == "" else None)

    def describe(self, status: dict) -> None:
        """Add deferred message texts and `internal` flags to status in place."""
        model = status.get("printer", {}).get("model")
        error = status.get("job", {}).get("error")
        if error:
            text = self._variants(self.error, error["code"], model)
            error["text"], error["internal"] = text or None, not isinstance(text, LocalizedText) and text == ""
        for entry in status.get("hms") or []:
            text = OWN_TEXTS.get(entry["code"]) or self._variants(self.hms, entry["code"], model)
            entry["text"], entry["internal"] = text or None, not isinstance(text, LocalizedText) and text == ""


def _clean(text: str) -> str:
    return " ".join(text.split())
