#! env python

"""
Coding notes:
For the sake of brevity,  we assume self.info and self.warning return True, 
and self.error and self.exception return False, and self.debug returns None.
"""

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
import pdb  # Add import for the debugger

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
        self, spec_file: str, output_dir: str, verbose: bool = False, extract_imports: bool = False,
        debug: bool = False
    ):
        """
        Initialize the notebook spec compiler.

        Args:
            spec_file: Path to the YAML specification file
            output_dir: Directory to store output files
            verbose: Enable verbose output
            extract_imports: Extract import statements from notebooks
            debug: Enable debugging with pdb on errors
        """
        self.spec_file = spec_file
        self.output_dir = Path(output_dir)
        self.verbose = verbose
        self.extract_imports = extract_imports
        self.debug = debug
        
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
    
    def error(self, message: str) -> None:
        """
        Log an error message and optionally drop into the debugger if debug mode is enabled.
        
        Args:
            message: The error message to log
        """
        logger.error(message)
        if self.debug:
            print(f"\n*** DEBUG MODE: Dropping into debugger due to error: {message} ***")
            pdb.set_trace()
        return False

    def info(self, message: str) -> None:
        """
        Log an info message.
        
        Args:
            message: The info message to log

        Returns: True
        """
        logger.info(message)
        return True
    
    def warning(self, message: str) -> None:
        """
        Log an info message.
        
        Args:
            message: The info message to log

        Returns: True
        """
        logger.warning(message)
        return True
    
    
    def exception(self, e: Exception, message: str) -> bool:
        """
        Handle an exception: log the error message and either drop into the debugger
        in debug mode or return False to indicate failure.
        
        Args:
            e: The exception object
            message: The error message to log
        
        Returns:
            bool: Always False in non-debug mode (to indicate failure)
            
        Raises:
            Exception: Re-raises the provided exception in debug mode after pdb session ends
        """
        logger.error(message, exc_info=True)
        if self.debug:
            print(f"\n*** DEBUG MODE: Exception caught: {message} ***")
            print("*** Dropping into debugger. Type 'c' to continue and raise the exception, or 'q' to quit. ***")
            print(f"*** Exception type: {type(e).__name__} ***")
            print(f"*** Exception message: {str(e)} ***")
            print("*** Traceback (most recent call last): ***")
            traceback.print_tb(e.__traceback__)
            pdb.post_mortem(e.__traceback__)  # This will start the debugger at the point of the exception
            # If the user continues from the debugger, we'll raise the exception
            raise e
        return False  # Signal that execution should continue with error handling

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
            
            # Validate the spec
            if not self.validate_spec():
                return False
        
            # Clone the repository
            if not self.clone_repositories():
                return False
        
            # Collect notebook paths
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
            return self.exception(e, f"Error running compiler: {e}")
        
    def _load_and_validate(self) -> bool:
        """Helper method to load and validate the spec."""
        return self.load_spec() and self.validate_spec()
    
    def _extract_imports_if_needed(self) -> bool:
        """Helper method to conditionally extract imports."""
        if not self.extract_imports:
            return self.info("Notebook import extracxtion not requested.  use --extract-imports to request or specify packages in the spec file.")
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
            return self.info(f"Successfully loaded spec from {self.spec_file}")
        except Exception as e:
            return self.exception(e, f"Failed to load YAML spec: {e}")

    def validate_spec(self) -> bool:
        """
        Perform basic validation on the loaded specification.

        Returns:
            bool: True if validation passed, False otherwise
        """
        # Define allowed keywords based on prototype-protocol.yaml
        self.allowed_keywords = {
            "top_level": ["image_spec_header", "selected_notebooks"],
            "header": [
                "image_name", "description", "valid_on", "expires_on", 
                "python_version", "nb_repo", "root_nb_directory"
            ],
            "selected_notebooks_entry": [
                "nb_repo", "root_nb_directory", "include_subdirs", "exclude_subdirs"
            ]
        }
        
        # Check for required fields and unknown keywords
        if not self._validate_top_level_structure():
            return False
        
        # Validate header section
        if not self._validate_header_section():
            return False
        
        # Validate selected_notebooks section
        if not self._validate_selected_notebooks_section():
            return False
        
        # Validate that all repositories in directory entries are specified
        if not self._validate_directory_repos():
            return False

        return self.info(f"Spec validation passed for image: {self.image_name}")

    def _validate_top_level_structure(self) -> bool:
        """
        Validate the top-level structure of the spec.
        
        Returns:
            bool: True if validation passed, False otherwise
        """
        # Check for required fields
        required_fields = ["image_spec_header", "selected_notebooks"]
        for field in required_fields:
            if field not in self.spec:
                return self.error(f"Missing required field: {field}")
        
        # Check for unknown top-level keywords
        for key in self.spec:
            if key not in self.allowed_keywords["top_level"]:
                return self.error(f"Unknown top-level keyword: {key}")
                
        return True

    def _validate_header_section(self) -> bool:
        """
        Validate the image_spec_header section.
        
        Returns:
            bool: True if validation passed, False otherwise
        """
        header = self.spec["image_spec_header"]
        
        # Check for unknown header keywords
        for key in header:
            if key not in self.allowed_keywords["header"]:
                return self.error(f"Unknown keyword in image_spec_header: {key}")
        
        # Check for required header fields
        required_header_fields = ["image_name", "python_version", "valid_on", "expires_on", "nb_repo"]
        for field in required_header_fields:
            if field not in header:
                return self.error(f"Missing required field in image_spec_header: {field}")
        
        # Extract header values
        self.image_name = header["image_name"]
        self.python_version = header["python_version"]
        self.valid_on = header["valid_on"]
        self.expires_on = header["expires_on"]
        self.default_nb_repo = header["nb_repo"]
        
        # Get default root notebook directory
        self.default_root_nb_directory = header.get("root_nb_directory", "")
        
        return True

    def _validate_selected_notebooks_section(self) -> bool:
        """
        Validate the selected_notebooks section.
        
        Returns:
            bool: True if validation passed, False otherwise
        """
        if "selected_notebooks" not in self.spec:
            return self.error("Missing selected_notebooks section")
        
        # Update allowed keywords for the new structure
        allowed_entry_keywords = [
            "nb_repo", "root_nb_directory", "include_subdirs", "exclude_subdirs"
        ]
        
        for entry in self.spec["selected_notebooks"]:
            for key in entry:
                if key not in allowed_entry_keywords:
                    return self.error(f"Unknown keyword in selected_notebooks entry: {key}")
        
        return True

    def _validate_directory_repos(self) -> bool:
        """
        Validate that all repositories in directory entries are specified.
        
        Returns:
            bool: True if validation passed, False otherwise
        """
        if "selected_notebooks" not in self.spec:
            return self.error("No selected_notebooks section in spec")
        
        # Track all repositories that need to be cloned
        self.repos_to_clone = {self.default_nb_repo: None}  # repo_url -> cloned_path
        
        for entry in self.spec["selected_notebooks"]:
            # Check if this entry specifies a custom repository
            nb_repo = entry.get("nb_repo", self.default_nb_repo)
            if not nb_repo:
                return self.error(f"Missing repository for entry: {entry}")
            
            # Add to the list of repos to clone
            self.repos_to_clone[nb_repo] = None
        
        return True

    def clone_repositories(self) -> bool:
        """
        Clone all repositories specified in the spec.

        Returns:
            bool: True if cloning was successful, False otherwise
        """
        if not hasattr(self, 'repos_to_clone') or not self.repos_to_clone:
            return self.error("No repositories to clone")

        # Create a temporary directory for the clones if it doesn't exist
        self.repo_base_dir = tempfile.mkdtemp(prefix="notebook-repos-")
        self.info(f"Using base directory for repositories: {self.repo_base_dir}")

        self.info(f"Cloning repositories {self.repos_to_clone.keys()}")
        
        # Clone each repository
        for repo_url in self.repos_to_clone:
            # Create a unique directory name based on the repo URL
            repo_name = repo_url.split('/')[-1].replace('.git', '')
            repo_dir = os.path.join(self.repo_base_dir, repo_name)
            
            self.info(f"Cloning repository {repo_url} to {repo_dir}")
            
            try:
                subprocess.run(
                    ["git", "clone", repo_url, repo_dir],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                # Store the cloned path
                self.repos_to_clone[repo_url] = repo_dir
                self.info(f"Successfully cloned repository {repo_url} to {repo_dir}")
            except subprocess.CalledProcessError as e:
                return self.exception(e, f"Failed to clone repository {repo_url}: {e.stderr}")

    def process_notebooks(self) -> bool:
        """
        Process all notebooks specified in the spec.
        
        Returns:
            bool: True if processing was successful, False otherwise
        """
        self.notebook_paths = []
        
        for entry in self.spec["selected_notebooks"]:
            # Get repository and directory information
            nb_repo = entry.get("nb_repo", self.default_nb_repo)
            
            # Find the repository directory
            repo_name = nb_repo.split('/')[-1].replace('.git', '')
            repo_dir = Path(self.repo_base_dir) / repo_name
            
            # Get root notebook directory (default or override)
            root_nb_directory = entry.get("root_nb_directory", self.default_root_nb_directory)
            
            # Process this directory entry
            self._process_directory_entry(entry, repo_dir, root_nb_directory)
        
        return self.info(f"Found {len(self.notebook_paths)} notebooks")

    def _process_directory_entry(self, entry: dict, repo_dir: Path, root_nb_directory: str) -> None:
        """
        Process a directory entry from the spec file.
        
        Args:
            entry: The directory entry from the spec
            repo_dir: Path to the repository
            root_nb_directory: Root notebook directory within the repository
        """
        # Get the repository override if specified
        nb_repo = entry.get("nb_repo", self.default_nb_repo)
        
        # Use the correct repository directory based on the override
        if nb_repo != self.default_nb_repo:
            repo_name = nb_repo.split('/')[-1].replace('.git', '')
            repo_dir = Path(self.repo_base_dir) / repo_name
        
        # Get the root_nb_directory override if specified
        if "root_nb_directory" in entry:
            root_nb_directory = entry["root_nb_directory"]
        
        # Construct the base path for notebooks
        base_path = repo_dir
        if root_nb_directory:
            base_path = base_path / root_nb_directory
        
        # Process include_subdirs
        include_subdirs = entry.get("include_subdirs", ["."])
        for subdir in include_subdirs:
            subdir_path = base_path / subdir
            if not subdir_path.exists():
                logger.warning(f"Included directory does not exist: {subdir_path}")
                continue
        
            # Find all notebooks in this directory
            for nb_path in subdir_path.glob("**/*.ipynb"):
                # Check if the notebook is in an excluded directory
                exclude_subdirs = entry.get("exclude_subdirs", [])
                excluded = False
                for exclude in exclude_subdirs:
                    exclude_path = base_path / exclude
                    if str(nb_path).startswith(str(exclude_path)):
                        excluded = True
                        break
            
                if not excluded:
                    self.notebook_paths.append(nb_path)

    def process_imports(self) -> bool:
        """
        Process imports from notebooks if requested.
        
        Returns:
            bool: True if processing was successful, False otherwise
        """
        if not self.extract_imports:
            return self.info("Notebook import extraction not requested. Use --extract-imports to request or specify packages in the spec file.")
        return self.extract_notebook_imports()

    def extract_notebook_imports(self) -> bool:
        """
        Extract import statements from notebooks and save to separate files.

        Returns:
            bool: True if extraction was successful, False otherwise
        """
        if not self.notebook_paths:
            return logger.warning("No notebooks found to extract imports from")

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
            self.info(
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

            self.info(
                f"Extracted imports from {len(unique_notebooks)} unique notebooks to {extract_dir}"
            )

        except Exception as e:
            return self.exception(e, f"Error extracting imports from notebooks: {e}")

    def find_requirements_files(self) -> bool:
        """
        Find requirements.txt files in the notebook directories.

        Returns:
            bool: True if requirements files were found, False otherwise
        """
        if not hasattr(self, 'repos_to_clone') or not self.repos_to_clone:
            return self.error("Repositories not cloned, cannot find requirements files")

        self.requirements_files = []

        # Look for requirements.txt in the same directories as notebooks
        notebook_dirs = {nb_path.parent for nb_path in self.notebook_paths}

        for dir_path in notebook_dirs:
            req_file = dir_path / "requirements.txt"
            if req_file.exists():
                self.requirements_files.append(req_file)
                logger.debug(f"Found requirements file: {req_file}")

        return self.info(f"Found {len(self.requirements_files)} requirements.txt files")

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
                return self.exception(e, f"Error processing requirements file {req_file}: {e}")

        return self.info(
            f"Processed requirements into {len(self.package_list)} unique packages"
        )

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
            self.info(f"Generated pip requirements file: {pip_requirements}")
        except Exception as e:
            return self.exception(e, f"Error generating pip requirements file: {e}")

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

            self.info(f"Generated conda environment file: {conda_env}")
        except Exception as e:
            return self.exception(e, f"Error generating conda environment file: {e}")

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
                # Find which repo this notebook belongs to
                repo_path = None
                for repo_url, path in self.repos_to_clone.items():
                    if str(nb_path).startswith(str(path)):
                        repo_path = path
                        break
            
                if repo_path:
                    # Get path relative to repo root
                    rel_path = nb_path.relative_to(repo_path)
                    unique_notebooks.add(str(rel_path))
                else:
                    # If we can't determine the repo, use the full path
                    unique_notebooks.add(str(nb_path))

            with open(notebook_list, "w") as f:
                for notebook in sorted(unique_notebooks):
                    f.write(f"{notebook}\n")

            return self.info(
                f"Generated notebook list with {len(unique_notebooks)} unique entries: {notebook_list}"
            )
        except Exception as e:
            return self.exception(e, f"Error generating notebook list: {e}")

    def cleanup(self) -> bool:
        """
        Clean up temporary files and directories.
        
        Returns:
            bool: True if cleanup was successful, False otherwise
        """
        try:
            if hasattr(self, 'repo_base_dir') and self.repo_base_dir and os.path.exists(self.repo_base_dir):
                self.info(f"Cleaning up repository directory: {self.repo_base_dir}")
                shutil.rmtree(self.repo_base_dir)
                return True
        except Exception as e:
            return self.exception(e, f"Error during cleanup: {e}")

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
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debugging with pdb on errors and preserve exception stack traces",
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
        debug=args.debug,
    )

    success = compiler.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
