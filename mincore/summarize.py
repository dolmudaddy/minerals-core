"""본문 기반 요약 (리뉴얼 5단계).

서비스와 무관한 엔진만 둔다: 지시문 로딩, 입력 조립, API 호출, 출력 검증, 재시도, provenance,
legacy 카드 변환. 지시문(독자·카테고리·서비스 필드)은 각 서비스 저장소의 prompts/summarize_v2.md.

body.status → 모드
  full / partial → body  : 3줄 요약 + 핵심 수치(원문 표기) + 인용(추출 단계 quotes에서 선택)
  lead_only      → lead  : 제목 번역 1줄 + 리드 1줄, 시사점 금지
  title_only     → title : 제목 번역 1줄
검증 실패 → 실패 사유를 붙여 최대 2회 재요청 → 그래도 실패면 폴백 모델로 같은 절차 → 그래도 실패면
카드 제외(호출하는 쪽이 보고에 기록).
소프트 점검(검증 통과 후): 줄 길이 100자 초과, 입력에 한국 근거가 없는 oda_relevance → 1회 보완 요청.
보완 뒤에도 줄이 길면 통과시키고 flags에 표시. 근거 없는 oda_relevance는 '해당 없음'으로 고치고 표시.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import yaml

from .card import SUMMARY_INPUT_MAX_CHARS

DEFAULT_MODEL = "claude-sonnet-5-5"
FALLBACK_MODEL = "claude-haiku-4-5-20251001"     # 주 모델이 재시도 후에도 실패할 때만
MAX_TOKENS = 2048
LINE_SOFT_MAX = 100
# 비용 추정용 단가 (USD / 1M 토큰: 입력, 출력). 보고서의 '추정 비용'에만 쓴다
PRICES = {"claude-sonnet-5-5": (3.0, 15.0), "claude-haiku-4-5-20251001": (1.0, 5.0)}
VALIDATION_RETRIES = 2
API_RETRY_WAITS = (5.0, 10.0)

MODE_OF = {"full": "body", "partial": "body", "lead_only": "lead", "title_only": "title"}
LINES_OF = {"body": 3, "lead": 2, "title": 1}
MIN_LINE = {"body": 15, "lead": 15, "title": 8}
NOT_APPLICABLE = "해당 없음"
# 시사점 필드: 본문 근거 있을 때만 쓰고 없으면 '해당 없음'. KCMO oda_relevance(한국 근거 점검 추가), CMW implication
IMPLICATION_FIELDS = ("oda_relevance", "implication")
BASIS_LABEL = {"lead": "[본문 미확보] ", "title": "[제목 기반] "}

REFUSAL = re.compile(
    r"죄송|사과드|요약할 수 없|요약이 불가|제공된 (?:정보|내용)만으로는|AI(?:로서| 언어 모델)|"
    r"I cannot|I can't|I'm sorry|I am sorry|As an AI|unable to (?:summari[sz]e|provide)", re.I)
NUM = re.compile(r"\d[\d.,]*\d|\d")
# 한국어 큰 수 단위: 원문에 같은 '숫자+단위'가 없으면 환산으로 본다 (예: 618 miliar → 618억, bilioni moja → 10억)
KR_UNIT = re.compile(r"(\d[\d.,]*)\s*(만|억|조)")
# oda_relevance 근거 점검: 입력(제목·본문)에 한국 관련 표기가 하나도 없으면 시사점을 쓸 근거가 없다고 본다
KOREA_EVIDENCE = re.compile(
    r"korea|한국|대한민국|韓國|韩国|h[àa]n qu[ốo]c|korsel|солонгос|корея|koreya|ເກົາຫຼີ|seoul|서울|"
    r"kiat|koica|kigam|komir|kores|kexim|edcf|kotra|kepco|posco|samsung|hyundai|hanwha|lotte|doosan|"
    r"ecopro|motie|산업통상자원부|산업부|외교부|코이카", re.I)
KOREA_EVIDENCE_CS = re.compile(r"(?<![A-Za-z])(?:LG|SK|LS)(?![A-Za-z])")


def has_korea_evidence(text: str) -> bool:
    return bool(KOREA_EVIDENCE.search(text or "") or KOREA_EVIDENCE_CS.search(text or ""))


def estimate_cost(usage: dict) -> float:
    """usage = {model: {"calls", "in", "out"}} → 추정 USD (단가표에 없는 모델은 0)."""
    tot = 0.0
    for m, u in (usage or {}).items():
        pi, po = PRICES.get(m, (0.0, 0.0))
        tot += u.get("in", 0) / 1e6 * pi + u.get("out", 0) / 1e6 * po
    return round(tot, 4)


# ── 지시문 ────────────────────────────────────────────────────
@dataclass
class Prompt:
    path: str
    version: str
    sha256: str
    meta: dict
    sections: dict[str, str]

    @property
    def categories(self) -> dict[str, str]:
        return self.meta.get("categories") or {}

    @property
    def fields(self) -> list[str]:
        """서비스 고유 출력 필드 (예: key_phrases, oda_relevance / english_phrases, implication)."""
        return list(self.meta.get("fields") or [])

    def system(self, mode: str) -> str:
        parts = [self.sections.get("common", ""), self.sections.get(f"mode:{mode}", ""),
                 self.sections.get("output", "")]
        return "\n\n".join(p.strip() for p in parts if p.strip())


def load_prompt(path: str | Path) -> Prompt:
    """--- YAML 머리말 --- 뒤에 '## common', '## mode:body|lead|title', '## output' 절."""
    p = Path(path)
    raw = p.read_text(encoding="utf-8").replace("\r\n", "\n")   # 줄바꿈 차이(Windows 체크아웃)로 sha가 바뀌지 않게
    m = re.match(r"^---\n(.*?)\n---\n(.*)$", raw, re.S)
    if not m:
        raise ValueError(f"{p}: YAML 머리말(---)이 없음")
    meta = yaml.safe_load(m.group(1)) or {}
    sections, cur = {}, None
    for line in m.group(2).split("\n"):
        h = re.match(r"^## (\S+)\s*$", line)
        if h:
            cur = h.group(1)
            sections[cur] = ""
        elif cur:
            sections[cur] += line + "\n"
    for need in ("common", "mode:body", "mode:lead", "mode:title", "output"):
        if need not in sections:
            raise ValueError(f"{p}: '## {need}' 절이 없음")
    return Prompt(str(path).replace("\\", "/"), str(meta.get("version", "")),
                  hashlib.sha256(raw.encode("utf-8")).hexdigest(), meta, sections)


# ── 입력 ──────────────────────────────────────────────────────
def read_body(cache_dir: str | Path, sha256: str | None) -> str:
    if not sha256:
        return ""
    f = Path(cache_dir) / f"{sha256}.txt"
    return f.read_text(encoding="utf-8") if f.exists() else ""


def mode_for(card: dict) -> str:
    return MODE_OF.get((card.get("body") or {}).get("status"), "title")


def build_input(card: dict, body_text: str, rss_summary: str | None, mode: str) -> str:
    ext = next(iter((card.get("ext") or {}).values()), {}) or {}
    ctx = {k: v for k, v in ext.items() if k in ("country", "search_query") and v}
    L = [f"제목: {card.get('title', '')}",
         f"매체: {(card.get('source') or {}).get('name') or ''}",
         f"발행일: {card.get('published') or '확인 불가'}"]
    if ctx:
        L.append("맥락: " + ", ".join(f"{k}={v}" for k, v in ctx.items()))
    L.append(f"본문 상태: {(card.get('body') or {}).get('status')}")
    if mode == "body":
        L += ["", "본문(앞부분):", '"""', body_text[:SUMMARY_INPUT_MAX_CHARS], '"""']
    elif mode == "lead":
        lead = body_text or rss_summary or ""
        L += ["", "리드:", '"""', lead[:1000], '"""']
    return "\n".join(L)


# ── 검증 ──────────────────────────────────────────────────────
def _num_keys(text: str) -> set[str]:
    """숫자 토큰 → 구두점 제거 키. 1.234,5 / 1,234.5 표기 차이를 흡수한다."""
    out = set()
    for tok in NUM.findall(text or ""):
        k = re.sub(r"[.,]", "", tok).lstrip("0") or "0"
        out.add(k)
    return out


def _ws(s: str) -> str:
    return re.sub(r"\s+", " ", s or "").strip().lower()


def implication_field(prompt: Prompt) -> str | None:
    return next((f for f in IMPLICATION_FIELDS if f in prompt.fields), None)


def validate_summary(out: dict, mode: str, source_text: str, prompt: Prompt,
                     published: str | None = None) -> list[str]:
    errs = []
    if not isinstance(out, dict):
        return ["JSON 객체가 아님"]
    lines = out.get("lines")
    need = ["lines", "category", "tags"] + prompt.fields + (["key_figures"] if mode == "body" else [])
    errs += [f"필수 키 없음: {k}" for k in need if k not in out]
    if not isinstance(lines, list) or not all(isinstance(x, str) for x in lines):
        errs.append("lines는 문자열 목록이어야 함")
        lines = []
    elif len(lines) != LINES_OF[mode]:
        errs.append(f"lines는 {LINES_OF[mode]}줄이어야 함 (현재 {len(lines)}줄)")
    short = [x for x in lines if len(x.strip()) < MIN_LINE[mode]]
    if short:
        errs.append(f"너무 짧은 줄 {len(short)}개 (최소 {MIN_LINE[mode]}자)")
    if prompt.categories and out.get("category") not in prompt.categories:
        errs.append(f"category 값 오류: {out.get('category')!r}")
    tags = out.get("tags")
    if not isinstance(tags, list) or not tags or not all(isinstance(t, str) and t.strip() for t in tags):
        errs.append("tags는 비어 있지 않은 문자열 목록이어야 함")

    for k in ("key_phrases", "english_phrases"):
        if k in prompt.fields and k in out:
            v = out[k]
            if not isinstance(v, list) or not all(isinstance(x, dict) and x.get("phrase") for x in v):
                errs.append(f"{k}는 {{phrase, meaning}} 목록이어야 함")

    impl = implication_field(prompt)
    if impl and impl in out:
        v = out.get(impl)
        if not isinstance(v, str) or not v.strip():
            errs.append(f"{impl}가 비어 있음")
        elif mode != "body" and v.strip() != NOT_APPLICABLE:
            errs.append(f"본문 미확보 카드는 {impl}를 '{NOT_APPLICABLE}'로 둬야 함")
    impl_text = [str(out.get(impl, ""))] if impl else []

    figs = out.get("key_figures") or []
    if mode == "body":
        if not isinstance(figs, list) or not all(isinstance(x, str) for x in figs):
            errs.append("key_figures는 문자열 목록이어야 함")
            figs = []
        src = _ws(source_text)
        bad = [f for f in figs if _ws(f) not in src]
        if bad:
            errs.append(f"key_figures가 원문 표기와 다름(환산·변형 금지): {bad[:3]}")

    # 거부·사과 문구 ("확인 불가"는 허용)
    text_out = " ".join(lines + impl_text)
    if REFUSAL.search(text_out):
        errs.append("거부·사과 문구 포함")

    # 단위 환산: 출력의 '숫자+만/억/조'가 원문에 그대로 없으면 실패 (한국어 원문은 통과)
    out_all = " ".join(lines + [x for x in figs if isinstance(x, str)] + impl_text)
    src_kr = {(re.sub(r"[.,]", "", n), u) for n, u in KR_UNIT.findall(source_text or "")}
    conv = [f"{n}{u}" for n, u in KR_UNIT.findall(out_all) if (re.sub(r"[.,]", "", n), u) not in src_kr]
    if conv:
        errs.append(f"단위 환산 의심(만·억·조로 바꾸지 말고 원문 숫자·단위 그대로): {conv[:3]}")

    # 숫자 대조: 출력 숫자(2자리 이상)가 입력에 있어야 함. 발행일의 연·월·일은 허용
    allowed = _num_keys(source_text) | _num_keys((published or "").replace("-", " "))
    used = _num_keys(" ".join(lines + [x for x in figs if isinstance(x, str)] + impl_text))
    missing = sorted(k for k in used if len(k) >= 2 and k not in allowed)
    if missing:
        errs.append(f"입력에 없는 숫자: {missing[:5]}")
    return errs


def parse_json(text: str) -> dict | None:
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    try:
        return json.loads(t)
    except ValueError:
        m = re.search(r"\{.*\}", t, re.S)
        if m:
            try:
                return json.loads(m.group(0))
            except ValueError:
                return None
    return None


# ── API ──────────────────────────────────────────────────────
def json_failure(text: str, stop_reason: str | None = None) -> str:
    """파싱 실패 원인 분류: 빈 응답 / 출력 길이 초과 / JSON 객체 없음 / 문법 오류(위치·주변 문자열)."""
    t = (text or "").strip()
    if not t:
        return "JSON 파싱 실패: 빈 응답"
    if stop_reason == "max_tokens":
        return f"JSON 파싱 실패: 출력 길이 초과(max_tokens {MAX_TOKENS}) — 끝: {t[-60:]!r}"
    fenced = t.startswith("```")
    t2 = re.sub(r"^```(?:json)?\s*|\s*```$", "", t)
    m = re.search(r"\{.*\}", t2, re.S)
    if not m:
        return "JSON 파싱 실패: JSON 객체 없음" + (" (코드펜스)" if fenced else "") + f" — 앞: {t[:60]!r}"
    try:
        json.loads(m.group(0))
    except ValueError as e:
        pos = getattr(e, "pos", 0) or 0
        near = m.group(0)[max(0, pos - 30):pos + 30]
        return f"JSON 파싱 실패: 문법 오류({getattr(e, 'msg', e)}, 위치 {pos}) — 주변: {near!r}"
    return "JSON 파싱 실패: 원인 미상"


@dataclass
class CallOut:
    """API 응답 한 건. 테스트 스텁은 문자열만 돌려줘도 된다(_as_out)."""
    text: str
    stop_reason: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0


def _as_out(r) -> CallOut:
    return r if isinstance(r, CallOut) else CallOut(str(r or ""))


def anthropic_call(model: str, system: str, messages: list[dict], max_tokens: int = MAX_TOKENS) -> CallOut:
    import anthropic  # 선택 의존성: pip install "mincore[summarize]"
    client = getattr(anthropic_call, "_client", None) or anthropic.Anthropic()
    anthropic_call._client = client
    for i, wait in enumerate((*API_RETRY_WAITS, None)):
        try:
            r = client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages)
            u = getattr(r, "usage", None)
            return CallOut("".join(b.text for b in r.content if getattr(b, "type", "") == "text"),
                           getattr(r, "stop_reason", None), getattr(u, "input_tokens", 0) or 0,
                           getattr(u, "output_tokens", 0) or 0)
        except Exception:
            if wait is None:
                raise
            time.sleep(wait)
    return CallOut("")


@dataclass
class SummaryResult:
    ok: bool
    summary: dict | None = None
    provenance: dict | None = None
    errors: list[str] = field(default_factory=list)
    attempts: int = 0
    usage: dict = field(default_factory=dict)            # {model: {"calls", "in", "out"}}
    retry_log: list[dict] = field(default_factory=list)  # 실패한 시도: 모델·사유·stop_reason·응답 끝부분
    flags: dict = field(default_factory=dict)


def soft_issues(out: dict, mode: str, source_text: str, prompt: Prompt) -> list[str]:
    issues = []
    long = [x for x in out.get("lines") or [] if len(x.strip()) > LINE_SOFT_MAX]
    if long:
        issues.append(f"줄 길이 초과: {LINE_SOFT_MAX}자 넘는 줄 {len(long)}개 — 한 줄에 한 가지 사실만 60자 안팎으로")
    if ("oda_relevance" in prompt.fields and mode == "body"
            and str(out.get("oda_relevance", "")).strip() != NOT_APPLICABLE
            and not has_korea_evidence(source_text)):
        issues.append(f"시사점 근거 없음: 입력에 한국 관련 근거가 없으므로 oda_relevance는 정확히 '{NOT_APPLICABLE}'")
    return issues


def _ask(call, model, system, messages, res: SummaryResult) -> CallOut:
    r = _as_out(call(model, system, messages, MAX_TOKENS))
    res.attempts += 1
    u = res.usage.setdefault(model, {"calls": 0, "in": 0, "out": 0})
    u["calls"] += 1
    u["in"] += r.input_tokens
    u["out"] += r.output_tokens
    return r


def _log_fail(res: SummaryResult, model: str, errors: list[str], r: CallOut | None = None):
    res.retry_log.append({"model": model, "errors": errors,
                          "stop_reason": r.stop_reason if r else None,
                          "out_tokens": r.output_tokens if r else 0,
                          "tail": (r.text or "")[-160:] if r else ""})


def _run_model(model, call, system, user, mode, source_text, prompt, published, retries,
               res: SummaryResult) -> dict | None:
    messages = [{"role": "user", "content": user}]
    out = None
    for _ in range(retries + 1):
        try:
            r = _ask(call, model, system, messages, res)
        except Exception as e:
            res.errors = [f"API 오류: {type(e).__name__}: {e}"[:200]]
            _log_fail(res, model, res.errors)
            return None
        out = parse_json(r.text)
        res.errors = [json_failure(r.text, r.stop_reason)] if out is None else validate_summary(
            out, mode, source_text, prompt, published)
        if not res.errors:
            break
        _log_fail(res, model, res.errors, r)
        messages += [{"role": "assistant", "content": r.text or "(빈 응답)"},
                     {"role": "user", "content": "검증 실패: " + "; ".join(res.errors)
                      + "\n규칙에 맞게 JSON만 다시 출력하세요. 입력에 없는 내용·숫자는 쓰지 마세요."}]
        out = None
    if out is None:
        return None

    soft = soft_issues(out, mode, source_text, prompt)
    if soft:                              # 1회 보완 요청. 보완본이 검증을 통과할 때만 바꾼다
        res.flags["soft_retry"] = [x.split(":")[0] for x in soft]
        messages += [{"role": "assistant", "content": json.dumps(out, ensure_ascii=False)},
                     {"role": "user", "content": "보완 요청: " + "; ".join(soft)
                      + "\n나머지 규칙은 그대로 지켜 JSON만 다시 출력하세요."}]
        try:
            r = _ask(call, model, system, messages, res)
            out2 = parse_json(r.text)
            errs2 = [json_failure(r.text, r.stop_reason)] if out2 is None else validate_summary(
                out2, mode, source_text, prompt, published)
            if errs2:
                _log_fail(res, model, ["보완본 " + e for e in errs2], r)
            else:
                out = out2
        except Exception as e:
            _log_fail(res, model, [f"보완본 API 오류: {type(e).__name__}"[:120]])
        long = [x for x in out["lines"] if len(x.strip()) > LINE_SOFT_MAX]
        if long:
            res.flags["long_lines"] = len(long)
        if any(x.startswith("시사점 근거 없음") for x in soft_issues(out, mode, source_text, prompt)):
            out["oda_relevance"] = NOT_APPLICABLE
            res.flags["oda_forced"] = True
    return out


def summarize_card(card: dict, prompt: Prompt, *, body_text: str = "", rss_summary: str | None = None,
                   model: str = DEFAULT_MODEL, call: Callable | None = None,
                   retries: int = VALIDATION_RETRIES, fallback_model: str | None = FALLBACK_MODEL) -> SummaryResult:
    call = call or anthropic_call
    mode = mode_for(card)
    user = build_input(card, body_text, rss_summary, mode)
    source_text = "\n".join([card.get("title") or "", body_text if mode != "title" else "",
                             (rss_summary or "") if mode == "lead" else ""])
    system = prompt.system(mode)
    res = SummaryResult(ok=False)
    used = model
    out = _run_model(model, call, system, user, mode, source_text, prompt, card.get("published"), retries, res)
    if out is None and fallback_model and fallback_model != model:
        primary_errors = res.errors
        res.flags = {}
        out = _run_model(fallback_model, call, system, user, mode, source_text, prompt,
                         card.get("published"), retries, res)
        used = fallback_model
        if out is not None:
            res.flags.update(fallback_from=model, primary_errors=primary_errors)
    if out is None:
        return res

    summary = {"lines": [x.strip() for x in out["lines"]],
               "basis": mode,
               "key_figures": out.get("key_figures", []) if mode == "body" else [],
               "quotes": ((card.get("body") or {}).get("quotes") or [])[:2] if mode == "body" else [],
               "category": out.get("category"),
               "tags": [t.strip() for t in out.get("tags", [])][:5]}
    for k in prompt.fields:
        summary[k] = out.get(k)
    impl = implication_field(prompt)
    if impl and mode != "body":
        summary[impl] = NOT_APPLICABLE
    res.ok, res.summary, res.errors = True, summary, []
    res.provenance = {
        "model": used, "prompt_file": prompt.path, "prompt_version": prompt.version,
        "prompt_sha256": prompt.sha256, "input_body_status": (card.get("body") or {}).get("status"),
        "input_chars": min(len(body_text), SUMMARY_INPUT_MAX_CHARS) if mode == "body" else len(body_text),
        "attempts": res.attempts,
        "summarized_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
    if res.flags:
        res.provenance["flags"] = res.flags
    if res.retry_log:
        res.provenance["retry_reasons"] = [e for x in res.retry_log for e in x["errors"]][:6]
    return res


# ── legacy 카드 변환 (기존 build_index·send_email·vault 생성기 입력 형식) ──
def to_legacy(card: dict, service: str, prompt: Prompt) -> dict:
    s = card.get("summary") or {}
    ext = (card.get("ext") or {}).get(service) or {}
    lines = list(s.get("lines") or [])
    if lines and s.get("basis") in BASIS_LABEL:
        lines[0] = BASIS_LABEL[s["basis"]] + lines[0]
    url = card.get("url") or card.get("gnews_url") or ext.get("v1_url") or ""
    common = {
        "id": ext.get("legacy_id") or card.get("card_id"),
        "title": card.get("title"),
        "url": url,
        "source": (card.get("source") or {}).get("name") or "",
        "tier_weight": (card.get("source") or {}).get("tier_weight"),
        "score": card.get("score"),
        "published": card.get("published") or "",
        "korean_summary": lines,
        "category": s.get("category"),
        "tags": s.get("tags") or [],
        "ai_processed": True,
        # v2 추가 키 (기존 소비 코드는 무시)
        "card_id": card.get("card_id"),
        "basis": s.get("basis"),
        "body_status": (card.get("body") or {}).get("status"),
        "key_figures": s.get("key_figures") or [],
        "quotes": s.get("quotes") or [],
    }
    if service == "kcmo":
        oda = s.get("oda_relevance") or ""
        common.update({
            "source_language": (card.get("source") or {}).get("language") or ext.get("source_language"),
            "country": ext.get("country"),
            "oda_relevance": "" if oda.strip() == NOT_APPLICABLE else oda,
            "category_ko": prompt.categories.get(s.get("category"), s.get("category")),
            "key_phrases": s.get("key_phrases") or [],
            "processed_at": (card.get("provenance") or {}).get("summarized_at"),
        })
    elif service == "cmw":
        imp = s.get("implication") or ""
        common.update({
            "summary_raw": ext.get("summary_raw", ""),
            "english_phrases": s.get("english_phrases") or [],
            "implication": "" if imp.strip() == NOT_APPLICABLE else imp,
        })
    return common
