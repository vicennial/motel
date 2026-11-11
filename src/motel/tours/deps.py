"""The import seam to the two sibling parts of the tours feature.

The engine reaches the concierge (credential proxy) and the guest side (runner, egress profile, image bake)
only through this module, and always as ``deps.<name>(...)`` so a test can replace one function. Each function
imports the real module on first use. A missing part is a clear "not installed" error, never a silent fallback:
the runtime code paths carry no test doubles. The doubles live in ``tests/tours/doubles.py``.

What the engine expects of each part (see ``docs/design/tours-contract.md``):

* ``motel.concierge.admin.ConciergeClient()``: ``issue / revoke / usage / health``.
* ``motel.concierge.settings.parse_tours_section(table, *, strict=False)``: the validated ``[tours]`` table (a
  ``TourConfig``). Keys that belong to the engine, such as ``[tours.own_login]``, are ignored by it and read by
  :mod:`motel.tours.config`.
* ``motel.tours.guest.userdata_extras(egress_hosts, resolvers, *, concierge_port, own_login)``: a ``GuestExtras``
  for a tour guest; the engine hands it to ``Controller.checkin(userdata_extras=...)`` unchanged.
* ``motel.tours.guest.runner_files()`` / ``runner_argv()``: the runner and its adapters (package data), copied to
  the guest, and the command that runs them as the ``guest`` user.
* ``motel.tours.bake``: ``bake(controller, setup_script_path, name, ...)``, ``images(controller)``,
  ``delete(controller, image_id)``.
"""

from __future__ import annotations

import importlib

from motel.tours.errors import NotSetUp


def _module(name: str, what: str):
    try:
        return importlib.import_module(name)
    except ModuleNotFoundError as exc:
        if exc.name is None or not name.startswith(exc.name):
            raise
        raise NotSetUp(f"tours need {what}, which is not part of this build of motel ({name})",
                       "motel doctor") from None


def concierge_client():
    """A client for the concierge admin socket."""
    return _module("motel.concierge.admin", "the concierge").ConciergeClient()


def parse_tours_section(table, *, strict: bool = False):
    """Validate the ``[tours]`` table through the concierge part (one validation path)."""
    return _module("motel.concierge.settings", "the concierge").parse_tours_section(table, strict=strict)


def userdata_extras(*, hosts, dns_resolvers, concierge_port, own_login: bool = False):
    """The egress profile and guest setup for a tour guest: a ``motel.cabins.userdata.GuestExtras``.

    ``concierge_port=None`` (or ``own_login``) opens no reverse forward; ``own_login`` adds api.anthropic.com."""
    return _module("motel.tours.guest", "the guest part").userdata_extras(
        list(hosts), list(dns_resolvers), concierge_port=concierge_port, own_login=own_login)


def runner_files() -> dict:
    """``runner.py`` and ``adapters.py`` as bytes, to be copied together into the guest's runner directory."""
    return _module("motel.tours.guest", "the guest part").runner_files()


def runner_argv() -> list:
    """The command (argv) that runs the runner as the unprivileged ``guest`` user on the cabin."""
    return _module("motel.tours.guest", "the guest part").runner_argv()


def concierge_install():
    """The installer's view of the concierge: ``layout()``, ``unit_text(port)``, ``socket_unit_text()``, ``entry_text()``."""
    return _module("motel.concierge.install", "the concierge")


def bake_module():
    return _module("motel.tours.bake", "the guest part")
