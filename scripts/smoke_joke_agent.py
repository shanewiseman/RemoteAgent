#!/usr/bin/env python3
"""Manual, subscription-consuming live workflow smoke test.

This file intentionally uses only the standard library and is never collected by
pytest. CI exercises the equivalent flow through the deterministic fake runner.
"""

from __future__ import annotations

import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid
from typing import Any


TERMINAL = {"succeeded", "failed", "cancelled", "interrupted", "expired"}


class SmokeFailure(RuntimeError):
    pass


def request_json(
    base_url: str,
    token: str,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    tolerate: tuple[int, ...] = (),
) -> Any:
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        if exc.code in tolerate:
            return None
        detail = exc.read().decode("utf-8", errors="replace")
        raise SmokeFailure(f"{method} {path} returned HTTP {exc.code}: {detail}") from exc
    except OSError as exc:
        raise SmokeFailure(f"{method} {path} failed: {exc}") from exc
    if not body:
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SmokeFailure(f"{method} {path} did not return JSON") from exc


def discover_agent(base_url: str, token: str, agent_id: str) -> None:
    body = request_json(base_url, token, "GET", "/api/v1/agents")
    agents = body.get("agents", body.get("items", [])) if isinstance(body, dict) else body
    if not isinstance(agents, list):
        raise SmokeFailure("agent discovery returned an unexpected shape")
    matches = [item for item in agents if isinstance(item, dict) and item.get("id") == agent_id]
    if len(matches) != 1:
        raise SmokeFailure(f"discovery expected exactly one {agent_id!r}, found {len(matches)}")
    if not matches[0].get("enabled", False):
        raise SmokeFailure(f"agent {agent_id!r} is disabled")


def submit(
    base_url: str,
    token: str,
    agent_id: str,
    prompt: str,
    conversation_key: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "agent_id": agent_id,
        "prompt": prompt,
        "idempotency_key": f"smoke-{uuid.uuid4()}",
    }
    if conversation_key is not None:
        payload["conversation_key"] = conversation_key
    body = request_json(base_url, token, "POST", "/api/v1/jobs", payload)
    if not isinstance(body, dict) or not body.get("job_id") or not body.get("conversation_key"):
        raise SmokeFailure("job submission did not return job_id and conversation_key")
    return body


def wait_for_job(base_url: str, token: str, job_id: str, deadline: float) -> dict[str, Any]:
    delay = 0.5
    last_status: str | None = None
    while time.monotonic() < deadline:
        body = request_json(base_url, token, "GET", f"/api/v1/jobs/{job_id}")
        if not isinstance(body, dict):
            raise SmokeFailure(f"job {job_id} returned an unexpected shape")
        status = str(body.get("status", ""))
        last_status = status
        if status in TERMINAL:
            if status != "succeeded":
                raise SmokeFailure(
                    f"job {job_id} ended as {status}: {body.get('error') or 'no error detail'}"
                )
            return body
        time.sleep(delay)
        delay = min(delay * 1.5, 5.0)
    raise SmokeFailure(f"job {job_id} timed out (last status: {last_status or 'unknown'})")


def assert_joke(
    value: Any,
    fixture: dict[str, Any],
    *,
    required: tuple[str, ...],
    forbidden: tuple[str, ...] = (),
) -> str:
    if not isinstance(value, str):
        raise SmokeFailure("agent result is not text")
    text = value.strip()
    if "\n" in text or "\r" in text:
        raise SmokeFailure("agent result is not one paragraph")
    prefix = str(fixture["response_prefix"])
    if not text.startswith(prefix) or text.count(prefix) != 1:
        raise SmokeFailure(f"agent result must begin exactly once with {prefix!r}")
    if not int(fixture["minimum_characters"]) <= len(text) <= int(
        fixture["maximum_characters"]
    ):
        raise SmokeFailure(f"agent result length {len(text)} is outside the contract")
    if text.startswith(("```", "{", "[", "- ", "* ")):
        raise SmokeFailure("agent result contains a wrapper/list instead of one joke")
    for token in required:
        if token not in text:
            raise SmokeFailure(f"agent result is missing required exact token {token!r}")
    for token in forbidden:
        if token in text:
            raise SmokeFailure(f"agent result repeated forbidden prior token {token!r}")
    return text


def main() -> int:
    if any(os.environ.get(name) for name in ("CI", "GITHUB_ACTIONS", "GITLAB_CI", "BUILDKITE")):
        raise SmokeFailure("live smoke is disabled in CI")

    base_url = os.environ["REMOTEAGENT_SMOKE_URL"]
    token = os.environ["REMOTEAGENT_SMOKE_TOKEN"]
    agent_id = os.environ.get("REMOTEAGENT_SMOKE_AGENT", "joke-agent")
    timeout = int(os.environ.get("REMOTEAGENT_SMOKE_TIMEOUT", "300"))
    keep = os.environ.get("REMOTEAGENT_SMOKE_KEEP", "0") == "1"
    json_output = os.environ.get("REMOTEAGENT_SMOKE_JSON", "0") == "1"

    fixture_path = pathlib.Path(__file__).resolve().parents[1] / "joke-agent" / "smoke-fixture.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))
    nonce = uuid.uuid4().hex[:12].upper()
    turn_1_marker = f"RA_T1_{nonce}"
    turn_2_marker = f"RA_T2_{nonce}"
    memory_marker = f"RA_MEMORY_{nonce}"
    prompt_1 = fixture["turn_1"].format(
        turn_1_marker=turn_1_marker,
        memory_marker=memory_marker,
    )
    prompt_2 = fixture["turn_2"].format(turn_2_marker=turn_2_marker)

    active_job: str | None = None
    conversation_key: str | None = None
    try:
        discover_agent(base_url, token, agent_id)
        first = submit(base_url, token, agent_id, prompt_1, None)
        active_job = str(first["job_id"])
        conversation_key = str(first["conversation_key"])
        deadline = time.monotonic() + timeout
        first_job = wait_for_job(base_url, token, active_job, deadline)
        first_text = assert_joke(
            first_job.get("result"),
            fixture,
            required=("telescope", turn_1_marker, memory_marker),
        )

        second = submit(base_url, token, agent_id, prompt_2, conversation_key)
        active_job = str(second["job_id"])
        if second["conversation_key"] != conversation_key:
            raise SmokeFailure("continuation returned a different conversation key")
        if active_job == first["job_id"]:
            raise SmokeFailure("continuation reused the first job id")
        second_job = wait_for_job(base_url, token, active_job, deadline)
        second_text = assert_joke(
            second_job.get("result"),
            fixture,
            required=("bicycle", turn_2_marker, memory_marker),
            forbidden=(turn_1_marker,),
        )
        if second_text == first_text:
            raise SmokeFailure("continuation duplicated the first response")

        result = {
            "ok": True,
            "agent_id": agent_id,
            "conversation_key": conversation_key,
            "jobs": [first["job_id"], second["job_id"]],
            "turn_1": first_text,
            "turn_2": second_text,
        }
        if json_output:
            print(json.dumps(result, sort_keys=True))
        else:
            print(f"live smoke passed for {agent_id}; conversation={conversation_key}")
            print(first_text)
            print(second_text)

        if not keep:
            request_json(
                base_url,
                token,
                "DELETE",
                f"/api/v1/conversations/{conversation_key}",
            )
        return 0
    except BaseException:
        if active_job is not None:
            try:
                request_json(
                    base_url,
                    token,
                    "POST",
                    f"/api/v1/jobs/{active_job}/cancel",
                    {},
                    tolerate=(404, 409),
                )
            except BaseException:
                pass
        if conversation_key:
            print(f"remoteagent: retained failed smoke conversation {conversation_key}", file=sys.stderr)
        raise


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except SmokeFailure as exc:
        print(f"remoteagent: live smoke failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
