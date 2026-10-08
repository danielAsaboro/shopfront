from __future__ import annotations

import logging

from app.classify.base import Classifier
from app.classify.stub import StubClassifier
from app.config import Settings

log = logging.getLogger(__name__)


def _aws_creds_available(region: str) -> bool:
    try:
        import boto3

        return boto3.Session(region_name=region).get_credentials() is not None
    except Exception:
        return False


def make_classifier(s: Settings) -> Classifier:
    want_bedrock = s.classifier == "bedrock" or (
        s.classifier == "auto" and _aws_creds_available(s.aws_region))
    if want_bedrock:
        from app.classify.bedrock import BedrockClassifier

        return BedrockClassifier(region=s.aws_region, model_id=s.bedrock_model_id)
    return StubClassifier()
