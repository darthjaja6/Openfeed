"""Local OpenCLI job service.

This package is intentionally independent from OpenFeed feed/topic logic. It
provides a small localhost queue + worker pool for browser-backed OpenCLI jobs
so multiple projects can share one machine's Chrome/OpenCLI resources.
"""
