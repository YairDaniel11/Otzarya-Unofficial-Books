#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
`Conection Type` parsing, ported from SeforimLibrary
core/src/commonMain/kotlin/.../core/models/Link.kt (ConnectionType.fromKnownStringOrNull).

The generator does NOT compare raw strings: it trims, lowercases and turns every single
space into '_' before looking the value up, and accepts a few aliases. Tracked links files
rely on that: the Sefaria-style spaced spellings ("ein mishpat / ner mitsvah" 180k,
"mesorat hashas" 117k, "related passage", "mishnah in talmud", "sifrei mitzvot" - almost
all under extraBooks/), "quotation_auto(_tanakh)", "none" and "" (1.2M). The aliases
"supercommentary", "footnote" and "ellucidation" are accepted by Link.kt but appear in no
tracked file (Sept 2026). Compare canonical names, never raw values.

Missing key / JSON null: Generator.kt decodes with coerceInputValues=true and
`connectionType: String = ""`, so both become "" -> OTHER. canonical_connection_type(None)
therefore returns "other". A misspelled "Connection Type" key is an unknown key
(ignoreUnknownKeys=true): the entry is read as if the type were missing -> OTHER.

canonical_connection_type(v) -> lowercase enum name ("footnotes", "ein_mishpat", ...),
or None when Link.kt rejects the value (fromString would then store it as OTHER).
"""
import unicodedata

_ALIASES = {
    "commentary": "commentary",
    "super_commentary": "super_commentary", "supercommentary": "super_commentary",
    "targum": "targum",
    "reference": "reference",
    "source": "source",
    "midrash": "midrash",
    "quotation": "quotation", "quotation_auto": "quotation",
    "quotation_auto_tanakh": "quotation",
    "mesorat_hashas": "mesorat_hashas",
    "ein_mishpat": "ein_mishpat", "ein_mishpat_/_ner_mitsvah": "ein_mishpat",
    "ein_mishpat_/_ner_mitzvah": "ein_mishpat",
    "dibur_hamatchil": "dibur_hamatchil",
    "parshanut": "parshanut",
    "mishnah_in_talmud": "mishnah_in_talmud",
    "related": "related", "related_passage": "related",
    "linker": "linker",
    "sifrei_mitzvot": "sifrei_mitzvot",
    "essay": "essay",
    "allusion": "allusion",
    "liturgy": "liturgy",
    "ellucidation": "elucidation", "elucidation": "elucidation",
    "explication": "explication",
    "law": "law",
    "summary": "summary",
    "footnotes": "footnotes", "footnote": "footnotes",
    "": "other", "none": "other", "other": "other",
}


def _kt_ws(c):
    """Kotlin Char.isWhitespace(): Character.isWhitespace || Character.isSpaceChar."""
    return c in "\t\n\x0b\f\r\x1c\x1d\x1e\x1f" or unicodedata.category(c) in ("Zs", "Zl", "Zp")


def kt_trim(s):
    """Kotlin String.trim() (keeps U+0085, unlike str.strip())."""
    i, j = 0, len(s)
    while i < j and _kt_ws(s[i]):
        i += 1
    while j > i and _kt_ws(s[j - 1]):
        j -= 1
    return s[i:j]


def canonical_connection_type(value):
    """Port of ConnectionType.fromKnownStringOrNull; non-strings -> None."""
    if value is None:            # missing key or JSON null -> default "" -> OTHER
        return "other"
    if not isinstance(value, str):
        return None
    # trim() -> lowercase() -> replace(' ', '_'): only U+0020, one for one.
    return _ALIASES.get(kt_trim(value).lower().replace(" ", "_"))
