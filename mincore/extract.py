"""원매체 주소 → 본문 상태 기록.

trafilatura(favor_precision) → (300자 미만·차단 시) 도메인 대체표 전략 → og:description → rss_summary.
본문 전문은 BodyResult.text(메모리)와 선택적 캐시 파일에만 존재하고, to_record()에는 들어가지 않는다.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

import trafilatura

from .domain_rules import DEFAULT_RULE, DomainRule, DomainRules
from .fetch import Fetcher
from .normalize import canonical_url, clean_html_text, norm_date, title_norm

FULL_MIN, PARTIAL_MIN = 800, 300
QUOTES_MAX_N, QUOTES_MAX_TOTAL = 2, 300

BOT_STATUS = {401, 403, 429, 503}
BOT_SIGNS = ("just a moment", "cf-browser-verification", "cf-challenge", "captcha",
             "are you a robot", "access denied", "perimeterx", "datadome",
             "enable javascript and cookies", "request unsuccessful")
PAYWALL_SIGNS = ("paywall", "subscribe to continue", "subscribers only",
                 "subscriber-only", "premium content", "to continue reading",
                 '"isaccessibleforfree":false', '"isaccessibleforfree": false',
                 '"isaccessibleforfree":"false"', '"isaccessibleforfree": "false"')

DEFAULT_KEYWORDS = (
    "lithium", "nickel", "cobalt", "copper", "rare earth", "graphite", "manganese", "tungsten",
    "uranium", "antimony", "gallium", "germanium", "critical mineral", "mineral", "mining",
    "ministry", "minister", "government", "mou", "kiat", "kigam", "koica",
    "nikel", "tembaga", "tambang", "hilirisasi", "kementerian",
    "리튬", "니켈", "코발트", "희토류", "흑연", "광물", "광산", "산업통상", "자원",
)

_UNIT = (r"%|percent|persen|tonnes?|tons?|kt|mt|mtpa|tpa|million|billion|trillion|miliar|juta|triliun"
         r"|us\$|usd|\$|€|eur|rp|mw|gw|gwh|km|ha|ppm|g/t|kg|억|만|톤|조|원|달러")
NUM_UNIT = re.compile(rf"(?:(?:us\$|\$|€|rp)\s?\d)|(?:\d[\d,.]*\s?(?:{_UNIT})(?![a-z]))", re.I)
QUOTED = re.compile(r"[“\"「『][^”\"」』]{12,}[”\"」』]")
SENT_SPLIT = re.compile(r"(?<=[.!?。])\s+|\n+")


def status_for(n: int) -> str:
    return "full" if n >= FULL_MIN else "partial" if n >= PARTIAL_MIN else "lead_only" if n > 0 else "title_only"


def classify_block(status: int | None, html_lower: str, body_len: int, err_kind: str | None) -> str:
    if err_kind == "timeout":
        return "timeout"
    if err_kind:
        return "http_error"
    if status in BOT_STATUS or (body_len < PARTIAL_MIN and any(s in html_lower[:20000] for s in BOT_SIGNS)):
        return "bot_block"
    if any(s in html_lower for s in PAYWALL_SIGNS):
        return "paywall"
    if status and status >= 400:
        return "http_error"
    return "parse_fail"


def select_quotes(text: str, keywords: Iterable[str] = DEFAULT_KEYWORDS,
                  max_n: int = QUOTES_MAX_N, max_total: int = QUOTES_MAX_TOTAL) -> list[str]:
    """우선순위: ① 숫자·단위 문장 ② 따옴표 인용 ③ 광물·기관 키워드 문장. 같은 순위는 앞 문장 우선."""
    kws = [k.lower() for k in keywords]
    ranked = []
    for pos, s in enumerate(x.strip() for x in SENT_SPLIT.split(text or "")):
        if not (40 <= len(s) <= 220):
            continue
        low = s.lower()
        rank = 0 if NUM_UNIT.search(s) else 1 if QUOTED.search(s) else 2 if any(k in low for k in kws) else None
        if rank is not None:
            ranked.append((rank, pos, s))
    out, total = [], 0
    for _, _, s in sorted(ranked):
        if len(out) >= max_n:
            break
        if total + len(s) <= max_total and s not in out:
            out.append(s)
            total += len(s)
    return out


_OG = [re.compile(p, re.I | re.S) for p in (
    r'<meta[^>]+property=["\']og:description["\'][^>]*content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*property=["\']og:description',
    r'<meta[^>]+name=["\']description["\'][^>]*content=["\']([^"\']+)',
    r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*name=["\']description',
)]


def og_description(html: str) -> str:
    for p in _OG:
        m = p.search(html or "")
        if m:
            return clean_html_text(m.group(1))
    return ""


def trafilatura_extract(html: str, url: str | None = None) -> tuple[str, str | None]:
    if not html:
        return "", None
    try:
        doc = trafilatura.bare_extraction(html, url=url, favor_precision=True, with_metadata=True)
    except Exception:
        return "", None
    if doc is None:
        return "", None
    d = doc if isinstance(doc, dict) else doc.as_dict()
    return (d.get("text") or "").strip(), norm_date(d.get("date"))


@dataclass
class BodyResult:
    status: str = "title_only"
    chars: int = 0
    sha256: str | None = None
    source: str = "none"        # trafilatura | amp | feed | presscorner_api | og_description | rss_summary | none
    block_type: str | None = None
    http_status: int | None = None
    extracted_date: str | None = None
    quotes: list[str] = field(default_factory=list)
    final_url: str | None = None
    strategies_tried: list[str] = field(default_factory=list)
    error: str | None = None
    text: str = field(default="", repr=False)   # 메모리 전용. 직렬화 금지

    def to_record(self) -> dict:
        """커밋되는 카드에 들어갈 필드. 본문 전문(text)은 제외."""
        return {k: getattr(self, k) for k in (
            "status", "chars", "sha256", "source", "block_type", "http_status",
            "extracted_date", "quotes", "final_url", "strategies_tried", "error")}


# ── 대체 전략 ────────────────────────────────────────────────
def _strategy_amp(url, fetcher: Fetcher, rule: DomainRule, ctx) -> str:
    base = re.sub(r"/amp/?$", "", url.rstrip("/"))
    best = ""
    for tmpl in rule.amp_templates:
        u = tmpl.format(url=url.rstrip("/"), base=base)
        if u.rstrip("/") == url.rstrip("/"):
            continue
        r = fetcher.get(u, min_interval=rule.min_interval)
        if r.status == 200:
            text, _ = trafilatura_extract(r.text, u)
            best = max(best, text, key=len)
            if len(best) >= PARTIAL_MIN:
                break
    return best


def _strategy_feed(url, fetcher: Fetcher, rule: DomainRule, ctx) -> str:
    import feedparser
    target = canonical_url(url)
    feeds = ctx.setdefault("_feeds", {})
    for fu in rule.feed_urls:
        if fu not in feeds:
            r = fetcher.get(fu)
            feeds[fu] = feedparser.parse(r.text) if r.status == 200 else None
        parsed = feeds[fu]
        for e in (parsed.entries if parsed else []):
            if canonical_url(e.get("link") or e.get("id") or "") == target:
                content = (e.get("content") or [{}])[0].get("value") or e.get("summary") or ""
                return clean_html_text(content)
    return ""


_PRESS_REF = re.compile(r"/presscorner/detail/(\w+)/([a-z]+)_(\d+)_(\d+)", re.I)


def _strategy_presscorner_api(url, fetcher: Fetcher, rule: DomainRule, ctx) -> str:
    m = _PRESS_REF.search(url)
    if not m:
        return ""
    lang, kind, yy, num = m.groups()
    api = (f"https://ec.europa.eu/commission/presscorner/api/documents"
           f"?reference={kind.upper()}/{yy}/{num}&language={lang.lower()}")
    r = fetcher.get(api, headers={"Accept": "application/json"})
    if r.status != 200:
        return ""
    try:
        res = json.loads(r.text).get("docuLanguageResource") or {}
    except ValueError:
        return ""
    return clean_html_text(re.sub(r"</p>\s*", "</p>\n", res.get("htmlContent") or ""))


STRATEGIES = {"amp": _strategy_amp, "feed": _strategy_feed, "presscorner_api": _strategy_presscorner_api}


def _rss_body(rss_summary: str | None, title: str | None) -> str:
    """RSS 설명 정리. Google News처럼 '제목 + 매체명'뿐이면 본문이 아니므로 버린다."""
    s = clean_html_text(rss_summary)
    if not s:
        return ""
    ns, nt = title_norm(s), title_norm(title)
    if nt and (ns.startswith(nt) or nt.startswith(ns)) and len(ns) < len(nt) + 60:
        return ""
    return s


# ── 메인 ─────────────────────────────────────────────────────
def extract_body(url: str, fetcher: Fetcher, rules: DomainRules | None = None, *,
                 title: str | None = None, rss_summary: str | None = None,
                 keywords: Iterable[str] = DEFAULT_KEYWORDS, cache_dir: str | Path | None = None,
                 ctx: dict | None = None) -> BodyResult:
    rule = rules.for_url(url) if rules else DEFAULT_RULE
    ctx = {} if ctx is None else ctx
    res = BodyResult()
    html = ""
    err_kind = None

    if rule.skip_fetch:
        res.block_type = "skipped"
    else:
        r = fetcher.get(url, min_interval=rule.min_interval)
        res.http_status, res.final_url, err_kind = r.status, r.final_url, r.error_kind
        res.error = r.error
        html = r.text if r.status and r.status < 400 else ""

    text, res.extracted_date = trafilatura_extract(html, url)
    best_src, best = "trafilatura", text

    if len(best) < PARTIAL_MIN:
        if not rule.skip_fetch:
            res.block_type = classify_block(res.http_status, (r.text or "").lower(), len(best), err_kind)
        for name in rule.strategies:
            if name in ("og", "rss_summary"):
                continue
            res.strategies_tried.append(name)
            try:
                cand = STRATEGIES[name](url, fetcher, rule, ctx)
            except Exception as e:
                cand, res.error = "", f"{name}: {e}"[:200]
            if len(cand) > len(best):
                best_src, best = name, cand
            if len(best) >= PARTIAL_MIN:
                break
        if len(best) < PARTIAL_MIN:
            for name, cand in (("og_description", og_description(html)),
                               ("rss_summary", _rss_body(rss_summary, title))):
                if len(cand) > len(best):
                    best_src, best = name, cand

    n = len(best)
    res.status, res.chars, res.text = status_for(n), n, best
    res.source = best_src if n else "none"
    if n:
        res.sha256 = hashlib.sha256(best.encode("utf-8")).hexdigest()
        res.quotes = select_quotes(best, keywords)
        if cache_dir:
            p = Path(cache_dir)
            p.mkdir(parents=True, exist_ok=True)
            (p / f"{res.sha256}.txt").write_text(best, encoding="utf-8")
    if n >= PARTIAL_MIN and res.source == "trafilatura":
        res.block_type = None
    return res
