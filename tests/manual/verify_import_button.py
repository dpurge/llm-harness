"""Real-browser check of the State tab's `Import state` button (manual, not pytest).

Run from the repo root:
    uv run --with playwright python tests/manual/verify_import_button.py

It starts the real page on a free loopback port with a scripted fake provider
(no user config, no network), drives headless Chromium with a REAL click and
prints whether the native file chooser opened and whether the import completed.
Exit code 0 only when both happened.
"""

import datetime as dt
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "src")]


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def serve(port: int) -> None:
    from nicegui import ui

    from llm_harness.gui.app import register_page
    from tests.fake_steps import reply_events
    from tests.fakes import router_factory_for
    from tests.test_gui import CONFIG, MODEL_1, StepFactory

    factory = StepFactory([reply_events({"type": "respond", "text": "hi"})])
    register_page(CONFIG, MODEL_1, router_factory_for(CONFIG, factory), False)
    ui.run(host="127.0.0.1", port=port, reload=False, show=False)


def exported_yaml() -> bytes:
    from llm_harness.state import Environment, OrchestratorState, RunState
    from llm_harness.state_io import export_yaml

    now = dt.datetime.now(dt.UTC)
    state = OrchestratorState(
        environment=Environment(now=now, user_name="u", computer_name="c"),
        goal="imported by the browser check",
    )
    return export_yaml(RunState(state=state, pending_question=None))


# Records, at the moment the hidden file input is clicked, whether the browser
# still counts the user's click as active and whether we are inside the click
# event itself. A file dialog needs that activation; a headless Playwright run
# opens its chooser even without it, so this is the stronger evidence.
RECORD_PICKER_CALL = """
window.__pickerCalls = [];
const nativeClick = HTMLInputElement.prototype.click;
HTMLInputElement.prototype.click = function (...args) {
  if (this.type === 'file') {
    window.__pickerCalls.push({
      activation: navigator.userActivation.isActive,
      insideEvent: window.event ? window.event.type : null,
    });
  }
  return nativeClick.apply(this, args);
};
"""


def drive(port: int) -> bool:
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            page = browser.new_page()
            page.add_init_script(RECORD_PICKER_CALL)
            page.goto(f"http://127.0.0.1:{port}/", wait_until="networkidle")
            page.get_by_role("tab", name="State").click()
            button = page.get_by_role("button", name="Import state")
            chooser_opened = False
            try:
                with page.expect_file_chooser(timeout=3000) as chooser:
                    button.click()
                chooser_opened = True
                print(
                    "file chooser event: FIRED, multiple =", chooser.value.is_multiple()
                )
            except PlaywrightTimeout:
                print("file chooser event: DID NOT FIRE within 3 s")
            calls = page.evaluate("window.__pickerCalls")
            print("file input clicked with:", calls)
            if not calls or calls[0] != {"activation": True, "insideEvent": "click"}:
                print("WARNING: picker not opened inside the user's click")
                chooser_opened = False
            page.locator(".q-uploader input[type=file]").set_input_files(
                {"name": "s.yaml", "mimeType": "text/yaml", "buffer": exported_yaml()}
            )
            try:
                page.get_by_text("state loaded").wait_for(timeout=5000)
                page.get_by_text("Goal: imported by the browser check").wait_for(
                    timeout=3000
                )
                print("import: state loaded, goal shown in the State tab")
                imported = True
            except PlaywrightTimeout:
                print(
                    "import: NOT completed; page text:", page.inner_text("body")[:300]
                )
                imported = False
            return chooser_opened and imported
        finally:
            browser.close()


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--serve":
        serve(int(sys.argv[2]))
        return 0
    port = free_port()
    server = subprocess.Popen(
        [sys.executable, __file__, "--serve", str(port)],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            with socket.socket() as probe:
                if probe.connect_ex(("127.0.0.1", port)) == 0:
                    break
            __import__("time").sleep(0.1)
        ok = drive(port)
    finally:
        server.terminate()
        server.wait(timeout=10)
    print("RESULT:", "ok" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
