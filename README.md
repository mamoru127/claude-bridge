# claude-bridge

ローカルの CLI エージェント（Claude CLI `claude -p` と Antigravity CLI `agy -p`）を
**OpenAI Responses API 互換の HTTP エンドポイント**として公開する小さなブリッジです。
Codex など「OpenAI 互換のベース URL」を指定できるツールから、Claude と Gemini を呼び出せるようになります。
どちらの CLI を実行するかは**リクエストのモデル名で決まります**（モデル名の偽装ではありません）。

- 特定のアプリケーションから独立しています。Python 標準ライブラリだけで動作します（追加依存なし）。

## 構成

```
claude-bridge/
├── claude_bridge/
│   ├── __main__.py       起動エントリポイント
│   ├── config.py         設定（環境変数・コマンドライン引数）と起動前バリデーション
│   ├── server.py         HTTP サーバー（認証・入力検証・モデルの振り分け）
│   ├── responses.py      Responses API のリクエスト解釈とレスポンス生成
│   ├── claude_cli.py     Claude CLI の非対話実行と stream-json 出力の逐次解釈
│   ├── antigravity_cli.py Antigravity CLI（agy）の headless 実行と NDJSON 出力の逐次解釈
│   ├── cli_process.py    CLI プロセスの起動・行読み・タイムアウト・回収（両 CLI で共通）
│   ├── upstream.py       ローカルの CLI が受け持たないモデルの上流への中継
│   ├── mcp_adapter.py    Codex app-server の RPC を MCP tool として公開する stdio MCP サーバー
│   ├── app_server.py     常駐 `codex app-server` と話す JSON-RPC クライアント
│   │                     （接続を持つのはブリッジ本体。アダプタは HTTP で頼むだけ）
│   ├── mobile_browser_patch.py  スマホRemoteの新規スレッドをMac版へバックグラウンド表示
│   ├── thread_preview_patch.py  自動作成タスクの初期依頼を一覧用previewへ保存
│   ├── uds_websocket.py  制御ソケット（UNIX ソケット上の WebSocket）クライアント
│   └── errors.py         OpenAI error 形式に変換できる共通エラー型
├── launchd/
│   ├── local.claude-bridge.plist     ブリッジを常駐させる launchd ユーザーエージェント
│   ├── local.codex-app-server.plist  共有 app-server を常駐させるユーザーエージェント
│   ├── codex-app-server-parent.mjs           内蔵ブラウザの署名検査を通す app-server 親プロセス
│   ├── launch-app-tools.py                  起動中の ChatGPT アプリへ MCP を接続
│   ├── install.sh                           ブリッジの plist を登録し、起動を確認する
│   ├── install-mobile-browser-route.sh      スマホRemoteのBrowser route処理を登録する
│   └── install-app-server.sh                app-server の plist を登録し、応答を確認する
└── tests/
    ├── test_bridge.py           HTTP・Responses API・Claude CLI 実行
    ├── test_antigravity.py      Antigravity CLI 実行・会話継続・モデル一覧
    ├── test_app_server_mcp.py   MCP アダプタ・app-server クライアント・CLI 引数
    └── test_uds_websocket.py    制御ソケットのハンドシェイクとフレーム処理
```

## 前提条件

1. **Python 3.11 以上と Claude CLI がインストール済みであること**（macOS は `which claude`、Windows は `Get-Command claude` で確認）
2. **Claude CLI の認証が済んでいること**
   あらかじめターミナルで `claude` を一度起動してログインを完了させてください。
   ブリッジ用にも Anthropic 用にも API キーは不要です。未認証のままだとブリッジは CLI の異常終了を
   `502 claude_cli_failed` として返します。
   動作確認: `claude -p "ping" --output-format json`
3. **（任意）Antigravity CLI がインストール済みで、Google アカウントでログイン済みであること**
   `gemini-3.8-flash-*` のモデルを使う場合のみ必要です。未インストールなら、そのモデルを選んだ
   リクエストだけが `500 antigravity_cli_not_found` で失敗し、Claude と上流中継には影響しません。

   ```bash
   curl -fsSL https://antigravity.google/cli/install.sh | bash   # ~/.local/bin/agy へ入る
   agy                                                          # 初回だけ対話でログイン
   ```

   ログインはブラウザの OAuth で、キャッシュされた資格情報を headless が使います。
   動作確認: `agy -p "ping" --model gemini-3.8-flash-medium --output-format stream-json`

## 起動

```bash
cd claude-bridge
python -m claude_bridge
```

既定で `http://127.0.0.1:8787` を待ち受けます。

### Windows（PowerShell）

Claude CLI を Windows 側へインストールしてログインした後、リポジトリのルートで起動します。

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m claude_bridge --working-dir (Get-Location).Path
```

別のプロジェクトで Claude にファイル操作を許可するときは、`--working-dir C:\path\to\project` を指定します。
`Get-Command claude` が `.cmd` を指す npm インストール版も起動できます。ヘルスチェックは
`Invoke-RestMethod http://127.0.0.1:8787/health` で確認してください。
Windows では HTTP ブリッジ、Claude CLI、上流中継を利用できます。`launchd/` の常駐設定、
UNIX ソケットを使う Codex app-server 共有、Mac 版 ChatGPT の Browser route は macOS 専用です。
Codex app-server の共有を有効にする `--enable-codex-mcp` は Windows では指定しないでください。
Antigravity CLI はプロンプトを `-p` 引数で受け取るため、OS 共通で UTF-8 の 16,000 バイトまでを
受け付けます。これを超える入力は CLI 起動前に `413 antigravity_cli_prompt_too_large` を返します。

| メソッド・パス | 内容 |
| --- | --- |
| `GET /health` | `{"status": "ok"}` を返すヘルスチェック |
| `GET /v1/models` | `--claude-models` と `--antigravity-models` で指定した名前を並べたモデル一覧 |
| `POST /v1/responses` | Responses API 本体。`stream` の真偽で JSON と SSE を切り替える |
| `POST /v1/images/generations` | 画像生成リクエストを上流へ素通しする（`--upstream-base-url` 必須） |
| `POST /v1/images/edits` | 画像編集リクエストを上流へ素通しする（`--upstream-base-url` 必須） |

画像 API の中継時は、Codex から受け取った `Authorization` を上流へそのまま渡します。
上記以外のパスは 404 を返します。

ルーティングはクエリ文字列を除いたパスで判定します。Codex は `GET /v1/models?client_version=0.144.5` の
ようにクエリを付けて送りますが、クエリの内容は読まずに無視します。

### モデルの振り分け

`--upstream-base-url` を指定すると、ブリッジは 1 つのエンドポイントで**モデル名ごとに行き先を変えます。**

| リクエストの `model` | 行き先 |
| --- | --- |
| `--claude-models` にある名前（既定は `claude-opus-5-5` と `claude-fable-5-1`） | ローカルの Claude CLI が処理する |
| `--antigravity-models` にある名前（既定は `gemini-3.8-flash-high` と `gemini-3.8-flash-medium`） | ローカルの Antigravity CLI（`agy`）が処理する |
| それ以外（`gpt-5.4` などの gpt 系すべて） | 受信ヘッダと本文のまま上流へ素通しする |

同じ名前を両方の一覧へ入れると、どちらの CLI で実行するかが名前から決まらなくなるため起動時に失敗します。
振り分けに失敗しても別の CLI や別のモデルへ**切り替え直すことはしません**。選ばれた CLI が失敗したら、
その理由をそのままエラーとして返します。

素通しの場合、ブリッジは本文を解釈せず、`Authorization` を含む受信ヘッダを転送して上流の応答を
そのまま返します（SSE も上流のチャンクを流すだけ）。転送しないのは接続単位のヘッダ
（`Connection` や `Transfer-Encoding` など）と、接続を張り直す際に付け替わる `Host` / `Content-Length`
だけです。**ブリッジ自身は上流の資格情報を持ちません。** 受信パスの `/v1` は中継先 URL のパスへ
置き換わるため、`POST /v1/responses` は `https://chatgpt.com/backend-api/codex/responses` へ届きます。

画像 API はモデル名による振り分けを行わず、`/v1/images/generations` と `/v1/images/edits` を常に
上流へ素通しします。生成の JSON 本文だけでなく、編集で使う `multipart/form-data` も解釈せず転送し、
上流のステータス・ヘッダ・本文をそのまま返します。`--upstream-base-url` がない場合は
`upstream_not_configured` を返します。

`GET /v1/models` も同じ考え方で、上流の一覧を先に並べ、`--claude-models` と `--antigravity-models` の
名前を後ろへ足して返します。Codex アプリのモデル選択に GPT と Claude と Gemini が並ぶのはこのためです。

`--upstream-base-url` を省略すると中継はせず、**一覧にない名前で来たときは Claude CLI が処理します。**
Antigravity CLI は明示的に割り当てた名前だけを処理します（`agy` は `opus` のような Claude の別名を
解釈できないため、未知の名前の受け皿にはできません）。

### オプション

| 引数 | 環境変数 | 既定値 | 説明 |
| --- | --- | --- | --- |
| `--host` | `CLAUDE_BRIDGE_HOST` | `127.0.0.1` | bind するアドレス |
| `--port` | `CLAUDE_BRIDGE_PORT` | `8787` | bind するポート |
| `--claude-path` | `CLAUDE_BRIDGE_CLAUDE_PATH` | `claude` | Claude CLI の実行ファイルパス |
| `--antigravity-path` | `CLAUDE_BRIDGE_ANTIGRAVITY_PATH` | `agy` | Antigravity CLI の実行ファイルパス |
| `--model` | `CLAUDE_BRIDGE_MODEL` | `claude-opus-5-5` | `--claude-models` にない名前で来たときに CLI へ渡すモデル名 |
| `--timeout` | `CLAUDE_BRIDGE_TIMEOUT_SECONDS` | `3600` | CLI 実行のタイムアウト秒。CLI の暴走に対する唯一の歯止め |
| `--max-request-bytes` | `CLAUDE_BRIDGE_MAX_REQUEST_BYTES` | `33554432` | CLI へ 1 回で渡す入力の上限バイト数。Claude API のリクエスト上限に合わせる。リクエスト全体ではなく、組み立てた入力そのもの（続きのターンでは差分の発言と system プロンプト）に対して判定する |
| `--max-upstream-request-bytes` | `CLAUDE_BRIDGE_MAX_UPSTREAM_REQUEST_BYTES` | `536870912` | 受信するリクエストボディの上限バイト数。上流中継か CLI 実行かによらず、この値で受け取る |
| `--working-dir` | `CLAUDE_BRIDGE_WORKING_DIR` | カレントディレクトリ | CLI を実行する作業ディレクトリ |
| `--add-dir` | `CLAUDE_BRIDGE_ADD_DIRS` | なし | 作業ディレクトリの外で CLI にツール操作を許可する追加ディレクトリ。引数は繰り返し指定、環境変数は OS のパス区切り（macOS は `:`、Windows は `;`）。存在しないパスは起動時に失敗する |
| `--claude-models` | `CLAUDE_BRIDGE_CLAUDE_MODELS` | `claude-opus-5-5,claude-fable-5-1` | Claude CLI で処理するモデル名のカンマ区切り。先頭が `model` 省略時の既定。`GET /v1/models` が返す一覧に載り、CLI の `--model` へ渡す名前でもある |
| `--antigravity-models` | `CLAUDE_BRIDGE_ANTIGRAVITY_MODELS` | `gemini-3.8-flash-high,gemini-3.8-flash-medium` | Antigravity CLI で処理するモデル名のカンマ区切り。`--antigravity-models ""` で Antigravity を公開しない（環境変数の空文字は既定値扱い） |
| `--upstream-base-url` | `CLAUDE_BRIDGE_UPSTREAM_BASE_URL` | なし（中継しない） | ローカルの CLI が受け持たないモデルを中継する上流。Codex の既定は `https://chatgpt.com/backend-api/codex` |
| `--enable-codex-mcp` | `CLAUDE_BRIDGE_ENABLE_CODEX_MCP` | 無効 | Codex app-server の MCP アダプタを Claude CLI へ渡す（後述） |
| `--app-server-socket` | `CLAUDE_BRIDGE_APP_SERVER_SOCKET` | `$CODEX_HOME/app-server-control/app-server-control.sock` | 繋ぎ先の app-server 制御ソケット。Codex アプリが繋ぐ場所と同じ |
| `--mcp-config-file` | `CLAUDE_BRIDGE_MCP_CONFIG_FILE` | なし | 追加で有効にする MCP サーバーを書いた JSON ファイル（`mcpServers` 形式、後述）。起動時に読み込み、読めなければ起動しない |
| `--passthrough-tools` | `CLAUDE_BRIDGE_PASSTHROUGH_TOOLS` | `*` | 親 Codex がリクエストで提示したツールのうち Claude へ受け渡す名前。`*` は全 function と `tool_search`、カンマ区切りで個別指定、引数の空文字で無効 |
| `--tool-search-query` | `CLAUDE_BRIDGE_TOOL_SEARCH_QUERY` | なし | 個別指定した遅延ツールをブリッジが先回り検索するときの検索語。`*` では Claude 自身に `tool_search` を渡すため不要 |

例:

```bash
python -m claude_bridge \
  --claude-path /opt/homebrew/bin/claude \
  --model sonnet \
  --timeout 300 \
  --working-dir /path/to/project
```

ブリッジ自体に API キーの設定は不要です。接続先はループバックアドレスに限定されます。

### 常駐させる（launchd / macOS）

ログイン時に自動起動し、落ちても再起動するユーザーエージェントとして登録できます。

```bash
cd claude-bridge
launchd/install.sh
```

`launchd/local.claude-bridge.plist` の Python と作業ディレクトリのパスを埋めて `~/Library/LaunchAgents/` へ複写し、
`launchctl bootstrap` で登録して `/health` が応答するまで確認します。
何度実行しても構いません（登録済みなら入れ替え、8787 を掴んでいる手動起動のプロセスも止めます）。

起動引数を変えるときは**リポジトリ側の plist を編集してから `install.sh` を再実行**します。`--add-dir` や追加 MCP 設定は利用者ごとに必要なものだけ設定します。
`~/Library/LaunchAgents/` のコピーを直接編集しても、次の `install.sh` で上書きされます。

| 操作 | コマンド |
| --- | --- |
| 状態確認 | `launchctl print gui/$(id -u)/local.claude-bridge` |
| 再起動 | `launchctl kickstart -k gui/$(id -u)/local.claude-bridge` |
| 停止（登録解除） | `launchctl bootout gui/$(id -u)/local.claude-bridge` |
| ログ | `tail -f ~/Library/Logs/claude-bridge/bridge.log` |

`KeepAlive` を有効にしているため、**`kill` で止めても launchd が起動し直します。**
止めたいときは `bootout` を使ってください。再起動は `kickstart -k` が確実です。

**`install.sh` と `kickstart -k` はブリッジを作り直します。** そのときブリッジが起動した
Claude CLI も一緒に終わります。app-server は別サービスなので巻き込まれず、`turn_start_async` で
走らせているターンは続きます。

ブリッジ経由で動いている Claude（Codex アプリのスレッド）から `install.sh` を実行した場合は、
再起動を別プロセスグループへ切り離してから即座に戻ります。直接 `bootout` すると自分ごと
SIGKILL され、`bootstrap` へ進めず登録解除だけが残ってブリッジが上がらなくなるためです。
その接続は切れますが、Codex が約 60 秒後に再接続します。結果は
`~/Library/Logs/claude-bridge/install.log` に残ります。

plist で明示している点:

- **`caffeinate -s`**: python を `caffeinate -s` 経由で起動し、AC 電源のときだけシステムスリープを
  抑止します。スリープするとブリッジも app-server も止まり、待たずに始めたターンが途中で死ぬためです。
  バッテリー駆動では抑止しないので、外出時に電池を使い切ることはありません。
- **`PATH`**: launchd はログインシェルの環境を引き継ぎません。`claude` と `codex` を見つけられるよう
  `/opt/homebrew/bin` を含む `PATH` を書いています。Homebrew の場所が変わったら追従が必要です。
- **`WorkingDirectory`**: `python -m claude_bridge` の import 元であり、`--working-dir` の既定値でもあります。
- **`CLAUDE_CODE_EFFORT_LEVEL: high`**: Claude CLI の推論エフォートです。ブリッジは Codex の `reasoning.effort` を
  CLI へ渡さないため、Codex アプリ側の設定は Claude には効きません。ブリッジ経由の Claude を
  常に high で動かすためにここで固定しています。
- **`ProcessType: Interactive`**: 応答を返すプロセスなので、バックグラウンドジョブ扱いの CPU 制限をかけません。
- **`ThrottleInterval`**: 設定不備で即死したときに再起動を繰り返さないための最小間隔です。
- **`CLAUDE_BRIDGE_MCP_CONFIG_FILE`**: 追加 MCP が必要な場合だけ plist に設定します。
  設定したファイルが存在しないとサービスは起動しません。中身は[後述の例](#他の-mcp-サーバーを追加するオプトイン)を参照してください。

ログは追記され続けるため、肥大化したら手動で削除してください（ローテーションは設定していません）。

Claude CLI の認証情報は login keychain にあります。ログインセッションのユーザーエージェントとして
動くため keychain は解錠済みですが、**ログインしていない状態では動きません**（LaunchDaemon ではないため）。

### スマホRemoteから内蔵Browserを使う

ChatGPTの内蔵Browserは、Mac版が同じスレッドを表示してBrowser routeを登録している必要があります。
iOS/Android Remoteで作られた新規スレッドをMac版へバックグラウンド表示する処理は、次で常駐化します。

```bash
cd claude-bridge
launchd/install-mobile-browser-route.sh
```

新規セッションの `originator` が `codex_chatgpt_ios_remote` または
`codex_chatgpt_android_remote` の場合だけ、`open -g codex://threads/<thread-id>` を一度実行します。
ChatGPTを前面には出さず、ユーザーのターンを再送・二重実行もしません。モデルの種類には依存しません。

同じサービスで、`codex_app.create_thread` の初期依頼を一覧用 `preview` へ保存します。
Codex 0.153.4 はツール出力だけで開始したタスクの `preview` を作らず、一覧APIで
除外してしまうためです。起動時に既存の未表示タスクを処理し、その後は1秒間隔で
DBの変更を確認します。DBに変更がない間はタスクを再検索しません。

対象は非アーカイブの `agent_created_thread` で、`preview` と最初のユーザー発言が
空、かつ最初の履歴が `codex_app.create_thread` の出力であるタスクだけです。
初期依頼から委譲用タグを外して `state_5.sqlite` の `preview` だけを更新します。
会話履歴は読み取り専用で扱い、既存の概要・表示名・実行状態は変更しません。
更新したタスクのIDだけを `~/Library/Logs/codex-mobile-browser-route/route.log` に記録します。

対象となる一覧経路は `thread/list` の `useStateDbOnly: true` です。
`false` の履歴ファイル走査経路では、本体が初期ツール出力を認識しない問題が残ります。
この経路まで直すにはapp-server本体の修正が必要で、ユーザー発言の偽装や履歴の書き換えは行いません。

この処理は `state_5.sqlite` と `thread_history_1.sqlite` のページ分割履歴形式に
対応しています。本体側でツール出力から `preview` が生成されるようになったら
削除できます。アプリ更新でDBスキーマが変わった場合はこの処理も更新が必要です。
反映時は上の登録コマンドで連携サービスだけを再起動します。app-serverと実行中タスクは停止しません。

## Codex の設定

### CLI（プロファイル切り替え）

```toml
[model_providers.claude_bridge]
name = "Claude CLI bridge"
base_url = "http://127.0.0.1:8787/v1"
wire_api = "responses"

[profiles.claude]
model_provider = "claude_bridge"
model = "claude-opus-5-5"
```

利用時: `codex --profile claude`

ローカル利用では `env_key` を指定しません。Claude CLI のログイン情報を使い、
Codex とブリッジの間に別の API キーは設けません。

### Codex デスクトップアプリ（トップレベル `model_provider` が必要）

**デスクトップアプリには `--profile` を渡す手段がありません。** アプリは `codex app-server --listen stdio://`
としてバックエンドを起動するため、`[profiles.*]` に書いた `model_provider` は適用されません。
アプリの既定経路をブリッジへ向けるには、**トップレベルに `model_provider` を書く**必要があります。

```toml
model = "claude-opus-5-5"
model_provider = "claude_bridge"
```

アプリのモデル選択を `claude-opus-5-5` にしておくと、その名前でブリッジにリクエストが届きます。
ブリッジは `GET /v1/models` でこの名前を公開して受け付け、**そのまま Claude CLI の `--model`
へ渡します。** 既定では `claude-opus-5-5`（Claude Opus 5.5）と `claude-fable-5-1`（Claude Fable 5.1）を
公開しているので、アプリのモデル選択がそのまま実行モデルの切り替えになります。
どちらを選んでも応答を生成するのは Claude で、OpenAI のモデルは一切呼び出されません。

**この名前は上流に実在しないものを選んでください。** 中継が有効なとき、ブリッジは上流の
モデル一覧から同じ名前を取り除いて自分の項目に置き換えます。上流に実在する名前
（`gpt-5.4` など）を指定すると、その GPT モデルがアプリの選択肢から消えます。

`config.toml` を書き換えた後は、設定を読み直させるために **Codex デスクトップアプリを再起動してください。**

### モデル選択に出てこないとき

Codex は、リクエストのモデル名を `GET /v1/models` で受け取った一覧と突き合わせ、**見つからない
名前をフォールバックのメタデータへ落とします。** フォールバックは `visibility` が `none` なので、
**ブリッジ側で `"list"` を宣言していても選択肢から消えます。**

照合は完全一致ではなく前方一致で、一致した中の最長が勝ちます。突き合わせ先は Codex に
埋め込まれた既知モデルの一覧ではなく、**`/v1/models` の応答そのもの**です。上流に実在しない
名前を公開できるのはこのためで、ブリッジが一覧に載せている限り「既知」として扱われます。

裏を返すと、**一覧を返せない状態では Claude の項目ごと消えます。** 次の順で確認してください。

1. `GET /v1/models` が 200 を返すか。中継が有効なとき、**上流の一覧取得に失敗すると一覧全体が
   `upstream_models_failed` で 502 になります**（Claude の項目も返りません）
2. 返せていても項目が欠けていないか。ブリッジは上流のエントリを雛形にして必須項目を継承します。
   **中継が無効だと雛形が空になり、Codex の版が要求する必須項目を満たせずに一覧全体の解釈が
   失敗します**
3. Codex 側に `model_catalog_json` が設定されていないか。設定されていると Codex は `/v1/models` を
   **参照しません。** その場合はカタログ JSON 側へ項目を足す必要があります

Codex はモデル一覧を `~/.codex/models_cache.json` に保存します。ブリッジが返した名前がここに
入っていれば、一覧は正しく届いています。

## Antigravity CLI（agy）

`--antigravity-models` に載せた名前で来たリクエストは、Antigravity CLI を headless で実行します。

```bash
agy -p "<プロンプト>" --model gemini-3.8-flash-high --output-format stream-json \
  --dangerously-skip-permissions
```

標準出力は NDJSON で、各行は `event` で種別を示し、本体を同じ名前のキーへ入れ子で持ちます
（agy 1.1.13 の実出力で確認）。

```json
{"event":"init","conversation_id":"<id>","init":{"model":"...","cwd":"...","tools":["..."]}}
{"event":"step_update","step_update":{"step_index":2,"state":"ACTIVE",
                                      "step_type":"agent_response","text_delta":"こん"}}
{"event":"result","result":{"conversation_id":"<id>","status":"SUCCESS","response":"こんにちは"}}
```

ブリッジは `agent_response` の `text_delta` を**最終回答の増分**として、`step_type` が `tool` の
step を**ツール進捗**として Codex へ送り、`result.response` を確定した本文とします。
`text_delta` は本物の増分で、`ACTIVE` と最後の `DONE` を連結すると `response` と一致します
（実測: 150行の応答で 5 個の増分に分割され、連結が確定本文と完全一致）。
`status` は `SUCCESS` / `ERROR` / `CANCELED` などの**大文字**です。

`--print-timeout` の既定は 5 分で、ブリッジの `--timeout` より先に打ち切られてしまうため、
ブリッジは同じ値を `--print-timeout` へ明示的に渡します。プロンプトは引数配列で渡すため、
シェルは介しません。

### 認証と課金

- **既定は Google アカウントのキャッシュ認証です。** ブリッジは認証を代行せず、トークンも
  Keychain の中身も読み書きしません。あらかじめターミナルで `agy` を一度起動してログインを
  済ませてください。この経路であれば Google AI Pro のベースライン枠で動き、API 利用料は発生しません。
- **API キー経路（従量課金になり得る）へは行きません。** ブリッジは agy を起動するとき、
  `GEMINI_API_KEY` / `GOOGLE_API_KEY` / `GOOGLE_GENAI_API_KEY` を**子プロセスの環境から
  取り除いてから渡します**。ブリッジを起動した環境にこれらが残っていても、agy へは届きません。
  （agy 側は設定の `modelProvider` を `"gemini"` にしない限りこれらを使いませんが、課金経路の
  判断を設定ファイルの状態に依存させないためです。）API キーで使いたい場合は、ブリッジを
  経由せず `agy` を直接実行してください。
- **AI Credit Overages や `useG1Credits` のような追加クレジットの設定を、ブリッジは変更しません。**
  従量課金へ自動で移行させる操作は一切行いません。課金設定の変更は Antigravity 側で明示的に
  行うものとして扱います。
- ログにプロンプト本文・CLI 出力・認証情報は出しません（Claude 側と同じ扱いです）。

### 会話の継続

初回の実行には `--conversation` を付けません。`result` が返した `conversation_id` を
ブリッジの会話キー（`prompt_cache_key`）へ記録し、2 回目以降を `--conversation <id>` で続けます。
Claude 側の `--session-id` / `--resume` とは別に持つため、同じスレッドで Claude と Antigravity を
切り替えても、相手の会話 ID を持ち込みません（切り替えた側は新しい会話として全履歴を渡し直します）。

会話 ID を記録するのは、**実行が最後まで正常に終わったターンだけ**です。`result` が
`conversation_id` を返さない場合は `502 antigravity_cli_invalid_output` でターンを失敗させます
（黙って毎回新しい会話になる状態を避けるためです）。`status` が `success` でない場合は
`502 antigravity_cli_error_result`、`result` の後に CLI が異常終了した場合は
`502 antigravity_cli_failed` になり、いずれも次のリクエストは全履歴を渡す新しい会話として
実行されます。

### サポート範囲（headless 契約でできないこと）

`agy` の headless 契約は「1 本のプロンプト文字列を渡して NDJSON を受け取る」だけです。
そのため次は**実現できません。黙って無視せず、明確なエラーか非対応として扱います。**

- **画像入力は扱えません。** `input_image` を含むリクエストは
  `400 unsupported_content_part` で拒否します（Claude のモデルを選べば従来どおり画像を送れます）。
- **Codex ネイティブツールの受け渡しはできません。** リクエストごとに変わるツール定義を
  `agy` へ登録する口がないため、Antigravity のモデルでは受け渡しも `tool_search` も行わず、
  ツールなしでそのまま実行します（`--passthrough-tools` は Claude のモデルにだけ効きます）。
  これは Antigravity 自身が持つワークスペースのスキルや MCP とは別の話で、そちらは
  `agy` の設定に従ってそのまま動きます。
- **agy 自身のツールはすべて自動承認します。** headless では対話の承認画面に答えられないため、
  ブリッジは `--dangerously-skip-permissions` を付けて起動します。ファイル操作の対象には
  `--working-dir` と共通の `--add-dir` を渡します。コマンド実行も確認なしで行われるため、
  信頼できるプロンプトとディレクトリだけをブリッジへ渡してください。
- `system` / `developer` の内容は捨てず、プロンプト先頭の `[system]` 節として渡します
  （`agy` に `--append-system-prompt` 相当の引数がないため）。複数発言の会話も
  `[user]` / `[assistant]` のラベル付きで 1 本のプロンプトへまとめます。
- **プロンプトはコマンドライン引数で渡します。** 極端に長い会話では OS の引数長上限
  （macOS で約 1MB）に当たり、`500 antigravity_cli_launch_failed` になります。
  黙って切り詰めることはしません。
- `reasoning.effort` は読みません。思考の強さはモデル名の `-high` / `-medium` で選びます。

### 実 CLI での確認結果（agy 1.1.13 / 2026-08-16）

一時ポートのブリッジ（18787）から実際の `agy` を呼び、次を確認済みです。

- `GET /v1/models` に Claude 2 件と Antigravity 2 件が並ぶ（`owned_by` は `anthropic` / `google`）
- 非ストリームの応答（`gemini-3.8-flash-medium`）
- ストリーミングで増分が 5 個に分割され、連結が確定本文と完全一致
- 同じ `prompt_cache_key` の 2 ターン目が `--conversation` で継続し、1 ターン目の内容を記憶
- 画像付きリクエストが `400 unsupported_content_part`
- 同じブリッジで Claude のモデルが従来どおり動く

`agy models` で確認した実在のモデル名は `gemini-3.8-flash-{high,medium,low}`、
`gemini-3.7-flash-*`、`gemini-3.6-flash-*`、`gemini-3.1-pro-{high,low}` などです。
`--effort` という独立フラグもありますが、思考の強さはモデル名にも含まれるため、ブリッジは
モデル名だけで指定し `--effort` を渡しません。

### Codex サブエージェントからの依頼

`spawn_agent` の `model` に登録済みの Claude / Gemini 名を指定できます。Codex の
モデル候補の説明は先頭5件までなので、一覧に見えなくてもモデル名の直接指定は可能です。
モデルを上書きするときは `fork_turns="none"` または必要なターン数を指定します。

collaboration v2 の `agent_message` は、初回依頼・追送・エージェントからの報告を
送信元と宛先付きのテキストとして CLI へ渡します。暗号化された本文は復号できないため
400 で拒否します。黙って依頼を落として CLI を実行することはありません。

## Codex app-server ツール（オプトイン）

Codex アプリのモデルを Claude へ流している状態でも、**Claude 側から Codex app-server の
RPC を呼べるようにする**ための仕組みです。**既定では無効**で、明示的に有効化したときだけ動きます。

ここで作ったスレッドのターンは、**ブリッジと Codex アプリが共有する 1 つの app-server** で
走ります。スレッドの状態は、それを走らせているプロセスのメモリにしかありません
（`~/.codex/state_5.sqlite` にもセッションファイルにも実行中を示す列はない）。
別プロセスを立てるとアプリからは `notLoaded` に見えるため、共有するのが条件になります。

この仕組みは、Codex 自身が実行する `--passthrough-tools` の `create_thread` とは別経路です。
受け渡しはアプリの選択モデルが code mode でないことが前提で、そちらが使えなくなったときも
app-server の RPC は動きます。

> 2026-07-31 に上流のモデル一覧の先頭が `gpt-5.6-sol`（`tool_mode: "code_mode_only"`）へ入れ替わり、
> Claude 用エントリが雛形ごとその宣言を受け継いだ結果、受け渡しが一度止まりました。code mode の
> Codex はツールを function tool として送らず `exec` のコード実行側（`tools.*`）へ寄せるため、
> `additional_tools` が `exec` / `wait` / `request_user_input` / `collaboration` だけになり、
> `tool_search` も遅延読み込みの `create_thread` も会話へ入りません。`_codex_model` で
> `tool_mode` を `null` に打ち消して直してあります。`model_info.supports_search_tool` は
> 雛形から引き継いだ `true` のままで、原因ではありませんでした。

```
Codex アプリ ──HTTP──> claude-bridge ──> claude --print --mcp-config ...
                 │        │  ▲                  │
                 │        │  └── HTTP ──── mcp_adapter <── MCP ──┘
                 │        │   /app-server/rpc
                 │        └─ JSON-RPC ─┐
                 └─ JSON-RPC ──────────┴─> codex app-server（共有・制御ソケット）
```

**app-server への接続を持つのはブリッジ本体です。** アダプタは Claude CLI の起動ごとに
生まれて消えるため、アダプタが持つと再起動のたびに実行中のターンが道連れになります。
常駐するブリッジ側へ寄せることで、`turn_start_async` で始めたターンはアダプタの寿命と無関係に
走り続けます。

### 有効化

```bash
CLAUDE_BRIDGE_ENABLE_CODEX_MCP=1 python -m claude_bridge
```

または

```bash
python -m claude_bridge --enable-codex-mcp --app-server-socket ~/.codex/app-server-control/app-server-control.sock
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
      "url": "https://example.com/mcp"
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
| `create_thread` | `thread/start` → `thread/name/set` → `turn/start` | 名前と初期プロンプト付きの別タスクを作り、完了を待たずに開始する |
| `thread_start` | `thread/start` | 新しいスレッドを作成する（ターンは開始しない） |
| `thread_name_set` | `thread/name/set` | スレッドの表示名を設定する |
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

通常は `create_thread` に `cwd`、`title`、`prompt`、必要なら `model` を渡します。
内部でスレッド作成、表示名設定、最初のターン開始を直列に行い、ターンの完了を待たずに
`threadId` と `turn` を返します。`cwd` が保存済みプロジェクトのパスと一致すると、実測では
Codex アプリのそのプロジェクト配下に表示されます。

低水準の RPC を個別に操作する場合は `turn_start_async` を使います。
アプリを共有 app-server へ繋がせていれば実行中の表示が出ます。繋がせていない場合は
`notLoaded` のままなので、進捗は次の手順で自分で読み取ります。

1. `thread_start` でスレッドを作る（`cwd` に作業ディレクトリ、必要なら `modelProvider`）。
2. `thread_name_set` で一覧表示用の名前を設定する。
3. `turn_start_async` で最初のターンを投げる。ここで戻り値は即座に返る。
4. 以降は `thread_read`（`includeTurns=true`）か `thread_turns_list` で状態を見る。
   `status` は `inProgress` → `completed` / `failed` / `interrupted` と変わる。

**スレッドは `thread_start` だけでは Codex アプリの一覧に出ません。** セッションファイル
（`~/.codex/sessions/...jsonl`）が作られるのは最初のターンを開始したときなので、
一覧に並べたいならターンの投入までを 1 セットで行ってください。

止まる条件は 2 つだけです。**共有 app-server を止める**か、**Mac がスリープする**か。
ブリッジ本体を止めてもターンは止まりません（app-server が別サービスとして常駐しているため）。
スリープは plist の `caffeinate -s` で、AC 電源のときだけ抑止しています。

### 共有 app-server

`codex app-server --listen unix://` を launchd で常駐させ、ブリッジはその制御ソケット
（`$CODEX_HOME/app-server-control/app-server-control.sock`）へ繋ぎます。ブリッジは
app-server を起動しません。ソケットが無ければ繋げず、その旨を返します。

```bash
launchd/install-app-server.sh   # 共有 app-server を登録し、応答を確認する
launchd/install.sh              # そのあとブリッジを登録する
```

接続設定だけを既存環境へ適用する場合は、次を使います。共有サーバーや実行中のタスクは
停止しません。アプリの再起動は、アプリ側で実行中のタスクを止めてから行ってください。

```bash
launchd/install-app-server.sh --connection-only
```

このコマンドは Bridge と同じ `python3` を使い、リポジトリ管理の
`launchd/launch-app-tools.py` を `~/.local/share/codex-shared-connection/` へ配置します。
アプリ同梱の `desktop-mcp.json` を元に、Codex の設定 API で `mcp_servers.codex_app` を
登録し、MCP設定を再読み込みして `create_thread`、`list_projects`、`list_threads`、
`read_thread`、`set_thread_title` の取得を確認します。既存の承認設定と他のMCP設定を保ち、
`omit_tools_from` の `deferred` 除外だけを解除します。個別ツールの無効化・許可リストは
勝手に解除せず、必要ツールが無効なら失敗として報告します。

実行前に `/Applications/ChatGPT.app` を起動してください。旧 `/Applications/Codex.app` と
同じアプリ識別子のため、旧アプリが動いていると新アプリへの起動要求がそちらへ渡る場合が
あります。接続先が見つからない場合は、旧アプリの作業状況を確認して終了し、新アプリを
起動してから再実行します。インストーラー自身はアプリや実行中タスクを終了しません。

設定を変更せず、共有サーバーの公開状況だけを確認する場合（`claude-bridge/` 内で実行）:

```bash
python3 -m claude_bridge.app_tools_setup --check
```

検証は共有サーバーのカタログと有効設定を対象とします。すでに実行中のターンへの反映や
実際のタスク作成成功までは保証しません。新しいターンでツール一覧を確認してください。

- **同じ app-server に Codex アプリも繋がせると、実行中の表示がアプリに出ます。**
  インストーラーは `CODEX_APP_SERVER_WS_URL=ws+unix://localhost$CODEX_HOME/app-server-control/app-server-control.sock:/rpc`
  を現在のログイン環境へ設定し、`local.codex-shared-connection` LaunchAgentで
  次回ログイン時にも設定します。`CODEX_HOME` 未指定なら `~/.codex` です。
  この設定が読み込まれた後にアプリを起動してください。接続設定とアプリのログイン項目の
  実行順序は保証されないため、ログイン直後に接続が分かれた場合はアプリを再起動します。
  UNIXソケット名をURLに埋め込むため、空白や `:` などを含む `CODEX_HOME` は扱いません。
  `CODEX_APP_SERVER_FORCE_CLI=1` が設定されている場合は解除してください。
- **`CODEX_APP_SERVER_USE_LOCAL_DAEMON=1` だけでは十分ではありません。**
  2026-09-05に確認したアプリは起動時のMCP追加設定があると自前のサーバーを起動し、
  共有サーバーと競合しました。明示URLの `localhost` はアプリの外部向けプロキシを避けるためにも必要です。
  `409 Remote app server already online` と対象タスクの `already has an active writer` が
  同時に出る場合は、接続先が分かれていないか確認してください。タスクのロックファイルは削除しません。
- 接続設定の確認は `launchctl getenv CODEX_APP_SERVER_WS_URL` とアプリログの
  `transport=websocket`、`initialized=true`、`next=connected` で行います。
  サーバーの応答だけでは、アプリが同じサーバーに接続した証拠になりません。
  アプリ更新後はMacとスマホで同じタスクの履歴を開けることも確認してください。
- MCP起動時は `exec` でアプリ同梱のNodeへ置き換え、共有 app-server を親として維持します。
  シェルやPythonの子プロセスとして直接MCPを起動すると、親プロセスが異なるため
  `missing-code-signing-identity` が出る場合があります。その診断結果だけで正規経路も
  失敗していると判断せず、上の `--check` を使って確認します。署名検査は無効化しません。
- 制御ソケットは生の JSON ではなく、**UNIX ソケット上の WebSocket** です。`app-server proxy --sock`
  は生バイトを中継するだけなので、その先で WebSocket を話す必要があります（`uds_websocket.py`）。
- 実行ファイルは Codex アプリ内蔵のものを使います。アプリはソケット越しに得た app-server の
  バージョンで接続可否を決めるため、**アプリを更新したらこのサービスも再起動**が要ります。
  親プロセスが 60 秒ごとに実行ファイルの差し替えを見張り、見つけたら app-server を落として
  KeepAlive の再起動に入れ替えさせます。SIGTERM から 10 秒以内に終了しない場合は、子の
  プロセスグループを強制終了して古いソケットを確実に手放します。走っているターンは
  巻き込まれて終わります。
- **古い実行ファイルのまま走り続けた app-server は、プロジェクトのスキルを読めなくなります。**
  macOS のファイルアクセス許可は差し替え前のプロセスに紐づいたままになるため、`~/Documents`
  配下の `AGENTS.md` と `.agents/skills` の走査が `Operation not permitted` で失敗し、
  `/` コマンドからプロジェクトのスキルが消えます。アプリ側にエラーは出ず、
  `~/Library/Logs/codex-app-server/app-server.log` の
  `failed to scan skill path` だけが手がかりになります。手で直すなら次のとおりです。

  ```bash
  launchctl kickstart -k "gui/$(id -u)/local.codex-app-server"
  ```
- macOS版の内蔵ブラウザは接続元の署名を親プロセスまで検査します。launchdがapp-serverを直接
  起動するとブラウザ接続が拒否されるため、インストール時にラッパーを`~/.local/share/claude-bridge/`
  へ配置し、OpenAI署名済みのアプリ同梱Nodeを親にしてapp-serverを起動します。
- ツールが一度も呼ばれなければ、ブリッジはソケットへ繋ぎません。
- **Claude CLI もブリッジ本体も、終了して app-server を巻き込みません。**
  これが `turn_start_async` で始めたターンを走り切らせるための条件です。
- 接続は 1 本しかないため、**受け取りは専用スレッド 1 本だけ**が行い、
  `id` で応答を、`threadId` で通知を待ち手へ振り分けます。送信だけをロックで直列化するので、
  `turn_start` が完了を待っている間も `thread_read` などの呼び出しは通ります。
  待ち手のいない通知（＝待たずに始めたターンの経過）はその場で捨てるため、溜まりません。
- **RPC のエラー応答と 1 件の時間切れでは接続を切りません。** 通信の失敗ではないためです。
  ここで切ると、進捗確認が 1 回失敗しただけで、走っている全部のターンが `interrupted` になります。
  切るのは、接続が閉じた／JSON として読めない／書き込めない、というつなぎ目の failure のときだけです。
- app-server のログは launchd 側（`~/Library/Logs/codex-app-server/app-server.log`）に出ます。

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

1. `instructions` と `input` からシステムプロンプトとユーザープロンプトを抽出（`tools` は読まずに捨てる）
2. `claude --print --output-format stream-json --input-format stream-json --verbose
   --thinking-display summarized --strict-mcp-config --model <model>
   [--mcp-config <JSON> --allowedTools mcp__codex_app_server] [--append-system-prompt <system>]` を
   **引数配列で**起動し、プロンプトは標準入力から渡す
   （`--mcp-config` が付くのは `--enable-codex-mcp` を指定したときだけ）
3. CLI が標準出力へ 1 行ずつ吐く JSON を読みながら、`result` の最終テキストを
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
| `upstream_not_configured` | 503 | 画像 API の中継先が設定されていない |
| `upstream_unreachable` | 502 | 中継先へ接続できない（`--upstream-base-url` 指定時） |
| `upstream_models_failed` / `upstream_models_invalid` | 502 | 上流のモデル一覧を取得・解釈できない |

`stream=true` の場合、CLI 実行に入った後のエラー（`claude_cli_*`）は上表の HTTP ステータスではなく
`response.failed` イベントとして届きます。リクエスト検証段階のエラーは SSE 開始前なので通常どおり JSON です。

## 制約（既知の未対応事項）

- **Claude では本文のストリーミングが「見かけ上」です。** 実行中の様子は `reasoning`（思考・
  ツール使用）と作業コメント（`message`）で逐次届きますが、いずれもブロックが確定してから
  全文を 1 個の `delta` として流すため、文字が少しずつ届くわけではありません。最終テキストが
  返り始めるまでの待ち時間は非ストリームと同じです。Claude CLI が本文を `result` でしか
  返さないためです。
  Antigravity では `step_update` の `text_delta` をそのまま `response.output_text.delta` として
  流すため、本文が少しずつ届きます（実測: 150行の応答で 5 分割）。確定した本文として載せるのは
  `result.response` の方です。
- **会話は CLI のセッションとして保持します。** リクエストの `prompt_cache_key`（Codex がスレッド
  ごとに送る固定値）を CLI 側の会話 ID に対応づけ、Claude は初回 `--session-id` / 2 回目以降
  `--resume`、Antigravity は 2 回目以降 `--conversation` で続きから実行します。CLI へ渡すのは
  前回から増えた発言だけです。`previous_response_id` は受理しますが値は読まずに捨てます。
  - 実行する CLI が前回と変わった会話は、続きにせず新しい会話として全履歴を渡し直します。
  - 中断されたターンの読み込み結果やツール実行結果も CLI 側の転記に残るため、割り込み後の
    次のメッセージは続きから動きます。
  - 同じ会話のリクエストは 1 本ずつ実行します。実行中に同じ会話の新しいリクエストが来たときの
    扱いは、先行する実行の相手が残っているかで決まります。切断後の再送や割り込みでは相手が
    すでにいないため、先行する CLI を落として引き継ぎます。サイド会話のように相手が残っている
    場合は落とさず、待たせもせず、その場限りのセッションで並行して実行します。並行して届いた
    側は履歴が枝分かれしていてどのみち転記を続けられないため、会話の続き先は先行する実行が
    持ったままにします。
  - Codex 側の履歴圧縮などで渡し済みの範囲と食い違った場合は、セッションを開始し直して
    全履歴を渡します。
  - 保持するのはブリッジのプロセス内メモリだけです（既定 64 会話、超えた分は古い順に破棄）。
    再起動すると対応づけは失われ、次のリクエストは全履歴を送る新しいセッションになります。
  - `prompt_cache_key` を送らないクライアントには、リクエストごとに使い捨てのセッションを割り当てます。
- **会話本文は `message`、`agent_message`、`function_call_output` から取得します。**
  `additional_tools` はツール受け渡し用に別途処理し、`function_call` などはプロンプトへ
  転記しません。本文が一つも残らなければ `invalid_request` で 400 になります。
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
- **入力はテキストと画像、出力はテキストのみ**（画像を扱えるのは Claude のモデルだけで、
  Antigravity のモデルはテキストのみ。前述の「Antigravity CLI（agy）」を参照）。
  `input_image` は `image_url` が
  `data:image/{png,jpeg,gif,webp};base64,...` の data URL か `http(s)` の URL のときに扱えます。
  それ以外の形式・音声・ファイル添付は 400 で拒否します。
  `system` / `developer` ロールへ画像を置くこともできません（`--append-system-prompt` が文字列のため）。
- **画像は本文サイズに直接効きます。** data URL は base64 の分だけ膨らみ、Codex は会話履歴として
  毎回送り直します。全画面スクリーンショット 1 枚で約 5MB、画像生成の結果も同じように積み上がります。
  上限は受信と CLI 実行で別に見ます。受信するリクエストボディは中継先によらず
  `CLAUDE_BRIDGE_MAX_UPSTREAM_REQUEST_BYTES`（既定512MB）、CLI へ 1 回で渡す入力は
  `CLAUDE_BRIDGE_MAX_REQUEST_BYTES`（既定32MB）を超えると 413 で拒否します。
  後者は履歴ぶんではなく、CLI へ渡す入力を組み立てたそのもの（stream-json の 1 行、または
  Antigravity へ渡すプロンプト文字列）で判定します。続きのターンで渡すのは新しい発言と
  毎ターン付け直す system プロンプトだけで、ツール結果の画像は Claude へ渡さず落とすため、
  履歴に画像が積み上がってもそのスレッドが 413 で止まることはありません。
- `temperature` / `top_p` / `max_output_tokens` などのサンプリングパラメータは
  Claude CLI に対応する引数がないため**黙って無視されます**。
- リクエストの `model` はレスポンスにそのまま反映し、**`--claude-models` / `--antigravity-models`
  に載せた名前のときだけ CLI の `--model` へ渡します。** 一覧にない名前は起動時の `--model` を
  Claude CLI で実行します。
  クライアントから任意のモデル文字列を CLI に流さないための意図的な設計で、CLI へ届くのは
  起動時に許可した名前だけです。`model` を省略したリクエストは一覧の先頭を選んだ扱いになります。
- `reasoning.effort` は読まずに捨てます。CLI には `--effort` がありますが、ブリッジは値を渡さない
  ため、実行時の effort は CLI の既定（または `CLAUDE_CODE_EFFORT_LEVEL`）のままです。launchd の
  plist では `CLAUDE_CODE_EFFORT_LEVEL=high` を設定しています。
- Claude CLI は `--permission-mode bypassPermissions` で起動します。非対話実行中に承認待ちで
  止まらないよう、Claude CLI 自身のツール実行をすべて自動承認します。
- **CLI 出力の総量に上限はありません。** 標準出力は 1 行ずつ読んで捨てるため総量はメモリに
  影響せず、実測では 8 割超をツール結果の行が占めます。ファイルを何十個も読むような作業では
  本文の長さと無関係に総量が伸びるため、総量での打ち切りは正常な長い作業だけを落とします。
  1 行の長さにも上限はありません（読み終えてからしか判定できず、確保を防げないため）。
  暴走に対する歯止めは `--timeout` が担います。
- 1 リクエストにつき CLI プロセスを 1 つ起動します。並行リクエストはプロセス数分のリソースを消費します。

## セキュリティ上の注意

- 既定の bind 先は `127.0.0.1` です。ループバック以外（`0.0.0.0` など）への bind は起動時に拒否します。
- **任意のシェル文字列を実行しません。** CLI は必ず引数配列で `subprocess.Popen` に渡し、`shell=True` は使いません。
  Claude へのプロンプトは引数ではなく標準入力から渡します。Antigravity は headless 契約が
  `-p <prompt>` の引数渡しのみのため引数で渡しますが、シェルを介さないので展開は起きません
  （代わりに引数長の上限を受けます。前述の「サポート範囲」を参照）。
- Claude CLI は **`--permission-mode bypassPermissions`**、Antigravity CLI は
  **`--dangerously-skip-permissions`** を使います。headless 実行中の全ツールを自動承認するため、
  `--working-dir` と `--add-dir` には信頼できるパスだけを指定してください。
- `--strict-mcp-config` を常に付けて起動するため、**ユーザー設定の MCP サーバーは一切読み込まれません。**
  既定では `--mcp-config` も渡さないので、外部 MCP サーバーは 1 つも起動しません。
  `--enable-codex-mcp` を明示したときだけ、同梱のアダプタ 1 つが追加されます。
  そのときも `--allowedTools` で許可するのはそのアダプタのツールだけです。
- ログにはメソッド・パス・ステータス・エラーコード・出力文字数のみを記録します。
  **プロンプト本文・CLI 出力・`Authorization` ヘッダの値・URL のクエリ文字列はログに出しません。**
  クエリで秘密値を渡すクライアントがあるため、記録するのはクエリを除いたパスだけです（404 のエラー本文も同様）。
- `--working-dir` で指定したディレクトリが Claude CLI と Antigravity CLI の作業ディレクトリになります。
  CLI はそのディレクトリのファイルを読めるため、**信頼できるディレクトリを指定してください。**
  `--add-dir` を足すと両CLIの操作範囲が広がります。既定では 1 つも渡しません。
- 中継が有効なとき、`--claude-models` 以外のリクエストは**本文もヘッダも解釈せず上流へ渡します。**
  `Authorization` もそのまま転送するため、ブリッジを経由させる上流は信頼できる相手に限ってください。
- このブリッジ自体に認証・レート制限・監査ログはこれ以上ありません。ローカル利用のみを想定しています。

## テスト

```bash
cd claude-bridge
python -m unittest discover -s tests
```

`subprocess.Popen` はテスト内で差し替えるため、実際の Claude CLI も `agy` も `codex app-server` も
起動しません（課金も発生しません）。Antigravity の擬似 NDJSON は agy 1.1.13 の実出力を写した
もので、実 CLI での疎通確認は前述の「実 CLI での確認結果」にまとめています。スレッド作成などの実際の RPC もテストでは実行せず、
転送内容の検証だけを行います。上流への中継はテスト内のスタブ HTTP サーバーへ向けて検証するため、
外部へは接続しません。
