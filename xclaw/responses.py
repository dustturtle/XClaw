"""Trusted delivery metadata that remains compatible with text-only channels."""


class DirectReply(str):
    """A completed local operation that did not create a model Run."""


class RejectedReply(str):
    """A known rejection, not a successful answer; callers should retain drafts."""
