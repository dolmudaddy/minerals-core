"""v1 ↔ v2 비교 (리뉴얼 5단계).

- write_regen_report: <out>/<service>/regen_report.{md,json} — 회차별 본문 확보율·body_status·검증 실패,
  진단서 비교표(본문 기반 비율·원매체 링크 비율·요약 실패 수)
- write_review: 한 회차의 v1 / v2 / 비교 모델 요약을 나란히 놓은 표 + 골든셋 초안

python -m mincore.compare report --service kcmo --out data/v2 [--test-before 2026-05-24]
python -m mincore.compare review --issue data/v2/kcmo/cards_v2_2026-10-24.json --n 30 \
    --md data/v2/kcmo/review_2026-10-24.md --golden data/v2/golden/golden_draft.json
"""

from __future__ import annotations

import argparse
import json
import statistics
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

STATUSES = ("full", "partial", "lead_only", "title_only")


def _pct(n, d):
    return f"{n / d * 100:.1f}%" if d else "-"


def _cell(s) -> str:
    if isinstance(s, list):
        s = " / ".join(str(x) for x in s)
    return str(s or "").replace("|", "/").replace("\n", " ").strip()


def write_regen_report(service: str, out_dir: str | Path, *, test_before: str | None = None) -> Path:
    folder = Path(out_dir) / service
    issues, all_lines = [], []
    for f in sorted(folder.glob("cards_v2_*.json")):
        d = json.loads(f.read_text(encoding="utf-8"))
        s, v1 = d["stats"], d.get("v1_metrics") or {}
        lens = [len(x) for c in d["cards"] for x in (c.get("summary") or {}).get("lines") or []]
        all_lines += lens
        issues.append({
            "line_median": statistics.median(lens) if lens else None, "line_max": max(lens, default=None),
            "over100": sum(x > 100 for x in lens), "oda_filled": s.get("oda_filled"),
            "flags": s.get("flags", {}), "model_used": s.get("model_used", {}),
            "retry_reason": s.get("retry_reason", {}), "cost_usd": s.get("cost_usd", 0.0),
            "usage": s.get("usage", {}),
            "week": d["week_of"], "test": bool(test_before and d["week_of"] < test_before),
            "v1_articles": s["v1_articles"], "cards": s["cards"], "excluded": s["excluded"],
            "body_got": s["body_got"], "body_status": s.get("body_status", {}),
            "basis": s.get("basis", {}), "original_link": s.get("original_link", 0),
            "reader_proxy_used": s.get("reader_proxy_used", 0), "fail_reason": s.get("fail_reason", {}),
            "v1_original_link": v1.get("original_link", 0), "v1_failed": v1.get("failed", 0),
            "model": d.get("model"), "prompt_version": d.get("prompt_version")})

    def total(rows):
        t = Counter()
        for r in rows:
            for k in ("v1_articles", "cards", "excluded", "body_got", "original_link",
                      "reader_proxy_used", "v1_original_link", "v1_failed"):
                t[k] += r[k]
            t["cost_usd"] += r["cost_usd"] or 0.0
            t["basis_body"] += r["basis"].get("body", 0)
        return t

    ops = [r for r in issues if not r["test"]]
    T, O = total(issues), total(ops)
    reasons, retry, flags = Counter(), Counter(), Counter()
    for r in issues:
        reasons.update(r["fail_reason"])
        retry.update(r["retry_reason"])
        flags.update(r["flags"])

    L = [f"# 재생성 보고 — {service.upper()} v2", "",
         f"생성 {datetime.now(timezone.utc).isoformat(timespec='seconds')} · 회차 {len(issues)}개"
         + (f" (시험 회차 {len(issues) - len(ops)}개 포함: {test_before} 이전)" if test_before else ""), ""]

    def comp_table(title, t):
        n = t["v1_articles"]
        return [f"## {title}", "", "| 지표 | v1 | v2 |", "|---|---:|---:|",
                f"| 기사 수 | {n} | {n} |",
                f"| 본문 기반 요약 비율 | 0/{n} (0.0%) — RSS 설명만 사용 | {t['basis_body']}/{n} ({_pct(t['basis_body'], n)}) |",
                f"| 원매체 링크 비율 | {t['v1_original_link']}/{n} ({_pct(t['v1_original_link'], n)}) | "
                f"{t['original_link']}/{n} ({_pct(t['original_link'], n)}) |",
                f"| 요약 실패 수 | {t['v1_failed']} | {t['excluded']} (검증 제외) |",
                f"| 본문 확보 (full+partial) | - | {t['body_got']}/{n} ({_pct(t['body_got'], n)}) |", ""]

    L += comp_table("진단서 비교표 (전체)", T)
    if test_before and len(ops) != len(issues):
        L += comp_table(f"진단서 비교표 (운영 회차만, {test_before} 이후)", O)
    L += ["## 회차별", "",
          "| 회차 | 기사 | 카드 | 제외 | 본문 확보 | full | partial | lead | title | 원매체 링크 | Jina "
          "| 줄 중앙값/최대 | 100자 초과 | 시사점 | 폴백 | 추정 비용 |",
          "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for r in issues:
        bs = r["body_status"]
        L.append(f"| {r['week']}{' (시험)' if r['test'] else ''} | {r['v1_articles']} | {r['cards']} | "
                 f"{r['excluded']} | {_pct(r['body_got'], r['v1_articles'])} | "
                 + " | ".join(str(bs.get(s, 0)) for s in STATUSES)
                 + f" | {r['original_link']} | {r['reader_proxy_used']} | {r['line_median']}/{r['line_max']} "
                 f"| {r['over100']} | {r['oda_filled'] if r['oda_filled'] is not None else '-'} "
                 f"| {r['flags'].get('fallback_from', 0)} | ${r['cost_usd'] or 0:.2f} |")
    med = statistics.median(all_lines) if all_lines else None
    L += ["", f"줄 길이(전체 {len(all_lines)}줄): 중앙값 {med} · 최대 {max(all_lines, default=None)} · "
          f"60자 초과 {sum(x > 60 for x in all_lines)} · 100자 초과 {sum(x > 100 for x in all_lines)}  ",
          f"추정 비용 합계 ${T['cost_usd']:.2f} (단가: mincore.summarize.PRICES)  ",
          f"표시(flags): {dict(flags) or '없음'}", ""]
    L += ["## 검증 실패 사유 (제외 카드 기준, 마지막 시도)", ""]
    L += [f"- {k}: {v}" for k, v in reasons.most_common()] or ["- 없음"]
    L += ["", "## 재시도 사유 (모든 실패 시도, 통과한 카드 포함)", ""]
    L += [f"- {k}: {v}" for k, v in retry.most_common(15)] or ["- 없음"]
    L.append("")
    md = folder / "regen_report.md"
    md.write_text("\n".join(L), encoding="utf-8")
    (folder / "regen_report.json").write_text(json.dumps(
        {"service": service, "issues": issues, "total": dict(T), "operational": dict(O),
         "fail_reasons": dict(reasons), "retry_reasons": dict(retry), "flags": dict(flags),
         "line_len": {"lines": len(all_lines), "median": med, "max": max(all_lines, default=None)}}, ensure_ascii=False, indent=1), encoding="utf-8")
    return md


def write_review(issue_path: str | Path, md_path: str | Path, golden_path: str | Path, n: int = 30) -> tuple[Path, Path]:
    d = json.loads(Path(issue_path).read_text(encoding="utf-8"))
    cards = d["cards"][:n]
    L = [f"# 요약 비교 — {d['service'].upper()} {d['week_of']} ({len(cards)}건)", "",
         f"v2 모델 {d['model']} · 지시문 {d.get('prompt_version')} (`{d.get('prompt_sha256', '')[:12]}`)", "",
         "| # | 기사 | 본문 | v1 요약 | v2 요약 | 비교 모델 | v1 시사점 | v2 시사점 |",
         "|---:|---|---|---|---|---|---|---|"]
    golden = []
    for i, c in enumerate(cards, 1):
        v1, s, cmp = c.get("v1") or {}, c.get("summary") or {}, c.get("compare") or {}
        cs = cmp.get("summary") or {}
        L.append(f"| {i} | [{_cell(c['title'])[:80]}]({c.get('url') or c.get('gnews_url') or ''}) | "
                 f"{c['body']['status']} {c['body']['chars']}자 | {_cell(v1.get('korean_summary'))} | "
                 f"{_cell(s.get('lines'))}{' · 수치: ' + _cell(s.get('key_figures')) if s.get('key_figures') else ''} | "
                 f"{_cell(cs.get('lines')) if cmp.get('ok') else _cell(cmp.get('errors')) or '-'} | "
                 f"{_cell(v1.get('oda_relevance'))} | {_cell(s.get('oda_relevance'))} |")
        golden.append({
            "card_id": c["card_id"], "title": c["title"], "url": c.get("url"),
            "body_status": c["body"]["status"], "body_sha256": c["body"]["sha256"],
            "model_output": s, "compare_output": cs if cmp.get("ok") else None,
            "v1_output": {k: v1.get(k) for k in ("korean_summary", "oda_relevance", "english_phrases") if k in v1},
            "corrected": None, "verdict": None, "notes": ""})
    L += ["", "※ 골든셋 초안: 위 순서대로 `golden_draft.json`의 `corrected`(교정 요약)·`verdict`(ok/fix/drop)를 채우면 "
          "이후 자동 채점 기준이 된다.", ""]
    mp, gp = Path(md_path), Path(golden_path)
    mp.parent.mkdir(parents=True, exist_ok=True)
    gp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text("\n".join(L), encoding="utf-8")
    gp.write_text(json.dumps({"service": d["service"], "week_of": d["week_of"], "model": d["model"],
                              "prompt_sha256": d.get("prompt_sha256"), "items": golden},
                             ensure_ascii=False, indent=1), encoding="utf-8")
    return mp, gp


def main(argv=None):
    ap = argparse.ArgumentParser(prog="python -m mincore.compare")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("report")
    p.add_argument("--service", required=True)
    p.add_argument("--out", default="data/v2")
    p.add_argument("--test-before")
    p = sub.add_parser("review")
    p.add_argument("--issue", required=True)
    p.add_argument("--md", required=True)
    p.add_argument("--golden", required=True)
    p.add_argument("--n", type=int, default=30)
    a = ap.parse_args(argv)
    if a.cmd == "report":
        print(write_regen_report(a.service, a.out, test_before=a.test_before))
    else:
        print(*write_review(a.issue, a.md, a.golden, a.n))


if __name__ == "__main__":
    main()
