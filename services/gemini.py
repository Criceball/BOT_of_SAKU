"""Gemini API の呼び出し（generateContent）を1か所にまとめる。

環境変数:
- GEMINI_API_KEY：Google AI Studio のキー
- GEMINI_CODE_MODEL：使うモデル（既定は gemini-3.8-flash）
"""

import logging
import os

import aiohttp

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_CODE_MODEL = os.getenv("GEMINI_CODE_MODEL", "gemini-3.8-flash")
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"


class GeminiError(Exception):
    """ユーザーに説明できる失敗。"""


async def generate(
    system_prompt: str,
    user_text: str,
    *,
    max_output_tokens: int,
    thinking_budget: int,
    temperature: float = 0.5,
) -> tuple[str, int, bool]:
    """本文・使ったトークン数・途中で切れたかどうかを返す。"""
    if not GEMINI_API_KEY:
        raise GeminiError("GEMINI_API_KEY が設定されていないので、今は使えません。")

    body = {
        "systemInstruction": {"parts": [{"text": system_prompt}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "maxOutputTokens": max_output_tokens,
            "temperature": temperature,
            "thinkingConfig": {"thinkingBudget": thinking_budget},
        },
    }
    url = ENDPOINT.format(model=GEMINI_CODE_MODEL)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}) as resp:
            if resp.status == 429:
                raise GeminiError("今は混み合っています。少し後で試してください。")
            if resp.status >= 400:
                detail = (await resp.text())[:300]
                log.warning("Gemini がエラーを返しました: %s %s", resp.status, detail)
                raise GeminiError("AIの呼び出しに失敗しました。時間をおいて試してください。")
            data = await resp.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise GeminiError("AIから答えがありませんでした。言い方を変えて試してください。")
    candidate = candidates[0]
    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts if not part.get("thought"))
    truncated = candidate.get("finishReason") == "MAX_TOKENS"
    used = int((data.get("usageMetadata") or {}).get("totalTokenCount", 0))
    return text, used, truncated
