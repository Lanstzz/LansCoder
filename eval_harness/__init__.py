"""Offline-first regression harness for LansCoder runtime behavior.

The harness is deliberately outside :mod:`lanscoder`: it observes the public
runtime API, but the runtime never imports this package.
"""

from eval_harness.replay.runner import RunResult, run_offline_case
from eval_harness.replay.extractor import ExtractionResult, extract_replay_case
from eval_harness.schema.models import CaseManifest, load_case_manifest

__all__ = [
    "CaseManifest",
    "ExtractionResult",
    "RunResult",
    "extract_replay_case",
    "load_case_manifest",
    "run_offline_case",
]
