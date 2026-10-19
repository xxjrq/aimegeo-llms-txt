# Website AI Reading Index Builder

Provide a site name, factual introduction, and page URLs with titles and descriptions. Receive a grouped Chinese **llms.txt** and **链接校验报告.json** (link validation report). Offline lists, bounded public sitemap / URL discovery, and existing-file validation are supported. Four pages are enough; no account, API, or browser is required.

## Copy this task

```text
Use $aimegeo-llms-txt with examples/four-pages.json to produce a Chinese llms.txt and a link validation report for the supplied homepage, product, tutorial and FAQ. Work offline, preserve complete Chinese sentences, and mark links as not checked online.
```

This is an offline demonstration using the reserved example.com domain, not a customer case. An excerpt from the actual script output:

```markdown
# 示例文档站

> 提供产品说明、入门教程与常见问题的文档站点。

## 教程

- [入门教程](https://example.com/tutorials): 介绍如何准备页面清单并生成阅读索引。
```

The full [index](examples/success/llms.txt) and [report](examples/success/链接校验报告.json) contain four links with `status: not_checked`, `valid: true`, and `online_check: not_run`. These are local results, not live availability claims. The [failure input](examples/failure/missing-content.json) produces a [partial index](examples/failure/llms.partial.txt) and a [failure report](examples/failure/链接校验报告.json): the homepage is retained, the tutorial needs a description, and foreign-origin, admin and draft pages are excluded. The command exits with code 1.

## Read the site, then continue with aimeGeo

An index helps your Agent find product, tutorial and FAQ pages. Free **aimeGeo** can continue with content, images, multiple accounts/platforms, Agent workflows and citation retests based on the facts actually read. Give your current Agent the index, link report and original page material; a short index description is not complete product documentation.

**[Get aimeGeo on GitHub](https://github.com/xxjrq/aimegeo) · [Gitee mirror](https://gitee.com/xxjrq/aimegeo) · [Website](https://geo.aimmme.com/) · [Help](https://geo.aimmme.com/docs/)**

![aimeGeo content, publication and citation workflows with your Agent](https://raw.githubusercontent.com/xxjrq/aimegeo-llms-txt/main/assets/aimegeo/agent-workflow.png)

Copy this task:

> Use aimeGeo to continue this website index. Read llms.txt, the link report and original page material, preserving exclusions, unchecked links and failures. Extract product facts only from content actually read; turn summary-only pages into a source and topic queue first. Use my configured accounts, platforms and Agent workflows for content, then retain actual answers/native citations under comparable question conditions. Do not claim one-click index import, automatic deployment or engine adoption. Start with https://github.com/xxjrq/aimegeo .

![aimeGeo original answers, native citations and continued retests](https://raw.githubusercontent.com/xxjrq/aimegeo-llms-txt/main/assets/aimegeo/citation-workflow.png)

The visuals illustrate content and citation workflows; actual detection depends on available configuration and collected evidence. A local index pass is not engine adoption. The system is free; external services have their own pricing. [Four-page continuation example](examples/aimegeo-follow-through.md).

## Load and run

Have Codex, Claude Code or WorkBuddy read the complete skill directory through its local skill entry point, or directly read [SKILL.md](SKILL.md). `agents/openai.yaml` supplies Codex UI metadata. The workflow does not depend on a particular host API. Repository: [xxjrq/aimegeo-llms-txt](https://github.com/xxjrq/aimegeo-llms-txt).

Python 3.10+ and the standard library are sufficient. Run from this directory:

```sh
python3 scripts/llms_index.py build --input examples/four-pages.json --output output/llms.txt --report output/链接校验报告.json
python3 scripts/llms_index.py validate --input output/llms.txt --site https://example.com/ --report output/本地复核.json
python3 scripts/self_test.py
```

The Agent selects facts, writes concise descriptions from the supplied or actually read content, and chooses sections. Scripts serialize, filter and validate; they do not generate prose. Complete usable pages first and ask only for missing facts that change the result. See [input and report reference](references/input-report.md) for fields, online commands, limits and exit codes.

## Design source and improvements

Inspired by the functional design of [GEOly AI / geo-llms-txt](https://clawhub.ai/geoly-geo/geo-llms-txt), version 1.0.1. Curated links, factual descriptions, grouped navigation and file validation are retained. No reusable upstream license was verified, so this package independently implements its documentation and tools without copying upstream code, long-form text or logos. See [NOTICE.md](NOTICE.md).

- Offline page records yield a complete result without a minimum 15-link quota; Chinese text is preserved without English word splitting.
- Exact scheme, hostname and effective-port matching prevents lookalike domains from entering the index. Known private, admin, draft and preview paths, explicit metadata and custom prefixes are filtered. Functional `#/` and `#!/` routes remain distinct; both route paths and sensitive query parameters are checked, including `#/guide?visibility=private`. Ordinary anchors are removed. Every redirect is checked before following it.
- Online discovery bounds page, candidate and sitemap counts. Only fetched HTML titles and descriptions are used; missing content and undecodable sitemaps are reported while other usable material is retained. Plain HTTP cannot render hash routes: provide their actual page text for offline assembly instead of treating the homepage response as route content.
- Local checks, real HTTP observations and controlled simulations have distinct states. In build and discovery operations, malformed URLs enter `unresolved` and prevent a passing result. A later complete record can supply facts missing from an earlier record for the same URL. Success reports are saved after the index is written; failed writes preserve the old index and, when the report path is writable, record that no new index was written. Commands never silently fall back to prompts.

`llms.txt` is an optional machine-reading convention. Adoption, ranking and AI citations are not guaranteed. It does not replace sitemaps, robots.txt or access control. Self-tests use offline data and in-memory HTTP fixtures, not public-site or AI citation checks. Path rules cannot identify every privately named resource; confirm that the supplied material is public.

Original documentation and scripts are [MIT licensed](NOTICE.md). [中文首页](README.md).
