# BOT_of_SAKU

## 機能

- `!join` / `!leave`：ボイスチャンネルへの参加・退出
- `!talk <文章>`：AIが返答し、音声でボイスチャンネルに読み上げ
- `@ボット名 <文章>`：メンションすると会話できます（チャンネルごとに直近の会話を記憶）

## セットアップ

1. `.env` を作成し、以下を設定します。
   - `DISCORD_BOT_TOKEN`
   - `GROQ_API_KEY`
   - `FISH_API_KEY`
   - `FISH_REFERENCE_ID`（Fish Audioの音声モデルID）
2. Discord Developer Portal で Message Content Intent を有効にします。
3. 音声再生には ffmpeg が必要です。
4. 依存パッケージ（discord.py、python-dotenv、groq、Fish Audio SDK）をインストールし、`python bot.py` で起動します。
