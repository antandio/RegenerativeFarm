"""WP1 sourced planner agent: bounded loop, sourced context, final critic.

No domain writes in this slice. The model gets no tools and no arbitrary
access: it only receives untrusted delimited source text and returns a
structured candidate that deterministic validators check.
"""

from .models import AgentCall, AgentDocGrant, AgentRun

__all__ = ["AgentCall", "AgentDocGrant", "AgentRun"]
