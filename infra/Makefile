# Called by `sam build` (BuildMethod: makefile in template.yaml) with ARTIFACTS_DIR set.
HERE := $(dir $(abspath $(lastword $(MAKEFILE_LIST))))

# The webhook function (Function URL): the app's own webhook modules; no pip install, the Lambda runtime has boto3.
build-RingWebhookFunction:
	python3 $(HERE)package_lambda.py "$(ARTIFACTS_DIR)"

# The queue consumer: the same package, another handler (app.store.lambda_consumer.handler).
build-WebhookConsumerFunction:
	python3 $(HERE)package_lambda.py "$(ARTIFACTS_DIR)"

# The public demo: the app, scripts/demo.py and samples/orders.csv, plus its wheels for Python 3.12 on arm64.
build-PublicDemoFunction:
	python3 $(HERE)package_demo.py "$(ARTIFACTS_DIR)"
