---
name: agent-doctor
description: Diagnose the AI agent environment before work starts and list, as compact tables, skills, hooks, MCP servers and CLAUDE.md / AGENTS.md files that may conflict with what the repository (team) expects — same-name skills with different content, skills with overlapping descriptions, skills and hooks that exist only on this machine/account, hooks that fire twice, hooks that likely do not work, MCP servers duplicated or present only on this machine, and personal CLAUDE.md / AGENTS.md that add instructions only on this machine — across claude.ai account, user, repository, plugin and organisation layers for Claude Code and Codex. Read-only. Use when joining a team repository, starting on a new machine or cloud environment, after installing plugins/skills, or when an agent behaves unexpectedly. Trigger on "環境診断", "agent-doctor", "skillが競合してないか", "同名のskill", "hooksが二重に動く", "想定外のhooks", "チームの設定とぶつかる", "作業前にチェック", "environment check", "skill conflict", "hook firing twice".
---

# agent-doctor

AIエージェントは、アカウント・ユーザー・リポジトリ・プラグイン・組織管理といった複数のレイヤーから skill と hooks を読み込む。
チームがリポジトリで skill / hooks を共有していても、参加したメンバーの個人環境に同名の skill や独自の hooks があると、その人だけ違う動きをする。
本人もチームも気づかない。
このスキルは、その「競合の可能性」がある skill・hooks・MCP サーバー・CLAUDE.md / AGENTS.md を表で一覧にする。

**出すのは検知結果だけ**で、どれが優先して使われるか、どう直すべきかは書かない。
優先されるかどうかは状況や AI の判断に左右されるので断定できず、対処はユーザーが一覧を見て決めるためである。
読み取り専用で、設定やファイルは変更しない。

## 手順

### Step 1: 走査する

```bash
python3 <このスキルのディレクトリ>/scripts/scan.py
```

- 作業対象のリポジトリ内で実行する（`--cwd` で指定も可）。ホームや設定ディレクトリが標準と違う場合は `--home` / `--claude-home` / `--codex-home` / `--managed-dir` で指定する
- 片方のエージェントだけ見たいときは `--agents claude` または `--agents codex`
- 出力はそのまま報告に使える Markdown（skills・hooks・MCP・CLAUDE.md / AGENTS.md の4節、各節に件数と表）

### Step 2: descriptionの重複だけ確認する

表のうち「descriptionの重複」の行は、文字の類似度で機械的に検出した候補である。
各行の2つの SKILL.md の description を読み、同じ依頼で両方が反応しそうかを判断する。
役割がはっきり分かれているものは行を消し、「競合の可能性あり」の件数をその分減らす。それ以外の行と件数は変えない。

### Step 3: 報告する

Step 2 を反映した Markdown をそのまま出す。前置きや結論の文章、優先順位の説明、対処の提案は付けない。
ユーザーの言語が日本語以外なら、見出しと表の文言だけ翻訳する。走査中に `※` で始まる注記が出た場合はそのまま残す。

出力の形:

```markdown
## skills

- 読み取り: n件
- 競合の可能性あり: n件

| 分類 | 対象 | 状況 |
|---|---|---|
| nameの重複 | ・`~/.claude/skills/x/SKILL.md`（個人）<br>・`<repo>/.claude/skills/x/SKILL.md`（チーム） | `x` が2か所にあり、内容が異なる |

## hooks

- 読み取り: n件
- 競合の可能性あり: n件

| 分類 | 対象 | 状況 |
|---|---|---|
| この環境でのみ発火 | ・`~/.claude/settings.json:12`（個人）Stop: `...` | 出どころ: 個人（1件）。リポジトリには含まれない |
```

分類の意味:

| 節 | 分類 | 意味 |
|---|---|---|
| skills | nameの重複 | 同じ名前の skill が複数の場所にあり、中身が違う（中身が完全に同じコピーは競合ではないので出さない） |
| skills | descriptionの重複 | 名前は違うが説明文が似ていて、同じ依頼で取り違えが起きうる |
| skills | この環境でのみ動作 | リポジトリに共有の設定（`.claude/` 等）があるのに、この端末・アカウントにしかない skill |
| hooks | 重複発火 | 別々の設定元の hooks が同じイベント・重なる条件で発火する（同じコマンドの二重登録を含む） |
| hooks | この環境でのみ発火 | リポジトリに共有の設定があるのに、この端末の設定や個人で入れたプラグインから発火する hooks |
| hooks | 動作しない恐れ | 存在しない変数を読む、スクリプトが無い・実行できない、matcher の書き間違いなど、想定どおりに動かない hooks |
| MCP | nameの重複 | 同じ名前のサーバーが複数の場所にあり、接続先が違う（接続先が同じものは出さない） |
| MCP | 接続先の重複 | 同じサーバーが別の名前で登録され、二重に起動する |
| MCP | この環境でのみ接続 | リポジトリに共有の設定があるのに、この端末・個人で入れたプラグインにしかないサーバー |
| MCP | 動作しない恐れ | `.mcp.json` のキーや置き場所が誤っていて読み込まれない |
| CLAUDE.md / AGENTS.md | この環境でのみ読み込み | リポジトリに共有の設定があるのに、個人の `~/.claude/CLAUDE.md`・`~/.claude/rules/`・`~/.codex/AGENTS.md`、またはリポジトリより上のフォルダの CLAUDE.md が読み込まれ、チームの指示に上乗せされる |

出どころは次の4つに分ける。

- チーム＝リポジトリに含まれるもの（リポジトリの settings で有効化したプラグインを含む）
- 個人＝この端末・アカウントだけのもの
- アカウント同期＝claude.ai アカウントから同期された skill
- 組織＝組織管理の設定

### Step 4: 詳細を確認された場合

ユーザーが特定の行について「どちらが使われるか」「どう直すか」「中身の違い」を尋ねたときだけ、`scan.py --format json` の結果と該当ファイルを読んで答える。
各検知の背景と一般的な対処は `references/checks.md`、走査する場所と公式に記載された優先順位は `references/sources.md` にある。
修正を頼まれた場合は、対象を示して了承を得てから変更する。個人設定（`~/.claude` 等）の変更は他のリポジトリにも影響することを伝える。

## 確認できないもの

- claude.ai アカウントの skill は、ローカルに同期済みの分しか見えない
- hooks は実際には発火させず、設定から判断している
- クラウド環境で実行した結果は、手元のPCとは別物
