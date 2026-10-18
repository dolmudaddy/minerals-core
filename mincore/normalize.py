"""URL·제목·날짜 정규화와 카드 ID."""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

TRACKING_PARAMS = {"fbclid", "gclid", "ref", "ref_src", "mc_cid", "mc_eid", "ocid", "cmpid", "amp"}


def domain_of(url: str | None) -> str:
    host = (urlparse(url or "").netloc or "").lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def is_gnews(url: str | None) -> bool:
    return domain_of(url) == "news.google.com"


def canonical_url(url: str | None) -> str:
    """호스트 소문자·www 제거, 추적 파라미터·fragment 제거, 끝의 /amp·/ 제거."""
    if not url:
        return ""
    p = urlparse(url.strip())
    host = domain_of(url)
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in TRACKING_PARAMS]
    path = re.sub(r"/amp/?$", "", p.path) or "/"
    if len(path) > 1:
        path = path.rstrip("/")
    return urlunparse(((p.scheme or "https").lower(), host, path, "", urlencode(query), ""))


def card_id(canonical: str) -> str:
    return hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]


_PUBLISHER_TAIL = re.compile(r"\s+[-–—|]\s+[^-–—|]{1,60}$")
_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)


def strip_publisher_tail(title: str) -> str:
    """Google News 제목 끝의 ' - 매체명'을 제거한다(본문이 남는 경우에만)."""
    t = (title or "").strip()
    stripped = _PUBLISHER_TAIL.sub("", t)
    return stripped if len(stripped) >= 15 else t


def title_norm(title: str | None) -> str:
    t = unicodedata.normalize("NFKC", html.unescape(title or ""))
    t = strip_publisher_tail(t).lower()
    t = _PUNCT.sub(" ", t).replace("_", " ")
    return re.sub(r"\s+", " ", t).strip()


def norm_date(s: str | None) -> str | None:
    """ISO('2026-09-15', '2026-10-11T05:10:25+00:00') 또는 RFC822 → YYYY-MM-DD."""
    if not s:
        return None
    s = str(s).strip()
    if re.match(r"\d{4}-\d{2}-\d{2}", s):
        return s[:10]
    try:
        return parsedate_to_datetime(s).date().isoformat()
    except (TypeError, ValueError, IndexError):
        return None


def clean_html_text(s: str | None) -> str:
    """RSS 요약의 HTML 조각 → 평문 (Google News의 <a href> 조각 정리용)."""
    if not s:
        return ""
    t = re.sub(r"<[^>]+>", " ", s)
    t = html.unescape(t).replace("\xa0", " ")
    return re.sub(r"\s+", " ", t).strip()
