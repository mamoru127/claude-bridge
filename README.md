# claude-bridge

Claude CLI と Antigravity CLI を、ローカルの OpenAI Responses API 互換エンドポイントから使うためのブリッジです。Codex から指定したモデル名に応じて CLI を呼び出します。Python 標準ライブラリだけで動きます。

## 準備

- Python 3.11 以上
- インストールとログインを済ませた Claude CLI（`claude -p "ping"` で確認）
- Gemini も使う場合のみ、インストールとログインを済ませた Antigravity CLI（`agy`）

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

[profiles.claude]
model_provider = "claude_bridge"
model = "claude-opus-5-5"
```

CLI では `codex --profile claude` で使えます。Codex デスクトップアプリで既定の接続先にする場合は、同じファイルの先頭（`[` で始まる設定より前）に次を追加し、アプリを再起動します。

```toml
model_provider = "claude_bridge"
model = "claude-opus-5-5"
```

Codex のモデル一覧に Claude と GPT を一緒に表示する場合は、ブリッジの起動時に `--upstream-base-url https://chatgpt.com/backend-api/codex` を指定します。Claude / Gemini 以外のモデルは上流へ中継され、Codex から受け取った認証ヘッダーもそのまま転送されます。ブリッジに上流用の API キーを設定する必要はありません。

## モデルと主な設定

| モデル名 | 実行先 |
| --- | --- |
| `claude-opus-5-5`, `claude-fable-5-1` | Claude CLI |
| `gemini-3.8-flash-high`, `gemini-3.8-flash-medium` | Antigravity CLI |
| その他 | `--upstream-base-url` 指定時は上流。未指定時は Claude CLI |

| 引数 | 用途 |
| --- | --- |
| `--working-dir PATH` | CLI の作業ディレクトリ |
| `--add-dir PATH` | 作業を許可する追加ディレクトリ |
| `--port PORT` | HTTP ポート（既定: `8787`） |
| `--claude-models NAMES` | Claude に割り当てるモデル名のカンマ区切り |
| `--antigravity-models NAMES` | Gemini に割り当てるモデル名のカンマ区切り。空文字で無効 |
| `--upstream-base-url URL` | それ以外のモデルの中継先 |
| `--enable-codex-mcp` | Claude から共有 Codex app-server のツールを使う（macOS のみ） |
| `--mcp-config-file PATH` | Claude に追加する MCP サーバーの設定 |

その他の引数は `python3 -m claude_bridge --help`（Windows は `py -3 -m claude_bridge --help`）で確認できます。

## macOS で常駐させる

```bash
launchd/install.sh
```

起動設定は `launchd/local.claude-bridge.plist` にあります。変更後は `launchd/install.sh` を再実行してください。この操作は実行中のブリッジを再起動します。

## 注意点

- ブリッジはループバックアドレスでのみ待ち受けます。ブリッジ自体に HTTP 認証はありません。
- Claude と Antigravity は非対話実行時のツール操作を自動承認します。`--working-dir` と `--add-dir` には信頼できる場所だけを指定してください。
- Windows では HTTP ブリッジ、Claude CLI、上流中継を利用できます。`launchd/`、共有 Codex app-server、Mac 版アプリの Browser 連携は macOS 専用です。
- 画像生成・編集 API は `--upstream-base-url` を指定した場合に上流へ中継します。

## テスト

```bash
python3 -m unittest discover -s tests
```
