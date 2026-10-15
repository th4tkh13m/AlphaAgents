"""Concurrency acceptance checks using gated callbacks, not timing estimates."""

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from alpha_agents.tree_search.bridges.androidworld.scheduler import (
    DeviceScheduler,
    DevicesUnavailable,
    TaskJob,
    UnhealthyDevice,
)


def test_all_nine_implementors_can_test_concurrently_then_evaluation_borrows():
    scheduler = DeviceScheduler(tuple(f"d{i}" for i in range(9)))
    admitted = threading.Barrier(10)
    checked = threading.Barrier(10)
    release = threading.Event()
    run_checks = threading.Event()

    def implement(owner):
        with scheduler.implementation(owner):
            admitted.wait(5)
            assert run_checks.wait(5)

            def check(device):
                checked.wait(5)
                assert release.wait(5)
                return device

            return scheduler.submit(TaskJob(owner, owner, "targeted", 1, check, owner)).result(10)

    try:
        with ThreadPoolExecutor(max_workers=9) as pool:
            futures = [pool.submit(implement, f"impl-{i}") for i in range(9)]
            try:
                admitted.wait(5)
                assert len(set(scheduler.status()["testing_reservations"].values())) == 9
                # Logical reservations do not keep idle devices from evaluation.
                evaluation = scheduler.submit(TaskJob("eval", "candidate", "s0", 1, lambda d: d))
                assert evaluation.result(5) in scheduler.devices
                run_checks.set()
                checked.wait(5)
                assert len(scheduler.status()["active_devices"]) == 9
            finally:
                run_checks.set()
                release.set()
            assert len({f.result(5) for f in futures}) == 9
    finally:
        scheduler.close()


def test_nine_devices_do_not_stall_at_three_per_competing_batch():
    scheduler = DeviceScheduler(tuple(f"d{i}" for i in range(9)), autostart=False)
    started = threading.Barrier(10)
    finish = threading.Event()

    def run(device):
        started.wait(5)
        assert finish.wait(5)
        return device

    futures = [scheduler.submit(TaskJob(f"{batch}-{i}", batch, "s0", 1, run))
               for batch, count in (("a", 5), ("b", 4)) for i in range(count)]
    scheduler.start()
    try:
        started.wait(5)
        assert len(scheduler.status()["active_devices"]) == 9
        assert sorted(scheduler.status()["running"].values()) == [4, 5]
    finally:
        finish.set()
        scheduler.close()
    assert len({f.result() for f in futures}) == 9


def test_testing_device_is_borrowed_then_reclaimed_without_preemption():
    scheduler = DeviceScheduler(("d0", "d1"), autostart=False)
    borrowed = threading.Barrier(3)
    release = threading.Event()
    order = []

    def long_evaluation(device):
        order.append(("borrow", device))
        borrowed.wait(5)
        assert release.wait(5)

    with scheduler.implementation("impl"):
        # Both devices are allowed to serve canonical work while no check waits.
        scheduler.submit(TaskJob("borrow", "evaluation", "s0", 10, long_evaluation))
        scheduler.submit(TaskJob("borrow-peer", "evaluation", "s0", 10, long_evaluation))
        scheduler.start()
        borrowed.wait(5)
        check = scheduler.submit(TaskJob("check", "impl", "targeted", 0,
                                        lambda d: order.append(("check", d)) or d, "impl"))
        assert not check.done()
        release.set()
        assert check.result(5) == scheduler.status()["testing_reservations"]["impl"]
    scheduler.close()
    assert len([x for x in order if x[0] == "borrow"]) == 2
    assert scheduler.status()["testing_reservations"] == {}


def test_waiting_check_moves_to_idle_device_while_its_old_device_is_borrowed():
    scheduler = DeviceScheduler(("d0", "d1"), autostart=False)
    home_started, peer_finished, release = threading.Event(), threading.Event(), threading.Event()
    with scheduler.implementation("impl") as reservation:
        home = reservation["device"]

        def evaluation(device):
            if device == home:
                home_started.set()
                assert release.wait(5)
            else:
                peer_finished.set()

        scheduler.submit(TaskJob("long", "evaluation", "s0", 1, evaluation))
        scheduler.submit(TaskJob("peer", "evaluation", "s0", 1, evaluation))
        scheduler.start()
        try:
            assert home_started.wait(5)
            assert peer_finished.wait(5)
            check = scheduler.submit(TaskJob("check", "impl", "targeted", 1, lambda d: d, "impl"))
            assert check.result(5) != home
            assert not release.is_set()
        finally:
            release.set()
    scheduler.close()


def test_cancelled_queued_check_does_not_hold_implementation_admission():
    scheduler = DeviceScheduler(("d0",), autostart=False)
    with scheduler.implementation("impl"):
        future = scheduler.submit(TaskJob("cancel", "impl", "targeted", 1, lambda d: d, "impl"))
        assert future.cancel()
    assert scheduler.status()["testing_reservations"] == {}
    scheduler.close()


def test_targeted_batch_uses_one_device_while_six_others_evaluate():
    scheduler = DeviceScheduler(tuple(f"d{i}" for i in range(9)), autostart=False)
    started = threading.Barrier(10)
    finish = threading.Event()

    def run(device):
        started.wait(5)
        assert finish.wait(5)
        return device

    with scheduler.implementation("a"), scheduler.implementation("b"), scheduler.implementation("c"):
        futures = [scheduler.submit(TaskJob(owner, owner, "targeted", 1, run, owner))
                   for owner in ("a", "b", "c")]
        futures += [scheduler.submit(TaskJob(f"eval-{i}", "evaluation", "s0", 1, run)) for i in range(6)]
        scheduler.start()
        try:
            started.wait(5)
            state = scheduler.status()
            assert len(state["active_devices"]) == 9
            for device, active in state["active_devices"].items():
                if active["testing_owner"]:
                    assert state["testing_reservations"][active["testing_owner"]] == device
        finally:
            finish.set()
        assert len({f.result(5) for f in futures}) == 9
    scheduler.close()


def test_implementor_admission_is_bounded_and_released_on_exception():
    scheduler = DeviceScheduler(("d0", "d1", "d2"), implementor_limit=1)
    entered = threading.Event()

    def waiting():
        with scheduler.implementation("second"):
            entered.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with pytest.raises(RuntimeError, match="engineering failure"):
            with scheduler.implementation("first"):
                future = pool.submit(waiting)
                with scheduler.condition:
                    assert scheduler.condition.wait_for(lambda: "second" in scheduler.waiting_implementors, 5)
                assert not entered.is_set()
                raise RuntimeError("engineering failure")
        future.result(5)
    assert entered.is_set()
    scheduler.close()


def test_quarantined_testing_device_is_reassigned_before_retry():
    scheduler = DeviceScheduler(("d0", "d1", "d2"))
    with scheduler.implementation("impl") as reservation:
        home = reservation["device"]

        def unhealthy(device):
            assert device == home
            raise UnhealthyDevice("cleanup failed")

        failed = scheduler.submit(TaskJob("first", "impl", "targeted", 1, unhealthy, "impl"))
        with pytest.raises(UnhealthyDevice):
            failed.result(5)
        retry = scheduler.submit(TaskJob("retry", "impl", "targeted", 1, lambda d: d, "impl"))
        assert retry.result(5) != home
    scheduler.close()


def test_shutdown_wakes_implementor_waiting_for_admission():
    scheduler = DeviceScheduler(("d0", "d1"), implementor_limit=1)

    def waiting():
        with scheduler.implementation("waiting"):
            pytest.fail("Closed scheduler admitted another implementor")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with scheduler.implementation("active"):
            future = pool.submit(waiting)
            with scheduler.condition:
                assert scheduler.condition.wait_for(lambda: bool(scheduler.waiting_implementors), 5)
            scheduler.close(wait=False)
            with pytest.raises(RuntimeError, match="closed"):
                future.result(5)
    scheduler.close()


def test_all_devices_quarantined_settle_checks_and_release_reservation():
    scheduler = DeviceScheduler(("d0",))
    with scheduler.implementation("impl"):
        def unhealthy(device):
            raise UnhealthyDevice("cleanup failed")

        failed = scheduler.submit(TaskJob("failure", "impl", "targeted", 1, unhealthy, "impl"))
        with pytest.raises(UnhealthyDevice):
            failed.result(5)
        with pytest.raises(DevicesUnavailable):
            scheduler.submit(TaskJob("retry", "impl", "targeted", 1, lambda d: d, "impl"))
    assert not scheduler.status()["testing_reservations"]
    scheduler.close()
