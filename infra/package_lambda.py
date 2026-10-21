"""Copy the webhook Lambdas' code, and only that, into a directory: `python infra/package_lambda.py OUT`.

One package serves both functions: the Function URL handler (app/store/lambda_webhook.py) and the queue
consumer (app/store/lambda_consumer.py); the template points each at its own module.

The Lambda runs the app's own modules (no fork of the HMAC or dedupe logic), but not the app's
dependencies: everything it imports is the standard library or boto3, which the Lambda runtime ships.
`app/ring/__init__.py` is replaced by an empty one because the real one builds the Ring client (httpx).
tests/test_lambda_webhook.py imports the result with only the standard library on the path.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FILES = ["app/__init__.py", "app/files.py", "app/models.py", "app/ring/webhooks.py",
         "app/store/__init__.py", "app/store/dynamodb.py", "app/store/lambda_webhook.py",
         "app/store/lambda_consumer.py"]
EMPTY = ["app/ring/__init__.py"]


def package(out: Path) -> list[Path]:
    written = []
    for rel in FILES:
        dest = out / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / rel, dest)
        written.append(dest)
    for rel in EMPTY:
        (out / rel).write_text("")
        written.append(out / rel)
    return written


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python infra/package_lambda.py OUT_DIR")
    for p in package(Path(sys.argv[1])):
        print(p)
