"""Bitcoin Argus — generate self-contained Docker Compose stacks for Bitcoin testnets.

The package reads a single ``config.yaml``, validates it, allocates non-conflicting
host ports, and renders one isolated Docker Compose project per enabled network.
"""

__version__ = "0.1.0"
