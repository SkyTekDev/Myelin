from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bridge.api_models import IoceClaimRequest


# CMS lets a Medicare contractor bypass specific I/OCE edits on a claim line
# (CR 10988, effective April 2019). The bridge passes a per-line bypass list to
# the unmodified CMS editor, limited to edits the deployment allows through
# IOCE_EDIT_BYPASS_ALLOWED. An empty or missing allowlist turns the feature off.


def normalize_edit(value: str) -> str:
    """Return an I/OCE edit number as three digits, such as "012"."""
    edit = value.strip()

    if not edit.isdigit() or not 1 <= len(edit) <= 3 or int(edit) == 0:
        raise ValueError(f"Invalid I/OCE edit number: {value!r}")

    return str(int(edit)).zfill(3)


def parse_allowlist(raw: str | None) -> frozenset[str]:
    """Parse a comma- or space-separated list of edit numbers."""
    if raw is None or not raw.strip():
        return frozenset()

    items = raw.replace(";", ",").replace(" ", ",").split(",")
    return frozenset(normalize_edit(item) for item in items if item.strip())


def disallowed_bypasses(
    payload: IoceClaimRequest,
    allowed: frozenset[str],
) -> list[dict[str, Any]]:
    """Lines that ask to bypass an edit the deployment does not allow."""
    return [
        {
            "claimLineId": str(line.claimLineId),
            "lineNumber": line.lineNumber,
            "edits": [edit for edit in line.editBypass if edit not in allowed],
        }
        for line in payload.claimLines
        if any(edit not in allowed for edit in line.editBypass)
    ]
