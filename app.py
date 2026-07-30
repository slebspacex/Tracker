"""
MM OPs Tracker — team task board linked to Warp Shop Floor operations.

Run on Windows (corp network):
  cd %USERPROFILE%\\grok-desktop-workspace
  python -m streamlit run app.py
"""

from __future__ import annotations

import json
import os
import re
from datetime import date, datetime, timedelta

import pandas as pd
import streamlit as st

from warp_client import (
    WarpClient,
    WarpError,
    apply_snapshot,
    load_token,
    op_url,
    parse_warp_input,
    wo_url,
)

TASKS_FILE = "team_tasks.json"
STATUSES = ["To Do", "In Progress", "Done", "Blocked"]
COMPLETED_WINDOW_HOURS = 10


# ---------- persistence ----------

def load_tasks() -> list:
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        for t in data:
            t.setdefault("completed_at", None)
        return data
    return []


def save_tasks(tasks: list) -> None:
    with open(TASKS_FILE, "w", encoding="utf-8") as f:
        json.dump(tasks, f, indent=2)


def clean_notes(text: str) -> str:
    if not isinstance(text, str) or not text.strip():
        return ""
    t = re.sub(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?\]\s*", "", text)
    return re.sub(r"\s+", " ", t).strip()


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00").replace("+00:00", ""))
    except (TypeError, ValueError):
        try:
            return datetime.fromisoformat(str(value)[:19])
        except (TypeError, ValueError):
            return None


def completion_time(task: dict) -> datetime | None:
    """When the OP was marked Done — prefer completed_at, else last_updated if Done."""
    if task.get("status") != "Done":
        return None
    return parse_dt(task.get("completed_at")) or parse_dt(task.get("last_updated"))


def next_id(tasks: list) -> int:
    return max((t.get("id", 0) for t in tasks), default=0) + 1


def new_task(
    *,
    name: str,
    assignee: str = "Unassigned",
    operation_id: int | None = None,
    work_order_id: int | None = None,
) -> dict:
    return {
        "id": 0,  # set by caller
        "name": name,
        "assignee": assignee or "Unassigned",
        "status": "To Do",
        "notes": "",
        "created": datetime.now().isoformat(),
        "last_updated": datetime.now().isoformat(),
        "completed_at": None,
        "warp_operation_id": operation_id,
        "warp_work_order_id": work_order_id,
        "warp_op_url": op_url(operation_id, work_order_id) if operation_id else "",
        "warp_wo_url": wo_url(work_order_id) if work_order_id else "",
        "warp_status": "",
        "warp_clocked_in": [],
        "warp_last_sync": None,
        "warp_auto_sync": True,
    }


def task_row(t: dict, *, extra: dict | None = None) -> dict:
    op = t.get("warp_operation_id")
    wo = t.get("warp_work_order_id")
    url = t.get("warp_op_url") or (op_url(op, wo) if op else "")
    row = {
        "Task": t.get("name", ""),
        "Assignee": t.get("assignee", ""),
        "Status": t.get("status", ""),
        "OP": int(op) if op else None,
        "WO": int(wo) if wo else None,
        "Warp": t.get("warp_status") or "",
        "Clocked in": ", ".join(t.get("warp_clocked_in") or []),
        "Open": url,
        "Updated": (t.get("last_updated") or "")[:16].replace("T", " "),
    }
    if extra:
        row.update(extra)
    return row


def render_op_table(rows: list[dict]) -> None:
    if not rows:
        st.info("None right now.")
        return
    df = pd.DataFrame(rows)

    def color_status(val: str) -> str:
        return {
            "Done": "background-color: #d4edda",
            "In Progress": "background-color: #fff3cd",
            "Blocked": "background-color: #f8d7da",
            "To Do": "background-color: #cce5ff",
        }.get(val, "")

    styled = df.style.map(color_status, subset=["Status"]) if "Status" in df.columns else df
    st.dataframe(
        styled,
        use_container_width=True,
        hide_index=True,
        column_config={
            "Open": st.column_config.LinkColumn("Open in Warp", display_text="Open ↗"),
            "OP": st.column_config.NumberColumn("OP", format="%d"),
            "WO": st.column_config.NumberColumn("WO", format="%d"),
        },
    )


def sync_one(task: dict, client: WarpClient) -> tuple[bool, str]:
    op_id = task.get("warp_operation_id")
    if not op_id:
        return False, "No Operation ID linked"
    try:
        snap = client.fetch_op(int(op_id))
        changes = apply_snapshot(task, snap, update_status=True)
        task["last_updated"] = datetime.now().isoformat()
        if changes:
            return True, "; ".join(changes)
        return (
            False,
            f"OK — OP {snap.operation_id} / WO {snap.work_order_id} | "
            f"Warp={snap.warp_status or 'n/a'} → {snap.tracker_status} | "
            f"clocked-in={snap.clocked_in or 'none'}",
        )
    except WarpError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected: {exc}"


# ---------- page ----------

st.set_page_config(page_title="MM OPs Tracker", layout="wide", page_icon="🚀")
st.title("🚀 MM OPs Tracker")
st.caption("Team tasks linked 1:1 to Warp operations · clock-in → In Progress · complete → Done")

tasks = load_tasks()

# ---------- sidebar ----------

st.sidebar.header("Filters")
assignees = sorted({t.get("assignee", "Unassigned") for t in tasks}) if tasks else []
sel_assignees = st.sidebar.multiselect("Assignee", assignees, default=assignees)
sel_statuses = st.sidebar.multiselect("Status", STATUSES, default=STATUSES)

st.sidebar.divider()
st.sidebar.header("Warp")

if "warp_token" not in st.session_state:
    st.session_state.warp_token = load_token() or ""

token_in = st.sidebar.text_input(
    "API token",
    value=st.session_state.warp_token,
    type="password",
    help="From sx-ai-sandbox setup warp --re-auth → ~/.sx-ai-auth/sx-ai-auth.json (WARP_API_TOKEN)",
)
if token_in != st.session_state.warp_token:
    st.session_state.warp_token = token_in

client = WarpClient(token=st.session_state.warp_token)
if client.ok:
    st.sidebar.success("Token set")
else:
    st.sidebar.warning("No token — status sync disabled")

st.sidebar.caption(
    "Run this app on your **Windows PC** (corp network). "
    "Warp will not resolve inside the Grok sandbox."
)

if st.sidebar.button("🔄 Sync all linked OPs", type="primary", use_container_width=True):
    if not client.ok:
        st.sidebar.error("Add a Warp token first")
    else:
        updated = 0
        messages = []
        for t in tasks:
            if not t.get("warp_auto_sync", True):
                continue
            if not t.get("warp_operation_id"):
                continue
            changed, msg = sync_one(t, client)
            label = f"#{t['id']} {t.get('name', '')}"
            if changed:
                updated += 1
                messages.append(f"✅ {label}: {msg}")
            elif msg.startswith("OK"):
                messages.append(f"• {label}: {msg}")
            else:
                messages.append(f"⚠️ {label}: {msg}")
        save_tasks(tasks)
        if updated:
            st.sidebar.success(f"Updated {updated} task(s)")
        for m in messages[:15]:
            st.sidebar.caption(m)
        st.rerun()

# ---------- metrics ----------

today = date.today()
done_today = sum(
    1
    for t in tasks
    if t.get("status") == "Done"
    and datetime.fromisoformat(t["last_updated"]).date() == today
)
in_prog = sum(1 for t in tasks if t.get("status") == "In Progress")
linked = sum(1 for t in tasks if t.get("warp_operation_id"))

# Activity lists (used for metrics + Activity tab)
now = datetime.now()
window_start = now - timedelta(hours=COMPLETED_WINDOW_HOURS)
in_progress_tasks = [t for t in tasks if t.get("status") == "In Progress"]
# Also treat clocked-in linked tasks as in progress even if status lag
for t in tasks:
    if t in in_progress_tasks:
        continue
    if t.get("warp_clocked_in") and t.get("status") != "Done":
        in_progress_tasks.append(t)

completed_recent = []
for t in tasks:
    if t.get("status") != "Done":
        continue
    ct = completion_time(t)
    if ct is not None and ct >= window_start:
        completed_recent.append((ct, t))
completed_recent.sort(key=lambda x: x[0], reverse=True)

c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Done today", done_today)
c2.metric("In Progress", len(in_progress_tasks))
c3.metric(f"Done last {COMPLETED_WINDOW_HOURS}h", len(completed_recent))
c4.metric("Tasks", len(tasks))
c5.metric("Linked to Warp", linked)

st.divider()

tab_activity, tab_board, tab_edit, tab_add = st.tabs(
    ["📡 Activity", "📋 Board", "✏️ Edit / Sync", "➕ Add task"]
)

# ---------- activity (in progress + completed last 10h) ----------

with tab_activity:
    st.subheader("Live floor snapshot")
    st.caption(
        f"In-progress OPs, plus completed OPs from the last **{COMPLETED_WINDOW_HOURS} hours**. "
        "Based on linked tracker tasks — use **Sync all** in the sidebar to refresh from Warp."
    )

    col_sync, col_hours = st.columns([1, 1])
    with col_hours:
        hours = st.slider(
            "Completed window (hours)",
            min_value=1,
            max_value=24,
            value=COMPLETED_WINDOW_HOURS,
            key="activity_hours",
        )
    with col_sync:
        st.write("")  # spacing
        st.write("")
        if st.button("🔄 Refresh from Warp", use_container_width=True, disabled=not client.ok):
            msgs = []
            for t in tasks:
                if not t.get("warp_operation_id") or not t.get("warp_auto_sync", True):
                    continue
                changed, msg = sync_one(t, client)
                if changed or not msg.startswith("OK"):
                    msgs.append(f"#{t['id']}: {msg}")
            save_tasks(tasks)
            if msgs:
                for m in msgs[:10]:
                    st.caption(m)
            st.rerun()

    # Recompute with slider hours
    window_start_dyn = now - timedelta(hours=hours)
    completed_dyn = []
    for t in tasks:
        if t.get("status") != "Done":
            continue
        ct = completion_time(t)
        if ct is not None and ct >= window_start_dyn:
            completed_dyn.append((ct, t))
    completed_dyn.sort(key=lambda x: x[0], reverse=True)

    # --- In Progress ---
    st.markdown("### 🔄 In progress")
    st.caption(f"{len(in_progress_tasks)} operation(s) currently in progress or clocked-in")
    ip_rows = []
    for t in sorted(
        in_progress_tasks,
        key=lambda x: x.get("last_updated") or "",
        reverse=True,
    ):
        ip_rows.append(
            task_row(
                t,
                extra={
                    "Notes": clean_notes(t.get("notes") or "")[:80],
                },
            )
        )
    render_op_table(ip_rows)

    st.divider()

    # --- Completed last N hours ---
    st.markdown(f"### ✅ Completed in the last {hours} hours")
    st.caption(
        f"{len(completed_dyn)} operation(s) marked Done since "
        f"{window_start_dyn.strftime('%Y-%m-%d %H:%M')}"
    )
    done_rows = []
    for ct, t in completed_dyn:
        age_h = (now - ct).total_seconds() / 3600.0
        done_rows.append(
            task_row(
                t,
                extra={
                    "Completed": ct.strftime("%Y-%m-%d %H:%M"),
                    "Hours ago": round(age_h, 1),
                    "Notes": clean_notes(t.get("notes") or "")[:80],
                },
            )
        )
    render_op_table(done_rows)

    if not in_progress_tasks and not completed_dyn:
        st.info(
            "Nothing to show yet. Link OPs under **Add task**, then sync. "
            "Completed OPs appear here when status becomes **Done** (from Warp or manual)."
        )

# ---------- board ----------

with tab_board:
    filtered = [
        t
        for t in tasks
        if (not sel_assignees or t.get("assignee") in sel_assignees)
        and t.get("status") in sel_statuses
    ]
    if not filtered:
        st.info("No tasks yet — use **Add task** and paste a Warp operation URL.")
    else:
        rows = [
            task_row(
                t,
                extra={
                    "ID": t["id"],
                    "Notes": clean_notes(t.get("notes") or "")[:100],
                },
            )
            for t in filtered
        ]
        # Put ID first
        ordered = []
        for r in rows:
            ordered.append({"ID": r.pop("ID", None), **r})
        render_op_table(ordered)

# ---------- edit ----------

with tab_edit:
    if not tasks:
        st.info("No tasks to edit.")
    else:
        labels = {
            f"#{t['id']} — {t.get('name')} ({t.get('assignee')}) [{t.get('status')}]": t["id"]
            for t in tasks
        }
        pick = st.selectbox("Task", list(labels.keys()))
        task = next(t for t in tasks if t["id"] == labels[pick])

        col_a, col_b = st.columns(2)
        with col_a:
            st.subheader("Manual update")
            new_status = st.selectbox(
                "Status", STATUSES, index=STATUSES.index(task.get("status", "To Do"))
            )
            new_assignee = st.text_input("Assignee", value=task.get("assignee", "Unassigned"))
            new_note = st.text_area("Add note", value="", height=80)
            if st.button("Save", type="primary"):
                old_status = task.get("status")
                task["status"] = new_status
                task["assignee"] = new_assignee.strip() or "Unassigned"
                if new_status == "Done" and old_status != "Done":
                    task["completed_at"] = datetime.now().isoformat()
                elif new_status != "Done":
                    task["completed_at"] = None
                if new_note.strip():
                    ts = datetime.now().strftime("%Y-%m-%d %H:%M")
                    task["notes"] = (
                        (task.get("notes") or "") + f"\n[{ts}] {new_note.strip()}"
                    ).strip()
                task["last_updated"] = datetime.now().isoformat()
                save_tasks(tasks)
                st.success("Saved")
                st.rerun()

        with col_b:
            st.subheader("Warp link")
            st.markdown(
                "Paste the **full browser URL** while on the operation, e.g.\n\n"
                "`…/shop-floor/work-order/**819689**/operation/**8246239**`\n\n"
                "- Number after **`/operation/`** = Operation ID (required)\n"
                "- Number after **`/work-order/`** = Work Order ID (optional; filled from API)"
            )
            paste = st.text_input(
                "Warp URL or Operation ID",
                value="",
                placeholder="https://warpdrive.spacex.corp/shop-floor/work-order/…/operation/…",
                key=f"paste_{task['id']}",
            )
            op_manual = st.text_input(
                "Operation ID only",
                value=str(task.get("warp_operation_id") or ""),
                key=f"op_{task['id']}",
                help="Large number from the URL path /operation/NNNNNN",
            )

            if task.get("warp_operation_id"):
                st.write(
                    f"Currently linked: **OP {task['warp_operation_id']}** · "
                    f"**WO {task.get('warp_work_order_id') or '—'}** · "
                    f"Warp status `{task.get('warp_status') or '—'}`"
                )
                if task.get("warp_op_url"):
                    st.markdown(f"[Open in Warp ↗]({task['warp_op_url']})")
                if task.get("warp_last_sync"):
                    st.caption(f"Last sync: {task['warp_last_sync']}")
                if task.get("warp_clocked_in"):
                    st.caption("Clocked in: " + ", ".join(task["warp_clocked_in"]))

            b1, b2, b3 = st.columns(3)
            with b1:
                if st.button("Save link", use_container_width=True):
                    parsed = parse_warp_input(paste) if paste.strip() else {}
                    op_id = None
                    wo_id = None
                    if op_manual.strip().isdigit():
                        op_id = int(op_manual.strip())
                    elif parsed.get("operation_id"):
                        op_id = parsed["operation_id"]
                    if parsed.get("work_order_id"):
                        wo_id = parsed["work_order_id"]

                    if not op_id:
                        st.error("Need an Operation ID (from /operation/ in the URL).")
                    elif op_id < 1000:
                        st.error(
                            f"{op_id} looks like a sequence #, not an Operation ID. "
                            "Use the large number after /operation/."
                        )
                    else:
                        task["warp_operation_id"] = op_id
                        # Only keep WO if it came from the URL; API will correct on sync
                        if wo_id and wo_id >= 1000:
                            task["warp_work_order_id"] = wo_id
                        task["warp_op_url"] = op_url(op_id, task.get("warp_work_order_id"))
                        task["warp_wo_url"] = (
                            wo_url(task["warp_work_order_id"])
                            if task.get("warp_work_order_id")
                            else ""
                        )
                        task["last_updated"] = datetime.now().isoformat()
                        save_tasks(tasks)
                        st.success(f"Linked to OP {op_id}")
                        st.rerun()
            with b2:
                if st.button(
                    "Sync from Warp",
                    use_container_width=True,
                    disabled=not client.ok or not task.get("warp_operation_id"),
                ):
                    changed, msg = sync_one(task, client)
                    save_tasks(tasks)
                    if changed:
                        st.success(msg)
                    else:
                        st.info(msg)
                    st.rerun()
            with b3:
                if st.button("Remove task", use_container_width=True):
                    tasks[:] = [t for t in tasks if t["id"] != task["id"]]
                    save_tasks(tasks)
                    st.rerun()

# ---------- add ----------

with tab_add:
    st.subheader("Add task")
    with st.form("add_task", clear_on_submit=True):
        name = st.text_input("Task name", placeholder="Optional if you paste a Warp URL")
        assignee = st.text_input("Assignee", value="Unassigned")
        st.markdown("**Link to Warp (recommended)**")
        paste = st.text_input(
            "Paste Warp operation URL",
            placeholder="…/shop-floor/work-order/819689/operation/8246239",
        )
        op_only = st.text_input(
            "Or Operation ID only",
            placeholder="8246239",
            help="Must be the Operation ID, not sequence 10/20/101",
        )
        pull = st.checkbox("Fetch title & status from Warp now", value=True)
        submitted = st.form_submit_button("Add", type="primary", use_container_width=True)

        if submitted:
            parsed = parse_warp_input(paste) if paste.strip() else {}
            op_id = None
            wo_id = None
            if op_only.strip().isdigit():
                op_id = int(op_only.strip())
            elif parsed.get("operation_id"):
                op_id = parsed["operation_id"]
            if parsed.get("work_order_id"):
                wo_id = parsed["work_order_id"]

            if not name.strip() and not op_id:
                st.warning("Enter a task name or an Operation ID / Warp URL.")
            elif op_id is not None and op_id < 1000:
                st.error(
                    f"{op_id} looks like a sequence number. "
                    "Use the large ID after /operation/ in the browser URL."
                )
            else:
                t = new_task(
                    name=name.strip() or (f"OP {op_id}" if op_id else "New task"),
                    assignee=assignee.strip() or "Unassigned",
                    operation_id=op_id,
                    work_order_id=wo_id if wo_id and wo_id >= 1000 else None,
                )
                t["id"] = next_id(tasks)

                if op_id and pull and client.ok:
                    try:
                        snap = client.fetch_op(op_id)
                        apply_snapshot(t, snap, update_status=True)
                        if not name.strip() and snap.title:
                            t["name"] = snap.title
                        st.success(
                            f"Added #{t['id']}: OP {snap.operation_id} / WO {snap.work_order_id} "
                            f"— {snap.title or t['name']} ({snap.tracker_status})"
                        )
                    except WarpError as exc:
                        tasks.append(t)
                        save_tasks(tasks)
                        st.warning(f"Task added but Warp fetch failed: {exc}")
                        st.rerun()
                else:
                    st.success(f"Added #{t['id']}")

                tasks.append(t)
                save_tasks(tasks)
                st.rerun()

st.divider()
with st.expander("How linking works"):
    st.markdown(
        """
1. Open the operation in Warp Shop Floor.
2. Copy the address bar URL. It looks like:
   `https://warpdrive.spacex.corp/shop-floor/work-order/**WORK_ORDER_ID**/operation/**OPERATION_ID**`
3. Paste that URL into **Add task** or **Edit → Warp link**.
4. Click **Sync from Warp** (or **Sync all** in the sidebar).

| Signal from Warp | Tracker status |
|---|---|
| Anyone clocked in on the OP | **In Progress** |
| Status complete (`C`) and no remaining qty | **Done** |
| Hold / quality block | **Blocked** |
| Released / unstarted | **To Do** |

**Auth:** `sx-ai-sandbox setup warp --re-auth` then token is read from  
`%USERPROFILE%\\.sx-ai-auth\\sx-ai-auth.json` or paste `WARP_API_TOKEN` in the sidebar.

**Run on Windows host** (not Grok sandbox) so `warpdrive.spacex.corp` resolves.
"""
    )
