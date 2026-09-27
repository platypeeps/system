"""The removal record's marker and actor, through `ingest` as sd:755 left it (sd:754).

sd:755's #347 added `actor` and `record` to `reporting.ingest`, and its tests
pin their validation (`test_report_bulk_acknowledge.IngestTakesAnActorAndARecord`).
These pin only what sd:754 adds on top: the two remove markers are stored as
`fields.record`, and the seven actor keys of the removal record, `reason`
included, are stored as given.
"""
import json
import tempfile
import unittest
from pathlib import Path

import sd_db
from sd_db import reporting
from sd_db.migrate import initialise

FILED = "2026-09-14T00:00:00+00:00"
FINGERPRINT = "a" * 64


class RemovalRecordThroughIngest(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        path = Path(self.tmp.name).resolve() / "sd.db"; initialise(path)
        self.db = sd_db.connect(path); self.addCleanup(self.db.close)

    def ingest(self, job, **kwargs):
        return reporting.ingest(self.db, job=job, run_id=FINGERPRINT, started=FILED, ended=FILED, exit_code=0,
                                text="remove item 72\n", source_path=f"sd-db.sh {job.replace('-', ' ')}",
                                removed={"item": 1}, **kwargs)

    def test_each_remove_marker_is_stored_as_the_top_level_record(self):
        for job in ("item-remove", "repo-remove"):
            with self.subTest(job=job):
                state = self.ingest(job, record=job)
                fields = json.loads(state["item"]["fields"])
                self.assertEqual(fields["record"], job)
                self.assertNotIn("record", fields["report"])
                self.assertEqual(state["item"]["external_id"], f"{job}:{FINGERPRINT}")

    def test_the_seven_actor_keys_are_stored_as_given(self):
        actor = {"who": "alex", "principal": "local", "program": "sd-db.sh item remove", "pid": 4242,
                 "ppid": 1, "session": None, "reason": "runner provisioning probe of 2026-09-09 is finished"}
        state = self.ingest("item-remove", actor=actor, record="item-remove")
        self.assertEqual(json.loads(state["item"]["fields"])["report"]["actor"], actor)


if __name__ == "__main__": unittest.main()
