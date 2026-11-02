"""Run modes (ADR-0008): production refuses laptop-only settings, development warns about them."""

import logging

import pytest
from pydantic import ValidationError

from aigw import check
from aigw.auth import hash_key
from aigw.config import GatewayConfig
from aigw.main import create_app


def production_config(**overrides) -> dict:
    config = {
        "mode": "production",
        "vault": {"address": "https://vault.vault.svc:8200", "auth": "kubernetes",
                  "kubernetes_role": "aigw"},
        "store": {"backend": "redis", "url": "rediss://redis.example.internal:6379/0"},
        "models": [{"name": "mock-echo", "provider": "mock"}],
        "teams": [{"name": "ci", "key_sha256": [hash_key("a-real-generated-key")]}],
    }
    return {**config, **overrides}


def test_production_config_that_is_safe_starts():
    config = GatewayConfig.model_validate(production_config())
    assert config.production_issues() == []
    create_app(config)


@pytest.mark.parametrize(("override", "issue"), [
    ({"teams": [{"name": "ci", "key_sha256": [hash_key("dev-platform-key")]}]}, "demo keys"),
    ({"store": {"backend": "memory"}}, "store: memory"),
    ({"store": {"backend": "redis", "url": "redis://redis:6379/0"}}, "rediss://"),
    ({"vault": {"address": "http://vault:8200", "auth": "kubernetes", "kubernetes_role": "aigw"}},
     "vault: address"),
    ({"vault": {"address": "https://vault:8200", "auth": "token"}}, "token auth"),
    ({"oidc": {"issuer": "http://localhost:8180/realms/aigw", "audience": "aigw"}}, "oidc: issuer"),
    ({"models": [{"name": "llm", "provider": "openai_compat", "base_url": "https://llm.internal/v1",
                  "api_key_env": "LLM_KEY"}]}, "env vars"),
])
def test_production_refuses_laptop_settings(override, issue):
    with pytest.raises(ValidationError, match=issue):
        GatewayConfig.model_validate(production_config(**override))
    # The same settings are allowed in development, where they are only reported.
    dev = GatewayConfig.model_validate(production_config(**override, mode="development"))
    assert any(issue in found for found in dev.production_issues())


def test_development_mode_warns_at_startup(caplog):
    config = GatewayConfig.model_validate(production_config(mode="development",
                                                            store={"backend": "memory"}))
    with caplog.at_level(logging.WARNING, logger="aigw"):
        create_app(config)
    assert "development mode" in caplog.text and "store: memory" in caplog.text


def test_team_needs_keys_or_groups_and_groups_need_oidc():
    with pytest.raises(ValidationError, match="needs key_sha256 or oidc_groups"):
        GatewayConfig.model_validate(production_config(teams=[{"name": "ci"}]))
    with pytest.raises(ValidationError, match="no oidc section"):
        GatewayConfig.model_validate(production_config(teams=[{"name": "eng",
                                                               "oidc_groups": ["eng"]}]))


def test_check_command(tmp_path, capsys):
    import yaml
    good, bad = tmp_path / "good.yaml", tmp_path / "bad.yaml"
    good.write_text(yaml.safe_dump(production_config(mode="development")))
    bad.write_text(yaml.safe_dump(production_config(mode="development",
                                                    store={"backend": "memory"})))
    assert check.main(["--production", str(good)]) == 0
    assert check.main([str(bad)]) == 0  # without --production it only reports
    assert check.main(["--production", str(bad)]) == 1
    assert "not production-ready: store: memory" in capsys.readouterr().out
    assert check.main([str(tmp_path / "missing.yaml")]) == 1
