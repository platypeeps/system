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
        self.model.eval()
'''

# The guard and the fp16 fix as the operator applied them by hand, with
# their own comments.
HAND = BIREFNET.replace(
    '"ZhengPeng7/BiRefNet"):\n',
    '"ZhengPeng7/BiRefNet"):\n'
    '        if model_name == "briaai/RMBG-2.0":  # non-commercial weights; same net, MIT\n'
    '            model_name = "ZhengPeng7/BiRefNet"\n').replace(
    '        self.model.eval()\n',
    '        self.model.float()  # ZhengPeng7 ships fp16 weights; the input is fp32\n'
    '        self.model.eval()\n')

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
grep -q 'mps patched' TRELLIS.2/trellis2/attn.py || echo '# mps patched' >> TRELLIS.2/trellis2/attn.py
mkdir -p .venv/bin
cat > .venv/bin/python <<'PYFAKE'
#!/bin/sh
printf 'HF_TOKEN=%%s HF_HUB_CACHE=%%s\\n' "${HF_TOKEN:+set}" "$HF_HUB_CACHE" >> "$FAKE_ENV_LOG"
if [ "$1" = -c ]; then
  if [ -f "$(dirname "$0")/../einops" ]; then e=yes; else e=no; fi
  if [ "$2" = "import einops" ]; then [ "$e" = yes ]; exit; fi
  echo "einops $e"
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


def commit_change(repo, name, text):
    """A new commit in `repo` that writes `name`; returns it."""
    (repo / name).write_text(text)
    git("commit", "-q", "-am", "change " + name, cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


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
        # A fake uv: `pip install --python <venv python> einops` marks the
        # venv, which the fake python reads as einops importing.
        self.stubs = self.base / "stubs"
        self.stubs.mkdir()
        self.uv_log = self.base / "uv.log"
        uv = self.stubs / "uv"
        uv.write_text('#!/bin/sh\necho "$*" >> "$FAKE_UV_LOG"\n'
                      '[ -z "$FAKE_UV_FAIL" ] || exit 1\n'
                      '[ "$1 $2 $3" = "pip install --python" ] && [ "$5" = einops ] &&'
                      ' touch "$(dirname "$4")/../einops"\n')
        uv.chmod(0o755)
        up = self.base / "upstream"
        self.pins = {".": make_repo(up / "trellis-mac", {"setup.sh": FAKE_SETUP,
                                                          "generate.py": "# fake\n"})}
        make_repo(up / "eigen", {"README": "eigen\n"})
        for d in GUARDED:
            files, sub = {"README": d + "\n"}, None
            if d == "TRELLIS.2":
                files = {"trellis2/pipelines/rembg/BiRefNet.py": BIREFNET,
                         "trellis2/attn.py": "attn = 'flash'\n"}
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
        e = {"HOME": str(self.home),
             "PATH": f"{self.stubs}:{os.environ.get('PATH', '/usr/bin:/bin')}",
             "FAKE_UV_LOG": str(self.uv_log),
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

    def test_install_casts_the_fp16_weights_to_float_before_eval_once(self):
        self.install()
        self.install()
        lines = (self.dest / "TRELLIS.2/trellis2/pipelines/rembg/BiRefNet.py").read_text().splitlines()
        i = lines.index("        self.model.eval()")
        self.assertTrue(lines[i - 1].startswith("        self.model.float()"), lines)
        self.assertEqual(sum(1 for line in lines if "self.model.float()" in line), 1)

    def test_a_hand_applied_guard_and_cast_count_as_swapped(self):
        self.install()
        f = self.dest / "TRELLIS.2/trellis2/pipelines/rembg/BiRefNet.py"
        f.write_text(HAND)
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.install()
        self.assertEqual(f.read_text(), HAND)

    def test_a_guard_without_the_cast_is_not_swapped_and_install_adds_it(self):
        self.install()
        f = self.dest / "TRELLIS.2/trellis2/pipelines/rembg/BiRefNet.py"
        f.write_text(HAND.replace(
            "        self.model.float()  # ZhengPeng7 ships fp16 weights; the input is fp32\n", ""))
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"DIFFERS +rembg")
        r = self.install()
        self.assertNotIn("fake setup done", r.stdout)
        text = f.read_text()
        self.assertEqual(text.count("briaai/RMBG-2.0"), 1)
        self.assertIn("        self.model.float()", text)
        self.assertEqual(self.run_sh("status").returncode, 0)

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

    def test_install_puts_einops_in_the_venv_after_setup(self):
        r = self.install()
        self.assertLess(r.stdout.index("fake setup done"), r.stdout.index("einops"))
        self.assertEqual(self.uv_log.read_text(),
                         f"pip install --python {self.dest}/.venv/bin/python einops\n")

    def test_install_adds_einops_to_a_finished_setup_from_before(self):
        self.install()
        (self.dest / ".venv/einops").unlink()
        r = self.install()
        self.assertNotIn("fake setup done", r.stdout)
        self.assertTrue((self.dest / ".venv/einops").exists())
        self.assertEqual(self.run_sh("status").returncode, 0)

    def test_a_failed_einops_install_fails_install(self):
        r = self.run_sh("install", FAKE_UV_FAIL="1")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("einops", r.stderr)

    # -- edits in a checkout whose pin moves (sd:9000 lane review)

    def upstream(self, d):
        return self.base / "upstream" / ("trellis-mac" if d == "." else d.replace("/", "_"))

    def test_a_pin_moves_over_the_installers_own_patches_and_reapplies_them(self):
        self.install()
        self.pins["deps/mtlgemm"] = commit_change(
            self.upstream("deps/mtlgemm"), "setup.py", 'extra_compile_args = ["-std=c++17", "-O3"]\n')
        self.pins["TRELLIS.2"] = commit_change(
            self.upstream("TRELLIS.2"), "trellis2/pipelines/rembg/BiRefNet.py",
            BIREFNET + "    # upstream change\n")
        self.write_pins()
        r = self.install()
        self.assertIn("fake setup done", r.stdout)
        for d in ("deps/mtlgemm", "TRELLIS.2"):
            self.assertEqual(self.head(d), self.pins[d], d)
        self.assertEqual((self.dest / "deps/mtlgemm/setup.py").read_text(),
                         'extra_compile_args = ["-std=c++20", "-O3"]\n')
        src = (self.dest / "TRELLIS.2/trellis2/pipelines/rembg/BiRefNet.py").read_text()
        self.assertIn("# upstream change", src)
        self.assertEqual(src.count('if model_name == "briaai/RMBG-2.0":'), 1, src)
        self.assertEqual(src.count("self.model.float()"), 1, src)
        self.assertEqual((self.dest / "TRELLIS.2/trellis2/attn.py").read_text().count("mps patched"), 1)
        self.assertEqual(self.run_sh("status").returncode, 0)

    def test_an_edit_install_did_not_make_refuses_and_moves_no_pin(self):
        self.install()
        before = {d: self.head(d) for d in self.pins}
        (self.dest / "generate.py").write_text("# operator fix\n")
        f = self.dest / "TRELLIS.2/trellis2/attn.py"
        f.write_text(f.read_text() + "# operator tweak\n")
        self.pins["."] = git("rev-parse", "HEAD", cwd=self.upstream("."))
        self.pins["TRELLIS.2"] = git("rev-parse", "HEAD", cwd=self.upstream("TRELLIS.2"))
        self.pins["deps/mtlgemm"] = git("rev-parse", "HEAD", cwd=self.upstream("deps/mtlgemm"))
        self.write_pins()
        r = self.run_sh("install")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("generate.py", r.stderr)
        self.assertIn("trellis2/attn.py", r.stderr)
        self.assertEqual({d: self.head(d) for d in before}, before)
        self.assertEqual((self.dest / "generate.py").read_text(), "# operator fix\n")
        self.assertIn("# operator tweak", f.read_text())

    def test_an_edit_on_top_of_a_known_patch_refuses(self):
        self.install()
        f = self.dest / "deps/mtlgemm/setup.py"
        f.write_text(f.read_text() + "# operator flag\n")
        self.pins["deps/mtlgemm"] = git("rev-parse", "HEAD", cwd=self.upstream("deps/mtlgemm"))
        self.write_pins()
        r = self.run_sh("install")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("deps/mtlgemm", r.stderr)
        self.assertIn("# operator flag", f.read_text())

    def test_dirty_files_with_no_record_of_the_patches_refuse(self):
        self.install()
        (self.dest / ".local-trellis-patches").unlink()
        self.pins["deps/mtlgemm"] = git("rev-parse", "HEAD", cwd=self.upstream("deps/mtlgemm"))
        self.write_pins()
        r = self.run_sh("install")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("setup.py", r.stderr)
        self.assertIn("-std=c++20", (self.dest / "deps/mtlgemm/setup.py").read_text())

    def test_an_edit_in_a_checkout_whose_pin_stays_is_left_alone(self):
        self.install()
        (self.dest / "generate.py").write_text("# operator fix\n")
        self.pins["deps/mtlgemm"] = git("rev-parse", "HEAD", cwd=self.upstream("deps/mtlgemm"))
        self.write_pins()
        self.install()
        self.assertEqual(self.head("deps/mtlgemm"), self.pins["deps/mtlgemm"])
        self.assertEqual((self.dest / "generate.py").read_text(), "# operator fix\n")

    def test_a_first_install_over_operator_files_keeps_them(self):
        self.dest.mkdir(parents=True)
        (self.dest / "generate.py").write_text("# operator copy\n")
        r = self.run_sh("install")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertEqual((self.dest / "generate.py").read_text(), "# operator copy\n")

    def test_a_dependency_folder_that_is_no_checkout_is_kept(self):
        d = self.dest / "deps/mtlbvh"
        d.mkdir(parents=True)
        (d / "local.txt").write_text("operator\n")
        r = self.run_sh("install")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("no git checkout", r.stderr)
        self.assertEqual((d / "local.txt").read_text(), "operator\n")

    def test_a_replaced_origin_is_named_so_it_can_be_restored(self):
        self.install()
        git("remote", "set-url", "origin", "https://example.test/fork.git", cwd=self.dest / "deps/mtlmesh")
        r = self.install()
        self.assertIn("https://example.test/fork.git", r.stdout)
        self.assertEqual(git("remote", "get-url", "origin", cwd=self.dest / "deps/mtlmesh"),
                         str(self.upstream("deps/mtlmesh")))

    # -- status

    def test_status_reports_einops_missing(self):
        self.install()
        (self.dest / ".venv/einops").unlink()
        r = self.run_sh("status")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertRegex(r.stdout, r"MISSING +einops")

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
