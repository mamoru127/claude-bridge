# claude-bridge

ローカルの Claude CLI (`claude -p`) を **OpenAI Responses API 互換の HTTP エンドポイント**として公開する小さなブリッジです。
Codex など「OpenAI 互換のベース URL」を指定できるツールから、Claude を呼び出せるようになります。

- Python 標準ライブラリのみで動作します（追加依存なし）。
- 呼び出し元のプロジェクトからは独立していて、相手側のコードや依存には手を入れません。

## 構成

```
claude-bridge/
├── claude_bridge/
│   ├── __main__.py       起動エントリポイント
│   ├── config.py         設定（環境変数・コマンドライン引数）と起動前バリデーション
│   ├── server.py         HTTP サーバー（認証・入力検証・モデルの振り分け）
│   ├── responses.py      Responses API のリクエスト解釈とレスポンス生成
│   ├── claude_cli.py     Claude CLI の非対話実行と stream-json 出力の逐次解釈
│   ├── upstream.py       Claude が受け持たないモデルの上流への中継
│   ├── mcp_adapter.py    Codex app-server の RPC を MCP tool として公開する stdio MCP サーバー
│   ├── app_server.py     専用の `codex app-server` 子プロセスと話す JSON-RPC クライアント
│   │                     （子プロセスを持つのはブリッジ本体。アダプタは HTTP で頼むだけ）
│   └── errors.py         OpenAI error 形式に変換できる共通エラー型
├── launchd/
│   ├── local.claude-bridge.plist  ログイン時に常駐させる launchd ユーザーエージェント
│   └── install.sh                 plist のパスを埋めて登録し、起動を確認するスクリプト
└── tests/
    ├── test_bridge.py           HTTP・Responses API・Claude CLI 実行
    └── test_app_server_mcp.py   MCP アダプタ・app-server クライアント・CLI 引数
```

## 前提条件

1. **Claude CLI がインストール済みであること**（`which claude` で確認）
2. **Claude CLI の認証が済んでいること**
   ブリッジは認証を一切代行しません。あらかじめターミナルで `claude` を一度起動してログインを完了させるか、
   `ANTHROPIC_API_KEY` を設定しておいてください。未認証のままだとブリッジは CLI の異常終了を
   `502 claude_cli_failed` として返します。
   動作確認: `claude -p "ping" --output-format json`

## 起動

```bash
cd claude-bridge
python -m claude_bridge
```

既定で `http://127.0.0.1:8787` を待ち受けます。

| メソッド・パス | 認証 | 内容 |
| --- | --- | --- |
| `GET /health` | 不要 | `{"status": "ok"}` を返すヘルスチェック |
| `GET /v1/models` | 必要 | `--claude-models`（既定は `claude-opus-5`）で指定した名前を並べたモデル一覧 |
| `POST /v1/responses` | 必要 | Responses API 本体。`stream` の真偽で JSON と SSE を切り替える |

（「認証」は `CLAUDE_BRIDGE_API_KEY` を設定した場合のみ必要という意味です。上記以外のパスは 404 を返します。）

ルーティングはクエリ文字列を除いたパスで判定します。Codex は `GET /v1/models?client_version=0.144.5` の
ようにクエリを付けて送りますが、クエリの内容は読まずに無視します。

### モデルの振り分け

`--upstream-base-url` を指定すると、ブリッジは 1 つのエンドポイントで**モデル名ごとに行き先を変えます。**

| リクエストの `model` | 行き先 |
| --- | --- |
| `--claude-models` にある名前（既定は `claude-opus-5` のみ） | ローカルの Claude CLI が処理する |
| それ以外（`gpt-5.4` などの gpt 系すべて） | 受信ヘッダと本文のまま上流へ素通しする |

素通しの場合、ブリッジは本文を解釈せず、`Authorization` を含む受信ヘッダを転送して上流の応答を
そのまま返します（SSE も上流のチャンクを流すだけ）。転送しないのは接続単位のヘッダ
（`Connection` や `Transfer-Encoding` など）と、接続を張り直す際に付け替わる `Host` / `Content-Length`
だけです。**ブリッジ自身は上流の資格情報を持ちません。** 受信パスの `/v1` は中継先 URL のパスへ
置き換わるため、`POST /v1/responses` は `https://chatgpt.com/backend-api/codex/responses` へ届きます。

`GET /v1/models` も同じ考え方で、上流の一覧を先に並べ、`--claude-models` の名前を後ろへ足して返します。
Codex アプリのモデル選択に GPT と Claude が並ぶのはこのためです。

`--upstream-base-url` を省略すると中継はせず、**どのモデル名で来ても Claude CLI が処理します。**
中継の有効時は `CLAUDE_BRIDGE_API_KEY` と併用できません（上流の認証をそのまま転送するため）。

### オプション

| 引数 | 環境変数 | 既定値 | 説明 |
| --- | --- | --- | --- |
| `--host` | `CLAUDE_BRIDGE_HOST` | `127.0.0.1` | bind するアドレス |
| `--port` | `CLAUDE_BRIDGE_PORT` | `8787` | bind するポート |
| `--claude-path` | `CLAUDE_BRIDGE_CLAUDE_PATH` | `claude` | Claude CLI の実行ファイルパス |
| `--model` | `CLAUDE_BRIDGE_MODEL` | `opus` | CLI に渡すモデル名 |
| `--timeout` | `CLAUDE_BRIDGE_TIMEOUT_SECONDS` | `3600` | CLI 実行のタイムアウト秒。CLI の暴走に対する唯一の歯止め |
| `--max-request-bytes` | `CLAUDE_BRIDGE_MAX_REQUEST_BYTES` | `33554432` | リクエストボディの上限バイト数。画像は data URL で送られてくるため大きめ |
| `--working-dir` | `CLAUDE_BRIDGE_WORKING_DIR` | カレントディレクトリ | CLI を実行する作業ディレクトリ |
| `--add-dir` | `CLAUDE_BRIDGE_ADD_DIRS` | なし | 作業ディレクトリの外で CLI にツール操作を許可する追加ディレクトリ。引数は繰り返し指定、環境変数は `:`（`os.pathsep`）区切り。存在しないパスは起動時に失敗する |
| `--claude-models` | `CLAUDE_BRIDGE_CLAUDE_MODELS` | `claude-opus-5` | API で受け付けるモデル名のカンマ区切り。`GET /v1/models` が返す一覧そのもの |
| `--upstream-base-url` | `CLAUDE_BRIDGE_UPSTREAM_BASE_URL` | なし（中継しない） | `--claude-models` 以外のモデルを中継する上流。Codex の既定は `https://chatgpt.com/backend-api/codex` |
| `--enable-codex-mcp` | `CLAUDE_BRIDGE_ENABLE_CODEX_MCP` | 無効 | Codex app-server の MCP アダプタを Claude CLI へ渡す（後述） |
| `--codex-path` | `CLAUDE_BRIDGE_CODEX_PATH` | `codex` | ブリッジが app-server として起動する Codex CLI の実行ファイルパス |
| `--mcp-config-file` | `CLAUDE_BRIDGE_MCP_CONFIG_FILE` | なし | 追加で有効にする MCP サーバーを書いた JSON ファイル（`mcpServers` 形式、後述）。起動時に読み込み、読めなければ起動しない |
| （なし） | `CLAUDE_BRIDGE_API_KEY` | なし | 設定すると `Authorization: Bearer` を必須にする共有シークレット |

例:

```bash
CLAUDE_BRIDGE_API_KEY=$(python -c 'import secrets; print(secrets.token_urlsafe(32))') \
python -m claude_bridge \
  --claude-path /opt/homebrew/bin/claude \
  --model sonnet \
  --timeout 300 \
  --working-dir /path/to/your/project
```

シークレットは引数ではなく環境変数でのみ受け取ります（`ps` で他プロセスから見えないようにするため）。

### 常駐させる（launchd / macOS）

ログイン時に自動起動し、落ちても再起動するユーザーエージェントとして登録できます。

```bash
cd claude-bridge
launchd/install.sh
```

`launchd/local.claude-bridge.plist` のプレースホルダ（python の絶対パス・作業ディレクトリ・
`$HOME`）を埋めて `~/Library/LaunchAgents/` へ書き出し、`launchctl bootstrap` で登録して
`/health` が応答するまで確認します。python は `command -v python3` を使うので、
仮想環境の python を使う場合は `CLAUDE_BRIDGE_PYTHON=/path/to/.venv/bin/python launchd/install.sh` と指定します。
何度実行しても構いません（登録済みなら入れ替え、8787 を掴んでいる手動起動のプロセスも止めます）。

起動引数を変えるときは**リポジトリ側の plist を編集してから `install.sh` を再実行**します。
`~/Library/LaunchAgents/` のコピーを直接編集しても、次の `install.sh` で上書きされます。

| 操作 | コマンド |
| --- | --- |
| 状態確認 | `launchctl print gui/$(id -u)/local.claude-bridge` |
| 再起動 | `launchctl kickstart -k gui/$(id -u)/local.claude-bridge` |
| 停止（登録解除） | `launchctl bootout gui/$(id -u)/local.claude-bridge` |
| ログ | `tail -f ~/Library/Logs/claude-bridge/bridge.log` |

`KeepAlive` を有効にしているため、**`kill` で止めても launchd が起動し直します。**
止めたいときは `bootout` を使ってください。再起動は `kickstart -k` が確実です。

**`install.sh` と `kickstart -k` はブリッジを作り直します。** そのとき Codex app-server の
子プロセスと、ブリッジが起動した Claude CLI も一緒に終わります。`turn_start_async` で走らせている
ターンがあるなら、終わってから実行してください。

plist で明示している点:

- **`caffeinate -s`**: python を `caffeinate -s` 経由で起動し、AC 電源のときだけシステムスリープを
  抑止します。スリープするとブリッジも app-server も止まり、待たずに始めたターンが途中で死ぬためです。
  バッテリー駆動では抑止しないので、外出時に電池を使い切ることはありません。
- **`PATH`**: launchd はログインシェルの環境を引き継ぎません。`claude` と `codex` を見つけられるよう
  `/opt/homebrew/bin` を含む `PATH` を書いています。Homebrew の場所が変わったら追従が必要です。
- **`WorkingDirectory`**: `python -m claude_bridge` の import 元であり、`--working-dir` の既定値でもあります。
- **`ProcessType: Interactive`**: 応答を返すプロセスなので、バックグラウンドジョブ扱いの CPU 制限をかけません。
- **`ThrottleInterval`**: 設定不備で即死したときに再起動を繰り返さないための最小間隔です。
- **`CLAUDE_BRIDGE_MCP_CONFIG_FILE`**: `~/.config/claude-bridge/mcp.json` を指しています。
  リポジトリ管理外のユーザー設定なので、**このファイルがないとサービスは起動しません**
  （設定ミスを黙って無視しない方針です）。中身は[後述の例](#他の-mcp-サーバーを追加するオプトイン)のとおりで、
  追加サーバーが不要なら plist からこのキーを消してください。

ログは追記され続けるため、肥大化したら手動で削除してください（ローテーションは設定していません）。

Claude CLI の認証情報は login keychain にあります。ログインセッションのユーザーエージェントとして
動くため keychain は解錠済みですが、**ログインしていない状態では動きません**（LaunchDaemon ではないため）。

## Codex の設定

### CLI（プロファイル切り替え）

```toml
[model_providers.claude_bridge]
name = "Claude CLI bridge"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"
# CLAUDE_BRIDGE_API_KEY を設定した場合のみ。Codex 側にも同じ値を環境変数で渡す。
env_key = "CLAUDE_BRIDGE_API_KEY"

[profiles.claude]
model_provider = "claude_bridge"
model = "opus"
```

利用時: `codex --profile claude`

`env_key` を指定した場合、Codex は `Authorization: Bearer $CLAUDE_BRIDGE_API_KEY` を送ります。
ブリッジ側と同じ値を両方のシェルに設定してください。

### Codex デスクトップアプリ（トップレベル `model_provider` が必要）

**デスクトップアプリには `--profile` を渡す手段がありません。** アプリは `codex app-server --listen stdio://`
としてバックエンドを起動するため、`[profiles.*]` に書いた `model_provider` は適用されません。
アプリの既定経路をブリッジへ向けるには、**トップレベルに `model_provider` を書く**必要があります。

```toml
model = "claude-opus-5"
model_provider = "claude_bridge"
```

アプリのモデル選択を `claude-opus-5` にしておくと、その名前でブリッジにリクエストが届きます。
ブリッジは `GET /v1/models` でこの名前を公開して受け付けますが、**実行するのは起動時の
`--model`（既定は `opus` = Claude Opus）で固定**です。リクエストの `model` は
レスポンスの `model` にそのまま返すだけで、Claude CLI の `--model` には渡しません。
つまり応答を生成しているのは常に Claude で、OpenAI のモデルは一切呼び出されません。

**この名前は上流に実在しないものを選んでください。** 中継が有効なとき、ブリッジは上流の
モデル一覧から同じ名前を取り除いて自分の項目に置き換えます。上流に実在する名前
（`gpt-5.4` など）を指定すると、その GPT モデルがアプリの選択肢から消えます。

`config.toml` を書き換えた後は、設定を読み直させるために **Codex デスクトップアプリを再起動してください。**

## Codex app-server ツール（オプトイン）

Codex アプリのモデルを Claude へ流している状態でも、**Claude 側から Codex app-server の
RPC を呼べるようにする**ための仕組みです。**既定では無効**で、明示的に有効化したときだけ動きます。

```
Codex アプリ ──HTTP──> claude-bridge ──> claude --print --mcp-config ...
                          │  ▲                  │
                          │  └── HTTP ──── mcp_adapter <── MCP ──┘
                          │   /app-server/rpc
                          └─ JSON-RPC ──> codex app-server（専用の子プロセス）
```

**app-server の子プロセスを持つのはブリッジ本体です。** アダプタは Claude CLI の起動ごとに
生まれて消えるため、アダプタが持つと再起動のたびに実行中のターンが道連れになります。
常駐するブリッジ側へ寄せることで、`turn_start_async` で始めたターンはアダプタの寿命と無関係に
走り続けます。

### 有効化

```bash
CLAUDE_BRIDGE_ENABLE_CODEX_MCP=1 python -m claude_bridge
```

または

```bash
python -m claude_bridge --enable-codex-mcp --codex-path /opt/homebrew/bin/codex
```

有効化すると、Claude CLI の起動引数に次の 2 つが加わります（無効時は一切付きません）。

- `--mcp-config '<JSON>'` — 同梱アダプタ 1 つだけを stdio MCP サーバーとして登録する JSON。
  引数配列の 1 要素として渡すので、シェルで組み立てることはありません。
- `--allowedTools mcp__codex_app_server` — `--print` では権限確認に答えられないため、
  **このサーバーのツールだけ**を事前に許可します。他のツールの権限は既定のままです。

`--strict-mcp-config` は有効時も付いたままなので、**読み込まれるのはこのアダプタだけ**です。
ユーザー設定の MCP サーバーが勝手に起動することはありません。

## 他の MCP サーバーを追加する（オプトイン）

`--strict-mcp-config` があるため、`~/.claude.json` やプロジェクトの `.mcp.json` に書いた
MCP サーバーはブリッジ経由では読まれません。使いたいものは
`--mcp-config-file` で**明示したものだけ**を渡します。

```bash
CLAUDE_BRIDGE_MCP_CONFIG_FILE=~/.config/claude-bridge/mcp.json python -m claude_bridge
```

ファイルは Claude Code の `.mcp.json` と同じ形式です。

```json
{
  "mcpServers": {
    "example-mcp": {
      "type": "http",
      "url": "https://mcp.example.com/mcp"
    }
  }
}
```

書いたサーバーは `--mcp-config` にまとめて載り、`--allowedTools` にも
`mcp__<サーバー名>` として追加されます（`--print` では権限確認に答えられないため）。

ファイルは**起動時に一度だけ**読みます。読めない・`mcpServers` がない・同梱アダプタと
同名（`codex_app_server`）といった設定は、リクエスト時ではなく起動時に失敗します。
パス内の `~` は展開されるので、launchd の環境変数にも書けます。

リクエストごとに `claude` を起動するため、MCP の接続もリクエストごとにやり直します。
`url` 形式（Streamable HTTP）は接続の往復だけで、短いプロンプト 3 回の実測では
MCP なしと差が出ませんでした。`command` 形式は毎回プロセスを起動するぶん重く、
`npx mcp-remote` では 2.9〜5.3 秒とばらつきました。リモートサーバーは `url` 形式が有利です。
それでも常用しないサーバーは書かないのが無難です。

### 公開するツール

| ツール名 | app-server の RPC | 内容 |
| --- | --- | --- |
| `thread_start` | `thread/start` | 新しいスレッドを作成する（ターンは開始しない） |
| `turn_start` | `turn/start` | ターンを 1 回実行し、**完了まで待って**最終メッセージを返す |
| `turn_start_async` | `turn/start` | ターンを開始し、**完了を待たずに**戻る。進捗は `thread_read` などで見る |
| `thread_list` | `thread/list` | スレッド一覧 |
| `thread_read` | `thread/read` | スレッド 1 件。`includeTurns` を真にすると各ターンの状態と本文まで返る |
| `thread_turns_list` | `thread/turns/list` | スレッド内のターン（タスク単位の実行）とその状態 |
| `thread_search` | `thread/search` | スレッドの全文検索 |

Claude から見えるツール名は `mcp__codex_app_server__thread_list` のようになります。
結果は app-server の応答 JSON をそのままテキストとして返します。

**`thread/delete` や `turn/steer` などの破壊的な RPC は公開していません。**

**`thread/items/list` も公開していません。** app-server 側が未実装で
（`thread/items/list is not supported yet`）、呼べば必ず失敗します。項目の中身は
`thread_read` の `includeTurns` で読めるため、そちらを使ってください。

`thread/turns/list` は `initialize` で `capabilities.experimentalApi` を宣言しないと
`-32600` で拒否されます。ブリッジは常に宣言します。

`turn/start` は外部のモデル API を呼びます。対象スレッドのプロバイダがこのブリッジ自身だと
ブリッジ → Claude → ブリッジと再帰します。`thread_start` は Codex の設定にある既定の
`model_provider` を使うため、`~/.codex/config.toml` が `claude_bridge` を既定にしている場合、
**指定しなければブリッジ経由（＝ローカルの Claude CLI が実行）になります。**
別のプロバイダで動かしたいときは `thread_start` に `modelProvider` を明示してください。

`turn_start` は最大 900 秒でタイムアウトします。実装やレビューのような長い作業には
`turn_start_async` を使い、`thread_read` や `thread_turns_list` で完了を確認してください。

### スレッドを別々に走らせて後から様子を見る

`turn_start_async` は、Codex アプリ（スマホを含む）で個別に進捗を追える独立したスレッドを
作るための道具です。手順は次のとおりです。

1. `thread_start` でスレッドを作る（`cwd` に作業ディレクトリ、必要なら `modelProvider`）。
2. `turn_start_async` で最初のターンを投げる。ここで戻り値は即座に返る。
3. 以降は `thread_read`（`includeTurns=true`）か `thread_turns_list` で状態を見る。
   `status` は `inProgress` → `completed` / `failed` / `interrupted` と変わる。

**スレッドは `thread_start` だけでは Codex アプリの一覧に出ません。** セッションファイル
（`~/.codex/sessions/...jsonl`）が作られるのは最初のターンを開始したときなので、
一覧に並べたいならターンの投入までを 1 セットで行ってください。

止まる条件は 2 つだけです。**ブリッジ本体を止める**か、**Mac がスリープする**か。
前者は launchd の再読み込みや `install.sh` の再実行でも起きるため、走らせている間は避けてください
（ブリッジが起動した Claude CLI も同時に落ちます）。後者は plist の `caffeinate -s` で、
AC 電源のときだけ抑止しています。

### 専用の app-server 子プロセス

**ブリッジ本体**が最初の RPC を受けたときに `codex app-server --listen stdio://` を自分の
子プロセスとして起動し、その子プロセスの stdin/stdout とだけやり取りします。
MCP アダプタは `POST /app-server/rpc` でブリッジへ頼むだけで、自分では起動しません。

- **デスクトップアプリが動かしている app-server には一切触れません。** 別プロセスです。
  アプリ側のプロセスの stdin/stdout に割り込むことはありません。
- そのため**アプリで開いているスレッドの「中の状態」は共有しません。** 見えるのはディスクに
  永続化された内容（`~/.codex/sessions` 由来のスレッド一覧・履歴）です。
  アプリ側で編集中のまだ書き出されていない状態は見えません。
- ツールが一度も呼ばれなければ `codex` プロセスは起動しません。
- **Claude CLI が終了しても子プロセスは残ります。** 終了するのはブリッジ本体を止めたときです。
  これが `turn_start_async` で始めたターンを走り切らせるための条件です。
- 子プロセスの stdio は 1 本しかないため、**読み取りは専用スレッド 1 本だけ**が行い、
  `id` で応答を、`threadId` で通知を待ち手へ振り分けます。送信だけをロックで直列化するので、
  `turn_start` が完了を待っている間も `thread_read` などの呼び出しは通ります。
  待ち手のいない通知（＝待たずに始めたターンの経過）はその場で捨てるため、溜まりません。
- **RPC のエラー応答と 1 件の時間切れでは子プロセスを落としません。** 通信の失敗ではないためです。
  ここで落とすと、進捗確認が 1 回失敗しただけで、走っている全部のターンが `interrupted` になります。
  落とすのは、stdout が閉じた／JSON として読めない／書き込めない、というつなぎ目の failure のときだけです。
- 子プロセスの stderr は捨てます（app-server が常時ログを吐くため、読まずに溜めると詰まる）。

### `POST /app-server/rpc`

MCP アダプタ専用の内部エンドポイントです。本文は `{"method", "params", "wait"}`。
`wait` は `turn/start` のときだけ意味を持ち、真なら完了まで待ちます（既定は待たない）。
応答は `{"result": ...}`、app-server 側の失敗は 502 `app_server_failed` になります。

### Responses API から見た挙動

Claude が MCP ツールを使っても、`POST /v1/responses` が返すのは**表示用の `reasoning`、作業コメント、最終テキストだけ**です
（`reasoning` と作業コメントは `stream=true` のときだけで、`stream=false` の応答は最終テキストだけです）。
`function_call` を偽装することはありません。Claude CLI の JSON 出力に `tool_use` /
`tool_result` が挟まっても、最終テキスト（`result`）の取り出しは壊れません。

## 動作

`POST /v1/responses` は次のように処理されます。

1. `Authorization` を検証（`CLAUDE_BRIDGE_API_KEY` 設定時のみ）
2. `instructions` と `input` からシステムプロンプトとユーザープロンプトを抽出（`tools` は読まずに捨てる）
3. `claude --print --output-format stream-json --input-format stream-json --verbose
   --thinking-display summarized --strict-mcp-config --model <model>
   [--mcp-config <JSON> --allowedTools mcp__codex_app_server] [--append-system-prompt <system>]` を
   **引数配列で**起動し、プロンプトは標準入力から渡す
   （`--mcp-config` が付くのは `--enable-codex-mcp` を指定したときだけ）
4. CLI が標準出力へ 1 行ずつ吐く JSON を読みながら、`result` の最終テキストを
   Responses API 形式で返す（`stream=true` なら SSE で逐次返す）

`input` は文字列でも配列でも受け付けます。配列の場合、`system` / `developer` ロールはシステムプロンプトへ、
`user` / `assistant` ロールは `[role]\n本文` 形式で連結してプロンプトになります。

標準入力へ渡すのは `--input-format stream-json` の 1 行（`{"type":"user","message":{...}}`）です。
テキストは 1 本のプロンプトへ連結し、`input_image` はその位置で `image` ブロックとして挟みます。
文字列をそのまま渡す既定の入力形式では画像を送れないため、テキストだけのリクエストも同じ経路を通します。
ユーザーメッセージが 1 件だけの場合は装飾なしでそのまま渡します。

### `stream=false`（既定）

1 個の JSON をそのまま返します。

```bash
curl http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model": "opus", "input": "ping と返して"}'
```

レスポンス例:

```json
{
  "id": "resp_...",
  "object": "response",
  "created_at": 1770000000,
  "status": "completed",
  "model": "opus",
  "output": [
    {
      "id": "msg_...",
      "type": "message",
      "role": "assistant",
      "status": "completed",
      "content": [{ "type": "output_text", "text": "...", "annotations": [] }]
    }
  ],
  "output_text": "..."
}
```

### `stream=true`

`Content-Type: text/event-stream` で SSE を返します。
**Claude の思考と使ったツールは実行中に `reasoning` アイテムとして逐次届き、作業の区切りで
Claude が書く本文は `message` アイテムとして届きます。最終的なアシスタントテキストは
完了後に 1 個の `output_text.delta` として送ります。**
本文がトークン単位で少しずつ届くわけではありません（後述の「制約」を参照）。

```bash
curl -N http://127.0.0.1:8787/v1/responses \
  -H 'Content-Type: application/json' \
  -d '{"model": "opus", "input": "ping と返して", "stream": true}'
```

イベントは次の順で送出されます。各イベントは `event:` 行と `data:` 行の組で、
`response.*` の `data` には `type` と 1 から始まる `sequence_number` が入ります。

| # | event | 内容 |
| --- | --- | --- |
| 1 | `response.created` | リクエスト受理。`status` は `in_progress` |
| 2 | `response.in_progress` | 同上。ここまでを CLI 起動前に送る |
| — | `response.output_item.done`（`reasoning`） | **実行中の思考とツール使用**。0 回以上、発生するたびに届く |
| — | 3〜8 と同じ 6 イベント（`message`） | **実行中の作業コメント**。0 回以上、書かれるたびに 1 件ぶんの組で届く |
| — | `bridge.keepalive` | **無音が続く間の合図**。15 秒ごと。Responses のイベントではない |
| 3 | `response.output_item.added` | assistant メッセージの開始 |
| 4 | `response.content_part.added` | 空の `output_text` パート |
| 5 | `response.output_text.delta` | **出力全文が 1 個の `delta`** |
| 6 | `response.output_text.done` | 確定した `text` |
| 7 | `response.content_part.done` | 本文入りの `part` |
| 8 | `response.output_item.done` | `status: completed` のメッセージ |
| 9 | `response.completed` | 完了した response オブジェクト（非ストリーム時のボディと同じ形） |
| 10 | — | 終端の `data: [DONE]` |

実行中の作業は `reasoning` アイテムとして表示されます。Claude の思考が届くたびにその本文を、
Bash や Read などのツールを使うたびにツール名と主要な引数だけを、それぞれ 1 件の `reasoning` として送ります。
思考本文は `--print` では既定で省略されるため、`--thinking-display summarized` を付けて出力させています
（本文が空の思考は送りません）。

```
event: response.output_item.done
data: {"type":"response.output_item.done","sequence_number":3,"output_index":0,"item":{"id":"rs_...","type":"reasoning","summary":[{"type":"summary_text","text":"**Bash を実行中**\nwc -l README.md"}]}}
```

表示に使われるのは `summary` です。Codex は Responses API のイベントしか読まないため、
`item.completed` のような別系統の event 名や、`summary` のない `text` だけのアイテムは
無視されて画面に出ません。`output_index` は reasoning と本文で 0 から通し番号になり、
`sequence_number` も他のイベントと同じ連番を共有します。完了した response の `output` には
含めません。

### 実行中の作業コメント

`reasoning` は実行中の表示にしか使われず、会話の記録には残りません。Claude が作業の区切りで
書く本文（CLI の `text` ブロック）は、最終テキストと同じ `message` アイテムとして送るため、
記録として残ります。途中の本文には `phase: "commentary"`、最終テキストには
`phase: "final_answer"` を付け、Codex App が途中の本文以降もツール実行を表示できるようにします。
アイテム ID は 1 件ごとに分け、`response.output_item.added` から
`response.output_item.done` まで最終テキストと同じ 6 イベントの組で流します。完了した
response の `output` にも、送った順で並びます。

Claude CLI は最後の `text` ブロックと同じ本文を `result` としても出すため、最後の 1 件だけは
保留して重複を落とし、最終テキストとしてのみ送ります。

作業コメントを送るのは `stream=true` のときだけです。`stream=false` では途中経過を送る先が
ないため、`reasoning` と同じく捨て、`output` は最終テキストの 1 件だけになります。

### 無音が続く間の合図

Claude が長く考えたり 1 つのツールに時間をかけたりする間、CLI は何も出力しません。この無音が
クライアントの待機上限を超えると接続が切られ、Codex では「再接続中 /5」のあと
`idle timeout waiting for SSE` になります。そのため出力が途切れている間は 15 秒ごとに
`bridge.keepalive` を送ります。

```
event: bridge.keepalive
data: {"type":"bridge.keepalive"}
```

SSE のコメント行（`: keepalive`）では足りません。コメントはイベントを 1 件も生まないため、
Codex の待機時間が更新されずに切断されます（実測で確認）。Responses の型と衝突しない名前の
イベントにして、受け取り側には未知の型として読み飛ばさせています。`sequence_number` は
持たせないので、Responses のイベントの連番はこの合図があっても途切れません。

SSE ヘッダ送出後に CLI が失敗した場合は HTTP ステータスを変えられないため、
`response.failed` イベントで返します。`response` の `error` には非ストリーム時と同じエラー本体が入ります。

```
event: response.failed
data: {"type":"response.failed","sequence_number":3,"response":{"id":"resp_...","status":"failed","error":{"message":"...","type":"api_error","code":"claude_cli_failed"}}}
```

`response.failed` の後にも `data: [DONE]` を送ります。

### エラー

ヘッダ送出前のエラーは HTTP ステータスと OpenAI 形式の JSON で返します。

```json
{ "error": { "message": "...", "type": "invalid_request_error", "param": "input[0].content[0]", "code": "unsupported_content_part" } }
```

| code | HTTP | 意味 |
| --- | --- | --- |
| `invalid_api_key` | 401 | `Authorization` が不正 |
| `not_found` | 404 | 未対応のパス |
| `invalid_request` | 400 | `input` が空・`model` が文字列でない・`stream` が真偽値でない など |
| `invalid_json` | 400 | ボディを JSON として解釈できない |
| `unsupported_content_part` | 400 | テキスト以外の入力 |
| `length_required` | 411 | `Content-Length` がない |
| `request_too_large` | 413 | ボディが上限超過 |
| `unsupported_media_type` | 415 | `Content-Type` が `application/json` でない |
| `claude_cli_timeout` | 504 | CLI がタイムアウト |
| `claude_cli_not_found` / `claude_cli_launch_failed` | 500 | CLI の実行ファイルが見つからない、または起動できない |
| `claude_cli_failed` / `claude_cli_error_result` | 502 | CLI が異常終了、またはエラー結果を返した |
| `claude_cli_invalid_output` | 502 | CLI の出力を JSON として解釈できない、または `result` が届かない |
| `upstream_unreachable` | 502 | 中継先へ接続できない（`--upstream-base-url` 指定時） |
| `upstream_models_failed` / `upstream_models_invalid` | 502 | 上流のモデル一覧を取得・解釈できない |

`stream=true` の場合、CLI 実行に入った後のエラー（`claude_cli_*`）は上表の HTTP ステータスではなく
`response.failed` イベントとして届きます。リクエスト検証段階のエラーは SSE 開始前なので通常どおり JSON です。

## 制約（既知の未対応事項）

- **本文のストリーミングは「見かけ上」です。** 実行中の様子は `reasoning`（思考・ツール使用）と
  作業コメント（`message`）で逐次届きますが、いずれもブロックが確定してから全文を 1 個の
  `delta` として流すため、文字が少しずつ届くわけではありません。最終テキストが返り始めるまでの
  待ち時間は非ストリームと同じです。
- **会話は CLI のセッションとして保持します。** リクエストの `prompt_cache_key`（Codex がスレッド
  ごとに送る固定値）を Claude CLI のセッション ID に対応づけ、初回は `--session-id`、2 回目以降は
  `--resume` で同じ転記へ書き足します。Claude へ渡すのは前回から増えた発言だけです。
  `previous_response_id` は受理しますが値は読まずに捨てます。
  - 中断されたターンの読み込み結果やツール実行結果も CLI 側の転記に残るため、割り込み後の
    次のメッセージは続きから動きます。
  - 同じ会話のリクエストは 1 本ずつ実行します。実行中に同じ会話の新しいリクエストが来ると、
    先行する CLI を落として引き継ぎます（切断後の再送や割り込みでは相手がすでにいないためです）。
  - Codex 側の履歴圧縮などで渡し済みの範囲と食い違った場合は、セッションを開始し直して
    全履歴を渡します。
  - 保持するのはブリッジのプロセス内メモリだけです（既定 64 会話、超えた分は古い順に破棄）。
    再起動すると対応づけは失われ、次のリクエストは全履歴を送る新しいセッションになります。
  - `prompt_cache_key` を送らないクライアントには、リクエストごとに使い捨てのセッションを割り当てます。
- **`input` の中の message 以外のアイテムも無視します。** Codex は `additional_tools` や
  `function_call` といった、会話ではないアイテムを `input` 配列に混ぜて送ります。
  ツールを実行しない実装なので、`tools` と同じく値を読まずに捨て、`message` だけをプロンプトにします。
  無視した結果 `message` が 1 つも残らなければ `invalid_request` で 400 になります。
- **リクエストの `tools` は受信するが無視します。**
  Codex はツールを使わない単純なプロンプトでも `tools`（空配列・非空配列とも）を必ず送るため、
  リクエストとしては受理します。ただし**値は一切読まず、そのまま捨てます**。
  クライアントが定義したツールを Claude CLI に渡すことも、レスポンスに `function_call` を
  偽装することもしません。Codex がファイル編集やコマンド実行をブリッジ経由の Claude に
  させようとしても、Codex 側から見ればツールを使わない応答が返ります。
  Claude が使えるのは Claude CLI 自身のツールと、`--enable-codex-mcp` を指定したときに限り
  ブリッジが自分で登録する Codex app-server のツールです（前述の一覧）。
  それらを使っても、Codex へ返るのは実行中の表示用 `reasoning`、作業コメント、最終テキストだけです。
- **`tool_choice` も受信するが無視します。** Codex は常に送るため受理しますが、ツールを
  実行しない実装なので `tools` と同じく値を読まずに捨てます。「必ずツールを呼べ」という
  指示どおりの応答にはなりません。
- **`usage`（トークン数）を返しません。** 偽の数値を返さないためです。
- **入力はテキストと画像、出力はテキストのみ。** `input_image` は `image_url` が
  `data:image/{png,jpeg,gif,webp};base64,...` の data URL か `http(s)` の URL のときに扱えます。
  それ以外の形式・音声・ファイル添付は 400 で拒否します。
  `system` / `developer` ロールへ画像を置くこともできません（`--append-system-prompt` が文字列のため）。
- **画像は本文サイズに直接効きます。** data URL は base64 の分だけ膨らみ、Codex は会話履歴として
  毎回送り直します（CLI へ渡し直すのは新しい発言だけですが、受信サイズには効きます）。
  全画面スクリーンショット 1 枚で約 5MB になるため、既定の
  `CLAUDE_BRIDGE_MAX_REQUEST_BYTES` は 32MB にしてあります。超えると 413 で拒否します。
- `temperature` / `top_p` / `max_output_tokens` などのサンプリングパラメータは
  Claude CLI に対応する引数がないため**黙って無視されます**。
- リクエストの `model` はレスポンスにそのまま反映しますが、**CLI に渡すモデルはサーバー起動時の
  `--model` 固定**です。クライアントから任意のモデル文字列を CLI に流さないための意図的な設計です。
  `GET /v1/models` に載る `claude-opus-5`（既定）も**API で受け付ける名前にすぎません。**
  この名前で来たリクエストも Claude Opus（起動時の `--model`）が応答します。
- **CLI 出力の総量に上限はありません。** 標準出力は 1 行ずつ読んで捨てるため総量はメモリに
  影響せず、実測では 8 割超をツール結果の行が占めます。ファイルを何十個も読むような作業では
  本文の長さと無関係に総量が伸びるため、総量での打ち切りは正常な長い作業だけを落とします。
  1 行の長さにも上限はありません（読み終えてからしか判定できず、確保を防げないため）。
  暴走に対する歯止めは `--timeout` が担います。
- 1 リクエストにつき CLI プロセスを 1 つ起動します。並行リクエストはプロセス数分のリソースを消費します。

## セキュリティ上の注意

- 既定の bind 先は `127.0.0.1` です。ループバック以外（`0.0.0.0` など）に bind する場合、
  `CLAUDE_BRIDGE_API_KEY` の設定が**必須**で、未設定なら起動時に失敗します。
- **任意のシェル文字列を実行しません。** CLI は必ず引数配列で `subprocess.Popen` に渡し、`shell=True` は使いません。
  プロンプトは引数ではなく標準入力から渡します。
- **`--dangerously-skip-permissions` は使いません。** 権限チェックは Claude CLI の既定動作のままです。
- `--strict-mcp-config` を常に付けて起動するため、**ユーザー設定の MCP サーバーは一切読み込まれません。**
  既定では `--mcp-config` も渡さないので、外部 MCP サーバーは 1 つも起動しません。
  `--enable-codex-mcp` を明示したときだけ、同梱のアダプタ 1 つが追加されます。
  そのときも `--allowedTools` で許可するのはそのアダプタのツールだけです。
- ログにはメソッド・パス・ステータス・エラーコード・出力文字数のみを記録します。
  **プロンプト本文・CLI 出力・`Authorization` ヘッダの値・URL のクエリ文字列はログに出しません。**
  クエリで秘密値を渡すクライアントがあるため、記録するのはクエリを除いたパスだけです（404 のエラー本文も同様）。
- トークン比較は `hmac.compare_digest` を使います。
- `GET /health` だけは認証なしで応答します（返すのは `{"status": "ok"}` のみ）。
  `GET /v1/models` と `POST /v1/responses` は `CLAUDE_BRIDGE_API_KEY` 設定時に認証必須です。
- `--working-dir` で指定したディレクトリが Claude CLI の作業ディレクトリになります。
  CLI はそのディレクトリのファイルを読めるため、**信頼できるディレクトリを指定してください。**
  `--add-dir` を足すとその範囲が広がります。既定では 1 つも渡しません。
- 中継が有効なとき、`--claude-models` 以外のリクエストは**本文もヘッダも解釈せず上流へ渡します。**
  `Authorization` もそのまま転送するため、ブリッジを経由させる上流は信頼できる相手に限ってください。
- このブリッジ自体に認証・レート制限・監査ログはこれ以上ありません。ローカル利用のみを想定しています。

## テスト

```bash
cd claude-bridge
python -m unittest discover -s tests
```

`subprocess.Popen` はテスト内で差し替えるため、実際の Claude CLI も `codex app-server` も
起動しません（課金も発生しません）。スレッド作成などの実際の RPC もテストでは実行せず、
転送内容の検証だけを行います。上流への中継はテスト内のスタブ HTTP サーバーへ向けて検証するため、
外部へは接続しません。
