"""PersonaLab — generic multi-persona QA framework.

Core (``personalab.core``) is app-agnostic. App-specific configs live outside
the package, under e.g. ``qa/`` for CareerOps.

See ``personalab/README.md`` for usage. The firewall test
``qa/tests/test_no_app_imports_in_core.py`` enforces that nothing under
``personalab/core/`` imports from ``qa/`` or other app namespaces.
"""

__version__ = "0.1.0"
