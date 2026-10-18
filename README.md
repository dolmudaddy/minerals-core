# minerals-core (`mincore`)

CMW(Critical Minerals Weekly)·KCMO(Korea Critical Minerals ODA Weekly) 공통 수집 코어.

서비스와 무관한 기반 기능만 담는다. 국가·광물 필터와 점수는 각 서비스 저장소의 `sources.yaml`/`collect.py`에 남긴다.

| 모듈 | 역할 |
|---|---|
| `normalize` | canonical URL, 제목 정규화, 날짜 정규화(ISO·RFC822), `card_id`(sha1 12자리) |
| `resolve` | Google News 중계 주소 → 원매체 주소 (googlenewsdecoder 배치 + 3회 지수 재시도 + 리다이렉트 추적) |
| `fetch` | 도메인당 동시 1건·최소 간격·429/503 Retry-After 재시도 |
| `extract` | trafilatura → 도메인 대체표 전략(amp·feed·presscorner_api·reader_proxy) → og:description → RSS 설명. `body_status`·인용 구절 |
| `domain_rules` (+ `domain_rules.yaml`) | 도메인별 대체표. **튜닝은 YAML에서만** |
| `card` | 통일 카드 `card.v1` 생성·검증, legacy raw 추가 키 |
| `ledger` | 발행 원장 (다음 회차 중복 제외) |
| `report` | 회차별 실행 보고 (`run_<날짜>.{json,md}`) |
| `summarize` | 본문 기반 요약 엔진: 지시문 파일(`prompts/summarize_v2.md`) 로딩, body_status별 모드, 출력 검증(구조·길이·거부 문구·숫자 대조)·2회 재시도·제외, provenance, legacy 카드 변환 |
| `regen` | 과거 발행분 일괄 재생성 v2 (회차 단위 저장·재개) |
| `compare` | v1↔v2 재생성 보고, 요약 비교표·골든셋 초안 |

## 본문 보관 원칙
공개 저장소이므로 **본문 전문은 카드·보고서 어디에도 저장하지 않는다.** 카드에는 길이·sha256·인용 구절(최대 2개, 합계 300자)만 남는다. 요약 단계용 전문은 `cache_dir`(서비스 저장소의 `data/_cache/bodies/`, gitignore)에만 쓴다.

## 설치
```
pip install "mincore @ git+https://github.com/dolmudaddy/minerals-core@v0.3.1"
# 개발
pip install -e ".[test]" && pytest
```

## 사용 예
```python
from mincore.resolve import resolve_many
from mincore.fetch import Fetcher
from mincore.extract import extract_body
from mincore.domain_rules import load_rules
from mincore.card import build_card

rules, fetcher = load_rules(), Fetcher()
for r in resolve_many(gnews_urls):
    body = extract_body(r.url, fetcher, rules, title=title, rss_summary=summary, cache_dir="data/_cache/bodies")
    card = build_card("kcmo", title=title, url=r.url, gnews_url=r.orig_url, body=body, ext={"country": "Laos"})
```

CLI: `python -m mincore.ledger {commit,bootstrap,check}`, `python -m mincore.report finalize`.

## reader_proxy (v0.2.0)
1차 요청이 **403**이고 도메인 규칙에 `reader_proxy`가 있을 때만 Jina Reader(`https://r.jina.ai/<url>`)로 본문을 받는다.
회차당 20건 상한·요청 간격 2초(`extract.READER_*`). 상한은 `extract_body(..., ctx=ctx)`의 `ctx`로 집계하므로 **한 회차 안에서는 같은 `ctx`를 넘겨야 한다.**
실패하면 기존대로 og:description → RSS 설명. 채택되면 `body.source == "reader_proxy"`, 실행 보고에 "Reader 프록시 사용 (채택/시도)"가 찍힌다.

## 요약 (v0.3.0)
`pip install "mincore[summarize] @ git+https://github.com/dolmudaddy/minerals-core@v0.3.1"` (anthropic 포함).

- 지시문은 서비스 저장소 `prompts/summarize_v2.md`: YAML 머리말(`version`, `categories`, `fields`) + `## common` / `## mode:body` / `## mode:lead` / `## mode:title` / `## output` 절.
- body.status → 모드: full·partial=body(3줄+핵심 수치+인용), lead_only=lead(2줄, 시사점 금지), title_only=title(1줄).
- 인용(quotes)은 모델이 만들지 않고 추출 단계 `body.quotes`에서 고른다.
- 검증 실패 → 사유를 붙여 2회 재요청 → 그래도 실패면 카드 제외.

```
python -m mincore.regen --service kcmo --input data --from 2026-05-13 --to 2026-10-18     --prompt prompts/summarize_v2.md --out data/v2 --resume [--max-issues 10] [--compare-model M --compare-n 30]
python -m mincore.compare review --issue data/v2/kcmo/cards_v2_2026-10-24.json --n 30     --md data/v2/kcmo/review_2026-10-24.md --golden data/v2/golden/golden_draft.json
```
