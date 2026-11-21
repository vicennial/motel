"""Human-readable output for the cabin verbs. ``--json`` bypasses this and prints the document itself."""

from __future__ import annotations

import json


def money_line(offer) -> str:
    return f"EUR {offer.get('total_hourly_net_eur')}/h net"


def billing_line(c) -> str:
    """Started hours and the estimate they give, or nothing when the provider's billing is not hour-aligned."""
    hours = (c.get("billing") or {}).get("billed_hours")
    cost = c.get("cost_estimate_net_eur")
    if hours is None or cost is None:
        return ""
    return f"est. EUR {cost} net for {hours} started hour{'' if hours == 1 else 's'}"


def sealed_text(c) -> str:
    """A sealed (idle) cabin admits no new work and is deleted at the end of its paid hour."""
    by = c.get("release_by")
    return f"sealed (idle); deleted by {by}" if by else "sealed (idle); deleted at the next reaper pass"


def cabin_line(c) -> str:
    left = c.get("time_left_s")
    if c.get("state") == "released":
        when = "released"
    elif left is None:
        when = ""
    elif left >= 0:
        when = f"{left // 3600}h{(left % 3600) // 60:02d}m left"
    else:
        when = "expired"
    disk = f" disk {c['disk']['size_gb']}G" if c.get("disk") else ""
    est = billing_line(c)
    return f"{c['lease_id']:<44} {c['state']:<16} {when:<12} {c.get('address') or '-':<16}{disk}" \
        + (f"  {est}" if est and c.get("state") != "released" else "") + (f"  {sealed_text(c)}" if c.get("sealed") else "")


def render_cabin(c) -> str:
    offer = c.get("offer") or {}
    lines = [(f"cabin {c['lease_id']} {c['state']}: {offer.get('server_type')} in {offer.get('location')}, "
             f"{money_line(offer)}, access {(c.get('access') or {}).get('mode')}")]
    if c.get("state") == "released":
        if c.get("released_at"):
            lines.append(f"released {c['released_at']}.")
    else:
        lines.append(f"expires {c.get('expires_at')}; deleted after {c.get('destroy_after')}, even if busy. "
                     "Deletion is final: copy results out first.")
    est = billing_line(c)
    if est:
        paid = (c.get("billing") or {}).get("paid_until")
        lines.append(f"{est} (the provider bills every started hour from creation"
                     + (f"; the current hour is paid until {paid}" if paid and c.get("state") != "released" else "")
                     + "; deleting early inside a paid hour refunds nothing)")
    if c.get("sealed"):
        lines.append(sealed_text(c))
    if offer.get("image_type") == "snapshot" and offer.get("image_name"):
        lines.append(f"image: {offer['image_name']} (baked on this host, id {offer.get('image_id')})")
    if c.get("disk"):
        lines.append(f"disk: {c['disk']['size_gb']} GB at {c['disk']['guest_path']} (~/disk), same lifetime")
    for w in c.get("warnings") or []:
        lines.append(f"warning: {w}")
    return "\n".join(lines)


def render(verb: str, doc: dict) -> str:
    if verb == "doctor":
        return "\n".join(f"{'ok  ' if c['ok'] else 'FAIL'} {c['check']}{': ' + c['detail'] if c['detail'] else ''}"
                         for c in doc["checks"])
    if verb == "images":
        if not doc["images"]:
            return "no baked images on this host (an operator makes one with: sudo motel-desk tour bake NAME --script FILE)"
        return "\n".join(f"{i['name']:<40} id {i['id']:<12} {i.get('created') or '-'}"
                         + (f"  {i['image_size_gb']:g} GB" if isinstance(i.get("image_size_gb"), (int, float)) else "")
                         for i in doc["images"])
    if verb == "reap" and doc.get("note"):
        return doc["note"]
    if verb == "reap":
        lines = [f"provider inventory: {doc['provider']['inventory']}"
                 + (f" ({doc['provider']['error']})" if doc["provider"]["error"] else "")]
        for key in ("planned", "held", "actions", "warnings", "errors", "orphans", "busy", "deferred"):
            lines.extend(f"{key}: {v if isinstance(v, str) else json.dumps(v, sort_keys=True)}"
                         for v in doc.get(key, []))
        if not doc["apply"]:
            lines.append("dry run: nothing changed (use --apply)")
        return "\n".join(lines)
    if verb in ("ls", "status"):
        prov = doc["provider"]
        lines = []
        if prov["inventory"] != "complete":
            lines.append(f"WARNING: provider inventory unavailable ({prov['error']}); local records only, "
                         "cannot confirm that no server exists")
        for c in doc["cabins"]:
            pend = {k: v for k, v in (c.get("cleanup") or {}).items() if v not in ("done", "not_created")}
            extra = f" cleanup {json.dumps(pend)}" if c.get("state") == "cleanup_pending" else ""
            lines.append(cabin_line(c) + extra)
        for o in doc["orphans"]:
            lines.append(f"ORPHAN {o['kind']} {o['id']} lease={o['lease']}: {o['note']}")
        hidden = doc.get("orphan_count", len(doc["orphans"])) - len(doc["orphans"])
        if hidden:
            lines.append(f"WARNING: {hidden} orphan resource(s) need operator review; details restricted")
        pending = (doc.get("global") or {}).get("live_or_pending_cabins", 0)
        if not doc["cabins"] and pending:
            lines.append(f"{pending} live or pending cabin(s) exist; details restricted")
        if not doc["cabins"] and doc.get("verified_empty") is True:
            lines.append("no cabins (provider inventory complete)")
        return "\n".join(lines)
    if verb == "setup":
        return "\n".join(f"{k}: {v}" for k, v in doc.items() if k not in ("schema", "ok"))
    return render_cabin(doc["cabin"])
