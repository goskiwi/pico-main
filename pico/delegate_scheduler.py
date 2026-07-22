"""Bounded concurrent scheduling for read-only delegate children.

This module intentionally knows nothing about tool registration, prompting, or
CLI arguments.  It receives normalized child-task specifications, reserves a
finite step budget, and returns one outcome per specification.
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
import time
from typing import Any, Callable, Mapping, Sequence

from .config import (
    DELEGATE_BATCH_TIMEOUT_SECONDS,
    DELEGATE_MAX_CONCURRENCY,
    DELEGATE_TOTAL_STEP_BUDGET,
)


DelegateChildRunner = Callable[[Any, Mapping[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class DelegateOutcome:
    """The terminal state for one requested delegate specification."""

    index: int
    spec: dict[str, Any]
    status: str
    reserved_steps: int
    result: dict[str, Any] | None = None
    error: str = ""
    duration_ms: int = 0


class DelegateScheduler:
    """Run bounded child agents concurrently and account for their step budget."""

    def __init__(self, agent, *, child_runner: DelegateChildRunner | None = None):
        self.agent = agent
        self._child_runner = child_runner
        self.last_reserved_steps = 0

    def run(self, specs: Sequence[Mapping[str, Any]]) -> list[DelegateOutcome]:
        """Return ordered outcomes for every supplied child-task specification.

        Budget is reserved before submitting work, so a batch can never start
        more child steps than its configured allowance.  A timeout is a
        scheduler boundary: queued futures are cancelled and running read-only
        children are ignored once the batch deadline elapses.
        """
        normalized_specs = [dict(spec) for spec in specs]
        outcomes: list[DelegateOutcome | None] = [None] * len(normalized_specs)
        futures = {}
        self.last_reserved_steps = 0
        remaining_steps = DELEGATE_TOTAL_STEP_BUDGET
        runner = self._resolve_child_runner()

        executor = ThreadPoolExecutor(
            max_workers=min(DELEGATE_MAX_CONCURRENCY, max(1, len(normalized_specs))),
            thread_name_prefix="pico-delegate",
        )
        timed_out = False
        try:
            for index, spec in enumerate(normalized_specs, start=1):
                reserved_steps = int(spec.get("max_steps", 3))
                if reserved_steps > remaining_steps:
                    outcomes[index - 1] = DelegateOutcome(
                        index=index,
                        spec=spec,
                        status="budget_exhausted",
                        reserved_steps=0,
                        error=(
                            "delegate step budget exhausted: "
                            f"requested {reserved_steps}, remaining {remaining_steps}"
                        ),
                    )
                    continue

                remaining_steps -= reserved_steps
                self.last_reserved_steps += reserved_steps
                started_at = time.monotonic()
                future = executor.submit(runner, self.agent, spec)
                futures[future] = (index, spec, reserved_steps, started_at)

            if futures:
                completed, pending = wait(
                    futures,
                    timeout=DELEGATE_BATCH_TIMEOUT_SECONDS,
                )
                timed_out = bool(pending)
                for future in completed:
                    index, spec, reserved_steps, started_at = futures[future]
                    outcomes[index - 1] = self._outcome_from_future(
                        future,
                        index=index,
                        spec=spec,
                        reserved_steps=reserved_steps,
                        started_at=started_at,
                    )
                for future in pending:
                    index, spec, reserved_steps, started_at = futures[future]
                    future.cancel()
                    outcomes[index - 1] = DelegateOutcome(
                        index=index,
                        spec=spec,
                        status="timeout",
                        reserved_steps=reserved_steps,
                        error=(
                            "delegate batch exceeded "
                            f"{DELEGATE_BATCH_TIMEOUT_SECONDS:g}s timeout"
                        ),
                        duration_ms=self._duration_ms(started_at),
                    )
        finally:
            # ThreadPoolExecutor cannot forcibly stop an in-flight Python
            # thread.  Do not wait past the timeout; delegate children are
            # read-only and their late results are deliberately discarded.
            executor.shutdown(wait=not timed_out, cancel_futures=True)

        return [outcome for outcome in outcomes if outcome is not None]

    def _resolve_child_runner(self) -> DelegateChildRunner:
        if self._child_runner is not None:
            return self._child_runner
        # A late import avoids a tools <-> scheduler import cycle while keeping
        # this module independent of tool registration.
        from .tools import run_delegate_child

        return run_delegate_child

    def _outcome_from_future(self, future, *, index, spec, reserved_steps, started_at):
        try:
            result = dict(future.result())
        except Exception as exc:
            return DelegateOutcome(
                index=index,
                spec=spec,
                status="error",
                reserved_steps=reserved_steps,
                error=f"{type(exc).__name__}: {exc}",
                duration_ms=self._duration_ms(started_at),
            )
        child_status = str(result.get("status", "completed")).strip()
        if child_status and child_status != "completed":
            stop_reason = str(result.get("stop_reason", "")).strip()
            return DelegateOutcome(
                index=index,
                spec=spec,
                status="error",
                reserved_steps=reserved_steps,
                error=(
                    f"child stopped with status={child_status}"
                    + (f" ({stop_reason})" if stop_reason else "")
                ),
                duration_ms=self._duration_ms(started_at),
            )
        return DelegateOutcome(
            index=index,
            spec=spec,
            status="ok",
            reserved_steps=reserved_steps,
            result=result,
            duration_ms=self._duration_ms(started_at),
        )

    @staticmethod
    def _duration_ms(started_at):
        return int((time.monotonic() - started_at) * 1000)
