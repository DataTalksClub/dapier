.PHONY: build deploy test validate

build:
	sam build --config-env sandbox

deploy:
	sam deploy --config-env sandbox

validate:
	sam validate --lint

test:
	uv run --with boto3 --with pytest --with pyyaml env PYTHONPATH=. pytest -q
