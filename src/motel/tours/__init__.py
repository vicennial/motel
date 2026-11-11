"""Tours: a coordinating agent runs a swarm of bounded, non-interactive agent jobs on disposable servers.

One tour has N guests. Each guest attempt runs on a fresh cabin that is deleted afterwards. Real API keys
never reach a guest: the concierge (``motel.concierge``) holds them and hands out per-attempt placeholders.
The user guide is ``docs/tours.md``; the seams between the parts are fixed in ``docs/design/tours-contract.md``.
"""
