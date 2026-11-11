"""motel concierge: the credential proxy for tours (see ``docs/concierge.md``).

Guests never hold a real API key. Each attempt gets a placeholder token; the concierge swaps it for the real key for
the allowed upstreams only, meters the cost and keeps the append-only ledger.

Seams for the other parts:

- ``motel.concierge.settings.parse_tours_section(table: dict, *, strict: bool = False) -> TourConfig``
- ``motel.concierge.admin.ConciergeClient(path).issue/revoke/usage/health``
- ``motel.concierge.install.unit_text(port, systemd_version=)``, ``socket_unit_text()``, ``layout()``, ``unit_changes(read, systemd_version)``,
  ``after_unit_changes(changes)``, ``unit_problems(text)``, ``detect_systemd_version()``
- ``motel.testing.FakeConcierge`` (in memory, same methods as the client)
"""

from motel.concierge.admin import ConciergeClient
from motel.concierge.settings import BindingConfig, TourConfig, parse_tours_section

__all__ = ["BindingConfig", "ConciergeClient", "TourConfig", "parse_tours_section"]
