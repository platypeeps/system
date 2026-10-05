/* The one script. Hand-written, served from disk, no build step and no
 * framework. It is the only script the page loads, which is what makes
 * `script-src 'self'` with no inline script a policy the dashboard can keep.
 *
 * What it does, and no more: the four keyboard conveniences requirement 5
 * names -- `j`/`k` through rows, `enter` to open, `/` to filter, `?` to show
 * the map. It submits workflow forms and builds no markup from a
 * string: every node below is created with `createElement` and filled with
 * `textContent`, so no assignment of raw markup appears in this folder.
 *
 * It also reveals nothing. A row the keyboard lands on is marked, not
 * revealed; every control on the page is rendered visible, because a
 * hover-only or focus-only affordance is one a finger never finds.
 */

(function () {
  "use strict";

  document.querySelectorAll("time[data-local-time]").forEach(function (node) {
    var date = new Date(node.getAttribute("datetime"));
    if (!Number.isNaN(date.getTime())) {
      node.textContent = new Intl.DateTimeFormat(undefined, {
        dateStyle: "medium", timeStyle: "short"
      }).format(date);
    }
  });
  document.querySelectorAll("[data-changed-fields] input, [data-changed-fields] select, [data-changed-fields] textarea")
    .forEach(function (node) { node.dataset.initialValue = node.value; });

  var captureForms = new WeakMap();
  document.querySelectorAll("[data-capture-form]").forEach(function (form) {
    var type = form.elements.namedItem("capture_type");
    var title = form.elements.namedItem("title");
    var body = form.elements.namedItem("body");
    var related = form.elements.namedItem("related_item");
    var revision = form.elements.namedItem("revision");
    var button = form.querySelector("button[type=submit]");
    var refresh = form.querySelector("[data-capture-refresh]");
    var context = form.querySelector("[data-capture-context]");
    var cli = form.closest(".capture-panel").querySelector("[data-capture-cli]");
    // The note kinds, and `followup` is one of them: a followup NOTE. The
    // followup ITEM is the form's own value below, `followup_item`, and it
    // posts to `/api/items` as `kind: "followup"` (sd:719 step 6a). One word,
    // two controls, each labelled for what it writes.
    var noteKinds = ["followup", "comment", "question", "decision", "proposal"];
    var target = null, requestNumber = 0, loading = false, saving = false, needsRefresh = false;

    function update() {
      var task = type.value === "task";
      var followupItem = type.value === "followup_item";
      var note = noteKinds.includes(type.value);
      // A followup item may name the item it follows up; a note must.
      var linking = note || (followupItem && Boolean(related.value));
      form.querySelector("[data-capture-task]").hidden = !(task || followupItem);
      form.querySelector("[data-capture-note]").hidden = !(note || followupItem);
      body.closest(".field").hidden = followupItem;
      type.disabled = saving;
      title.disabled = !(task || followupItem) || saving;
      title.required = task || followupItem;
      body.disabled = !note || saving;
      body.required = note;
      related.disabled = !(note || followupItem) || saving || related.options.length < 2;
      related.required = note;
      revision.disabled = !note || !target;
      revision.value = target ? target.revision : "";
      button.textContent = task ? "Add task" : followupItem ? "Add followup item" : "Add " + type.value + " note";
      button.disabled = saving || (linking && (loading || !target)) || (!task && !followupItem && !note);
      refresh.disabled = saving || loading || !linking || !related.value;
      refresh.hidden = !needsRefresh;
      form.setAttribute("action", task || followupItem ? "/api/items" : "/api/items/" + (related.value || "0") + "/notes");
      cli.textContent = task ? 'sd task add "Title"' :
        followupItem ? 'sd task add --kind followup "Title"' :
        'sd task note ' + (related.value || "ITEM_ID") + ' --kind ' + type.value + ' --body "Text"';
      form.dataset.cli = cli.textContent;
    }

    async function loadContext() {
      var ticket = ++requestNumber;
      var id = related.value;
      target = null;
      loading = false;
      needsRefresh = false;
      context.removeAttribute("data-error");
      var followupItem = type.value === "followup_item";
      if (!(noteKinds.includes(type.value) || followupItem) || !/^[1-9][0-9]*$/.test(id)) {
        context.textContent = related.options.length < 2 ? "No related items yet. Capture a task first." :
          followupItem ? "Optional: choose the item this followup follows up. Left blank, it stands alone." :
          "Choose an item to attach this note.";
        update();
        return;
      }
      loading = true;
      context.textContent = "Loading current item…";
      update();
      try {
        var response = await fetch("/api/items/" + encodeURIComponent(id) + "/capture-context", {
          credentials: "same-origin", cache: "no-store"
        });
        var result = await response.json();
        if (ticket !== requestNumber) { return; }
        if (!response.ok) { throw new Error(result.error || "Could not load the related item."); }
        if (!result.item || String(result.item.id) !== id || !/^[a-f0-9]{64}$/.test(result.revision) ||
            ![result.item.title, result.item.kind, result.item.status].every(function (value) { return typeof value === "string"; })) {
          throw new Error("The item response was incomplete. Refresh the item before saving.");
        }
        target = { item: result.item, revision: result.revision };
        var status = result.item.status === "done" ? "completed" : result.item.status.replaceAll("_", " ");
        if (result.item.parked_at) { status += "; parked"; }
        var label = "#" + result.item.id + " · " + result.item.title + " · " + result.item.kind +
          " · " + (result.item.repo || "No repository") + " [" + status + "]";
        related.selectedOptions[0].textContent = label;
        context.textContent = (followupItem ? "Follows up " : "Attached to ") + label;
      } catch (problem) {
        if (ticket !== requestNumber) { return; }
        context.textContent = problem.message || "Could not load the item. Refresh it before saving.";
        context.setAttribute("data-error", "true");
        needsRefresh = true;
      } finally {
        if (ticket === requestNumber) { loading = false; update(); }
      }
    }

    function changed() {
      if (saving) { return; }
      form.querySelector(".form-result").textContent = "";
      loadContext();
    }
    type.addEventListener("change", changed);
    related.addEventListener("change", changed);
    refresh.addEventListener("click", changed);
    captureForms.set(form, {
      submission: function () {
        if (saving || loading) { return null; }
        if (type.value === "task") { return { action: "/api/items", values: { title: title.value } }; }
        if (type.value === "followup_item") {
          if (!related.value) { return { action: "/api/items", values: { title: title.value, kind: "followup" } }; }
          if (!target || String(target.item.id) !== related.value) { return null; }
          return { action: "/api/items", values: { title: title.value, kind: "followup", followup_of: target.item.id } };
        }
        if (!noteKinds.includes(type.value) || !target || String(target.item.id) !== related.value) { return null; }
        return { action: "/api/items/" + target.item.id + "/notes",
          values: { body: body.value, kind: type.value, revision: target.revision } };
      },
      saving: function (value) { saving = value; update(); },
      stale: function (message) {
        target = null;
        needsRefresh = true;
        context.textContent = message || "The item changed. Refresh the item to review its current context before saving again.";
        context.setAttribute("data-error", "true");
        update();
      }
    });
    update();
  });

  // Now (sd:719 step 6). The rows come merged, ranked and banded from
  // `/api/now`; the script paints them and decides nothing. `row.band` is
  // the server's name for the rank, and it becomes a class and a pill's
  // text -- no threshold lives here (`now_screen.band` holds the pack's).
  document.querySelectorAll("[data-now]").forEach(function (panel) {
    var rows = panel.querySelector("[data-now-rows]");
    var status = panel.querySelector("[data-now-status]");
    var refresh = panel.querySelector("[data-now-refresh]");
    var address = panel.getAttribute("data-now");

    function cell(text, className) {
      var td = document.createElement("td");
      td.textContent = text;
      if (className) { td.className = className; }
      return td;
    }

    function empty(text) {
      rows.replaceChildren();
      var tr = document.createElement("tr");
      tr.appendChild(cell(text, "now-empty")).setAttribute("colspan", "4");
      rows.appendChild(tr);
    }

    function paint(document_) {
      var all = Array.isArray(document_.rows) ? document_.rows : [];
      var loud = all.filter(function (row) { return row.band !== "queued"; }).length;
      status.textContent = all.length ?
        all.length + " thing" + (all.length === 1 ? "" : "s") + " \u00b7 " + loud + " above the fold" :
        "Nothing is asking for anything.";
      if (!all.length) { empty("Nothing is asking for anything."); return; }
      rows.replaceChildren();
      all.forEach(function (row) {
        var tr = document.createElement("tr");
        var pill = document.createElement("span");
        pill.className = "status band-" + row.band;
        pill.textContent = row.band;
        var band = document.createElement("td");
        band.className = "now-band";
        band.appendChild(pill);
        if (row.band === "broken") { tr.className = "now-broken"; }
        tr.append(band, cell(row.what, "now-what"), cell(row.detail || "", "now-detail"), cell(row.source, "now-source"));
        rows.appendChild(tr);
      });
    }

    async function draw() {
      refresh.disabled = true;
      status.textContent = "Reading the fleet\u2026";
      try {
        var response = await fetch(address, { credentials: "same-origin", cache: "no-store" });
        var result = await response.json();
        if (!response.ok) { throw new Error(result.error || "Now could not be read."); }
        paint(result);
      } catch (problem) {
        // A Now that cannot be reached is itself the loudest thing there is:
        // every source has gone quiet at once, and an empty table would say
        // the opposite (the pack's `drawNow`).
        paint({ rows: [{ band: "broken", what: "cannot reach the server", detail: String(problem.message || problem), source: "dashboard" }] });
      } finally {
        refresh.disabled = false;
      }
    }
    refresh.addEventListener("click", draw);
    draw();
  });

  document.querySelectorAll("[data-workflow-form]:not([data-capture-form])").forEach(function (form) {
    var match = /^\/api\/items\/([1-9][0-9]*)\/notes$/.exec(form.getAttribute("action"));
    var kind = form.elements.namedItem("kind");
    if (!match || !kind) { return; }
    kind.addEventListener("change", function () {
      var previous = form.dataset.cli;
      var command = "sd task note " + match[1] + " --kind " + kind.value + " --body TEXT";
      form.dataset.cli = command;
      document.querySelectorAll(".cli-equivalents code.command").forEach(function (node) {
        if (node.textContent === previous) { node.textContent = command; }
      });
    });
  });

  document.querySelectorAll('[action="/api/providers/configure"]').forEach(function (form) {
    ["author", "reviewer"].forEach(function (role) {
      form.elements.namedItem("first_" + role).addEventListener("change", function (event) {
        var order = form.elements.namedItem(role + "_order");
        order.value = [event.target.value].concat(order.value.split(",").map(function (name) {
          return name.trim();
        }).filter(function (name) { return name && name !== event.target.value; })).join(", ");
      });
    });
  });

  var KEYS = [
    ["j", "next row"],
    ["k", "previous row"],
    ["enter", "open the row"],
    ["/", "focus the filter"],
    ["?", "this map"],
    ["escape", "close the map"]
  ];

  function rows() {
    var table = document.querySelector(".listing-table tbody");
    return table ? Array.prototype.slice.call(table.querySelectorAll("tr")) : [];
  }

  var index = -1;

  function mark(next) {
    var all = rows();
    if (!all.length) { return; }
    if (index >= 0 && all[index]) { all[index].removeAttribute("data-active"); }
    index = Math.max(0, Math.min(all.length - 1, next));
    var row = all[index];
    row.setAttribute("data-active", "true");
    if (row.scrollIntoView) { row.scrollIntoView({ block: "nearest" }); }
  }

  function open() {
    var all = rows();
    var row = all[index];
    if (!row) { return; }
    var link = row.querySelector("a[href]");
    if (link) { window.location.assign(link.getAttribute("href")); }
  }

  function keymap() {
    var existing = document.querySelector(".keymap");
    if (existing) { existing.remove(); return; }
    var box = document.createElement("aside");
    box.className = "keymap";
    var heading = document.createElement("h2");
    heading.textContent = "Keys";
    box.appendChild(heading);
    var list = document.createElement("dl");
    KEYS.forEach(function (pair) {
      var key = document.createElement("dt");
      key.textContent = pair[0];
      var what = document.createElement("dd");
      what.textContent = pair[1];
      list.appendChild(key);
      list.appendChild(what);
    });
    box.appendChild(list);
    document.body.appendChild(box);
  }

  function typing(target) {
    if (!target) { return false; }
    var name = (target.tagName || "").toLowerCase();
    return name === "input" || name === "textarea" || name === "select" ||
      target.isContentEditable === true;
  }

  document.addEventListener("submit", async function (event) {
    var form = event.target;
    if (!form.matches("[data-workflow-form]") || form.matches("[data-palette-form]")) { return; }
    event.preventDefault();
    var output = form.querySelector(".form-result");
    var button = form.querySelector("button[type=submit]");
    if (button.disabled) { return; }
    var capture = captureForms.get(form);
    var submission = capture ? capture.submission() : null;
    if (capture && !submission) { return; }
    var action = submission ? submission.action : form.getAttribute("action");
    var values = {};
    new FormData(form).forEach(function (value, key) {
      var input = form.elements.namedItem(key);
      if (key !== "revision" && form.hasAttribute("data-changed-fields") &&
          input && value === input.dataset.initialValue) { return; }
      values[key] = value;
    });
    if (submission) { values = submission.values; }
    ["priority", "due", "repo"].forEach(function (key) {
      if (Object.prototype.hasOwnProperty.call(values, key) && values[key] === "") {
        values[key] = null;
      }
    });
    if (values.priority !== undefined && values.priority !== null) {
      values.priority = Number(values.priority);
    }
    if (values.correct !== undefined) { values.correct = values.correct === "true"; }
    if (action === "/api/contributions/acknowledge") {
      values.event_ids = Array.from(form.querySelectorAll('[name="event_ids"]')).map(function (input) { return input.value; });
    }
    if (action === "/api/run") {
      // The item page's Queue assignment: one item at its revision (Tasks posts its own runs).
      var item = form.querySelector('[name="items"]');
      values = { items: [Number(item.value)], revisions: { [item.value]: item.dataset.itemRevision }, parallel: false,
        budget_minutes: Number(values.budget_minutes),
        ...(values.budget_usd !== undefined && values.budget_usd !== "" ? { budget_usd: Number(values.budget_usd) } : {}) };
    }
    if (/^\/api\/skill-reviews\/[0-9]+\/apply$/.test(action)) {
      values.notes = Array.from(form.querySelectorAll('[name="notes"]:checked')).map(function (input) { return Number(input.value); });
    }
    if (/^\/api\/bills\/[^/]+\/cap$/.test(action)) {
      // The route takes a number or null; a form field is a string, and blank clears.
      // Only a blank field becomes null: Number("abc") is NaN, which JSON carries
      // as null, and null is the clear (#435's review). A value that is not a
      // finite decimal (the number input's own grammar) goes as its string, and
      // the library refuses it with 400 instead of clearing anything.
      var cap = values.cap_usd_month;
      values.cap_usd_month = cap === "" ? null : /^[-+]?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/.test(cap) && Number.isFinite(Number(cap)) ? Number(cap) : cap;
    }
    if (action === "/api/providers/configure") {
      var enabled = {};
      Object.keys(values).filter(function (key) { return key.startsWith("enabled."); }).forEach(function (key) {
        enabled[key.slice(8)] = values[key] === "true";
      });
      values = { revision: values.revision, enabled: enabled, orders: {
        author: (values.author_order || "").split(",").map(function (name) { return name.trim(); }).filter(Boolean),
        reviewer: (values.reviewer_order || "").split(",").map(function (name) { return name.trim(); }).filter(Boolean)
      } };
    }
    var token = document.querySelector('meta[name="sd-csrf"]');
    button.disabled = true;
    if (capture) { capture.saving(true); }
    output.textContent = "Saving…";
    output.removeAttribute("data-error");
    var completed = false, uncertain = false;
    try {
      var response = await fetch(action, {
        method: "POST", credentials: "same-origin",
        headers: { "Content-Type": "application/json", "X-SD-CSRF": token ? token.content : "" },
        body: JSON.stringify(values)
      });
      var result = await response.json();
      if (!response.ok) {
        output.textContent = capture && result.reload ?
          "The related item changed. Refresh it below before saving this draft." :
          result.error || "The change could not be saved.";
        if (capture && (result.reload || response.status === 404)) {
          capture.stale(response.status === 404 ? "The related item is no longer available. Refresh it or choose another item." : null);
        }
        if (result.reload && !capture) {
          var reload = document.createElement("a");
          reload.href = window.location.pathname + window.location.search;
          reload.textContent = " " + (form.dataset.reloadLabel || "Reload current item");
          output.appendChild(reload);
        }
        output.setAttribute("data-error", "true");
        return;
      }
      completed = true;
      output.textContent = "Saved.";
      window.location.assign(action === "/api/contributions/acknowledge" ? window.location.pathname + window.location.search :
        result.item && typeof result.item === "object" ?
        "/item/" + encodeURIComponent(result.item.id) :
        action.startsWith("/api/skills/") ? "/skills" :
        action === "/api/providers/configure" || action.startsWith("/api/bills/") ? "/operations?area=usage" :
        result.service ? "/operations?area=services" : "/operations?area=jobs");
    } catch (problem) {
      if (completed && capture) {
        output.textContent = "Saved. Open the item from Tasks to continue.";
        return;
      }
      output.textContent = "The response was interrupted. Reload to check whether it saved before trying again.";
      output.setAttribute("data-error", "true");
      if (capture) {
        uncertain = true;
        output.textContent = "The response was interrupted; this may already be saved. Check the outcome before retrying. ";
        var check = document.createElement("a");
        check.href = values.revision ? "/item/" + form.elements.namedItem("related_item").value :
          "/tasks?q=" + encodeURIComponent(values.title);
        check.target = "_blank";
        check.rel = "noopener";
        check.textContent = "Check in a new tab";
        output.appendChild(check);
        var retry = document.createElement("button");
        retry.type = "button";
        retry.className = "listing-go";
        retry.textContent = "Retry after checking";
        retry.addEventListener("click", function () {
          capture.saving(false);
          output.textContent = "Review the draft before submitting it again.";
        }, { once: true });
        output.appendChild(retry);
      }
    } finally {
      if (capture) {
        if (!completed && !uncertain) { capture.saving(false); }
      } else { button.disabled = false; }
    }
  });

  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) { return; }
    if (event.key === "Escape") {
      var box = document.querySelector(".keymap");
      if (box) { box.remove(); }
      return;
    }
    if (typing(event.target)) { return; }
    if (event.key === "j") { mark(index + 1); event.preventDefault(); return; }
    if (event.key === "k") { mark(index - 1); event.preventDefault(); return; }
    if (event.key === "Enter") { open(); return; }
    if (event.key === "/") {
      var field = document.querySelector("[data-listing-filter]");
      if (field) { field.focus(); event.preventDefault(); }
      return;
    }
    if (event.key === "?") { keymap(); event.preventDefault(); }
  });
})();


/* Finite command picker: all server input is a registered ID plus typed values. */
(function () {
  "use strict";
  var dialog = document.getElementById("command-palette");
  if (!dialog) { return; }
  var form = dialog.querySelector("[data-palette-form]");
  var item = form.elements.namedItem("item"), command = form.elements.namedItem("command");
  var output = dialog.querySelector("[data-palette-output]");
  var message = dialog.querySelector("[data-palette-message]");
  var filter = dialog.querySelector("[data-palette-filter]");
  var inventory = null, selected = null, opener = null, busy = false;
  var pageItem = location.pathname.match(/^\/item\/([1-9][0-9]*)\/?$/);
  var chosenItem = pageItem ? pageItem[1] : null;

  function node(name, text) {
    var element = document.createElement(name);
    if (text !== undefined) { element.textContent = text; }
    return element;
  }
  async function request(url, values) {
    var options = {credentials: "same-origin", cache: "no-store"};
    if (values !== undefined) {
      options.method = "POST";
      options.headers = {"Content-Type": "application/json", "X-SD-CSRF": document.querySelector('meta[name="sd-csrf"]').content};
      options.body = JSON.stringify(values);
    }
    var response = await fetch(url, options), result = await response.json();
    if (!response.ok) { throw new Error(result.error || "Command request failed."); }
    return result;
  }
  function options(select, values, current) {
    select.replaceChildren();
    values.forEach(function (value) {
      var option = node("option", value.label); option.value = value.value;
      option.selected = String(value.value) === String(current);
      select.appendChild(option);
    });
  }
  function quote(value) {
    value = String(value);
    return /^[a-zA-Z0-9_./:=+-]+$/.test(value) ? value : "'" + value.replace(/'/g, "'\"'\"'") + "'";
  }
  function updateCommand() {
    var display = dialog.querySelector("[data-palette-cli]");
    if (!selected || !inventory.item) {
      form.dataset.cli = "sd runner commands prepare --help";
      display.textContent = "Choose an item and command to see its CLI request.";
      return;
    }
    var argv = ["sd", "runner", "commands", "prepare", "--item", inventory.item,
      "--command", selected.name, "--if-revision", inventory.revision,
      "--catalog", inventory.sha256, "--screen", dialog.dataset.screen];
    var values = {}, held = null, complete = true;
    Object.entries(selected.placeholders).forEach(function (pair) {
      var input = form.elements.namedItem("value-" + pair[0]);
      var value = pair[1] === "item" ? inventory.item : input && input.value;
      if (value === null || value === undefined || value === "") { complete = false; return; }
      values[pair[0]] = value;
      argv.push("--value", pair[0] + "=" + value);
      if (pair[1] === "assignment") { held = inventory.assignments.find(function (row) { return String(row.id) === String(value); }); }
    });
    if (selected.scope !== "worktree") {
      if (!held) { complete = false; }
      else { argv.push("--target-revision", held.revision, "--target-run", held.run ? held.run.id : "none"); }
    }
    form.dataset.cli = argv.map(quote).join(" ");
    display.textContent = complete ? "CLI: " + form.dataset.cli + "\nRegistered command: " +
      selected.argv.map(function (part) { return quote(part[0] === "{" ? values[part.slice(1, -1)] : part); }).join(" ") :
      "Complete the typed fields to see the exact CLI request.";
    form.querySelector("button[type=submit]").disabled = busy || !complete;
  }
  function selectCommand() {
    selected = inventory && inventory.entries.find(function (entry) { return entry.name === command.value; });
    var container = dialog.querySelector("[data-palette-values]"); container.replaceChildren();
    form.querySelector("button[type=submit]").disabled = busy || !selected || !inventory.item;
    updateCommand();
    dialog.querySelector("[data-palette-scope]").textContent = !selected ? "" : selected.scope === "worktree" ?
      (selected.mutates ? "Queues an isolated checkout. The runner retains its output; command success does not finish the task." :
        "Runs immediately in the item's held checkout, or its registered repository when no checkout is held.") :
      "Controls the selected existing attempt. An uncertain response will be reconciled before another action.";
    if (!selected) { return; }
    Object.entries(selected.placeholders).forEach(function (pair) {
      var name = pair[0], kind = pair[1];
      if (kind === "item") { return; }
      var label = node("label", kind === "destination" ? "Restore to a new absolute path" : kind === "assignment" ? "Attempt" : "Provider");
      var input = node(kind === "destination" ? "input" : "select"); input.name = "value-" + name; input.required = true;
      if (kind === "destination") { input.type = "text"; input.placeholder = "/absolute/new-directory"; }
      if (kind === "provider") { options(input, inventory.providers.map(function (provider) { return {value: provider, label: provider}; })); }
      if (kind === "assignment") { options(input, inventory.assignments.map(function (held) {
        return {value: held.id, label: "#" + held.id + " · " + held.status + (held.run ? " · attempt " + held.run.run : " · queued")};
      })); }
      input.addEventListener("input", updateCommand);
      input.addEventListener("change", updateCommand);
      label.appendChild(input); container.appendChild(label);
    });
    updateCommand();
  }
  function applyFilter() {
    var query = filter.value.toLowerCase();
    dialog.querySelectorAll("[data-palette-local]").forEach(function (button) { button.hidden = !button.textContent.toLowerCase().includes(query); });
    if (!inventory || !inventory.configured) { return; }
    var previous = command.value;
    options(command, inventory.entries.filter(function (entry) {
      return (entry.label + " " + entry.argv.join(" ")).toLowerCase().includes(query);
    }).map(function (entry) { return {value: entry.name, label: entry.label}; }), previous);
    selectCommand();
  }
  async function load() {
    form.hidden = true;
    message.textContent = "Loading registered commands…";
    try {
      inventory = await request("/api/palette?screen=" + encodeURIComponent(dialog.dataset.screen) +
        (chosenItem ? "&item=" + encodeURIComponent(chosenItem) : ""));
      if (!inventory.configured) { message.textContent = inventory.message; return; }
      form.hidden = false;
      message.textContent = inventory.entries.length ? "Choose an item and command." : "No commands are registered on this screen.";
      if (inventory.rejected && inventory.rejected.length) {
        message.textContent += " Rejected from commands.yaml: " + inventory.rejected.map(function (row) { return row.name + " (" + row.reason + ")"; }).join("; ") + ".";
      }
      options(item, [{value: "", label: "Choose an item"}].concat(inventory.items.map(function (row) {
        return {value: row.id, label: "#" + row.id + " · " + row.title};
      })), inventory.item);
      applyFilter();
    } catch (error) { message.textContent = error.message; }
  }
  function localActions() {
    var container = dialog.querySelector("[data-palette-actions]"); container.replaceChildren();
    document.querySelectorAll("main [data-workflow-form][data-cli]:not([data-palette-form])").forEach(function (target) {
      var submit = target.querySelector("button[type=submit]");
      if (!submit) { return; }
      var button = node("button", submit.textContent + " · " + target.dataset.cli);
      button.type = "button"; button.dataset.paletteLocal = "true";
      button.addEventListener("click", function () {
        dialog.close();
        var ancestor = target.parentElement;
        while (ancestor) { if (ancestor.tagName === "DETAILS") { ancestor.open = true; } ancestor = ancestor.parentElement; }
        target.scrollIntoView({block: "center"});
        (target.querySelector("input:not([type=hidden]),select,textarea") || submit).focus();
      });
      container.appendChild(button);
    });
  }
  function open(trigger) {
    if (dialog.open) { return; }
    opener = trigger || document.activeElement;
    localActions(); dialog.showModal(); filter.focus(); load();
  }
  document.querySelectorAll("[data-palette-open]").forEach(function (button) { button.addEventListener("click", function () { open(button); }); });
  dialog.querySelector("[data-palette-close]").addEventListener("click", function () { dialog.close(); });
  dialog.addEventListener("close", function () { if (opener && opener.isConnected) { opener.focus(); } });
  document.addEventListener("keydown", function (event) {
    var typing = event.target.closest("input,select,textarea,[contenteditable=true]");
    if ((!typing && event.key === "p" && !event.ctrlKey && !event.metaKey && !event.altKey) ||
        ((event.ctrlKey || event.metaKey) && event.key === "k")) {
      event.preventDefault(); open();
    }
  });
  filter.addEventListener("input", applyFilter);
  command.addEventListener("change", selectCommand);
  item.addEventListener("change", function () { chosenItem = item.value || null; load(); });

  async function readOutput(note, target) {
    var offset = 0, text = "", state;
    do {
      state = await request("/api/executions/" + note + "?offset=" + offset);
      text += state.output; offset = state.next_offset;
    } while (state.output.length && offset < 2097152);
    target.textContent = text || (state.output_expired ?
      "Output expired on " + state.output_expired + " (kept ninety days); the entry, arguments and exit code remain." :
      state.ended ? "Command ended with no output." : "Waiting for output…");
    return state;
  }
  form.addEventListener("submit", async function (event) {
    event.preventDefault();
    if (busy || !selected || !inventory.item) { return; }
    busy = true; form.querySelector("button[type=submit]").disabled = true;
    var polling, values = {}, target = null;
    try {
      Object.entries(selected.placeholders).forEach(function (pair) {
        var kind = pair[1], input = form.elements.namedItem("value-" + pair[0]);
        values[pair[0]] = kind === "item" ? inventory.item : kind === "assignment" ? Number(input.value) : input.value;
        if (kind === "assignment" && selected.scope !== "worktree") {
          var held = inventory.assignments.find(function (row) { return row.id === values[pair[0]]; });
          if (!held) { throw new Error("Choose a current attempt."); }
          target = {assignment: held.id, revision: held.revision, run: held.run ? held.run.id : null};
        }
      });
      var prepared = await request("/api/palette/prepare", {item: inventory.item, command: selected.name, values: values,
        revision: inventory.revision, catalog: inventory.sha256, screen: dialog.dataset.screen, target: target});
      var note = prepared.execution.note;
      output.textContent = "Execution #" + note + " recorded before dispatch.";
      dialog.querySelector("[data-palette-cli]").textContent += "\n" +
        (prepared.assignments.length ? "" : "sd runner commands execute " + note + "\n") +
        "sd runner commands output " + note;
      if (prepared.assignments.length) {
        message.textContent = "Queued assignment #" + prepared.assignments[0].id + ". Follow it on the item or in execution history.";
      } else {
        message.textContent = "Running execution #" + note + "…";
        polling = setInterval(function () { readOutput(note, output).catch(function (error) { message.textContent = error.message; }); }, 1000);
        await request("/api/palette/" + note + "/execute", {});
        var ended = await readOutput(note, output);
        message.textContent = "Execution #" + note + " ended with exit " + ended.exit_code + ".";
      }
    } catch (error) {
      message.textContent = error.message + " Inspect execution history before retrying an unfinished command.";
    } finally {
      clearInterval(polling); busy = false; form.querySelector("button[type=submit]").disabled = true;
      // A fresh snapshot is required for another authorization after this note changed the item.
    }
  });
  document.querySelectorAll("[data-execution]").forEach(function (button) {
    button.addEventListener("click", async function () {
      var target = document.querySelector('[data-execution-output="' + button.dataset.execution + '"]');
      try { await readOutput(button.dataset.execution, target); } catch (error) { target.textContent = error.message; }
    });
  });
  document.querySelectorAll("[data-execution-reconcile]").forEach(function (button) {
    button.addEventListener("click", async function () {
      var target = document.querySelector('[data-execution-output="' + button.dataset.executionReconcile + '"]');
      button.disabled = true;
      try {
        var result = await request("/api/palette/" + button.dataset.executionReconcile + "/reconcile", {});
        target.textContent = "Outcome reconciled: exit " + result.exit_code + ". " + result.output;
      } catch (error) { target.textContent = error.message; button.disabled = false; }
    });
  });
}());
