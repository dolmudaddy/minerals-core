"""발행 원장(published_ledger.json) — 이미 발행한 기사를 다음 회차에서 제외 (P7).

CLI:
  python -m mincore.ledger commit    --ledger data/published_ledger.json --service kcmo --cards data/cards_X.json
  python -m mincore.ledger bootstrap --ledger data/published_ledger.json --service kcmo --cards data/cards_*.json
  python -m mincore.ledger check     --ledger data/published_ledger.json --url URL --title TITLE
"""

from __future__ import annotations

import argparse
import json
from difflib import SequenceMatcher
from pathlib import Path

from .normalize import canonical_url, card_id, domain_of, is_gnews, title_norm

LEDGER_VERSION = 1
TITLE_SIM_THRESHOLD = 0.9
TITLE_SIM_WINDOW_WEEKS = 8
KEEP_WEEKS = 26


class Ledger:
    def __init__(self, path: str | Path, entries: dict | None = None):
        self.path = Path(path)
        self.entries: dict[str, dict] = entries or {}
        self._titles = {e["title_norm"]: u for u, e in self.entries.items() if e.get("title_norm")}

    @classmethod
    def load(cls, path: str | Path) -> "Ledger":
        p = Path(path)
        if not p.exists():
            return cls(p)
        d = json.loads(p.read_text(encoding="utf-8"))
        return cls(p, d.get("entries") or {})

    def weeks(self) -> list[str]:
        return sorted({e["week_of"] for e in self.entries.values() if e.get("week_of")})

    def check(self, url: str | None, title: str | None) -> str | None:
        """제외 사유('url' | 'title' | 'title_similar') 또는 None."""
        canon = canonical_url(url) if url and not is_gnews(url) else None
        if canon and canon in self.entries:
            return "url"
        tn = title_norm(title)
        if not tn:
            return None
        if tn in self._titles:
            return "title"
        recent = set(self.weeks()[-TITLE_SIM_WINDOW_WEEKS:])
        for e in self.entries.values():
            if e.get("week_of") in recent and e.get("title_norm"):
                if SequenceMatcher(None, tn, e["title_norm"]).ratio() >= TITLE_SIM_THRESHOLD:
                    return "title_similar"
        return None

    def add(self, *, url: str, title: str, service: str, week_of: str, gnews_url: str | None = None) -> bool:
        canon = canonical_url(url)
        if not canon or is_gnews(canon) or canon in self.entries:
            return False
        tn = title_norm(title)
        self.entries[canon] = {"card_id": card_id(canon), "title_norm": tn, "service": service,
                               "week_of": week_of, "gnews_url": gnews_url, "domain": domain_of(canon)}
        if tn:
            self._titles[tn] = canon
        return True

    def prune(self, keep_weeks: int = KEEP_WEEKS) -> int:
        keep = set(self.weeks()[-keep_weeks:])
        drop = [u for u, e in self.entries.items() if e.get("week_of") not in keep]
        for u in drop:
            self.entries.pop(u)
        self._titles = {e["title_norm"]: u for u, e in self.entries.items() if e.get("title_norm")}
        return len(drop)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        data = {"version": LEDGER_VERSION, "entries": dict(sorted(self.entries.items()))}
        self.path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


def commit_cards(ledger: Ledger, cards_path: str | Path, service: str, resolver=None) -> tuple[int, int]:
    """legacy cards 파일(cards_*.json / week-*.json)의 기사를 원장에 기록. (추가, 해석 실패) 반환.
    resolved_url 키가 있으면 우선 쓰고, 없으면 GN 주소를 resolver로 해석한다."""
    d = json.loads(Path(cards_path).read_text(encoding="utf-8"))
    week = d.get("week_of")
    arts = d.get("articles") or []
    urls = [a.get("resolved_url") or a.get("url") or "" for a in arts]
    gn_idx = [i for i, u in enumerate(urls) if is_gnews(u)]
    failed = 0
    if gn_idx:
        if resolver is None:
            from .resolve import resolve_many as resolver
        for i, r in zip(gn_idx, resolver([urls[i] for i in gn_idx])):
            urls[i] = r.url or ""
            failed += r.url is None
    added = 0
    for a, u in zip(arts, urls):
        if u:
            gn = a.get("url") if is_gnews(a.get("url")) else a.get("gnews_url")
            added += ledger.add(url=u, title=a.get("title", ""), service=service, week_of=week, gnews_url=gn)
    return added, failed


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m mincore.ledger")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("commit", "bootstrap"):
        p = sub.add_parser(name)
        p.add_argument("--ledger", required=True)
        p.add_argument("--service", required=True, choices=("kcmo", "cmw"))
        p.add_argument("--cards", nargs="+", required=True)
    p = sub.add_parser("check")
    p.add_argument("--ledger", required=True)
    p.add_argument("--url")
    p.add_argument("--title")
    a = ap.parse_args(argv)

    led = Ledger.load(a.ledger)
    if a.cmd == "check":
        print(led.check(a.url, a.title) or "not in ledger")
        return
    for c in sorted(a.cards):
        added, failed = commit_cards(led, c, a.service)
        print(f"[ledger] {Path(c).name}: +{added} (해석 실패 {failed})")
    pruned = led.prune()
    led.save()
    print(f"[ledger] 저장 {led.path} — 항목 {len(led.entries)} (보존기간 초과 정리 {pruned})")


if __name__ == "__main__":
    main()
