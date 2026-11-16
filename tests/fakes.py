"""Offline fakes for the LangChain synthesizer tests."""

from __future__ import annotations

from typing import Any

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.runnables import RunnableLambda


class StructuredFakeChatModel(FakeListChatModel):
    """A ``FakeListChatModel`` whose ``with_structured_output`` returns scripted objects.

    Lets unit tests exercise the real LCEL wiring (prompt rendering, chain
    composition, ``.ainvoke()`` / ``.abatch()``) without live API calls, per
    the repository's offline-testing conventions.
    """

    scripted: dict[str, list[Any]] = {}
    inputs_seen: list[tuple[str, Any]] = []

    def with_structured_output(self, schema, **kwargs: Any) -> RunnableLambda:
        schema_name = getattr(schema, "__name__", str(schema))
        # Shared per-schema queue: sequential chains (main synthesis, then the
        # left-out-items retry) consume scripted responses in order.
        queue = self.scripted.setdefault(schema_name, [])

        def _run(prompt_input: Any) -> Any:
            self.inputs_seen.append((schema_name, prompt_input))
            if not queue:
                raise AssertionError(f"unexpected structured-output call for {schema_name}")
            return queue.pop(0)

        return RunnableLambda(_run)
