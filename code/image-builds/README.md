# nb-curator

## Overview

This is a prototype of a notebook curation tool which enables a curator to
specify a set of notebooks and requirements which will then be used as the basis
for defining a Python environment inputs suitable for running all of them.  In
addition to defining a precise set of package versions to install,  it collects
inputs for
testinging the resulting environment and runners which execute the tests.  The
long term goal of this tool is to provide inputs to build and test Jupyter
notebook Docker images in a CI/CD enabling curators to deploy science platform
notebook images with minimal interaction with platform administrators.

## Basic Flow

The basic flow of the curator is to command different steps of the overall
process to execute or not on a per-run basis.   Eventually this enables
skipping over aspects of the process which have already been successfully
completed and iterating on the current task,  e.g. not constantly recompiling
and re-installing pacackages while iterating over failing notebook tests and
notebook updates.  If any step in the
sequence fails,  the process will exit with an error status.  To that end it has
the following features which are generally gated by CLI switches.  

- Loads, validates, updates, and saves the YAML notebook specification.
  Validation is currently incomplete but checks for required keywords.

- Optionally clones the git repositories for the notebooks if a
  local clone does not already exist,  otherwise it updates the existing clones
  from their repos or does nothing if --clone is not specified.  --repos-dir is
  used to specify the directory where the git repositories are cloned and/or
  already exist.

- Searches for relevant notebooks based on the notebook include/exclude
  patterns.

- Searches for requirements.txt files which specify Python package version
constraints at a granular level of single notebooks.  Exactly what to include
is a WIP,  but at a minimum one optional requirements.txt per notebook.

- Optionally initializes (--init-env) a target environment to support
  compilation of requirements, package installation, and testing.   Creates a
  corresponding JupyterLab kernel.  This can be further extended to setting
  up a complete sandbox environment for the curator.   This is useful even if
  the paradigm is to install/test to the current environment running nb-curator.

- If --compile is specified,  it will create both a conda environment .yml file
and a locked pip requirements.txt file based on compiling all the discovered
requirements.txt simultaneously,  with the goal of creating a package version
spec suitable for running ALL of the notebooks.  If --compile is not specified
it will continue to use the last set of compiled packages from the spec.

- If --install is specified,  it will install the compiled versions of packages in the conda environment,  which XXXXX again at this time is the runtime environment. After installation,  it will attempt to import any package which is explicitlylisted in a notebook file as a basic sanity check.

- If --test-notebooks is specified, run notebooks matching any of the subsequent
comma separated list of notebook names or regular expressions.  If no notebooks
or regexps are specified,  it will run all notebooks.  This is a headless crash
test which runs up to --jobs notebooks in parallel using a --timeout to kill
runaway notebooks.

- If --revise-spec is specified,  saves various products to the "out" section of
  the YAML spec:
      - List of discovered notebooks
      - Combined package vesion requirements
      - List of test imports
      - Basic conda spec .yml file for Python environment
  If --revise-spec is not specified,  it will not modify the input spec.

- If --cleanup is specified,  it will remove all cloned repositories.

- If a proposed/not-implemented --wipe-env is specified,  it will remove the
target environment.   This dedicated environment approach prevents contamination
between iterations of the tool as packages come-and-go from the spec but are
never removed from the target environment.

- Generates a trivial conda environment .yml file based on the Python version
specified by the curator spec.   Currently this .yml output is unused and
package installation and testing occur relative to the curator runtime
environment but in theory this could be used to set up a sandbox target
environment if proposed --create-target-env [name] is specified.

- If optional/proposed --submit-for-build is specified,  the spec is forwarded
  to the CI chain,  key information is supplied to the build framework, and a
  corresponding image is automatically built and pushed to the hub assuming all
  goes well.   This is all still TBD pending interest/approval of the above.
