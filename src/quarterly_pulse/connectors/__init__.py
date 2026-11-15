"""Async data connectors returning normalized ``ActivityItem`` records.

Each connector degrades gracefully: failures are recorded as warnings on the
returned :class:`~quarterly_pulse.models.ConnectorResult` instead of raising.
"""

from __future__ import annotations

from quarterly_pulse.connectors.docs import fetch_docs_activity
from quarterly_pulse.connectors.github import fetch_github_activity
from quarterly_pulse.connectors.linkedin import fetch_linkedin_activity

__all__ = ["fetch_docs_activity", "fetch_github_activity", "fetch_linkedin_activity"]
