"""합성 데이터만 사용한다(실제 기사 원문을 저장소에 두지 않음)."""

import json

import pytest

from mincore import card, extract, normalize
from mincore.domain_rules import DomainRules, load_rules
from mincore.extract import BodyResult, extract_body, select_quotes, status_for
from mincore.fetch import FetchResult, Fetcher
from mincore.ledger import Ledger
from mincore.report import RunReport, finalize
from mincore.resolve import resolve_many

# ── normalize ──────────────────────────────────────────────


@pytest.mark.parametrize("url,expected", [
    ("https://WWW.Example.com/a/b/?utm_source=x&id=3#frag", "https://example.com/a/b?id=3"),
    ("https://example.com/news/123/amp", "https://example.com/news/123"),
    ("https://example.com/news/123/?fbclid=zz", "https://example.com/news/123"),
    ("https://example.com/", "https://example.com/"),
])
def test_canonical_url(url, expected):
    assert normalize.canonical_url(url) == expected


def test_title_norm_strips_publisher_tail_and_punct():
    assert normalize.title_norm("Korea, Uzbekistan sign MoU on minerals - The Korea Times") == \
        "korea uzbekistan sign mou on minerals"
    # 꼬리를 떼면 너무 짧아지는 경우는 유지
    assert normalize.title_norm("Nickel - Reuters") == "nickel reuters"


@pytest.mark.parametrize("s,expected", [
    ("2026-09-15", "2026-09-15"),
    ("2026-10-11T05:10:25+00:00", "2026-10-11"),
    ("Tue, 15 Sep 2026 14:59:28 +0000", "2026-09-15"),
    ("garbage", None), (None, None),
])
def test_norm_date(s, expected):
    assert normalize.norm_date(s) == expected


def test_card_id_stable_12_hex():
    cid = normalize.card_id("https://example.com/a")
    assert len(cid) == 12 and cid == normalize.card_id("https://example.com/a")


def test_clean_html_text_gnews_snippet():
    s = '<a href="https://news.google.com/x">Title here</a>&nbsp;&nbsp;<font>Source</font>'
    assert normalize.clean_html_text(s) == "Title here Source"


# ── resolve ────────────────────────────────────────────────

GN = "https://news.google.com/rss/articles/CBMiXYZ?oc=5"


def test_resolve_retries_then_succeeds():
    calls = []

    def decoder(urls, interval):
        calls.append(list(urls))
        if len(calls) < 3:
            return [{"success": False, "message": "ssl"} for _ in urls]
        return [{"success": True, "decoded_url": "https://pub.example/a"} for _ in urls]

    sleeps = []
    out = resolve_many(["https://direct.example/x", GN], decoder=decoder, sleep=sleeps.append, log=lambda m: None)
    assert out[0].method == "direct"
    assert out[1].method == "decoder" and out[1].url == "https://pub.example/a" and out[1].attempts == 3
    assert len(calls) == 3 and all(c == [GN] for c in calls)
    assert len(sleeps) == 2 and sleeps[0] < sleeps[1]          # 지수 대기


def test_resolve_falls_back_to_redirect_then_failed():
    dec = lambda urls, i: [{"success": False} for _ in urls]
    out = resolve_many([GN, GN + "2"], decoder=dec, follow=lambda u: "https://pub.example/r" if u == GN else None,
                       sleep=lambda s: None, log=lambda m: None)
    assert out[0].method == "redirect" and out[0].url == "https://pub.example/r"
    assert out[1].method == "failed" and out[1].url is None and out[1].attempts == 4


# ── fetch ──────────────────────────────────────────────────

class FakeResp:
    def __init__(self, status, text="", url=None, headers=None):
        self.status_code, self.text, self.url, self.headers = status, text, url, headers or {}


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.urls = []

    def get(self, url, **kw):
        self.urls.append(url)
        r = self.responses.pop(0)
        if isinstance(r, Exception):
            raise r
        r.url = r.url or url
        return r


def test_fetcher_retries_429_honoring_retry_after():
    sess = FakeSession([FakeResp(429, headers={"Retry-After": "7"}), FakeResp(200, "ok")])
    sleeps = []
    f = Fetcher(session=sess, sleep=sleeps.append, clock=lambda: 0.0)
    r = f.get("https://a.example/x")
    assert r.status == 200 and r.retries == 1 and 7.0 in sleeps


def test_fetcher_domain_interval():
    t = [0.0]
    sleeps = []
    f = Fetcher(min_interval=0.5, session=FakeSession([FakeResp(200), FakeResp(200)]),
                sleep=sleeps.append, clock=lambda: t[0])
    f.get("https://a.example/1")
    f.get("https://a.example/2", min_interval=15)
    assert sleeps == [15.0]


# ── extract ────────────────────────────────────────────────

def article_html(n_paragraphs, og="An og description of the story."):
    # trafilatura는 동일 문단을 중복 제거하므로 문단마다 내용을 달리한다
    para = ("Paragraph {i}: the ministry said lithium output in region {i} rose 12% to 4,500 tonnes, "
            "according to officials. Analysts in district {i} expect further growth as refining capacity expands. ")
    body = "".join(f"<p>{para.format(i=i)}</p>" for i in range(n_paragraphs))
    return (f'<html><head><title>T</title><meta property="og:description" content="{og}">'
            f'</head><body><article><h1>Headline</h1>{body}</article></body></html>')


class StubFetcher:
    def __init__(self, mapping):
        self.mapping, self.calls = mapping, []

    def get(self, url, min_interval=None, headers=None):
        self.calls.append(url)
        status, text = self.mapping.get(url, (404, ""))
        return FetchResult(url, final_url=url, status=status, text=text)


@pytest.mark.parametrize("n,expected", [(0, "title_only"), (1, "lead_only"), (299, "lead_only"),
                                        (300, "partial"), (799, "partial"), (800, "full")])
def test_status_thresholds(n, expected):
    assert status_for(n) == expected


def test_extract_full_with_trafilatura(tmp_path):
    url = "https://news.example/a"
    res = extract_body(url, StubFetcher({url: (200, article_html(8))}), cache_dir=tmp_path)
    assert res.status == "full" and res.source == "trafilatura" and res.block_type is None
    assert (tmp_path / f"{res.sha256}.txt").exists()
    rec = res.to_record()
    assert "text" not in rec and rec["chars"] == res.chars


def test_extract_blocked_uses_amp_strategy():
    rules = DomainRules({"blocked.example": {"strategies": ["amp", "og"], "amp_templates": ["{url}/amp"]}})
    url = "https://blocked.example/story"
    f = StubFetcher({url: (403, "Access denied"), url + "/amp": (200, article_html(6))})
    res = extract_body(url, f, rules)
    assert res.source == "amp" and res.status in ("full", "partial")
    assert res.block_type == "bot_block"            # 1차 차단 기록은 남는다
    assert res.strategies_tried == ["amp"]


def test_extract_og_fallback_when_body_empty():
    url = "https://pay.example/x"
    html = '<html><head><meta property="og:description" content="Short teaser about nickel."></head><body></body></html>'
    res = extract_body(url, StubFetcher({url: (200, html)}))
    assert res.source == "og_description" and res.status == "lead_only" and res.block_type == "parse_fail"


def test_extract_skip_fetch_uses_rss_summary():
    rules = DomainRules({"reuters.com": {"skip_fetch": True, "strategies": ["rss_summary"]}})
    f = StubFetcher({})
    res = extract_body("https://www.reuters.com/a", f, rules, title="Korea signs deal",
                       rss_summary="<p>Korea and Kazakhstan signed an agreement on uranium supply on Monday.</p>")
    assert f.calls == [] and res.block_type == "skipped" and res.source == "rss_summary"


def test_rss_summary_that_only_repeats_title_is_ignored():
    rules = DomainRules({"reuters.com": {"skip_fetch": True}})
    res = extract_body("https://www.reuters.com/a", StubFetcher({}), rules,
                       title="Korea signs deal - Reuters", rss_summary='<a href="x">Korea signs deal</a> Reuters')
    assert res.status == "title_only" and res.source == "none"


def test_presscorner_api_strategy():
    page = "https://ec.europa.eu/commission/presscorner/detail/en/speech_26_1908"
    api = ("https://ec.europa.eu/commission/presscorner/api/documents?reference=SPEECH/26/1908&language=en")
    payload = json.dumps({"docuLanguageResource": {"htmlContent": "<p>" + "Raw materials matter. " * 40 + "</p>"}})
    res = extract_body(page, StubFetcher({page: (200, "<html><body></body></html>"), api: (200, payload)}), load_rules())
    assert res.source == "presscorner_api" and res.status == "full"


def test_select_quotes_priority():
    text = ("The weather was mild and the ceremony began on time with many guests in attendance. "
            "“This partnership will secure our supply chains for decades,” the minister said today. "
            "Cobalt refining will be discussed by the ministry with partner companies later on. "
            "Exports reached US$15.4 billion last year, up 8% from the previous period overall.")
    q = select_quotes(text)
    assert q[0].startswith("Exports reached")          # ① 숫자·단위
    assert q[1].startswith("“This partnership")   # ② 따옴표
    assert sum(map(len, q)) <= 300 and len(q) <= 2


def test_load_rules_matches_subdomain():
    rules = load_rules()
    assert rules.for_url("https://www.cnbcindonesia.com/news/1").strategies[0] == "amp"
    assert rules.for_url("https://finance.biggo.com/news/x").min_interval == 15
    assert rules.for_url("https://unknown.example/").strategies == []


# ── card ───────────────────────────────────────────────────

def test_build_card_valid_and_no_full_text():
    body = BodyResult(status="full", chars=3000, sha256="a" * 64, source="trafilatura",
                      quotes=["Output rose 12% to 4,500 tonnes."], text="x" * 3000)
    c = card.build_card("kcmo", title="Lithium deal - Daily News", url="https://www.pub.example/a/?utm_x=1",
                        gnews_url="https://news.google.com/rss/articles/abc", published="2026-10-10",
                        score=12, body=body, ext={"country": "Tanzania"})
    assert card.validate(c) == []
    assert c["url"] == "https://pub.example/a" and c["summary_input_max_chars"] == 4000
    assert "x" * 1001 not in json.dumps(c)
    extra = card.legacy_extra(c)
    assert extra["body_status"] == "full" and extra["resolved_url"] == c["url"]


def test_validate_flags_full_text():
    c = card.build_card("cmw", title="t", url="https://a.example/x", body=BodyResult())
    c["ext"]["cmw"]["text"] = "y" * 5000
    errs = card.validate(c)
    assert any("forbidden" in e for e in errs) and any("too long" in e for e in errs)


# ── ledger ─────────────────────────────────────────────────

def test_ledger_check_and_prune(tmp_path):
    led = Ledger(tmp_path / "l.json")
    assert led.add(url="https://www.pub.example/a/", title="Korea and Laos sign copper MoU - Vientiane Times",
                   service="kcmo", week_of="2026-10-04")
    assert led.check("https://pub.example/a?utm_source=x", "anything") == "url"
    assert led.check("https://other.example/b", "Korea and Laos sign copper MoU - Other") == "title"
    assert led.check("https://other.example/b", "Korea and Laos sign copper MoUs") == "title_similar"
    assert led.check("https://other.example/b", "Completely different story about graphite") is None
    led.save()
    led2 = Ledger.load(tmp_path / "l.json")
    for i in range(30):
        led2.add(url=f"https://p.example/{i}", title=f"story {i}", service="kcmo", week_of=f"2027-{i // 4 + 1:02d}-01")
    assert led2.prune(keep_weeks=2) > 0 and len(led2.weeks()) == 2


def test_ledger_ignores_unresolved_gnews(tmp_path):
    led = Ledger(tmp_path / "l.json")
    assert not led.add(url=GN, title="x", service="kcmo", week_of="2026-10-04")


# ── report ─────────────────────────────────────────────────

def test_report_write_and_finalize(tmp_path):
    rep = RunReport("kcmo", run_date="2026-10-14")
    rep.count("fetched", 100)
    rep.count("qualified", 60)
    rep.resolved("decoder")
    rep.resolved("direct")
    rep.excluded("url")
    rep.body("a.example", {"status": "full", "source": "trafilatura", "block_type": None})
    rep.body("b.example", {"status": "lead_only", "source": "og_description", "block_type": "bot_block",
                           "strategies_tried": ["amp"]})
    p = rep.write(tmp_path)
    md = p.with_suffix(".md").read_text(encoding="utf-8")
    assert "1/2 (50.0%)" in md and "b.example" in md
    raw = tmp_path / "raw.json"
    cards = tmp_path / "cards.json"
    raw.write_text(json.dumps({"articles": [{}, {}, {}]}), encoding="utf-8")
    cards.write_text(json.dumps({"articles": [{}, {}]}), encoding="utf-8")
    d = finalize(p, raw, cards)
    assert d["summarize"]["failed"] == 1
