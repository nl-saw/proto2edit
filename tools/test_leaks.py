"""Proofs for the publish gate: it must read every replayed commit, and this tree must be clean."""

import os
import shutil
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import leakcheck as LC

FAILED = []


def ck(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"  {detail}" if detail and not ok else ""))
    if not ok:
        FAILED.append(name)


def run(cwd, *args):
    p = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True)
    return p.returncode, p.stdout.strip()


def make_repo(root):
    os.makedirs(root, exist_ok=True)
    run(root, "init", "-q")
    run(root, "config", "user.email", "t@example.invalid")
    run(root, "config", "user.name", "test")
    # a file the gate refuses by name, added in the first commit and deleted in the second
    with open(os.path.join(root, "session.in"), "wb") as f:
        f.write(b"\x7f\x00\x00\x10" + b"x" * 64)
    run(root, "add", "-A")
    run(root, "commit", "-qm", "add a capture")
    run(root, "rm", "-q", "session.in")
    run(root, "commit", "-qm", "remove it again")


def main():
    # 1. a path added and deleted inside the range must still be seen
    tmp = os.path.join(LC.ROOT, "tmp", "test-leaks-repo")
    shutil.rmtree(tmp, ignore_errors=True)
    make_repo(tmp)
    saved = LC.ROOT
    try:
        LC.ROOT = tmp
        # rev_range must include the commit that ADDED the file: root..HEAD excludes root itself
        _rc, root = run(tmp, "rev-list", "--max-parents=0", "HEAD")
        rev_range = "HEAD"
        paths = LC.blobs_in_range(rev_range)
        ck("range walks every commit, including deleted paths", "session.in" in paths, str(paths))
        code = LC.main(["--tree", root])
        ck("tree check refuses a commit that carried the file", code != 0, f"exit {code}")
        code = LC.main(["--range", rev_range])
        ck("range check refuses a file added then deleted", code != 0, f"exit {code}")
        code = LC.main(["--tree", "HEAD"])
        ck("range is clean once the file is gone", code == 0, f"exit {code}")
    finally:
        LC.ROOT = saved
        shutil.rmtree(tmp, ignore_errors=True)

    # 2. this repository must be publishable as it stands
    code = LC.main(["--tree", "HEAD"])
    ck("this tree has no leaks", code == 0, f"exit {code}")
    marker = "refs/proto2/published-master"
    if subprocess.run(["git", "-C", LC.ROOT, "rev-parse", "--verify", "-q", marker],
                      capture_output=True).returncode == 0:
        code = LC.main(["--range", f"{marker}..HEAD"])
        ck("everything since the last publish has no leaks", code == 0, f"exit {code}")

    if FAILED:
        print(f"\n{len(FAILED)} check(s) failed: {', '.join(FAILED)}")
        return 1
    print("\nleak gate: proofs pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
