"""The mode resolver: which exchange, which settings file, which journal.

Worth its own exhaustive test file despite the module's size, because every
function here answers a question whose wrong answer trades real money against
the wrong account or writes fabricated fills into the live audit trail."""

import ast
from pathlib import Path

import pytest

import env

# Captured at import, which happens during collection and therefore before any
# fixture has run. See unpatched_env below.
_REAL_LOG_DIR = env.log_dir


@pytest.fixture(autouse=True)
def unpatched_env(monkeypatch):
    """Undo conftest's journal guard, for this file only.

    conftest.no_real_journal redirects env.log_dir to a tmp directory so that
    no test anywhere can write to the operator's journal. That is right for
    every other file and wrong for this one: these tests are what checks
    env.log_dir itself, and against a stub they would check the stub. A
    module-level autouse fixture runs after the conftest one, so this restores
    the real function for the duration of each test here - and nothing in this
    file writes a journal, so the guard has nothing to guard."""
    monkeypatch.setattr(env, "log_dir", _REAL_LOG_DIR)


def test_modes_are_exactly_test_and_live():
    assert env.MODES == (env.TEST, env.LIVE)
    assert env.TEST == "test"
    assert env.LIVE == "live"


@pytest.mark.parametrize("mode", ["test", "live"])
def test_validate_returns_a_valid_mode(mode):
    assert env.validate(mode) == mode


@pytest.mark.parametrize(
    "mode",
    [
        "tets",       # a typo
        "TEST",       # case matters; no silent normalisation
        "testnet",    # the old settings.json vocabulary
        "prod",
        "",
        None,
        True,         # the old boolean flag, arriving by some stale caller
        0,
    ],
)
def test_validate_rejects_anything_else(mode):
    with pytest.raises(ValueError):
        env.validate(mode)


def test_validate_names_the_valid_modes_in_its_message():
    """The operator reading this traceback is mid-restart. Tell them the answer."""
    with pytest.raises(ValueError) as excinfo:
        env.validate("tets")
    message = str(excinfo.value)
    assert "tets" in message
    assert "test" in message and "live" in message


@pytest.mark.parametrize("mode", ["tets", "TEST", "", None, True])
def test_every_accessor_rejects_an_invalid_mode(mode):
    """Fail closed, everywhere.

    A resolver that raises on validate() but quietly answers is_testnet() with
    False has picked LIVE for a mode nobody recognised. Each entry point
    validates rather than trusting a caller to have done it."""
    for fn in (env.is_testnet, env.label, env.settings_path, env.log_dir):
        with pytest.raises(ValueError):
            fn(mode)


def test_is_testnet_is_true_only_for_test():
    assert env.is_testnet(env.TEST) is True
    assert env.is_testnet(env.LIVE) is False


def test_labels_are_ascii():
    """Printed by the startup banner on a cp949 console, which cannot encode
    anything outside ASCII and has no error handler in front of it."""
    for mode in env.MODES:
        label = env.label(mode)
        assert label == label.encode("ascii").decode("ascii")

    assert env.label(env.TEST) == "TESTNET"
    assert env.label(env.LIVE) == "LIVE"


def test_settings_path_is_per_mode_and_under_futures():
    futures = Path(env.__file__).resolve().parent

    for mode in env.MODES:
        assert Path(env.settings_path(mode)) == futures / mode / "settings.json"

    assert env.settings_path(env.TEST) != env.settings_path(env.LIVE)


def test_log_dir_is_per_mode_and_under_futures():
    futures = Path(env.__file__).resolve().parent

    for mode in env.MODES:
        assert Path(env.log_dir(mode)) == futures / mode / "logs"

    assert env.log_dir(env.TEST) != env.log_dir(env.LIVE)


def test_paths_are_absolute():
    """The bot is documented as runnable from either the repo root or from
    inside futures/. A relative path would resolve to a different journal
    depending on the working directory the operator happened to start from."""
    for mode in env.MODES:
        assert Path(env.settings_path(mode)).is_absolute()
        assert Path(env.log_dir(mode)).is_absolute()


def test_no_credentials_are_imported():
    """env must stay importable on a clone with no futures/config.py.

    config.py is gitignored, so a module that reaches for credentials cannot be
    imported during test collection on a fresh checkout - the breakage commit
    7a6d4a0 had to fix in the notify tests. Credential selection belongs to
    client.py, which already imports config.

    Checked against the parsed import statements rather than the source text,
    so that naming the rule in a docstring does not trip the rule."""
    tree = ast.parse(Path(env.__file__).read_text())

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])

    assert "config" not in imported
    assert not hasattr(env, "config")
