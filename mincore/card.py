"""통일 카드 형식(card.v1) 생성·검증과 legacy raw 어댑터."""

from __future__ import annotations

from datetime import datetime, timezone

from .extract import QUOTES_MAX_TOTAL, BodyResult
from .normalize import canonical_url, card_id, domain_of, title_norm

SCHEMA_VERSION = "card.v1"
# 다음 단계(summarize 연계)에서 요약 입력으로 쓸 본문 최대 길이. 앞부분 우선.
SUMMARY_INPUT_MAX_CHARS = 4000
# 이 상태면 3줄 요약·시사점을 만들지 않는다 (summarize 연계 규칙, 다음 단계 구현)
NO_SUMMARY_STATUSES = ("lead_only", "title_only")

REQUIRED = ("schema_version", "service", "card_id", "title", "title_norm", "url", "source",
            "published", "collected_at", "score", "body", "summary", "provenance", "ext")
BODY_KEYS = ("status", "chars", "sha256", "source", "block_type", "http_status",
             "extracted_date", "quotes")
FORBIDDEN_KEYS = ("text", "body_text", "full_text", "content", "html")


def build_card(service: str, *, title: str, url: str, gnews_url: str | None = None,
               source_name: str = "", tier: str | None = None, tier_weight: int | float | None = None,
               language: str | None = None, published: str | None = None, published_from: str | None = None,
               score: int | float | None = None, body: BodyResult | dict | None = None,
               ext: dict | None = None, collected_at: str | None = None) -> dict:
    canon = canonical_url(url)
    rec = body.to_record() if isinstance(body, BodyResult) else dict(body or {})
    return {
        "schema_version": SCHEMA_VERSION,
        "service": service,
        "card_id": card_id(canon),
        "title": title,
        "title_norm": title_norm(title),
        "url": canon,
        "gnews_url": gnews_url,
        "source": {"name": source_name, "domain": domain_of(canon), "tier": tier,
                   "tier_weight": tier_weight, "language": language},
        "published": published,
        "published_from": published_from,
        "collected_at": collected_at or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "score": score,
        "body": {k: rec.get(k) for k in BODY_KEYS} | {
            "final_url": rec.get("final_url"), "strategies_tried": rec.get("strategies_tried") or []},
        "summary_input_max_chars": SUMMARY_INPUT_MAX_CHARS,
        "summary": None,
        "provenance": None,
        "ext": {service: ext or {}},
    }


def validate(card: dict) -> list[str]:
    """스키마 핵심 규칙 검사. 오류 문자열 목록(빈 목록이면 통과)."""
    errs = [f"missing: {k}" for k in REQUIRED if k not in card]
    if card.get("schema_version") != SCHEMA_VERSION:
        errs.append(f"schema_version != {SCHEMA_VERSION}")
    body = card.get("body") or {}
    if body.get("status") not in ("full", "partial", "lead_only", "title_only"):
        errs.append(f"body.status invalid: {body.get('status')}")
    quotes = body.get("quotes") or []
    if sum(len(q) for q in quotes) > QUOTES_MAX_TOTAL:
        errs.append("body.quotes exceeds limit")

    def walk(o, path=""):
        if isinstance(o, dict):
            for k, v in o.items():
                if k in FORBIDDEN_KEYS:
                    errs.append(f"forbidden key (full text?): {path}{k}")
                walk(v, f"{path}{k}.")
        elif isinstance(o, list):
            for v in o:
                walk(v, path)
        elif isinstance(o, str) and len(o) > 1000 and not path.startswith(("url", "gnews_url")):
            errs.append(f"string too long (full text?): {path.rstrip('.')}")
    walk(card)
    return errs


def legacy_extra(card: dict) -> dict:
    """legacy raw 기사에 '추가'할 키. 기존 키는 덮어쓰지 않는 것이 원칙이며,
    url/id 교체 여부는 호출하는 쪽(서비스 collect)이 결정한다."""
    b = card["body"]
    return {
        "card_id": card["card_id"],
        "resolved_url": card["url"],
        "gnews_url": card["gnews_url"],
        "body_status": b["status"],
        "body_chars": b["chars"],
        "body_sha256": b["sha256"],
        "body_source": b["source"],
        "block_type": b["block_type"],
        "extracted_date": b["extracted_date"],
        "quotes": b["quotes"],
    }
