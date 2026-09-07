from __future__ import annotations

from blueferry.threads import ConversationIndex


class _Jobs:
    def __init__(self):
        self.pending = []

    def submit(self, operation, *, on_success, on_error):
        self.pending.append((operation, on_success, on_error))

    def complete(self):
        operation, success, failure = self.pending.pop(0)
        try:
            result = operation()
        except Exception as error:
            failure(error)
        else:
            success(result)


def test_async_projection_coalesces_requests_and_discards_a_stale_result():
    revision = [1]
    jobs = _Jobs()
    results = []
    failures = []
    index = ConversationIndex(lambda: [], lambda _: [], revision=lambda: tuple(revision))

    def prepare():
        current = revision[0]
        return lambda: [{"key": f"thread:{current}"}]

    for _ in range(2):
        index.prepare_async(
            jobs.submit, prepare, lambda: results.append(index.threads()), failures.append,
        )
    assert len(jobs.pending) == 1
    assert not results
    revision[0] = 2
    jobs.complete()
    assert not results
    assert len(jobs.pending) == 1
    jobs.complete()
    assert results == [[{"key": "thread:2"}]] * 2
    assert not failures


def test_contact_invalidation_discards_pending_projection_and_retries_after_failure():
    jobs = _Jobs()
    index = ConversationIndex(lambda: [], lambda _: [], revision=lambda: (1,))
    failures = []
    results = []
    index.prepare_async(jobs.submit, lambda: lambda: [], lambda: results.append(True), failures.append)
    _operation, _success, fail = jobs.pending.pop()
    index.invalidate()
    fail(ValueError("obsolete failure"))
    assert not results and not failures
    jobs.complete()
    assert results == [True]


def test_waiting_mutation_cannot_make_a_later_request_rebuild_synchronously():
    jobs = _Jobs()
    revision = [1]
    index = ConversationIndex(
        lambda: (_ for _ in ()).throw(AssertionError("synchronous history load")),
        lambda _: [], revision=lambda: tuple(revision),
    )
    results = []
    failures = []
    def mutate():
        revision[0] += 1
        index.invalidate()
    def prepare():
        return lambda: [{"key": "thread"}]
    index.prepare_async(jobs.submit, prepare, mutate, failures.append)
    index.prepare_async(jobs.submit, prepare, lambda: results.append(index.threads()), failures.append)
    jobs.complete()
    assert not results
    jobs.complete()
    assert results == [[{"key": "thread"}]]
    assert not failures


def test_revision_read_failure_replies_to_waiters_and_allows_a_later_retry():
    jobs = _Jobs()
    failing = [False]
    def revision():
        if failing[0]:
            raise OSError("fixture database unavailable")
        return (1,)
    index = ConversationIndex(lambda: [], lambda _: [], revision=revision)
    results, failures = [], []
    def prepare():
        return lambda: []
    index.prepare_async(jobs.submit, prepare, lambda: results.append(True), failures.append)
    failing[0] = True
    jobs.complete()
    assert len(failures) == 1
    assert not results
    failing[0] = False
    index.prepare_async(jobs.submit, prepare, lambda: results.append(True), failures.append)
    jobs.complete()
    assert results == [True]


def test_projection_is_cached_until_source_changes(tmp_path) -> None:
    source = tmp_path / "history-source"
    source.write_text("first")
    loads = []

    index = ConversationIndex(
        lambda: loads.append(True) or [{"value": len(loads)}],
        lambda events: [{"key": "thread", "events": events}],
        source=source,
    )

    assert index.threads() == index.threads()
    assert len(loads) == 1

    source.write_text("second, longer")
    assert index.find("thread") is not None
    assert len(loads) == 2


def test_manual_invalidation_covers_contact_cache_changes(tmp_path) -> None:
    loads = []
    index = ConversationIndex(
        lambda: loads.append(True) or [], lambda _events: [],
        source=tmp_path / "missing",
    )

    index.threads()
    index.invalidate()
    index.threads()

    assert len(loads) == 2
