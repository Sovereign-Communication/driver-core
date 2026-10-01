"""driver-core: verified extraction -> Jev decision -> deterministic action.

Version note: ``2.0.0`` is the consent-law change. A consent became a
capability for one exact ``(action, params)`` pair with no wildcard at all,
and the ``*`` that previously stood for "every mutating action" was removed
from this package's vocabulary of grants. The wire contract did not change:
``STOP_REASONS``, refusal-is-``HTTP 200 ok:false`` and
:meth:`~driver_core.driver.StepResult.to_dict` are byte-identical to 1.x.
"""

__version__ = "2.0.0"
