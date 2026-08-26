"""Seed the explicitly-labelled P5 model-generated template drafts.

Usage (after migration 014):
    python -m scripts.seed_route_template_drafts

This command deliberately does not upgrade DRAFT/MODEL_GENERATED records to
REVIEWED.  A separate human review and source-verification workflow is needed
for that evidence transition.
"""

from __future__ import annotations

import asyncio

from app.templates.repositories import PostgresTemplateRepository
from app.templates.seed import model_generated_template_drafts


async def main() -> None:
    repository = PostgresTemplateRepository()
    for template in model_generated_template_drafts():
        await repository.save_template(template)
    print("Seeded 15 model-generated DRAFT route templates (3 cities × 5).")


if __name__ == "__main__":
    asyncio.run(main())
