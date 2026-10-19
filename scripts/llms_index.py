#!/usr/bin/env python3
"""Deterministic llms.txt assembly, bounded public-page discovery and checking.

Python 3.10+, standard library only. Text is supplied by the user/Agent or read
from HTML; this module does not invent descriptions or call a writing model.
"""

from __future__ import annotations

import argparse
import html
import http.client
import json
import posixpath
import re
import sys
import tempfile
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qsl, quote, unquote, urljoin, urlsplit, urlunsplit


MAX_BYTES = 2 * 1024 * 1024
PRIVATE_SEGMENTS = {
    "admin", "administrator", "wp-admin", "private", "internal", "draft", "drafts",
    "preview", "login", "signin", "sign-in", "logout", "account", "accounts",
    "dashboard", "后台", "草稿",
}
SECRET_KEYS = {"token", "access_token", "password", "auth", "session", "sessionid", "signature"}
TRACKERS = {"gclid", "fbclid", "msclkid"}
LINK = re.compile(r"^- \[((?:\\.|[^\]\\])+)\]\(([^()\s]+)\):\s*(.+)$")
LIST_ITEM = re.compile(r"^[ \t]*(?:[-+*]|[0-9]+[.)])[ \t]+")
# Start at the beginning of a backslash run and consume complete pairs: only
# an even run leaves a Markdown opener unescaped. Escaped label characters
# are also consumed in pairs, so a closing bracket uses the same parity rule.
# Reference definitions and case-insensitive HTTP autolinks remain unsupported.
OTHER_LINK = re.compile(
    r"(?<!\\)(?:\\\\)*(?:\[(?:\\.|[^\]\\\n])*\]\s*(?:\(|\[|:)|<https?://)", re.IGNORECASE)
ENTITY = re.compile(r"&(?:#[xX][0-9A-Fa-f]+|#[0-9]+|[A-Za-z][A-Za-z0-9]+);")


class InputError(ValueError):
    pass


class HTMLHrefLinks(HTMLParser):
    """Recognize actual HTML href attributes, without requesting their URLs."""

    def __init__(self, text):
        super().__init__(convert_charrefs=True)
        self.source_lines = text.splitlines()
        self.link_lines = set()

    def handle_starttag(self, tag, attrs):
        if not any(name == "href" for name, _ in attrs):
            return
        line, column = self.getpos()
        prefix = self.source_lines[line - 1][:column]
        # Markdown \<tag> is literal; complete backslash pairs leave it active.
        if (len(prefix) - len(prefix.rstrip("\\"))) % 2 == 0:
            self.link_lines.add(line)


def compact(value):
    return " ".join(value.split()) if isinstance(value, str) else ""


def decoded(value):
    for _ in range(4):
        result = unquote(value)
        if result == value:
            break
        value = result
    return value


def functional_query(query):
    # Decode keys only to identify trackers. Re-encoding values could corrupt
    # non-UTF8 escapes or change +/%20, bare flags, repeated keys and order.
    kept = []
    for component in query.split("&"):
        key = unquote(component.partition("=")[0].replace("+", " ")).casefold()
        if not key.startswith("utm_") and key not in TRACKERS:
            kept.append(component)
    return "&".join(kept)


def hash_route(fragment):
    """Only explicit / and !/ routers identify pages; ordinary anchors do not."""
    fragment = fragment.split("#", 1)[0]
    # An already visible query is opaque even when the route path still has
    # an encoded / prefix. Decoding that query would turn value bytes such as
    # %26visibility into a new parameter, or corrupt non-UTF8 percent bytes.
    fragment, query_separator, supplied_query = fragment.partition("?")
    # Decode an encoded route wrapper only until its prefix is recognizable.
    # Query escapes belong to their values: %23 and %26 are not delimiters.
    for _ in range(4):
        if fragment.startswith(("/", "!/")):
            break
        value = unquote(fragment)
        if value == fragment:
            break
        fragment = value
    prefix = "!" if fragment.startswith("!/") else ""
    route = fragment[len(prefix):]
    if not route.startswith("/"):
        return None
    path, _, query = route.partition("?")
    if query_separator:
        query = supplied_query
    return prefix, path, query


def canonical(raw, base=None):
    if not isinstance(raw, str) or not raw.strip():
        raise InputError("empty_url")
    raw = raw.strip()
    if re.search(r"[\x00-\x20\x7f\\]", raw):
        raise InputError("invalid_url_characters")
    parsed = urlsplit(urljoin(base, raw) if base else raw)
    if parsed.scheme.lower() not in {"https", "http"} or not parsed.hostname:
        raise InputError("http_url_required")
    if parsed.username is not None or parsed.password is not None:
        raise InputError("credentials_in_url")
    try:
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
        if port is not None and port == 0:
            raise ValueError("zero port is not a public HTTP endpoint")
    except (UnicodeError, ValueError) as exc:
        raise InputError("invalid_host_or_port") from exc
    if ":" in hostname:
        hostname = "[" + hostname + "]"
    default = 443 if parsed.scheme.lower() == "https" else 80
    netloc = hostname + (":" + str(port) if port is not None and port != default else "")
    path = quote(parsed.path or "/", safe="/%:@!$&'*,;=+-._~")
    # Remove only known tracking parameters. Keep functional query parameters.
    # Preserve existing percent bytes, separators, ordering and bare flags;
    # encode Unicode for HTTP and parentheses for unambiguous Markdown targets.
    query = quote(functional_query(parsed.query), safe="/%?:@!$&'*,;=+-._~")
    route = hash_route(parsed.fragment)
    fragment = ""
    if route:
        prefix, route_path, route_query = route
        fragment = prefix + quote(route_path, safe="/%:@!$&'*,;=+-._~")
        route_query = quote(functional_query(route_query), safe="/%?:@!$&'*,;=+-._~")
        if route_query:
            fragment += "?" + route_query
    return urlunsplit((parsed.scheme.lower(), netloc, path, query, fragment))


def origin(url):
    parsed = urlsplit(url)
    return parsed.scheme, parsed.hostname, (parsed.port if parsed.port is not None else
                                           (443 if parsed.scheme == "https" else 80))


class Scope:
    def __init__(self, site, excludes=()):
        self.site = canonical(site)
        self.origin = origin(self.site)
        self.excludes = []
        for path in excludes:
            if not path.startswith("/") or "?" in path or "#" in path:
                raise InputError("--exclude requires a path prefix, e.g. /members")
            self.excludes.append(decoded(path).casefold().rstrip("/") or "/")

    def select(self, raw, metadata=None, base=None):
        metadata = metadata or {}
        try:
            url = canonical(raw, base or self.site)
        except (InputError, ValueError):
            return None, "invalid_url"
        if origin(url) != self.origin:
            return url, "outside_origin"
        if metadata.get("visibility", "public") != "public":
            return url, "not_public"
        if metadata.get("status", "published") != "published":
            return url, "not_published"
        parsed = urlsplit(url)
        path = posixpath.normpath(decoded(parsed.path).replace("\\", "/")).casefold()
        segments = {part.split(";", 1)[0] for part in path.split("/")}
        route = hash_route(parsed.fragment)
        fragment_path = (posixpath.normpath(decoded(route[1]).replace("\\", "/")).casefold()
                         if route else "")
        fragment_segments = {part.split(";", 1)[0] for part in fragment_path.split("/")}
        if segments & PRIVATE_SEGMENTS or fragment_segments & PRIVATE_SEGMENTS:
            return url, "private_path"
        if any(prefix == "/" or any(candidate == prefix or candidate.startswith(prefix + "/")
                                   for candidate in (path, fragment_path)) for prefix in self.excludes):
            return url, "excluded_prefix"
        queries = [parsed.query] + ([route[2]] if route else [])
        for key, value in (pair for query in queries for pair in parse_qsl(query, keep_blank_values=True)):
            key, value = decoded(key).casefold(), decoded(value).casefold()
            if key in SECRET_KEYS or key in {"draft", "preview"} or (
                key in {"status", "visibility", "access"} and value in {"draft", "private", "internal"}
            ):
                return url, "private_query"
        return url, None


def new_report(operation, scope, evidence="local"):
    return {
        "schema_version": 1, "operation": operation, "evidence_kind": evidence,
        "site": scope.site, "valid": False, "online_check": "not_run",
        "links": [], "excluded": [], "unresolved": [], "failed_links": [],
        "errors": [], "warnings": [], "limits": {},
    }


def finish(report):
    report["counts"] = {key: len(report[key]) for key in
                        ("links", "excluded", "unresolved", "failed_links", "errors")}
    return report


def md_text(value):
    value = html.escape(compact(value), quote=False)
    value = re.sub(r"([\\\[\]*_`])", r"\\\1", value)
    # Supplied facts are plain text, including an overview on its own line.
    # Preserve leading heading/list characters literally instead of creating
    # extra document structure that the generated index cannot validate.
    value = re.sub(r"^([#+-])", r"\\\1", value)
    return re.sub(r"^(\d+)([.)])(?=\s|$)", r"\1\\\2", value)


def md_url(value):
    # An entity-looking sequence in a literal URL must survive Markdown's
    # single entity-decoding pass. Ordinary query separators stay unchanged.
    return ENTITY.sub(lambda match: "&amp;" + match.group()[1:], value)


def assemble(site, pages):
    """Serialize supplied facts, preserving Chinese text without word truncation."""
    lines = ["# " + md_text(site["name"]), "", "> " + md_text(site["description"]), ""]
    if compact(site.get("overview")):
        lines += [md_text(site["overview"]), ""]
    sections = {}
    for page in pages:
        sections.setdefault(page["section"], []).append(page)
    for section, items in sections.items():
        lines += ["## " + md_text(section), ""]
        lines += [f"- [{md_text(p['title'])}]({md_url(p['url'])}): {md_text(p['description'])}" for p in items]
        lines.append("")
    return "\n".join(lines)


def build(data, max_pages=20, excludes=()):
    if not isinstance(data, dict) or not isinstance(data.get("site"), dict):
        raise InputError("input requires a site object")
    site = data["site"]
    if not all(compact(site.get(k)) for k in ("url", "name", "description")):
        raise InputError("site.url, site.name and site.description are required")
    pages = data.get("pages")
    if not isinstance(pages, list) or len(pages) > 5000:
        raise InputError("pages must be a list of at most 5000 records")
    scope = Scope(site["url"], excludes)
    report = new_report("build", scope)
    report["limits"] = {"max_pages": max_pages}
    chosen, seen, complete_urls = [], set(), set()
    for index, page in enumerate(pages):
        if not isinstance(page, dict):
            report["unresolved"].append({"index": index, "reason": "page_object_required"})
            continue
        url, reason = scope.select(page.get("url"), page)
        record = {"index": index, "url": url}
        if reason:
            target = "unresolved" if reason == "invalid_url" else "excluded"
            report[target].append({**record, "reason": reason})
            continue
        if url in seen:
            report["excluded"].append({**record, "reason": "duplicate"})
            continue
        has_facts = all(compact(page.get(k)) for k in ("title", "description"))
        if has_facts:
            complete_urls.add(url)
        if len(chosen) >= max_pages:
            report["excluded"].append({**record, "reason": "page_limit"})
            continue
        if not has_facts:
            report["unresolved"].append({**record, "reason": "title_and_description_required"})
            continue
        seen.add(url)
        chosen.append({"url": url, "title": page["title"], "description": page["description"],
                       "section": compact(page.get("section")) or "页面"})
        report["links"].append({"url": url, "status": "not_checked", "local_valid": True,
                                "content_source": page.get("source", "provided_text")})
        if len(compact(page["description"])) > 160:
            report["warnings"].append({**record, "reason": "long_description_preserved"})
    # A later complete record can supply the facts missing from an earlier one.
    unresolved = []
    for record in report["unresolved"]:
        if record["reason"] == "title_and_description_required" and record["url"] in complete_urls:
            report["excluded"].append({**record, "reason": "duplicate"})
        else:
            unresolved.append(record)
    report["unresolved"] = unresolved
    if not chosen:
        report["errors"].append("no_public_pages_with_content")
    report["valid"] = bool(chosen) and not report["unresolved"]
    report["output_status"] = "complete" if report["valid"] else ("partial" if chosen else "not_written")
    return assemble(site, chosen) if chosen else None, finish(report)


class PageHTML(HTMLParser):
    # HTML permits omitting </p> before these block starts and parent ends.
    P_BOUNDARIES = {"address", "article", "aside", "blockquote", "details", "div", "dl",
                    "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3",
                    "h4", "h5", "h6", "header", "hgroup", "hr", "main", "menu", "nav", "ol",
                    "p", "pre", "search", "section", "table", "ul", "li", "dt", "dd"}
    VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
                 "meta", "param", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title, self.h1, self.paragraphs = [], [], []
        self.meta_description = self.og_description = ""
        self.in_title = self.in_h1 = self.in_p = False
        self.skip = 0
        self.current_p = []
        self.private = False
        self.open_tags = []
        self.p_parent = None

    def finish_paragraph(self):
        if self.in_p:
            text = compact("".join(self.current_p))
            if text:
                self.paragraphs.append(text)
            # An implicit p end also closes its still-open inline children.
            if "p" in self.open_tags:
                position = len(self.open_tags) - 1 - self.open_tags[::-1].index("p")
                del self.open_tags[position:]
        self.in_p = False
        self.current_p = []
        self.p_parent = None

    def close(self):
        super().close()
        self.finish_paragraph()

    def handle_starttag(self, tag, attrs):
        if tag in self.P_BOUNDARIES:
            self.finish_paragraph()
        # HTMLParser represents valueless/boolean attributes as None.
        attrs = {key: value or "" for key, value in attrs}
        if tag in {"script", "style", "nav", "header", "footer"}:
            self.skip += 1
        if tag == "title" and "svg" not in self.open_tags:
            self.in_title = True
        if tag == "h1" and not self.h1:
            self.in_h1 = True
        if tag == "p":
            self.in_p = True
            self.current_p = []
            self.p_parent = self.open_tags[-1] if self.open_tags else None
        if tag == "br" and self.in_p and not self.skip:
            self.current_p.append(" ")
        if tag == "meta":
            kind = (attrs.get("name") or attrs.get("property") or "").casefold()
            content = attrs.get("content", "")
            if kind == "description" and not self.meta_description:
                self.meta_description = content
            if kind == "og:description" and not self.og_description:
                self.og_description = content
            if kind in {"robots", "googlebot"} and "noindex" in content.casefold():
                self.private = True
        if tag == "input" and attrs.get("type", "").casefold() == "password":
            self.private = True
        if tag not in self.VOID_TAGS:
            self.open_tags.append(tag)

    def handle_endtag(self, tag):
        if tag in self.P_BOUNDARIES or tag in {"body", "html"} or (self.in_p and tag == self.p_parent):
            self.finish_paragraph()
        if tag in self.open_tags:
            position = len(self.open_tags) - 1 - self.open_tags[::-1].index(tag)
            del self.open_tags[position:]
        if tag in {"script", "style", "nav", "header", "footer"}:
            self.skip = max(0, self.skip - 1)
        if tag == "title":
            self.in_title = False
        if tag == "h1":
            self.in_h1 = False

    def handle_data(self, data):
        if self.skip or ("svg" in self.open_tags and "title" in self.open_tags):
            return
        if self.in_title:
            self.title.append(data)
        if self.in_h1:
            self.h1.append(data)
        if self.in_p:
            self.current_p.append(data)

    def facts(self):
        return (compact("".join(self.title)) or compact("".join(self.h1)),
                compact(self.meta_description) or compact(self.og_description) or
                (self.paragraphs[0] if self.paragraphs else ""))


@dataclass
class Reply:
    status: int
    headers: dict
    body: bytes


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class PublicClient:
    """No cookies, auth or environment proxy. Check every redirect before sending."""
    def __init__(self, scope, timeout=8, transport=None):
        self.scope, self.timeout, self.transport = scope, timeout, transport
        self.evidence = "simulated" if transport else "online"
        self.attempts = 0
        self.opener = None

    def _request(self, url):
        if self.transport:
            return self.transport(url)
        if self.opener is None:
            self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        request = urllib.request.Request(url, headers={"User-Agent": "aimegeo-llms-txt/1.0.0",
                                                       "Accept": "text/html,application/xml,text/xml,*/*;q=0.1"})
        try:
            response = self.opener.open(request, timeout=self.timeout)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            body = response.read(MAX_BYTES + 1)
            headers = {k.lower(): v for k, v in response.headers.items()}
            # X-Robots-Tag is repeatable and field names are case-insensitive.
            # A later nofollow field must not erase an earlier noindex.
            robots = response.headers.get_all("X-Robots-Tag", [])
            if robots:
                headers["x-robots-tag"] = ", ".join(robots)
            return Reply(response.code, headers, body)

    def get(self, raw):
        url, reason = self.scope.select(raw)
        status = "unresolved" if reason == "invalid_url" else ("excluded" if reason else "request_failed")
        record = {"url": url, "status": status,
                  "evidence_kind": self.evidence, "http_status": None, "redirects": []}
        if reason:
            return None, {**record, "reason": reason}
        if hash_route(urlsplit(url).fragment):
            # Fragments are never sent to HTTP servers. A homepage response
            # cannot establish the content or reachability of a client route.
            return None, {**record, "status": "unresolved", "reason": "hash_route_requires_supplied_content"}
        current, visited = url, set()
        for _ in range(6):
            if current in visited:
                return None, {**record, "reason": "redirect_loop"}
            visited.add(current)
            self.attempts += 1
            record["checked_at"] = datetime.now(timezone.utc).isoformat()
            try:
                reply = self._request(current)
            except (OSError, ValueError, urllib.error.URLError, http.client.HTTPException) as error:
                return None, {**record, "reason": type(error).__name__, "final_url": current}
            record.update(http_status=reply.status, final_url=current)
            if 300 <= reply.status < 400 and reply.status != 304:
                target = reply.headers.get("location")
                if not target:
                    return None, {**record, "reason": "redirect_without_location"}
                target, reason = self.scope.select(target, base=current)
                record["redirects"].append({"from": current, "to": target, "http_status": reply.status})
                if reason:
                    return None, {**record, "status": "unresolved" if reason == "invalid_url" else "excluded",
                                  "reason": "redirect_" + reason}
                if hash_route(urlsplit(target).fragment):
                    return None, {**record, "status": "unresolved", "final_url": target,
                                  "reason": "hash_route_requires_supplied_content"}
                current = target
                continue
            if not 200 <= reply.status < 300:
                return None, {**record, "status": "http_error", "reason": "http_status"}
            if "noindex" in reply.headers.get("x-robots-tag", "").casefold():
                return None, {**record, "status": "excluded", "reason": "noindex_or_password"}
            if len(reply.body) > MAX_BYTES:
                return None, {**record, "reason": "response_too_large"}
            return reply, {**record, "status": "reachable"}
        return None, {**record, "reason": "too_many_redirects"}


def read_html(reply):
    content_type = reply.headers.get("content-type", "")
    if "html" not in content_type.casefold():
        raise InputError("html_content_type_required")
    charset = re.search(r"charset\s*=\s*[\"']?([\w-]+)", content_type, re.I)
    try:
        text = reply.body.decode(charset.group(1) if charset else "utf-8")
    except (UnicodeError, LookupError) as exc:
        raise InputError("cannot_decode_html") from exc
    page = PageHTML()
    try:
        page.feed(text)
        page.close()
    except AssertionError as exc:
        raise InputError("cannot_parse_html") from exc
    if "noindex" in reply.headers.get("x-robots-tag", "").casefold():
        page.private = True
    return page


def record_fetch(report, record, sitemap=False):
    target = "sitemaps" if sitemap and record["status"] == "reachable" else (
        "unresolved" if record["status"] == "unresolved" else (
            "excluded" if record["status"] == "excluded" else "failed_links"))
    report.setdefault(target, []).append(record)


def discover(site, *, urls=None, sitemap=None, max_pages=20, max_sitemaps=5,
             max_candidates=200, timeout=8, excludes=(), client=None):
    scope = Scope(site, excludes)
    client = client or PublicClient(scope, timeout)
    report = new_report("discover", scope, client.evidence)
    report["limits"] = {"max_pages": max_pages, "max_sitemaps": max_sitemaps,
                        "max_candidates": max_candidates}
    report["sitemaps"] = []
    candidates, seen = [], set()

    def add(raw, base=None):
        url, reason = scope.select(raw, base=base)
        if reason:
            target = "unresolved" if reason == "invalid_url" else "excluded"
            report[target].append({"url": url, "reason": reason})
        elif url in seen:
            report["excluded"].append({"url": url, "reason": "duplicate"})
        elif len(candidates) < max_candidates:
            seen.add(url)
            candidates.append(url)
        elif "candidate_limit_reached" not in report["warnings"]:
            report["warnings"].append("candidate_limit_reached")

    if urls is not None:
        if len(urls) > 5000:
            raise InputError("URL list exceeds 5000 lines")
        for raw in urls:
            add(raw)
        if len(urls) > max_candidates:
            report["warnings"].append("candidate_limit_may_have_omitted_urls")
    else:
        queue, seen_maps = [sitemap or urljoin(scope.site, "/sitemap.xml")], set()
        while queue and len(seen_maps) < max_sitemaps and len(candidates) < max_candidates:
            raw = queue.pop(0)
            map_url, reason = scope.select(raw)
            if reason:
                target = "unresolved" if reason == "invalid_url" else "excluded"
                report[target].append({"url": map_url, "reason": reason})
                continue
            if map_url in seen_maps:
                continue
            seen_maps.add(map_url)
            reply, record = client.get(map_url)
            if not reply:
                record_fetch(report, record)
                continue
            record_fetch(report, record, sitemap=True)
            try:
                if b"<!DOCTYPE" in reply.body.upper() or b"<!ENTITY" in reply.body.upper():
                    raise InputError("xml_entities_not_supported")
                try:
                    root = ET.fromstring(reply.body)
                except (LookupError, ValueError) as exc:
                    raise InputError("cannot_decode_sitemap") from exc
                kind = root.tag.rsplit("}", 1)[-1]
                if kind not in {"sitemapindex", "urlset"}:
                    raise InputError("sitemap_root_required")
            except (ET.ParseError, InputError, LookupError) as exc:
                reason = "cannot_decode_sitemap" if isinstance(exc, LookupError) else str(exc)
                report["unresolved"].append({"url": map_url, "reason": reason})
                continue
            entries = list(root)
            for entry_index, item in enumerate(entries[:max_candidates]):
                loc = next((child.text for child in item if child.tag.rsplit("}", 1)[-1] == "loc"), None)
                if not loc or not loc.strip():
                    report["unresolved"].append({"url": record["final_url"],
                                                  "entry_index": entry_index,
                                                  "reason": "sitemap_loc_required"})
                    continue
                loc = loc.strip()
                if kind == "sitemapindex":
                    map_url, reason = scope.select(loc, base=record["final_url"])
                    if reason:
                        target = "unresolved" if reason == "invalid_url" else "excluded"
                        report[target].append({"url": map_url, "reason": reason})
                        continue
                    # Queue is bounded as well as number of network requests.
                    if len(queue) < max_sitemaps:
                        queue.append(map_url)
                    elif "sitemap_queue_limit_reached" not in report["warnings"]:
                        report["warnings"].append("sitemap_queue_limit_reached")
                else:
                    add(loc, base=record["final_url"])
            if len(entries) > max_candidates:
                report["warnings"].append("sitemap_entry_limit_reached")
        if queue:
            report["warnings"].append("sitemap_or_candidate_limit_reached")
    pages, final_seen = [], set()
    for url in candidates[:max_pages]:
        reply, record = client.get(url)
        if not reply:
            record_fetch(report, record)
            continue
        try:
            page = read_html(reply)
        except InputError as exc:
            report["unresolved"].append({**record, "reason": str(exc)})
            continue
        if page.private:
            report["excluded"].append({**record, "status": "excluded", "reason": "noindex_or_password"})
            continue
        if record["final_url"] in final_seen:
            report["excluded"].append({**record, "status": "excluded", "reason": "duplicate_final_url"})
            continue
        title, description = page.facts()
        if not title or not description:
            report["unresolved"].append({**record, "reason": "title_and_description_required"})
            continue
        final_seen.add(record["final_url"])
        pages.append({"url": record["final_url"], "title": title, "description": description,
                      "section": "页面", "visibility": "public", "status": "published",
                      "source": {"kind": client.evidence, "url": record["final_url"],
                                 "checked_at": record["checked_at"]}})
        report["links"].append(record)
    if len(candidates) > max_pages:
        report["warnings"].append("page_limit_reached_remaining_candidates_not_requested")
    report["online_check"] = client.evidence if client.attempts else "not_run"
    report["valid"] = bool(pages) and not report["failed_links"] and not report["unresolved"]
    if not pages:
        report["errors"].append("no_public_pages_with_content")
    # Site name and description must come from supplied facts or an actually read homepage.
    home = next((p for p in pages if p["url"] == canonical(site)), None)
    site_data = {"url": canonical(site)}
    if home:
        site_data.update(name=home["title"], description=home["description"])
    return {"site": site_data, "pages": pages}, finish(report)


def validate(text, site, *, online=False, timeout=8, excludes=(), client=None):
    scope = Scope(site, excludes)
    report = new_report("validate", scope)
    text = text.lstrip("\ufeff")
    lines = text.splitlines()
    html_links = HTMLHrefLinks(text)
    try:
        html_links.feed(text)
        html_links.close()
    except (AssertionError, NotImplementedError):
        report["unresolved"].append({"reason": "unsupported_html_markup"})
    report["unresolved"].extend({"line": number, "reason": "unsupported_html_link"}
                                for number in sorted(html_links.link_lines))
    if not lines or not re.match(r"^# \S", lines[0]):
        report["errors"].append("first_line_must_be_site_heading")
    if sum(bool(re.match(r"^# \S", line)) for line in lines) != 1:
        report["errors"].append("exactly_one_site_heading_required")
    summary = False
    section = False
    seen = set()
    for number, line in enumerate(lines, 1):
        if line.startswith("## "):
            section = bool(line[3:].strip())
        if line.startswith("> ") and not section and compact(line[2:]):
            summary = True
        item = LIST_ITEM.match(line)
        if not item:
            if OTHER_LINK.search(line):
                report["unresolved"].append({"line": number, "reason": "unsupported_markdown_link"})
            continue
        match = LINK.fullmatch("- " + line[item.end():])
        if not match:
            report["errors"].append({"line": number, "reason": "link_title_url_description_required"})
            continue
        title, raw, description = match.groups()
        if OTHER_LINK.search(title) or OTHER_LINK.search(description):
            report["unresolved"].append({"line": number, "reason": "unsupported_markdown_link"})
        # Markdown destinations decode semicolon-terminated HTML entities
        # exactly once. Check and request that real destination, not its source.
        raw = ENTITY.sub(lambda match: html.unescape(match.group()), raw)
        url, reason = scope.select(raw)
        if not section:
            report["errors"].append({"line": number, "reason": "link_requires_section"})
        if not title.strip() or not description.strip():
            report["errors"].append({"line": number, "reason": "empty_title_or_description"})
        if not raw.startswith(("http://", "https://")):
            reason = "absolute_http_url_required"
        if reason or url in seen:
            report["excluded"].append({"line": number, "url": url, "reason": reason or "duplicate"})
            continue
        seen.add(url)
        report["links"].append({"line": number, "url": url, "local_valid": True, "status": "not_checked"})
    if not summary:
        report["errors"].append("site_description_required")
    if not report["links"]:
        report["errors"].append("no_valid_links")
    if len(report["links"]) > 200:
        report["errors"].append("validation_limit_200_links_exceeded")
    if online:
        client = client or PublicClient(scope, timeout)
        report["evidence_kind"] = client.evidence
        final_seen = set()
        for record in report["links"][:200]:
            reply, result = client.get(record["url"])
            record.update(result)
            if reply and "html" in reply.headers.get("content-type", "").casefold():
                try:
                    if read_html(reply).private:
                        record.update(status="excluded", reason="noindex_or_password")
                        reply = None
                except InputError as exc:
                    report["unresolved"].append({**record, "reason": str(exc)})
            if reply:
                if record["final_url"] in final_seen:
                    record.update(status="excluded", reason="duplicate_final_url")
                    reply = None
                else:
                    final_seen.add(record["final_url"])
            if not reply:
                record_fetch(report, record)
        report["online_check"] = client.evidence if client.attempts else "not_run"
    report["valid"] = not any(report[key] for key in ("errors", "excluded", "unresolved", "failed_links"))
    return finish(report)


def write_text(path, content):
    """Replace a completed UTF-8 artifact; a failed write preserves the old file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix="." + path.name + ".", suffix=".tmp", delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_json(path, value):
    write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def bounded_int(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError(f"expected {low}..{high}")
        return number
    return parse


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("build", "discover", "validate"):
        command = sub.add_parser(name)
        command.add_argument("--report", required=True)
        command.add_argument("--exclude", action="append", default=[], help="additional private path prefix")
        if name != "discover":
            command.add_argument("--input", required=True)
        if name != "validate":
            command.add_argument("--output", required=True)
            command.add_argument("--max-pages", type=bounded_int(1, 200), default=20)
        if name != "build":
            command.add_argument("--site", required=True, help="absolute public-site URL")
            command.add_argument("--timeout", type=bounded_int(1, 30), default=8)
        if name == "discover":
            source = command.add_mutually_exclusive_group()
            source.add_argument("--urls", help="UTF-8 URL file; blank lines and # comments ignored")
            source.add_argument("--sitemap", help="default: /sitemap.xml; sitemap indexes supported")
            command.add_argument("--max-sitemaps", type=bounded_int(1, 20), default=5)
            command.add_argument("--max-candidates", type=bounded_int(1, 1000), default=200)
        if name == "validate":
            command.add_argument("--online", action="store_true", help="explicitly request public HTTP checks")
    args = parser.parse_args(argv)
    report = None
    write_stage = None
    try:
        outputs = [Path(args.report).resolve()]
        if hasattr(args, "output"):
            outputs.append(Path(args.output).resolve())
        inputs = [Path(getattr(args, k)).resolve() for k in ("input", "urls") if getattr(args, k, None)]
        if len(set(outputs)) != len(outputs) or any(p in inputs for p in outputs):
            raise InputError("input, output and report paths must be distinct")
        content = None
        if args.command == "build":
            data = json.loads(Path(args.input).read_text(encoding="utf-8"))
            content, report = build(data, args.max_pages, args.exclude)
        elif args.command == "discover":
            urls = None
            if args.urls:
                urls = [line.strip() for line in Path(args.urls).read_text(encoding="utf-8").splitlines()
                        if line.strip() and not line.lstrip().startswith("#")]
            data, report = discover(args.site, urls=urls, sitemap=args.sitemap, max_pages=args.max_pages,
                                    max_sitemaps=args.max_sitemaps, max_candidates=args.max_candidates,
                                    timeout=args.timeout, excludes=args.exclude)
            write_stage = "output"
            write_json(args.output, data)
        else:
            report = validate(Path(args.input).read_text(encoding="utf-8"), args.site,
                              online=args.online, timeout=args.timeout, excludes=args.exclude)
        if content is not None:
            write_stage = "output"
            write_text(args.output, content)
        write_stage = "report"
        write_json(args.report, report)
        print(json.dumps({"valid": report["valid"], "online_check": report["online_check"],
                          "counts": report["counts"]}, ensure_ascii=False))
        return 0 if report["valid"] else 1
    except (OSError, ValueError, InputError) as error:
        # Never fall back to prompts, a browser, external commands, or fabricated facts.
        failure = {"valid": False, "online_check": report["online_check"] if report else "not_run",
                   "error": str(error)}
        if report is not None and write_stage == "output":
            report["valid"] = False
            report["output_status"] = "not_written"
            report["errors"].append({"reason": "output_write_failed", "message": str(error)})
            finish(report)
            failure["output_status"] = "not_written"
            try:
                write_json(args.report, report)
            except (OSError, ValueError) as report_error:
                failure["report_error"] = str(report_error)
        print(json.dumps(failure, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
