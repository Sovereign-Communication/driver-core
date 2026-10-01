"""The concrete declared schemas the driver actually extracts.

:mod:`driver_core.schema` owns the mechanism; this module owns the two
declarations the product ships with. They live apart on purpose: the
mechanism is the reusable piece, these are the opinions, and mixing them
means every consumer of the mechanism inherits this product's field names.

The provenance rules are the interesting part and are chosen per field, not
globally. ``CASED`` on a window title stops ``"Untitled - Notepad"`` and
``"untitled - notepad"`` from reading as a disagreement. ``NUMERIC`` with a
tolerance on a line number stops a one-digit OCR wobble from escalating a
run. Getting these wrong does not break the system -- it makes it
needlessly trigger-happy, and an operator who learns to ignore spurious
disagreements has lost the signal entirely.
"""
from .perception import CLI, DOM, GUI, MCP
from .schema import (
    BOOLEAN, CASED, EXACT, OPTIONAL, REQUIRED, Field, Schema,
)

#: What the driver extracts from a screen.
#:
#: Deliberately small. Every field is something a decision about "what should
#: I do next" could plausibly depend on. A schema that tried to capture
#: everything on screen would have a dozen optional fields that are almost
#: always absent, which drags the honest-answer rate down and makes the
#: shortfall signal fire for uninteresting reasons.
SCREEN_SCHEMA = Schema(
    "driver-core-screen", "1.0.0",
    [
        Field("window_title", "string", presence=REQUIRED, provenance=CASED,
              description="Title bar text of the foreground window.",
              provenance_note="Casefolded: two extractors differing only in "
                              "capitalisation observed the same title."),
        Field("foreground_app", "string", presence=REQUIRED, provenance=CASED,
              description="Application name owning the foreground window."),
        Field("visible_text", "string", presence=OPTIONAL, provenance=CASED,
              description="Text legible on screen, when the target is text.",
              provenance_note="Casefolded and stripped; long free text is the "
                              "field most likely to produce a genuine "
                              "disagreement, and that is reported rather "
                              "than resolved."),
        Field("error_dialog_present", "boolean", presence=REQUIRED,
              provenance=BOOLEAN,
              description="Whether a modal error or warning dialog is up.",
              provenance_note="Boolean vocabulary is wide on purpose "
                              "(yes/1/checked/selected) because models "
                              "disagree on the token far more often than on "
                              "the meaning."),
        Field("dialog_kind", "enum", presence=OPTIONAL,
              values=("error", "warning", "confirmation", "information", "file_picker"),
              provenance=EXACT,
              description="Kind of modal, when one is present.",
              provenance_note="Exact: the vocabulary is closed, so a "
                              "casefold would let an undeclared label slip "
                              "through as a near-match."),
        Field("focused_field_label", "string", presence=OPTIONAL,
              provenance=CASED,
              description="Label of the element that currently has focus."),
        Field("blocking_controls", "string", presence=OPTIONAL,
              provenance=CASED,
              description="Labels of controls that would commit something "
                          "irreversible, as observed on screen."),
    ],
)

#: What the driver extracts from a command's output.
#:
#: No pixels, no model -- a command either produced output or it did not.
#: This schema exists so the same decision tier can be fed from a structured
#: target, and so the driver is target-agnostic above this line.
CLI_SCHEMA = Schema(
    "driver-core-cli", "1.0.0",
    [
        Field("exit_code", "integer", presence=REQUIRED, provenance=EXACT,
              description="Process exit status."),
        Field("stdout", "string", presence=OPTIONAL, provenance=EXACT,
              description="Standard output.",
              provenance_note="Exact: command output is already "
                              "deterministic, so normalising it would hide "
                              "a real difference between runs."),
        Field("stderr", "string", presence=OPTIONAL, provenance=EXACT,
              description="Standard error."),
    ],
)

#: Schemas by target class, so a caller picks one and does not have to know
#: which module declared it.
SCHEMAS_BY_TARGET = {
    "gui": SCREEN_SCHEMA,
    "screen": SCREEN_SCHEMA,
    "cli": CLI_SCHEMA,
    "dom": SCREEN_SCHEMA,
    "mcp": CLI_SCHEMA,
}

#: What a fetched document can honestly supply. Separate from
#: :data:`SCREEN_SCHEMA` because a document genuinely cannot report the
#: foreground application's name or whether a modal dialog is up -- and
#: reusing the screen schema for it would mean asking a reader to invent two
#: of its three required fields, which is the failure this project is built
#: against. A DOM target that cannot read a title reports a shortfall, which
#: is the truth, rather than a confident guess.
DOM_SCHEMA = Schema(
    "driver-core-dom", "1.0.0",
    [
        Field("window_title", "string", presence=REQUIRED, provenance=CASED,
              description="The document's <title>."),
        Field("visible_text", "string", presence=OPTIONAL, provenance=CASED,
              description="Readable text of the document body.",
              provenance_note="Casefolded and stripped; long free text is the "
                              "field most likely to produce a genuine "
                              "disagreement, and that is reported rather "
                              "than resolved."),
    ],
)

#: Which schema serves each declared target class. Declared next to the
#: schemas rather than guessed in the driver, because "a DOM target and a GUI
#: target extract different fields" is a real statement about the product and
#: belongs in the module that owns the declarations.
SCHEMA_BY_CLASS = {
    CLI: CLI_SCHEMA,
    MCP: CLI_SCHEMA,
    DOM: DOM_SCHEMA,
    GUI: SCREEN_SCHEMA,
}

#: The names the service accepts on the wire, each bound to a target class
#: **and** its schema in one place.
#:
#: Two parallel maps over the same string domain is how a request ends up
#: with the screen schema and an undeclared class -- and an undeclared class
#: permits any source, which quietly reopens the vision tier. Binding them
#: together means a name is either known completely or refused.
#:
#: The table is the whole set: there is deliberately no default for an absent
#: name. A default would have to be one of the four declared classes, and
#: whichever it was would silently observe a different machine than the
#: caller asked for -- or spend money. So the service asks instead.
WIRE_TARGETS = {
    "cli": (CLI, CLI_SCHEMA),
    "mcp": (MCP, CLI_SCHEMA),
    "dom": (DOM, DOM_SCHEMA),
    "gui": (GUI, SCREEN_SCHEMA),
    "screen": (GUI, SCREEN_SCHEMA),
}

#: Which target classes are answerable without a vision extractor.
#:
#: This is the load-bearing list for cost. A ``cli``, ``mcp`` or ``dom``
#: target that resolved to a model extractor would mean paying to have a
#: lossy rendering described when the state was already available exactly --
#: and paying N times, because extraction is a pool. It is declared here so
#: it can be asserted against, not just asserted in a comment.
FREE_CLASSES = (CLI, MCP, DOM)
