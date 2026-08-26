from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable

from app.audit.models import AuditDependency
from app.audit.registry import AuditRule


class IncrementalDependencyIndex:
    def __init__(self, rules: Iterable[AuditRule]):
        self._by_dependency: dict[AuditDependency, set[str]] = defaultdict(set)
        for rule in rules:
            for dependency in rule.dependencies:
                self._by_dependency[dependency].add(rule.rule_id)

    def affected_rule_ids(self, dependencies: Iterable[AuditDependency]) -> set[str]:
        return {
            rule_id
            for dependency in dependencies
            for rule_id in self._by_dependency.get(dependency, set())
        }

