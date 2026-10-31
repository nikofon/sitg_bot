import json
import os
import subprocess
from pathlib import Path

import pytest

from sitg_bot.config import Settings
from sitg_bot.services.training_knowledge import provider_settings_from_environment

ROOT = Path(__file__).resolve().parents[2]
PROVIDER_ENV = ("SITG_TRAINING_AI_API_KEY", "SITG_TRAINING_AI_MODEL")


def test_provider_configuration_is_owned_by_server_not_bot(monkeypatch):
    for name in PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)
    baseline = Settings(bot_token="fixture-token", _env_file=None).model_dump()
    monkeypatch.setenv(PROVIDER_ENV[0], "dummy-provider-key")
    monkeypatch.setenv(PROVIDER_ENV[1], "fixture-model")
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
    values = dict(zip(PROVIDER_ENV, ("dummy-provider-key", "fixture-model"), strict=True))
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
