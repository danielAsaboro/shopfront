from __future__ import annotations

from app.classify.base import Classifier
from app.classify.rules import RulesClassifier
from app.config import Settings


def _aws_creds_available(region: str) -> bool:
    try:
        import boto3

        return boto3.Session(region_name=region).get_credentials() is not None
    except Exception:
        return False


def make_classifier(s: Settings) -> Classifier:
    if s.classifier not in ("auto", "rules", "bedrock"):
        raise ValueError(f"CLASSIFIER must be auto, rules or bedrock, got {s.classifier!r}")
    if s.classifier == "bedrock" or (s.classifier == "auto" and _aws_creds_available(s.aws_region)):
        from app.classify.bedrock import BedrockClassifier

        return BedrockClassifier(region=s.aws_region, model_id=s.bedrock_model_id)
    return RulesClassifier()
