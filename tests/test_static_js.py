"""The page's own JavaScript has to parse.

There is no build step - app.js is served to the browser exactly as it
sits in the tree - so a syntax error is invisible until a person loads
the page, and then nothing works at all. It has happened: tidying a
nested ternary dropped a ": refresh" line, app.js stopped parsing, and
the browser went on running the last copy it had managed to load, so the
UI looked fine while every button did whatever the previous version did.

Nothing here checks behaviour. It only asserts the files are parseable,
which is the failure the tests could not see.
"""

import shutil
import subprocess
from pathlib import Path

import pytest

STATIC = Path(__file__).parent.parent / "beetdrop" / "static"
SCRIPTS = sorted(STATIC.glob("*.js"))


def have_node():
    return shutil.which("node") is not None


@pytest.mark.skipif(not have_node(), reason="node unavailable")
@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.name)
def test_the_script_parses(script):
    result = subprocess.run(["node", "--check", str(script)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, "%s does not parse:\n%s" % (
        script.name, result.stderr)


def test_the_scripts_are_actually_being_checked():
    # A glob that quietly matches nothing would make the check above pass
    # for every file it was written to guard.
    assert [p.name for p in SCRIPTS], "no scripts found to check"
    assert "app.js" in [p.name for p in SCRIPTS]
