"""Compatibility facade for decomposed Owner authorities."""

from . import owner_health as _health
from . import owner_maintenance as _maintenance
from . import owner_runtime as _runtime
from . import owner_supervisor as _supervisor
from . import queue as _queue

OwnerHealth = _health.OwnerHealth
OwnerHealthFacts = _health.OwnerHealthFacts
OwnerState = _health.OwnerState
OwnerStatus = _health.OwnerStatus
classify_owner_health = _health.classify_owner_health
evaluate_owner_health = _health.evaluate_owner_health
owner_is_healthy = _health.owner_is_healthy

ContextDisposition = _runtime.ContextDisposition
context_disposition = _runtime.context_disposition
run_owner = _runtime.run_owner

assert_owner_runtime_compatible = _supervisor.assert_owner_runtime_compatible
assert_owner_submit_admissible = _supervisor.assert_owner_submit_admissible
ensure_owner = _supervisor.ensure_owner
owner_status = _supervisor.owner_status
run_owner_launch_check = _supervisor.run_owner_launch_check

recover_broken_owner = _maintenance.recover_broken_owner
recover_stale_shutdown = _maintenance.recover_stale_shutdown
request_owner_shutdown = _maintenance.request_owner_shutdown

JobQueue = _queue.JobQueue

_commit_shutdown_decision = _maintenance.commit_shutdown_decision
_held_if_present = _health.held_if_present
_process_alive = _health.process_alive
_runtime_digest = _health.runtime_digest
