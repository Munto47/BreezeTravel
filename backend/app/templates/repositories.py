from __future__ import annotations

import json
from typing import Any, Protocol

from app.db.connection import get_pool
from app.templates.models import CityRouteTemplate, TemplateStatus


class TemplateRepository(Protocol):
    async def list_templates(self, city: str | None = None, status: TemplateStatus | None = None) -> list[CityRouteTemplate]: ...
    async def get_template(self, template_id: str) -> CityRouteTemplate | None: ...
    async def save_template(self, template: CityRouteTemplate) -> CityRouteTemplate: ...


class InMemoryTemplateRepository:
    def __init__(self, templates: list[CityRouteTemplate] | None = None):
        self.templates = {template.template_id: template for template in templates or []}

    async def list_templates(self, city: str | None = None, status: TemplateStatus | None = None) -> list[CityRouteTemplate]:
        return [
            template for template in sorted(self.templates.values(), key=lambda value: value.template_id)
            if (city is None or template.city == city) and (status is None or template.status is status)
        ]

    async def get_template(self, template_id: str) -> CityRouteTemplate | None:
        return self.templates.get(template_id)

    async def save_template(self, template: CityRouteTemplate) -> CityRouteTemplate:
        current = self.templates.get(template.template_id)
        if current and template.template_version <= current.template_version:
            raise ValueError("template version must advance")
        self.templates[template.template_id] = template
        return template


class PostgresTemplateRepository:
    def __init__(self, pool: Any | None = None):
        self._pool = pool

    async def _get_pool(self):
        return self._pool or await get_pool()

    @staticmethod
    def _from_row(row: Any) -> CityRouteTemplate:
        payload = row["template_json"]
        if isinstance(payload, str):
            payload = json.loads(payload)
        return CityRouteTemplate.model_validate(payload)

    async def list_templates(self, city: str | None = None, status: TemplateStatus | None = None) -> list[CityRouteTemplate]:
        terms: list[Any] = []
        clauses: list[str] = []
        if city:
            terms.append(city)
            clauses.append(f"city = ${len(terms)}")
        if status:
            terms.append(status.value)
            clauses.append(f"status = ${len(terms)}")
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            rows = await conn.fetch(f"SELECT template_json FROM city_route_templates {where} ORDER BY city, name", *terms)
        return [self._from_row(row) for row in rows]

    async def get_template(self, template_id: str) -> CityRouteTemplate | None:
        pool = await self._get_pool()
        async with pool.acquire() as conn:
            row = await conn.fetchrow("SELECT template_json FROM city_route_templates WHERE template_id = $1", template_id)
        return self._from_row(row) if row else None

    async def save_template(self, template: CityRouteTemplate) -> CityRouteTemplate:
        pool = await self._get_pool()
        payload = json.dumps(template.model_dump(mode="json"), ensure_ascii=False)
        async with pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO city_route_templates (template_id, city, name, template_version, status, provenance, last_verified_at, template_json)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8::jsonb)
                ON CONFLICT (template_id) DO UPDATE SET
                    city = EXCLUDED.city, name = EXCLUDED.name, template_version = EXCLUDED.template_version,
                    status = EXCLUDED.status, provenance = EXCLUDED.provenance,
                    last_verified_at = EXCLUDED.last_verified_at, template_json = EXCLUDED.template_json,
                    updated_at = NOW()
                WHERE city_route_templates.template_version < EXCLUDED.template_version
                """,
                template.template_id, template.city, template.name, template.template_version,
                template.status.value, template.provenance.value, template.last_verified_at, payload,
            )
        return template
