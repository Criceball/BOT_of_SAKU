"""コードを書くAI（Gemini）。HTML1枚のページを作る。

環境変数:
- GEMINI_API_KEY：Google AI Studio で作ったキー（無いと /code は使えない）
- GEMINI_CODE_MODEL：使うモデル（既定は gemini-3.8-flash）
"""

import logging
import os
import re

import aiohttp

log = logging.getLogger(__name__)

GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_CODE_MODEL = os.getenv("GEMINI_CODE_MODEL", "gemini-3.8-flash")
MAX_OUTPUT_TOKENS = 16000
ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

SYSTEM_PROMPT = """あなたは、ブラウザで動く Web ページを1つのHTMLファイルとして作ります。
- 出力は、```html で囲んだ単一のHTMLファイルだけにしてください（前置きや長い説明は書かない）。
- HTML・CSS・JavaScript はすべてそのファイルの中に書いてください。
- 外部の読み込みは、どうしても必要なとき以外は使わないでください。使うなら cdnjs.cloudflare.com だけにしてください。
- 画面の文字は日本語にしてください。
- 結果が見やすく、操作すると動くものを優先してください。
"""

FIX_PROMPT = """次のHTMLを、指示に従って直してください。直した後の完全なHTMLを、```html で囲んで出力してください。

--- 今のHTML ---
{html}

--- 直してほしいこと ---
{request}
"""


class CodeGenError(Exception):
    """ユーザーに説明できる失敗（長すぎる、キーがない、など）。"""


def _extract_html(text: str) -> str | None:
    match = re.search(r"```html\s*(.*?)```", text, re.S | re.I)
    if match:
        return match.group(1).strip()
    stripped = text.strip()
    if stripped.lower().startswith(("<!doctype", "<html")):
        return stripped
    return None


async def generate_html(request: str, previous_html: str | None = None) -> tuple[str, int]:
    """HTML1枚と、使ったトークン数を返す。previous_html があれば、それを直す。"""
    if not GEMINI_API_KEY:
        raise CodeGenError("GEMINI_API_KEY が設定されていないので、今は作れません。")

    if previous_html:
        user_text = FIX_PROMPT.format(html=previous_html, request=request)
    else:
        user_text = request

    body = {
        "systemInstruction": {"parts": [{"text": SYSTEM_PROMPT}]},
        "contents": [{"role": "user", "parts": [{"text": user_text}]}],
        "generationConfig": {
            "maxOutputTokens": MAX_OUTPUT_TOKENS,
            "temperature": 0.5,
            "thinkingConfig": {"thinkingBudget": 2048},
        },
    }
    url = ENDPOINT.format(model=GEMINI_CODE_MODEL)
    timeout = aiohttp.ClientTimeout(total=120)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.post(url, json=body, headers={"x-goog-api-key": GEMINI_API_KEY}) as resp:
            if resp.status == 429:
                raise CodeGenError("今日の作成回数が上限に近いようです。少し後で試してください。")
            if resp.status >= 400:
                detail = (await resp.text())[:300]
                log.warning("Gemini がエラーを返しました: %s %s", resp.status, detail)
                raise CodeGenError("AIの呼び出しに失敗しました。時間をおいて試してください。")
            data = await resp.json()

    candidates = data.get("candidates") or []
    if not candidates:
        raise CodeGenError("AIから答えがありませんでした。言い方を変えて試してください。")
    candidate = candidates[0]
    if candidate.get("finishReason") == "MAX_TOKENS":
        raise CodeGenError("ページが長すぎて途中で切れました。もう少し小さくして頼んでください。")

    parts = (candidate.get("content") or {}).get("parts") or []
    text = "".join(part.get("text", "") for part in parts)
    html = _extract_html(text)
    if not html:
        raise CodeGenError("HTMLとして取り出せませんでした。言い方を変えて試してください。")

    used = int((data.get("usageMetadata") or {}).get("totalTokenCount", 0))
    return html, used
