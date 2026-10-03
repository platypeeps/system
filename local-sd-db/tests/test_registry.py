"""The registry: identity from the file, state from the rows, and the refusals."""

import os
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from sd_db import connect, ensure_seeded, read_registry, seed, set_bill_cap, set_provider_state
from sd_db import provider_controls, registry, writes
from sd_db.errors import RegistryError
from sd_db.migrate import initialise
from sd_db.registry import beside, merge, parse
from sd_db.testing import FixtureHome

#: The registry as `WORKFLOW.md` ships it, with the endpoints kept short.
SHIPPED = """\
bills:
  anthropic: { cost: subscription }
  openai:    { cost: subscription }
  moonshot:  { cost: prepaid }
  minimax:   { cost: plan, meter: "https://minimax.example/remains" }
  baseten:   { cost: company, cap_usd_month: 50 }
  local:     { cost: local }
providers:
  claude:  { start: "claude -p",  vendor: anthropic, bill: anthropic, roles: [author, reviewer], reader: claude-json }
  codex:   { start: "codex exec", vendor: openai,    bill: openai,    roles: [author, reviewer], reader: codex-json }
  kimi:    { url: "https://moonshot.example/v1", model: kimi-k3, vendor: moonshot, bill: moonshot,
             roles: [reviewer], max_tokens: 16384, price: { in: 3.00, out: 15.00 } }
  minimax: { url: "https://minimax.example/v1", model: MiniMax-M3, vendor: minimax, bill: minimax,
             roles: [reviewer], max_tokens: 16384, price: { in: 0, out: 0 } }
  baseten: { url: "https://baseten.example/v1", model: fixture-model, vendor: deepseek,
             bill: baseten, roles: [reviewer], max_tokens: 16384, price: { in: 1.32, out: 3.96 } }
  # Shipped disabled: the local model is not pinned. A disabled entry never
  # resolves.
  exo:     { url: "http://localhost:52415/v1", model: "<pinned>", vendor: local, bill: local,
             roles: [author, reviewer], enabled: false, reason: "model not pinned" }
roles:
  author:   [claude, codex]
  reviewer: [codex, claude, minimax, kimi, baseten, exo]
"""


class TheShippedRegistry(unittest.TestCase):
    def setUp(self):
        self.registry = parse(SHIPPED, "providers.yaml")

    def test_author_and_reviewer_resolve_to_different_providers(self):
        self.assertEqual(self.registry.resolve("author").name, "claude")
        self.assertEqual(self.registry.resolve("reviewer").name, "codex")

    def test_a_disabled_entry_never_resolves(self):
        self.assertNotIn("exo", [p.name for p in self.registry.order("reviewer")])
        self.assertEqual(self.registry.providers["exo"].reason, "model not pinned")

    def test_identity_is_read_whole(self):
        kimi = self.registry.providers["kimi"]
        self.assertEqual(kimi.kind, "url")
        self.assertEqual(kimi.model, "kimi-k3")
        self.assertEqual(kimi.price, {"in": 3.0, "out": 15.0})
        self.assertEqual(self.registry.providers["claude"].kind, "start")
        self.assertEqual(self.registry.providers["claude"].reader, "claude-json")

    def test_a_comment_is_not_content(self):
        self.assertNotIn("Shipped disabled", str(self.registry.providers))


class TheRefusals(unittest.TestCase):
    def refuse(self, text):
        with self.assertRaises(RegistryError) as raised:
            parse(text, "providers.yaml")
        return str(raised.exception)

    def test_a_start_entry_on_a_capped_bill(self):
        text = SHIPPED.replace(
            '  baseten: { url: "https://baseten.example/v1", model: fixture-model, vendor: deepseek,\n'
            "             bill: baseten, roles: [reviewer], max_tokens: 16384, price: { in: 1.32, out: 3.96 } }",
            '  baseten: { start: "baseten run", vendor: deepseek, bill: baseten, roles: [reviewer] }',
        )
        self.assertNotEqual(text, SHIPPED)
        message = self.refuse(text)
        self.assertIn("capped bill", message)
        self.assertIn("nothing enforces", message)

    def test_author_and_reviewer_resolving_to_the_same_provider(self):
        text = SHIPPED.replace(
            "reviewer: [codex, claude,", "reviewer: [claude, codex,"
        )
        self.assertNotEqual(text, SHIPPED)
        message = self.refuse(text)
        self.assertIn("first in both", message)
        self.assertIn("review would be by the author", message)

    def test_a_provider_billed_to_a_bill_nobody_declared(self):
        message = self.refuse(SHIPPED.replace("bill: moonshot,", "bill: lunar,"))
        self.assertIn("lunar", message)

    def test_a_role_list_naming_an_unknown_provider(self):
        message = self.refuse(SHIPPED.replace("author:   [claude, codex]", "author:   [claude, ghost]"))
        self.assertIn("ghost", message)

    def test_an_entry_with_both_a_start_and_a_url(self):
        message = self.refuse(
            SHIPPED.replace('claude:  { start: "claude -p",', 'claude:  { start: "claude -p", url: "http://x/v1",')
        )
        self.assertIn("both", message)

    def test_an_entry_with_neither(self):
        message = self.refuse(SHIPPED.replace('start: "codex exec", ', ""))
        self.assertIn("neither", message)

    def test_a_section_that_is_not_a_mapping(self):
        """#438's verification round: a section present but a list or a
        scalar reached `.items()` and raised `AttributeError`, not the one
        refusal every caller catches. `bills: [not, a, mapping]` with the
        other two sections present is the shape that escaped."""
        for section, value in (("bills", "[not, a, mapping]"), ("providers", "3"), ("roles", "[author]")):
            with self.subTest(section=section):
                text = SHIPPED.replace(f"{section}:\n", f"{section}: {value}\n_{section}:\n", 1)
                self.assertNotEqual(text, SHIPPED)
                message = self.refuse(text)
                self.assertIn(f"{section!r} is not a mapping", message)

    def test_a_tab_anywhere_is_refused(self):
        """The parser promises to refuse tabs. The old check expanded the
        tabs away before looking for one, so it never fired and a tab in an
        indent or a value parsed as a space (sd:1219)."""
        for old, new in (("  claude:  {", "\tclaude:  {"),
                         ('start: "claude -p",', 'start:\t"claude -p",')):
            with self.subTest(new=new):
                text = SHIPPED.replace(old, new, 1)
                self.assertNotEqual(text, SHIPPED)
                self.assertIn("tab in indentation or content", self.refuse(text))

    def test_invalid_reasoning_controls_refuse(self):
        for field in ("thinking: true", "thinking: enabled", "reasoning_effort: []",
                      "reasoning_effort: medium", "thinking: disabled, reasoning_effort: none"):
            with self.subTest(field=field):
                self.refuse(SHIPPED.replace("model: MiniMax-M3,", f"model: MiniMax-M3, {field},"))
        self.refuse(SHIPPED.replace('start: "claude -p",', 'start: "claude -p", thinking: disabled,'))


class TheNewProvider(unittest.TestCase):
    """Criterion 3's second half: no code changes to add a `url` entry."""

    def test_a_second_local_url_entry_resolves_with_no_code_change(self):
        text = SHIPPED.replace(
            "roles:\n  author:",
            '  herdr:   { url: "http://localhost:8099/v1", model: herdr-1, vendor: local,\n'
            "             bill: local, roles: [reviewer] }\n"
            "roles:\n  author:",
        ).replace("reviewer: [codex,", "reviewer: [herdr, codex,")
        registry = parse(text, "providers.yaml")
        self.assertEqual(registry.resolve("reviewer").name, "herdr")
        self.assertEqual(registry.providers["herdr"].url, "http://localhost:8099/v1")

    def test_enabling_exo_resolves_it_the_same_way(self):
        text = SHIPPED.replace('enabled: false, reason: "model not pinned"', "enabled: true")
        registry = parse(text, "providers.yaml")
        self.assertIn("exo", [p.name for p in registry.order("reviewer")])


class TheRowsOverTheFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        state = self.home / ".local/share/sd"
        state.mkdir(parents=True)
        (state / "providers.yaml").write_text(SHIPPED, encoding="utf-8")
        initialise(home=self.home)
        self.connection = connect(home=self.home)
        self.addCleanup(self.connection.close)
        seed(self.connection, parse(SHIPPED, "providers.yaml"))

    def test_seeding_twice_writes_nothing_the_second_time(self):
        again = seed(self.connection, parse(SHIPPED, "providers.yaml"))
        self.assertEqual(again, 0)
        count = self.connection.execute("SELECT count(*) FROM provider").fetchone()[0]
        self.assertEqual(count, 6)

    def test_a_row_disables_a_provider_the_file_still_lists(self):
        set_provider_state(self.connection, "minimax", enabled=False, reason="paused")
        registry = read_registry(home=self.home, connection=self.connection)
        self.assertNotIn("minimax", [p.name for p in registry.order("reviewer")])
        self.assertEqual(registry.providers["minimax"].reason, "paused")
        self.assertEqual(registry.resolve("reviewer").name, "codex")

    def test_disabling_the_first_reviewer_is_refused_when_the_author_is_next(self):
        """Not a rule about files. `codex` disabled leaves `claude` first in
        both lists, which is a review by the author however it came about."""
        set_provider_state(self.connection, "codex", enabled=False, reason="paused")
        with self.assertRaises(RegistryError) as raised:
            read_registry(home=self.home, connection=self.connection)
        self.assertIn("'claude' is first in both", str(raised.exception))

    def test_a_row_reorders_a_role_without_touching_the_file(self):
        set_provider_state(self.connection, "kimi", reviewer_rank=-1)
        registry = read_registry(home=self.home, connection=self.connection)
        self.assertEqual(registry.resolve("reviewer").name, "kimi")
        self.assertIn("codex, claude", (self.home / ".local/share/sd/providers.yaml").read_text())

    def test_legacy_null_rank_still_inherits_yaml(self):
        self.connection.execute("UPDATE provider SET reviewer_rank=NULL WHERE name='minimax'")
        current = read_registry(home=self.home, connection=self.connection)
        self.assertIn("minimax", [entry.name for entry in current.order("reviewer")])
        self.assertIsNone(registry.order_policy(self.connection))

    def explicit_orders(self):
        state = provider_controls.snapshot(self.connection)
        return provider_controls.configure(self.connection,
            enabled={entry["name"]: entry["enabled"] for entry in state["providers"]},
            orders={**state["orders"], "reviewer": ["codex", "claude"]},
            expected_revision=state["revision"], who="operator")

    def test_six_provider_membership_migration_preserves_all_other_state(self):
        set_provider_state(self.connection, "kimi", enabled=False, reason="Operator disabled Kimi")
        set_bill_cap(self.connection, "baseten", 125.0)
        before = read_registry(home=self.home, connection=self.connection)
        raw_before = [tuple(row) for row in self.connection.execute("SELECT name, enabled, reason FROM provider ORDER BY name")]
        state = self.explicit_orders()
        after = read_registry(home=self.home, connection=self.connection)
        self.assertEqual(len(after.providers), 6)
        self.assertEqual(state["orders"], {"author": ["claude", "codex"], "reviewer": ["codex", "claude"]})
        self.assertEqual(after.bills, before.bills)
        self.assertEqual([tuple(row) for row in self.connection.execute("SELECT name, enabled, reason FROM provider ORDER BY name")], raw_before)
        for name, provider in before.providers.items():
            self.assertEqual({**after.providers[name].__dict__, "ranks": provider.ranks}, provider.__dict__)
        self.assertTrue(after.providers["minimax"].enabled)
        self.assertTrue(after.providers["baseten"].enabled)
        self.assertEqual((self.home / ".local/share/sd/providers.yaml").read_text(), SHIPPED)

    def test_explicit_exclusions_survive_yaml_ranks_and_new_provider_seeding(self):
        self.explicit_orders()
        changed = SHIPPED.replace("reviewer: [codex, claude, minimax, kimi, baseten, exo]",
            "reviewer: [minimax, baseten, codex, claude, kimi, exo, additional]")
        changed = changed.replace("roles:\n  author:",
            '  additional: {url: "https://example.invalid/v1", model: fixture, vendor: new, bill: local, roles: [reviewer]}\nroles:\n  author:')
        (self.home / ".local/share/sd/providers.yaml").write_text(changed)
        current = read_registry(home=self.home, connection=self.connection)
        self.assertEqual([entry.name for entry in current.order("reviewer")], ["codex", "claude", "additional"])
        with closing(connect(home=self.home, write=False)) as readonly:
            observed = read_registry(connection=readonly)
            self.assertEqual(observed.providers, current.providers)
            self.assertEqual(observed.bills, current.bills)
            self.assertEqual(observed.path.resolve(), current.path.resolve())

    def test_malformed_provider_membership_refuses_without_restoring_yaml_ranks(self):
        self.explicit_orders()
        valid = registry.order_policy(self.connection)
        for invalid in ("not json", None, [], {}, {**valid, "schema_version": True},
                '{"schema_version": 1, "explicit_providers": ["minimax"], "explicit_providers": ["codex"]}',
                {**valid, "schema_version": 2}, {**valid, "extra": 1},
                {**valid, "explicit_providers": []}, {**valid, "explicit_providers": ["codex", "codex"]},
                {**valid, "explicit_providers": [None]}, {**valid, "explicit_providers": [""]}):
            with self.subTest(invalid=invalid):
                writes.record_state(self.connection, "checkpoint", key=registry.ORDER_POLICY_KEY, body=invalid)
                before = tuple(self.connection.iterdump())
                with self.assertRaisesRegex(RegistryError, "unreadable provider order policy"):
                    read_registry(connection=self.connection)
                with closing(connect(home=self.home, write=False)) as readonly:
                    with self.assertRaisesRegex(RegistryError, "unreadable provider order policy"):
                        read_registry(connection=readonly)
                self.assertEqual(tuple(self.connection.iterdump()), before)

    def test_future_policy_key_leaves_v1_exclusions_unchanged(self):
        self.explicit_orders()
        before = read_registry(connection=self.connection)
        policy = registry.order_policy(self.connection)
        writes.record_state(self.connection, "checkpoint", key="provider-orders:v2",
            body={"schema_version": 2, "future_policy": "unrecognized"})
        for read_only in (False, True):
            with self.subTest(read_only=read_only), closing(connect(home=self.home, write=not read_only)) as reader:
                observed = read_registry(connection=reader)
                self.assertEqual(registry.order_policy(reader), policy)
                self.assertEqual(observed.providers, before.providers)
                self.assertEqual(observed.bills, before.bills)
                self.assertEqual([entry.name for entry in observed.order("reviewer")], ["codex", "claude"])

    def test_missing_explicit_provider_row_refuses_before_reseeding(self):
        self.explicit_orders()
        self.connection.execute("DELETE FROM provider WHERE name='minimax'")
        before = tuple(self.connection.iterdump())
        with self.assertRaisesRegex(RegistryError, "explicit provider rows are missing: minimax"):
            read_registry(connection=self.connection)
        self.assertEqual(tuple(self.connection.iterdump()), before)

    def interleaved_membership(self, *, read_only, outer_transaction=False):
        # Legacy row order differs from YAML, so a hybrid cannot resemble the old view.
        set_provider_state(self.connection, "baseten", reviewer_rank=-1)
        source = parse(SHIPPED, self.home / ".local/share/sd/providers.yaml")
        before = merge(source, self.connection)
        current = provider_controls.snapshot(self.connection)
        reader = connect(home=self.home, write=not read_only)
        self.addCleanup(reader.close)
        if outer_transaction:
            reader.execute("BEGIN")

        class InterleavedRead:
            switched = False

            @property
            def in_transaction(proxy):
                return reader.in_transaction

            def execute(proxy, sql, parameters=()):
                if sql.startswith("SELECT name, enabled, reason, author_rank, reviewer_rank") and not proxy.switched:
                    proxy.switched = True
                    provider_controls.configure(self.connection,
                        enabled={entry["name"]: entry["enabled"] for entry in current["providers"]},
                        orders={**current["orders"], "reviewer": ["codex", "claude"]},
                        expected_revision=current["revision"], who="concurrent operator")
                return reader.execute(sql, parameters)

        proxy = InterleavedRead()
        observed = merge(source, proxy)
        self.assertTrue(proxy.switched)
        self.assertEqual(reader.in_transaction, outer_transaction)
        # The first policy read precedes the writer; all remaining rows must match it.
        self.assertEqual(observed.providers, before.providers)
        if outer_transaction:
            reader.execute("ROLLBACK")
        after = merge(source, reader)
        self.assertEqual([entry.name for entry in after.order("reviewer")], ["codex", "claude"])

    def test_merge_holds_one_snapshot_for_read_only_connection(self):
        self.interleaved_membership(read_only=True)

    def test_merge_holds_one_snapshot_for_writable_connection(self):
        self.interleaved_membership(read_only=False)

    def test_merge_preserves_callers_existing_snapshot(self):
        self.interleaved_membership(read_only=True, outer_transaction=True)

    def test_merge_does_not_commit_pending_caller_writes(self):
        source = parse(SHIPPED)
        self.connection.execute("BEGIN")
        self.connection.execute("UPDATE provider SET reason='Pending caller change' WHERE name='minimax'")
        observed = merge(source, self.connection)
        self.assertEqual(observed.providers["minimax"].reason, "Pending caller change")
        self.assertTrue(self.connection.in_transaction)
        with closing(connect(home=self.home, write=False)) as reader:
            self.assertIsNone(merge(source, reader).providers["minimax"].reason)
        self.connection.execute("ROLLBACK")
        self.assertIsNone(merge(source, self.connection).providers["minimax"].reason)

    def test_merge_errors_end_only_owned_read_transactions(self):
        source = parse(SHIPPED)
        for read_only in (True, False):
            for outer in (True, False):
                with self.subTest(read_only=read_only, outer=outer):
                    with closing(connect(home=self.home, write=not read_only)) as reader:
                        if outer:
                            reader.execute("BEGIN")
                        with patch.object(registry, "order_policy", side_effect=RegistryError("synthetic read failure")):
                            with self.assertRaisesRegex(RegistryError, "synthetic read failure"):
                                merge(source, reader)
                        self.assertEqual(reader.in_transaction, outer)
                        if outer:
                            reader.execute("ROLLBACK")

    def test_a_row_carries_the_cap_the_dashboard_changed(self):
        set_bill_cap(self.connection, "baseten", 125.0)
        registry = read_registry(home=self.home, connection=self.connection)
        self.assertEqual(registry.bills["baseten"].cap_usd_month, 125.0)

    def test_a_row_cap_on_a_bill_a_start_entry_holds_is_dropped_and_reported_by_the_reader(self):
        """A cap a bare `UPDATE` put on a `start` entry's bill is a number
        nothing enforces (clause 15.15). The reader does not refuse the
        rows the way `parse` refuses the file -- that would fail every read
        and the repair with it -- it reads the bill without the cap and
        says so in `warnings`, in the file's sentence."""
        self.connection.execute("UPDATE bill SET cap_usd_month = 5.0 WHERE name = 'anthropic'")
        registry = read_registry(home=self.home, connection=self.connection)
        self.assertIsNone(registry.bills["anthropic"].cap_usd_month)
        self.assertEqual(len(registry.warnings), 1)
        self.assertIn("provider 'claude' is a 'start' entry on the capped bill 'anthropic'", registry.warnings[0])
        self.assertIn("nothing enforces", registry.warnings[0])
        merged = merge(parse(SHIPPED, "providers.yaml"), self.connection)
        self.assertEqual(len(merged.warnings), 1)
        self.assertIsNone(merged.bills["anthropic"].cap_usd_month)
        # Two `start` entries on the bill: one warning each, by provider name,
        # so the screen can name every entry the cap would not reach.
        two_starts = SHIPPED.replace("vendor: openai,    bill: openai,", "vendor: openai,    bill: anthropic,")
        merged = merge(parse(two_starts, "providers.yaml"), self.connection)
        self.assertEqual(len(merged.warnings), 2)
        self.assertIn("provider 'claude' is a 'start' entry on the capped bill 'anthropic'", merged.warnings[0])
        self.assertIn("provider 'codex' is a 'start' entry on the capped bill 'anthropic'", merged.warnings[1])
        self.assertIsNone(merged.bills["anthropic"].cap_usd_month)
        self.assertEqual(read_registry(home=self.home).warnings, ())
        set_bill_cap(self.connection, "baseten", 125.0)
        self.assertEqual(read_registry(home=self.home, connection=self.connection).bills["baseten"].cap_usd_month, 125.0)

    def test_the_merge_refuses_what_the_file_would_have(self):
        """Rows can create the collision the file was refused for."""
        set_provider_state(self.connection, "claude", reviewer_rank=-5)
        with self.assertRaises(RegistryError) as raised:
            read_registry(home=self.home, connection=self.connection)
        self.assertIn("first in both", str(raised.exception))

    def test_reading_without_a_connection_is_the_file_alone(self):
        set_provider_state(self.connection, "minimax", enabled=False)
        registry = read_registry(home=self.home)
        self.assertTrue(registry.providers["minimax"].enabled)
        merged = merge(registry, self.connection)
        self.assertFalse(merged.providers["minimax"].enabled)

    def test_reasoning_controls_survive_operational_overrides(self):
        text = SHIPPED.replace("model: MiniMax-M3,", "model: MiniMax-M3, thinking: disabled,").replace(
            "model: fixture-model,", "model: fixture-model, reasoning_effort: none,")
        (self.home / ".local/share/sd/providers.yaml").write_text(text)
        set_provider_state(self.connection, "minimax", enabled=False, reason="paused")
        set_provider_state(self.connection, "baseten", reviewer_rank=-1)
        registry = read_registry(home=self.home, connection=self.connection)
        self.assertEqual(registry.providers["minimax"].thinking, "disabled")
        self.assertFalse(registry.providers["minimax"].enabled)
        self.assertEqual(registry.providers["baseten"].reasoning_effort, "none")
        self.assertEqual(registry.resolve("reviewer").name, "baseten")


class TheFirstOpen(unittest.TestCase):
    """The prd's promise: on first open the file seeds the tables.

    `init` used to say "provider and bill will seed on first read" while no
    read seeded anything, so a machine whose file arrived after `init` kept
    an empty `provider` table until the dashboard's first toggle. These are
    the shapes of a read against an unseeded table: writable, writable
    again, read-only, after the dashboard has already written a row, and
    after the file has grown an entry the rows do not have.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        state = self.home / ".local/share/sd"
        state.mkdir(parents=True)
        (state / "providers.yaml").write_text(SHIPPED, encoding="utf-8")
        initialise(home=self.home)

    def counts(self, connection):
        return tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("provider", "bill")
        )

    def rows(self, connection):
        return [
            tuple(row)
            for row in connection.execute(
                "SELECT name, enabled, reason, author_rank, reviewer_rank "
                "FROM provider ORDER BY name"
            )
        ]

    def test_a_writable_read_of_an_unseeded_table_seeds_it_from_the_file(self):
        writer = connect(home=self.home)
        self.addCleanup(writer.close)
        self.assertEqual(self.counts(writer), (0, 0))
        registry = read_registry(home=self.home, connection=writer)
        self.assertEqual(self.counts(writer), (6, 6))
        self.assertFalse(writer.in_transaction, "the seed left a transaction open")
        by_name = {row[0]: row for row in self.rows(writer)}
        # `exo` declares both roles and is listed under one: a declared role
        # with no list position is NULL, and the file's position otherwise.
        self.assertEqual(by_name["exo"], ("exo", 0, "model not pinned", None, 5))
        self.assertEqual(by_name["claude"], ("claude", 1, None, 0, 1))
        self.assertEqual(by_name["kimi"], ("kimi", 1, None, None, 3))
        # And the caller's answer is the one the file gives.
        self.assertEqual(registry.resolve("reviewer").name, "codex")
        self.assertFalse(registry.providers["exo"].enabled)

    def test_a_second_writable_read_writes_nothing(self):
        writer = connect(home=self.home)
        self.addCleanup(writer.close)
        read_registry(home=self.home, connection=writer)
        before = (self.rows(writer), writer.total_changes)
        read_registry(home=self.home, connection=writer)
        self.assertEqual(ensure_seeded(writer, parse(SHIPPED, "providers.yaml")), 0)
        self.assertEqual((self.rows(writer), writer.total_changes), before)
        self.assertEqual(self.counts(writer), (6, 6))

    def test_a_read_only_connection_gets_the_file_view_and_seeds_nothing(self):
        reader = connect(home=self.home, write=False)
        self.addCleanup(reader.close)
        registry = read_registry(home=self.home, connection=reader)
        self.assertEqual(self.counts(reader), (0, 0))
        self.assertEqual(registry.resolve("author").name, "claude")
        self.assertEqual(registry.resolve("reviewer").name, "codex")
        self.assertFalse(registry.providers["exo"].enabled)
        self.assertEqual(ensure_seeded(reader, parse(SHIPPED, "providers.yaml")), 0)
        # The next writable open is the first open that seeds, and the
        # reader sees the rows it wrote.
        writer = connect(home=self.home)
        self.addCleanup(writer.close)
        read_registry(home=self.home, connection=writer)
        self.assertEqual(self.counts(writer), (6, 6))
        self.assertEqual(self.counts(reader), (6, 6))

    def test_a_row_the_dashboard_toggled_survives_a_later_read(self):
        writer = connect(home=self.home)
        self.addCleanup(writer.close)
        # `path` explicitly here; `TheDefaultRegistry` covers the omitted
        # form, which resolves beside the connection's database.
        path = self.home / ".local/share/sd/providers.yaml"
        current = provider_controls.snapshot(writer, path=path)
        enabled = {entry["name"]: entry["enabled"] for entry in current["providers"]}
        enabled["minimax"] = False
        provider_controls.configure(
            writer, enabled=enabled, orders=current["orders"],
            expected_revision=current["revision"], path=path, who="fixture",
        )
        toggled = self.rows(writer)
        self.assertEqual(self.counts(writer), (6, 6))
        registry = read_registry(home=self.home, connection=writer)
        self.assertEqual(self.rows(writer), toggled)
        self.assertFalse(registry.providers["minimax"].enabled)
        self.assertEqual(registry.providers["minimax"].reason, "Disabled by fixture")
        self.assertNotIn("minimax", [p.name for p in registry.order("reviewer")])

    def test_an_entry_added_to_the_file_later_gets_a_row_and_the_rest_keep_theirs(self):
        writer = connect(home=self.home)
        self.addCleanup(writer.close)
        read_registry(home=self.home, connection=writer)
        set_provider_state(writer, "kimi", enabled=False, reason="paused")
        text = SHIPPED.replace(
            "roles:\n  author:",
            '  herdr:   { url: "http://localhost:8099/v1", model: herdr-1, vendor: local,\n'
            "             bill: local, roles: [reviewer] }\n"
            "roles:\n  author:",
        ).replace("baseten, exo]", "baseten, exo, herdr]")
        self.assertNotEqual(text, SHIPPED)
        (self.home / ".local/share/sd/providers.yaml").write_text(text, encoding="utf-8")
        registry = read_registry(home=self.home, connection=writer)
        self.assertEqual(self.counts(writer), (7, 6))
        by_name = {row[0]: row for row in self.rows(writer)}
        self.assertEqual(by_name["herdr"], ("herdr", 1, None, None, 6))
        self.assertEqual(by_name["kimi"], ("kimi", 0, "paused", None, 3))
        self.assertFalse(registry.providers["kimi"].enabled)


#: What `$HOME` holds while the tests below run: a registry with one provider
#: the fixture's file does not name. A read that resolves through the
#: process's home returns it, and that is the failure.
DECOY = """\
bills:
  decoy: { cost: local }
providers:
  decoy:   { url: "http://localhost:1/v1", model: decoy, vendor: local, bill: decoy,
             roles: [author, reviewer] }
  decoy-2: { url: "http://localhost:2/v1", model: decoy, vendor: local, bill: decoy,
             roles: [author, reviewer] }
roles:
  author:   [decoy, decoy-2]
  reviewer: [decoy-2, decoy]
"""


class TheDefaultRegistry(unittest.TestCase):
    """A call that omits `path=` never reaches the process's `$HOME`.

    `snapshot()` and `configure()` used to default to `registry_path()`, which
    is `$HOME/.local/share/sd/providers.yaml`. A test that forgot `path=`
    read the developer's real registry, passed locally, and failed under CI's
    isolated home (#286, #288, 2026-09-12). Now the default is the file beside
    the connection's database: the fixture's, or a refusal naming the
    fixture's path. Both are proved against a decoy `$HOME` whose registry
    would parse fine and name a provider the fixture does not have.
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.home = FixtureHome(root / "fixture")
        initialise(home=self.home.path)
        self.decoy = root / "decoy"
        (self.decoy / ".local/share/sd").mkdir(parents=True)
        (self.decoy / ".local/share/sd/providers.yaml").write_text(DECOY, encoding="utf-8")
        patcher = patch.dict(os.environ, {"HOME": str(self.decoy)})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.writer = connect(home=self.home.path)
        self.addCleanup(self.writer.close)

    def test_the_default_is_the_registry_beside_the_connections_database(self):
        # `connect` opens the resolved path, so the registry beside it is resolved too.
        self.assertEqual(beside(self.writer), self.home.registry.resolve())
        self.assertEqual(read_registry(home=self.decoy).providers.keys(), {"decoy", "decoy-2"},
                         "the decoy must be a registry a read through $HOME would accept")

    def test_a_snapshot_without_path_reads_the_fixture_and_not_the_home(self):
        self.home.registry.write_text(SHIPPED, encoding="utf-8")
        current = provider_controls.snapshot(self.writer)
        names = {entry["name"] for entry in current["providers"]}
        self.assertNotIn("decoy", names)
        self.assertEqual(names, {"claude", "codex", "kimi", "minimax", "baseten", "exo"})
        enabled = {entry["name"]: entry["enabled"] for entry in current["providers"]}
        enabled["kimi"] = False
        after = provider_controls.configure(
            self.writer, enabled=enabled, orders=current["orders"],
            expected_revision=current["revision"], who="fixture",
        )
        self.assertFalse(next(e["enabled"] for e in after["providers"] if e["name"] == "kimi"))
        self.assertEqual(self.writer.execute("SELECT count(*) FROM provider").fetchone()[0], 6)
        self.assertEqual(
            (self.decoy / ".local/share/sd/providers.yaml").read_text(encoding="utf-8"), DECOY
        )

    def test_a_missing_fixture_registry_is_a_refusal_naming_it_not_a_read_elsewhere(self):
        self.assertFalse(self.home.registry.exists())
        with self.assertRaises(RegistryError) as caught:
            provider_controls.snapshot(self.writer)
        self.assertIn(str(self.home.registry.resolve()), str(caught.exception))
        self.assertNotIn(str(self.decoy), str(caught.exception))
        with self.assertRaises(RegistryError):
            read_registry(connection=self.writer)
        self.assertEqual(self.writer.execute("SELECT count(*) FROM provider").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
