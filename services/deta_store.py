"""公開チャンネルの会話の記録（/deta の自動保存）を置く場所。

- Upstash Redis が設定されていれば、そこに保存する（再起動しても残る）。
- 設定が無いときは、ボットのメモリに置く（再起動すると消える）。
"""

import logging

from services import usage_store

log = logging.getLogger(__name__)

INDEX_KEY = "deta:index"
_memory: dict[str, str] = {}


def _key(name: str) -> str:
    return f"deta:{name}"


async def save(name: str, text: str) -> None:
    """チャンネルの記録（ファイル名 → 本文）を保存する。同じ名前は上書き。"""
    if usage_store.enabled():
        try:
            await usage_store._run([["SET", _key(name), text], ["SADD", INDEX_KEY, name]])
            return
        except Exception as e:
            log.warning("[/deta] Upstash に保存できませんでした（メモリに残します）: %s", e)
    _memory[name] = text


async def read_all() -> dict[str, str]:
    """保存されている記録を、ファイル名 → 本文 の辞書で返す。"""
    if usage_store.enabled():
        try:
            names = (await usage_store._run([["SMEMBERS", INDEX_KEY]]))[0] or []
            if not names:
                return {}
            values = (await usage_store._run([["MGET", *[_key(n) for n in names]]]))[0] or []
            return {n: v for n, v in zip(names, values) if v}
        except Exception as e:
            log.warning("[/deta] Upstash から読めませんでした: %s", e)
    return dict(_memory)
