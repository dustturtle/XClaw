"""Trusted delivery metadata that remains compatible with text-only channels."""


class DirectReply(str):
    """A completed local operation that did not create a model Run."""


class RejectedReply(str):
    """A known rejection, not a successful answer; callers should retain drafts."""


class PresentationReply(str):
    """Server-created immutable UI identity, never parsed from model text."""
    def __new__(cls, text, *, presentation_id, message_id):
        result=super().__new__(cls,text)
        result.presentation_id=presentation_id
        result.message_id=message_id
        return result


class VerifiedModelReply(str):
    """A native model answer with provider-verified identity."""

    def __new__(cls, text, model):
        result = super().__new__(cls, text)
        result.response_model = model
        return result
