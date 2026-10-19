# claude-bridge

Claude CLI と Antigravity CLI を、ローカルの OpenAI Responses API 互換エンドポイントから使うためのブリッジです。Codex から指定したモデル名に応じて CLI を呼び出します。Python 標準ライブラリだけで動きます。

## 準備

- Python 3.11 以上
- インストールとログインを済ませた Claude CLI（`claude -p "ping"` で確認）
- Gemini も使う場合のみ、インストールとログインを済ませた Antigravity CLI（`agy` 1.1.15 以降）

ブリッジ用の API キーは不要です。Claude と Antigravity は各 CLI のログイン情報を使います。

## 起動

macOS / Linux:

```bash
cd claude-bridge
python3 -m claude_bridge --working-dir /path/to/project
```

Windows（PowerShell）:

```powershell
cd claude-bridge
py -3 -m claude_bridge --working-dir (Get-Location).Path
```

`--working-dir` は CLI に作業を許可するプロジェクトを指定します。省略すると起動時のディレクトリを使います。既定の接続先は `http://127.0.0.1:8787` です。

動作確認: macOS / Linux は `curl http://127.0.0.1:8787/health`、Windows は `Invoke-RestMethod http://127.0.0.1:8787/health`。

Claude だけを使う場合は `--antigravity-models ""` を付けてください。Antigravity CLI がなくても Claude のリクエストは動きます。

## Codex から使う

`~/.codex/config.toml` に追加します。

```toml
[model_providers.claude_bridge]
name = "Claude CLI bridge"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
requires_openai_auth = true

[profiles.claude]
model_provider = "claude_bridge"
model = "opus"
```

CLI では `codex --profile claude` で使えます。Codex デスクトップアプリで既定の接続先にする場合は、同じファイルの先頭（`[` で始まる設定より前）に次を追加し、アプリを再起動します。

```toml
model_provider = "claude_bridge"
model = "opus"
```

`requires_openai_auth` は、Codex のログイン情報を上流の ChatGPT へ転送するための設定です。ブリッジ用の API キーではありません。GPT も使う場合は、ブリッジの起動時に `--upstream-base-url https://chatgpt.com/backend-api/codex` を指定します。macOS の常駐設定では指定済みです。

音声を使う場合は、`~/.codex/config.toml` の先頭（テーブルより前）に次を追加し、Codex アプリを再起動してください。音声の WebRTC 開始を ChatGPT 側へ、WebSocket 制御を OpenAI API 側へ直接接続し、通常の会話はブリッジ経由のまま使います。

```toml
experimental_realtime_webrtc_call_base_url = "https://chatgpt.com/backend-api/codex"
experimental_realtime_ws_base_url = "wss://api.openai.com/v1"
```

ChatGPT ログイン用の音声開始 API は `/realtime/calls?intent=quicksilver&architecture=avas` で、本文も通常の `/v1/live` と異なります。単に `/live` を上流へ転送するだけでは動作しません。

## モデルと主な設定

| モデル名 | 実行先 |
| --- | --- |
| `opus`, `fable` | Claude CLI の同名別名（最新版） |
| `gemini-flash-high`, `gemini-flash-medium` | `agy models` にある最新の Gemini Flash |
| `gemini-4-argon-high`, `gemini-4-argon-medium` | Antigravity CLI の Gemini 4 Argon（アカウント側のアクセス権が必要） |
| その他 | `--upstream-base-url` 指定時は上流。未指定時は Claude CLI |

| 引数 | 用途 |
| --- | --- |
| `--working-dir PATH` | CLI の作業ディレクトリ |
| `--add-dir PATH` | 作業を許可する追加ディレクトリ |
| `--port PORT` | HTTP ポート（既定: `8787`） |
| `--claude-models NAMES` | Claude に割り当てるモデル名のカンマ区切り |
| `--antigravity-models NAMES` | Gemini に割り当てるモデル名のカンマ区切り。空文字で無効 |
| `--upstream-base-url URL` | それ以外のモデルの中継先 |
| `--enable-codex-mcp` | Claude から Codex app-server のスレッド操作を使う |
| `--mcp-config-file PATH` | Claude に追加する MCP サーバーの設定 |

Gemini Flash の公開名は固定です。実行時に `agy models` を参照して対応する最新バージョンへ変換し、結果は最大 10 分保持します。Argon は正式なモデル名をそのまま CLI へ渡します。`--antigravity-models` に実際のモデル名を指定して、バージョンや公開するモデルを選べます。macOS の常駐設定では Flash High と Argon High を公開します。

その他の引数は `python3 -m claude_bridge --help`（Windows は `py -3 -m claude_bridge --help`）で確認できます。

## macOS で常駐させる

```bash
CLAUDE_BRIDGE_WORKING_DIR=/path/to/project launchd/install.sh
```

起動設定は `launchd/local.claude-bridge.plist` にあります。Codex が渡すツール（imagegen など）は Claude でも既定で使えます。スレッド操作用の共有 app-server もインストーラーが設定します。macOS の ChatGPT アプリを `/Applications` にインストールしてから実行してください。ログイン時のアプリ復元が先に動いても、共有接続を明示して起動し直します。実行中のタスクがある間は終了を待ち、処理を中断しません。確認ログは `~/Library/Logs/claude-bridge/app-connection.log` です。使用中の 8787 を持つ別プロセスは停止しません。

このマシンだけの設定（Claude に触らせる追加ディレクトリ、追加の MCP サーバーなど）は、リポジトリではなく `~/.config/claude-bridge/bridge.env` に `NAME=value` の形で書きます。`install.sh` が plist の環境変数へ足します。変えたら `install.sh` を実行し直してください。

```bash
CLAUDE_BRIDGE_ADD_DIRS=/Users/me/project-a:/Users/me/project-b
CLAUDE_BRIDGE_MCP_CONFIG_FILE=/Users/me/.config/claude-bridge/mcp.json
```

Python はリポジトリの `.venv` があればそれを使います。別のものを使うときは `CLAUDE_BRIDGE_PYTHON` を指定します。

設定を変えたあとの再起動も `install.sh` で行います。`launchctl bootout` と `launchctl bootstrap` を続けて直接実行しないでください。`bootout` は旧プロセスが止まりきる前に戻るため、直後の `bootstrap` が `Bootstrap failed: 5: Input/output error` で失敗し、登録が外れたままブリッジが二度と上がらなくなります。`install.sh` は旧プロセスの終了を待ってから登録し直し、ブリッジ経由のシェルから実行された場合は自分が巻き添えで止まらないよう再起動を切り離します。

## Windows で常駐させる

Codex CLI をインストールし、ログインしてから PowerShell で実行します。

```powershell
.\windows\install.ps1 -WorkingDir C:\path\to\project
```

ログオン時にブリッジとスマホ Remote のスレッド表示補助を起動します。Codex の共有 app-server とスレッド操作も既定で使えます。再設定時も 8787 を使う別のプロセスは停止しません。

## 注意点

- Antigravity への指示と履歴は標準入力の `stream-json` で渡すため、従来の16KB制限はありません。
- ブリッジはループバックアドレスでのみ待ち受けます。ブリッジ自体に HTTP 認証はありません。
- Claude と Antigravity は非対話実行時のツール操作を自動承認します。`--working-dir` と `--add-dir` には信頼できる場所だけを指定してください。
- Windows でも Codex から渡されたツール（imagegen や Browser など）と共有 app-server のスレッド操作を利用できます。
- 画像生成・編集 API は `--upstream-base-url` を指定した場合に上流へ中継します。
- 音声はブリッジではなく Codex のネイティブ OpenAI 経路を使います。「Codex から使う」の音声設定が必要です。
- Codex で停止すると、ブリッジは 1 秒以内に Claude CLI を Bash などの子プロセスごと止めます。
- Claude はターン全体を 1 回のモデル呼び出しで実行するため、そのままでは作業中に送った指示が作業の終わるまで届きません。ブリッジの `turn_start` / `turn_start_async` で作業中の Claude のスレッドへ送った指示は、受理された時点でブリッジが Claude の作業を区切り、同じターンの続きとして指示に沿って進めます。
- iPhone のリモートや ChatGPT アプリから作業中に送った指示も、ブリッジが共有 app-server の記録（`CODEX_HOME` の `logs_N.sqlite`）を読み取り専用で見張り、差し込まれた時点で同じように区切ります。記録の文面は Codex の内部形式なので、Codex の更新で拾えなくなった場合は、指示が従来どおり作業の後に届きます。

## テスト

```bash
python3 -m unittest discover -s tests
```
