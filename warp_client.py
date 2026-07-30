"""
Warp (Warpdrive) Shop Floor client for MM OPs Tracker.

Reads operation / work-order status and builds deep links so the tracker
can mirror clock-in and completion state from Warp.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import requests

WARP_BASE = os.environ.get("WARP_BASE_URL", "https://warpdrive.spacex.corp").rstrip("/")
SHOP_FLOOR = f"{WARP_BASE}/ShopFloor"

# Deep links into the Warp SPA (matches real browser URLs, e.g.
# https://warpdrive.spacex.corp/shop-floor/work-order/4598043/operation/56330969)
WO_UI_URL = f"{WARP_BASE}/shop-floor/work-order/{{work_order_id}}"
OP_UI_URL = (
    f"{WARP_BASE}/shop-floor/work-order/{{work_order_id}}/operation/{{operation_id}}"
)
# Fallback when only an OP id is known (WO id filled in after sync)
OP_ONLY_UI_URL = f"{WARP_BASE}/shop-floor/operation/{{operation_id}}"

AUTH_PATHS = [
    Path.home() / ".sx-ai-auth" / "sx-ai-auth.json",
    Path(__file__).resolve().parent / "warp_config.json",
]

# Warp operation Status strings we have seen in Shop Floor MES
DONE_STATUSES = {
    "complete",
    "completed",
    "closed",
    "done",
    "cancelled",
    "canceled",
    "scrapped",
}
IN_PROGRESS_STATUSES = {
    "running",
    "in progress",
    "inprogress",
    "in-progress",
    "started",
    "active",
    "open",
}
BLOCKED_STATUSES = {
    "blocked",
    "on hold",
    "onhold",
    "on-hold",
    "hold",
    "suspended",
}
TODO_STATUSES = {
    "unstarted",
    "not started",
    "notstarted",
    "pending",
    "ready",
    "available",
    "new",
    "released",
}


class WarpError(Exception):
    """Raised when a Warp API call fails."""


@dataclass
class WarpOpSnapshot:
    """Normalized view of a Warp operation for the tracker."""

    operation_id: int
    work_order_id: Optional[int] = None
    sequence_number: Optional[int] = None
    title: str = ""
    warp_status: str = ""
    tracker_status: str = "To Do"
    clocked_in_users: list[str] = field(default_factory=list)
    open_quality_calls: list[int] = field(default_factory=list)
    complete_qty: Optional[float] = None
    planned_qty: Optional[float] = None
    actual_hours: Optional[float] = None
    shop_resource: str = ""
    display_name: str = ""
    work_order_status: str = ""
    work_order_base_id: str = ""
    op_url: str = ""
    wo_url: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def label(self) -> str:
        """Human-facing WO # / OP # label (BaseID + sequence)."""
        wo = self.work_order_base_id or (
            str(self.work_order_id) if self.work_order_id else ""
        )
        if wo and self.sequence_number is not None:
            return f"WO {wo} · OP {self.sequence_number}"
        if wo:
            return f"WO {wo}"
        if self.sequence_number is not None:
            return f"OP {self.sequence_number}"
        return f"OP id {self.operation_id}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "operation_id": self.operation_id,
            "work_order_id": self.work_order_id,
            "sequence_number": self.sequence_number,
            "title": self.title,
            "warp_status": self.warp_status,
            "tracker_status": self.tracker_status,
            "clocked_in_users": self.clocked_in_users,
            "open_quality_calls": self.open_quality_calls,
            "complete_qty": self.complete_qty,
            "planned_qty": self.planned_qty,
            "actual_hours": self.actual_hours,
            "shop_resource": self.shop_resource,
            "display_name": self.display_name,
            "work_order_status": self.work_order_status,
            "work_order_base_id": self.work_order_base_id,
            "op_url": self.op_url,
            "wo_url": self.wo_url,
        }


def operation_url(
    operation_id: int | str, work_order_id: int | str | None = None
) -> str:
    if work_order_id is not None and str(work_order_id).strip():
        return OP_UI_URL.format(
            work_order_id=work_order_id, operation_id=operation_id
        )
    return OP_ONLY_UI_URL.format(operation_id=operation_id)


def work_order_url(work_order_id: int | str) -> str:
    return WO_UI_URL.format(work_order_id=work_order_id)


def parse_warp_ref(text: str) -> dict[str, Optional[int]]:
    """
    Extract operation_id / work_order_id from a pasted Warp URL, bare ID,
    or strings like 'OP 12345' / 'WO 67890'.

    Real browser example:
      .../shop-floor/work-order/4598043/operation/56330969/test-plan/...
    """
    result: dict[str, Optional[int]] = {"operation_id": None, "work_order_id": None}
    if not text or not str(text).strip():
        return result
    s = str(text).strip()

    # Full path: /work-order/{wo}/operation/{op}
    m = re.search(
        r"/work-?orders?/(\d+)/operations?/(\d+)", s, re.IGNORECASE
    )
    if m:
        result["work_order_id"] = int(m.group(1))
        result["operation_id"] = int(m.group(2))
        return result

    op_patterns = [
        r"/operations?/(\d+)",
        r"[#&?]operation(?:Id|ID)?[=/](\d+)",
        r"\bOP[:\s#-]*(\d+)\b",
        r"\boperation[:\s#-]*(\d+)\b",
    ]
    wo_patterns = [
        r"/work-?orders?/(\d+)",
        r"[#&?]workOrder(?:Id|ID)?[=/](\d+)",
        r"\bWO[:\s#-]*(\d+)\b",
        r"\bwork[\s-]?order[:\s#-]*(\d+)\b",
    ]

    for pat in op_patterns:
        m = re.search(pat, s, re.IGNORECASE)
        if m:
            result["operation_id"] = int(m.group(1))
            break

    for pat in wo_patterns:
        m = re.search(pat, s, re.IGNORECASE)
        if m:
            result["work_order_id"] = int(m.group(1))
            break

    # Bare numeric ID — treat as operation ID if nothing else matched
    if result["operation_id"] is None and result["work_order_id"] is None:
        if re.fullmatch(r"\d+", s):
            result["operation_id"] = int(s)

    return result


def load_token(explicit: Optional[str] = None) -> Optional[str]:
    """Resolve a Warp Bearer token from arg, env, or local config files."""
    if explicit and explicit.strip():
        return explicit.strip()

    for key in ("WARP_API_TOKEN", "WARPDRIVE_TOKEN", "WARP_TOKEN"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()

    for path in AUTH_PATHS:
        try:
            if not path.exists():
                continue
            data = json.loads(path.read_text())
            for key in ("WARP_API_TOKEN", "WARPDRIVE_TOKEN", "WARP_TOKEN", "token"):
                val = data.get(key)
                if val and str(val).strip():
                    return str(val).strip()
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return None


def map_warp_status_to_tracker(
    warp_status: str,
    *,
    clocked_in: bool = False,
    has_open_quality_calls: bool = False,
    complete_qty: Optional[float] = None,
    planned_qty: Optional[float] = None,
) -> str:
    """
    Map Warp OP state → tracker status.

    Priority:
      1. Done (complete/closed, or full qty complete)
      2. Blocked (quality calls / hold)
      3. In Progress (clocked-in users or running status)
      4. To Do
    """
    status = (warp_status or "").strip().lower()

    if status in DONE_STATUSES:
        return "Done"

    if (
        planned_qty is not None
        and complete_qty is not None
        and planned_qty > 0
        and complete_qty >= planned_qty
    ):
        return "Done"

    if has_open_quality_calls or status in BLOCKED_STATUSES:
        return "Blocked"

    if clocked_in or status in IN_PROGRESS_STATUSES:
        return "In Progress"

    if status in TODO_STATUSES or not status:
        return "To Do"

    # Unknown Warp status: clocked-in still wins; otherwise leave as To Do
    if clocked_in:
        return "In Progress"
    return "To Do"


class WarpClient:
    def __init__(
        self,
        token: Optional[str] = None,
        base_url: str = WARP_BASE,
        timeout: float = 20.0,
    ):
        self.token = load_token(token)
        self.base_url = base_url.rstrip("/")
        self.shop_floor = f"{self.base_url}/ShopFloor"
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "MM-OPs-Tracker/1.0",
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"

    @property
    def is_authenticated(self) -> bool:
        return bool(self.token)

    def set_token(self, token: str) -> None:
        self.token = token.strip() if token else None
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
        else:
            self.session.headers.pop("Authorization", None)

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        if not self.token:
            raise WarpError(
                "No Warp API token configured. Set WARP_API_TOKEN or paste a token "
                "in the sidebar (Warp Settings)."
            )
        url = f"{self.shop_floor}{path}"
        try:
            resp = self.session.get(url, params=params, timeout=self.timeout)
        except requests.RequestException as exc:
            raise WarpError(f"Network error calling Warp: {exc}") from exc

        if resp.status_code in (401, 403):
            raise WarpError(
                "Warp authentication failed (401/403). Token may be missing or expired. "
                "Refresh via `sx-ai-sandbox setup warp --re-auth` or paste a new token."
            )
        if resp.status_code == 404:
            raise WarpError(f"Not found in Warp: {path}")
        if resp.status_code >= 400:
            body = (resp.text or "")[:300]
            raise WarpError(f"Warp API error {resp.status_code}: {body}")

        if not resp.content:
            return None
        try:
            return resp.json()
        except json.JSONDecodeError as exc:
            raise WarpError(f"Invalid JSON from Warp: {exc}") from exc

    def get_operation(self, operation_id: int) -> dict[str, Any]:
        data = self._get(f"/api/v1/operations/{int(operation_id)}")
        return data if isinstance(data, dict) else {}

    def get_work_order(self, work_order_id: int) -> dict[str, Any]:
        data = self._get(f"/api/v1/work-orders/{int(work_order_id)}")
        return data if isinstance(data, dict) else {}

    def get_work_order_operations(
        self, work_order_id: int, available_to_work: bool = False
    ) -> dict[str, Any]:
        data = self._get(
            f"/api/v1/work-orders/{int(work_order_id)}/operations",
            params={"availableToWork": str(available_to_work).lower()},
        )
        return data if isinstance(data, dict) else {}

    def get_labor_tickets(self, operation_id: int) -> list[dict[str, Any]]:
        data = self._get(f"/api/v1/labor-tickets/operation/{int(operation_id)}")
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            content = data.get("Content")
            if isinstance(content, list):
                return content
            # Some responses wrap a single object
            if content is not None and not isinstance(content, list):
                return [content] if content else []
        return []

    def get_work_order_by_base_id(
        self, base_id: str, lot_id: Optional[str] = None
    ) -> dict[str, Any]:
        params: dict[str, str] = {"baseID": str(base_id).strip()}
        if lot_id:
            params["lotID"] = str(lot_id).strip()
        data = self._get("/api/v1/work-orders", params=params)
        return data if isinstance(data, dict) else {}

    def snapshot_operation(self, operation_id: int) -> WarpOpSnapshot:
        """Fetch OP + labor tickets and produce a tracker-ready snapshot."""
        op = self.get_operation(int(operation_id))
        if not op:
            raise WarpError(f"Empty response for operation {operation_id}")

        op_id = int(op.get("OperationID") or operation_id)
        wo_id = op.get("WorkOrderID")
        try:
            wo_id_int = int(wo_id) if wo_id is not None else None
        except (TypeError, ValueError):
            wo_id_int = None

        clocked_users: list[str] = []
        for u in op.get("ClockedInUsers") or []:
            if not isinstance(u, dict):
                continue
            name = (
                u.get("DisplayName")
                or u.get("ADUsername")
                or " ".join(filter(None, [u.get("FirstName"), u.get("LastName")])).strip()
            )
            if name:
                clocked_users.append(str(name))

        # Labor tickets as secondary clock-in signal
        try:
            tickets = self.get_labor_tickets(op_id)
        except WarpError:
            tickets = []

        for t in tickets:
            if not isinstance(t, dict):
                continue
            if t.get("IsClockedIn"):
                clocked_user = t.get("ClockedInUser")
                clocked_name = (
                    clocked_user.get("DisplayName")
                    if isinstance(clocked_user, dict)
                    else None
                )
                name = t.get("UserDisplayName") or t.get("ADUsername") or clocked_name
                if name and name not in clocked_users:
                    clocked_users.append(str(name))

        open_calls = [int(c) for c in (op.get("OpenQualityCallIDs") or []) if c is not None]
        shop = op.get("ShopResource") or {}
        shop_code = ""
        if isinstance(shop, dict):
            shop_code = (
                shop.get("ShopResourceCode")
                or shop.get("Code")
                or shop.get("Name")
                or ""
            )

        warp_status = str(op.get("Status") or "")
        complete_qty = op.get("CompleteQuantity")
        planned_qty = op.get("PlannedQuantity")
        try:
            complete_f = float(complete_qty) if complete_qty is not None else None
        except (TypeError, ValueError):
            complete_f = None
        try:
            planned_f = float(planned_qty) if planned_qty is not None else None
        except (TypeError, ValueError):
            planned_f = None

        tracker_status = map_warp_status_to_tracker(
            warp_status,
            clocked_in=bool(clocked_users),
            has_open_quality_calls=bool(open_calls),
            complete_qty=complete_f,
            planned_qty=planned_f,
        )

        wo_status = ""
        wo_base = ""
        wo_display = ""
        if wo_id_int:
            try:
                wo = self.get_work_order(wo_id_int)
                wo_status = str(wo.get("StatusValue") or "")
                wo_base = str(wo.get("BaseID") or "")
                wo_display = str(wo.get("DisplayName") or "")
                # If WO itself is closed/complete and OP wasn't marked done, still done
                if tracker_status != "Done" and (wo_status or "").lower() in DONE_STATUSES:
                    tracker_status = "Done"
            except WarpError:
                pass

        actual = op.get("ActualTime")
        try:
            actual_f = float(actual) if actual is not None else None
        except (TypeError, ValueError):
            actual_f = None

        seq_raw = op.get("SequenceNumber")
        try:
            seq_num = int(seq_raw) if seq_raw is not None else None
        except (TypeError, ValueError):
            seq_num = None

        return WarpOpSnapshot(
            operation_id=op_id,
            work_order_id=wo_id_int,
            sequence_number=seq_num,
            title=str(op.get("Title") or ""),
            warp_status=warp_status,
            tracker_status=tracker_status,
            clocked_in_users=clocked_users,
            open_quality_calls=open_calls,
            complete_qty=complete_f,
            planned_qty=planned_f,
            actual_hours=actual_f,
            shop_resource=str(shop_code),
            display_name=wo_display,
            work_order_status=wo_status,
            work_order_base_id=wo_base,
            op_url=operation_url(op_id, wo_id_int),
            wo_url=work_order_url(wo_id_int) if wo_id_int else "",
            raw=op,
        )

    def lookup_and_snapshot(
        self,
        *,
        operation_id: Optional[int] = None,
        work_order_id: Optional[int] = None,
        base_id: Optional[str] = None,
        sequence: Optional[int] = None,
    ) -> WarpOpSnapshot:
        """
        Resolve identifiers to an operation snapshot.

        Preferred shop-floor path: BaseID (WO number) + SequenceNumber (OP number).
        Also accepts internal WorkOrderID / OperationID when known.
        """
        if operation_id and not (base_id and sequence is not None):
            # Direct OP id only when caller did not provide WO# + OP#
            return self.snapshot_operation(int(operation_id))

        wo_id = work_order_id
        resolved_base = (str(base_id).strip() if base_id else "") or ""

        if not wo_id and resolved_base:
            wo = self.get_work_order_by_base_id(resolved_base)
            wo_id = wo.get("WorkOrderID")
            if not resolved_base:
                resolved_base = str(wo.get("BaseID") or resolved_base)
            if not wo_id:
                raise WarpError(
                    f"No work order found for WO number (BaseID)={resolved_base}"
                )

        if not wo_id and operation_id:
            return self.snapshot_operation(int(operation_id))

        if not wo_id:
            raise WarpError(
                "Enter a WO number (Base ID, e.g. 3462114) and OP number "
                "(sequence, e.g. 62), or paste a Warp URL."
            )

        wo_ops = self.get_work_order_operations(int(wo_id), available_to_work=False)
        if not resolved_base:
            resolved_base = str(wo_ops.get("BaseID") or "")
        operations = wo_ops.get("Operations") or []
        if not operations:
            raise WarpError(
                f"Work order {resolved_base or wo_id} has no operations"
            )

        chosen = None
        if sequence is not None:
            seq_int = int(sequence)
            for op in operations:
                try:
                    op_seq = int(op.get("SequenceNumber"))
                except (TypeError, ValueError):
                    continue
                if op_seq == seq_int:
                    chosen = op
                    break
            if chosen is None:
                available = sorted(
                    {
                        int(o.get("SequenceNumber"))
                        for o in operations
                        if o.get("SequenceNumber") is not None
                    },
                    key=lambda x: x,
                )
                raise WarpError(
                    f"No OP number {seq_int} on WO {resolved_base or wo_id}. "
                    f"Available OP numbers: {available[:40]}"
                    + ("…" if len(available) > 40 else "")
                )
        else:
            # Prefer first non-complete op with clocked users, else first open, else first
            for op in operations:
                users = op.get("ClockedInUsers") or []
                st = str(op.get("Status") or "").lower()
                if users and st not in DONE_STATUSES:
                    chosen = op
                    break
            if chosen is None:
                for op in operations:
                    st = str(op.get("Status") or "").lower()
                    if st not in DONE_STATUSES:
                        chosen = op
                        break
            if chosen is None:
                chosen = operations[0]

        op_id = chosen.get("OperationID")
        if not op_id:
            raise WarpError("Operation listing missing OperationID")
        return self.snapshot_operation(int(op_id))


def apply_snapshot_to_task(task: dict, snap: WarpOpSnapshot, *, force_status: bool = True) -> list[str]:
    """
    Update a tracker task dict from a Warp snapshot.
    Returns list of human-readable change descriptions.
    """
    changes: list[str] = []

    task["warp_operation_id"] = snap.operation_id
    if snap.work_order_id:
        task["warp_work_order_id"] = snap.work_order_id
    if snap.work_order_base_id:
        task["warp_base_id"] = snap.work_order_base_id
    if snap.sequence_number is not None:
        task["warp_sequence"] = snap.sequence_number
    task["warp_op_url"] = snap.op_url
    if snap.wo_url:
        task["warp_wo_url"] = snap.wo_url
    task["warp_status"] = snap.warp_status
    task["warp_clocked_in"] = snap.clocked_in_users
    task["warp_last_sync"] = __import__("datetime").datetime.now().isoformat()

    if snap.title and (not task.get("name") or task.get("name") in ("", "Unassigned")):
        task["name"] = snap.title
        changes.append(f"name → {snap.title}")

    # Prefer clocked-in user as assignee when available
    if snap.clocked_in_users:
        primary = snap.clocked_in_users[0]
        if task.get("assignee") in (None, "", "Unassigned") or task.get("assignee") != primary:
            if task.get("assignee") != primary:
                changes.append(f"assignee → {primary}")
            task["assignee"] = primary

    if force_status and snap.tracker_status != task.get("status"):
        old = task.get("status")
        task["status"] = snap.tracker_status
        changes.append(f"status {old} → {snap.tracker_status}")

    # Append a sync note when something meaningful changed
    if changes:
        from datetime import datetime

        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        clocked = (
            f" clocked-in: {', '.join(snap.clocked_in_users)}"
            if snap.clocked_in_users
            else ""
        )
        note = (
            f"[{timestamp}] Warp sync {snap.label()}: "
            f"{snap.warp_status or 'n/a'} → {snap.tracker_status}{clocked}"
        )
        existing = task.get("notes") or ""
        task["notes"] = (existing + "\n" + note).strip()

    return changes
