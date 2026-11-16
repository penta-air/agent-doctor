# agent-doctor

AIエージェントの作業環境を、作業を始める前に診断するプラグインです。

Claude Code や Codex は、skill / hooks / MCP / CLAUDE.md・AGENTS.md を複数のレイヤーから読み込みます。
レイヤーは claude.ai アカウント・ユーザー・リポジトリ・プラグイン・組織管理です。
同じ名前の skill があっても警告は出ず、優先順位で片方が黙って隠れます。hooks は設定元が違えば同じタイミングで重ねて発火します。
agent-doctor はそれらを洗い出し、競合の可能性があるものを表で一覧にします。

読み取り専用で、設定やファイルは変更しません（修正は頼まれたときだけ、確認してから行います）。

## 想定している場面

チームがリポジトリに共有の skill や hooks を置き、出力形式や見る観点を揃えているとします。
そこに、自分のPCに同名の設計 skill や独自の hooks を入れているメンバーが加わりました。
Claude Code では**個人の skill がリポジトリの skill より優先される**ため、その人だけ別の手順で動き、本人もチームも気づきません。

agent-doctor はこれを「チーム（リポジトリで共有）／個人（その端末だけ）」の区別つきで指摘します。
中身が完全に同じコピーなら実害はないので、情報として扱います。

## 処理内容

新しいPC・クラウド環境・リポジトリで作業を始める前や、プラグインを追加したあとに、次のように頼んでください。

- 「環境診断して」
- 「skill が競合してないかチェックして」
- 「hooks が二重に動いている気がする」

結果は skills・hooks・MCP・CLAUDE.md / AGENTS.md の4節に分かれます。
各節に出るのは「読み取り件数」「競合の可能性あり件数」と、次の表だけです。
どれが優先されるか、どう直すべきかは書きません（必要なら続けて質問してください）。

| 節 | 分類 |
|---|---|
| skills | nameの重複（中身が違う同名 skill）／ descriptionの重複（取り違えそうな skill）／ この環境でのみ動作（リポジトリに無い個人の skill） |
| hooks | 重複発火 ／ この環境でのみ発火（リポジトリに無い個人の hooks）／ 動作しない恐れ |
| MCP | nameの重複（接続先が違う同名サーバー）／ 接続先の重複（別名での二重登録）／ この環境でのみ接続 ／ 動作しない恐れ |
| CLAUDE.md / AGENTS.md | この環境でのみ読み込み（個人の `~/.claude/CLAUDE.md` などがチームの指示に上乗せされる） |

表の列は「分類 / 対象（パス、hooks は行番号つき）/ 状況」です。

検知している内容は次のとおりです。

| 種類 | 主な検知内容 |
|---|---|
| skill | 同名の衝突（どれが勝ち、どれが隠れるか。中身が同じコピーは区別）、チームの skill が個人の skill に上書きされるケース、claude.ai アカウントの skill が短い名前を失うケース、シンボリックリンク等による二重登録、説明文が似ていて取り違えそうな組、壊れた SKILL.md |
| hooks | チームのリポジトリで動く個人の hooks、同じコマンドの二重登録、複数の設定元が同じイベントで発火、実在しない環境変数を読むため何も検査していないガード、参照先スクリプトの欠落・実行権限なし、matcher の書き間違い（配列・小文字） |
| MCP | 同じサーバーの別名での二重登録、同名サーバーのスコープ間の上書き、`.mcp.json` のキーや置き場所の誤り |
| CLAUDE.md / AGENTS.md | CLAUDE.md と AGENTS.md の内容の矛盾、AGENTS.override.md による上書き、Codex のサイズ上限超過 |

CLAUDE.md と AGENTS.md の内容の食い違いなどの細かい検知は、通常の報告には出ず、`--format json` の詳細で確認できます。
検知コードの一覧は [checks.md](skills/agent-doctor/references/checks.md)、走査する場所と優先順位は [sources.md](skills/agent-doctor/references/sources.md) にあります。

## 導入

Claude Code では、プラグインとして入れます。

```
/plugin marketplace add penta-air/agent-doctor
/plugin install agent-doctor@agent-doctor
```

ほかのエージェント（Codex など）では、`skills/agent-doctor/` フォルダを、エージェントが skill を読み込む場所（例: `~/.agents/skills/agent-doctor/`）にコピーしてください。
SKILL.md 形式なので、skill を読めるエージェントなら利用できます。

走査スクリプトは Python 3.11 以上で動きます。
標準ライブラリだけを使っているので、追加のインストールは要りません。

## 走査の手動実行

走査スクリプトだけを直接実行することもできます。

```bash
python3 skills/agent-doctor/scripts/scan.py                    # 上記の表形式
python3 skills/agent-doctor/scripts/scan.py --format json      # 詳細（表に出さない検知も含む）
python3 skills/agent-doctor/scripts/scan.py --agents claude    # Claude Code だけ
```

## 確認できないもの

- claude.ai アカウントの skill は、ローカルに同期されている分しか見えません
- サーバーから配信される組織管理設定は見えません（Claude Code の `/status` で確認できます）
- hooks を実際に発火させての検証はしません（設定からの推定です）
- クラウド環境で実行した結果は、手元のPCの設定とは別物です。それぞれの環境で実行してください
- Codex の skill の同名時の優先順位は公式に明記がないため、近いスコープ優先と推定しています

## ライセンス

MIT
