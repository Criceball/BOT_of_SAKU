"""/search のための、会話の記録（/deta フォルダの .txt）の抜き出し。

考え方：
1. 行から、日時と名前を外して、本文だけで比べる。
2. 形態素解析（fugashi）で、名詞・動詞・形容詞などの単語に分ける（助詞や記号は使わない）。
3. 質問の単語が、行にどれだけ含まれるかで点数をつける。どこにでもある単語（「です」など）は点が低い。
4. 関係の強い行（質問の単語の半分以上を含む行）だけを選び、前後の行を付けて渡す。
抜き出した文字は、AI に渡す前に伏せ字の処理（services/redact.py）を通す。
"""

import math
import re
import time
from collections import Counter

from services import deta_drive
from services.redact import mask_sensitive

CACHE_SECONDS = 300
MAX_HITS = 30          # 1回の検索で拾う行の上限
CONTEXT_LINES = 2      # 当たった行の前後に付ける行数
MAX_CHARS = 6000       # AI に渡す文字数の上限
MIN_COVERAGE = 0.6     # 質問の単語の何割が行に含まれていれば、関係ありとみなすか

# 行の先頭の「[日時] 名前: 」を外す
LINE_PREFIX = re.compile(r"^\[[^\]]*\]\s*[^:]*:\s*")
# 単語として使う品詞（助詞・助動詞・記号などは使わない）
CONTENT_POS = {"名詞", "動詞", "形容詞", "形状詞", "副詞"}

try:
    import fugashi
    _tagger = fugashi.Tagger()
except Exception:  # fugashi / unidic-lite が無い環境では、記号と助詞で区切るだけにする
    _tagger = None
FALLBACK_SPLIT = re.compile(r"[\s、。，．,.!！?？「」『』（）()]+|について|とは|って|ですか|ありますか|教えて|何|誰|いつ|どこ|の|は|が|を|に|で|と|も|か|て|な|よ|ね")
_cache: dict[str, list[str]] = {}
_cache_at: float = 0.0
_records: list[dict] = []  # 行ごとの情報：{"file", "index", "text", "terms"}


def _terms(text: str) -> set[str]:
    """本文から、比べるための単語の集合を作る（小文字にそろえる）。"""
    terms: set[str] = set()
    if _tagger is None:
        for part in FALLBACK_SPLIT.split(text):
            if part.strip() and not re.fullmatch(r"[\W_]+", part):
                terms.add(part.strip().lower())
        return terms
    for word in _tagger(text):
        pos = word.feature.pos1 if hasattr(word.feature, "pos1") else ""
        if pos and pos not in CONTENT_POS:
            continue
        surface = word.surface.strip().lower()
        if len(surface) >= 1 and not re.fullmatch(r"[\W_]+", surface):
            terms.add(surface)
    return terms


async def _load() -> None:
    """/deta の記録を読んで、行ごとに単語を作る（5分の間は、使い回す）。"""
    global _cache, _cache_at, _records
    if time.time() - _cache_at < CACHE_SECONDS and _records:
        return
    files = await deta_drive.read_history_files()
    _cache = {name: data.decode("utf-8-sig").splitlines() for name, data in files.items()}
    _records = []
    for name, lines in _cache.items():
        for i, line in enumerate(lines):
            body = LINE_PREFIX.sub("", line)
            _records.append({"file": name, "index": i, "text": body, "terms": _terms(body)})
    _cache_at = time.time()


def _select(query: str) -> str | None:
    query_terms = _terms(query)
    if not query_terms or not _records:
        return None
    total = len(_records)
    df: Counter = Counter()
    for rec in _records:
        df.update(rec["terms"] & query_terms)
    # 多くの行にある単語ほど軽く数える（idf）
    weight = {t: math.log((total + 1) / (df[t] + 0.5)) for t in query_terms}
    need = max(1, math.ceil(len(query_terms) * MIN_COVERAGE))

    scored = []
    for rec in _records:
        matched = rec["terms"] & query_terms
        if len(matched) < need:
            continue
        scored.append((sum(weight[t] for t in matched), rec))
    if not scored:
        return None
    scored.sort(key=lambda x: x[0], reverse=True)

    chosen: dict[str, set[int]] = {}
    for _, rec in scored[:MAX_HITS]:
        span = chosen.setdefault(rec["file"], set())
        lines = _cache[rec["file"]]
        for j in range(max(0, rec["index"] - CONTEXT_LINES), min(len(lines), rec["index"] + CONTEXT_LINES + 1)):
            span.add(j)

    parts: list[str] = []
    used = 0
    for name in sorted(chosen):
        block = [f"[{name}]"] + [_cache[name][j] for j in sorted(chosen[name])]
        text = mask_sensitive("\n".join(block))
        if used + len(text) > MAX_CHARS:
            break
        parts.append(text)
        used += len(text)
    return "\n\n".join(parts) or None


async def excerpts(query: str) -> str | None:
    """質問に関係する、会話の記録の抜き出し。関係するものが無ければ None（ウェブで調べる）。"""
    if not deta_drive.configured():
        return None
    try:
        await _load()
    except Exception as e:  # 記録が読めなくても、/search は答えられるように
        print(f"[調べる] /deta の記録を読めませんでした: {e!r}", flush=True)
        return None
    return _select(query)
