# BOT_of_SAKU

部活の Discord サーバーで動くボットです。AIとの会話、音声の返事、ファイル変換、HTMLページ作成などができます。ウェブで話す画面（`web_talk/`）と、同じプロセスで動きます。

## 使い方

### コマンド（スラッシュコマンド）

| コマンド | 内容 | 見える範囲 |
| --- | --- | --- |
| `/talk <文章>` | AIに話しかけます。返事と音声（mp3）が届きます | 全員 |
| `/deep <文章>` | 詳しく答えてほしいときに使います（賢いモデルで答えます） | 全員 |
| `/say <文章>` | 入力した文章を、みんなに送ります（音声も付けられます） | 全員 |
| `/cheer` | チャンネルのみんなに、応援メッセージを送ります | 全員 |
| `/code <内容> [fix_of]` | HTMLのページを作ります。リンクと .html ファイルが届きます。`fix_of` に前のページのIDを入れると、そのページを直します | 全員 |
| `/virtual` | ウェブで話すためのリンクとアクセスコードを表示します | 本人だけ |
| `/usage` | 今日のAIの使用量を表示します | 本人だけ |
| `/help` | コマンドの一覧と使い方を表示します | 本人だけ |
| `/ping` | ボットが動いているか、応答の速さを確かめます | 本人だけ |
| `/model` | AIが使っているモデルを表示します | 本人だけ |
| `/reset` | このチャンネルの会話の履歴を消します | 本人だけ |
| `/persona` | AIの性格を確認・変更・リセットします（管理者だけ） | 本人だけ |
| `/stats` | 今日の利用状況を表示します（管理者だけ） | 本人だけ |
| `/mood` | AIの声の調子を決めます（管理者だけ） | 本人だけ |
| `/voice` | AIの話す速さを決めます（管理者だけ） | 本人だけ |
| `/crazy` | 狂気モードを強制的にON/OFFします（管理者だけ） | 本人だけ |
| `/deta <チャンネル>` | 指定したチャンネルの会話を500件、テキストファイルで取り出します（管理者だけ） | 本人だけ |

### メンションとDM

- `@ボット名 <文章>`：メンションすると会話できます（チャンネルごとに直近の会話を覚えています）。
- メンションや `/talk` の文章に「詳しく教えて」と入ると、賢いモデルで答えます。
- 「音声で話して」「声で話して」「音声で答えて」「声で答えて」と言うと、返事に音声（mp3）が付きます。
- 「ウェブで話そう」と送ると、ウェブで話す画面のリンクとアクセスコードを、個人宛てのDMで送ります。

### 利用の上限

- 会話（`/talk`、`/deep`、メンション）は、1人1日20回までです。
- ボット全体で、1日のAIのトークン量にも上限（18万トークン）があります。超えると、会話を止めます。
- `/code` は、1人1日20回、全体で1日200回までです（`CODE_PER_USER_DAILY`、`CODE_GLOBAL_DAILY` で変えられます）。
- 上限は日本時間で日付が変わるとリセットされます。
- 使用量は Upstash Redis に保存すると、再起動しても残ります（設定が無いときは、再起動で消えます）。

### ファイル変換

`@ボット名 <変換先の形式>` とファイルを添付すると、形式を変換します。返信でファイルを指定しても変換できます。

- 例：PDFを添付して「これをjpegにして」→ 各ページをJPEGで返します（先頭10ページまで）。

| 変換元 | 変換先 |
| --- | --- |
| jpg / png / webp / gif / bmp | jpg / png / webp / gif / bmp / pdf |
| pdf | jpg / png / webp / gif / bmp（ページごと）、txt（テキスト抽出） |
| mp3 / wav / ogg / mp4 / gif | mp3 / wav / ogg / mp4 / gif |

添付ファイルは25MBまでです。

### /code で作るページ

- 作ったページは、`/view/<ID>` で表示されます。中身は、ブラウザの中で別の領域（sandbox）として動くので、サイトの情報には触れません。
- ページは7日で消えます（Upstash を設定しているとき）。

## 設定（環境変数）

`.env`（手元で動かすとき）や、Render の Environment（デプロイするとき）に設定します。

| 名前 | 必要 | 内容 |
| --- | --- | --- |
| `DISCORD_BOT_TOKEN` | 必須 | Discord のボットのトークン |
| `GROQ_API_KEY` | 必須 | 会話に使う Groq のキー |
| `FISH_API_KEY` | 必須 | 音声（Fish Audio）のキー |
| `FISH_REFERENCE_ID` | 必須 | Fish Audio の音声モデルID |
| `FISH_MODEL` | 任意 | 音声モデル（既定は無料枠の `s2.1-pro-free`） |
| `GROQ_MODEL` | 任意 | 普段の会話のモデル（既定は `openai/gpt-oss-20b`） |
| `GROQ_DEEP_MODEL` | 任意 | 「詳しく教えて」や `/deep` のモデル（既定は `openai/gpt-oss-120b`） |
| `PERSONA` | 任意 | AIの性格（`persona.txt` の中身を貼ります） |
| `CLUB_INFO` | 任意 | 部活の情報（活動内容や予定）。`club.txt` に書いても同じです |
| `KNOWLEDGE_URL` | 任意 | Google ドライブのテキストファイルの共有リンク（読むだけ） |
| `GEMINI_API_KEY` | `/code` に必要 | Google AI Studio のキー |
| `GEMINI_CODE_MODEL` | 任意 | `/code` のモデル（既定は `gemini-3.8-flash`） |
| `CODE_PER_USER_DAILY` | 任意 | `/code` の1人1日の回数（既定 20） |
| `CODE_GLOBAL_DAILY` | 任意 | `/code` の全体の1日の回数（既定 200） |
| `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` | 任意 | 使用量とページを保存する Upstash Redis |
| `WEB_ACCESS_KEY` | ウェブ画面に必要 | ウェブで話す画面の合言葉。無いと画面は止まり、ボットだけ動きます |
| `WEB_DEV_ACCESS_KEY` | 任意 | 開発者用の合言葉（回数制限がかかりません） |
| `WEB_TALK_URL` | 任意 | 画面のURL（無ければ Render が入れる `RENDER_EXTERNAL_URL` を使います） |
| `PACKAGE_URL` | 任意 | ウェブ画面の言葉・動画のパッケージ（Google ドライブの zip） |

## ファイルの場所

```
main.py          起動（ボットとウェブ画面を、同じプロセスで動かします）
bot.py           Discord のコマンドと返信
prompts.py       AIへの指示（性格・部活の情報）。persona.txt・club.txt は、非公開（Git に入れません）
services/        外部との通信と保存（Groq・Gemini・Fish Audio の呼び出し、codegen、code_store、usage_store、knowledge、converter）
config/          返信の文章（messages.json）と、読み込み（messages.py）
web_talk/        ウェブで話す画面（server.py と index.html）
```

返信の文章は、`config/messages.json` にまとめています。文章を直すときは、コードではなく、この JSON を編集します。

## セットアップ（手元で動かす場合）

1. `.env` を作り、必須の環境変数を入れます。
2. Discord Developer Portal で、**Message Content Intent** を有効にします。
3. 音声の変換には ffmpeg が必要です。
4. Python 3.10 以上で、`pip install -r requirements.txt` をしてから、`python main.py` で起動します。

## Render へのデプロイ

Dockerfile でデプロイします。

- **Runtime**：Docker（リポジトリ直下の Dockerfile）
- **Docker Command**：`python main.py`
- **Health Check Path**：`/healthz`
- 環境変数（Secret）：上の表の必須のもの。ウェブ画面も使うなら `WEB_ACCESS_KEY` を入れます。

ログに「[HTTP] ポート … で待ち受けます」と出て、ボットが「ログイン」した状態になれば、動いています。

注意：
- 無料枠は一定時間アクセスがないとスリープすることがあります。その間、Discord の接続も切れます。
- 音声の通話（ボイスチャンネル）の送信は UDP を使います。Render の無料枠では、ボイスチャンネルでの音声は使えません。
- `persona.txt`・`club.txt`・`.env` は Git に入れない設定です（`.gitignore`、`.dockerignore`）。
