from datetime import date
from sheets import todo_sheet
from helpers import format_date

def add_todo(task):
    try:
        sheet = todo_sheet()
        sheet.append_row([task, "Pending", date.today().strftime("%d/%m/%Y")])
        return f"✅ Added to your to-do list: _{task}_"
    except Exception as e:
        return f"❌ Error adding task: {str(e)}"

def complete_todo(task):
    try:
        sheet = todo_sheet()
        records = sheet.get_all_records()
        matches = [
            (i + 2, r) for i, r in enumerate(records)
            if task.lower() in r.get("Task", "").lower() and r.get("Status") == "Pending"
        ]
        if not matches:
            return f"❌ No pending task found matching '{task}'"
        if len(matches) > 1:
            return "_DISAMBIG_TODO_COMPLETE_:" + "|".join(r.get("Task", "") for _, r in matches)
        row_idx, r = matches[0]
        done_task = r.get("Task", "")
        sheet.update_cell(row_idx, 2, "Done")
        # Reload records to get updated list
        updated_records = sheet.get_all_records()
        pending = [rec for rec in updated_records if rec.get("Status") == "Pending"]
        def _escape(s):
            for ch in r"\_*[]()~`>#+=|{}.!-":
                s = s.replace(ch, f"\\{ch}")
            return s
        struck = f"~{_escape(done_task)}~"
        if not pending:
            return f"✅ {struck}\n\nNo more pending tasks\\!"
        lines = [f"✅ {struck}\n"]
        lines.append(f"📝 *{len(pending)} pending task\\(s\\):*\n")
        for rec in pending:
            t = _escape(rec.get("Task", ""))
            d = _escape(format_date(rec.get("Added", "")))
            lines.append(f"• {t} _\\(added {d}\\)_")
        return "\n".join(lines)
    except Exception as e:
        return f"❌ Error completing task: {str(e)}"

def delete_todo(task):
    try:
        sheet = todo_sheet()
        records = sheet.get_all_records()
        matches = [
            (i + 2, r) for i, r in enumerate(records)
            if task.lower() in r.get("Task", "").lower()
        ]
        if not matches:
            return f"❌ No task found matching '{task}'"
        if len(matches) > 1:
            return "_DISAMBIG_TODO_DELETE_:" + "|".join(r.get("Task", "") for _, r in matches)
        row_idx, r = matches[0]
        sheet.delete_rows(row_idx)
        return f"Deleted — {r.get('Task')} ✅"
    except Exception as e:
        return f"❌ Error deleting task: {str(e)}"

def list_todos():
    try:
        sheet = todo_sheet()
        records = sheet.get_all_records()
        pending = [r for r in records if r.get("Status") == "Pending"]
        if not pending:
            return "✅ No pending tasks!"
        response = f"📝 *{len(pending)} pending task(s):*\n\n"
        for r in pending:
            response += f"• {r.get('Task')} _(added {format_date(r.get('Added', ''))})_\n"
        return response
    except Exception as e:
        return f"❌ Error listing tasks: {str(e)}"
