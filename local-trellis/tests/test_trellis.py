"""trellis.sh pins every checkout, swaps the rembg weights and reports drift.

No test downloads or builds anything. Each pinned repository is a local git
repository with two commits, the pin being the first, so a clone that takes
the tip instead of the pin fails. The trellis-mac fake ships a `setup.sh`
that fails when a dependency folder is missing, as the real one would then
clone it at its tip, and that writes a fake venv python: it answers the
Hugging Face token probe from FAKE_HF and logs a `generate.py` run.
"""

import os
import pathlib
import re
import subprocess
import tempfile
import unittest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPT = HERE.parent / "trellis.sh"

# The folders the real setup.sh clones when they are missing (its
# `if [ ! -d ... ]` guards at trellis-mac d58628f).
GUARDED = ["deps/utils3d", "deps/mtlbvh", "deps/mtldiffrast", "deps/mtlmesh",
           "deps/mtlgemm", "deps/trellis2-apple", "TRELLIS.2"]

BIREFNET = '''from transformers import AutoModelForImageSegmentation


class BiRefNet:
    def __init__(self, model_name: str = "ZhengPeng7/BiRefNet"):
        self.model = AutoModelForImageSegmentation.from_pretrained(
            model_name, trust_remote_code=True
        )
'''

# The setup.py files whose -std=c++17 torch 2.15 refuses (C++20 or later).
CXX = ["deps/mtlbvh/setup.py", "deps/mtldiffrast/setup.py", "deps/mtlmesh/setup.py",
       "deps/mtlgemm/setup.py", "deps/trellis2-apple/o-voxel/setup.py"]
EIGEN = "deps/trellis2-apple/o-voxel/third_party/eigen"

FAKE_SETUP = """#!/bin/sh
set -e
cd "$(dirname "$0")"
for d in %s; do
  [ -e "$d/.git" ] || { echo "setup.sh would clone $d at its tip" >&2; exit 1; }
done
for f in %s; do
  if grep -q 'std=c++17' "$f" || ! grep -q 'std=c++20' "$f"; then
    echo "$f: #error C++20 or later compatible compiler is required to use PyTorch" >&2; exit 1
  fi
done
[ -f %s/README ] || { echo "o-voxel: eigen is missing" >&2; exit 1; }
[ -z "$FAKE_SETUP_FAIL" ] || { echo "setup failed" >&2; exit 1; }
mkdir -p .venv/bin
cat > .venv/bin/python <<'PYFAKE'
#!/bin/sh
printf 'HF_TOKEN=%%s HF_HUB_CACHE=%%s\\n' "${HF_TOKEN:+set}" "$HF_HUB_CACHE" >> "$FAKE_ENV_LOG"
if [ "$1" = -c ]; then
  hf=${FAKE_HF:-yes}
  if [ "$hf" = env ]; then if [ -n "$HF_TOKEN" ]; then hf=yes; else hf=no; fi; fi
  case "$hf" in yes|no) echo "token $hf" ;; *) exit 1 ;; esac
  [ "$hf" = no ] || echo "gate ${FAKE_GATE:-ok}"
  exit 0
fi
printf '%%s\\n' "$*" > "$FAKE_LOG"
PYFAKE
chmod +x .venv/bin/python
echo "fake setup done"
""" % (" ".join(GUARDED), " ".join(CXX), EIGEN)


def git(*args, cwd=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True,
                          text=True).stdout.strip()


def make_repo(path, files, submodule=None):
    """A repository whose first commit holds `files`, and `submodule`
    (url, path) when given; returns that commit."""
    path.mkdir(parents=True)
    git("init", "-q", cwd=path)
    git("config", "user.email", "test@example.test", cwd=path)
    git("config", "user.name", "test", cwd=path)
    for name, text in files.items():
        f = path / name
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(text)
    if submodule:
        git("-c", "protocol.file.allow=always", "submodule", "add", "-q", *submodule, cwd=path)
    git("add", "-A", cwd=path)
    git("commit", "-q", "-m", "pin", cwd=path)
    pin = git("rev-parse", "HEAD", cwd=path)
    git("commit", "-q", "--allow-empty", "-m", "tip", cwd=path)
    return pin


def default_pins():
    """The pin lines the script carries, as (path, url, commit)."""
    text = SCRIPT.read_text()
    body = re.search(r"<<'PINS'\n(.*?)\nPINS\n", text, re.S).group(1)
    return [tuple(line.split()) for line in body.splitlines()
            if line.strip() and not line.startswith("#")]


class Trellis(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.base = pathlib.Path(tmp.name)
        self.home = self.base / "home"
        self.home.mkdir()
        self.dest = self.home / ".local/share/trellis-mac"
        self.log = self.base / "generate.log"
        self.env_log = self.base / "python-env.log"
        self.cache = self.base / "hub"
        self.env_file = self.base / "env.sh"
        up = self.base / "upstream"
        self.pins = {".": make_repo(up / "trellis-mac", {"setup.sh": FAKE_SETUP,
                                                          "generate.py": "# fake\n"})}
        make_repo(up / "eigen", {"README": "eigen\n"})
        for d in GUARDED:
            files, sub = {"README": d + "\n"}, None
            if d == "TRELLIS.2":
                files = {"trellis2/pipelines/rembg/BiRefNet.py": BIREFNET}
            for f in CXX:
                if f.startswith(d + "/"):
                    files[f[len(d) + 1:]] = 'extra_compile_args = ["-std=c++17", "-O2"]\n'
            if d == "deps/trellis2-apple":
                sub = (str(up / "eigen"), "o-voxel/third_party/eigen")
            self.pins[d] = make_repo(up / d.replace("/", "_"), files, sub)
        self.pin_file = self.base / "pins"
        self.write_pins()

    def write_pins(self):
        up = self.base / "upstream"
        lines = []
        for d, sha in self.pins.items():
            src = up / ("trellis-mac" if d == "." else d.replace("/", "_"))
            lines.append(f"{d} {src} {sha}\n")
        self.pin_file.write_text("".join(lines))

    def run_sh(self, *args, **env):
        e = {"HOME": str(self.home), "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
             "TRELLIS_PINS": str(self.pin_file), "FAKE_LOG": str(self.log),
             "FAKE_ENV_LOG": str(self.env_log), "HF_HUB_CACHE": str(self.cache),
             "TRELLIS_ENV_FILE": str(self.env_file),
             "SD_PACK_ROOT": str(self.base / "no-pack"), "LANG": "en_US.UTF-8",
             # The fake trellis2-apple names its eigen submodule by a local path.
             "GIT_CONFIG_COUNT": "1", "GIT_CONFIG_KEY_0": "protocol.file.allow",
             "GIT_CONFIG_VALUE_0": "always", **env}
        e = {k: v for k, v in e.items() if v is not None}
        return subprocess.run(["sh", str(SCRIPT), *args], env=e, capture_output=True,
                              text=True, stdin=subprocess.DEVNULL, cwd=self.base)

    def install(self, **env):
        r = self.run_sh("install", **env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def head(self, d):
        return git("rev-parse", "HEAD", cwd=self.dest / d)

    # -- the pins the script carries

    def test_the_script_pins_trellis_mac_and_every_folder_setup_clones(self):
        pins = default_pins()
        self.assertEqual(sorted(p[0] for p in pins), sorted([".", *GUARDED]))
        for path, url, sha in pins:
            self.assertRegex(sha, r"^[0-9a-f]{40}$", path)
            self.assertTrue(url.startswith("https://github.com/"), path)
        root = [p for p in pins if p[0] == "."][0]
        self.assertTrue(root[2].startswith("d58628f"), root)
        self.assertEqual(dict((p[0], p[2]) for p in pins)["deps/utils3d"],
                         "9a4eb15e4021b67b12c460c7057d642626897ec8")

    # -- install

    def test_install_checks_out_each_pin_not_the_tip_and_runs_setup(self):
        r = self.install()
        self.assertIn("fake setup done", r.stdout)
        for d, sha in self.pins.items():
            self.assertEqual(self.head(d), sha, d)

    def test_install_swaps_rmbg_for_the_mit_birefnet_weights_once(self):
        self.install()
        self.install()
        src = (self.dest / "TRELLIS.2/trellis2/pipelines/rembg/BiRefNet.py").read_text()
        self.assertEqual(src.count('if model_name == "briaai/RMBG-2.0":'), 1, src)
        lines = src.splitlines()
        i = lines.index('    def __init__(self, model_name: str = "ZhengPeng7/BiRefNet"):')
        self.assertTrue(lines[i + 1].startswith('        if model_name == "briaai/RMBG-2.0":'))
        self.assertEqual(lines[i + 2], '            model_name = "ZhengPeng7/BiRefNet"')

    def test_a_hand_applied_guard_counts_as_swapped(self):
        self.install()
        f = self.dest / "TRELLIS.2/trellis2/pipelines/rembg/BiRefNet.py"
        f.write_text(BIREFNET.replace(
            '"ZhengPeng7/BiRefNet"):\n',
            '"ZhengPeng7/BiRefNet"):\n        if model_name == "briaai/RMBG-2.0":\n'
            '            model_name = "ZhengPeng7/BiRefNet"\n'))
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.install()
        self.assertEqual(f.read_text().count("briaai/RMBG-2.0"), 1)

    def test_install_builds_with_cxx20_and_the_eigen_submodule(self):
        self.install()
        for f in CXX:
            text = (self.dest / f).read_text()
            self.assertIn("-std=c++20", text, f)
            self.assertNotIn("-std=c++17", text, f)
        self.assertTrue((self.dest / EIGEN / "README").exists())

    def test_install_of_a_finished_setup_runs_setup_sh_no_more(self):
        self.install()
        r = self.install(FAKE_HF="no")
        self.assertNotIn("fake setup done", r.stdout)
        self.assertIn("setup finished at these pins", r.stdout)
        self.assertIn("hf auth login", r.stdout)

    def test_install_runs_setup_again_when_the_venv_is_gone(self):
        self.install()
        for f in (self.dest / ".venv/bin").iterdir():
            f.unlink()
        r = self.install()
        self.assertIn("fake setup done", r.stdout)

    def test_install_fails_when_the_rembg_hook_is_gone_upstream(self):
        sha = make_repo(self.base / "upstream/other", {
            "trellis2/pipelines/rembg/BiRefNet.py": "class BiRefNet:\n    pass\n"})
        self.pins["TRELLIS.2"] = sha
        self.write_pins()
        self.pin_file.write_text(self.pin_file.read_text().replace(
            str(self.base / "upstream/TRELLIS.2"), str(self.base / "upstream/other")))
        r = self.run_sh("install")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("BiRefNet.py", r.stderr)

    def test_install_prints_the_two_manual_steps(self):
        r = self.install()
        self.assertIn("hf auth login", r.stdout)
        self.assertIn("https://huggingface.co/facebook/dinov3-vitl16-pretrain-lvd1689m", r.stdout)

    def test_install_refuses_a_folder_on_a_volume(self):
        r = self.run_sh("install", TRELLIS_HOME="/Volumes/example/trellis-mac")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("system disk", r.stderr)

    def test_install_moves_a_checkout_whose_pin_moved(self):
        self.install()
        up = self.base / "upstream/deps_mtlgemm"
        self.pins["deps/mtlgemm"] = git("rev-parse", "HEAD", cwd=up)
        self.write_pins()
        self.install()
        self.assertEqual(self.head("deps/mtlgemm"), self.pins["deps/mtlgemm"])

    def test_a_failed_setup_leaves_status_unfinished_and_a_rerun_finishes(self):
        r = self.run_sh("install", FAKE_SETUP_FAIL="1")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        s = self.run_sh("status")
        self.assertEqual(s.returncode, 1, s.stdout + s.stderr)
        self.assertIn("DIFFERS", s.stdout)
        self.assertIn("setup", s.stdout)
        self.install()
        s = self.run_sh("status")
        self.assertEqual(s.returncode, 0, s.stdout + s.stderr)

    def test_install_stores_no_token(self):
        self.install(HF_TOKEN="hf_exampletoken")
        found = subprocess.run(["grep", "-rl", "hf_exampletoken", str(self.home)],
                               capture_output=True, text=True).stdout
        self.assertEqual(found, "")

    # -- status

    def test_status_with_nothing_installed_is_missing_and_exits_3(self):
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertIn("MISSING", r.stdout)

    def test_status_after_install_is_ok(self):
        self.install()
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotRegex(r.stdout, "MISSING|DIFFERS|FAILED")
        self.assertIn("ok", r.stdout)

    def test_status_names_a_checkout_whose_pin_moved(self):
        self.install()
        subprocess.run(["git", "-C", str(self.dest / "deps/mtlmesh"), "commit", "-q",
                        "--allow-empty", "-m", "local", "--no-verify"],
                       env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.test",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.test"},
                       check=True)
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"DIFFERS +deps/mtlmesh .*pinned " + self.pins["deps/mtlmesh"])

    def test_status_reports_a_missing_venv(self):
        self.install()
        for f in (self.dest / ".venv/bin").iterdir():
            f.unlink()
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"MISSING +venv")

    def test_status_reports_rmbg_not_swapped(self):
        self.install()
        f = self.dest / "TRELLIS.2/trellis2/pipelines/rembg/BiRefNet.py"
        f.write_text(BIREFNET)
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"DIFFERS +rembg")

    def test_status_reports_no_hugging_face_token(self):
        self.install()
        r = self.run_sh("status", FAKE_HF="no")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"DIFFERS +no Hugging Face token")
        self.assertIn("hf auth login", r.stdout)

    def test_a_failed_token_probe_is_no_pass(self):
        self.install()
        r = self.run_sh("status", FAKE_HF="crash")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"FAILED +Hugging Face token probe")

    def test_status_reports_the_dinov3_licence_not_accepted(self):
        self.install()
        r = self.run_sh("status", FAKE_GATE="gated")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"DIFFERS +DINOv3 licence not accepted")
        self.assertIn("dinov3-vitl16-pretrain-lvd1689m", r.stdout)

    def test_a_failed_gate_probe_is_no_pass(self):
        self.install()
        r = self.run_sh("status", FAKE_GATE="error OSError")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"FAILED +DINOv3 licence probe")

    def test_status_reports_an_unmounted_cache_volume(self):
        self.install()
        r = self.run_sh("status", HF_HUB_CACHE="/Volumes/sd-example-unmounted-9000/hub")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout,
                         r"MISSING +Hugging Face cache volume /Volumes/sd-example-unmounted-9000")

    # -- the token and the cache at run time

    def test_the_token_comes_from_the_env_file_and_is_written_nowhere(self):
        self.env_file.write_text("# shell env\nexport HUGGING_FACE_API_KEY='hf_example9000'\n")
        self.install(HF_TOKEN=None)
        r = self.run_sh("status", FAKE_HF="env", HF_TOKEN=None)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("HF_TOKEN=set", self.env_log.read_text())
        self.assertNotIn("hf_example9000", r.stdout + r.stderr)
        found = subprocess.run(["grep", "-rl", "hf_example9000", str(self.home)],
                               capture_output=True, text=True).stdout
        self.assertEqual(found, "")

    def test_no_token_anywhere_is_reported(self):
        self.install(HF_TOKEN=None)
        r = self.run_sh("status", FAKE_HF="env", HF_TOKEN=None)
        self.assertRegex(r.stdout, r"DIFFERS +no Hugging Face token")

    def fake_sd(self, body):
        sd = self.base / "pack/bin/sd"
        sd.parent.mkdir(parents=True)
        sd.write_text("#!/bin/sh\n" + body)
        sd.chmod(0o755)
        return str(self.base / "pack")

    def test_the_cache_defaults_to_the_bulk_storage_root(self):
        pack = self.fake_sd(f'[ "$*" = "config get sd.bulk_storage_root" ] && echo {self.base}/bulk\n')
        self.install()
        r = self.run_sh("generate", "in.png", HF_HUB_CACHE=None, SD_PACK_ROOT=pack)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"HF_HUB_CACHE={self.base}/bulk/system/huggingface/hub",
                      self.env_log.read_text().splitlines()[-1])

    def test_an_unread_bulk_storage_root_stops_generate(self):
        pack = self.fake_sd("exit 1\n")
        self.install()
        r = self.run_sh("generate", "in.png", HF_HUB_CACHE=None, SD_PACK_ROOT=pack)
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("sd.bulk_storage_root", r.stderr)
        self.assertFalse(self.log.exists())

    def test_generate_refuses_an_unmounted_cache_volume(self):
        self.install()
        r = self.run_sh("generate", "in.png", HF_HUB_CACHE="/Volumes/sd-example-unmounted-9000/hub")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("not mounted", r.stderr)
        self.assertFalse(self.log.exists())

    # -- generate

    def test_generate_runs_generate_py_in_the_venv(self):
        self.install()
        r = self.run_sh("generate", "in.png", "--output", "out/model")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.log.read_text().strip(),
                         f"{self.dest}/generate.py in.png --output out/model")

    def test_generate_without_an_image_is_usage(self):
        self.install()
        r = self.run_sh("generate")
        self.assertEqual(r.returncode, 1)
        self.assertIn("usage", r.stderr)

    def test_generate_before_install_names_install(self):
        r = self.run_sh("generate", "in.png")
        self.assertEqual(r.returncode, 1)
        self.assertIn("trellis.sh install", r.stderr)

    # -- usage

    def test_help_exits_0_and_declares_the_status_codes(self):
        r = self.run_sh("help")
        self.assertEqual(r.returncode, 0)
        self.assertIn("local-health-check reads these codes", r.stdout)

    def test_no_argument_is_usage_and_exits_1(self):
        r = self.run_sh()
        self.assertEqual(r.returncode, 1)
        self.assertIn("usage", r.stderr)


if __name__ == "__main__":
    unittest.main()
