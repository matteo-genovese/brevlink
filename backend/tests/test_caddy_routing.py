from __future__ import annotations

from pathlib import Path


def test_legal_routes_are_explicitly_before_short_link_matcher():
    caddyfile = (Path(__file__).resolve().parents[2] / "Caddyfile").read_text(encoding="utf-8")
    legal_start = caddyfile.index("@legal path")
    short_start = caddyfile.index("@short path_regexp")
    assert legal_start < short_start
    legal_matcher = caddyfile[legal_start:caddyfile.index("\n", legal_start)]
    for page in ("privacy", "terms", "cookies", "legal", "subprocessors"):
        assert f"/{page}" in legal_matcher
        assert f"/{page}/" in legal_matcher
    for route in ("handle /api/*", "handle_path /app/*", "handle /health", "@short path_regexp short"):
        assert route in caddyfile


def test_every_exact_path_answered_before_the_redirect_is_reserved(monkeypatch):
    """A path Caddy answers itself never reaches the {slug} redirect: no clicks.

    The open-status read omits reserved slugs instead of measuring them; this
    keeps that set in step with the file the deployment actually loads, so a
    new exact route added here cannot be answered as a link.
    """
    # This module needs no app fixture: give settings what it requires to load
    # the constant rather than a running app.
    monkeypatch.setenv("JWT_SECRET", "test-secret-that-is-long-enough-for-dev")
    from app.services.link_stats import RESERVED_SLUGS

    caddyfile = (Path(__file__).resolve().parents[2] / "Caddyfile").read_text(encoding="utf-8")
    legal = caddyfile[caddyfile.index("@legal path"):]
    legal = legal[:legal.index("\n")]
    legal_paths = {part.strip("/") for part in legal.split("path", 1)[1].split()}
    # /api/*, /docs, /redoc, /openapi.json, /health, /app and /app/*.
    exact = (legal_paths | {"api", "docs", "redoc", "openapi.json", "health", "app"}) - {"openapi.json"}
    assert {"privacy", "terms", "cookies", "legal", "subprocessors"} <= legal_paths
    assert exact <= RESERVED_SLUGS, exact - RESERVED_SLUGS
    # a dot cannot be in a slug, so that one is listed for the record only
    assert "openapi.json" in RESERVED_SLUGS


def test_saas_leg_serves_any_host_on_its_own_port():
    """Cloudflare for SaaS reaches Caddy with the customer hostname while the SNI
    is our fallback origin: the site must have no host matcher, or every customer
    domain would fall through."""
    caddyfile = (Path(__file__).resolve().parents[2] / "Caddyfile").read_text(encoding="utf-8")
    assert ":443 {" in caddyfile
    # Plain `tls internal` covers only names the site declares, and this site
    # declares none: without on_demand, Cloudflare gets no certificate for the
    # fallback origin and every customer domain fails with a 525.
    assert "on_demand" in caddyfile
    assert "issuer internal" in caddyfile
    # No certificate path may be hardcoded: a fresh clone has no such file and
    # Caddy refuses to start ("loading certificates: no such file or directory"),
    # which would break every self-hosted deployment.
    assert "/data/certs/" not in caddyfile
    # The certificate belongs to the fallback origin, so no host-specific site
    # may claim port 443: that would shadow the catch-all for customer hostnames.
    assert "proxy.brevl.ink:443 {" not in caddyfile


def test_deployment_overrides_are_optional_globs():
    """Deployment-local hardening must be injectable without editing this file,
    and a clone that ships no override files must stay valid as it is."""
    caddyfile = (Path(__file__).resolve().parents[2] / "Caddyfile").read_text(encoding="utf-8")
    assert "import /data/caddy/overrides/global/*.caddyfile" in caddyfile
    assert "import /data/caddy/overrides/tls/*.caddyfile" in caddyfile


def test_client_authentication_is_not_baked_into_the_default_config():
    """Enforcement must never ship in the image: it would break every self-hosted
    deployment without Cloudflare - and, because the production host pulls this
    image automatically, it would also arm itself on the live origin the moment it
    is pushed, before Authenticated Origin Pulls is switched on at Cloudflare."""
    caddyfile = (Path(__file__).resolve().parents[2] / "Caddyfile").read_text(encoding="utf-8")
    assert "client_auth" not in caddyfile
    assert "trust_pool" not in caddyfile


def test_routing_is_shared_not_duplicated():
    caddyfile = (Path(__file__).resolve().parents[2] / "Caddyfile").read_text(encoding="utf-8")
    assert caddyfile.count("import routing") >= 2
    assert caddyfile.count("@short path_regexp") == 1
