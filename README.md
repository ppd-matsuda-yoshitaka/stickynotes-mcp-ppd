# stickynotes-mcp-ppd

Windows の付箋（Microsoft Sticky Notes）を、MCP クライアント（Claude など）から**読み取り専用**で参照するためのサーバーです。

付箋アプリのデータベース `plum.sqlite` を読み取り専用モードで開き、一覧・取得・検索だけを提供します。付箋の作成・変更・削除、ファイルへの書き出しは行いません。

## 業務で使う前に

- 付箋の本文は MCP クライアントを経由して AI の提供元へ送られます。業務メモを扱う場合は、社内の AI 利用規程に従ってください。
- `plum.sqlite` は Microsoft が仕様を公開していないデータベースです。付箋アプリの更新で構造が変わると、このサーバーは読み取りに失敗する可能性があります（データが壊れることはありません）。
- 付箋に書かれた文章は AI への入力になります。外部から貼り付けた文章に指示が含まれていても、このサーバーには書き込みや送信の手段がありません。ただし、同じクライアントに書き込みのできる別のツールがつながっている場合、AI がそちらで指示に従う可能性はあります。

## ツール

| ツール | 内容 | 主な引数 |
| --- | --- | --- |
| `list_notes` | 更新日時の新しい順に一覧（1行目のプレビュー付き） | `limit`（1〜100、既定 20）, `offset`, `theme`, `is_open`, `include_deleted` |
| `get_note` | 1件の本文と属性 | `note_id` |
| `search_notes` | 本文の部分一致検索（大文字小文字を区別しない） | `query`, `limit`（1〜100、既定 20）, `include_deleted` |

- 3つとも MCP の `readOnlyHint` を付けています。クライアント側で「読み取り専用ツールは自動許可」といった設定ができます。
- 色（`theme`）: `Yellow`, `Green`, `Pink`, `Purple`, `Blue`, `Grey`, `Charcoal`。それ以外を指定するとエラーになります。
- 本文は太字・斜体・取り消し線・箇条書きを Markdown に変換して返します。本文中のバックスラッシュ（`C:\build` など）はそのまま残します。
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

## テスト

テストは一時フォルダに作った小さなデータベースだけを使います。本物の付箋データベースには触れません。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
```

## 経緯

[widisaadi/stickynotes-mcp](https://github.com/widisaadi/stickynotes-mcp) をレビューした結果を受けて、書き込み機能を持たない最小構成として書き直したものです。元のコードは流用していません。
