# 検知コード一覧

出どころは次の3つに分ける。

- チーム＝リポジトリに含まれ共有されるもの（`<repo>/.claude/`、`.agents/`、`.mcp.json`、リポジトリの settings で有効化されたプラグイン）
- 個人＝この端末・アカウントだけのもの（`~/.claude/`、`settings.local.json`、アカウント同期 skill、個人で有効化したプラグイン）
- 組織＝組織管理の設定

重大度は3段階ある。

- 要対応（action）は、このままだと作業中に誤動作する、または設定が反映されていないもの
- 注意（warning）は、誤動作の可能性があり、意図どおりか確認が必要なもの
- 情報（info）は、問題ではないが、知っておくと判断に役立つもの

## skill / コマンド / サブエージェント

| コード | 重大度 | 意味 | 対処 |
|---|---|---|---|
| SKILL_NAME_COLLISION | 要対応 | 同じ名前で中身の違う skill（またはコマンド）が複数レイヤーにあり、1つ以外は隠れる。名前は大文字小文字・空白・不可視文字を無視して比較。リポジトリ（チーム）の skill が個人の skill に負けている場合は "team version overridden" と付く | 不要な方を削除・改名。コピーなら1か所に寄せる |
| SKILL_DUPLICATE_IDENTICAL | 情報 | 同名の skill が複数あるが、フォルダの中身が完全に同じ。今は実害なし。ただし共有側が更新されると、優先される側の古いコピーが使われ続ける | 余分なコピーを消して共有側に一本化 |
| SKILL_PLUGIN_SHADOW | 注意 | プラグインの skill とローカルの skill が同名。両方読み込まれるが `/名前` はローカル側が動き、自動選択では似た候補が2つ並ぶ | どちらかを改名・無効化 |
| SKILL_LOADED_TWICE | 注意 | 同じフォルダがシンボリックリンク等で複数経路から見つかる。二重登録や読み込みエラーの原因 | 探索経路を1つにする |
| SKILL_SIMILAR_DESCRIPTION | 注意（候補） | 名前は違うが説明文が似ている。自動選択で取り違える可能性。**スキルが実物を読んで判断する** | 説明文に「いつ使わないか」を書き分ける |
| SKILL_NO_FRONTMATTER | 要対応 | SKILL.md の先頭に `---` のYAMLが無い。読み込まれない可能性 | name / description を書く |
| SKILL_NO_DESCRIPTION | 注意 | description が無く、自動で使われない | description を書く |
| SKILL_NAME_FOLDER_MISMATCH | 情報 | name とフォルダ名が違う。エージェントによって呼び出し名が変わる | 揃える |
| SUBAGENT_NAME_COLLISION | 注意 | 同名のサブエージェントが複数あり、1つ以外は隠れる | 改名・削除 |
| EMBEDDED_HOOKS | 情報 | skill / サブエージェントの frontmatter に hooks がある。設定ファイルには現れず、その skill が動いている間だけ発火する | 把握しておく |

## hooks

| コード | 重大度 | 意味 | 対処 |
|---|---|---|---|
| HOOK_DUPLICATE | 要対応 | 同じコマンドが別の設定元にも登録され、二重に実行される（プラグインの hooks を settings に手でコピーした場合など） | 片方を消す |
| PERSONAL_HOOKS_IN_TEAM_REPO | 注意 | リポジトリに共有のエージェント設定（`.claude/` 等）があるのに、個人設定・ローカル設定・個人で入れたプラグインの hooks もそこで動く。チームの他のメンバーには無い動作。PreToolUse / Stop などはエージェントを止めたり誘導したりできる | このリポジトリで不要なら対象プロジェクトに移す。必要ならチームの規約と食い違わないか確認 |
| HOOK_MULTI_SOURCE | 注意 | 別々の設定元の hooks が同じイベント・重なる条件で発火する。どちらかがブロックや書き換えをすると、もう片方の前提が崩れる | 意図を確認し、不要なら削除・条件を絞る |
| HOOK_UNDEFINED_INPUT_VAR | 要対応 | `$CLAUDE_TOOL_INPUT` 系など、実際には設定されない環境変数を読んでいる。hooks の入力は stdin の JSON で渡されるため、ガードが何も検査していない（静かに無効化されている） | `jq -r '.tool_input.command'` 等で stdin を読む |
| HOOK_SCRIPT_MISSING | 要対応 | hooks が呼ぶスクリプトが存在しない。発火のたびに失敗する | パスを直す・スクリプトを置く |
| HOOK_SCRIPT_NOT_EXECUTABLE | 注意 | 直接実行されるスクリプトに実行権限が無い | `chmod +x` か、インタプリタ経由で呼ぶ |
| HOOK_MATCHER_ARRAY | 要対応 | matcher が配列。無効な設定として扱われ、PreToolUse ではファイル内の他の hooks まで読み込まれなくなる | `"Edit\|Write"` のような1つの文字列にする |
| HOOK_MATCHER_CASE | 注意 | matcher のツール名が小文字（`bash` 等）。大文字小文字を区別するので一致しない | `Bash` のように大文字始まりにする |
| HOOK_EMPTY_COMMAND | 注意 | コマンドが空かコメントだけ。毎回何もしない処理が実行される | 中身を書くか削除 |
| HOOK_USER_PROJECT_PATH | 注意 | 個人設定（全リポジトリで有効）の hooks が `$CLAUDE_PROJECT_DIR` 配下のスクリプトを呼んでいる。スクリプトの無いリポジトリでは毎回エラーになり、同名の別スクリプトがあればそれが動く | リポジトリ側の settings に移すか、スクリプトを `~/.claude/` 配下に置く |
| HOOK_LEGACY_DECISION | 情報 | PreToolUse で旧形式の `{"decision":"block"}` を出力している | `hookSpecificOutput.permissionDecision` か exit code 2 に移行 |
| HOOK_BROAD_MATCHER | 情報 | ツール系イベントで全ツールに一致する | 必要なツールだけに絞る |
| HOOKS_DISABLED | 情報 | `disableAllHooks` が有効で、hooks が一切動かない | 意図どおりか確認 |

## MCP

| コード | 重大度 | 意味 | 対処 |
|---|---|---|---|
| MCP_DUPLICATE_SERVER | 注意 | 同じ接続先（コマンド＋引数、またはURL）が別名で二重登録。二重起動し、ツールも重複する | 片方を削除（プラグイン＋手動登録の重複が多い） |
| MCP_NAME_SHADOWED | 注意（同一内容なら情報） | 同名のサーバーが複数スコープにあり、1つ以外は無視される | 1か所に寄せる・改名 |
| MCP_PLAINTEXT_SECRET | 注意 | env / headers にトークン等が平文で書かれている（値は出力しない） | 環境変数参照（`"${GITHUB_TOKEN}"`）に置き換え、共有したことがあればトークンを再発行 |
| MCP_WRONG_KEY | 要対応 | `.mcp.json` のキーが `servers`（VS Code 形式）で、Claude Code が読まない | `mcpServers` に直す |
| MCP_WRONG_LOCATION | 注意 | `.mcp.json` が `.claude/` 内にあり読まれない | リポジトリ直下に移す |
| MCP_MANAGED | 情報 | 組織管理の MCP 設定があり、他のサーバーが制限される可能性 | 把握しておく |
| MCP_SHARED_ACROSS_AGENTS | 情報 | Claude と Codex の両方に同じサーバーがある | 設定の同期だけ注意 |

## CLAUDE.md / AGENTS.md

| コード | 重大度 | 意味 | 対処 |
|---|---|---|---|
| INSTRUCTIONS_DUAL | 情報 | 同じ階層に CLAUDE.md と AGENTS.md が別々にあり、内容がずれやすい。**スキルが内容を比較し、矛盾があれば要対応として報告する** | 片方から他方を読み込む（`@AGENTS.md`）かシンボリックリンクにする |
| AGENTS_OVERRIDDEN | 注意 | AGENTS.override.md があり、同じ階層の AGENTS.md は Codex に無視される | 統合するか、古い override を消す |
| AGENTS_TOO_LARGE | 注意 | AGENTS.md が 32KiB を超え、Codex で切り詰められる | 短くする・階層ごとに分ける |
| CONFIG_PARSE_ERROR | 要対応 | 設定ファイルの JSON / TOML が壊れていて、中の設定がすべて無視される | 構文を直す |
