"""Tests for the docker-compose stack.

Verifies the configuration files (Dockerfile, compose, nginx
config, .env.example) are valid and consistent. Doesn't
actually build or run containers — that requires Docker,
which is outside the test environment.

Tests:
- docker-compose.yml is valid YAML
- All 4 services are defined (web, nginx, redis, neo4j)
- Volumes are defined for stateful services
- The network is configured
- The Dockerfile has the expected stages
- The Nginx config has SSE-specific directives
  (proxy_buffering off, etc.)
- The .env.example has all required env vars
- The redis_sse module handles missing redis gracefully
"""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest
import yaml


REPO_ROOT = Path(__file__).parent.parent
DOCKER_DIR = REPO_ROOT / "docker"


# ─── docker-compose.yml structure ──────────────────────────────────

@pytest.fixture(scope="module")
def compose():
    """The parsed docker-compose.yml as a dict."""
    with open(REPO_ROOT / "docker-compose.yml") as f:
        return yaml.safe_load(f)


def test_compose_is_valid_yaml(compose):
    """The compose file should be valid YAML."""
    assert compose is not None
    assert "services" in compose
    assert "volumes" in compose
    assert "networks" in compose


def test_compose_has_4_services(compose):
    """The 4 services (web, nginx, redis, neo4j) should be defined."""
    services = compose["services"]
    assert "web" in services
    assert "nginx" in services
    assert "redis" in services
    assert "neo4j" in services
    assert len(services) == 4


def test_compose_web_service_uses_dockerfile(compose):
    """The web service should be built from the Dockerfile
    in the same directory."""
    web = compose["services"]["web"]
    assert "build" in web
    assert web["build"].get("context") == "."
    assert "Dockerfile" in web["build"].get("dockerfile", "Dockerfile")


def test_compose_web_service_dockerfile_uses_python_args(compose):
    """The web service's Dockerfile should accept the
    KGPIPELINE_PATH build arg (even if the compose file
    doesn't set it). This lets users vendor kgpipeline
    at build time."""
    web = compose["services"]["web"]
    # The compose file may or may not set the arg; the
    # Dockerfile's default is the kgpipeline sibling path.
    # We just check that the build context is the local dir
    # (not a sibling) — sibling contexts would require
    # additional setup.
    assert web["build"].get("context") in (".", "..")
    # The Dockerfile has the KGPIPELINE_PATH arg with a default
    # — verified in the Dockerfile test below.


def test_compose_web_service_exposes_8000(compose):
    """The web service should expose port 8000 (internal)."""
    web = compose["services"]["web"]
    assert "expose" in web
    assert "8000" in web["expose"]


def test_compose_web_service_depends_on_redis_and_neo4j(compose):
    """The web service should wait for redis and neo4j to be
    healthy before starting."""
    web = compose["services"]["web"]
    deps = web["depends_on"]
    assert "redis" in deps
    assert "neo4j" in deps
    # Both should be healthcheck-conditional
    assert deps["redis"].get("condition") == "service_healthy"
    assert deps["neo4j"].get("condition") == "service_healthy"


def test_compose_web_service_has_healthcheck(compose):
    """The web service should have a /healthz healthcheck."""
    web = compose["services"]["web"]
    assert "healthcheck" in web
    test = web["healthcheck"]["test"]
    assert "/healthz" in " ".join(test)


def test_compose_web_service_has_data_volume(compose):
    """The web service should mount a data volume for SQLite
    graph databases (kgpipeline output)."""
    web = compose["services"]["web"]
    assert "volumes" in web
    # Find the dpo-data mount
    assert any("dpo-data" in v for v in web["volumes"])


def test_compose_web_service_has_anthropic_env(compose):
    """The web service should pass through ANTHROPIC_API_KEY
    from the host (or a default empty string)."""
    web = compose["services"]["web"]
    env = web["environment"]
    assert "ANTHROPIC_API_KEY" in env
    # The default should be empty (not hardcoded)
    assert env["ANTHROPIC_API_KEY"] == "${ANTHROPIC_API_KEY:-}"


def test_compose_nginx_exposes_80(compose):
    """Nginx should expose port 80 (and optionally 443 for TLS)."""
    nginx = compose["services"]["nginx"]
    ports = nginx["ports"]
    # Should have port 80 mapped to host
    assert any("80:80" in p for p in ports)


def test_compose_nginx_depends_on_web(compose):
    """Nginx should wait for the web service to be healthy."""
    nginx = compose["services"]["nginx"]
    assert "web" in nginx["depends_on"]
    assert nginx["depends_on"]["web"].get("condition") == "service_healthy"


def test_compose_nginx_uses_local_config(compose):
    """Nginx should use the local config file (not a baked image
    config). This makes customization easier."""
    nginx = compose["services"]["nginx"]
    assert "volumes" in nginx
    assert any("nginx.conf" in v for v in nginx["volumes"])


def test_compose_redis_has_persistence(compose):
    """Redis should have a volume for persistence (in case
    SSE pub/sub is enabled and events matter)."""
    redis = compose["services"]["redis"]
    assert "volumes" in redis
    assert any("redis-data" in v for v in redis["volumes"])
    # The command should set sensible memory limits
    cmd = redis.get("command", "")
    assert "maxmemory" in cmd


def test_compose_neo4j_has_auth(compose):
    """Neo4j should have authentication configured."""
    neo4j = compose["services"]["neo4j"]
    assert "NEO4J_AUTH" in neo4j["environment"]


def test_compose_neo4j_exposes_bolt_port(compose):
    """Neo4j should expose the Bolt port (7687) for the
    kgpipeline integration to connect."""
    neo4j = compose["services"]["neo4j"]
    assert "expose" in neo4j
    assert "7687" in neo4j["expose"]


def test_compose_volumes_match_mounts(compose):
    """Every volume mount should reference a defined volume."""
    defined_volumes = set(compose.get("volumes", {}).keys())
    for service_name, service in compose["services"].items():
        for mount in service.get("volumes", []):
            # mount format: "name:path" or "path" (anonymous)
            if ":" in mount and not mount.startswith("./") and not mount.startswith("/"):
                vol_name = mount.split(":")[0]
                if not vol_name.startswith(("./", "/")):
                    assert vol_name in defined_volumes, \
                        f"service {service_name} uses undeclared volume {vol_name}"


def test_compose_network_is_private(compose):
    """All services should be on a single private network
    (dpo-net). Only Nginx should be exposed to the host."""
    network = compose["networks"]
    assert "dpo-net" in network
    # Web is exposed via the network but not via ports
    web = compose["services"]["web"]
    assert "ports" not in web  # only "expose", not "ports"
    # Nginx is the only one with host-port mapping
    assert "ports" in compose["services"]["nginx"]


# ─── Dockerfile structure ──────────────────────────────────────────

@pytest.fixture(scope="module")
def dockerfile_text():
    with open(REPO_ROOT / "Dockerfile") as f:
        return f.read()


def test_dockerfile_uses_python_311(dockerfile_text):
    """The Dockerfile should use Python 3.11 (matches the
    kgpipeline and dpo-agent)."""
    assert "python:3.11" in dockerfile_text


def test_dockerfile_has_build_and_runtime_stages(dockerfile_text):
    """Multi-stage build: a `build` stage and a `runtime`
    stage. The final image is built from the `runtime`
    stage."""
    assert "FROM python:3.11-slim AS build" in dockerfile_text
    assert "FROM python:3.11-slim AS runtime" in dockerfile_text


def test_dockerfile_copies_dpo_agent_source(dockerfile_text):
    """The Dockerfile should copy the dpo_agent directory."""
    assert "COPY dpo_agent ./dpo_agent" in dockerfile_text


def test_dockerfile_installs_server_extra(dockerfile_text):
    """The Dockerfile should install the [server] extra
    (FastAPI + uvicorn)."""
    assert 'pip install' in dockerfile_text
    assert '"[server]"' in dockerfile_text or '.[server]' in dockerfile_text


def test_dockerfile_copies_kgpipeline(dockerfile_text):
    """The Dockerfile should copy kgpipeline from the
    sibling repo (so the dpo-agent container can use the
    kgpipeline integration)."""
    assert "KGPIPELINE_PATH" in dockerfile_text
    assert "kgpipeline" in dockerfile_text


def test_dockerfile_creates_non_root_user(dockerfile_text):
    """The Dockerfile should create a non-root user for
    security."""
    assert "useradd" in dockerfile_text or "adduser" in dockerfile_text
    assert "USER dpo" in dockerfile_text or "USER app" in dockerfile_text


def test_dockerfile_has_healthcheck(dockerfile_text):
    """The Dockerfile should define a HEALTHCHECK."""
    assert "HEALTHCHECK" in dockerfile_text
    assert "/healthz" in dockerfile_text


def test_dockerfile_exposes_port_8000(dockerfile_text):
    """The Dockerfile should expose port 8000."""
    assert "EXPOSE 8000" in dockerfile_text


def test_dockerfile_runs_uvicorn_with_workers(dockerfile_text):
    """The CMD should be uvicorn with multiple workers
    (for production throughput)."""
    assert "uvicorn" in dockerfile_text
    assert "--workers" in dockerfile_text


# ─── Nginx config ──────────────────────────────────────────────────

@pytest.fixture(scope="module")
def nginx_text():
    with open(DOCKER_DIR / "nginx.conf") as f:
        return f.read()


def test_nginx_disables_proxy_buffering_for_sse(nginx_text):
    """The SSE endpoint must have proxy_buffering off —
    without this, Nginx buffers the entire response and the
    client sees nothing until the pipeline completes."""
    # Find the /pipeline/stream location block.
    match = re.search(
        r"location\s*=\s*/pipeline/stream\s*\{([^}]+)\}",
        nginx_text, re.DOTALL
    )
    assert match, "/pipeline/stream location block not found"
    block = match.group(1)
    assert "proxy_buffering off" in block


def test_nginx_disables_proxy_cache_for_sse(nginx_text):
    """The SSE endpoint should have proxy_cache off."""
    match = re.search(
        r"location\s*=\s*/pipeline/stream\s*\{([^}]+)\}",
        nginx_text, re.DOTALL
    )
    block = match.group(1)
    assert "proxy_cache off" in block


def test_nginx_has_long_timeout_for_sse(nginx_text):
    """The SSE endpoint should have a long timeout (the
    pipeline can take 30-60s)."""
    match = re.search(
        r"location\s*=\s*/pipeline/stream\s*\{([^}]+)\}",
        nginx_text, re.DOTALL
    )
    block = match.group(1)
    # Should have proxy_read_timeout 600s or longer
    assert re.search(r"proxy_read_timeout\s+\d+s", block), \
        "SSE endpoint should have proxy_read_timeout"


def test_nginx_supports_chunked_transfer_encoding_for_sse(nginx_text):
    """The SSE endpoint should have chunked_transfer_encoding
    on (SSE uses chunked transfer)."""
    match = re.search(
        r"location\s*=\s*/pipeline/stream\s*\{([^}]+)\}",
        nginx_text, re.DOTALL
    )
    block = match.group(1)
    assert "chunked_transfer_encoding" in block


def test_nginx_has_rate_limiting(nginx_text):
    """Nginx should have rate limit zones defined (for
    API + static)."""
    assert "limit_req_zone" in nginx_text
    assert "rate=" in nginx_text
    # The pipeline endpoint should have a rate limit applied
    assert "limit_req" in nginx_text


def test_nginx_defines_upstream(compose, nginx_text):
    """Nginx should have an upstream block pointing to the
    web service."""
    assert "upstream" in nginx_text
    # The upstream should reference "web:8000" (Docker network)
    assert "web:8000" in nginx_text


def test_nginx_has_security_headers(nginx_text):
    """Nginx should add basic security headers."""
    assert "X-Frame-Options" in nginx_text
    assert "X-Content-Type-Options" in nginx_text


def test_nginx_healthcheck_unauthenticated(nginx_text):
    """The /healthz location should be excluded from rate
    limiting (so docker healthchecks work)."""
    # The /healthz location block should be present
    assert "location = /healthz" in nginx_text
    # It should not have a limit_req
    match = re.search(
        r"location\s*=\s*/healthz\s*\{([^}]+)\}",
        nginx_text, re.DOTALL
    )
    if match:
        assert "limit_req" not in match.group(1)


# ─── .env.example structure ────────────────────────────────────────

@pytest.fixture(scope="module")
def env_example_text():
    with open(REPO_ROOT / ".env.example") as f:
        return f.read()


def test_env_example_has_anthropic_key(env_example_text):
    """The .env.example should have ANTHROPIC_API_KEY."""
    assert "ANTHROPIC_API_KEY=" in env_example_text


def test_env_example_has_openai_key(env_example_text):
    """The .env.example should have OPENAI_API_KEY (alternative)."""
    assert "OPENAI_API_KEY=" in env_example_text


def test_env_example_has_neo4j_config(env_example_text):
    """The .env.example should have Neo4j config (for the
    kgpipeline integration)."""
    assert "NEO4J_URI=" in env_example_text
    assert "NEO4J_USER=" in env_example_text
    assert "NEO4J_PASSWORD=" in env_example_text


def test_env_example_no_real_secrets_committed(env_example_text):
    """The .env.example should not contain real API keys
    (it should have placeholders)."""
    # Look for an actual Anthropic key format
    if re.search(r"sk-ant-[a-zA-Z0-9]{20,}", env_example_text):
        pytest.fail("Real ANTHROPIC_API_KEY detected in .env.example")
    if re.search(r"sk-[a-zA-Z0-9]{20,}", env_example_text):
        pytest.fail("Real OPENAI_API_KEY detected in .env.example")


# ─── .gitignore ────────────────────────────────────────────────────

def test_gitignore_excludes_env():
    """The .gitignore should exclude .env (secrets)."""
    with open(REPO_ROOT / ".gitignore") as f:
        content = f.read()
    # Either ".env" or ".env.*" should be in the gitignore
    assert re.search(r"^\.env(\.|$|\*)", content, re.MULTILINE) or \
           re.search(r"^\.env$", content, re.MULTILINE)


def test_gitignore_excludes_data():
    """The .gitignore should exclude the data/ directory
    (SQLite DBs, etc.)."""
    with open(REPO_ROOT / ".gitignore") as f:
        content = f.read()
    assert re.search(r"^data/?", content, re.MULTILINE)


# ─── Redis SSE module ──────────────────────────────────────────────

def test_redis_sse_module_imports_without_redis():
    """The redis_sse module should import even without redis
    installed (lazy import)."""
    from dpo_agent.integrations import redis_sse
    # Without REDIS_URL set, is_redis_enabled() returns False
    # regardless of whether redis is installed.
    if "REDIS_URL" in os.environ:
        del os.environ["REDIS_URL"]
    assert redis_sse.is_redis_enabled() is False


def test_redis_sse_publish_is_noop_without_redis_url():
    """publish_event_sync should be a no-op when REDIS_URL
    is not set, even if redis is installed."""
    from dpo_agent.integrations import redis_sse
    original = os.environ.get("REDIS_URL")
    if "REDIS_URL" in os.environ:
        del os.environ["REDIS_URL"]
    try:
        # Should not raise even without REDIS_URL.
        redis_sse.publish_event_sync("test-run-id", {"type": "test"})
    finally:
        if original is not None:
            os.environ["REDIS_URL"] = original


def test_redis_sse_disabled_when_no_redis_url():
    """is_redis_enabled() returns False when REDIS_URL is unset."""
    from dpo_agent.integrations import redis_sse
    original = os.environ.get("REDIS_URL")
    if "REDIS_URL" in os.environ:
        del os.environ["REDIS_URL"]
    try:
        assert redis_sse.is_redis_enabled() is False
    finally:
        if original is not None:
            os.environ["REDIS_URL"] = original
