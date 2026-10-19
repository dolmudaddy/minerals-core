"""리뉴얼 5단계: 요약·재생성·비교. 합성 데이터와 API 스텁만 사용."""

import hashlib
import json

import pytest

from mincore import compare, regen
from mincore.card import build_card
from mincore.extract import BodyResult
from mincore.resolve import Resolution
from mincore.summarize import (CallOut, estimate_cost, has_korea_evidence, json_failure, load_prompt,
                               mode_for, summarize_card, to_legacy, validate_summary)

PROMPT = """---
version: v2.0-test
service: kcmo
categories:
  policy_regulation: 정책·규제
  supply_chain_trade: 공급망·무역
fields: [key_phrases, oda_relevance]
---
## common
입력에 없는 정보는 쓰지 않는다.

## mode:body
3줄 요약.

## mode:lead
2줄.

## mode:title
1줄.

## output
JSON만 출력.
"""

BODY = ("Indonesia targets nickel exports of USD45 billion in 2026, the ministry said. "
        "Output rose 12% to 4,500 tonnes. Korea's POSCO signed an MoU with the ministry. ") * 4


@pytest.fixture
def prompt(tmp_path):
    p = tmp_path / "summarize_v2.md"
    p.write_text(PROMPT, encoding="utf-8")
    return load_prompt(p)


def card_with(status, chars=None, quotes=None):
    body = {"status": status, "chars": chars if chars is not None else len(BODY), "sha256": "a" * 64,
            "source": "trafilatura", "block_type": None, "http_status": 200, "extracted_date": None,
            "quotes": quotes or ["Output rose 12% to 4,500 tonnes."]}
    return build_card("kcmo", title="Indonesia targets nickel exports - Antara", url="https://pub.example/a",
                      source_name="Antara", published="2026-10-16", body=body, score=12,
                      ext={"country": "Indonesia", "legacy_id": "abc123", "source_language": "en"})


def good_body_out(**over):
    out = {"lines": ["인도네시아는 2026년 니켈 수출 목표를 USD45 billion으로 제시했음",
                     "생산량은 12% 늘어 4,500톤에 이르렀다고 부처가 밝혔음",
                     "한국 POSCO가 해당 부처와 MoU를 체결했음"],
           "key_figures": ["USD45 billion", "4,500 tonnes"], "category": "supply_chain_trade",
           "tags": ["인도네시아", "니켈"], "key_phrases": [{"phrase": "nickel exports", "meaning": "니켈 수출"}],
           "oda_relevance": "POSCO-부처 MoU로 한국 기업의 니켈 공급망 참여 근거가 확인됨"}
    out.update(over)
    return out


class Stub:
    def __init__(self, *outs):
        self.outs, self.calls = list(outs), []

    def __call__(self, model, system, messages, max_tokens):
        self.calls.append({"model": model, "system": system, "messages": [dict(m) for m in messages]})
        o = self.outs.pop(0) if len(self.outs) > 1 else self.outs[0]
        return o if isinstance(o, str) else json.dumps(o, ensure_ascii=False)


def test_prompt_sha_ignores_crlf(tmp_path, prompt):
    p = tmp_path / "crlf.md"
    p.write_bytes(PROMPT.replace("\n", "\r\n").encode("utf-8"))
    assert load_prompt(p).sha256 == prompt.sha256


def test_load_prompt_sections_and_sha(prompt):
    assert prompt.version == "v2.0-test" and len(prompt.sha256) == 64
    assert "3줄 요약" in prompt.system("body") and "3줄 요약" not in prompt.system("title")
    assert prompt.fields == ["key_phrases", "oda_relevance"]


@pytest.mark.parametrize("status,mode", [("full", "body"), ("partial", "body"),
                                         ("lead_only", "lead"), ("title_only", "title")])
def test_mode_for(status, mode):
    assert mode_for(card_with(status)) == mode


def test_body_summary_ok_with_quotes_and_provenance(prompt):
    stub = Stub(good_body_out())
    res = summarize_card(card_with("full"), prompt, body_text=BODY, call=stub)
    assert res.ok and res.attempts == 1, res.errors
    assert res.summary["basis"] == "body" and res.summary["quotes"] == ["Output rose 12% to 4,500 tonnes."]
    assert res.provenance["prompt_sha256"] == prompt.sha256 and res.provenance["input_body_status"] == "full"
    assert "본문(앞부분)" in stub.calls[0]["messages"][0]["content"]


def test_converted_number_fails(prompt):
    out = good_body_out(lines=["인도네시아는 니켈 수출 목표를 약 62조원으로 제시했음",
                               "생산량은 12% 늘어 4,500톤에 이르렀다고 부처가 밝혔음",
                               "한국 POSCO가 해당 부처와 MoU를 체결했음"],
                        key_figures=["62조원"])
    errs = validate_summary(out, "body", BODY, prompt, "2026-10-16")
    assert any("입력에 없는 숫자" in e for e in errs) and any("key_figures" in e for e in errs)


def test_number_format_variants_and_published_date_allowed(prompt):
    out = good_body_out(lines=["인도네시아는 10월 16일 니켈 수출 목표를 USD45 billion으로 제시했음",
                               "생산량은 12% 늘어 4500톤에 이르렀다고 부처가 밝혔음",
                               "한국 POSCO가 해당 부처와 MoU를 체결했음"])
    assert validate_summary(out, "body", BODY, prompt, "2026-10-16") == []


def test_refusal_and_line_count(prompt):
    out = good_body_out(lines=["죄송하지만 제공된 정보만으로는 요약할 수 없습니다", "짧음"])
    errs = validate_summary(out, "body", BODY, prompt)
    assert any("거부" in e for e in errs) and any("3줄" in e for e in errs)
    ok = good_body_out(lines=["인도네시아 니켈 수출 목표가 제시됐으나 세부는 확인 불가임",
                              "생산량은 12% 늘어 4,500톤에 이르렀다고 부처가 밝혔음",
                              "한국 POSCO가 해당 부처와 MoU를 체결했음"])
    assert validate_summary(ok, "body", BODY, prompt) == []


def test_lead_mode_blocks_implication_then_retries(prompt):
    bad = {"lines": ["인도네시아 니켈 수출 목표 제시", "부처는 생산 확대를 밝혔음 — 리드 기준"],
           "category": "supply_chain_trade", "tags": ["니켈"], "key_phrases": [],
           "oda_relevance": "한국 협력 확대 계기"}
    good = dict(bad, lines=["인도네시아, 니켈 수출 목표 제시했음", "부처가 생산 확대 방침을 밝혔다는 리드임"],
                oda_relevance="해당 없음")
    stub = Stub(bad, good)
    res = summarize_card(card_with("lead_only", chars=120), prompt, body_text="Indonesia targets nickel.",
                         call=stub)
    assert res.ok and res.attempts == 2
    assert "검증 실패" in stub.calls[1]["messages"][-1]["content"]
    assert res.summary["basis"] == "lead" and res.summary["oda_relevance"] == "해당 없음"
    assert res.summary["quotes"] == [] and res.summary["key_figures"] == []


def test_excluded_after_two_retries(prompt):
    stub = Stub("not json")
    res = summarize_card(card_with("full"), prompt, body_text=BODY, call=stub, fallback_model=None)
    assert not res.ok and res.attempts == 3 and len(stub.calls) == 3
    assert res.errors[0].startswith("JSON 파싱 실패: JSON 객체 없음")


def test_json_failure_causes():
    assert json_failure("") == "JSON 파싱 실패: 빈 응답"
    assert "출력 길이 초과" in json_failure('{"lines": ["a', "max_tokens")
    assert "코드펜스" in json_failure("```json\nnope\n```")
    e = json_failure('{"lines": ["그는 "핵심광물" 협정을 밝혔음"]}')
    assert e.startswith("JSON 파싱 실패: 문법 오류") and "핵심광물" in e


def test_fallback_model_after_primary_fails(prompt):
    def call(model, system, messages, max_tokens):
        if model == "primary":
            return CallOut("not json", "end_turn", 100, 10)
        return CallOut(json.dumps(good_body_out(), ensure_ascii=False), "end_turn", 200, 50)
    res = summarize_card(card_with("full"), prompt, body_text=BODY, call=call, model="primary",
                         fallback_model="claude-haiku-4-5-20251001")
    assert res.ok and res.provenance["model"] == "claude-haiku-4-5-20251001" and res.attempts == 4
    assert res.provenance["flags"]["fallback_from"] == "primary"
    assert res.usage["primary"] == {"calls": 3, "in": 300, "out": 30}
    assert estimate_cost(res.usage) == round(200 / 1e6 * 1.0 + 50 / 1e6 * 5.0, 4)


def test_soft_line_limit_one_retry_then_pass_flagged(prompt):
    long = "인도네시아는 2026년 니켈 수출 목표를 USD45 billion으로 제시했으며 생산량은 12% 늘어 " * 2
    stub = Stub(good_body_out(lines=[long, good_body_out()["lines"][1], good_body_out()["lines"][2]]))
    res = summarize_card(card_with("full"), prompt, body_text=BODY, call=stub, fallback_model=None)
    assert res.ok and res.attempts == 2 and "보완 요청" in stub.calls[1]["messages"][-1]["content"]
    assert res.provenance["flags"]["long_lines"] == 1
    fixed = Stub(good_body_out(lines=[long, *good_body_out()["lines"][1:]]), good_body_out())
    res2 = summarize_card(card_with("full"), prompt, body_text=BODY, call=fixed, fallback_model=None)
    assert res2.ok and "long_lines" not in res2.provenance["flags"]


def test_oda_without_korea_evidence_retried_then_forced(prompt):
    body = BODY.replace("Korea's POSCO signed an MoU with the ministry. ", "")
    assert not has_korea_evidence(body) and has_korea_evidence(BODY)
    out = good_body_out(lines=good_body_out()["lines"][:2] + ["부처는 수출 확대 방침을 유지한다고 밝혔음"],
                        oda_relevance="협력국 니켈 정책 변화는 공급망 협력에 참고가 됨")
    res = summarize_card(card_with("full"), prompt, body_text=body, call=Stub(out), fallback_model=None)
    assert res.ok and res.attempts == 2 and res.summary["oda_relevance"] == "해당 없음"
    assert res.provenance["flags"]["oda_forced"] is True
    ok = summarize_card(card_with("full"), prompt, body_text=BODY, call=Stub(good_body_out()), fallback_model=None)
    assert ok.attempts == 1 and "flags" not in ok.provenance


def test_title_mode_input_has_no_body(prompt):
    out = {"lines": ["인도네시아, 니켈 수출 목표 제시"], "category": "supply_chain_trade", "tags": ["니켈"],
           "key_phrases": [], "oda_relevance": "해당 없음"}
    stub = Stub(out)
    res = summarize_card(card_with("title_only", chars=0), prompt, body_text="", call=stub)
    assert res.ok and "본문(앞부분)" not in stub.calls[0]["messages"][0]["content"]


def test_to_legacy_kcmo(prompt):
    c = card_with("lead_only", chars=100)
    c["summary"] = {"lines": ["a" * 20, "b" * 20], "basis": "lead", "key_figures": [], "quotes": [],
                    "category": "policy_regulation", "tags": ["t"], "key_phrases": [],
                    "oda_relevance": "해당 없음"}
    c["provenance"] = {"summarized_at": "2026-10-19T00:00:00+00:00"}
    lg = to_legacy(c, "kcmo", prompt)
    for k in ("id", "title", "url", "source", "source_language", "country", "tier_weight", "score", "published",
              "korean_summary", "oda_relevance", "category", "category_ko", "tags", "key_phrases",
              "ai_processed", "processed_at"):
        assert k in lg, k
    assert lg["oda_relevance"] == "" and lg["korean_summary"][0].startswith("[본문 미확보] ")
    assert lg["category_ko"] == "정책·규제" and lg["id"] == "abc123" and lg["country"] == "Indonesia"


# ── regen ───────────────────────────────────────────────────

def write_kcmo_v1(d, week, n=3):
    arts = [{"id": f"id{i}", "title": f"Nickel story {i} - Pub", "url": f"https://news.google.com/rss/articles/X{i}",
             "source": "Google News", "source_language": "en", "country": "Indonesia", "tier_weight": 1,
             "score": 10, "published": week, "korean_summary": ["v1"], "oda_relevance": "v1 시사점",
             "category": "policy_regulation", "tags": ["v1"], "key_phrases": []} for i in range(n)]
    (d / f"cards_{week}.json").write_text(json.dumps({"week_of": week, "articles": arts}), encoding="utf-8")
    raw = [dict(a, summary_raw=f"Indonesia nickel lead {a['id']} with extra description text") for a in arts]
    raw.append({"id": "dropped", "summary_raw": ""})            # raw − cards = v1 요약 실패 1
    (d / f"raw_{week}.json").write_text(json.dumps({"articles": raw}), encoding="utf-8")


def fake_resolver(urls):
    return [Resolution(u, None if u.endswith("X2") else f"https://pub.example/{u[-2:]}",
                       "failed" if u.endswith("X2") else "decoder") for u in urls]


def fake_extractor(url, fetcher, rules, *, title, rss_summary, cache_dir, ctx):
    b = BodyResult()
    b.status, b.chars, b.text, b.source = "full", len(BODY), BODY, "trafilatura"
    b.sha256 = hashlib.sha256(BODY.encode()).hexdigest()
    b.quotes = ["Output rose 12% to 4,500 tonnes."]
    return b


def lead_or_body_stub(model, system, messages, max_tokens):
    if "본문(앞부분)" in messages[0]["content"]:
        return json.dumps(good_body_out(), ensure_ascii=False)
    return json.dumps({"lines": ["인도네시아 니켈 관련 보도가 나왔음", "리드만 확보되어 세부는 확인 불가임"],
                       "category": "policy_regulation", "tags": ["니켈"], "key_phrases": [],
                       "oda_relevance": "해당 없음"}, ensure_ascii=False)


def test_regen_run_resume_and_v1_untouched(tmp_path, prompt):
    data, out = tmp_path / "data", tmp_path / "v2"
    data.mkdir()
    for w in ("2026-05-13", "2026-10-11", "2026-10-18"):
        write_kcmo_v1(data, w)
    before = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in data.iterdir()}
    kw = dict(call=lead_or_body_stub, resolver=fake_resolver, extractor=fake_extractor, rules=None,
              cache_dir=tmp_path / "cache", log=lambda m: None, compare_model="sonnet-x", compare_n=2)
    done = regen.run("kcmo", data, out, prompt.path, date_from="2026-10-01", max_issues=1, **kw)
    assert done == ["2026-10-11"]
    done2 = regen.run("kcmo", data, out, prompt.path, date_from="2026-10-01", resume=True, **kw)
    assert done2 == ["2026-10-18"]
    # 모델이 바뀌면 --resume이어도 다시 만든다
    done3 = regen.run("kcmo", data, out, prompt.path, date_from="2026-10-18", resume=True,
                      **dict(kw, model="other-model"))
    assert done3 == ["2026-10-18"]
    doc = json.loads((out / "kcmo" / "cards_v2_2026-10-11.json").read_text(encoding="utf-8"))
    assert doc["stats"]["cards"] == 3 and doc["stats"]["body_status"] == {"full": 2, "lead_only": 1}
    assert doc["v1_metrics"] == {"articles": 3, "original_link": 0, "body_based": 0, "failed": 1}
    c0 = doc["cards"][0]
    assert c0["summary"]["basis"] == "body" and c0["legacy"]["korean_summary"] and c0["v1"]["oda_relevance"]
    assert c0["compare"]["model"] == "sonnet-x" and "compare" not in doc["cards"][2]
    lead = doc["cards"][2]
    assert lead["url"] is None and lead["summary"]["basis"] == "lead"
    assert all(len(s) <= 1000 for s in json.dumps(doc, ensure_ascii=False).split('"'))   # 본문 전문 없음
    state = json.loads((out / "kcmo" / "regen_state.json").read_text(encoding="utf-8"))
    assert set(state) == {"2026-10-11", "2026-10-18"}
    assert before == {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in data.iterdir()}

    md = compare.write_regen_report("kcmo", out, test_before="2026-05-24").read_text(encoding="utf-8")
    assert "진단서 비교표" in md and "| 2026-10-11 |" in md and "4/6 (66.7%)" in md and "줄 중앙값" in md
    mp, gp = compare.write_review(out / "kcmo" / "cards_v2_2026-10-11.json", tmp_path / "r.md",
                                  tmp_path / "g.json", n=30)
    g = json.loads(gp.read_text(encoding="utf-8"))
    assert len(g["items"]) == 3 and g["items"][0]["corrected"] is None
    assert "v1 요약" in mp.read_text(encoding="utf-8")


def test_regen_excludes_failed_cards(tmp_path, prompt):
    data, out = tmp_path / "data", tmp_path / "v2"
    data.mkdir()
    write_kcmo_v1(data, "2026-10-18", n=2)
    regen.run("kcmo", data, out, prompt.path, call=lambda *a: "sorry", resolver=fake_resolver,
              extractor=fake_extractor, rules=None, cache_dir=tmp_path / "c", log=lambda m: None)
    doc = json.loads((out / "kcmo" / "cards_v2_2026-10-18.json").read_text(encoding="utf-8"))
    assert doc["total"] == 0 and len(doc["excluded"]) == 2 and doc["excluded"][0]["attempts"] == 6   # 주+폴백
    assert {x["model"] for x in doc["excluded"][0]["retry_log"]} == {"claude-sonnet-5-5", "claude-haiku-4-5-20251001"}
    assert doc["stats"]["fail_reason"] == {"JSON 파싱 실패": 2}


def test_regen_cmw_reads_week_files(tmp_path, prompt):
    data, out = tmp_path / "data", tmp_path / "v2"
    data.mkdir()
    arts = [{"id": "c1", "title": "Rare earth story", "url": "https://pub.example/c1", "source": "Feed",
             "tier_weight": 2, "summary_raw": "desc", "published": "2026-10-12", "score": 5,
             "korean_summary": ["v1"], "english_phrases": [], "tags": [], "category": "research",
             "ai_processed": False}]
    (data / "week-2026-10-12.json").write_text(json.dumps({"articles": arts}), encoding="utf-8")
    items = regen.load_v1("cmw", data / "week-2026-10-12.json")
    assert items[0]["summary_raw"] == "desc" and items[0]["ext"]["summary_raw"] == "desc"
    assert regen.v1_metrics("cmw", data / "week-2026-10-12.json")["failed"] == 1
    assert [d for d, _ in regen.discover("cmw", data)] == ["2026-10-12"]


def test_regen_stops_on_resolve_outage(tmp_path, prompt):
    data, out = tmp_path / "data", tmp_path / "v2"
    data.mkdir()
    write_kcmo_v1(data, "2026-10-11")
    write_kcmo_v1(data, "2026-10-18")
    all_fail = lambda urls: [Resolution(u, None, "failed", error="ssl") for u in urls]
    logs = []
    done = regen.run("kcmo", data, out, prompt.path, call=lead_or_body_stub, resolver=all_fail,
                     extractor=fake_extractor, rules=None, cache_dir=tmp_path / "c", log=logs.append)
    assert done == [] and not (out / "kcmo" / "cards_v2_2026-10-11.json").exists()
    assert any("중단" in m for m in logs)


def test_korean_big_unit_conversion_fails(prompt):
    title = "Bahlil Targetkan Hilirisasi Indonesia Capai 618 Miliar Dolar"
    out = {"lines": ["바흘릴 장관이 하류산업화 목표를 618억 달러로 설정했음"], "category": "policy_regulation",
           "tags": ["니켈"], "key_phrases": [], "oda_relevance": "해당 없음"}
    errs = validate_summary(out, "title", title, prompt)
    assert any("단위 환산" in e for e in errs)
    out["lines"] = ["바흘릴 장관이 하류산업화 목표를 618 Miliar 달러로 설정했음"]
    assert validate_summary(out, "title", title, prompt) == []
    # 한국어 원문의 '억'은 그대로면 통과
    ko = "산업부는 1,616억원을 투자한다고 밝혔다"
    out["lines"] = ["산업부가 희토류 자석 공급망에 1,616억원 투자를 밝혔음"]
    assert validate_summary(out, "title", ko, prompt) == []
