"""bench/cost.py reproduces the four workload comparisons (A to D) from the research note, within rounding.

Expected numbers are the ones printed in the note, in USD at 1 EUR = 1.10 USD. The note's arithmetic was
re-derived while writing these tests. It holds everywhere except one basis difference, flagged in the test for
workload D: the note takes Fly.io's own 30-day figure ($528.02) where every other line uses 730 hours.
"""

import importlib.util
import json
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
BENCH = ROOT / "bench"


def load_cost():
    spec = importlib.util.spec_from_file_location("bench_cost", BENCH / "cost.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["bench_cost"] = module
    spec.loader.exec_module(module)
    return module


cost = load_cost()
PRICES = cost.load_prices()


def run(workload, **kw):
    w = cost.WORKLOADS[workload]
    args = dict(vcpu=w["vcpu"], ram=w["ram"], minutes=w["minutes"], count=w["count"], monthly=w["monthly"], hypothetical=True)
    args.update(kw)
    return cost.compute(PRICES, **args)


def by_id(res):
    return {q["id"]: q for q in res["vendors"]}


class Close(unittest.TestCase):
    def close(self, got, want, msg="", rel=0.006, abs_=0.0006):
        self.assertIsNotNone(got, msg)
        self.assertLessEqual(abs(got - want), max(abs_, rel * abs(want)), f"{msg}: got {got}, the note says {want}")


class WorkloadA(Close):
    """One 8 vCPU / 16 GiB hour."""

    def test_hetzner(self):
        self.close(run("A")["hetzner"]["usd"], 0.105, "CPX42 HEL1")
        self.assertEqual(run("A")["hetzner"]["type"], "cpx42")
        self.close(run("A", location="nbg1")["hetzner"]["usd"], 0.123, "CPX42 NBG1 est.")
        self.close(run("A", hetzner_type="cx43")["hetzner"]["usd"], 0.029, "CX43, flagged unavailable")

    def test_vendors(self):
        want = {"e2b": 0.662, "daytona": 0.662, "blaxel": 0.662, "modal": 0.952, "runloop": 1.267, "morph": 0.40,
                "fly-machines": 0.367, "vercel": 1.363, "codespaces": 0.72, "github-actions": 1.32, "depot": 1.44,
                "namespace": 0.48, "blacksmith": 0.96}
        got = by_id(run("A"))
        for vid, usd in want.items():
            self.close(got[vid]["cost_usd"], usd, vid)
        self.assertEqual(got["cloudflare"]["status"], "not_offered")
        self.close(got["namespace"]["alt_cost_usd"], 0.72, "namespace overage")
        self.close(by_id(run("A", util=0.1))["vercel"]["cost_usd"], 0.442, "vercel at 10% CPU")

    def test_ratios(self):
        self.close(by_id(run("A"))["daytona"]["ratio_vs_hetzner"], 6.3, "vs CPX42 HEL1", rel=0.01)
        self.close(by_id(run("A", location="nbg1"))["daytona"]["ratio_vs_hetzner"], 5.4, "vs CPX42 NBG1 est.", rel=0.01)
        self.close(by_id(run("A", hetzner_type="cx43"))["daytona"]["ratio_vs_hetzner"], 22.8, "vs CX43", rel=0.01)
        self.close(by_id(run("A"))["fly-machines"]["ratio_vs_hetzner"], 3.5, "Fly vs CPX42", rel=0.01)

    def test_break_even(self):
        self.close(by_id(run("A"))["daytona"]["break_even_minutes"], 9.5, "CPX42 HEL1", rel=0.01)
        self.close(by_id(run("A", location="nbg1"))["daytona"]["break_even_minutes"], 11, "CPX42 NBG1 est.", rel=0.02)
        self.close(by_id(run("A", hetzner_type="cx43"))["daytona"]["break_even_minutes"], 2.6, "CX43", rel=0.02)

    def test_five_minute_run(self):
        res = cost.compute(PRICES, vcpu=8, ram=16, minutes=5, hypothetical=True)
        self.close(by_id(res)["daytona"]["cost_usd"], 0.055, "5 minutes on a per-second vendor")
        self.close(res["hetzner"]["usd"], 0.105, "5 minutes still bills one started hour")


class WorkloadB(Close):
    """8 h a day, 22 days, 8 vCPU / 16 GiB, one fresh server per day: 176 machine-hours."""

    def test_hetzner(self):
        res = run("B")
        self.assertEqual(res["hetzner"]["billed_machine_hours"], 176)
        self.close(res["hetzner"]["usd"], 18.45, "CPX42 HEL1")
        self.close(run("B", location="nbg1")["hetzner"]["usd"], 21.70, "CPX42 NBG1 est.")
        self.close(run("B", hetzner_type="cx43")["hetzner"]["usd"], 5.11, "CX43")

    def test_vendors(self):
        want = {"e2b": 116.6, "daytona": 116.6, "blaxel": 116.6, "modal": 167.5, "runloop": 223.0, "morph": 70.4,
                "fly-machines": 64.5, "vercel": 239.9, "codespaces": 126.7, "github-actions": 232.3, "depot": 253.4,
                "namespace": 84.5}
        got = by_id(run("B"))
        for vid, usd in want.items():
            self.close(got[vid]["cost_usd"], usd, vid, rel=0.002)
        self.close(got["namespace"]["alt_cost_usd"], 126.7, "namespace overage", rel=0.002)
        self.close(by_id(run("B", util=0.1))["vercel"]["cost_usd"], 77.7, "vercel at 10% CPU", rel=0.002)

    def test_plan_fees_are_separate_and_added_only_in_the_monthly_total(self):
        e2b = by_id(run("B"))["e2b"]
        self.assertEqual(e2b["plan"], "Pro")
        self.assertEqual(e2b["plan_fee_usd_month"], 150)
        self.close(e2b["cost_usd"], 116.6, "usage only", rel=0.002)
        self.close(e2b["total_usd"], 266.6, "usage plus the $150 Pro fee", rel=0.002)
        self.close(by_id(run("B"))["modal"]["total_usd"], 137.5, "after the $30 Starter credit", rel=0.002)
        self.close(by_id(run("B", util=0.1))["vercel"]["total_usd"], 77.7, "$20 fee is covered by its $20 credit", rel=0.002)

    def test_ratios(self):
        got = by_id(run("B"))
        self.close(got["daytona"]["ratio_vs_hetzner"], 6.3, "Daytona", rel=0.01)
        self.close(got["e2b"]["total_usd"] / run("B")["hetzner"]["usd"], 14.4, "E2B with the Pro fee", rel=0.01)
        self.close(got["fly-machines"]["ratio_vs_hetzner"], 3.5, "Fly", rel=0.01)
        self.close(by_id(run("B", util=0.1))["vercel"]["ratio_vs_hetzner"], 4.2, "Vercel at 10% CPU", rel=0.02)

    def test_e2b_cannot_offer_the_size(self):
        q = by_id(cost.compute(PRICES, vcpu=8, ram=16, minutes=480, count=22))["e2b"]
        self.assertEqual(q["status"], "not_offered")
        self.assertIn("8 GiB", q["reason"])


class WorkloadC(Close):
    """10 parallel 4 vCPU / 8 GiB machines for 45 minutes: 7.5 machine-hours used, 10 billed by Hetzner."""

    def test_hetzner(self):
        res = run("C")
        self.assertEqual(res["hetzner"]["billed_machine_hours"], 10)
        self.close(res["hetzner"]["machine_hours_used"], 7.5, "machine-hours used")
        self.assertEqual(res["hetzner"]["type"], "cpx32")
        self.close(res["hetzner"]["usd"], 0.538, "CPX32 HEL1")
        self.close(run("C", location="nbg1")["hetzner"]["usd"], 0.635, "CPX32 NBG1 est.")
        self.close(run("C", hetzner_type="cx33")["hetzner"]["usd"], 0.158, "CX33")

    def test_vendors(self):
        want = {"e2b": 2.484, "daytona": 2.484, "blaxel": 2.484, "modal": 3.57, "runloop": 4.75, "morph": 1.50,
                "fly-machines": 1.37, "vercel": 5.11, "cloudflare": 3.01, "codespaces": 2.70, "github-actions": 5.40,
                "depot": 5.40, "namespace": 1.80}
        got = by_id(run("C"))
        for vid, usd in want.items():
            self.close(got[vid]["cost_usd"], usd, vid)
        self.close(got["namespace"]["alt_cost_usd"], 2.70, "namespace overage")
        self.close(by_id(run("C", util=0.1))["vercel"]["cost_usd"], 1.66, "vercel at 10% CPU")

    def test_ratios(self):
        got = by_id(run("C"))
        self.close(got["daytona"]["ratio_vs_hetzner"], 4.6, "vs CPX32 HEL1", rel=0.01)
        self.close(by_id(run("C", location="nbg1"))["daytona"]["ratio_vs_hetzner"], 3.9, "vs CPX32 NBG1 est.", rel=0.01)
        self.close(by_id(run("C", hetzner_type="cx33"))["daytona"]["ratio_vs_hetzner"], 15.7, "vs CX33", rel=0.01)
        self.close(got["fly-machines"]["ratio_vs_hetzner"], 2.6, "Fly", rel=0.02)
        self.close(got["namespace"]["ratio_vs_hetzner"], 3.3, "Namespace prepaid", rel=0.02)

    def test_e2b_is_offered_here(self):
        # 4 vCPU / 8 GiB for 45 minutes fits the free Hobby plan, so no hypothetical flag is needed.
        q = by_id(cost.compute(PRICES, vcpu=4, ram=8, minutes=45, count=10))["e2b"]
        self.assertEqual(q["status"], "ok")
        self.assertEqual(q["plan"], "Hobby")


class WorkloadD(Close):
    """16 vCPU / 32 GiB always on for a 730 h month."""

    def test_hetzner_monthly_cap(self):
        res = run("D")
        self.assertEqual(res["hetzner"]["type"], "cpx62")
        self.assertTrue(res["hetzner"]["monthly_cap_applied"])
        self.close(res["hetzner"]["usd"], 122.1, "CPX62 HEL1 (the EUR 110.99 monthly price)", rel=0.002)
        self.close(run("D", location="nbg1")["hetzner"]["usd"], 143.5, "CPX62 NBG1", rel=0.002)
        self.close(run("D", hetzner_type="cx53")["hetzner"]["usd"], 33.0, "CX53", rel=0.002)

    def test_vendors(self):
        want = {"e2b": 967.1, "daytona": 967.1, "blaxel": 967.1, "modal": 1389.7, "runloop": 1850.1, "morph": 584.0,
                "vercel": 1990.0, "codespaces": 1051.0, "github-actions": 1839.6, "depot": 2102.0, "namespace": 700.8}
        got = by_id(run("D"))
        for vid, usd in want.items():
            self.close(got[vid]["cost_usd"], usd, vid, rel=0.002)
        self.close(got["namespace"]["alt_cost_usd"], 1051.0, "namespace overage", rel=0.002)
        self.close(by_id(run("D", util=0.0))["vercel"]["cost_usd"], 495.0, "vercel idle memory floor", rel=0.002)
        self.assertEqual(got["cloudflare"]["status"], "not_offered")

    def test_fly_uses_730_hours_not_the_notes_30_day_figure(self):
        # The note prints $528.02, Fly's own 30-day figure (720 h). Every other line in D uses 730 h, so cost.py
        # applies 730 h to Fly too: 0.7334 x 730 = $535.38. The difference is about 1.4%.
        fly = by_id(run("D"))["fly-machines"]
        self.close(fly["cost_usd"], 535.4, "performance-16x for 730 h", rel=0.001)
        self.close(fly["cost_usd"], 528.02, "the note's 30-day figure", rel=0.02)
        self.close(fly["ratio_vs_hetzner"], 4.3, "Fly ratio, the note says 4.3x", rel=0.03)

    def test_e2b_plan_fee(self):
        e2b = by_id(run("D"))["e2b"]
        self.assertEqual(e2b["plan"], "Pro")
        self.close(e2b["total_usd"], 1117.0, "usage plus the $150 Pro fee", rel=0.002)
        self.assertTrue(any("hypothetical" in n for n in e2b["notes"]))

    def test_ratios(self):
        got = by_id(run("D"))
        self.close(got["daytona"]["ratio_vs_hetzner"], 7.9, "vs CPX62 HEL1", rel=0.01)
        self.close(by_id(run("D", location="nbg1"))["daytona"]["ratio_vs_hetzner"], 6.7, "vs CPX62 NBG1", rel=0.01)
        self.close(by_id(run("D", hetzner_type="cx53"))["daytona"]["ratio_vs_hetzner"], 29, "vs CX53", rel=0.02)

    def test_without_hypothetical_the_limits_apply(self):
        got = by_id(cost.compute(PRICES, vcpu=16, ram=32, minutes=43800, monthly=True))
        self.assertEqual(got["e2b"]["status"], "not_offered")
        self.assertEqual(got["vercel"]["status"], "not_offered")
        self.assertEqual(got["daytona"]["status"], "ok")


class Rules(Close):
    def test_hetzner_bills_started_hours(self):
        for minutes, hours in ((1, 1), (60, 1), (60.5, 2), (61, 2), (120, 2), (121, 3)):
            with self.subTest(minutes=minutes):
                self.assertEqual(cost.hetzner_quote(PRICES, cost.nearest_hetzner(PRICES, 8, 16), "hel1", minutes, 1)
                                 ["started_hours_per_machine"], hours)

    def test_minute_billing_rounds_each_machine_up(self):
        res = cost.compute(PRICES, vcpu=8, ram=16, minutes=1.2, count=10, only=["github-actions", "daytona"])
        got = by_id(res)
        self.close(got["github-actions"]["cost_usd"], 10 * 2 * 0.022, "two billed minutes each")
        self.close(got["daytona"]["cost_usd"], 10 * 0.6624 * 1.2 / 60, "per second is exact")

    def test_nearest_type_is_orderable_and_cx_is_never_chosen(self):
        for vcpu, ram in ((2, 4), (4, 8), (8, 16), (16, 32)):
            t = cost.nearest_hetzner(PRICES, vcpu, ram)
            self.assertTrue(t["available"])
            self.assertNotEqual(t["line"], "CX")
        self.assertIsNone(cost.nearest_hetzner(PRICES, 64, 128))

    def test_active_cpu_billing_uses_utilisation(self):
        hi = by_id(cost.compute(PRICES, vcpu=8, ram=16, minutes=60, util=1.0, only=["vercel"]))["vercel"]["cost_usd"]
        lo = by_id(cost.compute(PRICES, vcpu=8, ram=16, minutes=60, util=0.1, only=["vercel"]))["vercel"]["cost_usd"]
        self.close(hi - lo, 8 * 0.128 * 0.9, "only the CPU part scales")
        flat = [by_id(cost.compute(PRICES, vcpu=8, ram=16, minutes=60, util=u, only=["daytona"]))["daytona"]["cost_usd"]
                for u in (1.0, 0.1)]
        self.assertEqual(flat[0], flat[1])

    def test_break_even_is_the_crossing_point(self):
        res = cost.compute(PRICES, vcpu=8, ram=16, minutes=60, only=["daytona"])
        m = res["vendors"][0]["break_even_minutes"]
        just_under = cost.compute(PRICES, vcpu=8, ram=16, minutes=m * 0.99, only=["daytona"])
        just_over = cost.compute(PRICES, vcpu=8, ram=16, minutes=m * 1.01, only=["daytona"])
        self.assertLess(just_under["vendors"][0]["cost_usd"], res["hetzner"]["usd"])
        self.assertGreater(just_over["vendors"][0]["cost_usd"], res["hetzner"]["usd"])

    def test_no_machine_is_free(self):
        for q in run("A")["vendors"]:
            if q["status"] == "ok":
                self.assertGreater(q["cost_usd"], 0, q["id"])


class Data(unittest.TestCase):
    def test_fx_is_stated_once_as_an_assumption(self):
        self.assertEqual(PRICES["meta"]["fx_usd_per_eur"], 1.10)
        self.assertIn("Assumption", PRICES["meta"]["fx_note"])
        text = (BENCH / "prices.toml").read_text()
        self.assertEqual(text.count("fx_usd_per_eur"), 1)

    def test_every_vendor_entry_carries_its_source_and_date(self):
        ids = set()
        for v in PRICES["vendor"]:
            with self.subTest(v["id"]):
                self.assertNotIn(v["id"], ids)
                ids.add(v["id"])
                self.assertTrue(v["source"].startswith("https://"))
                self.assertRegex(v["read_date"], r"^\d{4}-\d{2}-\d{2}$")
                self.assertIsInstance(v["verified"], bool)
                self.assertEqual(v["verified"], not v["unverified_items"], "verified must match the unverified list")
                self.assertTrue(v["plan"])
                self.assertIn(v["granularity"], ("second", "minute"))
                self.assertIn(v["model"], ("resource", "mcu", "fixed_rate"))

    def test_the_report_flags_are_carried(self):
        flagged = {v["id"] for v in PRICES["vendor"] if not v["verified"]}
        for vid in ("daytona", "modal", "morph", "blacksmith", "codespaces", "github-actions", "fly-sprites"):
            self.assertIn(vid, flagged)
        for vid in ("e2b", "fly-machines", "blaxel"):
            self.assertNotIn(vid, flagged)

    def test_hetzner_cx_is_unavailable_and_cpx_ccx_are_orderable(self):
        for t in PRICES["hetzner"]["type"]:
            with self.subTest(t["name"]):
                self.assertEqual(t["available"], t["line"] in ("CPX", "CCX"))
        self.assertTrue(PRICES["hetzner"]["includes_ipv4"])
        self.assertEqual(PRICES["hetzner"]["billing"], "started_hour")

    def test_page_hourly_price_matches_hel1_monthly_over_624(self):
        # Evidence for the note's assumption that the single hourly price on the pages is the HEL1 price.
        for t in PRICES["hetzner"]["type"]:
            with self.subTest(t["name"]):
                self.assertAlmostEqual(t["eur_hour"] * 624, t["eur_month_hel1"], delta=t["eur_month_hel1"] * 0.01)

    def test_nbg1_estimates_match_the_note(self):
        by_name = {t["name"]: t for t in PRICES["hetzner"]["type"]}
        self.assertAlmostEqual(cost.eur_hour(by_name["cpx42"], "nbg1")[0], 0.1121, places=3)
        self.assertAlmostEqual(cost.eur_hour(by_name["cpx32"], "nbg1")[0], 0.0577, places=3)
        self.assertFalse(cost.eur_hour(by_name["cx43"], "nbg1")[1], "CX has no NBG1/HEL1 split, so no estimate")


class Cli(unittest.TestCase):
    def cost_py(self, *args):
        return subprocess.run([sys.executable, str(BENCH / "cost.py"), *args], capture_output=True, text=True, timeout=60)

    def test_table_for_the_example_in_the_task(self):
        p = self.cost_py("--vcpu", "8", "--ram", "16", "--minutes", "60", "--count", "10", "--cpu-util", "1.0")
        self.assertEqual(p.returncode, 0, p.stderr)
        for text in ("CPX42", "Break-even", "not offered", "* Unverified inputs", "Plan fee"):
            self.assertIn(text, p.stdout)
        self.assertRegex(p.stdout, r"Daytona sandbox\*")  # unverified rows are starred

    def test_json_output(self):
        p = self.cost_py("--vcpu", "4", "--ram", "8", "--minutes", "45", "--count", "10", "--json")
        doc = json.loads(p.stdout)
        self.assertEqual(doc["hetzner"]["type"], "cpx32")
        self.assertEqual(doc["inputs"]["count"], 10)
        self.assertIn("unverified_items", doc["vendors"][0])

    def test_workload_shortcut_prints_each_of_a_to_d(self):
        for w in "ABCD":
            p = self.cost_py("--workload", w)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertIn(f"Workload {w}:", p.stdout)

    def test_usage_error(self):
        p = self.cost_py("--vcpu", "8")
        self.assertEqual(p.returncode, 2)

    def test_the_public_page_table_is_the_calculator_output(self):
        page = (ROOT / "docs" / "cost.md")
        if not page.exists():
            self.skipTest("docs/cost.md not written yet")
        table = cost.docs_table(PRICES)
        self.assertIn(table, page.read_text(encoding="utf-8"), "docs/cost.md table is stale: run `python bench/cost.py --docs-table`")


if __name__ == "__main__":
    unittest.main()
