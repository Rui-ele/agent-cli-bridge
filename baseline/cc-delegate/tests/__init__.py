from pathlib import Path
import tempfile


def temp_workspace(case):
    # Keep all writes inside the checkout, including when the outer sandbox excludes
    # system temp. Seatbelt also allows system temp independently of the workspace.
    parent = Path(__file__).resolve().parents[1] / ".test-tmp"
    parent.mkdir(exist_ok=True)
    temp = tempfile.TemporaryDirectory(dir=parent)
    case.addCleanup(temp.cleanup)
    return Path(temp.name)
