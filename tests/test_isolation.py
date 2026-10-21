"""The test run itself: no network, no live classifier, nothing written into the repo."""

import socket
import tempfile
from pathlib import Path

import pytest

import app.main as main
from app.classify import make_classifier
from tests.conftest import NetworkBlocked


def test_outbound_sockets_are_blocked():
    with pytest.raises(NetworkBlocked):
        socket.create_connection(("bedrock-runtime.us-east-1.amazonaws.com", 443), timeout=1)
    with pytest.raises(NetworkBlocked):
        socket.socket().connect(("93.184.216.34", 443))


def test_app_was_imported_with_the_rules_classifier_and_a_temp_data_dir():
    # .env may say CLASSIFIER=auto with AWS credentials; the tests must never build a Bedrock client from it
    assert make_classifier(main.settings).name == "rules"
    assert Path(main.settings.data_dir).resolve().is_relative_to(Path(tempfile.gettempdir()).resolve())


def test_every_store_points_into_tmp(tmp_path):
    for p in (main.DATA, main.book.path, main.cache.path, main.webhook_log.path, main.live.path):
        assert Path(p).is_relative_to(tmp_path)
    assert main.classifier.name == "rules"
