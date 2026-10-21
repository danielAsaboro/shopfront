"""moto-backed DynamoDB and Secrets Manager for the AWS tests, built from infra/template.yaml so the table
the tests use is the table the stack creates. moto answers inside botocore: no socket is opened."""

from __future__ import annotations

from pathlib import Path

import boto3
import pytest
import yaml
from moto import mock_aws

TEMPLATE = Path(__file__).resolve().parent.parent / "infra" / "template.yaml"


class _CfnLoader(yaml.SafeLoader):
    pass


def _tag(loader, suffix, node):  # !Ref X -> {"Ref": "X"}, !GetAtt A.B -> {"GetAtt": "A.B"}, ...
    if isinstance(node, yaml.ScalarNode):
        value = loader.construct_scalar(node)
    elif isinstance(node, yaml.SequenceNode):
        value = loader.construct_sequence(node, deep=True)
    else:
        value = loader.construct_mapping(node, deep=True)
    return {suffix: value}


_CfnLoader.add_multi_constructor("!", _tag)


def template() -> dict:
    return yaml.load(TEMPLATE.read_text(), Loader=_CfnLoader)


def create_table(client, name: str = "shopfront-webhooks") -> str:
    props = template()["Resources"]["WebhookTable"]["Properties"]
    client.create_table(TableName=name, BillingMode=props["BillingMode"],
                        AttributeDefinitions=props["AttributeDefinitions"], KeySchema=props["KeySchema"])
    client.update_time_to_live(TableName=name, TimeToLiveSpecification={
        "AttributeName": props["TimeToLiveSpecification"]["AttributeName"], "Enabled": True})
    return name


@pytest.fixture
def aws(monkeypatch):
    for k, v in {"AWS_ACCESS_KEY_ID": "testing", "AWS_SECRET_ACCESS_KEY": "testing", "AWS_SESSION_TOKEN": "testing",
                 "AWS_DEFAULT_REGION": "us-east-1", "AWS_CONFIG_FILE": "/dev/null",
                 "AWS_SHARED_CREDENTIALS_FILE": "/dev/null"}.items():
        monkeypatch.setenv(k, v)
    with mock_aws():
        yield


@pytest.fixture
def ddb(aws):
    client = boto3.client("dynamodb", region_name="us-east-1")
    create_table(client)
    return client
