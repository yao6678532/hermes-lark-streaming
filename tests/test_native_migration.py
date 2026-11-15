from copy import deepcopy

from hermes_lark_streaming.migration import prepare_config


def test_native_config_migration_preserves_design_credentials_and_other_plugins():
    original = {
        "streaming": {"enabled": True, "width_mode": "default", "reasoning_mode": "merged", "footer_fields": ["model"]},
        "display": {"platforms": {"feishu": {"show_reasoning": True, "clarify_style": "card"}}},
        "feishu": {"app_id": "existing-bot", "app_secret": "existing-secret"},
        "plugins": {
            "enabled": ["my-existing-plugin"],
            "disabled": ["other", "hermes-lark-streaming"],
            "entries": {"my-existing-plugin": {"custom": True}},
        },
        "cron": {"existing": "preserved"},
    }
    before = deepcopy(original)
    config, changes = prepare_config(original)
    assert original == before
    assert config["feishu"] == before["feishu"]
    assert config["display"] == before["display"]
    assert config["cron"] == before["cron"]
    assert config["plugins"]["entries"] == before["plugins"]["entries"]
    assert config["plugins"]["disabled"] == ["other"]
    assert "my-existing-plugin" in config["plugins"]["enabled"]
    assert config["plugins"]["stream_reasoning_deltas"] is True
    assert config["streaming"]["reasoning_mode"] == "merged"
    assert all(change["path"].startswith(("plugins.", "streaming.")) for change in changes)
    assert prepare_config(config) == (config, [])


def test_native_config_migration_keeps_explicit_transport_tuning_and_disabled_streaming():
    original = {"streaming": {"enabled": True, "edit_interval": 0.4, "buffer_threshold": 10}}
    config, _changes = prepare_config(original)
    assert config["streaming"] == original["streaming"]
    disabled = {"streaming": {"enabled": False}, "plugins": {"disabled": ["hermes-lark-streaming"]}}
    assert prepare_config(disabled) == (disabled, [])
