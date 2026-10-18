"""The llm wrapper: with llmInaccurateResponse on, each product gets one of a
few wrong answers, picked by the product id's SHA-256, so runs repeat (sd:5029).

The wrapper is imported against a stub upstream `app` module in a temp
folder; no container starts and no cluster is reached.
"""

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tests.test_config import CONFIG_VARS, STUB

FOLDER = Path(__file__).resolve().parent.parent
WRAPPER = FOLDER / "llm_varied_wrong.py"

#: The upstream module's surface the wrapper uses, as app 2.2.0 has it.
UPSTREAM = """\
class _Logger:
    def info(self, *a): pass

class _App:
    logger = _Logger()
    def run(self, **kw): raise AssertionError("the stub server never runs")

app = _App()
api = object()
FlagdProvider = object
product_review_summaries_file_path = "./product-review-summaries.json"
inaccurate_product_review_summaries_file_path = "./inaccurate.json"
FLAG = {"on": False}

def check_feature_flag(name):
    return FLAG["on"] if name == "llmInaccurateResponse" else False

def load_product_review_summaries(path):
    return {}

def generate_response(product_id):
    return "the true summary of " + product_id
"""

PRODUCTS = ["PRODUCT0%d" % n for n in range(10)]


def load_wrapper():
    spec = importlib.util.spec_from_file_location("llm_varied_wrong", WRAPPER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class Wrapper(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.upstream_dir = Path(tmp.name)
        self.saved_path = list(sys.path)
        self.addCleanup(self.restore)
        sys.modules.pop("app", None)

    def restore(self):
        sys.path[:] = self.saved_path
        sys.modules.pop("app", None)

    def stub(self, text=UPSTREAM):
        (self.upstream_dir / "app.py").write_text(text, encoding="utf-8")
        wrapper = load_wrapper()
        return wrapper, wrapper.load_upstream(str(self.upstream_dir))

    def test_the_flag_on_gives_each_product_its_hashed_wrong_answer(self):
        wrapper, upstream = self.stub()
        wrapper.install(upstream)
        upstream.FLAG["on"] = True
        got = {pid: upstream.generate_response(pid) for pid in PRODUCTS}
        for pid, answer in got.items():
            index = int.from_bytes(hashlib.sha256(pid.encode()).digest()[:8], "big")
            self.assertEqual(answer, wrapper.WRONG[index % len(wrapper.WRONG)])
            self.assertEqual(upstream.generate_response(pid), answer)
        self.assertGreaterEqual(len(set(got.values())), 3, got)
        self.assertTrue(3 <= len(wrapper.WRONG) <= 5)

    def test_the_flag_off_keeps_the_upstream_answer(self):
        wrapper, upstream = self.stub()
        wrapper.install(upstream)
        self.assertEqual(upstream.generate_response("PRODUCT01"),
                         "the true summary of PRODUCT01")

    def test_an_upstream_without_what_the_wrapper_patches_fails_at_start(self):
        (self.upstream_dir / "app.py").write_text(
            UPSTREAM.replace("def generate_response", "def renamed_response"),
            encoding="utf-8")
        wrapper = load_wrapper()
        with self.assertRaises(SystemExit) as raised:
            wrapper.load_upstream(str(self.upstream_dir))
        self.assertIn("generate_response", str(raised.exception))

    def test_an_upstream_that_does_not_import_fails_at_start(self):
        (self.upstream_dir / "app.py").write_text("import no_such_module_here\n",
                                                  encoding="utf-8")
        wrapper = load_wrapper()
        with self.assertRaises(SystemExit) as raised:
            wrapper.load_upstream(str(self.upstream_dir))
        self.assertIn("no_such_module_here", str(raised.exception))


class Values(unittest.TestCase):
    def test_the_values_mount_the_wrapper_and_run_it(self):
        values = (FOLDER / "collector-values.yaml").read_text(encoding="utf-8")
        block = values.split("\n  llm:\n", 1)[1].split("\n\n", 1)[0]
        self.assertIn("existingConfigMap: llm-varied-wrong", block)
        self.assertIn("mountPath: /app/varied", block)
        self.assertIn('command: ["/venv/bin/python", "/app/varied/llm_varied_wrong.py"]',
                      block)

    def test_upgrade_ships_the_wrapper_before_helm_and_rolls_llm_on_a_change(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        folder = root / "local-opentelemetry-demo"
        folder.mkdir()
        for name in ("opentelemetry-demo.sh", "collector-values.yaml", WRAPPER.name):
            shutil.copy(FOLDER / name, folder / name)
        shutil.copytree(FOLDER.parent / "lib", root / "lib")
        stubs = root / "bin"
        stubs.mkdir()
        for tool in ("kind", "kubectl", "helm"):
            (stubs / tool).write_text(STUB, encoding="utf-8")
            (stubs / tool).chmod(0o755)
        log = root / "calls.log"
        env = {k: v for k, v in os.environ.items() if k not in CONFIG_VARS}
        env.update(PATH=f"{stubs}:{os.environ['PATH']}", STUB_LOG=str(log),
                   SYSTEM_TOOLS_CONFIG=str(root / "config"),
                   OTLP_EXPORT_URL="https://otlp.example.invalid/otel",
                   OTLP_EXPORT_AUTH="key-one")
        done = subprocess.run(["/bin/sh", str(folder / "opentelemetry-demo.sh"), "upgrade"],
                              capture_output=True, text=True, input="", env=env, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        calls = log.read_text(encoding="utf-8").splitlines()
        made = [i for i, c in enumerate(calls)
                if "create configmap llm-varied-wrong" in c
                and f"--from-file={folder / WRAPPER.name}" in c]
        helm = [i for i, c in enumerate(calls) if c.startswith("helm ") and " upgrade " in c]
        self.assertEqual(len(made), 1, calls)
        self.assertEqual(len(helm), 1, calls)
        self.assertLess(made[0], helm[0])
        cksum = subprocess.run(["cksum"], input=(folder / WRAPPER.name).read_bytes(),
                               capture_output=True).stdout.decode().split()[0]
        self.assertIn(f"components.llm.podAnnotations.llm-wrapper-cksum={cksum}",
                      calls[helm[0]])


if __name__ == "__main__":
    unittest.main()
