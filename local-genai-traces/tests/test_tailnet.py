"""`endpoint tailnet`: a satellite's OTLP/HTTP front door on the hub (sd:6945).

The hub's sd stage serves 127.0.0.1:4338 over tailscale serve on 4339. A
satellite exports to https://<hub>:4339, the hub named by its hub.json,
never a tracked file. A bare name takes the tailnet's MagicDNS suffix:
the HTTPS certificate names the full one. `tailscale` is a stub.
"""

import json

from .test_status import StatusCase

TAILSCALE = """#!/bin/sh
[ "$1 $2" = "status --json" ] || exit 1
[ -f "$STUB_DIR/tailscale-down" ] && exit 1
echo '{"MagicDNSSuffix": "example-tailnet.ts.net"}'
"""


class Tailnet(StatusCase):
    def setUp(self):
        super().setUp()
        self.home = self.root / "home"
        (self.home / ".config/sd").mkdir(parents=True)
        self.env["HOME"] = str(self.home)
        stub = self.root / "bin/tailscale"
        stub.write_text(TAILSCALE, encoding="utf-8")
        stub.chmod(0o755)

    def hub(self, name):
        (self.home / ".config/sd/hub.json").write_text(json.dumps({"hub": name, "port": 8769}))

    def lines(self, expect=0):
        done = self.run_verb("endpoint", "tailnet", expect=expect)
        return done, [line for line in done.stdout.splitlines() if line.startswith("OTEL_")]

    def test_a_full_name_is_the_endpoint(self):
        self.hub("hub.example-tailnet.ts.net")
        self.assertEqual(self.lines()[1], [
            "OTEL_EXPORTER_OTLP_ENDPOINT=https://hub.example-tailnet.ts.net:4339",
            "OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf",
        ])

    def test_a_bare_name_takes_the_tailnet_suffix(self):
        self.hub("hub")
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT=https://hub.example-tailnet.ts.net:4339", self.lines()[1])

    def test_the_port_follows_its_variable(self):
        self.hub("hub.example-tailnet.ts.net")
        self.env["GENAI_TRACES_TAILNET_PORT"] = "4400"
        self.assertIn("OTEL_EXPORTER_OTLP_ENDPOINT=https://hub.example-tailnet.ts.net:4400", self.lines()[1])

    def test_no_hub_json_fails_and_prints_no_endpoint(self):
        done, otel = self.lines(expect=1)
        self.assertEqual(otel, [])
        self.assertIn("hub.json", done.stderr)

    def test_a_bare_name_without_tailscale_fails(self):
        self.hub("hub")
        (self.containers / "tailscale-down").write_text("")
        self.assertEqual(self.lines(expect=1)[1], [])
