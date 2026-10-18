"""예의 규칙이 들어간 HTTP 클라이언트.

- 순차 사용 전제 → 도메인당 동시 1건
- 도메인별 최소 간격(기본 0.5초, 규칙으로 늘릴 수 있음), 타임아웃 15초
- 429/503: Retry-After를 따르고 없으면 5→15초 대기, 최대 2회 재시도
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable

import requests

from .normalize import domain_of

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9,ko;q=0.8"}
RETRY_STATUS = {429, 503}
RETRY_WAITS = (5.0, 15.0)
MAX_RETRY_AFTER = 60.0


@dataclass
class FetchResult:
    url: str
    final_url: str | None = None
    status: int | None = None
    text: str = ""
    error: str | None = None
    error_kind: str | None = None   # timeout | conn | None
    retries: int = 0
    elapsed: float = 0.0


class Fetcher:
    def __init__(self, min_interval: float = 0.5, timeout: float = 15.0,
                 session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self.min_interval = min_interval
        self.timeout = timeout
        self.session = session or requests.Session()
        self.sleep, self.clock = sleep, clock
        self._last: dict[str, float] = {}

    def _wait_turn(self, domain: str, interval: float) -> None:
        last = self._last.get(domain)
        if last is not None:
            gap = interval - (self.clock() - last)
            if gap > 0:
                self.sleep(gap)

    @staticmethod
    def _retry_after(resp, attempt: int) -> float:
        ra = resp.headers.get("Retry-After") if resp is not None else None
        if ra and ra.strip().isdigit():
            return min(float(ra), MAX_RETRY_AFTER)
        return RETRY_WAITS[min(attempt, len(RETRY_WAITS) - 1)]

    def get(self, url: str, *, min_interval: float | None = None, headers: dict | None = None,
            timeout: float | None = None) -> FetchResult:
        domain = domain_of(url)
        interval = max(self.min_interval, min_interval or 0)
        res = FetchResult(url)
        t0 = self.clock()
        for attempt in range(len(RETRY_WAITS) + 1):
            self._wait_turn(domain, interval)
            try:
                r = self.session.get(url, headers={**HEADERS, **(headers or {})},
                                     timeout=timeout or self.timeout, allow_redirects=True)
                res.status, res.final_url, res.text = r.status_code, r.url, r.text or ""
                res.error = res.error_kind = None
            except requests.Timeout:
                res.error, res.error_kind, r = "timeout", "timeout", None
            except requests.RequestException as e:
                res.error, res.error_kind, r = f"{type(e).__name__}: {e}"[:200], "conn", None
            finally:
                self._last[domain] = self.clock()
            if r is None or r.status_code not in RETRY_STATUS or attempt == len(RETRY_WAITS):
                break
            res.retries += 1
            self.sleep(self._retry_after(r, attempt))
        res.elapsed = round(self.clock() - t0, 2)
        return res
