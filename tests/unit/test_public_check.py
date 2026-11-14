import tarfile
import zipfile

import pytest

from strata_python import public_check

# Built at run time, so this file itself stays clean when the check scans the sdist that contains it.
AT = "@"
EMAIL = "ada.lovelace" + AT + "example.org"
HOME = "/home/" + "grace/projects"
DOT = "."


def kinds(findings):
    return [f.kind for f in findings]


def test_clean_text_has_no_findings():
    text = "Placeholders are fine: /home/ada/myapp, C:\\Users\\user, bot 41898282+bot" + AT + "users.noreply.github.com"
    text += ", Co-Authored-By: noreply" + AT + "anthropic.com"
    assert list(public_check.scan_text("README.md", text)) == []


@pytest.mark.parametrize(
    ("text", "kind"),
    [
        (f"contact {EMAIL}", "email address"),
        ("alice" + AT + "anthropic.com", "email address"),
        ("noreply" + AT + "github.com", "email address"),
        (f"server at 192{DOT}168{DOT}1{DOT}20", "private IPv4 address"),
        (f"listen on 10{DOT}0{DOT}0{DOT}5:8000", "private IPv4 address"),
        (f"bridge 172{DOT}20{DOT}3{DOT}4", "private IPv4 address"),
        (f"data in {HOME}", "home folder path"),
        ("D:\\" + "Users\\grace\\Videos", "home folder path"),
    ],
)
def test_built_in_patterns(text, kind):
    (finding,) = public_check.scan_text("a.py", f"ok\n{text}\n")
    assert (finding.where, finding.line, finding.kind) == ("a.py", 2, kind)


def test_public_addresses_and_versions_are_not_private():
    assert list(public_check.scan_text("a.py", "8.8.8.8, version 10.2, 172.32.0.1")) == []


def test_owner_patterns_come_from_the_environment_and_are_not_printed(monkeypatch):
    monkeypatch.setenv(public_check.DENY_ENV, "\nkitchen-?box\n\n")
    patterns = public_check.deny_patterns()
    (finding,) = public_check.scan_text("a.md", "runs on Kitchen-Box", patterns)
    assert finding.text == "(hidden)"
    assert "Kitchen" not in str(finding)
    assert public_check.deny_patterns("") == []


def test_scans_folders_sdists_and_wheels(tmp_path):
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "mod.py").write_text(f'OWNER = "{EMAIL}"\n')
    (tree / "logo.png").write_bytes(b"\x89PNG " + EMAIL.encode())  # binaries are skipped
    (tree / ".venv").mkdir()
    (tree / ".venv" / "x.py").write_text(EMAIL)  # so are build and tool folders
    sdist = tmp_path / "pkg-1.0.tar.gz"
    with tarfile.open(sdist, "w:gz") as archive:
        archive.add(tree / "src" / "mod.py", arcname="pkg-1.0/src/mod.py")
        folder = tarfile.TarInfo("pkg-1.0/src")
        folder.type = tarfile.DIRTYPE
        archive.addfile(folder)
    wheel = tmp_path / "pkg-1.0-py3-none-any.whl"
    with zipfile.ZipFile(wheel, "w") as archive:
        archive.writestr("pkg/", "")
        archive.writestr("pkg/notes.md", f"see {HOME}\n")
    single = tmp_path / "notes.txt"
    single.write_text("nothing here\n")

    found = public_check.scan([tree, sdist, wheel, single])
    assert [(f.where, f.kind) for f in found] == [
        ("src/mod.py", "email address"),
        ("pkg-1.0.tar.gz/pkg-1.0/src/mod.py", "email address"),
        ("pkg-1.0-py3-none-any.whl/pkg/notes.md", "home folder path"),
    ]


def test_main_exit_code_and_report(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv(public_check.DENY_ENV, raising=False)
    (tmp_path / "ok.md").write_text("clean\n")
    assert public_check.main([str(tmp_path)]) == 0
    (tmp_path / "bad.md").write_text(f"{EMAIL}\n")
    assert public_check.main([str(tmp_path)]) == 1
    err = capsys.readouterr().err
    assert "bad.md:1: email address" in err
    assert "1 personal or machine-specific detail(s)" in err
