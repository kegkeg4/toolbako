# バックアップと復元確認

更新日: 2026-10-25

## 実バックアップによる復元確認（2026-10-25）

既存サービスの実データから取得したバックアップを、別のローカルPostgreSQL 17へ復元した。runtime 9テーブルとSupabaseのauth/public/storage 39テーブル、Storageのファイル本体4件を照合し、すべて一致した。runtimeは行数・内容ハッシュだけでなく、権限・RLS・索引・トリガー、履歴4テーブルの削除拒否、金融明細の外部キー拒否も確認した。記録: `work/real-backup-restore-20261025.json`。

復元先は毎回新しいUUID付きディレクトリで、接続はloopback限定。メール・金融worker、Authサーバー、Storage APIは起動せず、検証後にPostgreSQLを停止した。元DBへの復元や業務データの書換えは行っていない。数値を含むテーブルの照合は`extra_float_digits`を揃え、高精度設定でも元と復元先の一致を確認した。

取得物は転送前にCMSで暗号化し、ローカルの`.local/resilience-20261025`へ保存した。復号鍵・復号済みデータを含むこのフォルダーは、Windows ACLで現在のOwnerユーザーだけに限定し、Gitには含めない。鍵を失うと復号できないため、バックアップの定期化時には暗号化物と鍵の別管理・復旧責任者を定める。今回の同一PC保存だけではPC故障への備えにならない。

**合格範囲はデータとruntime制約の隔離復元。** 復元先での実ログイン、Storage APIの所有者/非所有者アクセス、新しい稼働環境への切替は未完了。定期バックアップとファイルの別保管先への保存については、後述の最新状況を参照。runtimeの復元処理0.406秒は小規模ローカルDBの測定値で、サービス全体の復旧時間ではない。その後、利用者によるSupabase Proへの変更を確認し、日次DBバックアップ7世代が表示された。最新は2026-10-25 07:45:42 JST。DBバックアップにはStorageのファイル本体は含まれない。

## 確認済みの範囲

`tools/restore_drill.py` は、架空データだけでPostgreSQLのバックアップと復元を確認する独立したローカルツール。アプリの `DATABASE_URL` やSupabaseキーは使用しない。接続先は明示した `127.0.0.1` または `::1` に限定し、継承されたPGSERVICE・PGHOSTADDR等の設定を隔離する。

毎回UUID付きの新しいDBを2個作成し、片方へ現行runtime schemaと架空の注文・本人確認受付・監査・未送信通知・結果不明のStripe要求・金融台帳を格納する。`pg_dump --format=custom` と `pg_restore --single-transaction --exit-on-error` で別DBへ復元し、以下を比較する。

- 全9テーブルの行数と内容ハッシュ
- テーブル・schema権限、RLS、索引、変更禁止トリガー
- 監査・金融履歴4テーブルの削除拒否
- 存在しない売上を参照する金融明細の拒否
- 元DBと復元先のデータが確認処理で変化していないこと

今回の架空データでは全項目合格。実行記録は `work/restore-drill-verification.json`、接続先制限等を含む関連テストは8件合格。小さなローカルデータでの処理時間は実サービスの復旧時間を表さない。

## ローカルで再実行

既存の使い捨てローカルPostgreSQLと、そのサーバーに対応するバージョンの公式 `pg_dump` / `pg_restore` を使う。DB作成権限が必要。専用DSNは秘密値をチャットやコマンド引数に貼らず、環境変数で渡す。

```powershell
# ローカル専用の例。実サービスのDSNへ置き換えない。
$env:TOOLBAKO_DRILL_ADMIN_DSN='postgresql://postgres@127.0.0.1:55439/postgres'
.venv/Scripts/python.exe -m tools.restore_drill --pg-bin work/postgresql/pgsql/bin

$env:TOOLBAKO_DRILL_PG_BIN='work/postgresql/pgsql/bin'
.venv/Scripts/python.exe -m pytest -q tests/test_restore_drill.py
```

ソースDB・復元先DB・一時archiveは、この実行が作ったものだけを終了時に削除する。既存DBに復元する引数は用意していない。処理失敗は終了値1で、接続情報・生のプロバイダーエラーを表示しない。接続切断等で後片付けが失敗した場合も、既存DBの一括削除を行わず、今回の `toolbako_drill_` 名だけを個別確認する。

## 本番運用として残ること

2026-10-25の実データ復元は上記の範囲で合格したが、サービス全体の災害復旧完了ではない。DBバックアップだけではStorage APIで保管したファイル本体は復元できないため、今回も本体4件を別途取得・照合した。[Supabase公式バックアップ仕様](https://supabase.com/docs/guides/platform/backups)

1. 実プロジェクトで利用可能なバックアップ、保持期間、保管先、暗号化、アクセス責任者を確認する。
2. DBとファイル本体を別途保全し、復元に必要な秘密値はバックアップ本文と分離して管理する。
3. 承認済みの別環境へ復元し、会員・注文・金融台帳・監査・ファイルの認可まで確認する。
4. 復元先ではweb・メールworker・金融worker・Webhook受信を停止したまま検証する。古いoutboxの再送や結果不明の決済要求の再実行を防ぎ、Stripe側との照合後に復旧を判断する。
5. 許容データ損失時間と復旧時間を決め、実測値と復旧責任者を記録する。

Supabase ProのDBバックアップは確認済み。稼働中DBへの復元は実施していない。ファイル本体の定期保全は `STORAGE_BACKUP_OPERATIONS.md` を参照（実ファイル4件の暗号化・別保管先からの復元照合成功、毎日03:00 JSTの設定有効化済み。初回の時刻到来自動実行は未観測）。時刻到来による定期実行とサービス全体の復旧確認が残るため、本番公開条件のバックアップ項目を一括で完了にしない。

形式・復元オプションの根拠: [PostgreSQL pg_dump](https://www.postgresql.org/docs/17/app-pgdump.html)、[pg_restore](https://www.postgresql.org/docs/17/app-pgrestore.html)。
