from xhs_listener.streamlit_secrets import copy_secrets_to_environ


def test_copy_secrets_writes_scalars_and_flattens_tables() -> None:
    environ: dict[str, str] = {"KEEP": "old"}
    copy_secrets_to_environ(
        {
            "APP_LOGIN_USERNAME": "admin",
            "nested": {"AZURE_OPENAI_API_KEY": "secret"},
            "SKIP_LIST": ["nope"],
            "": "ignored",
        },
        environ,
    )
    assert environ["APP_LOGIN_USERNAME"] == "admin"
    assert environ["AZURE_OPENAI_API_KEY"] == "secret"
    assert "SKIP_LIST" not in environ
    assert environ["KEEP"] == "old"


def test_copy_secrets_can_preserve_existing_env() -> None:
    environ = {"APP_LOGIN_USERNAME": "from-env"}
    copy_secrets_to_environ(
        {"APP_LOGIN_USERNAME": "from-secrets"},
        environ,
        override=False,
    )
    assert environ["APP_LOGIN_USERNAME"] == "from-env"
