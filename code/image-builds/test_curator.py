import pytest
import os
import tempfile
import json
import shutil
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open

# Import the module and class being tested
from curator import NotebookSpecCompiler

# Sample valid YAML spec for testing
VALID_YAML_SPEC = """
image_spec_header:
  image_name: test-notebook-image
  description: Test notebook image
  valid_on: 2023-01-01
  expires_on: 2024-01-01
  python_version: "3.9"
  nb_repo: https://github.com/test/notebooks.git
  root_nb_directory: notebooks

selected_notebooks:
  - include_subdirs: ["."]
    exclude_subdirs: ["deprecated"]
  - nb_repo: https://github.com/test/more-notebooks.git
    root_nb_directory: examples
    include_subdirs: ["tutorials"]
"""

# Sample invalid YAML spec for testing
INVALID_YAML_SPEC = """
image_spec_header:
  image_name: test-notebook-image
  # Missing required fields
selected_notebooks:
  - include_subdirs: ["."]
"""

# Sample notebook JSON for testing
SAMPLE_NOTEBOOK = {
    "cells": [
        {
            "cell_type": "code",
            "source": [
                "import pandas as pd\n",
                "import numpy as np\n",
                "from sklearn.model_selection import train_test_split\n"
            ]
        },
        {
            "cell_type": "markdown",
            "source": "# This is a markdown cell"
        }
    ]
}

@pytest.fixture
def temp_dir():
    """Create a temporary directory for test files."""
    temp_dir = tempfile.mkdtemp()
    yield temp_dir
    shutil.rmtree(temp_dir)

@pytest.fixture
def valid_spec_file(temp_dir):
    """Create a valid spec file for testing."""
    spec_file = os.path.join(temp_dir, "valid_spec.yaml")
    with open(spec_file, "w") as f:
        f.write(VALID_YAML_SPEC)
    return spec_file

@pytest.fixture
def invalid_spec_file(temp_dir):
    """Create an invalid spec file for testing."""
    spec_file = os.path.join(temp_dir, "invalid_spec.yaml")
    with open(spec_file, "w") as f:
        f.write(INVALID_YAML_SPEC)
    return spec_file

@pytest.fixture
def sample_notebook_file(temp_dir):
    """Create a sample notebook file for testing."""
    notebook_file = os.path.join(temp_dir, "sample.ipynb")
    with open(notebook_file, "w") as f:
        json.dump(SAMPLE_NOTEBOOK, f)
    return notebook_file

@pytest.fixture
def sample_requirements_file(temp_dir):
    """Create a sample requirements.txt file for testing."""
    req_file = os.path.join(temp_dir, "requirements.txt")
    with open(req_file, "w") as f:
        f.write("pandas==1.3.5\n")
        f.write("numpy>=1.20.0\n")
        f.write("# This is a comment\n")
        f.write("scikit-learn\n")
    return req_file

def test_init_with_defaults(temp_dir, valid_spec_file):
    """Test initialization with default values."""
    compiler = NotebookSpecCompiler(
        spec_file=valid_spec_file,
        output_dir=temp_dir
    )
    
    assert compiler.spec_file == valid_spec_file
    assert compiler.output_dir == Path(temp_dir)
    assert compiler.repos_dir == Path(os.getcwd()) / "notebook-repos"
    assert not compiler.verbose
    assert not compiler.extract_imports
    assert not compiler.debug_mode
    assert not compiler.cleanup
    assert not compiler.compile

def test_init_with_custom_values(temp_dir, valid_spec_file):
    """Test initialization with custom values."""
    repos_dir = os.path.join(temp_dir, "repos")
    
    compiler = NotebookSpecCompiler(
        spec_file=valid_spec_file,
        output_dir=temp_dir,
        repos_dir=repos_dir,
        verbose=True,
        extract_imports=True,
        debug=True,
        cleanup=True,
        compile=True
    )
    
    assert compiler.spec_file == valid_spec_file
    assert compiler.output_dir == Path(temp_dir)
    assert compiler.repos_dir == Path(repos_dir)
    assert compiler.verbose
    assert compiler.extract_imports
    assert compiler.debug_mode
    assert compiler.cleanup
    assert compiler.compile

def test_load_spec_success(temp_dir, valid_spec_file):
    """Test successful loading of a valid spec file."""
    compiler = NotebookSpecCompiler(
        spec_file=valid_spec_file,
        output_dir=temp_dir
    )
    
    result = compiler.load_spec()
    
    assert result is True
    assert "image_spec_header" in compiler.spec
    assert "selected_notebooks" in compiler.spec
    assert compiler.spec["image_spec_header"]["image_name"] == "test-notebook-image"

def test_load_spec_failure(temp_dir):
    """Test failure when loading a non-existent spec file."""
    non_existent_file = os.path.join(temp_dir, "non_existent.yaml")
    compiler = NotebookSpecCompiler(
        spec_file=non_existent_file,
        output_dir=temp_dir
    )
    
    result = compiler.load_spec()
    
    assert result is False

def test_validate_spec_success(temp_dir, valid_spec_file):
    """Test successful validation of a valid spec."""
    compiler = NotebookSpecCompiler(
        spec_file=valid_spec_file,
        output_dir=temp_dir
    )
    
    # Load the spec first
    compiler.load_spec()
    
    # Then validate it
    result = compiler.validate_spec()
    
    assert result is True
    assert compiler.image_name == "test-notebook-image"
    assert compiler.python_version == "3.9"
    assert compiler.default_nb_repo == "https://github.com/test/notebooks.git"
    assert compiler.default_root_nb_directory == "notebooks"

def test_validate_spec_failure(temp_dir, invalid_spec_file):
    """Test failure when validating an invalid spec."""
    compiler = NotebookSpecCompiler(
        spec_file=invalid_spec_file,
        output_dir=temp_dir
    )
    
    # Load the spec first
    compiler.load_spec()
    
    # Then validate it
    result = compiler.validate_spec()
    
    assert result is False

def test_is_local_repo():
    """Test detection of local repositories."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir="dummy"
    )
    
    assert compiler.is_local_repo("file:///path/to/repo") is True
    assert compiler.is_local_repo("https://github.com/user/repo.git") is False
    assert compiler.is_local_repo("git@github.com:user/repo.git") is False

def test_get_local_repo_path():
    """Test extraction of local repository paths."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir="dummy"
    )
    
    path = compiler.get_local_repo_path("file:///path/to/repo")
    assert path == Path("/path/to/repo")
    
    # Test with user directory
    with patch('os.path.expanduser', return_value="/home/user/expanded"):
        path = compiler.get_local_repo_path("file://~/repo")
        assert path == Path("/home/user/expanded")

@patch('subprocess.run')
@patch('os.path.exists')
def test_setup_remote_repo_new(mock_exists, mock_run, temp_dir):
    """Test setting up a new remote repository."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir,
        repos_dir=temp_dir
    )
    
    repo_url = "https://github.com/test/repo.git"
    expected_repo_dir = Path(temp_dir) / "repo"
    
    # Mock that the repo doesn't exist yet
    mock_exists.return_value = False
    
    # Mock successful git clone
    mock_run.return_value = MagicMock(returncode=0)
    
    result = compiler._setup_remote_repo(repo_url)
    
    assert result == expected_repo_dir
    mock_run.assert_called_once()
    assert "git" in mock_run.call_args[0][0]
    assert "clone" in mock_run.call_args[0][0]

@patch('subprocess.run')
@patch('pathlib.Path.exists')
def test_setup_remote_repo_existing(mock_exists, mock_run, temp_dir):
    """Test updating an existing remote repository."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir,
        repos_dir=temp_dir
    )
    
    repo_url = "https://github.com/test/repo.git"
    expected_repo_dir = Path(temp_dir) / "repo"
    
    # Mock that the repo already exists
    mock_exists.return_value = True
    
    # Mock successful git pull
    mock_run.return_value = MagicMock(returncode=0)
    
    result = compiler._setup_remote_repo(repo_url)
    
    assert result == expected_repo_dir
    mock_run.assert_called_once()
    assert "git" in mock_run.call_args[0][0]
    assert "pull" in mock_run.call_args[0][0]

@patch('subprocess.run')
def test_setup_repositories(mock_run, temp_dir, valid_spec_file):
    """Test setting up all repositories specified in the spec."""
    compiler = NotebookSpecCompiler(
        spec_file=valid_spec_file,
        output_dir=temp_dir,
        repos_dir=temp_dir
    )
    
    # Load and validate the spec first
    compiler.load_spec()
    compiler.validate_spec()
    
    # Mock successful git commands
    mock_run.return_value = MagicMock(returncode=0)
    
    # Mock the _setup_remote_repo method
    with patch.object(compiler, '_setup_remote_repo') as mock_setup:
        mock_setup.side_effect = lambda url: Path(temp_dir) / url.split('/')[-1].replace('.git', '')
        
        result = compiler.setup_repositories()
        
        assert result is True
        assert len(compiler.repos_to_setup) == 2
        assert mock_setup.call_count == 2

def test_extract_root_package():
    """Test extraction of root package names from import statements."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir="dummy"
    )
    
    # Create mock regex match objects
    class MockMatch:
        def __init__(self, groups):
            self._groups = groups
        
        def group(self, index):
            index -= 1
            if index < len(self._groups):
                return self._groups[index]
            return None
    
    # Test "import package"
    match = MockMatch(["pandas", None])
    assert compiler._extract_root_package(match) == "pandas"
    
    # Test "from package import something"
    match = MockMatch([None, "sklearn.model_selection"])
    assert compiler._extract_root_package(match) == "sklearn"
    
    # Test built-in module (should return None)
    match = MockMatch(["os", None])
    assert compiler._extract_root_package(match) is None
    
    # Make sure we handle the case where both groups are None
    # This should not happen in practice with the regex pattern used,
    # but we should test for robustness
    match = MockMatch([None, None])
    assert compiler._extract_root_package(match) is None

@patch('json.load')
def test_extract_imports_from_notebook(mock_json_load, temp_dir):
    """Test extraction of imports from a notebook."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir
    )
    
    notebook = {
        "cells": [
            {
                "cell_type": "code",
                "source": [
                    "import pandas as pd\n",
                    "import numpy as np\n",
                    "from sklearn.model_selection import train_test_split\n",
                    "import os  # built-in module\n",
                    "from __future__ import annotations  # special import\n"
                ]
            },
            {
                "cell_type": "markdown",
                "source": "# This is a markdown cell with no imports"
            },
            {
                "cell_type": "code",
                "source": "import tensorflow as tf"
            }
        ]
    }
    
    imports = compiler._extract_imports_from_notebook(notebook)
    
    assert imports == {"pandas", "numpy", "sklearn", "tensorflow"}
    assert "os" not in imports  # built-in module should be excluded
    assert "__future__" not in imports  # special import should be excluded

def test_process_requirements(temp_dir, sample_requirements_file):
    """Test processing of requirements.txt files."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir
    )
    
    # Set up requirements files
    compiler.requirements_files = [Path(sample_requirements_file)]
    
    result = compiler.process_requirements()
    
    assert result is True
    assert "pandas==1.3.5" in compiler.package_list
    assert "numpy>=1.20.0" in compiler.package_list
    assert "scikit-learn" in compiler.package_list
    assert len(compiler.package_list) == 3  # Comments should be excluded

@patch('subprocess.run')
def test_compile_requirements(mock_run, temp_dir, sample_requirements_file):
    """Test compilation of requirements using uv pip compile."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir,
        compile=True
    )
    
    # Set up requirements files
    compiler.requirements_files = [Path(sample_requirements_file)]
    compiler.image_name = "test-image"
    
    # Mock successful uv pip compile run
    mock_run.return_value = MagicMock(returncode=0)
    
    # Mock the compiled requirements file
    compiled_file = os.path.join(temp_dir, "test-image_compiled_requirements.txt")
    with open(compiled_file, "w") as f:
        f.write("pandas==1.3.5\n")
        f.write("numpy==1.20.3\n")
        f.write("scikit-learn==1.0.2\n")
    
    result = compiler.compile_requirements()
    
    assert result is True
    mock_run.assert_called_once()
    assert "uv" in mock_run.call_args[0][0]
    assert "pip" in mock_run.call_args[0][0]
    assert "compile" in mock_run.call_args[0][0]

@patch('shutil.rmtree')
def test_cleanup_repos(mock_rmtree, temp_dir):
    """Test cleanup of repository directories."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir,
        repos_dir=temp_dir,
        cleanup=True
    )
    
    result = compiler.cleanup_repos()
    
    assert result is True
    mock_rmtree.assert_called_once_with(Path(temp_dir))

def test_generate_environment_specs(temp_dir):
    """Test generation of environment specification files."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir
    )
    
    # Set up package list and image name
    compiler.package_list = {"pandas==1.3.5", "numpy>=1.20.0", "scikit-learn"}
    compiler.image_name = "test-image"
    compiler.python_version = "3.9"
    
    result = compiler.generate_environment_specs()
    
    assert result is True
    
    # Check pip requirements file
    pip_file = Path(temp_dir) / "test-image_requirements.txt"
    assert pip_file.exists()
    with open(pip_file) as f:
        content = f.read()
        assert "pandas==1.3.5" in content
        assert "numpy>=1.20.0" in content
        assert "scikit-learn" in content
    
    # Check conda environment file
    conda_file = Path(temp_dir) / "test-image_environment.yml"
    assert conda_file.exists()
    with open(conda_file) as f:
        content = f.read()
        assert "python=3.9" in content
        assert "pip:" in content

def test_generate_notebook_list(temp_dir):
    """Test generation of notebook list file."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir
    )
    
    # Set up notebook paths and repos
    repo1_path = Path(temp_dir) / "repo1"
    repo2_path = Path(temp_dir) / "repo2"
    os.makedirs(repo1_path, exist_ok=True)
    os.makedirs(repo2_path, exist_ok=True)
    
    compiler.notebook_paths = [
        repo1_path / "notebook1.ipynb",
        repo1_path / "subdir" / "notebook2.ipynb",
        repo2_path / "notebook3.ipynb"
    ]
    
    compiler.repos_to_setup = {
        "https://github.com/test/repo1.git": repo1_path,
        "https://github.com/test/repo2.git": repo2_path
    }
    
    compiler.image_name = "test-image"
    
    # Create the notebook files
    os.makedirs(repo1_path / "subdir", exist_ok=True)
    for nb_path in compiler.notebook_paths:
        with open(nb_path, "w") as f:
            f.write("{}")
    
    result = compiler.generate_notebook_list()
    
    assert result is True
    
    # Check notebook list file
    notebook_list = Path(temp_dir) / "test-image_notebooks.txt"
    assert notebook_list.exists()
    with open(notebook_list) as f:
        content = f.read().splitlines()
        assert len(content) == 3
        assert "notebook1.ipynb" in content[0]
        assert "notebook3.ipynb" in content[1]
        assert "subdir/notebook2.ipynb" in content[2]

@patch('curator.NotebookSpecCompiler.load_spec')
@patch('curator.NotebookSpecCompiler.validate_spec')
@patch('curator.NotebookSpecCompiler.setup_repositories')
@patch('curator.NotebookSpecCompiler.collect_notebook_paths')
@patch('curator.NotebookSpecCompiler.find_requirements_files')
@patch('curator.NotebookSpecCompiler.process_requirements')
@patch('curator.NotebookSpecCompiler.generate_package_list')
@patch('curator.NotebookSpecCompiler.generate_notebook_list')
@patch('curator.NotebookSpecCompiler.process_imports')
def test_main_success(
    mock_process_imports, mock_generate_notebook_list, mock_generate_package_list,
    mock_process_requirements, mock_find_requirements_files, mock_collect_notebook_paths,
    mock_setup_repositories, mock_validate_spec, mock_load_spec, temp_dir
):
    """Test successful run of the compiler."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir
    )
    
    # Configure all mocks to return True
    for mock in [
        mock_load_spec, mock_validate_spec, mock_setup_repositories,
        mock_collect_notebook_paths, mock_find_requirements_files,
        mock_process_requirements, mock_generate_package_list,
        mock_generate_notebook_list, mock_process_imports
    ]:
        mock.return_value = True
    
    result = compiler.main()
    
    assert result is True
    mock_load_spec.assert_called_once()
    mock_validate_spec.assert_called_once()
    mock_setup_repositories.assert_called_once()
    mock_collect_notebook_paths.assert_called_once()
    mock_find_requirements_files.assert_called_once()
    mock_process_requirements.assert_called_once()
    mock_generate_package_list.assert_called_once()
    mock_generate_notebook_list.assert_called_once()
    mock_process_imports.assert_called_once()

@patch('curator.NotebookSpecCompiler.load_spec')
def test_main_failure(mock_load_spec, temp_dir):
    """Test failure handling during run."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir=temp_dir
    )
    
    # Make load_spec fail
    mock_load_spec.return_value = False
    
    result = compiler.main()
    
    assert result is False
    mock_load_spec.assert_called_once()

def test_logging_methods():
    """Test the logging methods."""
    compiler = NotebookSpecCompiler(
        spec_file="dummy.yaml",
        output_dir="dummy"
    )
    
    # Test info method
    with patch.object(compiler.logger, 'info') as mock_info:
        result = compiler.info("Test info message")
        assert result is True
        mock_info.assert_called_once_with("Test info message")
    
    # Test warning method
    with patch.object(compiler.logger, 'warning') as mock_warning:
        result = compiler.warning("Test warning message")
        assert result is True
        mock_warning.assert_called_once_with("Test warning message")
    
    # Test error method
    with patch.object(compiler.logger, 'error') as mock_error:
        result = compiler.error("Test error message")
        assert result is False
        mock_error.assert_called_once_with("Test error message")


