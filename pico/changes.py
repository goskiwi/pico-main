"""Build the final file-tool diff from Session mutation receipts."""

from itertools import pairwise
from pathlib import Path

from .mutations import content_revision, unified_text_diff


def build_final_diff(session, artifacts):
    grouped = {}
    for receipt in session.mutations:
        if receipt.get("status") == "not_applied":
            continue
        grouped.setdefault(receipt["path"], []).append(receipt)
    root = Path(session.workspace_root)
    chunks = []
    changed_paths = []
    uncertain_paths = []
    unavailable = []
    for logical, receipts in grouped.items():
        try:
            first, last = receipts[0], receipts[-1]
            if first.get("preimage_id"):
                _descriptor, before = artifacts.read_internal(
                    session.id,
                    first["preimage_id"],
                    expected_kind="workspace_preimage",
                )
                before_exists = True
            else:
                before, before_exists = b"", False
            target = root / logical
            if target.resolve() != target or not target.is_relative_to(root):
                raise ValueError("diff target was redirected")
            after_exists = target.is_file()
            after = target.read_bytes() if after_exists else b""
            actual = content_revision(after) if after_exists else "absent"
            if (last.get("status") != "applied" or actual != last.get("after_revision")
                    or any(left["after_revision"] != right["before_revision"]
                           for left, right in pairwise(receipts))):
                uncertain_paths.append(logical)
            before_revision = content_revision(before) if before_exists else "absent"
            if before_revision == actual:
                continue
            changed_paths.append(logical)
            chunks.append(
                unified_text_diff(
                    logical,
                    before.decode("utf-8"),
                    after.decode("utf-8"),
                    before_exists=before_exists,
                    after_exists=after_exists,
                )
            )
        except (OSError, UnicodeError, ValueError) as exc:
            unavailable.append({"path": logical, "reason": str(exc)})
    content = "".join(chunks)
    descriptor = artifacts.write_final_diff(session.id, content) if content else None
    return {
        "artifact_id": descriptor["artifact_id"] if descriptor else "",
        "changed_paths": changed_paths,
        "external_or_uncertain_paths": uncertain_paths,
        "unavailable": unavailable,
        "scope": "File tools only; command effects are reported by their tool results.",
    }
