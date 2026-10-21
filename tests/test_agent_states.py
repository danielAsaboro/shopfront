"""Ask Shopfront when Bedrock can't be used: a named state and one plain sentence, never a raw error."""

import pytest
from botocore.exceptions import ClientError, EndpointConnectionError, NoCredentialsError
from fastapi.testclient import TestClient

import app.main as main
from app import agent as shop_agent


def client_error(code, message="nope", status=400):
    return ClientError({"Error": {"Code": code, "Message": message},
                        "ResponseMetadata": {"HTTPStatusCode": status}}, "Converse")


@pytest.mark.parametrize("exc,reason", [
    (NoCredentialsError(), "NO_AWS_SIGN_IN"),
    (client_error("ExpiredTokenException", "The security token included in the request is expired"), "NO_AWS_SIGN_IN"),
    (EndpointConnectionError(endpoint_url="https://bedrock-runtime.us-east-1.amazonaws.com"), "BEDROCK_UNREACHABLE"),
    (client_error("403", 'request blocked: no rule allows host "bedrock-runtime.us-east-1.amazonaws.com"', 403),
     "BEDROCK_UNREACHABLE"),
    (client_error("AccessDeniedException", "You don't have access to the model"), "NO_MODEL_ACCESS"),
    (client_error("ThrottlingException", "Too many requests"), "BEDROCK_BUSY"),
    (RuntimeError("something else"), "AGENT_FAILED"),
])
def test_failures_become_named_states(exc, reason, monkeypatch):
    def factory(*a):
        def run(q):
            raise RuntimeError("wrapped by the agent loop") from exc
        return run
    monkeypatch.setattr(main, "agent_factory", factory)
    r = TestClient(main.app).post("/api/ask", json={"question": "did bread come?"})
    body = r.json()
    assert r.status_code == 503 and body["error"] == "AGENT_UNAVAILABLE" and body["reason"] == reason
    assert body["message"].endswith(".") and "Error" not in body["message"] and "http" not in body["message"]
    assert "proxy" not in body["message"] and "amazonaws" not in body["message"]


def test_no_sign_in_message_is_the_one_the_page_shows():
    e = shop_agent.unavailable(NoCredentialsError())
    assert e.message == "Ask needs an AWS sign-in with Bedrock access; it isn't available on this machine."
