"""Authorization layer: risk grading of actions, and the confirmation policy over it.

Kept separate from `workflow.validation`, which decides whether a *plan* is well-formed.
This package answers a different question — whether the action about to run may proceed
without asking the user.
"""
