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
    """Remove timestamps like [2025-06-04 10:15] from notes for cleaner display."""
    if not isinstance(notes_text, str) or not notes_text.strip():
        return ""
    
    # Remove timestamp patterns: [2025-06-04 10:15] or [2025-06-04 10:15:30]
    cleaned = re.sub(r'\[\d{4}-\d{2}-\d{2} \d{2}:\d{2}(:\d{2})?\]\s*', '', notes_text)
    
    # Clean up extra whitespace (newlines, multiple spaces, etc.)
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()
    
    return cleaned

st.set_page_config(page_title="Team Task Tracker", layout="wide")
st.title("🚀 Team Task Progress Tracker")

tasks = load_tasks()

# ===================== SIDEBAR =====================
st.sidebar.header("Filters")
all_assignees = sorted(set(t["assignee"] for t in tasks)) if tasks else []
selected_assignees = st.sidebar.multiselect("Assignee", all_assignees, default=all_assignees)

statuses = ["To Do", "In Progress", "Done", "Blocked"]
selected_statuses = st.sidebar.multiselect("Status", statuses, default=statuses)

# ===================== MAIN CONTENT =====================

# Metrics
today = date.today()
done_today = sum(1 for t in tasks if t["status"] == "Done" and 
                 datetime.fromisoformat(t["last_updated"]).date() == today)
in_progress = sum(1 for t in tasks if t["status"] == "In Progress")

col1, col2, col3, col4 = st.columns(4)
col1.metric("✅ Done Today", done_today)
col2.metric("🔄 In Progress", in_progress)
col3.metric("📋 Total Tasks", len(tasks))
col4.metric("📌 Open Tasks", len([t for t in tasks if t["status"] in ["To Do", "In Progress"]]))

st.divider()

# === Add New Task ===
with st.expander("➕ Add New Task", expanded=True):
    with st.form("add_form", clear_on_submit=True):
        col_a, col_b = st.columns([3, 2])
        with col_a:
            task_name = st.text_input("Task Name*")
        with col_b:
            assignee = st.text_input("Assignee", value="Unassigned")
        
        if st.form_submit_button("Add Task", type="primary"):
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
                st.success(f"Task #{new_id} added!")
                st.rerun()

# === Current Tasks (with clean Notes column) ===
st.subheader("Current Tasks")

filtered = [t for t in tasks 
            if (not selected_assignees or t["assignee"] in selected_assignees)
            and t["status"] in selected_statuses]

if filtered:
    df = pd.DataFrame(filtered)

    # Clean notes (remove timestamps) and create preview for the table
    df["Notes"] = df["notes"].apply(
        lambda x: clean_note_text(x)[:80] + "…" if len(clean_note_text(x)) > 80 else clean_note_text(x)
    )

    # Select columns for display
    display_df = df[["id", "name", "assignee", "status", "Notes", "last_updated"]].copy()
    display_df["last_updated"] = pd.to_datetime(display_df["last_updated"]).dt.strftime("%m/%d %H:%M")

    st.dataframe(
        display_df,
        use_container_width=True,
        hide_index=True,
        column_config={
            "id": st.column_config.NumberColumn("ID", width="small"),
            "name": st.column_config.TextColumn("Task", width="large"),
            "Notes": st.column_config.TextColumn("Notes", width="medium"),
        }
    )
else:
    st.info("No tasks match the current filters.")

# === Update Task ===
st.subheader("Update Task Progress")

if tasks:
    task_options = {f"#{t['id']} - {t['name']} ({t['assignee']})": t['id'] for t in tasks}
    selected_label = st.selectbox("Select a task to update", list(task_options.keys()))
    selected_id = task_options[selected_label]
    task = next(t for t in tasks if t["id"] == selected_id)

    col1, col2 = st.columns([1, 2])
    with col1:
        new_status = st.selectbox("New Status", statuses, index=statuses.index(task["status"]))
    with col2:
        new_note = st.text_area("Add note (optional)", value="", height=80)

    if st.button("Update Task", type="primary"):
        task["status"] = new_status
        if new_note.strip():
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
            task["notes"] = (task.get("notes", "") + f"\n[{timestamp}] {new_note.strip()}").strip()
        task["last_updated"] = datetime.now().isoformat()
        save_tasks(tasks)
        st.success("Task updated!")
        st.rerun()

# ===================== REMOVE TASKS =====================
st.divider()
st.subheader("🗑️ Remove Tasks")

if tasks:
    task_labels = {
        f"#{t['id']} - {t['name']} ({t['assignee']}) [{t['status']}]": t['id'] 
        for t in tasks
    }

    st.markdown("**Remove one or more tasks**")
    selected_to_remove = st.multiselect(
        "Select task(s) to remove",
        options=list(task_labels.keys())
    )

    if selected_to_remove:
        confirm = st.checkbox(f"I confirm I want to permanently remove the {len(selected_to_remove)} selected task(s)")
        
        if st.button("Remove Selected Task(s)", disabled=not confirm, type="secondary"):
            ids_to_remove = [task_labels[label] for label in selected_to_remove]
            tasks = [t for t in tasks if t["id"] not in ids_to_remove]
            save_tasks(tasks)
            st.success(f"Removed {len(ids_to_remove)} task(s).")
            st.rerun()
else:
    st.info("There are no tasks to remove.")

# Show full original notes (with timestamps) if needed
if st.checkbox("Show full original notes (with timestamps)"):
    for t in tasks:
        if t.get("notes"):
            st.markdown(f"**#{t['id']} – {t['name']}** ({t['assignee']})")
            st.text(t["notes"])
