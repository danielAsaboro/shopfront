# Called by `sam build` (BuildMethod: makefile in template.yaml) with ARTIFACTS_DIR set.
# Packages the app's own webhook modules; no pip install, the Lambda runtime has boto3.
build-RingWebhookFunction:
	python3 $(dir $(abspath $(lastword $(MAKEFILE_LIST))))package_lambda.py "$(ARTIFACTS_DIR)"
