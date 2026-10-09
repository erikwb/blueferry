"""BLE advertisement shape and cleanup regressions."""
from __future__ import annotations

from types import SimpleNamespace

import dbus
import pytest

from blueferry import bluez_setup, config


class TestAncsAdvertisement:
    def test_pairing_payload_is_discoverable_and_solicits_ancs(self):
        props = bluez_setup._AncsAdvert.GetAll(
            SimpleNamespace(compact=False), "org.bluez.LEAdvertisement1"
        )

        assert str(props["Type"]) == "peripheral"
        assert list(props["SolicitUUIDs"]) == [config.ANCS_SOLICIT_UUID]
        assert bool(props["Discoverable"])
        assert int(props["DiscoverableTimeout"]) == 180
        assert props["ManufacturerData"].signature == "qv"
        assert props["ServiceData"].signature == "sv"
        assert bytes(props["ManufacturerData"][dbus.UInt16(0xFFFF)]) == (
            b"\x50\xb0\x13\xf0"
        )
        assert bytes(props["ServiceData"][
            "00009999-0000-1000-8000-00805f9b34fb"
        ]) == b"\x9e\x85\x39\x96"

    def test_rejects_unknown_interface(self):
        with pytest.raises(dbus.exceptions.DBusException):
            bluez_setup._AncsAdvert.GetAll(None, "not.the.advert.interface")


def test_daemon_run_cleans_up_when_start_raises(make_daemon, monkeypatch):
    """A partial startup must not leak a hardware advertisement."""
    instance = make_daemon()
    stopped = []

    def fail_start():
        raise RuntimeError("partial startup")

    monkeypatch.setattr(instance, "start", fail_start)
    monkeypatch.setattr(instance, "stop", lambda: stopped.append(True))

    with pytest.raises(RuntimeError, match="partial startup"):
        instance.run()

    assert stopped == [True]


def test_cod_change_requires_explicit_authorization(monkeypatch):
    calls = []
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda args, **_kwargs: calls.append(args),
    )

    assert bluez_setup.set_cod(authorize=False) is False
    assert calls == []


def test_authorized_cod_change_uses_packaged_systemd_unit(monkeypatch):
    calls = []
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(bluez_setup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(bluez_setup.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda args, **kwargs: calls.append((args, kwargs))
        or type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    assert bluez_setup.set_cod(adapter="hci7", authorize=True) is True
    assert calls[0][0] == [
        "/usr/bin/systemctl",
        "start",
        "blueferry-btmgmt-set-class@7.service",
    ]
    assert calls[0][1]["timeout"] == 120
    assert calls[0][1]["env"]["LC_ALL"] == "C"
    # systemctl keeps the caller's stdin for the Polkit terminal agent.
    assert calls[0][1]["input_text"] is None


def test_root_cod_change_gives_btmgmt_an_empty_stdin_pipe(monkeypatch):
    """Running btmgmt directly needs the helper's BlueZ 5.72 workaround."""
    calls = []
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 0)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda args, **kwargs: calls.append((args, kwargs))
        or type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
    )

    assert bluez_setup.set_cod(adapter="hci7") is True
    assert calls[0][0][:4] == ["/usr/bin/btmgmt", "--index", "7", "class"]
    assert calls[0][1]["input_text"] == ""


@pytest.mark.parametrize(
    "stderr",
    [
        "Failed to start unit: Interactive authentication required.",
        "Error: No authentication agent found.",
    ],
)
def test_cod_change_explains_when_polkit_authentication_is_unavailable(
    monkeypatch, stderr,
):
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(bluez_setup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(bluez_setup.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda _args, **_kwargs: type(
            "Result",
            (),
            {"returncode": 1, "stdout": "", "stderr": stderr},
        )(),
    )

    with pytest.raises(
        bluez_setup.PairingError,
        match="No Polkit authentication is available to set device class",
    ) as failure:
        bluez_setup.set_cod(adapter="hci7", authorize=True)

    assert str(failure.value) == bluez_setup.POLKIT_UNAVAILABLE_MESSAGE


def test_cod_change_explains_when_the_packaged_systemd_unit_is_missing(monkeypatch):
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(bluez_setup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(bluez_setup.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda _args, **_kwargs: type(
            "Result",
            (),
            {
                "returncode": 1,
                "stdout": "",
                "stderr": (
                    "Failed to start blueferry-btmgmt-set-class@7.service: "
                    "Unit blueferry-btmgmt-set-class@7.service not found."
                ),
            },
        )(),
    )

    with pytest.raises(bluez_setup.PairingError) as failure:
        bluez_setup.set_cod(adapter="hci7", authorize=True)

    assert str(failure.value) == bluez_setup.DEVICE_CLASS_SERVICE_MISSING_MESSAGE


def test_cod_change_does_not_mislabel_an_unrelated_systemctl_failure(monkeypatch):
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(bluez_setup.os.path, "isfile", lambda _path: True)
    monkeypatch.setattr(bluez_setup.os, "access", lambda _path, _mode: True)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda _args, **_kwargs: type(
            "Result",
            (),
            {"returncode": 1, "stdout": "", "stderr": "Job failed"},
        )(),
    )

    assert bluez_setup.set_cod(adapter="hci7", authorize=True) is False


def test_cod_change_rejects_an_invalid_adapter(monkeypatch):
    calls = []
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda args, **_kwargs: calls.append(args),
    )

    assert bluez_setup.set_cod(adapter="hci0/../../evil", authorize=True) is False
    assert calls == []


def _without_systemd(
    monkeypatch, *, executables, no_new_privs=False, result=None, root_controlled=True,
):
    from blueferry import service_manager

    calls = []
    monkeypatch.setattr(bluez_setup.os, "geteuid", lambda: 1000)
    monkeypatch.setattr(
        service_manager, "init_system", lambda: service_manager.OPENRC,
    )
    monkeypatch.setattr(bluez_setup, "_executable", lambda path: path in executables)
    monkeypatch.setattr(bluez_setup, "_no_new_privs", lambda: no_new_privs)
    monkeypatch.setattr(bluez_setup, "_root_controlled", lambda path: root_controlled)
    monkeypatch.setattr(
        bluez_setup,
        "run_command",
        lambda args, **kwargs: calls.append((args, kwargs))
        or result
        or SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    return calls


@pytest.mark.parametrize("adapter,index", [("hci07", "7"), ("hci000", "0"), ("hci" + "0" * 5000 + "7", "7")])
def test_without_systemd_the_helper_runs_through_noninteractive_sudo(monkeypatch, adapter, index):
    calls = _without_systemd(
        monkeypatch,
        executables={bluez_setup.SET_COD_HELPER, bluez_setup.SUDO},
    )

    assert bluez_setup.set_cod(adapter=adapter, authorize=True) is True
    assert calls[0][0] == [
        "/usr/bin/sudo", "-n", "--", "/usr/lib/blueferry/blueferry-set-cod", index,
    ]


@pytest.mark.parametrize(
    "stderr",
    [
        "sudo: a password is required\n",
        "alice is not in the sudoers file.\n",
        "Sorry, user alice is not allowed to execute "
        "'/usr/lib/blueferry/blueferry-set-cod 2' as root on host.\n",
        "Sorry, user alice may not run sudo on host.\n",
        # Defaults requiretty
        "sudo: sorry, you must have a tty to run sudo\n",
        "sudo: a terminal is required to read the password; either use the -S "
        "option to read from standard input or configure an askpass helper\n",
        "sudo: 3 incorrect password attempts\n",
        "sudo: /usr/bin/sudo must be owned by uid 0 and have the setuid bit set\n",
        # sudo-rs, wording from src/common/error.rs
        "sudo: interactive authentication is required\n",
        "sudo: I'm sorry alice. I'm afraid I can't do that\n",
        "sudo-rs: Sorry, user alice may not run "
        "/usr/lib/blueferry/blueferry-set-cod 2 on host.\n",
        "sudo: maximum 3 incorrect authentication attempts\n",
    ],
)
def test_unauthorized_sudo_explains_the_sudoers_rule(monkeypatch, stderr):
    _without_systemd(
        monkeypatch,
        executables={bluez_setup.SET_COD_HELPER, bluez_setup.SUDO},
        result=SimpleNamespace(returncode=1, stdout="", stderr=stderr),
    )

    with pytest.raises(bluez_setup.CodAuthorizationRefused) as failure:
        bluez_setup.set_cod(adapter="hci2", authorize=True)

    assert str(failure.value) == bluez_setup.SUDO_NOT_AUTHORIZED_MESSAGE.format(index="2")
    assert "sudo /usr/lib/blueferry/blueferry-set-cod 2" in str(failure.value)


def test_no_new_privs_process_never_invokes_sudo(monkeypatch):
    calls = _without_systemd(
        monkeypatch,
        executables={bluez_setup.SET_COD_HELPER, bluez_setup.SUDO},
        no_new_privs=True,
    )

    with pytest.raises(bluez_setup.CodAuthorizationRefused, match="no_new_privs"):
        bluez_setup.set_cod(adapter="hci0", authorize=True)
    assert calls == []


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Name:\tpython\nNoNewPrivs:\t1\nSeccomp:\t0\n", True),
        ("Name:\tpython\nNoNewPrivs:\t0\n", False),
        ("Name:\tpython\n", False),
    ],
)
def test_no_new_privs_is_read_from_proc_status(tmp_path, text, expected):
    status = tmp_path / "status"
    status.write_text(text)

    assert bluez_setup._no_new_privs(str(status)) is expected
    assert bluez_setup._no_new_privs(str(tmp_path / "missing")) is False


def test_sudo_under_no_new_privs_is_explained(monkeypatch):
    _without_systemd(
        monkeypatch,
        executables={bluez_setup.SET_COD_HELPER, bluez_setup.SUDO},
        result=SimpleNamespace(
            returncode=1,
            stdout="",
            stderr=(
                'sudo: The "no new privileges" flag is set, which prevents '
                "sudo from running as root.\n"
            ),
        ),
    )

    with pytest.raises(bluez_setup.CodAuthorizationRefused, match="no_new_privs"):
        bluez_setup.set_cod(adapter="hci0", authorize=True)


def test_failed_helper_under_sudo_is_not_mislabelled(monkeypatch):
    _without_systemd(
        monkeypatch,
        executables={bluez_setup.SET_COD_HELPER, bluez_setup.SUDO},
        result=SimpleNamespace(returncode=1, stdout="", stderr="Invalid index\n"),
    )

    assert bluez_setup.set_cod(adapter="hci0", authorize=True) is False


@pytest.mark.parametrize(
    "executables",
    [set(), {bluez_setup.SUDO}, {bluez_setup.SET_COD_HELPER}],
)
def test_without_an_authorization_path_no_command_runs(monkeypatch, executables):
    calls = _without_systemd(monkeypatch, executables=executables)

    with pytest.raises(bluez_setup.CodAuthorizationRefused) as failure:
        bluez_setup.set_cod(adapter="hci3", authorize=True)

    assert calls == []
    assert str(failure.value) == (
        bluez_setup.COD_AUTHORIZATION_UNAVAILABLE_MESSAGE.format(index="3")
    )


def test_unauthorized_cod_change_never_tries_sudo(monkeypatch):
    calls = _without_systemd(
        monkeypatch,
        executables={bluez_setup.SET_COD_HELPER, bluez_setup.SUDO},
    )

    assert bluez_setup.set_cod(adapter="hci0", authorize=False) is False
    assert calls == []


@pytest.fixture
def adverts(monkeypatch):
    now = [0.0]
    requests, removed, dispatched = [], [], []

    class Manager:
        bus_name = ':1.42'

        def UnregisterAdvertisement(self, path, **kwargs):
            assert kwargs['signature'] == 'o'
            removed.append(str(path))

    def call_async(*, bus_name, object_path, dbus_interface, method, args, **kwargs):
        assert bus_name == Manager.bus_name
        assert object_path == '/org/bluez/hci7'
        assert dbus_interface == 'org.bluez.LEAdvertisingManager1'
        assert method == 'RegisterAdvertisement'
        path, options = args
        request = SimpleNamespace(path=str(path), options=options, **kwargs)
        request.cancelled = False
        def cancel():
            request.cancelled = True
        request.cancel = cancel
        requests.append(request)
        return request

    context = SimpleNamespace(iteration=lambda _block: dispatched.pop(0)() if dispatched else None)
    monkeypatch.setattr(bluez_setup, '_advert_instance', None)
    monkeypatch.setattr(bluez_setup, '_compact_advert_adapters', set())
    monkeypatch.setattr(bluez_setup, 'get_system_bus', lambda: SimpleNamespace(
        get_object=lambda *a, **k: None, call_async=call_async,
    ))
    monkeypatch.setattr(bluez_setup.dbus, 'Interface', lambda *a: Manager())
    monkeypatch.setattr(bluez_setup.dbus.service.Object, '__init__', lambda *a: None)
    monkeypatch.setattr(bluez_setup.dbus.service.Object, 'remove_from_connection', lambda *a: None)
    monkeypatch.setattr(bluez_setup.GLib.MainContext, 'default', lambda: context)
    monkeypatch.setattr(bluez_setup.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(bluez_setup.time, 'sleep', lambda delay: now.__setitem__(0, now[0] + delay))
    yield SimpleNamespace(now=now, requests=requests, removed=removed, dispatched=dispatched)
    bluez_setup.forget_advert_registration()


def test_pending_advert_is_not_active_and_registration_is_not_duplicated(adverts):
    assert not bluez_setup.register_advert('hci7')
    assert bluez_setup.advert_registration_pending()
    assert not bluez_setup.advert_registered()
    assert not bluez_setup.register_advert('hci7')
    assert len(adverts.requests) == 1
    request = adverts.requests[0]
    assert request.signature == 'oa{sv}'
    request.reply_handler()
    assert bluez_setup.advert_registered()
    assert not bluez_setup.advert_registration_pending()
    assert bluez_setup.register_advert('hci7')
    assert len(adverts.requests) == 1


@pytest.mark.parametrize('error_name', [
    'org.bluez.Error.Failed',
    'org.bluez.Error.AlreadyExists',
    'org.freedesktop.DBus.Error.NoReply',
])
def test_late_registration_failure_is_retried_with_a_new_path(adverts, error_name):
    bluez_setup.register_advert('hci7')
    old = adverts.requests[0]
    old.error_handler(dbus.exceptions.DBusException('failed', name=error_name))
    assert not bluez_setup.advert_registered()
    assert not bluez_setup.advert_registration_pending()
    assert adverts.removed == [old.path]
    bluez_setup.register_advert('hci7')
    new = adverts.requests[1]
    assert new.path != old.path
    old.reply_handler()  # A late completion must not resurrect the old request.
    assert not bluez_setup.advert_registered()
    new.reply_handler()
    assert bluez_setup.advert_registered()


def test_stopping_cancels_pending_advert_and_ignores_late_reply(adverts):
    bluez_setup.register_advert('hci7')
    request = adverts.requests[0]
    bluez_setup.unregister_advert('hci7')
    assert request.cancelled
    assert adverts.removed == [request.path]
    request.reply_handler()
    assert not bluez_setup.advert_registered()


def test_old_release_cannot_clear_a_new_registration(adverts):
    bluez_setup.register_advert('hci7')
    previous = bluez_setup._advert_instance
    bluez_setup.forget_advert_registration()
    assert adverts.removed == []  # Do not unregister against a new BlueZ owner.
    bluez_setup.register_advert('hci7')
    adverts.requests[-1].reply_handler()
    previous.Release()
    assert bluez_setup.advert_registered()
    bluez_setup._advert_instance.Release()
    assert not bluez_setup.advert_registered()


def test_pairing_dispatches_registration_and_waits_after_confirmation(adverts):
    adverts.dispatched.append(lambda: adverts.requests[0].reply_handler())
    assert bluez_setup.register_advert('hci7', settle_for_pairing=True)
    assert adverts.now[0] >= bluez_setup.PAIRING_ADVERT_SETTLE_SECONDS
    assert adverts.now[0] < bluez_setup.PAIRING_ADVERT_SETTLE_SECONDS + 0.2


def test_pairing_registration_deadline_cleans_up_an_unanswered_request(adverts):
    assert not bluez_setup.register_advert('hci7', settle_for_pairing=True)
    assert bluez_setup.ADVERT_ACTIVATION_TIMEOUT_SECONDS <= adverts.now[0] < 15.2
    assert adverts.requests[0].cancelled
    assert adverts.removed == [adverts.requests[0].path]
    adverts.requests[0].reply_handler()
    assert not bluez_setup.advert_registered()


def _advert_packet_bytes() -> int:
    """Size BlueZ accounts for the registered payload, as calc_max_adv_len does."""
    properties = bluez_setup._advert_instance.GetAll('org.bluez.LEAdvertisement1')
    size = 3  # Flags, added for a discoverable advertisement.
    size += sum(2 + 16 for _uuid in properties['SolicitUUIDs'])
    size += sum(4 + len(data) for data in properties['ManufacturerData'].values())
    size += sum(4 + len(data) for data in properties.get('ServiceData', {}).values())
    if 'tx-power' in properties.get('Includes', []):
        size += 3
    return size


def test_advert_keeps_its_full_payload_while_bluez_accepts_it(adverts):
    bluez_setup.register_advert('hci7')
    adverts.requests[0].reply_handler()

    properties = bluez_setup._advert_instance.GetAll('org.bluez.LEAdvertisement1')
    assert set(properties) >= {'SolicitUUIDs', 'ManufacturerData', 'ServiceData', 'Includes'}
    assert _advert_packet_bytes() == 40  # Needs extended advertising.


def test_rejected_advert_payload_is_retried_in_the_legacy_packet_size(adverts):
    bluez_setup.register_advert('hci7')
    adverts.requests[0].error_handler(dbus.exceptions.DBusException(
        'Failed to parse advertisement.', name='org.bluez.Error.Failed',
    ))

    bluez_setup.register_advert('hci7')

    properties = bluez_setup._advert_instance.GetAll('org.bluez.LEAdvertisement1')
    assert 'ServiceData' not in properties and 'Includes' not in properties
    assert list(properties['SolicitUUIDs']) == [bluez_setup.config.ANCS_SOLICIT_UUID]
    assert properties['ManufacturerData']  # Still not a solicitation-only advert.
    assert _advert_packet_bytes() <= 31
    adverts.requests[1].reply_handler()
    assert bluez_setup.advert_registered()


@pytest.mark.parametrize('error_name,message', [
    ('org.freedesktop.DBus.Error.NoReply', 'timed out'),
    ('org.bluez.Error.AlreadyExists', 'Already Exists'),
    ('org.bluez.Error.NotPermitted', 'Maximum advertisements reached'),
    # The controller refused it; nothing says the payload was at fault.
    ('org.bluez.Error.Failed', 'Failed to register advertisement'),
])
def test_failures_unrelated_to_the_payload_keep_the_full_advert(
    adverts, error_name, message,
):
    bluez_setup.register_advert('hci7')
    adverts.requests[0].error_handler(
        dbus.exceptions.DBusException(message, name=error_name)
    )

    bluez_setup.register_advert('hci7')

    assert _advert_packet_bytes() == 40


def test_pairing_retries_a_rejected_payload_without_waiting_for_the_daemon(adverts):
    adverts.dispatched.append(lambda: adverts.requests[0].error_handler(
        dbus.exceptions.DBusException(
            'Failed to parse advertisement.', name='org.bluez.Error.Failed',
        )
    ))
    adverts.dispatched.append(lambda: adverts.requests[1].reply_handler())

    assert bluez_setup.register_advert('hci7', settle_for_pairing=True)

    assert len(adverts.requests) == 2
    assert _advert_packet_bytes() <= 31


def test_pairing_does_not_loop_when_the_compact_advert_also_fails(adverts):
    for index in range(2):
        adverts.dispatched.append(lambda index=index: adverts.requests[index].error_handler(
            dbus.exceptions.DBusException(
                'Failed to parse advertisement.', name='org.bluez.Error.Failed',
            )
        ))

    assert not bluez_setup.register_advert('hci7', settle_for_pairing=True)

    assert len(adverts.requests) == 2


def test_a_new_bluez_owner_is_offered_the_full_advert_again(adverts):
    bluez_setup.register_advert('hci7')
    adverts.requests[0].error_handler(dbus.exceptions.DBusException(
        'Failed to parse advertisement.', name='org.bluez.Error.Failed',
    ))
    assert bluez_setup._compact_advert_adapters == {'hci7'}

    bluez_setup.forget_advert_registration()
    bluez_setup.register_advert('hci7')

    assert _advert_packet_bytes() == 40


def test_a_helper_others_can_replace_is_never_run_through_sudo(monkeypatch):
    calls = _without_systemd(
        monkeypatch,
        executables={bluez_setup.SET_COD_HELPER, bluez_setup.SUDO},
        root_controlled=False,
    )

    with pytest.raises(bluez_setup.CodAuthorizationRefused) as failure:
        bluez_setup.set_cod(adapter="hci0", authorize=True)

    assert calls == []
    assert str(failure.value) == bluez_setup.SET_COD_HELPER_INSECURE_MESSAGE
    assert "install -D -o root -g root -m 755" in str(failure.value)


def _fake_stat(entries):
    def stat(path):
        if path not in entries:
            raise FileNotFoundError(path)
        uid, mode = entries[path]
        return SimpleNamespace(st_uid=uid, st_mode=mode)

    return stat


_SAFE_TREE = {
    "/": (0, 0o40755),
    "/usr": (0, 0o40755),
    "/usr/lib": (0, 0o40755),
    "/usr/lib/blueferry": (0, 0o40755),
    "/usr/lib/blueferry/blueferry-set-cod": (0, 0o100755),
}


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        ({}, True),
        # The helper itself belongs to the user, or is group/world writable.
        ({"/usr/lib/blueferry/blueferry-set-cod": (1000, 0o100755)}, False),
        ({"/usr/lib/blueferry/blueferry-set-cod": (0, 0o100775)}, False),
        ({"/usr/lib/blueferry/blueferry-set-cod": (0, 0o100757)}, False),
        # A user-owned or writable directory lets the user swap the file.
        ({"/usr/lib/blueferry": (1000, 0o40755)}, False),
        ({"/usr/lib/blueferry": (0, 0o40777)}, False),
        ({"/usr": (0, 0o41777)}, False),
        # Unreadable metadata fails closed.
        ({"/usr/lib": None}, False),
    ],
)
def test_helper_must_be_replaceable_only_by_root(change, expected):
    tree = {**_SAFE_TREE, **change}
    tree = {path: entry for path, entry in tree.items() if entry is not None}

    assert bluez_setup._root_controlled(
        bluez_setup.SET_COD_HELPER, stat=_fake_stat(tree),
    ) is expected


def test_helper_ownership_is_checked_on_the_resolved_path():
    tree = {**_SAFE_TREE,
        bluez_setup.SET_COD_HELPER: (0, 0o120777),
        "/home": (0, 0o40755),
        "/home/alice": (1000, 0o40700),
        "/home/alice/set-cod": (1000, 0o100755),
    }
    assert bluez_setup._root_controlled(
        bluez_setup.SET_COD_HELPER, stat=_fake_stat(tree),
        readlink=lambda _path: "/home/alice/set-cod",
    ) is False


@pytest.mark.parametrize("unsafe", [None, "original_parent", "intermediate_parent", "link_owner"])
def test_helper_symlink_hops_and_their_directories_are_checked(unsafe):
    helper = bluez_setup.SET_COD_HELPER
    tree = {**_SAFE_TREE,
        helper: (0, 0o120777),
        "/opt": (0, 0o40755),
        "/opt/relay": (0, 0o40755),
        "/opt/relay/next": (0, 0o120777),
        "/opt/trusted": (0, 0o40755),
        "/opt/trusted/helper": (0, 0o100755),
    }
    links = {helper: "/opt/relay/next", "/opt/relay/next": "/opt/trusted/helper"}
    if unsafe == "original_parent":
        tree["/usr/lib/blueferry"] = (0, 0o40777)
    elif unsafe == "intermediate_parent":
        tree["/opt/relay"] = (1000, 0o40755)
    elif unsafe == "link_owner":
        tree[helper] = (1000, 0o120777)
    assert bluez_setup._root_controlled(
        helper, stat=_fake_stat(tree), readlink=links.__getitem__,
    ) is (unsafe is None)


@pytest.mark.parametrize("target", ["/opt/trusted/helper", "../../../opt/trusted/helper"])
def test_trusted_absolute_and_relative_helper_symlinks_are_allowed(target):
    tree = {**_SAFE_TREE,
        bluez_setup.SET_COD_HELPER: (0, 0o120777),
        "/opt": (0, 0o40755),
        "/opt/trusted": (0, 0o40755),
        "/opt/trusted/helper": (0, 0o100755),
    }
    assert bluez_setup._root_controlled(
        bluez_setup.SET_COD_HELPER, stat=_fake_stat(tree), readlink=lambda _path: target,
    ) is True


def test_symlink_dotdot_does_not_hide_a_writable_directory():
    tree = {**_SAFE_TREE,
        bluez_setup.SET_COD_HELPER: (0, 0o120777),
        "/opt": (0, 0o40755),
        "/opt/unsafe": (0, 0o40777),
        "/opt/trusted": (0, 0o40755),
        "/opt/trusted/helper": (0, 0o100755),
    }
    assert bluez_setup._root_controlled(
        bluez_setup.SET_COD_HELPER, stat=_fake_stat(tree),
        readlink=lambda _path: "/opt/unsafe/../trusted/helper",
    ) is False


def test_unreadable_symlink_and_symlink_cycles_fail_closed():
    tree = {**_SAFE_TREE, bluez_setup.SET_COD_HELPER: (0, 0o120777)}
    def unreadable(_path):
        raise PermissionError("not readable")
    for readlink in (unreadable, lambda _path: bluez_setup.SET_COD_HELPER):
        assert bluez_setup._root_controlled(
            bluez_setup.SET_COD_HELPER, stat=_fake_stat(tree), readlink=readlink,
        ) is False


@pytest.mark.parametrize("mode", [0o40755, 0o10755, 0o60755])
def test_helper_must_be_a_regular_file(mode):
    tree = {**_SAFE_TREE, bluez_setup.SET_COD_HELPER: (0, mode)}
    assert bluez_setup._root_controlled(
        bluez_setup.SET_COD_HELPER, stat=_fake_stat(tree),
    ) is False


def test_user_replaceable_symlink_never_reaches_sudo(monkeypatch, tmp_path):
    # Real filesystem regression: a user-controlled link points at a trusted
    # system binary. No command is executed; all privileged I/O is recorded.
    guard = bluez_setup._root_controlled
    calls = _without_systemd(
        monkeypatch, executables={bluez_setup.SET_COD_HELPER, bluez_setup.SUDO},
    )
    link = tmp_path / "helper"
    link.symlink_to("/usr/bin/true")
    monkeypatch.setattr(bluez_setup, "SET_COD_HELPER", str(link))
    monkeypatch.setattr(bluez_setup, "_executable", lambda _path: True)
    monkeypatch.setattr(bluez_setup, "_root_controlled", guard)
    with pytest.raises(bluez_setup.CodAuthorizationRefused):
        bluez_setup.set_cod(adapter="hci0", authorize=True)
    assert calls == []
