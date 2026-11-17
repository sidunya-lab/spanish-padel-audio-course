# Испанский для жизни и падела — Аудиокурс v4 (Бонус + 26 глав + Режим Тень)

Полностью проверенная, расширенная и готовая к автоматической сборке версия курса для **Padel Ambassador**.

## Что внутри

- `scripts_2026-11-17/` — выверенные DSL-сценарии 27 глав (`ch00_bonus.txt` + `ch01.txt`..`ch26.txt`), 0 ошибок по автовалидатору ТЗ.
- `examples/test_chapter.dsl.txt` — эталонная 2-минутная тестовая глава.
- `build_course.py` — полный Python-сборщик аудио по разделам 5–11 и 13 ТЗ (Gemini TTS `Charon` + Microsoft `Álvaro` / `Arnau` / `Elvira`, SHA-256 кэш, `faster-whisper` QA, `ffmpeg` `-45dB` trim, `-16 LUFS`, ID3 главы, дорожки «Режим Тень», автокопирование в iCloud Drive и Google Drive).
- `validate_dsl.py` — строгий линтер сценариев на соответствие всем правилам ТЗ.
- `.github/workflows/build_audio.yml` — облачная автосборка курса через GitHub Actions.
- `run_test_chapter_windows.bat` и `run_build_windows.bat` — запуск в 1 клик на Windows ПК (с поддержкой CUDA для RTX 3080 и автокопированием в `iCloudDrive`).
- `content/course_text_v4_2026-11-17.md` — полный читаемый учебник всех 27 глав с источниками.
- `content/Испанский_для_падела_карточки_Anki.csv` — готовая колода из 218 карточек для интервального повторения.
- `content/changelog_and_audit_v4_2026-11-17.md` — полный список исправленных ошибок и улучшений.

## Быстрый старт

### Вариант 1: Windows ПК (рекомендуется при наличии GPU NVIDIA + папки iCloud Drive)
1. Запусти `run_test_chapter_windows.bat` и введи `GEMINI_API_KEY` — через 1 минуту в `dist_test_chapter/chapters/` будет готова тестовая глава.
2. Запусти `run_build_windows.bat` — скрипт озвучит все 27 глав + дорожки «Режим Тень», проверит каждую русскую фразу через `faster-whisper`, соберёт единый MP3 с таймкодами и сам скопирует файлы в твой `iCloudDrive\Испанский для падела — v4` и `Google Drive`.

### Вариант 2: GitHub Actions в облаке
1. Загрузи файлы в репозиторий `sidunya-lab/spanish-padel-audio-course`.
2. В `Settings → Secrets and variables → Actions` добавь секрет `GEMINI_API_KEY`.
3. Во вкладке `Actions` запусти `Build Spanish Padel Audio Course (v4)` (сначала `test_only`, затем `full_course`).
