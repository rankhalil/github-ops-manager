"""Contains utilities for rendering Jinja2 templates."""

from pathlib import Path

import jinja2
import structlog
from pydantic import BaseModel

logger: structlog.stdlib.BoundLogger = structlog.get_logger(__name__)

# GitHub's maximum issue body length
GITHUB_MAX_BODY_LENGTH = 65536


def construct_jinja2_environment() -> jinja2.Environment:
    """Construct a Jinja2 environment."""
    jinja_env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    return jinja_env


def construct_jinja2_template_from_string(template_string: str, environment: jinja2.Environment | None = None) -> jinja2.Template:
    """Construct a Jinja2 template from a string."""
    if environment is None:
        environment = construct_jinja2_environment()
    return environment.from_string(template_string)


def construct_jinja2_template_from_file(template_path: Path | str, environment: jinja2.Environment | None = None) -> jinja2.Template:
    """Construct a Jinja2 template from a file."""
    if environment is None:
        environment = construct_jinja2_environment()
    try:
        with open(template_path, encoding="utf-8") as f:
            template_content = f.read()
    except FileNotFoundError:
        logger.error("Jinja2 template not found", template_path=template_path)
        raise
    return environment.from_string(template_content)


def render_template_with_model(model: BaseModel, template: jinja2.Template, max_body_length: int | None = None) -> str:
    """Render a Jinja2 template against a Pydantic model.

    Args:
        model: Pydantic model to render template with
        template: Jinja2 template to render
        max_body_length: Optional maximum body length. If provided and exceeded, will attempt progressive truncation.

    Returns:
        Rendered template string
    """
    try:
        rendered_template = template.render(model.model_dump())
    except jinja2.UndefinedError as exc:
        logger.error("Failed to render template with model", model_type=type(model).__name__, model=model, error=str(exc))
        raise

    # Check if we need to truncate
    if max_body_length and len(rendered_template) > max_body_length:
        logger.warning(
            "Rendered template exceeds maximum length, attempting progressive truncation",
            current_length=len(rendered_template),
            max_length=max_body_length,
            model_type=type(model).__name__,
        )
        rendered_template = _truncate_rendered_body(rendered_template, max_body_length)

    return rendered_template


def _truncate_rendered_body(body: str, max_length: int) -> str:
    """Truncate a rendered body to fit within the maximum length.

    This is a simple truncation strategy that cuts off the body and adds a warning message.
    For more sophisticated truncation (e.g., re-rendering with lower limits), this would need
    to be enhanced.

    Args:
        body: The rendered body string
        max_length: Maximum allowed length

    Returns:
        Truncated body string
    """
    if len(body) <= max_length:
        return body

    truncation_message = "\n\n... (body truncated to fit GitHub's 65536 character limit)"
    truncation_message_length = len(truncation_message)

    # Calculate how much of the original body we can keep
    available_length = max_length - truncation_message_length

    if available_length <= 0:
        logger.error("Cannot truncate body - truncation message itself is too long")
        raise ValueError(f"Body length {len(body)} exceeds maximum {max_length} and cannot be truncated")

    truncated_body = body[:available_length] + truncation_message

    logger.info(
        "Body truncated to fit GitHub limits",
        original_length=len(body),
        truncated_length=len(truncated_body),
        characters_removed=len(body) - len(truncated_body),
    )

    return truncated_body
