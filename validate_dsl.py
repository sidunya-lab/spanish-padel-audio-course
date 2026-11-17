#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Строгий валидатор всех 28 DSL-сценариев по всем правилам ТЗ (разделы 5, 6, 7, 11)."""

import re
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SCRIPTS_DIR = BASE_DIR / "scripts_2026-11-17"
TEST_FILE = BASE_DIR / "examples" / "test_chapter.dsl.txt"

FORBIDDEN_PHRASES = [
    "следующая фраза",
    "слушайте",
    "скажите по-испански",
    "а теперь вспомните предыдущую фразу",
    "по-испански так",
    "повторяйте вслух в паузах",
]

CYR_RE = re.compile(r"[А-Яа-яЁё]")
LAT_RE = re.compile(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ]")
DIGIT_RE = re.compile(r"\d")


def check_russian_segment(seg: str, fname: str, lineno: int, context: str, errors: list):
    # 1. Mixed Cyrillic + Latin inside a single word
    for word in re.findall(r"\S+", seg):
        clean = word.strip(".,!?;:«»\"'()—–-")
        if CYR_RE.search(clean) and LAT_RE.search(clean):
            errors.append(f"{fname}:{lineno} [{context}] MIXED CYR+LAT WORD: {word!r}")
        elif LAT_RE.search(clean):
            errors.append(f"{fname}:{lineno} [{context}] UNBRACED LATIN IN RUSSIAN TTS: {word!r} in {seg!r}")
        if DIGIT_RE.search(clean):
            errors.append(f"{fname}:{lineno} [{context}] DIGIT IN SPOKEN TEXT: {word!r}")
    low = seg.lower()
    for fp in FORBIDDEN_PHRASES:
        if fp in low:
            errors.append(f"{fname}:{lineno} [{context}] FORBIDDEN FORMULA: {fp!r}")


def check_braced_russian(text: str, fname: str, lineno: int, context: str, errors: list):
    parts = re.split(r"(\{[^}]+\})", text)
    # Check each Russian segment and count words between two {..} blocks
    for i, part in enumerate(parts):
        if part.startswith("{") and part.endswith("}"):
            es_inner = part[1:-1].strip()
            if not es_inner:
                errors.append(f"{fname}:{lineno} [{context}] EMPTY BRACES")
            if CYR_RE.search(es_inner):
                errors.append(f"{fname}:{lineno} [{context}] CYRILLIC INSIDE SPANISH BRACES: {part!r}")
        else:
            check_russian_segment(part, fname, lineno, context, errors)
            # Check if sandwiched between two {..} blocks!
            if 0 < i < len(parts) - 1 and parts[i - 1].startswith("{") and parts[i + 1].startswith("{"):
                ru_words = re.findall(r"[А-Яа-яЁё]+(?:-[А-Яа-яЁё]+)*", part)
                if len(ru_words) < 3:
                    errors.append(
                        f"{fname}:{lineno} [{context}] TOO SHORT RUSSIAN BRIDGE (<3 words: {len(ru_words)}): "
                        f"{parts[i-1]} + {part!r} + {parts[i+1]}"
                    )


def check_spanish_text(es: str, fname: str, lineno: int, context: str, errors: list):
    if CYR_RE.search(es):
        errors.append(f"{fname}:{lineno} [{context}] CYRILLIC IN SPANISH: {es!r}")
    if DIGIT_RE.search(es):
        errors.append(f"{fname}:{lineno} [{context}] DIGIT IN SPANISH: {es!r}")
    if "bolea" in es.lower():
        errors.append(f"{fname}:{lineno} [{context}] TYPO bolea INSTEAD OF volea: {es!r}")
    # Check balanced ¿? and ¡!
    if es.count("¿") != es.count("?"):
        errors.append(f"{fname}:{lineno} [{context}] UNBALANCED ¿?: {es!r}")
    if es.count("¡") != es.count("!"):
        errors.append(f"{fname}:{lineno} [{context}] UNBALANCED ¡!: {es!r}")


def validate_file(path: Path, errors: list):
    lines = path.read_text(encoding="utf-8").strip().splitlines()
    fname = path.name
    tags_seen = []
    for idx, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([NTGDLPSVE])\s+(.*)$", line)
        if not m:
            errors.append(f"{fname}:{idx} INVALID DSL LINE: {line!r}")
            continue
        tag, rest = m.group(1), m.group(2)
        tags_seen.append(tag)
        if tag == "N":
            pass
        elif tag == "T":
            fields = rest.split("|")
            if len(fields) < 2:
                errors.append(f"{fname}:{idx} [T] Missing spoken_title after '|': {rest!r}")
            else:
                check_braced_russian(fields[1], fname, idx, "T_spoken", errors)
        elif tag == "G":
            check_braced_russian(rest, fname, idx, "G", errors)
        elif tag == "D":
            fields = rest.split("|")
            if len(fields) != 2 or fields[0] not in ("A", "M", "F", "B"):
                errors.append(f"{fname}:{idx} [D] Invalid speaker or format: {rest!r}")
            else:
                check_spanish_text(fields[1], fname, idx, f"D_{fields[0]}", errors)
        elif tag == "L":
            check_braced_russian(rest, fname, idx, "L", errors)
        elif tag == "P":
            fields = rest.split("|")
            if len(fields) < 3:
                errors.append(f"{fname}:{idx} [P] Expected at least 3 fields: {rest!r}")
            else:
                check_spanish_text(fields[0], fname, idx, "P_es", errors)
                check_braced_russian(fields[1], fname, idx, "P_lead", errors)
                if fields[2].strip():
                    check_braced_russian(fields[2], fname, idx, "P_note", errors)
                if len(fields) >= 4 and fields[3].strip():
                    check_braced_russian(fields[3], fname, idx, "P_recall", errors)
        elif tag == "S":
            fields = rest.split("|")
            if len(fields) != 2:
                errors.append(f"{fname}:{idx} [S] Expected 2 fields: {rest!r}")
            else:
                check_braced_russian(fields[0], fname, idx, "S_ru", errors)
                check_spanish_text(fields[1], fname, idx, "S_es", errors)
        elif tag == "V":
            fields = rest.split("|")
            if len(fields) == 3:
                spk, es, ru = fields
                if spk not in ("A", "M", "F"):
                    errors.append(f"{fname}:{idx} [V] Invalid speaker {spk!r}")
            elif len(fields) == 2:
                es, ru = fields
            else:
                errors.append(f"{fname}:{idx} [V] Invalid format: {rest!r}")
                continue
            check_spanish_text(es, fname, idx, "V_es", errors)
            check_braced_russian(ru, fname, idx, "V_ru", errors)
        elif tag == "E":
            check_braced_russian(rest, fname, idx, "E", errors)


all_files = [TEST_FILE] + sorted(SCRIPTS_DIR.glob("*.txt"))
errors = []
for f in all_files:
    validate_file(f, errors)

print(f"Validated {len(all_files)} DSL files.")
if errors:
    print(f"FOUND {len(errors)} ISSUES:")
    for e in errors:
        print("  -", e)
    sys.exit(1)
else:
    print("ZERO ERRORS! All 28 DSL scripts pass 100% of TZ rules.")
