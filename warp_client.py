"""
Warp Shop Floor client for MM OPs Tracker.

Design rules (keep simple, avoid past bugs):
  - Operation ID is the only required identifier to sync.
  - Work Order ID is taken from the API response for that OP (never guessed).
  - Never auto-pick an OP from a work order.
  - Deep links use WO + OP from API data.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import requests

WARP_BASE = os.environ.get("WARP_BASE_URL", "https://warpdrive.spacex.corp").rstrip("/")
SHOP_FLOOR = f"{WARP_BASE}/ShopFloor"

AUTH_PATHS = [
    Path.home() / ".sx-ai-auth" / "sx-ai-auth.json",
    Path(__file__).resolve().parent / "warp_config.json",
]

# MES single-letter status codes (Shop Floor) + common words
DONE = {"c", "complete", "completed", "closed", "done", "x", "cancelled", "canceled", "scrapped"}
IN_PROGRESS = {"s", "running", "in progress", "inprogress", "in-progress", "started", "active", "w"}
BLOCKED = {"h", "blocked", "hold", "on hold", "onhold", "on-hold", "suspended"}
TODO = {"u", "r", "unstarted", "not started", "notstarted", "pending", "ready", "released", "new"}


class WarpError(Exception):
    pass


@dataclass
class OpSnapshot:
    operation_id: int
    work_order_id: Optional[int] = None
    title: str = ""
    warp_status: str = ""
    tracker_status: str = "To Do"
    clocked_in: list[str] = field(default_factory=list)
    complete_qty: Optional[float] = None
    planned_qty: Optional[float] = None
    available_qty: Optional[float] = None
    op_url: str = ""
    wo_url: str = ""

    def to_task_fields(self) -> dict[str, Any]:
        return {
            "warp_operation_id": self.operation_id,
            "warp_work_order_id": self.work_order_id,
            "warp_status": self.warp_status,
            "warp_clocked_in": list(self.clocked_in),
            "warp_op_url": self.op_url,
            "warp_wo_url": self.wo_url,
            "warp_last_sync": datetime.now().isoformat(),
        }


# ---------- auth ----------

def load_token(explicit: Optional[str] = None) -> Optional[str]:
    if explicit and explicit.strip():
        return explicit.strip()
    for key in ("WARP_API_TOKEN", "WARPDRIVE_TOKEN", "WARP_TOKEN"):
        val = os.environ.get(key)
        if val and val.strip():
            return val.strip()
    for path in AUTH_PATHS:
        try:
            if not path.is_file():
                continue
            data = json.loads(path.read_text(encoding="utf-8"))
            for key in ("WARP_API_TOKEN", "WARPDRIVE_TOKEN", "WARP_TOKEN", "token"):
                val = data.get(key)
                if val and str(val).strip():
                    return str(val).strip()
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return None


# ---------- URLs & parsing ----------

def op_url(operation_id: int, work_order_id: Optional[int] = None) -> str:
    """Browser deep link. Prefer WO+OP path (real SPA route)."""
    if work_order_id:
        return (
            f"{WARP_BASE}/shop-floor/work-order/{int(work_order_id)}"
            f"/operation/{int(operation_id)}"
        )
    # Fallback only — may 404 without WO; sync fills WO ASAP
    return f"{WARP_BASE}/ShopFloor/#/operations/{int(operation_id)}"


def wo_url(work_order_id: int) -> str:
    return f"{WARP_BASE}/shop-floor/work-order/{int(work_order_id)}"


def parse_warp_input(text: str) -> dict[str, Optional[int]]:
    """
    Parse a pasted Warp URL or bare number into operation_id / work_order_id.

    Correct browser URL shape:
      .../shop-floor/work-order/{WO}/operation/{OP}
    """
    out: dict[str, Optional[int]] = {"operation_id": None, "work_order_id": None}
    if not text or not str(text).strip():
        return out
    s = str(text).strip()

    # .../work-order/819689/operation/8246239
    m = re.search(
        r"/work-?orders?/(\d+)/operations?/(\d+)", s, re.IGNORECASE
    )
    if m:
        out["work_order_id"] = int(m.group(1))
        out["operation_id"] = int(m.group(2))
        return out

    # #/work-orders/819689/operations/8246239
    m = re.search(
        r"work-?orders?/(\d+)/operations?/(\d+)", s, re.IGNORECASE
    )
    if m:
        out["work_order_id"] = int(m.group(1))
        out["operation_id"] = int(m.group(2))
        return out

    # .../operations/8246239  or  OP 8246239
    m = re.search(r"/operations?/(\d+)", s, re.IGNORECASE)
    if m:
        out["operation_id"] = int(m.group(1))
    m = re.search(r"\bOP[:\s#-]*(\d+)\b", s, re.IGNORECASE)
    if m and out["operation_id"] is None:
        out["operation_id"] = int(m.group(1))

    m = re.search(r"/work-?orders?/(\d+)", s, re.IGNORECASE)
    if m and out["work_order_id"] is None:
        out["work_order_id"] = int(m.group(1))
    m = re.search(r"\bWO[:\s#-]*(\d+)\b", s, re.IGNORECASE)
    if m and out["work_order_id"] is None:
        out["work_order_id"] = int(m.group(1))

    # Bare number → Operation ID only (never invent a WO)
    if out["operation_id"] is None and out["work_order_id"] is None:
        if re.fullmatch(r"\d+", s):
            n = int(s)
            # Sequence numbers are small; real OP ids are large
            if n >= 1000:
                out["operation_id"] = n
    return out


# ---------- response helpers ----------

def unwrap(data: Any) -> Any:
    """Unwrap Warp ServiceResponse {Content: ...} envelopes."""
    for _ in range(4):
        if isinstance(data, dict) and data.get("Content") is not None:
            data = data["Content"]
            continue
        break
    return data


def _fnum(v: Any) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _truthy(v: Any) -> bool:
    if v is True or v == 1:
        return True
    if isinstance(v, str) and v.strip().lower() in {"true", "1", "yes"}:
        return True
    return False


def _empty_ts(v: Any) -> bool:
    if v is None or v == "" or v == 0:
        return True
    if isinstance(v, str) and v.strip().startswith(("0001-", "0000-")):
        return True
    return False


def _user_name(u: Any) -> str:
    if isinstance(u, str):
        return u.strip()
    if not isinstance(u, dict):
        return ""
    name = (
        u.get("DisplayName")
        or u.get("ADUsername")
        or u.get("UserDisplayName")
        or " ".join(filter(None, [u.get("FirstName"), u.get("LastName")])).strip()
    )
    return str(name).strip() if name else ""


def ticket_open(t: dict[str, Any]) -> bool:
    """SlimLaborTicket: IsClockedIn, ClockInTime, ClockOutTime."""
    if _truthy(t.get("IsClockedIn")) or _truthy(t.get("ClockedInFlag")):
        return True
    cin = t.get("ClockInTime") or t.get("ClockIn") or t.get("ActClockIn")
    cout = t.get("ClockOutTime") if "ClockOutTime" in t else t.get("ClockOut")
    if "ClockOutTime" not in t and "ClockOut" not in t:
        cout = t.get("ActClockOut")
    return bool(cin) and _empty_ts(cout)


def clocked_users(op: dict[str, Any], tickets: list[dict[str, Any]]) -> list[str]:
    names: list[str] = []
    raw = op.get("ClockedInUsers") or []
    if isinstance(raw, dict):
        raw = [raw]
    for u in raw:
        n = _user_name(u)
        if n and n not in names:
            names.append(n)
    for t in tickets:
        if not ticket_open(t):
            continue
        n = (
            t.get("UserDisplayName")
            or t.get("ADUsername")
            or _user_name(t.get("ClockedInUser"))
        )
        if not n:
            uid = t.get("UserID") or t.get("ClockedInUserID")
            n = f"User {uid}" if uid is not None else "Clocked-in user"
        if n not in names:
            names.append(str(n))
    return names


def map_status(
    warp_status: str,
    *,
    clocked_in: bool,
    complete_qty: Optional[float] = None,
    planned_qty: Optional[float] = None,
    available_qty: Optional[float] = None,
) -> str:
    """
    Priority:
      1. Cancelled → Done
      2. Anyone clocked in → In Progress
      3. Fully complete by qty → Done
      4. Status complete (C) if qty not still open → Done
      5. Blocked / running / released / blank
    """
    st = (warp_status or "").strip().lower()

    if st in {"x", "cancelled", "canceled", "scrapped"}:
        return "Done"

    if clocked_in:
        return "In Progress"

    qty_done = (
        planned_qty is not None
        and complete_qty is not None
        and planned_qty > 0
        and complete_qty >= planned_qty
    )
    qty_open = False
    if available_qty is not None and available_qty > 0:
        qty_open = True
    if (
        planned_qty is not None
        and complete_qty is not None
        and planned_qty > 0
        and complete_qty < planned_qty
    ):
        qty_open = True

    if qty_done and not qty_open:
        return "Done"

    if st in DONE:
        return "In Progress" if qty_open else "Done"

    if st in BLOCKED:
        return "Blocked"
    if st in IN_PROGRESS or not st:
        return "In Progress"
    if st in TODO:
        return "To Do"
    return "To Do"


# ---------- client ----------

class WarpClient:
    def __init__(self, token: Optional[str] = None, timeout: float = 25.0):
        self.token = load_token(token)
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Accept": "application/json",
                "User-Agent": "MM-OPs-Tracker/2.0",
            }
        )
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
            self.session.headers["ApiToken"] = self.token

    @property
    def ok(self) -> bool:
        return bool(self.token)

    def set_token(self, token: str) -> None:
        self.token = (token or "").strip() or None
        if self.token:
            self.session.headers["Authorization"] = f"Bearer {self.token}"
            self.session.headers["ApiToken"] = self.token
        else:
            self.session.headers.pop("Authorization", None)
            self.session.headers.pop("ApiToken", None)

    def _get(self, path: str, params: Optional[dict] = None) -> Any:
        if not self.token:
            raise WarpError(
                "No Warp token. Run: sx-ai-sandbox setup warp --re-auth "
                "or paste WARP_API_TOKEN in the sidebar."
            )
        url = f"{SHOP_FLOOR}{path}"
        p = dict(params or {})
        p.setdefault("ApiToken", self.token)
        try:
            resp = self.session.get(url, params=p, timeout=self.timeout)
        except requests.RequestException as exc:
            raise WarpError(
                f"Network error reaching Warp ({WARP_BASE}). "
                f"Run this app on your Windows host (corp network), not the sandbox. "
                f"Detail: {exc}"
            ) from exc

        if resp.status_code in (401, 403):
            raise WarpError(
                "Warp auth failed (401/403). Re-run: sx-ai-sandbox setup warp --re-auth"
            )
        if resp.status_code == 404:
            raise WarpError(f"Not found (404): {path} — check the Operation ID")
        if resp.status_code >= 400:
            raise WarpError(f"Warp API {resp.status_code}: {(resp.text or '')[:250]}")
        if not resp.content:
            return None
        try:
            return unwrap(resp.json())
        except json.JSONDecodeError as exc:
            raise WarpError(f"Invalid JSON from Warp: {exc}") from exc

    def get_operation(self, operation_id: int) -> dict[str, Any]:
        data = self._get(f"/api/v1/operations/{int(operation_id)}")
        if not isinstance(data, dict) or not data:
            raise WarpError(f"Empty response for operation {operation_id}")
        return data

    def get_labor_tickets(self, operation_id: int) -> list[dict[str, Any]]:
        try:
            data = self._get(f"/api/v1/labor-tickets/operation/{int(operation_id)}")
        except WarpError:
            return []
        if isinstance(data, list):
            return [t for t in data if isinstance(t, dict)]
        if isinstance(data, dict):
            for key in ("Content", "LaborTickets", "Items"):
                if isinstance(data.get(key), list):
                    return [t for t in data[key] if isinstance(t, dict)]
        return []

    def fetch_op(self, operation_id: int) -> OpSnapshot:
        """
        Fetch exactly this Operation ID. Never substitutes another OP.
        Work Order ID comes from the API response for this OP.
        """
        op_id = int(operation_id)
        if op_id < 1000:
            raise WarpError(
                f"{op_id} looks like a sequence number, not an Operation ID. "
                f"Use the number after /operation/ in the browser URL (usually 6+ digits)."
            )

        op = self.get_operation(op_id)
        # Trust requested ID; still read OperationID if present
        api_op_id = op.get("OperationID")
        if api_op_id is not None and int(api_op_id) != op_id:
            raise WarpError(
                f"API returned OperationID {api_op_id} for request {op_id} — refusing to use it"
            )

        wo_raw = op.get("WorkOrderID")
        try:
            wo_id = int(wo_raw) if wo_raw is not None else None
        except (TypeError, ValueError):
            wo_id = None

        tickets = self.get_labor_tickets(op_id)
        users = clocked_users(op, tickets)

        raw_status = op.get("Status")
        if isinstance(raw_status, dict):
            raw_status = (
                raw_status.get("Name")
                or raw_status.get("Value")
                or raw_status.get("Code")
                or ""
            )
        warp_status = str(raw_status if raw_status is not None else "")

        complete = _fnum(op.get("CompleteQuantity") or op.get("CompletedQuantity"))
        planned = _fnum(op.get("PlannedQuantity") or op.get("DesiredQuantity"))
        available = _fnum(op.get("AvailableQuantity"))

        tracker = map_status(
            warp_status,
            clocked_in=bool(users),
            complete_qty=complete,
            planned_qty=planned,
            available_qty=available,
        )

        return OpSnapshot(
            operation_id=op_id,
            work_order_id=wo_id,
            title=str(op.get("Title") or ""),
            warp_status=warp_status,
            tracker_status=tracker,
            clocked_in=users,
            complete_qty=complete,
            planned_qty=planned,
            available_qty=available,
            op_url=op_url(op_id, wo_id),
            wo_url=wo_url(wo_id) if wo_id else "",
        )


def apply_snapshot(task: dict, snap: OpSnapshot, *, update_status: bool = True) -> list[str]:
    """Write snapshot fields onto a task. Never changes operation_id to something else."""
    changes: list[str] = []

    # Guard: must stay on the same OP
    existing = task.get("warp_operation_id")
    if existing is not None and int(existing) != int(snap.operation_id):
        raise WarpError(
            f"Task is linked to OP {existing}; snapshot is OP {snap.operation_id}"
        )

    task.update(snap.to_task_fields())

    if snap.title and (
        not task.get("name")
        or task.get("name") in ("", "Unassigned")
        or str(task.get("name", "")).startswith("OP ")
    ):
        if task.get("name") != snap.title:
            changes.append(f"name → {snap.title}")
        task["name"] = snap.title

    if snap.clocked_in:
        primary = snap.clocked_in[0]
        if task.get("assignee") in (None, "", "Unassigned"):
            task["assignee"] = primary
            changes.append(f"assignee → {primary}")

    if update_status and snap.tracker_status != task.get("status"):
        old = task.get("status")
        task["status"] = snap.tracker_status
        changes.append(f"status {old} → {snap.tracker_status}")
        if snap.tracker_status == "Done":
            task["completed_at"] = datetime.now().isoformat()
        elif old == "Done" and snap.tracker_status != "Done":
            task["completed_at"] = None

    if changes:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M")
        clocked = f" clocked-in: {', '.join(snap.clocked_in)}" if snap.clocked_in else ""
        note = (
            f"[{ts}] Warp OP {snap.operation_id} (WO {snap.work_order_id}): "
            f"{snap.warp_status or 'n/a'} → {snap.tracker_status}{clocked}"
        )
        task["notes"] = ((task.get("notes") or "") + "\n" + note).strip()

    return changes
