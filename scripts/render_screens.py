"""Render real screens to PNG for visual inspection (offscreen, no display needed).

    DEMO_USER=demo.ceo DEMO_PASS=... python scripts/render_screens.py OUTDIR

Uses the live backend (COSMETICS_API_URL, default http://127.0.0.1:8000): nothing is mocked.
"""
import os
import sys
import time
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

from desktop.api.client import ApiClient  # noqa: E402
from desktop.main import MainWindow, build_app  # noqa: E402
from desktop.session import Session  # noqa: E402
from desktop.ui.shell import Shell  # noqa: E402


def pump(app: QApplication, seconds: float = 1.2) -> None:
    end = time.time() + seconds
    while time.time() < end:
        app.processEvents()
        time.sleep(0.01)


def main() -> None:
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    user, pw = os.environ["DEMO_USER"], os.environ["DEMO_PASS"]
    app = build_app()
    client = ApiClient()
    client.login(user, pw)
    session = Session(client)
    session.load_me()

    win = MainWindow(client)
    for w, h in ((1366, 768), (1920, 1080)):
        win.resize(w, h)
        win.show()
        win.login.username.setText(user)
        pump(app, 0.3)
        if w == 1366:
            win.grab().save(str(out / f"login_{w}.png"))
        shell = Shell(session)
        win.stack.addWidget(shell)
        win.stack.setCurrentWidget(shell)
        win.resize(w, h)
        pump(app, 0.3)

        shots = [("overview", None)]
        if session.can("users.view"):
            shots += [("settings", 0), ("settings", 1), ("settings", 2), ("settings", 3)]
        shots += [("inventory", None)]
        for page, tab in shots:
            shell.show_page(page)
            if tab is not None:
                st = shell.pages["settings"]
                if tab < st.tabs.count():
                    st.tabs.setCurrentIndex(tab)
                    st.refresh()
            pump(app, 1.5)
            name = f"{page}{'' if tab is None else '_tab' + str(tab)}_{w}.png"
            win.grab().save(str(out / name))
        win.stack.removeWidget(shell)
        shell.deleteLater()
    print("rendered:", sorted(p.name for p in out.glob("*.png")))


if __name__ == "__main__":
    main()
