"""``porter_web`` — the Gradio-based interactive workbench frontend.

Acts as the shared workbench between human users and AI agents (Human-in-the-Loop).
Exposes an interactive dashboard for URL inspection, parameter tuning, live streaming
progress, subtitle proofreading/review, artifact preview, and system diagnostics.

Design invariants:
* **One engine, fourth frontend.** Reuses the ``porter`` core library exclusively;
  holds no duplicate video processing or translation logic.
* **Lazy loading.** Heavy web frameworks (Gradio, FastAPI) are imported lazily inside
  :func:`porter_web.app.create_app` so that importing the package does not require
  the ``[web]`` extra.
* **Decoupled from Agent Harness.** Keeps heavy rich-media displays (video players,
  full subtitle lists) inside the web UI, sending only concise state updates back to
  the agent session context.
"""

from porter_web.app import create_app, main

__all__ = ["create_app", "main"]
