# -*- coding: utf-8 -*-
"""i18n.py: Lightweight bilingual (English default / Japanese) localization helper."""
import os
from typing import Dict, Optional, Tuple

_CURRENT_LANG = "ja" if os.environ.get("ED_NAV_LANG", "en").strip().lower().startswith("ja") else "en"

# Optional key-based registry: key -> (en_text, ja_text)
TRANSLATIONS: Dict[str, Tuple[str, str]] = {}


def get_lang() -> str:
    """Return active UI language ('en' or 'ja')."""
    return _CURRENT_LANG


def set_lang(lang: str) -> str:
    """Set active UI language ('en' or 'ja'). Defaults to 'en'."""
    global _CURRENT_LANG
    val = (lang or "en").strip().lower()
    _CURRENT_LANG = "ja" if val.startswith("ja") else "en"
    return _CURRENT_LANG


def register_translations(mapping: Dict[str, Tuple[str, str]]) -> None:
    """Register additional key -> (en, ja) pairs."""
    TRANSLATIONS.update(mapping)


def tr(en_text: str, ja_text: Optional[str] = None, **kwargs) -> str:
    """Return localized string for active language (English by default)."""
    if _CURRENT_LANG == "ja" and ja_text is not None:
        tpl = ja_text
    else:
        tpl = en_text
    return tpl.format(**kwargs) if kwargs else tpl


def t(key: str, default: Optional[str] = None, **kwargs) -> str:
    """Lookup translation by key, falling back to default or key."""
    pair = TRANSLATIONS.get(key)
    if pair is not None:
        tpl = pair[1] if _CURRENT_LANG == "ja" else pair[0]
    else:
        tpl = default if default is not None else key
    return tpl.format(**kwargs) if kwargs else tpl
