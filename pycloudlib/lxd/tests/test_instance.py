"""Tests for pycloudlib.lxd.instance."""
import re
from unittest import mock

import pytest

from pycloudlib.lxd.instance import LXDInstance, LXDVirtualMachineInstance
from pycloudlib.result import Result


class TestRestart:
    """Tests covering pycloudlib.lxd.instance.Instance.restart."""

    @pytest.mark.parametrize("force", (False, True))
    @mock.patch("pycloudlib.lxd.instance.subp")
    def test_restart_calls_lxc_cmd_with_force_param(self, m_subp, force):
        """Honor force param on restart."""
        instance = LXDInstance(name="my_vm")
        instance._do_restart(force=force)  # pylint: disable=protected-access
        if force:
            assert "--force" in m_subp.call_args[0][0]
        else:
            assert "--force" not in m_subp.call_args[0][0]

    @mock.patch("pycloudlib.lxd.instance.LXDInstance.shutdown")
    @mock.patch("pycloudlib.lxd.instance.subp")
    def test_restart_does_not_shutdown(self, _m_subp, m_shutdown):
        """Don't shutdown (stop) instance on restart."""
        instance = LXDInstance(name="my_vm")
        instance._do_restart()  # pylint: disable=protected-access
        assert not m_shutdown.called


class TestExecute:
    """Tests covering pycloudlib.lxd.instance.Instance.execute."""

    def test_all_rcs_acceptable_when_using_exec(self):
        """Test that we invoke util.subp with rcs=None for exec calls.

        rcs=None means that we will get a Result object back for all return
        codes, rather than an exception for non-zero return codes.
        """
        instance = LXDInstance(None, execute_via_ssh=False)
        with mock.patch("pycloudlib.lxd.instance.subp") as m_subp:
            instance.execute("some_command")
        assert 1 == m_subp.call_count
        args, kwargs = m_subp.call_args
        assert "exec" in args[0]
        assert kwargs.get("rcs", mock.sentinel.not_none) is None


class TestVirtualMachineXenialAgentOperations:  # pylint: disable=W0212
    """Tests covering pycloudlib.lxd.instance.LXDVirtualMachineInstance."""

    # Key information we want in the logs when using non-ssh Xenial instances.
    _missing_agent_msg = "missing lxd-agent"

    @mock.patch("pycloudlib.lxd.instance.subp")
    def test_exec_with_run_command_on_xenial_machine(self, m_subp, caplog):
        """Test exec does not work with xenial vm."""
        instance = LXDVirtualMachineInstance(
            None, execute_via_ssh=False, series="xenial"
        )

        instance._run_command(["test"], None)
        assert self._missing_agent_msg in caplog.text
        assert m_subp.call_count == 1

    @mock.patch("pycloudlib.lxd.instance.subp")
    def test_file_pull_with_agent_on_xenial_machine(self, m_subp, caplog):
        """Test file pull does not work with xenial vm."""
        instance = LXDVirtualMachineInstance(
            None, execute_via_ssh=False, series="xenial"
        )

        instance.pull_file("/some/file", "/some/local/file")
        assert self._missing_agent_msg in caplog.text
        assert m_subp.call_count == 1

    @mock.patch("pycloudlib.lxd.instance.subp")
    def test_file_push_with_agent_on_xenial_machine(self, m_subp, caplog):
        """Test file push does not work with xenial vm."""
        instance = LXDVirtualMachineInstance(
            None, execute_via_ssh=False, series="xenial"
        )

        instance.push_file("/some/file", "/some/local/file")
        assert self._missing_agent_msg in caplog.text
        assert m_subp.call_count == 1
        expected_msg = (
            "Many Xenial images do not support `lxc file push` due to missing"
            " lxd-agent: you may see unavoidable failures.\n"
            "See https://github.com/canonical/pycloudlib/issues/132 for"
            " details."
        )
        assert expected_msg in caplog.messages
        assert m_subp.call_count == 1


class TestIP:
    """Tests covering pycloudlib.lxd.instance.Instance.ip."""

    @pytest.mark.parametrize(
        "stdouts,stderr,return_code,sleeps,expected",
        (
            (
                ["unparseable"],
                "",
                0,
                150,
                TimeoutError(
                    "Unable to determine IP address after 150 retries."
                    " exit:0 stdout: unparseable stderr: "
                ),
            ),
            (  # retry on non-zero exit code
                ["10.0.0.1 (eth0)"],
                "",
                1,
                150,
                TimeoutError(
                    "Unable to determine IP address after 150 retries."
                    " exit:1 stdout: 10.0.0.1 (eth0) stderr: "
                ),
            ),
            (  # empty values will retry indefinitely
                [""],
                "",
                0,
                150,
                TimeoutError(
                    "Unable to determine IP address after 150 retries."
                    " exit:0 stdout:  stderr: "
                ),
            ),
            (  # only retry until success
                ["unparseable", "10.69.10.5 (eth0)\n"],
                "",
                0,
                1,
                "10.69.10.5",
            ),
            (["10.69.10.5 (eth0)\n"], "", 0, 0, "10.69.10.5"),
        ),
    )
    @mock.patch("pycloudlib.lxd.instance.time.sleep")
    @mock.patch("pycloudlib.lxd.instance.subp")
    def test_ip_parses_ipv4_output_from_lxc(
        self, m_subp, m_sleep, stdouts, stderr, return_code, sleeps, expected
    ):
        """IPv4 output matches specific vm name from `lxc list`.

        Errors are retried and result in TimeoutError on failure.
        """
        if len(stdouts) > 1:
            m_subp.side_effect = [
                Result(stdout=out, stderr=stderr, return_code=return_code)
                for out in stdouts
            ]
        else:
            m_subp.return_value = Result(
                stdout=stdouts[0], stderr=stderr, return_code=return_code
            )
        instance = LXDInstance(name="my_vm")
        lxc_mock = mock.call(
            ["lxc", "list", "^my_vm$", "-c4", "--format", "csv"]
        )
        if isinstance(expected, Exception):
            with pytest.raises(type(expected), match=re.escape(str(expected))):
                instance.ip  # pylint: disable=pointless-statement
            assert [lxc_mock] * sleeps == m_subp.call_args_list
        else:
            assert expected == instance.ip
            assert [lxc_mock] * (1 + sleeps) == m_subp.call_args_list
        assert sleeps == m_sleep.call_count


class TestWaitForStop:
    """Tests covering pycloudlib.lxd.instance.Instance.wait_for_stop."""

    @pytest.mark.parametrize("is_ephemeral", ((True), (False)))
    def test_wait_for_stop_does_not_wait_for_ephemeral_instances(
        self, is_ephemeral
    ):
        """LXDInstance.wait_for_stop does not wait on ephemeral instances."""
        instance = LXDInstance(name="test")
        with mock.patch.object(instance, "wait_for_state") as wait_for_state:
            with mock.patch.object(type(instance), "ephemeral", is_ephemeral):
                instance.wait_for_stop()

        call_count = 0 if is_ephemeral else 1
        assert call_count == wait_for_state.call_count


class TestShutdown:
    """Tests covering pycloudlib.lxd.instance.Instance.shutdown."""

    @pytest.mark.parametrize(
        "wait,force,cmd",
        (
            (True, False, ["lxc", "stop", "test"]),
            (False, False, ["lxc", "stop", "test"]),
            (True, True, ["lxc", "stop", "test", "--force"]),
        ),
    )
    @mock.patch("pycloudlib.lxd.instance.subp")
    def test_shutdown_calls_wait_for_stopped_state_when_wait_true(
        self, m_subp, wait, force, cmd
    ):
        """LXDInstance.wait_for_stopped called when wait is True."""
        instance = LXDInstance(name="test")
        with mock.patch.object(instance, "wait_for_stop") as wait_for_stop:
            with mock.patch.object(type(instance), "state", "RUNNING"):
                instance.shutdown(wait=wait, force=force)

        assert [mock.call(cmd)] == m_subp.call_args_list
        call_count = 1 if wait else 0
        assert call_count == wait_for_stop.call_count


class TestDelete:
    """Tests covering pycloudlib.lxd.instance.Instance.delete."""

    @pytest.mark.parametrize("is_ephemeral", ((True), (False)))
    @mock.patch("pycloudlib.lxd.instance.LXDInstance.shutdown")
    @mock.patch("pycloudlib.lxd.instance.subp")
    def test_delete_on_ephemeral_instance_calls_shutdown(
        self, m_subp, m_shutdown, is_ephemeral
    ):
        """Check if ephemeral instance delete stops it instead of deleting it.

        Also verify is delete is actually called if instance is not ephemeral.
        """
        instance = LXDInstance(name="test")

        with mock.patch.object(type(instance), "ephemeral", is_ephemeral):
            instance.delete(wait=False)

        if is_ephemeral:
            assert 1 == m_shutdown.call_count
            assert 0 == m_subp.call_count
        else:
            assert 0 == m_shutdown.call_count
            assert 1 == m_subp.call_count
            assert [
                mock.call(["lxc", "delete", "test", "--force"])
            ] == m_subp.call_args_list
