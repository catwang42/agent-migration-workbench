"""Claude via Vertex AI Model Garden (``CLAUDE_PATH=vertex``) — the demo path.

Same Anthropic Messages API, different client construction and different auth:
GCP Application Default Credentials, no Anthropic API key. Everything else —
prompt pass-through, retry policy, usage/latency capture, trace shape — is
inherited from :class:`amw.adapters.claude_anthropic._ClaudeMessagesAdapter` so
the two Claude paths cannot drift apart and produce different baselines.

Availability caveat (Claude on Vertex): no Files API, no Message Batches, no web
fetch. Nothing here uses them.
"""

from __future__ import annotations

import os
import time
from typing import Any, Callable

from amw.adapters.base import AdapterError
from amw.adapters.claude_anthropic import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MissingCredentialsError,
    _ClaudeMessagesAdapter,
)
from amw.config import ModelsConfig

__all__ = ["ClaudeVertexAdapter"]


class ClaudeVertexAdapter(_ClaudeMessagesAdapter):
    """Claude on Vertex AI Model Garden.

    Model IDs come from ``config/models.yaml`` under the ``vertex`` access path.
    Current-generation Claude models take the BARE first-party ID there
    (``claude-sonnet-5``); the ``anthropic.`` vendor prefix is Bedrock's
    convention and 404s on Vertex. Nothing is hardcoded here — see
    ``_resolve_model_id``.
    """

    name = "claude_vertex"
    ACCESS_PATH = "vertex"

    def __init__(
        self,
        models: ModelsConfig,
        *,
        project_id: str | None = None,
        region: str | None = None,
        client: Any | None = None,
        default_max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """
        :param project_id: defaults to ``$PROJECT_ID`` then
            ``$GOOGLE_CLOUD_PROJECT``.
        :param region: defaults to ``$CLAUDE_REGION``, then ``$REGION``, then
            ``$CLOUD_ML_REGION``.

        ``CLAUDE_REGION`` comes first because Claude and Gemini do not
        necessarily have capacity in the same region: Model Garden quota is
        per-region and per-base-model, so a project can serve Gemini in
        ``us-central1`` while Claude there returns 429 and only ``global``
        works. Overriding ``$REGION`` wholesale would move Gemini too. When the
        two differ the run is cross-region, which is a measurement caveat for
        the ``latency_p95`` gate — report footers must say so.
        """
        self.project_id = (
            project_id
            or os.environ.get("PROJECT_ID")
            or os.environ.get("GOOGLE_CLOUD_PROJECT")
            or None
        )
        self.region = (
            region
            or os.environ.get("CLAUDE_REGION")
            or os.environ.get("REGION")
            or os.environ.get("CLOUD_ML_REGION")
            or None
        )
        super().__init__(
            models,
            client=client,
            default_max_output_tokens=default_max_output_tokens,
            sleep=sleep,
        )

    def _build_client(self) -> Any:
        missing = [
            name
            for name, value in (("PROJECT_ID", self.project_id), ("REGION", self.region))
            if not value
        ]
        if missing:
            raise MissingCredentialsError(
                "ClaudeVertexAdapter is missing "
                + " and ".join(missing)
                + ". Set them in .env (see .env.example) or pass "
                "project_id=/region= explicitly. Claude on Vertex authenticates "
                "with GCP Application Default Credentials — run "
                "`gcloud auth application-default login`; there is no Anthropic "
                "API key on this path. To run with no credentials at all, use "
                "--mode replay."
            )
        # Imported here, not at module scope: importing this module must work
        # with no SDK extras, no credentials and no network (ground rule 4).
        try:
            from anthropic import AnthropicVertex
        except ImportError as exc:  # pragma: no cover - dependency is pinned
            raise AdapterError(
                "the `anthropic[vertex]` extra is required for "
                "CLAUDE_PATH=vertex; install it with "
                "`pip install -r requirements.txt`"
            ) from exc
        return AnthropicVertex(project_id=self.project_id, region=self.region)
