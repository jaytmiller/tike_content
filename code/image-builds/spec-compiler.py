#!/usr/bin/env python

import argparse
import os
import sys
import subprocess
import tempfile
import logging
import shutil
import json
import re
import traceback
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Any, Set
import yaml  # type: ignore

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
        self,
        spec_file: str,
        output_dir: str = "./output",
        verbose: bool = False,
        extract_imports: bool = False,
    ):
        """
        Initialize the NotebookSpecCompiler with a specification file and output directory.

        Args:
            spec_file: Path to the YAML specification file
            output_dir: Directory to store output files
            verbose: Enable verbose logging
            extract_imports: Extract import statements from notebooks
        """
        self.spec_file = spec_file
        self.output_dir = Path(output_dir)
        self.extract_imports = extract_imports

        if verbose:
            logger.setLevel(logging.DEBUG)

        # Initialize state variables
        self.spec: Dict[str, Any] = {}
        self.repo_dir: Optional[str] = None
        self.notebook_paths: List[Path] = []
        self.requirements_files: List[Path] = []
        self.package_list: Set[str] = set()
        self.image_name: Optional[str] = None
        self.nb_repo: Optional[str] = None
        self.python_version: Optional[str] = None
        self.root_nb_directory: str = ""
        self.valid_on: Optional[str] = None
        self.expires_on: Optional[str] = None
        # Create output directory
        os.makedirs(self.output_dir, exist_ok=True)