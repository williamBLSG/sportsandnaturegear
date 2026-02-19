"""State queue manager — determines which state to process today."""

from __future__ import annotations

import logging
from datetime import date, datetime

logger = logging.getLogger(__name__)


class StateQueueManagerError(Exception):
    pass


def get_todays_state(
    states: list[str],
    start_date: str,
    force_state: str | None = None,
) -> str:
    """Return the state to process today.

    If force_state is set, validates it exists in the queue and returns it.
    Otherwise computes the index from days elapsed since start_date.

    Raises StateQueueManagerError if:
    - force_state is not in the states list
    - start_date is in the future
    - queue is exhausted (all states already processed)
    """
    if force_state:
        if force_state not in states:
            raise StateQueueManagerError(
                f"Forced state '{force_state}' is not in the queue. "
                f"Valid states: {', '.join(states[:5])}..."
            )
        logger.info("Using forced state: %s", force_state)
        return force_state

    try:
        start = datetime.strptime(start_date, "%Y-%m-%d").date()
    except ValueError as e:
        raise StateQueueManagerError(
            f"Invalid start_date format '{start_date}': {e}"
        ) from e

    today = date.today()

    if start > today:
        raise StateQueueManagerError(
            f"Start date {start_date} is in the future (today is {today})"
        )

    days_elapsed = (today - start).days
    index = days_elapsed

    if index >= len(states):
        raise StateQueueManagerError(
            f"Queue exhausted: day {days_elapsed + 1} but only {len(states)} states. "
            f"All states have been processed."
        )

    state = states[index]
    logger.info(
        "Day %d of queue (started %s): processing %s",
        days_elapsed + 1, start_date, state,
    )
    return state
