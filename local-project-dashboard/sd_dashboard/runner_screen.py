"""Assignment controls rendered from shared runner readiness and ownership."""

from sd_db import runner, runner_controls

from .controls import field, form, select
from .markup import join, tag


def assignment_controls(assignment):
    identity, revision = assignment["id"], assignment["revision"]
    held = assignment.get("run")
    details = []
    if held:
        if held.get("cancel_requested"):
            details.append(tag("p", "Stop requested. The runner still owns cleanup.", class_="hint"))
        if held.get("quarantine"):
            details.append(tag("p", "Needs reconciliation: " + held["quarantine"], class_="notice"))
        if held.get("end_step") == "kept":
            details.append(tag("p", "Resolve changes in the kept checkout before resuming: ", tag("code", held["work_path"]), class_="hint"))
            details.append(form(f"/api/runner/{identity}/resume", label="Resume resolved checkout", command=f"sd worktree resume {identity} --if-revision {revision}", revision=revision, compact=True))
        if held.get("released_at"):
            details.append(tag("p", "Retained output: ", tag("code", held["retained_path"]), class_="hint"))
            details.append(tag("details", tag("summary", "Restore a copy"),
                form(f"/api/runner/{identity}/restore", field("New absolute destination", "destination", "", id=f"restore-{identity}", required=True, placeholder="/absolute/path/to/new-directory"),
                    label="Restore retained copy", command=f"sd worktree restore {identity} --run {held['run']} --destination NEW_DIRECTORY", revision=revision)))
        elif held.get("end_step"):
            details.append(tag("p", "Finishing cleanup: " + held["end_step"], class_="hint"))
    if assignment["status"] == "running" and not held:
        # No process to stop, so no control; the verb ends the row (sd:991).
        details.append(tag("p", "No owned runner attempt was recorded for this legacy assignment; end it with ",
            tag("code", f"sd runner cancel {identity}"), ".", class_="hint"))
    if (assignment["status"] == "queued" or (assignment["status"] == "running" and held)) and not (held and held.get("cancel_requested")):
        details.append(form(f"/api/runner/{identity}/cancel", label="Cancel queued assignment" if assignment["status"] == "queued" else "Request stop",
            command=f"sd runner cancel {identity}", revision=revision, compact=True))
    if assignment["status"] in {"blocked", "cancelled"} and (not held or held.get("released_at")):
        details.append(form(f"/api/runner/{identity}/requeue", label="Requeue assignment",
            command=f"sd runner requeue {identity}", revision=revision, compact=True))
    return join(details)


def item_controls(connection, row):
    if row["kind"] not in runner.RUNNABLE_KINDS:
        return ""
    ready = runner_controls.readiness(connection, row["id"])
    body = [tag("h2", "Run with an agent")]
    if row["kind"] in {"task", "work", "report"} and row["status"] != "done":
        repos = [(entry["path"], entry["path"]) for entry in ready["repos"] if entry["remote"]]
        body.append(tag("details", tag("summary", "Repository and branch"),
            tag("p", "Run the CLI command from the selected repository.", class_="hint"),
            form(f"/api/items/{row['id']}/prepare", select("Repository", "repo", [("", "Choose a repository…"), *repos], row["repo"], id="run-repo"),
                field("Item branch (existing or new)", "branch", row["branch"], id="run-branch", required=True),
                label="Save run setup", command=f"sd runner prepare {row['id']} --branch BRANCH", revision=ready["revision"])))
    if ready["allowed"]:
        body.append(form("/api/run", tag("input", type="hidden", name="items", value=str(row["id"]), data_item_revision=ready["revision"]),
            field("Time limit (minutes)", "budget_minutes", "90", type="number", id="item-budget", min="1", max="1440"),
            field("Budget (USD, optional)", "budget_usd", "", type="number", id="item-budget-usd", min="0", step="0.01"),
            label="Queue assignment", command=f"sd run --sequential {row['id']}"))
    else:
        body.append(tag("p", ready["reason"], class_="hint"))
    for assignment in ready["assignments"]:
        body.append(tag("article", tag("h3", f"Assignment #{assignment['id']} · {assignment['status']}"),
            tag("p", f"{assignment['role']} · {assignment['provider'] or 'Provider selected when started'}"),
            tag("p", assignment["result"], class_="hint") if assignment.get("result") else "",
            assignment_controls(assignment), class_="operation-card"))
    return tag("section", join(body), class_="control-panel")


def jobs_panel(connection):
    health = runner.heartbeat_state(connection)
    return tag("section", tag("h2", "Runner health"),
        tag("p", "Responding" if health["ok"] else health.get("reason") or "The runner needs attention.", class_="hint"),
        tag("p", "Last observed: " + health["timestamp"], class_="hint") if health.get("timestamp") else "",
        tag("p", "Accepted requests wait in the database until the runner starts them. A stop request is complete only after its owned process and checkout are settled.", class_="hint"))
