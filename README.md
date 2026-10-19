# stickynotes-mcp-ppd

Windows の付箋（Microsoft Sticky Notes）を、MCP クライアント（Claude など）から扱うためのサーバーです。

既定では**読み取り専用**です。付箋アプリのデータベース `plum.sqlite` を読み取り専用モードで開き、一覧・取得・検索だけを提供します。

環境変数で有効にした場合に限り、付箋の**新規作成**と**末尾への追記**もできます。既存の行の書き換え、色の変更、削除、ファイルへの書き出しは行いません。

## 業務で使う前に

- 付箋の本文は MCP クライアントを経由して AI の提供元へ送られます。業務メモを扱う場合は、社内の AI 利用規程に従ってください。
- `plum.sqlite` は Microsoft が仕様を公開していないデータベースです。付箋アプリの更新で構造や保存形式が変わると、読み取りに失敗したり、書き込んだ付箋がアプリで正しく表示されなかったりする可能性があります。
- 付箋に書かれた文章は AI への入力になります。外部から貼り付けた文章に指示が含まれていると、AI がそれに従って付箋を作成・追記する可能性があります。書き込みを有効にする場合は、クライアント側で書き込みツールを「毎回確認」にしてください。書き込みが無効なら、このサーバーには書き込みや送信の手段はありません。
- クラウド同期（Microsoft アカウントでのサインイン）を使っている環境では動作を確認していません。

## ツール

| ツール | 内容 | 主な引数 |
| --- | --- | --- |
| `list_notes` | 更新日時の新しい順に一覧（1行目のプレビュー付き） | `limit`（1〜100、既定 20）, `offset`, `theme`, `is_open`, `include_deleted` |
| `get_note` | 1件の本文と属性 | `note_id` |
| `search_notes` | 本文の部分一致検索（大文字小文字を区別しない） | `query`, `limit`（1〜100、既定 20）, `include_deleted` |

書き込みを有効にしたときだけ、次の2つが追加されます。

| ツール | 内容 | 主な引数 |
| --- | --- | --- |
| `create_note` | 新しい付箋を作る | `text`, `theme`（既定 `Yellow`）, `is_open`（既定 true） |
| `append_to_note` | 既存の付箋の末尾に行を足す | `note_id`, `text` |

- 読み取りの3つには MCP の `readOnlyHint` を付けています。クライアント側で「読み取り専用ツールは自動許可」といった設定ができます。書き込みの2つには付けていません。
- 色（`theme`）: `Yellow`, `Green`, `Pink`, `Purple`, `Blue`, `Grey`, `Charcoal`。それ以外を指定するとエラーになります。
- 本文は太字・斜体・取り消し線・箇条書きを Markdown に変換して返します。下線は Markdown に対応する記法がないため、文字だけを返します。本文中のバックスラッシュ（`C:\build` など）はそのまま返します。
- 検索は書式記号を取り除いた後の本文に対して行います。`%` や `_` はワイルドカードではなく、ただの文字として扱います。

## セットアップ

Python 3.10 以上が必要です。PowerShell で実行します。

```powershell
git clone https://github.com/ppd-matsuda-yoshitaka/stickynotes-mcp-ppd.git
cd stickynotes-mcp-ppd
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
```

MCP クライアントの設定（`mcpServers` 形式の例）。パスは実際の場所に置き換えてください。

```json
{
  "mcpServers": {
    "sticky-notes": {
      "command": "C:\\path\\to\\stickynotes-mcp-ppd\\.venv\\Scripts\\python.exe",
      "args": ["C:\\path\\to\\stickynotes-mcp-ppd\\server.py"]
    }
  }
}
```

付箋のデータベースは既定で次の場所から読みます。

```text
%LOCALAPPDATA%\Packages\Microsoft.MicrosoftStickyNotes_8wekyb3d8bbwe\LocalState\plum.sqlite
```

別の場所を使う場合は、環境変数 `STICKY_NOTES_DB_PATH` にパスを指定します。

## 書き込みを有効にする

クライアント設定のサーバー定義に `env` を追加します。

```json
"env": {
  "STICKY_NOTES_ENABLE_WRITE": "1"
}
```

書き込みには次の制限と安全策があります。

- **付箋アプリを閉じておく必要があります。** `Microsoft.Notes.exe` が動いている間は書き込みを拒否します。アプリが開いたままだと、アプリが持っている内容で書き込みが上書きされるためです。作成・追記した内容は、次にアプリを開いたときに表示されます。
- **書き込みのたびにバックアップを取ります。** SQLite のバックアップ機能を使い、書き込み途中のデータ（WAL）も含めて `%LOCALAPPDATA%\sticky-notes-mcp-ppd\backups` に保存します。直近 20 件を残し、それより古いものは削除します。保存先は `STICKY_NOTES_BACKUP_DIR` で変えられます。バックアップに失敗した場合は書き込みません。
- **既存の行は書き換えません。** 追記は末尾に行を足すだけなので、元の書式はそのまま残ります。削除済みの付箋には追記できません。
- **書き込む本文はプレーンテキストです。** `**` などの Markdown は書式に変換せず、そのまま文字として保存します。1回の書き込みは 10,000 文字までです。

バックアップから戻すときは、付箋アプリを閉じて `plum.sqlite` をバックアップファイルで置き換え、同じフォルダの `plum.sqlite-wal` と `plum.sqlite-shm` を削除します。

## テスト

テストは一時フォルダに作った小さなデータベースだけを使います。本物の付箋データベースには触れません。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
```

## 経緯

[widisaadi/stickynotes-mcp](https://github.com/widisaadi/stickynotes-mcp)（MIT License）をレビューした結果を受けて、データを失いにくい最小構成として、それを参考に書き直したものです。一部の関数と定数は元のコードと同じ形をしています。元のプロジェクトの著作権表示とライセンス文は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) に載せています。
