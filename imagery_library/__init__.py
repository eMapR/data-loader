"""Shared imagery library on top of DataLoader: imagery stored by product,
version, grid and tile (one DataLoader dataset per tile), a generated
GeoParquet catalog, and AOI + date-range discovery. See docs/library.md."""
from imagery_library.layout import Library, LibraryError, Product, open_library

__all__ = ["Library", "LibraryError", "Product", "open_library"]
