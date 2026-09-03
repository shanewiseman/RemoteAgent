SHELL := /usr/bin/env bash
REMOTE := ./scripts/remotectl

.DEFAULT_GOAL := help
.PHONY: help init bootstrap validate doctor build build-all auth-login auth-status \
	start stop restart down status logs migrate migrate-status agents agent-new \
	agent-validate agent-build smoke-live skills-sync backup backup-verify restore \
	cleanup cleanup-apply upgrade-check upgrade token-status token-rotate-cron \
	api-contracts api-contracts-check

help:
	@$(REMOTE) help

init:
	@$(REMOTE) init

bootstrap:
	@$(REMOTE) bootstrap

validate:
	@$(REMOTE) validate --all

doctor:
	@$(REMOTE) doctor

build:
	@$(REMOTE) build core

build-all:
	@$(REMOTE) build all

auth-login:
	@$(REMOTE) auth login --method chatgpt

auth-status:
	@$(REMOTE) auth status

token-status:
	@$(REMOTE) token status all

token-rotate-cron:
	@test "$(CONFIRM)" = YES || { echo "token-rotate-cron requires CONFIRM=YES" >&2; exit 2; }
	@$(REMOTE) token rotate cron --yes

start:
	@$(REMOTE) start

stop:
	@$(REMOTE) stop

restart:
	@$(REMOTE) restart

down:
	@$(REMOTE) down

status:
	@$(REMOTE) status

logs:
	@$(REMOTE) logs $(or $(SERVICE),router) $(if $(FOLLOW),--follow,) $(if $(SINCE),--since $(SINCE),) $(if $(TAIL),--tail $(TAIL),)

migrate:
	@$(REMOTE) migrate apply

migrate-status:
	@$(REMOTE) migrate status

agents:
	@$(REMOTE) agent list

agent-new:
	@test -n "$(AGENT)" || { echo "AGENT is required" >&2; exit 2; }
	@$(REMOTE) agent new "$(AGENT)" $(if $(NAME),--name "$(NAME)",)

agent-validate:
	@test -n "$(AGENT)" || { echo "AGENT is required" >&2; exit 2; }
	@$(REMOTE) agent validate "$(AGENT)"

agent-build:
	@test -n "$(AGENT)" || { echo "AGENT is required" >&2; exit 2; }
	@$(REMOTE) agent build "$(AGENT)"

smoke-live:
	@$(REMOTE) smoke live --agent $(or $(AGENT),joke-agent) $(if $(KEEP),--keep,)

skills-sync:
	@$(REMOTE) skills sync

backup:
	@$(REMOTE) backup create $(if $(OUTPUT),--output "$(OUTPUT)",)

backup-verify:
	@test -n "$(FILE)" || { echo "FILE is required" >&2; exit 2; }
	@$(REMOTE) backup verify "$(FILE)"

restore:
	@test -n "$(FILE)" || { echo "FILE is required" >&2; exit 2; }
	@test "$(CONFIRM)" = YES || { echo "restore requires CONFIRM=YES" >&2; exit 2; }
	@$(REMOTE) restore "$(FILE)" --yes

cleanup:
	@$(REMOTE) cleanup

cleanup-apply:
	@test "$(CONFIRM)" = YES || { echo "cleanup-apply requires CONFIRM=YES" >&2; exit 2; }
	@$(REMOTE) cleanup --apply --yes $(if $(DAYS),--older-than $(DAYS),)

upgrade-check:
	@$(REMOTE) upgrade check

upgrade:
	@test "$(CONFIRM)" = YES || { echo "upgrade requires CONFIRM=YES" >&2; exit 2; }
	@$(REMOTE) upgrade apply --yes

api-contracts:
	@PYTHONPATH=router/src .venv/bin/python router/scripts/export_contracts.py
	@.venv/bin/python cron/scripts/export_contract.py

api-contracts-check:
	@PYTHONPATH=router/src .venv/bin/python router/scripts/export_contracts.py --check
	@.venv/bin/python cron/scripts/export_contract.py --check
