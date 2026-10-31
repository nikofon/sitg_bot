import json
import os
import subprocess
from pathlib import Path

import pytest

from sitg_bot.config import Settings
from sitg_bot.services.training_ai import ProviderSettings
from sitg_bot.services.training_knowledge import (
    KnowledgeApplication,
    provider_settings_from_environment,
)
from sitg_bot.services.training_provider import analysis_provider_from_settings

ROOT = Path(__file__).resolve().parents[2]
PROVIDER_ENV = ("SITG_TRAINING_AI_API_KEY", "SITG_TRAINING_AI_MODEL", "SITG_TRAINING_AI_PROVIDER")


@pytest.fixture(autouse=True)
def clean_provider_environment(monkeypatch):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.mark.parametrize("selector,canonical,endpoint", [
    (None, "openai", "https://api.openai.com/v1/responses"),
    ("", "openai", "https://api.openai.com/v1/responses"),
    ("openai", "openai", "https://api.openai.com/v1/responses"),
    ("charm", "charm", "https://hyper.charm.land/v1/responses"),
    ("hyper", "charm", "https://hyper.charm.land/v1/responses"),
])
def test_server_selects_only_the_configured_provider(monkeypatch, selector, canonical, endpoint):
    if selector is not None:
        monkeypatch.setenv(PROVIDER_ENV[2], selector)
    monkeypatch.setenv(PROVIDER_ENV[0], "fixture-key")
    monkeypatch.setenv(PROVIDER_ENV[1], "operator-selected-model")
    app = KnowledgeApplication(object())
    assert app.settings.provider == canonical
    assert app.analysis.provider.endpoint == endpoint
    assert app.analysis.provider.settings.model == "operator-selected-model"
    assert "fixture-key" not in repr(app.settings)
    assert "fixture-key" not in json.dumps(app.settings.redacted())


@pytest.mark.parametrize("selector", ["unknown", "fake", "OPENAI", " charm "])
def test_unknown_provider_fails_without_guessing(monkeypatch, selector):
    monkeypatch.setenv(PROVIDER_ENV[2], selector)
    with pytest.raises(ValueError, match="SITG_TRAINING_AI_PROVIDER"):
        provider_settings_from_environment()


@pytest.mark.parametrize("variable,value,match", [
    ("SITG_TRAINING_AI_MODEL", " ", "MODEL"),
    ("SITG_TRAINING_AI_MODEL", "glm model", "MODEL"),
    ("SITG_TRAINING_AI_API_KEY", "key\n", "API_KEY"),
    ("SITG_TRAINING_AI_API_KEY", "sk-hyper-fixture", "PROVIDER=charm"),
])
def test_invalid_configuration_fails_closed(monkeypatch, variable, value, match):
    monkeypatch.setenv(variable, value)
    with pytest.raises(ValueError, match=match):
        provider_settings_from_environment()


async def test_absent_configuration_has_no_fake_or_network_fallback():
    from sitg_bot.services.training_ai import CommonKnowledgeRequest
    settings = provider_settings_from_environment()
    assert settings.provider == "openai" and settings.model == "unconfigured"
    assert settings.api_key is None
    with pytest.raises(RuntimeError, match="not configured"):
        await analysis_provider_from_settings(settings).estimate_common_knowledge(
            CommonKnowledgeRequest("Fact", "en", "all")
        )


def test_production_factory_rejects_fake_provider():
    with pytest.raises(ValueError, match="PROVIDER"):
        analysis_provider_from_settings(ProviderSettings("fake", "m"))


def test_provider_configuration_is_owned_by_server_not_bot(monkeypatch):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    baseline = Settings(bot_token="fixture-token", _env_file=None).model_dump()
    monkeypatch.setenv(PROVIDER_ENV[0], "dummy-provider-key")
    monkeypatch.setenv(PROVIDER_ENV[1], "fixture-model")
    monkeypatch.setenv(PROVIDER_ENV[2], "charm")
    assert Settings(bot_token="fixture-token", _env_file=None).model_dump() == baseline
    settings = provider_settings_from_environment()
    assert settings.model == "fixture-model"
    assert settings.api_key == "dummy-provider-key"


@pytest.mark.parametrize("configured", [False, True])
def test_vps_compose_forwards_optional_provider_configuration_only_to_server(configured):
    env = {key: value for key, value in os.environ.items() if key not in PROVIDER_ENV}
    env.update({
        "POSTGRES_PASSWORD": "dummy-password",
        "BOT_TOKEN": "fixture-token",
        "SITG_APPLICATION_CLIENT_TOKEN": "a" * 32,
        "SITG_ADMIN_TOKEN": "b" * 32,
        "SITG_TOKEN_DELIVERY_KEY": "c" * 32,
        "APPLICATION_SECURITY_KEY": "d" * 32,
        "MINI_APP_DOMAIN": "example.test",
    })
    values = dict(zip(PROVIDER_ENV, ("dummy-provider-key", "fixture-model", "charm"), strict=True))
    if configured:
        env.update(values)
    result = subprocess.run(
        ["docker", "compose", "--env-file", "/dev/null", "-f", str(ROOT / "compose.vps.yaml"),
         "config", "--format", "json"],
        cwd=ROOT, env=env, capture_output=True, text=True, check=True, timeout=30,
    )
    services = json.loads(result.stdout)["services"]
    for name, service in services.items():
        environment = service.get("environment", {})
        for variable in PROVIDER_ENV:
            if name == "server":
                assert environment[variable] == (values[variable] if configured else "")
            else:
                assert variable not in environment
