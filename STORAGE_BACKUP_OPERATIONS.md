# 商品画像・納品ファイルの定期バックアップ

更新: 2026-10-25。実装・人工データ検証済み。クラウド接続・初回実データ実行・定期稼働は承認後に行う。稼働済みと混同しない。

## 対象と方式

- 元: Supabase `xnjzgfaasbemffgorvnj` の `toolbako-images` / `toolbako-deliveries` のファイル本体。
- 先: Railway既存 `rare-manifestation` 内の非公開bucket `toolbako-file-backups`、専用prefix `toolbako-storage-v1`。
- worker: `toolbako-storage-backup`。1 CPU・0.5 GB RAM、volume・公開domainなし。毎日03:00 JST（UTC `0 18 * * *`）。1回15分以内で終了。
- restic 0.19.1を公式assetのSHA256で固定。ファイル名・manifestも暗号化。復号パスワードは別管理する。
- 毎回、一覧取得→本体取得→一覧再照合→暗号化snapshot→別の一時フォルダーへ復元→全SHA256・件数照合。成功後だけ30日以内の世代と最新7世代を保持し、それ以外を専用prefix内で削除する。
- 元ファイルへの書込み・削除・本番への自動復元機能はない。DB、Auth、金融・メール業務workerは起動しない。
- 取得中変更、空一覧、bucketごとの件数半減超、サイズ超過、復元不一致は失敗として保持整理を止める。

## 権限と秘密値

`BACKUP_SOURCE_KEY` は既存webの専用SupabaseサーバーキーをRailway変数参照で利用する設計。処理は指定2bucketの読取りのみだが、**キー自体はSupabaseの広い管理権限を持つ**。最小権限の読取専用キーとは説明しない。workerへの付与は明示承認を得てから行う。将来、Storage専用ユーザー/RLSに置換する場合は別途権限・復元検証が必要。

| 変数 | 設定 |
|---|---|
| BACKUP_ENABLED | `true`（接続承認後のみ） |
| BACKUP_SOURCE_KEY | 既存webのSupabaseキーへの変数参照 |
| BACKUP_S3_ENDPOINT / BACKUP_S3_BUCKET / BACKUP_S3_REGION | 専用bucketのENDPOINT / BUCKET / REGIONへの参照 |
| BACKUP_S3_ACCESS_KEY_ID / BACKUP_S3_SECRET_ACCESS_KEY | 専用bucket認証情報への参照 |
| BACKUP_PASSWORD | 暗号学的乱数から作る40文字以上の専用値。封印保存 |
| BACKUP_RETENTION_ENABLED | 初回はfalse、復元確認と保持削除の承認後true |
| BACKUP_ALERTS_ENABLED | 通知権限承認後true |
| BACKUP_EMAIL_API_KEY | 既存送信専用キーへの参照。宛先は固定info@onellc-ai.com |

秘密値はGit・ログ・チャットに記録しない。復号パスワードは管理者がRailwayとは別のパスワード保管庫にも保存する。紛失すると復元できない。データと同一サービスだけに置いた状態では災害復旧の準備完了にしない。

## 配備と初回確認

1. 新規private bucketとworkerへの接続、ファイル転送、保持期限経過分の削除、障害通知をまとめて承認する。
2. workerのconfig pathを `deploy/storage-backup/railway.json`、Dockerfileを `deploy/storage-backup/Dockerfile` に設定。既存web/monitorの設定を流用しない。
3. 初期化中はcronを無効化し、起動を `python -m tools.storage_backup initialize` にして空の専用repositoryを一度だけ初期化する。接続失敗を理由に再初期化しない。
4. `python -m tools.storage_backup run`、retention=falseで初回実行し、終了0、`status=verified`、`restored_and_hashed=true`、件数・bytes・snapshot IDを記録する。
5. 承認済みの保持整理・通知・cronを有効化。次の定期実行も成功したことを確認してから「自動稼働確認済み」に更新する。

## 復元

専用bucketのS3認証と復号パスワードを安全な作業環境へ設定し、公式resticでsnapshot一覧を確認する。各snapshotの `paths[0]` がpayloadの絶対パス。`restic restore SNAPSHOT_ID:PAYLOAD_PATH --target EMPTY_DIRECTORY --verify` で**payloadのsubtreeだけ**復元する。Windowsでは `C:/...` はsnapshot内の `/C/...` に対応する。

`python -m tools.storage_backup verify-local --payload RESTORED_PAYLOAD_DIRECTORY` で全ファイルのhash/一覧を検証する。このコマンドは失敗通知も含め通信しない。manifestのbucket/key/mimeと `objects/00000000.bin` の対応を利用し、承認された隔離Storageへ復元する。本番への書戻しは別途対象と影響を確認する。

DBのStorageメタデータ・RLS/ACL・Auth復元はこの処理の対象外。ファイル本体だけでサイト全体の復旧完了にはならない。

## 費用・限界

Railway Storage料金は $0.015/GB月。worker CPU/RAM・サービス転送は別途従量課金。新しい常時稼働サーバーは使用しない。初期制限は元ファイル合計1GB・2万件、repository計測値45GBまで。これは予算のハード上限ではなく安全停止条件で、metadataや転送等は別。月3万円の既存総予算内で実使用量を確認する。

Railway bucketにはObject Lock/世代管理機能がない。resticの暗号化と世代保持は、bucket資格情報を奪われた際の削除不可保証ではない。独立アカウントの変更不能コピーは別途検討する。

毎日成功すれば直近24時間程度が保全間隔。障害時には伸びる。コード実行中の失敗通知は1日1回に抑制する。通知配送成功や受信箱到達は別途確認が必要。Railway停止・cron未起動等、worker自体が走らない障害はこの通知では検知できず、外部監視が必要。

根拠: [Railway buckets](https://docs.railway.com/storage-buckets)、[restic保持仕様](https://restic.readthedocs.io/en/stable/060_forget.html)、[Supabase DBバックアップの範囲](https://supabase.com/docs/guides/platform/backups)。
