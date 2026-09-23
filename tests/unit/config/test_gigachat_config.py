"""Unit-тесты для GigaChatSettings: auto-detect token vs certificate auth."""

import ssl
from unittest.mock import MagicMock

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
    # auth_mode is a hard switch on LOCAL - pin it so tests don't depend on
    # whatever the real .env (loaded at import time) happens to set.
    monkeypatch.setenv("LOCAL", "True")


@pytest.fixture
def mock_load_cert_chain(monkeypatch):
    """Test fixtures use placeholder text, not real PEM - stub out the parts of
    ssl.SSLContext construction that would otherwise try to parse it."""
    load_cert_chain = MagicMock()
    monkeypatch.setattr(ssl.SSLContext, "load_cert_chain", load_cert_chain)
    monkeypatch.setattr(ssl.SSLContext, "load_verify_locations", MagicMock())
    return load_cert_chain


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


def test_cert_only_env_selects_certificate_mode(tmp_path, monkeypatch, mock_load_cert_chain):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")
    monkeypatch.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(cert_file))
    monkeypatch.setenv("GIGACHAT_KEY_FILEPATH", str(key_file))

    settings = GigaChatSettings()

    assert settings.auth_mode is GigaChatAuthMode.CERTIFICATE
    params = settings.base_params
    assert isinstance(params["ssl_context"], ssl.SSLContext)
    mock_load_cert_chain.assert_called_once_with(certfile=str(cert_file), keyfile=str(key_file))
    assert "cert_file" not in params
    assert "key_file" not in params
    assert "ca_bundle_file" not in params
    assert "credentials" not in params
    assert "scope" not in params


def test_ca_bundle_included_when_set(tmp_path, monkeypatch, mock_load_cert_chain):
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

    assert isinstance(settings.base_params["ssl_context"], ssl.SSLContext)


def test_verify_ssl_certs_false_disables_hostname_check(tmp_path, monkeypatch, mock_load_cert_chain):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")
    monkeypatch.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(cert_file))
    monkeypatch.setenv("GIGACHAT_KEY_FILEPATH", str(key_file))
    monkeypatch.setenv("GIGACHAT_VERIFY_SSL_CERTS", "False")

    settings = GigaChatSettings()
    context = settings.base_params["ssl_context"]

    assert context.check_hostname is False
    assert context.verify_mode == ssl.CERT_NONE


def test_local_true_with_both_set_raises(tmp_path, monkeypatch):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")
    monkeypatch.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(cert_file))
    monkeypatch.setenv("GIGACHAT_KEY_FILEPATH", str(key_file))

    with pytest.raises(ValueError, match="ambiguous"):
        GigaChatSettings()


def test_local_false_ignores_both_even_if_set(tmp_path, monkeypatch):
    cert_file = tmp_path / "cert.pem"
    key_file = tmp_path / "key.pem"
    cert_file.write_text("cert")
    key_file.write_text("key")
    monkeypatch.setenv("LOCAL", "False")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")
    monkeypatch.setenv("GIGACHAT_TLS_CERT_FILEPATH", str(cert_file))
    monkeypatch.setenv("GIGACHAT_KEY_FILEPATH", str(key_file))

    settings = GigaChatSettings()

    assert settings.auth_mode is None
    params = settings.base_params
    for key in ("credentials", "scope", "ssl_context", "cert_file", "key_file", "ca_bundle_file"):
        assert key not in params


def test_local_false_with_nothing_set_does_not_raise(monkeypatch):
    monkeypatch.setenv("LOCAL", "False")

    settings = GigaChatSettings()

    assert settings.auth_mode is None


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


def test_base_url_has_no_double_slash_with_default_endpoint(monkeypatch):
    """GIGACHAT_ENDPOINT defaults to '/v1' (leading slash); base_url must not
    duplicate the separator when GIGACHAT_ENDPOINT is left unset."""
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")
    monkeypatch.setenv("GIGACHAT_HOST", "gigachat-ift.sberdevices.delta.sbrf.ru")
    monkeypatch.delenv("GIGACHAT_ENDPOINT", raising=False)

    settings = GigaChatSettings()

    assert "//v1" not in settings.base_url.split("://", 1)[1]
    assert settings.base_url.endswith("/v1")


def test_base_url_endpoint_without_leading_slash_still_single_slash(monkeypatch):
    monkeypatch.setenv("LOCAL", "False")
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")
    monkeypatch.setenv("GIGACHAT_HOST", "api.giga.chat")
    monkeypatch.setenv("GIGACHAT_PORT", "")
    monkeypatch.setenv("GIGACHAT_ENDPOINT", "v1")

    settings = GigaChatSettings()

    assert settings.base_url == "http://api.giga.chat/v1"


def test_model_name_has_no_default_and_is_required(monkeypatch):
    monkeypatch.setenv("GIGACHAT_CREDENTIALS", "some-base64-credentials")
    monkeypatch.delenv("GIGACHAT_MODEL_NAME", raising=False)

    with pytest.raises(ValueError, match="GIGACHAT_MODEL_NAME"):
        GigaChatSettings()
