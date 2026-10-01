"""Перевод субтитров прямо на компьютере, без интернета.

Переводчик — открытая нейросеть (M2M100 или NLLB) в формате CTranslate2: её один раз
скачивает сама программа (с вашего согласия), дальше всё работает без сети.

Переводятся целые предложения (так перевод точнее), а потом перевод делится на строки
субтитров в тех же местах ролика, где звучит оригинал. Готовые переводы хранятся в проекте:
после каждого выреза заново переводится только то предложение, которое изменилось.
"""

from __future__ import annotations

import json
import logging
import re
import threading
from pathlib import Path

from glimpsy.editor import subtitles as subs

log = logging.getLogger(__name__)

HF = "https://huggingface.co/"
MODELS = {
    # ключ: (папка, репозиторий, файлы, размер в МБ, описание)
    "m2m": ("m2m100_418M-ct2-int8", "jncraton/m2m100_418M-ct2-int8",
            ["config.json", "model.bin", "sentencepiece.bpe.model", "shared_vocabulary.json"], 472,
            "Обычный (472 МБ) — свободная лицензия, можно для любых роликов"),
    "nllb": ("nllb-200-distilled-600M-ct2-int8", "JustFrederik/nllb-200-distilled-600M-ct2-int8",
             ["config.json", "model.bin", "sentencepiece.bpe.model", "shared_vocabulary.txt"], 600,
             "Точнее (600 МБ) — но только для некоммерческих роликов (лицензия CC BY-NC)"),
}
# код языка → (название, код M2M100, код NLLB)
LANGS = {
    "de": ("Deutsch (немецкий)", "__de__", "deu_Latn"),
    "en": ("English (английский)", "__en__", "eng_Latn"),
    "ru": ("Русский", "__ru__", "rus_Cyrl"),
    "uk": ("Українська", "__uk__", "ukr_Cyrl"),
    "es": ("Español (испанский)", "__es__", "spa_Latn"),
    "fr": ("Français (французский)", "__fr__", "fra_Latn"),
    "it": ("Italiano (итальянский)", "__it__", "ita_Latn"),
    "pt": ("Português (португальский)", "__pt__", "por_Latn"),
    "pl": ("Polski (польский)", "__pl__", "pol_Latn"),
    "tr": ("Türkçe (турецкий)", "__tr__", "tur_Latn"),
    "kk": ("Қазақша (казахский)", "__kk__", "kaz_Cyrl"),
}
SENTENCE_GAP_S = 1.2          # такая пауза тоже делит предложения


class TranslateError(RuntimeError):
    pass


def available() -> bool:
    """Есть ли в этой сборке движок перевода."""
    try:
        import ctranslate2  # noqa: F401
        import sentencepiece  # noqa: F401
    except ImportError:
        return False
    return True


def model_dir(key: str) -> Path:
    return subs.models_dir() / MODELS[key][0]


def model_ready(key: str) -> bool:
    d = model_dir(key)
    b = d / "model.bin"
    return all((d / f).exists() for f in MODELS[key][2]) and b.stat().st_size > MODELS[key][3] * 1_000_000 * 0.8


def downloads(key: str) -> list[tuple[str, Path, int]]:
    """Что скачать для модели: [(адрес, куда, МБ)] — только недостающее."""
    folder, repo, files, mb, _d = MODELS[key]
    out = []
    for f in files:
        dest = model_dir(key) / f
        if not dest.exists():
            out.append((f"{HF}{repo}/resolve/main/{f}", dest, mb if f == "model.bin" else 1))
    return out


def lang_code(key: str, lang: str) -> str:
    name, m2m, nllb = LANGS.get(lang, LANGS["ru"])
    return m2m if key == "m2m" else nllb


# ---------------- перевод ----------------

_engines: dict[str, tuple] = {}
_lock = threading.Lock()


def _engine(key: str):
    with _lock:
        if key not in _engines:
            if not model_ready(key):
                raise TranslateError("Переводчик ещё не скачан.")
            import ctranslate2
            import sentencepiece as spm

            from glimpsy.editor.transcript import whisper_threads

            d = model_dir(key)
            sp = spm.SentencePieceProcessor(model_file=str(d / "sentencepiece.bpe.model"))
            tr = ctranslate2.Translator(str(d), device="cpu", compute_type="int8", inter_threads=1,
                                        intra_threads=whisper_threads())
            _engines[key] = (sp, tr)
        return _engines[key]


def translate(texts: list[str], src: str, dst: str, key: str = "m2m", batch: int = 8,
              cancel: threading.Event | None = None) -> list[str]:
    """Перевести предложения (src, dst — коды языков: ru, de…)."""
    if not texts:
        return []
    sp, tr = _engine(key)
    s_tok, d_tok = lang_code(key, src), lang_code(key, dst)
    out: list[str] = []
    for i in range(0, len(texts), batch):
        if cancel is not None and cancel.is_set():
            break
        part = texts[i:i + batch]
        toks = [[s_tok] + sp.encode(t, out_type=str) + ["</s>"] for t in part]
        res = tr.translate_batch(toks, target_prefix=[[d_tok]] * len(part), beam_size=4,
                                 max_decoding_length=256, repetition_penalty=1.1)
        for r in res:
            hyp = [t for t in r.hypotheses[0] if t != d_tok]
            out.append(re.sub(r"\s+([!?.,:;…])", r"\1", sp.decode(hyp)).strip())
    return out


# ---------------- предложения и строки ----------------

def sentences(words: list[tuple[float, float, str]]) -> list[list[tuple[float, float, str]]]:
    """Слова ролика → предложения (по знакам конца предложения и долгим паузам)."""
    out: list[list[tuple[float, float, str]]] = []
    for w in words:
        if not out or re.search(r"[.!?…]$", out[-1][-1][2]) or w[0] - out[-1][-1][1] > SENTENCE_GAP_S:
            out.append([w])
        else:
            out[-1].append(w)
    return out


def split_like(text: str, parts: list[str]) -> list[str]:
    """Разделить перевод на столько же строк, сколько у оригинала, — пропорционально их длине."""
    words = text.split()
    n = len(parts)
    if n <= 1 or not words:
        return [text] + [""] * (n - 1) if n else []
    weights = [max(1, len(p)) for p in parts]
    total_w, total_c = sum(weights), sum(len(w) + 1 for w in words)
    out: list[list[str]] = [[] for _ in parts]
    k, acc, bound = 0, 0, weights[0] / total_w * total_c
    for j, w in enumerate(words):
        left_words, left_parts = len(words) - j, n - k
        # переходим к следующей строке, когда эта набрала свою долю (но каждой строке — хоть слово)
        if k < n - 1 and out[k] and (acc + len(w) / 2 > bound or left_words <= left_parts - 1):
            k += 1
            bound += weights[k] / total_w * total_c
        out[k].append(w)
        acc += len(w) + 1
    return [" ".join(x) for x in out]


class Cache:
    """Готовые переводы предложений проекта: transcript/translations.json."""

    def __init__(self, project_dir: Path) -> None:
        self.file = Path(project_dir) / "transcript" / "translations.json"
        self._data: dict[str, dict[str, str]] | None = None
        self._lock = threading.Lock()

    def _load(self) -> dict[str, dict[str, str]]:
        if self._data is None:
            try:
                self._data = json.loads(self.file.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self._data = {}
        return self._data

    def get(self, key: str, dst: str, text: str) -> str | None:
        with self._lock:
            return self._load().get(f"{key}:{dst}", {}).get(text)

    def put(self, key: str, dst: str, pairs: dict[str, str]) -> None:
        with self._lock:
            data = self._load()
            data.setdefault(f"{key}:{dst}", {}).update(pairs)
            self.file.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.file.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.file)


def translated_cues(words: list[tuple[float, float, str]], cache: Cache, key: str, dst: str,
                    max_chars: int = 42) -> tuple[list[tuple[float, float, str]], list[str]]:
    """Строки субтитров на языке dst: [(начало, конец, текст)] и предложения, которых ещё нет в переводе
    (вместо них пока стоит оригинал)."""
    from glimpsy.editor.transcript import cues

    out: list[tuple[float, float, str]] = []
    missing: list[str] = []
    for sent in sentences(words):
        src = " ".join(w[2] for w in sent)
        lines = cues(sent, max_chars)
        tgt = cache.get(key, dst, src)
        if tgt is None:
            missing.append(src)
            out.extend(lines)
            continue
        for (a, b, _t), text in zip(lines, split_like(tgt, [t for _a, _b, t in lines])):
            if text:
                out.append((a, b, text))
            elif out:                                    # строке не хватило слов — растягиваем прошлую
                pa, _pb, pt = out[-1]
                out[-1] = (pa, b, pt)
    for n in range(len(out) - 1):                        # строки не налезают друг на друга
        a, b, t = out[n]
        if b > out[n + 1][0]:
            out[n] = (a, max(a + 0.1, out[n + 1][0]), t)
    return out, missing
