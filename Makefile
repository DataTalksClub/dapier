.PHONY: check-dakit sync-dakit build deploy test plugin-test validate layer sync-dakit designer-install designer designer-build designer-console

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
	uv run --with boto3 --with pytest --with pyyaml --with "pyjwt[crypto]" --with mini-racer --with moto --with pypdf --with playwright env \
		PYTHONPATH=. AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
		AWS_DEFAULT_REGION=eu-west-1 DAPIER_SKIP_CONFIG_DB=1 pytest -q

# One plugin's test lane: `make plugin-test PLUGIN=slack`. Same environment
# as `make test`; CI runs these per plugin for fast feedback on plugin PRs.
plugin-test:
	uv run --with boto3 --with pytest --with pyyaml --with "pyjwt[crypto]" --with moto env \
		PYTHONPATH=. AWS_ACCESS_KEY_ID=testing AWS_SECRET_ACCESS_KEY=testing \
		AWS_DEFAULT_REGION=eu-west-1 DAPIER_SKIP_CONFIG_DB=1 pytest -q plugins/$(PLUGIN)/tests

designer-install:
	cd designer && npm install

designer: designer-install
	cd designer && npm run dev

designer-build:
	cd designer && npm run build

# Refresh the vendored dakit design system (src/web/vendor/) from the dakit
# checkout next to this repo: dakit's own sync tool copies the built bundle,
# the JS helpers, fonts and the checkers, and records checksums in
# dakit.lock.json. Rebuild the designer bundle afterwards (it inlines
# vendor/dakit.css) and commit everything — deploys serve the vendored copy.
DAKIT ?= ../dakit
DAKIT_FILES = dakit.css dialogs.js tabs.js collection.js shell.js split.js device-code.js
sync-dakit:
	node $(DAKIT)/scripts/sync.mjs --dest src/web/vendor --files $(DAKIT_FILES)

# App-authored styles the dakit checkers judge. The generated designer bundle
# (src/web/designer.js/.css) is checked through its sources in designer/src.
DAKIT_CHECK_PATHS = src/web/app.css src/web/public.css \
	src/web/index.html src/web/device.html src/web/designer.html \
	src/web/public-home.html src/web/privacy.html src/web/terms.html \
	src/web/js designer/src

# The vendored copy matches its lock; no accent borders, no raw colours, and
# spacing/type on the scales (existing off-scale debt lives in
# dakit-scale-baseline.json, which may only shrink;
# its 4 remaining entries are approved exceptions: the designer canvas
# 13.5px density step, and the 3 fluid clamp() headline/lede sizes on the
# public marketing pages — see the comment in src/web/public.css). `make test` runs these
# through tests/test_dakit_checks.py.
check-dakit:
	node src/web/vendor/sync.mjs --check --dest src/web/vendor
	node src/web/vendor/check-borders.mjs src/web designer/src
	node src/web/vendor/check-colors.mjs $(DAKIT_CHECK_PATHS)
	node src/web/vendor/check-scale.mjs --baseline dakit-scale-baseline.json $(DAKIT_CHECK_PATHS)

# Rebuild the bundle vendored into the console (src/web/designer.js/.css,
# served at /designer). Run after changing designer sources.
designer-console: designer-install
	cd designer && npm run build:console
