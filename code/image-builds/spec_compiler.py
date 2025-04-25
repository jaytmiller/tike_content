#! env python

import argparse
import os
import sys
import subprocess
import tempfile
import logging
import shutil
import traceback
import re
import json

from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Any, Set

from ruamel.yaml import YAML  # Replace standard yaml with ruamel.yaml

# Set up logging
logging.basicConfig(
    level=logging.INFO, format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)
logger = logging.getLogger("spec-compiler")


class NotebookSpecCompiler:
    """
    Class to process notebook image specifications, clone repositories,
    and generate environment specifications.
    """

    def __init__(
        self, spec_file: str, output_dir: str, verbose: bool = False, extract_imports: bool = False
    ):
        """
        Initialize the notebook spec compiler.

        Args:
            spec_file: Path to the YAML specification file
            output_dir: Directory to store output files
            verbose: Enable verbose output
            extract_imports: Extract import statements from notebooks
        """
        self.spec_file = spec_file
        self.output_dir = Path(output_dir)
        self.verbose = verbose
        self.extract_imports = extract_imports
        
        # Initialize empty values
        self.spec = {}
        self.image_name = ""
        self.default_nb_repo = ""
        self.default_root_nb_directory = ""
        self.python_version = ""
        self.repo_dir = None
        self.notebook_paths = []
        self.requirements_files = []
        self.package_list = set()
        
        # Create output directory if it doesn't exist
        os.makedirs(self.output_dir, exist_ok=True)

    def run(self) -> bool:
        """
        Run the compiler.
        
        Returns:
            bool: True if compilation was successful, False otherwise
        """
        try:
            # Load the spec file
            if not self.load_spec():
                return False
            
            # Collect notebook paths (now includes repository cloning)
            if not self.collect_notebook_paths():
                return False
            
            # Find requirements files
            if not self.find_requirements_files():
                return False
            
            # Process requirements
            if not self.process_requirements():
                return False
            
            # Generate package list
            if not self.generate_package_list():
                return False
            
            # Generate notebook list
            if not self.generate_notebook_list():
                return False
            
            # Extract imports if requested
            if not self.process_imports():
                return False
            
            # Cleanup
            if not self.cleanup():
                return False
            
            return True
        except Exception as e:
            logger.error(f"Error running compiler: {e}")
            return False
        
    def _load_and_validate(self) -> bool:
        """Helper method to load and validate the spec."""
        return self.load_spec() and self.validate_spec()
    
    def _extract_imports_if_needed(self) -> bool:
        """Helper method to conditionally extract imports."""
        if not self.extract_imports:
            logger.info("Notebook import extracxtion not requested.  use --extract-imports to request or specify packages in the spec file.")
            return True
        return self.extract_notebook_imports()

    def load_spec(self) -> bool:
        """
        Load and validate the YAML specification file using ruamel.yaml to preserve order.

        Returns:
            bool: True if loading was successful, False otherwise
        """
        try:
            yaml = YAML()  # Initialize ruamel.yaml
            yaml.preserve_quotes = True  # Preserve quotes in the YAML

            with open(self.spec_file, "r") as f:
                self.spec = yaml.load(f)
            logger.info(f"Successfully loaded spec from {self.spec_file}")
            return True
        except Exception as e:
            logger.error(f"Failed to load YAML spec: {e}")
            return False

    def validate_spec(self) -> bool:
        """
        Perform basic validation on the loaded specification.

        Returns:
            bool: True if validation passed, False otherwise
        """
        # Check for required fields
        required_fields = ["image_spec_header", "selected_notebooks"]

        for field in required_fields:
            if field not in self.spec:
                logger.error(f"Missing required field: {field}")
                return False

        # Extract header information
        header = self.spec["image_spec_header"]

        # Check for image name
        self.image_name = header.get("image_name")
        if not self.image_name:
            logger.error("Missing image_name in image_spec_header")
            return False

        # Check for notebook repository
        self.nb_repo = header.get("nb_repo")
        if not self.nb_repo:
            logger.error("Missing nb_repo in image_spec_header")
            return False

        # Get Python version
        self.python_version = header.get("python_version")
        if not self.python_version:
            logger.warning("No Python version specified, will use default")

        # Get root notebook directory
        self.root_nb_directory = header.get("root_nb_directory", "")

        # Get validity dates
        self.valid_on = header.get("valid_on")
        self.expires_on = header.get("expires_on")

        logger.info(f"Spec validation passed for image: {self.image_name}")
        return True

    def clone_repository(self) -> bool:
        """
        Clone the notebook repository specified in the spec.

        Returns:
            bool: True if cloning was successful, False otherwise
        """
        if not self.nb_repo:
            logger.error("No notebook repository specified in spec")
            return False

        # Create a temporary directory for the clone
        self.repo_dir = tempfile.mkdtemp(prefix="notebook-repo-")
        logger.info(f"Cloning repository {self.nb_repo} to {self.repo_dir}")

        try:
            subprocess.run(
                ["git", "clone", self.nb_repo, self.repo_dir],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info(f"Successfully cloned repository to {self.repo_dir}")
            return True
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to clone repository: {e.stderr}")
            return False

    def collect_notebook_paths(self) -> bool:
        """
        Collect notebook paths based on the spec and cloned repository.

        Returns:
            bool: True if collection was successful, False otherwise
        """
        self.notebook_paths = []

        # Process selected_notebooks section
        if "selected_notebooks" not in self.spec:
            logger.error("No selected_notebooks section in spec")
            return False

        repo_path = Path(self.repo_dir)
        
        for entry in self.spec["selected_notebooks"]:
            if "directories" in entry:
                self._process_directory_entry(entry, repo_path)
        
        logger.info(f"Collected {len(self.notebook_paths)} notebooks")
        return True

    def _process_directory_entry(self, entry: dict, repo_dir: Path, root_nb_directory: str) -> None:
        """
        Process a directory entry from the spec file.
        
        Args:
            entry: The directory entry from the spec
            repo_dir: Path to the repository
            root_nb_directory: Root notebook directory within the repository
        """
        directories = entry["directories"]
        
        # Construct the base path for notebooks
        base_path = repo_dir
        if root_nb_directory:
            base_path = base_path / root_nb_directory
        
        # Process include_subdirs
        include_subdirs = directories.get("include_subdirs", ["."])
        for subdir in include_subdirs:
            subdir_path = base_path / subdir
            if not subdir_path.exists():
                logger.warning(f"Included directory does not exist: {subdir_path}")
                continue
        
            # Find all notebooks in this directory
            for nb_path in subdir_path.glob("**/*.ipynb"):
                # Check if the notebook is in an excluded directory
                exclude_subdirs = directories.get("exclude_subdirs", [])
                excluded = False
                for exclude in exclude_subdirs:
                    exclude_path = base_path / exclude
                    if str(nb_path).startswith(str(exclude_path)):
                        excluded = True
                        break
            
                if not excluded:
                    self.notebook_paths.append(nb_path)

    def extract_notebook_imports(self) -> bool:
        """
        Extract import statements from notebooks and save to separate files.

        Returns:
            bool: True if extraction was successful, False otherwise
        """
        if not self.notebook_paths:
            logger.warning("No notebooks found to extract imports from")
            return True

        # Create the extraction directory
        extract_dir = self.output_dir / "extracted"
        os.makedirs(extract_dir, exist_ok=True)

        try:
            # Regular expressions to match import statements
            # Matches both "import package" and "from package import something"
            import_pattern = re.compile(
                r"^(?:import\s+([a-zA-Z0-9_\.]+))|(?:from\s+([a-zA-Z0-9_\.]+)\s+import)"
            )

            # Use a set to ensure each notebook is processed only once
            unique_notebooks = set(str(nb_path) for nb_path in self.notebook_paths)
            logger.info(
                f"Processing {len(unique_notebooks)} unique notebooks for import extraction"
            )

            for nb_path_str in unique_notebooks:
                nb_path = Path(nb_path_str)
                # Get notebook rootname
                rootname = nb_path.stem

                # Read the notebook
                with open(nb_path, "r", encoding="utf-8") as f:
                    try:
                        notebook = json.load(f)
                    except json.JSONDecodeError:
                        logger.warning(f"Could not parse notebook {nb_path} as JSON")
                        continue

                # Extract import statements
                imports = set()

                # Process each cell
                for cell in notebook.get("cells", []):
                    if cell.get("cell_type") == "code":
                        # Get the source code as a string
                        if isinstance(cell.get("source"), list):
                            source = "".join(cell.get("source", []))
                        else:
                            source = cell.get("source", "")

                        # Process each line
                        for line in source.split("\n"):
                            line = line.strip()
                            match = import_pattern.match(line)
                            if match:
                                # The pattern has two capture groups, one for each import style
                                # Take the first non-None group
                                package_path = match.group(1) or match.group(2)

                                # Extract the root package (first component before any dots)
                                root_package = package_path.split(".")[0]

                                # Skip built-in modules and special imports
                                if root_package not in [
                                    "__future__",
                                    "builtins",
                                    "sys",
                                    "os",
                                ]:
                                    imports.add(root_package)

                # Write imports to file
                output_file = extract_dir / f"imports-{rootname}.pip"
                with open(output_file, "w") as f:
                    for package in sorted(imports):
                        f.write(f"{package}\n")

                logger.debug(
                    f"Extracted {len(imports)} imports from {rootname} to {output_file}"
                )

            logger.info(
                f"Extracted imports from {len(unique_notebooks)} unique notebooks to {extract_dir}"
            )
            return True

        except Exception as e:
            logger.error(f"Error extracting imports from notebooks: {e}")
            logger.error(traceback.format_exc())
            return False

    def find_requirements_files(self) -> bool:
        """
        Find requirements.txt files in the notebook directories.

        Returns:
            bool: True if requirements files were found, False otherwise
        """
        if not self.repo_dir:
            logger.error("Repository not cloned, cannot find requirements files")
            return False

        self.requirements_files = []
        repo_path = Path(self.repo_dir)

        # Look for requirements.txt in the same directories as notebooks
        notebook_dirs = {nb_path.parent for nb_path in self.notebook_paths}

        for dir_path in notebook_dirs:
            req_file = dir_path / "requirements.txt"
            if req_file.exists():
                self.requirements_files.append(req_file)
                logger.debug(f"Found requirements file: {req_file}")

        logger.info(f"Found {len(self.requirements_files)} requirements.txt files")
        return True

    def process_requirements(self) -> bool:
        """
        Process requirements.txt files to generate a comprehensive package list.

        Returns:
            bool: True if processing was successful, False otherwise
        """
        if not self.requirements_files:
            logger.warning("No requirements.txt files found")
            return True

        for req_file in self.requirements_files:
            try:
                with open(req_file, "r") as f:
                    for line in f:
                        # Basic processing - could be enhanced with proper requirement parsing
                        line = line.strip()
                        if line and not line.startswith("#"):
                            self.package_list.add(line)
                            logger.debug(f"Added package requirement: {line}")
            except Exception as e:
                logger.error(f"Error processing requirements file {req_file}: {e}")
                return False

        logger.info(
            f"Processed requirements into {len(self.package_list)} unique packages"
        )
        return True

    def generate_environment_specs(self) -> bool:
        """
        Generate conda and pip environment specifications.

        Returns:
            bool: True if generation was successful, False otherwise
        """
        # Generate pip requirements file
        pip_requirements = (
            self.output_dir / f"{self.image_name.replace(' ', '_')}_requirements.txt"
        )
        try:
            with open(pip_requirements, "w") as f:
                for package in sorted(self.package_list):
                    f.write(f"{package}\n")
            logger.info(f"Generated pip requirements file: {pip_requirements}")
        except Exception as e:
            logger.error(f"Error generating pip requirements file: {e}")
            return False

        # Generate a simple conda environment YAML
        conda_env = (
            self.output_dir / f"{self.image_name.replace(' ', '_')}_environment.yml"
        )
        try:
            yaml = YAML()
            yaml.indent(mapping=2, sequence=4, offset=2)

            env_dict = {
                "name": self.image_name.replace(" ", "_").lower(),
                "channels": ["conda-forge", "defaults"],
                "dependencies": [
                    (
                        f"python={self.python_version}"
                        if self.python_version
                        else "python"
                    ),
                    "pip",
                    {"pip": sorted(self.package_list)},
                ],
            }

            with open(conda_env, "w") as f:
                yaml.dump(env_dict, f)

            logger.info(f"Generated conda environment file: {conda_env}")
        except Exception as e:
            logger.error(f"Error generating conda environment file: {e}")
            return False

        return True

    def generate_notebook_list(self) -> bool:
        """
        Generate a list of included notebooks.

        Returns:
            bool: True if generation was successful, False otherwise
        """
        notebook_list = (
            self.output_dir / f"{self.image_name.replace(' ', '_')}_notebooks.txt"
        )
        try:
            # Use a set to eliminate duplicates
            unique_notebooks = set()

            for nb_path in sorted(self.notebook_paths):
                # Get path relative to repo root
                rel_path = nb_path.relative_to(self.repo_dir)
                unique_notebooks.add(str(rel_path))

            with open(notebook_list, "w") as f:
                for notebook in sorted(unique_notebooks):
                    f.write(f"{notebook}\n")

            logger.info(
                f"Generated notebook list with {len(unique_notebooks)} unique entries: {notebook_list}"
            )
            return True
        except Exception as e:
            logger.error(f"Error generating notebook list: {e}")
            return False

    def cleanup(self) -> None:
        """Clean up temporary files and directories."""
        if self.repo_dir and os.path.exists(self.repo_dir):
            logger.info(f"Cleaning up repository directory: {self.repo_dir}")
            shutil.rmtree(self.repo_dir)


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Process notebook image specification YAML and prepare notebook environment"
    )
    parser.add_argument(
        "spec_file", type=str, help="Path to the YAML specification file"
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./output",
        help="Directory to store output files",
    )
    parser.add_argument(
        "--verbose", "-v", action="store_true", help="Enable verbose output"
    )
    parser.add_argument(
        "--extract-imports",
        action="store_true",
        help="Extract import statements from notebooks and save to separate files",
    )
    return parser.parse_args()


def main():
    args = parse_args()

    # Create and run the compiler
    compiler = NotebookSpecCompiler(
        spec_file=args.spec_file,
        output_dir=args.output_dir,
        verbose=args.verbose,
        extract_imports=args.extract_imports,
    )

    success = compiler.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
