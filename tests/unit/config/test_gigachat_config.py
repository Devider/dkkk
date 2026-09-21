"""Unit-тесты для GigaChatSettings: auto-detect token vs certificate auth."""

import pytest

from aigw_service.config.gigachat.config import GigaChatAuthMode, GigaChatSettings

ENV_VARS = (
    "GIGACHAT_CREDENTIALS",
    "GIGACHAT_TLS_CERT_FILEPATH",
    "GIGACHAT_KEY_FILEPATH",
    "GIGACHAT_CA_BUNDLE_FILEPATH",
)


@pytest.fixture(autouse=True)
def _clear_gigachat_env(monkeypatch):
    """Разрывает утечку переменных из реального .env, загруженного при импорте."""
    for var in ENV_VARS:
        monkeypatch.delenv(var, raising=False)
    # model has no default (fail fast) - set a value so unrelated tests don't
    # depend on GIGACHAT_MODEL_NAME being present in the real .env.
    monkeypatch.setenv("GIGACHAT_MODEL_NAME", "GigaChat-2")


def test_token_only_env_selects_token_mode(monkeypatch):
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")

    settings = GigaChatSettings()

    assert settings.auth_mode is GigaChatAuthMode.TOKEN
    params = settings.base_params
    assert params["credentials"] == "some-base64-credentials"
    assert params["scope"] == "GIGACHAT_API_PERS"
    assert "cert_file" not in params
    assert "key_file" not in params
    assert "ca_bundle_file" not in params


def test_cert_only_env_selects_certificate_mode(tmp_path, monkeypatch):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")
    monkeypatch.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(cert_file))
    monkeypatch.setenv("GIGACHAT_KEY_FILEPATH", str(key_file))

    settings = GigaChatSettings()

    assert settings.auth_mode is GigaChatAuthMode.CERTIFICATE
    params = settings.base_params
    assert params["cert_file"] == str(cert_file)
    assert params["key_file"] == str(key_file)
    assert "credentials" not in params
    assert "scope" not in params


def test_ca_bundle_included_when_set(tmp_path, monkeypatch):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    ca_bundle_file = tmp_path / "ca.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")
    ca_bundle_file.write_text("ca")
    monkeypatch.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(cert_file))
    monkeypatch.setenv("GIGACHAT_KEY_FILEPATH", str(key_file))
    monkeypatch.setenv("GIGACHAT_CA_BUNDLE_FILEPATH", str(ca_bundle_file))

    settings = GigaChatSettings()

    assert settings.base_params["ca_bundle_file"] == str(ca_bundle_file)


def test_both_token_and_cert_set_raises(tmp_path, monkeypatch):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")
    monkeypatch.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(cert_file))
    monkeypatch.setenv("GIGACHAT_KEY_FILEPATH", str(key_file))

    with pytest.raises(ValueError, match="ambiguous"):
        GigaChatSettings()


def test_neither_token_nor_cert_set_raises():
    with pytest.raises(ValueError, match="not configured"):
        GigaChatSettings()


def test_cert_mode_missing_key_raises(tmp_path, monkeypatch):
    cert_file = tmp_path / "cert.pem"
    cert_file.write_text("cert")
    monkeypatch.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(cert_file))

    with pytest.raises(ValueError, match="requires both"):
        GigaChatSettings()


def test_cert_mode_missing_file_on_disk_raises(tmp_path, monkeypatch):
    missing_cert = tmp_path / "does-not-exist.pem"
    key_file = tmp_path / "key.pem"
    key_file.write_text("key")
    monkeypatch.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(missing_cert))
    monkeypatch.setenv("GIGACHAT_KEY_FILEPATH", str(key_file))

    with pytest.raises(FileNotFoundError):
        GigaChatSettings()


def test_base_url_omits_port_when_unset(monkeypatch):
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")
    monkeypatch.setenv("GIGACHAT_HOST", "api.giga.chat")
    monkeypatch.setenv("GIGACHAT_PORT", "")

    settings = GigaChatSettings()

    assert ":" not in settings.base_url.split("//", 1)[1].split("/", 1)[0]


def test_base_url_includes_port_when_set(monkeypatch):
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")
    monkeypatch.setenv("GIGACHAT_HOST", "localhost")
    monkeypatch.setenv("GIGACHAT_PORT", "8443")

    settings = GigaChatSettings()

    assert "localhost:8443" in settings.base_url


def test_model_name_has_no_default_and_is_required(monkeypatch):
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")
    monkeypatch.delenv("GIGACHAT_MODEL_NAME", raising=False)

    with pytest.raises(ValueError, match="GIGACHAT_MODEL_NAME"):
        GigaChatSettings()
