# Everything you can do with this repository.
#
#   make demo      the whole platform, from nothing, in one command
#   make check     everything CI checks, locally
#   make down      stop it
#   make teardown  stop it and delete the data
#
# Tabs matter in a Makefile. .RECIPEPREFIX is left alone so the file stays
# portable to any make.

SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help

COMPOSE := docker compose
PYTHON  := python
VENV    := .venv
# The virtualenv's interpreter, whichever platform created it.
VENV_PY := $(if $(wildcard $(VENV)/Scripts/python.exe),$(VENV)/Scripts/python.exe,$(VENV)/bin/python)

KUSTOMIZE_IMAGE := registry.k8s.io/kustomize/kustomize:v5.4.3
TERRAFORM_IMAGE := hashicorp/terraform:1.9.8
# ESP-IDF, pinned. The same tag builds the firmware, runs QEMU (the esp32-qemu
# Compose service) and is used in CI.
IDF_IMAGE       := espressif/idf:v5.5.4
DEVICE_ID       ?= esp32-qemu-0001

# Benchmark defaults. Override on the command line:
#   make bench BENCH_DEVICES=200 BENCH_RATE=10
BENCH_DEVICES ?= 50
BENCH_RATE    ?= 10
BENCH_DURATION ?= 60
BENCH_LABEL   ?= default

.PHONY: help
help: ## Show this help
	@awk 'BEGIN {FS = ":.*##"; printf "\nTargets:\n"} \
		/^[a-zA-Z0-9_-]+:.*?##/ { printf "  \033[36m%-20s\033[0m %s\n", $$1, $$2 } \
		/^##@/ { printf "\n\033[1m%s\033[0m\n", substr($$0, 5) }' $(MAKEFILE_LIST)
	@echo ""

##@ Running the platform

.PHONY: demo
demo: ## Bring the whole stack up and wait until it is serving
	$(COMPOSE) up -d --build --wait
	@echo ""
	@echo "  Dashboard   http://localhost:$${WEB_PORT:-5173}"
	@echo "  API docs    http://localhost:$${API_PORT:-8000}/docs"
	@echo "  Metrics     http://localhost:$${INGEST_METRICS_PORT:-9101}/metrics"
	@echo ""
	@echo "  Logs        make logs"
	@echo "  Stop        make down"
	@echo ""

.PHONY: up
up: ## Bring the stack up without rebuilding
	$(COMPOSE) up -d --wait

.PHONY: down
down: ## Stop the stack, keep the data
	$(COMPOSE) down

.PHONY: teardown
teardown: ## Stop the stack and delete its volumes, certificates and data
	$(COMPOSE) down -v --remove-orphans
	rm -rf certs data
	@echo "stack removed, volumes deleted, local certificates removed"

.PHONY: logs
logs: ## Follow the logs of every service
	$(COMPOSE) logs -f --tail 50

.PHONY: ps
ps: ## Show service status
	$(COMPOSE) ps

.PHONY: psql
psql: ## Open a psql shell on TimescaleDB
	$(COMPOSE) exec timescaledb psql -U $${PG_USER:-telemetry} -d $${PG_DATABASE:-telemetry}

##@ Setup

.PHONY: venv
venv: ## Create the local virtualenv and install everything editable
	$(PYTHON) -m venv $(VENV)
	$(VENV_PY) -m pip install --upgrade pip
	$(VENV_PY) -m pip install -r requirements-dev.txt
	$(VENV_PY) -m pip install -e ./services/common
	$(VENV_PY) -m pip install -e ./edge/simulator
	$(VENV_PY) -m pip install -e ./services/ingest
	$(VENV_PY) -m pip install -e ./services/processing
	$(VENV_PY) -m pip install -e ./services/api
	$(VENV_PY) -m pip install -e ./services/alerting
	@echo "virtualenv ready: $(VENV_PY)"

.PHONY: certs
certs: ## Copy the device certificates out of the Compose volume to ./certs
	bash scripts/export_certs.sh

.PHONY: certs-register
certs-register: ## Record the issued certificates in the device registry
	$(VENV_PY) scripts/register_certificates.py --certs-dir certs

.PHONY: generate
generate: ## Regenerate the JSON schemas and the broker ACL from the code
	$(VENV_PY) scripts/gen_schemas.py
	$(VENV_PY) scripts/gen_broker_config.py

##@ Checks

.PHONY: check
check: lint typecheck test web-check analytics-check infra-check ## Everything CI checks

.PHONY: lint
lint: ## Lint and format-check the Python, and verify generated files are fresh
	$(VENV_PY) -m ruff check .
	$(VENV_PY) -m ruff format --check .
	$(VENV_PY) scripts/gen_schemas.py --check
	$(VENV_PY) scripts/gen_broker_config.py --check
	$(VENV_PY) -m alerting.main --check --rules config/alert_rules.yaml
	bash scripts/check_line_endings.sh

.PHONY: format
format: ## Reformat the Python
	$(VENV_PY) -m ruff format .
	$(VENV_PY) -m ruff check --fix .

.PHONY: typecheck
typecheck: ## Type-check the services
	$(VENV_PY) -m mypy

.PHONY: test
test: ## Unit tests. No Docker needed.
	$(VENV_PY) -m pytest tests/unit -q

.PHONY: test-integration
test-integration: ## Integration tests against the running stack
	bash scripts/export_certs.sh
	$(VENV_PY) -m pytest tests/integration -q -m integration

.PHONY: web-check
web-check: ## Typecheck, lint, test and build the dashboard
	cd web && npm ci && npm run typecheck && npm run lint && npm run test && npm run build

.PHONY: analytics-check
analytics-check: ## Run every analytical query against DuckDB on generated fixtures
	$(VENV_PY) analytics/local/run_queries.py --fixtures --require-rows

.PHONY: infra-check
infra-check: tf-check k8s-check ## Validate Terraform and the Kubernetes manifests

.PHONY: tf-check
tf-check: ## terraform fmt and validate both roots and test the IoT module, in a container
	docker run --rm -v "$(CURDIR):/work" -w /work/infra/terraform $(TERRAFORM_IMAGE) fmt -recursive -check -diff
	docker run --rm -v "$(CURDIR):/work" -w /work/infra/terraform/aws --entrypoint /bin/sh $(TERRAFORM_IMAGE) \
		-c "terraform init -backend=false -input=false >/dev/null && terraform validate"
	docker run --rm -v "$(CURDIR):/work" -w /work/infra/terraform/gcp --entrypoint /bin/sh $(TERRAFORM_IMAGE) \
		-c "terraform init -backend=false -input=false >/dev/null && terraform validate"
	docker run --rm -v "$(CURDIR):/work" -w /work/infra/terraform/aws/modules/iot --entrypoint /bin/sh $(TERRAFORM_IMAGE) \
		-c "terraform init -backend=false -input=false >/dev/null && terraform test"

.PHONY: k8s-check
k8s-check: ## Build the Kubernetes overlays and check they render
	docker run --rm -v "$(CURDIR):/work" -w /work $(KUSTOMIZE_IMAGE) \
		build --load-restrictor=LoadRestrictionsNone infra/k8s/base > /dev/null
	docker run --rm -v "$(CURDIR):/work" -w /work $(KUSTOMIZE_IMAGE) \
		build --load-restrictor=LoadRestrictionsNone infra/k8s/overlays/dev > /dev/null
	@echo "manifests render"

.PHONY: k8s-render
k8s-render: ## Print the rendered dev overlay
	@docker run --rm -v "$(CURDIR):/work" -w /work $(KUSTOMIZE_IMAGE) \
		build --load-restrictor=LoadRestrictionsNone infra/k8s/overlays/dev

##@ Benchmark and analytics

.PHONY: bench
bench: ## Run the throughput and latency benchmark against the running stack
	bash scripts/export_certs.sh
	$(VENV_PY) bench/benchmark.py \
		--devices $(BENCH_DEVICES) \
		--rate $(BENCH_RATE) \
		--duration $(BENCH_DURATION) \
		--label $(BENCH_LABEL)

.PHONY: export
export: ## Export the rollups to Parquet for the analytical layer
	$(VENV_PY) analytics/export/export.py --days 7 --out data/export

.PHONY: analytics
analytics: ## Run the analytical queries against the exported Parquet
	$(VENV_PY) analytics/local/run_queries.py --data data/export

##@ Device simulator

.PHONY: simulate
simulate: ## Run a fleet from the host against the running broker
	bash scripts/export_certs.sh
	$(VENV_PY) -m telemetry_simulator --host localhost --certs-dir certs \
		--devices $${DEVICE_COUNT:-10} --log-text

.PHONY: sample
sample: ## Print one sample message per device. No broker needed.
	$(VENV_PY) -m telemetry_simulator --dry-run --devices 10

##@ Firmware

.PHONY: firmware
firmware: ## Build every firmware variant (esp32, esp32c3, qemu) with ESP-IDF in a container
	docker run --rm -v "$(CURDIR)/edge/firmware:/project" -w /project $(IDF_IMAGE) \
		bash scripts/build.sh

.PHONY: firmware-test
firmware-test: ## Host tests for the firmware core, with ASan and UBSan
	$(MAKE) -C edge/firmware/test/host

.PHONY: device
device: ## Provision esp32-qemu-0001 and boot it in QEMU against the running stack
	$(COMPOSE) run --rm --no-deps -T pki \
		"bash /app/scripts/pki.sh device $(DEVICE_ID) && chown -R 10001:10001 /certs/devices"
	$(COMPOSE) run --rm --no-deps certs-register
	$(COMPOSE) --profile emulated-device run --rm --no-deps -e DEVICE_ID=$(DEVICE_ID) esp32-qemu
