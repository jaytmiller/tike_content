#! env python

"""This is the prototype of a notebook curation tool which enables a curator to
specify a set of notebooks and which will then be used as the basis for defining
a conda environment inputs suitable for running all of them.  In addition to
defining a precise set of package versions to install,  it collects inputs for
testinging the resulting environment and runners which execute the tests.  The
long term goal of this tool is to provide inputs to build and test Jupyter
notebook Docker images in a CI/CD enabling curators to deploy science platform
notebook images with minimal interaction with platform administrators.

To that end it has the following features:

- Loads, validates, updates, and saves the notebook specifications.

- Automaically clones the git repositories for the notebooks if a local clone
does not already exist,  otherwise it updates the existing clones from their repos.

- Searches for notebooks and package reuquirements.txt files in the specified
directories and subdirectories based on regular expressions.

If --clone is specified,  it will clone or update the specified repositories
depdending on whether they already exist or not.

If --init-env is specified,  it will initialize the target environment with
packages required by nb-curator and also register the environment as a local
jupyterlab kernel.

If --compile is specified,  it will create both a conda environment .yml file
and a locked pip requirements.txt file based on compiling the requirements.txt
The resulting environment should be suitable for running all the notebook files
found in the repositories,  modulo the completeness of their requirememts.txt files.

If --install is specified,  it will install the packages in the conda
environment,  which XXXXX again at this time is the runtime environment.
After installation,  it will attempt to import any package which is explicitly
listed in a notebook file as a basic sanity check.

If --test-notebooks is specified, run notebooks matching any of the subsequent
comma separated list of notebook names or regular expressions.  If no notebooks
or regexps are specified,  it will run all notebooks.

If --cleanup is specified,  it will remove all cloned repositories.

Generates a trivial conda environment .yml file based on the Python version
specified by the curator spec.   XXXX Currently this .yml output is unused and
package installation and testing occur relative to the curator runtime environment.
"""

import argparse
import os
import os.path
import sys
import stat
import subprocess
import logging
import shutil
import traceback
import re
import json
import pdb  # Add import for the debugger
import tempfile
import datetime
import glob
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
from typing import Optional, Set

from ruamel.yaml import YAML  # Replace standard yaml with ruamel.yaml

# =========================================================================================

NOTEBOOK_MAX_SECS = 30 * 60

CURATOR_PACKAGES = """
uv
mamba
papermill
ipykernel
""".strip().splitlines()

# =========================================================================================


class NotebookCurator:
    """
    Class to process notebook image specifications, clone repositories,
    and generate environment specifications.
    """

    def __init__(
        self,
        spec_file: str,
        revise_spec_file: bool = False,
        python_program: str = sys.executable,
        output_dir: str = "./output",
        repos_dir: str = None,
        verbose: bool = False,
        debug: bool = False,
        cleanup: bool = False,
        compile: bool = False,
        no_simplify_paths: bool = False,
        install: bool = False,
        test: bool = False,
        jobs: int = 1,
        timeout: int = 300,
        kernel="base",
        init_env: bool = False,  # Add this parameter
        clone: bool = False,
    ):
        """
        Initialize the notebook spec compiler.

        Args:
            spec_file: Path to the YAML specification file
            output_dir: Directory to store output files
            repos_dir: Directory to store cloned repositories (persistent)
            verbose: Enable verbose output
            debug: Enable debugging with pdb on errors
            cleanup: Whether to clean up repository clones after execution
            compile: Whether to use pip-compile to generate pinned requirements
            no_simplify_paths: Whether to skip path simplification in annotated requirements
            init_env: Whether to initialize the environment before processing
        """
        self.spec_file = spec_file
        self.python_program = python_program
        self.revise_spec_file = revise_spec_file
        self.output_dir = Path(output_dir)
        # Default to current working directory for repos if not specified
        self.repos_dir = (
            Path(repos_dir) if repos_dir else Path(os.getcwd()) / "notebook-repos"
        )
        self.verbose = verbose
        self.debug_mode = debug
        self.compile = compile
        self.no_simplify_paths = no_simplify_paths
        self.install = install
        self.test = test
        self.jobs = jobs
        self.timeout = timeout
        self.kernel = kernel
        self.cleanup = cleanup
        self.init_env = init_env  # Add this line
        self.clone = clone

        # Set up logging
        logging.basicConfig(
            level=logging.DEBUG if self.verbose else logging.INFO,
            format="%(levelname)s - %(message)s",
        )
        self.logger = logging.getLogger("curator")

        # Initialize empty values
        self.spec = {}
        self.image_name = ""
        self.default_nb_repo = ""
        self.default_root_nb_directory = ""
        self.repo_dir = None
        self.repos_to_setup = {}  # Will store repo_url -> path mappings
        self.errors = []
        self.warnings = []
        self.exceptions = []
        self.notebook_imports = []
        self.unique_notebooks = []

        # Create output directory if it doesn't exist
        os.makedirs(self.output_dir, exist_ok=True)
        # Create repositories directory if it doesn't exist
        os.makedirs(self.repos_dir, exist_ok=True)

    def _lformat(self, *args) -> str:
        return " ".join(map(str, args))

    def error(self, *args) -> bool:
        """
        Log an error message and optionally drop into the debugger if debug mode
        is enabled. return False indicating failure.
        """
        msg = self._lformat(*args)
        self.errors.append(msg)
        self.logger.error(msg)
        return False

    def info(self, *args) -> bool:
        """
        Log an info message. return True indicating success.
        """
        self.logger.info(self._lformat(*args))
        return True

    def warning(self, *args) -> bool:
        """
        Log a warning message. return True indicating success.
        """
        msg = self._lformat(*args)
        self.warnings.append(msg)
        self.logger.warning(msg)
        return True

    def debug(self, *args) -> None:
        """
        Log a debug message. return None
        """
        self.logger.debug(self._lformat(*args))
        return None

    def exception(self, e: Exception, *args) -> bool:
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
        msg = self._lformat(*args)
        self.exceptions.append(msg)
        self.error("EXCEPTION: ", msg)
        if self.debug_mode:
            print(f"\n*** DEBUG MODE: Exception caught: {msg} ***")
            print(
                "*** Dropping into debugger. Type 'c' to continue and raise the exception, or 'q' to quit. ***"
            )
            print(f"*** Exception type: {type(e).__name__} ***")
            print(f"*** Exception message: {str(e)} ***")
            print("*** Traceback (most recent call last): ***")
            traceback.print_tb(e.__traceback__)
            pdb.post_mortem(
                e.__traceback__
            )  # This will start the debugger at the point of the exception
            # If the user continues from the debugger, we'll raise the exception
            raise e
        return False  # Signal that execution should continue with error handling

    def print_log_counters(self):
        """
        Print the number of errors, warnings, and exceptions encountered during compilation.
        """
        print(f"Exceptions: {len(self.exceptions)}")
        print(f"Errors: {len(self.errors)}")
        print(f"Warnings: {len(self.warnings)}")

    @property
    def requested_python_version(self):
        version = self.spec["image_spec_header"]["python_version"]
        if not isinstance(version, str):
            raise ValueError(
                "Invalid python_version in spec file,  must be a YAML string of form 'x', 'x.y', or 'x.y.z'."
            )
        return list(map(int, version.split(".")))

    def main(self) -> bool:
        """
        Main function to run the curator.
        """
        try:
            return self._main()
        except Exception as e:
            return self.exception(e, f"Error curating: {e}")

    def _main(self) -> bool:
        """
        Run the curator.

        Returns:
            bool: True if requested actions were successful, False otherwise
        """
        # Initialize environment if requested
        if self.init_env:
            if not self.initialize_environment():
                return False

        # Load the spec file
        if not self.load_spec():
            return False

        # Validate the spec
        if not self.validate_spec():
            return False

        if not self.check_python_version():
            return False

        # Clone or use local repositories
        if not self.setup_repositories():
            return False

        # Collect notebook paths
        notebook_paths = self.collect_notebook_paths()
        if not notebook_paths:
            return False

        test_imports = self.extract_imports(notebook_paths)
        if not test_imports:
            return False

        # Find requirements files
        repo_requirements_files = self.find_requirements_files(notebook_paths)
        if not repo_requirements_files:
            return False

        # Compile package versions if requested
        if self.compile:
            package_versions = self.compile_requirements(repo_requirements_files)
            # Generate conda spec for trivial Python environment.
            conda_spec = self.generate_conda_spec()
            if not package_versions:
                return False
        else:
            if "out" not in self.spec:
                self.spec["out"] = {}
            conda_spec = self.spec["out"]["conda_spec"]
            package_versions = self.spec["out"]["package_versions"]

        if self.revise_spec_file and not self.revise_spec(
            package_versions,
            sorted(notebook_paths),
            sorted(test_imports.keys()),
            conda_spec,
        ):
            return False

        # Install packages if requested
        if self.install:
            if not self.install_packages(package_versions):
                return False
            if not self.test_imports(test_imports):
                return False

        # Retrict test notebooks to CLI regexes
        if self.test:
            tested_notebooks = self.filter_notebook_list(notebook_paths)
            if not tested_notebooks:
                return False
            if not self.test_notebooks(tested_notebooks):
                return False

        # Clean up if requested
        if self.cleanup:
            if not self.cleanup_repos():
                return False
        else:
            self.info("Skipping repository cleanup as --cleanup was not specified")

        return True

    def _load_and_validate(self) -> bool:
        """Helper method to load and validate the spec."""
        return self.load_spec() and self.validate_spec()

    def get_yaml(self):
        """Return our standard configuration of ruamel.yaml."""
        yaml = YAML()  # Initialize ruamel.yaml
        yaml.preserve_quotes = True  # Preserve quotes in the YAML
        yaml.indent(mapping=2, sequence=4, offset=2)
        return yaml

    def load_spec(self) -> bool:
        """
        Load and validate the YAML specification file using ruamel.yaml to preserve order.

        Returns:
            bool: True if loading was successful, False otherwise
        """
        try:
            yaml = self.get_yaml()
            with open(self.spec_file, "r") as f:
                self.spec = yaml.load(f)
            return self.info(f"Successfully loaded spec from {self.spec_file}")
        except Exception as e:
            return self.exception(e, f"Failed to load YAML spec: {e}")

    @property
    def top_level_keywords(self) -> list:
        """
        Return a list of top-level keywords from the spec file.
        """
        return self.allowed_keywords.keys()

    def validate_spec(self) -> bool:
        """
        Perform basic validation on the loaded specification.

        Returns:
            bool: True if validation passed, False otherwise
        """
        # Define allowed keywords based on prototype-protocol.yaml
        self.allowed_keywords = {
            "image_spec_header": [
                "image_name",
                "description",
                "valid_on",
                "expires_on",
                "python_version",
                "nb_repo",
                "root_nb_directory",
            ],
            "selected_notebooks": [
                "nb_repo",
                "root_nb_directory",
                "include_subdirs",
                "exclude_subdirs",
            ],
            "out": {},
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
            if key not in self.top_level_keywords:
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
            if key not in self.allowed_keywords["image_spec_header"]:
                return self.error(f"Unknown keyword in image_spec_header: {key}")

        # Check for required header fields
        required_header_fields = [
            "image_name",
            "python_version",
            "valid_on",
            "expires_on",
            "nb_repo",
        ]
        for field in required_header_fields:
            if field not in header:
                return self.error(
                    f"Missing required field in image_spec_header: {field}"
                )

        # Extract header values
        self.image_name = header["image_name"]
        self.python_version = str(header["python_version"])
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
        for entry in self.spec["selected_notebooks"]:
            for key in entry:
                if key not in self.allowed_keywords["selected_notebooks"]:
                    return self.error(
                        f"Unknown keyword in selected_notebooks entry: {key}"
                    )

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
        """
        return repo_url.startswith("file://")

    def get_local_repo_path(self, repo_url: str) -> Path:
        """
        Get the path to a local repository.
        """
        # Remove the file:// prefix and expand user directory if needed
        local_path = repo_url[repo_url.index("//") + 2 :]  # Remove "file://"
        return Path(os.path.expanduser(local_path))

    def setup_repositories(self) -> bool:
        """
        Set up all repositories specified in the spec - either clone remote repos
        or use local directories.

        Returns:
            bool: True if setup was successful, False otherwise
        """
        if not hasattr(self, "repos_to_setup") or not self.repos_to_setup:
            return self.error("No repositories to set up")

        self.info(f"Setting up repositories: {list(self.repos_to_setup.keys())}")

        # Process each repository
        for repo_url in self.repos_to_setup:
            if self.is_local_repo(repo_url):
                # Handle local repository
                local_path = self.get_local_repo_path(repo_url)
                if not local_path.exists():
                    return self.error(
                        f"Local repository path does not exist: {local_path}"
                    )

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
        repo_name = repo_url.split("/")[-1].replace(".git", "")
        repo_dir = self.repos_dir / repo_name
        if not self.clone:
            if repo_dir.exists():
                self.info(
                    f"No cloning requested and {repo_dir} exists.  Skipping clone/update."
                )
                return repo_dir
            else:
                raise RuntimeError(
                    f"No cloning requested and no clone exists at directory {repo_dir}."
                )
        try:  # clone or update
            if repo_dir.exists():
                # Repository already exists, try to update it
                self.info(
                    f"Repository already exists at {repo_dir}, attempting to update"
                )
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
                    self.warning(
                        f"Failed to update repository at {repo_dir}: {e.stderr}"
                    )
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
                        timeout=300,
                    )
                    self.info(
                        f"Successfully cloned repository {repo_url} to {repo_dir}"
                    )
                    return repo_dir
                except subprocess.TimeoutExpired:
                    # Clean up partial clone
                    if repo_dir.exists():
                        shutil.rmtree(repo_dir)
                    return self.error(f"Timeout cloning repository {repo_url}")
        except Exception as e:
            self.error(f"Failed to clone repository {repo_url}: {e.stderr}")
            return None

    def collect_notebook_paths(self) -> bool:
        """
        Collect paths to all notebooks specified in the spec.
        """
        notebook_paths = []

        for entry in self.spec["selected_notebooks"]:
            # Get repository and directory information
            nb_repo = entry.get("nb_repo", self.default_nb_repo)

            # Get the repository directory
            repo_dir = self.repos_to_setup[nb_repo]
            if not repo_dir:
                return self.error(f"Repository not set up: {nb_repo}")

            # Get root notebook directory (default or override)
            root_nb_directory = entry.get(
                "root_nb_directory", self.default_root_nb_directory
            )

            # Process this directory entry
            notebook_paths.extend(
                self._process_directory_entry(entry, repo_dir, root_nb_directory)
            )

        self.info(
            "Found", len(notebook_paths), "notebooks:", "\n" + "\n".join(notebook_paths)
        )
        return notebook_paths

    def _process_directory_entry(
        self, entry: dict, repo_dir: Path, root_nb_directory: str
    ) -> list[str]:
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
        notebook_paths = []
        include_subdirs = entry.get("include_subdirs", ["."])
        for subdir in include_subdirs:
            subdir_path = base_path / subdir
            if not subdir_path.exists():
                self.warning(f"Included directory does not exist: {subdir_path}")
                continue

            self.debug(f"Scanning {subdir_path} for notebooks")
            # Find all notebooks in this directory
            subdir_notebook_paths = []
            for nb_path in subdir_path.glob("**/*.ipynb"):
                # Check if the notebook is in an excluded directory
                exclude_subdirs = entry.get("exclude_subdirs", [])
                for exclude in exclude_subdirs:
                    if exclude in str(nb_path):
                        self.debug(f"Excluding {nb_path} due to {exclude}")
                        break
                else:
                    self.debug(f"Including {nb_path}")
                    subdir_notebook_paths.append(str(nb_path))
            notebook_paths.extend(subdir_notebook_paths)
        return notebook_paths

    def extract_imports(self, notebook_paths) -> bool:
        """
        Extract import statements from notebooks and save to separate files.

        Returns:
            list[str] imports if successful, None otherwise
        """
        if not notebook_paths:
            return self.warning("No notebooks found to extract imports from")

        # Create the extraction directory
        extract_dir = self.output_dir / "notebook-imports"
        os.makedirs(extract_dir, exist_ok=True)

        try:
            # Use a set to ensure each notebook is processed only once
            unique_notebooks = set(str(nb_path) for nb_path in notebook_paths)
            self.info(
                f"Processing {len(unique_notebooks)} unique notebooks for import extraction"
            )

            import_to_nb = dict()
            for nb_path_str in unique_notebooks:
                nb_dict = self._read_notebook_json(nb_path_str)
                imports = self._extract_imports_from_notebook(nb_dict)
                self.debug(f"Extracted {imports} imports from {nb_path_str}")
                for imp in imports:
                    if imp not in import_to_nb:
                        import_to_nb[imp] = []
                    import_to_nb[imp].append(nb_path_str)
            self.info(f"Extracted {len(import_to_nb)} imports:", "\n" + "\n".join(import_to_nb.keys()))
            return import_to_nb
        except Exception as e:
            self.exception(e, f"Error extracting imports from notebooks: {e}")
            return None

    def _read_notebook_json(self, nb_path: str) -> Optional[dict]:
        """
        Read and parse a notebook file as JSON.

        Args:
            nb_path: Path to the notebook file

        Returns:
            Optional[dict] The parsed notebook as a dictionary, or None if parsing failed
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

        # If both groups are None, return None
        if package_path is None:
            return None

        # Extract the root package (first component before any dots)
        root_package = package_path.split(".")[0]

        # Skip built-in modules and special imports
        if root_package in ["__future__", "builtins", "sys", "os"]:
            return None

        return root_package

    def find_requirements_files(self, notebook_paths) -> bool:
        """
        Find requirements.txt files in the notebook directories.

        Returns:
            Optional[List[Path]]: List of paths to requirements.txt files, or None if no files found
        """
        if not hasattr(self, "repos_to_setup") or not self.repos_to_setup:
            return self.error("Repositories not set up, cannot find requirements files")

        requirements_files = []

        # Look for requirements.txt in the same directories as notebooks
        notebook_dirs = {Path(nb_path).parent for nb_path in notebook_paths}
        for dir_path in notebook_dirs:
            req_file = dir_path / "requirements.txt"
            if req_file.exists():
                requirements_files.append(req_file)
                self.debug(f"Found requirements file: {req_file}")
        self.info(
            f"Found {len(requirements_files)} requirements.txt files:",
            "\n" + "\n".join(str(file) for file in requirements_files),
        )
        return requirements_files

    def combine_requirements(self, requirements_files) -> bool:
        """
        Process requirements.txt files from individual notebooks to generate a
        comprehensive package list.

        Returns:
            list[str] | bool:  list of raw package requirements if processing was successful, False otherwise
        """
        combined_requirements = set()
        for req_file in requirements_files:
            try:
                with open(req_file, "r") as f:
                    for line in f:
                        # Basic processing - could be enhanced with proper requirement parsing
                        line = line.strip()
                        if line and not line.startswith("#"):
                            combined_requirements.add(line)
                            self.debug(
                                f"From {req_file} added package requirement: {line}"
                            )
            except Exception as e:
                return self.exception(
                    e, f"Error processing requirements file {req_file}: {e}"
                )
        combined_requirements = list(sorted(combined_requirements))
        self.info(
            f"Processed requirements into {len(combined_requirements)} unique packages:\n",
            combined_requirements,
        )
        return sorted(list(combined_requirements))

    def generate_conda_spec(self) -> str | bool:
        """
        Generate conda spec dict.

        Returns:
            bool: spec dict if successful, False otherwise
        """
        try:
            return {
                "name": self.moniker.lower(),
                "channels": ["conda-forge"],
                "dependencies": [
                    (
                        f"python={self.python_version}"
                        if self.python_version
                        else "python"
                    ),
                    "pip",
                    {"pip": {}},
                ],
            }
        except Exception as e:
            return self.exception(e, f"Error generating conda environment: {e}")

    def filter_notebook_list(self, notebook_paths) -> list[str] | bool:
        """
        Generate a list of included notebooks.
        """
        try:
            # Use a set to eliminate duplicates
            unique_notebooks = set()
            for nb_path in sorted(notebook_paths):
                for regex in self.test.split(","):
                    if re.search(regex, nb_path):
                        unique_notebooks.add(nb_path)
            unique_notebooks = sorted(unique_notebooks)
            self.info(
                f"Filtered notebook list to {len(unique_notebooks)} unique entries: {unique_notebooks}"
            )
            return unique_notebooks
        except Exception as e:
            return self.exception(e, f"Error generating notebook list: {e}")

    def cleanup_repos(self) -> bool:
        """
        Clean up temporary files and directories.

        Returns:
            bool: True if cleanup was successful, False otherwise
        """
        try:
            if self.repos_dir and os.path.exists(self.repos_dir):
                self.info(f"Cleaning up repository directory: {self.repos_dir}")
                shutil.rmtree(self.repos_dir)
                return True
        except Exception as e:
            return self.exception(e, f"Error during cleanup: {e}")

    def revise_spec(
        self, package_versions, test_notebooks, test_imports, conda_spec
    ) -> bool:
        shutil.copy(self.spec_file, self.spec_file + ".bak")
        try:
            self.info(f"Revising spec file {self.spec_file} with program outputs.")
            if "out" not in self.spec:
                self.spec["out"] = dict()
            self.spec["out"]["conda_spec"] = conda_spec
            self.spec["out"]["package_versions"] = package_versions or []
            self.spec["out"]["test_notebooks"] = [str(p) for p in test_notebooks] or []
            self.spec["out"]["test_imports"] = test_imports or []
            with open(self.spec_file, "w+") as f:
                yaml = self.get_yaml()
                yaml.dump(self.spec, f)
            os.remove(self.spec_file + ".bak")
            return True
        except Exception as e:
            shutil.copy(self.spec_file + ".bak", self.spec_file)
            return self.exception(e, f"Error revising spec file: {e}")

    def run(self, args, **keys) -> bool:
        self.debug("Running:", args[0], keys)
        result = subprocess.run(
            args, check=False, capture_output=True, text=True, **keys
        )
        if result.returncode != 0:
            print()
            return self.error(
                f"Failed running {args} with {result.returncode}:\n{result.stderr}"
            )
        return result.stdout

    @property
    def moniker(self) -> str:
        return self.image_name.replace(" ", "-")

    @property
    def compile_out_file(self) -> Path:
        return self.output_dir / f"{self.moniker}-compile-output.txt"

    def compile_requirements(self, requirements_files) -> bool:
        """
        Compile requirements files into a fully specified dependency list,
        resolving all package versions.

        This method takes all discovered requirements files and uses pip-compile to generate
        a fully pinned requirements file with exact versions for all dependencies.

        Returns:
            bool: True if compilation was successful, False otherwise
        """
        try:
            # Run pip-compile to generate pinned requirements
            self.info("Compiling all requirements files to determine package versions.")
            output = self.run_uv_compile(self.compile_out_file, requirements_files)

            if output is False:
                self.error(
                    "========== Failed compiling requirements ==========",
                    "\n" + self.annotated_requirements(requirements_files),
                )
                return False

            # Read the compiled requirements and add them to the package list
            package_versions = []
            with open(self.compile_out_file, "r") as f:
                for line in f:
                    line = line.strip()
                    if line and not line.startswith("#"):
                        package_versions.append(line)
            self.info(
                f"Resolved requirements to {len(package_versions)} package versions:",
                "\n" + "\n".join(package_versions),
            )
            return package_versions
        except Exception as exc:
            return self.exception(exc, "Failed compiling requirements.")

    def annotated_requirements(self, requirements_files) -> str:
        result = []
        for req_file in requirements_files:
            with open(req_file, "r") as f:
                for pkgdep in f.read().splitlines():
                    if pkgdep and not pkgdep.startswith("#"):
                        result.append((pkgdep, str(req_file)))
        result = sorted(result)
        if not self.no_simplify_paths:
            pkgs, paths = zip(*result)
            # paths = remove_common_prefix(paths)
            result = zip(pkgs, paths)
        return "\n".join(f"{pkg:<20}  : {path:<55}" for pkg, path in result)

    def run_uv_compile(
        self, compiled_req_file: str, requirements_files: list[str]
    ) -> str:
        cmd = [
            "uv",
            "pip",
            "compile",
            "--output-file",
            str(compiled_req_file),
            "--no-header",
            "--python",
            self.python_program,
            "--python-version",
            self.python_version,
            "--annotate",
        ] + [str(f) for f in requirements_files]
        if self.verbose:
            cmd.append("--verbose")
        return self.run(cmd)

    def install_packages(self, package_versions) -> bool:
        """
        Install the compiled package list into the system Python environment.

        Returns:
            bool: True if installation was successful, False otherwise
        """
        try:
            if not package_versions:
                return self.warning("No packages found to install")

            self.info(
                f"Installing {len(package_versions)} packages into system Python environment"
            )

            # Create a temporary requirements file with all packages
            temp_req_file = self.output_dir / f"{self.moniker}-install-requirements.txt"

            with open(temp_req_file, "w") as f:
                for package in sorted(package_versions):
                    f.write(f"{package}\n")

            # Use pip to install the packages
            cmd = ["uv", "pip", "install", "-r", str(temp_req_file)]

            if self.verbose:
                cmd.append("--verbose")

            self.info(f"Running: {' '.join(cmd)}")

            result = subprocess.run(cmd, check=False, capture_output=True, text=True)

            if result.returncode != 0:
                self.error(
                    f"Package installation failed with return code {result.returncode}"
                )
                self.error(f"STDERR:\n{result.stderr}")
                return False

            self.info("Package installation completed successfully")
            if self.verbose and result.stdout:
                self.info(f"Installation output: {result.stdout}")

            return True

        except Exception as e:
            return self.exception(e, f"Error installing packages: {e}")

    def test_notebooks(self, notebook_paths: list[str | Path]) -> bool:
        """
        Test the installed packages by running all notebooks which match the
        specified regexes or all notebooks if no regexes are specified.
        """
        # notebooks = [str(path) for path in notebook_paths]
        failing = test_notebooks(
            notebook_paths, kernel=self.kernel, jobs=self.jobs, timeout=self.timeout
        )
        if failing:
            print(divider("FAILED"))
            for notebook in failing:
                self.error(f"Notebook {notebook} failed tests")
        else:
            return self.info("All notebooks passed tests")

    def test_imports(self, import_map: dict[str, list[str]]) -> bool:
        """
        Test the installed packages by importing all modules extracted from
        the selected notebooks.
        """
        self.info(f"Testing {len(import_map)} imports")
        errs = []
        for pkg in import_map:
            if pkg.startswith("#"):
                self.info("Skipping import pkg: ", pkg)
                continue
            try:
                __import__(pkg)
                self.info("Importing", pkg, "... ok")
            except Exception:
                traceback.print_exc()
                self.error(
                    "FAIL import", pkg, "by notebook", import_map.get(pkg, "unknown")
                )
                errs.append(pkg)
        if errs:
            self.error(f"Failed to import {len(errs)} packages:", errs)
            return False
        else:
            return self.info("All imports succeeded.")

    def initialize_environment(self) -> bool:
        """
        Initialize the environment for notebook processing.

        This is a stub method that can be implemented to set up the environment
        before processing notebooks (e.g., creating virtual environments,
        installing base dependencies, etc.).

        Returns:
            bool: True if initialization was successful, False otherwise
        """
        self.info("Initializing environment...")
        output = self.run(["pip", "install"] + CURATOR_PACKAGES)
        if not output:
            return self.error(
                "Installing curator pacakges in target environment failed: {output}"
            )
        output = self.run(
            f"python -m ipykernel install --user --name {self.init_env}".split()
        )
        if not output:
            return self.error(
                "Registering JupyterLab kernel for target environment failed: {output}"
            )
        # TODO: Implement environment initialization logic here
        self.info("Environment initialization completed successfully")
        return True

    def check_python_version(self) -> bool:
        """
        Check if the install environment Python version was requested.
        """
        self.info("Checking Python version...")
        output = self.run(["python", "--version"])
        system_version = list(map(int, output.strip().split()[-1].split(".")))
        requested_version = self.requested_python_version
        for i, version in enumerate(requested_version):
            if version != system_version[i]:
                return self.error(
                    f"The working environment is running Python {system_version} but "
                    f"Python {requested_version} is requested in the specification."
                )
        return True


# -------------------------------------------------------------------------------


def test_notebooks(notebooks, kernel="base", jobs=1, timeout=NOTEBOOK_MAX_SECS):
    """Run all the notebooks specified by globbing `notebook_globs` using the
    system Python environment, running `jobs` notebooks in parallel subprocesses.

    Notebooks running for longer than `timeout` seconds are termimated.

    Return   count of failed notebooks
    """
    print(
        divider(
            f"Testing {len(notebooks)} notebooks on kernel {kernel}  using {jobs} jobs"
        ).strip()
    )
    failing_notebooks = []
    with ProcessPoolExecutor(max_workers=jobs) as e:
        for failed, notebook, output in e.map(
            test_notebook,
            notebooks,
            ["base"] * len(notebooks),
            [timeout] * len(notebooks),
        ):
            sys.stdout.write(output)
            sys.stdout.flush()
            if failed:
                failing_notebooks.append(notebook)
    return failing_notebooks


def divider(title, char="*", width=100):
    """Print a divider with `title` centered between N `char` characters for a total of `width`."""
    return f" {title} ".center(width, char) + "\n"


# Because test_notebook chdir's,  it needs to be run serially or as a true subprocess.


def test_notebook(notebook, kernel="base", timeout=NOTEBOOK_MAX_SECS):
    """Run one `notebook` on JupyterHub `kernel` with temporary output."""

    if notebook.startswith("#"):
        return (0, divider(f"Skipping {notebook}"), " ")

    base_nb = os.path.basename(notebook)

    start = datetime.datetime.now()

    # print(divider(f"Starting test of {notebook} on {kernel}"))
    # sys.stdout.flush()

    output = divider(f"Testing '{base_nb}' on kernel '{kernel}'")

    here = os.getcwd()
    err = 1  # assume failed

    with tempfile.TemporaryDirectory() as temp_dir:

        source_path = os.path.dirname(os.path.abspath(notebook))
        test_dir = temp_dir + "/notebook-test"
        shutil.copytree(source_path, test_dir)
        os.chdir(test_dir)
        os.chmod(test_dir, stat.S_IRWXU)
        for path in glob.glob("*"):
            os.chmod(path, stat.S_IRWXU)

        if notebook.endswith(".ipynb"):
            cmd = f"papermill --no-progress-bar {os.path.basename(notebook)} -k {kernel} test.ipynb"
        elif notebook.endswith(".py"):
            cmd = f"python {notebook}"
        else:
            raise ValueError(f"Unhandled test file extension for: {notebook}")

        result = subprocess.run(
            cmd.split(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )  # maybe succeeds

        err = result.returncode
        output += result.stdout

    os.chdir(here)

    elapsed = datetime.datetime.now() - start
    output += divider(f"Tested {base_nb} {'OK' if not err else 'FAIL'} {elapsed}")

    return int(err != 0), notebook, output


# -------------------------------------------------------------------------------


def remove_common_prefix(strings: list[str]) -> list[str]:
    if not strings:
        return []
    # Find the shortest string (to avoid index out of range)
    shortest = min(strings, key=len)
    prefix_length = 0
    for i in range(len(shortest)):
        if all(s.startswith(shortest[: i + 1]) for s in strings):
            prefix_length = i + 1
        else:
            break
    # Remove the common prefix
    return [s[prefix_length:] for s in strings]


# -------------------------------------------------------------------------------


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Process notebook image specification YAML and prepare notebook environment and tests."
    )
    parser.add_argument(
        "spec_file", type=str, help="Path to the YAML specification file."
    )
    parser.add_argument(
        "--python-program",
        type=str,
        default=sys.executable,
        help="Path to python program to use for installation and test,  default to Python running this script.",
    )
    parser.add_argument(
        "--revise-spec-file",
        action="store_true",
        help="Add computed values to the spec file under outputs: like found-notebooks: or found-packages:",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default="./output",
        help="Directory to store output files",
    )
    parser.add_argument(
        "--repos-dir",
        type=str,
        default="./notebook-repos",
        help="Directory to store/locate cloned repos.",
    )
    parser.add_argument(
        "--clone",
        action="store_true",
        help="If set, clone or update notebook repos at --repos-dir.",
    )
    parser.add_argument(
        "--init-env",
        default=None,
        const="base",
        nargs="?",
        help="Initialize the environment before processing notebooks.  Target env should already be active.",
    )
    parser.add_argument(
        "-c",
        "--compile",
        action="store_true",
        help="Compile input package lists to generate pinned requirements.   Install optional with --install",
    )
    parser.add_argument(
        "-i",
        "--install",
        action="store_true",
        help="Pip install resolved notebook dependencies in system Python environment.",
    )
    parser.add_argument(
        "-t",
        "--test-notebooks",
        default=None,
        const=".*",
        nargs="?",
        type=str,
        help="Whether and/or which notebooks to crash-test headless.  Comma separated list of notebook names or regexes.  Default no notebooks if switch unspecified,  all notebooks if switch used but no names or regexes given.",
    )
    parser.add_argument(
        "-j",
        "--jobs",
        default=1,
        type=int,
        help="Number of parallel jobs to run for notebook testing.  Default 1.",
    )
    parser.add_argument(
        "--timeout",
        default=30 * 60,
        type=int,
        help="Timeout in seconds for notebook tests.  Notebooks running longer than this are killed.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable verbose output."
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help="Enable debugging with pdb on errors and preserve exception stack traces.",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="Cleanup repo clones after processing.",
    )
    parser.add_argument(
        "--no-simplify-paths",
        action="store_true",
        help="Use full paths in requirements table output for failed compiles.",
    )
    return parser.parse_args()


# -------------------------------------------------------------------------------


def main():
    args = parse_args()

    # Create and run the compiler
    compiler = NotebookCurator(
        spec_file=args.spec_file,
        revise_spec_file=args.revise_spec_file,
        output_dir=args.output_dir,
        verbose=args.verbose,
        debug=args.debug,
        compile=args.compile,
        install=args.install,
        test=args.test_notebooks,
        jobs=args.jobs,
        timeout=args.timeout,
        repos_dir=args.repos_dir,
        cleanup=args.cleanup,
        no_simplify_paths=args.no_simplify_paths,
        python_program=sys.executable,
        init_env=args.init_env,  # Add this line
        clone=args.clone,
    )

    success = compiler.main()
    compiler.print_log_counters()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
