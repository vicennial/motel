"""Entry point ``omnigent.sandbox_providers`` -> ``motel``."""

from __future__ import annotations

from omnigent.onboarding.sandboxes.registry import SandboxProviderContribution, SandboxProviderMetadata

from .config import MotelSandboxConfig, token_ttl_s


def get_contribution() -> SandboxProviderContribution:
    return SandboxProviderContribution(
        name="motel",
        providers={
            "motel": SandboxProviderMetadata(
                name="motel",
                launcher_class="omnigent.community.sandbox.motel.launcher:MotelSandboxLauncher",
                config_model=MotelSandboxConfig,
                # The cabin's longest life ([cabins] max_lifetime in motel's config) plus a margin, read when the
                # server starts, like Omnigent's own providers tie the token to the sandbox's lifetime.
                managed_token_ttl_s=token_ttl_s(),
            )
        },
    )
