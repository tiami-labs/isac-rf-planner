"""Build prompts from templates."""

from typing import Any, Dict, List


def build_view_prompt(view_index: int, total_views: int, prompts_config: Dict[str, Any]) -> str:
    """
    Build a prompt for analyzing a single view/tile.

    Args:
        view_index: Index of the current view (0-based)
        total_views: Total number of views
        prompts_config: Prompts configuration dictionary

    Returns:
        Formatted prompt string
    """
    template = prompts_config.get("view_prompt", "")
    return template.format(view_index=view_index + 1, total_views=total_views)


def build_aggregation_prompt(
    view_analyses: List[str], prompts_config: Dict[str, Any]
) -> str:
    """
    Build a prompt for aggregating multiple view analyses.

    Args:
        view_analyses: List of analysis strings from each view
        prompts_config: Prompts configuration dictionary

    Returns:
        Formatted prompt string
    """
    template = prompts_config.get("aggregation_prompt", "")
    num_views = len(view_analyses)

    # Format view analyses as a numbered list
    analyses_text = "\n\n".join(
        f"View {i+1}:\n{analysis}" for i, analysis in enumerate(view_analyses)
    )

    return template.format(num_views=num_views, view_analyses=analyses_text)

