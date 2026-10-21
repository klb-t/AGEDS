"""End-to-end acceptance of the whole increment (AGEDS-TASK-20261016-Z).

``layout`` composes the synthetic corpus parts into one multi-device layout;
``pipeline`` drives the real CLI (subprocesses, fresh data directory) and the
real HTTP service through every stage and returns a receipt that maps each
acceptance criterion to measured evidence. Nothing here is committed data:
everything is generated at run time outside the repository.
"""
