"""본문 기반 요약 (리뉴얼 5단계).

서비스와 무관한 엔진만 둔다: 지시문 로딩, 입력 조립, API 호출, 출력 검증, 재시도, provenance,
legacy 카드 변환. 지시문(독자·카테고리·서비스 필드)은 각 서비스 저장소의 prompts/summarize_v2.md.

body.status → 모드
  full / partial → body  : 3줄 요약 + 핵심 수치(원문 표기) + 인용(추출 단계 quotes에서 선택)
  lead_only      → lead  : 제목 번역 1줄 + 리드 1줄, 시사점 금지
  title_only     → title : 제목 번역 1줄
검증 실패 → 실패 사유를 붙여 최대 2회 재요청 → 그래도 실패면 카드 제외(호출하는 쪽이 보고에 기록).
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

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
MAX_TOKENS = 1200
VALIDATION_RETRIES = 2
API_RETRY_WAITS = (5.0, 10.0)

MODE_OF = {"full": "body", "partial": "body", "lead_only": "lead", "title_only": "title"}
LINES_OF = {"body": 3, "lead": 2, "title": 1}
MIN_LINE = {"body": 15, "lead": 15, "title": 8}
NOT_APPLICABLE = "해당 없음"
BASIS_LABEL = {"lead": "[본문 미확보] ", "title": "[제목 기반] "}

REFUSAL = re.compile(
    r"죄송|사과드|요약할 수 없|요약이 불가|제공된 (?:정보|내용)만으로는|AI(?:로서| 언어 모델)|"
    r"I cannot|I can't|I'm sorry|I am sorry|As an AI|unable to (?:summari[sz]e|provide)", re.I)
NUM = re.compile(r"\d[\d.,]*\d|\d")


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
        """서비스 고유 출력 필드 (예: key_phrases, oda_relevance / english_phrases)."""
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

    if "oda_relevance" in prompt.fields and "oda_relevance" in out:
        oda = out.get("oda_relevance")
        if not isinstance(oda, str) or not oda.strip():
            errs.append("oda_relevance가 비어 있음")
        elif mode != "body" and oda.strip() != NOT_APPLICABLE:
            errs.append(f"본문 미확보 카드는 oda_relevance를 '{NOT_APPLICABLE}'로 둬야 함")

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
    text_out = " ".join(lines + [str(out.get("oda_relevance", ""))])
    if REFUSAL.search(text_out):
        errs.append("거부·사과 문구 포함")

    # 숫자 대조: 출력 숫자(2자리 이상)가 입력에 있어야 함. 발행일의 연·월·일은 허용
    allowed = _num_keys(source_text) | _num_keys((published or "").replace("-", " "))
    used = _num_keys(" ".join(lines + [x for x in figs if isinstance(x, str)]
                              + [str(out.get("oda_relevance", ""))]))
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
def anthropic_call(model: str, system: str, messages: list[dict], max_tokens: int = MAX_TOKENS) -> str:
    import anthropic  # 선택 의존성: pip install "mincore[summarize]"
    client = getattr(anthropic_call, "_client", None) or anthropic.Anthropic()
    anthropic_call._client = client
    for i, wait in enumerate((*API_RETRY_WAITS, None)):
        try:
            r = client.messages.create(model=model, max_tokens=max_tokens, system=system, messages=messages)
            return "".join(b.text for b in r.content if getattr(b, "type", "") == "text")
        except Exception:
            if wait is None:
                raise
            time.sleep(wait)
    return ""


@dataclass
class SummaryResult:
    ok: bool
    summary: dict | None = None
    provenance: dict | None = None
    errors: list[str] = field(default_factory=list)
    attempts: int = 0


def summarize_card(card: dict, prompt: Prompt, *, body_text: str = "", rss_summary: str | None = None,
                   model: str = DEFAULT_MODEL, call: Callable | None = None,
                   retries: int = VALIDATION_RETRIES) -> SummaryResult:
    call = call or anthropic_call
    mode = mode_for(card)
    user = build_input(card, body_text, rss_summary, mode)
    source_text = "\n".join([card.get("title") or "", body_text if mode != "title" else "",
                             (rss_summary or "") if mode == "lead" else ""])
    system = prompt.system(mode)
    messages = [{"role": "user", "content": user}]
    res = SummaryResult(ok=False)
    for attempt in range(retries + 1):
        res.attempts = attempt + 1
        try:
            raw = call(model, system, messages, MAX_TOKENS)
        except Exception as e:
            res.errors = [f"API 오류: {type(e).__name__}: {e}"[:200]]
            break
        out = parse_json(raw)
        res.errors = ["JSON 파싱 실패"] if out is None else validate_summary(
            out, mode, source_text, prompt, card.get("published"))
        if not res.errors:
            break
        messages += [{"role": "assistant", "content": raw or "(빈 응답)"},
                     {"role": "user", "content": "검증 실패: " + "; ".join(res.errors)
                      + "\n규칙에 맞게 JSON만 다시 출력하세요. 입력에 없는 내용·숫자는 쓰지 마세요."}]
    if res.errors:
        return res

    summary = {"lines": [x.strip() for x in out["lines"]],
               "basis": mode,
               "key_figures": out.get("key_figures", []) if mode == "body" else [],
               "quotes": ((card.get("body") or {}).get("quotes") or [])[:2] if mode == "body" else [],
               "category": out.get("category"),
               "tags": [t.strip() for t in out.get("tags", [])][:5]}
    for k in prompt.fields:
        summary[k] = out.get(k)
    if "oda_relevance" in prompt.fields and mode != "body":
        summary["oda_relevance"] = NOT_APPLICABLE
    res.ok, res.summary = True, summary
    res.provenance = {
        "model": model, "prompt_file": prompt.path, "prompt_version": prompt.version,
        "prompt_sha256": prompt.sha256, "input_body_status": (card.get("body") or {}).get("status"),
        "input_chars": min(len(body_text), SUMMARY_INPUT_MAX_CHARS) if mode == "body" else len(body_text),
        "attempts": res.attempts,
        "summarized_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
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
        common.update({
            "summary_raw": ext.get("summary_raw", ""),
            "english_phrases": s.get("english_phrases") or [],
        })
    return common
