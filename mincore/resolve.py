"""Google News 중계 주소 → 원매체 주소.

1) googlenewsdecoder 배치  2) 실패분 최대 3회 재시도(2→4→8초, ±20% 지터)
3) 그래도 실패하면 GET 리다이렉트 추적. 최종 호스트가 google이면 실패.
"""

from __future__ import annotations

import random
import re
import time
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import requests

from .normalize import is_gnews

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


@dataclass
class Resolution:
    orig_url: str
    url: str | None          # 원매체 주소 (실패 시 None)
    method: str              # direct | decoder | redirect | failed
    attempts: int = 0
    error: str | None = None


def _default_decoder(urls, interval):
    from googlenewsdecoder import gnewsdecoder
    out = gnewsdecoder(urls, interval=interval, timeout=15)
    return [out] if isinstance(out, dict) else out


def _is_google(url: str) -> bool:
    return bool(re.search(r"(^|\.)google\.[a-z.]+$", urlparse(url).netloc.lower()))


def redirect_follow(url: str, session: requests.Session | None = None, timeout: float = 15) -> str | None:
    try:
        r = (session or requests).get(url, headers={"User-Agent": UA}, timeout=timeout, allow_redirects=True)
        if r.url and not _is_google(r.url):
            return r.url
    except requests.RequestException:
        pass
    return None


def resolve_many(urls: list[str], *, interval: float = 0.5, retries: int = 3, base_wait: float = 2.0,
                 decoder: Callable | None = None, follow: Callable | None = None,
                 sleep: Callable[[float], None] = time.sleep, log: Callable[[str], None] = print) -> list[Resolution]:
    """urls와 같은 순서로 Resolution 목록을 돌려준다. 비-GN 주소는 direct."""
    decoder = decoder or _default_decoder
    follow = follow or redirect_follow
    res = [Resolution(u, u, "direct") if not is_gnews(u) else Resolution(u, None, "failed") for u in urls]
    pending = [i for i, u in enumerate(urls) if is_gnews(u)]

    for attempt in range(retries + 1):
        if not pending:
            break
        if attempt:
            wait = base_wait * (2 ** (attempt - 1)) * random.uniform(0.8, 1.2)
            log(f"[resolve] 실패 {len(pending)}건 {wait:.1f}초 후 재시도 ({attempt}/{retries})")
            sleep(wait)
        try:
            out = decoder([urls[i] for i in pending], interval)
        except Exception as e:  # 배치 전체 실패 → 다음 회차에 전부 재시도
            out = [{"success": False, "message": str(e)}] * len(pending)
        still = []
        for i, r in zip(pending, out):
            res[i].attempts = attempt + 1
            dec = r.get("decoded_url") if r.get("success") else None
            if dec and not is_gnews(dec):
                res[i].url, res[i].method, res[i].error = dec, "decoder", None
            else:
                res[i].error = str(r.get("message") or "decode failed")[:200]
                still.append(i)
        pending = still

    for i in pending:
        sleep(interval)
        url = follow(urls[i])
        if url:
            res[i].url, res[i].method = url, "redirect"
    return res
