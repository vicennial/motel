"""Receipts: what a cabin run or a tour cost, in one stable block.

A receipt is built from fields the front desk already reports (the offer, the lease times, ``billing.billed_hours`` and
``cost_estimate_net_eur``). It states those numbers; it never prices anything itself, so there is one cost model: the
controller's started-hour arithmetic (``motel.billing``, ``Controller.estimated_cost``). Prices are the provider's list
price, net of VAT. Standard library only.

``cabin_receipt(view)`` and ``tour_receipt(tour)`` return the dict that ``--json`` shows under the ``receipt`` key.
``render_cabin_receipt`` and ``render_tour_receipt`` return the text lines for stderr.
"""

from __future__ import annotations

import decimal
from datetime import datetime

TERMINAL_TOUR = ("done", "failed", "cancelled")
LABEL = 14

BASIS_STARTED = "started_hours"     # every started hour is billed in full
BASIS_PRORATED = "prorated"         # the provider does not round up to the hour: the estimate follows the time used


def _time(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except (TypeError, ValueError):
        return None


def _dec(value):
    try:
        return decimal.Decimal(str(value))
    except (decimal.InvalidOperation, TypeError, ValueError):
        return None


def _money(value) -> str | None:
    number = _dec(value)
    return None if number is None else str(number.quantize(decimal.Decimal("0.0001")))


def cabin_receipt(view: dict, now=None) -> dict | None:
    """The receipt of one cabin, from the desk's cabin record. ``None`` when the record has no offer (no price known).

    ``now`` (an aware datetime) is the end of the wall time of a cabin that is not released yet."""
    offer = (view or {}).get("offer") or {}
    if offer.get("total_hourly_net_eur") is None:
        return None
    billing = view.get("billing") or {}
    start = _time(view.get("created_at"))
    end = _time(view.get("released_at")) or now
    wall = None
    if start is not None and end is not None:
        try:
            wall = max(0, int((end - start).total_seconds()))
        except TypeError:
            wall = None
    released = view.get("state") == "released"
    hours = billing.get("billed_hours")
    return {
        "server_type": offer.get("server_type"), "vcpus": offer.get("vcpus"), "memory_gb": offer.get("memory_gb"),
        "location": offer.get("location"), "disk_gb": (view.get("disk") or {}).get("size_gb"),
        "wall_seconds": wall, "billed_hours": hours, "basis": BASIS_STARTED if hours is not None else BASIS_PRORATED,
        "hourly_net_eur": _money(offer["total_hourly_net_eur"]),
        "estimated_total_net_eur": _money(view.get("cost_estimate_net_eur")) if view.get("cost_estimate_net_eur") is not None else None,
        "currency": "EUR", "final": released, "servers_left": 0 if released else 1,
    }


def _wall(seconds) -> str:
    if seconds is None:
        return "unknown"
    minutes, secs = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}h {minutes:02d}m {secs:02d}s"
    return f"{minutes}m {secs:02d}s" if minutes else f"{secs}s"


def _spec(r: dict) -> str:
    parts = [str(r.get("server_type") or "unknown type")]
    size = [x for x in (f"{r['vcpus']} vCPU" if r.get("vcpus") else None,
                        f"{r['memory_gb']:g} GB RAM" if isinstance(r.get("memory_gb"), (int, float)) and r.get("memory_gb") else None)
            if x]
    if size:
        parts.append("(" + ", ".join(size) + ")")
    if r.get("location"):
        parts.append(f"in {r['location']}")
    return " ".join(parts)


def _hours(hours) -> str:
    return f"{hours} started hour{'' if hours == 1 else 's'}"


def render_cabin_receipt(r: dict, name: str | None = None) -> list:
    """The receipt as stderr lines: a header, then one aligned row per fact."""
    if not r:
        return []
    price = f"EUR {r['hourly_net_eur']}/h net of VAT (provider list price, server + IPv4" \
            + (f" + {r['disk_gb']} GB disk" if r.get("disk_gb") else "") + ")"
    if r.get("billed_hours") is not None:
        billed = _hours(r["billed_hours"]) + " (a started hour is billed in full; deleting early refunds nothing)"
    else:
        billed = "by the time used (this provider does not round up to the hour)"
    total = "unknown" if r.get("estimated_total_net_eur") is None else f"EUR {r['estimated_total_net_eur']} net"
    left = "0" if r.get("servers_left") == 0 else f"{r.get('servers_left', 1)} (checkout is not confirmed: it may still be billing)"
    rows = [("server", _spec(r)), ("wall time", _wall(r.get("wall_seconds"))), ("billed", billed), ("price", price),
            ("est. total", total), ("servers left", left)]
    head = (f"receipt for cabin {name}" if name else "receipt") + (" (released)" if r.get("final") else "")
    return [f"motel: {head}"] + [f"  {label.ljust(LABEL)}{value}" for label, value in rows]


def tour_receipt(tour: dict) -> dict:
    """The receipt of a tour, from the tour record the desk reports (``guests[].result``, ``budget``).

    The servers total is the tour budget's ``spent_eur``: it settles every attempt of every guest (a retried guest
    included) with the same cost the controller computed for each cabin."""
    guests, usd_total, metered, unmetered, hours_total, left = [], decimal.Decimal(0), 0, 0, 0, 0
    for g in tour.get("guests") or []:
        res = g.get("result") or {}
        cab = res.get("cabin") or {}
        off = res.get("auth") == "own-login" or res.get("metered") is False
        usd = None if off or res.get("usd") is None else _dec(res["usd"])
        if off:
            unmetered += 1
        elif usd is not None:
            metered += 1
            usd_total += usd
        if isinstance(cab.get("billed_hours"), int):
            hours_total += cab["billed_hours"]
        if g.get("cleanup", "deleted") != "deleted":
            left += 1
        guests.append({
            "id": g.get("id"), "status": g.get("status"), "attempts": g.get("attempts"),
            "server_type": cab.get("server_type"), "vcpus": cab.get("vcpus"), "memory_gb": cab.get("memory_gb"),
            "wall_seconds": cab.get("wall_seconds"), "billed_hours": cab.get("billed_hours"),
            "hourly_net_eur": cab.get("hourly_net_eur"), "cost_net_eur": _money(res.get("cost_eur")) if res.get("cost_eur") is not None else None,
            "model_usd": None if usd is None else _money(usd),
            "model_metered": False if off else (True if usd is not None else None)})
    budget = tour.get("budget") or {}
    return {
        "final": tour.get("status") in TERMINAL_TOUR, "currency": "EUR", "guests": guests,
        "servers": {"estimated_total_net_eur": _money(budget.get("spent_eur")) if budget.get("spent_eur") is not None else None,
                    "billed_hours": hours_total, "max_eur": _money(budget.get("max_eur")) if budget.get("max_eur") is not None else None},
        "model": {"usd": _money(usd_total) if metered else None, "metered_guests": metered, "unmetered_guests": unmetered},
        "servers_left": left,
    }


def render_tour_receipt(r: dict, tour_id: str | None = None) -> list:
    """Stderr lines: a per-guest table, then the totals for servers and for model use."""
    if not r:
        return []
    rows = []
    for g in r.get("guests") or []:
        spec = _spec({"server_type": g.get("server_type"), "vcpus": g.get("vcpus"), "memory_gb": g.get("memory_gb")}) \
            if g.get("server_type") else "no server booked"
        cost = f"EUR {g['cost_net_eur']}" if g.get("cost_net_eur") is not None else "-"
        if g.get("model_metered") is False:
            model = "not metered"
        else:
            model = f"USD {g['model_usd']}" if g.get("model_usd") is not None else "-"
        extra = f"  ({g['attempts']} attempts)" if isinstance(g.get("attempts"), int) and g["attempts"] > 1 else ""
        rows.append((str(g.get("id")), spec, _wall(g.get("wall_seconds")), "-" if g.get("billed_hours") is None else str(g["billed_hours"]),
                     cost, model + extra))
    head = ("guest", "server", "wall time", "hours", "servers (est.)", "model use")
    widths = [max(len(row[i]) for row in [head, *rows]) for i in range(5)]
    lines = [f"motel: receipt for tour {tour_id}" if tour_id else "motel: receipt for the tour"]
    for row in [head, *rows]:
        lines.append("  " + "  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row[:5])) + "  " + row[5])
    srv, model = r.get("servers") or {}, r.get("model") or {}
    total = "unknown" if srv.get("estimated_total_net_eur") is None else f"EUR {srv['estimated_total_net_eur']} net"
    lines.append(f"  {'servers'.ljust(LABEL)}{total} ({_hours(srv.get('billed_hours', 0))}; provider list price, net of VAT)")
    if model.get("usd") is not None:
        use = f"USD {model['usd']} through the concierge ({model['metered_guests']} guest{'' if model['metered_guests'] == 1 else 's'} metered)"
    else:
        use = "none metered"
    if model.get("unmetered_guests"):
        use += f"; {model['unmetered_guests']} guest(s) on your own login are not metered"
    lines.append(f"  {'model use'.ljust(LABEL)}{use}")
    left = r.get("servers_left")
    lines.append(f"  {'servers left'.ljust(LABEL)}" + ("0" if left == 0 else f"{left} (a server may still be billing: motel tour status)"))
    return lines
