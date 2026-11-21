---
title: 運営者情報
---
| 項目 | 内容 |
|---|---|
| サイト名 | {{ site.name }} |
| 運営者 | {{ site.operator.name or "（公開前に記載します）" }} |
| お問い合わせ | {{ site.operator.contact or "（公開前に記載します）" }} |
{% if site.operator.x_account %}| X（旧Twitter） | [@{{ site.operator.x_account }}](https://x.com/{{ site.operator.x_account }}) |
{% endif %}

## このサイトについて

{{ site.name }}は、個人と小さな事業者が仕事で使うAIツールを選ぶための比較メディアです。料金・機能・データの扱いを、各サービスの公式サイトや公式ヘルプの公開情報にもとづいて整理しています。

## 記事の誤りについて

記事の内容に誤りや古い情報を見つけた場合は、上記のお問い合わせ先までお知らせください。確認のうえ訂正し、記事の更新日を改めます。

記事の作り方と広告の扱いは[編集方針・広告について](/policy/)をご覧ください。
