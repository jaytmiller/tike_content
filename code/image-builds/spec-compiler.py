#!/usr/bin/env python3

import argparse
import os
import sys
import yaml
import subprocess
import tempfile
import logging
from pathlib import Path
from datetime import datetime

# Set up logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger('spec-compiler')

def parse_args():
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description='Process TIKE image specification YAML and prepare notebook environment'
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

def load_yaml_spec(spec_file):
    """Load and validate the YAML specification file."""
    try:
        with open(spec_file, 'r') as f:
            spec = yaml.safe_load(f)
        logger.info(f"Successfully loaded spec from {spec_file}")
        return spec
    except Exception as e:
        logger.error(f"Failed to load YAML spec: {e}")
        sys.exit(1)

def validate_spec(spec):
    """Perform basic validation on the specification."""
    # Check for required fields
    required_fields = ['image_spec_header']
    
    for field in required_fields:
        if field not in spec:
            logger.error(f"Missing required field: {field}")
            return False
    
    # Extract header information
    header = spec['image_spec_header']
    
    # Check for image name
    image_name = next((item['image_name'] for item in header if 'image_name' in item), None)
    if not image_name:
        logger.error("Missing image_name in image_spec_header")
        return False
    
    # Check for notebook repository
    nb_repo = next((item['nb_repo'] for item in header if 'nb_repo' in item), None)
    if not nb_repo:
        logger.error("Missing nb_repo in image_spec_header")
        return False
    
    logger.info(f"Spec validation passed for image: {image_name}")
    return True

def clone_repository(spec):
    """Clone the notebook repository specified in the spec."""
    header = spec['image_spec_header']
    nb_repo = next((item['nb_repo'] for item in header if 'nb_repo' in item), None)
    
    if not nb_repo:
        logger.error("No notebook repository specified in spec")
        return None
    
    # Create a temporary directory for the clone
    repo_dir = tempfile.mkdtemp(prefix="tike-repo-")
    logger.info(f"Cloning repository {nb_repo} to {repo_dir}")
    
    try:
        subprocess.run(
            ["git", "clone", nb_repo, repo_dir],
            check=True,
            capture_output=True,
            text=True
        )
        logger.info(f"Successfully cloned repository to {repo_dir}")
        return repo_dir
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to clone repository: {e.stderr}")
        return None

def get_notebook_paths(spec, repo_dir):
    """Get paths to notebooks based on the spec and cloned repository."""
    header = spec['image_spec_header']
    root_nb_dir = next((item['root_nb_directory'] for item in header if 'root_nb_directory' in item), "")
    
    notebook_paths = []
    
    # Process selected_notebooks section
    if 'selected_notebooks' in spec:
        for entry in spec['selected_notebooks']:
            if 'directories' in entry:
                dirs_config = entry['directories']
                
                # Get the root directory for this entry
                entry_root = next((item['root_nb_directory'] for item in dirs_config 
                                  if 'root_nb_directory' in item), root_nb_dir)
                
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
                
                # TODO: Implement actual notebook path collection
    
    return notebook_paths

def main():
    args = parse_args()
    
    if args.verbose:
        logger.setLevel(logging.DEBUG)
    
    # Ensure output directory exists
    os.makedirs(args.output_dir, exist_ok=True)
    
    # Load and validate the spec
    spec = load_yaml_spec(args.spec_file)
    if not validate_spec(spec):
        logger.error("Specification validation failed")
        sys.exit(1)
    
    # Clone the repository
    repo_dir = clone_repository(spec)
    if not repo_dir:
        logger.error("Failed to clone repository")
        sys.exit(1)
    
    # Get notebook paths based on the spec
    notebook_paths = get_notebook_paths(spec, repo_dir)
    
    # TODO: Process requirements.txt files from the notebooks
    
    # TODO: Generate comprehensive package list
    
    # TODO: Create conda/pip environment specifications
    
    logger.info("Spec compilation completed successfully")

if __name__ == "__main__":
    main()