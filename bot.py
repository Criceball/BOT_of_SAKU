import asyncio
import io
import os
import random
import re
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone

import discord

from config.messages import get_list, get_msg
from services import bot_drive, codegen, code_store, coding, knowledge, usage_store, web_search
from services.gemini import GeminiError
from discord.ext import commands
from dotenv import load_dotenv
from groq import Groq
from fishaudio import FishAudio

from services.converter import (
    FORMAT_ALIASES,
    MAX_PDF_PAGES,
    UnsupportedConversion,
    convert_file,
    detect_target,
    normalize,
)
from prompts import (
    MAX_CHAT_CHARS,
    build_system_prompt,
    current_persona,
    is_crazy,
    reset_persona,
    sanitize_reply,
    set_persona,
    tts_speed,
    parse_emotions,
    strip_emotions,
    tts_prefix,
    wrap_user_message,
)

# 環境変数の読み込み
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GROQ_API_KEY = os.getenv("GROQ_API_KEY")
FISH_API_KEY = os.getenv("FISH_API_KEY")
# Fish Audioで取得した音声モデルのID（.env に設定してください）
FISH_REFERENCE_ID = os.getenv("FISH_REFERENCE_ID", "")
# 音声生成のモデル。既定は無料枠の s2.1-pro-free（有料に切り替えるなら s2.1-pro）
FISH_MODEL = os.getenv("FISH_MODEL", "s2.1-pro-free")

# 会話に使うGroqのモデル。llama-3.3-70b-versatile は2026年8月に停止されたため、後継を既定にする
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-20b")            # 普段の会話（軽くて安い）
GROQ_DEEP_MODEL = os.getenv("GROQ_DEEP_MODEL", "openai/gpt-oss-120b")  # 「詳しく教えて」や /deep で使う（賢い）
GROQ_SEARCH_MODEL = os.getenv("GROQ_SEARCH_MODEL", "openai/gpt-oss-20b")  # 調べ物（/search）で使う
# メンションでの会話で、チャンネルごとに保持するメッセージ数の上限
HISTORY_LIMIT = 3
# 履歴の返答1件あたりの文字数上限（入力トークンを抑えて、Groqの1分・1日の上限を守るため）
HISTORY_CHAR_LIMIT = 300
# 返答の最大トークン数（長すぎる返答で上限を使い切らないため）
# （gpt-ossは返事の前に思考の部分も書くので、少し余裕を持たせる）
MAX_REPLY_TOKENS = 2048
# 1日に使うトークンの上限。Groqの無料枠（1日20万）より手前で止める
DAILY_TOKEN_BUDGET = 180_000
BUDGET_MESSAGE = get_msg("budget_exhausted")
# 音声にする返答の文字数上限（Fish Audioの料金は文字数で決まるため）
TALK_CHAR_LIMIT = 300
# Discordの1メッセージあたりの文字数上限
DISCORD_MESSAGE_LIMIT = 2000
# Discordの添付ファイルの上限（ブーストなしのサーバー）と、1メッセージの添付数上限
MAX_FILE_BYTES = 25 * 1024 * 1024
MAX_UPLOAD_FILES = 10
# 1人1日の会話回数（メンションの会話と /talk の合計）。日付は日本時間で切り替える
DAILY_LIMIT_PER_USER = 20
JST = timezone(timedelta(hours=9))
QUOTA_MESSAGE = get_msg("quota_reached", limit=DAILY_LIMIT_PER_USER)

# 各クライアントの初期化
groq_client = Groq(api_key=GROQ_API_KEY)
fish_client = FishAudio(api_key=FISH_API_KEY)

# 必要なインテントだけを使う（Intents.all() は特権インテントを全部要求してしまうため）
# メッセージ本文の読み取り（メンション会話）に Message Content Intent が必要
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# チャンネルごとの会話履歴（メンションでの会話用）
chat_history: dict[int, deque] = defaultdict(lambda: deque(maxlen=HISTORY_LIMIT))
MENTION_PATTERN = re.compile(r"<@!?\d+>")
# 利用回数の記録（ユーザーID, 日付）→ 回数。再起動するとリセットされる
daily_usage: dict[tuple[int, str], int] = defaultdict(int)


def take_quota(user_id: int) -> tuple[int, str] | None:
    """利用回数を1つ使う。上限に達していれば None を返す。"""
    today = datetime.now(JST).strftime("%Y-%m-%d")
    # 前日以前の記録は捨てる
    for old_key in [k for k in daily_usage if k[1] != today]:
        del daily_usage[old_key]
    key = (user_id, today)
    if daily_usage[key] >= DAILY_LIMIT_PER_USER:
        return None
    daily_usage[key] += 1
    return key


def refund_quota(key: tuple[int, str] | None) -> None:
    # 失敗した場合は回数を戻す
    if key is not None and daily_usage[key] > 0:
        daily_usage[key] -= 1


# 日ごとの全体のトークン使用量（日付 → トークン数）。再起動するとリセットされる
daily_tokens: dict[str, int] = defaultdict(int)


def today_jst() -> str:
    return datetime.now(JST).strftime("%Y-%m-%d")


def budget_available() -> bool:
    return daily_tokens[today_jst()] < DAILY_TOKEN_BUDGET


async def record_tokens(used: int, user_id: int | None = None) -> None:
    today = today_jst()
    # 前日以前の記録は捨てる
    for old_day in [d for d in daily_tokens if d != today]:
        del daily_tokens[old_day]
    daily_tokens[today] += used
    # 再起動しても消えないように、保存する
    await usage_store.add(today, used, user_id)


DEEP_TRIGGERS = ("詳しく教えて", "詳しく考えて", "詳しく答えて")


def is_deep(text: str) -> bool:
    return any(t in text for t in DEEP_TRIGGERS)


async def generate_reply(messages: list[dict], model: str | None = None) -> tuple[str, int]:
    # Groqの呼び出しは同期APIのため、別スレッドで実行してイベントループを止めない
    completion = await asyncio.to_thread(
        groq_client.chat.completions.create,
        messages=messages,
        model=model or GROQ_MODEL,
        max_completion_tokens=MAX_REPLY_TOKENS,
    )
    used = completion.usage.total_tokens if completion.usage else 0
    return completion.choices[0].message.content, used


# 管理者が /crazy で変える設定（再起動すると元に戻る）。ONの間は、メンションの返事がすべて狂います
force_crazy: bool = False


async def synthesize_speech(text: str, crazy: bool = False) -> bytes:
    # 音声生成も同期APIのため、別スレッドで実行する。狂気モードのときは、タグと速さを変える
    return await asyncio.to_thread(
        fish_client.tts.convert,
        text=tts_prefix(crazy, None) + text,
        reference_id=FISH_REFERENCE_ID,
        format="mp3",
        model=FISH_MODEL,
        speed=tts_speed(crazy),
    )


def cut_at_sentence(text: str, limit: int) -> str:
    """limit文字以内に収める。できるだけ文の区切り（。！？）で切って、音声が途中で途切れないようにする。"""
    if len(text) <= limit:
        return text
    head = text[:limit]
    cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"))
    return head[: cut + 1] if cut > 0 else head


@bot.event
async def on_ready():
    print(f"ログイン成功ですよ〜: {bot.user.name}")


@bot.event
async def on_message(message: discord.Message):
    # ボット自身や他のボットの発言には反応しない
    if message.author.bot:
        return

    # 「ウェブで話そう」と送られたら、リンクとアクセスコードを個人宛てに送る
    if WEB_TALK_TRIGGER in message.content:
        await send_web_talk_dm(message)
        return

    # コマンド（!join など）以外で、ボットがメンションされたら会話する
    if (
        bot.user in message.mentions
        and not message.content.startswith(bot.command_prefix)
    ):
        natural = MENTION_PATTERN.sub("", message.content).strip()
        # 添付ファイルがあるときは、変換として扱う（言葉の反応はしない）
        if message.attachments or not await handle_natural_command(message, natural):
            await handle_mention(message)

    await bot.process_commands(message)


# ウェブで話す画面の案内（リンクとアクセスコード）。コードは || で囲み、Discord で隠す
WEB_TALK_TRIGGER = "ウェブで話そう"


def web_talk_message() -> str | None:
    # リンクは WEB_TALK_URL、なければ Render が自動で入れる RENDER_EXTERNAL_URL を使う
    url = os.getenv("WEB_TALK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
    key = os.getenv("WEB_ACCESS_KEY", "")
    if not url or not key:
        return None
    return f"ウェブで話せますよ〜\n{url}\nアクセスコード：||{key}||"


async def send_web_talk_dm(message: discord.Message) -> None:
    text = web_talk_message()
    if text is None:
        await message.reply(get_msg("web_link_missing"))
        return
    try:
        await message.author.send(text)
        await message.reply("個人宛てにリンクをお送りしましたよ〜")
    except discord.Forbidden:
        await message.reply("個人宛てに送れませんでした。このサーバーからのDMを許可してくださいね〜")


def get_attachments(message: discord.Message) -> list[discord.Attachment]:
    # 自分の添付がなければ、返信先のメッセージの添付を使う
    if message.attachments:
        return list(message.attachments)
    ref = message.reference.resolved if message.reference else None
    if isinstance(ref, discord.Message):
        return list(ref.attachments)
    return []


async def handle_conversion(message: discord.Message, attachment: discord.Attachment, text: str) -> None:
    src = normalize(os.path.splitext(attachment.filename)[1])
    if not src:
        await message.reply("このファイル形式は変換に対応していませんよ〜")
        return

    dst = detect_target(text, src)
    if dst is None:
        supported = ", ".join(sorted(FORMAT_ALIASES))
        await message.reply(f"変換先の形式を指定してくださいね〜（例: 「これをjpegにして」）。\n対応形式: {supported}")
        return

    if attachment.size > MAX_FILE_BYTES:
        await message.reply("ファイルが大きすぎますよ〜（25MBまで）。")
        return

    async with message.channel.typing():
        try:
            data = await attachment.read()
            outputs = await asyncio.to_thread(convert_file, data, src, dst)
        except UnsupportedConversion as e:
            await message.reply(f"{e}")
            return
        except Exception as e:
            await message.reply(f"変換に失敗しましたね〜: `{e}`")
            return

    if any(len(b) > MAX_FILE_BYTES for _, b in outputs):
        await message.reply("変換後のファイルが大きすぎて送信できませんよ〜")
        return

    files = [discord.File(io.BytesIO(b), filename=n) for n, b in outputs[:MAX_UPLOAD_FILES]]
    note = "（先頭ページのみ）" if src == "pdf" and dst != "txt" and len(outputs) == MAX_PDF_PAGES else ""
    await message.reply(f"{src} → {dst} に変換しましたよ〜{note}", files=files)


async def handle_mention(message: discord.Message) -> None:
    # メンション部分を取り除いて本文だけを取り出す
    text = MENTION_PATTERN.sub("", message.content).strip()

    # 添付ファイルがあれば、会話ではなくファイル変換として扱う
    attachments = get_attachments(message)
    if attachments:
        await handle_conversion(message, attachments[0], text)
        return

    if not text:
        await message.reply("何かお話しかけてくださいね！")
        return

    if len(text) > MAX_CHAT_CHARS:
        await message.reply(f"長すぎますね〜（{MAX_CHAT_CHARS}文字までです）。")
        return

    if not budget_available():
        await message.reply(BUDGET_MESSAGE)
        return

    quota_key = take_quota(message.author.id)
    if quota_key is None:
        await message.reply(QUOTA_MESSAGE)
        return

    history = chat_history[message.channel.id]
    # 誰の発言か分かるように名前を付け、タグで囲んでデータとして渡す
    history.append({
        "role": "user",
        "content": wrap_user_message(text, message.author.name),
    })

    async with message.channel.typing():
        try:
            reply_text, used_tokens = await generate_reply([
                {"role": "system", "content": build_system_prompt(extra=await knowledge.lookup(text), crazy=force_crazy)},
                *history,
            ], model=GROQ_DEEP_MODEL if is_deep(text) else GROQ_MODEL)
            await record_tokens(used_tokens, message.author.id)
        except Exception as e:
            history.pop()  # 失敗した発言は履歴に残さない
            refund_quota(quota_key)
            if "429" in str(e):
                # 無料枠の上限（1分・1日）に当たった場合は、分かりやすく案内する
                await message.reply("今は混み合っています。少し待ってから、もう一度話しかけてくださいね〜")
            else:
                await message.reply(f"はい！はい！エラーが発生しました: `{e}`")
            return

    reply_text = strip_emotions(sanitize_reply(reply_text))
    history.append({"role": "assistant", "content": reply_text[:HISTORY_CHAR_LIMIT]})
    # AIの返答で @everyone などが鳴らないように、メンションを無効にする
    files = []
    if wants_voice_reply(text):
        # 「音声で話して」と言われたときは、返事も音声で付ける（失敗したら文字だけ）
        try:
            audio_bytes = await synthesize_speech(cut_at_sentence(reply_text, TALK_CHAR_LIMIT), crazy=force_crazy)
            files.append(discord.File(io.BytesIO(audio_bytes), filename="reply.mp3"))
        except Exception:
            pass
    await message.reply(
        reply_text[:DISCORD_MESSAGE_LIMIT],
        files=files,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# Groq と Fish Audio で返事を作り、音声ファイル（mp3）を添付して送る（/talk）
# （VCの再生は、Renderの音声通信が使えないため使わない）
TALK_INPUT_LIMIT = 300


@bot.tree.command(name="talk", description="AIに話しかけますよ〜（返事と音声が届きますよ〜）")
@discord.app_commands.describe(text="話しかける文章")
async def talk_command(interaction: discord.Interaction, text: str):
    await run_talk(interaction, text, GROQ_MODEL)


@bot.tree.command(name="deep", description="詳しく答えてほしいときに使いますよ〜（賢いモデルで答えますよ〜）")
@discord.app_commands.describe(text="聞きたいこと")
async def deep_command(interaction: discord.Interaction, text: str):
    await run_talk(interaction, text, GROQ_DEEP_MODEL)


async def run_talk(interaction: discord.Interaction, text: str, model: str) -> None:
    if not text:
        await interaction.response.send_message(get_msg("talk_empty"), ephemeral=True)
        return
    if len(text) > TALK_INPUT_LIMIT:
        await interaction.response.send_message(get_msg("talk_too_long", limit=TALK_INPUT_LIMIT), ephemeral=True)
        return
    if not budget_available():
        await interaction.response.send_message(BUDGET_MESSAGE, ephemeral=True)
        return
    quota_key = take_quota(interaction.user.id)
    if quota_key is None:
        await interaction.response.send_message(QUOTA_MESSAGE, ephemeral=True)
        return

    await interaction.response.defer(thinking=True)
    try:
        # ① Groq APIでAIの返答テキストを生成
        raw_reply, used_tokens = await generate_reply([
            {"role": "system", "content": build_system_prompt(extra=await knowledge.lookup(text), crazy=force_crazy)},
            {"role": "user", "content": wrap_user_message(text, interaction.user.name)},
        ], model=model)
        await record_tokens(used_tokens, interaction.user.id)
        reply_text = strip_emotions(sanitize_reply(raw_reply))[:DISCORD_MESSAGE_LIMIT - 10]

        # 音声にする分は短くして、音声の料金を抑える
        audio_bytes = await synthesize_speech(
            cut_at_sentence(reply_text, TALK_CHAR_LIMIT), crazy=force_crazy or is_crazy(text)
        )
        audio_file = discord.File(io.BytesIO(audio_bytes), filename="reply.mp3")
        await interaction.followup.send(
            reply_text,
            file=audio_file,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except Exception as e:
        refund_quota(quota_key)
        await interaction.followup.send(get_msg("error_generic", error=e), ephemeral=True)


PERSONA_MAX_CHARS = 1000


# 性格の確認・変更・リセット（サーバーの管理者だけが使える）。結果は本人にだけ見える。
@bot.tree.command(name="persona", description="AIの性格を確認・変更・リセットしますよ〜（管理者だけ）")
@discord.app_commands.describe(action="確認・変更・リセットのどれか", text="変更するときの性格の文章")
@discord.app_commands.choices(action=[
    discord.app_commands.Choice(name="確認", value="show"),
    discord.app_commands.Choice(name="変更", value="set"),
    discord.app_commands.Choice(name="リセット", value="reset"),
])
async def persona_command(
    interaction: discord.Interaction,
    action: discord.app_commands.Choice[str],
    text: str = "",
):
    if not is_admin(interaction):
        await interaction.response.send_message(get_msg("admin_only_persona"), ephemeral=True)
        return

    if action.value == "show":
        msg = f"現在の性格:\n```\n{current_persona()[:1500]}\n```"
    elif action.value == "set":
        if not text.strip():
            msg = "変更するには text に性格の文章を入れてください。"
        elif len(text) > PERSONA_MAX_CHARS:
            msg = f"長すぎます（{PERSONA_MAX_CHARS}文字までです）。"
        else:
            set_persona(text)
            msg = "性格を変えました。再起動すると元に戻ります。"
    else:
        reset_persona()
        msg = "元の性格に戻しました。"
    await interaction.response.send_message(msg, ephemeral=True)


# 今日の使用量を表示する（/usage）。結果は本人にだけ見える。
@bot.tree.command(name="usage", description="今日のAIの使用量を表示しますよ〜")
async def usage_command(interaction: discord.Interaction):
    today = today_jst()
    stored = await usage_store.totals(today, interaction.user.id)
    mine = daily_usage.get((interaction.user.id, today), 0)
    msg = (
        "今日の使用量\n"
        f"- 全体のトークン：{stored['tokens']:,} / {DAILY_TOKEN_BUDGET:,}（{stored['requests']}回）\n"
        f"- あなたのトークン：{stored['user_tokens']:,}（{stored['user_requests']}回）\n"
        f"- あなたの回数：{mine} / {DAILY_LIMIT_PER_USER}"
    )
    await interaction.response.send_message(msg, ephemeral=True)


# 使えるコマンドの一覧と使い方を表示する（/help）。本人にだけ見える。
@bot.tree.command(name="help", description="コマンドの一覧と使い方を表示しますよ〜")
async def help_command(interaction: discord.Interaction):
    lines = ["コマンドの一覧ですよ〜"]
    for cmd in sorted(bot.tree.get_commands(), key=lambda c: c.name):
        lines.append(f"- `/{cmd.name}`：{cmd.description}")
    lines += [
        "",
        "使い方の例",
        f"- `/talk text:こんにちは`：AIに話しかけますよ〜（{TALK_INPUT_LIMIT}文字まで）。返事は文字と音声で届きますよ〜",
        "- `/say text:…`：文章を、みんなに送りますよ〜。voice:音声ありを選ぶと、音声も付きますよ〜",
        "- `/cheer`：チャンネルのみんなに応援メッセージを送りますよ〜（30秒の間隔があります）",
        "- `/virtual`：ウェブで話す画面のリンクとアクセスコードを見せますよ〜",
        "- `/usage`：今日の使用量と、あなたの残りの回数を見せますよ〜",
        "- `/reset`：このチャンネルの会話の履歴を消しますよ〜",
        "- `/deep`：詳しく答えてほしいときに使いますよ〜（賢いモデルで答えますよ〜）",
        "- `/model`：AIが使っているモデルを見せますよ〜",
        "- `/persona`：AIの性格を見る・変える・元に戻しますよ〜（管理者だけ）",
        "- `/deta`：チャンネルの会話を500件、テキストで取り出しますよ〜（管理者だけ）",
        "",
        "※ この一覧と結果は、あなただけに見えますよ〜",
    ]
    await interaction.response.send_message("\n".join(lines), ephemeral=True)


# 使っているモデルを表示する（/model）。本人にだけ見える。
@bot.tree.command(name="model", description="AIが使っているモデルを表示しますよ〜")
async def model_command(interaction: discord.Interaction):
    msg = (
        "使っているモデル\n"
        f"- 文章：`{GROQ_MODEL}`（Groq）\n"
        f"- 音声：`{FISH_MODEL}`（Fish Audio）"
    )
    await interaction.response.send_message(msg, ephemeral=True)

# 管理者用コマンドを使える役職の名前（カンマ区切りで ADMIN_ROLE_NAMES に変えられる）
ADMIN_ROLE_NAMES = {
    name.strip()
    for name in os.getenv("ADMIN_ROLE_NAMES", "幹部,班長,副班長").split(",")
    if name.strip()
}


def is_admin(interaction: discord.Interaction) -> bool:
    """サーバーの管理者、または ADMIN_ROLE_NAMES の役職を持つ人かどうか。"""
    if interaction.guild is None:
        return False
    member = interaction.user
    if getattr(member, "guild_permissions", None) and member.guild_permissions.administrator:
        return True
    return any(role.name in ADMIN_ROLE_NAMES for role in getattr(member, "roles", []))


ADMIN_ONLY_MESSAGE = get_msg("admin_only")


# このチャンネルの会話の履歴を消す（/reset）。結果は本人にだけ見える。
@bot.tree.command(name="reset", description="このチャンネルの会話の履歴を消しますよ〜")
async def reset_command(interaction: discord.Interaction):
    chat_history[interaction.channel_id].clear()
    await interaction.response.send_message(get_msg("reset_done"), ephemeral=True)


# 応答の速さを確かめる（/ping）
@bot.tree.command(name="ping", description="ボットが動いているか、応答の速さを確かめますよ〜")
async def ping_command(interaction: discord.Interaction):
    ms = round(bot.latency * 1000)
    await interaction.response.send_message(get_msg("pong", ms=ms), ephemeral=True)


# 今日の利用状況（全体）を表示する（/stats。管理者だけ）
@bot.tree.command(name="stats", description="今日の利用状況を表示しますよ〜（管理者だけ）")
async def stats_command(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(ADMIN_ONLY_MESSAGE, ephemeral=True)
        return
    today = datetime.now(JST).strftime("%Y-%m-%d")
    stored = await usage_store.totals(today_jst())
    total_tokens = stored["tokens"]
    requests = stored["requests"]
    users = len({uid for (uid, day), n in daily_usage.items() if day == today and n > 0})
    channels = sum(1 for history in chat_history.values() if history)
    msg = (
        "今日の利用状況（全体）\n"
        f"- トークン：{total_tokens:,} / {DAILY_TOKEN_BUDGET:,}\n"
        f"- 回数：{requests}回（{users}人）\n"
        f"- 会話中のチャンネル：{channels}\n"
        f"- 狂気モード：{'ON' if force_crazy else 'OFF'}"
    )
    await interaction.response.send_message(msg, ephemeral=True)


# Google ドライブの bot フォルダを読み直す（/load）。管理者だけ
@bot.tree.command(name="load", description="Google ドライブの bot フォルダを読み直しますよ〜（管理者だけ）")
async def load_command(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(ADMIN_ONLY_MESSAGE, ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True)
    try:
        sizes = await bot_drive.reload()
    except bot_drive.BotDriveError as e:
        await interaction.followup.send(get_msg("load_failed", error=str(e)), ephemeral=True)
        return
    files = "\n".join(f"- {name}：{size:,} バイト" for name, size in sizes.items()) or "（対象のファイルがありません）"
    await interaction.followup.send(get_msg("load_done", files=files), ephemeral=True)


# 狂気モードを切り替える（/crazy。管理者だけ）
@bot.tree.command(name="crazy", description="狂気モードをON/OFFしますよ〜。ONの間は、メンションの返事がすべて狂いますよ〜（管理者だけ）")
@discord.app_commands.describe(state="ON か OFF")
@discord.app_commands.choices(state=[
    discord.app_commands.Choice(name="ON", value="on"),
    discord.app_commands.Choice(name="OFF", value="off"),
])
async def crazy_command(interaction: discord.Interaction, state: discord.app_commands.Choice[str]):
    global force_crazy
    if not is_admin(interaction):
        await interaction.response.send_message(ADMIN_ONLY_MESSAGE, ephemeral=True)
        return
    force_crazy = state.value == "on"
    msg = "狂気モードを強制的にONにしました。" if force_crazy else "狂気モードをOFFにしました。"
    await interaction.response.send_message(msg, ephemeral=True)


CHEER_MESSAGES = get_list("cheer_messages")
CHEER_COOLDOWN_SECONDS = 30
cheer_last_sent: dict[int, float] = {}


# チャンネルのみんなに応援メッセージを送る（/cheer）。全員に見える。連続で送れないように間隔を空ける。
def cheer_text(channel_id: int) -> str:
    """応援メッセージの文章を返す。続けて送られないように、制限中は案内の文章を返す。"""
    now = time.monotonic()
    last = cheer_last_sent.get(channel_id)
    if last is not None and now - last < CHEER_COOLDOWN_SECONDS:
        wait = int(CHEER_COOLDOWN_SECONDS - (now - last))
        return get_msg("cooldown", wait=wait)
    cheer_last_sent[channel_id] = now
    return f"## {random.choice(CHEER_MESSAGES)}"


@bot.tree.command(name="cheer", description="チャンネルのみんなに応援メッセージを送りますよ〜！")
async def cheer_command(interaction: discord.Interaction):
    text = cheer_text(interaction.channel_id)
    await interaction.response.send_message(
        text,
        ephemeral=not text.startswith("## "),
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ウェブで話す画面のリンクとアクセスコードを、本人にだけ表示する（/virtual）
@bot.tree.command(name="virtual", description="ウェブで話すためのリンクとアクセスコードを表示しますよ〜")
async def virtual_command(interaction: discord.Interaction):
    text = web_talk_message()
    if text is None:
        await interaction.response.send_message(get_msg("web_link_missing"), ephemeral=True)
        return
    await interaction.response.send_message(text, ephemeral=True)


# 入力した文章を、みんなに送る（/say）。管理者も、ほかの人と同じ表示・同じ上限にする（管理者だとわからないように）。
@bot.tree.command(name="say", description="入力した文章を、みんなに送りますよ〜（音声も付けられますよ〜）")
@discord.app_commands.describe(text="送る文章（300文字まで）", voice="音声も作るか（省略するとテキストだけ）")
@discord.app_commands.choices(voice=[
    discord.app_commands.Choice(name="音声あり", value="yes"),
    discord.app_commands.Choice(name="テキストだけ", value="no"),
])
async def say_command(
    interaction: discord.Interaction,
    text: str,
    voice: discord.app_commands.Choice[str] | None = None,
):
    if len(text) > TALK_INPUT_LIMIT:
        await interaction.response.send_message(get_msg("talk_too_long", limit=TALK_INPUT_LIMIT), ephemeral=True)
        return
    if not budget_available():
        await interaction.response.send_message(BUDGET_MESSAGE, ephemeral=True)
        return
    quota_key = take_quota(interaction.user.id)
    if quota_key is None:
        await interaction.response.send_message(QUOTA_MESSAGE, ephemeral=True)
        return
    mentions = discord.AllowedMentions.none()
    with_voice = voice is not None and voice.value == "yes"
    if not with_voice:
        await interaction.response.send_message(text, allowed_mentions=mentions)
        return
    await interaction.response.defer()
    try:
        audio = await synthesize_speech(text)
    except Exception:
        daily_usage[quota_key] -= 1   # 音声が作れなかった分は数えない
        await interaction.followup.send(get_msg("voice_failed"), ephemeral=True)
        return
    await interaction.followup.send(
        text,
        file=discord.File(io.BytesIO(audio), filename="say.mp3"),
        allowed_mentions=mentions,
    )

# 指定したチャンネルの会話履歴を500件取り出して、テキストファイルで本人にだけ渡す（/deta。管理者だけ）
# 1行＝1発言：[日時] 名前: 内容。ボットの発言は含めない。
@bot.tree.command(name="deta", description="指定したチャンネルの会話を500件、テキストで取り出しますよ〜（管理者だけ）")
@discord.app_commands.describe(channel="取り出すチャンネル（省略すると、今のチャンネル）")
async def deta_command(interaction: discord.Interaction, channel: discord.TextChannel | None = None):
    if not is_admin(interaction):
        await interaction.response.send_message(ADMIN_ONLY_MESSAGE, ephemeral=True)
        return
    target = channel or interaction.channel
    perms = target.permissions_for(interaction.guild.me)
    if not (perms.view_channel and perms.read_message_history):
        await interaction.response.send_message(get_msg("no_history_permission"), ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    lines = []
    async for message in target.history(limit=500, oldest_first=True):
        if message.author.bot:
            continue
        stamp = message.created_at.astimezone(JST).strftime("%Y-%m-%d %H:%M")
        text = message.content.replace("\r", "").replace("\n", " / ")
        for attachment in message.attachments:
            text += f" [添付: {attachment.filename}]"
        lines.append(f"[{stamp}] {message.author.display_name}: {text}")
    body = "\n".join(lines) or "（発言がありませんでした）"
    file = discord.File(io.BytesIO(body.encode("utf-8-sig")), filename=f"{target.name}-history.txt")
    await interaction.followup.send(
        get_msg("deta_done", mention=target.mention, count=len(lines)),
        file=file,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )

# 「音声で話して」と言われたときは、返事も音声で送る
VOICE_REPLY_TRIGGERS = ("音声で話して", "声で話して", "音声で答えて", "声で答えて")


def wants_voice_reply(text: str) -> bool:
    return any(t in text for t in VOICE_REPLY_TRIGGERS)


async def start_health_server() -> None:
    # Koyeb などのWebサービスは、PORT で HTTP に応答しないと「異常」と判定される。
    # PORT が設定されているときだけ、ヘルスチェック用の小さなサーバーを立てる。
    port = os.getenv("PORT")
    if not port:
        return

    from aiohttp import web

    app = web.Application()
    app.router.add_get("/", lambda request: web.Response(text="ok"))
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", int(port)).start()


async def setup_hook() -> None:
    # 再起動の後も、今日の使用量を続きから数えられるように、保存された値を読み直す
    today = today_jst()
    daily_tokens[today] = (await usage_store.totals(today))["tokens"]
    # HTTP の応答は main.py（Web画面と同じポート）が受け持つので、ここでは立てない
    # Google ドライブの bot フォルダを読む（失敗しても、ボットは起動する）
    if bot_drive.configured():
        try:
            await bot_drive.reload()
            print("[Drive] bot フォルダを読みました", flush=True)
        except Exception as e:
            print(f"[Drive] 読めませんでした: {e!r}", flush=True)
    # スラッシュコマンドを Discord に登録する（反映まで数分〜1時間かかることがある）
    await bot.tree.sync()


bot.setup_hook = setup_hook

@bot.tree.command(name="code", description="HTMLのページを作りますよ〜（リンクで結果を見られますよ〜）")
@discord.app_commands.describe(
    request="作ってほしいページの内容",
    fix_of="直したいページのID（前に作ったページのリンクの最後の部分。直すときだけ入れますよ〜）",
)
async def build_code_page(user_id: int, request: str, fix_of: str | None) -> tuple[str, discord.File | None]:
    """HTMLのページを作り、返す文章と .html ファイルを返す。失敗したときは理由の文章だけを返す。"""
    try:
        keys = await code_store.take_quota(user_id)
    except code_store.QuotaError as e:
        return f"{e}", None
    if not budget_available():
        await code_store.refund_quota(keys)
        return get_msg("code_budget"), None

    previous = None
    if fix_of:
        previous = await code_store.load_page(fix_of.rstrip("/").split("/")[-1])
        if previous is None:
            await code_store.refund_quota(keys)
            return get_msg("code_fix_missing"), None

    try:
        html_text, used = await codegen.generate_html(request, previous)
    except codegen.CodeGenError as e:
        await code_store.refund_quota(keys)
        return f"{e}", None
    except Exception as e:
        print(f"[/code] 作れませんでした: {e!r}", flush=True)
        await code_store.refund_quota(keys)
        return get_msg("code_failed"), None

    await record_tokens(used, user_id)
    page_id = await code_store.save_page(html_text)
    base = os.getenv("WEB_TALK_URL") or os.getenv("RENDER_EXTERNAL_URL", "")
    link = f"{base}/view/{page_id}" if base else f"（ページID：{page_id}）"
    title = code_store.page_title(html_text)
    file = discord.File(io.BytesIO(html_text.encode("utf-8")), filename=f"{page_id}.html")
    return get_msg("code_done", title=title, link=link, page_id=page_id), file


async def code_command(interaction: discord.Interaction, request: str, fix_of: str | None = None):
    await interaction.response.defer(thinking=True)
    text, file = await build_code_page(interaction.user.id, request, fix_of)
    kwargs = {"file": file} if file else {}
    await interaction.followup.send(text, allowed_mentions=discord.AllowedMentions.none(), **kwargs)


# 言葉で /code・/cheer を呼ぶ（メンションの本文で判断する）
CODE_PATTERN = re.compile(r"(html|HTML).*(作って|作成して|制作して|書いて)|ゲーム.*(作って|作成して|制作して)")
CHEER_TRIGGERS = ("励まして", "応援して", "元気づけて")


def wants_code(text: str) -> bool:
    return bool(CODE_PATTERN.search(text))


def wants_cheer(text: str) -> bool:
    return any(t in text for t in CHEER_TRIGGERS)


# コードの相談（エラーの修正、動きの説明など）の言葉。強い言葉は単独で、弱い言葉は参照や コード があるときだけ使う
CODING_STRONG_WORDS = ("エラー", "error", "Error", "バグ", "直して", "動かない", "コード", "関数", "プログラム", "python", "Python", "javascript", "JavaScript", "discord.py", "html", "HTML", "css", "CSS")
CODING_WEAK_WORDS = ("なんで", "どうして", "なぜ", "説明して", "どういう意味")


def wants_coding_help(text: str, has_reference: bool) -> bool:
    if "```" in text:
        return True
    if any(w in text for w in CODING_STRONG_WORDS):
        return True
    return has_reference and any(w in text for w in CODING_WEAK_WORDS)


async def referenced_text(message: discord.Message) -> str | None:
    """返信先の発言の本文を返す（無ければ None）。"""
    ref = message.reference
    if ref is None or ref.message_id is None:
        return None
    target = ref.resolved if isinstance(ref.resolved, discord.Message) else None
    if target is None:
        try:
            target = await message.channel.fetch_message(ref.message_id)
        except discord.HTTPException:
            return None
    content = target.content.strip()
    return content[:3000] if content else None


async def build_coding_answer(user_id: int, question: str, referenced: str | None) -> tuple[list[str], discord.File | None]:
    """コードの相談に答える。返す文章の一覧と、長いときの全文ファイルを返す。"""
    try:
        keys = await code_store.take_quota(user_id)
    except code_store.QuotaError as e:
        return [f"{e}"], None
    if not budget_available():
        await code_store.refund_quota(keys)
        return [get_msg("code_budget")], None
    try:
        text, used, truncated = await coding.answer(question, referenced)
    except GeminiError as e:
        await code_store.refund_quota(keys)
        return [str(e)], None
    except Exception as e:
        print(f"[コード相談] 答えられませんでした: {e!r}", flush=True)
        await code_store.refund_quota(keys)
        return [get_msg("code_failed")], None

    await record_tokens(used, user_id)
    if truncated:
        text += "\n\n（長すぎて途中で切れましたよ〜。もう少し絞って聞いてくださいね〜）"
    chunks = coding.split_for_discord(text)
    if len(chunks) > 3:
        file = discord.File(io.BytesIO(text.encode("utf-8")), filename="answer.md")
        return [chunks[0], "続きは、ファイルにしましたよ〜"], file
    return chunks, None


async def send_chunks_reply(message: discord.Message, chunks: list[str], file: discord.File | None) -> None:
    for i, chunk in enumerate(chunks):
        kwargs = {"file": file} if (file and i == len(chunks) - 1) else {}
        if i == 0:
            await message.reply(chunk, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        else:
            await message.channel.send(chunk, allowed_mentions=discord.AllowedMentions.none(), **kwargs)


async def handle_natural_command(message: discord.Message, text: str) -> bool:
    """メンションの言葉が /code・/cheer・コードの相談に当たれば処理して True を返す。"""
    if wants_code(text):
        if not text:
            return False
        async with message.channel.typing():
            reply_text, file = await build_code_page(message.author.id, text, None)
        kwargs = {"file": file} if file else {}
        await message.reply(reply_text, allowed_mentions=discord.AllowedMentions.none(), **kwargs)
        return True
    if wants_cheer(text):
        await message.channel.send(cheer_text(message.channel.id), allowed_mentions=discord.AllowedMentions.none())
        return True
    if wants_search(text):
        async with message.channel.typing():
            chunks, file = await run_search(message.author.id, message.channel, text, wants_history(text))
        await send_chunks_reply(message, chunks, file)
        return True
    if not text:
        return False
    reference = await referenced_text(message)
    if wants_coding_help(text, has_reference=reference is not None):
        async with message.channel.typing():
            chunks, file = await build_coding_answer(message.author.id, text, reference)
        await send_chunks_reply(message, chunks, file)
        return True
    return False


# 調べ物（/search と「調べて」などの言葉）。Groq のウェブ検索、Drive の資料、最近の会話を使う
SEARCH_TRIGGERS = ("調べて", "検索して", "ググって", "調べてほしい", "調べてくれ")
HISTORY_WORDS = ("会話", "履歴", "さっき", "前に話", "話した", "今までの")


def wants_search(text: str) -> bool:
    return any(t in text for t in SEARCH_TRIGGERS)


def wants_history(text: str) -> bool:
    return any(w in text for w in HISTORY_WORDS)


async def channel_history_text(channel, limit: int = 20) -> str | None:
    """チャンネルの最近の人の発言を、古い順に並べて返す（ボットの発言は除く）。"""
    lines = []
    try:
        async for m in channel.history(limit=limit):
            if m.author.bot or not m.content:
                continue
            lines.append(f"{m.author.display_name}: {m.content[:200]}")
    except discord.HTTPException:
        return None
    return "\n".join(reversed(lines)) or None


async def run_search(user_id: int, channel, query: str, use_history: bool) -> tuple[list[str], discord.File | None]:
    """調べて、答えの文章の一覧と長いときの全文ファイルを返す。"""
    quota_key = take_quota(user_id)
    if quota_key is None:
        return [QUOTA_MESSAGE], None
    if not budget_available():
        refund_quota(quota_key)
        return [BUDGET_MESSAGE], None
    try:
        knowledge_text = await knowledge.lookup(query)
        history_text = await channel_history_text(channel) if use_history else None
        completion = await asyncio.to_thread(
            groq_client.chat.completions.create,
            messages=web_search.build_messages(query, knowledge_text, history_text),
            model=GROQ_SEARCH_MODEL,
            tools=web_search.GROQ_SEARCH_TOOLS,
            tool_choice="required",
            reasoning_effort="low",
            max_completion_tokens=MAX_REPLY_TOKENS,
        )
        text = web_search.clean_answer(completion.choices[0].message.content)
        used = completion.usage.total_tokens if completion.usage else 0
    except Exception as e:
        print(f"[調べる] 失敗: {e!r}", flush=True)
        refund_quota(quota_key)
        return [get_msg("search_failed")], None

    await record_tokens(used, user_id)
    if not text:
        return [get_msg("search_failed")], None
    chunks = coding.split_for_discord(text)
    if len(chunks) > 3:
        file = discord.File(io.BytesIO(text.encode("utf-8")), filename="search.md")
        return [chunks[0], "続きは、ファイルにしましたよ〜"], file
    return chunks, None


@bot.tree.command(name="search", description="ウェブで調べますよ〜（Drive の資料と、最近の会話も参考にしますよ〜）")
@discord.app_commands.describe(query="調べたいこと")
async def search_command(interaction: discord.Interaction, query: str):
    await interaction.response.defer(thinking=True)
    chunks, file = await run_search(interaction.user.id, interaction.channel, query, wants_history(query))
    for i, chunk in enumerate(chunks):
        kwargs = {"file": file} if (file and i == len(chunks) - 1) else {}
        await interaction.followup.send(chunk, allowed_mentions=discord.AllowedMentions.none(), **kwargs)


# ボットの起動（main.py から import されたときは起動しない）
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
