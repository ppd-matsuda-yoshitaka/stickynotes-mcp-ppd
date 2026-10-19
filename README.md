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

## 位置・サイズの調整（任意）

配置ツールは `STICKY_NOTES_ENABLE_LAYOUT=1` を MCP サーバーの `env` に追加し、サーバーを再接続すると公開されます。作成・追記用の `STICKY_NOTES_ENABLE_WRITE` とは独立しています。既定の読み取り専用設定には追加されません。

```json
"env": {
  "STICKY_NOTES_ENABLE_LAYOUT": "1"
}
```

| ツール | 用途 |
| --- | --- |
| `inspect_note_layout` | ノート ID と現在の矩形、最小サイズ、モニターの作業領域・DPI を取得 |
| `save_note_layout` | 手動配置を保存し `snapshot_id` を取得。ウィンドウは動かさない |
| `arrange_notes` | 右端から上→下に整列。縦に入らない分は左の列へ |
| `restore_note_layout` | `snapshot_id` の配置に復元 |

`arrange_notes` / `restore_note_layout` は既定で `dry_run=true`（プレビュー）です。`dry_run=false` で適用します。適用前には必ず現在の配置を別のスナップショットに保存し、その `snapshot_id` を返します。保存が失敗すれば移動しません。

整列の既定値は幅 **360 DIP**、高さ **240 DIP**、間隔 **12 DIP**、外側余白 **16 DIP** です。96 DPI では同じピクセル数、144 DPI では1.5倍になります。余白と間隔は見えている枠に対して取ります（不可視のリサイズ枠は含めません。枠の合計が間隔より大きい場合は、ウィンドウ同士が重ならない最小の間隔になります）。高さを削って詰め込まず、アプリの最小サイズを満たす列を左へ増やします。全体が作業領域に入り切らない場合は一切動かさずエラーを返します。タスクバーを除く作業領域を使い、負の座標のモニターにも対応します。

既定では保存上開いている全ノートを対象にし、現在のモニターを保ち、既存の列・上下順をおおむね尊重します。`note_ids` で対象を指定できますが、未選択の付箋と重なる整列は拒否します。`monitor_device`（`inspect_note_layout` の `device`）を指定すれば一つのモニターへ集められます。配列順を並び順としては使用しません。

Windows 標準の UI Automation で本文全体と DB の本文を一意に照合し、Win32 `SetWindowPos` で実ウィンドウだけを動かします。同一本文のノート、空メモ、未保存の編集、取得不能な本文、複数の編集領域、最小化・最大化など、確実に特定・操作できない場合は停止します。タイトルや本文の一部分だけで推測しません。装飾や箇条書きの UIA 表現が DB のプレーンテキストと異なる場合も、安全のため拒否します。

移動直前にプロセスの起動時刻、UIA の実行時 ID、本文、元の矩形、モニター構成を再確認します。DPI の異なるモニター間で移動するとアプリが DPI 変更に合わせてサイズを変えるため、一度だけ同じ矩形を再指定します。適用後も実際の矩形と本文を確認し、アプリが指定サイズを受け入れない場合などは元の矩形への復元を試みます。復元失敗はエラーで明示します。タイムアウトやプロセス終了では結果が未確定になる可能性があるため、エラーに含まれる `snapshot_id` を保持し、再度状態を確認してください。

スナップショットは `%LOCALAPPDATA%\sticky-notes-mcp-ppd\layouts` に JSON として保存します。`STICKY_NOTES_LAYOUT_DIR` で変更できます。ノート ID・矩形・可視枠・ウィンドウ識別情報・モニター情報を保存し、本文・色・DB の座標は保存・変更しません。手動配置を失わないよう自動削除はありません。復元時はモニター構成・作業領域・DPI が同一で、アプリの最小サイズを満たす必要があります。元の配置に重なりがあった場合はその重なりも復元します。

画面外の座標を無断で補正しません。原則として保存矩形全体が作業領域内にある必要がありますが、保存時の可視枠が作業領域内にあり、不可視枠だけが最大16 DIPはみ出す場合は、同じハンドル・プロセス起動時刻・UIA実行時ID・モニター・枠幅が維持されている場合に限り、元矩形をそのまま復元できます。モニター消失、可視部分のはみ出し、識別情報不足では適用前に停止します。識別情報のない旧スナップショットにはこの例外を適用しません。整列前には元配置もこの条件で復元可能か検証し、復元できない付箋（画面外にはみ出しているなど）があれば停止します。プレビューでは該当 ID を `unrestorable` に示します。`allow_unrestorable=true` を指定すると整列を続行し、結果の `unrestorable` に復元できない ID を返します（その配置のスナップショットからの復元は引き続き拒否されます）。

付箋アプリと同じ対話型 Windows セッションで実行してください。Windows PowerShell / .NET Framework の UI Automation を使うため追加ソフトは不要です。権限不足・スクリプト実行拒否では停止し、実行ポリシー変更やアクセス拒否の回避は行いません。正式な権限で実行できる環境が必要です。本文・色・ノートの作成削除・最前面設定は変更せず、アプリの強制終了、常駐監視、自動起動も行いません。アプリ再起動後の配置永続化はアプリ側の保存動作に依存します。

実装時に参照した Windows 仕様: [SetWindowPos](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setwindowpos)、[GetWindowRect](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-getwindowrect)、[DPI awareness](https://learn.microsoft.com/en-us/windows/win32/api/winuser/nf-winuser-setthreaddpiawarenesscontext)、[UIA TextPattern](https://learn.microsoft.com/en-us/dotnet/api/system.windows.automation.textpattern.documentrange)。

## テスト

テストは一時フォルダに作った小さなデータベースだけを使います。本物の付箋データベースには触れません。

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[test]"
.\.venv\Scripts\python.exe -m pytest
```

## 経緯

[widisaadi/stickynotes-mcp](https://github.com/widisaadi/stickynotes-mcp)（MIT License）をレビューした結果を受けて、データを失いにくい最小構成として、それを参考に書き直したものです。一部の関数と定数は元のコードと同じ形をしています。元のプロジェクトの著作権表示とライセンス文は [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) に載せています。
