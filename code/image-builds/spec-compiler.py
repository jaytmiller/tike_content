#! env python

import argparse
import os
import sys
import subprocess
import tempfile
import logging
import shutil
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Optional, Any, Set
import yaml # type: ignore

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('spec-compiler')

class NotebookSpecCompiler:
    """
    Class to process notebook image specifications, clone repositories,
    and generate environment specifications.
    """
    
    def __init__(self, spec_file: str, output_dir: str = './output', verbose: bool = False):
        """
        Initialize the NotebookSpecCompiler with a specification file and output directory.
        
        Args:
            spec_file: Path to the YAML specification file
            output_dir: Directory to store output files
            verbose: Enable verbose logging
        """
        self.spec_file = spec_file
        self.output_dir = Path(output_dir)
        
        if verbose:
            logger.setLevel(logging.DEBUG)
            
        # Initialize state variables
        self.spec: Dict[str, Any] = {}
        self.repo_dir: Optional[str] = None
        self.notebook_paths: List[Path] = []
        self.requirements_files: List[Path] = []
        self.package_list: Set[str] = set()
        
        # Create output directory
        os.makedirs(self.output_dir, exist_ok=True)
        
    def load_spec(self) -> bool:
        """
        Load and validate the YAML specification file.
        
        Returns:
            bool: True if loading was successful, False otherwise
        """
        try:
            with open(self.spec_file, 'r') as f:
                self.spec = yaml.safe_load(f)
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
        required_fields = ['image_spec_header']
        
        for field in required_fields:
            if field not in self.spec:
                logger.error(f"Missing required field: {field}")
                return False
        
        # Extract header information
        header = self.spec['image_spec_header']
        
        # Check for image name
        self.image_name = next((item['image_name'] for item in header if 'image_name' in item), None)
        if not self.image_name:
            logger.error("Missing image_name in image_spec_header")
            return False
        
        # Check for notebook repository
        self.nb_repo = next((item['nb_repo'] for item in header if 'nb_repo' in item), None)
        if not self.nb_repo:
            logger.error("Missing nb_repo in image_spec_header")
            return False
        
        # Get Python version
        self.python_version = next((item['python_version'] for item in header if 'python_version' in item), None)
        if not self.python_version:
            logger.warning("No Python version specified, will use default")
        
        # Get root notebook directory
        self.root_nb_directory = next((item['root_nb_directory'] for item in header if 'root_nb_directory' in item), "")
        
        # Get validity dates
        self.valid_on = next((item['valid_on'] for item in header if 'valid_on' in item), None)
        self.expires_on = next((item['expires_on'] for item in header if 'expires_on' in item), None)
        
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
                text=True
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
        if not self.repo_dir:
            logger.error("Repository not cloned, cannot collect notebook paths")
            return False
        
        self.notebook_paths = []
        
        # Process selected_notebooks section
        if 'selected_notebooks' not in self.spec:
            logger.error("No selected_notebooks section in spec")
            return False
        
        repo_path = Path(self.repo_dir)
        
        for entry in self.spec['selected_notebooks']:
            if 'directories' in entry:
                dirs_config = entry['directories']
                
                # Get the root directory for this entry
                entry_root = next((item['root_nb_directory'] for item in dirs_config 
                                  if 'root_nb_directory' in item), self.root_nb_directory)
                
                # Get include and exclude directories
                include_dirs = []
                exclude_dirs = []
                
                for item in dirs_config:
                    if 'include_subdirs' in item:
                        include_dirs.extend(item['include_subdirs'])
                    if 'exclude_subdirs' in item:
                        exclude_dirs.extend(item['exclude_subdirs'])
                
                # Default to current directory if no includes specified
                if not include_dirs:
                    include_dirs = ['.']
                
                logger.info(f"Processing directories: include={include_dirs}, exclude={exclude_dirs}")
                
                # Process each include directory
                for include_dir in include_dirs:
                    # Construct the full path
                    dir_path = repo_path / entry_root / include_dir
                    
                    if not dir_path.exists():
                        logger.warning(f"Directory does not exist: {dir_path}")
                        continue
                    
                    # Find all notebooks in this directory and subdirectories
                    for nb_path in dir_path.glob('**/*.ipynb'):
                        # Check if this notebook is in an excluded directory
                        is_excluded = False
                        for exclude_dir in exclude_dirs:
                            exclude_path = repo_path / entry_root / exclude_dir
                            if str(nb_path).startswith(str(exclude_path)):
                                is_excluded = True
                                break
                        
                        if not is_excluded:
                            self.notebook_paths.append(nb_path)
                            logger.debug(f"Added notebook: {nb_path}")
        
        logger.info(f"Collected {len(self.notebook_paths)} notebooks")
        return True
    
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
            req_file = dir_path / 'requirements.txt'
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
                with open(req_file, 'r') as f:
                    for line in f:
                        # Basic processing - could be enhanced with proper requirement parsing
                        line = line.strip()
                        if line and not line.startswith('#'):
                            self.package_list.add(line)
                            logger.debug(f"Added package requirement: {line}")
            except Exception as e:
                logger.error(f"Error processing requirements file {req_file}: {e}")
                return False
        
        logger.info(f"Processed requirements into {len(self.package_list)} unique packages")
        return True
    
    def generate_environment_specs(self) -> bool:
        """
        Generate conda and pip environment specifications.
        
        Returns:
            bool: True if generation was successful, False otherwise
        """
        # Generate pip requirements file
        pip_requirements = self.output_dir / f"{self.image_name.replace(' ', '_')}_requirements.txt"
        try:
            with open(pip_requirements, 'w') as f:
                for package in sorted(self.package_list):
                    f.write(f"{package}\n")
            logger.info(f"Generated pip requirements file: {pip_requirements}")
        except Exception as e:
            logger.error(f"Error generating pip requirements file: {e}")
            return False
        
        # Generate a simple conda environment YAML
        conda_env = self.output_dir / f"{self.image_name.replace(' ', '_')}_environment.yml"
        try:
            with open(conda_env, 'w') as f:
                f.write(f"name: {self.image_name.replace(' ', '_').lower()}\n")
                f.write("channels:\n")
                f.write("  - conda-forge\n")
                f.write("  - defaults\n")
                f.write("dependencies:\n")
                if self.python_version:
                    f.write(f"  - python={self.python_version}\n")
                else:
                    f.write("  - python\n")
                f.write("  - pip\n")
                f.write("  - pip:\n")
                for package in sorted(self.package_list):
                    f.write(f"    - {package}\n")
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
        notebook_list = self.output_dir / f"{self.image_name.replace(' ', '_')}_notebooks.txt"
        try:
            with open(notebook_list, 'w') as f:
                for nb_path in sorted(self.notebook_paths):
                    # Get path relative to repo root
                    rel_path = nb_path.relative_to(self.repo_dir)
                    f.write(f"{rel_path}\n")
            logger.info(f"Generated notebook list: {notebook_list}")
        except Exception as e:
            logger.error(f"Error generating notebook list: {e}")
            return False
        
        return True
    
    def cleanup(self) -> None:
        """Clean up temporary files and directories."""
        if self.repo_dir and os.path.exists(self.repo_dir):
            logger.info(f"Cleaning up repository directory: {self.repo_dir}")
            shutil.rmtree(self.repo_dir)
    
    def run(self) -> bool:
        """
        Run the full compilation process.
        
        Returns:
            bool: True if compilation was successful, False otherwise
        """
        try:
            # Load and validate the spec
            if not self.load_spec() or not self.validate_spec():
                logger.error("Specification loading or validation failed")
                return False
            
            # Clone the repository
            if not self.clone_repository():
                logger.error("Failed to clone repository")
                return False
            
            # Collect notebook paths
            if not self.collect_notebook_paths():
                logger.error("Failed to collect notebook paths")
                return False
            
            # Find requirements files
            if not self.find_requirements_files():
                logger.error("Failed to find requirements files")
                return False
            
            # Process requirements
            if not self.process_requirements():
                logger.error("Failed to process requirements")
                return False
            
            # Generate environment specifications
            if not self.generate_environment_specs():
                logger.error("Failed to generate environment specifications")
                return False
            
            # Generate notebook list
            if not self.generate_notebook_list():
                logger.error("Failed to generate notebook list")
                return False
            
            logger.info("Spec compilation completed successfully")
            return True
            
        except Exception as e:
            logger.error(f"Unexpected error during compilation: {e}")
            return False
        finally:
            self.cleanup()


def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Process notebook image specification YAML and prepare notebook environment'
    )
    parser.add_argument(
        'spec_file', 
        type=str, 
        help='Path to the YAML specification file'
    )
    parser.add_argument(
        '--output-dir', 
        type=str, 
        default='./output',
        help='Directory to store output files'
    )
    parser.add_argument(
        '--verbose', 
        '-v', 
        action='store_true',
        help='Enable verbose output'
    )
    return parser.parse_args()


def main():
    args = parse_args()
    
    # Create and run the compiler
    compiler = NotebookSpecCompiler(
        spec_file=args.spec_file,
        output_dir=args.output_dir,
        verbose=args.verbose
    )
    
    success = compiler.run()
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()