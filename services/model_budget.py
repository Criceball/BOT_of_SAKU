"""Groq のモデルごとの、1日のトークン上限。ボット（Discord）とウェブ画面（web_talk）の両方で、同じ数を使う。

- 各モデルの上限は、Groq の1日の上限（200,000）の9割。
- 上限に達したモデルは使わず、次の予備のモデルで答える。全部達したら、今日は止める。
- 数は、メモリと Upstash（あれば）に保存する。再起動しても、続きから数える。
"""

import os
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from services import usage_store

JST = timezone(timedelta(hours=9))

# モデルごとの、Groq の1日の上限（トークン）
MODEL_DAILY_TOKENS = {
    "openai/gpt-oss-20b": 200_000,
    "openai/gpt-oss-120b": 200_000,
    "qwen/qwen3.8-27b": 200_000,
}
DEFAULT_MODEL_DAILY_TOKENS = 200_000
LIMIT_RATIO = 0.9

_counts: dict[tuple[str, str], int] = defaultdict(int)


def today() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def model_limit(model: str) -> int:
    return int(MODEL_DAILY_TOKENS.get(model, DEFAULT_MODEL_DAILY_TOKENS) * LIMIT_RATIO)


def used_today(model: str) -> int:
    return _counts[(today(), model)]


def available(model: str) -> bool:
    return _counts[(today(), model)] < model_limit(model)


def any_available(models: list[str]) -> bool:
    return any(available(m) for m in models)


def pick(models: list[str]) -> str | None:
    """上限に達していない、先頭のモデル。無ければ None。"""
    return next((m for m in models if available(m)), None)


async def load_today(models: list[str]) -> None:
    """再起動の後も続きから数えられるように、今日のモデルごとの数を読み直す。"""
    day = today()
    for model in set(models):
        _counts[(day, model)] = await usage_store.model_total(day, model)


async def record(used: int, model: str, user_id: int | None = None) -> None:
    """使ったトークン数を、そのモデルの今日の数に足す（保存もする）。"""
    day = today()
    for old_key in [k for k in _counts if k[0] != day]:
        del _counts[old_key]
    _counts[(day, model)] += used
    await usage_store.add(day, used, user_id, model)
