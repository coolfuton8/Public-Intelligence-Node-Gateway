"""Deterministic screen parsing: windows, controls and their values from a captured frame.

No language or vision model is involved. Layout comes from classic image processing
(OpenCV) and text from Tesseract OCR. The result is a compact text tree a text-only model
can read and act on by element ID.
"""

from .analyze import Analyzer
from .model import Element, Screen, Window, render_text

__all__ = ["Analyzer", "Element", "Screen", "Window", "render_text"]
