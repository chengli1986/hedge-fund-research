# Listing fetchers: template or hand-written

Every production source has a decision here. `listing_templates.py` drives the
ones marked TEMPLATE from a spec in `config/sources.json`; the rest keep their
hand-written function in `fetch_articles.py`.

A source moves only when `scripts/compare_fetchers.py` shows the template
returning exactly the same titles, urls and dates from the live site. Every
switched source keeps its hand-written function, so a rollback is one flag
(`listing_template_active`).

Two rules decided the close calls:

- **One source is a hack, two with the same shape is a knob.** Every knob in
  the template (`path_prefix`, `date_attr`, `wait_until`, `date_separator`,
  `sort`, field chains) has at least two users.
- **An A/B that agrees today is necessary, not sufficient.** Read the
  hand-written function for rules nothing triggers right now, and ask what
  losing them would do. Losing a rule that prevents *wrong data* is
  disqualifying; losing one that would make the fetch return *fewer or no*
  rows is not, because zero fetches already alert (per-source isolation,
  `SOURCE_BROKEN`).

`tests/test_template_decisions_doc.py` fails if this file and
`config/sources.json` disagree.

## TEMPLATE — card_list (12)

| source | note |
| --- | --- |
| cambridge-associates | first migration |
| northleaf-capital | exposed the template's own word-fusing defect before switching |
| blue-owl-capital | `path_prefix` (PDF viewers on another path), `date_attr` |
| rothschild-co-am | `date_attr` |
| apollo-global-management | drops category, summary |
| natixis-im | `date_attr` on a custom element; its depth guard is covered by "no title, no row" (13 shallow links on the live page, none with a title div) |
| aberdeen | `wait_selector` |
| wellington | its hand-written fetcher was returning 10 rows with 6 distinct URLs; fixed first, then switched |
| pimco | `wait_until=domcontentloaded` |
| acadian-asset | month-granularity date stays in `date_raw`, which publish reads |
| troweprice | `date_separator` ("Date · Category"); U+00A0 in titles fixed on the hand-written side first |
| msci-research | `date_separator`, `date_part=after`. Reports "order differs" on about half of A/B runs: the site varies card order and serves exactly max_articles cards, so the set cannot change |

## TEMPLATE — rss_feed (5)

| source | note |
| --- | --- |
| ark-invest | `categories` whitelist, `sort`; drops category, summary |
| amundi | ISO pubDate |
| verdad-capital | Mailchimp archive feed |
| loomis-sayles | `path_prefix`, `sort` |
| resonanz-capital | `sort` |

## TEMPLATE — api_json (2)

| source | note |
| --- | --- |
| jpmam | AEM model.json |
| mfs-investment-management | Solr endpoint with params; entity-in-title fixed on the hand-written side first |

## HAND-WRITTEN — the listing needs code (14)

| source | why |
| --- | --- |
| kkr | URL lives in a JSON blob in `data-cmp-data-layer`, plus an AEM path rewrite |
| bridgewater | the date is in the card's grandparent |
| brookfield | title from `aria-label`, date by regex over card text, links filtered by a path regex |
| capital-group | no card container; the title is found by climbing up to ten parents and scrubbing UI noise |
| cohen-steers | builds its own Playwright session and reads every article's JSON-LD date in-page to reuse the Cloudflare clearance cookie |
| de-shaw | title and URL recovered by regex from the card's mailto share link |
| lazard-am | `_canonical_lazard_url` rewrite, title from `aria-label` |
| partners-group | walks three pages; dd.mm.yyyy would be mis-read by parse_date |
| research-affiliates | title from an `img` alt, and the date is pulled by regex out of the anchor's mixed text |
| baillie-gifford | title from a `title` attribute — the only user a `title_attr` knob would have, since research-affiliates cannot move anyway |
| oaktree | external cards carry their target in `data-link` with `href="#"`, and audio and text versions of one memo are deduplicated by title |
| robeco | URL regex `/insights/\d{4}/\d{2}/`, dd-mm-yyyy dates |
| matthews-asia | **A/B agrees today.** Its fetcher rejoins titles split across an `h4` and the following `p`; two different pieces were once both stored as "China Innovation:". Losing it corrupts data silently |
| franklin-templeton | **A/B agrees today.** It requires a date so undated hub/collection tiles never become rows, and re-sorts by date. Neither is exercised right now |

## HAND-WRITTEN — the source is a different kind of thing (9)

| source | why |
| --- | --- |
| man-group | attestation form |
| aqr | attestation form |
| gmo | scrapes its own API endpoint out of a `data-endpoint` attribute first |
| gsam | builds `gsam_summary` by joining two fields, and `fetch_content` reads it — the one extra listing field with a consumer |
| metlife-im | rewrites AEM repository paths into public URLs, sends two disclaimer cookies |
| principal-am | scavenges a live Coveo bearer token from a browser session, then POSTs |
| janus-henderson | the index carries no dates; each article page must be opened for one |
| ares-management | sitemap + per-article fetch |
| goehring-rozencwajg | sitemap + per-article fetch |

A `sitemap` type was considered for the last two and not built: what they
share (fetch the sitemap, filter locs, sort by lastmod) is thin, and what
differs (title chain, site-suffix stripping, JSON-LD vs a page selector for
the date) is most of both functions. Revisit if a third one appears.
