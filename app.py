import streamlit as st
import json
from datetime import datetime, date
import os
import pandas as pd
import re

from warp_client import (
    WarpClient,
    WarpError,
    apply_snapshot_to_task,
    load_token,
    operation_url,
    parse_warp_ref,
    work_order_url,
)

TASKS_FILE = "team_tasks.json"


def load_tasks():
    if os.path.exists(TASKS_FILE):
        with open(TASKS_FILE, "r") as f:
            return json.load(f)
    return []


def save_tasks(tasks):
    with open(TASKS_FILE, "w") as f:
        json.dump(tasks, f, indent=2)


def clean_note_text(notes_text):
    if not isinstance(notes_text, str) or not notes_text.strip():
        return ""
    cleaned = re.sub(r"\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?\]\s*", "", notes_text)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def ensure_warp_fields(task: dict) -> dict:
    """Backfill Warp fields on older task records."""
    task.setdefault("warp_operation_id", None)
    task.setdefault("warp_work_order_id", None)
    task.setdefault("warp_op_url", "")
    task.setdefault("warp_wo_url", "")
    task.setdefault("warp_status", "")
    task.setdefault("warp_clocked_in", [])
    task.setdefault("warp_last_sync", None)
    task.setdefault("warp_auto_sync", True)
    return task


def get_warp_client() -> WarpClient:
    token = st.session_state.get("warp_token") or load_token()
    return WarpClient(token=token)


def sync_task_from_warp(task: dict, client: WarpClient) -> tuple[bool, str]:
    """Sync one task. Returns (changed, message)."""
    op_id = task.get("warp_operation_id")
    wo_id = task.get("warp_work_order_id")
    if not op_id and not wo_id:
        return False, "No Warp OP/WO linked"

    try:
        snap = client.lookup_and_snapshot(
            operation_id=int(op_id) if op_id else None,
            work_order_id=int(wo_id) if wo_id and not op_id else None,
        )
        changes = apply_snapshot_to_task(task, snap, force_status=True)
        task["last_updated"] = datetime.now().isoformat()
        if changes:
            return True, "; ".join(changes)
        return False, f"Already up to date (Warp: {snap.warp_status or 'n/a'})"
    except WarpError as exc:
        return False, str(exc)
    except Exception as exc:  # noqa: BLE001
        return False, f"Unexpected error: {exc}"


def sync_all_linked_tasks(tasks: list, client: WarpClient) -> tuple[int, list[str]]:
    """Sync every task that has a Warp OP/WO and auto-sync enabled."""
    updated = 0
    messages = []
    for task in tasks:
        ensure_warp_fields(task)
        if not task.get("warp_auto_sync", True):
            continue
        if not task.get("warp_operation_id") and not task.get("warp_work_order_id"):
            continue
        changed, msg = sync_task_from_warp(task, client)
        label = f"#{task['id']} {task.get('name', '')}"
        if changed:
            updated += 1
            messages.append(f"✅ {label}: {msg}")
        elif msg.startswith("No Warp") or msg.startswith("Already"):
            pass
        else:
            messages.append(f"⚠️ {label}: {msg}")
    return updated, messages


# ===================== REMOVE TASKS DIALOG =====================
@st.dialog("Confirm Task Removal")
def remove_tasks_dialog(selected_to_remove, task_labels):
    st.warning("This action cannot be undone.")

    st.write("**You are about to permanently remove the following task(s):**")
    for label in selected_to_remove:
        st.write(f"- {label}")

    st.divider()

    confirm_input = st.text_input(
        "Type keyword to confirm deletion",
        placeholder="Take a Guess",
        key="remove_confirm_input",
        type="password",
        autocomplete="off",
    )

    if st.button(
        "Permanently Remove Tasks",
        type="primary",
        use_container_width=True,
        disabled=(confirm_input.strip().lower() != "mm"),
    ):
        ids_to_remove = [task_labels[label] for label in selected_to_remove]
        global tasks
        tasks = [t for t in tasks if t["id"] not in ids_to_remove]
        save_tasks(tasks)
        st.success(f"Successfully removed {len(ids_to_remove)} task(s).")
        st.rerun()


# ===================== PAGE CONFIG =====================
st.set_page_config(page_title="Team Task Tracker", layout="wide", page_icon="🚀")

st.title("🚀 MM OPs Tracker")
st.caption("Track progress across the team · linked to Warp work orders & operations")

tasks = load_tasks()
for t in tasks:
    ensure_warp_fields(t)

# ===================== SIDEBAR =====================
st.sidebar.header("🔍 Filters")
all_assignees = sorted(set(t["assignee"] for t in tasks)) if tasks else []
selected_assignees = st.sidebar.multiselect("Assignee", all_assignees, default=all_assignees)

statuses = ["To Do", "In Progress", "Done", "Blocked"]
selected_statuses = st.sidebar.multiselect("Status", statuses, default=statuses)

st.sidebar.divider()
st.sidebar.header("🔗 Warp Integration")

if "warp_token" not in st.session_state:
    st.session_state.warp_token = load_token() or ""

with st.sidebar.expander("Warp Settings", expanded=not bool(st.session_state.warp_token)):
    st.caption(
        "Token is read from `WARP_API_TOKEN`, `~/.sx-ai-auth/sx-ai-auth.json`, "
        "or pasted below (session only)."
    )
    token_input = st.text_input(
        "Warp API token",
        value=st.session_state.warp_token or "",
        type="password",
        key="warp_token_input",
        help="Bearer token for warpdrive.spacex.corp",
    )
    if token_input != (st.session_state.warp_token or ""):
        st.session_state.warp_token = token_input

    auto_sync_on_load = st.checkbox(
        "Auto-sync linked OPs on load",
        value=st.session_state.get("auto_sync_on_load", True),
        key="auto_sync_on_load",
        help="When enabled, pulls clock-in / complete status from Warp each refresh.",
    )

client = get_warp_client()
if client.is_authenticated:
    st.sidebar.success("Warp token configured")
else:
    st.sidebar.warning("No Warp token — links still work; status sync needs auth")

if st.sidebar.button("🔄 Sync all from Warp", use_container_width=True, type="primary"):
    if not client.is_authenticated:
        st.sidebar.error("Configure a Warp token first")
    else:
        with st.spinner("Syncing from Warp…"):
            n, msgs = sync_all_linked_tasks(tasks, client)
            save_tasks(tasks)
        if n:
            st.sidebar.success(f"Updated {n} task(s)")
        else:
            st.sidebar.info("No status changes")
        for m in msgs[:12]:
            st.sidebar.caption(m)
        st.rerun()

# Auto-sync once per session load (avoids hammering API on every widget interaction)
if (
    client.is_authenticated
    and st.session_state.get("auto_sync_on_load", True)
    and not st.session_state.get("_warp_synced_this_run")
):
    linked = [
        t
        for t in tasks
        if t.get("warp_auto_sync", True)
        and (t.get("warp_operation_id") or t.get("warp_work_order_id"))
    ]
    if linked:
        n, _ = sync_all_linked_tasks(tasks, client)
        if n:
            save_tasks(tasks)
    st.session_state._warp_synced_this_run = True

# ===================== METRICS =====================
today = date.today()
done_today = sum(
    1
    for t in tasks
    if t["status"] == "Done" and datetime.fromisoformat(t["last_updated"]).date() == today
)
in_progress = sum(1 for t in tasks if t["status"] == "In Progress")
open_tasks = len([t for t in tasks if t["status"] in ["To Do", "In Progress"]])
linked_count = sum(1 for t in tasks if t.get("warp_operation_id") or t.get("warp_work_order_id"))

col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("✅ Done Today", done_today)
col2.metric("🔄 In Progress", in_progress)
col3.metric("📋 Total Tasks", len(tasks))
col4.metric("📌 Open Tasks", open_tasks)
col5.metric("🔗 Linked to Warp", linked_count)

st.divider()

# ===================== TABS =====================
tab_current, tab_manage, tab_add, tab_warp = st.tabs(
    [
        "📋 Current Tasks",
        "🛠️ Manage Tasks",
        "➕ Add Task",
        "🔗 Warp Link / Import",
    ]
)

# ===================== TAB 1: CURRENT TASKS =====================
with tab_current:
    st.markdown(
        """
    <style>
    [data-testid="stDataFrame"] td:first-child {
        font-size: 5.5em !important;
        text-align: center !important;
        vertical-align: middle !important;
        padding: 8px 4px !important;
    }
    </style>
    """,
        unsafe_allow_html=True,
    )

    st.subheader("📋 Current Tasks")

    filtered = [
        t
        for t in tasks
        if (not selected_assignees or t["assignee"] in selected_assignees)
        and t["status"] in selected_statuses
    ]

    if filtered:
        df = pd.DataFrame(filtered)
        df["Notes"] = df["notes"].apply(
            lambda x: clean_note_text(x)[:80] + "…"
            if len(clean_note_text(x)) > 80
            else clean_note_text(x)
        )

        def warp_link_label(row):
            op = row.get("warp_operation_id")
            wo = row.get("warp_work_order_id")
            if op:
                return f"OP {int(op)}"
            if wo and not (isinstance(wo, float) and pd.isna(wo)):
                try:
                    return f"WO {int(wo)}"
                except (TypeError, ValueError):
                    return ""
            return ""

        def warp_href(row):
            url = row.get("warp_op_url") or ""
            if url:
                return url
            op = row.get("warp_operation_id")
            if op and not (isinstance(op, float) and pd.isna(op)):
                try:
                    return operation_url(int(op))
                except (TypeError, ValueError):
                    pass
            wo = row.get("warp_work_order_id")
            if wo and not (isinstance(wo, float) and pd.isna(wo)):
                try:
                    return work_order_url(int(wo))
                except (TypeError, ValueError):
                    pass
            return ""

        df["Warp"] = df.apply(warp_link_label, axis=1)
        df["Warp URL"] = df.apply(warp_href, axis=1)
        df["Clocked In"] = df.apply(
            lambda r: ", ".join(r["warp_clocked_in"])
            if isinstance(r.get("warp_clocked_in"), list) and r.get("warp_clocked_in")
            else "",
            axis=1,
        )

        display_df = df[
            [
                "id",
                "name",
                "assignee",
                "status",
                "Warp",
                "Warp URL",
                "Clocked In",
                "Notes",
                "last_updated",
            ]
        ].copy()
        display_df["last_updated"] = pd.to_datetime(display_df["last_updated"]).dt.strftime(
            "%m/%d %H:%M"
        )
        display_df = display_df.rename(columns={"status": "Status"})

        def get_status_icon(status):
            if status == "Done":
                return "✅"
            elif status == "In Progress":
                return "🔄"
            elif status == "Blocked":
                return "⛔"
            elif status == "To Do":
                return "📝"
            return ""

        display_df.insert(0, " ", display_df["Status"].apply(get_status_icon))
        display_df = display_df.drop(columns=["id"])

        def highlight_rows(row):
            status = row["Status"]
            color = {
                "Done": "#d4edda",
                "In Progress": "#fff3cd",
                "Blocked": "#f8d7da",
                "To Do": "#cce5ff",
            }.get(status, "")
            return [f"background-color: {color}" for _ in row]

        def color_status_text(val):
            colors = {
                "Done": "color: #155724; font-weight: 600;",
                "In Progress": "color: #856404; font-weight: 600;",
                "Blocked": "color: #721c24; font-weight: 600;",
                "To Do": "color: #004085; font-weight: 600;",
            }
            return colors.get(val, "")

        styled_df = (
            display_df.style.apply(highlight_rows, axis=1).map(
                color_status_text, subset=["Status"]
            )
        )

        with st.container(border=True):
            st.dataframe(
                styled_df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    " ": st.column_config.TextColumn("", width="small"),
                    "name": st.column_config.TextColumn("Task", width="large"),
                    "Notes": st.column_config.TextColumn("Notes", width="medium"),
                    "Status": st.column_config.TextColumn("Status", width="small"),
                    "Warp": st.column_config.TextColumn("Warp", width="small"),
                    "Warp URL": st.column_config.LinkColumn(
                        "Open in Warp",
                        display_text="Open ↗",
                        width="small",
                    ),
                    "Clocked In": st.column_config.TextColumn("Clocked In", width="medium"),
                },
            )
    else:
        st.info("No tasks match the current filters.")

# ===================== TAB 2: MANAGE TASKS =====================
with tab_manage:
    st.subheader("🛠️ Manage Tasks")

    col_update, col_remove = st.columns(2)

    with col_update:
        with st.container(border=True):
            st.markdown("#### ✏️ Update Task Progress")

            if tasks:
                task_options = {
                    f"#{t['id']} - {t['name']} ({t['assignee']})": t["id"] for t in tasks
                }
                selected_label = st.selectbox(
                    "Select task", list(task_options.keys()), key="update_select"
                )
                selected_id = task_options[selected_label]
                task = next(t for t in tasks if t["id"] == selected_id)
                ensure_warp_fields(task)

                # Warp link summary
                if task.get("warp_operation_id") or task.get("warp_work_order_id"):
                    parts = []
                    if task.get("warp_operation_id"):
                        parts.append(
                            f"[OP {task['warp_operation_id']}]"
                            f"({task.get('warp_op_url') or operation_url(task['warp_operation_id'])})"
                        )
                    if task.get("warp_work_order_id"):
                        parts.append(
                            f"[WO {task['warp_work_order_id']}]"
                            f"({task.get('warp_wo_url') or work_order_url(task['warp_work_order_id'])})"
                        )
                    warp_bits = " · ".join(parts)
                    if task.get("warp_status"):
                        warp_bits += f" · Warp status: **{task['warp_status']}**"
                    if task.get("warp_clocked_in"):
                        warp_bits += f" · Clocked in: {', '.join(task['warp_clocked_in'])}"
                    st.markdown(f"🔗 {warp_bits}")
                    if task.get("warp_last_sync"):
                        st.caption(f"Last Warp sync: {task['warp_last_sync']}")

                new_status = st.selectbox(
                    "New Status",
                    statuses,
                    index=statuses.index(task["status"]),
                    key="new_status",
                )

                new_assignee = st.text_input(
                    "Assignee",
                    value=task.get("assignee", "Unassigned"),
                    key="new_assignee",
                )

                new_note = st.text_area(
                    "Add note (optional)", value="", height=100, key="new_note"
                )

                c1, c2 = st.columns(2)
                with c1:
                    if st.button("Update Task", type="primary", use_container_width=True):
                        task["status"] = new_status

                        if new_assignee.strip() and new_assignee.strip() != task.get(
                            "assignee", ""
                        ):
                            task["assignee"] = new_assignee.strip()

                        if new_note.strip():
                            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                            task["notes"] = (
                                task.get("notes", "")
                                + f"\n[{timestamp}] {new_note.strip()}"
                            ).strip()

                        task["last_updated"] = datetime.now().isoformat()
                        save_tasks(tasks)
                        st.rerun()
                with c2:
                    can_sync = bool(
                        task.get("warp_operation_id") or task.get("warp_work_order_id")
                    )
                    if st.button(
                        "🔄 Sync from Warp",
                        use_container_width=True,
                        disabled=not can_sync or not client.is_authenticated,
                    ):
                        changed, msg = sync_task_from_warp(task, client)
                        save_tasks(tasks)
                        if changed:
                            st.success(msg)
                        else:
                            st.info(msg)
                        st.rerun()
            else:
                st.info("No tasks available to update.")

    with col_remove:
        with st.container(border=True):
            st.markdown("#### 🗑️ Remove Tasks")

            if tasks:
                task_labels = {
                    f"#{t['id']} - {t['name']} ({t['assignee']}) [{t['status']}]": t["id"]
                    for t in tasks
                }

                selected_to_remove = st.multiselect(
                    "Select task(s) to remove",
                    options=list(task_labels.keys()),
                    key="remove_select",
                )

                if selected_to_remove:
                    if st.button(
                        "Remove Selected", type="secondary", use_container_width=True
                    ):
                        remove_tasks_dialog(selected_to_remove, task_labels)
            else:
                st.info("No tasks to remove.")


# ===================== TAB 3: ADD TASK =====================
with tab_add:
    with st.container(border=True):
        st.subheader("➕ Add New Task")
        with st.form("add_form", clear_on_submit=True):
            col_a, col_b = st.columns([3, 2])
            with col_a:
                task_name = st.text_input(
                    "Task Name*", placeholder="What needs to be done?"
                )
            with col_b:
                assignee = st.text_input("Assignee", value="Unassigned")

            st.markdown("**Optional Warp link**")
            col_w1, col_w2, col_w3 = st.columns(3)
            with col_w1:
                warp_ref = st.text_input(
                    "Warp OP / URL",
                    placeholder="OP ID, WO ID, or Warp URL",
                    help="Paste a Warp operation URL or numeric OP/WO id",
                )
            with col_w2:
                warp_op_id = st.text_input("Operation ID", placeholder="e.g. 65105243")
            with col_w3:
                warp_wo_id = st.text_input("Work Order ID", placeholder="e.g. 9481728")

            auto_sync = st.checkbox(
                "Auto-sync status from Warp (clock-in → In Progress, complete → Done)",
                value=True,
            )
            pull_now = st.checkbox(
                "Fetch title/status from Warp on create",
                value=True,
                help="Requires a configured Warp token",
            )

            submitted = st.form_submit_button(
                "Add Task", type="primary", use_container_width=True
            )

            if submitted:
                if not task_name.strip() and not (warp_ref or warp_op_id or warp_wo_id):
                    st.warning("Task name or a Warp reference is required.")
                else:
                    parsed = parse_warp_ref(warp_ref) if warp_ref else {}
                    op_id = None
                    wo_id = None
                    if warp_op_id.strip().isdigit():
                        op_id = int(warp_op_id.strip())
                    elif parsed.get("operation_id"):
                        op_id = parsed["operation_id"]
                    if warp_wo_id.strip().isdigit():
                        wo_id = int(warp_wo_id.strip())
                    elif parsed.get("work_order_id"):
                        wo_id = parsed["work_order_id"]

                    new_id = max([t["id"] for t in tasks], default=0) + 1
                    new_task = {
                        "id": new_id,
                        "name": task_name.strip() or (f"OP {op_id}" if op_id else "New task"),
                        "assignee": assignee.strip() or "Unassigned",
                        "status": "To Do",
                        "notes": "",
                        "created": datetime.now().isoformat(),
                        "last_updated": datetime.now().isoformat(),
                        "warp_operation_id": op_id,
                        "warp_work_order_id": wo_id,
                        "warp_op_url": operation_url(op_id) if op_id else "",
                        "warp_wo_url": work_order_url(wo_id) if wo_id else "",
                        "warp_status": "",
                        "warp_clocked_in": [],
                        "warp_last_sync": None,
                        "warp_auto_sync": auto_sync,
                    }

                    if (op_id or wo_id) and pull_now and client.is_authenticated:
                        try:
                            snap = client.lookup_and_snapshot(
                                operation_id=op_id, work_order_id=wo_id if not op_id else None
                            )
                            apply_snapshot_to_task(new_task, snap, force_status=True)
                            if not task_name.strip() and snap.title:
                                new_task["name"] = snap.title
                        except WarpError as exc:
                            st.warning(f"Task created, but Warp fetch failed: {exc}")

                    tasks.append(new_task)
                    save_tasks(tasks)
                    st.success(f"Task #{new_id} added successfully!")
                    st.rerun()

# ===================== TAB 4: WARP LINK / IMPORT =====================
with tab_warp:
    st.subheader("🔗 Link existing tasks to Warp / Import OPs")

    col_link, col_import = st.columns(2)

    with col_link:
        with st.container(border=True):
            st.markdown("#### Attach Warp OP/WO to a task")
            if tasks:
                link_options = {
                    f"#{t['id']} - {t['name']} ({t['assignee']})": t["id"] for t in tasks
                }
                link_label = st.selectbox(
                    "Task", list(link_options.keys()), key="link_task_select"
                )
                link_id = link_options[link_label]
                link_task = next(t for t in tasks if t["id"] == link_id)
                ensure_warp_fields(link_task)

                link_ref = st.text_input(
                    "Paste Warp URL or OP/WO id",
                    key="link_ref",
                    placeholder="https://warpdrive.spacex.corp/ShopFloor/#/operations/123…",
                )
                c_op, c_wo = st.columns(2)
                with c_op:
                    link_op = st.text_input(
                        "Operation ID",
                        value=str(link_task.get("warp_operation_id") or ""),
                        key="link_op",
                    )
                with c_wo:
                    link_wo = st.text_input(
                        "Work Order ID",
                        value=str(link_task.get("warp_work_order_id") or ""),
                        key="link_wo",
                    )
                link_auto = st.checkbox(
                    "Auto-sync this task from Warp",
                    value=link_task.get("warp_auto_sync", True),
                    key="link_auto",
                )
                fetch_on_link = st.checkbox(
                    "Sync status now after linking", value=True, key="link_fetch"
                )

                if st.button("Save Warp link", type="primary", use_container_width=True):
                    parsed = parse_warp_ref(link_ref) if link_ref else {}
                    op_id = (
                        int(link_op)
                        if link_op.strip().isdigit()
                        else parsed.get("operation_id")
                    )
                    wo_id = (
                        int(link_wo)
                        if link_wo.strip().isdigit()
                        else parsed.get("work_order_id")
                    )
                    link_task["warp_operation_id"] = op_id
                    link_task["warp_work_order_id"] = wo_id
                    link_task["warp_op_url"] = operation_url(op_id) if op_id else ""
                    link_task["warp_wo_url"] = work_order_url(wo_id) if wo_id else ""
                    link_task["warp_auto_sync"] = link_auto
                    link_task["last_updated"] = datetime.now().isoformat()

                    if fetch_on_link and (op_id or wo_id) and client.is_authenticated:
                        changed, msg = sync_task_from_warp(link_task, client)
                        save_tasks(tasks)
                        st.success(f"Linked. {msg}")
                    else:
                        save_tasks(tasks)
                        st.success("Warp link saved.")
                    st.rerun()
            else:
                st.info("Add a task first.")

    with col_import:
        with st.container(border=True):
            st.markdown("#### Import operation from Warp")
            st.caption(
                "Look up an OP by ID (or WO + sequence) and create a tracker task "
                "with live status and a deep link."
            )
            imp_op = st.text_input("Operation ID", key="imp_op")
            imp_wo = st.text_input("Work Order ID (if no OP)", key="imp_wo")
            imp_seq = st.text_input("Sequence # (optional with WO)", key="imp_seq")
            imp_assignee = st.text_input(
                "Assignee override", value="", key="imp_assignee", placeholder="Leave blank to use clocked-in user"
            )

            if st.button(
                "Import from Warp",
                type="primary",
                use_container_width=True,
                disabled=not client.is_authenticated,
            ):
                if not client.is_authenticated:
                    st.error("Configure Warp token in the sidebar first.")
                else:
                    try:
                        op_id = int(imp_op) if imp_op.strip().isdigit() else None
                        wo_id = int(imp_wo) if imp_wo.strip().isdigit() else None
                        seq = int(imp_seq) if imp_seq.strip().isdigit() else None
                        snap = client.lookup_and_snapshot(
                            operation_id=op_id,
                            work_order_id=wo_id if not op_id else None,
                            sequence=seq,
                        )
                        # Avoid duplicate OP links
                        existing = next(
                            (
                                t
                                for t in tasks
                                if t.get("warp_operation_id") == snap.operation_id
                            ),
                            None,
                        )
                        if existing:
                            st.warning(
                                f"OP {snap.operation_id} already linked to task #{existing['id']}. "
                                "Use Sync instead."
                            )
                        else:
                            new_id = max([t["id"] for t in tasks], default=0) + 1
                            new_task = {
                                "id": new_id,
                                "name": snap.title or f"OP {snap.operation_id}",
                                "assignee": imp_assignee.strip()
                                or (snap.clocked_in_users[0] if snap.clocked_in_users else "Unassigned"),
                                "status": snap.tracker_status,
                                "notes": "",
                                "created": datetime.now().isoformat(),
                                "last_updated": datetime.now().isoformat(),
                                "warp_operation_id": snap.operation_id,
                                "warp_work_order_id": snap.work_order_id,
                                "warp_op_url": snap.op_url,
                                "warp_wo_url": snap.wo_url,
                                "warp_status": snap.warp_status,
                                "warp_clocked_in": snap.clocked_in_users,
                                "warp_last_sync": datetime.now().isoformat(),
                                "warp_auto_sync": True,
                            }
                            apply_snapshot_to_task(new_task, snap, force_status=True)
                            tasks.append(new_task)
                            save_tasks(tasks)
                            st.success(
                                f"Imported OP {snap.operation_id} as task #{new_id} "
                                f"({snap.tracker_status})"
                            )
                            st.rerun()
                    except WarpError as exc:
                        st.error(str(exc))

    st.divider()
    st.markdown(
        """
**How status sync works**

| Warp signal | Tracker status |
|---|---|
| OP status Complete / Closed, or qty fully complete | **Done** |
| Anyone clocked in on the OP, or status Running / In Progress | **In Progress** |
| Open quality calls, or On Hold / Blocked | **Blocked** |
| Unstarted / Ready / no activity | **To Do** |

- **Open in Warp** column links straight to the operation (or work order).
- **Sync all from Warp** (sidebar) refreshes every linked task.
- Auto-sync on load keeps clock-in / completion reflected without manual updates.
- Auth: set `WARP_API_TOKEN` in the environment, put it in `warp_config.json` as
  `{"WARP_API_TOKEN": "..."}`, or paste it under **Warp Settings**.
"""
    )
