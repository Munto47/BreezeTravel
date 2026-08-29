"""Evaluation packages.

Importing this namespace must remain side-effect free because authority tools
load governed evaluator subpackages before any external secret is consulted.
Import concrete runners from their modules explicitly.
"""

__all__: list[str] = []
