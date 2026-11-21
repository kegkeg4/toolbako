"""Static site builder for the AI tool media site.

Usage:
    python build.py            # preview build (noindex) into dist/
    SITE_URL=https://example.com python build.py   # production build

Editorial rules are enforced here so that an article that breaks them
cannot be published by accident. See README.md.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import os
import re
import shutil
import sys
from pathlib import Path
from xml.sax.saxutils import escape as xml_escape

import markdown
import yaml
from jinja2 import Environment, FileSystemLoader, select_autoescape

ROOT = Path(__file__).parent
DIST = ROOT / "dist"
SHORTCODE = re.compile(r"\[\[(link|cta):([a-z0-9-]+)\|([^\]]+)\]\]")
FRONT_MATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)
REQUIRED = ("title", "description", "category", "published", "updated", "verified", "sources")
# Facts older than this must be re-verified before the build passes in production.
MAX_VERIFIED_AGE_DAYS = 120
# Claims an article may not make: nobody on this site has hands-on tested anything
# unless the article says so explicitly with `hands_on: true`.
FORBIDDEN_WITHOUT_HANDS_ON = ("実際に使ってみた", "使ってみました", "試してみました", "体験レビュー")
# Superlatives that need evidence under the Premiums and Representations Act.
FORBIDDEN_ALWAYS = ("業界No.1", "業界最安", "絶対に", "必ず稼げ", "最強の")


class BuildError(Exception):
    pass


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def parse(path: Path) -> tuple[dict, str]:
    match = FRONT_MATTER.match(path.read_text(encoding="utf-8"))
    if not match:
        raise BuildError(f"{path.name}: front matter がありません")
    return yaml.safe_load(match.group(1)) or {}, match.group(2)


def as_date(value, where: str) -> dt.date:
    if isinstance(value, dt.datetime):
        return value.date()
    if isinstance(value, dt.date):
        return value
    raise BuildError(f"{where}: 日付は YYYY-MM-DD で書いてください ({value!r})")


def render_shortcodes(body: str, links: dict, where: str) -> tuple[str, bool]:
    """Replace [[link:key|text]] / [[cta:key|text]]. Returns (body, uses_affiliate)."""
    uses_affiliate = False

    def replace(match: re.Match) -> str:
        nonlocal uses_affiliate
        kind, key, text = match.groups()
        entry = links.get(key)
        if not entry:
            raise BuildError(f"{where}: links.yaml に '{key}' がありません")
        affiliate = (entry.get("affiliate_url") or "").strip()
        url = affiliate or entry["official_url"]
        if affiliate:
            uses_affiliate = True
        rel = "sponsored nofollow noopener" if affiliate else "noopener"
        anchor = (
            f'<a href="{html.escape(url, quote=True)}" rel="{rel}" target="_blank"'
            f' data-out="{key}">{html.escape(text)}</a>'
        )
        if kind == "cta":
            # Blank lines keep the block out of the surrounding paragraph.
            return f'\n\n<p class="cta">{anchor}</p>\n\n'
        return anchor

    return SHORTCODE.sub(replace, body), uses_affiliate


def to_html(body: str) -> tuple[str, list[dict]]:
    md = markdown.Markdown(
        extensions=["tables", "toc", "attr_list", "sane_lists"],
        extension_configs={"toc": {"toc_depth": "2-2", "slugify": lambda value, sep: _slug(value)}},
    )
    rendered = md.convert(body)
    rendered = rendered.replace("<table>", '<div class="table-wrap"><table>').replace("</table>", "</table></div>")
    toc = [{"id": item["id"], "name": item["name"]} for item in md.toc_tokens]
    return rendered, toc


def _slug(value: str) -> str:
    value = re.sub(r"\s+", "-", value.strip())
    return re.sub(r"[^\w\-ぁ-んァ-ヶ一-龠ー]", "", value) or "section"


def load_articles(links: dict, production: bool, today: dt.date) -> list[dict]:
    articles = []
    for path in sorted((ROOT / "content" / "articles").glob("*.md")):
        meta, body = parse(path)
        where = path.name
        if meta.get("draft"):
            continue
        missing = [key for key in REQUIRED if not meta.get(key)]
        if missing:
            raise BuildError(f"{where}: front matter に {', '.join(missing)} が必要です")
        for key in ("published", "updated", "verified"):
            meta[key] = as_date(meta[key], f"{where} {key}")
        if not 50 <= len(meta["description"]) <= 130:
            raise BuildError(f"{where}: description は50〜130字にしてください (現在{len(meta['description'])}字)")
        for source in meta["sources"]:
            if not (isinstance(source, dict) and source.get("title") and str(source.get("url", "")).startswith("https://")):
                raise BuildError(f"{where}: sources は title と https の url が必要です")
        text = meta["title"] + meta["description"] + body
        banned = list(FORBIDDEN_ALWAYS)
        if not meta.get("hands_on"):
            banned += FORBIDDEN_WITHOUT_HANDS_ON
        hits = [word for word in banned if word in text]
        if hits:
            raise BuildError(f"{where}: 使えない表現があります: {', '.join(hits)}")
        age = (today - meta["verified"]).days
        if production and age > MAX_VERIFIED_AGE_DAYS:
            raise BuildError(f"{where}: 情報の確認日が{age}日前です。再確認して verified を更新してください")
        body, uses_affiliate = render_shortcodes(body, links, where)
        content, toc = to_html(body)
        articles.append(
            {
                **meta,
                "slug": path.stem,
                "url": f"/articles/{path.stem}/",
                "content": content,
                "toc": toc,
                "uses_affiliate": uses_affiliate,
            }
        )
    slugs = {article["slug"] for article in articles}
    for article in articles:
        for related in article.get("related", []):
            if related not in slugs:
                raise BuildError(f"{article['slug']}.md: related の '{related}' が存在しません")
    return sorted(articles, key=lambda item: (item["updated"], item["published"]), reverse=True)


def load_pages(site: dict) -> list[dict]:
    env = Environment(autoescape=False)
    pages = []
    for path in sorted((ROOT / "content" / "pages").glob("*.md")):
        meta, body = parse(path)
        body = env.from_string(body).render(site=site)
        content, _ = to_html(body)
        pages.append({**meta, "slug": path.stem, "url": f"/{path.stem}/", "content": content})
    return pages


def write(path: str, text: str) -> None:
    target = DIST / path.lstrip("/")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def build() -> None:
    site = load_yaml(ROOT / "site.yaml")
    links = load_yaml(ROOT / "data" / "links.yaml")
    site_url = os.environ.get("SITE_URL", "").rstrip("/")
    production = bool(site_url)
    today = dt.date.today()

    for key, entry in links.items():
        if not str(entry.get("official_url", "")).startswith("https://"):
            raise BuildError(f"links.yaml {key}: official_url が必要です")
        affiliate = (entry.get("affiliate_url") or "").strip()
        if affiliate and not affiliate.startswith("https://"):
            raise BuildError(f"links.yaml {key}: affiliate_url は https で始めてください")

    if production:
        operator = site.get("operator") or {}
        if not operator.get("name") or not operator.get("contact"):
            raise BuildError("site.yaml: 本番公開には operator.name と operator.contact が必要です")

    site = {**site, "url": site_url or "http://localhost:8000", "production": production}
    categories = site["categories"]
    articles = load_articles(links, production, today)
    for article in articles:
        if article["category"] not in categories:
            raise BuildError(f"{article['slug']}.md: カテゴリ '{article['category']}' は site.yaml にありません")
    pages = load_pages(site)
    by_slug = {article["slug"]: article for article in articles}

    env = Environment(
        loader=FileSystemLoader(ROOT / "templates"),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.globals.update(site=site, categories=categories, year=today.year, nav_pages=pages)

    if DIST.exists():
        shutil.rmtree(DIST)
    shutil.copytree(ROOT / "static", DIST / "static")

    write("index.html", env.get_template("index.html").render(articles=articles, path="/"))
    for slug, category in categories.items():
        items = [article for article in articles if article["category"] == slug]
        write(
            f"category/{slug}/index.html",
            env.get_template("category.html").render(category=category, slug=slug, articles=items, path=f"/category/{slug}/"),
        )
    for article in articles:
        related = [by_slug[slug] for slug in article.get("related", [])]
        json_ld = {
            "@context": "https://schema.org",
            "@type": "Article",
            "headline": article["title"],
            "description": article["description"],
            "datePublished": article["published"].isoformat(),
            "dateModified": article["updated"].isoformat(),
            "mainEntityOfPage": site["url"] + article["url"],
            "author": {"@type": "Organization", "name": site["name"]},
            "publisher": {"@type": "Organization", "name": site["name"]},
        }
        write(
            article["url"] + "index.html",
            env.get_template("article.html").render(
                article=article,
                related=related,
                category=categories[article["category"]],
                json_ld=json.dumps(json_ld, ensure_ascii=False).replace("</", "<\\/"),
                path=article["url"],
            ),
        )
    for page in pages:
        write(page["url"] + "index.html", env.get_template("page.html").render(page=page, path=page["url"]))
    write("404.html", env.get_template("404.html").render(path="/404.html"))

    urls = ["/"] + [f"/category/{slug}/" for slug in categories] + [a["url"] for a in articles] + [p["url"] for p in pages]
    sitemap = "".join(f"<url><loc>{xml_escape(site['url'] + url)}</loc></url>" for url in urls)
    write("sitemap.xml", f'<?xml version="1.0" encoding="UTF-8"?><urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{sitemap}</urlset>')
    if production:
        write("robots.txt", f"User-agent: *\nAllow: /\nSitemap: {site['url']}/sitemap.xml\n")
    else:
        write("robots.txt", "User-agent: *\nDisallow: /\n")

    items = "".join(
        f"<item><title>{xml_escape(a['title'])}</title><link>{xml_escape(site['url'] + a['url'])}</link>"
        f"<guid>{xml_escape(site['url'] + a['url'])}</guid><description>{xml_escape(a['description'])}</description>"
        f"<pubDate>{dt.datetime.combine(a['published'], dt.time(0, 0)).strftime('%a, %d %b %Y 00:00:00 +0900')}</pubDate></item>"
        for a in articles[:20]
    )
    write(
        "feed.xml",
        f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0"><channel><title>{xml_escape(site["name"])}</title>'
        f'<link>{xml_escape(site["url"])}/</link><description>{xml_escape(site["description"])}</description>{items}</channel></rss>',
    )

    # Posts waiting to be published on social media, consumed by the posting job.
    queue = [
        {"slug": a["slug"], "url": site["url"] + a["url"], "text": text, "promotion": a["uses_affiliate"]}
        for a in articles
        for text in a.get("social", [])
    ]
    for post in queue:
        if len(post["text"]) > 110:
            raise BuildError(f"{post['slug']}.md: social の投稿は110字以内にしてください ({len(post['text'])}字)")
    write("social-queue.json", json.dumps(queue, ensure_ascii=False, indent=2))

    active = sum(1 for entry in links.values() if (entry.get("affiliate_url") or "").strip())
    mode = "本番" if production else "プレビュー(noindex)"
    print(f"{mode}: 記事{len(articles)}本 / 固定ページ{len(pages)}本 / 提携リンク {active}/{len(links)} 件が有効")


if __name__ == "__main__":
    try:
        build()
    except BuildError as error:
        print(f"ビルド失敗: {error}", file=sys.stderr)
        sys.exit(1)
