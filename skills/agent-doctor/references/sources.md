# 走査する場所と優先順位

パスは次のように表記する。`~/.claude` は `CLAUDE_CONFIG_DIR`、`~/.codex` は `CODEX_HOME` で変わる。

- `~` はホーム
- `<repo>` はリポジトリのルート（`.git` のある階層。無ければ作業ディレクトリ）
- `<managed>` は組織管理の設定ディレクトリ（Linux `/etc/claude-code`、macOS `/Library/Application Support/ClaudeCode`、Windows `C:\Program Files\ClaudeCode`）

## Claude Code

### skill / コマンド / サブエージェント

| レイヤー | 場所 |
|---|---|
| 組織管理 | `<managed>/.claude/skills/` |
| 個人 | `~/.claude/skills/<name>/SKILL.md` |
| claude.ai アカウント | `~/.claude/skills/synced/<bucket>/<name>/SKILL.md`（同期済みの分のみ） |
| リポジトリ | `<repo>/.claude/skills/`（ルートから作業ディレクトリまでの各階層）、下位フォルダの `.claude/skills/` |
| プラグイン | `~/.claude/plugins/installed_plugins.json` の installPath、または `~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/` 配下の `skills/` `commands/` `agents/` |
| コマンド | `~/.claude/commands/`, `<repo>/.claude/commands/` |
| サブエージェント | `~/.claude/agents/`, `<repo>/.claude/agents/` |

同名のときの優先順位（公式ドキュメントより）:

1. 組織管理 > 個人 > リポジトリ
2. claude.ai アカウントの skill は、他に同じ名前があると短い名前を譲り、`/anthropic-skills:<name>` でのみ動く
3. skill と `.claude/commands/` のファイルが同名なら skill が勝つ
4. プラグインの skill は `/<plugin>:<name>` の名前空間付きなので衝突しない（ただし自動選択では似た候補が並ぶ）
5. 名前は大文字小文字・空白・不可視文字を無視して比較される

プラグインの有効/無効は各 settings の `enabledPlugins`（ローカル > リポジトリ > 個人）で判定。どこにも書かれていなければ「不明」として扱い、走査対象に含める。

### hooks

| 設定元 | 場所 |
|---|---|
| 組織管理 | `<managed>/managed-settings.json` |
| 個人 | `~/.claude/settings.json` |
| リポジトリ | `<repo>/.claude/settings.json` |
| ローカル | `<repo>/.claude/settings.local.json` |
| プラグイン | `<plugin>/hooks/hooks.json`、`plugin.json` の `hooks` |
| skill / サブエージェント | frontmatter の `hooks`（有無のみ検出） |

hooks は上書きではなく**すべての設定元が合算されて発火する**。そのため複数の設定元が同じイベントに登録すると、全部が動く。

### MCP

| スコープ | 場所 |
|---|---|
| 組織管理 | `<managed>/managed-mcp.json` |
| 個人 | `~/.claude.json` の `mcpServers` |
| ローカル | `~/.claude.json` の `projects["<repo>"].mcpServers` |
| リポジトリ | `<repo>/.mcp.json` の `mcpServers` |
| プラグイン | `<plugin>/.mcp.json`、`plugin.json` の `mcpServers`（名前空間付き） |

同名のときはローカル > リポジトリ > 個人。

### CLAUDE.md（およびルール）

`<managed>/CLAUDE.md`、`~/.claude/CLAUDE.md`、`~/.claude/rules/`、作業ディレクトリから上位に向かう各階層の `CLAUDE.md` / `.claude/CLAUDE.md` / `CLAUDE.local.md`、`<repo>/.claude/rules/`。

## Codex

| 種類 | 場所 |
|---|---|
| skill | 作業ディレクトリから `<repo>` までの各階層の `.agents/skills/`、`~/.agents/skills/`、`~/.codex/skills/`、`/etc/codex/skills/` |
| MCP | `/etc/codex/config.toml`、`~/.codex/config.toml`、各階層の `.codex/config.toml` の `[mcp_servers.<name>]` |
| hooks | `~/.codex/hooks.json`、`<repo>/.codex/hooks.json`、`config.toml` の `[hooks]`（形式が Claude Code と同じ場合のみ解釈。ベストエフォート） |
| 指示 | `~/.codex/AGENTS.md`、ルートから作業ディレクトリまでの各階層で `AGENTS.override.md` → `AGENTS.md` の順に最初の空でないもの |

設定の優先順位はリポジトリ > 個人 > システム。
skill の同名時の扱いは公式に明記がなく、近いスコープ優先と推定している（報告でも推定である旨を添える）。

## 確認できないもの

- claude.ai アカウントの skill のうち、まだローカルに同期されていないもの
- サーバーから配信される組織管理設定（`/status` で有無を確認できる）
- `--plugin-dir` や `--add-dir` など起動オプションで一時的に追加されたもの
- 実際の発火順序や実行時の挙動（hooks は設定からの推定）
