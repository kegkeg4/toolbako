# ツールバコ 本番公開チェックリスト

更新日: 2026-10-13

判定：**本番決済は公開不可**。最新の証拠と変更内容は `RELEASE_PROGRESS_2026-10-09.md` を参照。管理画面の項目数はサービス完成度ではありません。

## あなた側で用意・決定が必要なもの

- [ ] 公開ドメイン、DNS、TLS、ホスティング先を決定する
- [x] 専用SupabaseのURL・公開／サーバーキー・DB接続情報をRailwayへ登録し、Auth／profiles／badge API・Postgres runtime schemaの実接続と新版配備を確認する（実会員登録は未完了）
- [ ] Stripe本番アカウント、Connect、Identity、Webhook endpointを有効化し、各Secretを登録する
- [ ] Resend等の送信ドメインを認証し、SPF・DKIM・DMARCを設定する
- [ ] 合同会社ONEの代表者、住所、電話番号、問い合わせ先、会社URLを確認し、特定商取引法表示と一致させる
- [ ] 適格請求書発行事業者の場合は、登録番号を確認して `LEGAL_INVOICE_NUMBER` に設定する（未確認の値は掲載しない）
- [ ] 利用規約、プライバシーポリシー、販売手数料、返金・紛争ルールを弁護士／専門家と確認する
- [ ] 本番のサポート窓口、本人確認の審査方針、返金・異議申立て時の責任者を決める
- [ ] Sentry等の監視先、アラート通知先、バックアップ保持期間、障害時の連絡手順を決める

## コード／インフラ側で公開前に必要なもの

- [x] 間接依存を含むハッシュ付きrequirementsを固定し、空のPython 3.12環境からの再インストールと互換性・脆弱性監査を確認する
- [x] 外部監視の送信前allowlist、添付除外、自動telemetry停止、問い合わせ番号のサーバー生成、URL access log停止をコードとテストで確認する
- [x] Supabaseの現行opaqueキー・旧JWTキーの互換性、ユーザーJWT分離、設定優先順をコード・Mockで検証する（実キーでの疎通は未完了）
- [ ] 隔離したSupabase環境でSQLの依存関係・適用・RLS試験を行い、確認済みの手順だけを本番へ適用する
- [x] 全ルートで利用するPostgres互換repositoryを実装（staging実接続済み、高負荷対策は未完了。単発売上の金融テーブルは別に正規化）
- [x] 不変な閲覧の不要UPDATE／COMMITを省略し、schema／stateの読込を統合する（ローカル実Postgres・外部CI合格、staging配備と匿名HTTP確認済み。GETの状態変更・outbox・金融検証・決済予約は省略しない。CWV／高負荷の合格ではない）
- [x] 注文作成・Webhook claim・状態更新・監査ログ・通知メールoutboxを同一DBトランザクションにする（ローカル実Postgresで検証）
- [ ] ステージング／本番の専用DB・TLS・session接続・権限・バックアップを設定し、復元訓練する
- [x] 通知メールworker、排他claim、再送キー、期限超過reviewを実装
- [ ] 通知メールworkerの配備、監視、review対応・保持期間を設定する
- [ ] Webhook outbox／再試行／dead-letterと一意制約で、複数ワーカーでも二重処理を防ぐ
- [x] Redisのカウンタと期限設定を原子的に実行し、接続障害時はアクセスを停止する
- [ ] Redisに実接続し、複数ワーカーで共通の制限・障害復旧を検証する
- [x] Supabase MFA登録・チャレンジ・AAL2確認・セッションID更新・重要操作時の10分間制限を実装する
- [ ] 実際のSupabaseで登録、誤コード、期限切れ、OAuth、トークン更新、端末紛失時の回復を検証する
- [x] SNS認証を明示allowlistで制御し、未設定のGoogle／Xを表示・redirectしない（任意機能。有効化はSupabase側の設定と実際のcallback検証後に`OAUTH_PROVIDERS`へ追加する）
- [x] 追記専用の永続監査ログと更新・削除拒否／内容変更検出を実装する
- [ ] 監査ログの長期検索・保持期間・バックアップ運用を設定する
- [ ] 退会期限を処理する定期ジョブをPostgres repository上で実装し、法定保持データだけ匿名化して残す
- [ ] Stripe invoice、charge、payment intent、refund、disputeのIDを注文・定期課金へ永続化する
- [x] runtime version 2へ単発JPYの決済・割当・分配・返金・銀行振込の追記台帳を実装（旧public SQLではなく実際のruntimeへ接続）
- [x] Sandbox限定の振込申請・分配・銀行振込・銀行振込前の分配取消worker、タイムアウト／保存前クラッシュ／Webhook照合をローカルで検証
- [ ] 実Stripe SandboxでJP/JPY口座と接続し、分配・入金・返金・取消を照合する
- [x] 結果不明のSandbox分配／銀行振込をGETだけで照合し、別管理者2名・MFA・30分期限で復旧承認する（ローカルPostgres＋Mock Stripeで検証）
- [x] 結果不明のSandbox単発Checkout／追加支払い／全額Refundを、GET・別管理者2名・MFA・30分期限で照合復旧する（ローカルPostgres＋Mock Stripeで検証。再請求・再返金は行わない）
- [x] 結果不明のSandbox分配取消をGET・二者承認で復旧し、取消の意思を維持する（ローカルPostgres＋Mock Stripe。実Stripeの検証は別項目）
- [x] キャンセル合意と全明細を検証し、未要求の残りの全額返金だけを二者承認で1件ずつ再開する（Sandbox限定、既存要求は再送しない。ローカルPostgres＋Mock Stripeで検証）
- [ ] 振込失敗後の再振込、振込後の回収、定期課金台帳を仕上げる
- [ ] 120日案を見直し、Stripe手動振込の保有期限を満たす規約・自動処理・アラートを確定する
- [ ] 振込処理は運営保留、本人確認、異議申立て、返金、二者承認を検証してからStripe APIを呼ぶ
- [ ] Stripe ConnectのTransfer／Payout方式と周期を確定し、納品承諾前に販売者の銀行口座へ出金されないことを検証する
- [x] Supabaseの公開商品画像／非公開納品ファイルを分離する実装、参加者認可後の60秒署名URL、非demoの一時ディスク保存拒否、画像再変換をローカル試験する（実接続は別項目）
- [x] 既存webの実Pre-deployで、2bucketの公開範囲・上限・MIMEが設定と一致する読取接続を確認する（実ファイル保存・署名download・匿名拒否・失効は未試験）
- [x] 運営画面へ未送信メール・期限超過lease・結果不明決済・失敗振込の読取専用診断を追加する（worker稼働・通知到達の証明ではない）
- [ ] 非公開オブジェクトストレージの実保存・署名URL・匿名拒否・再配備後の保持と、ClamAVの実接続・EICAR検知・定義更新を確認する
- [ ] 商品画像／納品ファイルの保持期間、参照されない孤立ファイルの安全な清掃、バックアップ・復元を運用する
- [ ] production環境で `DEMO_MODE=false`、十分な `SESSION_SECRET`、`ALLOWED_HOSTS`、proxy設定を確認する
- [ ] 新版配備後、ホスティング側proxyを含む実ログ・実監視先でアクセストークン、Cookie、本人確認情報、納品ファイル内容が出ないことを確認する（アプリ／SDK側のローカル検査は完了）

## 本番相当環境で必ず行う試験

- [ ] 新規登録、メール確認、ログイン、ログアウト、パスワード再設定、OAuthを実サービスで通す
- [ ] 出品→安全審査→公開→相談→購入→納品→承諾→評価→売上振込を通す
- [ ] 即時提供→購入ライブラリ、再開待ち通知、ブロック、NDA、販売者クーポンを実サービスで通す
- [ ] 決済失敗、重複Webhook、順不同Webhook、期限切れ、全額／部分返金、異議申立てを通す
- [ ] 定期課金の開始、更新失敗、再開、解約、期間終了、返金を通す
- [ ] 本人確認の承認、否認、再提出、Connect制限中の振込停止を通す
- [ ] 50MB境界、破損ZIP、zip bomb、危険パス、マルウェア検知、署名URL失効を通す
- [ ] 主要導線をiPhone／Android／PC、キーボードのみ、スクリーンリーダーで確認する
- [ ] 実デプロイURLでLighthouseとCore Web Vitals（LCP／INP／CLS）を計測する
- [ ] 想定ピークの2倍で負荷試験し、DB接続数、p95応答、エラー率を監視する
- [ ] バックアップから別環境へ復元し、復旧時間を記録する

## ファイル保管・運営診断の確認手順

`STORAGE_PROVIDER=supabase`、`SUPABASE_PRIVATE_BUCKET=toolbako-deliveries`、`SUPABASE_IMAGE_BUCKET=toolbako-images`を既存webへ設定する。サーバーキーは封印した環境変数だけに保持する。納品bucketは非公開・50,000,000 bytes・`application/octet-stream`、画像bucketは公開・5,000,000 bytes・`image/webp`の一致を必須とする。商品画像は下書きでもURLを知る人が閲覧できるため、個人情報・未公開資料は載せない。

- `python -m app.deployment_check`：既存のDB／Authに加え、bucket設定を読取専用で確認。保存・メール送信・送金はしない。
- `python -m app.storage_probe`：bucket設定のみ確認。`--write-canary`を明示した1回の検証では、小さな人工画像・テキストをUUIDパスへ保存し、公開画像／60秒署名URL／匿名の納品拒否を確認した後、今回のパスだけ削除する。標準Pre-deployへ書込canaryを常設しない。削除失敗は不合格として個別対応し、bucket全消去・顧客ファイル削除はしない。
- `python -m app.operations`：DB内のメール・金融要求・銀行振込の滞留件数だけ確認。終了値2は要確認、1は診断失敗。管理画面にも同じ集計を表示する。件数0をworker稼働・通知到達・決済成功の証明にしない。結果不明は無条件に再送しない。

ファイル保存と業務DBのcommitは別のサービスであり、通信切断・DB停止・画像編集では参照されないファイルが残り得る。保持期限と参照確認付き清掃を運用するまで、保存・署名URLの合格だけで運用完了にしない。商品を公開する前に再配備後の保持、実ClamAV／EICARと定義更新、署名URL失効、実利用者の認可を別途試験する。

## 追加サービスの予算・承認

2026-10-13時点でユーザーは「先に予算・構成を相談したい」と回答。新しい常時稼働サービスは未承認のため作成しない。既存Supabase内の2bucketと既存webの変更に限定する。既存サービス内の容量・転送量も料金体系に従い、無料を保証しない。

推奨案は、外部公開しないClamAV、共通アクセス制限のRedis、メール／金融処理worker。ClamAV公式推奨RAMは3〜4GiBで、RailwayのRAM料金は実使用1GBあたり月10米ドル。4GBを常時使用すればRAMだけで月約40米ドルとなる。worker／RedisのRAM・CPU・転送・Volumeは別途かかる。初期の実測・上限設計前に月20〜30米ドルを保証しない。構成、許容月額、超過時の安全な停止方針を相談してから配備する。

出典：[ClamAV Docker要件](https://docs.clamav.net/manual/Installing/Docker.html)、[Railway料金](https://docs.railway.com/pricing/plans)。料金・仕様は配備前に再確認する。

## 公開判定

上記の「コード／インフラ」と「本番相当環境の試験」に未完了が1件でもある間は、実カード決済を有効にしないでください。画面だけを一般公開する場合も、デモであることを明示し、個人情報と実ファイルを受け取らない運用にします。
