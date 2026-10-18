# 通知workerの配備仕様

2026-10-18、明示承認された専用サービス・限定DB権限・ドメイン限定Resendキーを作成し、人工通知1通のDeliveredと同じUUIDの再実行で送信0通を確認した。継続送信は未承認・未有効。保留2件は状態と時刻のSHA-256が一致し、試行回数0のまま。

サービスIDは`f6daa7ef-209c-4eac-9067-dfa9729e4e47`。DBのLOGINは`toolbako_mail_worker_login`、所属roleは`toolbako_mail_worker`だけ。実workerの接続でruntime全9テーブルのRLS維持、2つのメール用テーブルだけの読取、宛先/本文変更・メール削除・会員/金融テーブル参照の拒否を確認した。送信はUID 10001の`mailworker`で実行。

人工通知IDは`e94ae30b-791d-4c4d-9a85-6083df1fcc76`、ResendメールIDは`01a14d19-1c3a-711f-b900-e38c9e305548`。宛先は`info@onellc-ai.com`、件名は「ツールバコ 通知worker接続確認」。結果は初回`sent=1 retry=0 review=0`、同一ID再実行`sent=0 retry=0 review=0`、DBの試行回数1。検証通知の記録は保持する。

検証後は既定の`--dry-run`へ戻し、Cron未設定・Auto deploy切断・Restart Neverを確認。一時診断用の配備は停止した。保留2件を残しているため、実環境での`--dry-run`はpending/overdue各2、終了値2。Railwayの最後の無送信配備`0772ac18-5e21-493d-b9a5-d6b5b870c6a3`にはCrashed表示が残る。継続稼働・監視合格の証跡にはせず、定期送信の対象・保留2件の扱いを確定してから運用を有効化する。実送信成功は配備`fdff9011-3f47-4f78-8461-f48a9f37150c`で確認した。

Railwayでは新規変数を初回配備前に封印した際、実行環境で値が空になった。作成済みの封印欄をEditし、同じ専用値を再入力・配備して解消した。変数一覧の封印表示だけを完了判定にせず、実行環境で値の存在とDB接続・送信を確認する。秘密値は記録しない。

## 初回構成

- 対象：既存Railway `rare-manifestation` の専用 `toolbako-mail-worker`。
- 同じGitHubリポジトリ・release branch、Root Directory `/`、Dockerfile `deploy/mail-worker/Dockerfile`。
- 1 replica、1 vCPU、0.5 GB RAM、Volumeなし、公開ドメイン/TCP Proxyなし。
- 初回はCron未設定、Auto deploy無効、Restart Never。既定コマンドは件数だけの `--dry-run`。
- web用の `railway.json`、web用Pre-deploy、HTTP healthcheck、Start commandを継承しない。
- 新サービスはConfig as Codeを利用できないため、Dockerfileと起動コマンドは管理画面で指定する。RailwayのStart CommandはDocker ENTRYPOINTを置き換えるため、`python -m app.mail_worker --dry-run`と完全なコマンドを入力する。
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

送信を承認する前の継続確認には、同じ専用サービスのStart Commandを `python -m app.mail_monitor --watch --interval 60` とする。既存の限定DB接続を使い、毎回READ ONLYで件数のみ確認して接続を閉じる。資源上限・非公開接続・Cron未設定・Restart Neverを維持する。送信APIは呼ばず、古い保留メールもclaimしない。

`attention`/warningは滞留等の要確認であり、サービス停止とは分けてログに残す。`unavailable`/errorが3回続いた場合は終了値1。1回の確認は15秒まで、確認後60秒待つ。各ログには確認時刻、最終成功時刻、6種類の件数、連続失敗回数、読取専用・送信無効を記録し、メール本文・宛先・例外文字列は含めない。停止信号では安全に終了する。Activeだけでは通知到達・業務正常・アラート連絡の合格にしない。詳細は[運用手順](../../MAIL_OPERATIONS.md)。

単発診断の終了値2を0へ書き換えない。`python -m app.mail_monitor`（watchなし）と既存`app.mail_worker --dry-run`は滞留時に2のまま。既存の最後のdry-run配備のCrashed履歴は履歴として残す。

送信処理終了、15分以上の滞留、期限切れclaim、review、起動失敗を検知する。監視からメール本文・宛先・秘密値を外部へ送らない。診断終了値2は滞留等の要確認、1は診断/送信失敗。Restartを連続再実行に使わず、原因を確認する。

定期起動を採用する場合は、正常・失敗ともDB接続を閉じて終了し、停止したworkerのまま次の実行がスキップされないことを確認する。最短5分間隔・時刻の精密性は保証されないため、即時配信が必要なら別途常駐方式を設計する。

出典：[Railway Cron](https://docs.railway.com/cron-jobs)、[料金](https://docs.railway.com/pricing/plans)、[起動コマンド](https://docs.railway.com/deployments/start-command)、[Config as Code](https://docs.railway.com/config-as-code)、[Supabase roles](https://supabase.com/docs/guides/database/postgres/roles)。
