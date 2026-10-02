.PHONY: build deploy test validate layer sync-dakit designer-install designer designer-build designer-console

build: layer
	scripts/build-sam.sh

# py_mini_racer (embedded V8, for js code steps) ships only as a
# manylinux_2_27 aarch64 wheel, which SAM's cross-build for the arm64
# functions refuses to resolve — so it is installed here into a Lambda layer
# (attached to the API and worker functions in template.yaml) instead of
# requirements.txt. Staged under .aws-sam/ (ignored and gitignored) so SAM's
# repo copy never pulls the ~100 MB payload into the function bundles.
layer:
	mkdir -p .aws-sam/layers-src
	uv run --with pip python -m pip install --quiet --no-deps --no-compile --upgrade \
		--target .aws-sam/layers-src/js-runtime/python --only-binary=:all: \
		--platform manylinux_2_27_aarch64 --implementation py --abi none \
		--python-version 3.12 mini-racer==0.14.1
	cd .aws-sam/layers-src/js-runtime && python3 -m zipfile -c ../js-runtime.zip python

deploy:
	scripts/deploy.sh

validate:
	sam validate --lint

test:
	uv run --with boto3 --with pytest --with pyyaml --with "pyjwt[crypto]" --with mini-racer --with moto --with pypdf env \
		PYTHONPATH=. AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
		AWS_DEFAULT_REGION=eu-west-1 DAPIER_SKIP_CONFIG_DB=1 pytest -q

designer-install:
	cd designer && npm install

designer: designer-install
	cd designer && npm run dev

designer-build:
	cd designer && npm run build

# Refresh the vendored dakit design system (src/web/vendor/) from the dakit
# checkout next to this repo. Run after changing dakit tokens/styles, then
# commit the result — the console serves the vendored copy, never ../dakit.
sync-dakit:
	mkdir -p src/web/vendor/fonts
	cp ../dakit/dist/dakit.css src/web/vendor/dakit.css
	cp ../dakit/dist/tokens.css src/web/vendor/dakit-tokens.css
	cp ../dakit/fonts/*.woff2 src/web/vendor/fonts/

# Rebuild the bundle vendored into the console (src/web/designer.js/.css,
# served at /designer). Run after changing designer sources.
designer-console: designer-install
	cd designer && npm run build:console
