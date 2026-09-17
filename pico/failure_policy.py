"""Pure, trusted model guidance derived from durable failure facts."""

INVALID_OUTPUT_GUIDANCE = (
    "The previous model response was incomplete or did not match the required "
    "protocol. Return valid tool calls or one complete final answer."
)

REPEATED_FAILURE_GUIDANCE = (
    "The same failure has occurred three times with identical input and error "
    "details. Change the approach before trying again."
)


def guidance_for_failure(failure):
    """Return fixed guidance without copying untrusted failure identity text."""

    if failure is None:
        return ""
    parts = []
    if failure.category == "model" and failure.code == "invalid":
        parts.append(INVALID_OUTPUT_GUIDANCE)
    if failure.count == 3:
        parts.append(REPEATED_FAILURE_GUIDANCE)
    return "\n\n".join(parts)
