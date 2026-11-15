"""The public CLI delegates installation/removal to the host plugin manager."""

from unittest.mock import patch

from hermes_lark_streaming import __main__ as cli


def test_uninstall_uses_host_manager_without_legacy_removal():
    with (
        patch("sys.argv", ["hermes-lark-streaming", "uninstall"]),
        patch.object(
            cli,
            "manager_command",
            return_value=0,
        ) as manager,
    ):
        assert cli.main() == 0
    manager.assert_called_once_with("remove", "hermes-lark-streaming")


def test_install_refuses_to_publish_an_uncommitted_tree():
    with (
        patch("sys.argv", ["hermes-lark-streaming", "install"]),
        patch(
            "hermes_lark_streaming.native_plugin.check_host",
        ),
        patch.object(cli.subprocess, "check_output", return_value=" M hermes_lark_streaming/native_adapter.py\n"),
        patch.object(
            cli,
            "manager_command",
        ) as manager,
    ):
        assert cli.main() == 1
    manager.assert_not_called()


def test_verify_reports_residue_as_incompatible(capsys):
    with (
        patch("sys.argv", ["hermes-lark-streaming", "verify"]),
        patch.object(
            cli,
            "native_status",
            return_value={"compatible": False, "legacy_injections": ["old/run_turn.py"]},
        ),
    ):
        assert cli.main() == 1
    assert "old/run_turn.py" in capsys.readouterr().out
