# discord-translate

日本語・英語・韓国語のチャンネルを紐付けて、どれかへの発言を LLM で翻訳し、ほかのチャンネルへ Webhook で投稿する Discord Bot です。1サーバーでの利用を前提にしています。

## 準備

1. Discord Developer Portal で Bot を作り、**Message Content Intent** をオンにする。
2. Bot を `bot` と `applications.commands` のスコープで招待し、次の権限を与える：チャンネルの管理、Webhook の管理、メッセージを見る・送る、メッセージ履歴を読む、リアクションの追加。
3. `.env.example` を `.env` にコピーして値を入れる。

| キー | 必須 | 既定値 |
|---|---|---|
| `DISCORD_BOT_TOKEN` | ○ | |
| `LLM_API_KEY` | ○ | |
| `LLM_MODEL` | ○ | |
| `LLM_BASE_URL` | | `https://api.openai.com/v1` |
| `LLM_TIMEOUT_SECONDS` | | `30` |
| `LLM_MAX_RETRIES` | | `2` |
| `PAIRS_PATH` | | `pairs.json` |
| `BOTS_PATH` | | `PAIRS_PATH` と同じディレクトリの `bots.json` |
| `JA_CHANNEL_ID` / `EN_CHANNEL_ID` | | 両方あれば起動時にそのペアを `pairs.json` に追加する |

## 起動と停止

```
uv sync
uv run main.py
```

止めるときは `Ctrl+C`。

## Docker Compose で運用する

GitHub Actions は `main` と pull request でコンテナをビルドし、`v*` タグを push すると GHCR に同名タグのイメージを公開します。現在の Compose は `v0.0.2` を使用します。

ホストに `compose.yml` と `.env.example` を配置し、次のように準備します。

```bash
cp .env.example .env
mkdir -p data
printf '[]\n' > data/pairs.json
chmod 600 .env
docker compose config --quiet
```

`.env` の `DISCORD_BOT_TOKEN`、`LLM_API_KEY`、`LLM_MODEL` を実際の値に変えてから起動します。

```bash
docker compose pull
docker compose up -d
docker compose logs -f discord-translate
```

生成・更新される JSON はホストの `data/pairs.json` と `data/bots.json` に保存します。以前の環境から移行する場合は、**起動前**に既存の `pairs.json` を `data/pairs.json` へコピーするか、内容を貼り付けてください。既存ファイルがあれば `printf` の行は実行しません。`bots.json` は `/tr-bot add` の初回実行時に作られます。Bot は保存時に同じディレクトリへ一時ファイルを作って置き換えるため、`data` はホスト上で実行ユーザー（UID 1000）が書き込めるようにします。

## 翻訳チャンネルを作る

日本語・英語・韓国語のチャンネルを1組（クラスタ）にして、どのチャンネルの発言も、ほかの全チャンネルへそれぞれの言語に翻訳して投稿する。翻訳は発言の言語から各言語へ LLM が直接行う。

日本語のチャンネルで `/create-tr-channel` を実行する（「チャンネルの管理」の権限が必要）。

- 実行したチャンネルは `<元の名前>-jp` に改名される。
- チャンネル名を訳した `<英訳した名前>-en` と `<韓国語訳した名前>-ko` のチャンネルが作られ、`-jp`・`-en`・`-ko` の順に並ぶ。カテゴリと権限は元のチャンネルと同じになる。
- 作成前の発言はさかのぼって翻訳しない。
- クラスタは `pairs.json` に `[{"JP": "<ID>", "EN": "<ID>", "KO": "<ID>"}, ...]` の形で保存される。
- 起動時に日本語と英語だけのクラスタがあれば、韓国語のチャンネルを作って英語のチャンネルのすぐ下に置く。
- クラスタのチャンネルを削除すると、そのチャンネルは `pairs.json` から外れる。韓国語のチャンネルを消した場合は、次の起動時に作り直される。

## Bot の発言を翻訳する

Bot の発言は通常翻訳しないが、`/tr-bot` で指定した Bot の発言は翻訳する（「チャンネルの管理」の権限が必要）。

- `/tr-bot add bot:<Bot>`：その Bot の発言を翻訳するようにする。スラッシュコマンドへの応答も翻訳する。
- `/tr-bot remove bot:<Bot>`：その Bot の発言を翻訳しないようにする。
- `/tr-bot list`：翻訳する Bot の一覧を表示する。
- この Bot 自身は、訳文を翻訳し続けてしまうため指定できない。ほかの翻訳 Bot を指定すると互いに翻訳し合うおそれがあるので注意する。
- 翻訳するのは本文と添付ファイルの URL だけで、埋め込み（embed）は翻訳しない。Webhook からの投稿は翻訳しない。
- 指定した Bot は `bots.json` に `["<ID>", ...]` の形で保存される。

## 動き

- 発言を翻訳し、投稿者の名前とアイコンで紐付いたチャンネルに投稿する。返信先へのリンクと添付ファイルの URL も付ける。
- 原文の編集・削除を訳文に反映する。
- 翻訳に失敗すると原文に ⚠️ を付ける。
- 原文と訳文の対応はメモリにだけ持つため、再起動より前の発言の編集・削除は反映されない。
- 発言の本文とチャンネル名は `LLM_BASE_URL` の LLM ホストに送られる。
