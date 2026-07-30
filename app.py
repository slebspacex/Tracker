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
    task.setdefault("warp_base_id", "")  # human WO number (BaseID), e.g. 3462114
    task.setdefault("warp_sequence", None)  # human OP number (SequenceNumber), e.g. 62
    task.setdefault("warp_op_url", "")
    task.setdefault("warp_wo_url", "")
    task.setdefault("warp_status", "")
    task.setdefault("warp_clocked_in", [])
    task.setdefault("warp_last_sync", None)
    task.setdefault("warp_auto_sync", True)
    return task


def task_is_warp_linked(task: dict) -> bool:
    return bool(
        task.get("warp_operation_id")
        or task.get("warp_work_order_id")
        or task.get("warp_base_id")
        or task.get("warp_sequence") is not None
    )


def warp_display_label(task: dict) -> str:
    """Show WO # + OP # when known (what techs type), not internal IDs."""
    base = str(task.get("warp_base_id") or "").strip()
    seq = task.get("warp_sequence")
    if base and seq is not None and str(seq).strip() != "":
        return f"WO {base} · OP {seq}"
    if base:
        return f"WO {base}"
    if seq is not None and str(seq).strip() != "":
        return f"OP {seq}"
    op = task.get("warp_operation_id")
    wo = task.get("warp_work_order_id")
    if op:
        try:
            return f"OP id {int(op)}"
        except (TypeError, ValueError):
            return f"OP id {op}"
    if wo:
        try:
            return f"WO id {int(wo)}"
        except (TypeError, ValueError):
            return f"WO id {wo}"
    return ""


def get_warp_client() -> WarpClient:
    token = st.session_state.get("warp_token") or load_token()
    return WarpClient(token=token)


def sync_task_from_warp(task: dict, client: WarpClient) -> tuple[bool, str]:
    """Sync one task. Returns (changed, message)."""
    ensure_warp_fields(task)
    op_id = task.get("warp_operation_id")
    wo_id = task.get("warp_work_order_id")
    base_id = str(task.get("warp_base_id") or "").strip() or None
    seq = task.get("warp_sequence")
    try:
        seq_int = int(seq) if seq is not None and str(seq).strip() != "" else None
    except (TypeError, ValueError):
        seq_int = None

    if not op_id and not wo_id and not base_id:
        return False, "No Warp WO/OP linked"

    try:
        # Prefer human WO # + OP #; fall back to resolved internal IDs
        snap = client.lookup_and_snapshot(
            base_id=base_id,
            sequence=seq_int,
            operation_id=(
                int(op_id)
                if op_id and not (base_id and seq_int is not None)
                else None
            ),
            work_order_id=int(wo_id) if wo_id and not base_id else None,
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
        if not task_is_warp_linked(task):
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
        "Browser Warp does **not** always show `Authorization: Bearer`. "
        "Copy the **Cookie** header (or just `CurrentUserV4=eyJ…`) from "
        "Network → request → Headers → Request Headers."
    )
    st.caption(
        "Also accepted: `WARP_API_TOKEN` / `WARP_COOKIE` env vars, or `warp_config.json`."
    )
    token_input = st.text_area(
        "Warp Cookie or token",
        value=st.session_state.warp_token or "",
        key="warp_token_input",
        height=100,
        help="Paste Cookie header or CurrentUserV4 JWT from DevTools",
    )
    if token_input != (st.session_state.warp_token or ""):
        st.session_state.warp_token = token_input

    auto_sync_on_load = st.checkbox(
        "Auto-sync linked OPs on load",
        value=st.session_state.get("auto_sync_on_load", True),
        key="auto_sync_on_load",
        help="When enabled, pulls clock-in / complete status from Warp each refresh.",
    )

    if st.button("Test Warp connection", use_container_width=True):
        test_client = get_warp_client()
        if not test_client.is_authenticated:
            st.error("Paste a Cookie / token first.")
        else:
            try:
                # Lightweight call — any 200 means auth worked
                test_client._get("/api/v1/operations/1")
                st.success("Connected (auth accepted).")
            except WarpError as exc:
                msg = str(exc)
                if "404" in msg or "Not found" in msg:
                    st.success("Auth works (sample OP 1 not found — that’s OK).")
                else:
                    st.error(msg)

client = get_warp_client()
if client.is_authenticated:
    st.sidebar.success("Warp auth configured (cookie/token)")
else:
    st.sidebar.warning("No Warp auth — links work; Import/Sync need Cookie")

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
        if t.get("warp_auto_sync", True) and task_is_warp_linked(t)
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
linked_count = sum(1 for t in tasks if task_is_warp_linked(t))

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
            return warp_display_label(row.to_dict() if hasattr(row, "to_dict") else dict(row))

        def warp_href(row):
            url = row.get("warp_op_url") or ""
            if url and not (isinstance(url, float) and pd.isna(url)):
                return url
            op = row.get("warp_operation_id")
            wo = row.get("warp_work_order_id")
            try:
                op_i = int(op) if op is not None and not (isinstance(op, float) and pd.isna(op)) else None
            except (TypeError, ValueError):
                op_i = None
            try:
                wo_i = int(wo) if wo is not None and not (isinstance(wo, float) and pd.isna(wo)) else None
            except (TypeError, ValueError):
                wo_i = None
            if op_i:
                return operation_url(op_i, wo_i)
            if wo_i:
                return work_order_url(wo_i)
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

                # Warp link summary (human WO # / OP #)
                if task_is_warp_linked(task):
                    label = warp_display_label(task)
                    href = task.get("warp_op_url") or (
                        operation_url(
                            task["warp_operation_id"], task.get("warp_work_order_id")
                        )
                        if task.get("warp_operation_id")
                        else (
                            work_order_url(task["warp_work_order_id"])
                            if task.get("warp_work_order_id")
                            else ""
                        )
                    )
                    if href:
                        st.markdown(f"🔗 [{label}]({href})")
                    else:
                        st.markdown(f"🔗 {label}")
                    extra = []
                    if task.get("warp_status"):
                        extra.append(f"Warp status: **{task['warp_status']}**")
                    if task.get("warp_clocked_in"):
                        extra.append(f"Clocked in: {', '.join(task['warp_clocked_in'])}")
                    if extra:
                        st.caption(" · ".join(extra))
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
                    can_sync = task_is_warp_linked(task)
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

            st.markdown("**Optional Warp link** (WO number + OP number from Shop Floor)")
            st.caption(
                "Example from the Warp header **3462114 | SUP OP 62**: "
                "WO number = `3462114`, OP number = `62` (not the long internal IDs)."
            )
            col_w1, col_w2, col_w3 = st.columns(3)
            with col_w1:
                warp_wo_num = st.text_input(
                    "WO number",
                    placeholder="e.g. 3462114",
                    help="Base ID shown as SHOP FLOOR ####### in Warp",
                )
            with col_w2:
                warp_op_num = st.text_input(
                    "OP number",
                    placeholder="e.g. 62",
                    help="Sequence on the left rail (10, 20, 62…)",
                )
            with col_w3:
                warp_ref = st.text_input(
                    "Or paste Warp URL",
                    placeholder="…/work-order/…/operation/…",
                    help="Optional — we still prefer WO # + OP # above",
                )

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
                base_id = warp_wo_num.strip() or None
                seq = int(warp_op_num) if warp_op_num.strip().isdigit() else None
                parsed = parse_warp_ref(warp_ref) if warp_ref else {}
                # URL may only give internal IDs; still store them as fallback
                op_id = parsed.get("operation_id")
                wo_id = parsed.get("work_order_id")

                if not task_name.strip() and not (base_id or seq is not None or op_id or wo_id):
                    st.warning("Task name or a Warp WO number / OP number is required.")
                else:
                    default_name = task_name.strip()
                    if not default_name:
                        if base_id and seq is not None:
                            default_name = f"WO {base_id} · OP {seq}"
                        elif base_id:
                            default_name = f"WO {base_id}"
                        else:
                            default_name = "New task"

                    new_id = max([t["id"] for t in tasks], default=0) + 1
                    new_task = {
                        "id": new_id,
                        "name": default_name,
                        "assignee": assignee.strip() or "Unassigned",
                        "status": "To Do",
                        "notes": "",
                        "created": datetime.now().isoformat(),
                        "last_updated": datetime.now().isoformat(),
                        "warp_operation_id": op_id,
                        "warp_work_order_id": wo_id,
                        "warp_base_id": base_id or "",
                        "warp_sequence": seq,
                        "warp_op_url": operation_url(op_id, wo_id) if op_id else "",
                        "warp_wo_url": work_order_url(wo_id) if wo_id else "",
                        "warp_status": "",
                        "warp_clocked_in": [],
                        "warp_last_sync": None,
                        "warp_auto_sync": auto_sync,
                    }

                    can_lookup = base_id or op_id or wo_id
                    if can_lookup and pull_now and client.is_authenticated:
                        try:
                            snap = client.lookup_and_snapshot(
                                base_id=base_id,
                                sequence=seq,
                                operation_id=op_id if not (base_id and seq is not None) else None,
                                work_order_id=wo_id if not base_id else None,
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

                st.caption(
                    "Use the numbers from the Warp header, e.g. **3462114** and OP **62**."
                )
                c_wo, c_op = st.columns(2)
                with c_wo:
                    link_wo_num = st.text_input(
                        "WO number",
                        value=str(link_task.get("warp_base_id") or ""),
                        key="link_wo_num",
                        placeholder="e.g. 3462114",
                    )
                with c_op:
                    link_op_num = st.text_input(
                        "OP number",
                        value=str(link_task.get("warp_sequence") or ""),
                        key="link_op_num",
                        placeholder="e.g. 62",
                    )
                link_ref = st.text_input(
                    "Or paste Warp URL (optional)",
                    key="link_ref",
                    placeholder="…/shop-floor/work-order/…/operation/…",
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
                    base_id = link_wo_num.strip() or None
                    seq = int(link_op_num) if link_op_num.strip().isdigit() else None
                    parsed = parse_warp_ref(link_ref) if link_ref else {}
                    if base_id:
                        link_task["warp_base_id"] = base_id
                    if seq is not None:
                        link_task["warp_sequence"] = seq
                    # Keep any internal IDs from a pasted URL until sync resolves them
                    if parsed.get("operation_id"):
                        link_task["warp_operation_id"] = parsed["operation_id"]
                    if parsed.get("work_order_id"):
                        link_task["warp_work_order_id"] = parsed["work_order_id"]
                    if link_task.get("warp_operation_id"):
                        link_task["warp_op_url"] = operation_url(
                            link_task["warp_operation_id"],
                            link_task.get("warp_work_order_id"),
                        )
                    if link_task.get("warp_work_order_id"):
                        link_task["warp_wo_url"] = work_order_url(
                            link_task["warp_work_order_id"]
                        )
                    link_task["warp_auto_sync"] = link_auto
                    link_task["last_updated"] = datetime.now().isoformat()

                    if (
                        fetch_on_link
                        and task_is_warp_linked(link_task)
                        and client.is_authenticated
                    ):
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
                "Type the **WO number** and **OP number** from Shop Floor "
                "(e.g. WO `3462114`, OP `62` on the left rail)."
            )
            imp_wo = st.text_input(
                "WO number",
                key="imp_wo",
                placeholder="e.g. 3462114",
                help="Base ID — the number next to SHOP FLOOR in the header",
            )
            imp_op = st.text_input(
                "OP number",
                key="imp_op",
                placeholder="e.g. 62",
                help="Sequence number (10, 20, 62…) — not the long Operation ID in the URL",
            )
            imp_assignee = st.text_input(
                "Assignee override",
                value="",
                key="imp_assignee",
                placeholder="Leave blank to use clocked-in user",
            )

            if st.button(
                "Import from Warp",
                type="primary",
                use_container_width=True,
                disabled=not client.is_authenticated,
            ):
                if not client.is_authenticated:
                    st.error("Configure Warp token in the sidebar first.")
                elif not imp_wo.strip():
                    st.error("WO number is required.")
                elif not imp_op.strip().isdigit():
                    st.error("OP number is required (e.g. 62).")
                else:
                    try:
                        base_id = imp_wo.strip()
                        seq = int(imp_op.strip())
                        snap = client.lookup_and_snapshot(
                            base_id=base_id,
                            sequence=seq,
                        )
                        # Avoid duplicate OP links
                        existing = next(
                            (
                                t
                                for t in tasks
                                if t.get("warp_operation_id") == snap.operation_id
                                or (
                                    str(t.get("warp_base_id") or "") == base_id
                                    and t.get("warp_sequence") == seq
                                )
                            ),
                            None,
                        )
                        if existing:
                            st.warning(
                                f"{snap.label()} already linked to task #{existing['id']}. "
                                "Use Sync instead."
                            )
                        else:
                            new_id = max([t["id"] for t in tasks], default=0) + 1
                            new_task = {
                                "id": new_id,
                                "name": snap.title or snap.label(),
                                "assignee": imp_assignee.strip()
                                or (
                                    snap.clocked_in_users[0]
                                    if snap.clocked_in_users
                                    else "Unassigned"
                                ),
                                "status": snap.tracker_status,
                                "notes": "",
                                "created": datetime.now().isoformat(),
                                "last_updated": datetime.now().isoformat(),
                                "warp_operation_id": snap.operation_id,
                                "warp_work_order_id": snap.work_order_id,
                                "warp_base_id": snap.work_order_base_id or base_id,
                                "warp_sequence": snap.sequence_number
                                if snap.sequence_number is not None
                                else seq,
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
                                f"Imported {snap.label()} as task #{new_id} "
                                f"({snap.tracker_status})"
                            )
                            st.rerun()
                    except WarpError as exc:
                        st.error(str(exc))

    st.divider()
    st.markdown(
        """
**How to enter Warp OPs**

| What you type | Example | Where it is in Warp |
|---|---|---|
| **WO number** | `3462114` | Header: `SHOP FLOOR 3462114` (Base ID) |
| **OP number** | `62` | Left rail sequence / `SUP OP 62` |
| ~~Operation ID~~ | ~~`56330969`~~ | Long number in the URL — **not needed** |

**How status sync works**

| Warp signal | Tracker status |
|---|---|
| OP status Complete / Closed, or qty fully complete | **Done** |
| Anyone clocked in on the OP, or status Running / In Progress | **In Progress** |
| Open quality calls, or On Hold / Blocked | **Blocked** |
| Unstarted / Ready / no activity | **To Do** |

- **Open in Warp** uses the real Shop Floor URL after sync.
- **Sync all from Warp** (sidebar) refreshes every linked task.
- Auth: paste token under **Warp Settings**, or set `WARP_API_TOKEN`.
"""
    )
