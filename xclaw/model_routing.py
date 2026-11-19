"""Explicit, request-local model selection. Never mutate a shared provider/config.

Only native Responses supports alternate model IDs. Other backends continue to
use their configured default. Operator options are an allowlist, not a fallback
chain or a promise that a remote model stays available.
"""
from __future__ import annotations

import copy


def configured_models(settings) -> list[str]:
    if settings is None:
        return []
    default = getattr(settings, 'model', '')
    models = [default] if default else []
    if (getattr(settings, 'agent_backend', 'native') == 'native'
            and getattr(settings, 'llm_provider', '') == 'openai-responses'):
        for model in getattr(settings, 'web_model_options', []):
            if isinstance(model, str) and model.strip() == model and model and model not in models:
                models.append(model)
    return models


def resolve_request_model(settings, model: str | None = None) -> str:
    chosen = getattr(settings, 'model', '') if model is None else model
    if not isinstance(chosen, str) or chosen not in configured_models(settings):
        raise ValueError('Requested model is not configured or available for this backend')
    return chosen


def bind_request_model(llm, settings, model: str | None = None):
    """Create an isolated model/config view while reusing the HTTP transport.

    ResponsesProvider stores all per-call state in local variables. The only
    changed provider field is the clone's model; authentication/client ownership
    remains on the original. Child agents inherit the bound view, never a global
    model. No fallback occurs on a failed model request.
    """
    chosen = resolve_request_model(settings, model)
    turn_settings = settings.model_copy(update={'model': chosen})
    from xclaw.llm_responses import ResponsesProvider
    if chosen == settings.model and (not settings.web_model_options or not isinstance(llm, ResponsesProvider)):
        return llm, turn_settings
    if not isinstance(llm, ResponsesProvider):
        raise ValueError('Requested model requires the native Responses provider')
    turn_llm = copy.copy(llm)
    turn_llm.model = chosen
    turn_llm.require_model_identity = True
    return turn_llm, turn_settings
