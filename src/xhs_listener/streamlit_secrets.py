"""Copy Streamlit Cloud secrets into os.environ.

Community Cloud stores credentials in st.secrets, while this app (and TikHub /
OpenAI clients) reads os.getenv. Flatten scalar keys so a secrets.toml paste
from .env.example works without changing every call site.
"""
from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


def copy_secrets_to_environ(
    secrets: Mapping[str, Any],
    environ: MutableMapping[str, str],
    *,
    override: bool = True,
) -> None:
    """Write scalar secret keys into environ. Nested tables are flattened one level."""

    for key, value in secrets.items():
        if isinstance(value, Mapping):
            copy_secrets_to_environ(value, environ, override=override)
            continue
        if value is None or isinstance(value, (list, tuple, dict)):
            continue
        name = str(key).strip()
        if not name:
            continue
        if not override and name in environ:
            continue
        environ[name] = str(value)
