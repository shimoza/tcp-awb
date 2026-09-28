"""Modules for one platform, T Cloud Public (TCP): its price API and its cloud API.

The neutral core of the Workbench never imports this package (T-88), so that a second platform never carries it.
Only the command dispatcher names these modules, by their module path, and loads them on use.
"""
