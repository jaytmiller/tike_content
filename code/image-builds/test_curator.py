#!/usr/bin/env python

import unittest
import os
import sys
import tempfile
import shutil
import subprocess  # Add this import for the first error
from pathlib import Path
from unittest.mock import patch, MagicMock, mock_open
import json
from io import StringIO

# Import the module to test
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from spec_compiler import NotebookSpecCompiler, parse_args


class TestNotebookSpecCompiler(unittest.TestCase):
    """Test cases for the NotebookSpecCompiler class."""

    def setUp(self):
        """Set up test fixtures."""
        self.temp_dir = tempfile.mkdtemp()
        self.output_dir = os.path.join(self.temp_dir, "output")
        os.makedirs(self.output_dir, exist_ok=True)
        
        # Create a sample spec file
        self.spec_file = os.path.join(self.temp_dir, "test_spec.yaml")
        with open(self.spec_file, "w") as f:
            f.write("""
image_spec_header:
  image_name: test-image
  nb_repo: https://github.com/test/repo.git
  python_version: "3.9"
  root_nb_directory: notebooks
  valid_on: 2023-01-01
  expires_on: 2024-01-01

selected_notebooks:
  - directories:
      include_subdirs:
        - examples
      exclude_subdirs:
        - deprecated
""")

    def tearDown(self):
        """Tear down test fixtures."""
        shutil.rmtree(self.temp_dir)

    def test_init(self):
        """Test initialization of the compiler."""
        compiler = NotebookSpecCompiler(
            spec_file=self.spec_file,
            output_dir=self.output_dir,
            verbose=True,
            extract_imports=True
        )
        
        self.assertEqual(compiler.spec_file, self.spec_file)
        self.assertEqual(compiler.output_dir, Path(self.output_dir))
        self.assertTrue(compiler.extract_imports)
        self.assertEqual(compiler.package_list, set())
        self.assertEqual(compiler.notebook_paths, [])

    @patch("spec_compiler.YAML")
    def test_load_spec(self, mock_yaml):
        """Test loading a YAML specification file."""
        # Setup mock
        mock_yaml_instance = MagicMock()
        mock_yaml.return_value = mock_yaml_instance
        mock_yaml_instance.load.return_value = {
            "image_spec_header": {
                "image_name": "test-image",
                "nb_repo": "https://github.com/test/repo.git"
            },
            "selected_notebooks": []
        }
        
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        result = compiler.load_spec()
        
        self.assertTrue(result)
        mock_yaml_instance.load.assert_called_once()
        self.assertEqual(compiler.spec["image_spec_header"]["image_name"], "test-image")

    def test_validate_spec_valid(self):
        """Test validating a valid specification."""
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        compiler.spec = {
            "image_spec_header": {
                "image_name": "test-image",
                "nb_repo": "https://github.com/test/repo.git",
                "python_version": "3.9",
                "root_nb_directory": "notebooks",
                "valid_on": "2023-01-01",
                "expires_on": "2024-01-01"
            },
            "selected_notebooks": []
        }
        
        result = compiler.validate_spec()
        
        self.assertTrue(result)
        self.assertEqual(compiler.image_name, "test-image")
        self.assertEqual(compiler.nb_repo, "https://github.com/test/repo.git")
        self.assertEqual(compiler.python_version, "3.9")
        self.assertEqual(compiler.root_nb_directory, "notebooks")

    def test_validate_spec_missing_fields(self):
        """Test validating a specification with missing required fields."""
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        compiler.spec = {
            "image_spec_header": {
                # Missing image_name
                "nb_repo": "https://github.com/test/repo.git"
            },
            "selected_notebooks": []
        }
        
        result = compiler.validate_spec()
        
        self.assertFalse(result)

    @patch("subprocess.run")
    def test_clone_repository(self, mock_run):
        """Test cloning a repository."""
        mock_run.return_value.returncode = 0
        
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        compiler.nb_repo = "https://github.com/test/repo.git"
        
        result = compiler.clone_repository()
        
        self.assertTrue(result)
        self.assertIsNotNone(compiler.repo_dir)
        mock_run.assert_called_once()

    @patch("subprocess.run")
    def test_clone_repository_failure(self, mock_run):
        """Test handling a failed repository clone."""
        mock_run.side_effect = subprocess.CalledProcessError(1, "git clone", stderr="Error cloning")
        
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        compiler.nb_repo = "https://github.com/test/repo.git"
        
        result = compiler.clone_repository()
        
        self.assertFalse(result)

    def test_collect_notebook_paths(self):
        """Test collecting notebook paths from the repository."""
        # Create a mock repository structure
        repo_dir = os.path.join(self.temp_dir, "repo")
        os.makedirs(os.path.join(repo_dir, "notebooks", "examples"), exist_ok=True)
        os.makedirs(os.path.join(repo_dir, "notebooks", "deprecated"), exist_ok=True)
        
        # Create some test notebooks
        with open(os.path.join(repo_dir, "notebooks", "examples", "test1.ipynb"), "w") as f:
            f.write("{}")
        with open(os.path.join(repo_dir, "notebooks", "examples", "test2.ipynb"), "w") as f:
            f.write("{}")
        with open(os.path.join(repo_dir, "notebooks", "deprecated", "old.ipynb"), "w") as f:
            f.write("{}")
        
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        compiler.repo_dir = repo_dir
        compiler.root_nb_directory = "notebooks"
        compiler.spec = {
            "selected_notebooks": [
                {
                    "directories": {
                        "include_subdirs": ["examples"],
                        "exclude_subdirs": ["deprecated"]
                    }
                }
            ]
        }
        
        result = compiler.collect_notebook_paths()
        
        self.assertTrue(result)
        self.assertEqual(len(compiler.notebook_paths), 2)  # Should find 2 notebooks in examples
        # Verify the deprecated notebook is not included
        for path in compiler.notebook_paths:
            self.assertNotIn("deprecated", str(path))

    @patch("builtins.open", new_callable=mock_open)
    def test_find_requirements_files(self, mock_file):
        """Test finding requirements.txt files."""
        # Create a mock repository structure
        repo_dir = os.path.join(self.temp_dir, "repo")
        notebooks_dir = os.path.join(repo_dir, "notebooks")
        os.makedirs(notebooks_dir, exist_ok=True)
        
        # Create a requirements.txt file
        req_path = os.path.join(notebooks_dir, "requirements.txt")
        with open(req_path, "w") as f:
            f.write("numpy==1.21.0\npandas==1.3.0\n")
        
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        compiler.repo_dir = repo_dir
        
        # Create the notebook file in the same directory as requirements.txt
        notebook_path = Path(os.path.join(notebooks_dir, "test.ipynb"))
        with open(notebook_path, "w") as f:
            f.write("{}")
        
        compiler.notebook_paths = [notebook_path]
        
        result = compiler.find_requirements_files()
        
        self.assertTrue(result)
        self.assertEqual(len(compiler.requirements_files), 1)
        self.assertEqual(str(compiler.requirements_files[0]), req_path)

    def test_process_requirements(self):
        """Test processing requirements.txt files."""
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        
        # Create a temporary requirements file
        req_file = os.path.join(self.temp_dir, "requirements.txt")
        with open(req_file, "w") as f:
            f.write("numpy==1.21.0\n# Comment line\npandas==1.3.0\n")
        
        compiler.requirements_files = [Path(req_file)]
        
        result = compiler.process_requirements()
        
        self.assertTrue(result)
        self.assertEqual(len(compiler.package_list), 2)
        self.assertIn("numpy==1.21.0", compiler.package_list)
        self.assertIn("pandas==1.3.0", compiler.package_list)

    @patch("spec_compiler.YAML")
    def test_generate_environment_specs(self, mock_yaml):
        """Test generating environment specifications."""
        mock_yaml_instance = MagicMock()
        mock_yaml.return_value = mock_yaml_instance
        
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        compiler.image_name = "test-image"
        compiler.python_version = "3.9"
        compiler.package_list = {"numpy==1.21.0", "pandas==1.3.0"}
        
        result = compiler.generate_environment_specs()
        
        self.assertTrue(result)
        # Check if files were created
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "test-image_requirements.txt")))
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "test-image_environment.yml")))
        mock_yaml_instance.dump.assert_called_once()

    def test_generate_notebook_list(self):
        """Test generating a list of notebooks."""
        compiler = NotebookSpecCompiler(spec_file=self.spec_file, output_dir=self.output_dir)
        compiler.image_name = "test-image"
        compiler.repo_dir = os.path.join(self.temp_dir, "repo")
        compiler.notebook_paths = [
            Path(os.path.join(compiler.repo_dir, "notebooks", "test1.ipynb")),
            Path(os.path.join(compiler.repo_dir, "notebooks", "test2.ipynb"))
        ]
        
        result = compiler.generate_notebook_list()
        
        self.assertTrue(result)
        self.assertTrue(os.path.exists(os.path.join(self.output_dir, "test-image_notebooks.txt")))

    @patch("json.load")
    def test_extract_notebook_imports(self, mock_json_load):
        """Test extracting imports from notebooks."""
        # Mock a notebook with import statements
        mock_json_load.return_value = {
            "cells": [
                {
                    "cell_type": "code",
                    "source": [
                        "import numpy as np\n",
                        "from pandas import DataFrame\n",
                        "import os  # built-in module\n"
                    ]
                }
            ]
        }
        
        compiler = NotebookSpecCompiler(
            spec_file=self.spec_file, 
            output_dir=self.output_dir,
            extract_imports=True
        )
        compiler.notebook_paths = [Path(os.path.join(self.temp_dir, "test.ipynb"))]
        
        # Create the notebook file
        with open(os.path.join(self.temp_dir, "test.ipynb"), "w") as f:
            f.write("{}")
        
        result = compiler.extract_notebook_imports()
        
        self.assertTrue(result)
        extract_dir = os.path.join(self.output_dir, "extracted")
        self.assertTrue(os.path.exists(extract_dir))
        self.assertTrue(os.path.exists(os.path.join(extract_dir, "imports-test.pip")))

    @patch("sys.argv", ["spec_compiler.py", "test_spec.yaml", "--verbose", "--extract-imports"])
    def test_parse_args(self):
        """Test parsing command line arguments."""
        args = parse_args()
        
        self.assertEqual(args.spec_file, "test_spec.yaml")
        self.assertEqual(args.output_dir, "./output")
        self.assertTrue(args.verbose)
        self.assertTrue(args.extract_imports)

    @patch.object(NotebookSpecCompiler, "run")
    @patch("sys.exit")
    @patch("spec_compiler.parse_args")
    def test_main_success(self, mock_parse_args, mock_exit, mock_run):
        """Test the main function with successful execution."""
        mock_args = MagicMock()
        mock_args.spec_file = self.spec_file
        mock_args.output_dir = self.output_dir
        mock_args.verbose = False
        mock_args.extract_imports = False
        mock_parse_args.return_value = mock_args
        
        mock_run.return_value = True
        
        from spec_compiler import main
        main()
        
        mock_exit.assert_called_once_with(0)

    @patch.object(NotebookSpecCompiler, "run")
    @patch("sys.exit")
    @patch("spec_compiler.parse_args")
    def test_main_failure(self, mock_parse_args, mock_exit, mock_run):
        """Test the main function with failed execution."""
        mock_args = MagicMock()
        mock_args.spec_file = self.spec_file
        mock_args.output_dir = self.output_dir
        mock_args.verbose = False
        mock_args.extract_imports = False
        mock_parse_args.return_value = mock_args
        
        mock_run.return_value = False
        
        from spec_compiler import main
        main()
        
        mock_exit.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()