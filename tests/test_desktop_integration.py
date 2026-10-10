from types import SimpleNamespace

from lanmouse import desktop


def fake_kde(monkeypatch, initial):
    monkeypatch.setattr(desktop.sys, "platform", "linux")
    monkeypatch.setenv("WAYLAND_DISPLAY", "isolated-test")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "KDE")
    monkeypatch.setattr(desktop.shutil, "which", lambda command: command)
    values = dict(initial)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        if command[0] == "qdbus-qt6":
            return SimpleNamespace(stdout="")
        group = command[command.index("--group") + 1]
        key = command[command.index("--key") + 1]
        if command[0] == "kreadconfig6":
            return SimpleNamespace(stdout=values.get((group, key), ""))
        values[group, key] = command[-1]
        return SimpleNamespace(stdout="")

    monkeypatch.setattr(desktop.subprocess, "run", run)
    return values, calls


def test_kde_rule_preserves_other_applications_and_is_idempotent(monkeypatch):
    initial = {
        ("General", "rules"): "existing-one,existing-two",
        ("existing-one", "wmclass"): "another-app",
    }
    values, calls = fake_kde(monkeypatch, initial)
    assert desktop.configure_kde_taskbar(True)
    assert values["General", "rules"] == f"existing-one,existing-two,{desktop.RULE_ID}"
    assert values["existing-one", "wmclass"] == "another-app"
    assert values[desktop.RULE_ID, "wmclass"] == desktop.APP_ID
    assert values[desktop.RULE_ID, "wmclassmatch"] == "1"
    assert values[desktop.RULE_ID, "skiptaskbarrule"] == "2"
    assert values[desktop.RULE_ID, "skiptaskbar"] == "true"
    calls.clear()
    assert desktop.configure_kde_taskbar(True)
    assert all(command[0] == "kreadconfig6" for command in calls)


def test_kde_without_tray_restores_taskbar_access(monkeypatch):
    values, calls = fake_kde(monkeypatch, {})
    assert desktop.configure_kde_taskbar(True)
    assert desktop.configure_kde_taskbar(False)
    assert values[desktop.RULE_ID, "skiptaskbarrule"] == "1"
    assert values[desktop.RULE_ID, "skiptaskbar"] == "false"
    assert values[desktop.RULE_ID, "skipswitcherrule"] == "1"


def test_non_kde_does_not_change_desktop_configuration(monkeypatch):
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "GNOME")
    monkeypatch.setattr(
        desktop.subprocess,
        "run",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Unexpected desktop command")),
    )
    assert not desktop.configure_kde_taskbar(True)
