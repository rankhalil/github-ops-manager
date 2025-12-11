"""Contains utility functions for working with YAML files."""

import logging
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from github_ops_manager.schemas.tac import TestingAsCodeTestCaseDefinition, TestingAsCodeTestCaseDefinitions

logger = logging.getLogger(__name__)

yaml = YAML(typ="safe")


def load_yaml_file(path: Path) -> dict[str, Any]:
    """Loads a YAML file and returns a dictionary."""
    with open(path, encoding="utf-8") as f:
        return yaml.load(f)  # type: ignore[no-any-return]


def create_yaml_dumper() -> YAML:
    """Creates a properly configured YAML object for dumping with multiline string support."""
    yaml_dumper = YAML()
    yaml_dumper.default_flow_style = False
    yaml_dumper.explicit_start = True
    yaml_dumper.indent(mapping=2, sequence=4, offset=2)  # type: ignore[attr-defined]
    yaml_dumper.width = 4096  # Prevent line wrapping for long lines
    yaml_dumper.preserve_quotes = True

    # Configure multiline string handling
    def represent_str(dumper: Any, data: str) -> Any:
        """Custom string representer that uses literal scalar style for multiline strings."""
        if "\n" in data:
            # Use literal scalar style (|) for multiline strings
            return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
        # Use default representation for single-line strings
        return dumper.represent_scalar("tag:yaml.org,2002:str", data)

    yaml_dumper.representer.add_representer(str, represent_str)  # type: ignore[attr-defined]

    return yaml_dumper


def load_test_case_definitions_from_directory(directory_path: Path) -> TestingAsCodeTestCaseDefinitions:
    """Load and merge test case definitions from all YAML files in a directory.

    Args:
        directory_path: Path to directory containing YAML files with test case definitions

    Returns:
        TestingAsCodeTestCaseDefinitions object with all test cases from all files

    Raises:
        FileNotFoundError: If directory doesn't exist
        ValueError: If duplicate test case titles are found across files
        ValidationError: If any YAML file has invalid structure
    """
    if not directory_path.exists():
        raise FileNotFoundError(f"Directory not found: {directory_path.absolute()}")

    if not directory_path.is_dir():
        raise ValueError(f"Path is not a directory: {directory_path.absolute()}")

    # Find all YAML files in the directory (including .yafml from Quicksilver)
    yaml_files: list[Path] = []
    for extension in ["*.yaml", "*.yml", "*.yafml"]:
        yaml_files.extend(directory_path.glob(extension))

    if not yaml_files:
        raise ValueError(f"No YAML files found in directory: {directory_path.absolute()}")

    # Sort files for deterministic processing order
    yaml_files.sort()

    all_test_cases: list[TestingAsCodeTestCaseDefinition] = []
    seen_titles: dict[str, str] = {}  # title -> filename mapping for duplicate detection
    duplicate_titles: dict[str, list[str]] = {}  # title -> list of filenames mapping for duplicate detection

    for yaml_file in yaml_files:
        # Load and validate each file
        try:
            yaml_content = load_yaml_file(yaml_file)
            file_model = TestingAsCodeTestCaseDefinitions.model_validate(yaml_content)

            # Check for duplicate titles
            for test_case in file_model.test_cases:
                if test_case.title in seen_titles:
                    duplicate_titles[test_case.title].append(yaml_file.name)
                else:
                    seen_titles[test_case.title] = yaml_file.name

                all_test_cases.append(test_case)

        except Exception as e:
            raise ValueError(f"Error processing file '{yaml_file.name}': {str(e)}") from e

    if duplicate_titles:
        logger.error("%s duplicate test case titles found", len(duplicate_titles.keys()))
        for title, files in duplicate_titles.items():
            logger.error("  %s: %s", title, ", ".join(files))
        raise ValueError(f"{len(duplicate_titles.keys())} duplicate test case titles found")

    return TestingAsCodeTestCaseDefinitions(test_cases=all_test_cases)


def dump_yaml_to_file(data: Any, file_path: Path) -> None:
    """Dumps data to a YAML file with proper multiline string formatting."""
    yaml_dumper = create_yaml_dumper()
    with open(file_path, "w", encoding="utf-8") as f:
        yaml_dumper.dump(data, f)  # type: ignore[misc]
