import pytest


@pytest.fixture(autouse=True)
def _claude_config(tmp_path_factory, monkeypatch):
    """Skills install into the user-level Claude config dir: keep tests out of the real one."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))
