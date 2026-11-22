# ツールバコ マガジン（AIツール比較メディア）

仕事で使うAIツールを公式情報で比較し、アフィリエイトで収益化する静的サイト。
マーケットプレイス本体（リポジトリ直下の FastAPI アプリ）とは独立しており、このフォルダだけで完結する。

## ビルド

```bash
cd media
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python build.py                               # プレビュー（noindex）
SITE_URL=https://example.com .venv/bin/python build.py  # 本番
```

出力は `dist/`。ホスティング（Cloudflare Pages など）の設定は次のとおり。

| 項目 | 値 |
|---|---|
| ルートディレクトリ | `media` |
| ビルドコマンド | `pip install -r requirements.txt && python build.py` |
| 出力ディレクトリ | `dist` |
| 環境変数 | `SITE_URL`（本番のみ。例 `https://example.com`） |

`SITE_URL` がない間は全ページ noindex・robots.txt も全拒否になる。
本番ビルドは `site.yaml` の `operator.name` と `operator.contact` が空だと失敗する。

## 構成

| 場所 | 役割 |
|---|---|
| `site.yaml` | サイト名、運営者情報、カテゴリ |
| `data/links.yaml` | 外部リンクの台帳。`affiliate_url` を入れると全記事が提携リンクに切り替わり、該当記事にPR表記が出る |
| `content/articles/*.md` | 記事。ファイル名がURLになる |
| `content/pages/*.md` | 固定ページ（運営者情報・編集方針・プライバシー） |
| `dist/social-queue.json` | 記事の `social:` から作るSNS投稿の候補 |

記事内のリンクは `[[link:キー|表示文字]]`、ボタンは `[[cta:キー|表示文字]]` と書く。URLを記事に直接書かない。

## 編集ルール（build.py が検査するもの）

- front matter に `title` `description`（50〜130字） `category` `published` `updated` `verified` `sources` が必須
- `sources` は公式ページの https URL。本文の数字は必ずここに挙げたページで確認したものに限る
- `verified`（確認日）が120日を超えると本番ビルドが失敗する → 公式ページを再確認して更新する
- 「実際に使ってみた」等の表現は `hands_on: true` の記事だけ。実際に使っていない記事には付けない
- 「業界No.1」「最安」などの根拠が要る最上級表現は使わない
- SNS投稿文は110字以内

## 編集ルール（人とAIが守るもの）

- 料金・仕様は**公式ページを直接開いて**確認する。検索結果の抜粋や他のブログの数字は使わない
  （2026年11月の初回調査では、検索経由の数字に tl;dv の料金など複数の誤りがあった）
- 公式ページで確認できなかった項目は書かないか、「公式サイトで確認」と案内する
- 提携の有無・報酬額で比較の順番や評価を変えない
- 海外からのアクセスでは価格が現地通貨で出るサービスがある（Rimo など）。円建て価格は日本向けの公式記載で確認する
- 記事を直したら `updated` を、事実を再確認したら `verified` を更新する

## 定期作業

1. 既存記事の事実を公式ページで再確認し、変更があれば直す（料金改定・名称変更・サービス終了）
2. 新しい記事を追加する（1クラスターを深く。薄い記事を量産しない）
3. `social-queue.json` の未投稿分をSNSに出す
4. 提携承認が下りたら `data/links.yaml` に `affiliate_url` を入れる
5. 週次で、公開した記事・直した内容・アクセスと成果を報告する
