"""도메인별 대체표 로딩. 튜닝은 domain_rules.yaml에서만 한다."""

from __future__ import annotations

from dataclasses import dataclass, field
from importlib import resources
from pathlib import Path

import yaml

from .normalize import domain_of


@dataclass
class DomainRule:
    domain: str = ""
    strategies: list[str] = field(default_factory=list)
    skip_fetch: bool = False
    min_interval: float = 0.0
    amp_templates: list[str] = field(default_factory=lambda: ["{url}/amp"])
    feed_urls: list[str] = field(default_factory=list)
    aggregator: bool = False
    note: str = ""


DEFAULT_RULE = DomainRule()


class DomainRules:
    def __init__(self, table: dict[str, dict]):
        self.table = {d.lower(): DomainRule(domain=d.lower(), **cfg) for d, cfg in table.items()}

    def for_url(self, url: str) -> DomainRule:
        host = domain_of(url)
        parts = host.split(".")
        for i in range(len(parts) - 1):          # 하위 도메인 → 상위 도메인 순으로 매칭
            rule = self.table.get(".".join(parts[i:]))
            if rule:
                return rule
        return DEFAULT_RULE


def load_rules(override_path: str | Path | None = None) -> DomainRules:
    base = yaml.safe_load(resources.files("mincore").joinpath("domain_rules.yaml").read_text(encoding="utf-8"))
    table = dict(base.get("domains") or {})
    if override_path:
        extra = yaml.safe_load(Path(override_path).read_text(encoding="utf-8")) or {}
        table.update(extra.get("domains") or {})
    return DomainRules(table)
