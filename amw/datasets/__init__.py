"""Synthetic dataset generation for the workbench (T06).

The public surface is small on purpose — ``cli.py gen`` and every test should
need only this::

    from amw.datasets import generate, read_items, DatasetItem

    result = generate(customer="demo_patents", n=70, mode="hybrid")
    items = read_items("datasets/query_rewriter.jsonl")

Everything else (the patents fact bank, the per-subagent templates, the Gemini
realism pass) is an implementation detail of how the items get made.
"""

from __future__ import annotations

from amw.datasets.generator import (
    GENERATOR_VERSION,
    GenerationResult,
    SubagentResult,
    default_output_dir,
    generate,
    generate_items,
)
from amw.datasets.mix import MIX, allocate, mix_of, stratified_sample
from amw.datasets.schema import (
    DIFFICULTIES,
    Chunk,
    DatasetInput,
    DatasetItem,
    Difficulty,
    RubricCriterion,
    pretty,
    read_items,
    write_items,
)

__all__ = [
    "GENERATOR_VERSION",
    "GenerationResult",
    "SubagentResult",
    "generate",
    "generate_items",
    "default_output_dir",
    "MIX",
    "allocate",
    "mix_of",
    "stratified_sample",
    "DIFFICULTIES",
    "Difficulty",
    "Chunk",
    "DatasetInput",
    "DatasetItem",
    "RubricCriterion",
    "read_items",
    "write_items",
    "pretty",
]
