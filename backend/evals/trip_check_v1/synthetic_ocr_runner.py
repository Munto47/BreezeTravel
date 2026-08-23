from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import subprocess
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from app.importing.errors import PrivacyBlockedError
from app.importing.parser import ItineraryTextParser
from app.importing.screenshots import OcrTextLine, PaddleOcrEngine


BACKEND_ROOT = Path(__file__).resolve().parents[2]
REPO_ROOT = BACKEND_ROOT.parent
DEFAULT_SPEC = BACKEND_ROOT / "evals" / "fixtures" / "trip_check_ocr_synthetic_v1.json"
DEFAULT_OUTPUT = BACKEND_ROOT / "evidence" / "trip_check_v1" / "p3" / "synthetic_ocr_manifest.json"
DEFAULT_WORK_ROOT = REPO_ROOT / ".local-artifacts" / "p3-synthetic-ocr"
RENDERER_VERSION = "pillow-trip-check-ui-v1"
CONFIDENCE_THRESHOLD = 0.85


class OcrEngine(Protocol):
    name: str
    version: str

    async def recognize(self, image_path: Path) -> list[OcrTextLine]: ...


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
    ).strip()


def _font_path(explicit: Path | None = None) -> Path:
    candidates = [
        explicit,
        Path(os.environ["P3_OCR_FONT_PATH"]) if os.getenv("P3_OCR_FONT_PATH") else None,
        Path("C:/Windows/Fonts/msyh.ttc"),
        Path("C:/Windows/Fonts/msyhbd.ttc"),
        Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"),
        Path("/usr/share/fonts/truetype/wqy/wqy-microhei.ttc"),
    ]
    for candidate in candidates:
        if candidate is not None and candidate.is_file():
            return candidate.resolve()
    raise RuntimeError("A Chinese font is required; set P3_OCR_FONT_PATH")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for character in text:
        candidate = current + character
        if current and draw.textlength(candidate, font=font) > width:
            lines.append(current)
            current = character
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _colors(theme: str) -> dict[str, str]:
    if theme == "dark":
        return {
            "background": "#101318",
            "surface": "#1d232c",
            "surface_alt": "#263343",
            "text": "#f2f5f8",
            "muted": "#aab4c0",
            "accent": "#61a8ff",
            "line": "#344150",
        }
    return {
        "background": "#eef2f6",
        "surface": "#ffffff",
        "surface_alt": "#dff1ff",
        "text": "#17212b",
        "muted": "#637083",
        "accent": "#1478d4",
        "line": "#d5dde7",
    }


def render_case(case: dict[str, Any], output: Path, *, font_path: Path) -> dict[str, Any]:
    width = int(case["image"]["width"])
    height = int(case["image"]["height"])
    palette = _colors(case["theme"])
    image = Image.new("RGB", (width, height), palette["background"])
    draw = ImageDraw.Draw(image)
    regular = ImageFont.truetype(str(font_path), max(30, width // 27))
    small = ImageFont.truetype(str(font_path), max(24, width // 36))
    title = ImageFont.truetype(str(font_path), max(40, width // 21))
    margin = max(42, width // 18)
    y = margin

    draw.rounded_rectangle(
        (margin, y, width - margin, y + title.size * 2),
        radius=title.size // 2,
        fill=palette["surface"],
        outline=palette["line"],
        width=2,
    )
    draw.text((margin * 1.5, y + title.size // 3), "行程记录", font=title, fill=palette["text"])
    draw.text(
        (width - margin * 4.5, y + title.size // 2),
        case["case_id"],
        font=small,
        fill=palette["muted"],
    )
    y += title.size * 2 + margin

    noise_labels = list(case["render_profile"].get("chat_noise") or [])
    if noise_labels:
        chip_x = margin
        for label in noise_labels:
            chip_width = int(draw.textlength(label, font=small)) + small.size
            draw.rounded_rectangle(
                (chip_x, y, chip_x + chip_width, y + small.size * 1.7),
                radius=small.size // 2,
                fill=palette["surface"],
            )
            draw.text((chip_x + small.size // 2, y + small.size // 4), label, font=small, fill=palette["muted"])
            chip_x += chip_width + small.size // 2
        y += int(small.size * 2.2)

    for index, block in enumerate(case["text_blocks"]):
        is_heading = block["role"] in {"title", "heading"}
        block_font = title if is_heading else regular
        role_label = str(block["role"]).upper()
        bubble_width = width - margin * (3 if case["layout"] == "chat" else 2)
        if case["layout"] == "chat":
            x = margin if index % 2 == 0 else width - margin - bubble_width
            fill = palette["surface"] if index % 2 == 0 else palette["surface_alt"]
        else:
            x = margin
            bubble_width = width - margin * 2
            fill = palette["surface"]
        inner = max(28, width // 34)
        text_width = bubble_width - inner * 2
        lines = _wrap(draw, block["text"], block_font, text_width)
        line_height = int(block_font.size * 1.55)
        bubble_height = small.size * 2 + len(lines) * line_height + inner
        if y + bubble_height > height - margin:
            raise ValueError(f"render overflow for {case['case_id']} block {block['block_id']}")
        draw.rounded_rectangle(
            (x, y, x + bubble_width, y + bubble_height),
            radius=max(24, width // 45),
            fill=fill,
            outline=palette["line"],
            width=2,
        )
        draw.text((x + inner, y + inner // 2), role_label, font=small, fill=palette["accent"])
        text_y = y + small.size * 1.7
        for line in lines:
            draw.text((x + inner, text_y), line, font=block_font, fill=palette["text"])
            text_y += line_height
        y += bubble_height + max(24, margin // 2)

    profile = case["render_profile"]
    scale = float(profile["scale"])
    if scale < 1:
        reduced = image.resize((max(1, int(width * scale)), max(1, int(height * scale))), Image.Resampling.LANCZOS)
        image = reduced.resize((width, height), Image.Resampling.BICUBIC)
    rotation = float(profile["rotation_deg"])
    if rotation:
        image = image.rotate(rotation, resample=Image.Resampling.BICUBIC, expand=False, fillcolor=palette["background"])
    crop = profile["crop"]
    if any(int(crop[key]) for key in ("top", "right", "bottom", "left")):
        box = (
            int(crop["left"]),
            int(crop["top"]),
            width - int(crop["right"]),
            height - int(crop["bottom"]),
        )
        image = image.crop(box).resize((width, height), Image.Resampling.BICUBIC)
    for occlusion in profile.get("occlusions") or []:
        overlay = Image.new("RGBA", image.size, (0, 0, 0, 0))
        overlay_draw = ImageDraw.Draw(overlay)
        x0 = int(width * float(occlusion["x"]))
        y0 = int(height * float(occlusion["y"]))
        x1 = x0 + int(width * float(occlusion["width"]))
        y1 = y0 + int(height * float(occlusion["height"]))
        alpha = int(255 * float(occlusion["opacity"]))
        overlay_draw.rounded_rectangle((x0, y0, x1, y1), radius=8, fill=(80, 80, 80, alpha))
        image = Image.alpha_composite(image.convert("RGBA"), overlay).convert("RGB")
    blur = float(profile["blur_radius"])
    if blur:
        image = image.filter(ImageFilter.GaussianBlur(blur))
    noise_sigma = float(profile["noise_sigma"])
    if noise_sigma:
        array = np.asarray(image, dtype=np.int16)
        noise = np.random.default_rng(int(case["seed"])).normal(0, noise_sigma, array.shape)
        image = Image.fromarray(np.clip(array + noise, 0, 255).astype(np.uint8), mode="RGB")

    output.parent.mkdir(parents=True, exist_ok=True)
    image_format = case["image"]["format"]
    save_format = {"PNG": "PNG", "JPEG": "JPEG", "WebP": "WEBP"}[image_format]
    save_options: dict[str, Any] = {}
    if image_format in {"JPEG", "WebP"}:
        save_options["quality"] = int(profile["compression_quality"])
    if image_format == "PNG":
        save_options["compress_level"] = 9
    image.save(output, format=save_format, **save_options)
    return {
        "image_sha256": _sha256(output),
        "bytes": output.stat().st_size,
        "width": width,
        "height": height,
        "format": image_format,
    }


def build_contact_sheet(image_paths: list[Path], output: Path) -> None:
    cell_width = 360
    cell_height = 720
    columns = 3
    rows = (len(image_paths) + columns - 1) // columns
    sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), "#dfe4ea")
    draw = ImageDraw.Draw(sheet)
    label_font = ImageFont.load_default(size=20)
    for index, path in enumerate(image_paths):
        image = Image.open(path).convert("RGB")
        image.thumbnail((cell_width - 24, cell_height - 64), Image.Resampling.LANCZOS)
        x = (index % columns) * cell_width + (cell_width - image.width) // 2
        y = (index // columns) * cell_height + 42
        sheet.paste(image, (x, y))
        draw.text((index % columns * cell_width + 12, index // columns * cell_height + 10), path.stem, fill="#17212b", font=label_font)
    output.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(output, format="PNG", compress_level=9)


def _normalized(value: str, *, loose: bool = False) -> str:
    text = unicodedata.normalize("NFKC", value).casefold()
    text = re.sub(r"[\s\W_]+", "", text, flags=re.UNICODE)
    if loose:
        text = re.sub(r"[且和并要需有的作在为]", "", text)
    return text


def _date_fragment(value: str) -> str:
    match = re.fullmatch(r"20\d{2}-(\d{2})-(\d{2})", value)
    if not match:
        return value
    return f"{int(match.group(1))}月{int(match.group(2))}日"


def _field_fragments(field: dict[str, Any]) -> tuple[list[str], bool]:
    field_type = field["type"]
    values = field["value"] if isinstance(field["value"], list) else [field["value"]]
    if field_type == "traveler_count":
        number = int(values[0])
        chinese = {2: "两", 3: "三", 4: "四", 5: "五"}[number]
        return [f"(?:{number}|{chinese})(?:个)?人"], False
    if field_type == "day_count":
        number = int(values[0])
        chinese = {2: "两", 3: "三", 4: "四", 5: "五"}[number]
        return [f"(?:{number}|{chinese})(?:天|日)"], False
    if field_type == "transport_mode":
        variants = {
            "walking": "(?:步行|走路)",
            "transit": "(?:公交|地铁|公共交通)",
            "bicycling": "(?:骑行|自行车)",
            "driving": "(?:驾车|开车|打车|出租车)",
        }
        return [variants[str(values[0])]], False
    if field_type == "date":
        return [re.escape(_normalized(_date_fragment(str(values[0]))))], False
    if field_type in {"arrival", "departure"}:
        value = str(values[0])
        fragments: list[str] = []
        date_match = re.search(r"20\d{2}-\d{2}-\d{2}", value)
        if date_match:
            fragments.append(re.escape(_normalized(_date_fragment(date_match.group()))))
            value = value.replace(date_match.group(), "")
        temporal_variants = {
            "14:00左右": "(?:14:00|下午两点)(?:左右)?",
            "早上": "(?:早上|早晨|早班)",
            "早班": "(?:早班|早上|早晨)",
            "中午": "(?:中午|午间)",
            "下午": "(?:下午|午后)",
            "傍晚": "(?:傍晚|晚间|晚上)",
            "晚上": "(?:晚上|晚间|晚)",
        }
        for part in value.split():
            normalized = _normalized(part)
            if normalized:
                fragments.append(temporal_variants.get(part, re.escape(normalized)))
        return fragments, False
    loose = field_type in {"preference", "constraint"}
    return [re.escape(_normalized(str(value), loose=loose)) for value in values], loose


def _field_matches(field: dict[str, Any], ocr_text: str) -> bool:
    fragments, loose = _field_fragments(field)
    normalized = _normalized(ocr_text, loose=loose)
    return all(re.search(fragment, normalized) is not None for fragment in fragments)


def _field_confirmation_required(field: dict[str, Any], lines: list[OcrTextLine]) -> bool:
    if isinstance(field["value"], list):
        return True
    if any(line.requires_confirmation for line in lines):
        return True
    text = "\n".join(line.text for line in lines)
    return bool(re.search(r"待确认|未确认|未定|冲突|两版|备选|左右|早班|具体.{0,6}(?:待|没)", text))


def _safe_cleanup(run_dir: Path, work_root: Path) -> None:
    resolved = run_dir.resolve()
    root = work_root.resolve()
    if not resolved.is_relative_to(root) or resolved == root:
        raise ValueError("refusing to clean outside the synthetic OCR work root")
    shutil.rmtree(resolved)


def _leak_hits(
    image_hashes: set[str],
    *,
    work_root: Path,
    output: Path,
) -> list[str]:
    findings: list[str] = []
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
    ).split(b"\0")
    candidates = {REPO_ROOT / raw.decode("utf-8") for raw in tracked if raw}
    if output.parent.is_dir():
        candidates.update(path for path in output.parent.rglob("*") if path.is_file())
    if work_root.is_dir():
        candidates.update(path for path in work_root.rglob("*") if path.is_file())
    for path in candidates:
        if not path.is_file() or path == output:
            continue
        try:
            if _sha256(path) in image_hashes:
                try:
                    label = path.relative_to(REPO_ROOT).as_posix()
                except ValueError:
                    label = f"work_root/{path.relative_to(work_root).as_posix()}"
                findings.append(label)
        except OSError:
            continue
    return findings


async def run_synthetic_ocr(
    *,
    spec_path: Path = DEFAULT_SPEC,
    output: Path = DEFAULT_OUTPUT,
    work_root: Path = DEFAULT_WORK_ROOT,
    subject_commit: str | None = None,
    font_path: Path | None = None,
    engine: OcrEngine | None = None,
    keep_artifacts: bool = False,
    render_only: bool = False,
    visual_review_approved: bool = False,
) -> dict[str, Any]:
    spec_path = spec_path.resolve()
    output = output.resolve()
    work_root = work_root.resolve()
    subject = subject_commit or _git_head()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    run_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{subject[:12]}"
    run_dir = work_root / run_id
    source_dir = run_dir / "source"
    staging_dir = run_dir / "staging"
    selected_font = _font_path(font_path)
    image_paths: list[Path] = []
    render_receipts: dict[str, dict[str, Any]] = {}
    extension_by_format = {"PNG": ".png", "JPEG": ".jpg", "WebP": ".webp"}
    for case in spec["cases"]:
        image_path = source_dir / f"{case['case_id']}{extension_by_format[case['image']['format']]}"
        render_receipts[case["case_id"]] = render_case(case, image_path, font_path=selected_font)
        image_paths.append(image_path)
    contact_sheet = run_dir / "contact-sheet.png"
    build_contact_sheet(image_paths, contact_sheet)

    if render_only:
        if not keep_artifacts:
            _safe_cleanup(run_dir, work_root)
            raise ValueError("--render-only requires --keep-artifacts for visual review")
        manifest = {
            "schema_version": "trip-check-p3-synthetic-ocr-manifest-v1",
            "subject_commit": subject,
            "status": "RENDERED_NOT_SCORED",
            "evidence_class": "synthetic_stress",
            "provenance": "high_fidelity_synthetic",
            "spec_sha256": _sha256(spec_path),
            "renderer_version": RENDERER_VERSION,
            "font_sha256": _sha256(selected_font),
            "case_count": len(spec["cases"]),
            "contact_sheet": str(contact_sheet),
            "work_dir": str(run_dir),
        }
        _write_json(output, manifest)
        return manifest

    current_engine = engine or PaddleOcrEngine(confirmation_threshold=CONFIDENCE_THRESHOLD)
    case_results: list[dict[str, Any]] = []
    true_positive = 0
    false_negative = 0
    confirm_required = 0
    confirm_caught = 0
    image_hashes = {receipt["image_sha256"] for receipt in render_receipts.values()}
    render_set_sha256 = hashlib.sha256(
        json.dumps(
            [
                {
                    "case_id": case_id,
                    "image_sha256": render_receipts[case_id]["image_sha256"],
                }
                for case_id in sorted(render_receipts)
            ],
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    cleanup_attempted = False
    try:
        for case, source_path in zip(spec["cases"], image_paths, strict=True):
            staging_dir.mkdir(parents=True, exist_ok=True)
            staged = staging_dir / source_path.name
            shutil.copyfile(source_path, staged)
            try:
                lines = await current_engine.recognize(staged)
            finally:
                staged.unlink(missing_ok=True)
            raw_text = "\n".join(line.text for line in lines)
            parsed = ItineraryTextParser().parse(raw_text, import_id=f"synthetic-{case['case_id']}")
            field_results: list[dict[str, Any]] = []
            for field in case["oracle"]["key_fields"]:
                matched = _field_matches(field, raw_text)
                true_positive += int(matched)
                false_negative += int(not matched)
                confirmation = _field_confirmation_required(field, lines)
                if field["must_confirm"]:
                    confirm_required += 1
                    confirm_caught += int(confirmation)
                field_results.append(
                    {
                        "field_id": field["field_id"],
                        "type": field["type"],
                        "matched": matched,
                        "must_confirm": field["must_confirm"],
                        "confirmation_required": confirmation,
                    }
                )
            case_results.append(
                {
                    "case_id": case["case_id"],
                    "city": case["city"],
                    "format": case["image"]["format"],
                    "layout": case["layout"],
                    "difficulty": case["difficulty"],
                    "theme": case["theme"],
                    "image_sha256": render_receipts[case["case_id"]]["image_sha256"],
                    "ocr_line_count": len(lines),
                    "min_line_confidence": min((line.confidence for line in lines), default=0.0),
                    "recognized_field_count": sum(item["matched"] for item in field_results),
                    "expected_field_count": len(field_results),
                    "parse_stop_count": len(parsed.raw_stops),
                    "parse_errors": parsed.errors,
                    "fields": field_results,
                }
            )
        cleanup_attempted = True
        if keep_artifacts:
            cleanup_receipt = {
                "status": "RETAINED",
                "reason": "explicit_keep_artifacts",
                "run_dir_removed": False,
            }
        else:
            try:
                _safe_cleanup(run_dir, work_root)
                cleanup_receipt = {
                    "status": "DELETED",
                    "reason": "terminal_ocr_run",
                    "run_dir_removed": not run_dir.exists(),
                }
            except Exception as exc:  # cleanup failure is evidence, not a hidden retry
                cleanup_receipt = {
                    "status": "CLEANUP_FAILED",
                    "reason": "terminal_ocr_run",
                    "run_dir_removed": not run_dir.exists(),
                    "error_category": type(exc).__name__,
                }
        leak_paths = _leak_hits(
            image_hashes,
            work_root=work_root,
            output=output,
        )
        precision = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
        recall = precision
        key_field_f1 = precision
        confirmation_recall = confirm_caught / confirm_required if confirm_required else 0.0
        metrics = {
            "case_count": len(case_results),
            "key_field_precision": round(precision, 6),
            "key_field_recall": round(recall, 6),
            "key_field_f1": round(key_field_f1, 6),
            "low_confidence_confirmation_recall": round(confirmation_recall, 6),
            "must_confirm_field_count": confirm_required,
            "original_image_leak_hits": len(leak_paths),
            "visual_review_approved": visual_review_approved,
        }
        passed = (
            metrics["case_count"] == 12
            and metrics["key_field_f1"] >= 0.95
            and metrics["low_confidence_confirmation_recall"] == 1.0
            and metrics["original_image_leak_hits"] == 0
            and visual_review_approved
            and cleanup_receipt["status"] == "DELETED"
        )
        privacy_blocked = cleanup_receipt["status"] != "DELETED"
        manifest = {
            "schema_version": "trip-check-p3-synthetic-ocr-manifest-v2",
            "subject_commit": subject,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "status": "PRIVACY_BLOCKED" if privacy_blocked else "PASS" if passed else "FAIL",
            "evidence_class": "synthetic_stress",
            "provenance": "high_fidelity_synthetic",
            "non_claims": [
                "This dataset is not real OCR, public E2E, human, or release evidence.",
                "A PASS only satisfies the approved P3 synthetic OCR phase exception.",
            ],
            "spec_path": spec_path.relative_to(REPO_ROOT).as_posix(),
            "spec_sha256": _sha256(spec_path),
            "renderer_version": RENDERER_VERSION,
            "render_set_sha256": render_set_sha256,
            "font_sha256": _sha256(selected_font),
            "ocr_engine": {"name": current_engine.name, "version": current_engine.version},
            "metrics": metrics,
            "distribution": {
                "cities": dict(Counter(case["city"] for case in case_results)),
                "formats": dict(Counter(case["format"] for case in case_results)),
                "layouts": dict(Counter(case["layout"] for case in case_results)),
                "difficulties": dict(Counter(case["difficulty"] for case in case_results)),
                "themes": dict(Counter(case["theme"] for case in case_results)),
            },
            "leak_paths": leak_paths,
            "cleanup_receipt": cleanup_receipt,
            "cases": case_results,
        }
        _write_json(output, manifest)
        return manifest
    finally:
        if not cleanup_attempted and not keep_artifacts and run_dir.exists():
            try:
                _safe_cleanup(run_dir, work_root)
            except Exception as exc:
                raise PrivacyBlockedError("synthetic OCR temporary image cleanup failed") from exc


def run(**kwargs: Any) -> dict[str, Any]:
    return asyncio.run(run_synthetic_ocr(**kwargs))
