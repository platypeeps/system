"""Durable publication claims for the supported Google Drive connector.

This module never calls a network service. It dispatches one concrete connector
operation once, then validates its recorded response. A lost import response
requires readback; an empty search is never evidence that creating again is safe.
The caller must not execute a receipt or a pending operation a second time.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from html.parser import HTMLParser
from pathlib import Path

from . import publication_journal as journal
from . import writing
from .database import transaction
from .progress import _fields
from .workflow import WorkflowError, _text
from .writes import _transition, add_note, now, set_item_fields, unresolved_state

NATIVE = "application/vnd.google-apps.document"
FOLDER = "application/vnd.google-apps.folder"
MAX_PAYLOAD = 24 * 1024 * 1024
TERMINAL = {"published", "abandoned"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _result(value):
    if not isinstance(value, dict):
        raise WorkflowError("connector receipt must be a JSON object")
    if value.get("isError"):
        raise WorkflowError("connector reported an error; reconcile instead of recording success")
    value = value.get("structuredContent", value)
    if not isinstance(value, dict) or not isinstance(value.get("result", value), dict):
        raise WorkflowError("connector response body must be a JSON object")
    return value.get("result", value)


def _restore_epoch(connection):
    row = connection.execute("SELECT MAX(id) FROM state WHERE kind='restore'").fetchone()
    return row[0] or 0


def _restored(connection):
    if unresolved_state(connection, "restore"):
        raise WorkflowError("publication is held by an unresolved restore; reconcile recovery first")


def assert_mutable(connection, item):
    row = connection.execute("SELECT id FROM publication_claim WHERE active_item=?", (item,)).fetchone()
    if row:
        raise WorkflowError(f"publication claim {row['id']} owns this piece; finish or explicitly abandon it first")


def _context(context):
    if not isinstance(context, dict):
        raise WorkflowError("publication context needs profile, drafts, and published connector readbacks")
    profile = _result(context.get("profile"))
    account = _text(profile.get("email"), "verified Drive account")
    folders = {}
    for key, expected in (("drafts", "Drafts"), ("published", "Published")):
        folder = _result(context.get(key))
        folder_id = folder.get("id")
        if not isinstance(folder_id, str) or not folder_id or folder.get("title") != expected or folder.get("mime_type") != FOLDER:
            raise WorkflowError(f"Drive {key} folder readback must be the configured {expected} folder")
        folders[key] = folder_id
    if folders["drafts"] == folders["published"]:
        raise WorkflowError("Drafts and Published must be different folders")
    expected = context.get("expected")
    if not isinstance(expected, dict) or expected != {"account": account, **folders}:
        raise WorkflowError("verified Drive account and folders do not match the explicit machine configuration")
    return {"account": account, **folders}


class _HTML(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.text = []
        self.images = []
        self.image_positions = []
        self.image_aspects = []
        self.links = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"head", "script", "style"}:
            self.skip += 1
        if tag in {"p", "div", "br", "li", "td", "th", "h1", "h2", "h3", "h4", "pre"}:
            self.text.append(" ")
        if tag == "img":
            source = attrs.get("src", "")
            if not re.fullmatch(r"data:image/(?:png|jpeg|gif|webp);base64,[A-Za-z0-9+/=]+", source):
                raise WorkflowError("publication images must be embedded raster data, never local or remote references")
            self.images.append(source)
            self.image_positions.append(_digest(_normal("".join(self.text))))
            try:
                self.image_aspects.append(float(attrs["width"]) / float(attrs["height"]))
            except (KeyError, ValueError, ZeroDivisionError):
                raise WorkflowError("publication image dimensions must be known before import") from None
        if tag == "a" and attrs.get("href"):
            self.links.append(attrs["href"])

    def handle_endtag(self, tag):
        if tag in {"head", "script", "style"}:
            self.skip -= 1
        if tag in {"p", "div", "li", "td", "th", "h1", "h2", "h3", "h4", "pre"}:
            self.text.append(" ")

    def handle_data(self, data):
        if not self.skip:
            self.text.append(data)


def _normal(text):
    return " ".join(text.replace("\u00a0", " ").split())


def html_inventory(html):
    if not isinstance(html, str) or not html or len(html.encode()) > MAX_PAYLOAD:
        raise WorkflowError("publication HTML is empty or exceeds the 24 MiB payload limit")
    parsed = _HTML()
    parsed.feed(html)
    return {"text": _normal("".join(parsed.text)), "images": len(parsed.images),
            "image_sha256": [_digest(value) for value in parsed.images],
            "image_positions": parsed.image_positions, "image_aspects": parsed.image_aspects,
            "links": sorted(set(parsed.links))}


def _source_files(row, assets):
    snapshot = writing._snapshot(row)
    hashes = dict(snapshot["hashes"])
    root = snapshot["path"].parent.resolve()
    if not isinstance(assets, dict):
        raise WorkflowError("publication assets must be a path-to-hash object")
    for relative, expected in assets.items():
        path = root / relative
        if not isinstance(relative, str) or Path(relative).is_absolute() or not path.resolve().is_relative_to(root) or not path.is_file():
            raise WorkflowError("publication asset escapes the piece or is missing")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != expected:
            raise WorkflowError(f"publication asset changed: {relative}")
        hashes["asset:" + relative] = actual
    return snapshot, hashes


def render_payload(connection, item):
    from .publication_render import build
    row = writing._piece(connection, item)["item"]
    return build(writing._snapshot(row), _fields(row["fields"]).get("writing", {}), row["repo"])


def create_claim(connection, item, payload, context, *, expected_revision=None, who):
    """Bind current reviewed source, rendered HTML, and verified destination."""
    destination = _context(context)
    if not isinstance(payload, dict):
        raise WorkflowError("publication payload must be a JSON object")
    inventory = html_inventory(payload.get("html"))
    with transaction(connection):
        _restored(connection)
        assert_mutable(connection, item)
        state = writing._piece(connection, item, expected_revision)
        row = state["item"]
        if writing.pieces_owner(connection, row["repo"]) != "row":
            raise WorkflowError("publication claims require completed database cutover")
        for manifest, recorded in journal.for_piece(connection, row["repo"], row["piece"]):
            held = connection.execute("SELECT payload,state FROM publication_claim WHERE id=?", (manifest["claim"],)).fetchone()
            if held is None or json.loads(held["state"]) != recorded or json.loads(held["payload"]) != manifest["payload"]:
                raise WorkflowError("external publication journal contains newer evidence; recover it before another claim")
        writing.preflight(connection, item)
        if row["parked_at"]:
            raise WorkflowError("parked pieces cannot be published")
        previous = connection.execute("SELECT state FROM publication_claim WHERE item=?", (item,)).fetchall()
        if any(json.loads(entry[0]).get("reconcile_required") for entry in previous):
            raise WorkflowError("an earlier uncertain publication must be reconciled before another claim")
        tip = payload.get("tip")
        if tip:
            for existing in connection.execute("SELECT item,payload,state FROM publication_claim WHERE item<>?", (item,)):
                other, progress = json.loads(existing["payload"]), json.loads(existing["state"])
                if other.get("tip_source") == payload.get("tip_source") and (progress["phase"] != "abandoned" or progress.get("reconcile_required")):
                    raise WorkflowError("the attached tip is already reserved or published by another piece")
        snapshot, hashes = _source_files(row, payload.get("assets", {}))
        if payload != render_payload(connection, item):
            raise WorkflowError("publication payload differs from the canonical current-source render")
        if payload.get("source_hashes") != snapshot["hashes"]:
            raise WorkflowError("rendered source changed before the publication claim")
        fields = _fields(row["fields"])
        title = fields.get("writing", {}).get("title") or row["title"]
        if payload.get("title") != title:
            raise WorkflowError("rendered title differs from current writing metadata")
        if re.search(r"\bTODO\b|\bTBD\b|\[MISSING IMAGE:", inventory["text"]):
            raise WorkflowError("publication contains unresolved placeholder text")
        claim = uuid.uuid4().hex
        payload = {**payload, "inventory": inventory, "destination": destination,
                   "repo": row["repo"], "piece": row["piece"],
                   "source_hashes": hashes, "digest": snapshot["digest"],
                   "generation": row["gate_generation"], "metadata": fields.get("writing", {}),
                   "gate_records": fields.get("writing_gates", {}),
                   "restore_epoch": _restore_epoch(connection),
                   "staging_title": f"{title} [sd-claim:{claim}]"}
        state = {"phase": "claimed", "pending": None, "document_id": None, "receipts": [],
                 "payload_sha256": _digest(_json(payload))}
        timestamp = now()
        journal.create(connection, claim, item, payload, state)
        connection.execute("INSERT INTO publication_claim VALUES (?,?,?,?,?,?,?)",
                           (claim, item, item, _json(payload), _json(state), timestamp, timestamp))
        add_note(connection, item, "comment", f"Publication claim {claim} prepared for {destination['account']} / Published", session=who)
        return claim_state(connection, item, claim)


def _load(connection, item, claim):
    row = connection.execute("SELECT * FROM publication_claim WHERE id=? AND item=?", (claim, item)).fetchone()
    if row is None:
        raise WorkflowError("publication claim does not belong to this piece")
    return dict(row), json.loads(row["payload"]), json.loads(row["state"])


def claim_state(connection, item, claim, *, include_payload=False):
    row, payload, state = _load(connection, item, claim)
    return {"claim": claim, "item": item, "active": row["active_item"] is not None,
            "title": payload["title"], "destination": payload["destination"],
            "images": payload["inventory"]["images"], "digest": payload["digest"],
            **state, **({"payload": payload} if include_payload else {})}


def recover_journal(connection, item, *, repair_incomplete=False, who):
    """Recover lost database claim state; native readback still settles publication."""
    with transaction(connection):
        _restored(connection)
        repair = journal.repair_incomplete(connection) if repair_incomplete else {"archived": []}
        row = writing._piece(connection, item)["item"]
        recovered = []
        for manifest, recorded in journal.for_piece(connection, row["repo"], row["piece"]):
            claim, payload = manifest["claim"], manifest["payload"]
            held = connection.execute("SELECT payload,state FROM publication_claim WHERE id=?", (claim,)).fetchone()
            if held is not None and json.loads(held["payload"]) != payload:
                raise WorkflowError("journal payload conflicts with the restored database; preserve both for recovery")
            if held is not None and json.loads(held["state"]) == recorded:
                continue
            state = dict(recorded)
            if state["phase"] == "published":
                state.update({"phase": "moving", "pending": None, "recovered_publication": True})
            active = None if state["phase"] in TERMINAL else item
            existing = connection.execute("SELECT id FROM publication_claim WHERE active_item=? AND id<>?", (item, claim)).fetchone()
            if active and existing:
                raise WorkflowError("multiple journal claims need reconciliation; an existing active claim must be settled first")
            timestamp = now()
            if held is None:
                connection.execute("INSERT INTO publication_claim VALUES (?,?,?,?,?,?,?)",
                                   (claim, item, active, _json(payload), _json(state), timestamp, timestamp))
            else:
                connection.execute("UPDATE publication_claim SET active_item=?,state=?,updated_at=? WHERE id=?",
                                   (active, _json(state), timestamp, claim))
            journal.append(connection, claim, state)
            recovered.append(claim)
        if recovered or repair["archived"]:
            add_note(connection, item, "comment", "Recovered publication journal: " + _json({"claims": recovered, **repair}), session=who)
        return {"recovered": recovered, **repair, "claims": [claim_state(connection, item, claim) for claim in recovered]}


def _save(connection, claim, state, *, terminal=False):
    journal.append(connection, claim, state)
    connection.execute("UPDATE publication_claim SET state=?,updated_at=?,active_item=CASE WHEN ? THEN NULL ELSE active_item END WHERE id=?",
                       (_json(state), now(), terminal, claim))


def _current(connection, item, payload, *, reconciled_epoch=None):
    _restored(connection)
    if (payload["restore_epoch"] if reconciled_epoch is None else reconciled_epoch) != _restore_epoch(connection):
        raise WorkflowError("publication claim predates a restore; inspect and reconcile the destination first")
    row = writing.preflight(connection, item)["item"]
    snapshot, hashes = _source_files(row, payload.get("assets", {}))
    if hashes != payload["source_hashes"] or snapshot["digest"] != payload["digest"] or row["gate_generation"] != payload["generation"]:
        raise WorkflowError("publication source or evidence changed after the claim; no external write is allowed")
    if _fields(row["fields"]).get("writing", {}) != payload["metadata"]:
        raise WorkflowError("publication metadata changed after the claim")
    rendered = render_payload(connection, item)
    if rendered["html"] != payload["html"] or rendered["tip_source"] != payload.get("tip_source"):
        raise WorkflowError("publication payload or attached tip changed after the claim")


def _reads(payload, state):
    document = state.get("document_id")
    if not document:
        title = payload["staging_title"].replace("\\", "\\\\").replace("'", "\\'")
        return [{"tool": "google_drive_search", "arguments": {"item_type": "document", "special_filter_query_str": f"trashed = false and name = '{title}' and mimeType = '{NATIVE}'"},
                 "note": "Read every page. An empty search does not authorize another import."}]
    return [{"tool": "google_drive_get_file_metadata", "arguments": {"fileId": document}},
            {"tool": "google_drive_get_document", "arguments": {"document_id": document}}]


def dispatch(connection, item, claim, context, *, confirmed=False, who):
    """Dispatch one write once; repeated dispatch returns read-only recovery."""
    destination = _context(context)
    with transaction(connection):
        _, payload, state = _load(connection, item, claim)
        journal.require_synced(connection, claim, payload, state)
        if destination != payload["destination"]:
            raise WorkflowError("Drive account or destination differs from the publication claim")
        if state["phase"] in TERMINAL:
            return claim_state(connection, item, claim)
        if state["pending"] or state["phase"] in {"created", "moving"}:
            return {**claim_state(connection, item, claim), "execute": False, "readback": _reads(payload, state)}
        _current(connection, item, payload, reconciled_epoch=state.get("reconciled_epoch"))
        if not confirmed:
            raise WorkflowError("confirm this concrete piece and destination before dispatching its external write")
        if state["phase"] == "claimed":
            action = {"tool": "google_drive_import_document", "arguments": {
                "source_file": "CLAIM_HTML_PATH", "title": payload["staging_title"], "upload_mode": "native_google_docs"}}
            state["phase"] = "creating"
        elif state["phase"] == "verified":
            action = {"tool": "google_drive_update_file", "arguments": {
                "fileId": state["document_id"], "name": payload["title"],
                "addParents": destination["published"], "removeParents": ",".join(state["parent_ids"])}}
            state["phase"] = "moving"
        else:
            raise WorkflowError("publication requires destination reconciliation before another write")
        operation = uuid.uuid4().hex
        state["pending"] = {"id": operation, "tool": action["tool"], "at": now(), "who": who}
        _save(connection, claim, state)
        return {**claim_state(connection, item, claim), "execute": True, "operation": operation, "action": action}


def receipt(connection, item, claim, operation, response):
    """Record only the matching response; success never substitutes for readback."""
    response = _result(response)
    response_hash = _digest(_json(response))
    with transaction(connection):
        _, payload, state = _load(connection, item, claim)
        journal.require_synced(connection, claim, payload, state)
        previous = next((r for r in state["receipts"] if r["operation"] == operation), None)
        if previous:
            if previous["sha256"] != response_hash:
                raise WorkflowError("this operation already has a different receipt")
            return claim_state(connection, item, claim)
        pending = state["pending"]
        if not pending or pending["id"] != operation:
            raise WorkflowError("receipt does not match the pending publication operation")
        if pending["tool"] == "google_drive_import_document":
            document = response.get("documentId") or response.get("fileId")
            if response.get("success") is not True or response.get("converted") is not True or response.get("mimeType") != NATIVE or not isinstance(document, str) or not document:
                raise WorkflowError("import did not prove a native Google Doc; reconcile the uncertain result")
            state["document_id"] = document
            state["phase"] = "created"
        elif response.get("success") is not True or response.get("id") != state["document_id"]:
            raise WorkflowError("move response does not confirm the claimed document; reconcile")
        state["receipts"].append({"operation": operation, "sha256": response_hash, "at": now()})
        state["pending"] = None
        _save(connection, claim, state)
        return {**claim_state(connection, item, claim), "readback": _reads(payload, state)}


def _document_inventory(document):
    texts, image_refs, objects, links, image_positions = [], [], {}, [], []
    tabs = document.get("tabs")
    if tabs is not None:
        if not isinstance(tabs, list) or len(tabs) != 1:
            raise WorkflowError("publication readback requires the complete single-tab imported document")
        if tabs[0].get("childTabs"):
            raise WorkflowError("unexpected additional document tabs")
        content = tabs[0].get("documentTab", tabs[0])
    else:
        content = document
    body = content.get("body")
    if not isinstance(body, dict) or "content" not in body:
        raise WorkflowError("native document readback omitted its full body")
    objects.update(content.get("inlineObjects") or {})
    objects.update(content.get("positionedObjects") or {})

    def walk(value):
        if isinstance(value, list):
            for entry in value:
                walk(entry)
        elif isinstance(value, dict):
            if "textRun" in value:
                run = value["textRun"]
                texts.append(run.get("content", ""))
                url = run.get("textStyle", {}).get("link", {}).get("url")
                if url:
                    links.append(url)
            if "inlineObjectElement" in value:
                image_refs.append(value["inlineObjectElement"].get("inlineObjectId"))
                image_positions.append(_digest(_normal("".join(texts))))
            for key in ("content", "paragraph", "elements", "table", "tableRows", "tableCells"):
                if key in value:
                    walk(value[key])
            if "paragraph" in value and value["paragraph"].get("positionedObjectIds"):
                raise WorkflowError("publication images must remain inline so their order is verifiable")
            if "tableCells" in value:
                texts.append(" ")

    walk(body)
    image_aspects = []
    for image_id in image_refs:
        embedded = objects.get(image_id, {}).get("inlineObjectProperties", objects.get(image_id, {}).get("positionedObjectProperties", {})).get("embeddedObject", {})
        if not embedded.get("imageProperties", {}).get("contentUri"):
            raise WorkflowError("native image readback has a missing or unrendered image")
        try:
            dimensions = embedded["size"]
            if dimensions["width"]["unit"] != dimensions["height"]["unit"]:
                raise ValueError
            width, height = float(dimensions["width"]["magnitude"]), float(dimensions["height"]["magnitude"])
            if min(width, height) <= 0:
                raise ValueError
            image_aspects.append(width / height)
        except (KeyError, ValueError, TypeError, ZeroDivisionError):
            raise WorkflowError("native image dimensions are missing or invalid") from None
    return {"text": _normal("".join(texts)), "images": len(image_refs), "links": sorted(set(links)),
            "image_positions": image_positions, "image_aspects": image_aspects}


def reconcile(connection, item, claim, context, evidence, *, who):
    """Adopt a unique lost import, or prove the native content and final folder."""
    destination = _context(context)
    with transaction(connection):
        _, payload, state = _load(connection, item, claim)
        journal.require_synced(connection, claim, payload, state)
        if destination != payload["destination"]:
            raise WorkflowError("reconciliation account or folders differ from the claim")
        if state["phase"] == "published":
            return claim_state(connection, item, claim)
        if state["phase"] == "abandoned":
            if not state.get("reconcile_required"):
                raise WorkflowError("a never-dispatched abandoned claim has no destination to reconcile")
            assert_mutable(connection, item)
            connection.execute("UPDATE publication_claim SET active_item=? WHERE id=?", (item, claim))
            state["phase"] = state["abandoned_phase"]
            state["reconcile_required"] = False
        if not state["document_id"]:
            pages = evidence.get("search_pages")
            if state["phase"] != "creating" or not isinstance(pages, list) or not pages:
                raise WorkflowError("lost import needs every page of exact claim-title search results")
            matches = []
            for index, page in enumerate(pages):
                page = _result(page)
                if not isinstance(page.get("results"), list) or bool(page.get("next_page_token")) != (index < len(pages) - 1):
                    raise WorkflowError("claim search is incomplete; fetch every result page")
                matches.extend(page["results"])
            if len(matches) != 1:
                raise WorkflowError(f"claim import remains uncertain ({len(matches)} candidates); never automatically create again")
            found = matches[0]
            if (found.get("title") or found.get("name")) != payload["staging_title"] or (found.get("mime_type") or found.get("mimeType")) != NATIVE:
                raise WorkflowError("search result is not the exact native claim document")
            document_id = found.get("id") or found.get("fileId")
            if not isinstance(document_id, str) or not document_id:
                raise WorkflowError("search result omitted document identity")
            state["document_id"] = document_id
            state["phase"] = "created"
            state["pending"] = None
            _save(connection, claim, state)
            return {**claim_state(connection, item, claim), "readback": _reads(payload, state)}
        metadata = _result(evidence.get("metadata"))
        document = _result(evidence.get("document"))
        document_id = state["document_id"]
        if metadata.get("id") != document_id or document.get("documentId") != document_id or metadata.get("mime_type") != NATIVE:
            raise WorkflowError("readback is not the claimed native document")
        inventory = _document_inventory(document)
        expected = payload["inventory"]
        if any(inventory[key] != expected[key] for key in ("text", "images", "links", "image_positions")) or any(
                abs(have / want - 1) > 0.03 for have, want in zip(inventory["image_aspects"], expected["image_aspects"], strict=True)):
            raise WorkflowError("native destination text, images, or links differ from the immutable publication payload")
        parents = metadata.get("parent_ids")
        if not isinstance(parents, list) or not parents or not all(isinstance(p, str) and p for p in parents):
            raise WorkflowError("Drive parent identity is missing; cannot prove staging or publication")
        state["verification"] = {"at": now(), "document_id": document_id, "parents": parents,
                                 "metadata_sha256": _digest(_json(metadata)), "document_sha256": _digest(_json(document)),
                                 "text_sha256": _digest(inventory["text"]), "images": inventory["images"],
                                 "image_positions": inventory["image_positions"], "image_aspects": inventory["image_aspects"],
                                 "links": inventory["links"]}
        if state["phase"] == "moving":
            if parents != [destination["published"]]:
                raise WorkflowError("move remains uncertain; destination is not solely the claimed Published folder")
            if metadata.get("title") != payload["title"]:
                raise WorkflowError("published destination title differs from the claim")
            url = metadata.get("url")
            if not isinstance(url, str) or not re.fullmatch(r"https://docs\.google\.com/document/d/" + re.escape(document_id) + r"(?:/[^\s]*)?", url):
                raise WorkflowError("Drive did not return the claimed document's actual URL")
            # The external move may already have happened. Preserve uncertainty
            # when source changed; never falsify current readiness to finish.
            row = writing._piece(connection, item)["item"]
            fields = _fields(row["fields"])
            metadata_fields = fields.setdefault("writing", {})
            metadata_fields.setdefault("published_urls", {})["gdrive"] = url
            metadata_fields.setdefault("publication_url_provenance", {})["gdrive"] = {
                "kind": "verified-claim", "claim": claim, "url": url, "payload_sha256": state["payload_sha256"]}
            timestamp = now()
            try:
                _current(connection, item, payload, reconciled_epoch=_restore_epoch(connection))
                current = True
            except WorkflowError:
                current = False
            if current:
                metadata_fields.update({"published": timestamp[:10], "status": "published", "updated": timestamp[:10]})
            state.update({"phase": "published", "pending": None, "url": url, "verified_at": timestamp,
                          "current_draft_published": current, "reconciled_epoch": _restore_epoch(connection),
                          "reconcile_required": False})
            _save(connection, claim, state, terminal=True)
            if current:
                set_item_fields(connection, item, stage="published", ready_digest=None, shipped_at=timestamp, fields=fields)
                _transition(connection, item, "done", who=who, reason=f"Verified publication claim {claim}")
            else:
                metadata_fields.update({"status": "review", "updated": timestamp[:10]})
                set_item_fields(connection, item, stage="review", ready_digest=None, fields=fields)
                _transition(connection, item, "in_progress", who=who, reason=f"Earlier version published by claim {claim}; current source needs review")
            add_note(connection, item, "comment", f"Published {url}; text, {inventory['images']} images, links and Published folder read back", session=who)
        else:
            if destination["published"] in parents or metadata.get("title") != payload["staging_title"]:
                raise WorkflowError("claim staging identity or folder changed outside this publication")
            _current(connection, item, payload, reconciled_epoch=_restore_epoch(connection))
            state.update({"phase": "verified", "pending": None, "parent_ids": parents, "verified_at": now(),
                          "reconciled_epoch": _restore_epoch(connection)})
            _save(connection, claim, state)
        return claim_state(connection, item, claim)


def abandon(connection, item, claim, *, reason, leave=False, who):
    reason = _text(reason, "abandon reason")
    with transaction(connection):
        _, payload, state = _load(connection, item, claim)
        journal.require_synced(connection, claim, payload, state)
        if state["phase"] == "published":
            raise WorkflowError("a published claim cannot be abandoned")
        if state["phase"] == "abandoned":
            return claim_state(connection, item, claim)
        uncertain = state["phase"] != "claimed"
        if uncertain and not leave:
            raise WorkflowError("an external operation may exist; use --leave with a reason to retain it untouched")
        state.update({"abandoned_phase": state["phase"], "phase": "abandoned", "pending": None, "reason": reason,
                      "reconcile_required": uncertain, "abandoned_at": now()})
        _save(connection, claim, state, terminal=True)
        add_note(connection, item, "comment", f"Abandoned publication claim {claim}; external documents left untouched: {reason}", session=who)
        return claim_state(connection, item, claim)
