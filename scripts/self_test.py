#!/usr/bin/env python3
"""Offline business regression tests; HTTP replies are controlled simulations."""

import contextlib
from email.message import Message
import io
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parent))
import llms_index as index

ROOT = Path(__file__).resolve().parents[1]
SITE = "https://example.com/"


def html_reply(title="页面", description="来自实际读取的简介。", extra=""):
    body = f'<html><head><title>{title}</title><meta name="description" content="{description}"></head><body>{extra}</body></html>'
    return index.Reply(200, {"content-type": "text/html; charset=utf-8"}, body.encode())


def xml_reply(kind, urls):
    item = "sitemap" if kind == "sitemapindex" else "url"
    entries = "".join(f"<{item}><loc>{index.html.escape(url)}</loc></{item}>" for url in urls)
    return index.Reply(200, {"content-type": "application/xml"},
                       f'<{kind} xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">{entries}</{kind}>'.encode())


class FixtureHTTP:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def __call__(self, url):
        self.calls.append(url)
        if url not in self.routes:
            raise AssertionError("unexpected HTTP request: " + url)
        reply = self.routes[url]
        if isinstance(reply, Exception):
            raise reply
        return reply


class BusinessTests(unittest.TestCase):
    def setUp(self):
        self.data = json.loads((ROOT / "examples/four-pages.json").read_text(encoding="utf-8"))
        self.no_network = patch("urllib.request.OpenerDirector.open",
                                side_effect=AssertionError("live HTTP is forbidden in self_test"))
        self.no_network.start()
        self.addCleanup(self.no_network.stop)

    def client(self, routes):
        transport = FixtureHTTP(routes)
        return index.PublicClient(index.Scope(SITE), transport=transport), transport

    def test_four_pages_complete_and_exact_content(self):
        text, report = index.build(self.data)
        self.assertTrue(report["valid"])
        parsed = [index.LINK.fullmatch(line).groups() for line in text.splitlines() if line.startswith("- ")]
        self.assertEqual(parsed, [(p["title"], p["url"], p["description"]) for p in self.data["pages"]])
        self.assertEqual(report["counts"]["links"], 4)
        self.assertEqual(report["online_check"], "not_run")
        self.assertTrue(all(p["status"] == "not_checked" for p in report["links"]))
        self.assertEqual(report["failed_links"], [])
        self.assertTrue(index.validate(text, SITE)["valid"])

    def test_success_artifacts_reproduce_actual_business_result(self):
        text, report = index.build(self.data)
        self.assertEqual(text, (ROOT / "examples/success/llms.txt").read_text(encoding="utf-8"))
        saved = json.loads((ROOT / "examples/success/链接校验报告.json").read_text(encoding="utf-8"))
        self.assertEqual(report, saved)

    def test_chinese_description_preserved_not_word_truncated(self):
        description = "这是没有英文空格的中文简介，说明资料整理和索引生成的真实操作。" * 9
        self.data["pages"][0]["description"] = description
        text, report = index.build(self.data)
        self.assertEqual(index.LINK.fullmatch(next(l for l in text.splitlines() if l.startswith("- "))).group(3),
                         description)
        self.assertEqual(report["warnings"][0]["reason"], "long_description_preserved")

    def test_plain_text_overview_block_markers_round_trip_through_own_validation(self):
        for overview, escaped in (('# 产品资料', r'\# 产品资料'),
                                  ('## 产品资料', r'\## 产品资料'),
                                  ('- 公开资料说明', r'\- 公开资料说明'),
                                  ('+ 公开资料说明', r'\+ 公开资料说明'),
                                  ('1. 公开资料说明', r'1\. 公开资料说明'),
                                  ('2) 公开资料说明', r'2\) 公开资料说明'),
                                  ('---', r'\---')):
            with self.subTest(overview=overview):
                data = {**self.data, 'site': {**self.data['site'], 'overview': overview}}
                text, built = index.build(data)
                self.assertTrue(built['valid'])
                self.assertEqual(built['output_status'], 'complete')
                self.assertIn('\n' + escaped + '\n', text)
                checked = index.validate(text, SITE)
                self.assertTrue(checked['valid'], checked)
                self.assertEqual(len(checked['links']), len(self.data['pages']))
                self.assertEqual(data['site']['overview'], overview)

    def test_exact_origin_not_substring(self):
        scope = index.Scope(SITE)
        for url in ("https://evil-example.com/", "https://example.com.evil.test/", "https://docs.example.com/",
                    "http://example.com/", "https://example.com:8443/", "https://evil.test/?next=https://example.com/"):
            with self.subTest(url=url):
                self.assertEqual(scope.select(url)[1], "outside_origin")
        self.assertEqual(scope.select("https://EXAMPLE.com:443/faq#answer"), (SITE + "faq", None))

    def test_private_draft_encoded_and_explicit_metadata(self):
        scope = index.Scope(SITE, ["/members"])
        for url in ("/admin/settings", "/%61dmin/", "/a/%2e%2e/private/", "/%2564raft/", "/wp-admin/",
                    "/preview/one", "/faq?preview=true", "/faq?status=draft", "/faq?token=secret",
                    "/members", "/members/only", "/#/admin/settings", "/private;session=x/"):
            with self.subTest(url=url):
                self.assertIsNotNone(scope.select(url)[1])
        self.assertEqual(scope.select("/members-guide")[1], None)
        self.assertEqual(scope.select("/admin-guide")[1], None)
        self.assertEqual(scope.select("/public", {"visibility": "private"})[1], "not_public")
        self.assertEqual(scope.select("/public", {"status": "draft"})[1], "not_published")

    def test_credential_and_invalid_urls_rejected(self):
        scope = index.Scope(SITE)
        for url in ("https://example.com@evil-example.com/", "https://u:p@example.com/", "javascript:alert(1)",
                    "https://example.com:bad/", "https://[broken/", "https://example.com/a b", None):
            with self.subTest(url=url):
                self.assertIsNotNone(scope.select(url)[1])

    def test_private_hash_routes_with_queries_never_become_homepage(self):
        routes = ("#/admin?tab=users", "#!/admin?tab=users", "#/%61dmin?tab=users",
                  "#%2Fadmin%3Ftab%3Dusers", "#/private;session=x?tab=users",
                  "#/dashboard/reports?year=2026")
        for route in routes:
            with self.subTest(route=route):
                raw = SITE + route
                self.assertEqual(index.Scope(SITE).select(raw)[1], "private_path")
                data = {**self.data, "pages": [{"url": raw, "title": "后台资料", "description": "用户管理。"}]}
                text, report = index.build(data)
                self.assertIsNone(text)
                self.assertFalse(report["valid"])
                client, transport = self.client({})
                data, report = index.discover(SITE, urls=[raw], client=client)
                self.assertEqual(data["pages"], [])
                self.assertEqual(transport.calls, [])
                self.assertEqual(report["online_check"], "not_run")
        self.assertEqual(index.Scope(SITE).select(SITE + "#/admin-guide?tab=users"),
                         (SITE + "#/admin-guide?tab=users", None))

    def test_sensitive_queries_inside_hash_routes_are_excluded_before_requests(self):
        routes = ("#/guide?visibility=private", "#!/guide?preview=true", "#/guide?status=draft",
                  "#%2Fguide%3Faccess%3Dinternal", "#/guide?%2574oken=secret", "#/guide?session=x")
        for route in routes:
            with self.subTest(route=route):
                raw = SITE + route
                self.assertEqual(index.Scope(SITE).select(raw)[1], "private_query")
                text, report = index.build({**self.data, "pages": [self.data["pages"][0],
                    {"url": raw, "title": "不应公开", "description": "私有资料。"}]})
                self.assertEqual(len(report["links"]), 1)
                self.assertEqual(report["excluded"][0]["reason"], "private_query")
                self.assertNotIn("私有资料", text)
                client, transport = self.client({})
                _, report = index.discover(SITE, urls=[raw], client=client)
                self.assertEqual(transport.calls, [])
                self.assertEqual(report["excluded"][0]["reason"], "private_query")

    def test_functional_hash_pages_and_queries_remain_distinct(self):
        routes = ("#/products", "#/tutorials", "#!/faq", "#/products?lang=zh",
                  "#/products?lang=en", "#/products?utm_source=duplicate")
        pages = [{"url": SITE + route, "title": "页面" + str(i), "description": "用户提供的该页简介。"}
                 for i, route in enumerate(routes)]
        text, report = index.build({**self.data, "pages": pages})
        self.assertTrue(report["valid"])
        self.assertEqual([row["url"] for row in report["links"]], [SITE + route for route in routes[:-1]])
        self.assertEqual([row["reason"] for row in report["excluded"]], ["duplicate"])
        self.assertTrue(index.validate(text, SITE)["valid"])
        self.assertEqual(index.canonical(SITE + "#%2Fproducts%3Flang%3Dzh"), SITE + "#/products?lang=zh")
        self.assertEqual(index.Scope(SITE, ["/members"]).select("/#/members/guide")[1], "excluded_prefix")

    def test_http_discovery_cannot_claim_hash_route_content(self):
        client, transport = self.client({SITE: html_reply("首页", "这是主页内容。")})
        data, report = index.discover(SITE, urls=[SITE, SITE + "#/products"], client=client)
        self.assertEqual(transport.calls, [SITE])
        self.assertEqual([page["url"] for page in data["pages"]], [SITE])
        self.assertFalse(report["valid"])
        self.assertEqual(report["unresolved"][0]["reason"], "hash_route_requires_supplied_content")

    def test_hash_query_escapes_preserve_literal_values_and_encoded_wrappers(self):
        urls = [SITE + route for route in ("#/search?q=C%23", "#/search?q=a%26b",
                                           "#!/search?q=%2523")]
        pages = [{**self.data["pages"][0], "url": url} for url in urls]
        text, report = index.build({**self.data, "pages": pages})
        self.assertTrue(report["valid"])
        self.assertEqual([p["url"] for p in report["links"]], urls)
        for url in urls:
            self.assertIn("](" + url + "):", text)
            self.assertEqual(index.canonical(url), url)
        self.assertTrue(index.validate(text, SITE)["valid"])
        self.assertEqual(index.canonical(SITE + "#%2Fsearch%3Fq%3DC%2523"), urls[0])

    def test_redirect_hash_routes_never_use_homepage_content(self):
        for route in ("/#/products", "/#!/search?q=C%23"):
            with self.subTest(route=route):
                client, transport = self.client({SITE + "alias": index.Reply(302, {"location": route}, b"")})
                data, report = index.discover(SITE, urls=[SITE + "alias"], client=client)
                self.assertEqual(transport.calls, [SITE + "alias"])
                self.assertEqual(data["pages"], [])
                self.assertFalse(report["valid"])
                row = report["unresolved"][0]
                self.assertEqual(row["reason"], "hash_route_requires_supplied_content")
                self.assertEqual(row["final_url"], SITE.rstrip("/") + route)
                self.assertEqual(row["http_status"], 302)
                text, _ = index.build({**self.data, "pages": [{**self.data["pages"][0], "url": SITE + "alias"}]})
                client, transport = self.client({SITE + "alias": index.Reply(302, {"location": route}, b"")})
                validation = index.validate(text, SITE, online=True, client=client)
                self.assertFalse(validation["valid"])
                self.assertEqual(validation["links"][0]["status"], "unresolved")
                self.assertEqual(transport.calls, [SITE + "alias"])

    def test_malformed_build_urls_are_unresolved_even_with_complete_pages(self):
        for raw in (SITE.replace(".com/", ".com:bad/products"), "https://[broken/", None):
            for has_complete_page in (False, True):
                with self.subTest(raw=raw, has_complete_page=has_complete_page):
                    pages = self.data["pages"][:1] if has_complete_page else []
                    text, report = index.build({**self.data, "pages": [*pages, {"url": raw,
                        "title": "产品", "description": "输入的产品简介。"}]})
                    self.assertFalse(report["valid"])
                    self.assertEqual(report["output_status"], "partial" if has_complete_page else "not_written")
                    self.assertEqual(report["counts"]["unresolved"], 1)
                    self.assertEqual(report["unresolved"][0]["reason"], "invalid_url")
                    self.assertEqual(report["excluded"], [])
                    self.assertEqual(report["failed_links"], [])
                    self.assertEqual(text is not None, has_complete_page)

    def test_complete_record_recovers_incomplete_duplicate(self):
        complete = {**self.data["pages"][0], "url": SITE + "guide"}
        for missing in ("title", "description"):
            incomplete = {k: v for k, v in complete.items() if k != missing}
            incomplete["url"] += "?utm_source=first#intro"
            for pages in ([incomplete, complete], [complete, incomplete], [incomplete, incomplete, complete]):
                with self.subTest(missing=missing, records=len(pages), first_complete=pages[0] is complete):
                    text, report = index.build({**self.data, "pages": pages}, max_pages=1)
                    self.assertIsNotNone(text)
                    self.assertTrue(report["valid"])
                    self.assertEqual(report["output_status"], "complete")
                    self.assertEqual([p["url"] for p in report["links"]], [complete["url"]])
                    self.assertEqual(report["unresolved"], [])
                    self.assertEqual([p["reason"] for p in report["excluded"]], ["duplicate"] * (len(pages) - 1))
                    self.assertTrue(index.validate(text, SITE)["valid"])

    def test_dedup_tracking_fragment_but_keep_functional_query(self):
        self.data["pages"] = [{**self.data["pages"][0], "url": url} for url in
                              (SITE + "faq?utm_source=x#one", SITE + "faq#two", SITE + "faq?lang=zh",
                               SITE + "faq?lang=en", SITE + "faq?b=2&a=1", SITE + "faq?a=1&b=2")]
        text, report = index.build(self.data)
        self.assertEqual(len(report["links"]), 5)
        self.assertEqual([p["reason"] for p in report["excluded"]], ["duplicate"])
        self.assertIn(SITE + "faq?lang=zh", text)
        self.assertIn(SITE + "faq?a=1&b=2", text)
        self.assertIn(SITE + "faq?b=2&a=1", text)

    def test_repeated_functional_query_values_preserve_input_order(self):
        urls = [SITE + 'product?id=2&id=1', SITE + 'product?id=1&id=2',
                SITE + '#/product?id=2&id=1', SITE + '#/product?id=1&id=2']
        self.data['pages'] = [{**self.data['pages'][0], 'url': url} for url in urls]
        text, report = index.build(self.data)
        self.assertTrue(report['valid'])
        self.assertEqual(len(report['links']), 4)
        self.assertEqual(report['excluded'], [])
        for url in urls:
            self.assertEqual(index.canonical(url), url)
            self.assertIn(url, text)

    def test_query_bytes_encodings_bare_flags_and_repeated_values_are_preserved(self):
        query = 'q=%D6%D0%CE%C4&id=2&id=1&flag&empty=&q=%E4%B8%AD%E6%96%87&space=a%20b&plus=a+b&slash=%2f'
        urls = [SITE + 'search?' + query, SITE + '#/search?' + query,
                SITE + 'search?q=%E4%B8%AD%E6%96%87', SITE + 'search?q=%d6%d0%ce%c4']
        pages = [{**self.data['pages'][0], 'url': url} for url in urls]
        text, report = index.build({**self.data, 'pages': pages})
        self.assertTrue(report['valid'])
        self.assertEqual([page['url'] for page in report['links']], urls)
        self.assertEqual(report['excluded'], [])
        for url in urls:
            self.assertEqual(index.canonical(url), url)
            self.assertIn('](' + url + '):', text)
        self.assertTrue(index.validate(text, SITE)['valid'])
        self.assertEqual(index.functional_query('%75tm_source=x&' + query + '&GCLID=x'), query)

    def test_distinct_non_utf8_query_values_never_collapse_to_replacement_characters(self):
        urls = [SITE + 'search?q=%D6%D0%CE%C4', SITE + 'search?q=%D6%D0%CE%C5']
        text, report = index.build({**self.data, 'pages': [
            {**self.data['pages'][0], 'url': url} for url in urls]})
        self.assertTrue(report['valid'])
        self.assertEqual([page['url'] for page in report['links']], urls)
        self.assertNotIn('%EF%BF%BD', text)
        client, transport = self.client({url: html_reply() for url in urls})
        data, report = index.discover(SITE, urls=urls, client=client)
        self.assertTrue(report['valid'])
        self.assertEqual(transport.calls, urls)
        self.assertEqual([page['url'] for page in data['pages']], urls)

    def test_markdown_entities_cannot_hide_private_query_parameters(self):
        text, _ = index.build(self.data)
        for separator in ('&amp;', '&#38;', '&#x26;'):
            with self.subTest(separator=separator):
                destination = SITE + 'search?lang=zh' + separator + 'visibility=private'
                extra = '\n- [私有查询](' + destination + '): 不得发起请求。\n'
                client, transport = self.client({p['url']: html_reply() for p in self.data['pages']})
                report = index.validate(text + extra, SITE, online=True, client=client)
                self.assertFalse(report['valid'])
                self.assertEqual(report['excluded'][-1]['reason'], 'private_query')
                self.assertEqual(report['excluded'][-1]['url'], SITE + 'search?lang=zh&visibility=private')
                self.assertNotIn(SITE + 'search?lang=zh&visibility=private', transport.calls)

    def test_markdown_entity_destination_is_requested_after_one_decode(self):
        for raw, actual in [('lang=zh&amp;page=2', 'lang=zh&page=2'),
                            ('lang=zh&amp;amp;page=2', 'lang=zh&amp;page=2'),
                            ('lang=zh&not=2', 'lang=zh&not=2')]:
            with self.subTest(raw=raw):
                text = '# 查询\n\n> 说明。\n\n## 页面\n\n- [查询](' + SITE + 'search?' + raw + '): 真实目标。\n'
                url = SITE + 'search?' + actual
                client, transport = self.client({url: html_reply()})
                report = index.validate(text, SITE, online=True, client=client)
                self.assertTrue(report['valid'])
                self.assertEqual(transport.calls, [url])

    def test_generated_entity_looking_literal_query_round_trips(self):
        url = SITE + 'search?q=one&copy;=two&amp;visibility=private'
        text, report = index.build({**self.data, 'pages': [{**self.data['pages'][0], 'url': url}]})
        self.assertTrue(report['valid'])
        self.assertIn('&amp;copy;=two&amp;amp;visibility=private', text)
        client, transport = self.client({url: html_reply()})
        report = index.validate(text, SITE, online=True, client=client)
        self.assertTrue(report['valid'])
        self.assertEqual(transport.calls, [url])

    def test_query_parentheses_are_encoded_for_markdown_round_trip(self):
        raw_urls = [SITE + 'search?q=)', SITE + 'search?q=(manual)',
                    SITE + '#/search?q=)', SITE + '#/search?q=(manual)',
                    SITE + 'search?q=%28encoded%29']
        expected = [SITE + 'search?q=%29', SITE + 'search?q=%28manual%29',
                    SITE + '#/search?q=%29', SITE + '#/search?q=%28manual%29',
                    raw_urls[-1]]
        text, report = index.build({**self.data, 'pages': [
            {**self.data['pages'][0], 'url': url} for url in raw_urls]})
        self.assertTrue(report['valid'])
        self.assertEqual([row['url'] for row in report['links']], expected)
        validated = index.validate(text, SITE)
        self.assertTrue(validated['valid'])
        self.assertEqual([row['url'] for row in validated['links']], expected)
        for url in expected:
            self.assertEqual(index.canonical(url), url)

    def test_unicode_query_is_utf8_encoded_without_reencoding_existing_bytes(self):
        raw = SITE + 'search?q=中文&legacy=%D6%D0%CE%C4&plus=a+b&id=2&id=1&flag'
        expected = SITE + 'search?q=%E4%B8%AD%E6%96%87&legacy=%D6%D0%CE%C4&plus=a+b&id=2&id=1&flag'
        self.assertEqual(index.canonical(raw), expected)
        self.assertEqual(index.canonical(SITE + '#/search?q=中文'), SITE + '#/search?q=%E4%B8%AD%E6%96%87')
        client, transport = self.client({expected: html_reply()})
        data, report = index.discover(SITE, urls=[raw], client=client)
        self.assertTrue(report['valid'])
        self.assertEqual(transport.calls, [expected])
        self.assertEqual(data['pages'][0]['url'], expected)

    def test_encoded_router_prefix_never_decodes_visible_query_bytes(self):
        query = 'q=a%26visibility=private&legacy=%D6%D0%CE%C4&id=2&id=1&flag&empty=&escape=%2526'
        raw = [SITE + '#' + prefix + '?' + query
               for prefix in ('%2Fsearch', '%252Fsearch', '%21%2Fsearch')]
        expected = [SITE + '#/search?' + query, SITE + '#!/search?' + query]
        for url in raw:
            canonical = index.canonical(url)
            self.assertIn(canonical, expected)
            self.assertEqual(index.Scope(SITE).select(url), (canonical, None))
        text, report = index.build({**self.data, 'pages': [
            {**self.data['pages'][0], 'url': url} for url in raw]})
        self.assertTrue(report['valid'])
        self.assertEqual([row['url'] for row in report['links']], expected)
        self.assertTrue(index.validate(text, SITE)['valid'])
        for url in expected:
            self.assertIn(url, text)
        client, transport = self.client({})
        reply, result = client.get(raw[0])
        self.assertIsNone(reply)
        self.assertEqual(result['reason'], 'hash_route_requires_supplied_content')
        self.assertEqual(transport.calls, [])

    def test_encoded_router_private_parameter_is_still_excluded(self):
        for prefix in ('%2Fsearch', '%252Fsearch', '%21%2Fsearch'):
            url = SITE + '#' + prefix + '?q=a%26b&visibility=private'
            self.assertEqual(index.Scope(SITE).select(url)[1], 'private_query')
            client, transport = self.client({})
            reply, result = client.get(url)
            self.assertIsNone(reply)
            self.assertEqual(result['reason'], 'private_query')
            self.assertEqual(transport.calls, [])

    def test_encoded_router_wrapper_and_existing_path_query_escapes_survive(self):
        pairs = [
            (SITE + '#%2Fsearch%3Fq%3Da%2526visibility%3Dprivate',
             SITE + '#/search?q=a%26visibility=private'),
            (SITE + '#%2Fdocs%252Fv1?legacy=%D6%D0%CE%C4&q=%2526',
             SITE + '#/docs%2Fv1?legacy=%D6%D0%CE%C4&q=%2526'),
            (SITE + 'docs%2Fv1?legacy=%D6%D0%CE%C4&q=a%26b',
             SITE + 'docs%2Fv1?legacy=%D6%D0%CE%C4&q=a%26b'),
        ]
        for raw, expected in pairs:
            with self.subTest(raw=raw):
                self.assertEqual(index.canonical(raw), expected)
                self.assertEqual(index.canonical(expected), expected)
                self.assertEqual(index.Scope(SITE).select(raw), (expected, None))

    def test_explicit_zero_port_is_invalid_and_never_becomes_default_origin(self):
        for raw in ('https://example.com:0/guide', 'http://example.com:0/guide'):
            with self.subTest(raw=raw):
                with self.assertRaises(index.InputError):
                    index.canonical(raw)
                self.assertEqual(index.origin(raw)[2], 0)
                self.assertEqual(index.Scope(SITE).select(raw), (None, 'invalid_url'))
                client, transport = self.client({})
                reply, result = client.get(raw)
                self.assertIsNone(reply)
                self.assertEqual(result['reason'], 'invalid_url')
                self.assertEqual(transport.calls, [])
        self.assertEqual(index.canonical('https://example.com:443/guide'), SITE + 'guide')
        self.assertEqual(index.origin(SITE)[2], 443)

    def test_duplicate_robots_headers_preserve_noindex_for_discover_and_validate(self):
        url = SITE + 'headers'
        for values in [('noindex', 'nofollow'), ('nofollow', 'googlebot: noindex'), ('index', 'nofollow')]:
            with self.subTest(values=values):
                def response(*args, **kwargs):
                    reply = io.BytesIO(html_reply().body)
                    reply.code = 200
                    reply.headers = Message()
                    reply.headers.add_header('Content-Type', 'text/html; charset=utf-8')
                    for name, value in zip(('X-Robots-Tag', 'x-robots-tag'), values):
                        reply.headers.add_header(name, value)
                    return reply
                def client():
                    result = index.PublicClient(index.Scope(SITE))
                    result.opener = Mock(open=Mock(side_effect=response))
                    return result
                self.assertEqual(client()._request(url).headers['x-robots-tag'], ', '.join(values))
                denied = any('noindex' in v for v in values)
                data, report = index.discover(SITE, urls=[url], client=client())
                self.assertEqual(bool(data['pages']), not denied)
                if denied:
                    self.assertEqual(report['excluded'][0]['reason'], 'noindex_or_password')
                text, _ = index.build({**self.data, 'pages': [{**self.data['pages'][0], 'url': url}]})
                checked = index.validate(text, SITE, online=True, client=client())
                self.assertEqual(checked['valid'], not denied)
                if denied:
                    self.assertEqual(checked['excluded'][0]['reason'], 'noindex_or_password')

    def test_limit_and_small_sites(self):
        text, report = index.build(self.data, max_pages=1)
        self.assertTrue(report["valid"])
        self.assertEqual(len(report["links"]), 1)
        self.assertEqual([p["reason"] for p in report["excluded"]], ["page_limit"] * 3)
        self.assertTrue(index.validate(text, SITE)["valid"])

    def test_later_complete_facts_clear_missing_record_even_after_page_limit(self):
        a = {**self.data['pages'][0], 'url': SITE + 'a', 'title': 'A', 'description': 'A 的完整资料。'}
        b = {**self.data['pages'][0], 'url': SITE + 'b', 'title': 'B', 'description': 'B 的完整资料。'}
        incomplete = {**a, 'description': ''}
        text, report = index.build({**self.data, 'pages': [incomplete, b, a]}, max_pages=1)
        self.assertTrue(report['valid'])
        self.assertEqual(report['output_status'], 'complete')
        self.assertEqual(report['unresolved'], [])
        self.assertEqual([r['url'] for r in report['links']], [b['url']])
        self.assertEqual({(r['index'], r['reason']) for r in report['excluded']},
                         {(0, 'duplicate'), (2, 'page_limit')})
        self.assertTrue(index.validate(text, SITE)['valid'])
        # A non-public record must not supply the missing public facts.
        _, denied = index.build({**self.data, 'pages': [incomplete, b, {**a, 'visibility': 'private'}]}, max_pages=1)
        self.assertFalse(denied['valid'])
        self.assertEqual(denied['unresolved'][0]['reason'], 'title_and_description_required')

    def test_validate_ordinary_list_markers_check_public_and_private_links(self):
        text, _ = index.build(self.data)
        for marker in ('*', '+', '1.', '2)', '  *'):
            with self.subTest(marker=marker):
                ordinary = text.replace('\n- ', '\n' + marker + ' ')
                report = index.validate(ordinary, SITE)
                self.assertTrue(report['valid'])
                self.assertEqual(len(report['links']), len(self.data['pages']))
                report = index.validate(text + '\n' + marker + ' [后台](' + SITE + 'admin): 后台。\n', SITE)
                self.assertFalse(report['valid'])
                self.assertEqual(report['excluded'][-1]['reason'], 'private_path')

    def test_unsupported_markdown_links_prevent_complete_validation(self):
        text, _ = index.build(self.data)
        for extra in ('另见[后台](' + SITE + 'admin)。', '[后台][private]', '<' + SITE + 'admin>'):
            with self.subTest(extra=extra):
                report = index.validate(text + '\n' + extra + '\n', SITE)
                self.assertFalse(report['valid'])
                self.assertEqual(report['unresolved'][-1]['reason'], 'unsupported_markdown_link')
        nested = '# 站点\n\n> 简介。\n\n## 页面\n\n- [首页](' + SITE + '): 另见[后台](' + SITE + 'admin)。\n'
        report = index.validate(nested, SITE)
        self.assertFalse(report['valid'])
        self.assertEqual(report['unresolved'][-1]['reason'], 'unsupported_markdown_link')

    def test_shortcut_reference_definitions_cannot_hide_unchecked_private_links(self):
        text, _ = index.build(self.data)
        for extra in ('另见[后台]。\n\n[后台]: ' + SITE + 'admin',
                      '另见[后台]。\n\n  [后台]: <' + SITE + 'admin> "说明"',
                      '[后台]: ' + SITE + 'admin',
                      '- [补充](' + SITE + 'guide): 另见[后台]。\n\n[后台]: ' + SITE + 'admin'):
            with self.subTest(extra=extra):
                report = index.validate(text + '\n' + extra + '\n', SITE)
                self.assertFalse(report['valid'])
                self.assertTrue(report['unresolved'])
                self.assertTrue(all(row['reason'] == 'unsupported_markdown_link'
                                    for row in report['unresolved']))
                self.assertGreaterEqual(len(report['links']), len(self.data['pages']))
        # Without a definition these brackets are plain text, not a shortcut link.
        for plain in ('资料状态为[待补]。', r'说明中的\[后台\]是普通文字。'):
            self.assertTrue(index.validate(text + '\n' + plain + '\n', SITE)['valid'])

    def test_http_autolink_scheme_case_cannot_bypass_unsupported_link_detection(self):
        text, _ = index.build(self.data)
        for scheme in ('HTTPS', 'HtTpS', 'HTTP', 'hTtP'):
            with self.subTest(scheme=scheme):
                autolink = '<' + scheme + '://example.com/admin>'
                for extra in ('另见 ' + autolink + '。',
                              '- [补充](' + SITE + 'guide): 另见' + autolink + '。'):
                    report = index.validate(text + '\n' + extra + '\n', SITE)
                    self.assertFalse(report['valid'])
                    self.assertEqual(report['unresolved'][-1]['reason'], 'unsupported_markdown_link')
                    self.assertTrue(all(row['url'] != SITE + 'admin' for row in report['links']))

    def test_extra_autolink_in_list_title_cannot_hide_an_unchecked_destination(self):
        for scheme in ('https', 'HTTPS', 'HtTpS', 'HTTP'):
            with self.subTest(scheme=scheme):
                text = ('# 站点\n\n> 简介。\n\n## 页面\n\n- [<' + scheme
                        + '://example.com/admin>](' + SITE + '): 首页。\n')
                client, transport = self.client({SITE: html_reply('首页')})
                report = index.validate(text, SITE, online=True, client=client)
                self.assertFalse(report['valid'])
                self.assertEqual(report['unresolved'], [{'line': 7, 'reason': 'unsupported_markdown_link'}])
                self.assertEqual([row['url'] for row in report['links']], [SITE])
                self.assertEqual(transport.calls, [SITE])
        # Brackets escaped by the normal builder are literal title characters.
        data = {**self.data, 'pages': [{**self.data['pages'][0], 'title': '[资料] 标题：版本1'}]}
        text, built = index.build(data)
        self.assertTrue(built['valid'])
        report = index.validate(text, SITE)
        self.assertTrue(report['valid'], report)
        self.assertEqual(report['unresolved'], [])

    def test_extra_links_use_backslash_parity_in_descriptions_and_prose(self):
        text, _ = index.build(self.data)
        for count in range(7):
            slashes = '\\' * count
            forms = (slashes + '[后台](' + SITE + 'admin)',
                     slashes + '<HtTpS://example.com/admin>',
                     slashes + '[后台][private]\n' + slashes + '[private]: ' + SITE + 'admin',
                     slashes + '[后台]\n' + slashes + '[后台]: ' + SITE + 'admin')
            for form in forms:
                with self.subTest(count=count, form=form):
                    for extra in ('另见 ' + form,
                                  '- [补充](' + SITE + 'guide): 另见 ' + form):
                        report = index.validate(text + '\n' + extra + '\n', SITE)
                        self.assertEqual(report['valid'], bool(count % 2), report)
                        self.assertEqual(bool(report['unresolved']), count % 2 == 0)
                        self.assertTrue(all(row['reason'] == 'unsupported_markdown_link'
                                            for row in report['unresolved']))

    def test_title_autolinks_and_label_closers_use_backslash_parity(self):
        text, _ = index.build(self.data)
        for count in range(7):
            slashes = '\\' * count
            with self.subTest(count=count):
                extra = '- [标题 ' + slashes + '<HTTPS://example.com/admin>](' + SITE + 'guide): 简介。'
                report = index.validate(text + '\n' + extra + '\n', SITE)
                self.assertEqual(report['valid'], bool(count % 2), report)
                self.assertEqual(bool(report['unresolved']), count % 2 == 0)
                # An escaped closing bracket does not create an inline link.
                extra = '另见 [后台' + slashes + '](' + SITE + 'admin)'
                report = index.validate(text + '\n' + extra + '\n', SITE)
                self.assertEqual(report['valid'], bool(count % 2), report)
        # An escaped outer bracket must not hide an unescaped inner link.
        report = index.validate(text + '\n' + r'另见 \[普通文字 [后台](https://example.com/admin)' + '\n', SITE)
        self.assertFalse(report['valid'])
        self.assertEqual(report['unresolved'][-1]['reason'], 'unsupported_markdown_link')

    def test_normal_builder_literal_backslashes_and_links_still_self_validate(self):
        for count in range(7):
            with self.subTest(count=count):
                literal = '\\' * count + '[说明](https://example.com/admin)'
                data = {**self.data, 'site': {**self.data['site'], 'overview': literal},
                        'pages': [{**self.data['pages'][0], 'title': literal, 'description': literal}]}
                text, report = index.build(data)
                self.assertTrue(report['valid'])
                checked = index.validate(text, SITE)
                self.assertTrue(checked['valid'], checked)
                self.assertEqual(checked['unresolved'], [])

    def test_actual_html_href_links_are_unresolved_in_all_index_text_positions(self):
        for markup in ('<a href="https://example.com/admin">后台</a>',
                       "<A HREF='https://example.com/admin'>后台</A>",
                       '<a href=https://example.com/admin>后台</a>',
                       '<area HREF="/admin">', '<link href=/admin>', '<a href>后台</a>'):
            for position in ('title', 'description', 'introduction', 'ordinary'):
                with self.subTest(markup=markup, position=position):
                    text = '# 站点\n\n> 简介。\n\n## 页面\n\n- [首页](' + SITE + '): 首页。\n'
                    if position == 'title':
                        text = text.replace('[首页]', '[' + markup + ']')
                    elif position == 'description':
                        text = text.replace(': 首页。', ': ' + markup)
                    elif position == 'introduction':
                        text = text.replace('> 简介。', '> ' + markup)
                    else:
                        text += '\n' + markup + '\n'
                    report = index.validate(text, SITE)
                    self.assertFalse(report['valid'])
                    self.assertTrue(any(r['reason'] == 'unsupported_html_link' for r in report['unresolved']))
        text, _ = index.build(self.data)
        report = index.validate(text + '\n<a\n HREF="' + SITE + 'admin">后台</a>\n', SITE)
        self.assertFalse(report['valid'])
        self.assertEqual(report['unresolved'][-1]['reason'], 'unsupported_html_link')

    def test_entity_and_backslash_escaped_html_sources_are_literal_not_active_links(self):
        text, _ = index.build(self.data)
        for literal in ('&lt;a href="https://example.com/admin"&gt;后台&lt;/a&gt;',
                        '&lt;AREA HREF=/admin&gt;', r'\<a href="https://example.com/admin">后台</a>',
                        '<!-- <a href=/admin>示例</a> -->', '<span title="href=/admin">普通文字</span>'):
            with self.subTest(literal=literal):
                report = index.validate(text + '\n' + literal + '\n', SITE)
                self.assertTrue(report['valid'], report)
                self.assertEqual(report['unresolved'], [])
        for count in (2, 3, 4, 5):
            report = index.validate(text + '\n' + '\\' * count + '<a href=/admin>后台</a>\n', SITE)
            self.assertEqual(report['valid'], bool(count % 2))

    def test_plain_text_html_links_are_escaped_by_build_and_self_validate(self):
        literal = '<A HREF="https://example.com/admin">后台</A>'
        data = {**self.data, 'site': {**self.data['site'], 'overview': literal,
                                     'name': literal, 'description': literal},
                'pages': [{**self.data['pages'][0], 'title': literal, 'description': literal, 'section': literal}]}
        text, built = index.build(data)
        self.assertTrue(built['valid'])
        self.assertIn('&lt;A HREF=', text)
        report = index.validate(text, SITE)
        self.assertTrue(report['valid'], report)
        self.assertEqual(report['unresolved'], [])

    def test_missing_facts_partial_and_failure_snapshot(self):
        data = json.loads((ROOT / "examples/failure/missing-content.json").read_text(encoding="utf-8"))
        text, report = index.build(data)
        self.assertFalse(report["valid"])
        self.assertEqual(report["output_status"], "partial")
        self.assertEqual([p["url"] for p in report["links"]], [SITE])
        self.assertEqual(report["unresolved"][0]["url"], SITE + "tutorials")
        self.assertEqual({p["reason"] for p in report["excluded"]},
                         {"outside_origin", "private_path", "not_published"})
        self.assertEqual(report["failed_links"], [])
        self.assertEqual(text, (ROOT / "examples/failure/llms.partial.txt").read_text(encoding="utf-8"))
        self.assertEqual(report, json.loads((ROOT / "examples/failure/链接校验报告.json").read_text(encoding="utf-8")))

    def test_no_placeholder_when_all_missing(self):
        self.data["pages"] = [{"url": SITE + "not-read"}]
        text, report = index.build(self.data)
        self.assertIsNone(text)
        self.assertFalse(report["valid"])
        self.assertEqual(report["output_status"], "not_written")
        self.assertEqual(len(report["unresolved"]), 1)
        self.data["site"].pop("description")
        with self.assertRaises(index.InputError):
            index.build(self.data)

    def test_markdown_and_url_escaping_round_trip(self):
        self.data["pages"] = [{"url": SITE + "guide(one)", "title": "[标题] *说明*", "description": "中文\n下一行 <script>内容</script>"}]
        text, report = index.build(self.data)
        self.assertTrue(report["valid"])
        self.assertIn("guide%28one%29", text)
        self.assertEqual(sum(line.startswith("- ") for line in text.splitlines()), 1)
        self.assertNotIn("<script>", text)
        self.assertTrue(index.validate(text, SITE)["valid"])

    def test_validate_duplicate_and_malformed_and_outside_origin(self):
        text, _ = index.build(self.data)
        extra = "\n- [重复](https://example.com/?utm_source=x): 同一主页。\n"
        self.assertFalse(index.validate(text + extra, SITE)["valid"])
        report = index.validate(text + "\n- [外站](https://evil-example.com/): 外站。\n", SITE)
        self.assertFalse(report["valid"])
        self.assertEqual(report["excluded"][0]["reason"], "outside_origin")
        for broken in ("## 列表\n- [标题](https://example.com/): 简介", "# 网站\n\n## 页面\n- [标题](https://example.com/)"):
            self.assertFalse(index.validate(broken, SITE)["valid"])

    def test_discovery_filters_before_requests(self):
        client, transport = self.client({SITE + "sitemap.xml": xml_reply("urlset", [SITE, SITE + "#x",
            "https://evil-example.com/", SITE + "admin", SITE + "faq?preview=1"]), SITE: html_reply("首页", "主页简介。")})
        data, report = index.discover(SITE, client=client)
        self.assertTrue(report["valid"])
        self.assertEqual(transport.calls, [SITE + "sitemap.xml", SITE])
        self.assertEqual(len(data["pages"]), 1)
        self.assertEqual(data["site"]["name"], "首页")
        self.assertEqual(report["online_check"], "simulated")
        self.assertEqual(data["pages"][0]["source"]["kind"], "simulated")

    def test_nested_sitemaps_and_no_cross_site_fetch(self):
        client, transport = self.client({
            SITE + "sitemap.xml": xml_reply("sitemapindex", [SITE + "pages.xml", "https://evil-example.com/map.xml"]),
            SITE + "pages.xml": xml_reply("urlset", [SITE + "guide"]), SITE + "guide": html_reply("指南", "读取到的中文说明。")})
        data, report = index.discover(SITE, client=client)
        self.assertEqual(transport.calls, [SITE + "sitemap.xml", SITE + "pages.xml", SITE + "guide"])
        self.assertEqual(len(data["pages"]), 1)
        self.assertNotIn("name", data["site"])
        self.assertEqual(report["excluded"][0]["reason"], "outside_origin")

    def test_malformed_discovery_urls_never_count_as_success(self):
        malformed = "https://example.com:bad/products"
        for source in ("urls", "urlset", "sitemapindex"):
            with self.subTest(source=source):
                routes = {SITE: html_reply()}
                if source == "urlset":
                    routes[SITE + "sitemap.xml"] = xml_reply("urlset", [SITE, malformed])
                elif source == "sitemapindex":
                    routes[SITE + "sitemap.xml"] = xml_reply("sitemapindex", [SITE + "pages.xml", malformed])
                    routes[SITE + "pages.xml"] = xml_reply("urlset", [SITE])
                client, transport = self.client(routes)
                data, report = index.discover(SITE, client=client,
                    **({"urls": [SITE, malformed]} if source == "urls" else {}))
                self.assertEqual([p["url"] for p in data["pages"]], [SITE])
                self.assertFalse(report["valid"])
                self.assertEqual(report["counts"]["unresolved"], 1)
                self.assertEqual(report["unresolved"][0]["reason"], "invalid_url")
                self.assertEqual(report["excluded"], [])
                self.assertEqual(report["failed_links"], [])
                self.assertNotIn(malformed, transport.calls)
                self.assertEqual(report["online_check"], "simulated")

    def test_missing_empty_and_whitespace_sitemap_loc_are_unresolved_with_good_page_kept(self):
        for kind in ('urlset', 'sitemapindex'):
            for loc in ('', '<loc/>', '<loc> \n </loc>'):
                with self.subTest(kind=kind, loc=loc):
                    item = 'url' if kind == 'urlset' else 'sitemap'
                    good = SITE if kind == 'urlset' else SITE + 'pages.xml'
                    body = f'<{kind}><{item}><loc>{good}</loc></{item}><{item}>{loc}</{item}></{kind}>'
                    routes = {SITE: html_reply(), SITE + 'sitemap.xml':
                              index.Reply(200, {'content-type': 'application/xml'}, body.encode())}
                    if kind == 'sitemapindex':
                        routes[SITE + 'pages.xml'] = xml_reply('urlset', [SITE])
                    client, transport = self.client(routes)
                    data, report = index.discover(SITE, client=client)
                    self.assertEqual([page['url'] for page in data['pages']], [SITE])
                    self.assertFalse(report['valid'])
                    self.assertEqual(report['unresolved'], [{'url': SITE + 'sitemap.xml',
                        'entry_index': 1, 'reason': 'sitemap_loc_required'}])
                    self.assertEqual(report['failed_links'], [])
                    self.assertEqual(transport.calls[-1], SITE)

    def test_malformed_redirect_is_not_an_allowed_exclusion(self):
        client, transport = self.client({SITE: html_reply(), SITE + "start": index.Reply(302,
            {"location": "https://example.com:bad/products"}, b"")})
        data, report = index.discover(SITE, urls=[SITE, SITE + "start"], client=client)
        self.assertEqual(len(data["pages"]), 1)
        self.assertFalse(report["valid"])
        self.assertEqual(report["excluded"], [])
        self.assertEqual(report["unresolved"][0]["reason"], "redirect_invalid_url")
        self.assertEqual(transport.calls, [SITE, SITE + "start"])

    def test_sitemap_cycle_and_request_bound(self):
        client, transport = self.client({SITE + "sitemap.xml": xml_reply("sitemapindex", [SITE + "sitemap.xml", SITE + "next.xml"]),
            SITE + "next.xml": xml_reply("urlset", [SITE + "a", SITE + "b", SITE + "c"]),
            SITE + "a": html_reply(), SITE + "b": html_reply(), SITE + "c": html_reply()})
        data, report = index.discover(SITE, client=client, max_pages=1, max_sitemaps=2)
        self.assertEqual(len(transport.calls), 3)
        self.assertEqual(len(data["pages"]), 1)
        self.assertIn("page_limit_reached_remaining_candidates_not_requested", report["warnings"])

    def test_candidate_and_sitemap_limits(self):
        client, transport = self.client({SITE + "sitemap.xml": xml_reply("urlset", [SITE + "a", SITE + "b", SITE + "c"]),
                                        SITE + "a": html_reply(), SITE + "b": html_reply()})
        data, report = index.discover(SITE, client=client, max_candidates=2)
        self.assertEqual(len(data["pages"]), 2)
        self.assertEqual(len(transport.calls), 3)
        self.assertIn("sitemap_entry_limit_reached", report["warnings"])
        client, transport = self.client({SITE + "sitemap.xml": xml_reply("sitemapindex", [SITE + "next.xml"])})
        data, report = index.discover(SITE, client=client, max_sitemaps=1)
        self.assertEqual(transport.calls, [SITE + "sitemap.xml"])
        self.assertFalse(report["valid"])
        self.assertIn("sitemap_or_candidate_limit_reached", report["warnings"])

    def test_cross_origin_private_redirect_never_followed(self):
        for target in ("https://evil-example.com/landing", "/admin/landing", "/faq?preview=true",
                       "/#/admin?tab=users"):
            with self.subTest(target=target):
                client, transport = self.client({SITE + "start": index.Reply(302, {"location": target}, b"")})
                reply, record = client.get(SITE + "start")
                self.assertIsNone(reply)
                self.assertEqual(record["status"], "excluded")
                self.assertEqual(len(transport.calls), 1)
                self.assertEqual(record["http_status"], 302)

    def test_redirect_dedup_and_loop(self):
        client, transport = self.client({SITE + "alias": index.Reply(301, {"location": "/guide"}, b""),
                                        SITE + "guide": html_reply()})
        data, report = index.discover(SITE, urls=[SITE + "alias", SITE + "guide"], client=client)
        self.assertEqual([p["url"] for p in data["pages"]], [SITE + "guide"])
        self.assertEqual(report["excluded"][0]["reason"], "duplicate_final_url")
        client, transport = self.client({SITE + "loop": index.Reply(302, {"location": "/loop"}, b"")})
        self.assertIsNone(client.get(SITE + "loop")[0])
        self.assertEqual(len(transport.calls), 1)

    def test_missing_content_not_inferred_from_url(self):
        client, transport = self.client({SITE + "products": html_reply("产品", "")})
        data, report = index.discover(SITE, urls=[SITE + "products"], client=client)
        self.assertEqual(data["pages"], [])
        self.assertFalse(report["valid"])
        self.assertEqual(report["unresolved"][0]["reason"], "title_and_description_required")
        self.assertEqual(report["failed_links"], [])

    def test_real_html_fallbacks_without_fake_text(self):
        reply = index.Reply(200, {"content-type": "text/html"},
                            '<nav><p>导航内容。</p></nav><h1>中文标题</h1><script>伪简介</script><p>第一段<b>中文</b>内容。</p>'.encode())
        self.assertEqual(index.read_html(reply).facts(), ("中文标题", "第一段中文内容。"))
        client, _ = self.client({SITE + "guide": reply})
        data, report = index.discover(SITE, urls=[SITE + "guide"], client=client)
        self.assertTrue(report["valid"])
        self.assertEqual(data["pages"][0]["description"], "第一段中文内容。")

    def test_svg_title_is_not_a_document_title_or_part_of_h1(self):
        for body, expected in (
                ('<title>产品介绍</title><svg><title>品牌图标</title></svg><h1>正文标题</h1>', '产品介绍'),
                ('<svg><title>品牌图标</title></svg><title>产品介绍</title><h1>正文标题</h1>', '产品介绍'),
                ('<svg><title>品牌图标</title></svg><h1>正确产品标题</h1>', '正确产品标题'),
                ('<h1><svg><title>品牌图标</title></svg>正确产品标题</h1>', '正确产品标题')):
            with self.subTest(body=body):
                reply = index.Reply(200, {'content-type': 'text/html; charset=utf-8'},
                                    (body + '<p>真实简介。</p>').encode())
                self.assertEqual(index.read_html(reply).facts(), (expected, '真实简介。'))
                client, _ = self.client({SITE: reply})
                data, report = index.discover(SITE, urls=[SITE], client=client)
                self.assertTrue(report['valid'])
                self.assertEqual(data['pages'][0]['title'], expected)
                self.assertEqual(data['site']['name'], expected)

    def test_svg_only_title_without_document_title_or_h1_is_missing_content(self):
        reply = index.Reply(200, {'content-type': 'text/html; charset=utf-8'},
                            '<svg><title>品牌图标</title></svg><p>真实简介。</p>'.encode())
        self.assertEqual(index.read_html(reply).facts(), ('', '真实简介。'))
        client, _ = self.client({SITE: reply})
        data, report = index.discover(SITE, urls=[SITE], client=client)
        self.assertEqual(data['pages'], [])
        self.assertFalse(report['valid'])
        self.assertEqual(report['unresolved'][0]['reason'], 'title_and_description_required')

    def test_omitted_p_endtag_at_parent_end_or_eof_keeps_real_description(self):
        for ending in ('</body>', '</section></body>', ''):
            with self.subTest(ending=ending):
                body = '<title>产品</title><body><section><p>真实<b>简介</b>。' + ending
                reply = index.Reply(200, {'content-type': 'text/html; charset=utf-8'}, body.encode())
                self.assertEqual(index.read_html(reply).facts(), ('产品', '真实简介。'))
                client, _ = self.client({SITE + 'products': reply})
                data, report = index.discover(SITE, urls=[SITE + 'products'], client=client)
                self.assertTrue(report['valid'])
                self.assertEqual(data['pages'][0]['description'], '真实简介。')

    def test_implicit_paragraph_boundaries_keep_paragraphs_separate(self):
        for middle in ('<p>', '<div>不属于首段。</div><p>', '<h2>下一节</h2><p>'):
            with self.subTest(middle=middle):
                reply = index.Reply(200, {'content-type': 'text/html; charset=utf-8'},
                    ('<title>产品</title><p>首段<b>内容</b>。' + middle + '后段。').encode())
                page = index.read_html(reply)
                self.assertEqual(page.paragraphs, ['首段内容。', '后段。'])
                self.assertEqual(page.facts(), ('产品', '首段内容。'))
        reply = index.Reply(200, {'content-type': 'text/html; charset=utf-8'},
            '<title>产品</title><p><br><p>第一行<br>第二行</body>'.encode())
        self.assertEqual(index.read_html(reply).facts(), ('产品', '第一行 第二行'))
        # A paragraph can end with its actual container, beyond common blocks.
        for opening, closing in (('<table><tr><td>', '</td><td>后续单元格。</td></tr></table>'),
                                 ('<a href="/guide">', '</a>链接外的后续文字。'),
                                 ('<dialog>', '</dialog>后续文字。')):
            with self.subTest(parent=opening):
                reply = index.Reply(200, {'content-type': 'text/html; charset=utf-8'},
                    ('<title>产品</title><body>' + opening + '<p>首段<b>内容</b>。' + closing + '</body>').encode())
                self.assertEqual(index.read_html(reply).facts(), ('产品', '首段内容。'))

    def test_valueless_html_attributes_do_not_abort_usable_pages(self):
        reply = index.Reply(200, {"content-type": "text/html"},
            '<title>资料页</title><meta name="robots" content><meta name="description" content>'
            '<input type><p>用户可读的正文简介。</p>'.encode())
        self.assertEqual(index.read_html(reply).facts(), ("资料页", "用户可读的正文简介。"))
        client, _ = self.client({SITE: html_reply("主页", "主页简介。"), SITE + "notes": reply,
                                SITE + "empty": index.Reply(200, {"content-type": "text/html"},
                                    b'<title>Empty</title><input type><meta name="robots" content>')})
        data, report = index.discover(SITE, urls=[SITE, SITE + "notes", SITE + "empty"], client=client)
        self.assertEqual([page["url"] for page in data["pages"]], [SITE, SITE + "notes"])
        self.assertFalse(report["valid"])
        self.assertEqual(report["unresolved"][0]["reason"], "title_and_description_required")

    def test_unknown_sitemap_encoding_keeps_other_sitemap_results(self):
        client, transport = self.client({
            SITE + "sitemap.xml": xml_reply("sitemapindex", [SITE + "good.xml", SITE + "bad.xml"]),
            SITE + "good.xml": xml_reply("urlset", [SITE, SITE + "guide"]),
            SITE + "bad.xml": index.Reply(200, {"content-type": "application/xml"},
                b'<?xml version="1.0" encoding="NOT-A-REAL-ENCODING"?><urlset/>'),
            SITE: html_reply("首页", "首页简介。"), SITE + "guide": html_reply("指南", "指南简介。")})
        data, report = index.discover(SITE, client=client)
        self.assertEqual([page["url"] for page in data["pages"]], [SITE, SITE + "guide"])
        self.assertFalse(report["valid"])
        self.assertEqual(report["unresolved"], [{"url": SITE + "bad.xml", "reason": "cannot_decode_sitemap"}])
        self.assertEqual(len(transport.calls), 5)

    def test_parser_assertion_is_one_unresolved_page_not_whole_task_failure(self):
        from html.parser import HTMLParser
        rejects = False
        try:
            HTMLParser().feed('<![bogus]>')
        except AssertionError:
            rejects = True
        client, transport = self.client({SITE: html_reply('首页', '可用首页简介。'),
            SITE + 'bad': html_reply('异常页面', '不可作为可用材料。', '<![bogus]>')})
        data, report = index.discover(SITE, urls=[SITE, SITE + 'bad'], client=client)
        self.assertEqual([page['url'] for page in data['pages']], [SITE] if rejects else [SITE, SITE + 'bad'])
        self.assertEqual(report['valid'], not rejects)
        self.assertEqual([{key: r[key] for key in ('url', 'reason')} for r in report['unresolved']],
                         [{'url': SITE + 'bad', 'reason': 'cannot_parse_html'}] if rejects else [])
        if rejects:
            record = report['unresolved'][0]
            self.assertEqual(record['status'], 'reachable')
            self.assertEqual(record['evidence_kind'], 'simulated')
            self.assertEqual(record['http_status'], 200)
            self.assertEqual(record['redirects'], [])
            self.assertEqual(record['final_url'], SITE + 'bad')
            self.assertTrue(record['checked_at'])
        self.assertEqual(transport.calls, [SITE, SITE + 'bad'])

    def test_forced_parser_assertion_preserves_http_metadata_and_healthy_page(self):
        original_feed = index.PageHTML.feed
        def rejecting_feed(parser, text):
            if '<![forced-test-assertion]>' in text:
                raise AssertionError('Controlled older HTMLParser branch')
            return original_feed(parser, text)
        client, transport = self.client({SITE: html_reply('首页', '可用首页简介。'),
            SITE + 'bad': html_reply('异常页', '不得冒充可用资料。', '<![forced-test-assertion]>')})
        with patch.object(index.PageHTML, 'feed', rejecting_feed):
            data, report = index.discover(SITE, urls=[SITE, SITE + 'bad'], client=client)
        self.assertEqual([p['url'] for p in data['pages']], [SITE])
        self.assertFalse(report['valid'])
        self.assertEqual(len(report['unresolved']), 1)
        record = report['unresolved'][0]
        self.assertEqual({k: record[k] for k in ('url', 'reason', 'status', 'evidence_kind', 'http_status', 'redirects', 'final_url')},
            {'url': SITE + 'bad', 'reason': 'cannot_parse_html', 'status': 'reachable',
             'evidence_kind': 'simulated', 'http_status': 200, 'redirects': [], 'final_url': SITE + 'bad'})
        self.assertTrue(record['checked_at'])
        self.assertEqual(transport.calls, [SITE, SITE + 'bad'])

    def test_unsupported_multibyte_sitemap_keeps_other_results(self):
        client, transport = self.client({
            SITE + "sitemap.xml": xml_reply("sitemapindex", [SITE + "good.xml", SITE + "bad.xml"]),
            SITE + "good.xml": xml_reply("urlset", [SITE, SITE + "guide"]),
            SITE + "bad.xml": index.Reply(200, {"content-type": "application/xml"},
                b'<?xml version="1.0" encoding="GBK"?><urlset/>'),
            SITE: html_reply("首页", "首页简介。"), SITE + "guide": html_reply("指南", "指南简介。")})
        data, report = index.discover(SITE, client=client)
        self.assertEqual([page["url"] for page in data["pages"]], [SITE, SITE + "guide"])
        self.assertFalse(report["valid"])
        self.assertEqual(report["unresolved"], [{"url": SITE + "bad.xml", "reason": "cannot_decode_sitemap"}])
        self.assertEqual(len(transport.calls), 5)

    def test_noindex_and_password_are_excluded(self):
        replies = [html_reply(extra='<input type="password">'), html_reply(extra='<meta name="robots" content="noindex">')]
        reply = html_reply()
        reply.headers["x-robots-tag"] = "noindex"
        replies.append(reply)
        for reply in replies:
            client, _ = self.client({SITE + "hidden": reply})
            data, report = index.discover(SITE, urls=[SITE + "hidden"], client=client)
            self.assertEqual(data["pages"], [])
            self.assertEqual(report["excluded"][0]["reason"], "noindex_or_password")
            self.assertEqual(report["failed_links"], [])

    def test_http_error_and_timeout_separate_and_simulated(self):
        client, transport = self.client({SITE + "ok": html_reply(), SITE + "missing": index.Reply(404, {}, b"no"),
                                        SITE + "timeout": TimeoutError("fixture timeout")})
        data, report = index.discover(SITE, urls=[SITE + "ok", SITE + "missing", SITE + "timeout"], client=client)
        self.assertEqual(len(data["pages"]), 1)
        self.assertFalse(report["valid"])
        self.assertEqual([r["status"] for r in report["failed_links"]], ["http_error", "request_failed"])
        self.assertEqual([r["http_status"] for r in report["failed_links"]], [404, None])
        self.assertTrue(all(r["evidence_kind"] == "simulated" for r in report["failed_links"]))
        self.assertEqual(report["online_check"], "simulated")

    def test_empty_candidate_list_not_run(self):
        client, transport = self.client({})
        data, report = index.discover(SITE, urls=["https://evil-example.com/", "/admin"], client=client)
        self.assertEqual(transport.calls, [])
        self.assertEqual(report["online_check"], "not_run")
        self.assertEqual(report["failed_links"], [])
        self.assertFalse(report["valid"])

    def test_non_html_large_responses_and_invalid_xml(self):
        client, _ = self.client({SITE + "file": index.Reply(200, {"content-type": "application/pdf"}, b"pdf")})
        data, report = index.discover(SITE, urls=[SITE + "file"], client=client)
        self.assertFalse(report["valid"])
        self.assertEqual(report["unresolved"][0]["reason"], "html_content_type_required")
        client, _ = self.client({SITE + "large": index.Reply(200, {}, b"x" * (index.MAX_BYTES + 1))})
        reply, record = client.get(SITE + "large")
        self.assertIsNone(reply)
        self.assertEqual(record["reason"], "response_too_large")
        for body in (b"not xml", b'<html/>', b'<!DOCTYPE urlset [<!ENTITY x "foo">]><urlset/>'):
            client, _ = self.client({SITE + "sitemap.xml": index.Reply(200, {}, body)})
            data, report = index.discover(SITE, client=client)
            self.assertFalse(report["valid"])
            self.assertEqual(len(report["unresolved"]), 1)

    def test_online_validation_failures_change_result(self):
        text, _ = index.build(self.data)
        routes = {p["url"]: html_reply() for p in self.data["pages"]}
        routes[SITE + "faq"] = index.Reply(503, {}, b"")
        client, transport = self.client(routes)
        report = index.validate(text, SITE, online=True, client=client)
        self.assertFalse(report["valid"])
        self.assertEqual(report["online_check"], "simulated")
        self.assertEqual(len(transport.calls), 4)
        self.assertEqual(report["failed_links"][0]["http_status"], 503)
        self.assertEqual(sum(r["status"] == "reachable" for r in report["links"]), 3)

    def test_online_validation_noindex_changes_result(self):
        text, _ = index.build({**self.data, "pages": self.data["pages"][:1]})
        client, _ = self.client({SITE: html_reply(extra='<meta name="robots" content="noindex">')})
        report = index.validate(text, SITE, online=True, client=client)
        self.assertFalse(report["valid"])
        self.assertEqual(report["excluded"][0]["reason"], "noindex_or_password")

    def test_non_html_noindex_header_is_excluded_but_public_pdf_reachable(self):
        url = SITE + "manual.pdf"
        text, _ = index.build({**self.data, "pages": [{**self.data["pages"][0], "url": url}]})
        for directive in ("noindex", "googlebot: noindex, nofollow", None):
            with self.subTest(directive=directive):
                headers = {"content-type": "application/pdf"}
                if directive is not None:
                    headers["x-robots-tag"] = directive
                client, _ = self.client({url: index.Reply(200, headers, b"%PDF-1.4 fixture")})
                report = index.validate(text, SITE, online=True, client=client)
                self.assertEqual(report["valid"], directive is None)
                self.assertEqual(report["links"][0]["status"], "reachable" if directive is None else "excluded")
                if directive is not None:
                    self.assertEqual(report["excluded"][0]["reason"], "noindex_or_password")

    def test_online_validation_deduplicates_redirect_destinations(self):
        self.data["pages"] = [{**self.data["pages"][0], "url": SITE + "alias"},
                              {**self.data["pages"][0], "url": SITE + "guide"}]
        text, _ = index.build(self.data)
        client, _ = self.client({SITE + "alias": index.Reply(302, {"location": "/guide"}, b""),
                                 SITE + "guide": html_reply()})
        report = index.validate(text, SITE, online=True, client=client)
        self.assertFalse(report["valid"])
        self.assertEqual(report["excluded"][0]["reason"], "duplicate_final_url")

    def test_candidate_omission_across_sitemaps_reported(self):
        client, transport = self.client({
            SITE + "sitemap.xml": xml_reply("sitemapindex", [SITE + "one.xml", SITE + "two.xml"]),
            SITE + "one.xml": xml_reply("urlset", [SITE + "a"]),
            SITE + "two.xml": xml_reply("urlset", [SITE + "b", SITE + "c"]),
            SITE + "a": html_reply(), SITE + "b": html_reply()})
        data, report = index.discover(SITE, client=client, max_candidates=2)
        self.assertEqual(len(data["pages"]), 2)
        self.assertIn("candidate_limit_reached", report["warnings"])
        self.assertNotIn(SITE + "c", transport.calls)

    def test_incomplete_http_read_is_recorded_as_failure(self):
        client, _ = self.client({SITE + "cut": index.http.client.IncompleteRead(b"partial", 100)})
        data, report = index.discover(SITE, urls=[SITE + "cut"], client=client)
        self.assertFalse(report["valid"])
        self.assertEqual(data["pages"], [])
        self.assertEqual(report["failed_links"][0]["reason"], "IncompleteRead")
        self.assertEqual(report["online_check"], "simulated")

    def cli(self, *arguments, cwd=None):
        return subprocess.run([sys.executable, "-S", "-B", str(ROOT / "scripts/llms_index.py"), *map(str, arguments)],
                              cwd=cwd or ROOT, input="", text=True, capture_output=True, timeout=30)

    def test_cli_success_and_partial_exit_codes(self):
        with tempfile.TemporaryDirectory(prefix="llms-self-test-") as folder:
            out, report = Path(folder) / "llms.txt", Path(folder) / "report.json"
            result = self.cli("build", "--input", ROOT / "examples/four-pages.json", "--output", out, "--report", report)
            self.assertEqual(result.returncode, 0, result.stderr)
            parsed = json.loads(report.read_text())
            self.assertEqual(parsed["counts"]["links"], 4)
            self.assertEqual(out.read_text(), (ROOT / "examples/success/llms.txt").read_text())
            result = self.cli("build", "--input", ROOT / "examples/failure/missing-content.json", "--output", out, "--report", report)
            self.assertEqual(result.returncode, 1, result.stderr)
            self.assertEqual(json.loads(report.read_text())["output_status"], "partial")
            self.assertEqual(out.read_text(), (ROOT / "examples/failure/llms.partial.txt").read_text())

    def test_cli_bad_json_bad_option_and_collision_no_prompt(self):
        with tempfile.TemporaryDirectory(prefix="llms-self-test-") as folder:
            source, out, report = (Path(folder) / name for name in ("input.json", "llms.txt", "report.json"))
            source.write_text("{broken", encoding="utf-8")
            result = self.cli("build", "--input", source, "--output", out, "--report", report)
            self.assertEqual(result.returncode, 2)
            self.assertFalse(json.loads(result.stderr)["valid"])
            self.assertFalse(out.exists())
            result = self.cli("build", "--input", source, "--output", out, "--report", report, "--max-pages", "0")
            self.assertEqual(result.returncode, 2)
            original = source.read_text()
            result = self.cli("build", "--input", source, "--output", source, "--report", report)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(source.read_text(), original)

    def test_cli_malformed_url_produces_partial_report_and_exit_one(self):
        with tempfile.TemporaryDirectory(prefix="llms-self-test-") as folder:
            source, out, report = (Path(folder) / name for name in ("input.json", "llms.txt", "report.json"))
            data = {**self.data, "pages": [self.data["pages"][0], {"url": "https://example.com:bad/products",
                    "title": "产品", "description": "产品简介。"}]}
            source.write_text(json.dumps(data), encoding="utf-8")
            result = self.cli("build", "--input", source, "--output", out, "--report", report)
            self.assertEqual(result.returncode, 1, result.stderr)
            saved = json.loads(report.read_text())
            self.assertFalse(saved["valid"])
            self.assertEqual(saved["output_status"], "partial")
            self.assertEqual(saved["counts"]["unresolved"], 1)
            self.assertEqual(out.read_text(), index.build({**self.data, "pages": self.data["pages"][:1]})[0])

    def test_cli_output_directory_replaces_stale_success_report_with_failure(self):
        with tempfile.TemporaryDirectory(prefix="llms-self-test-") as folder:
            out, report = Path(folder) / "llms.txt", Path(folder) / "report.json"
            result = self.cli("build", "--input", ROOT / "examples/four-pages.json", "--output", out, "--report", report)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(report.read_text())["valid"])
            out.unlink()
            out.mkdir()
            result = self.cli("build", "--input", ROOT / "examples/four-pages.json", "--output", out, "--report", report)
            self.assertEqual(result.returncode, 2)
            saved = json.loads(report.read_text())
            self.assertFalse(saved["valid"])
            self.assertEqual(saved["output_status"], "not_written")
            self.assertEqual(saved["errors"][-1]["reason"], "output_write_failed")
            self.assertEqual(saved["counts"]["errors"], 1)
            self.assertFalse(json.loads(result.stderr)["valid"])
            self.assertEqual(list(out.iterdir()), [])
            self.assertEqual({p.name for p in Path(folder).iterdir()}, {"llms.txt", "report.json"})

    def test_atomic_output_failure_preserves_old_index_and_saves_failure_report(self):
        with tempfile.TemporaryDirectory(prefix="llms-self-test-") as folder:
            out, report = Path(folder) / "llms.txt", Path(folder) / "report.json"
            out.write_text("旧索引内容。", encoding="utf-8")
            index.write_json(report, {"valid": True, "output_status": "complete"})
            original_replace = Path.replace

            def deny_output(source, target):
                if Path(target) == out:
                    raise PermissionError("simulated output write denial")
                return original_replace(source, target)

            stderr = io.StringIO()
            with patch.object(Path, "replace", deny_output), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(stderr):
                code = index.main(["build", "--input", str(ROOT / "examples/four-pages.json"),
                                   "--output", str(out), "--report", str(report)])
            self.assertEqual(code, 2)
            self.assertEqual(out.read_text(), "旧索引内容。")
            saved = json.loads(report.read_text())
            self.assertFalse(saved["valid"])
            self.assertEqual(saved["output_status"], "not_written")
            self.assertEqual(saved["errors"][-1]["reason"], "output_write_failed")
            self.assertFalse(json.loads(stderr.getvalue())["valid"])
            self.assertEqual({p.name for p in Path(folder).iterdir()}, {"llms.txt", "report.json"})

    def test_portable_without_host_license_icon_or_siblings(self):
        with tempfile.TemporaryDirectory(prefix="llms-portable-") as folder:
            folder = Path(folder)
            shutil.copyfile(ROOT / "scripts/llms_index.py", folder / "llms_index.py")
            (folder / "pages.json").write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
            result = subprocess.run([sys.executable, "-S", "-B", str(folder / "llms_index.py"), "build", "--input", "pages.json",
                                     "--output", "llms.txt", "--report", "report.json"],
                                    cwd=folder, input="", text=True, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual((folder / "llms.txt").read_text(), index.build(self.data)[0])
            self.assertEqual(json.loads((folder / "report.json").read_text())["online_check"], "not_run")


if __name__ == "__main__":
    print("Business self-test: offline records and in-memory simulated HTTP; no public-site or AI checks.", flush=True)
    unittest.main(verbosity=2)
