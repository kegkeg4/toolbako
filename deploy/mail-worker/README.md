# 通知workerの配備仕様

このフォルダーは準備用。サービス作成、DB権限付与、秘密値保存、実メール送信はまだ実行していない。

## 初回構成

- 対象：既存Railway `rare-manifestation` の専用 `toolbako-mail-worker`。
- 同じGitHubリポジトリ・release branch、Root Directory `/`、Dockerfile `deploy/mail-worker/Dockerfile`。
- 1 replica、1 vCPU、0.5 GB RAM、Volumeなし、公開ドメイン/TCP Proxyなし。
- 初回はCron未設定、Auto deploy無効、Restart Never。既定コマンドは件数だけの `--dry-run`。
- web用の `railway.json`、web用Pre-deploy、HTTP healthcheck、Start commandを継承しない。
- 環境変数は `APP_ENV=staging`、`DEMO_MODE=false`、専用`DATABASE_URL`と封印した`PGPASSWORD`だけから開始。送信試験時に専用の封印済み`EMAIL_API_KEY`を追加する。
- Stripe、Supabase Auth/Storage、Redis、webセッション等の秘密値は渡さない。

RAMとCPUを30日間常に上限まで使用した場合は約$25/月＋転送。1ドル170円、余裕10%なら約4,675円。短時間の実行は実使用量で変わる。既存の追加基盤見込み1.5万〜1.8万円と合わせて、承認済み月3万円枠内で構成する。円建て強制上限ではない。

## DBアクセス

`grants.sql`は明示承認後に対象DBだけで一度実行する運用スクリプトで、標準migrationに含めない。既存role名があれば停止する。

NOLOGINの`toolbako_mail_worker`はschema_versionとemail_outboxの読取、および送信状態・試行回数・claim・時刻の更新だけを持つ。宛先・差出人・件名・本文の更新、新規通知の作成、削除、金融/会員データへの権限は与えない。専用LOGINを別に作成し、必要なrole membershipだけを与える。管理者DB資格情報をworkerへコピーしない。

RLSは維持し、送信済み/reviewの行はworkerがpendingに戻せない。保留中の2件は初回試験の対象IDに含めず、変更前後で状態・試行回数が同じことを確認する。

## 送信の段階

1. 専用接続で `python -m app.mail_worker --dry-run`。件数と終了値だけを確認する。
2. 宛先・件名・本文を承認した人工通知1件を、運営用接続でoutboxへ作成する。UUIDを記録し、workerには作成権限を与えない。
3. 専用workerで `python -m app.mail_worker --apply --message-id <承認したUUID> --limit 1` を一度実行する。同じUUID以外を処理しない。ResendのDeliveredまで別途照合し、`sent=1`だけで到達済みとしない。
4. 同じUUIDで再実行して再送0件、従来の保留2件が未変更であることを確認する。試験記録は保持し、無断削除しない。
5. 継続送信の範囲、保留2件の扱い、監視通知先・担当者が確定するまでCronと`--all-pending`は有効化しない。

全件処理には `--apply --all-pending` の両方が必要。裸の実行や従来の`--limit 20`は送信しない。空・不正な対象指定を全件処理へ置き換えない。送信バッチは240秒で終了し、途中停止したclaimは5分leaseと同一冪等キーで回復する。23時間を超える結果不明の再送はreviewにする。

## 稼働前に必要な監視

送信処理終了、15分以上の滞留、期限切れclaim、review、起動失敗を検知する。監視からメール本文・宛先・秘密値を外部へ送らない。診断終了値2は滞留等の要確認、1は診断/送信失敗。Restartを連続再実行に使わず、原因を確認する。

定期起動を採用する場合は、正常・失敗ともDB接続を閉じて終了し、停止したworkerのまま次の実行がスキップされないことを確認する。最短5分間隔・時刻の精密性は保証されないため、即時配信が必要なら別途常駐方式を設計する。

出典：[Railway Cron](https://docs.railway.com/cron-jobs)、[料金](https://docs.railway.com/pricing/plans)、[Supabase roles](https://supabase.com/docs/guides/database/postgres/roles)。
