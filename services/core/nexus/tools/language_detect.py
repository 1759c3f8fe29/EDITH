"""Language detection for auto-detect input/output language (spec §12, §37).

Uses the STT model's own language detection when available, falls back to
fastText or a lightweight heuristic. The goal: the user never has to pick a
language -- NEXUS hears what is spoken and replies in the same language.
"""

from __future__ import annotations

import asyncio
import re
from pathlib import Path
from typing import Any

from ..logging_setup import get_logger
from .registry import ToolResult, tool

log = get_logger("tools.language_detect")

# Language code mapping: BCP-47 -> common name
LANGUAGE_NAMES: dict[str, str] = {
    "en": "English",
    "en-US": "English (US)",
    "en-GB": "English (UK)",
    "hi": "Hindi",
    "hi-IN": "Hindi (India)",
    "es": "Spanish",
    "es-ES": "Spanish (Spain)",
    "es-MX": "Spanish (Mexico)",
    "fr": "French",
    "fr-FR": "French (France)",
    "de": "German",
    "de-DE": "German (Germany)",
    "zh": "Chinese",
    "zh-CN": "Chinese (Simplified)",
    "zh-TW": "Chinese (Traditional)",
    "ja": "Japanese",
    "ja-JP": "Japanese (Japan)",
    "ko": "Korean",
    "ko-KR": "Korean (Korea)",
    "pt": "Portuguese",
    "pt-BR": "Portuguese (Brazil)",
    "pt-PT": "Portuguese (Portugal)",
    "it": "Italian",
    "it-IT": "Italian (Italy)",
    "ru": "Russian",
    "ru-RU": "Russian (Russia)",
    "ar": "Arabic",
    "ar-SA": "Arabic (Saudi Arabia)",
    "vi": "Vietnamese",
    "vi-VN": "Vietnamese (Vietnam)",
    "th": "Thai",
    "th-TH": "Thai (Thailand)",
    "tr": "Turkish",
    "tr-TR": "Turkish (Turkey)",
    "pl": "Polish",
    "pl-PL": "Polish (Poland)",
    "nl": "Dutch",
    "nl-NL": "Dutch (Netherlands)",
    "sv": "Swedish",
    "sv-SE": "Swedish (Sweden)",
    "da": "Danish",
    "da-DK": "Danish (Denmark)",
    "fi": "Finnish",
    "fi-FI": "Finnish (Finland)",
    "no": "Norwegian",
    "no-NO": "Norwegian (Norway)",
    "cs": "Czech",
    "cs-CZ": "Czech (Czechia)",
    "el": "Greek",
    "el-GR": "Greek (Greece)",
    "he": "Hebrew",
    "he-IL": "Hebrew (Israel)",
    "id": "Indonesian",
    "id-ID": "Indonesian (Indonesia)",
    "ms": "Malay",
    "ms-MY": "Malay (Malaysia)",
    "tl": "Tagalog",
    "tl-PH": "Tagalog (Philippines)",
    "uk": "Ukrainian",
    "uk-UA": "Ukrainian (Ukraine)",
    "bn": "Bengali",
    "bn-BD": "Bengali (Bangladesh)",
    "bn-IN": "Bengali (India)",
    "ta": "Tamil",
    "ta-IN": "Tamil (India)",
    "te": "Telugu",
    "te-IN": "Telugu (India)",
    "mr": "Marathi",
    "mr-IN": "Marathi (India)",
    "gu": "Gujarati",
    "gu-IN": "Gujarati (India)",
    "kn": "Kannada",
    "kn-IN": "Kannada (India)",
    "ml": "Malayalam",
    "ml-IN": "Malayalam (India)",
    "pa": "Punjabi",
    "pa-IN": "Punjabi (India)",
    "ur": "Urdu",
    "ur-PK": "Urdu (Pakistan)",
    "fa": "Persian",
    "fa-IR": "Persian (Iran)",
    "sw": "Swahili",
    "sw-KE": "Swahili (Kenya)",
    "am": "Amharic",
    "am-ET": "Amharic (Ethiopia)",
}

# Script detection patterns for quick heuristic
SCRIPT_PATTERNS = {
    "Devanagari": re.compile(r"[ऀ-ॿ]"),      # Hindi, Marathi, Sanskrit
    "Bengali": re.compile(r"[ঀ-৿]"),         # Bengali, Assamese
    "Gurmukhi": re.compile(r"[਀-੿]"),        # Punjabi
    "Gujarati": re.compile(r"[઀-૿]"),        # Gujarati
    "Oriya": re.compile(r"[଀-୿]"),           # Odia
    "Tamil": re.compile(r"[஀-௿]"),           # Tamil
    "Telugu": re.compile(r"[ఀ-౿]"),          # Telugu
    "Kannada": re.compile(r"[ಀ-೿]"),         # Kannada
    "Malayalam": re.compile(r"[ഀ-ൿ]"),       # Malayalam
    "Sinhala": re.compile(r"[඀-෿]"),         # Sinhala
    "Thai": re.compile(r"[฀-๿]"),            # Thai
    "Lao": re.compile(r"[຀-໿]"),             # Lao
    "Tibetan": re.compile(r"[ༀ-࿿]"),         # Tibetan
    "Myanmar": re.compile(r"[က-႟]"),         # Burmese
    "Georgian": re.compile(r"[Ⴀ-ჿ]"),        # Georgian
    "Ethiopic": re.compile(r"[ሀ-፿]"),        # Amharic, Tigrinya
    "Cherokee": re.compile(r"[Ꭰ-᏿]"),        # Cherokee
    "Canadian_Aboriginal": re.compile(r"[᐀-ᙿ]"),  # Cree, Inuktitut
    "Khmer": re.compile(r"[ក-៿]"),           # Khmer
    "Mongolian": re.compile(r"[᠀-᢯]"),       # Mongolian
    "Hiragana": re.compile(r"[぀-ゟ]"),        # Japanese Hiragana
    "Katakana": re.compile(r"[゠-ヿ]"),        # Japanese Katakana
    "Hangul": re.compile(r"[가-힯]"),          # Korean
    "CJK": re.compile(r"[一-鿿]"),             # Chinese, Japanese Kanji
    "Arabic": re.compile(r"[؀-ۿ]"),          # Arabic, Persian, Urdu
    "Hebrew": re.compile(r"[֐-׿]"),          # Hebrew
    "Cyrillic": re.compile(r"[Ѐ-ӿ]"),        # Russian, Ukrainian, Bulgarian, etc.
    "Greek": re.compile(r"[Ͱ-Ͽ]"),           # Greek
    "Armenian": re.compile(r"[԰-֏]"),        # Armenian
    "Latin": re.compile(r"[\u0000-\u007F]"),           # Basic Latin (ASCII)
    "Latin_Extended": re.compile(r"[\u0080-ɏ]"),  # Latin Extended
}

# Script to language mapping (primary language for script)
SCRIPT_TO_LANGUAGE: dict[str, str] = {
    "Devanagari": "hi-IN",
    "Bengali": "bn-BD",
    "Gurmukhi": "pa-IN",
    "Gujarati": "gu-IN",
    "Oriya": "or-IN",
    "Tamil": "ta-IN",
    "Telugu": "te-IN",
    "Kannada": "kn-IN",
    "Malayalam": "ml-IN",
    "Sinhala": "si-LK",
    "Thai": "th-TH",
    "Lao": "lo-LA",
    "Tibetan": "bo-CN",
    "Myanmar": "my-MM",
    "Georgian": "ka-GE",
    "Ethiopic": "am-ET",
    "Cherokee": "chr-US",
    "Canadian_Aboriginal": "iu-CA",
    "Khmer": "km-KH",
    "Mongolian": "mn-MN",
    "Hiragana": "ja-JP",
    "Katakana": "ja-JP",
    "Hangul": "ko-KR",
    "CJK": "zh-CN",
    "Arabic": "ar-SA",
    "Hebrew": "he-IL",
    "Cyrillic": "ru-RU",
    "Greek": "el-GR",
    "Armenian": "hy-AM",
    "Latin": "en-US",
    "Latin_Extended": "en-US",
}

# NVIDIA Magpie TTS supported languages
MAGPIE_LANGUAGES = {
    "en-US", "es-US", "fr-FR", "de-DE", "zh-CN", "vi-VN",
    "it-IT", "hi-IN", "ja-JP", "ko-KR", "ar-AR", "pt-BR"
}

# Groq Whisper supported languages (primary subtags)
WHISPER_LANGUAGES = {
    "en", "es", "fr", "de", "zh", "ja", "ko", "pt", "it",
    "ru", "ar", "vi", "hi", "th", "tr", "pl", "nl", "sv",
    "da", "fi", "no", "cs", "el", "he", "id", "ms", "tl",
    "uk", "bn", "ta", "te", "mr", "gu", "kn", "ml", "pa",
    "ur", "fa", "sw", "am"
}


def detect_script(text: str) -> str | None:
    """Detect the primary script used in text."""
    if not text:
        return None
    # Count characters per script
    script_counts: dict[str, int] = {}
    for script, pattern in SCRIPT_PATTERNS.items():
        matches = pattern.findall(text)
        if matches:
            script_counts[script] = len(matches)
    if not script_counts:
        return "Latin"
    # Return script with most characters
    return max(script_counts, key=script_counts.get)


def detect_language_from_text(text: str) -> str | None:
    """Heuristic language detection from text content."""
    if not text or not text.strip():
        return None

    script = detect_script(text)
    if script and script in SCRIPT_TO_LANGUAGE:
        return SCRIPT_TO_LANGUAGE[script]

    # For Latin script, use simple word frequency heuristics
    text_lower = text.lower()
    words = re.findall(r"\b\w+\b", text_lower)
    if not words:
        return "en-US"

    # Common word indicators
    indicators = {
        "en-US": {"the", "and", "is", "to", "of", "a", "in", "that", "it", "for", "you", "with", "on", "as", "at", "be", "this", "have", "or", "by"},
        "es-ES": {"el", "la", "de", "que", "y", "en", "un", "es", "se", "no", "te", "lo", "le", "da", "su", "por", "son", "con", "para", "al"},
        "fr-FR": {"le", "la", "de", "et", "à", "un", "il", "est", "je", "ne", "pas", "que", "se", "ce", "sur", "pour", "dans", "avec", "une", "des"},
        "de-DE": {"der", "die", "und", "in", "den", "von", "zu", "das", "mit", "sich", "des", "auf", "für", "ist", "im", "dem", "nicht", "ein", "eine", "als"},
        "it-IT": {"il", "di", "che", "e", "la", "per", "un", "in", "a", "da", "con", "non", "si", "del", "una", "le", "su", "degli", "dei", "delle"},
        "pt-BR": {"o", "a", "de", "e", "do", "da", "em", "um", "para", "com", "não", "uma", "os", "no", "se", "na", "pelo", "pelos", "que", "como"},
    }

    scores: dict[str, int] = {}
    for lang, common_words in indicators.items():
        scores[lang] = sum(1 for w in words if w in common_words)

    if scores:
        best = max(scores, key=scores.get)
        if scores[best] > 0:
            return best

    return "en-US"


def normalize_to_bcp47(lang: str | None) -> str | None:
    """Normalize language code to BCP-47 format."""
    if not lang:
        return None
    lang = lang.strip().lower().replace("_", "-")
    # Already in BCP-47 format
    if "-" in lang:
        return lang
    # Map primary subtag to common BCP-47
    primary_to_bcp47 = {
        "en": "en-US", "hi": "hi-IN", "es": "es-ES", "fr": "fr-FR",
        "de": "de-DE", "zh": "zh-CN", "ja": "ja-JP", "ko": "ko-KR",
        "pt": "pt-BR", "it": "it-IT", "ru": "ru-RU", "ar": "ar-SA",
        "vi": "vi-VN", "th": "th-TH", "tr": "tr-TR", "pl": "pl-PL",
        "nl": "nl-NL", "sv": "sv-SE", "da": "da-DK", "fi": "fi-FI",
        "no": "no-NO", "cs": "cs-CZ", "el": "el-GR", "he": "he-IL",
        "id": "id-ID", "ms": "ms-MY", "tl": "tl-PH", "uk": "uk-UA",
        "bn": "bn-BD", "ta": "ta-IN", "te": "te-IN", "mr": "mr-IN",
        "gu": "gu-IN", "kn": "kn-IN", "ml": "ml-IN", "pa": "pa-IN",
        "ur": "ur-PK", "fa": "fa-IR", "sw": "sw-KE", "am": "am-ET",
    }
    return primary_to_bcp47.get(lang, lang)


def get_tts_language(lang: str | None) -> str | None:
    """Get the best TTS language code for the given language."""
    if not lang:
        return "en-US"
    bcp47 = normalize_to_bcp47(lang)
    if not bcp47:
        return "en-US"
    # Check if Magpie supports it directly
    if bcp47 in MAGPIE_LANGUAGES:
        return bcp47
    # Try primary subtag
    primary = bcp47.split("-")[0]
    for magpie_lang in MAGPIE_LANGUAGES:
        if magpie_lang.startswith(primary + "-"):
            return magpie_lang
    # Default to English
    return "en-US"


def get_stt_language(lang: str | None) -> str | None:
    """Get the best STT language code for the given language."""
    if not lang:
        return None  # Auto-detect
    bcp47 = normalize_to_bcp47(lang)
    if not bcp47:
        return None
    primary = bcp47.split("-")[0]
    if primary in WHISPER_LANGUAGES:
        return primary  # Whisper uses primary subtags
    # For NVIDIA Parakeet, it accepts arbitrary BCP-47
    return bcp47


@tool(
    "language.detect",
    title="Detect language from text",
    description="Detect the language of a text string using script analysis and word frequency.",
    capability="system.info",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "Text to analyze"},
        },
        "required": ["text"],
    },
)
def language_detect(params: dict[str, Any]) -> ToolResult:
    text = str(params.get("text") or "")
    if not text:
        return ToolResult(ok=False, error="No text provided")

    detected = detect_language_from_text(text)
    script = detect_script(text)

    return ToolResult(
        ok=True,
        data={
            "language": detected,
            "language_name": LANGUAGE_NAMES.get(detected, detected) if detected else "Unknown",
            "script": script,
            "confidence": "high" if script and script != "Latin" else "medium",
        },
        summary=f"Detected: {LANGUAGE_NAMES.get(detected, detected) if detected else 'Unknown'} ({detected})",
    )


@tool(
    "language.normalize",
    title="Normalize language code",
    description="Convert a language code to BCP-47 format and find best TTS/STT codes.",
    capability="system.info",
    read_only=True,
    parameters={
        "type": "object",
        "properties": {
            "language": {"type": "string", "description": "Language code to normalize"},
        },
        "required": ["language"],
    },
)
def language_normalize(params: dict[str, Any]) -> ToolResult:
    lang = str(params.get("language") or "")
    if not lang:
        return ToolResult(ok=False, error="No language provided")

    bcp47 = normalize_to_bcp47(lang)
    tts_lang = get_tts_language(lang)
    stt_lang = get_stt_language(lang)

    return ToolResult(
        ok=True,
        data={
            "original": lang,
            "bcp47": bcp47,
            "tts_language": tts_lang,
            "stt_language": stt_lang,
            "tts_supported": tts_lang in MAGPIE_LANGUAGES if tts_lang else False,
            "stt_supported": stt_lang in WHISPER_LANGUAGES if stt_lang else False,
        },
        summary=f"{lang} -> BCP-47: {bcp47}, TTS: {tts_lang}, STT: {stt_lang}",
    )


# Export functions for use by other modules
__all__ = [
    "detect_language_from_text",
    "detect_script",
    "normalize_to_bcp47",
    "get_tts_language",
    "get_stt_language",
    "LANGUAGE_NAMES",
    "MAGPIE_LANGUAGES",
    "WHISPER_LANGUAGES",
]