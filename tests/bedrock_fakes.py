"""Converse API replies shaped like Bedrock's, for fakes that stand in for bedrock-runtime."""


def tool_reply(name: str = "label_door_frame", request_id: str = "rid", **tool_input) -> dict:
    """A Converse response whose only content block is a toolUse of `name` with `tool_input`."""
    return {"output": {"message": {"role": "assistant", "content": [
                {"toolUse": {"toolUseId": "tooluse_1", "name": name, "input": tool_input}}]}},
            "stopReason": "tool_use", "ResponseMetadata": {"RequestId": request_id},
            "usage": {"inputTokens": 1000, "outputTokens": 40, "totalTokens": 1040}, "metrics": {"latencyMs": 100}}


def label(type: str, confidence: float, reason: str, request_id: str = "rid") -> dict:
    return tool_reply(request_id=request_id, type=type, confidence=confidence, reason=reason)
