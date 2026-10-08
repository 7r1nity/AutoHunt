from app.intent.circuit import CircuitBreaker, CircuitState


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0


def test_breaker_opens_after_threshold_then_probes_half_open(monkeypatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr("app.intent.circuit.time.monotonic", lambda: clock.now)
    transitions: list[tuple] = []
    breaker = CircuitBreaker(
        "test",
        failure_threshold=3,
        window_seconds=30,
        cooldown_seconds=30,
        on_state_change=lambda name, old, new: transitions.append((name, old, new)),
    )

    assert breaker.state is CircuitState.CLOSED
    breaker.record_failure()
    breaker.record_failure()
    assert breaker.allow() is True
    breaker.record_failure()  # third failure → open
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False
    assert transitions[-1] == ("test", CircuitState.CLOSED, CircuitState.OPEN)

    clock.now += 31  # cooldown elapsed
    assert breaker.allow() is True  # half-open probe
    assert breaker.state is CircuitState.HALF_OPEN
    assert breaker.allow() is False  # only one probe at a time

    breaker.record_success()  # probe succeeded → closed
    assert breaker.state is CircuitState.CLOSED
    assert breaker.allow() is True


def test_breaker_half_open_failure_reopens(monkeypatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr("app.intent.circuit.time.monotonic", lambda: clock.now)
    breaker = CircuitBreaker("test", failure_threshold=2, cooldown_seconds=10)

    breaker.record_failure()
    breaker.record_failure()
    assert breaker.state is CircuitState.OPEN

    clock.now += 11
    assert breaker.allow() is True
    breaker.record_failure()  # probe failed → reopen
    assert breaker.state is CircuitState.OPEN
    assert breaker.allow() is False


def test_successes_prune_old_failures(monkeypatch) -> None:
    clock = FakeClock()
    monkeypatch.setattr("app.intent.circuit.time.monotonic", lambda: clock.now)
    breaker = CircuitBreaker("test", failure_threshold=3, window_seconds=30)

    breaker.record_failure()
    breaker.record_failure()
    clock.now += 31  # both failures now outside the window
    breaker.record_success()
    breaker.record_failure()  # only one recent failure remains
    assert breaker.allow() is True
    assert breaker.state is CircuitState.CLOSED
