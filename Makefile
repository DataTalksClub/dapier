.PHONY: build deploy test validate designer-install designer designer-build designer-console

build:
	sam build --config-env sandbox

deploy:
	scripts/deploy.sh

validate:
	sam validate --lint

test:
	uv run --with boto3 --with pytest --with pyyaml --with "pyjwt[crypto]" env \
		PYTHONPATH=. AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
		AWS_DEFAULT_REGION=eu-west-1 DAPIER_SKIP_CONFIG_DB=1 pytest -q

designer-install:
	cd designer && npm install

designer: designer-install
	cd designer && npm run dev

designer-build:
	cd designer && npm run build

# Rebuild the bundle vendored into the console (src/web/designer.js/.css,
# served at /designer). Run after changing designer sources.
designer-console: designer-install
	cd designer && npm run build:console
