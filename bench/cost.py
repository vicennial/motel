#!/usr/bin/env python3
"""Cost calculator: one workload, priced under each vendor's real billing rule.

    python bench/cost.py --vcpu 8 --ram 16 --minutes 60
    python bench/cost.py --vcpu 4 --ram 8 --minutes 45 --count 10
    python bench/cost.py --workload A            # the four workloads from the research note (A, B, C, D)
    python bench/cost.py --from-result result.json

Standard library only (Python 3.11 or newer). Prices come from bench/prices.toml, which carries the vendor,
the source URL and the date each price was read. All figures are LIST PRICES. Nothing here measures a vendor.

Billing rules modelled:
  Hetzner        every started hour is billed in full, capped at the monthly price per server.
  per second     the exact duration is billed.
  per minute     each machine is rounded up to a whole minute.
  active CPU     the CPU rate applies to --cpu-util times the duration; memory is billed for the whole duration.
Plan fees are monthly. They are shown beside the cost, never hidden inside it. With --monthly the output also
shows a total that adds the fee (less any usage credit the plan includes).
"""

from __future__ import annotations

import argparse
import json
import math
import sys
import tomllib
from pathlib import Path

PRICES_FILE = Path(__file__).with_name("prices.toml")
EPS = 1e-9
RESULT_MARKER = "MOTEL_BENCH_RESULT "

# The four workload comparisons from the research note (list prices read 2026-11-14).
WORKLOADS = {
    "A": dict(vcpu=8, ram=16, minutes=60, count=1, monthly=False,
              label="one machine, 8 vCPU / 16 GiB, 1 hour"),
    "B": dict(vcpu=8, ram=16, minutes=480, count=22, monthly=True,
              label="8 vCPU / 16 GiB, 8 h a day, 22 days (one fresh server per day)"),
    "C": dict(vcpu=4, ram=8, minutes=45, count=10, monthly=False,
              label="10 parallel machines, 4 vCPU / 8 GiB, 45 minutes each"),
    "D": dict(vcpu=16, ram=32, minutes=43800, count=1, monthly=True,
              label="one machine, 16 vCPU / 32 GiB, always on for a 730 h month"),
}
# Vendors shown on the public page (docs/cost.md), in column order.
DOCS_VENDORS = ["daytona", "e2b", "modal", "fly-machines", "vercel"]


# --------------------------------------------------------------------------------------------- prices

def load_prices(path: Path = PRICES_FILE) -> dict:
    with open(path, "rb") as handle:
        return tomllib.load(handle)


def hours_started(minutes: float) -> int:
    return max(1, math.ceil(minutes / 60 - EPS))


def rate_per_hour(rate: float, unit: str, vcpus_per_core: float = 1) -> float:
    """Convert a vendor rate such as 0.00003942 per core-second to USD per vCPU-hour or per GiB-hour."""
    _what, per = unit.split("-")
    seconds = {"second": 3600.0, "minute": 60.0, "hour": 1.0}[per]
    value = rate * seconds
    return value / vcpus_per_core if unit.startswith("core-") else value


# --------------------------------------------------------------------------------------------- Hetzner

def eur_hour(t: dict, location: str) -> tuple[float, bool]:
    """(EUR per hour, is_estimate). The page shows one hourly price (HEL1). NBG1 is scaled by the monthly ratio."""
    if location == "hel1":
        return t["eur_hour"], False
    scale = t["eur_month_nbg1"] / t["eur_month_hel1"]
    return t["eur_hour"] * scale, scale != 1


def nearest_hetzner(prices: dict, vcpu: float, ram: float, *, available_only: bool = True, line: str | None = None):
    best = None
    for t in prices["hetzner"]["type"]:
        if available_only and not t["available"]:
            continue
        if line and t["line"] != line:
            continue
        if t["vcpu"] >= vcpu and t["ram_gib"] >= ram:
            if best is None or (t["eur_hour"], t["name"]) < (best["eur_hour"], best["name"]):
                best = t
    return best


def hetzner_quote(prices: dict, t: dict, location: str, minutes: float, count: int) -> dict:
    fx = prices["meta"]["fx_usd_per_eur"]
    h = prices["hetzner"]
    hourly, est = eur_hour(t, location)
    cap = t["eur_month_" + location]
    started = hours_started(minutes)
    months, rest = divmod(started, h["hours_per_month"])
    per_server = months * cap + min(rest * hourly, cap)
    capped = per_server < started * hourly - 1e-12
    eur = per_server * count
    return {
        "type": t["name"], "line": t["line"], "vcpu": t["vcpu"], "ram_gib": t["ram_gib"], "location": location,
        "available": t["available"], "eur_per_hour": hourly, "hourly_is_estimate": est,
        "started_hours_per_machine": started, "billed_machine_hours": started * count,
        "machine_hours_used": minutes / 60 * count, "monthly_cap_applied": capped,
        "eur": eur, "usd": eur * fx,
        "one_started_hour_usd": hourly * fx,
    }


# --------------------------------------------------------------------------------------------- vendors

def billed_minutes(granularity: str, minutes: float) -> float:
    if granularity == "minute":
        return float(max(1, math.ceil(minutes - EPS)))
    return float(minutes)


def describe_limits(p: dict) -> str:
    bits = []
    if p.get("max_vcpu"):
        bits.append(f"{p['max_vcpu']} vCPU")
    if p.get("max_ram_gib"):
        bits.append(f"{p['max_ram_gib']} GiB")
    out = " / ".join(bits)
    if p.get("max_session_minutes"):
        m = p["max_session_minutes"]
        cap = f"{m / 60:g} h" if m % 60 == 0 else f"{m} min"
        out = (out + ", " if out else "") + f"session cap {cap}"
    return out


def pick_plan(v: dict, vcpu: float, ram: float, minutes: float, hypothetical: bool):
    """The first plan whose limits fit. Returns (plan, hypothetical_reason, refusal_reason)."""
    plans = v["plan"]

    def fits(p, check_size=True):
        if check_size:
            if p.get("max_vcpu") and vcpu > p["max_vcpu"]:
                return False
            if p.get("max_ram_gib") and ram > p["max_ram_gib"]:
                return False
        return not (p.get("max_session_minutes") and minutes > p["max_session_minutes"])

    for p in plans:
        if fits(p):
            return p, None, None
    best = plans[-1]
    reason = f"exceeds the largest plan's limits ({describe_limits(best)})"
    if hypothetical:
        for p in plans:
            if fits(p, check_size=False):
                return p, "hypothetical: " + reason, None
        return best, "hypothetical: " + reason, None
    return None, None, reason


def pick_size(v: dict, vcpu: float, ram: float):
    sizes = v.get("size")
    if not sizes:
        return None, None
    fit = [s for s in sizes if s["vcpu"] >= vcpu and (s.get("ram_gib") is None or s["ram_gib"] >= ram)]
    if not fit:
        biggest = max(sizes, key=lambda s: (s["vcpu"], s.get("ram_gib") or 0))
        ram_text = f" / {biggest['ram_gib']} GiB" if biggest.get("ram_gib") else ""
        return None, f"largest listed size is {biggest['vcpu']} vCPU{ram_text}"

    def key(s):
        r = s["rate"] * (60 if s["rate_unit"] == "minute" else 1) if v["model"] == "fixed_rate" else 0
        return (r, s["vcpu"], s.get("ram_gib") or 0)

    return min(fit, key=key), None


def machine_cost(v: dict, vcpu: float, ram: float, minutes: float, util: float, size: dict | None, *, with_alt=False):
    """USD for one machine for `minutes`, under the vendor's rule. Returns (usd, alt_usd or None, creation_usd)."""
    bm = billed_minutes(v["granularity"], minutes)
    hours = bm / 60
    creation = v.get("creation_usd", 0.0)
    if v["model"] == "fixed_rate":
        per_min = size["rate"] / (60 if size["rate_unit"] == "hour" else 1)
        alt = None
        if with_alt and size.get("rate_alt"):
            alt = size["rate_alt"] / (60 if size["rate_unit"] == "hour" else 1) * bm
        return per_min * bm, alt, 0.0
    if v["model"] == "mcu":
        mcus = max(vcpu / v["vcpu_per_mcu"], ram / v["gib_per_mcu"])
        return mcus * v["mcu_usd"] * hours, None, 0.0
    # model == "resource"
    ev, er = (size["vcpu"], size["ram_gib"]) if size else (vcpu, ram)
    cpu_hr = rate_per_hour(v["cpu_rate"], v["cpu_rate_unit"], v.get("vcpus_per_core", 1))
    ram_hr = rate_per_hour(v["ram_rate"], v["ram_rate_unit"])
    cpu_factor = util if v["cpu_billing"] == "active" else 1.0
    usd = ev * cpu_hr * hours * cpu_factor + er * ram_hr * hours
    if size and size.get("disk_gb") and v.get("disk_rate"):
        usd += size["disk_gb"] * rate_per_hour(v["disk_rate"], v["disk_rate_unit"]) * hours
    return usd, None, creation


def vendor_quote(v: dict, *, vcpu: float, ram: float, minutes: float, count: int, util: float,
                 hypothetical: bool, monthly: bool, hetzner_usd: float | None) -> dict:
    out = {"id": v["id"], "vendor": v["vendor"], "product": v["product"], "verified": v["verified"],
           "unverified_items": list(v.get("unverified_items", [])), "granularity": v["granularity"],
           "cpu_billing": v.get("cpu_billing", "n/a"), "source": v["source"], "read_date": v["read_date"],
           "status": "ok", "reason": None, "cost_usd": None, "alt_cost_usd": None, "alt_label": None,
           "ratio_vs_hetzner": None, "plan": None, "plan_fee_usd_month": None, "plan_included_credit_usd": None,
           "total_usd": None, "billed_as": None, "hourly_usd": None, "break_even_minutes": None, "notes": []}
    plan, hypo_reason, refusal = pick_plan(v, vcpu, ram, minutes, hypothetical)
    if plan is None:
        out.update(status="not_offered", reason=refusal)
        return out
    size, size_refusal = pick_size(v, vcpu, ram)
    if v.get("size") and size is None:
        out.update(status="not_offered", reason=size_refusal)
        return out
    if hypo_reason:
        out["notes"].append(hypo_reason)
    if size and v.get("sizes_complete") is False and size is min(v["size"], key=lambda s: s["vcpu"]) and size["vcpu"] > vcpu:
        out["notes"].append("smaller sizes may exist but are not in the price list")
    per_machine, alt, creation = machine_cost(v, vcpu, ram, minutes, util, size, with_alt=True)
    cost = (per_machine + creation) * count
    out["cost_usd"] = cost
    if alt is not None:
        out["alt_cost_usd"] = (alt + creation) * count
        out["alt_label"] = "overage rate"
    # Hourly rate without per-creation fees: used for the break-even duration.
    hourly, _, _ = machine_cost(v, vcpu, ram, 60, util, size)
    out["hourly_usd"] = hourly
    if v["model"] == "fixed_rate":
        out["billed_as"] = size["name"]
    elif size:
        out["billed_as"] = f"{size['name']} ({size['vcpu']} vCPU / {size['ram_gib']} GiB)"
    elif v["model"] == "mcu":
        out["billed_as"] = f"{max(vcpu / v['vcpu_per_mcu'], ram / v['gib_per_mcu']):g} MCU"
    else:
        out["billed_as"] = f"{vcpu:g} vCPU + {ram:g} GiB"
    rule = {"second": "per second", "minute": "per minute"}[v["granularity"]]
    if v.get("cpu_billing") == "active":
        rule += f", CPU active {util:.0%}"
    out["billed_as"] += f", {rule}"
    fee = plan.get("fee_usd_month", 0)
    credit = plan.get("included_credit_usd", 0)
    out["plan"], out["plan_fee_usd_month"], out["plan_included_credit_usd"] = plan["name"], fee, credit
    if monthly:
        out["total_usd"] = fee + max(0.0, cost - credit)
    if hetzner_usd:
        out["ratio_vs_hetzner"] = cost / hetzner_usd
    return out


# --------------------------------------------------------------------------------------------- compute

def compute(prices: dict, *, vcpu: float, ram: float, minutes: float, count: int = 1, util: float = 1.0,
            location: str = "hel1", hetzner_type: str | None = None, hypothetical: bool = False,
            monthly: bool = False, only: list[str] | None = None) -> dict:
    fx = prices["meta"]["fx_usd_per_eur"]
    by_name = {t["name"]: t for t in prices["hetzner"]["type"]}
    if hetzner_type:
        if hetzner_type.lower() not in by_name:
            raise SystemExit(f"cost.py: unknown Hetzner type {hetzner_type!r}. Known: {', '.join(sorted(by_name))}")
        t = by_name[hetzner_type.lower()]
    else:
        t = nearest_hetzner(prices, vcpu, ram)
    hz = hetzner_quote(prices, t, location, minutes, count) if t else None
    ref = None
    cx = nearest_hetzner(prices, vcpu, ram, available_only=False, line="CX")
    if cx and not cx["available"] and (t is None or cx["name"] != t["name"]):
        ref = hetzner_quote(prices, cx, location, minutes, count)
    vendors = []
    for v in prices["vendor"]:
        if only and v["id"] not in only:
            continue
        q = vendor_quote(v, vcpu=vcpu, ram=ram, minutes=minutes, count=count, util=util,
                         hypothetical=hypothetical, monthly=monthly, hetzner_usd=hz["usd"] if hz else None)
        if q["status"] == "ok" and hz and q["hourly_usd"]:
            q["break_even_minutes"] = (60 * hz["one_started_hour_usd"] / q["hourly_usd"]
                                       if q["hourly_usd"] > hz["one_started_hour_usd"] else None)
            q["cheaper_at_every_duration"] = q["hourly_usd"] <= hz["one_started_hour_usd"]
        if q["status"] == "ok" and ref and ref["usd"] and q["cost_usd"] is not None:
            q["ratio_vs_cx_reference"] = q["cost_usd"] / ref["usd"]
        vendors.append(q)
    return {
        "inputs": {"vcpu": vcpu, "ram_gib": ram, "minutes": minutes, "count": count, "cpu_util": util,
                   "location": location, "hypothetical": hypothetical, "monthly": monthly},
        "prices": {"read_date": prices["meta"]["read_date"], "fx_usd_per_eur": fx, "fx_note": prices["meta"]["fx_note"],
                   "hetzner_includes_ipv4": prices["hetzner"]["includes_ipv4"]},
        "hetzner": hz, "hetzner_cx_reference": ref, "vendors": vendors,
    }


# --------------------------------------------------------------------------------------------- output

def money(x: float | None) -> str:
    if x is None:
        return "-"
    if abs(x) < 0.01:
        return f"${x:.4f}"
    if abs(x) < 1:
        return f"${x:.3f}"
    return f"${x:,.2f}"


def ratio_text(r: float | None) -> str:
    return "-" if r is None else f"{r:.1f}x"


def render_table(rows: list[list[str]], header: list[str]) -> str:
    cols = list(zip(header, *rows, strict=True)) if rows else [(h,) for h in header]
    widths = [max(len(c) for c in col) for col in cols]
    line = lambda r: "  ".join(c.ljust(w) for c, w in zip(r, widths, strict=False)).rstrip()  # noqa: E731
    return "\n".join([line(header), "  ".join("-" * w for w in widths)] + [line(r) for r in rows])


def render_text(res: dict) -> str:
    i, hz, ref = res["inputs"], res["hetzner"], res["hetzner_cx_reference"]
    fx = res["prices"]["fx_usd_per_eur"]
    lines = []
    count = f" x {i['count']} machines" if i["count"] != 1 else ""
    lines.append(f"Workload: {i['vcpu']:g} vCPU / {i['ram_gib']:g} GiB, {i['minutes']:g} minutes{count}"
                 f"  (CPU utilisation for active-CPU billing: {i['cpu_util']:.0%})")
    lines.append(f"List prices read {res['prices']['read_date']}. USD. EUR to USD at {fx} (an assumption).")
    lines.append("")
    rows = []
    if hz:
        est = " est." if hz["hourly_is_estimate"] else ""
        cap = ", monthly cap applied" if hz["monthly_cap_applied"] else ""
        rows.append([f"Hetzner {hz['type'].upper()} ({hz['location'].upper()}{est})",
                     f"{hz['started_hours_per_machine']} started h x {i['count']}, EUR {hz['eur_per_hour']:.4f}/h{cap}",
                     money(hz["usd"]), "1.0x", "-", ""])
    else:
        rows.append(["Hetzner", "no orderable type is large enough", "-", "-", "-", ""])
    for q in res["vendors"]:
        star = "" if q["verified"] else "*"
        name = f"{q['product']}{star}"
        if q["status"] != "ok":
            rows.append([name, "", "not offered", "-", "-", q["reason"]])
            continue
        fee = f"${q['plan_fee_usd_month']:g} ({q['plan']})" if q["plan_fee_usd_month"] else "none"
        notes = list(q["notes"])
        if q["alt_cost_usd"] is not None:
            notes.append(f"{q['alt_label']}: {money(q['alt_cost_usd'])}")
        if q.get("plan_included_credit_usd") and q["plan_fee_usd_month"]:
            notes.append(f"plan includes ${q['plan_included_credit_usd']:g} usage credit")
        rows.append([name, q["billed_as"], money(q["cost_usd"]), ratio_text(q["ratio_vs_hetzner"]), fee, "; ".join(notes)])
    lines.append(render_table(rows, ["Vendor / product", "Billed as", "Cost", "vs Hetzner", "Plan fee / month", "Notes"]))
    lines.append("")
    lines.append("Plan fees are not in the cost column. Free credits and included allowances are not deducted.")
    if i["monthly"]:
        lines.append("Monthly totals (plan fee plus usage above any included credit):")
        for q in res["vendors"]:
            if q["status"] == "ok":
                lines.append(f"  {q['product']}{'' if q['verified'] else '*'}: {money(q['total_usd'])}")
    if hz:
        lines.append(f"Hetzner prices include the primary IPv4 (EUR {0.5:.2f}/month); the hourly price shown on hetzner.com is "
                     f"assumed to be HEL1; NBG1 hourly is an estimate.")
    if ref:
        lines.append(f"Reference only: Hetzner {ref['type'].upper()} would cost {money(ref['usd'])} "
                     f"(EUR {ref['eur']:.3f}) but was flagged unavailable on {res['prices']['read_date']}, so it is not used.")
    unv = [q for q in res["vendors"] if not q["verified"]]
    if unv:
        lines.append("")
        lines.append("* Unverified inputs:")
        for q in unv:
            for item in q["unverified_items"]:
                lines.append(f"  * {q['product']}: {item}")
    if hz:
        lines.append("")
        lines.append(f"Break-even: one Hetzner started hour for one {hz['type'].upper()} costs {money(hz['one_started_hour_usd'])}. "
                     f"A run shorter than this is cheaper on the vendor (per machine, same size, list prices):")
        for q in res["vendors"]:
            if q["status"] != "ok":
                continue
            if q["break_even_minutes"] is not None:
                lines.append(f"  {q['product']}{'' if q['verified'] else '*'}: {q['break_even_minutes']:.1f} minutes")
            elif q.get("cheaper_at_every_duration"):
                lines.append(f"  {q['product']}: cheaper at every duration")
    return "\n".join(lines)


def docs_table(prices: dict) -> str:
    """The markdown table in docs/cost.md. A test compares the page to this output."""
    results = {k: compute(prices, vcpu=w["vcpu"], ram=w["ram"], minutes=w["minutes"], count=w["count"])
               for k, w in WORKLOADS.items()}
    keys = list(WORKLOADS)

    def cell_h(hz):
        return f"{hz['type'].upper()} {money(hz['usd'])}"

    rows = [["Hetzner orderable type, HEL1"] + [cell_h(results[k]["hetzner"]) for k in keys]]
    nb = {k: compute(prices, vcpu=w["vcpu"], ram=w["ram"], minutes=w["minutes"], count=w["count"], location="nbg1")
          for k, w in WORKLOADS.items()}
    rows.append(["Hetzner, NBG1 (hourly estimated)"] + [money(nb[k]["hetzner"]["usd"]) for k in keys])
    vendors = {v["id"]: v for v in prices["vendor"]}
    for vid in DOCS_VENDORS:
        cells = []
        for k in keys:
            q = next(x for x in results[k]["vendors"] if x["id"] == vid)
            if q["status"] != "ok":
                cells.append("not offered")
            else:
                cells.append(f"{money(q['cost_usd'])} ({ratio_text(q['ratio_vs_hetzner'])})")
        v = vendors[vid]
        label = f"{v['product']}{'' if v['verified'] else '*'}"
        rows.append([label] + cells)
    header = ["List price, USD"] + [f"{k}: {WORKLOADS[k]['label']}" for k in keys]
    out = ["| " + " | ".join(header) + " |", "|" + "|".join(["---"] * len(header)) + "|"]
    out += ["| " + " | ".join(r) + " |" for r in rows]
    return "\n".join(out)


# --------------------------------------------------------------------------------------------- result files

def read_result(path: str) -> dict:
    """A result.json from bench/run_workload.sh, or a log that contains its MOTEL_BENCH_RESULT line."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    for line in reversed(text.splitlines()):
        if line.startswith(RESULT_MARKER):
            return json.loads(line[len(RESULT_MARKER):])
    try:
        return json.loads(text)
    except ValueError:
        raise SystemExit(f"cost.py: {path} is neither a result.json nor a log with a {RESULT_MARKER.strip()} line")


def inputs_from_result(prices: dict, result: dict, timed_only: bool) -> dict:
    if result.get("schema", "").split("/")[0] != "motel-bench-result":
        raise SystemExit("cost.py: not a motel-bench-result file")
    seconds = (result.get("build_seconds", 0) + result.get("test_seconds", 0)) if timed_only else result["wall_seconds"]
    types = {t["name"]: t for t in prices["hetzner"]["type"]}
    t = types.get(str(result.get("server_type") or "").lower())
    if t:
        vcpu, ram = t["vcpu"], t["ram_gib"]
    else:
        vcpu = result["cpus"]
        ram = math.ceil(result["mem_total_kib"] / 1048576 - EPS)
    return {"vcpu": vcpu, "ram": ram, "minutes": seconds / 60, "type": t["name"] if t else None}


def run_summary(res: dict, result: dict, base: dict, billed_eur: float | None) -> str:
    hz = res["hetzner"]
    kind = result.get("server_type") or "an unnamed machine"
    where = "" if not result.get("dry_run") else " (DRY RUN: a stand-in command, not the real workload)"
    lines = [f"This run took {base['minutes']:.1f} minutes on {kind}{where}."]
    if hz:
        lines.append(f"At Hetzner list price that is {hz['started_hours_per_machine']} started hour(s) of {hz['type'].upper()} "
                     f"({hz['location'].upper()}): EUR {hz['eur']:.4f}, USD {hz['usd']:.4f}.")
    if billed_eur is not None:
        lines.append(f"motel's own estimate for the cabin: EUR {billed_eur:.4f} net (--billed-eur; includes boot time and "
                     f"anything else billed).")
    lines.append(f"The same vCPU ({base['vcpu']:g}), RAM ({base['ram']:g} GiB) and minutes at each vendor's list price are in the "
                 f"table below.")
    lines.append("This is a list-price calculation, not a run on those vendors. Their CPUs and storage may be faster or "
                 "slower, so the minutes could differ there.")
    if result.get("status") not in (None, "ok"):
        lines.append(f"Note: the run status was {result.get('status')!r}, so the minutes may not be a full run.")
    return "\n".join(lines)


# --------------------------------------------------------------------------------------------- main

def parse_args(argv):
    p = argparse.ArgumentParser(prog="cost.py", description="Price one workload under each vendor's billing rule (list prices).")
    p.add_argument("--vcpu", type=float, help="vCPUs per machine")
    p.add_argument("--ram", type=float, help="GiB of RAM per machine")
    p.add_argument("--minutes", type=float, help="duration of each machine, in minutes")
    p.add_argument("--count", type=int, default=1, help="number of machines running this long (default 1)")
    p.add_argument("--cpu-util", type=float, default=1.0,
                   help="average CPU use from 0 to 1, for vendors that bill active CPU (default 1.0)")
    p.add_argument("--location", choices=["hel1", "nbg1"], default="hel1",
                   help="Hetzner location (default hel1; nbg1 hourly prices are estimates)")
    p.add_argument("--hetzner-type", help="use this Hetzner type instead of the nearest orderable one (for example cpx42)")
    p.add_argument("--hypothetical", action="store_true",
                   help="also price vendors whose size or session limit does not allow the request, and mark the rows")
    p.add_argument("--monthly", action="store_true", help="the workload is one month of usage: also show totals with plan fees")
    p.add_argument("--only", help="comma-separated vendor ids to show (see prices.toml)")
    p.add_argument("--workload", choices=sorted(WORKLOADS), help="a workload from the research note (A, B, C or D)")
    p.add_argument("--from-result", metavar="FILE", help="result.json (or a log containing its result line) from run_workload.sh")
    p.add_argument("--timed-only", action="store_true", help="with --from-result: use build plus test time, not the whole script")
    p.add_argument("--billed-eur", type=float, help="with --from-result: the cabin cost motel reported, to print beside the list price")
    p.add_argument("--prices", default=str(PRICES_FILE), help="path to prices.toml")
    p.add_argument("--json", action="store_true", help="print JSON instead of a table")
    p.add_argument("--docs-table", action="store_true", help="print the markdown table used in docs/cost.md and exit")
    return p.parse_args(argv)


def main(argv=None) -> int:
    a = parse_args(sys.argv[1:] if argv is None else argv)
    prices = load_prices(Path(a.prices))
    if a.docs_table:
        print(docs_table(prices))
        return 0
    only = [s.strip() for s in a.only.split(",")] if a.only else None
    result = base = None
    kw = dict(count=a.count, util=a.cpu_util, location=a.location, hetzner_type=a.hetzner_type,
              hypothetical=a.hypothetical, monthly=a.monthly, only=only)
    if a.from_result:
        result = read_result(a.from_result)
        base = inputs_from_result(prices, result, a.timed_only)
        kw["hetzner_type"] = a.hetzner_type or base["type"]
        args = dict(vcpu=base["vcpu"], ram=base["ram"], minutes=base["minutes"])
    elif a.workload:
        w = WORKLOADS[a.workload]
        args = dict(vcpu=w["vcpu"], ram=w["ram"], minutes=w["minutes"])
        kw.update(count=w["count"], monthly=w["monthly"], hypothetical=True)
        print(f"Workload {a.workload}: {w['label']} (limits ignored where a vendor cannot offer the size: --hypothetical)\n",
              file=sys.stderr if a.json else sys.stdout)
    elif None in (a.vcpu, a.ram, a.minutes):
        print("cost.py: give --vcpu, --ram and --minutes, or --workload, or --from-result", file=sys.stderr)
        return 2
    else:
        args = dict(vcpu=a.vcpu, ram=a.ram, minutes=a.minutes)
    if not 0 <= a.cpu_util <= 1 or args["minutes"] <= 0 or args["vcpu"] <= 0 or args["ram"] <= 0 or kw["count"] < 1:
        print("cost.py: --cpu-util must be 0 to 1; --vcpu, --ram, --minutes and --count must be positive", file=sys.stderr)
        return 2
    res = compute(prices, **args, **kw)
    if result is not None:
        res["run"] = {"result": result, "minutes": base["minutes"], "billed_eur_reported_by_motel": a.billed_eur}
    if a.json:
        print(json.dumps(res, indent=2, sort_keys=True))
        return 0
    if result is not None:
        print(run_summary(res, result, base, a.billed_eur))
        print()
    print(render_text(res))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
