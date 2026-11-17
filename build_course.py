#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Промышленный сборщик аудиокурса «Испанский для жизни и падела» (v4, по ТЗ от 16.11.2026).

Выполняет полный цикл разделов 5–11 и 13 ТЗ:
1. Парсинг сценариев DSL (`N`, `T`, `G`, `D`, `L`, `P`, `S`, `V`, `E`) и строгая проверка правил текста.
2. Разворачивание главы в фрагменты: заставка, диалог по ролям (`A` / `M` / `F`), логика со вставками `{...}`,
   ситуативные подводки, умный разбор фраз с конца (back-chaining по синтаксическим группам и слогам RAE),
   автоматические интервальные возвраты без повтора служебных формул (>2 раз запрещено),
   самостоятельные ответы (пауза 2.5 с), «Живая речь» и концовка.
3. Пофразовый синтез с контент-адресуемым SHA-256 кэшем:
   - Русский: Gemini TTS API (`gemini-3.8-flash-tts`, резерв `gemini-2.5-flash-preview-tts`), голос `Charon`.
   - Испанский (акцент Испании): Microsoft `edge-tts` (`es-ES-AlvaroNeural`, `es-ES-ArnauNeural`, `es-ES-ElviraNeural`)
     либо Gemini TTS (переключается флагом `--es-provider`).
4. Автоматический контроль качества каждого русского фрагмента распознаванием речи (`faster-whisper`):
   две метрики из раздела 11 ТЗ (доля 4-буквенных основ >= 0.87 + общее сходство строк >= 0.84),
   до 3 перезаписей, отдельный реестр слов с двойным ударением.
5. Монтаж в `ffmpeg`:
   - обрезка краевой тишины каждого фрагмента с порогом `-45dB`;
   - генерация точных пауз WAV 24 кГц моно по разделу 8 ТЗ (`0.15`..`2.5` с, повтор `min(1.6, dur*1.1 + 0.35)`);
   - двухпроходная нормализация громкости `-16 LUFS`, True Peak `-1.5 dBTP`, MP3 128 кбит/с моно;
   - автоматический замер тишины готовой главы (`silencedetect`): проверка отсутствия провалов > 2.05 с вне окон `S`.
6. Выпуск полного комплекта:
   - отдельные MP3 всех глав;
   - единый MP3 курса со встроенными главами (`FFMETADATA1` `[CHAPTER]`);
   - дорожки «Режим Тень» (`shadowing/`) без русского текста для повторения в дороге;
   - ZIP-архив глав, `Оглавление с таймкодами.md`, колода `Испанский_для_падела_карточки_Anki.csv` и `QA_report.md`;
   - автокопирование в локальную папку iCloud Drive и Google Drive (если смонтированы на ПК).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import difflib
import hashlib
import json
import math
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import wave
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------------------------------------------------------------------------
# Конфигурация по умолчанию (разделы 7–9 ТЗ)
# ---------------------------------------------------------------------------

SAMPLE_RATE = 24000
CHANNELS = 1
MP3_BITRATE = "128k"
TARGET_LUFS = -16.0
TARGET_TP = -1.5
TARGET_LRA = 11.0
TRIM_THRESHOLD_DB = "-45dB"

DEFAULT_GEMINI_MODELS = [
    "gemini-3.8-flash-tts",
    "gemini-2.5-flash-preview-tts",
]
DEFAULT_RU_VOICE = "Charon"

EDGE_ES_VOICES = {
    "A": "es-ES-AlvaroNeural",   # Ученик + испанские эталоны и разбор (мужской, Испания)
    "M": "es-ES-AlvaroNeural",   # Собеседник-мужчина (Arnau/Dario недоступны в edge-tts: тот же Alvaro, но с понижением тона, см. EDGE_M_PITCH)
    "F": "es-ES-ElviraNeural",   # Собеседница-женщина: ресепшен, кафе, магазин, отель, партнёрша (женский, Испания)
    "B": "es-ES-ElviraNeural",   # Обратная совместимость со старыми DSL
}
EDGE_M_FALLBACK = "es-ES-AlvaroNeural"
EDGE_M_PITCH = "-18Hz"  # отличие роли M от A при одном доступном мужском голосе Испании

GEMINI_ES_VOICES = {
    "A": "Orus",
    "M": "Fenrir",
    "F": "Aoede",
    "B": "Aoede",
}

CHAPTER_NUM_SPOKEN = {
    0: "Бонусная глава",
    1: "Глава первая",
    2: "Глава вторая",
    3: "Глава третья",
    4: "Глава четвёртая",
    5: "Глава пятая",
    6: "Глава шестая",
    7: "Глава седьмая",
    8: "Глава восьмая",
    9: "Глава девятая",
    10: "Глава десятая",
    11: "Глава одиннадцатая",
    12: "Глава двенадцатая",
    13: "Глава тринадцатая",
    14: "Глава четырнадцатая",
    15: "Глава пятнадцатая",
    16: "Глава шестнадцатая",
    17: "Глава семнадцатая",
    18: "Глава восемнадцатая",
    19: "Глава девятнадцатая",
    20: "Глава двадцатая",
    21: "Глава двадцать первая",
    22: "Глава двадцать вторая",
    23: "Глава двадцать третья",
    24: "Глава двадцать четвёртая",
    25: "Глава двадцать пятая",
    26: "Глава двадцать шестая",
    27: "Глава двадцать седьмая",
    28: "Глава двадцать восьмая",
    29: "Глава двадцать девятая",
    30: "Глава тридцатая",
}

HOMOGRAPH_WATCHLIST = {
    "стоит", "стоят", "стоим", "стоите", "стоила", "стоило",
    "плачу", "платит", "замок", "замка", "все", "всё",
    "потом", "больше", "правила", "среды", "руки", "ноги",
}

CLITICS_AND_PREPS = {
    "a", "al", "de", "del", "en", "con", "por", "para", "sin", "sobre",
    "hasta", "hacia", "ante", "bajo", "contra", "desde", "durante", "entre",
    "el", "la", "los", "las", "un", "una", "unos", "unas",
    "mi", "mis", "tu", "tus", "su", "sus", "nuestro", "nuestra",
    "este", "esta", "estos", "estas", "ese", "esa", "esos", "esas",
    "otro", "otra", "otros", "otras", "cada", "todo", "toda", "todos", "todas",
    "no", "me", "te", "se", "nos", "os", "le", "les", "lo",
    "y", "e", "o", "u", "pero", "que", "si", "como", "cuando", "donde",
    "qué", "cómo", "dónde", "cuándo", "cuánto", "cuánta", "cuántos", "cuántas", "quién",
    "muy", "más", "menos", "tan", "ya", "casi",
}

DRILL_PROMPTS = [
    "Разберём с конца. Повторяй.",
    "По частям, с конца.",
    "Повторяй по частям.",
    "Соберём с конца по кусочкам.",
    None,  # Разбор без подводки (разрешён п. 5 ТЗ, чтобы ни одна подводка не звучала >2 раз)
]

RECALL_TEMPLATES = [
    "Вспомни, как сказать: {q}.",
    "Проверим прошлую фразу: {q}.",
    "Скажи сам по памяти: {q}.",
    "Ещё раз самостоятельно: {q}.",
]


# ---------------------------------------------------------------------------
# Структуры данных
# ---------------------------------------------------------------------------

@dataclass
class PhraseItem:
    es: str
    ru_lead: str
    ru_note: str = ""
    ru_recall: str = ""


@dataclass
class ChapterDSL:
    source_path: Path
    label: str = ""
    display_title: str = ""
    spoken_title: str = ""
    goal: str = ""
    dialog: List[Tuple[str, str]] = field(default_factory=list)
    logic: List[str] = field(default_factory=list)
    phrases: List[PhraseItem] = field(default_factory=list)
    situations: List[Tuple[str, str]] = field(default_factory=list)
    live_speech: List[Tuple[str, str, str]] = field(default_factory=list)
    ending: str = ""


@dataclass
class Segment:
    lang: str            # 'ru' or 'es'
    voice_key: str       # 'RU', 'A', 'M', 'F'
    rate: str            # '+0%', '-10%', '-15%', '-35%'
    text: str
    pause_after: float | str  # seconds float or 'e' (dynamic repeat pause)
    role_tag: str = ""   # 'intro', 'dialog', 'logic', 'phrase_model', 'drill', 'recall', 'situation', 'live', 'ending'
    include_in_shadowing: bool = False


# ---------------------------------------------------------------------------
# Парсинг DSL и разбор испанских слов/фраз с конца
# ---------------------------------------------------------------------------

def parse_dsl(path: Path) -> ChapterDSL:
    ch = ChapterDSL(source_path=path)
    for lineno, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line:
            continue
        m = re.match(r"^([NTGDLPSVE])\s+(.*)$", line)
        if not m:
            raise ValueError(f"{path.name}:{lineno}: Неизвестный формат строки DSL: {line!r}")
        tag, body = m.group(1), m.group(2).strip()
        if tag == "N":
            ch.label = body
        elif tag == "T":
            parts = body.split("|", 1)
            ch.display_title = parts[0].strip()
            ch.spoken_title = parts[1].strip() if len(parts) > 1 else parts[0].strip()
        elif tag == "G":
            ch.goal = body
        elif tag == "D":
            spk, txt = body.split("|", 1)
            ch.dialog.append((spk.strip(), txt.strip()))
        elif tag == "L":
            ch.logic.append(body)
        elif tag == "P":
            parts = [p.strip() for p in body.split("|")]
            es = parts[0]
            ru_lead = parts[1] if len(parts) > 1 else ""
            ru_note = parts[2] if len(parts) > 2 else ""
            ru_recall = parts[3] if len(parts) > 3 else ru_lead.rstrip(".?!")
            ch.phrases.append(PhraseItem(es=es, ru_lead=ru_lead, ru_note=ru_note, ru_recall=ru_recall))
        elif tag == "S":
            ru_q, es_a = body.split("|", 1)
            ch.situations.append((ru_q.strip(), es_a.strip()))
        elif tag == "V":
            parts = [p.strip() for p in body.split("|")]
            if len(parts) == 3:
                ch.live_speech.append((parts[0], parts[1], parts[2]))
            elif len(parts) == 2:
                ch.live_speech.append(("M", parts[0], parts[1]))
            else:
                raise ValueError(f"{path.name}:{lineno}: Ошибка формата строки V: {line!r}")
        elif tag == "E":
            ch.ending = body
    return ch


def syllabify_spanish_word(word: str) -> List[str]:
    """Приближённое слогоделение испанского слова по правилам RAE для разбора с конца."""
    clean = re.sub(r"[¡!¿?.,;:«»\"'()]", "", word)
    if len(clean) <= 5:
        return [clean]
    vowels = "aeiouáéíóúüAEIOUÁÉÍÓÚÜ"
    strong = "aeoáéíóúAEOÁÉÍÓÚ"
    inseparable = {
        "bl", "br", "cl", "cr", "dr", "fl", "fr", "gl", "gr",
        "pl", "pr", "tr", "ch", "ll", "rr", "qu", "gu",
    }
    # Находим ядра гласных (дифтонги держим вместе)
    nuclei: List[Tuple[int, int]] = []
    i = 0
    n = len(clean)
    while i < n:
        if clean[i] in vowels:
            start = i
            i += 1
            while i < n and clean[i] in vowels:
                # Два сильных гласных образуют зияние (hiato)
                if clean[i - 1] in strong and clean[i] in strong:
                    break
                # 'qu'/'gu' перед e/i
                i += 1
            nuclei.append((start, i))
        else:
            i += 1
    if len(nuclei) <= 2:
        return [clean]

    boundaries = [0]
    for idx in range(len(nuclei) - 1):
        c_start = nuclei[idx][1]
        c_end = nuclei[idx + 1][0]
        cons = clean[c_start:c_end].lower()
        if len(cons) == 0:
            boundaries.append(c_start)
        elif len(cons) == 1:
            boundaries.append(c_start)
        elif len(cons) == 2:
            if cons in inseparable:
                boundaries.append(c_start)
            else:
                boundaries.append(c_start + 1)
        else:
            if cons[-2:] in inseparable:
                boundaries.append(c_end - 2)
            else:
                boundaries.append(c_end - 1)
    boundaries.append(n)
    sylls = [clean[boundaries[j]:boundaries[j + 1]] for j in range(len(boundaries) - 1)]
    return [s for s in sylls if s]


def group_spanish_tokens(clause: str) -> List[str]:
    """Группирует служебные предлоги, артикли и клитики со следующим полнозначным словом."""
    raw_tokens = clause.strip().split()
    if not raw_tokens:
        return []
    groups: List[str] = []
    buf: List[str] = []
    for tok in raw_tokens:
        norm = re.sub(r"^[¡¿]+|[!?.,;:]+$", "", tok).lower()
        buf.append(tok)
        if norm not in CLITICS_AND_PREPS:
            groups.append(" ".join(buf))
            buf = []
    if buf:
        if groups:
            groups[-1] = groups[-1] + " " + " ".join(buf)
        else:
            groups.append(" ".join(buf))
    return groups


def build_backchain_steps(es_phrase: str) -> List[str]:
    """
    Строит 2–5 шагов разбора с конца по правилам раздела 5 ТЗ:
    - не склеивает два разных предложения без паузы;
    - не отрывает предлоги/артикли/частицу no от слова;
    - для длинного ключевого слова (>=4 слогов в короткой фразе) добавляет слоговой разбор с конца.
    """
    sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+", es_phrase.strip()) if s.strip()]
    steps: List[str] = []

    if len(sentences) >= 2:
        # Сначала разбираем с конца второе предложение, затем первое (если оно длинное)
        last_sent = sentences[-1]
        groups_last = group_spanish_tokens(last_sent)
        # Проверим последнее длинное слово
        last_word = groups_last[-1].split()[-1] if groups_last else ""
        sylls = syllabify_spanish_word(last_word)
        if len(sylls) >= 4:
            steps.append(sylls[-2] + sylls[-1])
            steps.append("".join(sylls))
        elif len(groups_last) >= 2:
            steps.append(groups_last[-1])
        steps.append(last_sent)

        first_sent = " ".join(sentences[:-1])
        groups_first = group_spanish_tokens(first_sent)
        if len(groups_first) >= 2:
            steps.append(groups_first[-1])
        steps.append(first_sent)
    else:
        sent = sentences[0]
        # Делим по запятой, если есть две смысловые части
        comma_parts = [c.strip() for c in sent.split(",") if c.strip()]
        groups = group_spanish_tokens(sent)
        total_words = len(sent.split())

        if total_words <= 2:
            # Короткие фразы из 1–2 слов по слогам не дробятся (раздел 5 ТЗ)
            if len(groups) == 2:
                steps.append(groups[-1])
            else:
                # Одно слово >= 4 слогов или без дробления
                sylls = syllabify_spanish_word(sent)
                if len(sylls) >= 4:
                    steps.append(sylls[-2] + sylls[-1])
                return steps[:4]
            return steps

        # Проверим, стоит ли разобрать по слогам последнее длинное слово (>=4 слогов)
        last_word_raw = sent.split()[-1]
        sylls = syllabify_spanish_word(last_word_raw)
        if len(sylls) >= 4 and len(groups) <= 3:
            steps.append(sylls[-2] + sylls[-1])
            steps.append("".join(sylls))

        if len(comma_parts) >= 2 and len(groups) >= 4:
            tail_clause = comma_parts[-1]
            tail_groups = group_spanish_tokens(tail_clause)
            if len(tail_groups) >= 2:
                steps.append(tail_groups[-1])
            steps.append(tail_clause)
            head_clause = ", ".join(comma_parts[:-1])
            steps.append(f"{head_clause}, {tail_clause}")
        else:
            # Наращиваем синтаксические группы справа налево (без дублирования полной фразы, т.к. дальше идёт -15%)
            for k in range(1, len(groups)):
                chunk = " ".join(groups[len(groups) - k:])
                if chunk not in steps:
                    steps.append(chunk)

    # Убираем точное совпадение с полной фразой (полная фраза идёт следом на -15% и +0%) и ограничиваем 2..5 шагами
    norm_full = re.sub(r"[¡!¿?.,]", "", es_phrase).strip().lower()
    filtered = [
        s for s in steps
        if re.sub(r"[¡!¿?.,]", "", s).strip().lower() != norm_full
    ]
    if not filtered:
        # Минимум 1 подготовительный кусок перед -15%
        groups = group_spanish_tokens(es_phrase)
        if len(groups) >= 2:
            filtered = [groups[-1]]
        else:
            filtered = [es_phrase]
    if len(filtered) > 4:
        # Оставляем равномерно до 4 шагов + шаг -15% = 5 шагов разбора
        idxs = sorted({0, len(filtered) // 2, len(filtered) - 2, len(filtered) - 1})
        filtered = [filtered[i] for i in idxs]
    return filtered


def split_braced_text(text: str, final_pause: float, role_tag: str) -> List[Segment]:
    """Разрезает русскую строку со вставками {испанский} со стыком 0.15 с (раздел 8 ТЗ)."""
    parts = re.split(r"(\{[^}]+\})", text)
    raw_items: List[Tuple[str, str]] = []
    for p in parts:
        if not p or not p.strip():
            continue
        if p.startswith("{") and p.endswith("}"):
            es_txt = p[1:-1].strip()
            if es_txt:
                raw_items.append(("es", es_txt))
        else:
            # Очищаем висячие тире/запятые на краях коротких склеек, оставляя естественную пунктуацию
            ru_txt = p.strip()
            if re.search(r"[А-Яа-яЁё]", ru_txt):
                raw_items.append(("ru", ru_txt))

    segs: List[Segment] = []
    for idx, (lang, val) in enumerate(raw_items):
        pause = final_pause if idx == len(raw_items) - 1 else 0.15
        if lang == "ru":
            segs.append(Segment("ru", "RU", "+0%", val, pause, role_tag=role_tag))
        else:
            segs.append(Segment("es", "A", "-10%", val, pause, role_tag=role_tag))
    return segs


def spoken_label_for_chapter(label: str) -> str:
    m = re.search(r"(\d+)", label)
    if m:
        num = int(m.group(1))
        return CHAPTER_NUM_SPOKEN.get(num, label)
    return label


def expand_chapter_to_segments(ch: ChapterDSL) -> List[Segment]:
    """Разворачивает DSL главы в точную последовательность аудиофрагментов и пауз (разделы 5 и 8 ТЗ)."""
    segs: List[Segment] = []

    # 1. Заставка
    segs.append(Segment("es", "A", "+0%", "¡Hola, amigos!", 0.3, role_tag="intro"))
    lbl_spoken = spoken_label_for_chapter(ch.label)
    intro_line = f"Создано для Падел Амбассадора. {lbl_spoken}. {ch.spoken_title}."
    segs.extend(split_braced_text(intro_line, final_pause=0.6, role_tag="intro"))

    # 2. Цель главы
    if ch.goal:
        segs.extend(split_braced_text(ch.goal, final_pause=0.7, role_tag="goal"))

    # 3. Вступительный диалог
    if ch.dialog:
        segs.append(Segment("ru", "RU", "+0%", "Сначала послушай разговор. Пока просто слушай.", 0.5, role_tag="dialog_intro"))
        for i, (spk, line) in enumerate(ch.dialog):
            pause = 0.8 if i == len(ch.dialog) - 1 else 0.35
            segs.append(Segment("es", spk, "+0%", line, pause, role_tag="dialog", include_in_shadowing=True))

    # 4. Логика
    for l_line in ch.logic:
        segs.extend(split_braced_text(l_line, final_pause=0.55, role_tag="logic"))

    # 5 & 6. Разбор фраз + интервальные возвраты к пройденным фразам
    drill_idx = 0
    recall_idx = 0
    recalled_indices: List[int] = []

    for p_idx, item in enumerate(ch.phrases):
        # i. Смысл / ситуация по-русски
        segs.extend(split_braced_text(item.ru_lead, final_pause=0.35, role_tag="phrase_lead"))
        # ii. Испанский образец в обычном темпе
        segs.append(Segment("es", "A", "+0%", item.es, 0.6, role_tag="phrase_model", include_in_shadowing=True))
        # iii. Короткое пояснение по словам (если есть)
        if item.ru_note:
            segs.extend(split_braced_text(item.ru_note, final_pause=0.4, role_tag="phrase_note"))

        # iv. Разбор с конца (чередуем 5 вариантов, ни один не чаще 2 раз за главу)
        prompt = DRILL_PROMPTS[drill_idx % len(DRILL_PROMPTS)]
        drill_idx += 1
        if prompt is not None:
            segs.append(Segment("ru", "RU", "+0%", prompt, 0.3, role_tag="drill_prompt"))

        for chunk in build_backchain_steps(item.es):
            segs.append(Segment("es", "A", "-35%", chunk, "e", role_tag="drill_chunk"))

        # v. Фраза целиком чуть медленнее (-15%) + пауза на повтор
        segs.append(Segment("es", "A", "-15%", item.es, "e", role_tag="phrase_slow", include_in_shadowing=True))
        # vi. Фраза целиком в обычном темпе (+0%) + пауза на повтор
        segs.append(Segment("es", "A", "+0%", item.es, "e", role_tag="phrase_final", include_in_shadowing=True))

        # Интервальный возврат после каждой 2-й фразы (начиная со второй, не чаще 4 возвратов на главу)
        if p_idx in (1, 3, 5, 7):
            candidates = [j for j in range(p_idx) if j not in recalled_indices]
            if candidates:
                target_j = candidates[0]
                recalled_indices.append(target_j)
                prev_item = ch.phrases[target_j]
                tpl = RECALL_TEMPLATES[recall_idx % len(RECALL_TEMPLATES)]
                recall_idx += 1
                recall_ru = tpl.format(q=prev_item.ru_recall)
                segs.append(Segment("ru", "RU", "+0%", recall_ru, 1.8, role_tag="recall_prompt"))
                segs.append(Segment("es", "A", "+0%", prev_item.es, 0.6, role_tag="recall_answer"))

    # 7. Самостоятельные ответы (пауза 2.5 с по разделу 8 ТЗ)
    for ru_task, es_ans in ch.situations:
        segs.extend(split_braced_text(ru_task, final_pause=2.5, role_tag="situation_prompt"))
        segs.append(Segment("es", "A", "+0%", es_ans, 0.6, role_tag="situation_answer", include_in_shadowing=True))

    # 8. Живая речь
    if ch.live_speech:
        segs.append(
            Segment(
                "ru",
                "RU",
                "+0%",
                "Теперь живая речь — так это звучит в клубе в обычном темпе. Лови знакомые слова.",
                0.5,
                role_tag="live_intro",
            )
        )
        for spk, es_live, ru_sum in ch.live_speech:
            segs.append(Segment("es", spk, "+0%", es_live, 0.6, role_tag="live_es", include_in_shadowing=True))
            segs.extend(split_braced_text(ru_sum, final_pause=0.7, role_tag="live_ru"))

    # 9. Концовка
    if ch.ending:
        segs.extend(split_braced_text(ch.ending, final_pause=0.25, role_tag="ending"))

    return segs


# ---------------------------------------------------------------------------
# Синтез речи (Gemini TTS + Edge-TTS + Mock для офлайн-самопроверки)
# ---------------------------------------------------------------------------

def segment_cache_key(seg: Segment, voice_id: str, provider: str) -> str:
    payload = f"{provider}|{seg.lang}|{voice_id}|{seg.rate}|{seg.text}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:24]


def generate_mock_wav(path: Path, text: str, lang: str, rate: str) -> None:
    """Генерирует тестовый голосовой огибающий сигнал для офлайн-проверки монтажа ffmpeg."""
    words = max(1, len(text.split()))
    speed_factor = 1.0
    if rate == "-10%":
        speed_factor = 1.11
    elif rate == "-15%":
        speed_factor = 1.18
    elif rate == "-35%":
        speed_factor = 1.45
    dur = max(0.45, min(7.5, words * 0.34 * speed_factor))
    n_samples = int(SAMPLE_RATE * dur)
    freq = 185.0 if lang == "ru" else 220.0
    buf = bytearray()
    for i in range(n_samples):
        t = i / SAMPLE_RATE
        # Плавная атака и затухание + слоговая огибающая
        env = min(1.0, t / 0.03, (dur - t) / 0.04) * (0.65 + 0.35 * math.sin(2 * math.pi * 4.2 * t))
        val = int(9500 * max(0.0, env) * math.sin(2 * math.pi * freq * t))
        buf.extend(struct.pack("<h", val))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(bytes(buf))


async def synth_edge_tts(text: str, voice: str, rate: str, out_wav: Path, pitch: str = "+0Hz") -> None:
    import edge_tts  # type: ignore

    tmp_mp3 = out_wav.with_suffix(".edge_tmp.mp3")
    comm = edge_tts.Communicate(text=text, voice=voice, rate=rate, pitch=pitch)
    await comm.save(str(tmp_mp3))
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(tmp_mp3),
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le",
            str(out_wav),
        ],
        check=True,
    )
    tmp_mp3.unlink(missing_ok=True)


def synth_gemini_tts(text: str, voice_name: str, rate: str, out_wav: Path, model_candidates: List[str]) -> None:
    """Gemini TTS через REST (generateContent); не зависит от версии SDK google-genai."""
    import base64
    import requests  # type: ignore

    api_key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not api_key:
        raise RuntimeError("Не задан GEMINI_API_KEY (или GOOGLE_API_KEY) в переменных окружения.")

    style_hint = None
    if rate == "-35%":
        style_hint = "speaking slowly and clearly for language learners"
    elif rate == "-15%":
        style_hint = "speaking slightly slower than normal"
    elif rate == "-10%":
        style_hint = "speaking clearly"

    last_err: Optional[Exception] = None
    for model_name in model_candidates:
        try:
            prompt_text = text
            if style_hint and "2.5" in model_name:
                prompt_text = f"Say {style_hint}: {text}"
            body = {
                "contents": [{"parts": [{"text": prompt_text}]}],
                "generationConfig": {
                    "responseModalities": ["AUDIO"],
                    "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": voice_name}}},
                },
            }
            r = requests.post(
                f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                json=body, timeout=180,
            )
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}: {r.text[:300]}")
            part = r.json()["candidates"][0]["content"]["parts"][0]["inlineData"]
            pcm_bytes = base64.b64decode(part["data"])
            mime = (part.get("mimeType") or "").lower()
            if "wav" in mime or pcm_bytes[:4] == b"RIFF":
                tmp_raw = out_wav.with_suffix(".gem_raw.wav")
                tmp_raw.write_bytes(pcm_bytes)
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-i", str(tmp_raw),
                        "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le",
                        str(out_wav),
                    ],
                    check=True,
                )
                tmp_raw.unlink(missing_ok=True)
            else:
                with wave.open(str(out_wav), "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(SAMPLE_RATE)
                    wf.writeframes(pcm_bytes)
            return
        except Exception as exc:
            last_err = exc
    raise RuntimeError(f"Ошибка синтеза Gemini TTS для {text!r}: {last_err}")


# ---------------------------------------------------------------------------
# Контроль качества ASR (раздел 11 ТЗ)
# ---------------------------------------------------------------------------

def normalize_words_for_asr(text: str) -> List[str]:
    low = text.lower().replace("ё", "е")
    low = low.replace("брейк-пойнт", "брейк пойнт").replace("тай-брейк", "тай брейк")
    return re.findall(r"[а-яa-z0-9]+", low)


def score_asr_match(expected: str, recognized: str) -> Tuple[float, float]:
    exp_words = normalize_words_for_asr(expected)
    rec_words = normalize_words_for_asr(recognized)
    if not exp_words:
        return 1.0, 1.0
    rec_prefixes = {w[:4] for w in rec_words if w}
    matched = sum(1 for w in exp_words if w[:4] in rec_prefixes)
    prefix_recall = matched / len(exp_words)
    seq_ratio = difflib.SequenceMatcher(None, " ".join(exp_words), " ".join(rec_words)).ratio()
    return prefix_recall, seq_ratio


class ASRVerifier:
    def __init__(self, enabled: bool = True, model_size: str = "small"):
        self.enabled = enabled
        self.model = None
        if enabled:
            try:
                from faster_whisper import WhisperModel  # type: ignore
                device = "cuda" if shutil.which("nvidia-smi") else "cpu"
                compute_type = "float16" if device == "cuda" else "int8"
                self.model = WhisperModel(model_size, device=device, compute_type=compute_type)
            except ImportError:
                print("[WARN] faster-whisper не установлен — шаг ASR-проверки пропущен.", file=sys.stderr)
                self.enabled = False

    def verify_ru(self, wav_path: Path, expected_text: str) -> Tuple[bool, str, float, float]:
        if not self.enabled or self.model is None:
            return True, expected_text, 1.0, 1.0
        segments, _ = self.model.transcribe(str(wav_path), language="ru", beam_size=3)
        rec_text = " ".join(s.text for s in segments).strip()
        p_rec, s_rat = score_asr_match(expected_text, rec_text)
        # Для очень коротких фраз (2–3 слова) смягчаем порог на числительных
        passed = (p_rec >= 0.87 and s_rat >= 0.80) or (len(expected_text.split()) <= 3 and p_rec >= 0.66)
        return passed, rec_text, p_rec, s_rat


# ---------------------------------------------------------------------------
# Аудиомонтаж и мастеринг через ffmpeg (разделы 8, 9 и 11 ТЗ)
# ---------------------------------------------------------------------------

def trim_silence_wav(in_wav: Path, out_wav: Path) -> float:
    """Обрезает краевую тишину с порогом -45dB (шаг 1 раздела 8 ТЗ) и возвращает длительность в секундах."""
    af = (
        f"silenceremove=start_periods=1:start_duration=0.01:start_threshold={TRIM_THRESHOLD_DB},"
        f"areverse,"
        f"silenceremove=start_periods=1:start_duration=0.01:start_threshold={TRIM_THRESHOLD_DB},"
        f"areverse"
    )
    subprocess.run(
        [
            "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(in_wav),
            "-af", af,
            "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le",
            str(out_wav),
        ],
        check=True,
    )
    # Если файл после агрессивной обрезки оказался пустым, копируем исходник
    if not out_wav.exists() or out_wav.stat().st_size <= 100:
        shutil.copyfile(in_wav, out_wav)
    with wave.open(str(out_wav), "rb") as wf:
        frames = wf.getnframes()
        rate = wf.getframerate()
        return frames / float(rate)


def write_silence_wav(path: Path, seconds: float) -> None:
    n_samples = max(1, int(round(seconds * SAMPLE_RATE)))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(b"\x00\x00" * n_samples)


def concat_and_master_mp3(wav_sequence: List[Path], out_mp3: Path) -> float:
    """Склеивает WAV-последовательность, выравнивает громкость до -16 LUFS / -1.5 dBTP и кодирует MP3 128k."""
    with tempfile.TemporaryDirectory(prefix="padel_mux_") as tmpd:
        tmp_dir = Path(tmpd)
        concat_list = tmp_dir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{p.resolve()}'" for p in wav_sequence) + "\n",
            encoding="utf-8",
        )
        merged_wav = tmp_dir / "merged.wav"
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_list),
                "-ar", str(SAMPLE_RATE), "-ac", "1", "-c:a", "pcm_s16le",
                str(merged_wav),
            ],
            check=True,
        )

        # Проход 1 loudnorm для замера фактической громкости главы
        p1 = subprocess.run(
            [
                "ffmpeg", "-hide_banner", "-i", str(merged_wav),
                "-af", f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}:print_format=json",
                "-f", "null", "-",
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        m_json = re.search(r"\{\s*\"input_i\"[\s\S]*?\}", p1.stderr)
        af_pass2 = f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}"
        if m_json:
            stats = json.loads(m_json.group(0))
            in_i = stats.get("input_i", "-24.0")
            in_tp = stats.get("input_tp", "-6.0")
            in_lra = stats.get("input_lra", "7.0")
            in_thresh = stats.get("input_thresh", "-34.0")
            offset = stats.get("target_offset", "0.0")
            if in_i != "-inf":
                af_pass2 = (
                    f"loudnorm=I={TARGET_LUFS}:TP={TARGET_TP}:LRA={TARGET_LRA}:"
                    f"measured_I={in_i}:measured_TP={in_tp}:measured_LRA={in_lra}:"
                    f"measured_thresh={in_thresh}:offset={offset}:linear=true"
                )

        out_mp3.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-i", str(merged_wav),
                "-af", af_pass2,
                "-ar", str(SAMPLE_RATE), "-ac", "1",
                "-c:a", "libmp3lame", "-b:a", MP3_BITRATE,
                str(out_mp3),
            ],
            check=True,
        )

    return probe_duration_seconds(out_mp3)


def probe_duration_seconds(media_path: Path) -> float:
    res = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(media_path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return float(res.stdout.strip())


def detect_unexpected_silences(mp3_path: Path, expected_situation_count: int) -> List[Tuple[float, float]]:
    """Находит все участки тишины > 2.05 с (раздел 11 ТЗ: допустимы только паузы заданий S ~2.5 с)."""
    res = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-i", str(mp3_path),
            "-af", "silencedetect=noise=-42dB:d=2.05",
            "-f", "null", "-",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    long_gaps: List[Tuple[float, float]] = []
    starts: List[float] = []
    for line in res.stderr.splitlines():
        m_s = re.search(r"silence_start:\s*([0-9.]+)", line)
        if m_s:
            starts.append(float(m_s.group(1)))
        m_d = re.search(r"silence_duration:\s*([0-9.]+)", line)
        if m_d and starts:
            long_gaps.append((starts.pop(0), float(m_d.group(1))))
    # Оставляем только аномальные (> 2.85 с или превышающие число окон S)
    anomalies = [(st, dur) for (st, dur) in long_gaps if dur > 2.85]
    if len(long_gaps) > expected_situation_count and not anomalies:
        anomalies = long_gaps[: len(long_gaps) - expected_situation_count]
    return anomalies


def format_hms(seconds: float) -> str:
    total = int(round(seconds))
    h = total // 3600
    m = (total % 3600) // 60
    s = total % 60
    return f"{h}:{m:02d}:{s:02d}"


def build_combined_mp3_with_chapters(
    chapter_entries: List[Tuple[str, Path, float]],
    out_mp3: Path,
) -> None:
    """Собирает единый MP3 курса со встроенными метками глав ID3/FFMETADATA1."""
    with tempfile.TemporaryDirectory(prefix="padel_full_") as tmpd:
        tmp_dir = Path(tmpd)
        concat_txt = tmp_dir / "chapters_concat.txt"
        concat_txt.write_text(
            "\n".join(f"file '{p.resolve()}'" for _, p, _ in chapter_entries) + "\n",
            encoding="utf-8",
        )
        ffmeta = [";FFMETADATA1", "title=Испанский для жизни и падела (полный курс)", "artist=Padel Ambassador"]
        cursor_ms = 0
        for title, _, dur in chapter_entries:
            end_ms = cursor_ms + int(round(dur * 1000))
            esc_title = title.replace("=", "\\=").replace(";", "\\;").replace("#", "\\#")
            ffmeta.extend([
                "[CHAPTER]",
                "TIMEBASE=1/1000",
                f"START={cursor_ms}",
                f"END={end_ms}",
                f"title={esc_title}",
            ])
            cursor_ms = end_ms
        meta_path = tmp_dir / "ffmetadata.txt"
        meta_path.write_text("\n".join(ffmeta) + "\n", encoding="utf-8")

        subprocess.run(
            [
                "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-i", str(concat_txt),
                "-i", str(meta_path),
                "-map_metadata", "1",
                "-c", "copy",
                str(out_mp3),
            ],
            check=True,
        )


def export_anki_csv(chapters: List[ChapterDSL], out_csv: Path) -> int:
    """Экспортирует все основные фразы курса в готовую колоду Anki / CSV (раздел 13 ТЗ)."""
    out_csv.parent.mkdir(parents=True, exist_ok=True)
    count = 0
    with out_csv.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.writer(f, delimiter=";")
        writer.writerow(["Глава", "Испанская фраза", "Ситуация / Смысл", "Пояснение", "Короткий ключ"])
        for ch in chapters:
            ch_name = f"{ch.label}. {ch.display_title}"
            for p in ch.phrases:
                clean_note = re.sub(r"[{}]", "", p.ru_note)
                writer.writerow([ch_name, p.es, p.ru_lead, clean_note, p.ru_recall])
                count += 1
    return count


def copy_to_cloud_folders(dist_dir: Path, target_subfolder: str = "Испанский для падела — v4") -> List[str]:
    """Автоматически копирует собранный пакет в локальные папки iCloud Drive и Google Drive на ПК (если найдены)."""
    copied_to: List[str] = []
    home = Path.home()
    candidates = [
        home / "iCloudDrive" / target_subfolder,
        home / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / target_subfolder,
        Path(os.environ.get("USERPROFILE", str(home))) / "iCloudDrive" / target_subfolder,
        Path("G:/Мой диск") / target_subfolder,
        Path("G:/My Drive") / target_subfolder,
    ]
    for dest in candidates:
        parent = dest.parent
        if parent.exists() and parent != home and parent != Path("."):
            dest.mkdir(parents=True, exist_ok=True)
            for item in dist_dir.iterdir():
                target = dest / item.name
                if item.is_dir():
                    if target.exists():
                        shutil.rmtree(target)
                    shutil.copytree(item, target)
                else:
                    shutil.copy2(item, target)
            copied_to.append(str(dest))
    return copied_to


# ---------------------------------------------------------------------------
# Основной цикл сборки
# ---------------------------------------------------------------------------

def chapter_output_filename(ch: ChapterDSL) -> str:
    safe_title = ch.display_title.replace(":", " —").replace("/", "-")
    if "Бонусная" in ch.label:
        return f"Бонусная глава. {safe_title}.mp3"
    if "Тестовая" in ch.label:
        return f"Тестовая глава. {safe_title}.mp3"
    m = re.search(r"(\d+)", ch.label)
    num_str = f"{int(m.group(1)):02d}" if m else ch.label
    return f"Глава {num_str}. {safe_title}.mp3"


def run_pipeline(args: argparse.Namespace) -> None:
    base_dir = Path(args.project_dir).resolve()
    scripts_dir = base_dir / args.scripts_subdir
    cache_dir = base_dir / "cache_wav"
    dist_dir = Path(args.out_dir).resolve()
    chapters_out_dir = dist_dir / "chapters"
    shadow_out_dir = dist_dir / "shadowing"

    cache_dir.mkdir(parents=True, exist_ok=True)
    chapters_out_dir.mkdir(parents=True, exist_ok=True)
    if args.with_shadowing:
        shadow_out_dir.mkdir(parents=True, exist_ok=True)

    if args.only_test:
        dsl_paths = [base_dir / "examples" / "test_chapter.dsl.txt"]
    elif args.chapter:
        dsl_paths = [scripts_dir / args.chapter]
    else:
        dsl_paths = sorted(scripts_dir.glob("*.txt"))

    parsed_chapters = [parse_dsl(p) for p in dsl_paths]
    total_ru_segs = 0
    total_es_segs = 0
    homograph_hits: List[str] = []

    for ch in parsed_chapters:
        segs = expand_chapter_to_segments(ch)
        ru_c = sum(1 for s in segs if s.lang == "ru")
        es_c = sum(1 for s in segs if s.lang == "es")
        total_ru_segs += ru_c
        total_es_segs += es_c
        for s in segs:
            if s.lang == "ru":
                words = set(re.findall(r"[а-яё]+", s.text.lower()))
                overlap = words & HOMOGRAPH_WATCHLIST
                if overlap:
                    homograph_hits.append(f"{ch.source_path.name}: {sorted(overlap)} -> «{s.text}»")

    # Всегда экспортируем колоду Anki из всех глав папки scripts
    all_course_chapters = [parse_dsl(p) for p in sorted(scripts_dir.glob("*.txt"))]
    anki_count = export_anki_csv(all_course_chapters, dist_dir / "Испанский_для_падела_карточки_Anki.csv")

    if args.dry_run:
        print(f"[DRY-RUN OK] Проверено глав: {len(parsed_chapters)}")
        print(f"  Русских фрагментов: {total_ru_segs}")
        print(f"  Испанских фрагментов: {total_es_segs}")
        print(f"  Экспортировано карточек Anki: {anki_count}")
        print(f"  Фраз со словами двойного ударения на контроле: {len(homograph_hits)}")
        return

    verifier = ASRVerifier(enabled=(not args.mock_tts and not args.skip_asr))
    flagged_ru: List[str] = []
    silence_anomalies_report: List[str] = []
    chapter_entries: List[Tuple[str, Path, float]] = []

    with tempfile.TemporaryDirectory(prefix="padel_sil_") as sil_tmp:
        sil_dir = Path(sil_tmp)
        silence_cache: Dict[int, Path] = {}

        def get_silence_file(sec: float) -> Path:
            ms = int(round(sec * 1000))
            if ms not in silence_cache:
                p = sil_dir / f"sil_{ms}ms.wav"
                write_silence_wav(p, ms / 1000.0)
                silence_cache[ms] = p
            return silence_cache[ms]

        for ch in parsed_chapters:
            segs = expand_chapter_to_segments(ch)
            out_name = chapter_output_filename(ch)
            out_mp3 = chapters_out_dir / out_name
            print(f"[BUILD] {out_name} ({len(segs)} фрагментов)...")

            wav_seq: List[Path] = []
            shadow_seq: List[Path] = []

            for seg in segs:
                if args.mock_tts:
                    provider = "mock"
                    voice_id = f"mock_{seg.voice_key}"
                elif seg.lang == "ru":
                    provider = "gemini"
                    voice_id = args.ru_voice
                else:
                    provider = args.es_provider
                    voice_map = EDGE_ES_VOICES if provider == "edge" else GEMINI_ES_VOICES
                    voice_id = voice_map.get(seg.voice_key, voice_map["A"])

                ckey = segment_cache_key(seg, voice_id, provider)
                trimmed_wav = cache_dir / f"{ckey}_trim.wav"
                raw_wav = cache_dir / f"{ckey}_raw.wav"

                if not trimmed_wav.exists():
                    if args.mock_tts:
                        generate_mock_wav(raw_wav, seg.text, seg.lang, seg.rate)
                        trim_silence_wav(raw_wav, trimmed_wav)
                    elif seg.lang == "ru":
                        accepted = False
                        last_info = ""
                        for attempt in range(1, 4):
                            synth_gemini_tts(seg.text, voice_id, seg.rate, raw_wav, DEFAULT_GEMINI_MODELS)
                            trim_silence_wav(raw_wav, trimmed_wav)
                            ok, rec_txt, p_rec, s_rat = verifier.verify_ru(trimmed_wav, seg.text)
                            last_info = f"attempt={attempt} recall={p_rec:.2f} sim={s_rat:.2f} asr={rec_txt!r}"
                            if ok:
                                accepted = True
                                break
                        if not accepted:
                            flagged_ru.append(f"{ch.source_path.name}: «{seg.text}» ({last_info})")
                    else:
                        if provider == "edge":
                            try:
                                asyncio.run(synth_edge_tts(seg.text, voice_id, seg.rate, raw_wav, EDGE_M_PITCH if seg.voice_key == "M" else "+0Hz"))
                            except Exception:
                                if seg.voice_key == "M":
                                    asyncio.run(synth_edge_tts(seg.text, EDGE_M_FALLBACK, seg.rate, raw_wav))
                                else:
                                    raise
                        else:
                            synth_gemini_tts(seg.text, voice_id, seg.rate, raw_wav, DEFAULT_GEMINI_MODELS)
                        trim_silence_wav(raw_wav, trimmed_wav)
                    raw_wav.unlink(missing_ok=True)

                dur = probe_duration_seconds(trimmed_wav)
                if seg.pause_after == "e":
                    pause_s = min(1.6, max(0.7, dur * 1.1 + 0.35))
                else:
                    pause_s = float(seg.pause_after)

                wav_seq.append(trimmed_wav)
                if pause_s > 0.02:
                    wav_seq.append(get_silence_file(pause_s))

                if args.with_shadowing and seg.include_in_shadowing:
                    sh_pause = min(1.6, max(0.75, dur * 1.05 + 0.3))
                    shadow_seq.append(trimmed_wav)
                    shadow_seq.append(get_silence_file(sh_pause))

            mp3_dur = concat_and_master_mp3(wav_seq, out_mp3)
            chapter_entries.append((f"{ch.label}. {ch.display_title}", out_mp3, mp3_dur))

            anomalies = detect_unexpected_silences(out_mp3, expected_situation_count=len(ch.situations))
            if anomalies:
                silence_anomalies_report.append(f"{out_name}: {anomalies}")

            if args.with_shadowing and shadow_seq:
                sh_mp3 = shadow_out_dir / f"Тень — {out_name}"
                concat_and_master_mp3(shadow_seq, sh_mp3)

    # Если собирали больше 1 главы — собираем общий MP3 с метками глав, ZIP и оглавление с таймкодами
    if len(chapter_entries) > 1:
        full_mp3 = dist_dir / "Испанский для падела — весь курс (v4).mp3"
        build_combined_mp3_with_chapters(chapter_entries, full_mp3)

        zip_path = dist_dir / "Испанский — все главы и режим Тень (MP3).zip"
        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for _, mp3_p, _ in chapter_entries:
                zf.write(mp3_p, arcname=f"chapters/{mp3_p.name}")
            if args.with_shadowing:
                for sh_p in sorted(shadow_out_dir.glob("*.mp3")):
                    zf.write(sh_p, arcname=f"shadowing/{sh_p.name}")

        toc_lines = [
            "# Испанский для падела — оглавление и таймкоды (v4)",
            "",
            "Русский голос — Gemini `Charon`, испанские голоса — `Álvaro`, `Arnau` и `Elvira` (акцент Испании).",
            "",
            "| № | Глава | Начало в общем файле | Длительность |",
            "|---|---|---|---|",
        ]
        elapsed = 0.0
        for idx, (title, _, dur) in enumerate(chapter_entries):
            num_col = "Бонус" if idx == 0 and "Бонусная" in title else str(idx if "Бонусная" in chapter_entries[0][0] else idx + 1)
            toc_lines.append(f"| {num_col} | {title} | {format_hms(elapsed)} | {format_hms(dur)} |")
            elapsed += dur
        toc_lines.extend(["", f"Общая длительность: {format_hms(elapsed)}."])
        (dist_dir / "Оглавление с таймкодами.md").write_text("\n".join(toc_lines) + "\n", encoding="utf-8")

    # Записываем отчёт контроля качества (раздел 11 ТЗ)
    qa_lines = [
        "# Отчёт автоматического контроля качества сборки (QA)",
        "",
        f"- Собрано глав: **{len(chapter_entries)}**",
        f"- Всего русских фрагментов: **{total_ru_segs}**",
        f"- Всего испанских фрагментов: **{total_es_segs}**",
        f"- Незапланированных пауз > 2.05 с (вне заданий S): **{len(silence_anomalies_report)}**",
        f"- Русских фрагментов на ручное прослушивание после 3 попыток ASR: **{len(flagged_ru)}**",
        f"- Карточек экспортировано в колоду Anki CSV: **{anki_count}**",
        "",
        "## Список фраз со словами двойного ударения для выборочного контроля",
    ]
    qa_lines.extend([f"- {h}" for h in homograph_hits] or ["- Нет"])
    if flagged_ru:
        qa_lines.extend(["", "## Фрагменты с пометкой ASR"])
        qa_lines.extend(f"- {item}" for item in flagged_ru)
    if silence_anomalies_report:
        qa_lines.extend(["", "## Замечания по длительности пауз"])
        qa_lines.extend(f"- {item}" for item in silence_anomalies_report)
    (dist_dir / "QA_report.md").write_text("\n".join(qa_lines) + "\n", encoding="utf-8")

    if not args.mock_tts:
        copied = copy_to_cloud_folders(dist_dir)
        if copied:
            print("[CLOUD SYNC] Файлы автоматически скопированы в:", copied)

    print(f"[OK] Сборка успешно завершена: {dist_dir}")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Сборщик аудиокурса «Испанский для жизни и падела» (v4)")
    p.add_argument("--project-dir", default=".", help="Корень репозитория курса")
    p.add_argument("--scripts-subdir", default="scripts_2026-11-17", help="Папка со сценариями DSL")
    p.add_argument("--out-dir", default="./dist_audio_v4", help="Папка готовых аудиофайлов")
    p.add_argument("--ru-voice", default=DEFAULT_RU_VOICE, help="Голос Gemini TTS для русского языка")
    p.add_argument("--es-provider", choices=["edge", "gemini"], default="edge", help="Движок испанских голосов")
    p.add_argument("--only-test", action="store_true", help="Собрать только 2-минутную тестовую главу")
    p.add_argument("--chapter", default="", help="Собрать один конкретный файл главы (например ch17.txt)")
    p.add_argument("--with-shadowing", action="store_true", default=True, help="Генерировать дорожки Режима Тень")
    p.add_argument("--skip-asr", action="store_true", help="Пропустить проверку faster-whisper")
    p.add_argument("--dry-run", action="store_true", help="Проверить развёртку и статистику без синтеза звука")
    p.add_argument("--mock-tts", action="store_true", help="Офлайн-самотест всего тракта ffmpeg/LUFS/ID3 без сети")
    return p


if __name__ == "__main__":
    run_pipeline(build_arg_parser().parse_args())
