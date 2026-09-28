"""Work role assignments are policy, independent of Provider registration."""
from __future__ import annotations

from agent_host.provider_contract import ProviderRequirements, compatibility_errors
from config import settings


def work_provider_roles() -> dict[str, str]:
    return {"coding": settings.WORK_CODING_PROVIDER,
        "execution": settings.WORK_EXECUTION_PROVIDER}


def work_role_candidates(manifests) -> dict[str, list[str]]:
    requirements = {"coding": ProviderRequirements(task_kind="general", workspace_access="write"),
        "execution": ProviderRequirements(task_kind="general")}
    manifests = tuple(manifests)
    return {role: sorted(manifest.provider_id for manifest in manifests
        if not compatibility_errors(manifest, requirement))
        for role, requirement in requirements.items()}


def work_context_requirements(runtime, *, roles: dict[str, str], primary_policy: dict,
                              additional_policies: dict) -> dict[str, ProviderRequirements]:
    """Keep registered general agents addressable independently of assignments.

    Registration owns capabilities. Existing Host policy overrides remain
    authoritative and never change an adapter's manifest.
    """
    policies = dict(additional_policies)
    for provider in (*work_role_candidates(runtime.provider_manifests())["execution"], *roles.values()):
        policies.setdefault(provider, {})
    primary = roles["execution"]
    policies[primary] = {**policies[primary], **primary_policy}
    contexts = {}
    for provider, policy in policies.items():
        if not isinstance(provider, str) or not provider.strip() or not isinstance(policy, dict):
            raise ValueError("invalid Work provider policy")
        provider = provider.strip().lower()
        manifest = runtime.get_manifest(provider)
        # The primary recipient remains known to role-only Chat even while its
        # runtime is unavailable. Execution still requires registered capability.
        if manifest is None and provider != primary:
            continue
        baseline = {"task_kind": "general", "ownership": "managed"}
        if manifest is not None:
            caps = manifest.capabilities
            baseline.update(workspace_access=caps.workspace_access,
                workspace_ownership=caps.workspace_ownership, resume=caps.resume)
        contexts[provider] = ProviderRequirements.from_dict({**baseline, **policy})
    return contexts
