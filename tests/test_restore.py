"""Tests for putting Home Assistant back when this integration goes away.

Stopping the proxy frees the port but leaves Home Assistant on loopback, so
without a restore the instance is off the network entirely: nothing serves the
address people use, and the port Home Assistant is on refuses anything that is
not the machine itself. Recovering needs a shell, which is a fair thing to ask
of somebody undoing a mistake and not of somebody uninstalling an integration.
"""

from custom_components.ha_rbac import http_config


def test_the_snapshot_records_only_what_the_move_overwrites() -> None:
    """Everything else may be edited while this is installed and is not ours."""
    current = {
        "server_host": ["0.0.0.0"],
        "server_port": 8123,
        "ssl_certificate": "/ssl/fullchain.pem",
        "ip_ban_enabled": True,
    }

    assert http_config.snapshot(current) == {
        "server_host": ["0.0.0.0"],
        "server_port": 8123,
    }


def test_a_restore_undoes_the_move_and_nothing_else() -> None:
    """An SSL certificate added while this was installed survives removing it."""
    previous = http_config.snapshot({"server_host": ["0.0.0.0"], "server_port": 8123})
    moved = http_config.target_config(
        {"server_host": ["0.0.0.0"], "server_port": 8123}, 8124
    )
    # Something changed by hand in the meantime.
    running = {**moved, "ssl_certificate": "/ssl/fullchain.pem"}

    restored = http_config.restore_config(running, previous)

    assert restored["server_host"] == ["0.0.0.0"]
    assert restored["server_port"] == 8123
    assert restored["ssl_certificate"] == "/ssl/fullchain.pem"


def test_keys_the_move_invented_are_removed_again() -> None:
    """`trusted_proxies` and `use_x_forwarded_for` are set by the move.

    An instance that had neither before must not be left with the proxy trusted
    after the proxy is gone, which would leave Home Assistant reading forwarded
    addresses from whatever else can reach loopback.
    """
    previous = http_config.snapshot({"server_port": 8123})
    moved = http_config.target_config({"server_port": 8123}, 8124)
    assert moved["trusted_proxies"] == ["127.0.0.1"]
    assert moved["use_x_forwarded_for"] is True

    restored = http_config.restore_config(moved, previous)

    assert "trusted_proxies" not in restored
    assert "use_x_forwarded_for" not in restored
    assert "server_host" not in restored, "was never set, so must not be invented"
    assert restored["server_port"] == 8123


def test_a_trusted_proxy_of_their_own_survives() -> None:
    """The move appends to the list; the restore has to put the list back."""
    original = {"server_port": 8123, "trusted_proxies": ["10.0.0.0/8"]}
    previous = http_config.snapshot(original)
    moved = http_config.target_config(original, 8124)
    assert moved["trusted_proxies"] == ["10.0.0.0/8", "127.0.0.1"]

    restored = http_config.restore_config(moved, previous)

    assert restored["trusted_proxies"] == ["10.0.0.0/8"]


def test_the_round_trip_is_faithful() -> None:
    """Move then restore leaves the config it started from."""
    original = {
        "server_host": ["0.0.0.0"],
        "server_port": 8123,
        "ssl_certificate": "/ssl/fullchain.pem",
        "cors_allowed_origins": ["https://cast.home-assistant.io"],
        "trusted_proxies": ["10.0.0.0/8"],
        "use_x_forwarded_for": True,
        "ip_ban_enabled": True,
    }
    previous = http_config.snapshot(original)

    moved = http_config.target_config(original, 8124)
    restored = http_config.restore_config(moved, previous)

    assert restored == original


def test_the_meta_keys_do_not_come_back() -> None:
    """A restore is a fresh config, not a replay of a failed trial."""
    previous = http_config.snapshot({"server_port": 8123})
    running = {
        "server_host": ["127.0.0.1"],
        "server_port": 8124,
        "created_at": "2026-01-01T00:00:00+00:00",
        "error": "boom",
        "error_message": "it did not bind",
    }

    restored = http_config.restore_config(running, previous)

    for key in ("created_at", "error", "error_message"):
        assert key not in restored
