"""Сценная озвучка русских фраз (v4.1).

Зачем: у Gemini TTS суточный лимит около 100 запросов на модель, а в курсе более тысячи
русских фраз. Вместо запроса на каждую фразу мы отправляем «сцену» — 10–15 соседних
русских фраз за один запрос, а затем режем получившееся аудио обратно на фразы.

Как режем: faster-whisper даёт отметки времени слов; ожидаемые слова сопоставляются
с распознанными (difflib), границы фраз ставятся в самой тихой точке между последним
словом одной фразы и первым словом следующей. Каждый кусок затем проходит обычную
ASR-проверку сборщика; неудачные фразы уходят в повторную сцену.

Счётчик запросов хранится в файле quota_state.json и сбрасывается по тихоокеанскому
времени (как у Google), поэтому прерванную сборку можно просто запустить снова: готовые
фразы берутся из cache_wav.
"""
from __future__ import annotations

import datetime as _dt
import difflib
import json
import re
import subprocess
import wave
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

import numpy as np

SCENE_SIZE_DEFAULT = 12          # фраз в одной сцене
SCENE_MAX_CHARS = 1800           # потолок по символам, чтобы озвучка не «плыла»
DAILY_BUDGET_DEFAULT = 90        # запас от лимита 100 в сутки

SCENE_PREAMBLE = ""              # при необходимости: короткая инструкция по стилю (проверяется на тесте)
DEBUG_DIR: Optional[Path] = None
SCENE_SEPARATOR = "\n\n"         # пустая строка между фразами даёт естественную паузу


class QuotaStop(RuntimeError):
    """Суточный бюджет запросов исчерпан — сборка останавливается и продолжится завтра."""


# ---------------------------------------------------------------------------
# Счётчик запросов (по тихоокеанскому времени)
# ---------------------------------------------------------------------------

def _pt_today() -> str:
    # Тихоокеанское время зимой UTC-8, летом UTC-7; для счётчика достаточно UTC-8 (запас 1 час)
    return (_dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(hours=8)).strftime("%Y-%m-%d")


class QuotaCounter:
    def __init__(self, path: Path, budget: int):
        self.path = path
        self.budget = budget
        self.day = _pt_today()
        self.used = 0
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data.get("day") == self.day:
                    self.used = int(data.get("used", 0))
            except Exception:
                pass

    def take(self) -> None:
        if self.used >= self.budget:
            raise QuotaStop(f"Бюджет запросов на сегодня исчерпан ({self.used}/{self.budget}). Запусти сборку снова после полуночи по тихоокеанскому времени.")
        self.used += 1
        self._save()

    def mark_exhausted(self) -> None:
        self.used = max(self.used, self.budget)
        self._save()

    def _save(self) -> None:
        self.path.write_text(json.dumps({"day": self.day, "used": self.used, "budget": self.budget}), encoding="utf-8")


# ---------------------------------------------------------------------------
# Нарезка аудио сцены на фразы
# ---------------------------------------------------------------------------

_UNITS = ["ноль","один","два","три","четыре","пять","шесть","семь","восемь","девять","десять","одиннадцать","двенадцать","тринадцать","четырнадцать","пятнадцать","шестнадцать","семнадцать","восемнадцать","девятнадцать"]
_TENS = {2:"двадцать",3:"тридцать",4:"сорок",5:"пятьдесят",6:"шестьдесят",7:"семьдесят",8:"восемьдесят",9:"девяносто"}
_HUND = {1:"сто",2:"двести",3:"триста",4:"четыреста",5:"пятьсот",6:"шестьсот",7:"семьсот",8:"восемьсот",9:"девятьсот"}


def ru_number_words(n: int) -> List[str]:
    if n < 20:
        return [_UNITS[n]]
    if n < 100:
        return [_TENS[n // 10]] + ([_UNITS[n % 10]] if n % 10 else [])
    if n < 1000:
        return [_HUND[n // 100]] + (ru_number_words(n % 100) if n % 100 else [])
    return [str(n)]


def _norm_words(text: str) -> List[str]:
    low = text.lower().replace("ё", "е")
    low = low.replace("брейк-пойнт", "брейк пойнт").replace("тай-брейк", "тай брейк")
    out: List[str] = []
    for tok in re.findall(r"[а-яa-z0-9]+", low):
        if tok.isdigit():
            out.extend(ru_number_words(int(tok)))
        else:
            out.append(tok)
    return out


def _wsim(a: str, b: str) -> float:
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(a=a, b=b).ratio()


def _fuzzy_align(exp: List[str], rec: List[str]) -> Dict[int, int]:
    """Монотонное выравнивание слов (Needleman–Wunsch с нечётким сходством). Возвращает exp_idx -> rec_idx."""
    n, m = len(exp), len(rec)
    GAP = -0.4
    score = np.zeros((n + 1, m + 1), dtype=np.float32)
    back = np.zeros((n + 1, m + 1), dtype=np.int8)  # 0 diag, 1 up (пропуск ожидаемого), 2 left (лишнее распознанное)
    for i in range(1, n + 1):
        score[i, 0] = i * GAP
        back[i, 0] = 1
    for j in range(1, m + 1):
        score[0, j] = j * GAP
        back[0, j] = 2
    for i in range(1, n + 1):
        for j in range(1, m + 1):
            sim = _wsim(exp[i - 1], rec[j - 1])
            d = score[i - 1, j - 1] + (sim * 2 - 1 if sim >= 0.6 else -1.0)
            u = score[i - 1, j] + GAP
            l = score[i, j - 1] + GAP
            best = max(d, u, l)
            score[i, j] = best
            back[i, j] = 0 if best == d else (1 if best == u else 2)
    pairs: Dict[int, int] = {}
    i, j = n, m
    while i > 0 or j > 0:
        mv = back[i, j]
        if i > 0 and j > 0 and mv == 0:
            if _wsim(exp[i - 1], rec[j - 1]) >= 0.6:
                pairs[i - 1] = j - 1
            i, j = i - 1, j - 1
        elif i > 0 and (mv == 1 or j == 0):
            i -= 1
        else:
            j -= 1
    return pairs


def _read_wav_mono(path: Path) -> Tuple[np.ndarray, int]:
    with wave.open(str(path), "rb") as wf:
        rate = wf.getframerate()
        raw = wf.readframes(wf.getnframes())
        ch = wf.getnchannels()
    arr = np.frombuffer(raw, dtype=np.int16).astype(np.float32)
    if ch > 1:
        arr = arr.reshape(-1, ch).mean(axis=1)
    return arr, rate


def _quietest_point(samples: np.ndarray, rate: int, t0: float, t1: float) -> float:
    """Время самой тихой точки (окно 20 мс) в интервале [t0, t1]."""
    a, b = int(max(0, t0) * rate), int(min(len(samples) / rate, t1) * rate)
    if b - a < 2:
        return (t0 + t1) / 2
    win = max(1, int(0.02 * rate))
    seg = np.abs(samples[a:b])
    if len(seg) <= win:
        return (t0 + t1) / 2
    kernel = np.ones(win) / win
    env = np.convolve(seg, kernel, mode="valid")
    idx = int(np.argmin(env)) + win // 2
    return (a + idx) / rate


def split_scene_by_alignment(
    scene_wav: Path,
    phrases: List[str],
    whisper_model,
    out_wavs: List[Path],
) -> List[bool]:
    """Режет scene_wav на len(phrases) файлов. Возвращает флаги «граница найдена надёжно»."""
    n = len(phrases)
    seg_iter, _ = whisper_model.transcribe(str(scene_wav), language="ru", beam_size=3, word_timestamps=True)
    rec: List[Tuple[str, float, float]] = []
    for s in seg_iter:
        for w in (s.words or []):
            for tok in _norm_words(w.word):
                rec.append((tok, float(w.start), float(w.end)))

    exp: List[Tuple[str, int]] = []   # (слово, номер фразы)
    for i, ph in enumerate(phrases):
        for tok in _norm_words(ph):
            exp.append((tok, i))

    pairs = _fuzzy_align([w for w, _ in exp], [w for w, _, _ in rec])
    first_start: Dict[int, float] = {}
    last_end: Dict[int, float] = {}
    for ei, rj in sorted(pairs.items()):
        ph_i = exp[ei][1]
        _, ws, we = rec[rj]
        if ph_i not in first_start:
            first_start[ph_i] = ws
        last_end[ph_i] = we

    samples, rate = _read_wav_mono(scene_wav)
    total = len(samples) / rate
    ok = [True] * n
    bounds: List[float] = [0.0]
    for i in range(n - 1):
        if i in last_end and (i + 1) in first_start and first_start[i + 1] > last_end[i]:
            cut = _quietest_point(samples, rate, last_end[i], first_start[i + 1])
        else:
            ok[i] = ok[i + 1] = False
            cut = bounds[-1]  # граница не найдена: фразы вокруг неё будут перезапрошены
        bounds.append(max(cut, bounds[-1]))
    bounds.append(total)

    pad = 0.04
    for i in range(n):
        a = max(0.0, bounds[i] - (0 if i == 0 else pad))
        b = min(total, bounds[i + 1] + (0 if i == n - 1 else pad))
        if b - a < 0.15:
            ok[i] = False
            continue
        subprocess.run(
            ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", str(scene_wav),
             "-ss", f"{a:.3f}", "-to", f"{b:.3f}", "-ac", "1", "-c:a", "pcm_s16le", str(out_wavs[i])],
            check=True,
        )
    return ok


# ---------------------------------------------------------------------------
# Основной проход: озвучить все недостающие русские фразы сценами
# ---------------------------------------------------------------------------

def chunk_phrases(phrases: List[str], size: int, max_chars: int = SCENE_MAX_CHARS) -> List[List[str]]:
    scenes: List[List[str]] = []
    cur: List[str] = []
    chars = 0
    for p in phrases:
        if cur and (len(cur) >= size or chars + len(p) > max_chars):
            scenes.append(cur)
            cur, chars = [], 0
        cur.append(p)
        chars += len(p)
    if cur:
        scenes.append(cur)
    return scenes


def synth_ru_scenes(
    todo: List[str],
    *,
    cache_path_for: Callable[[str], Tuple[Path, Path]],   # text -> (raw_wav, trimmed_wav)
    synth_scene: Callable[[str, Path], None],             # (text, out_wav) — один запрос к Gemini TTS
    trim: Callable[[Path, Path], float],
    verifier,
    counter: QuotaCounter,
    scene_size: int = SCENE_SIZE_DEFAULT,
    rounds: int = 3,
    log: Callable[[str], None] = print,
) -> List[str]:
    """Озвучивает фразы из todo, кладёт результат в кэш. Возвращает фразы, не прошедшие проверку."""
    pending = list(dict.fromkeys(todo))  # уникальные, порядок сохранён
    flagged: Dict[str, str] = {}
    for rnd in range(1, rounds + 1):
        if not pending:
            break
        scenes = chunk_phrases(pending, scene_size)
        log(f"[SCENES] раунд {rnd}: фраз {len(pending)}, сцен {len(scenes)}, использовано запросов сегодня {counter.used}/{counter.budget}")
        next_pending: List[str] = []
        for si, scene in enumerate(scenes, 1):
            counter.take()
            scene_wav = cache_path_for(scene[0])[0].with_suffix(f".scene{rnd}_{si}.wav")
            text = (SCENE_PREAMBLE + SCENE_SEPARATOR.join(scene)) if SCENE_PREAMBLE else SCENE_SEPARATOR.join(scene)
            synth_scene(text, scene_wav)
            parts = [scene_wav.with_suffix(f".p{k}.wav") for k in range(len(scene))]
            ok_flags = split_scene_by_alignment(scene_wav, scene, verifier.model, parts)
            if DEBUG_DIR is not None:
                DEBUG_DIR.mkdir(parents=True, exist_ok=True)
                import shutil as _sh
                _sh.copyfile(scene_wav, DEBUG_DIR / f"scene_r{rnd}_{si}.wav")
                (DEBUG_DIR / f"scene_r{rnd}_{si}.json").write_text(json.dumps({"phrases": scene, "cut_ok": ok_flags}, ensure_ascii=False, indent=1), encoding="utf-8")
            for phrase, part, ok_cut in zip(scene, parts, ok_flags):
                raw, trimmed = cache_path_for(phrase)
                good = False
                if ok_cut:
                    trim(part, trimmed)
                    good, rec_txt, p_rec, s_rat = verifier.verify_ru(trimmed, phrase)
                    if not good:
                        flagged[phrase] = f"recall={p_rec:.2f} sim={s_rat:.2f} asr={rec_txt!r}"
                        log(f"[SCENES]   ASR-отказ: {phrase!r} -> {rec_txt!r}")
                if not ok_cut:
                    log(f"[SCENES]   граница не найдена: {phrase!r}")
                if good:
                    flagged.pop(phrase, None)
                else:
                    trimmed.unlink(missing_ok=True)
                    next_pending.append(phrase)
                part.unlink(missing_ok=True)
            scene_wav.unlink(missing_ok=True)
            log(f"[SCENES] сцена {si}/{len(scenes)} готова ({len(scene)} фраз), запросов сегодня {counter.used}/{counter.budget}")
        pending = next_pending
    return pending
