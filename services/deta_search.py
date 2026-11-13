"""/search のための、/deta フォルダの会話の抜き出し。

考え方：ファイル全体は AI に渡さない。質問の言葉を含む行を機械的に探し、その前後の行だけを渡す。
抜き出した文字は、Groq に送る前に伏せ字の処理（services/redact.py）を通す。
"""

import re
import time

from services import deta_drive
from services.redact import mask_sensitive

CACHE_SECONDS = 300
MAX_HITS = 30          # 1回の検索で拾う行の上限
CONTEXT_LINES = 2      # 当たった行の前後に付ける行数
MAX_CHARS = 6000       # AI に渡す文字数の上限

STOP_TOKENS = {
    "について", "とは", "って", "教えて", "ですか", "ありますか", "何", "誰", "いつ", "どこ",
    "の", "は", "が", "を", "に", "で", "と", "も", "か", "て", "な", "よ", "ね", "?", "？",
}

_cache: dict[str, list[str]] = {}
_cache_at: float = 0.0


async def _load() -> dict[str, list[str]]:
    """/deta のファイルを読む（5分の間は、読んだ内容を使う）。"""
    global _cache, _cache_at
    if time.time() - _cache_at < CACHE_SECONDS:
        return _cache
    files = await deta_drive.read_history_files()
    _cache = {name: data.decode("utf-8-sig").splitlines() for name, data in files.items()}
    _cache_at = time.time()
    return _cache


def _keywords(query: str) -> set[str]:
    """質問から、探す言葉（2文字の組み）を作る。日本語は空白で区切られないので、2文字ずつにする。"""
    words = re.split(r"[\s、。，．,.!！?？「」『』（）()]+", query)
    grams: set[str] = set()
    for w in words:
        if not w or w in STOP_TOKENS:
            continue
        if len(w) <= 2:
            grams.add(w)
        else:
            grams.update(w[i:i + 2] for i in range(len(w) - 1))
    return grams


def _select(files: dict[str, list[str]], query: str) -> str | None:
    grams = _keywords(query)
    if not grams:
        return None
    hits: list[tuple[int, str, int]] = []  # (点数, ファイル名, 行の番号)
    for name, lines in files.items():
        for i, line in enumerate(lines):
            score = sum(1 for g in grams if g in line)
            if score:
                hits.append((score, name, i))
    if not hits:
        return None
    # 点数の高い行を優先し、同じ点数なら新しい行（ファイルの後ろ）を優先する
    hits.sort(key=lambda h: (h[0], h[2]), reverse=True)
    chosen: dict[str, set[int]] = {}
    for _, name, i in hits[:MAX_HITS]:
        span = chosen.setdefault(name, set())
        for j in range(max(0, i - CONTEXT_LINES), min(len(files[name]), i + CONTEXT_LINES + 1)):
            span.add(j)

    parts: list[str] = []
    total = 0
    for name in sorted(chosen):
        block = [f"[{name}]"]
        lines = files[name]
        for j in sorted(chosen[name]):
            block.append(lines[j])
        text = mask_sensitive("\n".join(block))
        if total + len(text) > MAX_CHARS:
            break
        parts.append(text)
        total += len(text)
    return "\n\n".join(parts) or None


async def excerpts(query: str) -> str | None:
    """質問に関係する、/deta の会話の抜き出し。無ければ None。"""
    if not deta_drive.configured():
        return None
    try:
        files = await _load()
    except Exception as e:  # /deta が読めなくても、/search は答えられるように
        print(f"[調べる] /deta を読めませんでした: {e!r}", flush=True)
        return None
    if not files:
        return None
    return _select(files, query)
