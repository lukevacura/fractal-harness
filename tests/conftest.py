import pytest


@pytest.fixture(autouse=True)
def _claude_config(tmp_path_factory, monkeypatch):
    """Skills install into the user-level Claude config dir: keep tests out of the real one."""
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path_factory.mktemp("claude-config")))


@pytest.fixture(autouse=True)
def _root_signal_flag(monkeypatch):
    """The shipped root signal is "tty" (confirm on the controlling terminal). Tests run without one, so they use
    "flag" (a terminal on stdin, or --yes), the behaviour they were written against; test_root_signal.py sets "tty"."""
    from fractal_harness import root_signal
    monkeypatch.setattr(root_signal, "DEFAULT", "flag")
