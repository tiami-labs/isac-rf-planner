"""File path utilities."""

from pathlib import Path
from typing import Optional, Union


def ensure_dir(path: Path) -> Path:
    """
    Ensure a directory exists, creating it if necessary.

    Args:
        path: Directory path

    Returns:
        Path object
    """
    path = Path(path)
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_output_path(input_path: Path, output_dir: Optional[Path] = None, suffix: str = "_output") -> Path:
    """
    Generate an output path from an input path.

    Args:
        input_path: Input file path
        output_dir: Optional output directory (default: same as input)
        suffix: Suffix to add before extension

    Returns:
        Output path
    """
    input_path = Path(input_path)
    if output_dir:
        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = input_path.stem
        ext = input_path.suffix
        return output_dir / f"{stem}{suffix}{ext}"
    else:
        return input_path.parent / f"{input_path.stem}{suffix}{input_path.suffix}"

