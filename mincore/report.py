"""회차별 실행 보고 (P15).

collect 단계에서 RunReport에 기록 → data/reports/run_<날짜>.{json,md}
summarize 이후: python -m mincore.report finalize --report data/reports/run_X.json --raw data/raw_X.json --cards data/cards_X.json
"""

from __future__ import annotations

import argparse
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("full", "partial", "lead_only", "title_only")


def _pct(n, d):
    return f"{n / d * 100:.1f}%" if d else "-"


class RunReport:
    def __init__(self, service: str, run_date: str | None = None):
        self.service = service
        self.run_date = run_date or datetime.now(timezone.utc).date().isoformat()
        self.started = time.time()
        self.sources: dict[str, dict] = {}
        self.counts: Counter = Counter()
        self.resolve: Counter = Counter()
        self.ledger_excluded: Counter = Counter()
        self.ledger_items: list[dict] = []   # {reason, title, url} — 원장 제외 기사 목록
        self.bodies: list[dict] = []      # {domain, status, source, block_type, strategies_tried}
        self.summarize: dict | None = None

    # ── 기록 ──
    def source(self, name: str, *, fetched: int = 0, qualified: int = 0, error: str | None = None):
        s = self.sources.setdefault(name, {"fetched": 0, "qualified": 0, "error": None})
        s["fetched"] += fetched
        s["qualified"] += qualified
        s["error"] = error or s["error"]

    def count(self, key: str, n: int = 1):
        self.counts[key] += n

    def resolved(self, method: str):
        self.resolve[method] += 1

    def excluded(self, reason: str, title: str | None = None, url: str | None = None):
        self.ledger_excluded[reason] += 1
        if title or url:
            self.ledger_items.append({"reason": reason, "title": title, "url": url})

    def body(self, domain: str, rec: dict):
        self.bodies.append({"domain": domain, "status": rec.get("status"), "source": rec.get("source"),
                            "block_type": rec.get("block_type"),
                            "strategies_tried": rec.get("strategies_tried") or []})

    # ── 출력 ──
    def to_dict(self) -> dict:
        st = Counter(b["status"] for b in self.bodies)
        blocked = Counter(b["domain"] for b in self.bodies if b["block_type"])
        return {
            "service": self.service,
            "run_date": self.run_date,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "elapsed_s": round(time.time() - self.started, 1),
            "counts": dict(self.counts),
            "sources": self.sources,
            "resolve": dict(self.resolve),
            "ledger_excluded": dict(self.ledger_excluded),
            "ledger_excluded_items": self.ledger_items,
            "body_status": {s: st[s] for s in STATUSES},
            "body_source": dict(Counter(b["source"] for b in self.bodies)),
            "block_type": dict(Counter(b["block_type"] for b in self.bodies if b["block_type"])),
            "blocked_domains_top20": dict(blocked.most_common(20)),
            "strategy_use": dict(Counter(s for b in self.bodies for s in b["strategies_tried"])),
            "reader_proxy": {"tried": sum("reader_proxy" in b["strategies_tried"] for b in self.bodies),
                             "used": sum(b["source"] == "reader_proxy" for b in self.bodies)},
            "summarize": self.summarize,
        }

    def write(self, out_dir: str | Path) -> Path:
        out = Path(out_dir)
        out.mkdir(parents=True, exist_ok=True)
        p = out / f"run_{self.run_date}.json"
        d = self.to_dict()
        p.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
        p.with_suffix(".md").write_text(render_md(d), encoding="utf-8")
        return p


def render_md(d: dict) -> str:
    n = sum(d["body_status"].values())
    got = d["body_status"]["full"] + d["body_status"]["partial"]
    gn = sum(v for k, v in d["resolve"].items() if k != "direct")
    gn_ok = gn - d["resolve"].get("failed", 0)
    c = d["counts"]
    rp = d.get("reader_proxy") or {}
    L = [f"# 실행 보고 — {d['service'].upper()} {d['run_date']}", "",
         f"생성 {d['generated_at']} · 소요 {d['elapsed_s']}초", "",
         "## 핵심 지표", "",
         "| 항목 | 값 |", "|---|---:|",
         f"| 수집 기사 | {c.get('fetched', '-')} |",
         f"| 필터 통과 | {c.get('qualified', '-')} |",
         f"| 원장 제외 | {sum(d['ledger_excluded'].values())} {dict(d['ledger_excluded']) or ''} |",
         f"| 회차 내 중복 (원매체 주소 기준) | {c.get('dedup_dropped', 0)} |",
         f"| 최종 기사 | {n} |",
         f"| GN 주소 변환 성공률 | {gn_ok}/{gn} ({_pct(gn_ok, gn)}) |",
         f"| **본문 확보율** (full+partial) | **{got}/{n} ({_pct(got, n)})** |",
         f"| Reader 프록시 사용 (본문 채택/시도) | {rp.get('used', 0)}/{rp.get('tried', 0)} |"]
    if d.get("summarize"):
        s = d["summarize"]
        L.append(f"| 요약 실패 (raw − cards) | {s['failed']} ({s['raw']} → {s['cards']}) |")
    L += ["", "## 본문 상태", "", "| " + " | ".join(STATUSES) + " |", "|" + "---:|" * len(STATUSES),
          "| " + " | ".join(str(d["body_status"][s]) for s in STATUSES) + " |", "",
          f"- 본문 출처: {d['body_source']}",
          f"- 주소 해석: {d['resolve']}",
          f"- 1차 차단 유형: {d['block_type'] or '-'}",
          f"- 대체 전략 사용: {d['strategy_use'] or '-'}", "",
          "## 도메인별 1차 차단 (상위 20)", "", "| 도메인 | 건수 |", "|---|---:|"]
    L += [f"| {k} | {v} |" for k, v in d["blocked_domains_top20"].items()] or ["| - | 0 |"]
    items = d.get("ledger_excluded_items") or []
    if items:
        L += ["", f"## 원장 제외 목록 ({len(items)}건)", "", "| # | 사유 | 제목 |", "|---:|---|---|"]
        L += [f"| {i} | {it['reason']} | {(it.get('title') or it.get('url') or '').replace('|', '/')[:120]} |"
              for i, it in enumerate(items, 1)]
    errs = {k: v for k, v in d["sources"].items() if v.get("error")}
    if errs:
        L += ["", "## 소스 오류", ""] + [f"- {k}: {v['error']}" for k, v in errs.items()]
    return "\n".join(L) + "\n"


def finalize(report_path: str | Path, raw_path: str | Path, cards_path: str | Path) -> dict:
    rp = Path(report_path)
    d = json.loads(rp.read_text(encoding="utf-8"))
    raw = json.loads(Path(raw_path).read_text(encoding="utf-8")).get("articles") or []
    cards = json.loads(Path(cards_path).read_text(encoding="utf-8")).get("articles") or []
    d["summarize"] = {"raw": len(raw), "cards": len(cards), "failed": max(0, len(raw) - len(cards))}
    rp.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    rp.with_suffix(".md").write_text(render_md(d), encoding="utf-8")
    return d


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m mincore.report")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("finalize")
    p.add_argument("--report", required=True)
    p.add_argument("--raw", required=True)
    p.add_argument("--cards", required=True)
    a = ap.parse_args(argv)
    d = finalize(a.report, a.raw, a.cards)
    print(f"[report] 요약 실패 {d['summarize']['failed']} ({d['summarize']['raw']} → {d['summarize']['cards']})")


if __name__ == "__main__":
    main()
