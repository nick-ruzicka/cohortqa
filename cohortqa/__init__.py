"""CohortQA — generic multi-persona QA framework.

Core (``cohortqa.core``) is app-agnostic. App-specific configs live outside
the package, under e.g. ``qa/`` for CareerOps.

See ``cohortqa/README.md`` for usage. The firewall test
``qa/tests/test_no_app_imports_in_core.py`` enforces that nothing under
``cohortqa/core/`` imports from ``qa/`` or other app namespaces.
"""

__version__ = "0.1.0"
