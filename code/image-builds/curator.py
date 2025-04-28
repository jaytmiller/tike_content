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


class NotebookSpecCompiler:
    """
    Class to process notebook image specifications, clone repositories,
    and generate environment specifications.
    """

    def __init__(
        self, spec_file: str, output_dir: str, repos_dir: str = None, verbose: bool = False, 
        extract_imports: bool = False, debug: bool = False, cleanup: bool = False,
        use_pip_compile: bool = False
    ):
        """
        Initialize the notebook spec compiler.

        Args:
            spec_file: Path to the YAML specification file
            output_dir: Directory to store output files
            repos_dir: Directory to store cloned repositories (persistent)
            verbose: Enable verbose output
            extract_imports: Extract import statements from notebooks
            debug: Enable debugging with pdb on errors
            cleanup: Whether to clean up repository clones after execution
            use_pip_compile: Whether to use pip-compile to generate pinned requirements
        """
        self.spec_file = spec_file
        self.output_dir = Path(output_dir)
        # Default to current working directory for repos if not specified
        self.repos_dir = Path(repos_dir) if repos_dir else Path(os.getcwd()) / "notebook-repos"
        self.verbose = verbose
        self.extract_imports = extract_imports
        self.debug_mode = debug
        self.cleanup = cleanup
        self.use_pip_compile = use_pip_compile
        
        # Set up logging
        logging.basicConfig(
            level=logging.DEBUG if self.debug_mode else logging.INFO, 
                format="%(asctime)s - %(levelname)s - %(message)s")
        self.logger = logging.getLogger("curator")

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
        self.repos_to_setup = {}  # Will store repo_url -> path mappings
        
        # Create output directory if it doesn't exist
        os.makedirs(self.output_dir, exist_ok=True)
        # Create repositories directory if it doesn't exist
        os.makedirs(self.repos_dir, exist_ok=True)
    
    def error(self, message: str) -> bool:
        """
        Log an error message and optionally drop into the debugger if debug mode
        is enabled. return False indicating failure.
        """
        self.logger.error(message)
        if self.debug_mode:
            print(f"\n*** DEBUG MODE: Dropping into debugger due to error: {message} ***")
            pdb.set_trace()
        return False

    def info(self, message: str) -> bool:
        """
        Log an info message. return True indicating success.
        """
        self.logger.info(message)
        return True
    
    def warning(self, message: str) -> bool:
        """
        Log a warning message. return True indicating success.
        """
        self.logger.warning(message)
        return True
    
    def debug(self, message: str) -> None:
        """
        Log a debug message. return None
        """
        self.logger.debug(message)
        return None
    
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
        self.logger.error(message, exc_info=True)
        if self.debug_mode:
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
        
            # Clone or use local repositories
            if not self.setup_repositories():
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
            
            # Compile requirements with pip-compile if requested
            if self.use_pip_compile:
                if not self.compile_requirements():
                    return False
            
            # Clean up if requested
            if self.cleanup:
                if not self.cleanup_repos():
                    return False
            else:
                self.info("Skipping repository cleanup as --cleanup was not specified")
            
            return True
        except Exception as e:
            return self.exception(e, f"Error running compiler: {e}")
        
    def _load_and_validate(self) -> bool:
        """Helper method to load and validate the spec."""
        return self.load_spec() and self.validate_spec()
    
    def _extract_imports_if_needed(self) -> bool:
        """Helper method to conditionally extract imports."""
        if not self.extract_imports:
            return self.info("Notebook import extraction not requested. Use --extract-imports to request or specify packages in the spec file.")
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
        self.repos_to_setup = {self.default_nb_repo: None}  # repo_url -> path
        
        for entry in self.spec["selected_notebooks"]:
            # Check if this entry specifies a custom repository
            nb_repo = entry.get("nb_repo", self.default_nb_repo)
            if not nb_repo:
                return self.error(f"Missing repository for entry: {entry}")
            
            # Add to the list of repos to clone
            self.repos_to_setup[nb_repo] = None
        
        return True

    def is_local_repo(self, repo_url: str) -> bool:
        """
        Check if a repository URL refers to a local directory.
        
        Args:
            repo_url: The repository URL or path
            
        Returns:
            bool: True if it's a local repository, False otherwise
        """
        return repo_url.startswith("file://")

    def get_local_repo_path(self, repo_url: str) -> Path:
        """
        Get the path to a local repository.
        
        Args:
            repo_url: The repository URL with file:// prefix
            
        Returns:
            Path: The path to the local repository
        """
        # Remove the file:// prefix and expand user directory if needed
        local_path = repo_url[7:]  # Remove "file://"
        return Path(os.path.expanduser(local_path))

    def setup_repositories(self) -> bool:
        """
        Set up all repositories specified in the spec - either clone remote repos
        or use local directories.

        Returns:
            bool: True if setup was successful, False otherwise
        """
        if not hasattr(self, 'repos_to_setup') or not self.repos_to_setup:
            return self.error("No repositories to set up")

        self.info(f"Setting up repositories: {list(self.repos_to_setup.keys())}")
        
        # Process each repository
        for repo_url in self.repos_to_setup:
            if self.is_local_repo(repo_url):
                # Handle local repository
                local_path = self.get_local_repo_path(repo_url)
                if not local_path.exists():
                    return self.error(f"Local repository path does not exist: {local_path}")
                
                self.repos_to_setup[repo_url] = local_path
                self.info(f"Using local repository at {local_path}")
            else:
                # Handle remote repository that needs to be cloned
                repo_path = self._setup_remote_repo(repo_url)
                if not repo_path:
                    return False
                
                self.repos_to_setup[repo_url] = repo_path
        
        return True

    def _setup_remote_repo(self, repo_url: str) -> Optional[Path]:
        """
        Set up a remote repository by cloning it or using an existing clone.
        
        Args:
            repo_url: The repository URL
            
        Returns:
            Optional[Path]: The path to the repository, or None if setup failed
        """
        # Create a unique directory name based on the repo URL
        repo_name = repo_url.split('/')[-1].replace('.git', '')
        repo_dir = self.repos_dir / repo_name
        
        # Check if the repository already exists
        if repo_dir.exists():
            # Repository already exists, try to update it
            self.info(f"Repository already exists at {repo_dir}, attempting to update")
            try:
                # Try to pull the latest changes
                subprocess.run(
                    ["git", "-C", str(repo_dir), "pull"],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.info(f"Successfully updated repository at {repo_dir}")
                return repo_dir
            except subprocess.CalledProcessError as e:
                self.warning(f"Failed to update repository at {repo_dir}: {e.stderr}")
                self.warning("Will continue with existing repository version")
                return repo_dir
        else:
            # Repository doesn't exist, clone it
            self.info(f"Cloning repository {repo_url} to {repo_dir}")
            try:
                subprocess.run(
                    ["git", "clone", "--single-branch", repo_url, str(repo_dir)],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.info(f"Successfully cloned repository {repo_url} to {repo_dir}")
                return repo_dir
            except subprocess.CalledProcessError as e:
                self.error(f"Failed to clone repository {repo_url}: {e.stderr}")
                return None

    def collect_notebook_paths(self) -> bool:
        """
        Collect paths to all notebooks specified in the spec.
        
        Returns:
            bool: True if collection was successful, False otherwise
        """
        self.notebook_paths = []
        
        for entry in self.spec["selected_notebooks"]:
            # Get repository and directory information
            nb_repo = entry.get("nb_repo", self.default_nb_repo)
            
            # Get the repository directory
            repo_dir = self.repos_to_setup[nb_repo]
            if not repo_dir:
                return self.error(f"Repository not set up: {nb_repo}")
            
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
        # Construct the base path for notebooks
        base_path = repo_dir
        if root_nb_directory:
            base_path = base_path / root_nb_directory
        
        # Process include_subdirs
        include_subdirs = entry.get("include_subdirs", ["."])
        for subdir in include_subdirs:
            subdir_path = base_path / subdir
            if not subdir_path.exists():
                self.warning(f"Included directory does not exist: {subdir_path}")
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
            return self.warning("No notebooks found to extract imports from")

        # Create the extraction directory
        extract_dir = self.output_dir / "extracted"
        os.makedirs(extract_dir, exist_ok=True)

        try:
            # Use a set to ensure each notebook is processed only once
            unique_notebooks = set(str(nb_path) for nb_path in self.notebook_paths)
            self.info(f"Processing {len(unique_notebooks)} unique notebooks for import extraction")

            for nb_path_str in unique_notebooks:
                nb_path = Path(nb_path_str)
                self._process_notebook_for_imports(nb_path, extract_dir)

            self.info(f"Extracted imports from {len(unique_notebooks)} unique notebooks to {extract_dir}")
            return True

        except Exception as e:
            return self.exception(e, f"Error extracting imports from notebooks: {e}")

    def _process_notebook_for_imports(self, nb_path: Path, extract_dir: Path) -> None:
        """
        Process a single notebook to extract imports and write them to a file.
        
        Args:
            nb_path: Path to the notebook file
            extract_dir: Directory to write extracted imports
        """
        # Get notebook rootname
        rootname = nb_path.stem

        # Read and parse the notebook
        notebook = self._read_notebook_json(nb_path)
        if not notebook:
            return
        
        # Extract imports from the notebook
        imports = self._extract_imports_from_notebook(notebook)
        
        # Write imports to file
        output_file = extract_dir / f"imports-{rootname}.pip"
        with open(output_file, "w") as f:
            for package in sorted(imports):
                f.write(f"{package}\n")

        self.debug(f"Extracted {len(imports)} imports from {rootname} to {output_file}")

    def _read_notebook_json(self, nb_path: Path) -> Optional[dict]:
        """
        Read and parse a notebook file as JSON.
        
        Args:
            nb_path: Path to the notebook file
        
        Returns:
            Optional[dict]: The parsed notebook as a dictionary, or None if parsing failed
        """
        try:
            with open(nb_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except json.JSONDecodeError:
            self.warning(f"Could not parse notebook {nb_path} as JSON")
            return None

    def _extract_imports_from_notebook(self, notebook: dict) -> Set[str]:
        """
        Extract import statements from a notebook.
        
        Args:
            notebook: The notebook as a dictionary
        
        Returns:
            Set[str]: Set of imported package names
        """
        # Regular expressions to match import statements
        # Matches both "import package" and "from package import something"
        import_pattern = re.compile(
            r"^(?:import\s+([a-zA-Z0-9_\.]+))|(?:from\s+([a-zA-Z0-9_\.]+)\s+import)"
        )
        
        imports = set()
        
        # Process each cell
        for cell in notebook.get("cells", []):
            if cell.get("cell_type") == "code":
                source = self._get_cell_source(cell)
                
                # Process each line
                for line in source.split("\n"):
                    line = line.strip()
                    match = import_pattern.match(line)
                    if match:
                        root_package = self._extract_root_package(match)
                        if root_package:
                            imports.add(root_package)
        
        return imports

    def _get_cell_source(self, cell: dict) -> str:
        """
        Get the source code from a notebook cell.
        
        Args:
            cell: The notebook cell as a dictionary
        
        Returns:
            str: The source code as a string
        """
        # Get the source code as a string
        if isinstance(cell.get("source"), list):
            return "".join(cell.get("source", []))
        else:
            return cell.get("source", "")

    def _extract_root_package(self, match) -> Optional[str]:
        """
        Extract the root package name from a regex match of an import statement.
        
        Args:
            match: The regex match object
        
        Returns:
            Optional[str]: The root package name, or None if it's a built-in module
        """
        # The pattern has two capture groups, one for each import style
        # Take the first non-None group
        package_path = match.group(1) or match.group(2)
        
        # Extract the root package (first component before any dots)
        root_package = package_path.split(".")[0]
        
        # Skip built-in modules and special imports
        if root_package in ["__future__", "builtins", "sys", "os"]:
            return None
        
        return root_package

    def find_requirements_files(self) -> bool:
        """
        Find requirements.txt files in the notebook directories.

        Returns:
            bool: True if requirements files were found, False otherwise
        """
        if not hasattr(self, 'repos_to_setup') or not self.repos_to_setup:
            return self.error("Repositories not set up, cannot find requirements files")

        self.requirements_files = []

        # Look for requirements.txt in the same directories as notebooks
        notebook_dirs = {nb_path.parent for nb_path in self.notebook_paths}

        for dir_path in notebook_dirs:
            req_file = (dir_path / "requirements.txt").relative_to(os.getcwd())
            if req_file.exists():
                self.requirements_files.append(req_file)
                self.debug(f"Found requirements file: {req_file}")

        return self.info(f"Found {len(self.requirements_files)} requirements.txt files")

    def process_requirements(self) -> bool:
        """
        Process requirements.txt files to generate a comprehensive package list.

        Returns:
            bool: True if processing was successful, False otherwise
        """
        if not self.requirements_files:
            return self.warning("No requirements.txt files found")

        for req_file in self.requirements_files:
            try:
                with open(req_file, "r") as f:
                    for line in f:
                        # Basic processing - could be enhanced with proper requirement parsing
                        line = line.strip()
                        if line and not line.startswith("#"):
                            self.package_list.add(line)
                            self.debug(f"From {req_file} added package requirement: {line}")
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

            return self.info(f"Generated conda environment file: {conda_env}")
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
                for repo_url, path in self.repos_to_setup.items():
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

    def generate_package_list(self) -> bool:
        """
        Generate a comprehensive package list from processed requirements files
        and other sources.

        Returns:
            bool: True if generation was successful, False otherwise
        """
        try:
            # If we don't have any packages yet, warn but continue
            if not self.package_list:
                self.warning("No packages found in requirements files")
            
            # Generate environment specifications using the package list
            if not self.generate_environment_specs():
                return False
            
            self.info(f"Generated package list with {len(self.package_list)} packages")
            return True
        except Exception as e:
            return self.exception(e, f"Error generating package list: {e}")

    def compile_requirements(self) -> bool:
        """
        Compile requirements files into a fully specified dependency list using pip-compile.
        
        This method takes all discovered requirements files and uses pip-compile to generate
        a fully pinned requirements file with exact versions for all dependencies.
        
        Returns:
            bool: True if compilation was successful, False otherwise
        """
        try:
            if not self.requirements_files:
                return self.warning("No requirements files found to compile")
            
            # Create a temporary combined requirements file
            combined_req_file = self.output_dir / "combined_requirements.txt"
            with open(combined_req_file, "w") as outfile:
                for req_file in self.requirements_files:
                    self.info(f"Adding requirements from {req_file}")
                    with open(req_file, "r") as infile:
                        outfile.write(f"# From {req_file}\n")
                        outfile.write(infile.read())
                        outfile.write("\n\n")
            
            # Output file path for the compiled requirements
            compiled_req_file = self.output_dir / f"{self.image_name.replace(' ', '_')}_compiled_requirements.txt"
            
            # Run pip-compile to generate pinned requirements
            self.info(f"Running pip-compile on combined requirements")
            try:
                result = subprocess.run(
                    [
                        "pip-compile", 
                        "--output-file", str(compiled_req_file),
                        "--no-header",
                        "--no-emit-index-url",
                        "--allow-unsafe",
                        str(combined_req_file)
                    ],
                    check=True,
                    capture_output=True,
                    text=True,
                )
                self.info(f"Successfully compiled requirements to {compiled_req_file}")
                
                # Read the compiled requirements and add them to the package list
                with open(compiled_req_file, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            self.package_list.add(line)
                
                # Clean up the temporary combined file
                os.remove(combined_req_file)
                
                return True
                
            except subprocess.CalledProcessError as e:
                return self.error(f"pip-compile failed: {e.stderr}")
                
        except Exception as e:
            return self.exception(e, f"Error compiling requirements: {e}")

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
    parser.add_argument(
        "--use-pip-compile",
        action="store_true",
        help="Use pip-compile to generate pinned requirements",
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
        use_pip_compile=args.use_pip_compile,
    )

    success = compiler.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
