"""System package-management steps.

Holds :mod:`nexus_steps.package.install`, which hides the platform-native
package manager (Homebrew / apt / Chocolatey) behind one OS-agnostic step so
a single job definition can provision dependencies across a heterogeneous
cluster.

This package is a plain namespace — module registration happens through the
``_STEP_MODULES`` list in :mod:`nexus_steps`.
"""
