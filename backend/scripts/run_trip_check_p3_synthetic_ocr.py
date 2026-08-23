from __future__ import annotations

import argparse
import json
from pathlib import Path

from evals.trip_check_v1.synthetic_ocr_runner import (
    DEFAULT_OUTPUT,
    DEFAULT_SPEC,
    DEFAULT_WORK_ROOT,
    run,
)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the P3 synthetic OCR phase dataset")
    parser.add_argument("--spec", type=Path, default=DEFAULT_SPEC)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--work-root", type=Path, default=DEFAULT_WORK_ROOT)
    parser.add_argument("--subject-commit", default=None)
    parser.add_argument("--font", type=Path, default=None)
    parser.add_argument("--render-only", action="store_true")
    parser.add_argument("--keep-artifacts", action="store_true")
    parser.add_argument("--visual-review-approved", action="store_true")
    args = parser.parse_args()
    manifest = run(
        spec_path=args.spec,
        output=args.output,
        work_root=args.work_root,
        subject_commit=args.subject_commit,
        font_path=args.font,
        render_only=args.render_only,
        keep_artifacts=args.keep_artifacts,
        visual_review_approved=args.visual_review_approved,
    )
    print(json.dumps({"status": manifest["status"], "output": str(args.output)}, ensure_ascii=False))
    return 0 if manifest["status"] in {"PASS", "RENDERED_NOT_SCORED"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
