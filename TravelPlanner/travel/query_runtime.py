"""Per-step reuse and timings; never share a traveller's plan between requests."""
import copy
import hashlib
import json
import logging
import time
from contextvars import ContextVar
from functools import wraps

log = logging.getLogger(__name__)
_scope = ContextVar('travel_step_memo', default=None)


def planning_step(function):
    @wraps(function)
    def wrapped(job, *args, **kwargs):
        token = _scope.set({})
        started = time.monotonic()
        try:
            return function(job, *args, **kwargs)
        finally:
            log.warning('TRAVEL-TIMING stage=%s seconds=%.3f', job.get('index', -1) + 1,
                        time.monotonic() - started)
            _scope.reset(token)
    return wrapped


def step_memo(function):
    @wraps(function)
    def wrapped(*args, **kwargs):
        scope = _scope.get()
        if scope is None:
            return function(*args, **kwargs)
        # Hash arguments (including the provider credential); never log them.
        key = (function.__module__, function.__name__, hashlib.sha256(
            json.dumps([args, kwargs], sort_keys=True, default=str).encode()).hexdigest())
        if key not in scope:
            scope[key] = function(*args, **kwargs)
        return copy.deepcopy(scope[key])
    return wrapped
