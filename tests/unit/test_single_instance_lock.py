"""单实例锁：避免多个程序实例互相覆盖 settings.json。"""
from __future__ import annotations

import os

import pytest

from pa_agent import main as main_mod


@pytest.fixture
def lock_file(tmp_path, monkeypatch):
    path = tmp_path / ".pa_agent.lock"
    monkeypatch.setattr(main_mod, "_lock_path", lambda: path)
    return path


def test_lock_acquired_when_absent(lock_file):
    acquired, message = main_mod.acquire_single_instance_lock()
    assert acquired is True and message == ""
    assert lock_file.read_text().strip() == str(os.getpid())


def test_lock_blocks_when_other_live_process_holds_it(lock_file):
    lock_file.write_text("1\n", encoding="utf-8")   # pid 1 一定活着
    acquired, message = main_mod.acquire_single_instance_lock()
    assert acquired is False
    assert "pid=1" in message


def test_stale_lock_is_taken_over(lock_file):
    lock_file.write_text("999999\n", encoding="utf-8")   # 不存在的 pid
    acquired, _ = main_mod.acquire_single_instance_lock()
    assert acquired is True
    assert lock_file.read_text().strip() == str(os.getpid())


def test_release_only_removes_own_lock(lock_file):
    lock_file.write_text("1\n", encoding="utf-8")
    main_mod.release_single_instance_lock()
    assert lock_file.exists()          # 不是自己的锁，不动

    lock_file.write_text(f"{os.getpid()}\n", encoding="utf-8")
    main_mod.release_single_instance_lock()
    assert not lock_file.exists()


def test_release_without_lock_is_safe(lock_file):
    main_mod.release_single_instance_lock()
    assert not lock_file.exists()
