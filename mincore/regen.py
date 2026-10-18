"""과거 발행분 일괄 재생성 (리뉴얼 5단계, v2).

python -m mincore.regen --service kcmo --input data --from 2026-05-13 --to 2026-10-18 \
    --prompt prompts/summarize_v2.md --out data/v2 [--resume] [--max-issues 10]

회차마다: 주소 해석 → 본문 추출(도메인 규칙·reader_proxy, 회차별 ctx) → card.v1 → 요약·검증 → 저장.
- 입력 v1 파일은 읽기만 한다. 출력은 <out>/<service>/cards_v2_<날짜>.json (회차 단위 → 중단·재개 가능)
- 본문 전문은 --cache-dir(기본 data/_cache/bodies, gitignore)에만 쓴다
- 검증 실패 카드는 cards에서 빼고 excluded[]에 사유만 남긴다
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from . import __version__
from .card import build_card, validate
from .domain_rules import load_rules
from .extract import BodyResult, _rss_body, extract_body
from .fetch import Fetcher
from .normalize import clean_html_text, is_gnews, norm_date
from .resolve import resolve_many
from .summarize import DEFAULT_MODEL, Prompt, load_prompt, summarize_card, to_legacy

# 서비스별 v1 파일 규칙
PATTERNS = {"kcmo": "cards_*.json", "cmw": "week-*.json"}
# 회차의 GN 주소 해석 실패가 이 비율을 넘으면 저장하지 않고 중단한다(일시 차단 → 제목 기반 카드 양산 방지).
# --resume으로 다시 돌리면 그 회차부터 이어진다.
RESOLVE_FAIL_MAX = 0.5


class ResolveOutage(RuntimeError):
    pass
DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")


# ── v1 입력 ──────────────────────────────────────────────────
def discover(service: str, input_dir: str | Path, date_from: str | None = None,
             date_to: str | None = None) -> list[tuple[str, Path]]:
    out = []
    for f in sorted(Path(input_dir).glob(PATTERNS[service])):
        m = DATE_RE.search(f.name)
        if not m or "v2" in f.name:
            continue
        d = m.group(1)
        if (date_from and d < date_from) or (date_to and d > date_to):
            continue
        out.append((d, f))
    return out


def load_v1(service: str, path: Path) -> list[dict]:
    """v1 회차 파일 → 공통 입력 기사 목록. v1 요약은 비교용으로 v1 키에 보관."""
    arts = json.loads(path.read_text(encoding="utf-8")).get("articles") or []
    raw_summary = {}
    if service == "kcmo":                    # KCMO cards에는 summary_raw가 없음 → 같은 날짜 raw에서
        rp = path.with_name(path.name.replace("cards_", "raw_"))
        if rp.exists():
            for a in json.loads(rp.read_text(encoding="utf-8")).get("articles") or []:
                raw_summary[a.get("id")] = a.get("summary_raw", "")
    items = []
    for a in arts:
        v1 = {k: a.get(k) for k in ("korean_summary", "oda_relevance", "category", "tags",
                                    "key_phrases", "english_phrases", "ai_processed") if k in a}
        ext = {"legacy_id": a.get("id"), "v1_url": a.get("url")}
        if service == "kcmo":
            ext.update(country=a.get("country"), source_language=a.get("source_language"))
            summary_raw = raw_summary.get(a.get("id"), "")
        else:
            summary_raw = a.get("summary_raw", "")
            ext["summary_raw"] = summary_raw
        items.append({"title": a.get("title", ""), "url": a.get("url", ""), "source": a.get("source", ""),
                      "language": a.get("source_language"), "published": a.get("published"),
                      "score": a.get("score"), "tier_weight": a.get("tier_weight"),
                      "summary_raw": summary_raw, "ext": ext, "v1": v1})
    return items


def v1_metrics(service: str, path: Path) -> dict:
    arts = json.loads(path.read_text(encoding="utf-8")).get("articles") or []
    n = len(arts)
    if service == "kcmo":
        rp = path.with_name(path.name.replace("cards_", "raw_"))
        raw_n = len(json.loads(rp.read_text(encoding="utf-8")).get("articles") or []) if rp.exists() else n
        failed = max(0, raw_n - n)
    else:
        failed = sum(1 for a in arts if a.get("ai_processed") is False)
    return {"articles": n, "original_link": sum(1 for a in arts if a.get("url") and not is_gnews(a["url"])),
            "body_based": 0, "failed": failed}


# ── 회차 처리 ────────────────────────────────────────────────
class Regen:
    def __init__(self, service: str, prompt: Prompt, *, model: str = DEFAULT_MODEL,
                 cache_dir: str | Path = "data/_cache/bodies", compare_model: str | None = None,
                 compare_n: int = 0, call: Callable | None = None, resolver: Callable = resolve_many,
                 extractor: Callable = extract_body, fetcher: Fetcher | None = None, rules=None,
                 log: Callable[[str], None] = print):
        self.service, self.prompt, self.model = service, prompt, model
        self.cache_dir = cache_dir
        self.compare_model, self.compare_left = compare_model, compare_n if compare_model else 0
        self.call, self.resolver, self.extractor = call, resolver, extractor
        self.fetcher = fetcher or Fetcher()
        self.rules = rules if rules is not None else load_rules()
        self.log = log

    def _body(self, item, res, ctx) -> BodyResult:
        if res.url:
            return self.extractor(res.url, self.fetcher, self.rules, title=item["title"],
                                  rss_summary=item["summary_raw"], cache_dir=self.cache_dir, ctx=ctx)
        b = BodyResult(block_type="resolve_fail", error=res.error)   # 주소 해석 실패 → RSS 설명만
        lead = _rss_body(item["summary_raw"], item["title"])
        if lead:
            b.status, b.chars, b.source, b.text = "lead_only", len(lead), "rss_summary", lead
        return b

    def process_issue(self, week: str, items: list[dict]) -> dict:
        resolutions = self.resolver([it["url"] for it in items])
        gn = [r for it, r in zip(items, resolutions) if is_gnews(it["url"])]
        failed = [r for r in gn if not r.url]
        if failed:
            self.log(f"[regen] GN 주소 해석 실패 {len(failed)}/{len(gn)}: "
                     f"{dict(Counter((r.error or '')[:80] for r in failed).most_common(3))}")
        if gn and len(failed) / len(gn) > RESOLVE_FAIL_MAX:
            raise ResolveOutage(f"{week}: GN 해석 실패 {len(failed)}/{len(gn)} > {RESOLVE_FAIL_MAX:.0%}")
        ctx: dict = {}
        cards, excluded = [], []
        st = {k: Counter() for k in ("body_status", "body_source", "resolve", "fail_reason", "basis")}
        for i, (it, r) in enumerate(zip(items, resolutions), 1):
            st["resolve"][r.method] += 1
            body = self._body(it, r, ctx)
            is_gn = is_gnews(it["url"])
            card = build_card(self.service, title=it["title"], url=r.url or it["url"],
                              gnews_url=it["url"] if is_gn else None, source_name=it["source"],
                              tier_weight=it["tier_weight"], language=it["language"],
                              published=norm_date(it["published"]) or body.extracted_date,
                              published_from="rss" if it["published"] else None,
                              score=it["score"], body=body, ext=it["ext"])
            if not r.url:
                card["url"] = None
            st["body_status"][card["body"]["status"]] += 1
            st["body_source"][card["body"]["source"]] += 1
            res = summarize_card(card, self.prompt, body_text=body.text,
                                 rss_summary=clean_html_text(it["summary_raw"]), model=self.model, call=self.call)
            if not res.ok:
                excluded.append({"card_id": card["card_id"], "title": it["title"], "url": card["url"] or it["url"],
                                 "body_status": card["body"]["status"], "attempts": res.attempts,
                                 "errors": res.errors})
                for e in res.errors:
                    st["fail_reason"][e.split(":")[0]] += 1
                self.log(f"  [{i}/{len(items)}] EXCLUDED {card['body']['status']:<10} {'; '.join(res.errors)[:90]}")
                continue
            card["summary"], card["provenance"] = res.summary, res.provenance
            st["basis"][res.summary["basis"]] += 1
            if self.compare_left > 0:
                self.compare_left -= 1
                cmp = summarize_card(card, self.prompt, body_text=body.text,
                                     rss_summary=clean_html_text(it["summary_raw"]),
                                     model=self.compare_model, call=self.call)
                card["compare"] = {"model": self.compare_model, "ok": cmp.ok, "summary": cmp.summary,
                                   "errors": cmp.errors}
            card["v1"] = it["v1"]
            card["legacy"] = to_legacy(card, self.service, self.prompt)
            errs = [e for e in validate(card) if "url" not in e or card["url"]]
            if errs:
                self.log(f"  [card.v1 검증] {card['card_id']}: {errs}")
            cards.append(card)
            self.log(f"  [{i}/{len(items)}] {card['body']['status']:<10} {res.summary['basis']:<5} "
                     f"try={res.attempts} {it['title'][:60]}")
        n = len(items)
        got = st["body_status"]["full"] + st["body_status"]["partial"]
        return {
            "schema_version": "card.v1", "service": self.service, "week_of": week,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "mincore_version": __version__, "model": self.model,
            "prompt_version": self.prompt.version, "prompt_sha256": self.prompt.sha256,
            "stats": {"v1_articles": n, "cards": len(cards), "excluded": len(excluded),
                      "body_got": got, "body_rate": round(got / n, 3) if n else None,
                      "original_link": sum(1 for c in cards if c["url"] and not is_gnews(c["url"]))
                      + sum(1 for e in excluded if e["url"] and not is_gnews(e["url"])),
                      "reader_proxy_used": ctx.get("reader_proxy_used", 0),
                      **{k: dict(v) for k, v in st.items()}},
            "total": len(cards), "cards": cards, "excluded": excluded,
        }


def run(service: str, input_dir: str | Path, out_dir: str | Path, prompt_path: str | Path, *,
        date_from: str | None = None, date_to: str | None = None, resume: bool = False,
        max_issues: int | None = None, **kw) -> list[str]:
    prompt = load_prompt(prompt_path)
    out = Path(out_dir) / service
    out.mkdir(parents=True, exist_ok=True)
    state_p = out / "regen_state.json"
    state = json.loads(state_p.read_text(encoding="utf-8")) if state_p.exists() else {}
    rg = Regen(service, prompt, **kw)
    done = []
    for week, path in discover(service, input_dir, date_from, date_to):
        target = out / f"cards_v2_{week}.json"
        if resume and target.exists():
            rg.log(f"[regen] {week} 이미 있음 → 건너뜀")
            continue
        if max_issues is not None and len(done) >= max_issues:
            rg.log(f"[regen] --max-issues {max_issues} 도달 → 중단 (--resume으로 이어서)")
            break
        items = load_v1(service, path)
        rg.log(f"\n[regen] {service} {week}: v1 {len(items)}건 ({path.name})")
        try:
            doc = rg.process_issue(week, items)
        except ResolveOutage as e:
            rg.log(f"[regen][중단] {e} — 이 회차는 저장하지 않음. 잠시 뒤 --resume으로 다시 실행")
            break
        doc["v1_file"] = path.name
        doc["v1_metrics"] = v1_metrics(service, path)
        target.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        state[week] = {"done_at": doc["generated_at"], "model": doc["model"],
                       "prompt_version": doc["prompt_version"], "prompt_sha256": doc["prompt_sha256"],
                       "cards": doc["total"], "excluded": len(doc["excluded"])}
        state_p.write_text(json.dumps(dict(sorted(state.items())), ensure_ascii=False, indent=1), encoding="utf-8")
        s = doc["stats"]
        rg.log(f"[regen] {week} 저장: 카드 {s['cards']}/{s['v1_articles']} · 제외 {s['excluded']} · "
               f"본문 {s['body_got']}/{s['v1_articles']}")
        done.append(week)
    return done


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m mincore.regen")
    ap.add_argument("--service", required=True, choices=sorted(PATTERNS))
    ap.add_argument("--input", required=True, help="v1 회차 파일 폴더 (KCMO data/, CMW data/)")
    ap.add_argument("--out", default="data/v2")
    ap.add_argument("--prompt", required=True)
    ap.add_argument("--from", dest="date_from")
    ap.add_argument("--to", dest="date_to")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--max-issues", type=int)
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--compare-model")
    ap.add_argument("--compare-n", type=int, default=0)
    ap.add_argument("--cache-dir", default="data/_cache/bodies")
    ap.add_argument("--test-before", help="이 날짜 이전 회차는 보고서에 '시험 회차'로 표시")
    ap.add_argument("--no-report", action="store_true")
    a = ap.parse_args(argv)
    done = run(a.service, a.input, a.out, a.prompt, date_from=a.date_from, date_to=a.date_to,
               resume=a.resume, max_issues=a.max_issues, model=a.model, cache_dir=a.cache_dir,
               compare_model=a.compare_model, compare_n=a.compare_n)
    print(f"[regen] 이번 실행 {len(done)}회차: {', '.join(done) or '-'}")
    if not a.no_report:
        from .compare import write_regen_report
        p = write_regen_report(a.service, a.out, test_before=a.test_before)
        print(f"[regen] 보고서: {p}")


if __name__ == "__main__":
    sys.exit(main())
