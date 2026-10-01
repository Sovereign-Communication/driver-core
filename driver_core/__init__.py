"""driver-core: verified extraction -> Jev decision -> deterministic action.

Version note: ``3.0.0`` makes the perception tiers reachable from the product
and closes the request that could reach the strongest one by accident.

* **Declared sources are live.** ``DRIVER_CLI_COMMAND``,
  ``DRIVER_MCP_COMMAND`` with ``DRIVER_MCP_TOOL``, ``DRIVER_DOM_URL`` and
  ``DRIVER_SCREEN`` register real sources on a default ``Driver`` and
  ``Service``. Each tier is off unless its own setting is set, commands are
  tokenised rather than shelled, and ``/health`` reports what is live. Before
  this, the tiers existed only for a caller writing Python by hand: a default
  driver reported ``Tried: none`` and every ``POST /step`` ended in
  ``no_capture``.
* **``schema`` is required on ``POST /step``.** Absent, blank or unrecognised
  is a 400 naming the valid names. The previous behaviour defaulted to the
  screen schema while leaving the target class *undeclared*, and an undeclared
  class permits any source to answer with pixels last -- so the request a
  caller made without thinking was the one that could reach both the strongest
  structured tier and the vision tier.

Nothing was added to or removed from a response. ``STOP_REASONS``,
refusal-is-``HTTP 200 ok:false``, the eleven keys of
:meth:`~driver_core.driver.StepResult.to_dict` and the field set of the
request body are all identical to 2.x; a host that already declared
``schema`` is unaffected.

``2.0.0`` was the consent-law change: a consent became a capability for one
exact ``(action, params)`` pair with no wildcard at all, and the ``*`` that
previously stood for "every mutating action" was removed from this package's
vocabulary of grants. The wire contract did not change then either --
``STOP_REASONS``, refusal-is-``HTTP 200 ok:false`` and
:meth:`~driver_core.driver.StepResult.to_dict` are byte-identical to 1.x.
"""

__version__ = "3.0.0"
