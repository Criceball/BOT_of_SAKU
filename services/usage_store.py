"""トークンの使用量を、サーバーを再起動しても消えないように保存する。

Upstash Redis（無料枠）を使う。環境変数 UPSTASH_REDIS_REST_URL と UPSTASH_REDIS_REST_TOKEN を
設定すると有効になる。設定がないとき、保存に失敗したときは、メモリだけに残す（再起動で消える）。
"""

import logging
import os

import aiohttp

log = logging.getLogger(__name__)

URL = os.getenv("UPSTASH_REDIS_REST_URL", "").rstrip("/")
TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
TTL_SECONDS = 40 * 86400   # 日付ごとの記録は、40日たったら自動で消える
_memory: dict[str, int] = {}


def enabled() -> bool:
    return bool(URL and TOKEN)


async def _run(commands: list[list]) -> list:
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=5)) as session:
        async with session.post(
            f"{URL}/pipeline",
            json=commands,
            headers={"Authorization": f"Bearer {TOKEN}"},
        ) as resp:
            resp.raise_for_status()
            data = await resp.json()
    return [item.get("result") for item in data]


def _keys(day: str, user_id: int | None) -> list[str]:
    keys = [f"tok:{day}", f"req:{day}"]
    if user_id is not None:
        keys += [f"utok:{day}:{user_id}", f"ureq:{day}:{user_id}"]
    return keys


async def add(day: str, tokens: int, user_id: int | None = None) -> None:
    """その日の合計と、その人の合計に、トークン数と回数を足す。"""
    amounts = [tokens, 1, tokens, 1][: len(_keys(day, user_id))]
    keys = _keys(day, user_id)
    if enabled():
        try:
            commands = []
            for key, amount in zip(keys, amounts):
                commands += [["INCRBY", key, amount], ["EXPIRE", key, TTL_SECONDS]]
            await _run(commands)
            return
        except Exception as e:
            log.warning("使用量を保存できませんでした（メモリに残します）: %s", e)
    for key, amount in zip(keys, amounts):
        _memory[key] = _memory.get(key, 0) + amount


async def totals(day: str, user_id: int | None = None) -> dict[str, int]:
    """その日の使用量を返す。"""
    keys = _keys(day, user_id)
    values: list[int] | None = None
    if enabled():
        try:
            results = await _run([["GET", key] for key in keys])
            values = [int(v or 0) for v in results]
        except Exception as e:
            log.warning("使用量を読めませんでした（メモリの値を使います）: %s", e)
    if values is None:
        values = [_memory.get(key, 0) for key in keys]
    names = ["tokens", "requests", "user_tokens", "user_requests"]
    return dict(zip(names, values + [0] * (4 - len(values))))
