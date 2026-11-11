"""How each cloud bills, and the arithmetic that follows from it. Standard library only.

Some clouds bill a server or a volume per STARTED hour, counted from the moment the provider created it. Deleting
such a resource five minutes into a paid hour saves nothing, and deleting it one minute after the hour turns over costs
a whole extra hour. For these clouds motel keeps an idle resource until a *release window* just before the next billed
hour starts, so that a resource that is going to be released is released at the end of an hour it has paid for already.

This module is the one place that states the billing granularity of every provider. A cloud whose granularity could
not be confirmed from its own pricing documentation is ``UNKNOWN``, and unknown means no alignment at all: the resource
is released as soon as the rest of motel would release it (the behaviour before this module existed).

What the time arithmetic never does: it never extends a promise. A TTL, a maximum lifetime, an explicit ``checkout``
and ``cabin run`` are not delayed by anything here; callers pass the deadline in and the answer is "release now" when
waiting would pass it.

Sources are the providers' own pages, read 10 and 11 November 2026. Each entry names the sentence that decided it.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import timedelta

STARTED_HOUR = "started_hour"   # every started hour is billed in full; the clock starts at the provider's created time
PER_SECOND = "per_second"       # per second after a minimum; nothing to align to
UNKNOWN = "unknown"             # not confirmed from an official source: no alignment

HOUR = timedelta(hours=1)
DEFAULT_MARGIN = timedelta(minutes=6)      # the release window ends this long before the next billed hour starts
MIN_MARGIN, MAX_MARGIN = timedelta(minutes=4), timedelta(minutes=30)
REAP_INTERVAL = timedelta(minutes=10)      # the housekeeping timer (motel.installer.INTERVAL_MINUTES)
REAP_SLACK = timedelta(minutes=1)          # timer accuracy plus a pass that starts a little late
PROVIDER_EARLY_LIMIT = timedelta(minutes=5)  # a provider `created` this much before our own record of asking is not believed

# The clock anchor of every entry is the provider's `created` time of the resource. That the provider's hour starts there is
# motel's ASSUMPTION: the billing pages quoted below say billing begins when the product is made available, not which
# timestamp the API reports. It is made safe by taking the EARLIEST of the provider's time and the local time at which the
# create response arrived (see ``anchor``), so a skewed provider clock can only move the window earlier.
ANCHOR = "created"


@dataclass(frozen=True)
class Rule:
    """Billing of one kind of resource at one provider."""

    granularity: str
    source: str            # the official page that states it (empty for a provider with no bill)
    note: str = ""         # the deciding sentence, or why it is unknown
    minimum_s: int = 0     # per_second clouds: the minimum charge
    inferred: bool = False  # True when the cited page is a general billing page, not a statement about this resource

    @property
    def aligned(self) -> bool:
        """True when releasing early inside a paid hour saves nothing."""
        return self.granularity == STARTED_HOUR


@dataclass(frozen=True)
class ProviderBilling:
    servers: Rule
    volumes: Rule


_NONE = Rule(UNKNOWN, "", "not a cloud: no bill")
_HZ_SERVER = Rule(
    STARTED_HOUR, "https://docs.hetzner.com/cloud/billing/faq/",
    '"We always round up the hourly usage of a server." and "If you create a server just for a few minutes, '
    'we will still bill you for one whole hour."')
# INFERRED: no Hetzner page says "volumes are billed per started hour". The volume page says only "billed hourly"; the
# rounding comes from Hetzner's general billing page, which covers products in general.
_HZ_VOLUME = Rule(
    STARTED_HOUR, "https://docs.hetzner.com/general/billing-and-account-management/billing-at-hetzner/billing-system-hetzner/",
    '(inferred from a general billing page) "We round up partial hours." and "Billing begins when we give you access to '
    'a product." Volumes: "Volumes have a monthly price cap and are billed hourly." '
    "(https://docs.hetzner.com/cloud/volumes/overview)", inferred=True)
_LINODE = Rule(
    STARTED_HOUR, "https://www.akamai.com/cloud/pricing",
    '"You are charged the hourly rate for a service (rates get rounded up to the nearest hour)."')
_LINODE_VOLUME = Rule(
    STARTED_HOUR, "https://www.akamai.com/cloud/pricing/block-storage",
    '"You are charged the hourly rate for a service (rates get rounded up to the nearest hour)."')
_PER_SECOND_60 = "per second with a 60 second minimum"

# Keyed by the provider's ``name``. Cabins (servers) exist on Hetzner only; the other server rules are recorded for
# completeness and for the day a cloud gains cabins.
BILLING: dict = {
    "hetzner": ProviderBilling(_HZ_SERVER, _HZ_VOLUME),
    "digitalocean": ProviderBilling(
        Rule(PER_SECOND, "https://docs.digitalocean.com/products/droplets/details/pricing/",
             '"Bundled-plan CPU Droplets are billed per second with a minimum charge of 60 seconds or $0.01, '
             'whichever is higher."', minimum_s=60),
        Rule(UNKNOWN, "https://docs.digitalocean.com/products/volumes/details/pricing/",
             '"Charges accrue hourly for as long as the volume exists." Whether a partial hour is rounded up is not '
             "stated.")),
    "linode": ProviderBilling(_LINODE, _LINODE_VOLUME),
    "vultr": ProviderBilling(
        Rule(STARTED_HOUR, "https://docs.vultr.com/support/platform/billing/how-am-i-billed-for-my-servers",
             '"Vultr bills all servers on an hourly basis." and "The minimum billing unit is one hour, and hourly '
             'charges begin when a server is deployed."'),
        Rule(UNKNOWN, "https://docs.vultr.com/support/platform/billing/how-am-i-billed-for-my-servers",
             "the page does not address block storage")),
    "scaleway": ProviderBilling(
        Rule(UNKNOWN, "https://www.scaleway.com/en/docs/instances/reference-content/understanding-instance-pricing/",
             "the page states hourly billing; its rounding of a partial hour could not be read (HTTP 403)"),
        Rule(UNKNOWN, "https://www.scaleway.com/en/docs/instances/reference-content/understanding-instance-pricing/",
             "hourly billing from creation to deletion; rounding of a partial hour could not be read (HTTP 403)")),
    "aws": ProviderBilling(
        Rule(PER_SECOND, "https://aws.amazon.com/ec2/pricing/on-demand/",
             '"pay for compute capacity by the hour or second (minimum of 60 seconds)"', minimum_s=60),
        Rule(PER_SECOND, "https://aws.amazon.com/ebs/pricing/",
             '"Provisioned storage for gp2 volumes will be billed in per-second increments, with a 60-second '
             'minimum."', minimum_s=60)),
    "gcp": ProviderBilling(
        Rule(PER_SECOND, "https://cloud.google.com/blog/products/gcp/extending-per-second-billing-in-google",
             '"per-second billing, with a one minute minimum, to Compute Engine"', minimum_s=60),
        Rule(PER_SECOND, "https://cloud.google.com/blog/products/gcp/extending-per-second-billing-in-google",
             '"Persistent Disks, which has been billed by the second since its launch in 2013"')),
    "azure": ProviderBilling(
        Rule(UNKNOWN, "https://azure.microsoft.com/pricing/details/virtual-machines/linux/",
             "no official statement of the granularity could be confirmed"),
        Rule(UNKNOWN, "https://azure.microsoft.com/pricing/details/managed-disks/",
             '"billed on an hourly basis" and "pro-rated on an hourly basis using the monthly price"; a started hour '
             "is not stated")),
    "local": ProviderBilling(_NONE, _NONE),
    "fake": ProviderBilling(_NONE, _NONE),
}

UNKNOWN_BILLING = ProviderBilling(Rule(UNKNOWN, "", "provider not in the billing table"),
                                  Rule(UNKNOWN, "", "provider not in the billing table"))


def provider_billing(name: str) -> ProviderBilling:
    return BILLING.get(name, UNKNOWN_BILLING)


def volume_rule(provider) -> Rule:
    """The rule for a volume provider. A provider object may carry its own ``volume_billing`` (the in-memory fake does,
    for tests); everyone else is looked up by name."""
    own = getattr(provider, "volume_billing", None)
    return own if isinstance(own, Rule) else provider_billing(getattr(provider, "name", "")).volumes


# ---------------------------------------------------------------------------------------------------- the clock

def anchor(provider_times, received_times, record, now):
    """(clock anchor, source) of a resource: the EARLIEST of the provider's own ``created`` times and the local times at
    which the create responses arrived. A resource cannot have been created after we heard about it, so a provider clock
    that runs ahead (or a time in the future) is replaced by the arrival time, and one that runs behind keeps the hour
    early. Both can only move the window earlier, which is the safe side: it never delays a release past the real hour.

    ``provider_times`` and ``received_times`` are iterables of aware datetimes (naive ones, and None, are ignored: a time
    of unknown zone is no clock). A provider time more than ``PROVIDER_EARLY_LIMIT`` before ``record`` (the controller's
    own time before it asked for the resource) is not believed either. ``record`` is the last resort. The anchor is
    clamped to ``now``."""
    def aware(t):
        return t if t is not None and getattr(t, "tzinfo", None) is not None else None
    record = aware(record)
    provider = [t for t in map(aware, provider_times or ()) if t is not None
                and (record is None or t >= record - PROVIDER_EARLY_LIMIT)]
    received = [t for t in map(aware, received_times or ()) if t is not None]
    if provider or received:
        earliest = min(provider + received)
        return min(earliest, now), "provider" if earliest in provider else "received"
    if record is not None:
        return min(record, now), "record"
    return None, "none"


def paid_until(start, now):
    """The end of the billed hour ``now`` falls in. At an exact boundary a new hour has started."""
    elapsed = max(timedelta(0), now - start)
    return start + (int(elapsed // HOUR) + 1) * HOUR


def billed_hours(start, end) -> int:
    """Started hours from ``start`` to ``end`` (at least one: a resource that exists has started an hour)."""
    elapsed = max(timedelta(0), end - start)
    return max(1, math.ceil(elapsed / HOUR))


@dataclass(frozen=True)
class Window:
    """The release window of the billed hour ``now`` is in. Release when ``release`` is true."""

    paid_until: object
    opens: object          # a pass that runs before this may wait: the next pass still lands in the window
    closes: object         # paid_until minus the margin
    release: bool
    why: str               # "window" | "next_pass_too_late" | "deadline" | "wait"


def release_window(start, now, *, margin=DEFAULT_MARGIN, interval=REAP_INTERVAL, slack=REAP_SLACK,
                   deadline=None) -> Window:
    """May a resource that could be released now wait for the end of its paid hour?

    The window is the last ``interval`` before ``paid_until - margin``: one housekeeping pass always lands in it, and
    the release it starts finishes before the next billed hour. A pass inside the window (or in the margin after it,
    which is still the paid hour) releases. A pass before the window waits, unless the next pass (``interval`` plus
    ``slack`` from now) would come after the window closed: then it releases now, so a resource is never carried
    into a new billed hour. ``deadline`` is a promise (a TTL): the resource is never kept past it, so a pass also
    releases when the next one would come after the deadline."""
    end = paid_until(start, now)
    closes = end - margin
    opens = closes - interval
    if now >= opens:
        return Window(end, opens, closes, True, "window")
    if now + interval + slack > closes:
        return Window(end, opens, closes, True, "next_pass_too_late")
    if deadline is not None and now + interval + slack > deadline:
        return Window(end, opens, closes, True, "deadline")
    return Window(end, opens, closes, False, "wait")
