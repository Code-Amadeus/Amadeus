"""Minimal role-reference context for model-driven Provider requests.

The execution Provider is not the conversational character. Preserve the
model-authored task and add only the identity fact that disappears at the
delegation boundary: who the user was addressing in the parent conversation.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


MAIN_ROLE_NAME_METADATA_KEY = "main_role_name"


def with_main_role_reference(
    task: str,
    *,
    metadata: Mapping[str, Any] | None,
    execution_provider: str,
) -> str:
    """Tell an execution model how to resolve role-relative self-reference.

    ``task`` remains the durable payload stored by ProviderRuntime. This
    function renders a request-local model prompt only; it does not infer a
    referent, rewrite user language, or make the role a subject when another
    subject is explicitly named.
    """

    body = str(task or "").rstrip()
    envelope = metadata if isinstance(metadata, Mapping) else {}
    role_name = " ".join(
        str(envelope.get(MAIN_ROLE_NAME_METADATA_KEY) or "").split()
    )
    if not role_name:
        return body
    provider = str(execution_provider or "").strip()

    binding = (
        "[Amadeus role-reference context]\n"
        f'This task comes from a conversation whose main role is "{role_name}"; '
        f'the execution Provider is "{provider}". In source-request wording '
        "carried into the task, a second-person self-reference such as "
        "'yourself', '你自己', or 'あなた自身' refers to the main role, not "
        "the execution Provider. This only resolves that reference and never "
        "overrides another explicitly named subject.\n"
        "[/Amadeus role-reference context]"
    )
    return f"{body}\n\n{binding}" if body else binding
