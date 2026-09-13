"""Private navigation between immutable trip revisions, never public tokens."""

from typing import Any

from app.trip_understanding.errors import CommandTargetChangedError


def advance_edit_history(
    revision: int,
    proposal: dict[str, Any],
    *,
    can_undo: bool,
    command_type: str,
) -> tuple[int, dict[str, list[int]]]:
    """Return the business snapshot to copy and the new undo/redo stacks.

    Legacy results only promise one undo. Do not reconstruct older edit history
    from consecutive revision numbers (which also contain past undo operations).
    """
    saved = proposal.get("edit_history")
    if saved is None:
        undo = [revision - 1] if can_undo and revision > 1 else []
        redo: list[int] = []
    else:
        if not isinstance(saved, dict) or set(saved) != {"undo", "redo"}:
            raise CommandTargetChangedError("edit history is unavailable")
        for stack in saved.values():
            if not isinstance(stack, list) or any(type(value) is not int or not 1 <= value < revision for value in stack):
                raise CommandTargetChangedError("edit history is unavailable")
        undo, redo = list(saved["undo"]), list(saved["redo"])
    source_revision = revision
    if command_type == "UNDO":
        if not undo:
            raise CommandTargetChangedError("no edit is available to undo")
        source_revision = undo.pop()
        redo.append(revision)
    elif command_type == "REDO":
        if not redo:
            raise CommandTargetChangedError("no edit is available to redo")
        source_revision = redo.pop()
        undo.append(revision)
    else:
        undo.append(revision)
        redo = []
    return source_revision, {"undo": undo, "redo": redo}
