"""bench/run_workload.sh: the result-recording path (dry run only; the real build takes about 20 minutes) and the
hand-off from a result file to cost.py --from-result."""

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "bench" / "run_workload.sh"
COST = ROOT / "bench" / "cost.py"
MARKER = "MOTEL_BENCH_RESULT "
BASH = shutil.which("bash")

FIELDS = {"schema", "workload", "tag", "commit", "dry_run", "status", "exit_status", "build_exit_status",
          "test_exit_status", "server_type", "cpus", "mem_total_kib", "cpu_model", "kernel", "os", "compiler",
          "started_utc",
          "finished_utc", "fetch_seconds", "build_seconds", "test_seconds", "wall_seconds", "tests_result", "tests_run"}


@unittest.skipUnless(BASH, "bash is required")
class DryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bench-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def run_script(self, *args):
        return subprocess.run([BASH, str(SCRIPT), *args], cwd=self.tmp, capture_output=True, text=True, timeout=60)

    def test_dry_run_writes_a_complete_result_file(self):
        p = self.run_script("--dry-run", "--type", "cpx42")
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads((self.tmp / "result.json").read_text())
        self.assertEqual(set(doc), FIELDS)
        self.assertEqual(doc["schema"], "motel-bench-result/1")
        self.assertIs(doc["dry_run"], True)
        self.assertEqual(doc["status"], "dry_run")
        self.assertEqual(doc["exit_status"], 0)
        self.assertEqual(doc["server_type"], "cpx42")
        self.assertEqual(doc["tag"], "v3.13.9")
        self.assertGreaterEqual(doc["cpus"], 1)
        self.assertGreater(doc["mem_total_kib"], 0)
        self.assertTrue(doc["cpu_model"])
        self.assertGreaterEqual(doc["wall_seconds"], 1)
        self.assertRegex(doc["started_utc"], r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

    def test_the_same_json_is_printed_on_a_marked_stdout_line(self):
        p = self.run_script("--dry-run")
        lines = [ln for ln in p.stdout.splitlines() if ln.startswith(MARKER)]
        self.assertEqual(len(lines), 1)
        self.assertEqual(json.loads(lines[0][len(MARKER):]), json.loads((self.tmp / "result.json").read_text()))
        self.assertIsNone(json.loads(lines[0][len(MARKER):])["server_type"])

    def test_custom_output_path(self):
        p = self.run_script("--dry-run", "--out", "nested.json")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((self.tmp / "nested.json").exists())
        self.assertFalse((self.tmp / "result.json").exists())

    def test_bad_arguments_are_refused(self):
        for args in (["--type", 'a"b'], ["--type", "x y"], ["--bogus"], ["--workdir", "/"], ["--workdir", "."]):
            with self.subTest(args=args):
                p = self.run_script("--dry-run", *args)
                self.assertEqual(p.returncode, 64, p.stderr)
                self.assertFalse((self.tmp / "result.json").exists())

    def test_a_dry_run_leaves_the_work_directory_alone(self):
        keep = self.tmp / "precious"
        keep.mkdir()
        (keep / "file").write_text("x")
        p = self.run_script("--dry-run", "--workdir", "precious")
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertTrue((keep / "file").exists())

    def test_the_real_path_never_deletes_a_directory_it_did_not_make(self):
        keep = self.tmp / "precious"
        keep.mkdir()
        (keep / "file").write_text("x")
        p = self.run_script("--workdir", "precious", "--skip-deps")
        self.assertEqual(p.returncode, 64, p.stderr)
        self.assertTrue((keep / "file").exists())
        self.assertIn("not made by this script", p.stderr)

    def test_the_pinned_commit_is_in_the_script_and_the_docs(self):
        text = SCRIPT.read_text()
        self.assertIn('TAG="v3.13.9"', text)
        self.assertIn('COMMIT="8183fa5e3f78ca6ab862de7fb8b14f3d929421e0"', text)
        readme = (ROOT / "bench" / "README.md")
        if readme.exists():
            self.assertIn("8183fa5e3f78ca6ab862de7fb8b14f3d929421e0", readme.read_text())


class FromResult(unittest.TestCase):
    """cost.py --from-result with a hand-made result for a real-looking run (not a measurement)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bench-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)

    def result(self, **kw):
        doc = {"schema": "motel-bench-result/1", "workload": "cpython-pgo-build-and-test", "tag": "v3.13.9",
               "commit": "x", "dry_run": False, "status": "ok", "exit_status": 0, "server_type": "cpx42", "cpus": 8,
               "mem_total_kib": 15 * 1048576, "cpu_model": "test cpu", "fetch_seconds": 120, "build_seconds": 900,
               "test_seconds": 480, "wall_seconds": 1500}
        doc.update(kw)
        return doc

    def run_cost(self, doc, *args, log=False):
        path = self.tmp / ("run.log" if log else "result.json")
        if log:
            path.write_text("progress line\n" + "MOTEL_BENCH_RESULT " + json.dumps(doc) + "\nmotel: released\n")
        else:
            path.write_text(json.dumps(doc))
        return subprocess.run([sys.executable, str(COST), "--from-result", str(path), *args], capture_output=True, text=True,
                              timeout=60)

    def test_uses_the_server_type_size_and_the_whole_script_time(self):
        p = self.run_cost(self.result(), "--json")
        self.assertEqual(p.returncode, 0, p.stderr)
        doc = json.loads(p.stdout)
        self.assertEqual(doc["inputs"]["vcpu"], 8)
        self.assertEqual(doc["inputs"]["ram_gib"], 16)
        self.assertAlmostEqual(doc["inputs"]["minutes"], 25.0)
        self.assertEqual(doc["hetzner"]["type"], "cpx42")
        self.assertEqual(doc["hetzner"]["started_hours_per_machine"], 1)

    def test_text_says_what_is_measured_and_what_is_a_calculation(self):
        p = self.run_cost(self.result())
        self.assertIn("This run took 25.0 minutes on cpx42", p.stdout)
        self.assertIn("EUR 0.0953", p.stdout)
        self.assertIn("list-price calculation, not a run on those vendors", p.stdout)

    def test_a_log_with_the_marker_line_works_too(self):
        p = self.run_cost(self.result(), "--json", log=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertEqual(json.loads(p.stdout)["hetzner"]["type"], "cpx42")

    def test_timed_only_leaves_out_the_fetch_step(self):
        doc = json.loads(self.run_cost(self.result(), "--json", "--timed-only").stdout)
        self.assertAlmostEqual(doc["inputs"]["minutes"], 23.0)

    def test_two_started_hours(self):
        doc = json.loads(self.run_cost(self.result(wall_seconds=3700), "--json").stdout)
        self.assertEqual(doc["hetzner"]["started_hours_per_machine"], 2)

    def test_unknown_type_falls_back_to_cpus_and_memory(self):
        doc = json.loads(self.run_cost(self.result(server_type="my-laptop", cpus=4, mem_total_kib=int(7.6 * 1048576)),
                                       "--json").stdout)
        self.assertEqual(doc["inputs"]["vcpu"], 4)
        self.assertEqual(doc["inputs"]["ram_gib"], 8)
        self.assertEqual(doc["hetzner"]["type"], "cpx32")

    def test_a_failed_run_is_called_out(self):
        p = self.run_cost(self.result(status="build_failed", exit_status=1))
        self.assertIn("build_failed", p.stdout)

    def test_billed_eur_is_printed_beside_the_list_price(self):
        p = self.run_cost(self.result(), "--billed-eur", "0.0953")
        self.assertIn("motel's own estimate for the cabin: EUR 0.0953", p.stdout)

    def test_garbage_is_refused(self):
        path = self.tmp / "x.json"
        path.write_text("not json")
        p = subprocess.run([sys.executable, str(COST), "--from-result", str(path)], capture_output=True, text=True)
        self.assertNotEqual(p.returncode, 0)


if __name__ == "__main__":
    unittest.main()
