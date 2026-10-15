"""Offline profile installation contracts, using isolated Hermes homes."""

import importlib.util
from pathlib import Path

import pytest
import yaml


SCRIPT = Path(__file__).resolve().parents[1] / "scripts/configure_hermes_bots.py"


@pytest.fixture
def setup_module():
    spec = importlib.util.spec_from_file_location("hermes_bot_setup", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dry_run_does_not_create_home(setup_module, tmp_path):
    home = tmp_path / "isolated"
    setup_module.configure(home, dry_run=True)
    assert not home.exists()


def test_install_is_idempotent_and_does_not_copy_credentials(setup_module, tmp_path):
    home = tmp_path / "isolated"
    setup_module.configure(home)
    extra = home / "profiles/unrelated"
    extra.mkdir()
    (extra / "SOUL.md").write_text("user", encoding="utf-8")
    dev = home / "profiles/coop-dev"
    config = yaml.safe_load((dev / "config.yaml").read_text(encoding="utf-8"))
    config["display"] = {"custom_user_setting": True}
    (dev / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    setup_module.configure(home)
    assert yaml.safe_load((dev / "config.yaml").read_text(encoding="utf-8"))["display"]["custom_user_setting"]
    assert (extra / "SOUL.md").read_text(encoding="utf-8") == "user"
    assert not (home / "active_profile").exists()
    assert not (home / ".env").exists()
    for name in setup_module.BOTS:
        profile = home / "profiles" / name
        assert not (profile / "auth.json").exists()
        assert "API_KEY=" not in (profile / ".env").read_text(encoding="utf-8")
        profile_config = yaml.safe_load((profile / "config.yaml").read_text(encoding="utf-8"))
        assert profile_config["fallback_providers"] == []
        assert profile_config["compression"]["enabled"] is False
        assert profile_config["auxiliary"]["title_generation"]["model_upgrade_enabled"] is False
    assert setup_module.inspect_profiles(home)[0]["status"] == "consumer_plan_restricted"


def test_key_sync_only_reads_glm_key(setup_module, tmp_path):
    source = tmp_path / "project.env"
    source.write_text("GLM_API_KEY=test-only-glm\nOPENAI_API_KEY=must-not-copy\nGEMINI_API_KEY=must-not-copy\n", encoding="utf-8")
    home = tmp_path / "isolated"
    setup_module.configure(home, sync_review_key=True, key_source=source)
    review_env = (home / "profiles/coop-review/.env").read_text(encoding="utf-8")
    assert "test-only-glm" in review_env
    assert "must-not-copy" not in review_env
    for name in ("coop-dev", "coop-expert"):
        assert "test-only-glm" not in (home / "profiles" / name / ".env").read_text(encoding="utf-8")


def test_gemini_bridge_opt_in_updates_only_selected_profile(setup_module, tmp_path):
    home = tmp_path / "isolated"
    setup_module.configure(home)
    review_soul = home / "profiles/coop-review/SOUL.md"
    review_soul.write_text("user-edited review role", encoding="utf-8")
    other_files = {
        path: path.read_bytes()
        for name in ("coop-review", "coop-expert")
        for path in (home / "profiles" / name).iterdir() if path.is_file()
    }
    setup_module.configure(home, profile_name="coop-dev", enable_gemini_bridge=True)
    config_path = home / "profiles/coop-dev/config.yaml"
    config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    assert config["antigravity_cli"]["experimental_subscription_bridge"] is True
    assert config["platform_toolsets"]["cli"] == []
    assert config["providers"]["gemini"]["enabled"] is False
    assert setup_module.inspect_profiles(home)[0]["status"] == "experimental_live_unverified"
    assert all(path.read_bytes() == contents for path, contents in other_files.items())
    # A later normal configuration update must preserve the explicit opt-in.
    setup_module.configure(home, profile_name="coop-dev")
    assert yaml.safe_load(config_path.read_text(encoding="utf-8"))["antigravity_cli"]["experimental_subscription_bridge"] is True
    setup_module.configure(home, profile_name="coop-dev", enable_gemini_bridge=False)
    assert setup_module.inspect_profiles(home)[0]["experimental_subscription_bridge"] is False


def test_unmanaged_or_edited_soul_is_never_overwritten(setup_module, tmp_path):
    home = tmp_path / "isolated"
    profile = home / "profiles/coop-review"
    profile.mkdir(parents=True)
    soul = profile / "SOUL.md"
    soul.write_text("user identity", encoding="utf-8")
    with pytest.raises(ValueError, match="unmanaged"):
        setup_module.configure(home)
    assert soul.read_text(encoding="utf-8") == "user identity"
    assert not (home / "profiles/coop-dev").exists()


def test_review_and_expert_actual_tools_are_empty(setup_module, tmp_path, monkeypatch):
    home = tmp_path / "isolated"
    setup_module.configure(home)
    from hermes_cli.tools_config import _get_platform_tools
    from agent.coding_context import coding_selection
    from model_tools import get_tool_definitions
    for name in ("coop-review", "coop-expert"):
        cfg = yaml.safe_load((home / "profiles" / name / "config.yaml").read_text(encoding="utf-8"))
        monkeypatch.setenv("HERMES_HOME", str(home / "profiles" / name))
        assert coding_selection(platform="cli", config=cfg) is None
        for platform in ("cli", "tui", "desktop", "discord", "telegram", "api-server"):
            enabled = list(_get_platform_tools(cfg, platform))
            assert enabled == []
            assert get_tool_definitions(enabled_toolsets=enabled, quiet_mode=True) == []


def test_dev_disabled_even_with_shell_key_before_transport(setup_module, tmp_path, monkeypatch):
    home = tmp_path / "isolated"
    setup_module.configure(home)
    monkeypatch.setenv("HERMES_HOME", str(home / "profiles/coop-dev"))
    monkeypatch.setenv("GEMINI_API_KEY", "test-not-a-real-key")
    from hermes_cli.runtime_provider import resolve_runtime_provider
    def forbidden_transport(*args, **kwargs):
        pytest.fail("provider guard must refuse before any transport or credential resolution")
    monkeypatch.setattr("hermes_cli.runtime_provider._ladder_rungs", forbidden_transport)
    with pytest.raises(ValueError, match="disabled"):
        resolve_runtime_provider(requested="gemini")


def test_edited_managed_soul_blocks_reinstallation(setup_module, tmp_path):
    home = tmp_path / "isolated"
    setup_module.configure(home)
    soul = home / "profiles/coop-expert/SOUL.md"
    soul.write_text("custom user soul", encoding="utf-8")
    with pytest.raises(ValueError, match="SOUL"):
        setup_module.configure(home)
    assert soul.read_text(encoding="utf-8") == "custom user soul"


@pytest.mark.parametrize("failure_step", ["config", "metadata"])
def test_partial_installation_is_safely_resumable(setup_module, tmp_path, monkeypatch, failure_step):
    home = tmp_path / "isolated"
    if failure_step == "config":
        import hermes_cli.config as module
        original = module.atomic_config_replace
        def fail(path, data, **kwargs):
            if path.parent.name == "coop-review" and "agent" in data:
                raise OSError("test interrupted config write")
            return original(path, data, **kwargs)
        monkeypatch.setattr(module, "atomic_config_replace", fail)
    else:
        import hermes_cli.profiles as module
        original = module.write_profile_meta
        def fail(path, **kwargs):
            if path.name == "coop-review" and "display_name" in kwargs:
                raise OSError("test interrupted metadata write")
            return original(path, **kwargs)
        monkeypatch.setattr(module, "write_profile_meta", fail)
    with pytest.raises(OSError, match="interrupted"):
        setup_module.configure(home)
    monkeypatch.undo()
    setup_module.configure(home)
    assert all(item["exists"] for item in setup_module.inspect_profiles(home))


def test_root_model_credentials_and_endpoint_are_never_inherited(setup_module, tmp_path):
    home = tmp_path / "isolated"
    home.mkdir()
    root = {"model": {"provider": "custom", "default": "foreign", "api_key": "sentinel-inline-key", "base_url": "https://foreign.invalid", "openai_runtime": "codex_app_server"}, "providers": {"custom": {"api_key": "sentinel-provider-key"}}}
    root_text = yaml.safe_dump(root)
    (home / "config.yaml").write_text(root_text, encoding="utf-8")
    setup_module.configure(home)
    assert (home / "config.yaml").read_text(encoding="utf-8") == root_text
    for name, bot in setup_module.BOTS.items():
        text = (home / "profiles" / name / "config.yaml").read_text(encoding="utf-8")
        assert "sentinel" not in text
        assert "foreign.invalid" not in text
        cfg = yaml.safe_load(text)
        assert cfg["model"] == setup_module.desired_config(bot)["model"]
        assert cfg["providers"] == setup_module.desired_config(bot).get("providers", {})
