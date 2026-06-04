import streamlit as st
import json
from datetime import datetime, date
import os
import pandas as pd
import re

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
    cleaned = re.sub(r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?\]\s*', '', notes_text)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    return cleaned

# ===================== PAGE CONFIG =====================
st.set_page_config(page_title="Team Task Tracker", layout="wide", page_icon="🚀")

st.title("🚀 Team Task Progress Tracker")
st.caption("Track progress across the team")

tasks = load_tasks()

# ===================== SIDEBAR =====================
st.sidebar.header("🔍 Filters")
all_assignees = sorted(set(t["assignee"] for t in tasks)) if tasks else []
selected_assignees = st.sidebar.multiselect("Assignee", all_assignees, default=all_assignees)

statuses = ["To Do", "In Progress", "Done", "Blocked"]
selected_statuses = st.sidebar.multiselect("Status", statuses, default=statuses)

# ===================== METRICS =====================
today = date.today()
done_today = sum(1 for t in tasks if t["status"] == "Done" and 
                 datetime.fromisoformat(t["last_updated"]).date() == today)
in_progress = sum(1 for t in tasks if t["status"] == "In Progress")
open_tasks = len([t for t in tasks if t["status"] in ["To Do", "In Progress"]])

col1, col2, col3, col4 = st.columns(4)
col1.metric("✅ Done Today", done_today)
col2.metric("🔄 In Progress", in_progress)
col3.metric("📋 Total Tasks", len(tasks))
col4.metric("📌 Open Tasks", open_tasks)

st.divider()

# ===================== ADD NEW TASK =====================
with st.container(border=True):
    st.subheader("➕ Add New Task")
    with st.form("add_form", clear_on_submit=True):
        col_a, col_b = st.columns([3, 2])
        with col_a:
            task_name = st.text_input("Task Name*", placeholder="What needs to be done?")
        with col_b:
            assignee = st.text_input("Assignee", value="Unassigned")

        submitted = st.form_submit_button("Add Task", type="primary", use_container_width=True)

        if submitted:
            if task_name.strip():
                new_id = max([t["id"] for t in tasks], default=0) + 1
                new_task = {
                    "id": new_id,
                    "name": task_name.strip(),
                    "assignee": assignee.strip() or "Unassigned",
                    "status": "To Do",
                    "notes": "",
                    "created": datetime.now().isoformat(),
                    "last_updated": datetime.now().isoformat()
                }
                tasks.append(new_task)
                save_tasks(tasks)
                st.success(f"Task #{new_id} added successfully!")
                st.rerun()
            else:
                st.warning("Task name is required.")

# ===================== CURRENT TASKS =====================
st.subheader("📋 Current Tasks")

filtered = [t for t in tasks 
            if (not selected_assignees or t["assignee"] in selected_assignees)
            and t["status"] in selected_statuses]

if filtered:
    df = pd.DataFrame(filtered)

    df["Notes"] = df["notes"].apply(
        lambda x: clean_note_text(x)[:80] + "…" if len(clean_note_text(x)) > 80 else clean_note_text(x)
    )

    display_df = df[["id", "name", "assignee", "status", "Notes", "last_updated"]].copy()
    display_df["last_updated"] = pd.to_datetime(display_df["last_updated"]).dt.strftime("%m/%d %H:%M")
    display_df = display_df.rename(columns={"status": "Status"})

    # ====================== ROW + STATUS COLORING ======================
    def highlight_rows(row):
        status = row["Status"]
        if status == "Done":
            color = "#d4edda"
        elif status == "In Progress":
            color = "#fff3cd"
        elif status == "Blocked":
            color = "#f8d7da"
        elif status == "To Do":
            color = "#cce5ff"
        else:
            color = ""
        return [f'background-color: {color}' for _ in row]

    def color_status_text(val):
        if val == "Done":
            return "color: #155724; font-weight: 600;"
        elif val == "In Progress":
            return "color: #856404; font-weight: 600;"
        elif val == "Blocked":
            return "color: #721c24; font-weight: 600;"
        elif val == "To Do":
            return "color: #004085; font-weight: 600;"
        return ""

    styled_df = (
        display_df.style
        .apply(highlight_rows, axis=1)
        .map(color_status_text, subset=["Status"])
    )

    with st.container(border=True):
        st.dataframe(
            styled_df,
            use_container_width=True,
            hide_index=True,
            column_config={
                "id": st.column_config.NumberColumn("ID", width="small"),
                "name": st.column_config.TextColumn("Task", width="large"),
                "Notes": st.column_config.TextColumn("Notes", width="medium"),
                "Status": st.column_config.TextColumn("Status", width="medium"),
            }
        )
else:
    st.info("No tasks match the current filters.")

# ===================== TASK MANAGEMENT =====================
st.subheader("🛠️ Manage Tasks")

col_update, col_remove = st.columns(2)

# ===================== UPDATE TASK (with Reassign) =====================
with col_update:
    with st.container(border=True):
        st.markdown("#### ✏️ Update Task")

        if tasks:
            task_options = {f"#{t['id']} - {t['name']} ({t['assignee']})": t['id'] for t in tasks}
            selected_label = st.selectbox("Select task", list(task_options.keys()), key="update_select")
            selected_id = task_options[selected_label]
            task = next(t for t in tasks if t["id"] == selected_id)

            # Status
            new_status = st.selectbox(
                "New Status", 
                statuses, 
                index=statuses.index(task["status"]), 
                key="new_status"
            )

            # Reassign
            new_assignee = st.text_input(
                "Reassign To", 
                value=task["assignee"],
                key="new_assignee"
            )

            # Note
            new_note = st.text_area(
                "Add note (optional)", 
                value="", 
                height=100, 
                key="new_note"
            )

            if st.button("Update Task", type="primary", use_container_width=True):
                task["status"] = new_status
                task["assignee"] = new_assignee.strip() or "Unassigned"
                if new_note.strip():
                    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
                    task["notes"] = (task.get("notes", "") + f"\n[{timestamp}] {new_note.strip()}").strip()
                task["last_updated"] = datetime.now().isoformat()
                save_tasks(tasks)
                st.success("Task updated!")
                st.rerun()
        else:
            st.info("No tasks available to update.")

# ===================== REMOVE TASKS =====================
with col_remove:
    with st.container(border=True):
        st.markdown("#### 🗑️ Remove Tasks")

        if tasks:
            task_labels = {
                f"#{t['id']} - {t['name']} ({t['assignee']}) [{t['status']}]": t['id'] 
                for t in tasks
            }

            selected_to_remove = st.multiselect(
                "Select task(s) to remove",
                options=list(task_labels.keys()),
                key="remove_select"
            )

            if selected_to_remove:
                confirm = st.checkbox(
                    f"Confirm removal of {len(selected_to_remove)} task(s)", 
                    key="confirm_remove"
                )

                if st.button("Remove Selected", disabled=not confirm, type="secondary", use_container_width=True):
                    ids_to_remove = [task_labels[label] for label in selected_to_remove]
                    tasks = [t for t in tasks if t["id"] not in ids_to_remove]
                    save_tasks(tasks)
                    st.success(f"Removed {len(ids_to_remove)} task(s).")
                    st.rerun()
        else:
            st.info("No tasks to remove.")

# ===================== FULL NOTES EXPANDER =====================
with st.expander("📜 Show Full Original Notes (with timestamps)"):
    if tasks:
        for t in tasks:
            if t.get("notes"):
                st.markdown(f"**#{t['id']} – {t['name']}** ({t['assignee']})")
                st.text(t["notes"])
    else:
        st.info("No notes available.")
